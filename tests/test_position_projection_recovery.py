from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import trading_daemon as daemon
from src.trade_intent_store import (
    STATUS_FILLED,
    STATUS_PREPARED,
    STATUS_SUBMITTED,
    STATUS_SUBMITTING,
    STATUS_VALIDATED,
    TradeIntentSpec,
    TradeIntentStore,
    build_idempotency_key,
)


def filled_buy(store: TradeIntentStore, *, quantity: int = 1000) -> dict:
    spec = TradeIntentSpec(
        idempotency_key=build_idempotency_key(
            account_fingerprint="acct",
            business_date="20260817",
            strategy_leg="A",
            side="BUY",
            ts_code="000001.SZ",
            purpose="OPEN",
            source_key="projection-test",
        ),
        account_fingerprint="acct",
        strategy_leg="A",
        side="BUY",
        ts_code="000001.SZ",
        business_date="20260817",
        signal_date="20260814",
        planned_exit_date="20260819",
        purpose="OPEN",
        source_key="projection-test",
        target_qty=quantity,
        target_amount=10000,
        price_type="FIXED_PRICE",
        limit_price=10,
        metadata={"name": "平安银行"},
    )
    row = store.create_intent(spec)
    intent_id = str(row["intent_id"])
    store.transition_intent(intent_id, STATUS_VALIDATED)
    store.transition_intent(intent_id, STATUS_PREPARED)
    store.transition_intent(intent_id, STATUS_SUBMITTING)
    store.transition_intent(
        intent_id,
        STATUS_SUBMITTED,
        broker_order_id="QMT-PROJECTION",
    )
    return store.transition_intent(
        intent_id,
        STATUS_FILLED,
        filled_qty=quantity,
        filled_amount=quantity * 10.2,
    )


class PositionProjectionRecoveryTests(unittest.TestCase):
    def test_daemon_periodic_alert_throttles_only_successful_delivery(self) -> None:
        daemon._ONCE_PER_STATE.clear()
        daemon._ONCE_PER_ATTEMPT.clear()
        with (
            patch.object(daemon.time, "time", side_effect=[1000.0, 1030.0, 1061.0, 1061.0]),
            patch.object(daemon, "_notify", side_effect=[False, True]) as notify,
        ):
            first = daemon._notify_once_per("qmt", 300, "title", "body")
            second = daemon._notify_once_per("qmt", 300, "title", "body")
            third = daemon._notify_once_per("qmt", 300, "title", "body")
        self.assertFalse(first)
        self.assertFalse(second)
        self.assertTrue(third)
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(daemon._ONCE_PER_STATE["qmt"], 1061.0)

    def test_qmt_timeout_requests_one_process_level_restart(self) -> None:
        exits: list[int] = []

        class ImmediateThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self) -> None:
                self.target()

        daemon._QMT_FATAL_RESTART_EVENT.clear()
        try:
            with (
                patch.object(daemon, "write_broker_health") as health,
                patch.object(daemon, "write_heartbeat") as heartbeat,
                patch.object(daemon, "_clear_qmt_last_success") as clear_cache,
                patch.object(daemon, "_notify_async") as notify,
                patch.object(daemon.threading, "Thread", ImmediateThread),
                patch.object(daemon.time, "sleep", return_value=None),
                patch.object(daemon.os, "_exit", side_effect=lambda code: exits.append(code)),
            ):
                daemon._on_qmt_execution_timeout("operation=query_positions", 7)
                daemon._on_qmt_execution_timeout("duplicate", 8)
            self.assertEqual(exits, [daemon.EXIT_CODE_QMT_CHANNEL_POISONED])
            health.assert_called_once()
            heartbeat.assert_called_once_with("qmt_channel_poisoned_restarting")
            clear_cache.assert_called_once()
            notify.assert_called_once()
        finally:
            daemon._QMT_FATAL_RESTART_EVENT.clear()

    def test_filled_buy_is_projected_before_marking_transaction_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TradeIntentStore(root / "events.sqlite3")
            row = filled_buy(store)
            positions_path = root / "positions.json"
            with (
                patch.object(daemon, "POSITIONS_FILE", positions_path),
                patch.object(daemon, "_trade_intent_store_instance", store),
                patch.object(daemon, "_exit_account_fingerprint", return_value="acct"),
                patch.object(daemon, "_track_execution", return_value=None),
            ):
                projected = daemon._project_recovered_buy_intents([
                    {
                        "stock_code": "000001.SZ",
                        "volume": 1000,
                        "cost_price": 10.2,
                    }
                ])
            self.assertEqual(projected, 1)
            positions = json.loads(positions_path.read_text(encoding="utf-8"))
            self.assertEqual(positions[0]["order_id"], "QMT-PROJECTION")
            self.assertEqual(positions[0]["shares"], 1000)
            self.assertEqual(positions[0]["strategy_leg"], "A")
            self.assertEqual(positions[0]["planned_exit_date"], "20260819")
            refreshed = store.get_intent(str(row["intent_id"]))
            self.assertEqual(refreshed["metadata"]["position_projected_qty"], 1000)

    def test_projection_blocks_when_broker_position_cannot_cover_fill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TradeIntentStore(root / "events.sqlite3")
            filled_buy(store)
            with (
                patch.object(daemon, "POSITIONS_FILE", root / "positions.json"),
                patch.object(daemon, "_trade_intent_store_instance", store),
                patch.object(daemon, "_exit_account_fingerprint", return_value="acct"),
                patch.object(daemon, "_track_execution", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "无法唯一投影"):
                    daemon._project_recovered_buy_intents([
                        {"stock_code": "000001.SZ", "volume": 900, "cost_price": 10.2}
                    ])

    def test_record_buy_updates_partial_fill_cumulatively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "positions.json"
            with (
                patch.object(daemon, "POSITIONS_FILE", path),
                patch.object(daemon, "_track_execution", return_value=None),
            ):
                common = dict(
                    order_id="QMT-PARTIAL",
                    ts_code="000001.SZ",
                    name="平安银行",
                    signal_date="20260814",
                    buy_date="20260817",
                    buy_price=10.2,
                    strategy_leg="A",
                    planned_exit_date_override="20260819",
                )
                daemon.record_buy(shares=400, **common)
                daemon.record_buy(shares=1000, **common)
                daemon.record_buy(shares=1000, **common)
            positions = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0]["shares"], 1000)
            self.assertEqual(positions[0]["entry_shares"], 1000)

    def test_corrupt_position_file_never_masquerades_as_empty_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "positions.json"
            path.write_text("{broken", encoding="utf-8")
            with patch.object(daemon, "POSITIONS_FILE", path):
                with self.assertRaisesRegex(RuntimeError, "读取持仓文件失败"):
                    daemon.load_positions()


if __name__ == "__main__":
    unittest.main()
