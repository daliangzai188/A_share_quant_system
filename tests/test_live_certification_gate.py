from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.live_certification import validate_live_certification
from tests.test_opening_position_policy import make_engine


class LiveCertificationGateTests(unittest.TestCase):
    def test_missing_certification_fails_closed_and_removes_only_buys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine = make_engine([])
            engine.project_root = Path(temporary)
            engine.config["strategy_model3"].update(
                {
                    "require_live_certification": True,
                    "certification_summary_path": "cert/missing.json",
                    "certification_expected_scenario": "expected",
                }
            )

            _state, decisions, orders = engine.build_model3_plan("20260803")

            self.assertIn("BLOCK_MODEL3_BUY_BY_CERTIFICATION", set(decisions["action"]))
            self.assertFalse(
                not orders.empty and orders["side"].astype(str).str.upper().eq("BUY").any()
            )

    def test_validator_requires_pass_and_exact_current_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "cert.json"
            config = {
                "certification_summary_path": "cert.json",
                "certification_required_status": "PASS",
                "certification_expected_scenario": "current",
            }
            path.write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "current_executable": True,
                        "scenario": "research_only",
                    }
                ),
                encoding="utf-8",
            )
            mismatch = validate_live_certification(root, config)
            self.assertFalse(mismatch.ok)
            self.assertIn("配置期望=current", mismatch.reason)

            path.write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "current_executable": True,
                        "scenario": "current",
                    }
                ),
                encoding="utf-8",
            )
            passed = validate_live_certification(root, config)
            self.assertTrue(passed.ok)


if __name__ == "__main__":
    unittest.main()
