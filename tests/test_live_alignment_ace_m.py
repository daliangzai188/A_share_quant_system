"""实盘逐笔对账的ACE+M口径（2026-11-13起）：停手日ACE小仓照常对账、M按重算的候选单对账、
D在停手日/A/C/E有计划/账户里有M/M当天有候选时都不该开仓；拿不到参照就报“没有被检查”。"""
from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.equity_curve_stop import allowed_flags, gated_replay
from src.live_alignment import plan_codes_by_date, run_alignment
from src.review_triggers import CHECK_REPAIR_ACTION, alignment_triggers
from src.strategy_m_reference import MReference

START = "20261113"
LIVE_COLUMNS = ["entry_date", "ts_code", "strategy_leg", "name", "execution_status", "exit_date", "entry_fill_amount"]


def _replay(rows):
    return pd.DataFrame(
        [{"action_date": d, "ts_code": c, "strategy_leg": leg, "position_opened": True} for d, c, leg in rows],
        columns=["action_date", "ts_code", "strategy_leg", "position_opened"],
    )


def _live(rows, columns=LIVE_COLUMNS):
    """rows：(买入日, 代码, 腿, 名称, 平仓状态, 最后卖出日, 买入成交额)，与实盘成交汇总同名的列。"""

    return pd.DataFrame([dict(zip(columns, row)) for row in rows], columns=columns)


def _cand(code, *, name="测试", buy_open=10.5, limit_up=12.0):
    return {"ts_code": code, "name": name, "buy_open": buy_open, "buy_limit_up": limit_up}


class FakeReference:
    """按买入日给出候选单；没登记的日子当作重算失败。"""

    def __init__(self, by_day):
        self.by_day = by_day

    def __call__(self, day):
        if day not in self.by_day:
            return {"status": "ERROR", "error": "ValueError: 涨停池缺失：%s" % day, "candidates": []}
        return {"status": "OK", "error": "", "candidates": self.by_day[day]}


def _keys(triggers):
    return [t.key for t in triggers]


def _check(live, reference, *, replay=None, stops=(), plan_codes=None, ace_m_start=START):
    return alignment_triggers(
        replay if replay is not None else _replay([]), live, stop_dates=set(stops),
        start="20261101", end="20261130", ace_m_start=ace_m_start,
        m_reference=reference, ace_plan_codes=plan_codes,
    )


class AceHaltDayTests(unittest.TestCase):
    def test_halt_day_ace_buy_is_checked_against_ungated_replay_from_ace_m_start(self) -> None:
        replay = _replay([("20261110", "000003.SZ", "A"), ("20261113", "000001.SZ", "A"),
                          ("20261116", "000002.SZ", "C")])
        live = _live([
            ("20261112", "000009.SZ", "E", "甲", "已平仓", "20261113", 50000.0),
            ("20261113", "000001.SZ", "A", "乙", "持仓中", "", 13000.0),
        ])
        stops = {"20261112", "20261113", "20261116"}
        triggers = _check(live, FakeReference({}), replay=replay, stops=stops)
        self.assertEqual(_keys(triggers), [
            "alignment:missing:20261110:000003.SZ",
            "alignment:missing:20261116:000002.SZ",
            "alignment:stop_day:20261112:000009.SZ",   # 生效日之前仍按旧口径：停手日不该开仓
        ])
        self.assertTrue(triggers[0].detail.endswith("（漏单或没成交）"))
        self.assertIn("当天可用额度不够买一手", triggers[1].detail)
        legacy = _check(live, None, replay=replay, stops=stops, ace_m_start="")
        self.assertIn("alignment:stop_day:20261113:000001.SZ", _keys(legacy))


