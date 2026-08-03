from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import ModuleType

import pandas as pd

# 纯逻辑测试不读取.env；开发机未安装python-dotenv时注入无副作用最小桩。
if "dotenv" not in sys.modules:
    dotenv_stub = ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv_stub

from scripts.monitor_strategy_d_intraday import calc_shares_below_target_amount
from src.combined_live_engine import CombinedLiveEngine


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_engine(positions: list[dict]) -> CombinedLiveEngine:
    """构造不访问磁盘和券商的组合状态机，用于纯计划层回归测试。"""
    engine = object.__new__(CombinedLiveEngine)
    engine.project_root = PROJECT_ROOT
    engine.config = {
        "trade_mode": "live",
        "position": {"initial_cash": 500_000},
        "live_trade": {"max_single_order_amount": 0},
        "strategy_l": {
            "enabled": True,
            "live_order_enabled": True,
            "position_pct": 0.825,
        },
        "strategy_model3": {
            "enabled": True,
            "live_order_enabled": True,
            "selected_rule_name": "test",
        },
    }
    engine.load_positions = lambda: positions
    engine.load_latest_abc_orders = lambda: (
        Path("test_orders.csv"),
        pd.DataFrame([{
            "strategy_leg": "A",
            "side": "BUY",
            "ts_code": "002800.SZ",
            "name": "测试新候选",
            "planned_order_date": "20260803",
            "reference_price": 10.0,
            "round_lot_shares": 10_000,
            "estimated_shares": 10_000,
        }]),
    )
    engine.load_yesterday_e2_signal = lambda _today: None
    engine.load_today_e2_signal = lambda _today: None
    engine.load_today_l_signal = lambda _today: None
    engine.load_yesterday_l_signal = lambda _today: {
        "signal_date": "20260731",
        "planned_buy_date": "20260803",
        "ts_code": "300001.SZ",
        "name": "测试L候选",
        "limit_close": 10.0,
        "market_segment": "chi_next",
        "segment_retreat_state_bucket": "neutral",
        "market_chain_count_bucket": "15_30",
        "theme_limit_count": 3,
        "first_time_detail_bucket": "before_1430",
    }
    engine.active_strategy_mode = lambda: 3
    engine.active_strategy_name = lambda: "MODEL3"
    engine.is_b_strategy_removed = lambda: True
    return engine


