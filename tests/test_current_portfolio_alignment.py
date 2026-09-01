from __future__ import annotations

import json
from pathlib import Path
import unittest

from scripts.certify_current_executable_portfolio import (
    EXPECTED_D_DAILY_CANDIDATE_COUNT,
    load_sources,
)


ROOT = Path(__file__).resolve().parents[1]


class CurrentPortfolioAlignmentTests(unittest.TestCase):
    """旧身份回放只作历史归档；当前可复现性由严格证书测试锁定。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = load_sources()
        cls.legacy = json.loads(
            (
                ROOT
                / "reports/current_portfolio_alignment/legacy_identity_alignment.json"
            ).read_text(encoding="utf-8")
        )

    def test_legacy_alignment_cannot_be_current_or_strict(self) -> None:
        self.assertEqual(self.legacy["input_start_date"], "20240520")
        self.assertEqual(self.legacy["input_end_date"], "20260514")
        self.assertNotIn("status", self.legacy)
        self.assertNotIn("current_executable", self.legacy)
        self.assertNotIn("release_eligible", self.legacy)
        self.assertNotIn("capacity_certified", self.legacy)

    def test_current_certificate_uses_return_first_three_year_anchor(self) -> None:
        current = json.loads(
            (
                ROOT
                / "reports/current_portfolio_alignment/return_first_live_certification.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(current["window"]["start"], "20230901")
        self.assertEqual(current["window"]["end"], "20260831")
        self.assertTrue(current["strict_asof_passed"])
        self.assertEqual(current["status"], "PASS")
        self.assertTrue(current["current_executable"])
        self.assertFalse(current["release_eligible"])
        self.assertFalse(current["independent_oos_certified"])
        self.assertFalse(current["capacity_certified"])
        self.assertEqual(current["scenario"], "acde_return_first_10240_20260831_v13")
        self.assertEqual(current["release_id"], "ACDE_RETURN_FIRST_10240_20260831_V13")
        self.assertEqual(current["combo_metrics"]["trade_count"], 175)
        self.assertAlmostEqual(
            current["combo_metrics"]["equity_multiple"], 10240.653243754481
        )
        self.assertTrue(current["deterministic_double_replay"])

    def test_legacy_leg_breakdown_stays_archived(self) -> None:
        self.assertEqual(self.legacy["executed_trade_count"], 128)
        self.assertAlmostEqual(self.legacy["equity_multiple"], 1727.906227926422)
        self.assertEqual(self.legacy["d_trade_count"], 18)
        self.assertEqual(self.legacy["a_trade_count"], 45)
        self.assertEqual(self.legacy["e_trade_count"], 47)
        self.assertEqual(self.legacy["c_trade_count"], 18)


    def test_d_source_is_the_complete_daily_candidate_ledger(self) -> None:
        self.assertEqual(len(self.sources.strategy_d), EXPECTED_D_DAILY_CANDIDATE_COUNT)
        self.assertEqual(self.sources.strategy_d.index.nunique(), 45)
        self.assertIn("20241129", self.sources.strategy_d.index)
        self.assertIn("20241212", self.sources.strategy_d.index)
        self.assertIn("20250414", self.sources.strategy_d.index)


if __name__ == "__main__":
    unittest.main()
