from __future__ import annotations

import datetime
import os
import sys
import unittest
from types import ModuleType
from unittest.mock import MagicMock, patch

os.environ["A_SYSTEM_DISABLE_NOTIFICATIONS"] = "1"

if "dotenv" not in sys.modules:
    dotenv_stub = ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv_stub

from scripts import trading_daemon as daemon


class LiveBroadcastAccuracyTests(unittest.TestCase):
    def test_takeprofit_label_uses_auction_wording_before_0925(self) -> None:
        self.assertEqual(
            daemon._takeprofit_submission_label(datetime.time(9, 20)),
            "集合竞价预挂",
        )
        self.assertEqual(
            daemon._takeprofit_submission_label(datetime.time(9, 24, 59)),
            "集合竞价预挂",
        )

    def test_takeprofit_label_uses_continuous_recovery_wording_after_0925(self) -> None:
        self.assertEqual(
            daemon._takeprofit_submission_label(datetime.time(10, 59, 59)),
            "连续竞价补挂",
        )
        self.assertEqual(
            daemon._takeprofit_submission_label(datetime.time(13, 30)),
            "连续竞价补挂",
        )

    def test_same_leg_open_position_blocks_broadcast_open_plan(self) -> None:
        positions = [
            {
                "order_id": "1090547061",
                "ts_code": "603118.SH",
                "strategy_leg": "L",
                "buy_date": "20260817",
                "status": "open",
                "shares": 11_800,
            }
        ]

        self.assertTrue(
            daemon._local_position_blocks_open_plan_broadcast(
                positions, "20260818"
            )
        )

    def test_final_summary_reports_zero_plans_when_l_holding_blocks_l_candidate(self) -> None:
        current_position = {
            "order_id": "1090547061",
            "ts_code": "603118.SH",
            "strategy_leg": "L",
            "buy_date": "20260817",
            "status": "open",
            "shares": 11_800,
        }
        l_candidate = {
            "planned_buy_date": "20260818",
            "planned_exit_date": "20260819",
            "planned_exit_rule": "T+2_close",
            "ts_code": "603186.SH",
            "name": "华正新材",
            "limit_close": 18.0,
            "position_pct": 0.825,
        }
        log = MagicMock()
        config = {
            "active_strategy_profile": {"mode": 3},
            "strategy_model3": {"enabled": True, "live_order_enabled": True},
            "strategy_l": {"live_order_enabled": True},
        }

        with (
            patch.object(daemon, "load_json_config", return_value=config),
            patch.object(daemon, "_load_e2_signal_for_signal_date", return_value=None),
            patch.object(daemon, "_load_m_signal_for_signal_date", return_value=None),
            patch.object(daemon, "_load_l_signal_for_signal_date", return_value=l_candidate),
            patch.object(daemon, "_model3_l_base_rule_pass_for_log", return_value=(True, "通过")),
            patch.object(daemon, "_planned_shares_by_equity", return_value=10_000),
            patch.object(daemon, "load_positions", return_value=[current_position]),
            patch.object(daemon, "logger", return_value=log),
        ):
            daemon._log_final_decision_summary("20260817", "20260818", None)

        message = str(log.info.call_args.args[0])
        self.assertIn("有旧策略仓尚未实际清空", message)
        self.assertIn("开仓计划：❌ 无", message)
        self.assertNotIn("开仓计划：✅", message)

    def test_recent_auto_cleared_position_still_blocks_broadcast(self) -> None:
        positions = [
            {
                "order_id": "1090547061",
                "ts_code": "603118.SH",
                "strategy_leg": "L",
                "buy_date": "20260817",
                "status": "closed",
                "shares": 11_800,
                "sell_price": 0.0,
                "exit_fills_by_date": {},
                "ghost_cleared_at": "2026-08-18 08:07:28",
                "ghost_clear_source": "账户心跳",
                "ghost_clear_reason": "QMT接口查询成功且返回无实盘持仓",
            }
        ]

        self.assertTrue(
            daemon._local_position_blocks_open_plan_broadcast(
                positions, "20260818"
            )
        )

    def test_closed_position_with_real_exit_does_not_block_broadcast(self) -> None:
        positions = [
            {
                "order_id": "old",
                "ts_code": "600000.SH",
                "strategy_leg": "L",
                "status": "closed",
                "shares": 0,
                "sell_price": 10.5,
            }
        ]

        self.assertFalse(
            daemon._local_position_blocks_open_plan_broadcast(
                positions, "20260818"
            )
        )


if __name__ == "__main__":
    unittest.main()
