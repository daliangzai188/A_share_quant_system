"""用人工核验的券商成交证据修复历史退出金额。

本模块只处理已经平仓、但退出金额缺失的历史记录。所有记录必须按买入日、
股票、策略腿、卖出日和总股数严格匹配；券商截图未提供的委托编号不得补造。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping


class BrokerExitEvidenceError(ValueError):
    """券商退出证据无法与权威持仓账安全匹配。"""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _date(value: Any) -> str:
    digits = "".join(char for char in _text(value) if char.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _code(value: Any) -> str:
    text = _text(value).upper()
    if not text or "." in text:
        return text
    if text.startswith(("6", "9")):
        return f"{text}.SH"
    if text.startswith(("0", "2", "3")):
        return f"{text}.SZ"
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return text


def _positive_int(value: Any, field: str) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError) as exc:
        raise BrokerExitEvidenceError(f"{field}必须是正整数") from exc
    if number <= 0:
        raise BrokerExitEvidenceError(f"{field}必须是正整数")
    return number


def _money(value: Any, field: str, *, allow_zero: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise BrokerExitEvidenceError(f"{field}不是有效金额") from exc
    if number < 0 or (number == 0 and not allow_zero):
        raise BrokerExitEvidenceError(f"{field}必须为{'非负数' if allow_zero else '正数'}")
    return number


def _position_quantity(position: Mapping[str, Any]) -> int:
    for field in ("entry_shares", "shares"):
        try:
            quantity = int(float(position.get(field, 0) or 0))
        except (TypeError, ValueError):
            quantity = 0
        if quantity > 0:
            return quantity
    return 0


@dataclass(frozen=True)
class NormalizedExitEvidence:
    evidence_id: str
    entry_date: str
    ts_code: str
    name: str
    strategy_leg: str
    signal_date: str
    exit_date: str
    exit_time: str
    filled_qty: int
    displayed_fill_price: Decimal
    displayed_price_decimals: int
    fill_amount: Decimal
    fee: Decimal
    net_sell_amount: Decimal
    source: str
    evidence_file: str
    evidence_sha256: str
    broker_order_id: str

    @property
    def effective_fill_price(self) -> Decimal:
        return self.fill_amount / Decimal(self.filled_qty)


def normalize_exit_evidence(raw: Mapping[str, Any]) -> NormalizedExitEvidence:
    entry_date = _date(raw.get("entry_date"))
    exit_date = _date(raw.get("exit_date"))
    ts_code = _code(raw.get("ts_code"))
    strategy_leg = _text(raw.get("strategy_leg")).upper()
    if not entry_date or not exit_date or not ts_code or not strategy_leg:
        raise BrokerExitEvidenceError("证据缺少买入日、卖出日、股票代码或策略腿")
    quantity = _positive_int(raw.get("filled_qty"), "filled_qty")
    displayed_price = _money(raw.get("displayed_fill_price"), "displayed_fill_price")
    decimals = int(raw.get("displayed_price_decimals", 3))
    if decimals < 0 or decimals > 6:
        raise BrokerExitEvidenceError("displayed_price_decimals必须在0到6之间")
    fill_amount = _money(raw.get("fill_amount"), "fill_amount")
    fee = _money(raw.get("fee", 0), "fee", allow_zero=True)
    net_amount = _money(raw.get("net_sell_amount"), "net_sell_amount")
    money_tolerance = Decimal("0.02")
    if abs(fill_amount - fee - net_amount) > money_tolerance:
        raise BrokerExitEvidenceError("成交金额-税费与累计卖出金额不一致")
    price_tolerance = Decimal("0.5") * (Decimal(10) ** -decimals)
    if abs(fill_amount / Decimal(quantity) - displayed_price) > price_tolerance:
        raise BrokerExitEvidenceError("截图显示均价与成交金额/数量不符合显示精度")
    evidence_id = _text(raw.get("evidence_id"))
    if not evidence_id:
        evidence_id = f"{exit_date}-{ts_code}-{quantity}-{fill_amount:.2f}"
    digest = _text(raw.get("evidence_sha256")).lower()
    if digest and (len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest)):
        raise BrokerExitEvidenceError("evidence_sha256格式错误")
    return NormalizedExitEvidence(
        evidence_id=evidence_id,
        entry_date=entry_date,
        ts_code=ts_code,
        name=_text(raw.get("name")),
        strategy_leg=strategy_leg,
        signal_date=_date(raw.get("signal_date")),
        exit_date=exit_date,
        exit_time=_text(raw.get("exit_time")),
        filled_qty=quantity,
        displayed_fill_price=displayed_price,
        displayed_price_decimals=decimals,
        fill_amount=fill_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        fee=fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        net_sell_amount=net_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        source=_text(raw.get("source")) or "券商成交截图",
        evidence_file=_text(raw.get("evidence_file")),
        evidence_sha256=digest,
        broker_order_id=_text(raw.get("broker_order_id")),
    )


def _matches(position: Mapping[str, Any], evidence: NormalizedExitEvidence) -> bool:
    if _date(position.get("buy_date")) != evidence.entry_date:
        return False
    if _code(position.get("ts_code")) != evidence.ts_code:
        return False
    if _text(position.get("strategy_leg")).upper() != evidence.strategy_leg:
        return False
    if evidence.signal_date and _date(position.get("signal_date")) != evidence.signal_date:
        return False
    return True


def build_broker_evidence_plan(
    positions: list[dict[str, Any]], records: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """校验证据并返回可应用计划；不修改输入持仓。"""

    normalized = [normalize_exit_evidence(record) for record in records]
    if not normalized:
        raise BrokerExitEvidenceError("证据记录为空")
    ids = [item.evidence_id for item in normalized]
    if len(ids) != len(set(ids)):
        raise BrokerExitEvidenceError("同一批次存在重复evidence_id")
    plans: list[dict[str, Any]] = []
    used_indices: set[int] = set()
    for evidence in normalized:
        indices = [
            index for index, position in enumerate(positions)
            if _matches(position, evidence)
        ]
        if not indices:
            raise BrokerExitEvidenceError(f"未找到匹配持仓：{evidence.evidence_id}")
        if used_indices.intersection(indices):
            raise BrokerExitEvidenceError("多条证据匹配到同一持仓记录")
        group = [positions[index] for index in indices]
        if any(_text(item.get("status")).lower() != "closed" for item in group):
            raise BrokerExitEvidenceError(f"目标持仓尚未全部平仓：{evidence.evidence_id}")
        known_dates = {_date(item.get("sell_date")) for item in group if _date(item.get("sell_date"))}
        if known_dates and known_dates != {evidence.exit_date}:
            raise BrokerExitEvidenceError(f"账本卖出日与证据不一致：{evidence.evidence_id}")
        quantity = sum(_position_quantity(item) for item in group)
        if quantity != evidence.filled_qty:
            raise BrokerExitEvidenceError(
                f"持仓数量与证据不一致：{evidence.evidence_id}，账本{quantity}/证据{evidence.filled_qty}"
            )
        if evidence.name:
            known_names = {_text(item.get("name")) for item in group if _text(item.get("name"))}
            if known_names and known_names != {evidence.name}:
                raise BrokerExitEvidenceError(f"股票名称与证据不一致：{evidence.evidence_id}")
        for item in group:
            old = item.get("manual_exit_evidence")
            if isinstance(old, Mapping) and _text(old.get("evidence_id")) not in {"", evidence.evidence_id}:
                raise BrokerExitEvidenceError(f"持仓已有其他人工证据：{evidence.evidence_id}")
            ledger = item.get("exit_fills_by_date")
            if isinstance(ledger, Mapping):
                old_amount = sum(
                    float(value.get("amount", 0) or 0)
                    for value in ledger.values() if isinstance(value, Mapping)
                )
                if old_amount > 0 and not (
                    isinstance(old, Mapping) and _text(old.get("evidence_id")) == evidence.evidence_id
                ):
                    raise BrokerExitEvidenceError(f"持仓已有非零退出金额：{evidence.evidence_id}")
        plans.append({"evidence": evidence, "position_indices": indices})
        used_indices.update(indices)
    return plans


def apply_broker_evidence_plan(
    positions: list[dict[str, Any]], plans: list[dict[str, Any]], *, applied_at: str
) -> list[dict[str, Any]]:
    """返回写入证据后的新持仓列表，精确保持券商成交总金额。"""

    updated = copy.deepcopy(positions)
    for plan in plans:
        evidence: NormalizedExitEvidence = plan["evidence"]
        indices: list[int] = list(plan["position_indices"])
        remaining_amount = evidence.fill_amount
        for offset, index in enumerate(indices):
            position = updated[index]
            quantity = _position_quantity(position)
            if offset == len(indices) - 1:
                allocated = remaining_amount
            else:
                allocated = (
                    evidence.fill_amount * Decimal(quantity) / Decimal(evidence.filled_qty)
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                remaining_amount -= allocated
            effective_price = allocated / Decimal(quantity)
            position["entry_shares"] = quantity
            position["shares"] = 0
            position["status"] = "closed"
            position["sell_date"] = evidence.exit_date
            position["sell_price"] = float(effective_price)
            position["exit_fills_by_date"] = {
                evidence.exit_date: {"qty": quantity, "amount": float(allocated)}
            }
            position["manual_exit_evidence"] = {
                "evidence_id": evidence.evidence_id,
                "source": evidence.source,
                "evidence_file": evidence.evidence_file,
                "evidence_sha256": evidence.evidence_sha256,
                "broker_order_id": evidence.broker_order_id or None,
                "broker_order_id_status": (
                    "PROVIDED" if evidence.broker_order_id else "NOT_VISIBLE_IN_SCREENSHOT"
                ),
                "displayed_fill_price": float(evidence.displayed_fill_price),
                "displayed_price_decimals": evidence.displayed_price_decimals,
                "group_filled_qty": evidence.filled_qty,
                "group_fill_amount": float(evidence.fill_amount),
                "fee": float(evidence.fee),
                "net_sell_amount": float(evidence.net_sell_amount),
                "exit_time": evidence.exit_time,
                "applied_at": applied_at,
            }
    return updated

