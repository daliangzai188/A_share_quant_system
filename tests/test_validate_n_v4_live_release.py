from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from scripts.validate_n_v4_live_release import n_v4_live_config_errors


class ValidateNV4LiveReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.config = json.loads(
            (root / "config" / "config.json").read_text(encoding="utf-8")
        )

    def test_current_config_passes_n_v4_semantic_gate(self) -> None:
        self.assertEqual(n_v4_live_config_errors(self.config), [])

    def test_pause_or_disable_blocks_release(self) -> None:
        for field, value in (
            ("enabled", False),
            ("live_order_enabled", False),
            ("entry_pause", True),
        ):
            with self.subTest(field=field):
                config = copy.deepcopy(self.config)
                config["strategy_n"][field] = value
                self.assertTrue(n_v4_live_config_errors(config))

    def test_trade_count_drift_blocks_release(self) -> None:
        config = copy.deepcopy(self.config)
        config["portfolio_certification"]["live_candidate_metrics"]["trade_count"] = 165
        self.assertIn("正式组合交易笔数不是166", n_v4_live_config_errors(config))

    def test_windows_deploy_locks_native_exit_code_before_logging(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "deploy_n_v4_windows.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "$NativeOutput = & py -3.11 @CommandArguments 2>&1", script
        )
        self.assertIn("$NativeExitCode = $LASTEXITCODE", script)
        self.assertIn("$null -eq $NativeExitCode", script)
        self.assertNotIn("2>&1 | Tee-Object", script)
        self.assertIn("param([switch]$PreflightOnly)", script)
        self.assertIn('Write-DeployLog "DEPLOY_PREFLIGHT_COMPLETE"', script)


if __name__ == "__main__":
    unittest.main()
