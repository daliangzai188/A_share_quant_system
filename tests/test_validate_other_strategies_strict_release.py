from __future__ import annotations

import unittest

from scripts.validate_other_strategies_strict_release import decision


class OtherStrategiesStrictReleaseTest(unittest.TestCase):
    def test_compound_harm_forces_retirement(self) -> None:
        nested = {"combination_compound_non_decreasing_passed": False}

        self.assertEqual(
            decision(nested, "FROZEN_FOR_FUTURE_PAPER_OOS_ONLY"),
            "RETIRE_BY_OUTER_OOS_COMPOUND_RULE",
        )

    def test_frozen_candidate_is_paper_only(self) -> None:
        nested = {"combination_compound_non_decreasing_passed": True}

        self.assertEqual(
            decision(nested, "FROZEN_FOR_FUTURE_PAPER_OOS_ONLY"),
            "PAPER_OOS_ONLY_NOT_LIVE",
        )

    def test_non_harming_leg_without_candidate_is_not_live(self) -> None:
        nested = {"combination_compound_non_decreasing_passed": True}

        self.assertEqual(
            decision(nested, "NO_VARIANT_PASSED_TRAINING_GATE"),
            "NOT_LIVE_NO_FORWARD_CANDIDATE",
        )


if __name__ == "__main__":
    unittest.main()
