from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.certify_current_executable_portfolio import lock_or_verify_input_manifest


class PortfolioManifestPolicyTests(unittest.TestCase):
    def test_ac_builder_is_portable_and_certification_has_no_empirical_calibration(self) -> None:
        root = Path(__file__).resolve().parents[1]
        builder = (root / "scripts" / "build_ac_daily_candidates.py").read_text(encoding="utf-8")
        certify = (root / "scripts" / "certify_current_executable_portfolio.py").read_text(encoding="utf-8")
        self.assertIn("Path(__file__).resolve().parents[1]", builder)
        self.assertNotIn("/Users/user/Desktop/A_System", builder)
        self.assertNotIn("AC_CALIB_K", certify)
        self.assertIn("AC_BUY_FEE_RATE", certify)
        self.assertIn("AC_SELL_FEE_RATE", certify)

    def test_default_only_verifies_and_refresh_must_be_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            first = {"schema_version": 1, "files": [{"path": "a.csv", "sha256": "v1"}]}
            with self.assertRaises(FileNotFoundError):
                lock_or_verify_input_manifest(path, first, refresh=False)
            lock_or_verify_input_manifest(path, first, refresh=True)
            lock_or_verify_input_manifest(path, first, refresh=False)

            changed = {"schema_version": 1, "files": [{"path": "a.csv", "sha256": "v2"}]}
            with self.assertRaises(RuntimeError):
                lock_or_verify_input_manifest(path, changed, refresh=False)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), first)
            lock_or_verify_input_manifest(path, changed, refresh=True)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), changed)


if __name__ == "__main__":
    unittest.main()
