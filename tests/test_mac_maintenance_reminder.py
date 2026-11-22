"""2026-10-03季度体检提醒漏发：旧任务在launchd后台读不到Desktop，每次退出码2。"""
from __future__ import annotations

import datetime
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, folder: str = "scripts"):
    spec = importlib.util.spec_from_file_location(name, ROOT / folder / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


reminder = _load("mac_monthly_maintenance_reminder")
installer = _load("install_mac_maintenance_reminder", "tools")
FIRST_SATURDAY_OCT = datetime.date(2026, 10, 3)


class ReminderRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = root / "maintenance_reminder.json"
        self.state = root / "state.json"
        self.config.write_text(json.dumps({"bark_url": "https://bark.example/key/"}), encoding="utf-8")
        self.sent: list[tuple[str, str, str]] = []

    def _push(self, url: str, title: str, body: str) -> bool:
        self.sent.append((url, title, body))
        return True

    def _run(self, day: datetime.date, push=None) -> int:
        return reminder.run(day, config_path=self.config, state_path=self.state, push=push or self._push)

    def test_first_saturday_of_october_sends_maintenance_and_quarterly_review_once(self) -> None:
        self.assertEqual(self._run(FIRST_SATURDAY_OCT), 0)
        titles = [title for _url, title, _body in self.sent]
        self.assertEqual(titles, ["🔧 每月例行维护日", "📋 方案甲季度体检日"])
        self.assertIn("甲·季度体检，截至 2026-10-03", self.sent[1][2])
        self.assertEqual(self.sent[0][0], "https://bark.example/key")
        self.assertEqual(self._run(FIRST_SATURDAY_OCT), 0)
        self.assertEqual(len(self.sent), 2, "同一天重复触发不得重复推送")

    def test_other_days_send_nothing(self) -> None:
        for day in (datetime.date(2026, 10, 10), datetime.date(2026, 10, 2)):
            self.assertEqual(self._run(day), 0)
        self.assertEqual(self.sent, [])
        self._run(datetime.date(2026, 11, 7))
        self.assertEqual([title for _u, title, _b in self.sent], ["🔧 每月例行维护日"])

    def test_missing_config_fails_loudly(self) -> None:
        self.config.unlink()
        self.assertEqual(self._run(FIRST_SATURDAY_OCT), 1)
        self.assertEqual(self.sent, [])

    def test_failed_push_exits_nonzero_and_retries_next_time(self) -> None:
        self.assertEqual(self._run(FIRST_SATURDAY_OCT, push=lambda *_args: False), 1)
        self.assertFalse(self.state.exists())
        self.assertEqual(self._run(FIRST_SATURDAY_OCT), 0)
        self.assertEqual(len(self.sent), 2)


class RealPushTests(unittest.TestCase):
    """2026-11-23：上面的测试把推送函数换掉了，真正发请求那一步从没测过；分组名没转码，
    7月起每次推送都在发请求前报 'ascii' codec 错误。这里用本机临时服务真发一次。"""

    def test_every_real_message_link_is_plain_ascii(self) -> None:
        for day in (datetime.date(2026, 11, 7), FIRST_SATURDAY_OCT, datetime.date(2027, 1, 2)):
            for title, body in reminder.maintenance_messages(day):
                link = reminder.push_url("https://bark.example/key", title, body)
                self.assertTrue(link.isascii(), link)

    def test_push_reaches_server_with_title_body_and_group(self) -> None:
        import http.server
        import threading
        import urllib.parse

        received: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server 规定的名字
                received.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args) -> None:
                return None

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        title, body = reminder.maintenance_messages(datetime.date(2026, 11, 7))[0]
        base = f"http://127.0.0.1:{server.server_address[1]}/key"
        self.assertTrue(reminder._push(base, title, body))
        self.assertEqual(len(received), 1)
        path, _, query = received[0].partition("?")
        self.assertEqual([urllib.parse.unquote(part) for part in path.split("/")[2:]], [title, body])
        self.assertEqual(urllib.parse.parse_qs(query), {"group": ["A股实盘"]})


class InstallerTests(unittest.TestCase):
    def test_launchd_job_never_points_into_desktop(self) -> None:
        payload = installer.plist_payload()
        joined = " ".join(payload["ProgramArguments"]) + payload["StandardErrorPath"]
        self.assertNotIn("/Desktop/", joined)
        self.assertIn("Application Support/A_System", payload["ProgramArguments"][1])
        self.assertEqual(payload["StartCalendarInterval"], {"Weekday": 6, "Hour": 10, "Minute": 0})
        self.assertEqual(payload["Label"], "com.asystem.maintenance")

    def test_reminder_reads_bark_from_private_config_not_desktop_env(self) -> None:
        source = (ROOT / "scripts" / "mac_monthly_maintenance_reminder.py").read_text(encoding="utf-8")
        self.assertNotIn("/Desktop/A_System/.env", source)


if __name__ == "__main__":
    unittest.main()
