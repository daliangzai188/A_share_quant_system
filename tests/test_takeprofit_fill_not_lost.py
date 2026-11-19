"""止盈单成交后卖出价丢失（2026-11-19 实盘问题）。

11-19 扬子新材（E，3000股）早上挂了涨停价-0.01（4.70）的止盈单，约13:08成交。账户心跳每10秒对一次券商持仓，
连续3次查不到就按“手动卖出”关了仓（13:09:49），卖出价记成0；止盈线程5分钟才查一轮，再来时仓已关，
成交价就丢了：没有“盘中止盈成交”通知，成交汇总卖出金额是0，执行对账跳过它，净值账本报“缺真实卖出成交”。
修复后：
- 逐票同步：这只票今天有本系统卖单在途时，先等卖单按券商成交价回写，最多等10分钟；
- 收盘核对和程序启动时，用QMT当天系统卖单的成交给“被当成手动卖出、卖出价是0”的仓补价（股数必须对上）；
- 执行对账推送：同一笔交易的分片合并成一行，直接写“比回测好/差x%”，缺卖出价的也列出来。
"""
from __future__ import annotations

import csv
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.daemon_testing import patch_daemon

from scripts import live_execution_audit as audit
from src.daemon import close, qmt, state

TODAY = datetime.date(2026, 11, 19)
CONFIG = {"live_trade": {"broker_missing_confirm_count": 3}}


class MemoryStore:
    def __init__(self, positions):
        self.positions = json.loads(json.dumps(positions))

    def load(self):
        return json.loads(json.dumps(self.positions))

    def save(self, positions):
        self.positions = json.loads(json.dumps(positions))


def e_position(**changes):
    row = {"order_id": "E1", "ts_code": "002652.SZ", "name": "扬子新材", "strategy_leg": "E",
           "signal_date": "20261117", "buy_date": "20261118", "planned_exit_date": "20261119",
           "shares": 3000, "buy_price": 4.43, "status": "open"}
    row.update(changes)
    return row


def ghost_closed_e():
    # 11-19 13:09:49 逐票同步把它当成手动卖出关掉后的样子
    return e_position(status="closed", sell_date="20261119", sell_price=0.0,
                      ghost_cleared_at="2026-11-19 13:09:49", ghost_clear_source="逐票同步-账户心跳",
                      ghost_clear_reason="QMT连续3次完整且资产自洽的账户/持仓快照确认无此股票（按手动卖出/已平仓同步）",
                      broker_confirmed_absent=True)


def m_lot(order_id, shares, buy, sell):
    return {"order_id": order_id, "ts_code": "301288.SZ", "name": "清研环境", "strategy_leg": "M",
            "buy_date": "20261118", "planned_exit_date": "20261119", "shares": 0, "entry_shares": shares,
            "buy_price": buy, "status": "closed", "sell_date": "20261119", "sell_price": sell,
            "exit_fills_by_date": {"20261119": {"qty": shares, "amount": shares * sell}}}


