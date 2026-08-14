from __future__ import annotations

import threading
import time
import tempfile
import unittest
from pathlib import Path

from src.broker_adapter import (
    AccountSnapshot,
    OrderFill,
    OrderRequest,
    OrderResult,
    PositionSnapshot,
)
from src.broker_execution_service import BrokerExecutionService, IntentBrokerExecutionService
from src.trade_intent_store import (
    STATUS_CANCELLED,
    STATUS_FILLED,
    STATUS_RECOVERY_REQUIRED,
    STATUS_SUBMITTED,
    TradeIntentStore,
)


class FakeAdapter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []
        self._active_qmt_path = "C:/QMT"

    def _enter(self, label: str, delay: float = 0.005) -> str:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(label)
        time.sleep(delay)
        with self.lock:
            self.active -= 1
        return label

    def connect(self) -> None:
        self._enter("connect")

    def disconnect(self) -> None:
        self._enter("disconnect")

    def query_account(self) -> AccountSnapshot:
        self._enter("query_account")
        return AccountSnapshot(account_id="masked", total_asset=100000)

    def query_positions(self) -> list[PositionSnapshot]:
        self._enter("query_positions")
        return []

    def query_orders(self) -> list[dict]:
        self._enter("query_orders")
        return []

    def query_trades(self) -> list[dict]:
        self._enter("query_trades")
        return []

    def get_full_tick(self, _codes: list[str]) -> dict:
        self._enter("get_full_tick")
        return {}

    def place_order(self, request: OrderRequest) -> OrderResult:
        self._enter(f"place:{request.remark}")
        return OrderResult(
            ts_code=request.ts_code,
            broker_code=request.broker_code,
            side=request.side,
            quantity=request.quantity,
            accepted=True,
            order_id=f"OID-{request.remark}",
        )

    def cancel_order(self, order_id: str) -> bool:
        self._enter(f"cancel:{order_id}")
        return True

    def get_order_fill(self, order_id: str) -> OrderFill:
        self._enter(f"fill:{order_id}")
        return OrderFill(order_id=order_id)

    def explode(self) -> None:
        self._enter("explode")
        raise RuntimeError("boom")


class BrokerExecutionServiceTests(unittest.TestCase):
    def test_concurrent_callers_never_enter_adapter_concurrently(self) -> None:
        adapter = FakeAdapter()
        service = BrokerExecutionService(default_timeout=2)
        proxy = service.proxy(lambda: adapter)
        barrier = threading.Barrier(13)
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait()
                if index % 3 == 0:
                    proxy.query_orders()
                elif index % 3 == 1:
                    proxy.query_positions()
                else:
                    request = OrderRequest(
                        ts_code="000001.SZ",
                        broker_code="000001.SZ",
                        side="BUY",
                        quantity=100,
                        price_type="FIXED_PRICE",
                        price=10,
                        remark=str(index),
                    )
                    proxy.place_order(request)
            except BaseException as exc:  # pragma: no cover - diagnostic capture
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        service.shutdown()
        self.assertEqual(errors, [])
        self.assertEqual(adapter.max_active, 1)
        self.assertEqual(len(adapter.calls), 12)
        metrics = service.metrics()
        self.assertEqual(metrics["submitted"], 12)
        self.assertEqual(metrics["completed"], 12)
        self.assertEqual(metrics["failed"], 0)


