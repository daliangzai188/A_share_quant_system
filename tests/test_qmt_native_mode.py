import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.qmt_native_mode import LogFollower, live_status, native_settings, native_start, native_stop, status_lines

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

    def test_status_line_merged_under_another_print_is_found(self):
        # QMT puts prints of the same moment under one header: the status line is then a continuation line
        self.write('20261028', ('2026-10-28 09:37:12,894 [INFO] [0x1] reqid = 0C:\\QMT\\GJQMT\\python\\A_SYSTEM_QMT_INNER.py_1, '
                                'output = [A_SYSTEM] 09:37:12 trading switch: pause (stop_windows.py)\r\n'
                                '[A_SYSTEM] 09:37:12 ⏸ 交易已暂停（交易开关 pause）\r\n'
                                '[A_SYSTEM] 09:37:12 status: trading=pause daemon_alive=True log_lines=38\r\n'
                                '2026-10-28 09:38:12,906 [INFO] [0x1] reqid = 0C:\\QMT\\GJQMT\\python\\A_SYSTEM_QMT_INNER.py_1, '
                                'output = [A_SYSTEM] 09:38:12 ⏸ 交易已暂停（交易开关 pause）\r\n'
                                '[A_SYSTEM] 09:38:12 status: trading=pause daemon_alive=False log_lines=38\r\n'
                                '2026-10-28 09:38:20,1 [INFO] [0x1] reqid = 0C:\\QMT\\GJQMT\\python\\OTHER.py_1, output = x\r\n'
                                '[A_SYSTEM] 09:38:20 status: trading=run daemon_alive=True\r\n').encode('utf-8'))
        live = live_status(self.settings, dt.datetime(2026, 10, 28, 9, 38, 30))
        self.assertEqual((live['last_status_at'], live['trading'], live['daemon_alive']),
                         (dt.datetime(2026, 10, 28, 9, 38, 12), 'pause', False))

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
        self.assertTrue(any('已暂停：交易执行程序已结束' in line for line in out))
        self.assertTrue(any('既不开仓也不平仓' in line for line in out))
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

    def test_log_follower_shows_daemon_lines_pushes_and_switch_changes(self):
        def qmt(at, payload, strategy='A_SYSTEM_QMT_INNER'):
            return ('2026-10-27 %s,100 [INFO] [0x00001f48] reqid = 0C:\\QMT\\GJQMT\\python\\%s.py_1, output = %s\r\n'
                    % (at, strategy, payload)).encode('utf-8')
        out = []
        follower = LogFollower(self.settings, out.append)
        self.write('20261027', qmt('09:00:02', '[A_SYSTEM] 09:00:02 status: trading=run daemon_alive=True last="旧行"'))
        follower.start_at_end(NOW.date(), history=5)
        self.assertEqual(out, ['▶ 交易中（交易开关 = run）', '旧行'])   # a build without echo: status shows the latest line
        out[:] = []
        self.write('20261027', qmt('09:00:05', '[A_SYSTEM_LOG] 2026-10-27 09:00:05 | ✅ [账户] 账户***90 总资产6.89万 | 无持仓')
                   + qmt('09:00:06', '[A_SYSTEM_LOG] 最终开仓计划') + '┃ ★ 明日A/C/E均无开仓计划\r\n'.encode('utf-8')
                   + qmt('09:00:07', '[A_SYSTEM_NOTIFY live] 1a-5 /%E2%9C%85%20A_System%E8%BF%90%E8%A1%8C%E6%AD%A3%E5%B8%B8/x?y=1')
                   + qmt('09:00:08', '[A_SYSTEM_EXPORT live] a b 1/1 x')
                   + qmt('09:00:09', '[A_SYSTEM_LOG] 其他策略的行', strategy='OTHER')
                   + qmt('09:00:41', '[A_SYSTEM] 09:00:41 trading switch: pause (stop_windows.py)')
                   + qmt('09:00:42', '[A_SYSTEM] 09:00:42 status: trading=pause daemon_alive=False last="x"')
                   + qmt('09:00:43', '[A_SYSTEM_LOG] half')
                   + b'\r\n'
                   + qmt('09:00:44', '[A_SYSTEM_NOTIFY live] 1a-6 /%E2%9C%85/x')
                   + b'%E4%B8%8E%E6%8C%81 wrapped rest of a long push\r\n'
                   + '[A_SYSTEM_LOG] 2026-10-27 09:00:45 | 同一次刷新里的下一行\r\n'.encode('utf-8')
                   + b'[A_SYSTEM] 09:00:46 status: trading=run daemon_alive=True last="y"\r\n')
        with open(self.dir / 'XtClient_FormulaOutput_20261027.log', 'ab') as handle:
            handle.write(b'2026-10-27 09:00:44,1 [INFO] partial line without end')
        follower.poll(NOW.date())
        self.assertEqual(out, ['2026-10-27 09:00:05 | ✅ [账户] 账户***90 总资产6.89万 | 无持仓', '最终开仓计划',
                               '┃ ★ 明日A/C/E均无开仓计划', '📣 推送：✅ A_System运行正常',
                               '[A_SYSTEM] 09:00:41 trading switch: pause (stop_windows.py)', '⏸ 交易已暂停（交易开关 = pause）', 'half',
                               '📣 推送：✅', '2026-10-27 09:00:45 | 同一次刷新里的下一行', '▶ 交易中（交易开关 = run）'])

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
