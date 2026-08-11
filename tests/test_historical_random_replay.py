from __future__ import annotations

import unittest

import pandas as pd

from src.historical_random_replay import (
    build_market_context,
    market_regime,
    run_random_windows,
    validate_replay_detail,
)


def sample_detail() -> pd.DataFrame:
    dates = [f"202501{day:02d}" for day in range(1, 13)]
    rows = []
    for index, date in enumerate(dates):
        executed = index in {0, 2, 4, 6, 8, 10}
        rows.append(
            {
                "signal_date": date,
                "status": "EXECUTED" if executed else "NO_CANDIDATE",
                "strategy_leg": "A" if executed else "",
                "ts_code": "000001.SZ" if executed else "",
                "exit_date": dates[min(index + 2, len(dates) - 1)] if executed else "",
                "account_return": (0.10 if index % 4 == 0 else -0.05) if executed else 0.0,
            }
        )
    return pd.DataFrame(rows)


class HistoricalRandomReplayTests(unittest.TestCase):
    def test_random_windows_are_reproducible(self) -> None:
        kwargs = {
            "window_lengths": [5],
            "samples_per_length": 4,
            "random_seed": 123,
            "sampling_mode": "balanced_start_year",
            "market_context": {
                f"202501{day:02d}": 80.0 for day in range(1, 13)
            },
            "regime_breakpoints": [50, 100, 150],
            "regime_labels": ["weak", "neutral", "strong", "very_strong"],
        }
        first = run_random_windows(sample_detail(), **kwargs)
        second = run_random_windows(sample_detail(), **kwargs)
        pd.testing.assert_frame_equal(first["windows"], second["windows"])
        self.assertEqual(len(first["windows"]), 4)
        self.assertEqual(first["windows"]["start_index"].nunique(), 4)

    def test_right_boundary_trade_does_not_read_future_return(self) -> None:
        result = run_random_windows(
            sample_detail(),
            window_lengths=[3],
            samples_per_length=10,
            random_seed=1,
            sampling_mode="uniform",
            market_context={f"202501{day:02d}": 40.0 for day in range(1, 13)},
            regime_breakpoints=[50, 100, 150],
            regime_labels=["weak", "neutral", "strong", "very_strong"],
        )
        first = result["windows"].loc[result["windows"]["start_index"].eq(0)].iloc[0]
        # 20250103发出的交易要到20250105才退出，不得进入01~03窗口收益。
        self.assertEqual(int(first["executed_signal_count"]), 2)
        self.assertEqual(int(first["complete_trade_count"]), 1)
        self.assertEqual(int(first["right_boundary_open_trade_count"]), 1)
        self.assertAlmostEqual(float(first["compound_multiple"]), 1.10)

    def test_market_context_requires_consistent_global_count(self) -> None:
        good = pd.DataFrame(
            [
                {"trade_date": "20250101", "market_limit_up_count": 88, "market_segment": "sh"},
                {"trade_date": "20250101", "market_limit_up_count": 88, "market_segment": "sz"},
            ]
        )
        self.assertEqual(build_market_context(good)["20250101"], 88.0)
        bad = good.copy()
        bad.loc[1, "market_limit_up_count"] = 89
        with self.assertRaisesRegex(ValueError, "不一致"):
            build_market_context(bad)

    def test_detail_validation_rejects_duplicate_dates(self) -> None:
        duplicated = pd.concat([sample_detail(), sample_detail().iloc[[0]]])
        with self.assertRaisesRegex(ValueError, "重复"):
            validate_replay_detail(duplicated)

    def test_market_regime_boundaries(self) -> None:
        points = [50, 100, 150]
        labels = ["weak", "neutral", "strong", "very_strong"]
        self.assertEqual(market_regime(49, points, labels), "weak")
        self.assertEqual(market_regime(50, points, labels), "neutral")
        self.assertEqual(market_regime(100, points, labels), "strong")
        self.assertEqual(market_regime(150, points, labels), "very_strong")


if __name__ == "__main__":
    unittest.main()
