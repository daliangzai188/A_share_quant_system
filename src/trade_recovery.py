"""券商真实状态驱动的统一交易恢复。

恢复器只使用QMT当前返回的仓位、委托和成交，不依赖某个定时任务
是否曾经执行。无法唯一对应的意图保持 ``RECOVERY_REQUIRED``，上层必须
fail-closed，不允许猜测或重发。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from src.trade_intent_store import (
    STATUS_CANCELLED,
    STATUS_CANCEL_REQUESTED,
    STATUS_FILLED,
    STATUS_PARTIALLY_FILLED,
    STATUS_PREPARED,
    STATUS_RECOVERY_REQUIRED,
    STATUS_REJECTED,
    STATUS_SUBMITTED,
    STATUS_SUBMITTING,
    TradeIntentStore,
)


_ORDER_ID_FIELDS = ["order_id", "m_nOrderID", "order_sysid", "m_strOrderSysID"]
_CODE_FIELDS = ["ts_code", "stock_code", "m_strInstrumentID", "instrument_id"]
_SIDE_FIELDS = ["side", "order_type", "m_nOrderType", "entrust_bs", "direction"]
_ORDER_QTY_FIELDS = ["quantity", "order_volume", "m_nOrderVolume", "volume", "entrust_amount"]
_FILLED_QTY_FIELDS = ["filled_qty", "traded_volume", "m_nTradedVolume", "deal_volume"]
_PRICE_FIELDS = ["avg_price", "traded_price", "m_dTradedPrice", "deal_price", "price"]
_STATUS_FIELDS = ["status_code", "order_status", "m_nOrderStatus", "status"]
_REMARK_FIELDS = ["remark", "order_remark", "m_strOrderRemark", "strategy_remark"]


def _object_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: getattr(value, name)
            for name in value.__dataclass_fields__
        }
    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            item = getattr(value, name)
        except Exception:
            continue
        if not callable(item) and isinstance(
            item, (str, int, float, bool, list, tuple, dict, type(None))
        ):
            result[name] = item
    return result


def _first_present(data: Mapping[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        if name not in data:
            continue
        value = data[name]
        if value is None or (isinstance(value, str) and value == ""):
            continue
        return value
    return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        return text
    if text.startswith(("6", "9")):
        return f"{text}.SH"
    if text.startswith(("0", "2", "3")):
        return f"{text}.SZ"
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return text


def _normalize_side(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"23", "BUY", "B", "买入", "STOCK_BUY"}:
        return "BUY"
    if text in {"24", "SELL", "S", "卖出", "STOCK_SELL"}:
        return "SELL"
    return text


def normalize_broker_positions(values: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw_value in values or []:
        raw = _object_to_dict(raw_value)
        ts_code = _normalize_code(_first_present(raw, _CODE_FIELDS, ""))
        if not ts_code:
            continue
        result.append(
            {
                "snapshot_key": ts_code,
                "ts_code": ts_code,
                "volume": max(_to_int(_first_present(raw, ["volume", "m_nVolume", "total_volume"], 0)), 0),
                "can_use_volume": max(
                    _to_int(_first_present(raw, ["can_use_volume", "m_nCanUseVolume"], 0)), 0
                ),
                "cost_price": max(
                    _to_float(_first_present(raw, ["cost_price", "m_dOpenPrice", "open_price"], 0)), 0.0
                ),
                "market_value": max(
                    _to_float(_first_present(raw, ["market_value", "m_dMarketValue"], 0)), 0.0
                ),
            }
        )
    return sorted(result, key=lambda item: item["snapshot_key"])


def normalize_broker_orders(values: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, raw_value in enumerate(values or []):
        raw = _object_to_dict(raw_value)
        order_id = str(_first_present(raw, _ORDER_ID_FIELDS, "") or "").strip()
        ts_code = _normalize_code(_first_present(raw, _CODE_FIELDS, ""))
        if not order_id and not ts_code:
            continue
        result.append(
            {
                "snapshot_key": order_id or f"NO_ID_{index:08d}",
                "order_id": order_id,
                "ts_code": ts_code,
                "side": _normalize_side(_first_present(raw, _SIDE_FIELDS, "")),
                "order_qty": max(_to_int(_first_present(raw, _ORDER_QTY_FIELDS, 0)), 0),
                "filled_qty": max(_to_int(_first_present(raw, _FILLED_QTY_FIELDS, 0)), 0),
                "avg_price": max(_to_float(_first_present(raw, _PRICE_FIELDS, 0)), 0.0),
                "status_code": _to_int(_first_present(raw, _STATUS_FIELDS, -1), -1),
                "status_text": str(_first_present(raw, ["status_text", "order_status_text"], "") or ""),
                "remark": str(_first_present(raw, _REMARK_FIELDS, "") or "").strip(),
                "strategy_name": str(
                    _first_present(raw, ["strategy_name", "m_strStrategyName"], "") or ""
                ).strip(),
            }
        )
    return sorted(result, key=lambda item: item["snapshot_key"])


def normalize_broker_trades(values: Iterable[Any]) -> list[dict[str, Any]]:
    aggregates: dict[str, dict[str, Any]] = {}
    for index, raw_value in enumerate(values or []):
        raw = _object_to_dict(raw_value)
        order_id = str(_first_present(raw, _ORDER_ID_FIELDS, "") or "").strip()
        if not order_id:
            continue
        quantity = max(
            _to_int(_first_present(raw, ["traded_volume", "m_nVolume", "volume", "traded_qty"], 0)),
            0,
        )
        price = max(_to_float(_first_present(raw, ["traded_price", "m_dPrice", "price"], 0)), 0.0)
        item = aggregates.setdefault(
            order_id,
            {
                "snapshot_key": order_id,
                "order_id": order_id,
                "ts_code": _normalize_code(_first_present(raw, _CODE_FIELDS, "")),
                "side": _normalize_side(_first_present(raw, _SIDE_FIELDS, "")),
                "filled_qty": 0,
                "filled_amount": 0.0,
                "trade_count": 0,
                "last_trade_time": "",
            },
        )
        if quantity > 0:
            item["filled_qty"] += quantity
            item["filled_amount"] += quantity * price
            item["trade_count"] += 1
        trade_time = str(
            _first_present(raw, ["traded_time", "m_strTradeTime", "trade_time", "m_nTradeTime"], "")
            or ""
        )
        if trade_time:
            item["last_trade_time"] = max(str(item["last_trade_time"]), trade_time)
    return sorted(aggregates.values(), key=lambda item: item["snapshot_key"])


@dataclass(frozen=True)
class RecoveryOutcome:
    status: str
    recovery_id: str
    snapshot_sha256: str
    recoverable_count: int
    recovered_count: int
    active_count: int
    unresolved_count: int
    unresolved: tuple[Mapping[str, Any], ...]


class TradeRecoveryCoordinator:
    """将一次QMT快照与所有未终态交易意图对账。"""

    def __init__(self, store: TradeIntentStore):
        self.store = store

    @staticmethod
    def _order_matches_intent(order: Mapping[str, Any], intent: Mapping[str, Any]) -> bool:
        if str(order.get("ts_code", "")) != _normalize_code(intent.get("ts_code", "")):
            return False
        if str(order.get("side", "")) != str(intent.get("side", "")).upper():
            return False
        order_qty = int(order.get("order_qty", 0) or 0)
        if order_qty > 0 and order_qty != int(intent.get("target_qty", 0) or 0):
            return False
        expected_remark = str((intent.get("metadata") or {}).get("remark", "") or "").strip()
        actual_remark = str(order.get("remark", "") or "").strip()
        return bool(expected_remark and actual_remark and expected_remark == actual_remark)

    @staticmethod
    def _target_status(
        intent: Mapping[str, Any],
        order: Mapping[str, Any],
        trade: Mapping[str, Any] | None,
    ) -> tuple[str, int, float]:
        order_qty = max(int(order.get("filled_qty", 0) or 0), 0)
        order_amount = order_qty * max(float(order.get("avg_price", 0.0) or 0.0), 0.0)
        trade_qty = max(int((trade or {}).get("filled_qty", 0) or 0), 0)
        trade_amount = max(float((trade or {}).get("filled_amount", 0.0) or 0.0), 0.0)
        filled_qty = max(order_qty, trade_qty)
        filled_amount = trade_amount if trade_qty >= order_qty and trade_amount > 0 else order_amount
        target_qty = int(intent.get("target_qty", 0) or 0)
        status_code = int(order.get("status_code", -1) or -1)
        status_text = str(order.get("status_text", "") or "").upper()

        if filled_qty >= target_qty or status_code == 56 or any(
            marker in status_text for marker in ("已成", "FILLED", "ALL_TRADED")
        ):
            return STATUS_FILLED, min(filled_qty, target_qty), filled_amount
        if status_code in {53, 54} or any(
            marker in status_text for marker in ("已撤", "部撤", "CANCELLED", "CANCELED")
        ):
            return STATUS_CANCELLED, filled_qty, filled_amount
        if status_code == 57 or any(marker in status_text for marker in ("废单", "REJECTED")):
            return (STATUS_CANCELLED if filled_qty > 0 else STATUS_REJECTED), filled_qty, filled_amount
        current = str(intent.get("status", ""))
        if filled_qty > 0:
            return STATUS_PARTIALLY_FILLED, filled_qty, filled_amount
        if current == STATUS_CANCEL_REQUESTED:
            return STATUS_CANCEL_REQUESTED, 0, 0.0
        return STATUS_SUBMITTED, 0, 0.0

    def _apply_order_truth(
        self,
        intent: Mapping[str, Any],
        order: Mapping[str, Any],
        trade: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        intent_id = str(intent["intent_id"])
        order_id = str(order.get("order_id", "") or "")
        current = self.store.get_intent(intent_id) or dict(intent)
        status = str(current["status"])
        if status in {STATUS_PREPARED, STATUS_SUBMITTING}:
            current = self.store.transition_intent(
                intent_id,
                STATUS_RECOVERY_REQUIRED,
                expected_statuses={status},
                reason="启动恢复已在券商找到对应委托",
                broker_order_id=order_id,
            )
        target, filled_qty, filled_amount = self._target_status(current, order, trade)
        current_status = str(current["status"])
        if current_status == STATUS_CANCEL_REQUESTED and target == STATUS_SUBMITTED:
            target = STATUS_CANCEL_REQUESTED
        elif current_status == STATUS_PARTIALLY_FILLED and target == STATUS_SUBMITTED:
            target = STATUS_PARTIALLY_FILLED
        return self.store.transition_intent(
            intent_id,
            target,
            expected_statuses={current_status},
            reason="QMT真实委托/成交恢复",
            broker_order_id=order_id,
            filled_qty=filled_qty,
            filled_amount=filled_amount,
        )

    def recover(
        self,
        *,
        daemon_boot_id: str,
        account_fingerprint: str,
        business_date: str,
        positions: Iterable[Any],
        orders: Iterable[Any],
        trades: Iterable[Any],
    ) -> RecoveryOutcome:
        recovery_id = self.store.start_recovery_run(daemon_boot_id)
        normalized_positions = normalize_broker_positions(positions)
        normalized_orders = normalize_broker_orders(orders)
        normalized_trades = normalize_broker_trades(trades)
        snapshot_sha256 = self.store.record_recovery_snapshot(
            recovery_id,
            positions=normalized_positions,
            orders=normalized_orders,
            trades=normalized_trades,
        )
        order_by_id = {
            str(item.get("order_id", "")): item
            for item in normalized_orders
            if str(item.get("order_id", ""))
        }
        trade_by_order = {
            str(item.get("order_id", "")): item
            for item in normalized_trades
            if str(item.get("order_id", ""))
        }
        intents = self.store.list_recoverable_intents(
            account_fingerprint=account_fingerprint,
            business_date_on_or_before=business_date,
        )
        recovered = 0
        active = 0
        unresolved: list[dict[str, Any]] = []
        for original in intents:
            intent = self.store.get_intent(str(original["intent_id"])) or original
            order_id = str(intent.get("broker_order_id", "") or "")
            order = order_by_id.get(order_id) if order_id else None
            if order is None and not order_id:
                matches = [
                    item for item in normalized_orders
                    if self._order_matches_intent(item, intent)
                ]
                if len(matches) == 1:
                    order = matches[0]
                elif len(matches) > 1:
                    unresolved.append(
                        {
                            "intent_id": intent["intent_id"],
                            "ts_code": intent["ts_code"],
                            "reason": f"券商快照匹配到{len(matches)}张委托，无法唯一归属",
                        }
                    )
                    continue
            if order is not None:
                recovered_row = self._apply_order_truth(
                    intent,
                    order,
                    trade_by_order.get(str(order.get("order_id", ""))),
                )
                recovered += 1
                if str(recovered_row["status"]) not in {
                    STATUS_FILLED,
                    STATUS_CANCELLED,
                    STATUS_REJECTED,
                }:
                    active += 1
                continue

            trade = trade_by_order.get(order_id) if order_id else None
            if trade is not None and int(trade.get("filled_qty", 0) or 0) >= int(intent["target_qty"]):
                status = str(intent["status"])
                if status in {STATUS_PREPARED, STATUS_SUBMITTING}:
                    intent = self.store.transition_intent(
                        str(intent["intent_id"]),
                        STATUS_RECOVERY_REQUIRED,
                        expected_statuses={status},
                        reason="只找到券商成交回报，先进入恢复态",
                        broker_order_id=order_id,
                    )
                self.store.transition_intent(
                    str(intent["intent_id"]),
                    STATUS_FILLED,
                    expected_statuses={str(intent["status"])},
                    reason="QMT真实成交回报确认全成",
                    broker_order_id=order_id,
                    filled_qty=int(intent["target_qty"]),
                    filled_amount=float(trade.get("filled_amount", 0.0) or 0.0),
                )
                recovered += 1
                continue

            current_status = str(intent["status"])
            if current_status != STATUS_RECOVERY_REQUIRED:
                intent = self.store.transition_intent(
                    str(intent["intent_id"]),
                    STATUS_RECOVERY_REQUIRED,
                    expected_statuses={current_status},
                    reason="启动对账未在券商快照找到唯一委托/成交",
                    broker_order_id=order_id or None,
                )
            position = next(
                (
                    item for item in normalized_positions
                    if item["ts_code"] == _normalize_code(intent["ts_code"])
                ),
                None,
            )
            unresolved.append(
                {
                    "intent_id": intent["intent_id"],
                    "ts_code": intent["ts_code"],
                    "side": intent["side"],
                    "status": STATUS_RECOVERY_REQUIRED,
                    "broker_order_id": order_id,
                    "broker_position_volume": int((position or {}).get("volume", 0) or 0),
                    "reason": "无唯一券商委托/成交证据，持仓只作风险提示不猜测归属",
                }
            )

        status = "PASS" if not unresolved else "BLOCKED"
        details = {
            "business_date": business_date,
            "position_count": len(normalized_positions),
            "order_count": len(normalized_orders),
            "trade_count": len(normalized_trades),
            "active_count": active,
            "unresolved": unresolved,
        }
        self.store.finish_recovery_run(
            recovery_id,
            status=status,
            broker_snapshot_sha256=snapshot_sha256,
            recovered_count=recovered,
            unresolved_count=len(unresolved),
            details=details,
        )
        return RecoveryOutcome(
            status=status,
            recovery_id=recovery_id,
            snapshot_sha256=snapshot_sha256,
            recoverable_count=len(intents),
            recovered_count=recovered,
            active_count=active,
            unresolved_count=len(unresolved),
            unresolved=tuple(unresolved),
        )
