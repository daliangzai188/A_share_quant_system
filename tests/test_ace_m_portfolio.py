"""ACE + M（2026-11-12）：资金规则、方案甲停手期小仓、M 并行持仓、M 选股与执行路径。"""
from __future__ import annotations

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pandas as pd

if "dotenv" not in sys.modules:
    stub = ModuleType("dotenv")
    stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = stub

from scripts import trading_daemon as td
from tests.daemon_testing import patch_daemon
from src import portfolio_ace_m as PM
from src import strategy_m as SM
from src.equity_curve_stop import METHOD_ID, live_gate

from tests.test_equity_curve_stop import make_engine


class PortfolioRuleTests(unittest.TestCase):
    def test_ace_limits_full_reduced_and_m_exposure(self) -> None:
        full = PM.ace_limits(PM.GATE_FULL, 100_000, 0)
        self.assertAlmostEqual(full["target_pct"], 0.825)
        self.assertAlmostEqual(full["hard_pct"], 0.85)
        reduced = PM.ace_limits(PM.GATE_REDUCED, 100_000, 0)
        self.assertAlmostEqual(reduced["target_pct"], 0.20)
        self.assertAlmostEqual(reduced["hard_pct"], 0.225)
        # M 用 82.5% 持仓时，ACE 只剩 99.5% − 82.5% = 17%
        squeezed = PM.ace_limits(PM.GATE_FULL, 100_000, 82_500)
        self.assertAlmostEqual(squeezed["target_pct"], 0.17)
        self.assertAlmostEqual(squeezed["hard_pct"], 0.17)
        # M 用闲置 17.5% 持仓时，ACE 目标 82%（研究回测 min(0.825, 0.995−0.175)）
        beside = PM.ace_limits(PM.GATE_FULL, 100_000, 17_500)
        self.assertAlmostEqual(beside["target_pct"], 0.82)
        blocked = PM.ace_limits(PM.GATE_BLOCKED, 100_000, 0)
        self.assertEqual(blocked["target_pct"], 0.0)
        self.assertEqual(blocked["hard_pct"], 0.0)

    def test_m_budget_idle_and_beside_ace(self) -> None:
        idle = PM.m_budget(total_asset=100_000, available_cash=100_000, ace_active_today=False,
                           ace_value=0, ace_unfilled_target=0, cash_buffer=1000)
        self.assertAlmostEqual(idle["amount"], 82_500)
        beside = PM.m_budget(total_asset=100_000, available_cash=100_000, ace_active_today=True,
                             ace_value=0, ace_unfilled_target=82_500, cash_buffer=1000)
        # 17.5%、99.5%−ACE目标(17,000)、现金−缓冲−ACE预留(16,500) 取小
        self.assertAlmostEqual(beside["amount"], 16_500)
        holding = PM.m_budget(total_asset=100_000, available_cash=17_600, ace_active_today=True,
                              ace_value=82_400, ace_unfilled_target=0, cash_buffer=1000)
        self.assertAlmostEqual(holding["amount"], 16_600)

    def test_gate_mode_mapping(self) -> None:
        self.assertEqual(PM.ace_gate_mode(SimpleNamespace(allowed=True)), PM.GATE_FULL)
        self.assertEqual(PM.ace_gate_mode(SimpleNamespace(allowed=False, halt_valid=True)), PM.GATE_REDUCED)
        self.assertEqual(PM.ace_gate_mode(SimpleNamespace(allowed=False)), PM.GATE_BLOCKED)

    def test_family_filter(self) -> None:
        m = {"strategy_leg": "M"}
        a = {"strategy_leg": "A"}
        self.assertTrue(PM.position_in_family(m, "M"))
        self.assertFalse(PM.position_in_family(m, "ACE"))
        self.assertTrue(PM.position_in_family(a, "ACE"))
        self.assertTrue(PM.position_in_family(a, None))


