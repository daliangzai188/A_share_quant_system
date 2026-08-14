"""唯一串行券商执行通道。

所有QMT查询、下单、撤单和成交确认都通过同一个工作线程FIFO执行。调用方可以保留
同步函数写法，但不再直接持有或并发调用原始QMT adapter。
"""
from __future__ import annotations

import itertools
import queue
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from src.broker_adapter import (
    AccountSnapshot,
    BrokerAdapter,
    OrderFill,
    OrderRequest,
    OrderResult,
    PositionSnapshot,
    QuoteSnapshot,
)


AdapterProvider = Callable[[], BrokerAdapter]


@dataclass
class BrokerCommand:
    sequence: int
    operation: str
    adapter_provider: AdapterProvider | None
    function: Callable[..., Any] | None
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: Future[Any] = field(default_factory=Future)
    enqueued_at: float = field(default_factory=time.monotonic)


class BrokerExecutionService:
    """进程内唯一FIFO执行器；原始adapter只能在工作线程中被调用。"""

    def __init__(
        self,
        *,
        name: str = "qmt-execution",
        default_timeout: float = 120.0,
        max_queue_size: int = 0,
    ) -> None:
        self.name = str(name or "qmt-execution")
        self.default_timeout = max(float(default_timeout), 0.1)
        self._queue: queue.Queue[BrokerCommand | object] = queue.Queue(
            maxsize=max(int(max_queue_size or 0), 0)
        )
        self._sequence = itertools.count(1)
        self._lifecycle_lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._stop_token = object()
        self._stopping = False
        self._worker_ident: int | None = None
        self._metrics_lock = threading.Lock()
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._active_operation = ""
        self._last_sequence = 0

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            if self._stopping:
                raise RuntimeError("券商执行服务已停止，不能重新接收命令")
            self._worker = threading.Thread(
                target=self._run,
                daemon=True,
                name=self.name,
            )
            self._worker.start()

    def _run(self) -> None:
        self._worker_ident = threading.get_ident()
        while True:
            command = self._queue.get()
            try:
                if command is self._stop_token:
                    return
                if not isinstance(command, BrokerCommand):
                    continue
                with self._metrics_lock:
                    self._active_operation = command.operation
                    self._last_sequence = command.sequence
                try:
                    if command.function is not None:
                        result = command.function(*command.args, **command.kwargs)
                    else:
                        if command.adapter_provider is None:
                            raise RuntimeError("券商命令缺少adapter_provider")
                        adapter = command.adapter_provider()
                        method = getattr(adapter, command.operation)
                        result = method(*command.args, **command.kwargs)
                except BaseException as exc:  # Future负责把原异常送回调用线程
                    with self._metrics_lock:
                        self._failed += 1
                    command.future.set_exception(exc)
                else:
                    with self._metrics_lock:
                        self._completed += 1
                    command.future.set_result(result)
                finally:
                    with self._metrics_lock:
                        self._active_operation = ""
            finally:
                self._queue.task_done()

    def _submit(
        self,
        *,
        operation: str,
        adapter_provider: AdapterProvider | None,
        function: Callable[..., Any] | None,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
        timeout: float | None,
    ) -> Any:
        # 工作线程内的重入调用直接执行，避免adapter内部辅助函数再次走代理时死锁。
        if self._worker_ident is not None and threading.get_ident() == self._worker_ident:
            if function is not None:
                return function(*args, **dict(kwargs))
            if adapter_provider is None:
                raise RuntimeError("券商命令缺少adapter_provider")
            return getattr(adapter_provider(), operation)(*args, **dict(kwargs))

        self.start()
        with self._lifecycle_lock:
            if self._stopping:
                raise RuntimeError("券商执行服务正在停止，拒绝新命令")
            command = BrokerCommand(
                sequence=next(self._sequence),
                operation=str(operation),
                adapter_provider=adapter_provider,
                function=function,
                args=args,
                kwargs=dict(kwargs),
            )
            self._queue.put(command)
            with self._metrics_lock:
                self._submitted += 1

        wait_timeout = self.default_timeout if timeout is None else max(float(timeout), 0.01)
        try:
            return command.future.result(timeout=wait_timeout)
        except FutureTimeoutError as exc:
            # 命令可能已经在QMT内部执行，绝不能把超时解释为“肯定未提交”。调用方必须
            # 让对应交易意图进入RECOVERY_REQUIRED，再查券商真实委托。
            raise TimeoutError(
                f"券商执行命令超时:sequence={command.sequence} operation={command.operation};"
                "结果未知，禁止盲目重发"
            ) from exc

    def call(
        self,
        adapter_provider: AdapterProvider,
        operation: str,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._submit(
            operation=operation,
            adapter_provider=adapter_provider,
            function=None,
            args=args,
            kwargs=kwargs,
            timeout=timeout,
        )

    def call_function(
        self,
        function: Callable[..., Any],
        *args: Any,
        operation: str = "callable",
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._submit(
            operation=operation,
            adapter_provider=None,
            function=function,
            args=args,
            kwargs=kwargs,
            timeout=timeout,
        )

    def proxy(self, adapter_provider: AdapterProvider) -> "SerializedBrokerProxy":
        return SerializedBrokerProxy(self, adapter_provider)

    def metrics(self) -> dict[str, Any]:
        with self._metrics_lock:
            return {
                "name": self.name,
                "worker_alive": bool(self._worker and self._worker.is_alive()),
                "submitted": self._submitted,
                "completed": self._completed,
                "failed": self._failed,
                "queue_depth": self._queue.qsize(),
                "active_operation": self._active_operation,
                "last_sequence": self._last_sequence,
            }

    def shutdown(self, *, wait: bool = True, timeout: float = 10.0) -> None:
        with self._lifecycle_lock:
            if self._stopping:
                worker = self._worker
            else:
                self._stopping = True
                worker = self._worker
                if worker is not None and worker.is_alive():
                    self._queue.put(self._stop_token)
        if wait and worker is not None and worker.is_alive():
            worker.join(timeout=max(float(timeout), 0.0))


class SerializedBrokerProxy(BrokerAdapter):
    """BrokerAdapter兼容代理；每个公开动作都进入同一执行服务。"""

    def __init__(self, service: BrokerExecutionService, adapter_provider: AdapterProvider):
        self._service = service
        self._adapter_provider = adapter_provider

    @property
    def execution_service(self) -> BrokerExecutionService:
        return self._service

    def connect(self) -> None:
        self._service.call(self._adapter_provider, "connect")

    def disconnect(self) -> None:
        self._service.call(self._adapter_provider, "disconnect")

    def query_account(self) -> AccountSnapshot:
        return self._service.call(self._adapter_provider, "query_account")

    def query_positions(self) -> list[PositionSnapshot]:
        return self._service.call(self._adapter_provider, "query_positions")

    def query_orders(self) -> list[dict[str, Any]]:
        return self._service.call(self._adapter_provider, "query_orders")

    def query_trades(self) -> list[dict[str, Any]]:
        return self._service.call(self._adapter_provider, "query_trades")

    def get_full_tick(self, ts_codes: list[str]) -> dict[str, QuoteSnapshot]:
        return self._service.call(self._adapter_provider, "get_full_tick", ts_codes)

    def place_order(self, request: OrderRequest) -> OrderResult:
        return self._service.call(self._adapter_provider, "place_order", request)

    def cancel_order(self, order_id: str) -> bool:
        return self._service.call(self._adapter_provider, "cancel_order", order_id)

    def get_order_fill(self, order_id: str) -> OrderFill:
        return self._service.call(self._adapter_provider, "get_order_fill", order_id)

    def get_serialized_attribute(self, name: str, default: Any = None) -> Any:
        def read_attribute() -> Any:
            return getattr(self._adapter_provider(), name, default)

        return self._service.call_function(
            read_attribute,
            operation=f"getattr:{name}",
        )
