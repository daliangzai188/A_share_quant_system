from __future__ import annotations

import json
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
            with patch.object(keeper, "HEARTBEAT", heartbeat_path):
                status, age, heartbeat_pid = keeper.heartbeat_state()

        self.assertEqual(status, "sleeping")
        self.assertLess(age, 5)
        self.assertEqual(heartbeat_pid, os.getpid())

    def test_keeper_rejects_health_left_by_previous_daemon_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            health_path = Path(temp_dir) / "broker_health.json"
            health_path.write_text(
                json.dumps(
                    {
                        "status": "verified",
                        "updated_ts": time.time(),
                        "pid": 100,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(keeper, "BROKER_HEALTH", health_path):
                status, age, same_process = keeper.broker_health_state(200)

        self.assertEqual(status, "verified")
        self.assertLess(age, 5)
        self.assertFalse(same_process)

    def test_runtime_account_failure_is_visible_to_keeper_even_on_weekend(self) -> None:
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
        notify_async = parent.notify_async
        with patch.object(
            trading_daemon,
            "write_heartbeat",
            heartbeat,
        ), patch.object(
            trading_daemon,
            "_notify_async",
            notify_async,
        ):
            trading_daemon._publish_system_ready()

        self.assertEqual(
            parent.mock_calls,
            [
                call.heartbeat("running"),
                call.notify_async(
                    "connection",
                    trading_daemon.SYSTEM_READY_TITLE,
                    trading_daemon.SYSTEM_READY_BODY,
                    level="timeSensitive",
                ),
            ],
        )


class RecoveryGateTests(unittest.TestCase):
    def test_recovery_requires_program_and_account_from_same_process(self) -> None:
        healthy = keeper.program_and_account_ready(
            program_state="sleeping",
            heartbeat_age=5,
            heartbeat_same_process=True,
            broker_state="verified",
            broker_same_process=True,
        )
        self.assertTrue(healthy)

        cases = [
            {"program_state": "qmt_blocked"},
            {"heartbeat_age": keeper.STALE_LIMIT + 1},
            {"heartbeat_same_process": False},
            {"broker_state": "starting"},
            {"broker_state": "unavailable"},
            {"broker_same_process": False},
        ]
        base = {
            "program_state": "running",
            "heartbeat_age": 1,
            "heartbeat_same_process": True,
            "broker_state": "verified",
            "broker_same_process": True,
        }
        for override in cases:
            values = {**base, **override}
            with self.subTest(override=override):
                self.assertFalse(keeper.program_and_account_ready(**values))

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
