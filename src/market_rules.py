"""A股涨跌停与固定时点成交规则。

价格涨跌停使用未复权价格；收益复权由 ``src.adjusted_returns`` 单独负责。
本模块默认采用悲观可执行口径：策略若声明“开盘买”，开盘已经涨停就不假设
能够成交；策略若声明“收盘卖”，收盘仍在跌停就不假设能够成交。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable


PRICE_TICK = 0.01


def normalize_trade_date(value: object) -> str:
    return str(value or "").replace("-", "")[:8]


def normalize_code(value: object) -> str:
    return str(value or "").strip().upper()


def market_segment(ts_code: object) -> str:
    code = normalize_code(ts_code)
    prefix = code.split(".")[0]
    if code.endswith(".BJ") or prefix.startswith(("4", "8", "9")):
        return "bj"
    if prefix.startswith(("688", "689")):
        return "star"
    if prefix.startswith(("300", "301")):
        return "chi_next"
    if code.endswith(".SH") and prefix.startswith("6"):
        return "sh_main"
    if code.endswith(".SZ") and prefix.startswith(("000", "001", "002", "003")):
        return "sz_main"
    return "other"


def is_st_name(name: object) -> bool:
    text = str(name or "").upper().replace(" ", "")
    return "ST" in text or "退" in text or "PT" in text


def listing_trade_day_number(
    list_date: object,
    trade_date: object,
    trade_dates: Iterable[str],
) -> int | None:
    """返回上市后的第几个交易日（上市日=1）；资料不足返回 ``None``。"""

    listed = normalize_trade_date(list_date)
    current = normalize_trade_date(trade_date)
    if len(listed) != 8 or len(current) != 8 or current < listed:
        return None
    eligible = [normalize_trade_date(day) for day in trade_dates]
    eligible = [day for day in eligible if listed <= day <= current]
    return len(eligible) or None


def price_limit_pct(
    ts_code: object,
    *,
    name: object = "",
    trade_date: object = "",
    listing_day_number: int | None = None,
) -> float | None:
    """返回当日涨跌停比例；``None`` 表示上市初期当日无价格涨跌幅限制。"""

    segment = market_segment(ts_code)
    day = normalize_trade_date(trade_date)

    if listing_day_number is not None:
        if segment in {"chi_next", "star"} and listing_day_number <= 5:
            return None
        if segment == "bj" and listing_day_number <= 1:
            return None
        if (
            segment in {"sh_main", "sz_main"}
            and day >= "20230410"
            and listing_day_number <= 5
        ):
            return None

    if segment == "bj":
        return 0.30
    if segment == "star":
        return 0.20
    if segment == "chi_next":
        # 创业板注册制涨跌幅规则自2020-08-24起改为20%。
        return 0.20 if not day or day >= "20200824" else 0.10
    if is_st_name(name):
        return 0.05
    return 0.10


def round_stock_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def limit_up_price(pre_close: float, limit_pct: float | None) -> float | None:
    if limit_pct is None:
        return None
    return round_stock_price(float(pre_close) * (1.0 + float(limit_pct)))


def limit_down_price(pre_close: float, limit_pct: float | None) -> float | None:
    if limit_pct is None:
        return None
    return round_stock_price(float(pre_close) * (1.0 - float(limit_pct)))


def fixed_open_buy_executable(
    *,
    pre_close: float,
    open_price: float,
    limit_pct: float | None,
    tolerance: float = 1e-6,
) -> bool:
    """固定开盘买入：开盘已涨停时保守判定为不可成交。"""

    cap = limit_up_price(pre_close, limit_pct)
    if cap is None:
        return True
    return float(open_price) < cap - tolerance


def fixed_close_sell_executable(
    *,
    pre_close: float,
    close_price: float,
    limit_pct: float | None,
    tolerance: float = 1e-6,
) -> bool:
    """固定收盘卖出：收盘仍跌停时保守判定为不可成交并顺延。"""

    floor = limit_down_price(pre_close, limit_pct)
    if floor is None:
        return True
    return float(close_price) > floor + tolerance


def orderable_buy_quantity(
    *,
    ts_code: object,
    available_amount: float,
    execution_price: float,
) -> int:
    """按市场申报单位把目标金额向下取整为可买股数。"""

    amount = float(available_amount)
    price = float(execution_price)
    if amount <= 0 or price <= 0:
        return 0
    raw = int(amount // price)
    segment = market_segment(ts_code)
    if segment == "star":
        return raw if raw >= 200 else 0
    if segment == "bj":
        return raw if raw >= 100 else 0
    return (raw // 100) * 100
