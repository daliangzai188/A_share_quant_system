from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import install_windows_runtime_guard as guard
from src.qmt_inner_start_gate import assert_selected_transport_ready


class RuntimeGuardEncodingTest(unittest.TestCase):
    def run_main(self, *, payload=None, error=None):
        with tempfile.TemporaryDirectory() as temporary:
            raw_stdout = io.BytesIO()
            raw_stderr = io.BytesIO()
            stdout = io.TextIOWrapper(raw_stdout, encoding="gbk", errors="strict")
            stderr = io.TextIOWrapper(raw_stderr, encoding="gbk", errors="strict")
            report_path = Path(temporary) / "status.json"
            with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr), \
                    patch.object(sys, "argv", ["guard", "--status"]), \
                    patch.object(guard, "REPORT_PATH", report_path), \
                    patch.object(guard, "status", return_value=payload, side_effect=error):
                result = guard.main()
                stdout.flush()
                output = json.loads(raw_stdout.getvalue().decode("utf-8"))
                report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None
            return result, output, report

    def test_gbk_pipe_preserves_original_error_and_returns_failure(self):
        original = "计划任务安装失败：拒绝访问 \ufffd"
        result, output, report = self.run_main(error=RuntimeError(original))
        self.assertEqual(result, 1)
        self.assertEqual(output, {"status": "ERROR", "reason": original})
        self.assertIsNone(report)

    def test_gbk_pipe_preserves_status_json_and_utf8_report(self):
        payload = {"status": "NOT_INSTALLED", "reason": "任务不存在 \ufffd"}
        result, output, report = self.run_main(payload=payload)
        self.assertEqual(result, 1)
        self.assertEqual(output, payload)
        self.assertEqual(report, payload)

    def test_malformed_exception_text_cannot_hide_original_error(self):
        original = "错误 \udcff"
        result, output, _ = self.run_main(error=RuntimeError(original))
        self.assertEqual(result, 1)
        self.assertEqual(output["reason"], original)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows PowerShell")
    def test_actual_powershell_chinese_output_decodes_as_utf8(self):
        # Build Chinese text from ASCII so the command-line encoding cannot
        # influence this check of PowerShell's redirected stdout and stderr.
        result = guard._powershell(
            "$message = [string][char]0x62d2 + [char]0x7edd + [char]0x8bbf + [char]0x95ee; "
            "[Console]::Out.WriteLine($message); [Console]::Error.WriteLine($message)"
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "拒绝访问")
        self.assertEqual(result.stderr.strip(), "拒绝访问")


class QMTTransportSelectionTest(unittest.TestCase):
    def test_existing_inner_config_without_selection_keeps_manual_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_config = root / "inner.json"
            private_config.write_text("{}", encoding="utf-8")
            marker = root / ".manual_stop.json"
            marker.write_text('{"source":"migration"}', encoding="utf-8")
            before = marker.read_bytes()
            with patch.dict(os.environ, {"QMT_INNER_CONFIG": str(private_config)}, clear=True), \
                    patch("qmt_inner.protocol.FileClient") as client:
                with self.assertRaisesRegex(RuntimeError, "QMT_TRANSPORT=qmt_inner"):
                    assert_selected_transport_ready(root)
                client.assert_not_called()
            self.assertEqual(marker.read_bytes(), before)

    def test_blank_transport_is_not_implicit_approval_for_miniqmt(self):
        with tempfile.TemporaryDirectory() as temporary:
            private_config = Path(temporary) / "inner.json"
            private_config.write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"QMT_INNER_CONFIG": str(private_config), "QMT_TRANSPORT": " "}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "未明确设置QMT_TRANSPORT"):
                    assert_selected_transport_ready(temporary)

    def test_original_miniqmt_selection_remains_explicitly_available(self):
        with patch.dict(os.environ, {"QMT_TRANSPORT": "miniqmt"}, clear=True):
            self.assertEqual(assert_selected_transport_ready(Path.cwd()),
                             {"transport": "miniqmt", "checked": False})

    def test_original_default_remains_when_no_inner_config_exists(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(assert_selected_transport_ready(Path.cwd()),
                             {"transport": "miniqmt", "checked": False})


if __name__ == "__main__":
    unittest.main()
