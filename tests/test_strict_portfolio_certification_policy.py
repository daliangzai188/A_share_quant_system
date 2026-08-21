from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.certify_strict_asof_portfolio import (
    EXPECTED_EQUITY_MULTIPLE,
    EXPECTED_LEG_COUNTS,
    EXPECTED_TRADE_COUNT,
)
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
        self.assertFalse(payload["release_eligible"])
        self.assertFalse(payload["current_executable"])
        self.assertEqual(payload["executed_trade_count"], EXPECTED_TRADE_COUNT)
        self.assertAlmostEqual(payload["equity_multiple"], EXPECTED_EQUITY_MULTIPLE)
        actual = {
            "D": payload["d_trade_count"],
            "A": payload["a_trade_count"],
            "E": payload["e_trade_count"],
            "C": payload["c_trade_count"],
        }
        self.assertEqual(actual, EXPECTED_LEG_COUNTS)

    def test_legacy_identity_script_cannot_overwrite_official_certificate(self) -> None:
        source = (ROOT / "scripts/certify_current_executable_portfolio.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('LEGACY_IDENTITY_ALIGNMENT_PATH = OUTPUT_DIR / "legacy_identity_alignment.json"', source)
        self.assertNotIn('path = OUTPUT_DIR / "live_certification.json"', source)


if __name__ == "__main__":
    unittest.main()