class OwnExitInFlightTests(unittest.TestCase):
    def setUp(self):
        qmt._broker_missing_streak.clear()
        qmt._own_exit_missing_since.clear()
        self.addCleanup(qmt._broker_missing_streak.clear)
        self.addCleanup(qmt._own_exit_missing_since.clear)
        self.clock = [1000.0]

    def run_rounds(self, store, in_flight, seconds):
        changed = []
        with patch_daemon("load_positions", side_effect=store.load), \
                patch_daemon("save_positions", side_effect=store.save), \
                patch_daemon("load_json_config", return_value=CONFIG), \
                patch_daemon("today_beijing", return_value=TODAY), \
                patch_daemon("_track_execution"), \
                patch_daemon("_maybe_resume_pipeline_after_trade"), \
                patch_daemon("_exit_in_flight_local_order_ids", return_value=in_flight), \
                patch.object(qmt.time, "monotonic", side_effect=lambda: self.clock[0]):
            for second in seconds:
                self.clock[0] = 1000.0 + second
                changed.append(qmt.reconcile_missing_local_positions([], "账户心跳", snapshot_complete=True))
        return changed

    def test_takeprofit_in_flight_waits_instead_of_closing_as_manual_sale(self):
        store = MemoryStore([e_position()])
        # 13:08:39起每10秒一轮，止盈线程5分钟内会来回写：这期间一直不关仓
        self.assertEqual(sum(self.run_rounds(store, {"E1"}, range(0, 300, 10))), 0)
        self.assertEqual(store.positions[0]["status"], "open")
        self.assertNotIn("002652.SZ", qmt._broker_missing_streak)

    def test_after_ten_minutes_the_old_rule_applies(self):
        store = MemoryStore([e_position()])
        changed = self.run_rounds(store, {"E1"}, [0, 300, 600, 610, 620])
        self.assertEqual(changed, [0, 0, 0, 0, 1])                    # 超时后再连续3次才关
        self.assertEqual(store.positions[0]["status"], "closed")

    def test_without_own_sell_order_closes_after_three_rounds_as_before(self):
        for in_flight in (set(), None, {"OTHER"}):                    # 没有自家卖单 / 读不到状态
            qmt._broker_missing_streak.clear()
            qmt._own_exit_missing_since.clear()
            store = MemoryStore([e_position()])
            self.assertEqual(self.run_rounds(store, in_flight, [0, 10, 20]), [0, 0, 1])
            self.assertEqual(store.positions[0]["status"], "closed")


class InFlightStateTests(unittest.TestCase):
    def test_reads_only_todays_unfinished_own_sell_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "exit_execution_state.json"
            intents = [
                {"token": "t1", "trade_date": "20261119", "account_fingerprint": "FP", "status": "SUBMITTED",
                 "local_order_id": "E1"},
                {"token": "t2", "trade_date": "20261119", "account_fingerprint": "FP", "status": "RESOLVED",
                 "local_order_id": "M1"},
                {"token": "t3", "trade_date": "20261118", "account_fingerprint": "FP", "status": "SUBMITTED",
                 "local_order_id": "OLD"},
                {"token": "t4", "trade_date": "20261119", "account_fingerprint": "OTHER", "status": "PREPARED",
                 "local_order_id": "X"},
            ]
            path.write_text(json.dumps({"version": 1, "state_revision": 1, "batches": {}, "intents": intents}),
                            encoding="utf-8")
            with patch.object(state, "EXIT_EXECUTION_STATE_FILE", path), \
                    patch.object(state, "_exit_account_fingerprint", return_value="FP"):
                self.assertEqual(state._exit_in_flight_local_order_ids("20261119"), {"E1"})
                path.write_text("{坏了", encoding="utf-8")                    # 主文件坏了：用备份
                self.assertEqual(state._exit_in_flight_local_order_ids("20261119"), {"E1"})
                path.write_text("{坏了", encoding="utf-8")
                state._state_backup_path(path).write_text("{坏了", encoding="utf-8")
                self.assertIsNone(state._exit_in_flight_local_order_ids("20261119"))   # 主备都坏：不触发进程恢复


