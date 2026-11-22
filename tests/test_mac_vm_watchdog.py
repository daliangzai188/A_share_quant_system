from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import install_mac_vm_watchdog as installer
from scripts import mac_vm_watchdog as watchdog


class RuntimeWatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 15, 1, 15, tzinfo=timezone.utc)
        self.config = {"syncthing_url": "http://127.0.0.1:8384", "syncthing_api_key": "test-only",
                       "syncthing_folder": "a-system", "heartbeat_stale_sec": 900}

    def _response(self, modified: str, **flags):
        return io.BytesIO(json.dumps({"local": {"modified": modified, **flags}}).encode())

    def test_running_vm_with_stale_heartbeat_alerts_and_is_not_healthy(self):
        state = {"was_down": False}
        with patch.object(watchdog.urllib.request, "urlopen", return_value=self._response("2026-09-14T17:58:53Z")), \
                patch.object(watchdog, "_notify", return_value=True) as notify, \
                patch.object(watchdog, "_save_state"):
            result = watchdog._check_runtime(self.config, state, self.now, self.now.timestamp(), 3600)
        self.assertEqual(result, 1)
        self.assertEqual(state["runtime_status"], "STALE")
        self.assertIn("436分钟", notify.call_args.args[2])

    def test_syncthing_failure_is_unknown_and_alerted(self):
        state = {}
        with patch.object(watchdog.urllib.request, "urlopen", side_effect=TimeoutError), \
                patch.object(watchdog, "_notify", return_value=True) as notify, \
                patch.object(watchdog, "_save_state"):
            self.assertEqual(watchdog._check_runtime(self.config, state, self.now, self.now.timestamp(), 3600), 1)
        self.assertEqual(state["runtime_status"], "UNKNOWN")
        notify.assert_called_once()

    def test_recovery_requires_fresh_local_heartbeat(self):
        state = {"runtime_status": "STALE", "last_runtime_alert": self.now.timestamp() - 300}
        with patch.object(watchdog.urllib.request, "urlopen", return_value=self._response("2026-09-15T09:14:50+08:00")), \
                patch.object(watchdog, "_notify", return_value=True) as notify, \
                patch.object(watchdog, "_save_state"):
            self.assertEqual(watchdog._check_runtime(self.config, state, self.now, self.now.timestamp(), 3600), 0)
        self.assertEqual(state["heartbeat"]["age_sec"], 10)
        self.assertNotIn("last_runtime_alert", state)
        self.assertIn("心跳已恢复", notify.call_args.args[1])

    def test_repeated_failure_respects_alert_interval(self):
        state = {"runtime_status": "STALE", "last_runtime_alert": self.now.timestamp() - 300}
        with patch.object(watchdog.urllib.request, "urlopen", return_value=self._response("2026-09-14T17:58:53Z")), \
                patch.object(watchdog, "_notify") as notify, patch.object(watchdog, "_save_state"):
            self.assertEqual(watchdog._check_runtime(self.config, state, self.now, self.now.timestamp(), 3600), 1)
        notify.assert_not_called()

    def test_deleted_or_future_heartbeat_never_passes(self):
        for modified, flags in [("2026-09-15T01:14:50Z", {"deleted": True}), ("2026-09-15T03:00:00Z", {})]:
            with self.subTest(modified=modified, flags=flags), \
                    patch.object(watchdog.urllib.request, "urlopen", return_value=self._response(modified, **flags)):
                with self.assertRaises(ValueError):
                    watchdog._heartbeat_status(self.config, self.now.timestamp())

    def test_remote_endpoint_rejected_before_sending_api_key(self):
        with patch.object(watchdog.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(ValueError):
                watchdog._heartbeat_status({**self.config, "syncthing_url": "http://example.com:8384"}, self.now.timestamp())
        urlopen.assert_not_called()

    def test_windows_subsecond_precision_is_supported_by_macos_python(self):
        with patch.object(watchdog.urllib.request, "urlopen", return_value=self._response("2026-09-15T09:14:50.3625165+08:00")):
            result = watchdog._heartbeat_status(self.config, self.now.timestamp())
        self.assertEqual(result["status"], "HEALTHY")

    def test_default_heartbeat_is_forwarder_log_with_30_minute_limit(self):
        # 2026-11-23：外部守护进程 10-22 起不再写 daemon_heartbeat.txt（换 QMT 原生版），改看通知转发程序的日志
        config = {k: v for k, v in self.config.items() if k != "heartbeat_stale_sec"}
        for minutes_ago, expected in ((20, "HEALTHY"), (29, "HEALTHY"), (31, "STALE")):
            modified = datetime.fromtimestamp(self.now.timestamp() - minutes_ago * 60, timezone.utc).isoformat()
            with self.subTest(minutes_ago=minutes_ago), \
                    patch.object(watchdog.urllib.request, "urlopen", return_value=self._response(modified)) as urlopen:
                result = watchdog._heartbeat_status(config, self.now.timestamp())
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["file"], "logs/qmt_notify_forwarder.log")
            query = watchdog.urllib.parse.urlparse(urlopen.call_args.args[0].full_url).query
            self.assertEqual(watchdog.urllib.parse.parse_qs(query)["file"], ["logs/qmt_notify_forwarder.log"])

    def test_old_daemon_heartbeat_is_not_watched_any_more(self):
        source = (Path(watchdog.__file__)).read_text(encoding="utf-8")
        self.assertNotIn('"logs/daemon_heartbeat.txt"', source.replace("'", '"'))

    def test_stale_alert_names_the_forwarder(self):
        state = {}
        with patch.object(watchdog.urllib.request, "urlopen", return_value=self._response("2026-09-14T17:58:53Z")), \
                patch.object(watchdog, "_notify", return_value=True) as notify, \
                patch.object(watchdog, "_save_state"):
            watchdog._check_runtime(self.config, state, self.now, self.now.timestamp(), 3600)
        self.assertIn("通知转发程序", notify.call_args.args[2])
        self.assertNotIn("daemon", notify.call_args.args[2])

    def test_bark_request_encodes_chinese_group(self):
        with patch.object(watchdog.urllib.request, "urlopen", return_value=io.BytesIO(b"{}")) as urlopen:
            self.assertTrue(watchdog._notify({"bark_url": "https://example.test/test"}, "告警", "心跳停止"))
        url = urlopen.call_args.args[0]
        self.assertTrue(url.isascii())
        self.assertEqual(watchdog.urllib.parse.parse_qs(watchdog.urllib.parse.urlparse(url).query)["group"], ["A股实盘"])


