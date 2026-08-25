from __future__ import annotations

import unittest

import pandas as pd

from scripts.research_strategy_a_current_window import (
    StrategyACurrentWindowResearch,
    TOP_DEVELOPMENT_CANDIDATES,
)


class StrategyACurrentWindowSelectionTest(unittest.TestCase):
    @staticmethod
    def gate_row(variant: str, *, combo_drawdown: float) -> dict[str, object]:
        row: dict[str, object] = {
            "variant": variant,
            "candidate_ok_rate": 1.0,
            "changed_signal_dates": 3,
        }
        multiplier = 1.0 if variant == "BASELINE_CURRENT_A" else 1.10
        for period in ("full", "development", "validation_2025h2", "test_2026h1"):
            row[f"{period}_a_trade_count"] = 20
            row[f"{period}_a_equity_multiple"] = 2.0 * multiplier
            row[f"{period}_combo_equity_multiple"] = 3.0 * multiplier
            row[f"{period}_a_max_drawdown"] = -0.10
            row[f"{period}_combo_max_drawdown"] = combo_drawdown
        return row

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

    def test_combo_drawdown_worsening_blocks_research_gate(self) -> None:
        frame = pd.DataFrame(
            [
                self.gate_row("BASELINE_CURRENT_A", combo_drawdown=-0.10),
                self.gate_row("COMPOUND_UP_BUT_COMBO_RISK_BAD", combo_drawdown=-0.14),
            ]
        )
        research = StrategyACurrentWindowResearch.__new__(
            StrategyACurrentWindowResearch
        )

        result = research._add_comparison_and_gates(frame)
        candidate = result[
            result["variant"].eq("COMPOUND_UP_BUT_COMBO_RISK_BAD")
        ].iloc[0]

        self.assertFalse(bool(candidate["development_gate_passed"]))
        self.assertFalse(bool(candidate["full_gate_passed"]))


if __name__ == "__main__":
    unittest.main()
