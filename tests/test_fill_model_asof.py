from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.score_limit_up_fill_probability import default_output_path
from src.fill_model import FillProbabilityEstimator


def sample(day: int, turnover: float) -> dict[str, object]:
    return {
        "trade_date": f"202401{day:02d}",
        "ts_code": f"000{day:03d}.SZ",
        "limit_times": 1,
        "board_type": "opened",
        "first_time_bucket": "morning",
        "market_sentiment_level": "neutral",
        "segment_market_sentiment_level": "neutral",
        "market_segment": "sz_main",
        "limit_up_count": 50,
        "turnover_rate": turnover,
        "circ_mv": 100000.0,
        "fd_amount": 0.0,
        "fd_amount_to_circ_mv": 0.0,
        "is_fd_amount_abnormal": False,
    }


class HistoricalAsOfFillTests(unittest.TestCase):
    def test_asof_cli_defaults_to_separate_output(self) -> None:
        config = {
            "output_limit_up_fill_scored_path": "live.csv",
            "output_historical_asof_fill_scored_path": "historical.csv",
        }
        self.assertEqual(
            default_output_path(config, historical_asof=True), "historical.csv"
        )
        self.assertEqual(
            default_output_path(config, historical_asof=False), "live.csv"
        )

    def test_appending_future_rows_does_not_change_past_scores(self) -> None:
        estimator = FillProbabilityEstimator("config/config.json")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = pd.DataFrame([sample(index, float(index)) for index in range(1, 32)])
            extended = pd.concat([base, pd.DataFrame([sample(32, 9999.0)])], ignore_index=True)
            base_path = root / "base.csv"
            extended_path = root / "extended.csv"
            base.to_csv(base_path, index=False)
            extended.to_csv(extended_path, index=False)
            base_out = root / "base_out.csv"
            extended_out = root / "extended_out.csv"
            estimator.score_limit_up_table_asof(base_path, base_out, 412500.0)
            estimator.score_limit_up_table_asof(extended_path, extended_out, 412500.0)
            dtypes = {
                "trade_date": str,
                "ts_code": str,
                "model_training_end_date": str,
            }
            left = pd.read_csv(base_out, dtype=dtypes)
            right = pd.read_csv(extended_out, dtype=dtypes)
            right = right[right["trade_date"].isin(left["trade_date"])]
            columns = [
                "trade_date", "ts_code", "sample_count", "suggested_turnover_rate",
                "fill_space_ratio", "fill_probability", "model_training_end_date",
            ]
            pd.testing.assert_frame_equal(
                left[columns].reset_index(drop=True),
                right[columns].reset_index(drop=True),
                check_dtype=False,
            )
            last = left.iloc[-1]
            self.assertEqual(int(last["sample_count"]), 30)
            self.assertEqual(str(last["model_training_end_date"]), "20240130")
            self.assertEqual(str(last["fill_probability_method"]), "asof_turnover_space_proxy_v2")


if __name__ == "__main__":
    unittest.main()
