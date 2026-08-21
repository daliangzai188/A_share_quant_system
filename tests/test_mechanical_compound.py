from __future__ import annotations

import unittest

import pandas as pd

from src.mechanical_compound import (
    MECHANICAL_COMPOUND_STANDARD_ID,
    MechanicalCompoundError,
    mechanical_compound,
    mechanical_compound_frame,
)


class MechanicalCompoundTests(unittest.TestCase):
    def test_compounds_actual_account_returns_in_order(self) -> None:
        result = mechanical_compound([0.10, -0.05, 0.20], initial_equity=100.0)
        self.assertEqual(result.standard_id, MECHANICAL_COMPOUND_STANDARD_ID)
        self.assertEqual(result.trade_count, 3)
        self.assertAlmostEqual(result.equity_multiple, 1.10 * 0.95 * 1.20)
        self.assertAlmostEqual(result.final_equity, 100.0 * 1.10 * 0.95 * 1.20)
        self.assertAlmostEqual(result.max_drawdown, -0.05)

    def test_missing_return_fails_instead_of_silent_drop(self) -> None:
        with self.assertRaises(MechanicalCompoundError):
            mechanical_compound([0.10, None, 0.20])

    def test_single_account_frame_requires_sorted_unique_signal_days(self) -> None:
        duplicated = pd.DataFrame(
            {
                "signal_date": ["20260102", "20260102"],
                "account_return": [0.01, 0.02],
            }
        )
        with self.assertRaises(MechanicalCompoundError):
            mechanical_compound_frame(duplicated)

        unsorted = pd.DataFrame(
            {
                "signal_date": ["20260105", "20260102"],
                "account_return": [0.01, 0.02],
            }
        )
        with self.assertRaises(MechanicalCompoundError):
            mechanical_compound_frame(unsorted)


if __name__ == "__main__":
    unittest.main()