class IntentBrokerExecutionServiceTests(unittest.TestCase):
    def _service(self, path: Path) -> tuple[IntentBrokerExecutionService, FakeAdapter]:
        adapter = FakeAdapter()
        service = IntentBrokerExecutionService(
            intent_store=TradeIntentStore(path),
            account_fingerprint_provider=lambda: "acct-hash",
            business_date_provider=lambda: "20260817",
            default_timeout=2,
        )
        return service, adapter

    @staticmethod
    def _request(source_key: str = "A-open-1") -> OrderRequest:
        return OrderRequest(
            ts_code="000001.SZ",
            broker_code="000001.SZ",
            side="BUY",
            quantity=100,
            price_type="FIXED_PRICE",
            price=10,
            strategy_name="A_SYSTEM_ABC",
            remark="盘前买入-20260817",
            strategy_leg="A",
            business_date="20260817",
            signal_date="20260814",
            planned_exit_date="20260819",
            purpose="OPEN",
            source_key=source_key,
        )

    def test_order_is_persisted_before_qmt_and_duplicate_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "execution_events.sqlite3"
            service, adapter = self._service(path)
            proxy = service.proxy(lambda: adapter)
            first = proxy.place_order(self._request())
            duplicate = proxy.place_order(self._request())
            self.assertTrue(first.accepted)
            self.assertEqual(first.intent_id, duplicate.intent_id)
            self.assertEqual(first.order_id, duplicate.order_id)
            self.assertEqual(adapter.calls, ["place:盘前买入-20260817"])
            row = TradeIntentStore(path).get_intent(first.intent_id)
            self.assertEqual(row["status"], STATUS_SUBMITTED)
            self.assertEqual(row["broker_order_id"], first.order_id)
            service.shutdown()

    def test_unknown_submit_result_blocks_blind_retry(self) -> None:
        class ExplodingAdapter(FakeAdapter):
            def place_order(self, request: OrderRequest) -> OrderResult:
                self._enter(f"place:{request.remark}")
                raise RuntimeError("QMT reply lost")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "execution_events.sqlite3"
            adapter = ExplodingAdapter()
            recovery_reasons: list[str] = []
            service = IntentBrokerExecutionService(
                intent_store=TradeIntentStore(path),
                account_fingerprint_provider=lambda: "acct-hash",
                business_date_provider=lambda: "20260817",
                default_timeout=2,
                recovery_required_callback=lambda reason, _sequence: recovery_reasons.append(reason),
            )
            proxy = service.proxy(lambda: adapter)
            with self.assertRaisesRegex(RuntimeError, "reply lost"):
                proxy.place_order(self._request())
            row = TradeIntentStore(path).list_recoverable_intents()[0]
            self.assertEqual(row["status"], STATUS_RECOVERY_REQUIRED)
            with self.assertRaisesRegex(RuntimeError, "中毒|禁止重发"):
                proxy.place_order(self._request())
            with self.assertRaisesRegex(RuntimeError, "中毒"):
                proxy.query_account()
            self.assertEqual(adapter.calls, ["place:盘前买入-20260817"])
            self.assertEqual(len(recovery_reasons), 1)
            self.assertIn("结果未知", recovery_reasons[0])
            service.shutdown()

    def test_fill_and_cancel_update_same_authoritative_intent(self) -> None:
        class FilledAdapter(FakeAdapter):
            terminal = False

            def get_order_fill(self, order_id: str) -> OrderFill:
                self._enter(f"fill:{order_id}")
                if self.terminal:
                    return OrderFill(
                        order_id=order_id,
                        status_text="已撤",
                        filled_qty=40,
                        avg_price=10,
                        is_terminal=True,
                        is_partial=True,
                    )
                return OrderFill(
                    order_id=order_id,
                    status_text="部成",
                    filled_qty=40,
                    avg_price=10,
                    is_partial=True,
                )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "execution_events.sqlite3"
            adapter = FilledAdapter()
            service = IntentBrokerExecutionService(
                intent_store=TradeIntentStore(path),
                account_fingerprint_provider=lambda: "acct-hash",
                business_date_provider=lambda: "20260817",
                default_timeout=2,
            )
            proxy = service.proxy(lambda: adapter)
            result = proxy.place_order(self._request())
            proxy.get_order_fill(result.order_id)
            row = TradeIntentStore(path).get_intent(result.intent_id)
            self.assertEqual(row["filled_qty"], 40)
            self.assertTrue(proxy.cancel_order(result.order_id))
            adapter.terminal = True
            proxy.get_order_fill(result.order_id)
            row = TradeIntentStore(path).get_intent(result.intent_id)
            self.assertEqual(row["status"], STATUS_CANCELLED)
            self.assertEqual(row["filled_qty"], 40)
            service.shutdown()

    def test_full_fill_enters_terminal_filled_state(self) -> None:
        class FilledAdapter(FakeAdapter):
            def get_order_fill(self, order_id: str) -> OrderFill:
                return OrderFill(
                    order_id=order_id,
                    status_text="全成",
                    filled_qty=100,
                    avg_price=10.1,
                    is_terminal=True,
                    is_filled=True,
                )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "execution_events.sqlite3"
            adapter = FilledAdapter()
            service = IntentBrokerExecutionService(
                intent_store=TradeIntentStore(path),
                account_fingerprint_provider=lambda: "acct-hash",
                business_date_provider=lambda: "20260817",
            )
            proxy = service.proxy(lambda: adapter)
            result = proxy.place_order(self._request())
            proxy.get_order_fill(result.order_id)
            row = TradeIntentStore(path).get_intent(result.intent_id)
            self.assertEqual(row["status"], STATUS_FILLED)
            self.assertEqual(row["filled_amount"], 1010)
            service.shutdown()

    def test_second_non_idempotent_buy_is_rejected_while_first_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "execution_events.sqlite3"
            service, adapter = self._service(path)
            proxy = service.proxy(lambda: adapter)
            first = proxy.place_order(self._request("A-open-1"))
            second = proxy.place_order(self._request("A-open-2"))
            self.assertTrue(first.accepted)
            self.assertFalse(second.accepted)
            self.assertIn("ACTIVE_BUY_INTENT_EXISTS", second.message)
            self.assertEqual(adapter.calls, ["place:盘前买入-20260817"])
            service.shutdown()

    def test_full_status_without_qty_uses_authoritative_target_qty(self) -> None:
        class FilledWithoutQtyAdapter(FakeAdapter):
            def get_order_fill(self, order_id: str) -> OrderFill:
                return OrderFill(
                    order_id=order_id,
                    status_text="全成",
                    filled_qty=0,
                    avg_price=10.1,
                    is_terminal=True,
                    is_filled=True,
                )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "execution_events.sqlite3"
            adapter = FilledWithoutQtyAdapter()
            service = IntentBrokerExecutionService(
                intent_store=TradeIntentStore(path),
                account_fingerprint_provider=lambda: "acct-hash",
                business_date_provider=lambda: "20260817",
            )
            proxy = service.proxy(lambda: adapter)
            result = proxy.place_order(self._request())
            proxy.get_order_fill(result.order_id)
            row = TradeIntentStore(path).get_intent(result.intent_id)
            self.assertEqual(row["status"], STATUS_FILLED)
            self.assertEqual(row["filled_qty"], 100)
            self.assertEqual(row["filled_amount"], 1010)
            service.shutdown()