class DLegTests(unittest.TestCase):
    def test_d_not_started_on_halt_ace_plan_m_position_or_m_plan_days(self) -> None:
        replay = _replay([("20261117", "000001.SZ", "A")])
        live = _live([
            ("20261116", "300001.SZ", "D", "d1", "已平仓", "20261117", 30000.0),   # 停手日
            ("20261117", "000001.SZ", "A", "a1", "持仓中", "", 50000.0),
            ("20261117", "300002.SZ", "D", "d2", "已平仓", "20261118", 30000.0),   # A有计划
            ("20261117", "301001.SZ", "M", "m1", "已平仓", "20261118", 40000.0),
            ("20261118", "300003.SZ", "D", "d3", "已平仓", "20261119", 30000.0),   # 账户里还有M
            ("20261119", "301002.SZ", "M", "m2", "已平仓", "20261120", 40000.0),
            ("20261119", "300004.SZ", "D", "d4", "已平仓", "20261120", 30000.0),   # M当天买入
            ("20261123", "300005.SZ", "D", "d5", "已平仓", "20261124", 30000.0),   # M当天有候选
            ("20261124", "300006.SZ", "D", "d6", "已平仓", "20261125", 30000.0),   # 候选单没算出来
            ("20261125", "300007.SZ", "D", "d7", "持仓中", "", 30000.0),           # 可以开
        ])
        reference = FakeReference({
            "20261117": [_cand("301001.SZ")],
            "20261118": [],
            "20261119": [_cand("301002.SZ")],
            "20261123": [_cand("301003.SZ"), _cand("301004.SZ")],
            "20261125": [],
        })
        triggers = _check(live, reference, replay=replay, stops={"20261116"})
        self.assertEqual(_keys(triggers), [
            "alignment:stop_day:20261116:300001.SZ",
            "alignment:d_conflict:20261117:300002.SZ",
            "alignment:d_m_position:20261118:300003.SZ",
            "alignment:d_m_position:20261119:300004.SZ",
            "alignment:d_m_plan:20261123:300005.SZ",
            "alignment:d_unchecked:20261124:300006.SZ",
        ])
        by_key = {t.key: t for t in triggers}
        self.assertIn("301001.SZ", by_key["alignment:d_m_position:20261118:300003.SZ"].detail)
        self.assertIn("301003.SZ、301004.SZ", by_key["alignment:d_m_plan:20261123:300005.SZ"].detail)
        self.assertIn("重算", by_key["alignment:d_m_plan:20261123:300005.SZ"].detail)
        unchecked = by_key["alignment:d_unchecked:20261124:300006.SZ"]
        self.assertEqual(unchecked.category, "检查失效")
        self.assertEqual(unchecked.action, CHECK_REPAIR_ACTION)
        self.assertIn("涨停池缺失", unchecked.detail)


