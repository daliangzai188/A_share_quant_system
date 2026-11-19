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


class AuditPushTests(unittest.TestCase):
    def run_audit(self, positions):
        today = datetime.date.today().strftime("%Y%m%d")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily = root / "data" / "raw" / "daily"
            daily.mkdir(parents=True)
            (daily / "20261118.csv").write_text(
                "ts_code,trade_date,open,close\n301288.SZ,20261118,20.92,22.83\n002652.SZ,20261118,4.40,4.28\n",
                encoding="utf-8")
            (daily / f"{today}.csv").write_text(
                f"ts_code,trade_date,open,close\n301288.SZ,{today},22.0,19.34\n002652.SZ,{today},4.20,4.71\n",
                encoding="utf-8")
            pos_path = root / "positions.json"
            for row in positions:
                row["sell_date"] = today
            pos_path.write_text(json.dumps(positions, ensure_ascii=False), encoding="utf-8")
            out = root / "reports" / "live_execution_audit.csv"
            pushes = []
            with patch.object(audit, "PROJECT_ROOT", root), patch.object(audit, "POSITIONS", pos_path), \
                    patch.object(audit, "OUT", out), patch.object(audit, "_daily_cache", {}), \
                    patch.object(audit, "bark", side_effect=lambda title, body: pushes.append((title, body))):
                audit.main()
            header = out.read_text(encoding="utf-8-sig").splitlines()[0] if out.exists() else ""
        return pushes, header

    def test_two_lots_of_one_trade_become_one_clear_line(self):
        pushes, header = self.run_audit([m_lot("M1", 400, 20.92, 19.59), m_lot("M2", 100, 20.94, 19.58),
                                         ghost_closed_e()])
        title, body = pushes[0]
        self.assertEqual(title, "🧾 执行对账：清研环境（M）比回测好1.26%，另有1笔缺卖出价")
        self.assertIn("清研环境（M）500股：比回测好1.26%｜买入均价20.924（开盘价20.92，多花0.02%）｜"
                      "卖出均价19.588（收盘价19.34，多卖1.28%）", body)
        self.assertIn("扬子新材（E）3000股：缺卖出成交价", body)
        self.assertNotIn("正=比回测口径差", body)
        self.assertEqual(body.count("清研环境"), 1)                         # 两片合并成一行
        self.assertEqual(header, ",".join(audit.FIELDS))                     # CSV格式不变

    def test_backfilled_takeprofit_reads_naturally(self):
        e_row = ghost_closed_e()
        e_row.update(sell_price=4.70, exit_fills_by_date={"20261119": {"qty": 3000, "amount": 14100.0}})
        title, body = self.run_audit([e_row])[0][0]
        # 回测（src/ace_m_replay.py）按收盘价卖、不模拟止盈，所以止盈成交也和收盘价比
        self.assertEqual(title, "🧾 执行对账：扬子新材（E）比回测差0.89%")
        self.assertIn("卖出均价4.70（收盘价4.71，少卖0.21%）", body)
        self.assertIn("买入均价4.43（开盘价4.40，多花0.68%）", body)


if __name__ == "__main__":
    unittest.main()
