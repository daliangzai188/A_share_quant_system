from __future__ import annotations

import json
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import install_mac_fusion_awake as mac
from scripts import install_windows_runtime_guard as installer
from scripts import windows_session_awake_guard as awake


class WindowsAwakeTest(unittest.TestCase):
    def run_guard(self, directory, *, api_result=1, mutex_error=0):
        kernel = SimpleNamespace(
            CreateMutexW=Mock(return_value=123), CloseHandle=Mock(return_value=True),
            SetThreadExecutionState=Mock(return_value=api_result),
        )
        waiter = Mock()
        waiter.wait.side_effect = KeyboardInterrupt
        with patch.object(awake, "os", SimpleNamespace(name="nt", getpid=lambda: 456)), \
             patch.object(awake, "__file__", str(directory / "guard.py")), \
             patch.object(awake.ctypes, "WinDLL", return_value=kernel, create=True), \
             patch.object(awake.ctypes, "get_last_error", return_value=mutex_error, create=True), \
             patch.object(awake.threading, "Event", return_value=waiter):
            if mutex_error == 183:
                awake.main()
            else:
                with self.assertRaises(KeyboardInterrupt if api_result else RuntimeError):
                    awake.main()
        return kernel

    def test_only_system_sleep_is_requested_and_released_on_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kernel = self.run_guard(root)
            report = json.loads((root / "session_awake_status.json").read_text())
        flags = [call.args[0] for call in kernel.SetThreadExecutionState.call_args_list]
        self.assertEqual(flags, [0x80000001, 0x80000000])
        self.assertTrue(report["display_sleep_allowed"])
        self.assertTrue(report["screen_lock_allowed_by_guard"])
        self.assertFalse(report["authentication_or_update_policy_changed"])
        kernel.CloseHandle.assert_called_once_with(123)

    def test_api_failure_is_not_reported_as_awake(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kernel = self.run_guard(root, api_result=0)
            report = json.loads((root / "session_awake_status.json").read_text())
        self.assertEqual(report["status"], "FAILED")
        self.assertFalse(report["system_sleep_prevented"])
        kernel.CloseHandle.assert_called_once_with(123)

    def test_duplicate_guard_exits_without_changing_execution_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            kernel = self.run_guard(Path(temporary), mutex_error=183)
        kernel.SetThreadExecutionState.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(123)


class WindowsInstallTest(unittest.TestCase):
    def test_installer_copies_private_guard_and_starts_only_awake_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "private" / "session_awake_guard.py"
            pythonw = root / "pythonw.exe"
            result = subprocess.CompletedProcess([], 0, '{}', '')
            with patch.object(installer.sys, "platform", "win32"), \
                 patch.object(installer, "_session_paths", return_value=(helper, pythonw)), \
                 patch.object(installer, "_powershell", return_value=result) as execute:
                payload = installer.install()
            self.assertEqual(helper.read_bytes(), installer.SESSION_AWAKE_SOURCE.read_bytes())
        script = execute.call_args.args[0]
        self.assertIn("-RunLevel Limited", script)
        self.assertIn("-ExecutionTimeLimit ([TimeSpan]::Zero)", script)
        self.assertIn("Start-ScheduledTask -TaskName 'A_System_SessionStabilityGuard'", script)
        self.assertNotIn("Start-ScheduledTask -TaskName 'A_System_RuntimeGuard'", script)
        self.assertNotIn("configure_windows_session_stability", script)
        self.assertNotIn("-RunLevel Highest", script)
        self.assertEqual(payload["status"], "INSTALLED")

    def test_status_rejects_legacy_task_and_stale_private_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "guard.py"
            helper.write_bytes(installer.SESSION_AWAKE_SOURCE.read_bytes())
            for friendly, stale, expected in [(False, False, "OUTDATED"),
                                               (True, False, "INSTALLED"),
                                               (True, True, "OUTDATED")]:
                if stale:
                    helper.write_text("old guard")
                output = json.dumps({"session_lock_friendly": friendly})
                result = subprocess.CompletedProcess([], 0, output, '')
                with patch.object(installer.sys, "platform", "win32"), \
                     patch.object(installer, "_session_paths", return_value=(helper, root / "pythonw.exe")), \
                     patch.object(installer, "_powershell", return_value=result):
                    self.assertEqual(installer.status()["status"], expected)


class MacAwakeTest(unittest.TestCase):
    def test_pattern_matches_only_target_vm_even_with_metacharacters(self):
        vmx = Path("/Users/test/VM (copy)+[2].vmwarevm/Windows.vmx")
        pattern = mac.process_pattern(vmx)
        command = mac.VMX_EXECUTABLE + " -@ pipe -H " + str(vmx)
        self.assertIsNotNone(re.fullmatch(pattern, command))
        self.assertIsNone(re.fullmatch(pattern, command.replace("[2]", "[3]")))

    def test_install_uses_private_source_without_starting_vm(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            vmx = home / "VM test.vmx"
            vmx.write_text("test")
            with patch.object(mac.sys, "platform", "darwin"), \
                 patch.object(mac.subprocess, "run") as run:
                result = mac.install(vmx, home=home)
            plist = plistlib.loads(Path(result["plist"]).read_bytes())
            helper = Path(plist["ProgramArguments"][1])
            self.assertEqual(helper.read_bytes(), mac.SOURCE.read_bytes())
            self.assertTrue(plist["RunAtLoad"])
            self.assertTrue(plist["KeepAlive"])
            self.assertFalse(result["starts_vm"])
            self.assertFalse(result["closed_lid_supported"])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[1] for command in commands], ["bootout", "bootstrap"])

    def test_detection_refuses_to_guess_when_multiple_vms_are_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = [Path(temporary) / f"{i}.vmx" for i in range(2)]
            for path in paths:
                path.touch()
            result = subprocess.CompletedProcess([], 0, "\n".join(map(str, paths)), "")
            with patch.object(mac.subprocess, "run", return_value=result):
                with self.assertRaises(RuntimeError):
                    mac.detect_vmx()


if __name__ == "__main__":
    unittest.main()
