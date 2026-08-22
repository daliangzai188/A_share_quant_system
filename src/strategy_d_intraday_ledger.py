"""策略D历史盘中事件账本的纯机械路径重建。

本模块只处理已经标准化的分钟K线，不采集数据、不读取账户、不生成真实委托。
核心原则是保守可证伪：

1. 分钟收盘价到达涨停价才确认该分钟结束时处于封板状态；
2. 从封板到非封板计一次炸板，从非封板到封板计一次回封；
3. 14:00起、14:55撤单前第一次满足炸板2~3次的回封是第一可交易信号；
4. 信号发出后，只有14:55撤单前价格再次低于涨停价，才把涨停限价排队单
   记为“价格穿透确认可成交”；始终封住时没有历史队列深度，必须记为未知；
5. 同一分钟内既触涨停又交易到涨停下方时，OHLC无法确认先后顺序，路径标记歧义。

一分钟K线仍无法还原封单队列前方数量。它可以确认开板后的限价单可成交，却不能
证明始终封板期间是否排到。完整队列认证需要历史tick/盘口快照。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.strategy_d_spec import (
    D_FIRST_TIME_BUCKETS,
    D_MAX_OPEN_TIMES,
    D_MIN_OPEN_TIMES,
    D_ORDER_CANCEL_HHMM,
    D_SIGNAL_START_HHMM,
    classify_first_time_bucket_hhmm,
)


# A股价格最小变动单位通常为0.01元。容差必须小于半个价位，
# 否则“涨停价低1分”会被误判为封板。
PRICE_TOLERANCE = 0.001


@dataclass(frozen=True)
class IntradayReplayPolicy:
    signal_start_hhmm: int = D_SIGNAL_START_HHMM
    cancel_hhmm: int = D_ORDER_CANCEL_HHMM
    min_open_times: int = D_MIN_OPEN_TIMES
    max_open_times: int = D_MAX_OPEN_TIMES
    allowed_first_time_buckets: frozenset[str] = D_FIRST_TIME_BUCKETS
    price_tolerance: float = PRICE_TOLERANCE


def normalize_hhmm(value: object) -> int:
    """把09:35、935、20241018093500等形式统一成HHMM。"""

    if pd.isna(value):
        return 0
    text = str(value).strip().replace(".0", "")
    if not text:
        return 0
    if ":" in text:
        parts = text.split(":")
        try:
            return int(parts[0]) * 100 + int(parts[1])
        except (TypeError, ValueError, IndexError):
            return 0
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) >= 12:
        return int(digits[8:12])
    if len(digits) >= 4:
        return int(digits[-4:])
    if digits:
        return int(digits)
    return 0


def normalize_minute_bars(
    bars: pd.DataFrame,
    *,
    ts_code: str = "",
    trade_date: str = "",
) -> pd.DataFrame:
    """统一分钟字段并过滤A股连续竞价/收盘前时段。"""

    columns = [
        "ts_code",
        "trade_date",
        "hhmm",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]
    if bars is None or bars.empty:
        return pd.DataFrame(columns=columns)
    frame = bars.copy()
    aliases = {
        "vol": "volume",
        "bar_time": "_time",
        "trade_time": "_time",
        "datetime": "_time",
        "time": "_time",
    }
    for source, target in aliases.items():
        if source in frame.columns and target not in frame.columns:
            frame = frame.rename(columns={source: target})
    if "ts_code" not in frame.columns:
        frame["ts_code"] = ts_code
    if "trade_date" not in frame.columns:
        frame["trade_date"] = trade_date
    if "hhmm" not in frame.columns:
        time_source = frame.get("_time", pd.Series("", index=frame.index))
        frame["hhmm"] = time_source.map(normalize_hhmm)
    else:
        frame["hhmm"] = frame["hhmm"].map(normalize_hhmm)
    frame["trade_date"] = (
        frame["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    )
    frame["ts_code"] = frame["ts_code"].fillna(ts_code).astype(str)
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column not in frame.columns:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[
        frame["hhmm"].between(930, 1130) | frame["hhmm"].between(1300, 1500)
    ].copy()
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame = frame[
        frame["open"].gt(0)
        & frame["high"].gt(0)
        & frame["low"].gt(0)
        & frame["close"].gt(0)
        & frame["high"].ge(frame["low"])
    ].copy()
    return (
        frame[columns]
        .drop_duplicates(["ts_code", "trade_date", "hhmm"], keep="last")
        .sort_values("hhmm")
        .reset_index(drop=True)
    )


def infer_bar_minutes(bars: pd.DataFrame) -> int:
    if bars.empty:
        return 0
    values = sorted(set(int(value) for value in bars["hhmm"] if int(value) > 0))
    deltas: list[int] = []
    for left, right in zip(values, values[1:]):
        left_minutes = left // 100 * 60 + left % 100
        right_minutes = right // 100 * 60 + right % 100
        delta = right_minutes - left_minutes
        if 0 < delta <= 10:
            deltas.append(delta)
    return int(pd.Series(deltas).mode().iloc[0]) if deltas else 0


def coverage_status(bars: pd.DataFrame) -> dict[str, Any]:
    """判断分钟路径是否覆盖09:30起跟踪和14:55撤单边界。"""

    if bars.empty:
        return {
            "minute_status": "MISSING_MINUTE_DATA",
            "bar_minutes": 0,
            "bar_count": 0,
            "first_hhmm": 0,
            "last_hhmm": 0,
            "path_complete": False,
        }
    bar_minutes = infer_bar_minutes(bars)
    first_hhmm = int(bars["hhmm"].min())
    last_hhmm = int(bars["hhmm"].max())
    if bar_minutes == 1:
        expected_minimum = 230
        start_ok = first_hhmm <= 931
    elif bar_minutes == 5:
        expected_minimum = 46
        # BaoStock 5分钟为结束标签，第一根通常是09:35。
        start_ok = first_hhmm <= 935
    else:
        expected_minimum = 1
        start_ok = False
    path_complete = bool(
        start_ok
        and last_hhmm >= D_ORDER_CANCEL_HHMM
        and len(bars) >= expected_minimum
    )
    if not path_complete:
        status = "INCOMPLETE_MINUTE_PATH"
    elif bar_minutes == 1:
        status = "READY_1M_PATH_NO_QUEUE_DEPTH"
    elif bar_minutes == 5:
        status = "APPROXIMATE_5M_PATH_NO_QUEUE_DEPTH"
    else:
        status = "UNSUPPORTED_BAR_FREQUENCY"
    return {
        "minute_status": status,
        "bar_minutes": bar_minutes,
        "bar_count": int(len(bars)),
        "first_hhmm": first_hhmm,
        "last_hhmm": last_hhmm,
        "path_complete": path_complete,
    }


def daily_consistency_status(
    bars: pd.DataFrame,
    *,
    limit_price: float,
    daily_high: float,
    daily_close: float,
    price_tolerance: float = PRICE_TOLERANCE,
) -> dict[str, Any]:
    """交叉核对分钟K与母池日线。

    母池入选的必要事实是“当日价格到过交易所官方涨停价”。若另一
    数据源的完整分钟K连这一点都不能重现，必须单独标记数据源不一致，
    禁止把“48根bar齐全”写成“路径已认证”。
    """

    data = normalize_minute_bars(bars)
    if data.empty:
        return {
            "minute_max_high": None,
            "minute_last_close": None,
            "minute_confirms_daily_touch": False,
            "minute_daily_high_diff": None,
            "minute_daily_close_diff": None,
        }
    minute_max_high = float(data["high"].max())
    minute_last_close = float(data.sort_values("hhmm").iloc[-1]["close"])
    return {
        "minute_max_high": minute_max_high,
        "minute_last_close": minute_last_close,
        "minute_confirms_daily_touch": bool(
            limit_price > 0
            and minute_max_high >= float(limit_price) - float(price_tolerance)
        ),
        "minute_daily_high_diff": minute_max_high - float(daily_high),
        "minute_daily_close_diff": minute_last_close - float(daily_close),
    }


def replay_intraday_path(
    bars: pd.DataFrame,
    *,
    limit_price: float,
    policy: IntradayReplayPolicy | None = None,
) -> dict[str, Any]:
    """重建首次封板、炸板、回封、第一可交易信号和保守排队成交。"""

    policy = policy or IntradayReplayPolicy()
    data = normalize_minute_bars(bars)
    coverage = coverage_status(data)
    base: dict[str, Any] = {
        **coverage,
        "first_seal_hhmm": 0,
        "first_time_bucket": "unknown",
        "total_open_times": 0,
        "eligible_signal_hhmm": 0,
        "open_times_at_signal": 0,
        "signal_rule_current": False,
        "signal_rule_first_before_1400": False,
        "queue_fill_status": "NO_SIGNAL",
        "fill_hhmm": 0,
        "cancel_hhmm": policy.cancel_hhmm,
        "path_ambiguous": False,
        "ambiguous_bar_count": 0,
        "event_count": 0,
        "events": [],
    }
    if data.empty or limit_price <= 0:
        if limit_price <= 0:
            base["minute_status"] = "BAD_LIMIT_PRICE"
        return base

    tolerance = float(policy.price_tolerance)
    data = data.copy()
    data["at_limit_close"] = (data["close"] - limit_price).abs().le(tolerance)
    data["touched_limit"] = data["high"].ge(limit_price - tolerance)
    data["traded_below_limit"] = data["low"].lt(limit_price - tolerance)
    data["intrabar_path_ambiguous"] = data["touched_limit"] & data[
        "traded_below_limit"
    ]
    ambiguous_count = int(data["intrabar_path_ambiguous"].sum())
    events: list[dict[str, Any]] = []
    was_sealed = False
    ever_sealed = False
    open_times = 0
    first_seal = 0
    signal_hhmm = 0
    open_times_at_signal = 0
    signal_row_index = -1

    for index, row in data.iterrows():
        hhmm = int(row["hhmm"])
        at_limit = bool(row["at_limit_close"])
        if at_limit and not was_sealed:
            event_type = "FIRST_SEAL" if not ever_sealed else "RESEAL"
            events.append(
                {
                    "hhmm": hhmm,
                    "event_type": event_type,
                    "open_times": open_times,
                }
            )
            if not ever_sealed:
                ever_sealed = True
                first_seal = hhmm
            bucket = classify_first_time_bucket_hhmm(first_seal)
            if (
                signal_hhmm == 0
                and hhmm >= policy.signal_start_hhmm
                and hhmm < policy.cancel_hhmm
                and policy.min_open_times <= open_times <= policy.max_open_times
                and bucket in policy.allowed_first_time_buckets
            ):
                signal_hhmm = hhmm
                open_times_at_signal = open_times
                signal_row_index = int(index)
                events.append(
                    {
                        "hhmm": hhmm,
                        "event_type": "FIRST_ELIGIBLE_RESEAL_SIGNAL",
                        "open_times": open_times,
                    }
                )
        elif not at_limit and was_sealed:
            open_times += 1
            events.append(
                {
                    "hhmm": hhmm,
                    "event_type": "LIMIT_OPEN_BREAK",
                    "open_times": open_times,
                }
            )
        was_sealed = at_limit

    fill_status = "NO_SIGNAL"
    fill_hhmm = 0
    if signal_hhmm:
        after_signal = data.iloc[signal_row_index + 1 :].copy()
        after_signal = after_signal[after_signal["hhmm"].lt(policy.cancel_hhmm)]
        penetrated = after_signal["traded_below_limit"]
        if bool(penetrated.any()):
            first_fill = after_signal[penetrated].iloc[0]
            fill_hhmm = int(first_fill["hhmm"])
            fill_status = "CONFIRMED_FILL_PRICE_TRADED_BELOW_LIMIT"
            events.append(
                {
                    "hhmm": fill_hhmm,
                    "event_type": "QUEUE_FILL_CONFIRMED_BY_PRICE",
                    "open_times": open_times_at_signal,
                }
            )
        else:
            fill_status = "QUEUE_FILL_UNKNOWN_NO_DEPTH_CANCEL_1455"
            events.append(
                {
                    "hhmm": policy.cancel_hhmm,
                    "event_type": "CANCEL_UNVERIFIABLE_QUEUE_ORDER",
                    "open_times": open_times_at_signal,
                }
            )

    first_bucket = classify_first_time_bucket_hhmm(first_seal)
    base.update(
        {
            "first_seal_hhmm": first_seal,
            "first_time_bucket": first_bucket,
            "total_open_times": open_times,
            "eligible_signal_hhmm": signal_hhmm,
            "open_times_at_signal": open_times_at_signal,
            "signal_rule_current": bool(signal_hhmm),
            "signal_rule_first_before_1400": bool(signal_hhmm and first_seal < 1400),
            "queue_fill_status": fill_status,
            "fill_hhmm": fill_hhmm,
            "path_ambiguous": bool(ambiguous_count),
            "ambiguous_bar_count": ambiguous_count,
            "event_count": int(len(events)),
            "events": events,
        }
    )
    return base


def event_rows(
    replay: dict[str, Any], *, trade_date: str, ts_code: str
) -> list[dict[str, Any]]:
    return [
        {
            "trade_date": trade_date,
            "ts_code": ts_code,
            "event_sequence": index,
            **event,
        }
        for index, event in enumerate(replay.get("events", []), start=1)
    ]
