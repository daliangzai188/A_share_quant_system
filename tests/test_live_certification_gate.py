from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import datetime as dt

from src.live_certification import (
    certification_config_sha256,
    certification_files_sha256,
    validate_live_certification,
)
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

    def test_stale_or_changed_certification_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = root / "code.py"
            inputs = root / "manifest.json"
            code.write_text("version=1\n", encoding="utf-8")
            inputs.write_text('{"data":"v1"}', encoding="utf-8")
            model3 = {
                "certification_summary_path": "cert.json",
                "certification_required_status": "PASS",
                "certification_expected_scenario": "current",
                "certification_max_age_hours": 24,
                "certification_require_hashes": True,
            }
            full_config = {"strategy_model3": model3}
            generated = dt.datetime(2026, 8, 10, tzinfo=dt.timezone.utc)
            payload = {
                "status": "PASS",
                "current_executable": True,
                "scenario": "current",
                "generated_at": generated.isoformat(),
                "config_sha256": certification_config_sha256(full_config),
                "code_files": ["code.py"],
                "code_sha256": certification_files_sha256(root, ["code.py"]),
                "input_files": ["manifest.json"],
                "input_sha256": certification_files_sha256(root, ["manifest.json"]),
            }
            (root / "cert.json").write_text(json.dumps(payload), encoding="utf-8")

            passed = validate_live_certification(
                root,
                model3,
                full_config=full_config,
                now=generated + dt.timedelta(hours=1),
            )
            self.assertTrue(passed.ok)

            code.write_text("version=2\n", encoding="utf-8")
            changed = validate_live_certification(
                root,
                model3,
                full_config=full_config,
                now=generated + dt.timedelta(hours=1),
            )
            self.assertFalse(changed.ok)
            self.assertIn("code文件", changed.reason)

            code.write_text("version=1\n", encoding="utf-8")
            stale = validate_live_certification(
                root,
                model3,
                full_config=full_config,
                now=generated + dt.timedelta(hours=25),
            )
            self.assertFalse(stale.ok)
            self.assertIn("过期", stale.reason)


if __name__ == "__main__":
    unittest.main()
