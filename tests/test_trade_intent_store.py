from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from src.trade_intent_store import (
    STATUS_FILLED,
    STATUS_PARTIALLY_FILLED,
    STATUS_PLANNED,
    STATUS_PREPARED,
    STATUS_SUBMITTED,
    STATUS_SUBMITTING,
    STATUS_VALIDATED,
    TradeIntentSpec,
    TradeIntentStore,
    build_idempotency_key,
)


def intent_spec(*, quantity: int = 1000) -> TradeIntentSpec:
    key = build_idempotency_key(
        account_fingerprint="acct-hash",
        business_date="20260817",
        strategy_leg="A",
        side="BUY",
        ts_code="000001.SZ",
        purpose="OPEN",
        source_key="combined-plan-row-1",
    )
    return TradeIntentSpec(
        idempotency_key=key,
        account_fingerprint="acct-hash",
        strategy_leg="A",
        side="BUY",
        ts_code="000001.SZ",
        business_date="20260817",
        signal_date="20260814",
        planned_exit_date="20260819",
        purpose="OPEN",
        source_key="combined-plan-row-1",
        target_qty=quantity,
        target_amount=10_000,
        price_type="FIXED_PRICE",
        limit_price=10.0,
        metadata={"name": "平安银行"},
    )


class TradeIntentStoreTests(unittest.TestCase):
    def test_create_is_idempotent_and_conflicting_payload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TradeIntentStore(Path(temporary) / "execution_events.sqlite3")
            first = store.create_intent(intent_spec())
            duplicate = store.create_intent(intent_spec())
            self.assertTrue(first["created"])
            self.assertFalse(duplicate["created"])
            self.assertEqual(first["intent_id"], duplicate["intent_id"])
            self.assertEqual(first["status"], STATUS_PLANNED)
            with self.assertRaisesRegex(ValueError, "不同交易意图"):
                store.create_intent(intent_spec(quantity=2000))

    def test_state_machine_cas_and_fill_are_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TradeIntentStore(Path(temporary) / "execution_events.sqlite3")
            row = store.create_intent(intent_spec())
            intent_id = str(row["intent_id"])
            row = store.transition_intent(
                intent_id, STATUS_VALIDATED, expected_statuses={STATUS_PLANNED}
            )
            row = store.transition_intent(
                intent_id,
                STATUS_PREPARED,
                expected_statuses={STATUS_VALIDATED},
                expected_version=int(row["version"]),
            )
            store.transition_intent(
                intent_id, STATUS_SUBMITTING, expected_statuses={STATUS_PREPARED}
            )
            store.transition_intent(
                intent_id,
                STATUS_SUBMITTED,
                expected_statuses={STATUS_SUBMITTING},
                broker_order_id="QMT-1",
            )
            partial = store.transition_intent(
                intent_id,
                STATUS_PARTIALLY_FILLED,
                expected_statuses={STATUS_SUBMITTED},
                filled_qty=400,
                filled_amount=4000,
            )
            same_status = store.transition_intent(
                intent_id,
                STATUS_PARTIALLY_FILLED,
                expected_statuses={STATUS_PARTIALLY_FILLED},
                filled_qty=300,
                filled_amount=3000,
            )
            self.assertEqual(same_status["filled_qty"], 400)
            self.assertEqual(same_status["filled_amount"], 4000)
            filled = store.transition_intent(
                intent_id,
                STATUS_FILLED,
                expected_statuses={STATUS_PARTIALLY_FILLED},
                filled_qty=1000,
                filled_amount=10100,
            )
            self.assertEqual(filled["avg_fill_price"], 10.1)
            with self.assertRaisesRegex(RuntimeError, "非法交易意图迁移"):
                store.transition_intent(intent_id, STATUS_SUBMITTED)
            with self.assertRaisesRegex(RuntimeError, "状态CAS失败"):
                store.transition_intent(
                    intent_id, STATUS_FILLED, expected_statuses={STATUS_SUBMITTED}
                )
            history = store.transition_history(intent_id)
            self.assertEqual(history[0]["to_status"], STATUS_PLANNED)
            self.assertEqual(history[-1]["to_status"], STATUS_FILLED)
            self.assertEqual(store.audit()["status"], "PASS")

    def test_concurrent_create_keeps_one_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TradeIntentStore(Path(temporary) / "execution_events.sqlite3")
            rows: list[dict] = []
            errors: list[BaseException] = []

            def worker() -> None:
                try:
                    rows.append(store.create_intent(intent_spec()))
                except BaseException as exc:  # pragma: no cover - diagnostic capture
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len({row["intent_id"] for row in rows}), 1)
            self.assertEqual(sum(bool(row["created"]) for row in rows), 1)
            self.assertEqual(store.audit()["intent_count"], 1)

    def test_recovery_run_and_recoverable_query_share_same_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "execution_events.sqlite3"
            store = TradeIntentStore(path)
            row = store.create_intent(intent_spec())
            intent_id = str(row["intent_id"])
            store.transition_intent(intent_id, STATUS_VALIDATED)
            store.transition_intent(intent_id, STATUS_PREPARED)
            recoverable = store.list_recoverable_intents(
                account_fingerprint="acct-hash", business_date_on_or_before="20260817"
            )
            self.assertEqual([item["intent_id"] for item in recoverable], [intent_id])
            recovery_id = store.start_recovery_run("boot-1")
            store.finish_recovery_run(
                recovery_id,
                status="PASS",
                broker_snapshot_sha256="abc",
                recovered_count=1,
                unresolved_count=0,
                details={"intent_ids": [intent_id]},
            )
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
