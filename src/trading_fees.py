"""A股交易费率的日期化唯一口径。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_STAMP_TAX_SCHEDULE = (
    {"start_date": "19000101", "end_date": "20230827", "rate": 0.001},
    {"start_date": "20230828", "end_date": "99991231", "rate": 0.0005},
)


def stamp_tax_rate_for_date(
    trade_date: object,
    schedule: Sequence[Mapping[str, Any]] | None = None,
) -> float:
    day = str(trade_date or "").replace("-", "")[:8]
    if len(day) != 8:
        raise ValueError("印花税计算需要YYYYMMDD交易日期")
    rows = schedule or DEFAULT_STAMP_TAX_SCHEDULE
    for row in rows:
        start = str(row.get("start_date", "19000101")).replace("-", "")[:8]
        end = str(row.get("end_date", "99991231")).replace("-", "")[:8]
        if start <= day <= end:
            return float(row["rate"])
    raise ValueError(f"{day}没有匹配的印花税费率")


def account_return_after_fees(
    *,
    stock_return_before_fees: float,
    exit_date: str,
    position_pct: float,
    commission_rate: float,
    transfer_fee_rate: float,
    stamp_tax_schedule: Sequence[Mapping[str, Any]] | None = None,
) -> float:
    buy_fee_rate = float(commission_rate) + float(transfer_fee_rate)
    sell_fee_rate = (
        float(commission_rate)
        + float(transfer_fee_rate)
        + stamp_tax_rate_for_date(exit_date, stamp_tax_schedule)
    )
    stock_return = float(stock_return_before_fees)
    return (
        stock_return - buy_fee_rate - (1.0 + stock_return) * sell_fee_rate
    ) * float(position_pct)
