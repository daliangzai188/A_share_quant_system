"""策略D逐笔/队列严格重放的统一契约和认证闸门。

本模块不连接行情商，也不修改正式策略。它只接收已经标准化、且由数据供应方
声明完整的逐笔委托/成交事件。任何缺字段、缺交易所、缺交易日或只含分钟K的
输入都必须 fail-closed，不能进入D独立腿和ACDE组合复利比较。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import pandas as pd

from src.strategy_d_spec import (
    D_MIN_FILL_PROBABILITY,
    D_ORDER_CANCEL_HHMM,
    D_SIGNAL_START_HHMM,
    common_candidate_rejection_reason,
    d_rank_key,
    live_sentiment_is_historical_strong,
)


STANDARD_L2_EVENT_TYPES = frozenset(
    {"ORDER_ADD", "ORDER_CANCEL", "TRADE", "BOOK_SNAPSHOT"}
)
REQUIRED_L2_EVENT_COLUMNS = frozenset(
    {
        "trade_date",
        "ts_code",
        "event_time",
        "sequence",
        "event_type",
        "price",
        "volume",
        "side",
        "order_id",
    }
)
REQUIRED_L2_MANIFEST_COLUMNS = frozenset(
    {
        "trade_date",
        "exchange",
        "status",
        "full_market",
        "sequence_complete",
        "includes_orders",
        "includes_trades",
        "includes_snapshots",
        "coverage_start_hhmm",
        "coverage_end_hhmm",
        "volume_unit",
    }
)
REQUIRED_EXCHANGES = ("SSE", "SZSE", "BSE")
REQUIRED_D_SCAN_COLUMNS = frozenset(
    {
        "trade_date",
        "scan_id",
        "event_time",
        "ts_code",
        "limit_price",
        "last_price",
        "bid_volume_1",
        "circ_mv",
        "previous_day_limit_up",
        "historical_st",
        "market_segment",
        "fill_probability",
        "fill_reliable",
    }
)


@dataclass(frozen=True)
class StrictQueuePolicy:
    cancel_hhmm: int = D_ORDER_CANCEL_HHMM
    price_tolerance: float = 0.001


@dataclass
class DSnapshotState:
    ts_code: str
    limit_price: float
    circ_mv: float
    previous_day_limit_up: bool
    historical_st: bool
    market_segment: str
    was_sealed: bool = False
    ever_sealed: bool = False
    first_seal_hhmm: int = 0
    last_seal_hhmm: int = 0
    open_times: int = 0
    bid_volume_1: float = 0.0
    fill_probability: float = 0.0
    fill_reliable: bool = False


def as_bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def numeric_float(value: object, default: float = 0.0) -> float:
    number = pd.to_numeric(value, errors="coerce")
    return float(default) if pd.isna(number) else float(number)


def normalize_event_time(value: object) -> int:
    """统一为HHMMSSmmm整数；至少保留秒，供应方有毫秒时保留毫秒。"""

    if pd.isna(value):
        return 0
    text = str(value).strip().replace(".0", "")
    if not text:
        return 0
    if ":" in text:
        clock = text.split()[-1]
        main, _, fraction = clock.partition(".")
        parts = main.split(":")
        if len(parts) < 2:
            return 0
        try:
            hour = int(parts[0])
            minute = int(parts[1])
            second = int(parts[2]) if len(parts) > 2 else 0
            millis = int((fraction + "000")[:3]) if fraction else 0
            return ((hour * 10000 + minute * 100 + second) * 1000) + millis
        except ValueError:
            return 0
    digits = "".join(character for character in text if character.isdigit())
    # YYYYMMDDHHMMSSmmm / HHMMSSmmm / HHMMSS / HHMM
    if len(digits) >= 17:
        digits = digits[8:17]
    elif len(digits) >= 14:
        digits = digits[8:14] + "000"
    elif len(digits) == 6:
        digits += "000"
    elif len(digits) == 4:
        digits += "00000"
    elif len(digits) > 9:
        digits = digits[-9:]
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def event_hhmm(event_time_key: int) -> int:
    return int(event_time_key) // 100000 % 10000


def normalize_l2_events(events: pd.DataFrame) -> pd.DataFrame:
    missing = sorted(REQUIRED_L2_EVENT_COLUMNS - set(events.columns))
    if missing:
        raise ValueError(f"标准L2事件缺少字段：{missing}")
    frame = events.copy()
    frame["trade_date"] = frame["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    frame["ts_code"] = frame["ts_code"].astype(str)
    frame["event_time_key"] = frame["event_time"].map(normalize_event_time)
    frame["sequence"] = pd.to_numeric(frame["sequence"], errors="coerce")
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame["event_type"] = frame["event_type"].astype(str).str.upper().str.strip()
    frame["side"] = frame["side"].fillna("").astype(str).str.upper().str.strip()
    frame["order_id"] = frame["order_id"].fillna("").astype(str).str.strip()
    if frame["event_time_key"].le(0).any():
        raise ValueError("标准L2事件存在无法解析的event_time")
    if frame["sequence"].isna().any() or frame["sequence"].duplicated().any():
        raise ValueError("标准L2事件sequence缺失或重复")
    unsupported = sorted(set(frame["event_type"]) - STANDARD_L2_EVENT_TYPES)
    if unsupported:
        raise ValueError(f"标准L2事件含未知event_type：{unsupported}")
    quantity_events = frame["event_type"].isin({"ORDER_ADD", "ORDER_CANCEL", "TRADE"})
    if frame.loc[quantity_events, "volume"].isna().any() or frame.loc[quantity_events, "volume"].lt(0).any():
        raise ValueError("标准L2委托/成交事件volume缺失或为负")
    add_rows = frame["event_type"].eq("ORDER_ADD")
    if frame.loc[add_rows, "order_id"].eq("").any():
        raise ValueError("ORDER_ADD缺少order_id，无法重建价格时间优先队列")
    if frame.loc[add_rows, "order_id"].duplicated().any():
        raise ValueError("ORDER_ADD存在重复order_id，无法证明逐笔序列唯一")
    return frame.sort_values(["event_time_key", "sequence"]).reset_index(drop=True)


def strict_stream_metadata_reasons(
    metadata: Mapping[str, Any],
    *,
    signal_time_key: int,
    cancel_hhmm: int,
) -> list[str]:
    reasons: list[str] = []
    checks = {
        "full_stock_order_stream": "不是逐票完整委托流",
        "sequence_complete": "逐笔序列不完整",
        "includes_orders": "缺逐笔委托",
        "includes_trades": "缺逐笔成交",
    }
    for field, message in checks.items():
        if not as_bool(metadata.get(field, False)):
            reasons.append(message)
    if str(metadata.get("volume_unit", "")).upper() != "SHARE":
        reasons.append("成交量单位不是SHARE")
    start = int(metadata.get("coverage_start_hhmm", 0) or 0)
    end = int(metadata.get("coverage_end_hhmm", 0) or 0)
    if start > 930:
        reasons.append("逐笔流未覆盖09:30前队列建立")
    if end < int(cancel_hhmm):
        reasons.append("逐笔流未覆盖14:55撤单边界")
    if event_hhmm(signal_time_key) < 930 or event_hhmm(signal_time_key) >= int(cancel_hhmm):
        reasons.append("信号时间不在09:30~14:55")
    return reasons


def replay_price_time_queue(
    events: pd.DataFrame,
    *,
    limit_price: float,
    signal_time: object,
    order_quantity: int,
    metadata: Mapping[str, Any],
    policy: StrictQueuePolicy | None = None,
) -> dict[str, Any]:
    """按价格时间优先，重放涨停买一前方队列和14:55撤单。

    输入必须是单一股票、单一交易日的完整逐笔委托/成交流。历史分钟K、盘口
    快照或供应方声明不完整时直接返回``BLOCKED_INCOMPLETE_L2``。
    """

    policy = policy or StrictQueuePolicy()
    signal_key = normalize_event_time(signal_time)
    reasons = strict_stream_metadata_reasons(
        metadata, signal_time_key=signal_key, cancel_hhmm=policy.cancel_hhmm
    )
    if order_quantity <= 0 or int(order_quantity) % 100 != 0:
        reasons.append("模拟委托股数必须为正数且按100股取整")
    if limit_price <= 0:
        reasons.append("涨停价无效")
    try:
        data = normalize_l2_events(events)
    except ValueError as exc:
        reasons.append(str(exc))
        data = pd.DataFrame()
    if not data.empty and (data["trade_date"].nunique() != 1 or data["ts_code"].nunique() != 1):
        reasons.append("队列重放一次只能包含单一股票、单一交易日")
    if reasons:
        return {
            "certifiable": False,
            "status": "BLOCKED_INCOMPLETE_L2",
            "reasons": reasons,
            "order_quantity": int(max(order_quantity, 0)),
            "filled_quantity": 0,
            "cancelled_quantity": int(max(order_quantity, 0)),
            "fill_hhmm": 0,
        }

    queue: deque[list[Any]] = deque()
    order_lookup: dict[str, list[Any]] = {}
    virtual_id = "__D_VIRTUAL_ORDER__"
    virtual_added = False
    filled_quantity = 0
    fill_hhmm = 0
    integrity_errors: list[str] = []
    tolerance = float(policy.price_tolerance)

    def at_limit(value: object) -> bool:
        number = pd.to_numeric(value, errors="coerce")
        return bool(pd.notna(number) and abs(float(number) - float(limit_price)) <= tolerance)

    def add_order(order_id: str, volume: int) -> None:
        entry: list[Any] = [order_id, int(volume)]
        queue.append(entry)
        order_lookup[order_id] = entry

    def cancel_order(order_id: str, volume: int) -> None:
        entry = order_lookup.get(order_id)
        if entry is None:
            integrity_errors.append(f"撤单引用未知order_id={order_id}")
            return
        deduction = entry[1] if volume <= 0 else min(int(volume), int(entry[1]))
        entry[1] -= deduction
        if entry[1] <= 0:
            order_lookup.pop(order_id, None)

    def consume_trade(volume: int, hhmm: int) -> None:
        nonlocal filled_quantity, fill_hhmm
        remaining = int(volume)
        while remaining > 0:
            while queue and int(queue[0][1]) <= 0:
                queue.popleft()
            if not queue:
                # 完整流里仍可能出现市价买单/本档上方申报，不能据此凭空构造队列。
                return
            entry = queue[0]
            consumed = min(remaining, int(entry[1]))
            entry[1] -= consumed
            remaining -= consumed
            if entry[0] == virtual_id:
                filled_quantity += consumed
                if fill_hhmm == 0:
                    fill_hhmm = hhmm
            if int(entry[1]) <= 0:
                queue.popleft()
                order_lookup.pop(str(entry[0]), None)

    cancel_key = normalize_event_time(f"{policy.cancel_hhmm:04d}00")
    for row in data.itertuples(index=False):
        event_key = int(row.event_time_key)
        # 信号由该时点的回封/盘口事件触发，虚拟委托必须排在同一时点原始
        # 事件之后；否则会把本应排在前面的真实封单错放到虚拟单后面。
        if not virtual_added and event_key > signal_key:
            add_order(virtual_id, int(order_quantity))
            virtual_added = True
        if event_key >= cancel_key:
            break
        event_type = str(row.event_type)
        price_matches = at_limit(row.price)
        if event_type == "ORDER_ADD" and str(row.side) == "BUY" and price_matches:
            add_order(str(row.order_id), int(row.volume))
        elif (
            event_type == "ORDER_CANCEL"
            and str(row.order_id)
            and (str(row.order_id) in order_lookup or price_matches)
        ):
            cancel_order(str(row.order_id), int(row.volume))
        elif event_type == "TRADE" and price_matches:
            consume_trade(int(row.volume), event_hhmm(event_key))
        elif event_type == "TRADE" and virtual_added and float(row.price) < limit_price - tolerance:
            # 涨停限价买单仍有效时价格向下穿越，剩余量按保守可成交确认。
            remaining = int(order_quantity) - filled_quantity
            if remaining > 0:
                filled_quantity += remaining
                if fill_hhmm == 0:
                    fill_hhmm = event_hhmm(event_key)
                entry = order_lookup.get(virtual_id)
                if entry is not None:
                    entry[1] = 0
                    order_lookup.pop(virtual_id, None)

    if not virtual_added and signal_key < cancel_key:
        # 单票信号后可能没有任何成交事件；完整日清单已证明覆盖到14:55时，
        # 仍应把虚拟单视为已挂入但零成交，并在14:55撤销。
        add_order(virtual_id, int(order_quantity))
        virtual_added = True
    if not virtual_added:
        reasons.append("逐笔事件没有覆盖到信号时点")
    if integrity_errors:
        reasons.extend(sorted(set(integrity_errors))[:20])
    if reasons:
        return {
            "certifiable": False,
            "status": "BLOCKED_INTEGRITY_ERROR",
            "reasons": reasons,
            "order_quantity": int(order_quantity),
            "filled_quantity": 0,
            "cancelled_quantity": int(order_quantity),
            "fill_hhmm": 0,
        }
    cancelled = max(int(order_quantity) - int(filled_quantity), 0)
    if filled_quantity >= int(order_quantity):
        status = "STRICT_FULL_FILL_BEFORE_1455"
    elif filled_quantity > 0:
        status = "STRICT_PARTIAL_FILL_CANCEL_REMAINDER_1455"
    else:
        status = "STRICT_NO_FILL_CANCEL_1455"
    return {
        "certifiable": True,
        "status": status,
        "reasons": [],
        "order_quantity": int(order_quantity),
        "filled_quantity": int(filled_quantity),
        "cancelled_quantity": int(cancelled),
        "fill_hhmm": int(fill_hhmm),
        "cancel_hhmm": int(policy.cancel_hhmm),
    }


def strict_l2_manifest_gate(
    manifest: pd.DataFrame,
    *,
    required_open_dates: Sequence[str],
    required_exchanges: Sequence[str] = REQUIRED_EXCHANGES,
) -> dict[str, Any]:
    """检查全窗口、全交易所、全市场L2日文件是否齐备。"""

    expected = {
        (str(date), str(exchange).upper())
        for date in required_open_dates
        for exchange in required_exchanges
    }
    if manifest is None or manifest.empty:
        return {
            "passed": False,
            "expected_file_count": len(expected),
            "complete_file_count": 0,
            "missing_file_count": len(expected),
            "missing_examples": [f"{date}|{exchange}" for date, exchange in sorted(expected)[:20]],
            "invalid_file_count": 0,
            "invalid_examples": [],
        }
    missing_columns = sorted(REQUIRED_L2_MANIFEST_COLUMNS - set(manifest.columns))
    if missing_columns:
        return {
            "passed": False,
            "expected_file_count": len(expected),
            "complete_file_count": 0,
            "missing_file_count": len(expected),
            "missing_examples": [],
            "invalid_file_count": int(len(manifest)),
            "invalid_examples": [f"MANIFEST_MISSING_COLUMNS:{','.join(missing_columns)}"],
        }
    frame = manifest.copy()
    frame["trade_date"] = frame["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    frame["exchange"] = frame["exchange"].astype(str).str.upper()
    boolean_columns = [
        "full_market", "sequence_complete", "includes_orders", "includes_trades", "includes_snapshots"
    ]
    truth = lambda series: series.astype(str).str.lower().isin({"true", "1", "yes"})
    valid = frame["status"].astype(str).str.upper().eq("COMPLETE")
    for column in boolean_columns:
        valid &= truth(frame[column])
    valid &= pd.to_numeric(frame["coverage_start_hhmm"], errors="coerce").le(930)
    valid &= pd.to_numeric(frame["coverage_end_hhmm"], errors="coerce").ge(D_ORDER_CANCEL_HHMM)
    valid &= frame["volume_unit"].astype(str).str.upper().eq("SHARE")
    frame["_key"] = list(zip(frame["trade_date"], frame["exchange"]))
    valid_keys = set(frame.loc[valid, "_key"])
    present_keys = set(frame["_key"])
    missing = sorted(expected - present_keys)
    invalid = sorted((expected & present_keys) - valid_keys)
    return {
        "passed": bool(not missing and not invalid and expected <= valid_keys),
        "expected_file_count": len(expected),
        "complete_file_count": len(expected & valid_keys),
        "missing_file_count": len(missing),
        "missing_examples": [f"{date}|{exchange}" for date, exchange in missing[:20]],
        "invalid_file_count": len(invalid),
        "invalid_examples": [f"{date}|{exchange}" for date, exchange in invalid[:20]],
    }


def replay_synchronized_d_scans(
    scans: pd.DataFrame,
    *,
    coverage_metadata: Mapping[str, Any],
    sentiment_minimum: int = 88,
    sentiment_maximum: int = 132,
    allowed_segments: set[str] | None = None,
    min_fill_probability: float = D_MIN_FILL_PROBABILITY,
    price_tolerance: float = 0.015,
) -> dict[str, Any]:
    """按实盘轮询顺序重建当前封板情绪、D候选和同日排序第一名。

    ``scans``不是普通逐笔表，而是由完整L2日文件按实盘扫描频率生成的同步
    全市场快照。每个``scan_id``必须包含完全相同的股票宇宙；缺一只即整日
    fail-closed，防止在部分行情上低估或高估当前封板数。
    """

    missing = sorted(REQUIRED_D_SCAN_COLUMNS - set(scans.columns))
    reasons: list[str] = []
    if missing:
        reasons.append(f"同步D快照缺少字段：{missing}")
    required_truth = {
        "full_market": "不是全市场同步快照",
        "sequence_complete": "底层逐笔序列不完整",
        "includes_snapshots": "缺盘口快照",
        "includes_orders": "缺逐笔委托，无法继续做排队认证",
        "includes_trades": "缺逐笔成交，无法继续做排队认证",
    }
    for field, message in required_truth.items():
        if not as_bool(coverage_metadata.get(field, False)):
            reasons.append(message)
    if int(coverage_metadata.get("coverage_start_hhmm", 0) or 0) > 930:
        reasons.append("同步快照未从09:30开始")
    if int(coverage_metadata.get("coverage_end_hhmm", 0) or 0) < D_ORDER_CANCEL_HHMM:
        reasons.append("同步快照未覆盖14:55")
    if reasons:
        return {
            "certifiable": False,
            "status": "BLOCKED_INCOMPLETE_SYNCHRONIZED_MARKET_SCANS",
            "reasons": reasons,
            "signals": [],
            "scan_audit": [],
        }

    data = scans.copy()
    data["trade_date"] = data["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    data["ts_code"] = data["ts_code"].astype(str)
    data["event_time_key"] = data["event_time"].map(normalize_event_time)
    if data["event_time_key"].le(0).any():
        reasons.append("同步快照存在无法解析的event_time")
    if data["trade_date"].nunique() != 1:
        reasons.append("同步快照一次只能重放一个交易日")
    if data.duplicated(["scan_id", "ts_code"]).any():
        reasons.append("同步快照同一scan_id出现重复股票")
    scan_universes = [
        frozenset(group["ts_code"])
        for _, group in data.groupby("scan_id", sort=False)
    ]
    expected_universe = scan_universes[0] if scan_universes else frozenset()
    if not expected_universe:
        reasons.append("同步快照股票宇宙为空")
    if any(universe != expected_universe for universe in scan_universes):
        reasons.append("不同scan_id的股票宇宙不一致")
    declared_size = int(coverage_metadata.get("universe_size", 0) or 0)
    if declared_size and declared_size != len(expected_universe):
        reasons.append(
            f"同步快照宇宙数量与声明不一致：scan={len(expected_universe)} declared={declared_size}"
        )
    if reasons:
        return {
            "certifiable": False,
            "status": "BLOCKED_INCOMPLETE_SYNCHRONIZED_MARKET_SCANS",
            "reasons": reasons,
            "signals": [],
            "scan_audit": [],
        }

    allowed = allowed_segments or {"sh_main", "sz_main", "chi_next", "star", "bj"}
    first_scan = (
        data.sort_values(["event_time_key", "scan_id"])
        .groupby("ts_code", sort=False)
        .first()
    )
    states: dict[str, DSnapshotState] = {}
    for ts_code, row in first_scan.iterrows():
        states[str(ts_code)] = DSnapshotState(
            ts_code=str(ts_code),
            limit_price=numeric_float(row["limit_price"]),
            circ_mv=numeric_float(row["circ_mv"]),
            previous_day_limit_up=as_bool(row["previous_day_limit_up"]),
            historical_st=as_bool(row["historical_st"]),
            market_segment=str(row["market_segment"]),
        )

    signals: list[dict[str, Any]] = []
    scan_audit: list[dict[str, Any]] = []
    ordered = data.sort_values(["event_time_key", "scan_id", "ts_code"])
    for scan_id, group in ordered.groupby("scan_id", sort=False):
        event_keys = set(int(value) for value in group["event_time_key"])
        if len(event_keys) != 1:
            return {
                "certifiable": False,
                "status": "BLOCKED_INCOMPLETE_SYNCHRONIZED_MARKET_SCANS",
                "reasons": [f"scan_id={scan_id}内event_time不一致"],
                "signals": [],
                "scan_audit": scan_audit,
            }
        event_key = next(iter(event_keys))
        hhmm = event_hhmm(event_key)
        for row in group.itertuples(index=False):
            state = states[str(row.ts_code)]
            limit_price = numeric_float(row.limit_price)
            last_price = numeric_float(row.last_price)
            if limit_price > 0:
                state.limit_price = limit_price
            at_limit = bool(
                state.limit_price > 0
                and last_price > 0
                and abs(last_price - state.limit_price) < float(price_tolerance)
            )
            if at_limit:
                if not state.ever_sealed:
                    state.ever_sealed = True
                    state.first_seal_hhmm = hhmm
                if not state.was_sealed:
                    state.last_seal_hhmm = hhmm
                state.was_sealed = True
                state.bid_volume_1 = numeric_float(row.bid_volume_1)
            else:
                if state.was_sealed:
                    state.open_times += 1
                state.was_sealed = False
                state.bid_volume_1 = 0.0
            state.fill_probability = numeric_float(row.fill_probability)
            state.fill_reliable = as_bool(row.fill_reliable)

        sealed_count = sum(1 for state in states.values() if state.was_sealed)
        candidates: list[DSnapshotState] = []
        if hhmm >= D_SIGNAL_START_HHMM and live_sentiment_is_historical_strong(
            sealed_count,
            minimum=sentiment_minimum,
            maximum=sentiment_maximum,
        ):
            for state in states.values():
                if (
                    state.previous_day_limit_up
                    or state.historical_st
                    or state.market_segment not in allowed
                    or not state.was_sealed
                    or state.circ_mv <= 0
                    or not state.fill_reliable
                    or state.fill_probability < float(min_fill_probability)
                ):
                    continue
                reason = common_candidate_rejection_reason(
                    open_times=state.open_times,
                    first_seal_hhmm=state.first_seal_hhmm,
                    last_seal_hhmm=state.last_seal_hhmm,
                    require_tail_reseal=True,
                )
                if not reason:
                    candidates.append(state)
        ranked = sorted(
            candidates,
            key=lambda state: d_rank_key(
                open_times=state.open_times,
                fd_amount_to_circ_mv=(
                    state.bid_volume_1
                    * state.limit_price
                    / (state.circ_mv * 10000.0)
                ),
                ts_code=state.ts_code,
            ),
            reverse=True,
        )
        scan_audit.append(
            {
                "scan_id": str(scan_id),
                "hhmm": hhmm,
                "sealed_count": sealed_count,
                "candidate_count": len(ranked),
                "candidate_codes": [state.ts_code for state in ranked],
            }
        )
        if ranked and not signals:
            selected = ranked[0]
            fd_ratio = selected.bid_volume_1 * selected.limit_price / (
                selected.circ_mv * 10000.0
            )
            signals.append(
                {
                    "trade_date": str(group.iloc[0]["trade_date"]),
                    "signal_event_time_key": event_key,
                    "signal_hhmm": hhmm,
                    "ts_code": selected.ts_code,
                    "sealed_count": sealed_count,
                    "open_times": selected.open_times,
                    "first_seal_hhmm": selected.first_seal_hhmm,
                    "last_seal_hhmm": selected.last_seal_hhmm,
                    "bid_volume_1": selected.bid_volume_1,
                    "fd_amount_to_circ_mv": fd_ratio,
                    "fill_probability": selected.fill_probability,
                    "ranked_candidate_codes": [state.ts_code for state in ranked],
                }
            )
    return {
        "certifiable": True,
        "status": "STRICT_SYNCHRONIZED_MARKET_REPLAY_COMPLETE",
        "reasons": [],
        "signals": signals,
        "scan_audit": scan_audit,
        "universe_size": len(expected_universe),
    }
