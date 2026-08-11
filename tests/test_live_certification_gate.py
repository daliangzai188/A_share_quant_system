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
    validate_strategy_release_freeze,
)
from tests.test_opening_position_policy import make_engine
from scripts.certify_current_executable_portfolio import resolve_m_release_status


class LiveCertificationGateTests(unittest.TestCase):
    @staticmethod
    def _freeze_payload(certification: dict, *, oos_start: str = "20260811") -> dict:
        return {
            "schema_version": 1,
            "status": "FROZEN",
            "release_id": "model3-20260811-v1",
            "frozen_at": "2026-08-11T00:00:00+00:00",
            "strategy_priority_order": ["D", "L", "A", "M", "E2", "C"],
            "research_input_end_date": certification["input_end_date"],
            "oos_start_date": oos_start,
            "certification_status": certification["status"],
            "certification_scenario": certification["scenario"],
            "config_sha256": certification["config_sha256"],
            "code_sha256": certification["code_sha256"],
            "input_sha256": certification["input_sha256"],
        }

    def test_m风险接受使用独立认证状态且不伪造非劣通过(self) -> None:
        status = resolve_m_release_status(
            m_live_enabled=True,
            m_noninferior=False,
            risk_accepted=True,
            noninferiority_reason="2024回撤变差",
        )
        self.assertEqual(status, "PASS_WITH_RISK_ACCEPTANCE")

        with self.assertRaisesRegex(RuntimeError, "2024回撤变差"):
            resolve_m_release_status(
                m_live_enabled=True,
                m_noninferior=False,
                risk_accepted=False,
                noninferiority_reason="2024回撤变差",
            )

    def test_validator_accepts_configured_risk_acceptance_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {
                "certification_summary_path": "cert.json",
                "certification_required_status": "PASS_WITH_RISK_ACCEPTANCE",
                "certification_expected_scenario": "current_with_m_gap_leg",
            }
            (root / "cert.json").write_text(
                json.dumps(
                    {
                        "status": "PASS_WITH_RISK_ACCEPTANCE",
                        "current_executable": True,
                        "scenario": "current_with_m_gap_leg",
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(validate_live_certification(root, config).ok)

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

    def test_release_freeze_binds_certification_hashes_order_and_oos_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            certification = {
                "status": "PASS_WITH_RISK_ACCEPTANCE",
                "scenario": "current_with_m_gap_leg",
                "input_end_date": "20260514",
                "config_sha256": "config-v1",
                "code_sha256": "code-v1",
                "input_sha256": "input-v1",
            }
            model3 = {
                "strategy_release_freeze_path": "freeze.json",
                "strategy_priority_order": ["D", "L", "A", "M", "E2", "C"],
            }
            (root / "freeze.json").write_text(
                json.dumps(self._freeze_payload(certification)), encoding="utf-8"
            )
            passed = validate_strategy_release_freeze(
                root,
                model3,
                certification,
                now=dt.datetime(2026, 8, 11, 1, tzinfo=dt.timezone.utc),
            )
            self.assertTrue(passed.ok)
            self.assertIn("model3-20260811-v1", passed.reason)

            changed = dict(certification, code_sha256="code-v2")
            mismatch = validate_strategy_release_freeze(
                root,
                model3,
                changed,
                now=dt.datetime(2026, 8, 11, 1, tzinfo=dt.timezone.utc),
            )
            self.assertFalse(mismatch.ok)
            self.assertIn("code_sha256", mismatch.reason)

            invalid_oos = self._freeze_payload(certification, oos_start="20260514")
            (root / "freeze.json").write_text(json.dumps(invalid_oos), encoding="utf-8")
            invalid = validate_strategy_release_freeze(
                root,
                model3,
                certification,
                now=dt.datetime(2026, 8, 11, 1, tzinfo=dt.timezone.utc),
            )
            self.assertFalse(invalid.ok)
            self.assertIn("必须晚于", invalid.reason)

    def test_live_certification_fails_closed_when_required_freeze_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model3 = {
                "certification_summary_path": "cert.json",
                "certification_required_status": "PASS",
                "certification_expected_scenario": "current",
                "require_strategy_release_freeze": True,
                "strategy_release_freeze_path": "missing-freeze.json",
                "strategy_priority_order": ["D", "L", "A", "M", "E2", "C"],
            }
            (root / "cert.json").write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "current_executable": True,
                        "scenario": "current",
                    }
                ),
                encoding="utf-8",
            )
            check = validate_live_certification(root, model3)
            self.assertFalse(check.ok)
            self.assertIn("冻结清单不存在", check.reason)


if __name__ == "__main__":
    unittest.main()
