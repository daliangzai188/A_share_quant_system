import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.qmt_native_mode import live_status, native_settings, native_start, native_stop, status_lines

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 27, 2, 33, 0)


def status_line(at, alive='True', last='2026-10-27 02:31:28 | ✅ [账户] 2026-10-27 02:31:28 | 账户***90 总资产6.89万 | 无持仓',
                trading='run'):
    switch = 'trading=%s ' % trading if trading else ''   # builds before the trading switch print none
    return ('%s,425 [INFO] [0x00001f48] reqid = 0C:\\QMT\\GJQMT\\python\\A_SYSTEM_QMT_INNER.py_00030017, '
            'output = [A_SYSTEM] %s status: %sdaemon_alive=%s log_lines=414 saves=0/0 kv=0.0s last="%s" '
            'daemon_at=trading_daemon.py:10769 main\r\n' % (at.strftime('%Y-%m-%d %H:%M:%S'), at.strftime('%H:%M:%S'),
                                                               switch, alive, last))


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
        self.assertTrue(any('在交易' in l and '20 秒前' in l for l in lines))
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

    def test_automatic_recovery_never_touches_the_switch(self):
        def boom(*args, **kwargs):
            raise AssertionError('keeper/scheduled starts must not change the trading switch')
        out = []
        code = native_start(self.settings, self.dir, automatic=True, out=out.append, switch=boom, logged_in=boom)
        self.assertEqual(code, 0)
        self.assertIn('不改变交易开关', out[0])

    def flow(self, run, after, logged_in=True, **kwargs):
        """run start/stop with a fake clock; `after` = status line kwargs written once the switch changed."""
        clock = [NOW]
        switched, written = [], []

        def sleep(seconds):
            clock[0] += dt.timedelta(seconds=seconds)
            if switched and after is not None:   # QMT prints a status line every 20 s
                if not written or (clock[0] - written[-1]).total_seconds() >= 20:
                    self.write('20261027', status_line(clock[0], **after))
                    written.append(clock[0])
        out = []
        code = run(self.settings, out=out.append, switch=lambda value, by: switched.append(value),
                   logged_in=lambda: logged_in, clock=lambda: clock[0], sleep=sleep, **kwargs)
        return code, switched, out

    def start(self, after, logged_in=True):
        (self.dir / '.manual_stop.json').write_text('{}', encoding='utf-8')
        return self.flow(lambda settings, **kw: native_start(settings, self.dir, False, qmt=lambda: True,
                                                             task=lambda n: 'Running', **kw), after, logged_in)

    def test_start_sets_run_and_waits_for_the_trading_thread(self):
        code, switched, out = self.start(dict(trading='run', alive='True'))
        self.assertEqual((code, switched), (0, ['run']), out)
        self.assertFalse((self.dir / '.manual_stop.json').exists())
        self.assertIn('已开始交易', out[-1])

    def test_start_before_login_sets_run_and_asks_to_log_in(self):
        code, switched, out = self.start(None, logged_in=False)
        self.assertEqual((code, switched), (1, ['run']))
        self.assertIn('请先打开并登录国金 QMT', out[-1])

    def test_start_reports_an_old_build_without_the_switch(self):
        code, _, out = self.start(dict(trading=None, alive='True'))
        self.assertEqual(code, 1)
        self.assertIn('旧版本', out[-1])

    def test_stop_sets_pause_confirms_and_pushes_once(self):
        pushes = []
        code, switched, out = self.flow(native_stop, dict(trading='pause', alive='False'), notify=lambda: pushes.append(1))
        self.assertEqual((code, switched, len(pushes)), (0, ['pause'], 1), out)
        self.assertTrue(any('已暂停：交易线程已结束' in line for line in out))
        self.assertTrue(any('账户***90 总资产6.89万' in line for line in out))
        code, _, out = self.flow(native_stop, None, logged_in=False, notify=lambda: pushes.append(1))
        self.assertEqual((code, len(pushes)), (0, 2))
        self.write('20261027', status_line(NOW + dt.timedelta(seconds=1), trading=None))
        code, _, out = self.flow(native_stop, dict(trading=None, alive='True'), notify=lambda: pushes.append(1))
        self.assertEqual((code, len(pushes)), (1, 2))   # an old build ignores the switch: tell how to stop by hand
        self.assertIn('点 A_SYSTEM 的 ■', out[-1])

    def test_paused_status_is_healthy_and_shows_the_switch(self):
        self.write('20261027', status_line(NOW - dt.timedelta(seconds=10), trading='pause', alive='False'))
        ok, lines = status_lines(self.settings, now=NOW, qmt=lambda: True, task=lambda n: 'Running',
                                 switch=lambda: {'Trading': 'pause', 'ChangedAt': '2026-10-27 08:30:00', 'ChangedBy': 'stop_windows.py'})
        self.assertTrue(ok, lines)
        self.assertTrue(lines[0].startswith('交易开关：pause'))
        self.assertTrue(any(line.startswith('⏸') for line in lines))

    def test_start_windows_never_starts_the_old_daemon(self):
        repo = self.dir / 'repo'
        (repo / 'src').mkdir(parents=True)
        (repo / 'config').mkdir()
        shutil.copy(ROOT / 'start_windows.py', repo)
        for name in ('qmt_native_mode.py', 'runtime_stop_state.py'):
            shutil.copy(ROOT / 'src' / name, repo / 'src')
        (repo / 'src' / '__init__.py').write_text('', encoding='utf-8')
        (repo / 'config' / 'qmt_native.json').write_text(
            '{"enabled": true, "qmt_log_dir": "%s"}' % str(self.dir).replace('\\', '\\\\'), encoding='utf-8')
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        for args in (['--no-tail', '--automatic-recovery'], []):
            done = subprocess.run([sys.executable, str(repo / 'start_windows.py')] + args, cwd=str(repo), env=env,
                                  capture_output=True, text=True, encoding='utf-8', timeout=120)
            self.assertIn('不改变交易开关' if args else '无法写入交易开关', done.stdout, done.stderr)
            self.assertEqual(done.returncode, 0 if args else 1, done.stdout)  # no registry here: nothing started
            self.assertFalse((repo / '.daemon_pid').exists())
            self.assertFalse((repo / 'logs').exists())


if __name__ == '__main__':
    unittest.main()