class MLegTests(unittest.TestCase):
    def test_first_buyable_candidate_or_a_reason_to_skip_every_one_ahead(self) -> None:
        ahead = [
            _cand("000101.SZ"),                                  # 方案甲当天计划的股票
            _cand("300102.SZ", buy_open=12.0, limit_up=12.0),    # 开盘就涨停
            _cand("300103.SZ", buy_open=None, limit_up=None),    # 停牌
            _cand("300104.SZ", buy_open=500.0, limit_up=600.0),  # 一手 > 实际成交额
            _cand("300105.SZ", name="*ST测试"),                  # ST
        ]
        live = _live([("20261117", "300106.SZ", "M", "测试", "持仓中", "", 40000.0)])
        plan_codes = {"20261117": {"000101.SZ"}}
        ok = _check(live, FakeReference({"20261117": ahead + [_cand("300106.SZ")]}), plan_codes=plan_codes)
        self.assertEqual(ok, [])

        affordable = list(ahead)
        affordable[3] = _cand("300104.SZ", buy_open=250.0, limit_up=300.0)   # 100×299.99 ≤ 40000
        wrong = _check(live, FakeReference({"20261117": affordable + [_cand("300106.SZ")]}), plan_codes=plan_codes)
        self.assertEqual(_keys(wrong), ["alignment:m_order:20261117:300106.SZ"])
        self.assertIn("候选第6名", wrong[0].detail)
        self.assertIn("300104.SZ", wrong[0].detail)

        unknown_fill = _live([("20261117", "300106.SZ", "M", "测试", "持仓中", "", float("nan"))])
        self.assertEqual(_check(unknown_fill, FakeReference({"20261117": affordable + [_cand("300106.SZ")]}),
                                plan_codes=plan_codes), [], "成交额未知就不能断定钱够买前面那只")

    def test_star_market_lot_is_two_hundred_shares(self) -> None:
        candidates = [_cand("688001.SH", buy_open=45.0, limit_up=50.0), _cand("300106.SZ")]
        cheap = _live([("20261117", "300106.SZ", "M", "测试", "持仓中", "", 9000.0)])
        self.assertEqual(_check(cheap, FakeReference({"20261117": candidates})), [])
        enough = _live([("20261117", "300106.SZ", "M", "测试", "持仓中", "", 10000.0)])
        self.assertEqual(_keys(_check(enough, FakeReference({"20261117": candidates}))),
                         ["alignment:m_order:20261117:300106.SZ"])

    def test_st_extra_unchecked_and_halt_day(self) -> None:
        live = _live([
            ("20261117", "300201.SZ", "M", "ST测试", "已平仓", "20261118", 40000.0),
            ("20261119", "300202.SZ", "M", "测试", "已平仓", "20261120", 40000.0),
            ("20261123", "300203.SZ", "M", "测试", "已平仓", "20261124", 40000.0),
            ("20261125", "300204.SZ", "M", "测试", "持仓中", "", 40000.0),
        ])
        reference = FakeReference({
            "20261117": [_cand("300201.SZ")],
            "20261119": [_cand("300299.SZ")],
            "20261125": [_cand("300204.SZ")],
        })
        triggers = _check(live, reference, stops={"20261125"})
        self.assertEqual(_keys(triggers), [
            "alignment:m_st:20261117:300201.SZ",
            "alignment:m_extra:20261119:300202.SZ",
            "alignment:m_unchecked:20261123:300203.SZ",   # 停手日20261125买M是允许的
        ])
        self.assertIn("300299.SZ", triggers[1].detail)
        self.assertEqual(triggers[2].category, "检查失效")
        self.assertEqual(triggers[2].action, CHECK_REPAIR_ACTION)
        no_reference = _check(live.iloc[[1]], None)
        self.assertEqual(_keys(no_reference), ["alignment:m_unchecked:20261119:300202.SZ"])
        self.assertIn("没有提供M候选单参照", no_reference[0].detail)

    def test_ace_stock_overlap_and_one_per_day(self) -> None:
        live = _live([
            ("20261113", "000001.SZ", "A", "甲", "持仓中", "", 50000.0),
            ("20261116", "000001.SZ", "M", "甲", "已平仓", "20261117", 10000.0),   # 方案甲还持有
            ("20261118", "000002.SZ", "C", "乙", "持仓中", "", 50000.0),
            ("20261118", "000002.SZ", "M", "乙", "已平仓", "20261119", 10000.0),   # 方案甲当天也买了
            ("20261119", "300301.SZ", "M", "丙", "已平仓", "20261120", 10000.0),   # 上一笔M当天才卖
            ("20261124", "300302.SZ", "M", "丁", "持仓中", "", 10000.0),
            ("20261124", "300303.SZ", "M", "戊", "持仓中", "", 10000.0),           # 一天两只
        ])
        replay = _replay([("20261113", "000001.SZ", "A"), ("20261118", "000002.SZ", "C")])
        reference = FakeReference({
            "20261116": [_cand("000001.SZ")],
            "20261118": [_cand("000002.SZ")],
            "20261119": [_cand("300301.SZ")],
            "20261124": [_cand("300302.SZ"), _cand("300303.SZ", buy_open=12.0, limit_up=12.0)],
        })
        self.assertEqual(_keys(_check(live, reference, replay=replay)), [
            "alignment:m_ace_same:20261116:000001.SZ",
            "alignment:m_ace_same:20261118:000002.SZ",
            "alignment:m_overlap:20261119:300301.SZ",
            "alignment:m_multi:20261124",
        ])

    def test_holdings_need_status_and_dates_are_normalised(self) -> None:
        rows = [
            ("20261117", "300401.SZ", "M", "测试", "已平仓", 20261118.0, 10000.0),
            ("20261118", "300402.SZ", "M", "测试", "已平仓", 20261119.0, 10000.0),
            ("20261120", "300403.SZ", "M", "测试", "已平仓", "2026-11-23", 10000.0),
            ("20261123", "300404.SZ", "M", "测试", "持仓中", "", 10000.0),
        ]
        reference = FakeReference({day: [_cand(code)] for day, code, *_rest in rows})
        self.assertEqual(_keys(_check(_live(rows), reference)), [
            "alignment:m_overlap:20261118:300402.SZ",
            "alignment:m_overlap:20261123:300404.SZ",
        ])
        without_status = _live([row[:4] for row in rows], columns=LIVE_COLUMNS[:4])
        self.assertEqual(_check(without_status, reference), [], "没有平仓状态就无法断定持仓，不报警")

    def test_m_before_ace_m_start_is_checked_the_old_way(self) -> None:
        live = _live([("20261112", "301001.SZ", "M", "测试", "已平仓", "20261113", 10000.0)])
        self.assertEqual(_keys(_check(live, FakeReference({}))), ["alignment:extra:20261112:301001.SZ"])