class BackfillTests(unittest.TestCase):
    def adapter(self, takeprofit_qty=3000):
        orders = [{"order_id": 101, "stock_code": "002652.SZ", "order_remark": "盘中止盈-20261119-1a2b3c4d",
                   "order_type": 24, "order_status": 56},
                  {"order_id": 202, "stock_code": "301288.SZ", "order_remark": "ABC平仓-M-20261119",
                   "order_type": 24, "order_status": 56}]
        trades = [{"order_id": 101, "stock_code": "002652.SZ", "traded_volume": takeprofit_qty, "traded_price": 4.70},
                  {"order_id": 202, "stock_code": "301288.SZ", "traded_volume": 500, "traded_price": 19.588}]
        return SimpleNamespace(query_orders=Mock(return_value=orders), query_trades=Mock(return_value=trades))

    def run_backfill(self, store, adapter):
        with patch_daemon("load_positions", side_effect=store.load), \
                patch_daemon("save_positions", side_effect=store.save), \
                patch_daemon("today_beijing", return_value=TODAY), \
                patch_daemon("_qmt_get", return_value=adapter), \
                patch_daemon("_track_execution") as track, \
                patch_daemon("_notify") as notify:
            filled = close._backfill_ghost_cleared_sell_prices({}, Mock())
        return filled, notify, track

    def test_ghost_closed_takeprofit_gets_the_real_fill_price(self):
        store = MemoryStore([ghost_closed_e(), m_lot("M1", 400, 20.92, 19.59), m_lot("M2", 100, 20.94, 19.58)])
        adapter = self.adapter()
        filled, notify, track = self.run_backfill(store, adapter)
        self.assertEqual(filled, 1)
        e_row = store.positions[0]
        self.assertAlmostEqual(e_row["sell_price"], 4.70)
        self.assertEqual(e_row["exit_fills_by_date"]["20261119"]["qty"], 3000)
        self.assertAlmostEqual(e_row["exit_fills_by_date"]["20261119"]["amount"], 14100.0)
        self.assertEqual(e_row["status"], "closed")
        self.assertEqual(store.positions[1]["sell_price"], 19.59)          # 已有成交的M不动
        self.assertEqual(notify.call_args.args[1], "🧾 卖出价已补全")
        self.assertIn("002652.SZ 扬子新材 3000股@4.70", notify.call_args.args[2])
        track.assert_called_once_with("rebuild_summary")
        self.assertFalse(state._is_recent_auto_ghost_clear(e_row, as_of_date="20261119"))  # 不会被当误清恢复

        # 再跑一次：没有待补的仓，连QMT都不查
        adapter2 = self.adapter()
        filled, notify, _ = self.run_backfill(store, adapter2)
        self.assertEqual(filled, 0)
        adapter2.query_orders.assert_not_called()
        notify.assert_not_called()

    def test_quantity_mismatch_is_left_alone(self):
        store = MemoryStore([ghost_closed_e()])
        filled, notify, _ = self.run_backfill(store, self.adapter(takeprofit_qty=2000))
        self.assertEqual(filled, 0)
        self.assertEqual(store.positions[0]["sell_price"], 0.0)
        notify.assert_not_called()

    def test_no_ghost_closed_position_means_no_qmt_query(self):
        store = MemoryStore([m_lot("M1", 400, 20.92, 19.59)])
        adapter = self.adapter()
        self.assertEqual(self.run_backfill(store, adapter)[0], 0)
        adapter.query_orders.assert_not_called()


DAILY_HEADER = "ts_code,trade_date,open,high,low,close,pre_close\n"
# 11-18：两只的买入日；11-19：扬子新材昨收4.28、涨停价4.71，最高4.70（=涨停价-0.01）收4.40；清研环境没冲板
DAY_1118 = {"301288.SZ": (20.92, 23.5, 20.5, 22.83, 21.0), "002652.SZ": (4.40, 4.55, 4.24, 4.28, 4.40)}
DAY_1119 = {"301288.SZ": (22.0, 23.0, 19.0, 19.34, 22.83), "002652.SZ": (4.28, 4.70, 4.08, 4.40, 4.28)}


def daily_csv(day, bars):
    return DAILY_HEADER + "".join("%s,%s,%s,%s,%s,%s,%s\n" % ((code, day) + bar) for code, bar in bars.items())


