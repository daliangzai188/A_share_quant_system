from __future__ import annotations

import json
import importlib.metadata
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.configure_new_windows_env import configure


# Other pure strategy tests inject a minimal dotenv stub during discovery.
# Load the installed parser under a private package name, and replace the stub
# only for this test's duration without affecting the rest of the suite.
package = importlib.metadata.distribution("python-dotenv").locate_file("dotenv")
spec = importlib.util.spec_from_file_location(
    "_a_system_test_dotenv", package / "__init__.py",
    submodule_search_locations=[str(package)],
)
dotenv = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dotenv
spec.loader.exec_module(dotenv)
dotenv_values = dotenv.dotenv_values


class NewWindowsEnvironmentTest(unittest.TestCase):
    def setUp(self):
        replacement = patch.dict(sys.modules, {"dotenv": dotenv})
        replacement.start()
        self.addCleanup(replacement.stop)

    def setup_files(self, directory, mode="read_only"):
        root = directory / "project"
        root.mkdir()
        (root / "config").mkdir()
        (root / "config/config.json").write_text('{"trade_mode":"paper"}')
        (root / "config/strategy_config.json").write_text('{}')
        (root / ".manual_stop.json").write_text('{"source":"test"}')
        private = directory / "private" / "config.json"
        private.parent.mkdir()
        private.write_text(json.dumps({"account_id": "TEST_ACCOUNT", "mode": mode}))
        return root, private

    def test_rebuild_keeps_credentials_and_protected_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, private = self.setup_files(Path(temporary))
            env = root / ".env"
            env.write_text("TUSHARE_TOKEN='test-only-token'\nBARK_URL='test-only-url'\nEXTRA=keep\n")
            protected = [root / ".manual_stop.json", root / "config/config.json", private]
            before = [p.read_bytes() for p in protected]
            qmt = Path(temporary) / "QMT" / "userdata"
            qmt.mkdir(parents=True)
            result = configure(root, private, qmt)
            values = dotenv_values(env)
            self.assertEqual(values["TUSHARE_TOKEN"], "test-only-token")
            self.assertEqual(values["BARK_URL"], "test-only-url")
            self.assertEqual(values["EXTRA"], "keep")
            self.assertEqual(values["QMT_TRANSPORT"], "qmt_inner")
            self.assertEqual(values["QMT_ACCOUNT_ID"], "TEST_ACCOUNT")
            self.assertEqual(Path(values["QMT_PATH"]), qmt.resolve())
            self.assertEqual([p.read_bytes() for p in protected], before)
            self.assertFalse(result["business_process_started"])
            self.assertFalse(result["credential_values_printed"])
            self.assertNotIn("TEST_ACCOUNT", json.dumps(result))

    def test_live_or_missing_manual_stop_rejects_without_creating_env(self):
        for mode, stopped in [("live", True), ("read_only", False)]:
            with tempfile.TemporaryDirectory() as temporary:
                root, private = self.setup_files(Path(temporary), mode)
                if not stopped:
                    (root / ".manual_stop.json").unlink()
                with self.assertRaises(RuntimeError):
                    configure(root, private)
                self.assertFalse((root / ".env").exists())

    def test_other_account_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, private = self.setup_files(Path(temporary))
            env = root / ".env"
            env.write_text("QMT_ACCOUNT_ID=OTHER_ACCOUNT\n")
            before = env.read_bytes()
            with self.assertRaises(RuntimeError):
                configure(root, private)
            self.assertEqual(env.read_bytes(), before)

    def test_example_placeholder_can_be_replaced_by_local_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, private = self.setup_files(Path(temporary))
            (root / ".env").write_text("QMT_ACCOUNT_ID=your_qmt_account_id_here\n")
            configure(root, private)
            self.assertEqual(dotenv_values(root / ".env")["QMT_ACCOUNT_ID"], "TEST_ACCOUNT")


if __name__ == "__main__":
    unittest.main()