class GatedReplayTests(unittest.TestCase):
    def test_plans_from_ungated_date_stay_on_halt_days(self) -> None:
        dates = pd.bdate_range("2026-01-01", periods=120).strftime("%Y%m%d").tolist()
        values = np.concatenate([np.linspace(1.0, 2.0, 100), np.linspace(2.0, 1.2, 20)])
        halted = [d for d, ok in zip(dates, allowed_flags(values, ma_window=60, lag=6)) if not ok]
        self.assertGreaterEqual(len(halted), 4)
        cut = halted[2]
        plans = pd.DataFrame({"action_date": halted, "ts_code": ["%06d.SZ" % i for i in range(len(halted))]})
        shadow = {"nav": pd.Series(values, index=dates), "legs": {"A": plans, "D": pd.DataFrame()},
                  "action_dates": dates, "execution": {}}
        seen = []

        def fake_replay(legs, *, action_dates, priority, **_execution):
            seen.append(sorted(legs["A"]["action_date"]))
            return pd.DataFrame()

        with patch("src.acde_rolling_framework.replay_action_date_cash_portfolio", fake_replay):
            gated_replay(shadow, ma_window=60, lag=6)
            gated_replay(shadow, ma_window=60, lag=6, ungated_from=cut)
        self.assertEqual(seen, [[], [d for d in halted if d >= cut]])


class RunAlignmentTests(unittest.TestCase):
    def test_plan_codes_cover_every_leg_in_window(self) -> None:
        legs = {
            "A": pd.DataFrame({"action_date": ["20261112", "20261113"], "ts_code": ["000001.SZ", "000002.SZ"]}),
            "C": pd.DataFrame({"action_date": ["20261113"], "ts_code": ["000003.SZ"]}),
            "D": pd.DataFrame(),
        }
        self.assertEqual(plan_codes_by_date(legs, "20261113", "20261130"),
                         {"20261113": {"000002.SZ", "000003.SZ"}})

    def test_wires_ungated_replay_halt_days_reference_and_plan_codes(self) -> None:
        dates = pd.bdate_range("2026-01-01", periods=120).strftime("%Y%m%d").tolist()
        values = np.concatenate([np.linspace(1.0, 2.0, 100), np.linspace(2.0, 1.2, 20)])
        halted = {d for d, ok in zip(dates, allowed_flags(values, ma_window=60, lag=6)) if not ok}
        cut = sorted(halted)[2]
        shadow = {"nav": pd.Series(values, index=dates), "action_dates": dates, "execution": {},
                  "legs": {"A": pd.DataFrame({"action_date": [cut], "ts_code": ["000001.SZ"]}), "D": pd.DataFrame()}}
        seen = []

        def fake_gated(_shadow, *, ma_window, lag, ungated_from=""):
            seen.append(ungated_from)
            return _replay([])

        def fake_triggers(_replay_frame, _live_frame, **kwargs):
            seen.append(kwargs)
            return []

        with patch("src.live_alignment.gated_replay", fake_gated), \
                patch("src.live_alignment.alignment_triggers", fake_triggers):
            run_alignment(shadow, _live([]), ma_window=60, lag=6, start="20260101", end=dates[-1],
                          ace_m_start=cut, raw_dir="raw")
            run_alignment(shadow, _live([]), ma_window=60, lag=6, start="20260101", end=dates[-1])
        self.assertEqual(seen[0], cut)
        self.assertEqual(seen[1]["stop_dates"], halted)
        self.assertEqual(seen[1]["ace_m_start"], cut)
        self.assertIsInstance(seen[1]["m_reference"], MReference)
        self.assertEqual(seen[1]["ace_plan_codes"], {cut: {"000001.SZ"}})
        self.assertEqual(seen[2], "")
        self.assertIsNone(seen[3]["m_reference"])
        self.assertIsNone(seen[3]["ace_plan_codes"])


