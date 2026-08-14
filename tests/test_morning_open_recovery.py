from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
    def test_d_broadcast_is_blocked_by_model3_l_candidate(self) -> None:
        line = trading_daemon._d_candidate_gate_line(
            mode=3,
            day_label="明日",
            l_buy={"ts_code": "603118.SH", "name": "共进股份"},
            mode1_buy=None,
        )
        self.assertIn("阻断", line)
        self.assertIn("L正式开仓计划 603118.SH 共进股份", line)

    def test_fill_check_detail_uses_real_auction_seed_not_nominal_signal_shares(self) -> None:
        plan = {
            "final_buy": {
                "strategy": "L",
                "ts_code": "603118.SH",
                "name": "共进股份",
                "shares": 23400,
            }
        }
        state = {
            "items": [{
                "ts_code": "603118.SH",
                "auction_planned_qty": 12200,
                "target_actual_amount": 229100.0,
                "hard_cap_amount": 236100.0,
            }]
        }
        with patch.object(trading_daemon, "load_positions", return_value=[]), \
             patch.object(trading_daemon, "load_pending_buys", return_value=[]), \
             patch.object(trading_daemon, "_pov_load_state", return_value=state):
            detail = trading_daemon._open_plan_execution_detail(plan)

        self.assertIn("实盘竞价种子计划12200股", detail)
        self.assertIn("目标22.91万/82.5%", detail)
        self.assertNotIn("23400股@参考", detail)

    def test_0935_active_pov_reports_progress_instead_of_false_missing_order(self) -> None:
        old_plan = trading_daemon._last_final_plan
        trading_daemon._last_final_plan = {
            "action_date": "20260813",
            "final_buy": {
                "strategy": "L",
                "ts_code": "603118.SH",
                "name": "共进股份",
                "shares": 23400,
            },
        }
        try:
            with patch.object(trading_daemon, "today_beijing", return_value=beijing_at(9, 35)), \
                 patch.object(trading_daemon, "load_positions", return_value=[]), \
                 patch.object(trading_daemon, "load_pending_buys", return_value=[]), \
                 patch.object(trading_daemon, "_pov_load_state", return_value={"items": []}), \
                 patch.object(trading_daemon, "has_position_bought_today", return_value=False), \
                 patch.object(trading_daemon, "_pov_active_today", return_value=True), \
                 patch.object(trading_daemon, "_qmt_get") as qmt, \
                 patch.object(trading_daemon, "_notify", return_value=True) as notify:
                trading_daemon.job_open_plan_fill_check()
        finally:
            trading_daemon._last_final_plan = old_plan

        qmt.assert_not_called()
        self.assertEqual(notify.call_args.args[1], "⏳ POV开仓执行中")
        self.assertNotIn("计划未执行", notify.call_args.args[1])

    def test_fill_check_detail_prefers_actual_registered_fill(self) -> None:
        plan = {
            "final_buy": {
                "strategy": "L",
                "ts_code": "603118.SH",
                "name": "共进股份",
                "shares": 23400,
            }
        }
        position = {
            "status": "open",
            "buy_date": "20260813",
            "ts_code": "603118.SH",
            "shares": 12100,
            "buy_price": 17.63,
        }
        with patch.object(trading_daemon, "today_beijing", return_value=beijing_at(9, 35)), \
             patch.object(trading_daemon, "load_positions", return_value=[position]):
            detail = trading_daemon._open_plan_execution_detail(plan)

        self.assertIn("实际已成交并登记12100股@均价17.63", detail)

    def test_d_status_uses_final_l_candidate_instead_of_ac_count_only(self) -> None:
        old_plan = trading_daemon._last_final_plan
        trading_daemon._last_final_plan = {
            "signal_date": "20260812",
            "execution_expired": False,
            "final_buy": {
                "strategy": "L",
                "ts_code": "603118.SH",
                "name": "共进股份",
            },
        }
        try:
            with patch.object(trading_daemon, "_load_ab_checklist", return_value=pd.DataFrame()), \
                 patch.object(trading_daemon, "now_beijing", return_value=beijing_at(18, 0)), \
                 patch.object(trading_daemon, "_strategy_d_monitor_running", return_value=False), \
                 patch.object(trading_daemon, "load_positions", return_value=[]), \
                 patch.object(trading_daemon, "logger") as logger_factory:
                trading_daemon._log_d_status_for_signal("20260812")
        finally:
            trading_daemon._last_final_plan = old_plan

        calls = repr(logger_factory.return_value.info.call_args_list)
        self.assertIn("今日已有%s正式候选", calls)
        self.assertIn("603118.SH", calls)
        self.assertIn("共进股份", calls)

    def test_normal_auction_uses_seed_cap_and_only_remainder_goes_to_pov(self) -> None:
        auction_qty, auction_cap, share = trading_daemon._pov_auction_seed_quantity(
            total_target_qty=8200,
            reference_price=10.0,
            signal_day_amount=10_000_000.0,
            live_cfg={"pov_auction_share": 0.001},
            lot_size=100,
        )

        self.assertEqual(auction_qty, 1000)
        self.assertAlmostEqual(auction_cap, 10_000.0)
        self.assertAlmostEqual(share, 0.001)
        self.assertAlmostEqual(82_500.0 - auction_qty * 10.0, 72_500.0)

    def test_0930_pov_recovery_persists_82_5_target_and_85_hard_cap(self) -> None:
        from src.live_order_gateway import LiveOrderGateway

        config = {
            "broker": {"enabled": True},
            "live_trade": {
                "pov_enabled": True,
                "entry_actual_amount_rebalance_enabled": True,
                "real_order_confirm_text": "CONFIRMED",
                "max_position_pct": 0.85,
                "max_total_position_pct": 0.825,
                "entry_min_acceptable_position_pct": 0.80,
                "max_single_order_amount": 0,
                "cash_buffer_amount": 1000,
                "total_liquidity_cap_pct": 0.005,
                "liquidity_cap_fail_closed": True,
                "round_lot_size": 100,
            },
        }
        account = SimpleNamespace(total_asset=100_000.0, available_cash=100_000.0)
        quote = SimpleNamespace(last_price=10.0)
        adapter = SimpleNamespace(
            query_account=lambda: account,
            query_positions=lambda: [],
            get_full_tick=lambda _codes: {"600000.SH": quote},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = Path(temp_dir) / "orders.csv"
            pd.DataFrame([{
                "side": "BUY",
                "strategy_leg": "L",
                "ts_code": "600000.SH",
                "broker_code": "600000.SH",
                "name": "测试L",
                "signal_date": "20260812",
                "reference_price": 10.0,
                "round_lot_shares": 1000,
                "exit_n_days": 1,
                "strategy_name": "A_SYSTEM_L",
            }]).to_csv(plan, index=False)
            with patch.object(trading_daemon, "load_json_config", return_value=config), \
                 patch.object(LiveOrderGateway, "assert_real_order_allowed"), \
                 patch.object(trading_daemon, "_qmt_get", return_value=adapter), \
                 patch.object(trading_daemon, "_broker_has_preexisting_strategy_position", return_value=False), \
                 patch.object(trading_daemon, "_signal_day_amount", return_value=100_000_000.0), \
                 patch.object(trading_daemon, "st_open_forbidden", return_value=False), \
                 patch.object(trading_daemon, "_track_execution"), \
                 patch.object(trading_daemon, "_pov_enqueue") as enqueue:
                ok = trading_daemon._enqueue_opening_pov_from_plan(
                    plan,
                    open_action="ALLOW_MODEL3_L_SUPPLEMENT",
                    reason="测试L恢复",
                )

        self.assertTrue(ok)
        enqueue.assert_called_once()
        item = enqueue.call_args.args[0][0]
        self.assertEqual(item["total_target_qty"], 8200)
        self.assertAlmostEqual(item["target_actual_amount"], 82_500.0)
        self.assertAlmostEqual(item["min_acceptable_amount"], 80_000.0)
        self.assertAlmostEqual(item["hard_cap_amount"], 85_000.0)
        self.assertEqual(item["auction_planned_qty"], 0)
        self.assertEqual(item["pov_planned_qty"], 8200)

    def test_open_action_resolver_covers_model3_l_and_m_for_both_entry_points(self) -> None:
        l_decisions = pd.DataFrame(
            [{"action": "ALLOW_MODEL3_L_SUPPLEMENT", "strategy_leg": "L"}]
        )
        m_decisions = pd.DataFrame(
            [{"action": "ALLOW_M_BUY", "strategy_leg": "M"}]
        )
        with patch.object(trading_daemon, "is_strategy_l_mode", return_value=False), \
             patch.object(trading_daemon, "is_strategy_model3_mode", return_value=True):
            self.assertEqual(
                trading_daemon._combined_open_action_for_current_mode(l_decisions),
                "ALLOW_MODEL3_L_SUPPLEMENT",
            )
            self.assertEqual(
                trading_daemon._combined_open_action_for_current_mode(m_decisions),
                "ALLOW_M_BUY",
            )
        with patch.object(trading_daemon, "is_strategy_l_mode", return_value=False), \
             patch.object(trading_daemon, "is_strategy_model3_mode", return_value=False):
            self.assertEqual(
                trading_daemon._combined_open_action_for_current_mode(m_decisions),
                "ALLOW_M_BUY",
            )

    def test_open_action_resolver_keeps_leg_priority_when_decisions_conflict(self) -> None:
        decisions = pd.DataFrame(
            [
                {"action": "ALLOW_E2_BUY", "strategy_leg": "E2"},
                {"action": "ALLOW_M_BUY", "strategy_leg": "M"},
                {"action": "ALLOW_MODEL3_L_REPLACE", "strategy_leg": "L"},
            ]
        )
        with patch.object(trading_daemon, "is_strategy_l_mode", return_value=False), \
             patch.object(trading_daemon, "is_strategy_model3_mode", return_value=True):
            self.assertEqual(
                trading_daemon._combined_open_action_for_current_mode(decisions),
                "ALLOW_MODEL3_L_REPLACE",
            )

    def _assert_0930_executes_action(self, action: str, expected_reason: str) -> None:
        decisions = pd.DataFrame([{"action": action, "strategy_leg": "TEST"}])
        with patch.object(trading_daemon, "check_and_close_positions"), \
             patch.object(trading_daemon, "confirm_pending_premarket_buys"), \
             patch.object(trading_daemon, "_d_relay_pair_active_today", return_value=False), \
             patch.object(trading_daemon, "_pov_active_today", return_value=False), \
             patch.object(trading_daemon, "has_position_bought_today", return_value=False), \
             patch.object(trading_daemon, "load_pending_buys", return_value=[]), \
             patch.object(trading_daemon, "load_combined_decisions", return_value=(decisions, Path("orders.csv"))), \
             patch.object(trading_daemon, "is_strategy_l_mode", return_value=False), \
             patch.object(trading_daemon, "is_strategy_model3_mode", return_value=True), \
             patch.object(trading_daemon, "_enqueue_opening_pov_from_plan", return_value=True) as execute, \
             patch.object(trading_daemon, "_notify"):
            trading_daemon.job_opening_buy()

        execute.assert_called_once_with(
            Path("orders.csv"),
            open_action=action,
            reason=expected_reason,
        )

    def test_0930_fallback_executes_model3_l(self) -> None:
        self._assert_0930_executes_action(
            "ALLOW_MODEL3_L_SUPPLEMENT", "L/model3补位 09:30开仓"
        )

    def test_0930_fallback_executes_m(self) -> None:
        self._assert_0930_executes_action("ALLOW_M_BUY", "M 09:30开仓")

    def test_formal_candidate_failure_never_switches_to_d(self) -> None:
        decisions = pd.DataFrame(
            [{"action": "ALLOW_ABC_BUY_PREVIEW", "strategy_leg": "A"}]
        )
        with patch.object(trading_daemon, "check_and_close_positions"), \
             patch.object(trading_daemon, "confirm_pending_premarket_buys"), \
             patch.object(trading_daemon, "_d_relay_pair_active_today", return_value=False), \
             patch.object(trading_daemon, "_pov_active_today", return_value=False), \
             patch.object(trading_daemon, "has_position_bought_today", return_value=False), \
             patch.object(trading_daemon, "load_pending_buys", return_value=[]), \
             patch.object(trading_daemon, "load_combined_decisions", return_value=(decisions, Path("orders.csv"))), \
             patch.object(trading_daemon, "is_strategy_l_mode", return_value=False), \
             patch.object(trading_daemon, "is_strategy_model3_mode", return_value=True), \
             patch.object(trading_daemon, "_enqueue_opening_pov_from_plan", return_value=False), \
             patch.object(trading_daemon, "job_strategy_d") as start_d, \
             patch.object(trading_daemon, "_notify"):
            trading_daemon.job_opening_buy()

        start_d.assert_not_called()

    def test_order_selector_rejects_multiple_buys_for_same_formal_leg(self) -> None:
        orders = pd.DataFrame([
            {"side": "BUY", "strategy_leg": "L", "ts_code": "600001.SH"},
            {"side": "BUY", "strategy_leg": "L", "ts_code": "600002.SH"},
            {"side": "BUY", "strategy_leg": "M", "ts_code": "000001.SZ"},
        ])
        selected = trading_daemon._select_unique_buy_order_for_action(
            orders,
            "ALLOW_MODEL3_L_SUPPLEMENT",
            context="测试唯一订单",
        )
        self.assertTrue(selected.empty)

    def test_order_selector_uses_action_leg_and_never_executes_lower_leg(self) -> None:
        orders = pd.DataFrame([
            {"side": "BUY", "strategy_leg": "L", "ts_code": "600001.SH"},
            {"side": "BUY", "strategy_leg": "M", "ts_code": "000001.SZ"},
        ])
        selected = trading_daemon._select_unique_buy_order_for_action(
            orders,
            "ALLOW_MODEL3_L_SUPPLEMENT",
            context="测试腿序订单",
        )
        self.assertEqual(selected["ts_code"].tolist(), ["600001.SH"])

    def test_d_opening_plan_classifier_covers_every_formal_leg(self) -> None:
        for action in trading_daemon.D_INTRADAY_BLOCKING_BUY_ACTIONS:
            with self.subTest(action=action):
                decisions = pd.DataFrame([{"action": action, "reason": "正式开仓"}])
                self.assertTrue(trading_daemon.blocks_d_for_opening_plan(decisions))

    def test_d_gate_requires_empty_account_and_no_other_open_plan(self) -> None:
        allowed = pd.DataFrame(
            [{"action": "ALLOW_D_INTRADAY_MONITOR", "strategy_leg": "D"}]
        )
        conflicting = pd.concat(
            [
                allowed,
                pd.DataFrame(
                    [{"action": "ALLOW_MODEL3_L_REPLACE", "strategy_leg": "L"}]
                ),
            ],
            ignore_index=True,
        )
        with patch.object(trading_daemon, "has_open_local_position", return_value=False):
            self.assertTrue(trading_daemon.d_intraday_monitor_gate(allowed)[0])
            self.assertFalse(trading_daemon.d_intraday_monitor_gate(conflicting)[0])
        with patch.object(trading_daemon, "has_open_local_position", return_value=True):
            self.assertFalse(trading_daemon.d_intraday_monitor_gate(allowed)[0])

    def test_0930_opening_review_starts_allowed_d_monitor(self) -> None:
        decisions = pd.DataFrame(
            [{"action": "ALLOW_D_INTRADAY_MONITOR", "strategy_leg": "D"}]
        )
        with patch.object(trading_daemon, "now_beijing", return_value=beijing_at(9, 30)), \
             patch.object(trading_daemon, "check_and_close_positions"), \
             patch.object(trading_daemon, "confirm_pending_premarket_buys"), \
             patch.object(trading_daemon, "_d_relay_pair_active_today", return_value=False), \
             patch.object(trading_daemon, "_pov_active_today", return_value=False), \
             patch.object(trading_daemon, "has_position_bought_today", return_value=False), \
             patch.object(trading_daemon, "has_open_local_position", return_value=False), \
             patch.object(trading_daemon, "load_pending_buys", return_value=[]), \
             patch.object(
                 trading_daemon,
                 "load_combined_decisions",
                 return_value=(decisions, Path("orders.csv")),
             ), patch.object(trading_daemon, "is_strategy_l_mode", return_value=False), \
             patch.object(trading_daemon, "job_strategy_d") as start_d:
            trading_daemon.job_opening_buy()

        start_d.assert_called_once_with()

    def test_late_opening_review_does_not_backfill_intraday_history(self) -> None:
        decisions = pd.DataFrame(
            [{"action": "ALLOW_D_INTRADAY_MONITOR", "strategy_leg": "D"}]
        )
        with patch.object(trading_daemon, "now_beijing", return_value=beijing_at(9, 31)), \
             patch.object(trading_daemon, "check_and_close_positions"), \
             patch.object(trading_daemon, "confirm_pending_premarket_buys"), \
             patch.object(trading_daemon, "_d_relay_pair_active_today", return_value=False), \
             patch.object(trading_daemon, "_pov_active_today", return_value=False), \
             patch.object(trading_daemon, "has_position_bought_today", return_value=False), \
             patch.object(trading_daemon, "has_open_local_position", return_value=False), \
             patch.object(trading_daemon, "load_pending_buys", return_value=[]), \
             patch.object(
                 trading_daemon,
                 "load_combined_decisions",
                 return_value=(decisions, Path("orders.csv")),
             ), patch.object(trading_daemon, "is_strategy_l_mode", return_value=False), \
             patch.object(trading_daemon, "job_strategy_d") as start_d, \
             patch.object(trading_daemon, "_notify") as notify:
            trading_daemon.job_opening_buy()

        start_d.assert_not_called()
        notify.assert_called_once()

    def test_startup_between_0930_and_0935_recovers_opening_chain(self) -> None:
        with patch.object(trading_daemon, "now_beijing", return_value=beijing_at(9, 32)), \
             patch.object(trading_daemon, "is_trade_day", return_value=True), \
             patch.object(trading_daemon, "job_opening_buy") as opening:
            trading_daemon.startup_catchup_strategy_d()

        opening.assert_called_once_with()

    def test_missing_0900_cache_is_rebuilt_but_late_d_fallback_is_not_started(self) -> None:
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
             ) as rebuild, patch.object(trading_daemon, "job_strategy_d") as start_d, \
             patch.object(trading_daemon, "_notify") as notify:
            trading_daemon.startup_catchup_strategy_d()

        rebuild.assert_called_once_with()
        start_d.assert_not_called()
        notify.assert_called_once()

    def test_expired_c_plan_is_display_only_and_not_current_execution(self) -> None:
        candidate = {
            "strategy": "C",
            "ts_code": "600881.SH",
            "name": "亚泰集团",
        }
        with patch.object(trading_daemon, "now_beijing", return_value=beijing_at(11, 19)), \
             patch.object(trading_daemon, "has_position_bought_today", return_value=False), \
             patch.object(trading_daemon, "has_open_local_position", return_value=False), \
             patch.object(trading_daemon, "load_pending_buys", return_value=[]), \
             patch.object(trading_daemon, "_pov_active_today", return_value=False):
            self.assertTrue(
                trading_daemon._ordinary_open_plan_expired(candidate, "20260813")
            )

        lines = trading_daemon._build_candidate_choice_lines(
            [("C", candidate, "无入围候选")],
            ["C"],
            None,
            [],
            "原C计划窗口已过且未成交，当前不追补",
        )
        self.assertIn("账户空仓时让路排序：C策略 600881.SH 亚泰集团", lines)
        self.assertIn("实际选择：不开仓｜原C计划窗口已过且未成交，当前不追补", lines)

    def test_e2_is_not_mistaken_for_expired_ordinary_open_plan(self) -> None:
        with patch.object(trading_daemon, "now_beijing", return_value=beijing_at(11, 19)):
            self.assertFalse(
                trading_daemon._ordinary_open_plan_expired(
                    {"strategy": "E2", "ts_code": "000001.SZ"}, "20260813"
                )
            )

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
