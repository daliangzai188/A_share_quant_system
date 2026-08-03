"""策略D接力的实盘执行计算。

本模块只做无副作用计算，不读取账户、不连接QMT、不提交委托。实盘守护进程负责
取得同一时点的真实账户、持仓和盘口快照后调用这里，从而把以下铁律集中在一处：

1. 集合竞价只卖虚拟匹配量能够安全承接的部分，数据不完整时卖出数量为0；
2. 开盘后的新仓累计买入额不得超过D累计确认卖出额，不动用原有空闲现金；
3. 新仓以总资产82.5%为目标、85%为单票硬顶，并同时受真实可用现金约束；
4. 现价超过开盘价2%或已经涨停时停止追价。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def floor_lot(quantity: float, lot_size: int = 100) -> int:
    """向下取整到整手；异常值统一返回0。"""
    if quantity <= 0 or lot_size <= 0:
        return 0
    return max(int(quantity // lot_size) * lot_size, 0)


@dataclass(frozen=True)
class AuctionSellDecision:
    quantity: int
    reference_price: float
    matched_quantity: int
    unmatched_sell_ratio: float
    reason: str


def calculate_auction_safe_sell_quantity(
    quote: Any,
    *,
    position_shares: int,
    max_participation: float,
    max_unmatched_sell_ratio: float,
    book_volume_unit: int = 1,
    lot_size: int = 100,
    max_reference_spread_pct: float = 0.001,
) -> AuctionSellDecision:
    """根据09:23虚拟匹配盘口计算D集合竞价安全卖量。

    开放式集合竞价期间，买一/卖一应同时给出虚拟参考价，买一/卖一量中的较小值
    作为匹配量；卖二量作为卖方未匹配量。任一字段缺失、价差异常、卖方失衡过大
    时均返回0，宁可把全部D留到开盘后的成对POV，也不猜测容量后整仓砸盘。
    """
    shares = max(int(position_shares or 0), 0)
    if quote is None or shares <= 0:
        return AuctionSellDecision(0, 0.0, 0, 0.0, "持仓或竞价行情为空")

    bid_prices = list(getattr(quote, "bid_prices", None) or [])
    ask_prices = list(getattr(quote, "ask_prices", None) or [])
    bid_volumes = list(getattr(quote, "bid_volumes", None) or [])
    ask_volumes = list(getattr(quote, "ask_volumes", None) or [])
    if (
        not bid_prices or not ask_prices
        or len(bid_volumes) < 2 or len(ask_volumes) < 2
    ):
        return AuctionSellDecision(0, 0.0, 0, 0.0, "竞价虚拟盘口字段不完整")

    bid_price = float(bid_prices[0] or 0.0)
    ask_price = float(ask_prices[0] or 0.0)
    if bid_price <= 0 or ask_price <= 0:
        return AuctionSellDecision(0, 0.0, 0, 0.0, "竞价参考价无效")
    reference_price = (bid_price + ask_price) / 2.0
    spread_pct = abs(bid_price - ask_price) / max(reference_price, 0.01)
    if spread_pct > max(max_reference_spread_pct, 0.0):
        return AuctionSellDecision(
            0, reference_price, 0, 0.0,
            f"竞价买一卖一价差{spread_pct:.3%}超过允许值",
        )

    unit = max(int(book_volume_unit or 1), 1)
    matched = int(
        min(float(bid_volumes[0] or 0.0), float(ask_volumes[0] or 0.0)) * unit
    )
    unmatched_sell = int(float(ask_volumes[1] or 0.0) * unit) if len(ask_volumes) > 1 else 0
    if matched <= 0:
        return AuctionSellDecision(0, reference_price, 0, 0.0, "竞价虚拟匹配量为0")
    unmatched_ratio = unmatched_sell / matched
    if unmatched_ratio > max(max_unmatched_sell_ratio, 0.0):
        return AuctionSellDecision(
            0, reference_price, matched, unmatched_ratio,
            f"卖方未匹配量/匹配量={unmatched_ratio:.2%}，卖压过大",
        )

    capacity = floor_lot(
        matched * min(max(max_participation, 0.0), 1.0),
        lot_size,
    )
    quantity = min(shares, capacity)
    if quantity <= 0:
        return AuctionSellDecision(
            0, reference_price, matched, unmatched_ratio, "安全参与量不足一手"
        )
    return AuctionSellDecision(
        quantity,
        reference_price,
        matched,
        unmatched_ratio,
        "整仓处于安全容量内" if quantity >= shares else "仅卖安全容量部分",
    )


@dataclass(frozen=True)
class RelayBuyDecision:
    quantity: int
    amount_cap: float
    target_gap: float
    hard_cap_room: float
    released_cash_room: float
    reason: str


def calculate_relay_buy_quantity(
    *,
    target_amount: float,
    hard_cap_amount: float,
    confirmed_buy_amount: float,
    confirmed_sell_amount: float,
    available_cash: float,
    cash_buffer: float,
    flow_budget: float,
    order_price: float,
    lot_size: int = 100,
) -> RelayBuyDecision:
    """按“已卖D多少钱，最多买新仓多少钱”计算本片买入数量。

    ``available_cash`` 只用于防废单；真正的接力资金闸门是
    ``confirmed_sell_amount - confirmed_buy_amount``。因此账户原本留存的17.5%现金
    不会被成对POV使用。
    """
    target_gap = max(float(target_amount) - float(confirmed_buy_amount), 0.0)
    hard_room = max(float(hard_cap_amount) - float(confirmed_buy_amount), 0.0)
    released_room = max(float(confirmed_sell_amount) - float(confirmed_buy_amount), 0.0)
    cash_room = max(float(available_cash) - max(float(cash_buffer), 0.0), 0.0)
    amount_cap = min(
        target_gap,
        hard_room,
        released_room,
        cash_room,
        max(float(flow_budget), 0.0),
    )
    if order_price <= 0 or lot_size <= 0 or amount_cap <= 0:
        return RelayBuyDecision(
            0, amount_cap, target_gap, hard_room, released_room, "价格或可用预算为0"
        )
    # 减去一分钱，避免浮点误差使“最坏委托金额”刚好越过任一硬顶。
    quantity = floor_lot(max((amount_cap - 0.01) / order_price, 0.0), lot_size)
    reason = "允许下单" if quantity >= lot_size else "安全预算不足一手"
    return RelayBuyDecision(
        quantity, amount_cap, target_gap, hard_room, released_room, reason
    )


def relay_buy_limit_price(
    quote: Any,
    *,
    chase_cap: float = 0.02,
) -> tuple[float, str]:
    """返回接力买单上限价；不允许追价时返回0和原因。"""
    if quote is None:
        return 0.0, "新仓行情为空"
    last = float(getattr(quote, "last_price", 0.0) or 0.0)
    open_price = float(getattr(quote, "open_price", 0.0) or 0.0)
    upper_limit = float(getattr(quote, "upper_limit", 0.0) or 0.0)
    if last <= 0 or open_price <= 0:
        return 0.0, "新仓现价或开盘价无效"
    if upper_limit > 0 and last >= upper_limit - 1e-6:
        return 0.0, "新仓已经涨停"
    cap_price = float(
        Decimal(str(open_price * (1.0 + max(float(chase_cap), 0.0)))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )
    if last > cap_price + 1e-9:
        return 0.0, f"新仓现价超过开盘价{max(float(chase_cap), 0.0):.1%}追价线"
    if upper_limit > 0:
        cap_price = min(cap_price, upper_limit)
    return cap_price, "允许接力买入"
