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
from dataclasses import dataclass, field, replace
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
from src.trade_intent_store import (
    RECOVERABLE_STATUSES,
    STATUS_CANCELLED,
    STATUS_CANCEL_REQUESTED,
    STATUS_FAILED,
    STATUS_FILLED,
    STATUS_PARTIALLY_FILLED,
    STATUS_PLANNED,
    STATUS_PREPARED,
    STATUS_RECOVERY_REQUIRED,
    STATUS_REJECTED,
    STATUS_SUBMITTED,
    STATUS_SUBMITTING,
    STATUS_VALIDATED,
    TERMINAL_STATUSES,
    TradeIntentSpec,
    TradeIntentStore,
    build_idempotency_key,
)
from src.strategy_identity import normalize_strategy_leg


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
        timeout_callback: Callable[[str, int], None] | None = None,
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
        self._poisoned = False
        self._poison_reason = ""
        self._poison_sequence = 0
        self._timeout_callback = timeout_callback
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
                with self._lifecycle_lock:
                    poisoned_after_earlier_command = (
                        self._poisoned and command.sequence > self._poison_sequence
                    )
                    poison_reason = self._poison_reason
                if poisoned_after_earlier_command:
                    command.future.set_exception(
                        RuntimeError(
                            "券商执行服务已因前一条超时命令中毒，"
                            f"拒绝继续访问QMT:{poison_reason}"
                        )
                    )
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
            if self._poisoned:
                raise RuntimeError(
                    "券商执行服务已因未知结果超时中毒，"
                    f"必须重启进程后恢复:{self._poison_reason}"
                )
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
            reason = (
                f"sequence={command.sequence} operation={command.operation} "
                f"timeout={wait_timeout:.1f}s"
            )
            callback: Callable[[str, int], None] | None = None
            with self._lifecycle_lock:
                if not self._poisoned:
                    self._poisoned = True
                    self._poison_reason = reason
                    self._poison_sequence = command.sequence
                    callback = self._timeout_callback
            if callback is not None:
                try:
                    callback(reason, command.sequence)
                except BaseException:
                    # 超时回调只负责触发进程级恢复，其自身失败
                    # 不能改写原始命令“结果未知”的事实。
                    pass
            raise TimeoutError(
                f"券商执行命令超时:{reason};"
                "结果未知，执行通道已封锁，禁止盲目重发"
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
                "poisoned": self._poisoned,
                "poison_reason": self._poison_reason,
                "poison_sequence": self._poison_sequence,
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


def _infer_strategy_leg(request: OrderRequest) -> str:
    declared = normalize_strategy_leg(request.strategy_leg)
    if declared:
        return declared
    text = f"{request.strategy_name}|{request.remark}".upper()
    for leg in ("E", "D", "A", "C"):
        markers = (f"STRATEGY_{leg}", f"A_SYSTEM_{leg}", f"{leg}策略", f"|{leg}|")
        if any(marker in text for marker in markers):
            return leg
    # ABC公共执行路径必须由调用方声明实际腿；UNKNOWN会被统一执行器拒绝。
    return ""


class IntentBrokerExecutionService(BrokerExecutionService):
    """在唯一串行通道内原子推进交易意图和真实QMT动作。"""

    def __init__(
        self,
        *,
        intent_store: TradeIntentStore,
        account_fingerprint_provider: Callable[[], str],
        business_date_provider: Callable[[], str],
        name: str = "qmt-intent-execution",
        default_timeout: float = 120.0,
        timeout_callback: Callable[[str, int], None] | None = None,
        recovery_required_callback: Callable[[str, int], None] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            default_timeout=default_timeout,
            timeout_callback=timeout_callback,
        )
        self._recovery_required_callback = recovery_required_callback
        self.intent_store = intent_store
        self.account_fingerprint_provider = account_fingerprint_provider
        self.business_date_provider = business_date_provider

    def proxy(self, adapter_provider: AdapterProvider) -> "IntentSerializedBrokerProxy":
        return IntentSerializedBrokerProxy(self, adapter_provider)

    def _notify_recovery_required(self, reason: str) -> None:
        # 下单/撤单抛异常时，券商侧可能已经受理。此时和超时完全一样，当前
        # QMT会话的后续结果都不再可信：先毒化唯一执行通道，阻止进程退出前
        # 队列中的下一笔命令继续执行，再通知宿主做进程级事实恢复。
        with self._lifecycle_lock:
            if not self._poisoned:
                self._poisoned = True
                self._poison_reason = str(reason)
                self._poison_sequence = 0
        callback = self._recovery_required_callback
        if callback is None:
            return
        try:
            callback(str(reason), 0)
        except BaseException:
            # 权威意图已经标成RECOVERY_REQUIRED；回调只负责让宿主进程
            # 立即重启恢复，回调失败不能掩盖原始券商未知结果异常。
            pass

    def _intent_spec(self, request: OrderRequest) -> TradeIntentSpec:
        account_fingerprint = str(self.account_fingerprint_provider() or "").strip()
        business_date = str(request.business_date or self.business_date_provider() or "").strip()
        strategy_leg = _infer_strategy_leg(request)
        purpose = str(request.purpose or ("OPEN" if request.side.upper() == "BUY" else "CLOSE")).upper()
        source_key = str(request.source_key or "").strip()
        if not strategy_leg:
            raise RuntimeError(
                f"统一交易意图缺少strategy_leg:{request.ts_code} {request.remark}"
            )
        if not source_key:
            raise RuntimeError(
                f"统一交易意图缺少source_key:{request.ts_code} {request.remark}"
            )
        key = build_idempotency_key(
            account_fingerprint=account_fingerprint,
            business_date=business_date,
            strategy_leg=strategy_leg,
            side=request.side,
            ts_code=request.ts_code,
            purpose=purpose,
            source_key=source_key,
        )
        return TradeIntentSpec(
            idempotency_key=key,
            account_fingerprint=account_fingerprint,
            strategy_leg=strategy_leg,
            side=request.side,
            ts_code=request.ts_code,
            business_date=business_date,
            signal_date=request.signal_date,
            planned_exit_date=request.planned_exit_date,
            purpose=purpose,
            source_key=source_key,
            target_qty=request.quantity,
            target_amount=max(float(request.quantity or 0) * float(request.price or 0), 0.0),
            price_type=request.price_type,
            limit_price=request.price,
            metadata={
                "broker_code": request.broker_code,
                "strategy_name": request.strategy_name,
                "remark": request.remark,
                **dict(request.metadata or {}),
            },
        )

    def submit_order(
        self,
        adapter_provider: AdapterProvider,
        request: OrderRequest,
        *,
        timeout: float | None = None,
    ) -> OrderResult:
        spec = self._intent_spec(request)
        row = self.intent_store.create_intent(spec)
        intent_id = str(row["intent_id"])
        status = str(row["status"])
        if status in {STATUS_SUBMITTED, STATUS_PARTIALLY_FILLED, STATUS_CANCEL_REQUESTED}:
            order_id = str(row.get("broker_order_id", "") or "")
            if not order_id:
                raise RuntimeError(f"活跃交易意图缺少券商单号:{intent_id}")
            return OrderResult(
                ts_code=request.ts_code,
                broker_code=request.broker_code,
                side=request.side,
                quantity=request.quantity,
                accepted=True,
                order_id=order_id,
                message="ORDER_ALREADY_SUBMITTED_IDEMPOTENT",
                intent_id=intent_id,
            )
        if status == STATUS_FILLED:
            return OrderResult(
                ts_code=request.ts_code,
                broker_code=request.broker_code,
                side=request.side,
                quantity=request.quantity,
                accepted=True,
                order_id=str(row.get("broker_order_id", "") or ""),
                message="ORDER_ALREADY_FILLED_IDEMPOTENT",
                intent_id=intent_id,
            )
        if status in {STATUS_SUBMITTING, STATUS_RECOVERY_REQUIRED}:
            raise RuntimeError(
                f"交易意图结果未知，必须先按券商真实状态恢复，禁止重发:{intent_id}"
            )
        if status in TERMINAL_STATUSES:
            return OrderResult(
                ts_code=request.ts_code,
                broker_code=request.broker_code,
                side=request.side,
                quantity=request.quantity,
                accepted=False,
                order_id=str(row.get("broker_order_id", "") or ""),
                message=f"INTENT_TERMINAL_{status}",
                intent_id=intent_id,
            )

        if status == STATUS_PLANNED:
            row = self.intent_store.transition_intent(
                intent_id, STATUS_VALIDATED, expected_statuses={STATUS_PLANNED}, reason="统一执行校验通过"
            )
            status = str(row["status"])
        if status == STATUS_VALIDATED:
            row = self.intent_store.transition_intent(
                intent_id, STATUS_PREPARED, expected_statuses={STATUS_VALIDATED}, reason="券商提交前事务落盘"
            )
            status = str(row["status"])
        if status != STATUS_PREPARED:
            raise RuntimeError(f"交易意图不在可提交状态:{intent_id} status={status}")

        def execute() -> OrderResult:
            if request.side.upper() == "BUY":
                conflicts = self.intent_store.list_active_intents(
                    account_fingerprint=spec.account_fingerprint,
                    side="BUY",
                    exclude_intent_id=intent_id,
                )
                if conflicts:
                    conflict = conflicts[0]
                    self.intent_store.transition_intent(
                        intent_id,
                        STATUS_REJECTED,
                        expected_statuses={STATUS_PREPARED},
                        reason=(
                            "已有未终态买入意图占用统一资金通道:"
                            f"{conflict.get('strategy_leg', '')} "
                            f"{conflict.get('ts_code', '')} "
                            f"{conflict.get('status', '')}"
                        ),
                        error_code="ACTIVE_BUY_INTENT_EXISTS",
                    )
                    return OrderResult(
                        ts_code=request.ts_code,
                        broker_code=request.broker_code,
                        side=request.side,
                        quantity=request.quantity,
                        accepted=False,
                        message=(
                            "ACTIVE_BUY_INTENT_EXISTS:"
                            f"{conflict.get('strategy_leg', '')}:"
                            f"{conflict.get('ts_code', '')}:"
                            f"{conflict.get('status', '')}"
                        ),
                        intent_id=intent_id,
                    )
            self.intent_store.transition_intent(
                intent_id,
                STATUS_SUBMITTING,
                expected_statuses={STATUS_PREPARED},
                reason="进入唯一QMT执行通道",
            )
            try:
                result = adapter_provider().place_order(request)
            except BaseException as exc:
                try:
                    self.intent_store.transition_intent(
                        intent_id,
                        STATUS_RECOVERY_REQUIRED,
                        expected_statuses={STATUS_SUBMITTING},
                        reason="QMT提交抛异常，结果未知",
                        error_code=type(exc).__name__,
                        error_message=str(exc),
                    )
                finally:
                    self._notify_recovery_required(
                        f"券商提交异常且结果未知:intent={intent_id} {type(exc).__name__}:{exc}"
                    )
                raise
            if bool(result.accepted):
                order_id = str(result.order_id or "").strip()
                if not order_id:
                    try:
                        self.intent_store.transition_intent(
                            intent_id,
                            STATUS_RECOVERY_REQUIRED,
                            expected_statuses={STATUS_SUBMITTING},
                            reason="QMT受理但未返回券商单号",
                            error_code="MISSING_ORDER_ID",
                        )
                    finally:
                        self._notify_recovery_required(
                            f"券商受理但未返回委托号:intent={intent_id}"
                        )
                    raise RuntimeError("QMT受理委托但未返回order_id，禁止盲目重发")
                try:
                    self.intent_store.transition_intent(
                        intent_id,
                        STATUS_SUBMITTED,
                        expected_statuses={STATUS_SUBMITTING},
                        reason="QMT委托已受理",
                        broker_order_id=order_id,
                    )
                except BaseException as exc:
                    self._notify_recovery_required(
                        f"券商已受理但事务账本绑定失败:intent={intent_id} order={order_id} "
                        f"{type(exc).__name__}:{exc}"
                    )
                    raise
            else:
                self.intent_store.transition_intent(
                    intent_id,
                    STATUS_REJECTED,
                    expected_statuses={STATUS_SUBMITTING},
                    reason="QMT明确拒单",
                    error_code="QMT_REJECTED",
                    error_message=str(result.message or ""),
                )
            return replace(result, intent_id=intent_id)

        try:
            return self.call_function(
                execute,
                operation=f"submit_intent:{intent_id}",
                timeout=timeout,
            )
        except TimeoutError:
            # 工作线程仍可能完成并自行写SUBMITTED；若进程终止则保留SUBMITTING，
            # 两种情况都由启动恢复统一查询券商，调用方不得重发。
            raise

    def request_cancel(
        self,
        adapter_provider: AdapterProvider,
        order_id: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        account_fingerprint = str(self.account_fingerprint_provider() or "").strip()
        business_date = str(self.business_date_provider() or "").strip()
        intent = self.intent_store.get_by_broker_order_id(
            order_id,
            account_fingerprint=account_fingerprint,
            business_date=business_date,
        )
        if intent is not None and str(intent["status"]) in {
            STATUS_SUBMITTED,
            STATUS_PARTIALLY_FILLED,
        }:
            self.intent_store.transition_intent(
                str(intent["intent_id"]),
                STATUS_CANCEL_REQUESTED,
                expected_statuses={STATUS_SUBMITTED, STATUS_PARTIALLY_FILLED},
                reason="撤单请求进入唯一QMT执行通道",
            )
        try:
            return bool(self.call(adapter_provider, "cancel_order", order_id, timeout=timeout))
        except BaseException as exc:
            try:
                current = self.intent_store.get_by_broker_order_id(
                    order_id,
                    account_fingerprint=account_fingerprint,
                    business_date=business_date,
                )
                if current is not None and str(current["status"]) not in TERMINAL_STATUSES:
                    self.intent_store.transition_intent(
                        str(current["intent_id"]),
                        STATUS_RECOVERY_REQUIRED,
                        expected_statuses={str(current["status"])},
                        reason="撤单调用异常，订单终态未知",
                        error_code=type(exc).__name__,
                        error_message=str(exc),
                    )
            finally:
                self._notify_recovery_required(
                    f"撤单调用异常且终态未知:order={order_id} {type(exc).__name__}:{exc}"
                )
            raise

    def query_order_fill(
        self,
        adapter_provider: AdapterProvider,
        order_id: str,
        *,
        timeout: float | None = None,
    ) -> OrderFill:
        fill: OrderFill = self.call(
            adapter_provider, "get_order_fill", order_id, timeout=timeout
        )
        intent = self.intent_store.get_by_broker_order_id(
            order_id,
            account_fingerprint=str(self.account_fingerprint_provider() or "").strip(),
            business_date=str(self.business_date_provider() or "").strip(),
        )
        if intent is None:
            return fill
        status = str(intent["status"])
        if status in TERMINAL_STATUSES:
            return fill
        qty = max(int(fill.filled_qty or 0), 0)
        if fill.is_filled and qty <= 0:
            # QMT偶尔先回“全成”终态，成交数量字段晚一拍。
            # 终态已明确时用原始委托量恢复，避免产生FILLED+0股。
            qty = int(intent["target_qty"])
        amount = qty * max(float(fill.avg_price or 0.0), 0.0)
        if fill.is_filled or qty >= int(intent["target_qty"]):
            target = STATUS_FILLED
        elif fill.is_terminal:
            target = STATUS_CANCELLED if status != STATUS_SUBMITTING else STATUS_REJECTED
        elif qty > 0:
            target = STATUS_PARTIALLY_FILLED
        else:
            return fill
        allowed_current = set(RECOVERABLE_STATUSES) | {STATUS_SUBMITTED, STATUS_PARTIALLY_FILLED}
        if status in allowed_current:
            self.intent_store.transition_intent(
                str(intent["intent_id"]),
                target,
                expected_statuses={status},
                reason="QMT成交/委托终态确认",
                filled_qty=qty,
                filled_amount=amount,
            )
        return fill


class IntentSerializedBrokerProxy(SerializedBrokerProxy):
    def __init__(
        self,
        service: IntentBrokerExecutionService,
        adapter_provider: AdapterProvider,
    ) -> None:
        super().__init__(service, adapter_provider)
        self._intent_service = service

    def place_order(self, request: OrderRequest) -> OrderResult:
        return self._intent_service.submit_order(self._adapter_provider, request)

    def cancel_order(self, order_id: str) -> bool:
        return self._intent_service.request_cancel(self._adapter_provider, order_id)

    def get_order_fill(self, order_id: str) -> OrderFill:
        return self._intent_service.query_order_fill(self._adapter_provider, order_id)
