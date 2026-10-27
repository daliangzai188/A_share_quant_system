import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src import qmt_process_control
from src.qmt_native_mode import live_status, native_settings, native_start, native_stop, status_lines

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 27, 2, 33, 0)


def status_line(at, alive='True', last='2026-10-27 02:31:28 | ✅ [账户] 2026-10-27 02:31:28 | 账户***90 总资产6.89万 | 无持仓'):
    return ('%s,425 [INFO] [0x00001f48] reqid = 0C:\\QMT\\GJQMT\\python\\A_SYSTEM_QMT_INNER.py_00030017, '
            'output = [A_SYSTEM] %s status: daemon_alive=%s log_lines=414 saves=0/0 kv=0.0s last="%s" '
            'daemon_at=trading_daemon.py:10769 main\r\n' % (at.strftime('%Y-%m-%d %H:%M:%S'), at.strftime('%H:%M:%S'), alive, last))


class Boom:
    def __getattr__(self, name):
        raise AssertionError('keeper/scheduled starts must not touch QMT')


class FakeControl:
    def __init__(self, pids, logged_in, close_ok=True):
        self.running, self.logged_in, self.close_ok, self.calls = list(pids), logged_in, close_ok, []

    def pids(self):
        return list(self.running)

    def main_window(self):
        return bool(self.running) and self.logged_in

    def close(self):
        self.calls.append('close')
        if self.close_ok:
            self.running = []
        return self.close_ok, 'QMT 已正常关闭' if self.close_ok else '无法结束 QMT 进程'

    def launch(self):
        self.calls.append('launch')
        self.running, self.logged_in = [7], False
        return True, '已启动 QMT'


class QmtNativeModeTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.settings = {'enabled': True, 'qmt_log_dir': str(self.dir), 'strategy_file': 'A_SYSTEM_QMT_INNER',
                         'forwarder_task': 'A_System_QmtNotifyForwarder', 'stale_minutes': 10}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, day, text):
        path = self.dir / ('XtClient_FormulaOutput_%s.log' % day)
        with open(path, 'ab') as handle:
            handle.write(text if isinstance(text, bytes) else text.encode('utf-8'))

    def test_enabled_only_with_an_explicit_true(self):
        root = self.dir / 'repo'
        (root / 'config').mkdir(parents=True)
        self.assertIsNone(native_settings(root))
        (root / 'config' / 'qmt_native.json').write_text('{"enabled": false}', encoding='utf-8')
        self.assertIsNone(native_settings(root))
        (root / 'config' / 'qmt_native.json').write_text('{not json', encoding='utf-8')
        self.assertIsNone(native_settings(root))
        (root / 'config' / 'qmt_native.json').write_text('{"enabled": true, "since": "2026-10-22"}', encoding='utf-8')
        self.assertEqual(native_settings(root)['since'], '2026-10-22')
        self.assertTrue(native_settings(ROOT)['enabled'])  # the repository ships the native mode on

    def test_last_status_line_release_and_account(self):
        self.write('20261027', '[A_SYSTEM] 01:12:01 started: mode=live release=202610262343-15e8b81fd88e\r\n'
                   .encode('gbk') + b'\xc4\xe3\xba\xc3 gbk noise\r\n')
        self.write('20261027', status_line(NOW - dt.timedelta(seconds=76)))
        self.write('20261027', '2026-10-27 02:32:30,001 [INFO] [0x00001f48] reqid = 0C:\\QMT\\GJQMT\\python\\OTHER.py_1, '
                   'output = [A_SYSTEM] 02:32:30 status: daemon_alive=False\r\n')
        live = live_status(self.settings, NOW)
        self.assertEqual(live['last_status_at'], NOW - dt.timedelta(seconds=76))
        self.assertEqual(live['age_seconds'], 76)
        self.assertTrue(live['daemon_alive'])
        self.assertEqual(live['release'], '202610262343-15e8b81fd88e')
        self.assertEqual(live['account'], '✅ [账户] 2026-10-27 02:31:28 | 账户***90 总资产6.89万 | 无持仓')

    def test_after_midnight_yesterdays_log_counts(self):
        self.write('20261026', status_line(dt.datetime(2026, 10, 26, 23, 59, 50)))
        live = live_status(self.settings, dt.datetime(2026, 10, 27, 0, 0, 5))
        self.assertEqual(live['age_seconds'], 15)
        self.assertIsNone(live_status(self.settings, dt.datetime(2026, 10, 28, 9, 0))['last_status_at'])

    def test_status_lines_healthy_stale_and_forwarder_started(self):
        self.write('20261027', status_line(NOW - dt.timedelta(seconds=20)))
        ok, lines = status_lines(self.settings, now=NOW, qmt=lambda: True, task=lambda n: 'Running')
        self.assertTrue(ok, lines)
        self.assertTrue(any('在运行' in l and '20 秒前' in l for l in lines))
        states, started = ['Ready', 'Running'], []
        ok, lines = status_lines(self.settings, ensure_forwarder=True, now=NOW, qmt=lambda: True,
                                 task=lambda n: states.pop(0), start=lambda n: started.append(n) or True)
        self.assertTrue(ok, lines)
        self.assertEqual(started, ['A_System_QmtNotifyForwarder'])
        later = NOW + dt.timedelta(minutes=15)
        ok, lines = status_lines(self.settings, now=later, qmt=lambda: True, task=lambda n: 'Running')
        self.assertFalse(ok)
        self.assertTrue(any('退出 QMT 再登录' in l for l in lines))
        ok, lines = status_lines(self.settings, now=NOW, qmt=lambda: False, task=lambda n: None)
        self.assertFalse(ok)
        self.assertTrue(any('没有运行' in l for l in lines) and any('未找到' in l for l in lines))

    def test_automatic_recovery_never_touches_qmt(self):
        out = []
        code = native_start(self.settings, self.dir, automatic=True, out=out.append, control=Boom())
        self.assertEqual(code, 0)
        self.assertIn('不启动外部守护进程', out[0])

    def run_start(self, control, output_after_launch, now=NOW):
        clock = [now]
        written = []

        def sleep(seconds):
            clock[0] += dt.timedelta(seconds=seconds)
            if output_after_launch and 'launch' in control.calls and not written:
                self.write('20261027', status_line(clock[0]))
                written.append(1)
        (self.dir / '.manual_stop.json').write_text('{}', encoding='utf-8')
        out = []
        code = native_start(self.settings, self.dir, automatic=False, out=out.append, control=control,
                            clock=lambda: clock[0], sleep=sleep, qmt=lambda: True, task=lambda n: 'Running')
        return code, out

    def test_start_when_already_running_changes_nothing(self):
        self.write('20261027', status_line(NOW - dt.timedelta(seconds=15)))
        control = FakeControl(pids=[42], logged_in=True)
        code, out = self.run_start(control, False)
        self.assertEqual((code, control.calls), (0, []), out)
        self.assertFalse((self.dir / '.manual_stop.json').exists())
        self.assertIn('本来就在运行', out[-1])

    def test_start_restarts_a_logged_in_qmt_whose_strategy_is_silent(self):
        self.write('20261027', status_line(NOW - dt.timedelta(minutes=30)))
        control = FakeControl(pids=[42], logged_in=True)
        code, out = self.run_start(control, True)
        self.assertEqual((code, control.calls), (0, ['close', 'launch']), out)
        self.assertFalse((self.dir / '.manual_stop.json').exists())
        self.assertIn('已开始运行', out[-1])

    def test_start_launches_qmt_and_waits_for_login(self):
        control = FakeControl(pids=[], logged_in=False)
        code, out = self.run_start(control, False)
        self.assertEqual((code, control.calls), (1, ['launch']), out)
        self.assertTrue(any('请在 QMT 登录窗口登录' in line for line in out))
        self.assertIn('15 分钟内 A_SYSTEM 没有开始输出', out[-1])
        control = FakeControl(pids=[42], logged_in=False)       # QMT open at its login window: keep it
        code, out = self.run_start(control, False)
        self.assertEqual(control.calls, [])

    def test_stop_closes_qmt_and_pushes_once(self):
        self.write('20261027', status_line(NOW - dt.timedelta(seconds=15)))
        pushes, out = [], []
        control = FakeControl(pids=[42], logged_in=True)
        code = native_stop(self.settings, out=out.append, control=control, notify=lambda: pushes.append(1), clock=lambda: NOW)
        self.assertEqual((code, control.calls, len(pushes)), (0, ['close'], 1))
        self.assertTrue(any('账户***90 总资产6.89万 | 无持仓' in line for line in out))
        control = FakeControl(pids=[], logged_in=False)
        self.assertEqual(native_stop(self.settings, out=out.append, control=control, notify=lambda: pushes.append(1)), 0)
        control = FakeControl(pids=[42], logged_in=True, close_ok=False)
        self.assertEqual(native_stop(self.settings, out=out.append, control=control, notify=lambda: pushes.append(1)), 1)
        self.assertEqual(len(pushes), 1)

    def test_tasklist_rows_are_parsed(self):
        rows = '"XtItClient.exe","1016","Console","1","823,000 K"\n"XtItClient.exe","2044","Console","1","10 K"\n'
        done = type('Done', (), {'returncode': 0, 'stdout': rows})()
        original = qmt_process_control._run
        qmt_process_control._run = lambda command, timeout=60: done
        try:
            self.assertEqual(qmt_process_control.ProcessControl().pids(), [1016, 2044])
            done.stdout = 'INFO: No tasks are running which match the specified criteria.\n'
            self.assertEqual(qmt_process_control.ProcessControl().pids(), [])
        finally:
            qmt_process_control._run = original

    def test_start_windows_never_starts_the_old_daemon(self):
        repo = self.dir / 'repo'
        (repo / 'src').mkdir(parents=True)
        (repo / 'config').mkdir()
        shutil.copy(ROOT / 'start_windows.py', repo)
        for name in ('qmt_native_mode.py', 'qmt_process_control.py', 'runtime_stop_state.py'):
            shutil.copy(ROOT / 'src' / name, repo / 'src')
        (repo / 'src' / '__init__.py').write_text('', encoding='utf-8')
        (repo / 'config' / 'qmt_native.json').write_text(
            '{"enabled": true, "qmt_log_dir": "%s"}' % str(self.dir).replace('\\', '\\\\'), encoding='utf-8')
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        for args in (['--no-tail', '--automatic-recovery'], []):
            done = subprocess.run([sys.executable, str(repo / 'start_windows.py')] + args, cwd=str(repo), env=env,
                                  capture_output=True, text=True, encoding='utf-8', timeout=120)
            self.assertIn('运行在 QMT 内置 Python 中', done.stdout, done.stderr)
            self.assertEqual(done.returncode, 0 if args else 1, done.stdout)  # no tasklist here: nothing started
            self.assertFalse((repo / '.daemon_pid').exists())
            self.assertFalse((repo / 'logs').exists())


if __name__ == '__main__':
    unittest.main()
