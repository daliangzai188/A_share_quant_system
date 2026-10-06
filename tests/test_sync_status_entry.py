from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import open_sync_status as sync


class SyncthingEntryTest(unittest.TestCase):
    def test_running_service_is_reused_without_starting_another(self):
        with patch.object(sync, "gui_listening", return_value=True), \
             patch.object(sync.subprocess, "Popen") as popen:
            self.assertEqual(sync.ensure_running(), "already_running")
        popen.assert_not_called()

    def test_stopped_service_starts_before_gui_is_opened(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "Programs/Syncthing/syncthing.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.dict(sync.os.environ, {"LOCALAPPDATA": temporary}), \
                 patch.object(sync, "gui_listening", side_effect=[False, True]), \
                 patch.object(sync.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                 patch.object(sync.subprocess, "Popen") as popen:
                self.assertEqual(sync.ensure_running(), "started")
        command = popen.call_args.args[0]
        self.assertEqual(command, [str(executable), "serve", "--no-console", "--no-browser"])

    def test_private_shortcuts_start_sync_and_preserve_device_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pythonw = root / "pythonw.exe"
            pythonw.touch()
            result = subprocess.CompletedProcess([], 0, "", "")
            with patch.dict(sync.os.environ, {"LOCALAPPDATA": temporary}), \
                 patch.object(sync.sys, "platform", "win32"), \
                 patch.object(sync.sys, "executable", str(root / "python.exe")), \
                 patch.object(sync.subprocess, "run", return_value=result) as run:
                report = sync.install_shortcuts()
            self.assertEqual(Path(report["helper"]).read_bytes(), Path(sync.__file__).read_bytes())
            self.assertFalse(report["device_identity_changed"])
        script = run.call_args.args[0][-1]
        self.assertIn("--start-only", script)
        self.assertIn("GetFolderPath('Desktop')", script)
        self.assertIn("GetFolderPath('Startup')", script)
        self.assertNotIn("config.xml", script)


if __name__ == "__main__":
    unittest.main()
