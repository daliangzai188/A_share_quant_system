from __future__ import annotations

import unittest

import pandas as pd

from src.live_performance import completed_live_trades, rolling_metrics


class LivePerformanceTests(unittest.TestCase):
    def test_incomplete_trade_is_excluded_and_fees_are_deducted(self) -> None:
        raw = pd.DataFrame(
            [
                {
                    "trade_key": "1",
                    "entry_date": "20260801",
                    "exit_date": "20260802",
                    "ts_code": "000001.SZ",
                    "strategy_leg": "A",
                    "entry_filled_qty": 1000,
                    "entry_fill_amount": 10000,
                    "exit_filled_qty": 1000,
                    "exit_fill_amount": 11000,
                    "total_slippage_bps": 10,
                },
                {
                    "trade_key": "2",
                    "entry_date": "20260803",
                    "exit_date": "20260804",
                    "ts_code": "000002.SZ",
                    "strategy_leg": "C",
                    "entry_filled_qty": 1000,
                    "entry_fill_amount": 10000,
                    "exit_filled_qty": 1000,
                    "exit_fill_amount": 0,
                    "total_slippage_bps": 0,
                },
            ]
        )
        trades, quality = completed_live_trades(raw, {})
        self.assertEqual(len(trades), 1)
        self.assertEqual(quality["incomplete_trade_rows"], 1)
        self.assertLess(float(trades.iloc[0]["net_return"]), 0.10)
        self.assertGreater(float(trades.iloc[0]["estimated_fees"]), 0)

    def test_rolling_metrics_exposes_actual_pnl_and_loss_streak(self) -> None:
        trades = pd.DataFrame(
            {
                "strategy_leg": ["A", "A", "L"],
                "net_return": [0.10, -0.05, -0.02],
                "net_pnl": [100.0, -50.0, -20.0],
                "entry_fill_amount": [1000.0, 1000.0, 1000.0],
                "total_slippage_bps": [10.0, 20.0, 0.0],
            }
        )
        metrics = rolling_metrics(trades, [2])
        overall = metrics.iloc[0]
        self.assertEqual(int(overall["sample_count"]), 3)
        self.assertEqual(int(overall["max_consecutive_losses"]), 2)
        self.assertAlmostEqual(float(overall["total_net_pnl"]), 30.0)
        l_row = metrics[metrics["segment"].eq("策略L")].iloc[0]
        self.assertAlmostEqual(float(l_row["hypothetical_max_drawdown"]), -0.02)


if __name__ == "__main__":
    unittest.main()
