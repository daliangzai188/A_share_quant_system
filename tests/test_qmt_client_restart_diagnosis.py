"""2026-09-15~09-28客户端定时重启：现场归类与只读排查脚本的离线测试。"""
from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from scripts import diagnose_qmt_client_restarts as diag
from src.qmt_client_probe import BEIJING_TZ, classify_qmt_client_state, describe_processes, mask_title


def _at(hour: int, minute: int, second: int = 0, day: int = 21) -> dt.datetime:
    return dt.datetime(2026, 9, day, hour, minute, second, tzinfo=BEIJING_TZ)


class ClientStateTests(unittest.TestCase):
    def test_login_window_means_manual_login_is_required(self):
        processes = [{"pid": 7, "started_at": _at(20, 52, 24), "window_titles": ["国金证券QMT交易端 登录"]}]
        state = classify_qmt_client_state(processes, last_verified_at=_at(20, 51, 50))
        self.assertEqual(state["state"], "client_login_pending")
        self.assertIn("手动登录", state["action"])
        self.assertIn("09-21 20:52:24", state["summary"])

    def test_restart_after_last_verification_is_reported_with_time(self):
        processes = [{"pid": 7, "started_at": _at(20, 52, 24), "window_titles": ["国金证券QMT交易端"]}]
        state = classify_qmt_client_state(processes, last_verified_at=_at(20, 51, 50))
        self.assertEqual(state["state"], "client_restarted")
        self.assertIn("20:52:24重新启动", state["summary"])

    def test_long_running_client_points_to_the_model(self):
        processes = [{"pid": 7, "started_at": _at(10, 45, day=28), "window_titles": ["国金证券QMT交易端"]}]
        state = classify_qmt_client_state(processes, last_verified_at=_at(20, 51, 50, day=29))
        self.assertEqual(state["state"], "model_not_running")
        self.assertIn("模型交易", state["action"])

    def test_missing_client_and_unknown_platform(self):
        self.assertEqual(classify_qmt_client_state([], last_verified_at=None)["state"], "client_missing")
        unknown = classify_qmt_client_state(None, last_verified_at=None)
        self.assertEqual(unknown["state"], "unknown")
        self.assertIn("核对客户端登录", unknown["action"])

    def test_unverified_daemon_does_not_claim_restart(self):
        processes = [{"pid": 7, "started_at": _at(20, 52, 24), "window_titles": []}]
        state = classify_qmt_client_state(processes, last_verified_at=None)
        self.assertEqual(state["state"], "client_running_unverified")

    def test_newest_process_wins_during_overlap(self):
        processes = [
            {"pid": 1, "started_at": _at(9, 0, day=15), "window_titles": []},
            {"pid": 2, "started_at": _at(20, 52, 24), "window_titles": []},
        ]
        state = classify_qmt_client_state(processes, last_verified_at=_at(20, 51, 50))
        self.assertEqual(state["state"], "client_restarted")

    def test_titles_and_descriptions_hide_account_numbers(self):
        self.assertEqual(mask_title("资金账号 8881234503"), "资金账号 ***03")
        text = describe_processes([{"pid": 7, "started_at": _at(20, 52, 24), "window_titles": ["登录"]}])
        self.assertIn("2026-09-21 20:52:24", text)
        self.assertEqual(describe_processes([]), "无客户端进程")


class DiagnosisScriptTests(unittest.TestCase):
    def test_mask_keeps_timestamps_and_hides_accounts(self):
        line = "20260921 205224 账号8881234503 登录 资金账号 55512345"
        masked = diag.mask(line, secrets=["8881234503"])
        self.assertIn("20260921", masked)
        self.assertIn("205224", masked)
        self.assertNotIn("8881234503", masked)
        self.assertNotIn("55512345", masked)
        self.assertIn("***03", masked)

    def test_parse_line_uses_line_date_then_falls_back(self):
        stamp, current = diag.parse_line_datetime("[2026-09-21 20:52:24.123] restart", None)
        self.assertEqual(stamp, dt.datetime(2026, 9, 21, 20, 52, 24))
        stamp, current = diag.parse_line_datetime("20:52:30 login", current)
        self.assertEqual(stamp, dt.datetime(2026, 9, 21, 20, 52, 30))
        self.assertEqual(diag.parse_line_datetime("no time here", None), (None, None))

    def test_scan_log_file_keeps_window_lines_and_skips_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "XtClient_20260921.log"
            lines = [
                "2026-09-21 20:51:00 心跳正常",
                "2026-09-21 20:52:20 定时重启客户端",
                "2026-09-21 20:52:40 登录密码错误",
                "20:52:45 等待输入验证码",
                "2026-09-21 15:00:00 检测到新版本 update",
                "2026-09-10 20:52:00 restart（早于排查区间）",
            ]
            path.write_bytes("\n".join(lines).encode("gbk"))
            report = diag.scan_log_file(path, since=dt.date(2026, 9, 14), until=dt.date(2026, 9, 30))
        window = [item["line"] for item in report["window_lines"]]
        self.assertIn("2026-09-21 20:52:20 定时重启客户端", window)
        self.assertIn("20:52:45 等待输入验证码", window)
        self.assertFalse(any("密码" in line for line in window))
        self.assertNotIn("2026-09-21 20:51:00 心跳正常", window)
        strong = [item["line"] for item in report["strong_lines"]]
        self.assertEqual(strong, ["2026-09-21 15:00:00 检测到新版本 update"])

    def test_config_scan_skips_credentials(self):
        text = "AutoRestart=1\nRestartTime=08:50\npassword=abc\nAutoLogin=1\nFontSize=12"
        hits = diag.scan_config_text(text)
        self.assertEqual(hits, ["AutoRestart=1", "RestartTime=08:50", "AutoLogin=1"])

    def test_outage_episodes_and_correlation(self):
        log = [
            "2026-09-21 20:51:50 | ✅ [账户] ok",
            "2026-09-21 20:52:51 | QMT账户连接状态=未验证：QMT inner bridge heartbeat is stale",
            "2026-09-21 20:53:51 | QMT连接失败: QMT inner bridge heartbeat is stale",
            "2026-09-18 20:53:11 | QMT inner bridge restarted; reconnect and reconcile before trading",
            "2026-09-10 08:51:01 | QMT inner bridge heartbeat is stale",
        ]
        episodes = diag.extract_outage_episodes(log, dt.date(2026, 9, 14))
        self.assertEqual([e["start"] for e in episodes], ["2026-09-18 20:53:11", "2026-09-21 20:52:51"])
        self.assertEqual(episodes[1]["minutes"], 1.0)
        self.assertEqual(episodes[0]["kinds"], ["模型实例变化"])
        evidence = [
            {"time": "2026-09-21 20:52:24", "source": "QMT日志", "detail": "定时重启"},
            {"time": "2026-09-21 21:30:00", "source": "QMT日志", "detail": "无关"},
        ]
        result = diag.correlate(episodes[1:], evidence)
        self.assertEqual(result[0]["evidence_count"], 1)
        self.assertEqual(result[0]["evidence"][0]["detail"], "定时重启")

    def test_main_refuses_to_run_outside_windows(self):
        if diag.os.name == "nt":
            self.skipTest("Windows 上会真实采集")
        with self.assertRaises(SystemExit):
            diag.main([])


if __name__ == "__main__":
    unittest.main()