class AuditPushTests(unittest.TestCase):
    """执行对账（2026-11-19起卖出基准含止盈：计划卖出日最高价摸到涨停价-0.01时按止盈价比）。"""

    def run_audit(self, positions, days=None, previous=None, config=None):
        days = days or {"20261118": DAY_1118, "20261119": DAY_1119}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = root / "data" / "raw" / "daily"
            daily.mkdir(parents=True)
            for day, bars in days.items():
                (daily / f"{day}.csv").write_text(daily_csv(day, bars), encoding="utf-8")
            if config is not None:
                (root / "config").mkdir()
                (root / "config" / "config.json").write_text(json.dumps(config), encoding="utf-8")
            pos_path = root / "positions.json"
            pos_path.write_text(json.dumps(positions, ensure_ascii=False), encoding="utf-8")
            out = root / "reports" / "live_execution_audit.csv"
            if previous is not None:
                out.parent.mkdir(parents=True)
                out.write_text(previous, encoding="utf-8-sig")
            pushes = []
            with patch.object(audit, "PROJECT_ROOT", root), patch.object(audit, "POSITIONS", pos_path), \
                    patch.object(audit, "OUT", out), patch.object(audit, "_daily_cache", {}), \
                    patch.object(audit, "bark", side_effect=lambda title, body: pushes.append((title, body))):
                audit.main()
            rows = []
            if out.exists():
                with open(out, newline="", encoding="utf-8-sig") as fh:
                    rows = list(csv.DictReader(fh))
            header = out.read_text(encoding="utf-8-sig").splitlines()[0] if out.exists() else ""
        trade_pushes = [p for p in pushes if p[0].startswith("🧾 执行对账：")]   # 周五另有周报
        return trade_pushes, rows, header

    def test_two_lots_of_one_trade_become_one_clear_line(self):
        today = datetime.date.today().strftime("%Y%m%d")
        lots = [m_lot("M1", 400, 20.92, 19.59), m_lot("M2", 100, 20.94, 19.58)]
        missing = ghost_closed_e()
        for row in lots + [missing]:
            row.update(sell_date=today, planned_exit_date=today)
        pushes, rows, header = self.run_audit(lots + [missing], days={"20261118": DAY_1118, today: DAY_1119})
        title, body = pushes[0]
        self.assertEqual(title, "🧾 执行对账：清研环境（M）比规则好1.26%，另有1笔缺卖出价")
        self.assertIn("清研环境（M）500股：比规则好1.26%｜买入均价20.924（开盘价20.92，多花0.02%）｜"
                      "卖出均价19.588（收盘价19.34，多卖1.28%）", body)
        self.assertIn("扬子新材（E）3000股：缺卖出成交价", body)
        self.assertEqual(body.count("清研环境"), 1)                         # 两片合并成一行
        self.assertEqual(header, ",".join(audit.FIELDS))
        self.assertEqual({r["sell_bench_rule"] for r in rows}, {"收盘价"})

    def test_takeprofit_fill_is_compared_with_takeprofit_price(self):
        # 11-19实例：止盈单4.70成交，收盘4.40。按规则（含止盈）只差买入多花的0.68%，止盈多卖的6.82%单独写
        e_row = ghost_closed_e()
        e_row.update(sell_price=4.70, exit_fills_by_date={"20261119": {"qty": 3000, "amount": 14100.0}})
        pushes, rows, _header = self.run_audit([e_row])
        title, body = pushes[0]
        self.assertEqual(title, "🧾 执行对账：扬子新材（E）比规则差0.68%（止盈成交，比收盘多卖6.82%）")
        self.assertIn("买入均价4.43（开盘价4.40，多花0.68%）｜卖出均价4.70（止盈价4.70，持平；收盘价4.40，止盈比收盘多卖6.82%）",
                      body)
        self.assertIn("计划卖出日最高价摸到涨停价−0.01时按止盈价卖", body)
        self.assertEqual((rows[0]["bench_close"], rows[0]["sell_bench_rule"], rows[0]["exit_close"]), ("4.7", "止盈价", "4.4"))
        self.assertAlmostEqual(float(rows[0]["sell_slip_pct"]), 0.0, places=6)    # 滑点报警只看执行成本

    def test_old_row_is_recomputed_quietly(self):
        # 换版前已按收盘价入账（比回测好6.14%）的那一笔：重算成止盈口径，不再重复推送，入账时间不变
        e_row = ghost_closed_e()
        e_row.update(sell_price=4.70)
        old_fields = audit.FIELDS[:14]
        previous = ",".join(old_fields) + "\n" + ",".join([
            "E1", "002652.SZ", "扬子新材", "E", "20261118", "20261119", "4.43", "4.4", "0.6818", "4.7", "4.4",
            "-6.8182", "-6.1364", "2026-11-19 19:01:40"]) + "\n"
        pushes, rows, header = self.run_audit([e_row], previous=previous)
        self.assertEqual(pushes, [])
        self.assertEqual(header, ",".join(audit.FIELDS))
        self.assertEqual((rows[0]["bench_close"], rows[0]["sell_bench_rule"], rows[0]["recorded_at"]),
                         ("4.7", "止盈价", "2026-11-19 19:01:40"))
        self.assertAlmostEqual(float(rows[0]["total_slip_pct"]), 0.6818, places=4)

    def test_row_kept_when_market_data_is_gone(self):
        previous = ",".join(audit.FIELDS[:14]) + "\nX1,600000.SH,浦发银行,A,20260701,20260703,10,10,0,10,10,0,0,2026-07-03 19:00:00\n"
        old = {"order_id": "X1", "ts_code": "600000.SH", "name": "浦发银行", "strategy_leg": "A", "buy_date": "20260701",
               "planned_exit_date": "20260703", "sell_date": "20260703", "buy_price": 10, "sell_price": 10,
               "status": "closed", "entry_shares": 100}
        _pushes, rows, _header = self.run_audit([old], previous=previous)
        self.assertEqual([(r["order_id"], r["bench_close"], r["sell_bench_rule"]) for r in rows], [("X1", "10", "")])

    def test_no_takeprofit_when_not_on_planned_day_or_disabled(self):
        e_row = ghost_closed_e()
        e_row.update(sell_price=4.70)
        early = dict(e_row, planned_exit_date="20991231")                 # 计划卖出日之前就卖了：那天不会挂止盈
        manual = dict(e_row, manual_exit_only=True)                        # 人工退出仓不挂止盈
        for row, config in ((early, None), (manual, None), (e_row, {"live_trade": {"intraday_takeprofit_enabled": False}})):
            _pushes, rows, _header = self.run_audit([row], config=config)
            self.assertEqual((rows[0]["bench_close"], rows[0]["sell_bench_rule"]), ("4.4", "收盘价"))

    def test_delayed_exit_still_uses_planned_day_takeprofit(self):
        # 计划卖出日摸到止盈价、实盘却拖到下一天才卖：按规则那天就该止盈成交，基准仍是止盈价
        e_row = ghost_closed_e()
        e_row.update(sell_date="20261120", sell_price=4.30)
        days = {"20261118": DAY_1118, "20261119": DAY_1119, "20261120": {"002652.SZ": (4.35, 4.40, 4.20, 4.30, 4.40)}}
        pushes, rows, _header = self.run_audit([e_row], days=days)
        self.assertEqual((rows[0]["bench_close"], rows[0]["sell_bench_rule"], rows[0]["exit_close"]), ("4.7", "止盈价", "4.3"))
        self.assertIn("11/20平仓，比规则差9.19%", pushes[0][1])

    def test_waits_for_planned_day_data(self):
        e_row = ghost_closed_e()
        e_row.update(sell_date="20261120", sell_price=4.30)
        days = {"20261118": DAY_1118, "20261120": {"002652.SZ": (4.35, 4.40, 4.20, 4.30, 4.40)}}
        pushes, rows, _header = self.run_audit([e_row], days=days)
        self.assertEqual((pushes, rows), ([], []))                         # 计划卖出日行情没有就先不算，下次再算


if __name__ == "__main__":
    unittest.main()
