from __future__ import annotations

import json
from pathlib import Path
import unittest

import pandas as pd

from src.strategy_n import load_n_spec, select_n_daily_picks


ROOT = Path(__file__).resolve().parents[1]


def row(**overrides):
    value = {
        "trade_date": "20260818", "ts_code": "300001.SZ", "name": "测试",
        "limit_close": 10.0, "market_segment": "chi_next",
        "allow_buy_reliable": True, "is_fill_score_reliable": True,
        "is_fd_amount_abnormal": False, "strategy_compatible": True,
        "fill_probability": 0.8, "segment_limit_max_height_bucket": "1",
        "segment_retreat_state_bucket": "retreat_weak",
        "first_time_minutes": 600, "circ_mv": 10000,
    }
    value.update(overrides)
    return value


class StrategyNTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        cls.spec = load_n_spec(cls.config)

    def test_exact_condition_and_rank_order(self) -> None:
        frame = pd.DataFrame([
            row(ts_code="300003.SZ", first_time_minutes=610, circ_mv=5000),
            row(ts_code="300002.SZ", first_time_minutes=590, circ_mv=9000),
            row(ts_code="300001.SZ", first_time_minutes=590, circ_mv=8000),
            row(ts_code="300004.SZ", segment_limit_max_height_bucket="2", first_time_minutes=500),
        ])
        pick = select_n_daily_picks(frame, self.spec, signal_date="20260818")
        self.assertEqual(len(pick), 1)
        self.assertEqual(str(pick.iloc[0]["ts_code"]), "300001.SZ")

    def test_reliability_and_fill_probability_are_fail_closed(self) -> None:
        frame = pd.DataFrame([
            row(allow_buy_reliable=False),
            row(ts_code="300002.SZ", fill_probability=0.59),
        ])
        self.assertTrue(
            select_n_daily_picks(frame, self.spec, signal_date="20260818").empty
        )

    def test_locked_historical_candidate_ledger_is_exact(self) -> None:
        locked = pd.read_csv(
            ROOT / "reports" / "strategy_n" / "n_backtest_candidates.csv",
            dtype={"trade_date": str},
            low_memory=False,
        )
        self.assertEqual(len(locked), 46)
        self.assertEqual(locked["trade_date"].nunique(), 46)
        self.assertTrue(locked["execution_status"].eq("OK").all())
        self.assertTrue(locked["sample_scope"].eq("COMPLETE_DAILY_CANDIDATES").all())


if __name__ == "__main__":
    unittest.main()
