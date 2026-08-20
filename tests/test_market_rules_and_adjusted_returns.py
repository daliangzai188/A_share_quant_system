from __future__ import annotations

import unittest

import pandas as pd

from src.adjusted_returns import linked_forward_adjusted_return
from src.market_rules import (
    fixed_close_sell_executable,
    fixed_open_buy_executable,
    limit_down_price,
    limit_up_price,
    price_limit_pct,
)
from src.trading_fees import stamp_tax_rate_for_date


class MarketRulesTests(unittest.TestCase):
    def test_board_and_st_limits(self) -> None:
        self.assertEqual(price_limit_pct("000001.SZ", trade_date="20260820"), 0.10)
        self.assertEqual(price_limit_pct("600001.SH", name="*ST测试", trade_date="20260820"), 0.05)
        self.assertEqual(price_limit_pct("301001.SZ", name="ST测试", trade_date="20260820"), 0.20)
        self.assertEqual(price_limit_pct("688001.SH", trade_date="20260820"), 0.20)
        self.assertEqual(price_limit_pct("920001.BJ", trade_date="20260820"), 0.30)

    def test_listing_no_limit_windows(self) -> None:
        self.assertIsNone(
            price_limit_pct("001999.SZ", trade_date="20260820", listing_day_number=5)
        )
        self.assertEqual(
            price_limit_pct("001999.SZ", trade_date="20260820", listing_day_number=6),
            0.10,
        )
        self.assertIsNone(
            price_limit_pct("920999.BJ", trade_date="20260820", listing_day_number=1)
        )

    def test_exchange_rounding_and_fixed_time_fill_policy(self) -> None:
        self.assertEqual(limit_up_price(10.05, 0.10), 11.06)
        self.assertEqual(limit_down_price(10.05, 0.10), 9.05)
        self.assertFalse(
            fixed_open_buy_executable(pre_close=10.05, open_price=11.06, limit_pct=0.10)
        )
        self.assertFalse(
            fixed_close_sell_executable(pre_close=10.05, close_price=9.05, limit_pct=0.10)
        )
        self.assertTrue(
            fixed_close_sell_executable(pre_close=10.05, close_price=9.06, limit_pct=0.10)
        )


class AdjustedReturnTests(unittest.TestCase):
    def test_pre_close_link_handles_ex_dividend_gap(self) -> None:
        frames = {
            "20250428": pd.DataFrame(
                [{"ts_code": "300824.SZ", "close": 11.75, "pre_close": 12.47}]
            ).set_index("ts_code"),
            "20250429": pd.DataFrame(
                [{"ts_code": "300824.SZ", "close": 11.90, "pre_close": 11.63}]
            ).set_index("ts_code"),
        }
        value = linked_forward_adjusted_return(
            ts_code="300824.SZ",
            buy_date="20250428",
            buy_price=12.87 * 1.001,
            sell_date="20250429",
            sell_price=11.90 * 0.999,
            trade_dates=["20250428", "20250429"],
            daily_loader=frames.get,
        )
        raw = 11.90 * 0.999 / (12.87 * 1.001) - 1.0
        self.assertAlmostEqual(value, -0.067695, places=5)
        self.assertGreater(value, raw + 0.009)


class TradingFeeTests(unittest.TestCase):
    def test_stamp_tax_schedule(self) -> None:
        self.assertEqual(stamp_tax_rate_for_date("20230827"), 0.001)
        self.assertEqual(stamp_tax_rate_for_date("20230828"), 0.0005)
        self.assertEqual(stamp_tax_rate_for_date("20260820"), 0.0005)


if __name__ == "__main__":
    unittest.main()
