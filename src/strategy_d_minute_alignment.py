"""策略D实盘与研究回测共用的一分钟封板/炸板/回封重建口径。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.strategy_d_factor_rules import trading_minutes_between
from src.strategy_d_intraday_ledger import PRICE_TOLERANCE


# 回测数据的分钟标签：09:30为独立开盘K线，之后按分钟结束时刻标记；
# 午后第一根为13:01。这里预先列出合法时钟，禁止对HHMM直接做十进制range。
_COMPLETED_TRADING_MINUTE_HHMM = tuple(
    hour * 100 + minute
    for hour in (9, 10, 11)
    for minute in range(60)
    if 930 <= hour * 100 + minute <= 1130
) + tuple(
    hour * 100 + minute
    for hour in (13, 14, 15)
    for minute in range(60)
    if 1301 <= hour * 100 + minute <= 1500
)


@dataclass(frozen=True)
class StrictMinutePath:
    certifiable: bool
    reason: str = ""
    last_completed_hhmm: int = 0
    was_sealed: bool = False
    ever_sealed: bool = False
    first_seal_hhmm: int = 0
    last_seal_hhmm: int = 0
    last_reseal_hhmm: int = 0
    open_times: int = 0
    last_break_hhmm: int = 0
    last_break_close: float = 0.0
    previous_seal_to_break_minutes: int = 0
    pre_signal_low_price: float = 0.0
    signal_cumulative_amount: float = 0.0

    @property
    def has_reseal(self) -> bool:
        return bool(
            self.certifiable
            and self.ever_sealed
            and self.open_times >= 1
            and self.last_reseal_hhmm > 0
        )

    @property
    def has_fresh_reseal(self) -> bool:
        return bool(
            self.has_reseal
            and self.last_reseal_hhmm == self.last_completed_hhmm
            and self.was_sealed
        )


def expected_completed_minute_hhmm(current_hhmm: int) -> list[int]:
    """返回Tushare/QMT回测口径下，此刻应已完成的分钟标签。

    两套数据都把09:30集合竞价/开盘快照保留为一根独立K线；连续竞价分钟
    使用结束时刻标记，所以第一根完整交易分钟是09:31，午后第一根是13:01。
    """

    current = int(current_hhmm)
    if current < 930:
        return []
    capped_current = min(current, 1500)
    return [
        hhmm
        for hhmm in _COMPLETED_TRADING_MINUTE_HHMM
        if hhmm <= capped_current
    ]


def replay_completed_minute_path(
    bars: Sequence[Mapping[str, Any]],
    *,
    limit_price: float,
    current_hhmm: int,
    price_tolerance: float = PRICE_TOLERANCE,
) -> StrictMinutePath:
    """严格按回测的“分钟收盘价是否等于涨停价”重建日内路径。"""

    expected = expected_completed_minute_hhmm(current_hhmm)
    if limit_price <= 0:
        return StrictMinutePath(False, "涨停价无效")
    if not expected:
        return StrictMinutePath(False, "尚未形成已完成的一分钟K线")

    expected_set = set(expected)
    normalized: dict[int, dict[str, float]] = {}
    duplicates: list[int] = []
    for raw in bars or []:
        try:
            hhmm = int(raw.get("hhmm", 0) or 0)
        except (TypeError, ValueError):
            continue
        if hhmm not in expected_set:
            continue
        if hhmm in normalized:
            duplicates.append(hhmm)
            continue
        try:
            close = float(raw.get("close", 0.0) or 0.0)
            low = float(raw.get("low", close) or close)
            amount = float(raw.get("amount", 0.0) or 0.0)
        except (TypeError, ValueError):
            return StrictMinutePath(False, f"{hhmm:04d}分钟K线数值无法解析")
        if close <= 0 or low <= 0:
            return StrictMinutePath(False, f"{hhmm:04d}分钟K线价格无效")
        normalized[hhmm] = {"close": close, "low": low, "amount": max(amount, 0.0)}

    if duplicates:
        return StrictMinutePath(False, f"一分钟K线时间重复:{sorted(set(duplicates))[:5]}")
    missing = [hhmm for hhmm in expected if hhmm not in normalized]
    if missing:
        return StrictMinutePath(
            False,
            f"一分钟K线不完整，缺{len(missing)}根，示例:{missing[:5]}",
        )

    was_sealed = False
    ever_sealed = False
    first_seal_hhmm = 0
    last_seal_hhmm = 0
    last_reseal_hhmm = 0
    open_times = 0
    last_break_hhmm = 0
    last_break_close = 0.0
    previous_seal_to_break_minutes = 0
    cumulative_amount = 0.0
    running_low = 0.0
    pre_signal_low_price = 0.0
    signal_cumulative_amount = 0.0

    for hhmm in expected:
        bar = normalized[hhmm]
        close = bar["close"]
        low = bar["low"]
        cumulative_amount += bar["amount"]
        running_low = low if running_low <= 0 else min(running_low, low)
        sealed = abs(close - float(limit_price)) <= float(price_tolerance)
        if sealed and not was_sealed:
            if not ever_sealed:
                ever_sealed = True
                first_seal_hhmm = hhmm
            else:
                last_reseal_hhmm = hhmm
                pre_signal_low_price = running_low
                signal_cumulative_amount = cumulative_amount
            last_seal_hhmm = hhmm
        elif not sealed and was_sealed:
            open_times += 1
            last_break_hhmm = hhmm
            last_break_close = close
            previous_seal_to_break_minutes = trading_minutes_between(
                last_seal_hhmm, hhmm
            )
        was_sealed = sealed

    return StrictMinutePath(
        certifiable=True,
        last_completed_hhmm=expected[-1],
        was_sealed=was_sealed,
        ever_sealed=ever_sealed,
        first_seal_hhmm=first_seal_hhmm,
        last_seal_hhmm=last_seal_hhmm,
        last_reseal_hhmm=last_reseal_hhmm,
        open_times=open_times,
        last_break_hhmm=last_break_hhmm,
        last_break_close=last_break_close,
        previous_seal_to_break_minutes=previous_seal_to_break_minutes,
        pre_signal_low_price=pre_signal_low_price,
        signal_cumulative_amount=signal_cumulative_amount,
    )
