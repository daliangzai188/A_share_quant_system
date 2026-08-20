from __future__ import annotations

import unittest

import pandas as pd

from scripts.strategy_health_monitor import completed_n_sequence


class StrategyHealthMonitorNTests(unittest.TestCase):
    def test_only_complete_real_n_round_trips_enter_health_sequence(self) -> None:
        base = {
            "entry_date": "20260820",
            "exit_date": "20260821",
            "entry_filled_qty": 1000,
            "entry_fill_amount": 10000.0,
            "exit_filled_qty": 1000,
            "exit_fill_amount": 9500.0,
        }
        raw = pd.DataFrame([
            dict(base, trade_key="n-complete", signal_date="20260819", ts_code="000001.SZ", strategy_leg="N"),
            dict(base, trade_key="n-open", signal_date="20260819", ts_code="000002.SZ", strategy_leg="N", exit_filled_qty=0, exit_fill_amount=0.0),
            dict(base, trade_key="a-complete", signal_date="20260819", ts_code="000003.SZ", strategy_leg="A"),
        ])
        config = {
            "live_performance_report": {"active_legs": ["A", "N"]},
            "analysis": {
                "commission_rate": 0.0003,
                "transfer_fee_rate": 0.00001,
                "minimum_commission": 5.0,
                "stamp_tax_schedule": [
                    {"start_date": "20230828", "end_date": "99991231", "rate": 0.0005}
                ],
            },
        }

        sequence = completed_n_sequence(raw, config)

        self.assertEqual(len(sequence), 1)
        self.assertEqual(sequence[0][0:3], ("20260819", "000001.SZ", 1))
        self.assertLess(sequence[0][3], -0.05)


if __name__ == "__main__":
    unittest.main()
