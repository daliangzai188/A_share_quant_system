from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.certify_strict_asof_portfolio import (
    EXPECTED_EQUITY_MULTIPLE,
    EXPECTED_LEG_COUNTS,
    EXPECTED_TRADE_COUNT,
)
from scripts import validate_other_live_strategies_strict as strict
from src.mechanical_compound import MECHANICAL_COMPOUND_STANDARD_ID
from src.strict_asof import STRICT_ASOF_STANDARD_ID, STRICT_DISCOVERY


ROOT = Path(__file__).resolve().parents[1]


class StrictPortfolioCertificationPolicyTests(unittest.TestCase):
    def test_official_certificate_is_strict_mechanical_and_not_releaseable(self) -> None:
        payload = json.loads(
            (ROOT / "reports/current_portfolio_alignment/live_certification.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertEqual(payload["strict_asof_standard_id"], STRICT_ASOF_STANDARD_ID)
        self.assertTrue(payload["strict_asof_passed"])
        self.assertEqual(payload["compound_standard_id"], MECHANICAL_COMPOUND_STANDARD_ID)
        self.assertEqual(payload["research_protocol"], STRICT_DISCOVERY)
        self.assertNotIn("status", payload)
        self.assertNotIn("current_executable", payload)
        self.assertNotIn("release_eligible", payload)
        self.assertNotIn("capacity_certified", payload)
        self.assertEqual(payload["input_start_date"], "20240630")
        self.assertEqual(payload["input_end_date"], "20260630")
        self.assertEqual((strict.START, strict.END), ("20240630", "20260630"))
        report = (
            ROOT / "reports/current_portfolio_alignment/strict_asof_portfolio_report.md"
        ).read_text(encoding="utf-8")
        self.assertIn("20240630～20260630", report)
        self.assertIn("STRICT_DISCOVERY", report)
        self.assertEqual(payload["executed_trade_count"], EXPECTED_TRADE_COUNT)
        self.assertAlmostEqual(payload["equity_multiple"], EXPECTED_EQUITY_MULTIPLE)
        actual = {
            "D": payload["d_trade_count"],
            "A": payload["a_trade_count"],
            "E": payload["e_trade_count"],
            "C": payload["c_trade_count"],
        }
        self.assertEqual(actual, EXPECTED_LEG_COUNTS)

        audit = json.loads(
            (ROOT / payload["strict_asof_audit_path"]).read_text(encoding="utf-8")
        )
        self.assertNotIn("release_eligible", audit)
        standalone = audit["strict_leg_standalone_metrics"]
        self.assertEqual(standalone["A"]["trade_count"], 82)
        self.assertAlmostEqual(standalone["A"]["equity_multiple"], 94.39844282719737)
        self.assertEqual(standalone["E"]["trade_count"], 74)
        self.assertAlmostEqual(standalone["E"]["equity_multiple"], 11.70378989651547)
        candidate = audit["strict_leg_candidate_metrics"]
        self.assertEqual(candidate["A"]["trade_count"], 103)
        self.assertEqual(candidate["E"]["trade_count"], 89)

    def test_legacy_identity_script_cannot_overwrite_official_certificate(self) -> None:
        source = (ROOT / "scripts/certify_current_executable_portfolio.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('LEGACY_IDENTITY_ALIGNMENT_PATH = OUTPUT_DIR / "legacy_identity_alignment.json"', source)
        self.assertNotIn('path = OUTPUT_DIR / "live_certification.json"', source)


if __name__ == "__main__":
    unittest.main()
