from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pandas as pd

os.environ["A_SYSTEM_DISABLE_NOTIFICATIONS"] = "1"
if "dotenv" not in sys.modules:
    dotenv_stub = ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv_stub

from scripts import trading_daemon


def beijing_at(hour: int, minute: int, second: int = 0) -> datetime.datetime:
    return datetime.datetime(
        2026, 8, 13, hour, minute, second, tzinfo=trading_daemon.BEIJING_TZ
    )


class MorningNotificationDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_plan = trading_daemon._last_final_plan

    def tearDown(self) -> None:
        trading_daemon._last_final_plan = self.old_plan

    def test_failed_delivery_does_not_write_false_success_marker(self) -> None:
        trading_daemon._last_final_plan = {
            "action_date": "20260813",
            "final_buy": None,
            "hold_line": "",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            trading_daemon, "_PLAN_PUSH_STATE", Path(temp_dir) / "push.json"
        ), patch.object(trading_daemon, "_notify", return_value=False):
            trading_daemon.push_open_plan_notification("早盘")
            self.assertFalse(trading_daemon._PLAN_PUSH_STATE.exists())

    def test_successful_delivery_writes_idempotency_marker(self) -> None:
        trading_daemon._last_final_plan = {
            "action_date": "20260813",
            "final_buy": None,
            "hold_line": "",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            trading_daemon, "_PLAN_PUSH_STATE", Path(temp_dir) / "push.json"
        ), patch.object(trading_daemon, "_notify", return_value=True):
            trading_daemon.push_open_plan_notification("早盘")
            state = json.loads(trading_daemon._PLAN_PUSH_STATE.read_text(encoding="utf-8"))

        self.assertEqual(state["last_pushed"], "20260813-早盘")


class OpeningRecoveryWindowTest(unittest.TestCase):
    def test_startup_between_0930_and_0935_recovers_opening_chain(self) -> None:
        with patch.object(trading_daemon, "now_beijing", return_value=beijing_at(9, 32)), \
             patch.object(trading_daemon, "is_trade_day", return_value=True), \
             patch.object(trading_daemon, "job_opening_buy") as opening:
            trading_daemon.startup_catchup_strategy_d()

        opening.assert_called_once_with()

    def test_missing_0900_cache_is_rebuilt_once_then_d_fallback_can_start(self) -> None:
        decisions = pd.DataFrame(
            [{"action": "ALLOW_D_INTRADAY_MONITOR", "strategy_leg": "D"}]
        )
        with patch.object(trading_daemon, "now_beijing", return_value=beijing_at(10, 40)), \
             patch.object(trading_daemon, "is_trade_day", return_value=True), \
             patch.object(trading_daemon, "_d_relay_pair_active_today", return_value=False), \
             patch.object(trading_daemon, "_pov_active_today", return_value=False), \
             patch.object(trading_daemon, "_strategy_d_monitor_running", return_value=False), \
             patch.object(trading_daemon, "read_cached_combined_decisions", return_value=None), \
             patch.object(
                 trading_daemon,
                 "load_combined_decisions",
                 return_value=(decisions, Path("orders.csv")),
             ) as rebuild, patch.object(trading_daemon, "job_strategy_d") as start_d:
            trading_daemon.startup_catchup_strategy_d()

        rebuild.assert_called_once_with()
        start_d.assert_called_once_with()

    def test_late_auction_buy_is_blocked_before_any_order_path(self) -> None:
        with patch.object(trading_daemon, "now_beijing", return_value=beijing_at(9, 25)), \
             patch.object(trading_daemon, "check_and_close_positions"), \
             patch.object(trading_daemon, "has_position_bought_today") as bought, \
             patch.object(trading_daemon, "read_cached_combined_decisions") as combined, \
             patch.object(trading_daemon, "_notify"):
            trading_daemon.job_premarket_buy()

        bought.assert_not_called()
        combined.assert_not_called()

    def test_late_auction_sell_is_blocked_before_loading_positions(self) -> None:
        with patch.object(trading_daemon, "now_beijing", return_value=beijing_at(9, 25)), \
             patch.object(trading_daemon, "check_and_close_positions"), \
             patch.object(trading_daemon, "_has_premarket_close_plan", return_value=True), \
             patch.object(trading_daemon, "load_positions") as positions, \
             patch.object(trading_daemon, "_notify"):
            trading_daemon.job_premarket_sell()

        positions.assert_not_called()

    def test_recovery_only_confirms_old_orders_but_never_creates_new_plan(self) -> None:
        with patch.object(trading_daemon, "check_and_close_positions"), \
             patch.object(trading_daemon, "confirm_pending_premarket_buys") as confirm, \
             patch.object(trading_daemon, "_d_relay_pair_active_today", return_value=False), \
             patch.object(trading_daemon, "_pov_active_today", return_value=False), \
             patch.object(trading_daemon, "has_position_bought_today", return_value=False), \
             patch.object(trading_daemon, "load_pending_buys", return_value=[]), \
             patch.object(trading_daemon, "load_combined_decisions") as combined:
            trading_daemon.job_opening_buy(recovery_only=True)

        confirm.assert_called_once_with()
        combined.assert_not_called()


if __name__ == "__main__":
    unittest.main()
