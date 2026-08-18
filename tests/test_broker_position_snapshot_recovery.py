from __future__ import annotations

import copy
import os
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

os.environ["A_SYSTEM_DISABLE_NOTIFICATIONS"] = "1"

if "dotenv" not in sys.modules:
    dotenv_stub = ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv_stub

from scripts import trading_daemon as daemon
from src.broker_execution_service import BrokerExecutionService


class _MemoryPositionStore:
    def __init__(self, positions: list[dict]) -> None:
        self.positions = copy.deepcopy(positions)

    def load(self) -> list[dict]:
        return copy.deepcopy(self.positions)

    def save(self, positions: list[dict]) -> None:
        self.positions = copy.deepcopy(positions)


def _ghost_position(*, shares: int = 11_800) -> dict:
    return {
        "order_id": "1090547061",
        "ts_code": "603118.SH",
        "name": "共进股份",
        "signal_date": "20260814",
        "buy_date": "20260817",
        "planned_exit_date": "20260818",
        "planned_exit_time": "2026-08-18 14:55",
        "shares": shares,
        "entry_shares": shares,
        "buy_price": 19.26,
        "strategy_leg": "L",
        "status": "closed",
        "sell_date": "20260818",
        "sell_price": 0.0,
        "exit_fills_by_date": {},
        "ghost_cleared_at": daemon.now_beijing().strftime("%Y-%m-%d 08:07:28"),
        "ghost_clear_source": "账户心跳",
        "ghost_clear_reason": "QMT接口查询成功且返回无实盘持仓",
    }