class LiveGateHaltValidTests(unittest.TestCase):
    def test_only_valid_explicit_halt_is_reduced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"equity_curve_stop": {"enabled": True, "ma_window": 60, "lag_trading_days": 6,
                                            "decision_path": "d.json"}}
            path = root / "d.json"
            self.assertFalse(live_gate(config, root, "20260921").halt_valid)   # 判定缺失 → fail-closed
            payload = {"method_id": METHOD_ID, "action_date": "20260921", "ma_window": 60, "lag_trading_days": 6,
                       "allowed": False, "reason": "影子净值低于60日均线，方案甲停手"}
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            check = live_gate(config, root, "20260921")
            self.assertFalse(check.allowed)
            self.assertTrue(check.halt_valid)
            self.assertEqual(PM.ace_gate_mode(check), PM.GATE_REDUCED)
            payload["action_date"] = "20260918"   # 过期 → fail-closed
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(PM.ace_gate_mode(live_gate(config, root, "20260921")), PM.GATE_BLOCKED)


class CombinedEngineAceMTests(unittest.TestCase):
    def _engine(self, *, allowed: bool, halt_valid: bool = False, positions=None, ac_leg=None):
        engine = make_engine(allowed=allowed, positions=positions, ac_leg=ac_leg)
        reason = "影子净值低于60日均线，方案甲停手" if not allowed else "正常开仓"
        engine.equity_curve_stop_check = lambda _today: SimpleNamespace(
            allowed=allowed, reason=reason, source="test", halt_valid=halt_valid)
        engine.has_m_plan_today = lambda _today: False
        return engine

    def test_m_plan_today_blocks_d(self) -> None:
        engine = self._engine(allowed=True)
        engine.has_m_plan_today = lambda _today: True
        _state, decisions, _orders = engine.build_mode1_plan("20260921")
        self.assertNotIn("ALLOW_D_INTRADAY_MONITOR", set(decisions["action"]))

    def test_valid_halt_keeps_a_plan_at_reduced_target(self) -> None:
        engine = self._engine(allowed=False, halt_valid=True, ac_leg="A")
        state, decisions, orders = engine.build_mode1_plan("20260921")
        actions = set(decisions["action"])
        self.assertIn("ALLOW_ABC_BUY_PREVIEW", actions)
        self.assertIn("ACE_REDUCED_POSITION", actions)
        self.assertIn("BLOCK_D_INTRADAY_MONITOR", actions)
        self.assertFalse(orders.empty)
        self.assertEqual(state.iloc[0]["ace_gate_mode"], PM.GATE_REDUCED)
        self.assertAlmostEqual(float(state.iloc[0]["ace_mode_target_pct"]), 0.20)

    def test_valid_halt_without_plan_does_not_start_d(self) -> None:
        engine = self._engine(allowed=False, halt_valid=True)
        _state, decisions, _orders = engine.build_mode1_plan("20260921")
        self.assertNotIn("ALLOW_D_INTRADAY_MONITOR", set(decisions["action"]))
        self.assertIn("BLOCK_D_INTRADAY_MONITOR", set(decisions["action"]))

    def test_fail_closed_gate_still_blocks_everything(self) -> None:
        engine = self._engine(allowed=False, halt_valid=False, ac_leg="A")
        _state, decisions, orders = engine.build_mode1_plan("20260921")
        self.assertIn("BLOCK_ABC_BUY", set(decisions["action"]))
        self.assertTrue(orders.empty)

    def test_m_position_does_not_block_ace_but_blocks_d(self) -> None:
        m_pos = {"strategy_leg": "M", "status": "open", "ts_code": "300001.SZ", "planned_exit_date": "20260921",
                 "shares": 100}
        engine = self._engine(allowed=True, positions=[m_pos], ac_leg="A")
        _state, decisions, _orders = engine.build_mode1_plan("20260921")
        self.assertIn("ALLOW_ABC_BUY_PREVIEW", set(decisions["action"]))
        engine = self._engine(allowed=True, positions=[m_pos])
        _state, decisions, _orders = engine.build_mode1_plan("20260921")
        self.assertNotIn("ALLOW_D_INTRADAY_MONITOR", set(decisions["action"]))
        self.assertTrue(any("M持仓" in r for r in decisions["reason"]))

    def test_ace_position_still_blocks_new_ace(self) -> None:
        a_pos = {"strategy_leg": "A", "status": "open", "ts_code": "000001.SZ", "planned_exit_date": "20260922",
                 "shares": 100}
        engine = self._engine(allowed=True, positions=[a_pos], ac_leg="C")
        _state, decisions, _orders = engine.build_mode1_plan("20260921")
        self.assertIn("BLOCK_ABC_BUY", set(decisions["action"]))


class DaemonFamilyScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.positions = [
            {"strategy_leg": "M", "status": "open", "ts_code": "300001.SZ", "buy_date": "20260918", "shares": 100,
             "buy_price": 10.0},
            {"strategy_leg": "A", "status": "closed", "ts_code": "000001.SZ", "buy_date": "20260917", "shares": 100},
        ]

    def test_m_position_invisible_to_ace_guards(self) -> None:
        broker = [SimpleNamespace(ts_code="300001.SZ", volume=100, market_value=1000.0)]
        with patch_daemon("load_positions", lambda: list(self.positions)):
            self.assertTrue(td.has_open_local_position())
            self.assertTrue(td.has_open_local_position("M"))
            self.assertFalse(td.has_open_local_position("ACE"))
            self.assertFalse(td._broker_has_preexisting_strategy_position(broker, "20260921"))
            self.assertTrue(td._broker_has_preexisting_strategy_position(broker, "20260921", family="M"))
            self.assertEqual(td._strategy_only_market_value(broker, family="ACE"), 0.0)
            self.assertEqual(td._strategy_only_market_value(broker, family="M"), 1000.0)
            self.assertEqual(td._strategy_only_market_value(broker), 1000.0)

    def test_m_exits_at_close_like_other_t2_legs(self) -> None:
        self.assertIn("M", td.T2_CLOSE_STRATEGY_LEGS)
        self.assertIn("M", td._ACTIVE_AUTO_STRATEGY_LEGS)

    def _resize(self, mode: str, m_value: float, market_value: float = 0.0):
        orders = pd.DataFrame([{"side": "BUY", "ts_code": "000001.SZ", "strategy_leg": "A", "signal_date": "20260918",
                                "reference_price": 10.0, "round_lot_shares": 100, "estimated_shares": 100}])
        account = SimpleNamespace(total_asset=100_000.0, available_cash=100_000.0)
        with patch_daemon("_ace_gate_mode_for", lambda _d="": mode), \
                patch_daemon("_signal_day_amount", lambda *_a: 1e12), \
                patch_daemon("load_positions", lambda: []):
            return td.resize_buy_orders_for_live_account(orders, account, {}, market_value, log_adjust=False,
                                                         m_market_value=m_value).iloc[0]

    def test_resize_full_reduced_and_beside_m(self) -> None:
        full = self._resize(PM.GATE_FULL, 0.0)
        self.assertAlmostEqual(full["live_target_amount"], 82_500.0)
        self.assertAlmostEqual(full["live_hard_cap_amount"], 85_000.0)
        reduced = self._resize(PM.GATE_REDUCED, 0.0)
        self.assertAlmostEqual(reduced["live_target_amount"], 20_000.0)
        self.assertAlmostEqual(reduced["live_min_acceptable_amount"], 20_000.0 * 0.80 / 0.825, places=4)
        squeezed = self._resize(PM.GATE_FULL, 82_500.0)
        self.assertAlmostEqual(squeezed["live_target_amount"], 17_000.0)
        blocked = self._resize(PM.GATE_BLOCKED, 0.0)
        self.assertEqual(int(blocked["round_lot_shares"]), 0)


def _daily_frame(n: int, overrides: dict[str, dict[str, float]] | None = None) -> pd.DataFrame:
    rows = []
    for i in range(n):
        code = "%06d.SZ" % (1000 + i)
        rows.append({"ts_code": code, "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0, "pre_close": 10.0,
                     "amount": 50_000.0, "vol": 1000.0})
    frame = pd.DataFrame(rows)
    for code, values in (overrides or {}).items():
        if code in set(frame.ts_code):
            for k, v in values.items():
                frame.loc[frame.ts_code == code, k] = v
        else:
            frame = pd.concat([frame, pd.DataFrame([dict({"ts_code": code}, **values)])], ignore_index=True)
    return frame


class StrategyMRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        # 90 个交易日的日历（上市满60个交易日的判断需要足够长的日历），日线只给最后20天
        start = datetime.date(2025, 9, 1)
        self.dates = [(start + datetime.timedelta(days=i)).strftime("%Y%m%d") for i in range(90)]
        self.t = self.dates[-1]
        self.list_dates = {}
        base = {"open": 10.0, "high": 10.0, "low": 9.0, "close": 10.0, "pre_close": 10.0, "amount": 80_000.0,
                "vol": 1000.0}
        self.daily = {}
        for i, d in enumerate(self.dates[-20:]):
            ov = {"300100.SZ": dict(base), "300200.SZ": dict(base), "000900.SZ": dict(base)}
            if d in self.dates[-6:-1]:      # T-5..T-1: 每天 +4% → 5日约 +21.7%
                ov["300100.SZ"].update(close=10.4, pre_close=10.0)
                ov["300200.SZ"].update(close=10.4, pre_close=10.0)
            if d == self.dates[-2]:         # T-1：000900 主板涨停（昨日涨停股）
                ov["000900.SZ"].update(close=11.0, pre_close=10.0, high=11.0)
            if d == self.t:
                ov["300100.SZ"].update(open=10.5, high=12.0, low=10.4, close=12.0, pre_close=10.0, amount=120_000.0)
                ov["300200.SZ"].update(open=12.0, high=12.0, low=12.0, close=12.0, pre_close=10.0, amount=150_000.0)
                ov["000900.SZ"].update(close=11.11, pre_close=11.0)   # 昨日涨停股今日 +1%
            self.daily[d] = _daily_frame(1100, ov)
        for code in list(self.daily[self.t].ts_code):
            self.list_dates[code] = "20200101"

    def _run(self, fd_amount: float = 1_000.0):
        limit_today = pd.DataFrame([{"ts_code": "300100.SZ", "fd_amount": fd_amount},
                                    {"ts_code": "300200.SZ", "fd_amount": 0.0}])
        basic = pd.DataFrame([{"ts_code": "300100.SZ", "circ_mv": 100_000.0},
                              {"ts_code": "300200.SZ", "circ_mv": 100_000.0}])
        return SM.build_m_candidates(self.t, self.dates, self.daily, limit_today, basic, self.list_dates,
                                     lambda code, day: False)

    def test_selects_strong_20cm_and_rejects_one_word(self) -> None:
        cands, diag = self._run()
        self.assertTrue(diag["mood_ok"])
        self.assertEqual(diag["n20_limit_up"], 2)
        self.assertEqual(list(cands.ts_code), ["300100.SZ"])     # 300200 一字板被剔除
        self.assertEqual(diag["rejects"].get("one_word"), 1)

    def test_huge_seal_rejected(self) -> None:
        cands, diag = self._run(fd_amount=0.06 * 100_000.0 * 1e4)
        self.assertTrue(cands.empty)
        self.assertEqual(diag["rejects"].get("fd_ratio"), 1)

    def test_st_and_young_stocks_excluded(self) -> None:
        limit_today = pd.DataFrame([{"ts_code": "300100.SZ", "fd_amount": 0.0}])
        basic = pd.DataFrame([{"ts_code": "300100.SZ", "circ_mv": 100_000.0}])
        st_all = SM.build_m_candidates(self.t, self.dates, self.daily, limit_today, basic, self.list_dates,
                                       lambda code, day: code == "300100.SZ")[0]
        self.assertTrue(st_all.empty)
        young = dict(self.list_dates, **{"300100.SZ": self.dates[40]})
        self.assertTrue(SM.build_m_candidates(self.t, self.dates, self.daily, limit_today, basic, young,
                                              lambda c, d: False)[0].empty)

    def test_missing_daily_fails_closed(self) -> None:
        daily = dict(self.daily)
        daily.pop(self.dates[-3])
        with self.assertRaises(ValueError):
            SM.build_m_candidates(self.t, self.dates, daily, None, None, self.list_dates, lambda c, d: False)