def _daily_frame(n, overrides=None):
    rows = [{"ts_code": "%06d.SZ" % (1000 + i), "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0,
             "pre_close": 10.0, "amount": 50_000.0, "vol": 1000.0} for i in range(n)]
    frame = pd.DataFrame(rows)
    for code, values in (overrides or {}).items():
        if code in set(frame.ts_code):
            for key, value in values.items():
                frame.loc[frame.ts_code == code, key] = value
        else:
            frame = pd.concat([frame, pd.DataFrame([dict({"ts_code": code}, **values)])], ignore_index=True)
    return frame


def _write_m_raw(root: Path) -> tuple[Path, str, str]:
    """M信号日T有两只20cm强势涨停（300100在stock_basic里；300300不在，上市日未知）和一只一字板，
    T-1有一只主板涨停；返回（原始数据目录，信号日，买入日）。"""

    start = datetime.date(2025, 9, 1)
    dates = [(start + datetime.timedelta(days=i)).strftime("%Y%m%d") for i in range(91)]
    signal, buy = dates[-2], dates[-1]
    raw = root / "data" / "raw"
    for sub in ("daily", "daily_basic", "limit_list", "stock_basic"):
        (raw / sub).mkdir(parents=True)
    pd.DataFrame({"exchange": "SSE", "cal_date": list(reversed(dates)), "is_open": 1}).to_csv(
        raw / "trade_calendar.csv", index=False)
    base = {"open": 10.0, "high": 10.0, "low": 9.0, "close": 10.0, "pre_close": 10.0, "amount": 80_000.0, "vol": 1000.0}
    window = dates[-21:-1]
    strong = ("300100.SZ", "300200.SZ", "300300.SZ")
    for day in window:
        overrides = {code: dict(base) for code in strong + ("000900.SZ",)}
        if day in window[-6:-1]:
            for code in strong:
                overrides[code].update(close=10.4, pre_close=10.0)
        if day == window[-2]:
            overrides["000900.SZ"].update(close=11.0, pre_close=10.0, high=11.0)
        if day == signal:
            overrides["300100.SZ"].update(open=10.5, high=12.0, low=10.4, close=12.0, pre_close=10.0, amount=120_000.0)
            overrides["300300.SZ"].update(open=10.5, high=12.0, low=10.4, close=12.0, pre_close=10.0, amount=130_000.0)
            overrides["300200.SZ"].update(open=12.0, high=12.0, low=12.0, close=12.0, pre_close=10.0, amount=150_000.0)
            overrides["000900.SZ"].update(close=11.11, pre_close=11.0)
        _daily_frame(1100, overrides).to_csv(raw / "daily" / ("%s.csv" % day), index=False)
    _daily_frame(1100, {"300100.SZ": dict(base, open=12.5, pre_close=12.0, close=13.0, high=13.2, low=12.4)}).to_csv(
        raw / "daily" / ("%s.csv" % buy), index=False)
    codes = list(_daily_frame(1100, {code: dict(base) for code in strong + ("000900.SZ",)}).ts_code)
    pd.DataFrame({"ts_code": codes, "circ_mv": 100_000.0}).to_csv(raw / "daily_basic" / ("%s.csv" % signal), index=False)
    pd.DataFrame([
        {"ts_code": "300100.SZ", "name": "测试一", "fd_amount": 1_000.0, "limit": "U"},
        {"ts_code": "300200.SZ", "name": "测试二", "fd_amount": 0.0, "limit": "U"},
        {"ts_code": "300300.SZ", "name": "测试三", "fd_amount": 0.0, "limit": "U"},
    ]).to_csv(raw / "limit_list" / ("%s.csv" % signal), index=False)
    pd.DataFrame([{"ts_code": "000900.SZ", "name": "测试四", "fd_amount": 0.0, "limit": "U"}]).to_csv(
        raw / "limit_list" / ("%s.csv" % window[-2]), index=False)
    listed = [code for code in codes if code != "300300.SZ"]
    pd.DataFrame({"ts_code": listed, "name": "测试", "list_date": "20200101"}).to_csv(
        raw / "stock_basic" / "stock_basic_all.csv", index=False)
    return raw, signal, buy


class MReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raw, self.signal, self.buy = _write_m_raw(Path(self.tmp.name))

    def test_same_candidates_as_live_signal_script(self) -> None:
        import scripts.run_strategy_m_signal as S

        out = Path(self.tmp.name) / "reports" / "strategy_m"
        with patch.object(S, "RAW", self.raw), patch.object(S, "CALENDAR_PATH", self.raw / "trade_calendar.csv"), \
                patch.object(S, "STOCK_BASIC_PATH", self.raw / "stock_basic" / "stock_basic_all.csv"), \
                patch.object(S, "OUTPUT_DIR", out), \
                patch.object(S, "ROLLING_SIGNAL_PATH", out / "m_signals_recent.json"), \
                patch.object(S, "RUN_STATUS_PATH", out / "m_signal_runs_recent.json"):
            self.assertEqual(S.run(self.signal), 0)
        live = pd.read_csv(out / ("m_signal_%s_candidates.csv" % self.signal), dtype={"ts_code": str})
        reference = MReference(self.raw)(self.buy)
        self.assertEqual(reference["status"], "OK", reference["error"])
        self.assertEqual(reference["signal_date"], self.signal)
        self.assertEqual([(c["rank"], c["ts_code"]) for c in reference["candidates"]],
                         list(zip(live["rank"].astype(int), live["ts_code"])))
        self.assertEqual(list(live["ts_code"]), ["300100.SZ"], "一字板剔除；上市日未知的不进股票池")
        first = reference["candidates"][0]
        self.assertEqual((first["name"], first["buy_open"], first["buy_pre_close"], first["buy_limit_up"]),
                         ("测试一", 12.5, 12.0, 14.4))

    def test_suspended_candidate_has_no_buy_prices(self) -> None:
        frame = pd.read_csv(self.raw / "daily" / ("%s.csv" % self.buy), dtype={"ts_code": str})
        frame[frame.ts_code != "300100.SZ"].to_csv(self.raw / "daily" / ("%s.csv" % self.buy), index=False)
        first = MReference(self.raw)(self.buy)["candidates"][0]
        self.assertEqual((first["ts_code"], first["buy_open"], first["buy_limit_up"]), ("300100.SZ", None, None))

    def test_missing_data_is_an_error_not_an_exception(self) -> None:
        reference = MReference(self.raw)
        self.assertIn("不是交易日", reference("20250101")["error"])
        (self.raw / "daily" / ("%s.csv" % self.buy)).unlink()
        self.assertIn("买入日日线缺失", reference(self.buy)["error"])
        (self.raw / "limit_list" / ("%s.csv" % self.signal)).unlink()
        fresh = MReference(self.raw)(self.buy)
        self.assertEqual(fresh["status"], "ERROR")
        self.assertIn("涨停池缺失", fresh["error"])
        self.assertEqual(MReference(Path(self.tmp.name) / "none")(self.buy)["status"], "ERROR")


if __name__ == "__main__":
    unittest.main()