class InstallerFirstCheckTest(unittest.TestCase):
    """2026-11-23：装完等哨兵第一次检查并打印结果，旧状态文件不能冒充这次的检查。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "vm_watchdog_state.json"

    def test_state_path_is_the_one_the_watchdog_writes(self):
        self.assertEqual(installer.STATE_PATH, watchdog.STATE_PATH)

    def test_reports_the_check_written_after_install(self):
        self.state.write_text(json.dumps({"was_down": False, "runtime_status": "HEALTHY",
                                          "heartbeat": {"file": "logs/qmt_notify_forwarder.log", "age_sec": 42}}))
        result = installer.first_check(time.time() - 5, timeout=2, state_path=self.state)
        self.assertEqual(result, {"runtime_status": "HEALTHY", "heartbeat_file": "logs/qmt_notify_forwarder.log",
                                  "heartbeat_age_sec": 42})

    def test_vm_down_is_reported(self):
        self.state.write_text(json.dumps({"was_down": True}))
        self.assertEqual(installer.first_check(time.time() - 5, timeout=2, state_path=self.state)["runtime_status"],
                         "VM_NOT_RUNNING")

    def test_old_state_file_does_not_count(self):
        self.state.write_text(json.dumps({"was_down": False, "runtime_status": "HEALTHY"}))
        old = time.time() - 3600
        os.utime(self.state, (old, old))
        self.assertEqual(installer.first_check(time.time() - 5, timeout=1.5, state_path=self.state)["runtime_status"],
                         "NO_CHECK_YET")

    def test_installer_no_longer_writes_heartbeat_settings(self):
        source = Path(installer.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"heartbeat_stale_sec"', source)


if __name__ == "__main__":
    unittest.main()
