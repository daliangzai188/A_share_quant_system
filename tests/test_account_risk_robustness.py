from __future__ import annotations

import unittest

import pandas as pd
from pandas.testing import assert_frame_equal

from src.account_risk_historical import RiskOverlaySpec, validate_inputs
from src.account_risk_robustness import (
    random_contiguous_window_results,
    summarize_random_windows,
)


class AccountRiskRobustnessTest(unittest.TestCase):
    def setUp(self) -> None:
        dates = pd.bdate_range("2026-01-01", periods=14).strftime("%Y%m%d").tolist()
        self.daily = pd.DataFrame({"signal_date": dates})
        self.trades = pd.DataFrame(
            [
                {
                    "signal_date": dates[index],
                    "exit_date": dates[index + 1],
                    "account_return": value,
                    "strategy_leg": "A",
                    "ts_code": f"000{index:03d}.SZ",
                }
                for index, value in enumerate(
                    [0.10, -0.05, -0.04, 0.08, 0.03, -0.02, 0.06, -0.03, 0.04, 0.02]
                )
            ]
        )
        self.trades, self.calendar = validate_inputs(self.trades, self.daily)

    def sample(self, seed: int) -> pd.DataFrame:
        return random_contiguous_window_results(
            self.trades,
            self.calendar,
            RiskOverlaySpec(None, 0.18, 2, 3),
            window_trade_counts=[4, 6],
            samples_per_window=20,
            seed=seed,
            retained_floor=0.7,
        )

    def test_random_windows_are_reproducible(self) -> None:
        assert_frame_equal(self.sample(123), self.sample(123))

    def test_different_seed_changes_sampled_starts(self) -> None:
        first = self.sample(123)
        second = self.sample(456)
        self.assertNotEqual(first["start_trade_index"].tolist(), second["start_trade_index"].tolist())

    def test_summary_has_one_row_per_window_size(self) -> None:
        summary = summarize_random_windows(self.sample(123))
        self.assertEqual(summary["window_trade_count"].tolist(), [4, 6])
        self.assertEqual(summary["sample_count"].tolist(), [20, 20])
        self.assertTrue(summary["retained_floor_pass_rate"].between(0, 1).all())

    def test_invalid_window_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "窗口交易笔数"):
            random_contiguous_window_results(
                self.trades,
                self.calendar,
                RiskOverlaySpec(None, 0.18, 2, 3),
                window_trade_counts=[11],
                samples_per_window=1,
                seed=1,
                retained_floor=0.7,
            )


if __name__ == "__main__":
    unittest.main()
