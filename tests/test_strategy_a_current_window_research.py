from __future__ import annotations

import unittest

import pandas as pd

from scripts.research_strategy_a_current_window import (
    StrategyACurrentWindowResearch,
    TOP_DEVELOPMENT_CANDIDATES,
)


class StrategyACurrentWindowSelectionTest(unittest.TestCase):
    def test_selection_uses_development_rank_then_validation_gate(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "variant": "BASELINE_CURRENT_A",
                    "development_gate_passed": True,
                    "validation_gate_passed": True,
                    "development_score": 0.0,
                    "development_a_compound_uplift": 0.0,
                    "test_a_equity_multiple": 999.0,
                },
                {
                    "variant": "DEV_FIRST_VALIDATION_FAIL",
                    "development_gate_passed": True,
                    "validation_gate_passed": False,
                    "development_score": 0.30,
                    "development_a_compound_uplift": 0.20,
                    "test_a_equity_multiple": 9999.0,
                },
                {
                    "variant": "DEV_SECOND_VALIDATION_PASS",
                    "development_gate_passed": True,
                    "validation_gate_passed": True,
                    "development_score": 0.20,
                    "development_a_compound_uplift": 0.10,
                    "test_a_equity_multiple": 0.01,
                },
            ]
        )
        selected, shortlist = StrategyACurrentWindowResearch.choose_sequential_candidate(frame)
        self.assertEqual(selected, "DEV_SECOND_VALIDATION_PASS")
        self.assertEqual(
            shortlist,
            ["DEV_FIRST_VALIDATION_FAIL", "DEV_SECOND_VALIDATION_PASS"][:TOP_DEVELOPMENT_CANDIDATES],
        )

    def test_no_validated_candidate_keeps_current_baseline(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "variant": "BASELINE_CURRENT_A",
                    "development_gate_passed": True,
                    "validation_gate_passed": True,
                    "development_score": 0.0,
                    "development_a_compound_uplift": 0.0,
                },
                {
                    "variant": "ONLY_DEVELOPMENT_WINNER",
                    "development_gate_passed": True,
                    "validation_gate_passed": False,
                    "development_score": 0.20,
                    "development_a_compound_uplift": 0.10,
                },
            ]
        )
        selected, shortlist = StrategyACurrentWindowResearch.choose_sequential_candidate(frame)
        self.assertEqual(selected, "BASELINE_CURRENT_A")
        self.assertEqual(shortlist, ["ONLY_DEVELOPMENT_WINNER"])


if __name__ == "__main__":
    unittest.main()