class MSignalScriptGuardTests(unittest.TestCase):
    """收盘脚本的数据完整性门禁：涨停池股票缺日线 → ERROR（M次日不开仓），不改变选股规则。"""

    def _setup(self, root: Path, missing_in_yday: bool) -> str:
        import scripts.run_strategy_m_signal as S

        start = datetime.date(2025, 9, 1)
        dates = [(start + datetime.timedelta(days=i)).strftime("%Y%m%d") for i in range(90)]
        raw = root / "data" / "raw"
        (raw / "daily").mkdir(parents=True)
        (raw / "daily_basic").mkdir(parents=True)
        (raw / "limit_list").mkdir(parents=True)
        (raw / "stock_basic").mkdir(parents=True)
        pd.DataFrame({"exchange": "SSE", "cal_date": dates, "is_open": 1}).to_csv(raw / "trade_calendar.csv", index=False)
        frame = _daily_frame(1100)
        for d in dates[-20:]:
            out = frame if not (missing_in_yday and d == dates[-2]) else frame[frame.ts_code != "001000.SZ"]
            out.to_csv(raw / "daily" / f"{d}.csv", index=False)
        pd.DataFrame({"ts_code": frame.ts_code, "circ_mv": 100_000.0}).to_csv(raw / "daily_basic" / f"{dates[-1]}.csv", index=False)
        for d in dates[-2:]:
            pd.DataFrame([{"ts_code": "001000.SZ", "name": "测试", "fd_amount": 0.0, "limit": "U"}]).to_csv(
                raw / "limit_list" / f"{d}.csv", index=False)
        pd.DataFrame({"ts_code": frame.ts_code, "name": "测试", "list_date": "20200101"}).to_csv(
            raw / "stock_basic" / "stock_basic_all.csv", index=False)
        return dates[-1]

    def _run(self, missing_in_yday: bool) -> int:
        import scripts.run_strategy_m_signal as S

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = self._setup(root, missing_in_yday)
            raw = root / "data" / "raw"
            out = root / "reports" / "strategy_m"
            with patch.object(S, "RAW", raw), patch.object(S, "CALENDAR_PATH", raw / "trade_calendar.csv"), \
                    patch.object(S, "STOCK_BASIC_PATH", raw / "stock_basic" / "stock_basic_all.csv"), \
                    patch.object(S, "OUTPUT_DIR", out), \
                    patch.object(S, "ROLLING_SIGNAL_PATH", out / "m_signals_recent.json"), \
                    patch.object(S, "RUN_STATUS_PATH", out / "m_signal_runs_recent.json"):
                rc = S.run(day)
                runs = json.loads((out / "m_signal_runs_recent.json").read_text(encoding="utf-8"))["runs"]
            self.last_status = runs[-1]["status"]
            return rc

    def test_complete_data_runs(self) -> None:
        self.assertEqual(self._run(False), 0)
        self.assertIn(self.last_status, {"SIGNAL_READY", "NO_CANDIDATE"})

    def test_limit_up_stock_missing_from_daily_fails_closed(self) -> None:
        self.assertEqual(self._run(True), 1)
        self.assertEqual(self.last_status, "ERROR")


class FakeAdapter:
    def __init__(self, quotes: dict[str, SimpleNamespace], fills: dict[str, list]):
        self.quotes = quotes
        self.fills = fills          # order_id -> [OrderFill-like]
        self.orders: list = []
        self.cancelled: list[str] = []

    def query_account(self):
        return SimpleNamespace(total_asset=100_000.0, available_cash=100_000.0)

    def query_positions(self):
        return []

    def get_full_tick(self, codes):
        return {c: self.quotes[c] for c in codes if c in self.quotes}

    def place_order(self, request):
        self.orders.append(request)
        return SimpleNamespace(accepted=True, order_id="oid%d" % len(self.orders), message="")

    def get_order_fill(self, order_id):
        seq = self.fills.get(order_id) or [SimpleNamespace(filled_qty=0, avg_price=0.0, is_terminal=False)]
        return seq[0] if len(seq) == 1 else seq.pop(0)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True


class MExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "m_execution_state.json"
        self.signal = {"signal_date": "20260918", "planned_buy_date": "20260921", "status": "SIGNAL_READY",
                       "m_version": SM.M_VERSION,
                       "candidates": [{"rank": 1, "ts_code": "300100.SZ", "name": "测试一"},
                                      {"rank": 2, "ts_code": "300200.SZ", "name": "测试二"}]}
        self.recorded: list[dict] = []
        self.config = {"trade_mode": "live", "broker_adapter_enabled": True, "qmt_enabled": True,
                       "broker": {}, "live_trade": {"cash_buffer_amount": 1000, "pov_chase_cap": 0.02,
                                                    "pov_participation": 0.10}}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patches(self, adapter, *, now: datetime.datetime, decisions=None, positions=None):
        tz = td.BEIJING_TZ
        return [
            patch_daemon("M_EXEC_STATE_PATH", self.state_path),
            patch_daemon("load_json_config", lambda _p: self.config),
            patch_daemon("_qmt_get", lambda _cfg: adapter),
            patch_daemon("_m_signal_for_today", lambda _d: self.signal),
            patch_daemon("read_cached_combined_decisions",
                         lambda: (decisions, None) if decisions is not None else None),
            patch_daemon("load_positions", lambda: list(positions or [])),
            patch_daemon("now_beijing", lambda: now.replace(tzinfo=tz)),
            patch_daemon("today_beijing", lambda: now.date()),
            patch_daemon("_ace_gate_mode_for", lambda _d="": PM.GATE_FULL),
            patch_daemon("_m_exit_date", lambda _d: "20260922"),
            patch_daemon("_notify", lambda *a, **k: None),
            patch_daemon("_record_live_buy", lambda **kw: self.recorded.append(kw)),
            patch_daemon("_m_start_pov_worker", lambda: None),
            patch("src.live_order_gateway.LiveOrderGateway.assert_real_order_allowed", lambda *a, **k: None),
        ]

    def _run(self, fn, adapter, **kw):
        ps = self._patches(adapter, **kw)
        for p in ps:
            p.start()
        try:
            fn()
        finally:
            for p in reversed(ps):
                p.stop()

    def _quote(self, *, open_price=0.0, last=0.0, upper=12.0):
        return SimpleNamespace(open_price=open_price, last_price=last, upper_limit=upper, pre_close=10.0,
                               suspended=False, amount=5e7, ask_prices=[last] if last else None)

    def test_auction_preplace_idle_ace_uses_825(self) -> None:
        adapter = FakeAdapter({"300100.SZ": self._quote(), "300200.SZ": self._quote()}, {})
        self._run(td.job_m_premarket_buy, adapter, now=datetime.datetime(2026, 9, 21, 9, 20, 30))
        self.assertEqual(len(adapter.orders), 1)
        order = adapter.orders[0]
        self.assertEqual(order.ts_code, "300100.SZ")
        self.assertEqual(order.strategy_leg, "M")
        self.assertAlmostEqual(order.price, 11.99)                 # 涨停价-0.01
        self.assertEqual(order.quantity, 6800)                     # 82,500 / 11.99 → 6800股
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertFalse(state["items"][0]["ace_active"])

    def test_auction_beside_ace_uses_idle_cash_and_skips_ace_stock(self) -> None:
        decisions = pd.DataFrame([{"action": "ALLOW_ABC_BUY_PREVIEW", "strategy_leg": "A", "side": "BUY",
                                   "ts_code": "300100.SZ"}])
        adapter = FakeAdapter({"300100.SZ": self._quote(), "300200.SZ": self._quote()}, {})
        self._run(td.job_m_premarket_buy, adapter, now=datetime.datetime(2026, 9, 21, 9, 20, 30),
                  decisions=decisions)
        self.assertEqual(len(adapter.orders), 1)
        order = adapter.orders[0]
        self.assertEqual(order.ts_code, "300200.SZ")              # 300100 与 ACE 计划同票，跳过
        self.assertLessEqual(order.quantity * order.price, 17_000.0 + 1e-6)

    def test_open_at_limit_moves_to_next_candidate(self) -> None:
        # 竞价预挂 300100，零成交；开盘涨停 → 撤单，竞价后改选 300200（开盘 10.8 < 涨停 12）
        adapter = FakeAdapter({"300100.SZ": self._quote(open_price=12.0, last=12.0),
                               "300200.SZ": self._quote(open_price=10.8, last=10.8)}, {})
        self._run(td.job_m_premarket_buy, adapter, now=datetime.datetime(2026, 9, 21, 9, 20, 30))
        self._run(td.job_m_after_auction, adapter, now=datetime.datetime(2026, 9, 21, 9, 26, 5))
        self.assertIn("oid1", adapter.cancelled)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["items"][0]["stage"], "select")
        ps = self._patches(adapter, now=datetime.datetime(2026, 9, 21, 9, 30, 30))
        for p in ps:
            p.start()
        try:
            with patch_daemon("_confirm_fill",
                              lambda *a, **k: SimpleNamespace(filled_qty=a[2], avg_price=10.82, is_terminal=True)):
                st = td._m_load_state()
                td._m_pov_round(st, {}, self.config, "20260921")
        finally:
            for p in reversed(ps):
                p.stop()
        item = st["items"][0]
        self.assertEqual(item["ts_code"], "300200.SZ")
        self.assertGreater(int(item["filled_qty"]), 0)
        self.assertLessEqual(adapter.orders[-1].price, round(10.8 * 1.02, 2) + 1e-9)

    def test_auction_fill_recorded_with_t2_exit(self) -> None:
        adapter = FakeAdapter({"300100.SZ": self._quote(open_price=10.5, last=10.5), "300200.SZ": self._quote()},
                              {"oid1": [SimpleNamespace(filled_qty=6800, avg_price=10.5, is_terminal=True)]})
        self._run(td.job_m_premarket_buy, adapter, now=datetime.datetime(2026, 9, 21, 9, 20, 30))
        self._run(td.job_m_after_auction, adapter, now=datetime.datetime(2026, 9, 21, 9, 26, 5))
        self.assertEqual(len(self.recorded), 1)
        rec = self.recorded[0]
        self.assertEqual(rec["strategy_leg"], "M")
        self.assertEqual(rec["shares"], 6800)
        self.assertEqual(rec["planned_exit_date_override"], "20260922")
        self.assertEqual(adapter.cancelled, [])

    def test_pov_after_auction_records_only_pov_slices(self) -> None:
        """回归（演练发现）：POV收口记账不得把竞价单号并入，否则组合意图成交量超过聚合持仓。"""
        adapter = FakeAdapter({"300100.SZ": self._quote(open_price=8.68, last=8.68, upper=9.48),
                               "300200.SZ": self._quote()},
                              {"oid1": [SimpleNamespace(filled_qty=1200, avg_price=8.68, is_terminal=True)]})
        self._run(td.job_m_premarket_buy, adapter, now=datetime.datetime(2026, 9, 21, 9, 20, 30))
        self._run(td.job_m_after_auction, adapter, now=datetime.datetime(2026, 9, 21, 9, 26, 5))
        ps = self._patches(adapter, now=datetime.datetime(2026, 9, 21, 9, 30, 30))
        for p in ps:
            p.start()
        try:
            with patch_daemon("_confirm_fill",
                              lambda *a, **k: SimpleNamespace(filled_qty=a[2], avg_price=8.68, is_terminal=True)):
                st = td._m_load_state()
                td._m_pov_round(st, {}, self.config, "20260921")
                td._m_pov_finalize(st, "目标金额已买足")
        finally:
            for p in reversed(ps):
                p.stop()
        self.assertEqual(len(self.recorded), 2)
        auction_rec, pov_rec = self.recorded
        self.assertEqual(auction_rec["order_id"], "oid1")
        self.assertNotIn("oid1", pov_rec["component_order_ids"])
        self.assertEqual(pov_rec["component_order_ids"], ["oid2"])
        self.assertEqual(pov_rec["shares"], adapter.orders[-1].quantity)

    def test_st_name_never_ordered(self) -> None:
        self.signal["candidates"] = [{"rank": 1, "ts_code": "300100.SZ", "name": "*ST测试"}]
        adapter = FakeAdapter({"300100.SZ": self._quote()}, {})
        self._run(td.job_m_premarket_buy, adapter, now=datetime.datetime(2026, 9, 21, 9, 20, 30))
        self.assertEqual(adapter.orders, [])

    def test_existing_m_position_blocks_new_m(self) -> None:
        adapter = FakeAdapter({"300100.SZ": self._quote()}, {})
        pos = [{"strategy_leg": "M", "status": "open", "ts_code": "300300.SZ", "buy_date": "20260918"}]
        self._run(td.job_m_premarket_buy, adapter, now=datetime.datetime(2026, 9, 21, 9, 20, 30), positions=pos)
        self.assertEqual(adapter.orders, [])


if __name__ == "__main__":
    unittest.main()
