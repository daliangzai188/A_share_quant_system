from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.certify_acde_v12_release import (
    EXPECTED_EQUITY_MULTIPLE,
    EXPECTED_LEG_COUNTS,
    EXPECTED_TRADE_COUNT,
)
from src.mechanical_compound import MECHANICAL_COMPOUND_STANDARD_ID
from src.strict_asof import STRICT_ASOF_STANDARD_ID, STRICT_DISCOVERY


ROOT = Path(__file__).resolve().parents[1]


class StrictPortfolioCertificationPolicyTests(unittest.TestCase):
    def test_official_v12_certificate_is_strict_user_approved_and_not_oos(self) -> None:
        payload = json.loads(
            (ROOT / "reports/current_portfolio_alignment/live_certification.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertEqual(payload["strict_asof_standard_id"], STRICT_ASOF_STANDARD_ID)
        self.assertTrue(payload["strict_asof_passed"])
        self.assertEqual(payload["compound_standard_id"], MECHANICAL_COMPOUND_STANDARD_ID)
        self.assertEqual(payload["research_protocol"], STRICT_DISCOVERY)
        self.assertEqual(payload["status"], "PASS")
        self.assertTrue(payload["current_executable"])
        self.assertFalse(payload["release_eligible"])
        self.assertTrue(payload["user_approved_for_current_live"])
        self.assertNotIn("capacity_certified", payload)
        self.assertEqual(payload["input_start_date"], "20230701")
        self.assertEqual(payload["input_end_date"], "20260630")
        report = (
            ROOT / "reports/current_portfolio_alignment/strict_asof_portfolio_report.md"
        ).read_text(encoding="utf-8")
        self.assertIn("20230701～20260630", report)
        self.assertIn("STRICT_DISCOVERY", report)
        self.assertEqual(payload["executed_trade_count"], EXPECTED_TRADE_COUNT)
        self.assertAlmostEqual(payload["equity_multiple"], EXPECTED_EQUITY_MULTIPLE)
        self.assertEqual(payload["leg_counts"], EXPECTED_LEG_COUNTS)

        audit = json.loads(
            (ROOT / payload["strict_asof_audit_path"]).read_text(encoding="utf-8")
        )
        self.assertFalse(audit["release_eligible"])
        self.assertTrue(audit["user_approved_for_current_live"])
        self.assertEqual(audit["metrics"]["trade_count"], 176)
        self.assertEqual(audit["metrics"]["leg_counts"], EXPECTED_LEG_COUNTS)

    def test_legacy_identity_script_cannot_overwrite_official_certificate(self) -> None:
        source = (ROOT / "scripts/certify_current_executable_portfolio.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('LEGACY_IDENTITY_ALIGNMENT_PATH = OUTPUT_DIR / "legacy_identity_alignment.json"', source)
        self.assertNotIn('path = OUTPUT_DIR / "live_certification.json"', source)


if __name__ == "__main__":
    unittest.main()
