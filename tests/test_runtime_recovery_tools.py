from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts import audit_windows_power_events
from scripts import ensure_windows_runtime
from scripts import win_daemon_keeper
from src.runtime_stop_state import (
    clear_manual_stop,
    load_manual_stop,
    write_manual_stop,
)


class WindowsPowerAuditTest(unittest.TestCase):
    def test_unexpected_power_events_take_priority_in_conclusion(self) -> None:
        report = audit_windows_power_events.summarize(
            [
                {"Id": 1074, "TimeCreated": "2026-08-12T22:00:00", "Message": "planned"},
                {"Id": 41, "TimeCreated": "2026-08-12T23:28:30", "Message": "unexpected"},
                {"Id": 6005, "TimeCreated": "2026-08-13T10:35:00", "Message": "boot"},
            ]
        )

        self.assertEqual(report["conclusion"], "DETECTED_UNEXPECTED_POWER_LOSS_OR_CRASH")
        self.assertEqual(report["classification_counts"]["UNEXPECTED_POWER_LOSS_OR_CRASH"], 1)

    def test_planned_shutdown_is_distinguished_from_power_loss(self) -> None:
        report = audit_windows_power_events.summarize(
            [{"Id": 1074, "ProviderName": "User32", "Message": "shutdown.exe"}]
        )

        self.assertEqual(report["conclusion"], "DETECTED_PLANNED_OR_CLEAN_SHUTDOWN")
        self.assertEqual(report["events"][0]["classification"], "PLANNED_SHUTDOWN_OR_RESTART")


class EnsureWindowsRuntimeTest(unittest.TestCase):
    def test_healthy_runtime_never_restarts(self) -> None:
        with patch.object(
            ensure_windows_runtime,
            "runtime_status",
            return_value=(True, True, 100, 200),
        ), patch.object(ensure_windows_runtime.subprocess, "run") as run, patch.object(
            ensure_windows_runtime.subprocess, "Popen"
        ) as popen, patch.object(ensure_windows_runtime.sys, "platform", "win32"):
            result = ensure_windows_runtime.ensure_runtime()

        self.assertEqual(result, 0)
        run.assert_not_called()
        popen.assert_not_called()

    def test_manual_stop_blocks_scheduled_runtime_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_manual_stop(root, source="test")
            with patch.object(
                ensure_windows_runtime,
                "PROJECT_ROOT",
                root,
            ), patch.object(
                ensure_windows_runtime,
                "runtime_status",
                return_value=(False, False, None, None),
            ), patch.object(
                ensure_windows_runtime.subprocess,
                "run",
            ) as run, patch.object(
                ensure_windows_runtime.subprocess,
                "Popen",
            ) as popen, patch.object(
                ensure_windows_runtime.sys,
                "platform",
                "win32",
            ):
                result = ensure_windows_runtime.ensure_runtime()

        self.assertEqual(result, 0)
        run.assert_not_called()
        popen.assert_not_called()

    def test_manual_start_clear_restores_automatic_recovery_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_manual_stop(root, source="test")
            self.assertIsNotNone(load_manual_stop(root))
            self.assertTrue(clear_manual_stop(root))
            self.assertIsNone(load_manual_stop(root))

    def test_keeper_never_restarts_when_manual_stop_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_manual_stop(root, source="test")
            with patch.object(
                win_daemon_keeper,
                "PROJECT_ROOT",
                root,
            ), patch.object(
                win_daemon_keeper.subprocess,
                "run",
            ) as run, patch.object(
                win_daemon_keeper,
                "log",
            ):
                started = win_daemon_keeper.start_daemon()

        self.assertFalse(started)
        run.assert_not_called()

    def test_normal_startup_auto_checks_and_installs_runtime_guard(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "start_windows.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('install_windows_runtime_guard.py', source)
        self.assertIn('"--status"', source)
        self.assertIn("每日08:15", source)
        self.assertIn('automatic_recovery = "--automatic-recovery" in sys.argv', source)
        self.assertIn("clear_manual_stop(root)", source)


if __name__ == "__main__":
    unittest.main()
