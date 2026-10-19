"""客户端守护中的本机同步恢复；全部使用临时身份与进程/HTTP替身。"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import qmt_client_guard as guard


class SyncthingRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "A_System" / "qmt_client_recovery"
        self.home.mkdir(parents=True)
        self.identity = self.root / "Syncthing"
        self.identity.mkdir()
        (self.identity / "config.xml").write_text(
            '<configuration><gui enabled="true" tls="false">'
            '<address>127.0.0.1:8384</address></gui>'
            '<device id="TEST_EXISTING_DEVICE"/><folder id="UNCHANGED"/></configuration>')
        (self.identity / "cert.pem").write_bytes(b"TEST EXISTING CERTIFICATE")
        (self.identity / "key.pem").write_bytes(b"TEST EXISTING PRIVATE IDENTITY")
        executable = self.root / "Programs" / "Syncthing" / "syncthing.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        self.helper = self.home.parent / "sync_tools" / "open_sync_status.py"
        self.helper.parent.mkdir()
        self.helper.write_bytes((SCRIPTS / "open_sync_status.py").read_bytes())
        self.settings = dict(enabled=True, qmt_exe="XtItClient.exe", project_root=str(self.root),
                             check_interval_seconds=30, retry_delays_seconds=[60, 180],
                             syncthing_recovery=dict(guard.SYNC_DEFAULTS, enabled=True,
                             helper_sha256=hashlib.sha256(self.helper.read_bytes()).hexdigest()))
        self.env = patch.dict(guard.os.environ, {"LOCALAPPDATA": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.identity_before = {p.name: p.read_bytes() for p in self.identity.iterdir()}

    def tick(self, state=None, now=1000, health=("stopped", "healthy"), process=False, result=None):
        state = state if state is not None else guard.RecoveryState()
        run_result = subprocess.CompletedProcess([], 0) if result is None else result
        with patch.object(guard, "syncthing_health", side_effect=health) as probe, \
             patch.object(guard, "syncthing_process_running", return_value=process), \
             patch.object(guard.subprocess, "run", return_value=run_result) as run:
            status = guard.recover_syncthing(self.home, self.settings, state, now)
        return status, state, run, probe

    def test_stopped_sync_recovers_via_private_helper_and_does_not_change_identity(self):
        status, state, run, _ = self.tick()
        self.assertEqual(status["state"], "healthy_after_recovery")
        self.assertTrue(status["healthy"])
        self.assertFalse(status["folder_sync_verified"])
        self.assertEqual(run.call_args.args[0], [sys.executable, str(self.helper), "--start-only"])
        self.assertEqual(run.call_args.kwargs["timeout"], 70)
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual({p.name: p.read_bytes() for p in self.identity.iterdir()}, self.identity_before)
        # 启动前状态已持久化，即使helper之后/进程中途退出也不会立即重复拉起。
        saved = json.loads((self.home / "state.json").read_text())["syncthing"]
        self.assertEqual(saved["status"]["state"], "starting")
        self.assertEqual(saved["next_attempt_at"], 1130)

    def test_healthy_and_not_due_do_not_start_or_requery(self):
        status, state, run, probe = self.tick(health=("healthy",))
        self.assertEqual(status["state"], "healthy")
        run.assert_not_called()
        self.assertEqual(probe.call_count, 1)
        _, _, run, probe = self.tick(state, now=1030, health=())
        run.assert_not_called()
        probe.assert_not_called()

    def test_unknown_health_or_existing_process_never_start_duplicate(self):
        for health, process, expected in [("unverified", False, "health_unverified"),
                                         ("stopped", True, "existing_process_unhealthy"),
                                         ("stopped", None, "process_unverified")]:
            with self.subTest(expected=expected):
                status, _, run, _ = self.tick(health=(health,), process=process)
                self.assertEqual(status["state"], expected)
                self.assertFalse(status["healthy"])
                run.assert_not_called()

    def test_missing_or_changed_helper_never_installs_itself(self):
        self.helper.unlink()
        status, _, run, _ = self.tick(health=("stopped",))
        self.assertEqual(status["state"], "helper_missing")
        self.assertFalse(self.helper.exists())
        run.assert_not_called()
        self.helper.write_text("raise AssertionError('unreviewed helper')")
        status, _, run, _ = self.tick(health=("stopped",))
        self.assertEqual(status["state"], "helper_unverified")
        run.assert_not_called()

    def test_missing_identity_or_executable_is_not_implicitly_created(self):
        (self.identity / "key.pem").unlink()
        status, _, run, _ = self.tick(health=("stopped",))
        self.assertEqual(status["state"], "existing_identity_missing")
        run.assert_not_called()
        self.assertFalse((self.identity / "key.pem").exists())
        (self.identity / "key.pem").write_bytes(self.identity_before["key.pem"])
        (self.root / "Programs" / "Syncthing" / "syncthing.exe").unlink()
        with patch.object(guard.shutil, "which", return_value=None):
            status, _, run, _ = self.tick(health=("stopped",))
        self.assertEqual(status["state"], "executable_missing")
        run.assert_not_called()

    def test_unsupported_existing_gui_is_not_rewritten(self):
        original = (self.identity / "config.xml").read_text().replace("127.0.0.1:8384", "127.0.0.1:9090")
        (self.identity / "config.xml").write_text(original)
        status, _, run, _ = self.tick(health=("stopped",))
        self.assertEqual(status["state"], "gui_configuration_unsupported")
        run.assert_not_called()
        self.assertEqual((self.identity / "config.xml").read_text(), original)

    def test_inherited_environment_cannot_switch_identity_or_unpause_folders(self):
        for name in ("STHOMEDIR", "STCONFDIR", "STDATADIR", "STGUIADDRESS", "STGUIAPIKEY",
                     "STPAUSED", "STUNPAUSED"):
            with self.subTest(name=name), patch.dict(guard.os.environ, {name: "TEST_OVERRIDE"}):
                status, _, run, _ = self.tick(health=("stopped",))
                self.assertEqual(status["state"], "environment_override_unverified")
                run.assert_not_called()

    def test_helper_success_without_health_is_not_recovery(self):
        status, state, run, _ = self.tick(health=("stopped", "unverified"))
        self.assertEqual(status["state"], "start_unverified")
        self.assertFalse(status["healthy"])
        self.assertEqual(state.syncthing["failures"], 1)
        self.assertEqual(run.call_count, 1)

    def test_timeout_persists_backoff_across_guard_restart_and_retries_later(self):
        state = guard.RecoveryState()
        def timeout(*args, **kwargs):
            saved = json.loads((self.home / "state.json").read_text())
            self.assertEqual(saved["syncthing"]["next_attempt_at"], 1130)
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        with patch.object(guard, "syncthing_health", return_value="stopped"), \
             patch.object(guard, "syncthing_process_running", return_value=False), \
             patch.object(guard.subprocess, "run", side_effect=timeout):
            status = guard.recover_syncthing(self.home, self.settings, state, 1000)
        self.assertEqual(status["state"], "start_timeout")
        recreated = guard.RecoveryState(**json.loads((self.home / "state.json").read_text()))
        status, _, run, _ = self.tick(recreated, now=1080, health=("stopped",))
        self.assertEqual(status["state"], "backoff")
        run.assert_not_called()
        status, _, run, _ = self.tick(recreated, now=1140)
        self.assertEqual(status["state"], "healthy_after_recovery")
        self.assertEqual(run.call_count, 1)

    def test_sync_in_original_loop_runs_even_when_trading_is_manually_stopped(self):
        (self.home / "config.json").write_text(json.dumps(self.settings))
        observed = dict(kind="login", manual_stop=True)
        result = subprocess.CompletedProcess([], 0)
        with patch.object(guard, "acquire_mutex", return_value=object()), \
             patch.object(guard, "observe", return_value=observed), \
             patch.object(guard, "syncthing_health", side_effect=["stopped", "healthy"]), \
             patch.object(guard, "syncthing_process_running", return_value=False), \
             patch.object(guard.subprocess, "run", return_value=result) as run, \
             patch.object(guard.subprocess, "Popen") as popen, \
             patch.object(guard.time, "sleep", side_effect=InterruptedError("end test iteration")):
            with self.assertRaises(InterruptedError):
                guard.run_guard(self.home)
        status = json.loads((self.home / "status.json").read_text())
        self.assertEqual(status["state"], "manual_stop")
        self.assertTrue(status["syncthing"]["healthy"])
        self.assertFalse(status["starts_trading_daemon"])
        self.assertFalse(status["sends_orders"])
        self.assertEqual(run.call_count, 1)
        popen.assert_not_called()
        for handler in list(guard.logging.getLogger("qmt_client_guard").handlers):
            handler.close()
            guard.logging.getLogger("qmt_client_guard").removeHandler(handler)

    def test_config_can_disable_recovery_and_bounds_resource_use(self):
        self.settings["syncthing_recovery"]["enabled"] = False
        status, _, run, probe = self.tick(health=())
        self.assertEqual(status["state"], "disabled")
        probe.assert_not_called()
        run.assert_not_called()
        for change in ({"check_interval_seconds": 0}, {"start_timeout_seconds": 0},
                       {"retry_delays_seconds": [float("nan")]}, {"url": "http://remote/"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                guard.sync_recovery_settings({"syncthing_recovery": change})

    def test_install_stages_exact_private_dependency_and_preserves_user_policy(self):
        python = self.root / "python.exe"
        python.touch()
        python.with_name("pythonw.exe").touch()
        exe = self.root / "XtItClient.exe"
        exe.touch()
        existing = dict(self.settings)
        existing["syncthing_recovery"] = dict(existing["syncthing_recovery"], enabled=False,
                                             check_interval_seconds=120)
        (self.home / "config.json").write_text(json.dumps(existing))
        self.helper.write_text("old helper")
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(guard.sys, "executable", str(python)), \
             patch.object(guard, "powershell", return_value=result) as powershell, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.install(self.home, self.root, exe), 0)
        installed = json.loads((self.home / "config.json").read_text())
        self.assertFalse(installed["syncthing_recovery"]["enabled"])
        self.assertEqual(installed["syncthing_recovery"]["check_interval_seconds"], 120)
        self.assertEqual(self.helper.read_bytes(), (SCRIPTS / "open_sync_status.py").read_bytes())
        self.assertEqual(installed["syncthing_recovery"]["helper_sha256"],
                         hashlib.sha256(self.helper.read_bytes()).hexdigest())
        self.assertIn("-MultipleInstances IgnoreNew", powershell.call_args.args[0][-1])
        self.assertEqual({p.name: p.read_bytes() for p in self.identity.iterdir()}, self.identity_before)
        fresh = guard.build_install_settings(self.root, exe)
        self.assertTrue(fresh["syncthing_recovery"]["enabled"])


class SyncthingObservationTests(unittest.TestCase):
    def test_health_requires_official_json_and_does_not_follow_redirects(self):
        for code, body, expected in [(200, b'{"status":"OK"}', "healthy"),
                                     (200, b"some other service", "unverified"),
                                     (302, b'{"status":"OK"}', "unverified"),
                                     (200, b"x" * 4097, "unverified")]:
            with self.subTest(expected=expected, code=code):
                response = Mock(status=code)
                response.read.return_value = body
                connection = Mock()
                connection.getresponse.return_value = response
                with patch.object(guard.http.client, "HTTPConnection", return_value=connection) as factory:
                    self.assertEqual(guard.syncthing_health(), expected)
                factory.assert_called_once_with("127.0.0.1", 8384, timeout=2)
                connection.request.assert_called_once_with("GET", "/rest/noauth/health")
                response.read.assert_called_once_with(4097)
                connection.close.assert_called_once()

    def test_only_connection_refused_means_service_stopped(self):
        for error, expected in [(ConnectionRefusedError(guard.errno.ECONNREFUSED, "refused"), "stopped"),
                                (TimeoutError("timeout"), "unverified")]:
            connection = Mock()
            connection.request.side_effect = error
            with patch.object(guard.http.client, "HTTPConnection", return_value=connection):
                self.assertEqual(guard.syncthing_health(), expected)

    def test_process_query_is_three_state_and_never_kills_processes(self):
        for result, expected in [(subprocess.CompletedProcess([], 0, '"syncthing.exe","123","Console","1","1 K"\n'), True),
                                 (subprocess.CompletedProcess([], 0, "INFO: No tasks are running which match the specified criteria.\n"), False),
                                 (subprocess.CompletedProcess([], 1, ""), None),
                                 (subprocess.CompletedProcess([], 0, ""), None)]:
            with patch.object(guard.subprocess, "run", return_value=result) as run:
                self.assertIs(guard.syncthing_process_running(), expected)
                self.assertEqual(run.call_args.args[0][1:],
                                 ["/FI", "IMAGENAME eq syncthing.exe", "/FO", "CSV", "/NH"])
                self.assertEqual(run.call_args.kwargs["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
