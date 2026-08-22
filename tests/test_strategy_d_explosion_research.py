from __future__ import annotations

import unittest

import pandas as pd

from scripts.research_strategy_d_explosion_features import (
    RULES,
    research_decision,
    simple_metrics,
    winsorized_multiple,
)


class StrategyDExplosionResearchTest(unittest.TestCase):
    def test_first_before_1400_excludes_1400_and_later(self) -> None:
        frame = pd.DataFrame(
            {
                "first_time": [135959, 140000, 143001],
                "open_times": [2, 2, 2],
            }
        )
        rule = next(item for item in RULES if item.name == "first_before_1400")

        self.assertEqual(rule.predicate(frame).tolist(), [True, False, False])

    def test_concentrated_rule_requires_open2_and_time_band(self) -> None:
        frame = pd.DataFrame(
            {
                "first_time": [105959, 110000, 132959, 133000, 120000],
                "open_times": [2, 2, 2, 2, 3],
            }
        )
        rule = next(item for item in RULES if item.name == "open2_first_1100_1330")

        self.assertEqual(rule.predicate(frame).tolist(), [False, True, True, False, False])

    def test_dual_gate_cannot_override_missing_intraday_mother_pool(self) -> None:
        self.assertEqual(
            research_decision(
                dual_gate_passed=True,
                complete_intraday_event_pool=False,
            ),
            "KEEP_CURRENT_PENDING_COMPLETE_INTRADAY_EVENT_POOL",
        )

    def test_candidate_pool_metrics_are_explicitly_diagnostic(self) -> None:
        metrics = simple_metrics(pd.Series([0.10, -0.05, 0.02]))

        self.assertEqual(metrics["sample_count"], 3)
        self.assertAlmostEqual(metrics["win_rate"], 2 / 3)
        self.assertAlmostEqual(metrics["explosion_rate_gte_10pct"], 1 / 3)
        self.assertAlmostEqual(metrics["big_loss_rate_lte_minus_5pct"], 1 / 3)

    def test_winsorized_multiple_caps_extremes(self) -> None:
        raw = pd.Series([0.01, 0.02, 1.0, -0.01, -0.02])

        self.assertLess(winsorized_multiple(raw), float((1 + raw).prod()))


if __name__ == "__main__":
    unittest.main()