class OpeningPositionPolicyTests(unittest.TestCase):
    def test_production_position_configuration_is_82_5_with_85_hard_cap(self) -> None:
        runtime = json.loads((PROJECT_ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        strategy = json.loads((PROJECT_ROOT / "config" / "strategy_config.json").read_text(encoding="utf-8"))

        self.assertEqual(runtime["live_trade"]["max_total_position_pct"], 0.825)
        self.assertEqual(runtime["live_trade"]["max_position_pct"], 0.85)
        self.assertFalse(runtime["live_trade"]["transition_use_full_available_cash"])
        self.assertEqual(runtime["strategy_d"]["position_pct"], 0.825)
        self.assertEqual(runtime["strategy_l"]["position_pct"], 0.825)
        self.assertEqual(runtime["live_trade"]["capacity_wall_alert_threshold"], 17_000_000)
        self.assertEqual(strategy["position"]["target_position_pct"], 0.825)
        self.assertEqual(strategy["position"]["max_total_position_pct"], 0.825)

    def test_due_non_d_position_blocks_all_new_buy_plans(self) -> None:
        position = {
            "strategy_leg": "A",
            "ts_code": "600000.SH",
            "name": "今日到期旧仓",
            "status": "open",
            "shares": 10_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260803",
        }
        engine = make_engine([position])

        _state, decisions, orders = engine.build_mode1_plan("20260803")

        self.assertFalse(
            not orders.empty and orders["side"].astype(str).str.upper().eq("BUY").any()
        )
        self.assertIn("BLOCK_ABC_BUY", set(decisions["action"]))
        self.assertIn("BLOCK_D_INTRADAY_MONITOR", set(decisions["action"]))

    def test_due_e2_position_keeps_sell_but_drops_new_buy(self) -> None:
        position = {
            "strategy_leg": "E2",
            "ts_code": "000001.SZ",
            "name": "今日到期E2",
            "status": "sell_pending",
            "shares": 5_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260803",
        }
        engine = make_engine([position])

        _state, decisions, orders = engine.build_mode1_plan("20260803")

        self.assertIn("PLAN_SELL_E2", set(decisions["action"]))
        self.assertTrue(orders["side"].astype(str).str.upper().eq("SELL").any())
        self.assertFalse(orders["side"].astype(str).str.upper().eq("BUY").any())

    def test_model3_l_cannot_supplement_while_due_non_l_position_exists(self) -> None:
        position = {
            "strategy_leg": "A",
            "ts_code": "600000.SH",
            "name": "今日到期旧仓",
            "status": "open",
            "shares": 10_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260803",
        }
        engine = make_engine([position])

        _state, decisions, orders = engine.build_model3_plan("20260803")

        self.assertIn("BLOCK_MODEL3_L_BY_HOLDING_POSITION", set(decisions["action"]))
        self.assertFalse(
            not orders.empty and orders["side"].astype(str).str.upper().eq("BUY").any()
        )

    def test_due_l_position_only_generates_sell_in_standalone_mode(self) -> None:
        position = {
            "strategy_leg": "L",
            "ts_code": "300001.SZ",
            "name": "今日到期L",
            "status": "open",
            "shares": 2_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260803",
        }
        engine = make_engine([position])
        engine.active_strategy_mode = lambda: 2
        engine.active_strategy_name = lambda: "L"

        _state, decisions, orders = engine.build_l_mode_plan("20260803")

        self.assertIn("PLAN_SELL_L", set(decisions["action"]))
        self.assertIn("BLOCK_L_BUY_BY_L_POSITION", set(decisions["action"]))
        self.assertTrue(orders["side"].astype(str).str.upper().eq("SELL").all())

    def test_d_relay_for_ac_candidate_still_sells_first_and_does_not_buy_early(self) -> None:
        position = {
            "strategy_leg": "D",
            "ts_code": "600001.SH",
            "name": "D旧仓",
            "status": "open",
            "shares": 3_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260805",
        }
        engine = make_engine([position])

        _state, decisions, orders = engine.build_mode1_plan("20260803")

        self.assertIn("PLAN_SELL_D_FIRST", set(decisions["action"]))
        self.assertTrue(orders["side"].astype(str).str.upper().eq("SELL").all())
        self.assertFalse(orders["side"].astype(str).str.upper().eq("BUY").any())

    def test_d_does_not_relay_for_l_only_candidate(self) -> None:
        position = {
            "strategy_leg": "D",
            "ts_code": "600001.SH",
            "name": "D旧仓",
            "status": "open",
            "shares": 3_000,
            "buy_date": "20260731",
            "planned_exit_date": "20260805",
        }
        engine = make_engine([position])
        engine.load_latest_abc_orders = lambda: (Path("empty.csv"), pd.DataFrame())

        _state, decisions, orders = engine.build_model3_plan("20260803")

        self.assertNotIn("PLAN_SELL_D_FIRST", set(decisions["action"]))
        self.assertIn("BLOCK_MODEL3_L_BY_HOLDING_POSITION", set(decisions["action"]))
        self.assertFalse(
            not orders.empty and orders["side"].astype(str).str.upper().eq("BUY").any()
        )

    def test_d_target_amount_rounds_down_without_exceeding_82_5_pct(self) -> None:
        shares = calc_shares_below_target_amount(825_000.0, 10.0)
        self.assertEqual(shares, 82_400)
        self.assertLess(shares * 10.0, 825_000.0)


if __name__ == "__main__":
    unittest.main()
