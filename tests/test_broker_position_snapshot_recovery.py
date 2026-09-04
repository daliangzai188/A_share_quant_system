from __future__ import annotations

import copy
import datetime
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
        "strategy_leg": "E",
        "status": "closed",
        "sell_date": "20260818",
        "sell_price": 0.0,
        "exit_fills_by_date": {},
        # 固定事故日，避免测试随着日历推进后把“未来清理时间”误判为非近期。
        "ghost_cleared_at": "2026-08-18 08:07:28",
        "ghost_clear_source": "账户心跳",
        "ghost_clear_reason": "QMT接口查询成功且返回无实盘持仓",
    }


class BrokerPositionSnapshotRecoveryTests(unittest.TestCase):
    def tearDown(self) -> None:
        daemon._broker_missing_streak.clear()

    def test_query_success_but_inconsistent_empty_position_snapshot_is_rejected(self) -> None:
        account = SimpleNamespace(total_asset=278_600.0, available_cash=50_600.0)
        local = [_ghost_position()]

        with (
            patch.object(daemon, "load_positions", return_value=local),
            patch.object(
                daemon,
                "today_beijing",
                return_value=datetime.date(2026, 8, 18),
            ),
        ):
            with self.assertRaisesRegex(
                daemon.BrokerSnapshotInconsistentError,
                "本轮禁止清理本地持仓或执行新买入",
            ):
                daemon._sanitize_account_snapshot(account, [])

        self.assertEqual(account.total_asset, 278_600.0)

    def test_pending_buy_reported_frozen_cash_does_not_fake_missing_position(self) -> None:
        """复现2026-09-04：预挂买单冻结6.74万后，不得把资产差额
        误当成近期策略持仓在QMT中消失。
        """

        account = SimpleNamespace(
            total_asset=81_912.70,
            available_cash=14_520.70,
            frozen_cash=67_392.00,
        )
        recent_ghost = _ghost_position()
        recent_ghost["sell_date"] = "20260902"
        recent_ghost["ghost_cleared_at"] = "2026-09-02 11:14:31"
        with (
            patch.object(daemon, "load_positions", return_value=[recent_ghost]),
            patch.object(
                daemon,
                "today_beijing",
                return_value=datetime.date(2026, 9, 4),
            ),
        ):
            daemon._sanitize_account_snapshot(account, [])

        self.assertEqual(account.total_asset, 81_912.70)

    def test_pending_buy_order_fallback_explains_frozen_cash_when_asset_field_missing(self) -> None:
        account = SimpleNamespace(
            total_asset=81_912.70,
            available_cash=14_520.70,
            frozen_cash=0.0,
        )

        class Adapter:
            def __init__(self) -> None:
                self.order_queries = 0

            def query_account(self):
                return account

            def query_positions(self):
                return []

            def query_orders(self):
                self.order_queries += 1
                return [
                    {
                        "order_status": 50,
                        "order_type": 23,
                        "order_volume": 400,
                        "traded_volume": 0,
                        "order_price": 168.48,
                    }
                ]

        adapter = Adapter()
        recent_ghost = _ghost_position()
        recent_ghost["sell_date"] = "20260902"
        recent_ghost["ghost_cleared_at"] = "2026-09-02 11:14:31"
        with (
            patch.object(daemon, "load_positions", return_value=[recent_ghost]),
            patch.object(
                daemon,
                "today_beijing",
                return_value=datetime.date(2026, 9, 4),
            ),
        ):
            returned_account, returned_positions = daemon._qmt_query_account_positions(
                adapter
            )

        self.assertIs(returned_account, account)
        self.assertEqual(returned_positions, [])
        self.assertEqual(adapter.order_queries, 1)

    def test_terminal_buy_order_cannot_hide_real_missing_position(self) -> None:
        account = SimpleNamespace(
            total_asset=278_600.0,
            available_cash=50_600.0,
            frozen_cash=0.0,
        )
        terminal_order = {
            "order_status": 54,
            "order_type": 23,
            "order_volume": 11_800,
            "traded_volume": 0,
            "order_price": 19.32,
        }
        with (
            patch.object(daemon, "load_positions", return_value=[_ghost_position()]),
            patch.object(
                daemon,
                "today_beijing",
                return_value=datetime.date(2026, 8, 18),
            ),
        ):
            with self.assertRaises(daemon.BrokerSnapshotInconsistentError):
                daemon._sanitize_account_snapshot(
                    account,
                    [],
                    active_orders=[terminal_order],
                )

    def test_snapshot_inconsistency_never_resets_healthy_qmt_session(self) -> None:
        old_reconnect_count = daemon._qmt_reconnect_count
        daemon._qmt_reconnect_count = 2
        log = SimpleNamespace(error=lambda *_args, **_kwargs: None)
        error = daemon.BrokerSnapshotInconsistentError("模拟账务快照不一致")
        try:
            with (
                patch.object(
                    daemon,
                    "load_json_config",
                    return_value={
                        "broker_adapter_enabled": True,
                        "qmt_enabled": True,
                        "broker": {"enabled": True},
                    },
                ),
                patch.object(daemon, "_qmt_get", return_value=object()),
                patch.object(
                    daemon,
                    "_qmt_query_account_positions",
                    side_effect=error,
                ),
                patch.object(daemon, "_qmt_reset") as reset,
                patch.object(daemon, "write_broker_health") as health,
                patch.object(daemon, "_notify_once_per") as notify,
            ):
                daemon._print_account_status(log)

            reset.assert_not_called()
            health.assert_called_once_with(
                "snapshot_inconsistent",
                error=error,
                failure_count=0,
            )
            notify.assert_called_once()
            self.assertEqual(daemon._qmt_reconnect_count, 0)
        finally:
            daemon._qmt_reconnect_count = old_reconnect_count

    def test_waiting_free_writer_exits_process_without_another_qmt_reset(self) -> None:
        """底层连接资源已经耗尽时，再disconnect/connect只会继续泄漏。

        心跳必须直接交给统一进程恢复门，由keeper拉起全新daemon。
        """

        log = SimpleNamespace(error=lambda *_args, **_kwargs: None)
        error = RuntimeError("WaitingFreeWriter instances exceed maximum limit")
        with (
            patch.object(
                daemon,
                "load_json_config",
                return_value={
                    "broker_adapter_enabled": True,
                    "qmt_enabled": True,
                    "broker": {"enabled": True},
                },
            ),
            patch.object(daemon, "_qmt_get", return_value=object()),
            patch.object(
                daemon,
                "_qmt_query_account_positions",
                side_effect=error,
            ),
            patch.object(
                daemon,
                "_request_qmt_resource_recovery",
                return_value=True,
            ) as recover,
            patch.object(daemon, "_qmt_reset") as reset,
        ):
            daemon._print_account_status(log)

        recover.assert_called_once_with(error)
        reset.assert_not_called()

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
            patch.object(
                daemon,
                "today_beijing",
                return_value=datetime.date(2026, 8, 18),
            ),
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
