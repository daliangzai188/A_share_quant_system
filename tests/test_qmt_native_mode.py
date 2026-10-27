import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.qmt_native_mode import live_status, native_settings, start_report, status_lines

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 27, 2, 33, 0)


def status_line(at, alive='True', last='2026-10-27 02:31:28 | ✅ [账户] 2026-10-27 02:31:28 | 账户***90 总资产6.89万 | 无持仓'):
    return ('%s,425 [INFO] [0x00001f48] reqid = 0C:\\QMT\\GJQMT\\python\\A_SYSTEM_QMT_INNER.py_00030017, '
            'output = [A_SYSTEM] %s status: daemon_alive=%s log_lines=414 saves=0/0 kv=0.0s last="%s" '
            'daemon_at=trading_daemon.py:10769 main\r\n' % (at.strftime('%Y-%m-%d %H:%M:%S'), at.strftime('%H:%M:%S'), alive, last))


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

    def test_automatic_recovery_never_reports_or_starts_anything(self):
        def boom(*args):
            raise AssertionError('no status calls for keeper/scheduled starts')
        out = []
        code = start_report(self.settings, automatic=True, out=out.append, qmt=boom, task=boom, start=boom)
        self.assertEqual(code, 0)
        self.assertIn('不再启动外部守护进程', out[0])

    def test_start_windows_never_starts_the_old_daemon(self):
        repo = self.dir / 'repo'
        (repo / 'src').mkdir(parents=True)
        (repo / 'config').mkdir()
        shutil.copy(ROOT / 'start_windows.py', repo)
        shutil.copy(ROOT / 'src' / 'qmt_native_mode.py', repo / 'src')
        (repo / 'src' / '__init__.py').write_text('', encoding='utf-8')
        (repo / 'config' / 'qmt_native.json').write_text(
            '{"enabled": true, "qmt_log_dir": "%s"}' % str(self.dir).replace('\\', '\\\\'), encoding='utf-8')
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        for args in (['--no-tail', '--automatic-recovery'], []):
            done = subprocess.run([sys.executable, str(repo / 'start_windows.py')] + args, cwd=str(repo), env=env,
                                  capture_output=True, text=True, encoding='utf-8', timeout=120)
            self.assertIn('QMT 内置 Python 中运行', done.stdout, done.stderr)
            self.assertEqual(done.returncode, 0 if args else 1, done.stdout)  # no QMT here: reported, not started
            self.assertFalse((repo / '.daemon_pid').exists())
            self.assertFalse((repo / 'logs').exists())


if __name__ == '__main__':
    unittest.main()
