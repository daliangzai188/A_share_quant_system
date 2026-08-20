from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from src.live_certification import validate_live_certification
from src.strict_asof import LOCKED_OOS, STRICT_ASOF_STANDARD_ID


class LiveCertificationStrictAsOfTests(unittest.TestCase):
    def certification_config(self) -> dict[str, object]:
        return {
            "certification_summary_path": "certification.json",
            "certification_required_status": "PASS",
            "certification_expected_scenario": "strict_strategy",
            "certification_require_strict_asof": True,
        }

    @staticmethod
    def payload(audit_hash: str = "") -> dict[str, object]:
        return {
            "status": "PASS",
            "scenario": "strict_strategy",
            "current_executable": True,
            "strict_asof_standard_id": STRICT_ASOF_STANDARD_ID,
            "strict_asof_passed": True,
            "research_protocol": LOCKED_OOS,
            "release_eligible": True,
            "strict_asof_audit_path": "audit.json",
            "strict_asof_audit_sha256": audit_hash,
        }

    @staticmethod
    def write_audit(root: Path, *, protocol: str = LOCKED_OOS, eligible: bool = True) -> str:
        path = root / "audit.json"
        path.write_text(
            json.dumps(
                {
                    "standard_id": STRICT_ASOF_STANDARD_ID,
                    "strict_asof_passed": True,
                    "research_protocol": protocol,
                    "release_eligible": eligible,
                }
            ),
            encoding="utf-8",
        )
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_discovery_result_cannot_pass_live_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = self.payload(self.write_audit(root, protocol="STRICT_DISCOVERY", eligible=False))
            payload["research_protocol"] = "STRICT_DISCOVERY"
            payload["release_eligible"] = False
            (root / "certification.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            result = validate_live_certification(root, self.certification_config())
            self.assertFalse(result.ok)
            self.assertIn("开发段收益不得发布", result.reason)

    def test_locked_oos_strict_result_passes_asof_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit_hash = self.write_audit(root)
            (root / "certification.json").write_text(
                json.dumps(self.payload(audit_hash)), encoding="utf-8"
            )
            result = validate_live_certification(root, self.certification_config())
            self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
