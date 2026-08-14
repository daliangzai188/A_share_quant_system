from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.trade_intent_store import (
    STATUS_CANCELLED,
    STATUS_FILLED,
    STATUS_PREPARED,
    STATUS_RECOVERY_REQUIRED,
    STATUS_SUBMITTED,
    STATUS_SUBMITTING,
    STATUS_VALIDATED,
    TradeIntentSpec,
    TradeIntentStore,
    build_idempotency_key,
)
from src.trade_recovery import TradeRecoveryCoordinator


def make_spec(source_key: str = "open-1") -> TradeIntentSpec:
    return TradeIntentSpec(
        idempotency_key=build_idempotency_key(
            account_fingerprint="acct",
            business_date="20260817",
            strategy_leg="A",
            side="BUY",
            ts_code="000001.SZ",
            purpose="OPEN",
            source_key=source_key,
        ),
        account_fingerprint="acct",
        strategy_leg="A",
        side="BUY",
        ts_code="000001.SZ",
        business_date="20260817",
        signal_date="20260814",
        planned_exit_date="20260819",
        purpose="OPEN",
        source_key=source_key,
        target_qty=1000,
        target_amount=10000,
        price_type="FIXED_PRICE",
        limit_price=10,
        metadata={"remark": "PREMARKET-A-000001"},
    )


def advance_to_prepared(store: TradeIntentStore, source_key: str = "open-1") -> dict:
    row = store.create_intent(make_spec(source_key))
    row = store.transition_intent(str(row["intent_id"]), STATUS_VALIDATED)
    return store.transition_intent(str(row["intent_id"]), STATUS_PREPARED)


class TradeRecoveryCoordinatorTests(unittest.TestCase):
    def test_empty_recovery_persists_all_broker_truth_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TradeIntentStore(Path(temporary) / "events.sqlite3")
            result = TradeRecoveryCoordinator(store).recover(
                daemon_boot_id="boot-1",
                account_fingerprint="acct",
                business_date="20260817",
                positions=[{"stock_code": "000001.SZ", "volume": 1000, "can_use_volume": 1000}],
                orders=[{"order_id": "external-1", "stock_code": "600000.SH", "order_type": 23}],
                trades=[{"order_id": "external-1", "stock_code": "600000.SH", "traded_volume": 100, "traded_price": 10}],
            )
            self.assertEqual(result.status, "PASS")
            self.assertEqual(result.recoverable_count, 0)
            self.assertEqual(store.audit()["broker_recovery_object_count"], 3)
            run = store.get_recovery_run(result.recovery_id)
            self.assertEqual(run["status"], "PASS")
            self.assertEqual(run["details"]["position_count"], 1)

    def test_submitting_intent_is_bound_by_exact_broker_remark_and_filled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TradeIntentStore(Path(temporary) / "events.sqlite3")
            row = advance_to_prepared(store)
            row = store.transition_intent(str(row["intent_id"]), STATUS_SUBMITTING)
            result = TradeRecoveryCoordinator(store).recover(
                daemon_boot_id="boot-2",
                account_fingerprint="acct",
                business_date="20260817",
                positions=[{"stock_code": "000001.SZ", "volume": 1000}],
                orders=[{
                    "order_id": "QMT-1",
                    "stock_code": "000001.SZ",
                    "order_type": 23,
                    "order_volume": 1000,
                    "traded_volume": 1000,
                    "order_status": 56,
                    "order_remark": "PREMARKET-A-000001",
                }],
                trades=[{
                    "order_id": "QMT-1",
                    "stock_code": "000001.SZ",
                    "order_type": 23,
                    "traded_volume": 1000,
                    "traded_price": 10.2,
                }],
            )
            self.assertEqual(result.status, "PASS")
            recovered = store.get_intent(str(row["intent_id"]))
            self.assertEqual(recovered["status"], STATUS_FILLED)
            self.assertEqual(recovered["broker_order_id"], "QMT-1")
            self.assertEqual(recovered["filled_qty"], 1000)
            self.assertAlmostEqual(recovered["avg_fill_price"], 10.2)

    def test_existing_order_id_recovers_partial_cancel_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TradeIntentStore(Path(temporary) / "events.sqlite3")
            row = advance_to_prepared(store, "open-cancel")
            row = store.transition_intent(str(row["intent_id"]), STATUS_SUBMITTING)
            row = store.transition_intent(
                str(row["intent_id"]), STATUS_SUBMITTED, broker_order_id="QMT-2"
            )
            result = TradeRecoveryCoordinator(store).recover(
                daemon_boot_id="boot-3",
                account_fingerprint="acct",
                business_date="20260817",
                positions=[{"stock_code": "000001.SZ", "volume": 400}],
                orders=[{
                    "order_id": "QMT-2",
                    "stock_code": "000001.SZ",
                    "order_type": 23,
                    "order_volume": 1000,
                    "traded_volume": 400,
                    "traded_price": 10,
                    "order_status": 53,
                    "order_remark": "PREMARKET-A-000001",
                }],
                trades=[],
            )
            self.assertEqual(result.status, "PASS")
            recovered = store.get_intent(str(row["intent_id"]))
            self.assertEqual(recovered["status"], STATUS_CANCELLED)
            self.assertEqual(recovered["filled_qty"], 400)

    def test_position_alone_never_guesses_intent_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TradeIntentStore(Path(temporary) / "events.sqlite3")
            row = advance_to_prepared(store)
            result = TradeRecoveryCoordinator(store).recover(
                daemon_boot_id="boot-4",
                account_fingerprint="acct",
                business_date="20260817",
                positions=[{"stock_code": "000001.SZ", "volume": 1000}],
                orders=[],
                trades=[],
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.unresolved_count, 1)
            recovered = store.get_intent(str(row["intent_id"]))
            self.assertEqual(recovered["status"], STATUS_RECOVERY_REQUIRED)
            self.assertEqual(result.unresolved[0]["broker_position_volume"], 1000)

    def test_ambiguous_broker_orders_block_instead_of_picking_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TradeIntentStore(Path(temporary) / "events.sqlite3")
            advance_to_prepared(store)
            common = {
                "stock_code": "000001.SZ",
                "order_type": 23,
                "order_volume": 1000,
                "order_status": 50,
                "order_remark": "PREMARKET-A-000001",
            }
            result = TradeRecoveryCoordinator(store).recover(
                daemon_boot_id="boot-5",
                account_fingerprint="acct",
                business_date="20260817",
                positions=[],
                orders=[{"order_id": "QMT-A", **common}, {"order_id": "QMT-B", **common}],
                trades=[],
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertIn("2张委托", result.unresolved[0]["reason"])


if __name__ == "__main__":
    unittest.main()
