"""策略C第三分支退出周期从候选到实盘校验的贯通测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import pandas as pd

from scripts.run_paper_ab_filtered_daily_ops import estimate_planned_order
from src.live_order_gateway import LiveOrderGateway


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config/strategy_config.json"
THIRD_PROFILE = (
    "C_THIRD_LIMITUP30_50_RANK4_10_FD01_03_"
    "CHAIN_NOT15_AMOUNT_NOT2_3"
)


class StrategyCProfileExitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def selected(profile_id: str) -> pd.Series:
        return pd.Series(
            {
                "strategy_leg": "C",
                "signal_date": "20260827",
                "planned_action": "PLAN_BUY_T1_OPEN",
                "ts_code": "000001.SZ",
                "name": "退出测试",
                "planned_position_pct": 0.825,
                "historical_reference_next_trade_date": "20260828",
                "historical_reference_next_open": 10.0,
                "matched_condition_profile_ids": profile_id,
                "risk_flags": "无",
            }
        )

    def test_planned_order_uses_t2_for_third_branch(self) -> None:
        order = estimate_planned_order(
            self.config,
            self.selected(THIRD_PROFILE),
            "20260827",
        ).iloc[0]

        self.assertEqual(order["matched_strategy_branch_ids"], "C_BRANCH_3_PROFIT_EXPANSION")
        self.assertEqual(order["exit_rule"], "fixed_hold2_close")
        self.assertEqual(int(order["exit_signal_offset"]), 2)
        self.assertEqual(int(order["exit_n_days"]), 1)
        self.assertEqual(str(order["planned_exit_date"]), "20260831")

    def test_planned_order_keeps_t3_for_existing_branch(self) -> None:
        order = estimate_planned_order(
            self.config,
            self.selected("C_CORE_REFINEMENT_1100_1330_MULTI_OPEN"),
            "20260827",
        ).iloc[0]

        self.assertEqual(order["matched_strategy_branch_ids"], "C_BRANCH_1_CORE_REFINEMENT")
        self.assertEqual(order["exit_rule"], "fixed_hold3_close")
        self.assertEqual(int(order["exit_signal_offset"]), 3)
        self.assertEqual(int(order["exit_n_days"]), 2)
        self.assertEqual(str(order["planned_exit_date"]), "20260901")

    @staticmethod
    def gateway() -> LiveOrderGateway:
        gateway = LiveOrderGateway.__new__(LiveOrderGateway)
        gateway.live_config = {
            "max_single_order_amount": 0,
            "max_position_pct": 0.85,
            "max_total_position_pct": 0.825,
            "round_lot_size": 100,
            "limit_price_tolerance": 0.001,
            "default_price_type": "LATEST_PRICE",
            "strategy_name": "TEST",
            "reject_limit_up_buy": True,
            "reject_limit_down_sell": True,
            "duplicate_order_check": True,
            "enforce_trading_time": True,
            "allowed_exchanges": ["SZ", "SH"],
            "allow_buy": True,
            "allow_sell": True,
            "real_order_enabled": True,
        }
        gateway.is_trading_time = lambda _side: True  # type: ignore[method-assign]
        return gateway

    def test_live_preview_preserves_branch_and_exit_contract(self) -> None:
        planned = estimate_planned_order(
            self.config,
            self.selected(THIRD_PROFILE),
            "20260827",
        )
        planned.loc[:, "round_lot_shares"] = 100
        planned.loc[:, "planned_amount_by_equity"] = 1000.0
        quote = SimpleNamespace(
            last_price=10.0,
            upper_limit=11.0,
            lower_limit=9.0,
            suspended=False,
        )

        preview = self.gateway().validate_planned_orders(
            planned,
            account_cash=100000.0,
            open_orders=[],
            quote_map={"000001.SZ": quote},
            positions=[],
            account_total_asset=100000.0,
            current_market_value=0.0,
        ).iloc[0]

        self.assertEqual(preview["validation_status"], "PASS")
        self.assertEqual(preview["matched_strategy_branch_ids"], "C_BRANCH_3_PROFIT_EXPANSION")
        self.assertEqual(preview["exit_rule"], "fixed_hold2_close")
        self.assertEqual(int(preview["exit_n_days"]), 1)
        self.assertEqual(str(preview["planned_exit_date"]), "20260831")

    def test_live_preview_rejects_c_order_when_exit_contract_is_missing(self) -> None:
        planned = estimate_planned_order(
            self.config,
            self.selected(THIRD_PROFILE),
            "20260827",
        )
        planned.loc[:, "round_lot_shares"] = 100
        planned.loc[:, "planned_amount_by_equity"] = 1000.0
        planned.loc[:, "planned_exit_date"] = ""
        quote = SimpleNamespace(
            last_price=10.0,
            upper_limit=11.0,
            lower_limit=9.0,
            suspended=False,
        )

        preview = self.gateway().validate_planned_orders(
            planned,
            account_cash=100000.0,
            open_orders=[],
            quote_map={"000001.SZ": quote},
            positions=[],
            account_total_asset=100000.0,
            current_market_value=0.0,
        ).iloc[0]

        self.assertEqual(preview["validation_status"], "REJECTED")
        self.assertIn(
            "C_PLANNED_EXIT_DATE_MISSING_OR_INVALID",
            preview["reject_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
