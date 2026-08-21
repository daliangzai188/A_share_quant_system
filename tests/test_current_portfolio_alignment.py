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
        self.assertFalse(self.legacy["current_executable"])
        self.assertFalse(self.legacy["strict_asof_passed"])
        self.assertFalse(self.legacy["release_eligible"])

    def test_current_certificate_uses_new_anchor(self) -> None:
        current = json.loads(
            (
                ROOT / "reports/current_portfolio_alignment/live_certification.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(current["input_start_date"], "20240630")
        self.assertEqual(current["input_end_date"], "20260630")
        self.assertTrue(current["strict_asof_passed"])
        self.assertFalse(current["release_eligible"])

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