class BrokerPositionSnapshotRecoveryTests(unittest.TestCase):
    def tearDown(self) -> None:
        daemon._broker_missing_streak.clear()

    def test_query_success_but_inconsistent_empty_position_snapshot_is_rejected(self) -> None:
        account = SimpleNamespace(total_asset=278_600.0, available_cash=50_600.0)
        local = [_ghost_position()]

        with patch.object(daemon, "load_positions", return_value=local):
            with self.assertRaisesRegex(
                daemon.BrokerSnapshotInconsistentError,
                "本轮禁止清理本地持仓或执行新买入",
            ):
                daemon._sanitize_account_snapshot(account, [])

        self.assertEqual(account.total_asset, 278_600.0)

    def test_broker_reappearance_restores_strategy_identity_and_exit_plan(self) -> None:
        store = _MemoryPositionStore([_ghost_position()])
        broker = [
            SimpleNamespace(
                ts_code="603118.SH",
                volume=11_800,
                market_value=228_000.0,
            )
        ]

        with (
            patch.object(daemon, "load_positions", side_effect=store.load),
            patch.object(daemon, "save_positions", side_effect=store.save),
            patch.object(daemon, "_notify", return_value=True),
        ):
            restored = daemon.restore_ghost_cleared_strategy_positions(
                broker,
                "单元测试恢复",
            )

        self.assertEqual(restored, 1)
        row = store.positions[0]
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["sell_date"])
        self.assertIsNone(row["sell_price"])
        self.assertEqual(row["planned_exit_date"], "20260818")
        self.assertEqual(row["planned_exit_time"], "2026-08-18 14:55")
        self.assertEqual(row["ghost_restore_source"], "单元测试恢复")

    def test_quantity_mismatch_never_guesses_strategy_ownership(self) -> None:
        store = _MemoryPositionStore([_ghost_position()])
        broker = [SimpleNamespace(ts_code="603118.SH", volume=11_700)]

        with (
            patch.object(daemon, "load_positions", side_effect=store.load),
            patch.object(daemon, "save_positions", side_effect=store.save) as save,
            patch.object(daemon, "_notify", return_value=True),
        ):
            restored = daemon.restore_ghost_cleared_strategy_positions(
                broker,
                "单元测试歧义",
            )

        self.assertEqual(restored, 0)
        self.assertEqual(store.positions[0]["status"], "closed")
        save.assert_not_called()

    def test_recent_auto_ghost_still_blocks_new_strategy_entry(self) -> None:
        broker = [SimpleNamespace(ts_code="603118.SH", volume=11_800)]

        with patch.object(daemon, "load_positions", return_value=[_ghost_position()]):
            self.assertTrue(
                daemon._broker_has_preexisting_strategy_position(
                    broker,
                    as_of_date="20260818",
                )
            )

    def test_two_explicit_empty_snapshots_do_not_close_local_position(self) -> None:
        open_position = _ghost_position()
        open_position["status"] = "open"
        open_position.pop("ghost_cleared_at")
        open_position.pop("ghost_clear_source")
        open_position.pop("ghost_clear_reason")
        store = _MemoryPositionStore([open_position])
        config = {"live_trade": {"broker_missing_confirm_count": 3}}

        daemon._broker_missing_streak.clear()
        with (
            patch.object(daemon, "load_positions", side_effect=store.load),
            patch.object(daemon, "save_positions", side_effect=store.save),
            patch.object(daemon, "load_json_config", return_value=config),
        ):
            first = daemon.clear_local_positions_when_broker_empty(
                "测试第1轮",
                snapshot_complete=True,
            )
            second = daemon.clear_local_positions_when_broker_empty(
                "测试第2轮",
                snapshot_complete=True,
            )

        self.assertEqual((first, second), (0, 0))
        self.assertEqual(store.positions[0]["status"], "open")
        self.assertEqual(daemon._broker_missing_streak["603118.SH"], 2)

    def test_third_complete_missing_snapshot_closes_local_as_manually_handled(self) -> None:
        open_position = _ghost_position()
        open_position["status"] = "open"
        store = _MemoryPositionStore([open_position])
        config = {"live_trade": {"broker_missing_confirm_count": 3}}

        with (
            patch.object(daemon, "load_positions", side_effect=store.load),
            patch.object(daemon, "save_positions", side_effect=store.save),
            patch.object(daemon, "load_json_config", return_value=config),
        ):
            for round_no in range(1, 4):
                changed = daemon.reconcile_missing_local_positions(
                    [],
                    f"完整快照第{round_no}轮",
                    snapshot_complete=True,
                )

        self.assertEqual(changed, 1)
        self.assertEqual(store.positions[0]["status"], "closed")
        self.assertTrue(store.positions[0]["broker_confirmed_absent"])

    def test_incomplete_or_exception_snapshot_never_counts_as_missing(self) -> None:
        store = _MemoryPositionStore([_ghost_position()])
        store.positions[0]["status"] = "open"

        with patch.object(daemon, "load_positions", side_effect=store.load):
            changed = daemon.reconcile_missing_local_positions(
                [],
                "接口异常后的非完整结果",
                snapshot_complete=False,
            )

        self.assertEqual(changed, 0)
        self.assertEqual(daemon._broker_missing_streak, {})
        self.assertEqual(store.positions[0]["status"], "open")

    def test_valid_code_reappearance_resets_same_stock_missing_counter(self) -> None:
        store = _MemoryPositionStore([_ghost_position()])
        store.positions[0]["status"] = "open"
        daemon._broker_missing_streak["603118.SH"] = 2

        with patch.object(daemon, "load_positions", side_effect=store.load):
            changed = daemon.reconcile_missing_local_positions(
                [SimpleNamespace(ts_code="603118.SH", volume=11_800)],
                "股票code恢复",
                snapshot_complete=True,
            )

        self.assertEqual(changed, 0)
        self.assertNotIn("603118.SH", daemon._broker_missing_streak)
        self.assertEqual(store.positions[0]["status"], "open")

    def test_multiple_local_slices_count_once_per_complete_snapshot(self) -> None:
        first = _ghost_position(shares=5_000)
        first["status"] = "open"
        second = _ghost_position(shares=6_800)
        second["status"] = "open"
        second["order_id"] = "1090547062"
        store = _MemoryPositionStore([first, second])
        config = {"live_trade": {"broker_missing_confirm_count": 3}}

        with (
            patch.object(daemon, "load_positions", side_effect=store.load),
            patch.object(daemon, "load_json_config", return_value=config),
        ):
            changed = daemon.reconcile_missing_local_positions(
                [],
                "单轮两分片",
                snapshot_complete=True,
            )

        self.assertEqual(changed, 0)
        self.assertEqual(daemon._broker_missing_streak["603118.SH"], 1)

    def test_ordinary_query_exception_does_not_poison_or_stop_execution_service(self) -> None:
        class FlakyAdapter:
            def __init__(self) -> None:
                self.calls = 0

            def query_positions(self):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("QMT接口临时失败")
                return []

        raw = FlakyAdapter()
        service = BrokerExecutionService(default_timeout=1.0)
        proxy = service.proxy(lambda: raw)
        try:
            with self.assertRaisesRegex(RuntimeError, "QMT接口临时失败"):
                proxy.query_positions()
            self.assertFalse(service.metrics()["poisoned"])
            self.assertTrue(service.metrics()["worker_alive"])
            self.assertEqual(proxy.query_positions(), [])
        finally:
            service.shutdown()


if __name__ == "__main__":
    unittest.main()
