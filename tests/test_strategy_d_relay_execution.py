from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.broker_adapter import QuoteSnapshot
from src.strategy_d_relay_execution import (
    calculate_auction_safe_sell_quantity,
    calculate_relay_buy_quantity,
    relay_buy_limit_price,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StrategyDRelayExecutionTests(unittest.TestCase):
    def test_auction_only_sells_quantity_supported_by_virtual_match(self) -> None:
        quote = QuoteSnapshot(
            ts_code="600001.SH",
            broker_code="600001.SH",
            bid_prices=[10.0, 9.99],
            ask_prices=[10.0, 10.01],
            bid_volumes=[1_000_000, 100_000],
            ask_volumes=[1_000_000, 20_000],
        )

        decision = calculate_auction_safe_sell_quantity(
            quote,
            position_shares=100_000,
            max_participation=0.025,
            max_unmatched_sell_ratio=0.05,
        )

        self.assertEqual(decision.quantity, 25_000)
        self.assertEqual(decision.matched_quantity, 1_000_000)
        self.assertIn("安全容量部分", decision.reason)

    def test_small_position_can_full_auction_only_when_capacity_is_sufficient(self) -> None:
        quote = QuoteSnapshot(
            ts_code="600001.SH",
            broker_code="600001.SH",
            bid_prices=[10.0, 9.99],
            ask_prices=[10.0, 10.01],
            bid_volumes=[1_000_000, 0],
            ask_volumes=[1_000_000, 0],
        )
        decision = calculate_auction_safe_sell_quantity(
            quote,
            position_shares=20_000,
            max_participation=0.025,
            max_unmatched_sell_ratio=0.05,
        )
        self.assertEqual(decision.quantity, 20_000)
        self.assertIn("整仓", decision.reason)

    def test_missing_or_sell_imbalanced_auction_book_sells_zero(self) -> None:
        missing = QuoteSnapshot(ts_code="600001.SH", broker_code="600001.SH")
        self.assertEqual(calculate_auction_safe_sell_quantity(
            missing,
            position_shares=100_000,
            max_participation=0.025,
            max_unmatched_sell_ratio=0.05,
        ).quantity, 0)

        imbalanced = QuoteSnapshot(
            ts_code="600001.SH", broker_code="600001.SH",
            bid_prices=[10.0, 9.99], ask_prices=[10.0, 10.01],
            bid_volumes=[1_000_000, 0], ask_volumes=[1_000_000, 100_000],
        )
        self.assertEqual(calculate_auction_safe_sell_quantity(
            imbalanced,
            position_shares=100_000,
            max_participation=0.025,
            max_unmatched_sell_ratio=0.05,
        ).quantity, 0)

    def test_relay_buy_never_uses_preexisting_free_cash(self) -> None:
        # 账户原有17.5万元现金，即使QMT可用现金很多，D只确认卖出2万元时，
        # 接力新仓本片的最坏委托金额也只能小于等于2万元。
        decision = calculate_relay_buy_quantity(
            target_amount=825_000,
            hard_cap_amount=850_000,
            confirmed_buy_amount=0,
            confirmed_sell_amount=20_000,
            available_cash=195_000,
            cash_buffer=1_000,
            flow_budget=100_000,
            order_price=10.0,
        )
        self.assertEqual(decision.quantity, 1_900)
        self.assertLessEqual(decision.quantity * 10.0, 20_000)
        self.assertEqual(decision.released_cash_room, 20_000)

    def test_relay_buy_respects_82_5_target_and_85_hard_cap(self) -> None:
        target_limited = calculate_relay_buy_quantity(
            target_amount=825_000,
            hard_cap_amount=850_000,
            confirmed_buy_amount=820_000,
            confirmed_sell_amount=900_000,
            available_cash=900_000,
            cash_buffer=0,
            flow_budget=100_000,
            order_price=10.0,
        )
        self.assertEqual(target_limited.quantity, 400)
        self.assertLess(target_limited.quantity * 10.0, 5_000)

        hard_limited = calculate_relay_buy_quantity(
            target_amount=900_000,
            hard_cap_amount=850_000,
            confirmed_buy_amount=846_000,
            confirmed_sell_amount=900_000,
            available_cash=900_000,
            cash_buffer=0,
            flow_budget=100_000,
            order_price=10.0,
        )
        self.assertEqual(hard_limited.quantity, 300)
        self.assertLess(hard_limited.quantity * 10.0, 4_000)

    def test_relay_buy_stops_above_open_plus_two_percent_or_at_limit(self) -> None:
        chased = QuoteSnapshot(
            ts_code="002800.SZ", broker_code="002800.SZ",
            last_price=10.21, open_price=10.0, upper_limit=11.0,
        )
        self.assertEqual(relay_buy_limit_price(chased, chase_cap=0.02)[0], 0.0)
        limited = QuoteSnapshot(
            ts_code="002800.SZ", broker_code="002800.SZ",
            last_price=11.0, open_price=10.0, upper_limit=11.0,
        )
        self.assertEqual(relay_buy_limit_price(limited, chase_cap=0.02)[0], 0.0)

    def test_production_config_enables_paired_pov_and_keeps_hard_caps(self) -> None:
        config = json.loads((PROJECT_ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        live = config["live_trade"]
        self.assertTrue(live["d_relay_paired_pov_enabled"])
        self.assertEqual(float(live["max_total_position_pct"]), 0.825)
        self.assertEqual(float(live["max_position_pct"]), 0.85)
        self.assertEqual(float(live["d_relay_auction_max_participation"]), 0.025)
        self.assertEqual(float(live["d_relay_pair_chase_cap"]), 0.02)


if __name__ == "__main__":
    unittest.main()
