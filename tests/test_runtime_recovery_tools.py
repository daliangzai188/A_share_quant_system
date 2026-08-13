from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import audit_windows_power_events
from scripts import ensure_windows_runtime


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


if __name__ == "__main__":
    unittest.main()
