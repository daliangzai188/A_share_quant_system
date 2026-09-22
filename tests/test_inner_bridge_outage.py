"""2026-09-22故障回归：桥接失联不重启，停手播报不能显示可执行计划。"""
from __future__ import annotations

import datetime
import os
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

os.environ["A_SYSTEM_DISABLE_NOTIFICATIONS"] = "1"
from scripts import trading_daemon as daemon


STALE = "QMT inner bridge heartbeat is stale"
CONFIG = {"broker_adapter_enabled": True, "qmt_enabled": True,
          "broker": {"enabled": True, "transport": "qmt_inner"}}


class InnerBridgeOutageTests(unittest.TestCase):
    def test_stale_and_wrapped_backoff_never_request_process_restart(self):
        for error in (RuntimeError(STALE), daemon.QMTReconnectBackoffError(
                "QMT连接失败: 完整备用path/session: " + STALE)):
            with self.subTest(error=str(error)), patch.object(
                    daemon, "_qmt_connect_failure_streak", 150), patch.object(
                    daemon, "_request_process_recovery") as restart:
                self.assertFalse(daemon._request_qmt_reconnect_stalled_recovery(error))
                restart.assert_not_called()

    def test_legacy_miniqmt_resource_recovery_still_works(self):
        with patch.object(daemon, "_qmt_connect_failure_streak", 3), patch.object(
                daemon, "_request_process_recovery", return_value=True) as restart:
            self.assertTrue(daemon._request_qmt_reconnect_stalled_recovery(
                RuntimeError("QMT连接失败: connect=-1")))
            restart.assert_called_once()

    def test_overnight_outage_alerts_and_never_retries_twice_or_restarts(self):
        with patch.object(daemon, "load_json_config", return_value=CONFIG), patch.object(
                daemon, "now_beijing", return_value=datetime.datetime(2026, 9, 21, 21)), patch.object(
                daemon, "_qmt_get", side_effect=RuntimeError(STALE)) as query, patch.object(
                daemon, "_qmt_reset") as reset, patch.object(
                daemon, "_qmt_reconnect_count", 0), patch.object(
                daemon, "write_broker_health") as health, patch.object(
                daemon, "_notify_once_per") as alert, patch.object(
                daemon, "_request_process_recovery") as restart:
            daemon._print_account_status(MagicMock())
            query.assert_called_once()
            reset.assert_called_once()
            health.assert_called_once()
            self.assertEqual(health.call_args.args[0], "unavailable")
            alert.assert_called_once()
            self.assertIn("交易连接未恢复", alert.call_args.args[2])
            self.assertEqual(alert.call_args.args[1], 1800)
            restart.assert_not_called()

    def test_backoff_stale_stays_unavailable_without_reset_or_restart(self):
        with patch.object(daemon, "load_json_config", return_value=CONFIG), patch.object(
                daemon, "now_beijing", return_value=datetime.datetime(2026, 9, 22, 9)), patch.object(
                daemon, "_qmt_get", side_effect=daemon.QMTReconnectBackoffError(STALE)), patch.object(
                daemon, "_qmt_reset") as reset, patch.object(
                daemon, "write_broker_health") as health, patch.object(
                daemon, "_notify_once_per"), patch.object(
                daemon, "_request_process_recovery") as restart:
            daemon._print_account_status(MagicMock())
            self.assertEqual(health.call_args.args[0], "unavailable")
            reset.assert_not_called()
            restart.assert_not_called()

    def test_startup_outage_is_reported_without_claiming_cause(self):
        with patch.object(daemon, "load_json_config", return_value=CONFIG), patch.object(
                daemon, "_qmt_get", side_effect=RuntimeError(STALE)), patch.object(
                daemon, "_qmt_reset"), patch.object(daemon, "logger", return_value=MagicMock()), patch.object(
                daemon, "write_broker_health"), patch.object(daemon, "_LAST_QMT_ERROR_TEXT", ""), patch.object(
                daemon, "_notify_once_per") as alert:
            self.assertFalse(daemon.check_qmt_connection(allow_full_scan=True))
            alert.assert_called_once()
            self.assertIn("核对客户端登录", alert.call_args.args[3])
            self.assertNotIn("客户端已退出", alert.call_args.args[3])

    def test_stop_gate_prevents_sizing_and_preparing_order_broadcast(self):
        orders = pd.DataFrame([dict(strategy_leg="C", ts_code="600325.SH",
                                    name="华发股份", round_lot_shares=151000,
                                    reference_price=3.0)])
        log = MagicMock()
        with patch.object(daemon, "load_json_config", return_value={}), patch.object(
                daemon, "_equity_curve_stop_block_reason", return_value="影子净值低于60日均线"), patch.object(
                daemon, "logger", return_value=log), patch.object(
                daemon, "_live_plan_sizing") as sizing, patch.object(
                daemon, "_load_e_signal_for_signal_date") as signal:
            daemon._log_final_decision_summary("20260921", "20260922", orders)
            sizing.assert_not_called()
            signal.assert_not_called()
        message = log.info.call_args.args[0]
        self.assertIn("方案甲停手", message)
        self.assertNotIn("共 1 笔", message)
        self.assertNotIn("准备下单时间", message)

    def test_gate_read_failure_also_blocks_summary(self):
        log = MagicMock()
        with patch.object(daemon, "load_json_config", side_effect=[{}, OSError("missing")]), patch.object(
                daemon, "logger", return_value=log), patch.object(
                daemon, "_live_plan_sizing") as sizing:
            daemon._log_final_decision_summary("20260921", "20260922", pd.DataFrame())
        self.assertIn("读取失败", log.info.call_args.args[0])
        sizing.assert_not_called()

    def test_stopped_day_never_reports_missed_entry_even_with_stale_plan_cache(self):
        plan = dict(action_date="20260922", final_buy={"strategy": "C", "ts_code": "600325.SH"})
        with patch.object(daemon, "now_beijing", return_value=datetime.datetime(2026, 9, 22, 10, 39)), patch.object(
                daemon, "is_trade_day", return_value=True), patch.object(
                daemon, "_last_final_plan", plan), patch.object(
                daemon, "_equity_curve_stop_block_reason", return_value="方案甲停手"), patch.object(
                daemon, "_notify") as notify, patch.object(
                daemon, "_ordinary_open_plan_expired") as expired:
            daemon._notify_missed_open_window_if_needed("启动补检")
            notify.assert_not_called()
            expired.assert_not_called()

    def test_decision_chain_prioritizes_stop_over_expired_window(self):
        orders = pd.DataFrame([dict(strategy_leg="C", side="BUY", ts_code="600325.SH",
                                    name="华发股份", round_lot_shares=151000,
                                    reference_price=3.0)])
        with patch.object(daemon.glob, "glob", return_value=["orders.csv"]), patch.object(
                pd, "read_csv", return_value=orders), patch.object(
                daemon, "next_n_trade_days", return_value=datetime.date(2026, 9, 22)), patch.object(
                daemon, "_load_e_signal_for_signal_date", return_value=None), patch.object(
                daemon, "_load_readonly_candidate_for_broadcast", return_value=(None, "无候选")), patch.object(
                daemon, "load_positions", return_value=[]), patch.object(
                daemon, "_last_final_plan", None), patch.object(
                daemon, "_equity_curve_stop_block_reason", return_value="方案甲停手"), patch.object(
                daemon, "_ordinary_open_plan_expired", return_value=True) as expiry, patch.object(
                daemon, "_live_plan_sizing") as sizing, patch.object(
                daemon, "logger", return_value=MagicMock()) as logger:
            daemon._log_decision_chain_summary("20260921")
            self.assertEqual(daemon._last_final_plan["equity_curve_stop_reason"], "方案甲停手")
            self.assertFalse(daemon._last_final_plan["execution_expired"])
            expiry.assert_not_called()
            sizing.assert_not_called()
            self.assertIn("方案甲停手", logger.return_value.info.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
