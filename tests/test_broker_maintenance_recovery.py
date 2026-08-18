from __future__ import annotations

import json
import datetime
import inspect
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import ANY, MagicMock, call, patch

# 单元测试不得向正式 Bark 地址发任何通知。
os.environ["A_SYSTEM_DISABLE_NOTIFICATIONS"] = "1"
# qmt_adapter 导入期只需 load_dotenv；测试环境未必安装 python-dotenv。
if "dotenv" not in sys.modules:
    dotenv_stub = ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv_stub

from scripts import trading_daemon
from scripts import win_daemon_keeper as keeper
from src.qmt_adapter import mask_account_id


class BrokerHealthStateTests(unittest.TestCase):
    def test_account_mask_never_exposes_full_broker_account(self) -> None:
        self.assertEqual(mask_account_id("1234567890"), "****90")
        self.assertEqual(mask_account_id("7"), "****7")

    def test_daemon_health_file_masks_account_and_records_current_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            health_path = Path(temp_dir) / "broker_health.json"
            with patch.object(trading_daemon, "BROKER_HEALTH_FILE", health_path):
                trading_daemon.write_broker_health(
                    "verified",
                    account_id="12345678",
                    failure_count=0,
                )

            payload = json.loads(health_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "verified")
            self.assertEqual(payload["pid"], os.getpid())
            self.assertEqual(payload["account"], "****78")
            self.assertNotIn("12345678", health_path.read_text(encoding="utf-8"))
            self.assertFalse(list(health_path.parent.glob("*.tmp")))

    def test_process_heartbeat_contains_current_pid_and_keeper_parses_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            heartbeat_path = Path(temp_dir) / "daemon_heartbeat.txt"
            with patch.object(trading_daemon, "HEARTBEAT_FILE", heartbeat_path):
                trading_daemon.write_heartbeat("sleeping")
                self.assertFalse(list(heartbeat_path.parent.glob("*.tmp")))
            with patch.object(keeper, "HEARTBEAT", heartbeat_path):
                status, age, heartbeat_pid = keeper.heartbeat_state()

        self.assertEqual(status, "sleeping")
        self.assertLess(age, 5)
        self.assertEqual(heartbeat_pid, os.getpid())

    def test_runtime_account_failure_is_recorded_by_daemon_even_on_weekend(self) -> None:
        health_writer = MagicMock()
        old_reconnect_count = trading_daemon._qmt_reconnect_count
        trading_daemon._qmt_reconnect_count = 0
        try:
            with patch.object(
                trading_daemon,
                "_qmt_get",
                side_effect=RuntimeError("模拟券商维护"),
            ), patch.object(
                trading_daemon,
                "_qmt_reset",
            ), patch.object(
                trading_daemon,
                "qmt_is_critical_window",
                return_value=False,
            ), patch.object(
                trading_daemon,
                "write_broker_health",
                health_writer,
            ):
                trading_daemon._print_account_status(MagicMock())
        finally:
            trading_daemon._qmt_reconnect_count = old_reconnect_count

        health_writer.assert_any_call(
            "unavailable",
            error=ANY,
            failure_count=1,
        )

    def test_qmt_success_cache_does_not_persist_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "qmt_last_success.json"
            with patch.object(trading_daemon, "QMT_LAST_SUCCESS_FILE", cache_path):
                trading_daemon._save_qmt_last_success(
                    qmt_path=r"C:\QMT\userdata_mini",
                    session_id="1002",
                )

            payload = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertNotIn("account_id", payload)

    def test_ready_notification_is_sent_only_with_running_heartbeat(self) -> None:
        parent = MagicMock()
        heartbeat = parent.heartbeat
        notify_retry = parent.notify_retry
        with patch.object(
            trading_daemon,
            "write_heartbeat",
            heartbeat,
        ), patch.object(
            trading_daemon,
            "_notify_with_retry_async",
            notify_retry,
        ):
            trading_daemon._publish_system_ready()

        self.assertEqual(
            parent.mock_calls,
            [
                call.heartbeat("running"),
                call.notify_retry(
                    "connection",
                    trading_daemon.SYSTEM_READY_TITLE,
                    trading_daemon.SYSTEM_READY_BODY,
                    level="timeSensitive",
                ),
            ],
        )

    def test_periodic_health_schedule_uses_even_hours_at_minute_02(self) -> None:
        timezone = trading_daemon.BEIJING_TZ
        cases = (
            (
                datetime.datetime(2026, 8, 18, 15, 59, tzinfo=timezone),
                datetime.datetime(2026, 8, 18, 16, 2, tzinfo=timezone),
            ),
            (
                datetime.datetime(2026, 8, 18, 16, 2, 1, tzinfo=timezone),
                datetime.datetime(2026, 8, 18, 18, 2, tzinfo=timezone),
            ),
            (
                datetime.datetime(2026, 8, 18, 23, 30, tzinfo=timezone),
                datetime.datetime(2026, 8, 19, 0, 2, tzinfo=timezone),
            ),
        )

        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(
                    trading_daemon._next_periodic_health_beacon_at(now),
                    expected,
                )

    def test_periodic_health_snapshot_requires_fresh_same_process_facts(self) -> None:
        timezone = trading_daemon.BEIJING_TZ
        now = datetime.datetime(2026, 8, 18, 8, 2, tzinfo=timezone)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            heartbeat = root / "daemon_heartbeat.txt"
            broker_health = root / "broker_health.json"
            heartbeat.write_text(
                f"{(now - datetime.timedelta(seconds=10)).isoformat()} pid=123 sleeping\n",
                encoding="utf-8",
            )
            broker_health.write_text(
                json.dumps(
                    {
                        "updated_at": (now - datetime.timedelta(seconds=20)).isoformat(),
                        "pid": 123,
                        "status": "verified",
                        "account": "****03",
                    }
                ),
                encoding="utf-8",
            )

            snapshot = trading_daemon._periodic_health_snapshot(
                now=now,
                heartbeat_path=heartbeat,
                broker_health_path=broker_health,
                current_pid=123,
            )

            self.assertTrue(snapshot["ok"])
            self.assertEqual(snapshot["heartbeat_age_sec"], 10)
            self.assertEqual(snapshot["broker_age_sec"], 20)
            self.assertEqual(snapshot["account"], "****03")

            broker_health.write_text(
                json.dumps(
                    {
                        "updated_at": (now - datetime.timedelta(seconds=181)).isoformat(),
                        "pid": 123,
                        "status": "verified",
                        "account": "****03",
                    }
                ),
                encoding="utf-8",
            )
            stale = trading_daemon._periodic_health_snapshot(
                now=now,
                heartbeat_path=heartbeat,
                broker_health_path=broker_health,
                current_pid=123,
                broker_health_fresh_sec=180,
            )

        self.assertFalse(stale["ok"])
        self.assertTrue(any("QMT验证已陈旧181秒" in value for value in stale["errors"]))

    def test_periodic_health_publish_is_read_only_and_never_calls_qmt(self) -> None:
        now = datetime.datetime.now(tz=trading_daemon.BEIJING_TZ)
        pid = os.getpid()
        config = {
            "notify": {
                "health_beacon": {
                    "enabled": True,
                    "heartbeat_fresh_sec": 45,
                    "broker_health_fresh_sec": 180,
                    "retry_attempts": 3,
                    "retry_sec": 30,
                }
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            heartbeat = root / "daemon_heartbeat.txt"
            broker_health = root / "broker_health.json"
            heartbeat.write_text(
                f"{now.isoformat()} pid={pid} sleeping\n",
                encoding="utf-8",
            )
            broker_health.write_text(
                json.dumps(
                    {
                        "updated_at": now.isoformat(),
                        "pid": pid,
                        "status": "verified",
                        "account": "****03",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(trading_daemon, "HEARTBEAT_FILE", heartbeat), \
                 patch.object(trading_daemon, "BROKER_HEALTH_FILE", broker_health), \
                 patch.object(trading_daemon, "_qmt_get") as qmt_get, \
                 patch.object(
                     trading_daemon,
                     "_notify_with_retry_async",
                 ) as notify_async:
                snapshot = trading_daemon._publish_periodic_health_beacon(
                    config,
                    now=now,
                )

        self.assertTrue(snapshot["ok"])
        qmt_get.assert_not_called()
        notify_async.assert_called_once()
        self.assertEqual(notify_async.call_args.args[0], "health_beacon")
        self.assertEqual(notify_async.call_args.args[1], "✅ A_System运行正常")

    def test_periodic_health_code_has_no_trading_or_broker_call_path(self) -> None:
        source = "\n".join(
            (
                inspect.getsource(trading_daemon._periodic_health_snapshot),
                inspect.getsource(trading_daemon._publish_periodic_health_beacon),
                inspect.getsource(trading_daemon._periodic_health_beacon_loop),
            )
        )
        for forbidden in (
            "_qmt_get",
            "_qmt_lock",
            "_exit_sell_lock",
            "load_positions",
            "run_job",
            "place_order",
            "cancel_order",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class RecoveryGateTests(unittest.TestCase):
    def test_fresh_unparsed_heartbeat_requires_consecutive_confirmation(self) -> None:
        with patch.object(keeper, "HEARTBEAT_PID_MISMATCH_CONFIRMATIONS", 3):
            self.assertEqual(
                keeper.heartbeat_restart_reason(
                    age=1,
                    heartbeat_same_process=False,
                    mismatch_count=1,
                ),
                "",
            )
            self.assertIn(
                "连续3次",
                keeper.heartbeat_restart_reason(
                    age=1,
                    heartbeat_same_process=False,
                    mismatch_count=3,
                ),
            )

    def test_stale_heartbeat_still_restarts_immediately(self) -> None:
        with patch.object(keeper, "STALE_LIMIT", 60):
            self.assertIn(
                "心跳陈旧",
                keeper.heartbeat_restart_reason(
                    age=61,
                    heartbeat_same_process=True,
                    mismatch_count=0,
                ),
            )

    def test_gbk_console_cannot_break_keeper_log(self) -> None:
        class GbkConsole:
            encoding = "gbk"

            def __init__(self) -> None:
                self.output: list[str] = []

            def write(self, value: str) -> int:
                # 模拟 Windows PowerShell：原始 emoji 无法编码，替换后的文本可输出。
                value.encode(self.encoding)
                self.output.append(value)
                return len(value)

            def flush(self) -> None:
                return None

        console = GbkConsole()
        file_logger = MagicMock()
        with patch.object(keeper.sys, "stdout", console), patch.object(
            keeper,
            "_get_file_logger",
            return_value=file_logger,
        ):
            keeper.log("✅ 程序与账户已恢复正常")

        file_logger.info.assert_called_once()
        self.assertIn("✅ 程序与账户已恢复正常", file_logger.info.call_args.args[0])
        self.assertTrue(console.output)

    def test_successful_push_stays_successful_when_log_fails(self) -> None:
        encoding_error = UnicodeEncodeError(
            "gbk",
            "✅",
            0,
            1,
            "illegal multibyte sequence",
        )
        with patch("src.notify.notify", return_value=True), patch.object(
            keeper,
            "log",
            side_effect=encoding_error,
        ):
            sent = keeper.notify(
                "✅ 程序与账户已恢复正常",
                "模拟恢复通知",
                event="connection",
            )

        self.assertTrue(sent)

    def test_keeper_recovery_depends_only_on_current_pid_heartbeat(self) -> None:
        self.assertTrue(
            keeper.process_heartbeat_ready(
                heartbeat_age=5,
                heartbeat_same_process=True,
            )
        )
        self.assertFalse(
            keeper.process_heartbeat_ready(
                heartbeat_age=keeper.STALE_LIMIT + 1,
                heartbeat_same_process=True,
            )
        )
        self.assertFalse(
            keeper.process_heartbeat_ready(
                heartbeat_age=1,
                heartbeat_same_process=False,
            )
        )

    def test_keeper_source_has_no_broker_or_trade_state_dependency(self) -> None:
        source = Path(keeper.__file__).read_text(encoding="utf-8")
        forbidden = (
            "BROKER_HEALTH",
            "broker_health_state",
            "program_and_account_ready",
            "query_account",
            "query_orders",
            "query_trades",
            "query_positions",
        )
        for marker in forbidden:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source)

    def test_restart_policy_never_returns_permanent_stop(self) -> None:
        self.assertEqual(
            keeper.restart_delay_seconds(keeper.MAX_CONSECUTIVE_RESTARTS),
            keeper.CHECK_INTERVAL,
        )
        self.assertEqual(
            keeper.restart_delay_seconds(keeper.MAX_CONSECUTIVE_RESTARTS + 1),
            keeper.CRASH_LOOP_RETRY_SEC,
        )
        self.assertGreater(keeper.restart_delay_seconds(10_000), 0)


if __name__ == "__main__":
    unittest.main()
