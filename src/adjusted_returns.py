"""基于每日 ``pre_close`` 链接计算前复权持有期收益。"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any


DailyLoader = Callable[[str], Any]


def linked_forward_adjusted_return(
    *,
    ts_code: str,
    buy_date: str,
    buy_price: float,
    sell_date: str,
    sell_price: float,
    trade_dates: Sequence[str],
    daily_loader: DailyLoader,
) -> float:
    """计算买入价到卖出价的前复权收益，正确跨越分红除权日。

    ``pre_close`` 是交易所按除权除息规则修正后的前收盘参考价。把买入日的
    ``close / buy_price``、中间日的 ``close / pre_close`` 与退出日的
    ``sell_price / pre_close`` 链接，等价于在统一前复权价格序列上计算收益。
    """

    if buy_price <= 0 or sell_price <= 0:
        raise ValueError("买卖价格必须大于0")
    dates = [str(value) for value in trade_dates]
    try:
        buy_index = dates.index(str(buy_date))
        sell_index = dates.index(str(sell_date))
    except ValueError as exc:
        raise ValueError("买卖日期不在交易日历中") from exc
    if sell_index < buy_index:
        raise ValueError("卖出日期不能早于买入日期")
    if sell_index == buy_index:
        return float(sell_price) / float(buy_price) - 1.0

    buy_frame = daily_loader(str(buy_date))
    if buy_frame is None or ts_code not in buy_frame.index:
        raise ValueError(f"{buy_date}缺少{ts_code}行情")
    buy_close = float(buy_frame.loc[ts_code, "close"])
    if buy_close <= 0:
        raise ValueError(f"{buy_date} {ts_code}收盘价无效")
    factor = buy_close / float(buy_price)

    for index in range(buy_index + 1, sell_index + 1):
        date = dates[index]
        frame = daily_loader(date)
        if frame is None or ts_code not in frame.index:
            # 停牌日不改变净值；复牌日pre_close会承接期间的除权调整。
            continue
        row = frame.loc[ts_code]
        pre_close = float(row.get("pre_close", 0.0) or 0.0)
        price = float(sell_price) if date == str(sell_date) else float(row["close"])
        if pre_close <= 0 or price <= 0:
            raise ValueError(f"{date} {ts_code}缺少有效pre_close/price")
        factor *= price / pre_close
    return factor - 1.0
