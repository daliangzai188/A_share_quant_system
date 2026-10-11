"""登录通信现场回归；临时日志、临时配置，禁止调用券商或控制真实窗口。"""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from qmt_client_guard import (BEIJING, RecoveryState, choose_action, connection_from_log,
                              parse_connection_event, build_install_settings,
                              public_connection_profile, status_payload, inner_bridge_health,
                              decode_log, observe, validate_settings)


class ConnectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = dt.datetime.now(BEIJING).replace(microsecond=0).timestamp()
        self.start = self.now - 300
        base = self.root / 'userdata' / 'log'
        base.mkdir(parents=True)
        self.path = base / ('XtClient_' + dt.datetime.fromtimestamp(self.now, BEIJING).strftime('%Y%m%d') + '.log')
        self.cache = {}

    def line(self, text, seconds=-10):
        stamp = dt.datetime.fromtimestamp(self.now + seconds, BEIJING).strftime('%Y-%m-%d %H:%M:%S')
        # 第一、第二十六进制字段是线程信息，不能据此排除真实登录回报。
        return stamp + ',348 [INFO] [0x00002908] [0x000011e4] ' + text + '\n'

    def scan(self, **kwargs):
        return connection_from_log(self.root, self.start, self.cache, now=self.now,
                                   session_id=kwargs.pop('session_id', '9296'), **kwargs)

    def test_real_qt_status_format_is_online(self):
        self.path.write_text(self.line('[TC::CProxyClient::slot_loginStatus] statusOri = 9, statusNow = 12'))
        self.assertTrue(self.scan()['online'])

    def test_status_ori_12_is_not_current_online(self):
        self.path.write_text(self.line('[TC::CProxyClient::slot_loginStatus] statusOri = 12, statusNow = 22'))
        self.assertFalse(self.scan()['online'])
        self.assertEqual(self.scan()['last_status'], 22)

    def test_later_disconnect_overrides_cached_success(self):
        self.path.write_text(self.line('[TC::CProxyClient::slot_loginStatus] statusNow = 12', -20))
        self.assertTrue(self.scan()['online'])
        with self.path.open('a') as stream:
            stream.write(self.line('[TC::CProxyClient::slot_loginStatus] statusNow = 22', -5))
        self.assertFalse(self.scan()['online'])

    def test_same_second_events_use_file_order(self):
        self.path.write_text(self.line('onRelogin status:12') + self.line('onUserStatus status:22'))
        self.assertFalse(self.scan()['online'])

    def test_login_connection_lost_differs_from_quote_disconnect(self):
        self.path.write_text(self.line('onRelogin status:12', -20)
                             + self.line('[net][handleRecvData] error, remote addr: 114.28.170.219:55300', -5))
        self.assertTrue(self.scan()['online'])
        with self.path.open('a') as stream:
            stream.write(self.line('[net][handleRecvData] error, remote addr: 114.28.170.219:56001', -2))
        self.assertFalse(self.scan()['online'])

    def test_tcp_connect_does_not_keep_old_login_success(self):
        self.path.write_text(self.line('onRelogin status:12', -20)
                             + self.line('[net][onConnected] remote addr: 114.28.170.219:56001', -5))
        result = self.scan()
        self.assertFalse(result['online'])
        self.assertEqual(result['state'], 'unverified')
        self.assertEqual(result['endpoint']['host'], '114.28.170.219')

    def test_real_qt_connection_format_records_selected_line(self):
        self.path.write_text(self.line('[TC::CProxyClient::onConnected] local_addr = 127.0.0.1:58511, '
                                      'remote_addr = 114.28.170.219:56001', -20)
                             + self.line('[TC::CProxyClient::slot_loginStatus] statusNow = 12', -10))
        result = self.scan()
        self.assertTrue(result['online'])
        self.assertEqual(result['endpoint'], {'host':'114.28.170.219','port':56001})

    def test_tcp_or_offline_success_is_never_positive_login_evidence(self):
        self.path.write_text(self.line('[net][onConnected] remote addr: 114.28.170.219:56001')
                             + self.line('onTradeServerLoginSuccess offline'))
        self.assertFalse(self.scan()['online'])

    def test_noise_does_not_push_online_event_out_of_fixed_tail(self):
        self.path.write_text(self.line('onRelogin status:12') + ('quote noise\n' * 40000))
        self.assertGreater(self.path.stat().st_size, 262144)
        self.assertTrue(self.scan()['online'])
        # 游标和证据随 RecoveryState 保存，守护重启无需猜测旧日志尾部。
        state = RecoveryState(connection=self.cache)
        restarted = RecoveryState(**json.loads(json.dumps(vars(state))))
        self.cache = restarted.connection
        self.assertTrue(self.scan()['online'])

    def test_budgeted_scan_cannot_report_online_before_reaching_later_disconnect(self):
        self.path.write_text(self.line('onRelogin status:12', -20) + ('noise\n' * 100)
                             + self.line('onUserStatus status:22', -5))
        result = self.scan(scan_budget=180)
        self.assertFalse(result['online'])
        self.assertFalse(result['caught_up'])
        for _ in range(12):
            result = self.scan(scan_budget=180)
            if result['caught_up']:
                break
        self.assertTrue(result['caught_up'])
        self.assertEqual(result['last_status'], 22)

    def test_client_restart_rejects_old_success(self):
        self.path.write_text(self.line('onRelogin status:12'))
        self.assertTrue(self.scan()['online'])
        self.start = self.now - 2
        self.assertFalse(self.scan(session_id='new-client')['online'])

    def test_truncated_and_rewritten_log_invalidates_cached_success(self):
        self.path.write_text(self.line('onRelogin status:12'))
        self.assertTrue(self.scan()['online'])
        self.path.write_text('noise\n' * 100)
        self.assertFalse(self.scan()['online'])

    def test_partial_line_waits_until_completed(self):
        text = self.line('onRelogin status:12')
        self.path.write_text(text[:-1])
        self.assertFalse(self.scan()['online'])
        with self.path.open('a') as stream:
            stream.write('\n')
        self.assertTrue(self.scan()['online'])

    def test_other_account_status_panel_is_not_client_login_evidence(self):
        self.path.write_text(self.line('[TC::CAccountsPanel::slotLoginStatus] status = 12'))
        self.assertFalse(self.scan()['online'])

    def test_future_and_prelaunch_events_are_ignored(self):
        self.assertIsNone(parse_connection_event(self.line('onRelogin status:12', -400), self.start, self.now))
        self.assertIsNone(parse_connection_event(self.line('onRelogin status:12', 60), self.start, self.now))

    def test_no_valid_start_time_is_unverified(self):
        self.path.write_text(self.line('onRelogin status:12'))
        self.assertFalse(connection_from_log(self.root, 0, {}, now=self.now)['online'])

    def test_midnight_can_read_previous_day_login(self):
        self.now = dt.datetime.now(BEIJING).replace(hour=0, minute=1, second=0, microsecond=0).timestamp()
        self.start = self.now - 300
        yesterday = self.path.parent / ('XtClient_' + dt.datetime.fromtimestamp(self.now - 120, BEIJING).strftime('%Y%m%d') + '.log')
        yesterday.write_text(self.line('onRelogin status:12', -120))
        self.assertTrue(self.scan()['online'])

    def test_default_native_login_does_not_use_extra_credentials(self):
        settings = dict(enabled=True, retry_delays_seconds=[60])
        observed = dict(kind='login', credentials_ready=True, controls_supported=True)
        self.assertEqual(choose_action(observed, RecoveryState(), self.now, settings), 'waiting_native_login')
        observed['captcha'] = True
        self.assertEqual(choose_action(observed, RecoveryState(), self.now, settings), 'waiting_captcha')

    def test_manual_stop_blocks_recovery_without_clearing_marker(self):
        for kind in ('missing', 'login', 'main'):
            observed = dict(kind=kind, manual_stop=True, broker_online=True)
            self.assertEqual(choose_action(observed, RecoveryState(), self.now, {'enabled':True}), 'manual_stop')

    def test_install_preserves_user_disabled_state_intervals_and_policy(self):
        (self.root / 'config').mkdir()
        profile = dict(preferred_login_server={'host':'114.28.170.219','port':56001}, native_server_failover=True)
        (self.root / 'config' / 'qmt_client_recovery.json').write_text(json.dumps(profile))
        cfg = build_install_settings(self.root, 'XtItClient.exe', dict(enabled=False,
                                    retry_delays_seconds=[120, 600], check_interval_seconds=45))
        self.assertFalse(cfg['enabled'])
        self.assertEqual(cfg['retry_delays_seconds'], [120, 600])
        self.assertEqual(cfg['check_interval_seconds'], 45)
        self.assertEqual(cfg['preferred_login_server'], profile['preferred_login_server'])
        self.assertEqual(cfg['login_mode'], 'native_only')

    def test_public_profile_excludes_credentials_paths_and_secrets(self):
        profile = public_connection_profile(dict(login_mode='native_only', account='private',
                 password='secret', token='secret', qmt_exe='private', project_root='private'))
        self.assertEqual(profile, {'login_mode':'native_only'})

    def test_client_online_is_separate_from_inner_model_and_trading_readiness(self):
        observed = dict(kind='main', broker_online=True,
                        inner_bridge={'state':'heartbeat_unavailable','healthy':False}, manual_stop=True,
                        connection={'endpoint':{'host':'114.28.170.219','port':56001}})
        settings = dict(enabled=True,preferred_login_server={'host':'114.28.170.219','port':56001})
        payload = status_payload(observed, RecoveryState(), settings, 'manual_stop', self.now)
        self.assertTrue(payload['broker_online'])
        self.assertFalse(payload['inner_bridge']['healthy'])
        self.assertTrue(payload['manual_stop'])
        self.assertFalse(payload['sends_orders'])
        self.assertFalse(payload['starts_trading_daemon'])
        self.assertTrue(payload['requires_business_verification'])

    def test_gbk_password_error_is_not_lost_as_mojibake(self):
        self.assertIn('密码错误', decode_log('QMT 密码错误'.encode('gb18030')))

    def test_observation_does_not_read_extra_credentials_in_native_mode(self):
        home = self.root / 'private'
        home.mkdir()
        (home / 'credentials.json').write_text('not valid JSON; must never be read')
        (self.root / '.manual_stop.json').write_text('{}')
        self.path.write_text(self.line('[TC::CProxyClient::slot_loginStatus] statusNow = 12'))
        settings = dict(project_root=str(self.root), qmt_exe=str(self.root / 'bin.x64' / 'XtItClient.exe'),
                        monitor_inner_bridge=False)
        with patch('qmt_client_guard.helper', return_value=dict(kind='main', pid=9296, started_at=self.start)):
            result = observe(home, settings, RecoveryState())
        self.assertFalse(result['credentials_ready'])
        self.assertEqual(result['credential_version'], 0)
        self.assertTrue(result['manual_stop'])
        self.assertTrue(result['broker_online'])

    def test_malformed_retry_settings_cannot_create_retry_storm(self):
        base = dict(enabled=True, project_root=str(self.root), qmt_exe='XtItClient.exe',
                    check_interval_seconds=30, retry_delays_seconds=[60])
        for change in ({'retry_delays_seconds':[float('nan')]},
                       {'retry_delays_seconds':[0]}, {'retry_delays_seconds':'60'}, {'enabled':'false'}):
            with self.assertRaises(ValueError):
                validate_settings({**base, **change})

    def test_inner_health_uses_only_authenticated_heartbeat(self):
        settings = dict(project_root=str(Path(__file__).resolve().parents[1]))
        with patch('qmt_inner.protocol.load_settings', return_value={}), \
                patch('qmt_inner.protocol.FileClient') as client:
            client.return_value.heartbeat.side_effect = RuntimeError('QMT inner bridge heartbeat is stale')
            health = inner_bridge_health(settings)
            self.assertFalse(health['healthy'])
            client.return_value.connect.assert_not_called()
            client.return_value.call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
