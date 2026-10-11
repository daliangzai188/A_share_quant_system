import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from qmt_client_guard import RecoveryState, choose_action, record_attempt, broker_online_from_log
from qmt_win32_login import password_is_masked, same_field

SETTINGS = dict(enabled=True, retry_delays_seconds=[60, 180, 600, 1800], login_mode='local_credentials')


class RecoveryTests(unittest.TestCase):
    def action(self, **changes):
        observed = dict(kind='login', credentials_ready=True, controls_supported=True)
        observed.update(changes)
        return choose_action(observed, RecoveryState(), 1000, SETTINGS)

    def test_login_without_challenge(self):
        self.assertEqual(self.action(), 'login')

    def test_captcha_never_submits(self):
        self.assertEqual(self.action(captcha=True), 'waiting_captcha')

    def test_missing_credentials(self):
        self.assertEqual(self.action(credentials_ready=False), 'waiting_credentials')

    def test_manual_stop_prevents_launch_and_login(self):
        for kind in ('missing', 'login', 'main'):
            self.assertEqual(self.action(kind=kind, manual_stop=True), 'manual_stop')

    def test_main_never_restarts_even_when_offline(self):
        self.assertEqual(self.action(kind='main', broker_online=False), 'waiting_native_reconnect')
        self.assertEqual(self.action(kind='main', broker_online=True), 'client_online')

    def test_unknown_or_unreadable_process_does_not_start_duplicate(self):
        self.assertEqual(self.action(kind='observation_error'), 'observation_error')
        self.assertEqual(self.action(kind='unknown'), 'window_unverified')

    def test_locked_desktop_does_not_type(self):
        self.assertEqual(self.action(locked=True), 'waiting_unlock')

    def test_unsupported_controls_do_not_type(self):
        self.assertEqual(self.action(controls_supported=False), 'controls_unsupported')

    def test_password_error_latches_across_guard_restarts(self):
        state = RecoveryState()
        obs = dict(kind='login', password_error=True, credentials_ready=True, controls_supported=True)
        self.assertEqual(choose_action(obs, state, 1000, SETTINGS), 'password_error')
        recreated = RecoveryState(**vars(state))
        obs['password_error'] = False
        self.assertEqual(choose_action(obs, recreated, 5000, SETTINGS), 'password_error')
        obs['credential_version'] = 3
        self.assertEqual(choose_action(obs, recreated, 5000, SETTINGS), 'login')

    def test_backoff_persists_and_caps(self):
        state = RecoveryState()
        for delay in [60, 180, 600, 1800, 1800]:
            record_attempt(state, 1000, SETTINGS)
            self.assertEqual(state.next_attempt, 1000 + delay)
        recreated = RecoveryState(**vars(state))
        self.assertEqual(choose_action(dict(kind='missing'), recreated, 2000, SETTINGS), 'backoff')

    def test_launch_is_not_online(self):
        self.assertEqual(self.action(kind='missing'), 'launch')

    def test_offline_and_old_log_success_are_not_online(self):
        import datetime as dt
        from qmt_client_guard import BEIJING
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / 'userdata' / 'log'
            base.mkdir(parents=True)
            now = dt.datetime.now(BEIJING)
            path = base / ('XtClient_' + now.strftime('%Y%m%d') + '.log')
            stamp = now.strftime('%Y-%m-%d %H:%M:%S')
            path.write_text(stamp + ' onTradeServerLoginSuccess offline\n')
            self.assertFalse(broker_online_from_log(directory, 0))
            path.write_text(stamp + ' onRelogin status:12\n')
            self.assertTrue(broker_online_from_log(directory, now.timestamp() - 2))
            self.assertFalse(broker_online_from_log(directory, now.timestamp() + 2))
            path.write_text(stamp + ' onRelogin status:12\n' + stamp + ' onUserStatus status:22\n')
            self.assertFalse(broker_online_from_log(directory, 0))

    def test_password_probe_rejects_plaintext_and_empty(self):
        def make(solid):
            image = bytearray([245, 245, 245, 255] * (20 * 20))
            for y in range(4, 16):
                for x in range(5, 15):
                    if solid or x in (5, 14) or y in (4, 15):
                        image[(y * 20 + x) * 4:(y * 20 + x + 1) * 4] = bytes([128,128,128,255])
            return image
        self.assertFalse(password_is_masked(bytes([245,245,245,255] * 400),20,20))
        self.assertFalse(password_is_masked(make(False),20,20))
        self.assertTrue(password_is_masked(make(True),20,20))

    def test_field_change_catches_wrong_input_tolerates_caret(self):
        a = bytes([245,245,245,255] * 1000)
        b = bytearray(a)
        b[0:8] = bytes([0] * 8)
        self.assertTrue(same_field(a,b))
        b[0:400] = bytes([0] * 400)
        self.assertFalse(same_field(a,b))


if __name__ == '__main__':
    unittest.main()
