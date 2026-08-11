from __future__ import annotations

import copy
import unittest

from src.release_compound_guard import (
    PASS_NONINFERIOR,
    REJECT_BELOW_FLOOR,
    REJECT_NOT_COMPARABLE,
    REVIEW_WITHIN_FLOOR,
    CompoundGuardError,
    enforce_release_decision,
    evaluate_certification_candidate,
    validate_policy,
)


class ReleaseCompoundGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = {
            "schema_version": 1,
            "status": "ACTIVE",
            "policy_id": "test-policy",
            "anchor": {
                "release_id": "release-anchor",
                "scenario": "current",
                "input_start_date": "20240101",
                "input_end_date": "20241231",
                "input_sha256": "input-hash",
                "initial_equity": 500000,
                "position_pct": 0.825,
                "equity_multiple": 100.0,
            },
            "hard_floor_ratio": 0.7,
            "hard_floor_multiple": 70.0,
            "automatic_release_ratio": 1.0,
            "automatic_release_multiple": 100.0,
            "accepted_certification_statuses": ["PASS", "PASS_WITH_RISK_ACCEPTANCE"],
            "comparison_requirements": {
                "same_scenario": True,
                "same_input_window": True,
                "same_input_sha256": True,
                "same_initial_equity": True,
                "same_position_pct": True,
            },
        }
        self.certification = {
            "status": "PASS",
            "scenario": "current",
            "input_start_date": "20240101",
            "input_end_date": "20241231",
            "input_sha256": "input-hash",
            "equity_multiple": 100.0,
        }
        self.runtime_config = {
            "portfolio_certification": {
                "initial_equity": 500000,
                "position_pct": 0.825,
            }
        }

    def evaluate(self, multiple: float) -> dict:
        candidate = dict(self.certification)
        candidate["equity_multiple"] = multiple
        return evaluate_certification_candidate(
            self.policy, candidate, self.runtime_config
        )

    def test_equal_or_higher_is_noninferior(self) -> None:
        equal = self.evaluate(100.0)
        higher = self.evaluate(110.0)
        self.assertEqual(equal["status"], PASS_NONINFERIOR)
        self.assertTrue(equal["automatic_release_allowed"])
        self.assertEqual(higher["status"], PASS_NONINFERIOR)

    def test_seventy_to_one_hundred_requires_manual_review(self) -> None:
        at_floor = self.evaluate(70.0)
        below_anchor = self.evaluate(99.999)
        self.assertEqual(at_floor["status"], REVIEW_WITHIN_FLOOR)
        self.assertTrue(at_floor["hard_floor_passed"])
        self.assertTrue(at_floor["manual_review_eligible"])
        self.assertEqual(below_anchor["status"], REVIEW_WITHIN_FLOOR)

    def test_below_seventy_percent_is_rejected(self) -> None:
        result = self.evaluate(69.999)
        self.assertEqual(result["status"], REJECT_BELOW_FLOOR)
        self.assertFalse(result["hard_floor_passed"])
        self.assertFalse(result["manual_review_eligible"])

    def test_different_input_is_not_comparable_even_if_return_is_higher(self) -> None:
        candidate = dict(self.certification)
        candidate["input_sha256"] = "different"
        candidate["equity_multiple"] = 200.0
        result = evaluate_certification_candidate(
            self.policy, candidate, self.runtime_config
        )
        self.assertEqual(result["status"], REJECT_NOT_COMPARABLE)
        self.assertFalse(result["automatic_release_allowed"])

    def test_policy_floor_cannot_be_silently_changed(self) -> None:
        tampered = copy.deepcopy(self.policy)
        tampered["hard_floor_ratio"] = 0.6
        with self.assertRaisesRegex(CompoundGuardError, "hard_floor_multiple"):
            validate_policy(tampered)

    def test_comparability_checks_cannot_be_disabled(self) -> None:
        tampered = copy.deepcopy(self.policy)
        tampered["comparison_requirements"]["same_position_pct"] = False
        with self.assertRaisesRegex(CompoundGuardError, "不可关闭"):
            validate_policy(tampered)

    def test_manual_review_cannot_be_released_without_explicit_acceptance(self) -> None:
        review = self.evaluate(80.0)
        with self.assertRaisesRegex(CompoundGuardError, "显式添加"):
            enforce_release_decision(review)
        enforce_release_decision(review, accept_reduction_within_floor=True)

    def test_below_floor_has_no_manual_bypass(self) -> None:
        rejected = self.evaluate(69.0)
        with self.assertRaisesRegex(CompoundGuardError, "禁止发布"):
            enforce_release_decision(
                rejected, accept_reduction_within_floor=True
            )


if __name__ == "__main__":
    unittest.main()
