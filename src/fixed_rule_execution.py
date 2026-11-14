"""按固定规则在日线上模拟成交和收益：T+1开盘买入（开盘一字涨停买不到）、持有期满收盘卖出（跌停顺延）。

影子账、停手判定、逐笔对账和研究回放都按这里计算每笔计划的成交价与前复权收益。2026-11-14 从
scripts/build_ac_daily_candidates.py（脚本现移到 research/）原样搬来，代码未改；导入时读取交易日历和股票基本信息，和原来一样。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.adjusted_returns import linked_forward_adjusted_return
from src.market_rules import (
    fixed_close_sell_executable,
    fixed_open_buy_executable,
    limit_up_price,
    listing_trade_day_number,
    price_limit_pct,
)

ROOT = Path(__file__).resolve().parents[1]
DAILY = ROOT / "data" / "raw" / "daily"

cal = pd.read_csv(ROOT / "data" / "raw" / "trade_calendar.csv", dtype=str)
ccol = "cal_date" if "cal_date" in cal.columns else cal.columns[0]
if "is_open" in cal.columns:
    cal = cal[cal["is_open"].astype(str).isin({"1", "1.0", "True"})]
DATES = sorted(cal[ccol].tolist())
DIDX = {d: i for i, d in enumerate(DATES)}
_dc: dict[str, pd.DataFrame] = {}
_afc: dict[str, pd.DataFrame] = {}

STOCK_BASIC_PATH = ROOT / "data" / "raw" / "stock_basic" / "stock_basic_all.csv"
if STOCK_BASIC_PATH.exists():
    _stock_basic = pd.read_csv(
        STOCK_BASIC_PATH,
        dtype={"ts_code": str, "list_date": str, "name": str},
        low_memory=False,
    ).drop_duplicates("ts_code", keep="last").set_index("ts_code")
else:
    _stock_basic = pd.DataFrame()


def day(d: str):
    if d not in _dc:
        p = DAILY / f"{d}.csv"
        _dc[d] = pd.read_csv(p, dtype={"ts_code": str}).set_index("ts_code") if p.exists() else pd.DataFrame()
    return None if _dc[d].empty else _dc[d]


def adj_factor_value(trade_date: str, ts_code: str) -> float | None:
    """读取独立Tushare复权因子，供精确现金账本交叉校验。"""

    if trade_date not in _afc:
        path = ROOT / "data" / "raw" / "adj_factor" / f"{trade_date}.csv"
        _afc[trade_date] = (
            pd.read_csv(path, dtype={"ts_code": str}, low_memory=False).set_index("ts_code")
            if path.exists()
            else pd.DataFrame()
        )
    frame = _afc[trade_date]
    if frame.empty or ts_code not in frame.index:
        return None
    value = pd.to_numeric(frame.loc[ts_code, "adj_factor"], errors="coerce")
    return None if pd.isna(value) or float(value) <= 0 else float(value)


def stock_meta(code: str, name: str = "") -> tuple[str, str]:
    resolved_name = str(name or "")
    list_date = ""
    if not _stock_basic.empty and code in _stock_basic.index:
        row = _stock_basic.loc[code]
        if not resolved_name:
            resolved_name = str(row.get("name", "") or "")
        list_date = str(row.get("list_date", "") or "").replace(".0", "")
    return resolved_name, list_date


def limit_cap(code: str, pre: float, *, name: str = "", trade_date: str = "") -> float | None:
    resolved_name, list_date = stock_meta(code, name)
    listing_day = listing_trade_day_number(list_date, trade_date, DATES)
    pct = price_limit_pct(
        code,
        name=resolved_name,
        trade_date=trade_date,
        listing_day_number=listing_day,
    )
    return limit_up_price(pre, pct)


@dataclass(frozen=True)
class TradeReturnResult:
    status: str
    buy_date: str
    exit_date: str
    stock_return: float | None
    exit_rule: str = ""
    entry_reference_price: float | None = None
    entry_price: float | None = None
    exit_reference_price: float | None = None
    exit_price: float | None = None
    buy_adj_factor: float | None = None
    sell_adj_factor: float | None = None


@dataclass(frozen=True)
class FixedOpenEntryResult:
    status: str
    buy_date: str
    open_price: float | None


def fixed_open_entry_details(
    sig: str,
    code: str,
    *,
    name: str = "",
) -> FixedOpenEntryResult:
    """只检查T+1开盘是否可成交，不读取计划退出日之后的数据。"""

    i = DIDX.get(sig)
    if i is None or i + 1 >= len(DATES):
        return FixedOpenEntryResult("NO_CALENDAR", "", None)
    buy_date = DATES[i + 1]
    frame = day(buy_date)
    if frame is None or code not in frame.index:
        return FixedOpenEntryResult("NO_PRICE", buy_date, None)
    row = frame.loc[code]
    open_price = float(row["open"])
    pre_close = float(row.get("pre_close", 0) or 0)
    if open_price <= 0:
        return FixedOpenEntryResult("BAD_PRICE", buy_date, None)

    resolved_name, list_date = stock_meta(code, name)
    listing_day = listing_trade_day_number(list_date, buy_date, DATES)
    buy_limit_pct = price_limit_pct(
        code,
        name=resolved_name,
        trade_date=buy_date,
        listing_day_number=listing_day,
    )
    if pre_close > 0 and not fixed_open_buy_executable(
        pre_close=pre_close,
        open_price=open_price,
        limit_pct=buy_limit_pct,
    ):
        return FixedOpenEntryResult("LIMIT_UP_UNBUYABLE", buy_date, open_price)
    return FixedOpenEntryResult("OK", buy_date, open_price)


def trade_return_details(
    sig: str,
    code: str,
    hold: int,
    *,
    name: str = "",
    use_intraday_takeprofit: bool = False,
    takeprofit_offset: float = 0.01,
    sell_delay_max: int = 4,
    cutoff_date: str | None = None,
) -> TradeReturnResult:
    """按固定开盘买/固定收盘卖规则计算可执行的前复权收益。"""

    i = DIDX.get(sig)
    if i is None or i + hold >= len(DATES):
        return TradeReturnResult("NO_CALENDAR", "", "", None)
    entry = fixed_open_entry_details(sig, code, name=name)
    d1 = entry.buy_date
    if entry.status != "OK" or entry.open_price is None:
        return TradeReturnResult(entry.status, d1, "", None)
    open_price = float(entry.open_price)

    resolved_name, list_date = stock_meta(code, name)

    buy_price = open_price * 1.001
    for k in range(hold, hold + max(int(sell_delay_max), 1)):
        if i + k >= len(DATES):
            break
        exit_date = DATES[i + k]
        if cutoff_date and exit_date > str(cutoff_date):
            break
        frame = day(exit_date)
        if frame is None or code not in frame.index:
            continue
        exit_row = frame.loc[code]
        exit_pre_close = float(exit_row.get("pre_close", 0) or 0)
        exit_close = float(exit_row.get("close", 0) or 0)
        exit_high = float(exit_row.get("high", 0) or 0)
        exit_listing_day = listing_trade_day_number(list_date, exit_date, DATES)
        exit_limit_pct = price_limit_pct(
            code,
            name=resolved_name,
            trade_date=exit_date,
            listing_day_number=exit_listing_day,
        )

        sell_price: float | None = None
        exit_rule = ""
        cap = limit_up_price(exit_pre_close, exit_limit_pct) if exit_pre_close > 0 else None
        if (
            use_intraday_takeprofit
            # 实盘止盈监控只在原计划退出日运行。若计划日跌停/停牌导致延期，
            # 后续交易日按延期卖出的固定收盘口径处理，不能回测出实盘不存在的止盈。
            and k == hold
            and cap is not None
            and exit_high >= cap - float(takeprofit_offset) - 1e-9
        ):
            sell_price = cap - float(takeprofit_offset)
            exit_rule = "INTRADAY_LIMIT_UP_MINUS_OFFSET"
        elif exit_pre_close > 0 and fixed_close_sell_executable(
            pre_close=exit_pre_close,
            close_price=exit_close,
            limit_pct=exit_limit_pct,
        ):
            sell_price = exit_close * 0.999
            exit_rule = "FIXED_CLOSE"
        elif exit_pre_close <= 0 and exit_close > 0:
            sell_price = exit_close * 0.999
            exit_rule = "FIXED_CLOSE_NO_LIMIT_REFERENCE"

        if sell_price is None or sell_price <= 0:
            continue
        try:
            stock_return = linked_forward_adjusted_return(
                ts_code=code,
                buy_date=d1,
                buy_price=buy_price,
                sell_date=exit_date,
                sell_price=sell_price,
                trade_dates=DATES,
                daily_loader=day,
            )
        except ValueError:
            return TradeReturnResult("NO_ADJUSTED_PRICE", d1, exit_date, None, exit_rule)
        return TradeReturnResult(
            "OK",
            d1,
            exit_date,
            stock_return,
            exit_rule,
            entry_reference_price=open_price,
            entry_price=buy_price,
            exit_reference_price=(
                sell_price
                if exit_rule == "INTRADAY_LIMIT_UP_MINUS_OFFSET"
                else exit_close
            ),
            exit_price=sell_price,
            buy_adj_factor=adj_factor_value(d1, code),
            sell_adj_factor=adj_factor_value(exit_date, code),
        )
    return TradeReturnResult("SELL_UNRESOLVED", d1, "", None)


def trade_return(
    sig: str,
    code: str,
    hold: int,
    *,
    name: str = "",
    use_intraday_takeprofit: bool = False,
    takeprofit_offset: float = 0.01,
):
    """兼容旧调用，返回 ``(状态, 买日, 卖日, 前复权个股收益)``。"""

    result = trade_return_details(
        sig,
        code,
        hold,
        name=name,
        use_intraday_takeprofit=use_intraday_takeprofit,
        takeprofit_offset=takeprofit_offset,
    )
    return result.status, result.buy_date, result.exit_date, result.stock_return
