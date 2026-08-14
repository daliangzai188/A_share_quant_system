from __future__ import annotations

import threading
import time
import unittest

from src.broker_adapter import (
    AccountSnapshot,
    OrderFill,
    OrderRequest,
    OrderResult,
    PositionSnapshot,
)
from src.broker_execution_service import BrokerExecutionService


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

    def test_timeout_is_reported_as_unknown_not_as_rejection(self) -> None:
        adapter = FakeAdapter()
        service = BrokerExecutionService(default_timeout=0.01)

        def slow() -> str:
            time.sleep(0.05)
            return "done"

        with self.assertRaisesRegex(TimeoutError, "结果未知"):
            service.call_function(slow, operation="slow", timeout=0.005)
        # 等工作线程完成未知结果后仍可处理下一条命令。
        time.sleep(0.06)
        self.assertEqual(service.call_function(lambda: "next", timeout=1), "next")
        service.shutdown()


if __name__ == "__main__":
    unittest.main()
