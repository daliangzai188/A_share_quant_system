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

    def test_current_certificate_uses_v12_three_year_anchor(self) -> None:
        current = json.loads(
            (
                ROOT / "reports/current_portfolio_alignment/live_certification.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(current["input_start_date"], "20230701")
        self.assertEqual(current["input_end_date"], "20260630")
        self.assertTrue(current["strict_asof_passed"])
        self.assertEqual(current["status"], "PASS")
        self.assertTrue(current["current_executable"])
        self.assertFalse(current["release_eligible"])
        self.assertTrue(current["user_approved_for_current_live"])
        self.assertEqual(current["scenario"], "acde_ced_v12_6046_formal")
        self.assertEqual(current["release_id"], "ACDE_CED_V12_6046_20260630")
        self.assertEqual(current["executed_trade_count"], 176)
        self.assertAlmostEqual(current["equity_multiple"], 6046.316593512633)
        self.assertNotIn("capacity_certified", current)

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
