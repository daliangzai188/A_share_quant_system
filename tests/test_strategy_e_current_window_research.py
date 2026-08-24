from __future__ import annotations

import unittest

import pandas as pd

from scripts.research_strategy_e_current_window import (
    StrategyECurrentWindowResearch,
    TOP_DEVELOPMENT_CANDIDATES,
)


class StrategyECurrentWindowSelectionTests(unittest.TestCase):
    @staticmethod
    def _stable_row(
        variant: str,
        *,
        e_multiple: float,
        combo_multiple: float,
        minimum_uplift: float,
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "variant": variant,
            "candidate_ok_rate": 1.0,
            "full_e_equity_multiple": e_multiple,
            "full_combo_equity_multiple": combo_multiple,
            "development_e_compound_uplift": minimum_uplift,
            "validation_2025h2_e_compound_uplift": minimum_uplift + 0.01,
            "test_2026h1_e_compound_uplift": minimum_uplift + 0.02,
        }
        for period in ("development", "validation_2025h2", "test_2026h1"):
            row[f"{period}_e_trade_count"] = 10
            row[f"{period}_e_equity_multiple"] = e_multiple
            row[f"{period}_combo_equity_multiple"] = combo_multiple
            row[f"{period}_e_max_drawdown"] = -0.10
            row[f"{period}_combo_max_drawdown"] = -0.08
        return row

    def test_selection_uses_development_order_then_validation_gate(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "variant": "BASELINE_CURRENT_E",
                    "development_gate_passed": True,
                    "validation_2025h2_gate_passed": True,
                    "development_score": 0.0,
                    "development_e_compound_uplift": 0.0,
                    "test_2026h1_e_equity_multiple": 999.0,
                },
                {
                    "variant": "DEV_FIRST_VALIDATION_FAIL",
                    "development_gate_passed": True,
                    "validation_2025h2_gate_passed": False,
                    "development_score": 0.30,
                    "development_e_compound_uplift": 0.20,
                    "test_2026h1_e_equity_multiple": 9999.0,
                },
                {
                    "variant": "DEV_SECOND_VALIDATION_PASS",
                    "development_gate_passed": True,
                    "validation_2025h2_gate_passed": True,
                    "development_score": 0.20,
                    "development_e_compound_uplift": 0.10,
                    "test_2026h1_e_equity_multiple": 0.01,
                },
            ]
        )

        selected, shortlist = (
            StrategyECurrentWindowResearch.choose_sequential_candidate(frame)
        )

        self.assertEqual(selected, "DEV_SECOND_VALIDATION_PASS")
        self.assertEqual(
            shortlist,
            ["DEV_FIRST_VALIDATION_FAIL", "DEV_SECOND_VALIDATION_PASS"][
                :TOP_DEVELOPMENT_CANDIDATES
            ],
        )

    def test_test_period_never_changes_sequential_selection(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "variant": "BASELINE_CURRENT_E",
                    "development_gate_passed": True,
                    "validation_2025h2_gate_passed": True,
                    "development_score": 0.0,
                    "development_e_compound_uplift": 0.0,
                    "test_2026h1_e_equity_multiple": 1.0,
                },
                {
                    "variant": "LOW_TEST_BUT_VALIDATED",
                    "development_gate_passed": True,
                    "validation_2025h2_gate_passed": True,
                    "development_score": 0.2,
                    "development_e_compound_uplift": 0.1,
                    "test_2026h1_e_equity_multiple": 0.01,
                },
                {
                    "variant": "HIGH_TEST_NOT_VALIDATED",
                    "development_gate_passed": True,
                    "validation_2025h2_gate_passed": False,
                    "development_score": 0.1,
                    "development_e_compound_uplift": 0.05,
                    "test_2026h1_e_equity_multiple": 9999.0,
                },
            ]
        )

        selected, _ = StrategyECurrentWindowResearch.choose_sequential_candidate(frame)

        self.assertEqual(selected, "LOW_TEST_BUT_VALIDATED")

    def test_no_development_candidate_keeps_current_baseline(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "variant": "BASELINE_CURRENT_E",
                    "development_gate_passed": True,
                    "validation_2025h2_gate_passed": True,
                    "development_score": 0.0,
                    "development_e_compound_uplift": 0.0,
                },
                {
                    "variant": "ONLY_FULL_WINDOW_WINNER",
                    "development_gate_passed": False,
                    "validation_2025h2_gate_passed": True,
                    "development_score": 0.3,
                    "development_e_compound_uplift": 0.2,
                },
            ]
        )

        selected, shortlist = (
            StrategyECurrentWindowResearch.choose_sequential_candidate(frame)
        )

        self.assertEqual(selected, "BASELINE_CURRENT_E")
        self.assertEqual(shortlist, [])

    def test_stable_challenger_allows_equal_combo_but_rejects_degradation(self) -> None:
        baseline = self._stable_row(
            "BASELINE_CURRENT_E",
            e_multiple=1.0,
            combo_multiple=1.0,
            minimum_uplift=0.0,
        )
        stable = self._stable_row(
            "STABLE_EQUAL_COMBO",
            e_multiple=1.1,
            combo_multiple=1.2,
            minimum_uplift=0.01,
        )
        for period in ("development", "validation_2025h2", "test_2026h1"):
            stable[f"{period}_combo_equity_multiple"] = 1.0
        # 开发段组合低于基准，即使全窗更高也不能归为“分段非劣”。
        unstable = self._stable_row(
            "UNSTABLE_SEGMENT_DEGRADATION",
            e_multiple=1.3,
            combo_multiple=1.4,
            minimum_uplift=0.20,
        )
        unstable["development_combo_equity_multiple"] = 0.99

        selected = StrategyECurrentWindowResearch._stable_noninferiority_challenger(
            pd.DataFrame([baseline, stable, unstable])
        )

        self.assertEqual(selected, "STABLE_EQUAL_COMBO")


if __name__ == "__main__":
    unittest.main()