class BrokerExecutionServiceResilienceTests(unittest.TestCase):

    def test_exception_does_not_kill_worker(self) -> None:
        adapter = FakeAdapter()
        service = BrokerExecutionService(default_timeout=2)
        with self.assertRaisesRegex(RuntimeError, "boom"):
            service.call(lambda: adapter, "explode")
        self.assertEqual(service.proxy(lambda: adapter).query_orders(), [])
        metrics = service.metrics()
        self.assertTrue(metrics["worker_alive"])
        self.assertEqual(metrics["failed"], 1)
        self.assertEqual(metrics["completed"], 1)
        service.shutdown()

    def test_proxy_covers_order_cancel_fill_and_serialized_attribute(self) -> None:
        adapter = FakeAdapter()
        service = BrokerExecutionService(default_timeout=2)
        proxy = service.proxy(lambda: adapter)
        request = OrderRequest(
            ts_code="000001.SZ",
            broker_code="000001.SZ",
            side="BUY",
            quantity=100,
            price_type="FIXED_PRICE",
            price=10,
            remark="one",
        )
        result = proxy.place_order(request)
        self.assertTrue(result.accepted)
        self.assertTrue(proxy.cancel_order(result.order_id))
        self.assertEqual(proxy.get_order_fill(result.order_id).order_id, result.order_id)
        self.assertEqual(proxy.get_serialized_attribute("_active_qmt_path"), "C:/QMT")
        self.assertEqual(
            adapter.calls,
            ["place:one", "cancel:OID-one", "fill:OID-one"],
        )
        service.shutdown()

    def test_timeout_poisoning_rejects_followup_and_calls_restart_hook_once(self) -> None:
        adapter = FakeAdapter()
        callbacks: list[tuple[str, int]] = []
        service = BrokerExecutionService(
            default_timeout=0.01,
            timeout_callback=lambda reason, sequence: callbacks.append((reason, sequence)),
        )

        def slow() -> str:
            time.sleep(0.05)
            return "done"

        with self.assertRaisesRegex(TimeoutError, "结果未知"):
            service.call_function(slow, operation="slow", timeout=0.005)
        # 即使底层调用稍后自行返回，该进程中的QMT通道仍然不可信；
        # 必须由daemon进程级重启，不能在原线程上继续开平仓。
        time.sleep(0.06)
        with self.assertRaisesRegex(RuntimeError, "必须重启进程"):
            service.call_function(lambda: "next", timeout=1)
        self.assertEqual(len(callbacks), 1)
        self.assertIn("operation=slow", callbacks[0][0])
        metrics = service.metrics()
        self.assertTrue(metrics["poisoned"])
        self.assertEqual(metrics["poison_sequence"], callbacks[0][1])
        service.shutdown()

    def test_commands_already_queued_behind_timeout_never_reach_adapter(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        service = BrokerExecutionService(default_timeout=0.02)
        errors: list[BaseException] = []

        def stuck() -> str:
            entered.set()
            release.wait(1)
            return "late"

        def first() -> None:
            try:
                service.call_function(stuck, operation="stuck", timeout=0.02)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(TimeoutError, "执行通道已封锁"):
            service.call_function(lambda: "must-not-run", operation="queued", timeout=0.03)
        release.set()
        thread.join(1)
        self.assertTrue(any(isinstance(exc, TimeoutError) for exc in errors))
        service.shutdown()


if __name__ == "__main__":
    unittest.main()
