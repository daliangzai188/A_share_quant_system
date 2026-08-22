#!/usr/bin/env python3
"""逐派研究盘中首板D2、D4、D5，并与当前D6做真实占仓合并。

本脚本只使用正式两年窗口 ``2024-06-30~2026-06-30`` 的完整首板触板
一分钟事件账本。D1需要09:24以前的历史竞价过程字段，D3需要补齐所有达到
7%~9%但最终没有触板的失败分母，二者不能用当前触板母池伪造。本脚本因此
只对当前数据已经严格覆盖的三派执行收益研究：

* D2：09:30~10:00第一次封板时挂涨停价；
* D4：全天第一次封板时挂涨停价；
* D5：第一次炸板后，下一分钟可成交买入或预挂固定折价限价单。

每条规则依次计算：Dx独立腿、Dx优先且D6兜底的合并D、冻结A/E/C后的ACDE。
只有Dx独立复利大于1、样本不少于20笔、合并D复利高于当前D6、ACDE复利
高于当前正式基线，才标记为可保留研究候选。脚本不会修改正式策略配置。

运行：

    python3 scripts/research_strategy_d_six_schools.py
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import itertools
import json
import logging
from pathlib import Path
import sys
from typing import Any, Callable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_other_live_strategies_strict as strict  # noqa: E402
from scripts.build_strategy_d_intraday_event_ledger import iter_minute_groups  # noqa: E402
from scripts.research_strategy_d_explosion_features import (  # noqa: E402
    build_current_other_legs,
    combo_replay,
    executed_metrics,
    replay_d_only,
)
from scripts.research_strategy_d_full_window_features_and_gates import (  # noqa: E402
    BASELINE_ACDE_LEG_COUNTS,
    BASELINE_ACDE_MULTIPLE,
    BASELINE_ACDE_TRADE_COUNT,
    BASELINE_D_MULTIPLE,
    BASELINE_D_TRADE_COUNT,
    END,
    START,
    TOLERANCE,
    assert_formal_baseline,
    load_ledger,
    trading_minutes_between,
)
from src.strategy_d_intraday_ledger import (  # noqa: E402
    PRICE_TOLERANCE,
    normalize_minute_bars,
)
from src.strict_asof import STRICT_DISCOVERY  # noqa: E402


LOGGER = logging.getLogger("research_strategy_d_six_schools")

LEDGER_PATH = ROOT / "data/research/strategy_d_intraday/event_ledger_full_window.csv"
MINUTE_PATH = ROOT / "data/research/strategy_d_intraday/minute_1m_tushare.csv"
OUTPUT_DIR = ROOT / "reports/strategy_d_six_schools"
MIN_RETAIN_TRADE_COUNT = 20
FIRST_12M_END = "20250630"
SECOND_12M_START = "20250701"

SIGNAL_FIELDS = frozenset(
    {
        "signal_hhmm",
        "first_seal_hhmm",
        "first_break_hhmm",
        "open_gap_pct",
        "signal_return_from_open",
        "signal_return_from_preclose",
        "pre_signal_min_return",
        "signal_cumulative_amount_vs_prev_day",
        "signal_recent_5m_amount_vs_prev_day",
        "signal_limit_close_share",
        "break_close_depth_pct",
        "break_low_depth_pct",
        "first_seal_to_break_minutes",
        "market_ever_sealed_count",
        "same_segment_ever_sealed_count",
        "market_first_break_count",
        "market_first_break_rate",
        "market_segment",
    }
)

AFTER_SIGNAL_FIELDS = frozenset(
    {
        "queue_price_confirmed",
        "queue_fill_hhmm",
        "post_signal_min_low",
        "next_minute_entry_price",
        "next_minute_hhmm",
        "daily_close",
        "closed_at_limit",
        "account_return",
        "exit_date",
    }
)


@dataclass(frozen=True)
class StyleRule:
    style: str
    name: str
    description: str
    fields: tuple[str, ...]
    entry_model: str
    predicate: Callable[[pd.DataFrame], pd.Series]
    bid_discount: float = 0.0


def numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _all(frame: pd.DataFrame) -> pd.Series:
    return pd.Series(True, index=frame.index)


def _between(column: str, low: float, high: float) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: numeric(frame[column]).between(low, high)


def _ge(column: str, value: float) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: numeric(frame[column]).ge(value)


def _le(column: str, value: float) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: numeric(frame[column]).le(value)


def _segment(values: set[str]) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: frame["market_segment"].astype(str).isin(values)


def _and(*predicates: Callable[[pd.DataFrame], pd.Series]) -> Callable[[pd.DataFrame], pd.Series]:
    def predicate(frame: pd.DataFrame) -> pd.Series:
        result = pd.Series(True, index=frame.index)
        for item in predicates:
            result &= item(frame).fillna(False)
        return result

    return predicate


def rules() -> list[StyleRule]:
    """冻结结构化规则网格；所有筛选字段都必须在下单时点已经知道。"""

    d2 = [
        StyleRule("D2", "d2_all", "09:30~10:00全部开盘冲板", tuple(), "QUEUE_LIMIT", _all),
        StyleRule("D2", "d2_0930_0935", "09:30~09:35首封", ("signal_hhmm",), "QUEUE_LIMIT", _between("signal_hhmm", 930, 935)),
        StyleRule("D2", "d2_0936_0945", "09:36~09:45首封", ("signal_hhmm",), "QUEUE_LIMIT", _between("signal_hhmm", 936, 945)),
        StyleRule("D2", "d2_0946_1000", "09:46~10:00首封", ("signal_hhmm",), "QUEUE_LIMIT", _between("signal_hhmm", 946, 1000)),
        StyleRule("D2", "d2_gap_le_2", "开盘涨幅不超过2%", ("open_gap_pct",), "QUEUE_LIMIT", _le("open_gap_pct", 0.02)),
        StyleRule("D2", "d2_gap_2_5", "开盘涨幅2%~5%", ("open_gap_pct",), "QUEUE_LIMIT", _between("open_gap_pct", 0.02, 0.05)),
        StyleRule("D2", "d2_gap_5_9", "开盘涨幅5%~9%", ("open_gap_pct",), "QUEUE_LIMIT", _between("open_gap_pct", 0.05, 0.09)),
        StyleRule("D2", "d2_amount_ge_5pct", "首封前成交额至少前日5%", ("signal_cumulative_amount_vs_prev_day",), "QUEUE_LIMIT", _ge("signal_cumulative_amount_vs_prev_day", 0.05)),
        StyleRule("D2", "d2_amount_ge_10pct", "首封前成交额至少前日10%", ("signal_cumulative_amount_vs_prev_day",), "QUEUE_LIMIT", _ge("signal_cumulative_amount_vs_prev_day", 0.10)),
        StyleRule("D2", "d2_amount_ge_20pct", "首封前成交额至少前日20%", ("signal_cumulative_amount_vs_prev_day",), "QUEUE_LIMIT", _ge("signal_cumulative_amount_vs_prev_day", 0.20)),
        StyleRule("D2", "d2_main_board", "仅沪深主板", ("market_segment",), "QUEUE_LIMIT", _segment({"sh_main", "sz_main"})),
        StyleRule("D2", "d2_growth_board", "仅创业板或科创板", ("market_segment",), "QUEUE_LIMIT", _segment({"chi_next", "star"})),
        StyleRule("D2", "d2_late_amount", "09:46后且成交额至少前日10%", ("signal_hhmm", "signal_cumulative_amount_vs_prev_day"), "QUEUE_LIMIT", _and(_between("signal_hhmm", 946, 1000), _ge("signal_cumulative_amount_vs_prev_day", 0.10))),
        StyleRule("D2", "d2_gap_le5_amount", "开盘不超5%且成交额至少前日10%", ("open_gap_pct", "signal_cumulative_amount_vs_prev_day"), "QUEUE_LIMIT", _and(_le("open_gap_pct", 0.05), _ge("signal_cumulative_amount_vs_prev_day", 0.10))),
    ]

    d4 = [
        StyleRule("D4", "d4_all", "全天第一次封板挂单", tuple(), "QUEUE_LIMIT", _all),
        StyleRule("D4", "d4_1001_1030", "10:01~10:30首封", ("signal_hhmm",), "QUEUE_LIMIT", _between("signal_hhmm", 1001, 1030)),
        StyleRule("D4", "d4_1031_1100", "10:31~11:00首封", ("signal_hhmm",), "QUEUE_LIMIT", _between("signal_hhmm", 1031, 1100)),
        StyleRule("D4", "d4_1101_1130", "11:01~11:30首封", ("signal_hhmm",), "QUEUE_LIMIT", _between("signal_hhmm", 1101, 1130)),
        StyleRule("D4", "d4_1300_1330", "13:00~13:30首封", ("signal_hhmm",), "QUEUE_LIMIT", _between("signal_hhmm", 1300, 1330)),
        StyleRule("D4", "d4_1331_1400", "13:31~14:00首封", ("signal_hhmm",), "QUEUE_LIMIT", _between("signal_hhmm", 1331, 1400)),
        StyleRule("D4", "d4_1401_1430", "14:01~14:30首封", ("signal_hhmm",), "QUEUE_LIMIT", _between("signal_hhmm", 1401, 1430)),
        StyleRule("D4", "d4_1431_1454", "14:31~14:54首封", ("signal_hhmm",), "QUEUE_LIMIT", _between("signal_hhmm", 1431, 1454)),
        StyleRule("D4", "d4_first_before_1100", "11:00前首封", ("signal_hhmm",), "QUEUE_LIMIT", _le("signal_hhmm", 1100)),
        StyleRule("D4", "d4_first_1101_1400", "11:01~14:00首封", ("signal_hhmm",), "QUEUE_LIMIT", _between("signal_hhmm", 1101, 1400)),
        StyleRule("D4", "d4_first_after_1400", "14:00后首封", ("signal_hhmm",), "QUEUE_LIMIT", _between("signal_hhmm", 1401, 1454)),
        StyleRule("D4", "d4_amount_ge_50pct", "首封前成交额至少前日50%", ("signal_cumulative_amount_vs_prev_day",), "QUEUE_LIMIT", _ge("signal_cumulative_amount_vs_prev_day", 0.50)),
        StyleRule("D4", "d4_amount_ge_100pct", "首封前成交额至少前日100%", ("signal_cumulative_amount_vs_prev_day",), "QUEUE_LIMIT", _ge("signal_cumulative_amount_vs_prev_day", 1.00)),
        StyleRule("D4", "d4_pre_low_ge_0", "首封前最低价不低于昨收", ("pre_signal_min_return",), "QUEUE_LIMIT", _ge("pre_signal_min_return", 0.0)),
        StyleRule("D4", "d4_gap_le_5", "开盘涨幅不超过5%", ("open_gap_pct",), "QUEUE_LIMIT", _le("open_gap_pct", 0.05)),
        StyleRule("D4", "d4_main_board", "仅沪深主板", ("market_segment",), "QUEUE_LIMIT", _segment({"sh_main", "sz_main"})),
        StyleRule("D4", "d4_growth_board", "仅创业板或科创板", ("market_segment",), "QUEUE_LIMIT", _segment({"chi_next", "star"})),
        StyleRule("D4", "d4_market_ever_20_60", "信号时全市场已首封20~60只", ("market_ever_sealed_count",), "QUEUE_LIMIT", _between("market_ever_sealed_count", 20, 60)),
        StyleRule("D4", "d4_market_ever_61_120", "信号时全市场已首封61~120只", ("market_ever_sealed_count",), "QUEUE_LIMIT", _between("market_ever_sealed_count", 61, 120)),
        StyleRule("D4", "d4_market_break_rate_le_20", "信号时首板首次炸板率不超20%", ("market_first_break_rate",), "QUEUE_LIMIT", _le("market_first_break_rate", 0.20)),
        StyleRule("D4", "d4_midday_amount", "11:01~14:00且成交额至少前日50%", ("signal_hhmm", "signal_cumulative_amount_vs_prev_day"), "QUEUE_LIMIT", _and(_between("signal_hhmm", 1101, 1400), _ge("signal_cumulative_amount_vs_prev_day", 0.50))),
        StyleRule("D4", "d4_late_amount", "14:00后且成交额至少前日100%", ("signal_hhmm", "signal_cumulative_amount_vs_prev_day"), "QUEUE_LIMIT", _and(_between("signal_hhmm", 1401, 1454), _ge("signal_cumulative_amount_vs_prev_day", 1.00))),
    ]

    d5 = [
        StyleRule("D5", "d5_next_all", "首次炸板后下一分钟可成交买入", tuple(), "NEXT_MINUTE", _all),
        StyleRule("D5", "d5_bid_0_5_all", "首次炸板后预挂涨停价下0.5%", tuple(), "FIXED_BID", _all, 0.005),
        StyleRule("D5", "d5_bid_1_all", "首次炸板后预挂涨停价下1%", tuple(), "FIXED_BID", _all, 0.010),
        StyleRule("D5", "d5_bid_2_all", "首次炸板后预挂涨停价下2%", tuple(), "FIXED_BID", _all, 0.020),
        StyleRule("D5", "d5_bid_3_all", "首次炸板后预挂涨停价下3%", tuple(), "FIXED_BID", _all, 0.030),
        StyleRule("D5", "d5_break_before_1000", "10:00前首次炸板", ("signal_hhmm",), "NEXT_MINUTE", _between("signal_hhmm", 930, 1000)),
        StyleRule("D5", "d5_break_1001_1130", "10:01~11:30首次炸板", ("signal_hhmm",), "NEXT_MINUTE", _between("signal_hhmm", 1001, 1130)),
        StyleRule("D5", "d5_break_1300_1400", "13:00~14:00首次炸板", ("signal_hhmm",), "NEXT_MINUTE", _between("signal_hhmm", 1300, 1400)),
        StyleRule("D5", "d5_break_1401_1454", "14:01~14:54首次炸板", ("signal_hhmm",), "NEXT_MINUTE", _between("signal_hhmm", 1401, 1454)),
        StyleRule("D5", "d5_depth_close_le_0_5", "炸板分钟收盘回落不超过0.5%", ("break_close_depth_pct",), "NEXT_MINUTE", _le("break_close_depth_pct", 0.005)),
        StyleRule("D5", "d5_depth_close_0_5_1", "炸板分钟收盘回落0.5%~1%", ("break_close_depth_pct",), "NEXT_MINUTE", _between("break_close_depth_pct", 0.005, 0.010)),
        StyleRule("D5", "d5_depth_close_1_2", "炸板分钟收盘回落1%~2%", ("break_close_depth_pct",), "NEXT_MINUTE", _between("break_close_depth_pct", 0.010, 0.020)),
        StyleRule("D5", "d5_depth_close_ge_2", "炸板分钟收盘回落至少2%", ("break_close_depth_pct",), "NEXT_MINUTE", _ge("break_close_depth_pct", 0.020)),
        StyleRule("D5", "d5_hold_le_5m", "封板5分钟内即炸", ("first_seal_to_break_minutes",), "NEXT_MINUTE", _le("first_seal_to_break_minutes", 5)),
        StyleRule("D5", "d5_hold_6_30m", "封板6~30分钟后炸", ("first_seal_to_break_minutes",), "NEXT_MINUTE", _between("first_seal_to_break_minutes", 6, 30)),
        StyleRule("D5", "d5_hold_31_120m", "封板31~120分钟后炸", ("first_seal_to_break_minutes",), "NEXT_MINUTE", _between("first_seal_to_break_minutes", 31, 120)),
        StyleRule("D5", "d5_first_before_1000", "10:00前首封后首次炸板", ("first_seal_hhmm",), "NEXT_MINUTE", _between("first_seal_hhmm", 930, 1000)),
        StyleRule("D5", "d5_first_1001_1400", "10:01~14:00首封后首次炸板", ("first_seal_hhmm",), "NEXT_MINUTE", _between("first_seal_hhmm", 1001, 1400)),
        StyleRule("D5", "d5_first_after_1400", "14:00后首封后首次炸板", ("first_seal_hhmm",), "NEXT_MINUTE", _between("first_seal_hhmm", 1401, 1454)),
        StyleRule("D5", "d5_amount_ge_100pct", "炸板时成交额至少前日100%", ("signal_cumulative_amount_vs_prev_day",), "NEXT_MINUTE", _ge("signal_cumulative_amount_vs_prev_day", 1.00)),
        StyleRule("D5", "d5_main_board", "仅沪深主板", ("market_segment",), "NEXT_MINUTE", _segment({"sh_main", "sz_main"})),
        StyleRule("D5", "d5_growth_board", "仅创业板或科创板", ("market_segment",), "NEXT_MINUTE", _segment({"chi_next", "star"})),
        StyleRule("D5", "d5_market_break_rate_le_30", "信号时首板首次炸板率不超30%", ("market_first_break_rate",), "NEXT_MINUTE", _le("market_first_break_rate", 0.30)),
        StyleRule("D5", "d5_bid_1_hold_6_30", "封板6~30分钟后炸并低挂1%", ("first_seal_to_break_minutes",), "FIXED_BID", _between("first_seal_to_break_minutes", 6, 30), 0.010),
        StyleRule("D5", "d5_bid_2_depth_ge_1", "炸板收盘回落至少1%并低挂2%", ("break_close_depth_pct",), "FIXED_BID", _ge("break_close_depth_pct", 0.010), 0.020),
        StyleRule("D5", "d5_bid_1_midday", "10:01~14:00炸板并低挂1%", ("signal_hhmm",), "FIXED_BID", _between("signal_hhmm", 1001, 1400), 0.010),
        StyleRule("D5", "d5_bid_1_amount", "炸板时成交额至少前日100%并低挂1%", ("signal_cumulative_amount_vs_prev_day",), "FIXED_BID", _ge("signal_cumulative_amount_vs_prev_day", 1.00), 0.010),
    ]

    result = d2 + d4 + d5
    for rule in result:
        unknown = set(rule.fields) - SIGNAL_FIELDS
        forbidden = set(rule.fields) & AFTER_SIGNAL_FIELDS
        if unknown or forbidden:
            raise ValueError(
                f"规则{rule.name}使用非法字段：unknown={sorted(unknown)} "
                f"after_signal={sorted(forbidden)}"
            )
    return result


def signal_features(
    bars: pd.DataFrame,
    *,
    signal_index: int,
    pre_close: float,
    previous_day_amount_yuan: float,
    limit_price: float,
) -> dict[str, float]:
    before = bars.iloc[: signal_index + 1]
    recent = before.tail(5)
    amount = numeric(before["amount"]).fillna(0.0)
    recent_amount = numeric(recent["amount"]).fillna(0.0)
    signal_close = float(before.iloc[-1]["close"])
    open_price = float(before.iloc[0]["open"])
    at_limit = (numeric(before["close"]) - limit_price).abs().le(PRICE_TOLERANCE)
    return {
        "open_gap_pct": open_price / pre_close - 1.0 if pre_close > 0 else np.nan,
        "signal_return_from_open": signal_close / open_price - 1.0 if open_price > 0 else np.nan,
        "signal_return_from_preclose": signal_close / pre_close - 1.0 if pre_close > 0 else np.nan,
        "pre_signal_min_return": float(numeric(before["low"]).min()) / pre_close - 1.0 if pre_close > 0 else np.nan,
        "signal_cumulative_amount_vs_prev_day": float(amount.sum()) / previous_day_amount_yuan if previous_day_amount_yuan > 0 else np.nan,
        "signal_recent_5m_amount_vs_prev_day": float(recent_amount.sum()) / previous_day_amount_yuan if previous_day_amount_yuan > 0 else np.nan,
        "signal_limit_close_share": float(at_limit.mean()),
    }


def extract_style_events(
    ledger: pd.DataFrame,
    minute_path: Path,
    daily_data: strict.DailyData,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """流式扫描一次千万行分钟文件，生成首封和首次炸板信号时点特征。"""

    lookup = ledger.set_index(["trade_date", "ts_code"], drop=False)
    first_rows: list[dict[str, Any]] = []
    break_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    skipped_status = 0
    for counter, (key, raw) in enumerate(iter_minute_groups(minute_path), 1):
        if key not in lookup.index:
            raise RuntimeError(f"分钟股票日不在冻结账本：{key}")
        seen.add(key)
        meta = lookup.loc[key]
        if isinstance(meta, pd.DataFrame):
            raise RuntimeError(f"冻结账本日期+代码重复：{key}")
        if str(meta["minute_status"]) != "READY_1M_PATH_NO_QUEUE_DEPTH":
            skipped_status += 1
            continue
        bars = normalize_minute_bars(raw, ts_code=key[1], trade_date=key[0])
        if len(bars) != 241:
            raise RuntimeError(f"完整一分钟路径不是241根：{key} rows={len(bars)}")
        limit_price = float(meta["limit_price"])
        pre_close = float(meta["pre_close"])
        previous_date = str(meta["previous_trade_date"])
        previous = daily_data.day(previous_date)
        previous_amount_yuan = 0.0
        if not previous.empty and key[1] in previous.index:
            previous_amount_yuan = float(previous.loc[key[1]].get("amount", 0.0) or 0.0) * 1000.0
        at_limit = (numeric(bars["close"]) - limit_price).abs().le(PRICE_TOLERANCE)
        sealed_indexes = np.flatnonzero(at_limit.to_numpy())
        if len(sealed_indexes) == 0:
            skipped_status += 1
            continue
        first_index = int(sealed_indexes[0])
        first_hhmm = int(bars.iloc[first_index]["hhmm"])
        if first_hhmm >= 1455:
            continue
        common = {
            "trade_date": key[0],
            "ts_code": key[1],
            "name": str(meta["name"]),
            "market_segment": str(meta["market_segment"]),
            "pre_close": pre_close,
            "limit_price": limit_price,
            "daily_close": float(meta["daily_close"]),
            "closed_at_limit": bool(meta["closed_at_limit"]),
            "failed_to_close_at_limit": bool(meta["failed_to_close_at_limit"]),
            "first_seal_hhmm": first_hhmm,
        }
        first_feature = signal_features(
            bars,
            signal_index=first_index,
            pre_close=pre_close,
            previous_day_amount_yuan=previous_amount_yuan,
            limit_price=limit_price,
        )
        later = bars.iloc[first_index + 1 :].copy()
        later = later[numeric(later["hhmm"]).lt(1455)]
        penetration = numeric(later["low"]).lt(limit_price - PRICE_TOLERANCE)
        queue_confirmed = bool(penetration.any())
        queue_fill_hhmm = int(later.loc[penetration].iloc[0]["hhmm"]) if queue_confirmed else 0
        first_rows.append(
            {
                **common,
                **first_feature,
                "style_event": "FIRST_SEAL",
                "signal_hhmm": first_hhmm,
                "first_break_hhmm": 0,
                "break_close_depth_pct": 0.0,
                "break_low_depth_pct": 0.0,
                "first_seal_to_break_minutes": 0,
                "queue_price_confirmed": queue_confirmed,
                "queue_fill_hhmm": queue_fill_hhmm,
                "post_signal_min_low": float(numeric(later["low"]).min()) if not later.empty else np.nan,
                "next_minute_hhmm": 0,
                "next_minute_entry_price": np.nan,
            }
        )

        first_break_index = -1
        for index in range(first_index + 1, len(bars)):
            if bool(at_limit.iloc[index - 1]) and not bool(at_limit.iloc[index]):
                first_break_index = index
                break
        if first_break_index < 0:
            continue
        break_hhmm = int(bars.iloc[first_break_index]["hhmm"])
        if break_hhmm >= 1454 or first_break_index + 1 >= len(bars):
            continue
        next_bar = bars.iloc[first_break_index + 1]
        next_hhmm = int(next_bar["hhmm"])
        if next_hhmm >= 1455:
            continue
        break_bar = bars.iloc[first_break_index]
        break_feature = signal_features(
            bars,
            signal_index=first_break_index,
            pre_close=pre_close,
            previous_day_amount_yuan=previous_amount_yuan,
            limit_price=limit_price,
        )
        after_break = bars.iloc[first_break_index + 1 :].copy()
        after_break = after_break[numeric(after_break["hhmm"]).lt(1455)]
        break_rows.append(
            {
                **common,
                **break_feature,
                "style_event": "FIRST_BREAK",
                "signal_hhmm": break_hhmm,
                "first_break_hhmm": break_hhmm,
                "break_close_depth_pct": max(limit_price / float(break_bar["close"]) - 1.0, 0.0),
                "break_low_depth_pct": max(limit_price / float(break_bar["low"]) - 1.0, 0.0),
                "first_seal_to_break_minutes": trading_minutes_between(first_hhmm, break_hhmm),
                "queue_price_confirmed": False,
                "queue_fill_hhmm": 0,
                "post_signal_min_low": float(numeric(after_break["low"]).min()) if not after_break.empty else np.nan,
                "next_minute_hhmm": next_hhmm,
                # 下一分钟开盘以0.1%买入滑点计，不能超过当日涨停价。
                "next_minute_entry_price": min(float(next_bar["open"]) * 1.001, limit_price),
            }
        )
        if counter % 5000 == 0:
            LOGGER.info("分钟路径扫描进度：%d组", counter)

    expected_ready = set(
        zip(
            ledger.loc[ledger["minute_status"].eq("READY_1M_PATH_NO_QUEUE_DEPTH"), "trade_date"].astype(str),
            ledger.loc[ledger["minute_status"].eq("READY_1M_PATH_NO_QUEUE_DEPTH"), "ts_code"].astype(str),
        )
    )
    missing_ready = sorted(expected_ready - seen)
    if missing_ready:
        raise RuntimeError(f"分钟文件缺少已认证路径：{missing_ready[:10]}")
    first = pd.DataFrame(first_rows)
    breaks = pd.DataFrame(break_rows)
    first = add_signal_time_market_context(first, first, breaks)
    breaks = add_signal_time_market_context(breaks, first, breaks)
    return pd.concat([first, breaks], ignore_index=True), {
        "ready_path_count": int(len(expected_ready)),
        "first_seal_event_count": int(len(first)),
        "first_break_event_count": int(len(breaks)),
        "skipped_or_mismatch_count": int(skipped_status),
    }


def add_signal_time_market_context(
    frame: pd.DataFrame,
    first_events: pd.DataFrame,
    break_events: pd.DataFrame,
) -> pd.DataFrame:
    """只用严格早于或等于信号分钟的首封/首次炸板计数构造盘中情绪。"""

    if frame.empty:
        return frame
    first_by_day = {date: group for date, group in first_events.groupby("trade_date", sort=False)}
    break_by_day = {date: group for date, group in break_events.groupby("trade_date", sort=False)}
    rows: list[dict[str, float]] = []
    for row in frame.itertuples(index=False):
        day_first = first_by_day[str(row.trade_date)]
        day_break = break_by_day.get(str(row.trade_date), pd.DataFrame())
        hhmm = int(row.signal_hhmm)
        ever = int(numeric(day_first["first_seal_hhmm"]).le(hhmm).sum())
        same_segment = int(
            (
                numeric(day_first["first_seal_hhmm"]).le(hhmm)
                & day_first["market_segment"].astype(str).eq(str(row.market_segment))
            ).sum()
        )
        broken = (
            int(numeric(day_break["first_break_hhmm"]).le(hhmm).sum())
            if not day_break.empty
            else 0
        )
        rows.append(
            {
                "market_ever_sealed_count": ever,
                "same_segment_ever_sealed_count": same_segment,
                "market_first_break_count": broken,
                "market_first_break_rate": broken / ever if ever > 0 else 0.0,
            }
        )
    result = frame.reset_index(drop=True).copy()
    return pd.concat([result, pd.DataFrame(rows)], axis=1)


def style_pool(events: pd.DataFrame, style: str) -> pd.DataFrame:
    if style == "D2":
        return events[
            events["style_event"].eq("FIRST_SEAL")
            & numeric(events["signal_hhmm"]).between(930, 1000)
        ].copy()
    if style == "D4":
        return events[events["style_event"].eq("FIRST_SEAL")].copy()
    if style == "D5":
        return events[events["style_event"].eq("FIRST_BREAK")].copy()
    raise ValueError(f"未知D流派：{style}")


def select_daily_first(pool: pd.DataFrame, rule: StyleRule) -> pd.DataFrame:
    selected = pool[rule.predicate(pool).fillna(False)].copy()
    if selected.empty:
        return selected
    selected["_amount_rank"] = numeric(
        selected["signal_recent_5m_amount_vs_prev_day"]
    ).fillna(-np.inf)
    return (
        selected.sort_values(
            ["trade_date", "signal_hhmm", "_amount_rank", "ts_code"],
            ascending=[True, True, False, True],
        )
        .groupby("trade_date", as_index=False)
        .head(1)
        .reset_index(drop=True)
    )


class OutcomeCache:
    def __init__(self, daily_data: strict.DailyData) -> None:
        self.daily_data = daily_data
        self.cache: dict[tuple[str, str, float], dict[str, Any]] = {}

    def outcome(self, row: pd.Series, entry_price: float) -> dict[str, Any]:
        key = (str(row["trade_date"]), str(row["ts_code"]), round(float(entry_price), 6))
        if key not in self.cache:
            execution = row.copy()
            execution["signal_date"] = key[0]
            execution["limit_close"] = float(entry_price)
            self.cache[key] = strict.d_execution(execution, self.daily_data)
        return self.cache[key]


def outcome_frame(
    picks: pd.DataFrame,
    rule: StyleRule,
    cache: OutcomeCache,
    *,
    optimistic_queue: bool = False,
) -> tuple[pd.DataFrame, dict[str, int]]:
    rows: list[dict[str, Any]] = []
    selected = filled = unresolved = 0
    for _, row in picks.iterrows():
        selected += 1
        entry_price = 0.0
        fill_hhmm = 0
        if rule.entry_model == "QUEUE_LIMIT":
            if not bool(row["queue_price_confirmed"]) and not optimistic_queue:
                continue
            entry_price = float(row["limit_price"])
            fill_hhmm = int(row["queue_fill_hhmm"] or row["signal_hhmm"])
        elif rule.entry_model == "NEXT_MINUTE":
            entry_price = float(row["next_minute_entry_price"])
            fill_hhmm = int(row["next_minute_hhmm"])
        elif rule.entry_model == "FIXED_BID":
            entry_price = float(row["limit_price"]) * (1.0 - float(rule.bid_discount))
            if not float(row["post_signal_min_low"]) <= entry_price + PRICE_TOLERANCE:
                continue
        else:
            raise ValueError(f"未知成交模型：{rule.entry_model}")
        if entry_price <= 0:
            continue
        filled += 1
        execution = cache.outcome(row, entry_price)
        unresolved += int(str(execution.get("status", "")) != "OK")
        rows.append(
            {
                **row.to_dict(),
                "signal_date": str(row["trade_date"]),
                "strategy_leg": "D",
                "d_school": rule.style,
                "rule": rule.name,
                "entry_model": rule.entry_model,
                "entry_price": entry_price,
                "fill_hhmm": fill_hhmm,
                **execution,
            }
        )
    if not rows:
        result = pd.DataFrame(
            columns=[
                "signal_date", "strategy_leg", "ts_code", "name", "status",
                "exit_date", "account_return", "d_school", "rule",
            ]
        )
    else:
        result = pd.DataFrame(rows).sort_values("signal_date").reset_index(drop=True)
    return result, {
        "selected_day_count": selected,
        "filled_count": filled,
        "unresolved_exit_count": unresolved,
    }


def merge_with_d6(
    dx_outcomes: pd.DataFrame,
    blocked_dates: set[str],
    d6_outcomes: pd.DataFrame,
) -> pd.DataFrame:
    fallback = d6_outcomes[~d6_outcomes["signal_date"].astype(str).isin(blocked_dates)].copy()
    if dx_outcomes.empty:
        return fallback.sort_values("signal_date").reset_index(drop=True)
    return pd.concat([dx_outcomes, fallback], ignore_index=True).sort_values(
        "signal_date"
    ).reset_index(drop=True)


def flatten(prefix: str, metrics: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "trade_count",
        "win_rate",
        "avg_account_return",
        "median_account_return",
        "equity_multiple",
        "max_drawdown",
        "max_profit",
        "max_loss",
        "profit_loss_ratio",
        "max_consecutive_losses",
    )
    return {f"{prefix}_{key}": metrics.get(key) for key in keys}


def evaluate_rules(
    events: pd.DataFrame,
    d6_outcomes: pd.DataFrame,
    other_legs: dict[str, pd.DataFrame],
    cache: OutcomeCache,
) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    result_rows: list[dict[str, Any]] = []
    artifacts: dict[str, dict[str, Any]] = {}
    for index, rule in enumerate(rules(), 1):
        pool = style_pool(events, rule.style)
        picks = select_daily_first(pool, rule)
        outcomes, counts = outcome_frame(picks, rule, cache)
        optimistic, optimistic_counts = outcome_frame(
            picks, rule, cache, optimistic_queue=True
        )
        standalone = replay_d_only(outcomes, START, END)
        standalone_metrics = executed_metrics(standalone)
        blocked_dates = set(picks["trade_date"].astype(str))
        merged_outcomes = merge_with_d6(outcomes, blocked_dates, d6_outcomes)
        merged_detail = replay_d_only(merged_outcomes, START, END)
        merged_metrics = executed_metrics(merged_detail)
        combo_detail, combo_metrics = combo_replay(merged_outcomes, other_legs)
        optimistic_merged = merge_with_d6(optimistic, blocked_dates, d6_outcomes)
        optimistic_detail = replay_d_only(optimistic_merged, START, END)
        optimistic_metrics = executed_metrics(optimistic_detail)
        _, optimistic_combo_metrics = combo_replay(optimistic_merged, other_legs)
        first_half = executed_metrics(
            standalone[standalone["signal_date"].between(START, FIRST_12M_END)]
        )
        second_half = executed_metrics(
            standalone[standalone["signal_date"].between(SECOND_12M_START, END)]
        )
        independent_profitable = (
            int(standalone_metrics["trade_count"]) >= MIN_RETAIN_TRADE_COUNT
            and float(standalone_metrics["equity_multiple"]) > 1.0 + TOLERANCE
        )
        d_improved = float(merged_metrics["equity_multiple"]) > BASELINE_D_MULTIPLE + TOLERANCE
        combo_improved = float(combo_metrics["equity_multiple"]) > BASELINE_ACDE_MULTIPLE + TOLERANCE
        retain = bool(
            independent_profitable
            and d_improved
            and combo_improved
            and counts["unresolved_exit_count"] == 0
        )
        result_rows.append(
            {
                "style": rule.style,
                "rule": rule.name,
                "description": rule.description,
                "entry_model": rule.entry_model,
                "bid_discount": rule.bid_discount,
                "signal_time_fields": ",".join(rule.fields),
                "raw_event_count": int(rule.predicate(pool).fillna(False).sum()),
                **counts,
                "optimistic_filled_count": optimistic_counts["filled_count"],
                **flatten("dx", standalone_metrics),
                **flatten("merged_d", merged_metrics),
                **flatten("acde", combo_metrics),
                **flatten("optimistic_merged_d", optimistic_metrics),
                **flatten("optimistic_acde", optimistic_combo_metrics),
                "dx_first_12m_trade_count": int(first_half["trade_count"]),
                "dx_first_12m_multiple": float(first_half["equity_multiple"]),
                "dx_second_12m_trade_count": int(second_half["trade_count"]),
                "dx_second_12m_multiple": float(second_half["equity_multiple"]),
                "independent_profitable_and_sample_sufficient": independent_profitable,
                "merged_d_compound_improved": d_improved,
                "acde_compound_improved": combo_improved,
                "triple_gate_passed": retain,
                "formal_strategy_modified": False,
            }
        )
        artifacts[rule.name] = {
            "rule": rule,
            "picks": picks,
            "outcomes": outcomes,
            "standalone": standalone,
            "merged_outcomes": merged_outcomes,
            "merged_detail": merged_detail,
            "combo_detail": combo_detail,
        }
        LOGGER.info(
            "%s %s：Dx=%d笔/%.6f倍，合并D=%.6f倍，ACDE=%.6f倍，保留=%s（%d/%d）",
            rule.style,
            rule.name,
            int(standalone_metrics["trade_count"]),
            float(standalone_metrics["equity_multiple"]),
            float(merged_metrics["equity_multiple"]),
            float(combo_metrics["equity_multiple"]),
            retain,
            index,
            len(rules()),
        )
    return pd.DataFrame(result_rows), artifacts


def merge_multiple_styles(
    selected_names: tuple[str, ...],
    artifacts: dict[str, dict[str, Any]],
    d6_outcomes: pd.DataFrame,
) -> tuple[pd.DataFrame, set[str]]:
    pick_rows: list[pd.DataFrame] = []
    outcome_maps: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for name in selected_names:
        picks = artifacts[name]["picks"].copy()
        picks["selected_rule"] = name
        pick_rows.append(picks)
        outcomes = artifacts[name]["outcomes"]
        outcome_maps[name] = {
            (str(row["signal_date"]), str(row["ts_code"])): row.to_dict()
            for _, row in outcomes.iterrows()
        }
    if not pick_rows:
        return d6_outcomes.copy(), set()
    all_picks = pd.concat(pick_rows, ignore_index=True)
    priority = {"D2": 0, "D4": 1, "D5": 2}
    all_picks["_style_priority"] = all_picks["selected_rule"].map(
        lambda name: priority[artifacts[str(name)]["rule"].style]
    )
    chosen = (
        all_picks.sort_values(
            ["trade_date", "signal_hhmm", "_style_priority", "ts_code"],
            ascending=[True, True, True, True],
        )
        .groupby("trade_date", as_index=False)
        .head(1)
    )
    blocked_dates = set(chosen["trade_date"].astype(str))
    dx_rows: list[dict[str, Any]] = []
    for _, row in chosen.iterrows():
        name = str(row["selected_rule"])
        value = outcome_maps[name].get((str(row["trade_date"]), str(row["ts_code"])))
        if value is not None:
            dx_rows.append(value)
    dx = pd.DataFrame(dx_rows)
    return merge_with_d6(dx, blocked_dates, d6_outcomes), blocked_dates


def evaluate_final_combinations(
    search: pd.DataFrame,
    artifacts: dict[str, dict[str, Any]],
    d6_outcomes: pd.DataFrame,
    other_legs: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, tuple[str, ...], pd.DataFrame, pd.DataFrame]:
    eligible = search[search["triple_gate_passed"]].copy()
    best_by_style = (
        eligible.sort_values(
            ["style", "acde_equity_multiple", "merged_d_equity_multiple"],
            ascending=[True, False, False],
        )
        .groupby("style", as_index=False)
        .head(1)
    )
    names = list(best_by_style["rule"].astype(str))
    rows: list[dict[str, Any]] = []
    details: dict[tuple[str, ...], tuple[pd.DataFrame, pd.DataFrame]] = {}
    for size in range(0, len(names) + 1):
        for subset in itertools.combinations(names, size):
            merged, blocked = merge_multiple_styles(subset, artifacts, d6_outcomes)
            d_detail = replay_d_only(merged, START, END)
            d_metrics = executed_metrics(d_detail)
            combo_detail, combo_metrics = combo_replay(merged, other_legs)
            passed = (
                float(d_metrics["equity_multiple"]) > BASELINE_D_MULTIPLE + TOLERANCE
                and float(combo_metrics["equity_multiple"]) > BASELINE_ACDE_MULTIPLE + TOLERANCE
            ) if subset else True
            rows.append(
                {
                    "rules": ";".join(subset) if subset else "D6_ONLY",
                    "added_style_count": len(subset),
                    "blocked_d6_signal_day_count": len(blocked),
                    **flatten("d", d_metrics),
                    **flatten("acde", combo_metrics),
                    "final_dual_gate_passed": bool(passed),
                }
            )
            details[subset] = (d_detail, combo_detail)
    result = pd.DataFrame(rows)
    passed_nonempty = result[
        result["final_dual_gate_passed"] & result["added_style_count"].gt(0)
    ].copy()
    if passed_nonempty.empty:
        selected: tuple[str, ...] = tuple()
    else:
        selected_text = str(
            passed_nonempty.sort_values(
                ["acde_equity_multiple", "d_equity_multiple"], ascending=False
            ).iloc[0]["rules"]
        )
        selected = tuple(selected_text.split(";"))
    selected_detail = details[selected]
    return result, selected, selected_detail[0], selected_detail[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="研究盘中首板D2/D4/D5并与D6合并")
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    parser.add_argument("--minute-bars", type=Path, default=MINUTE_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ledger_path = args.ledger if args.ledger.is_absolute() else ROOT / args.ledger
    minute_path = args.minute_bars if args.minute_bars.is_absolute() else ROOT / args.minute_bars
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    ledger, ledger_audit = load_ledger(ledger_path)
    daily_data = strict.daily_data()
    LOGGER.info("扫描完整首板触板一分钟路径，提取D2/D4/D5信号")
    events, event_audit = extract_style_events(ledger, minute_path, daily_data)

    source, source_audit = strict.source_audit()
    if not bool(source_audit.get("passed")):
        raise RuntimeError("严格as-of正式源审计未通过")
    d6_outcomes = strict.build_d(source, daily_data)
    d6_detail = replay_d_only(d6_outcomes, START, END)
    d6_metrics = executed_metrics(d6_detail)
    other_legs = build_current_other_legs()
    baseline_combo_detail, baseline_combo_metrics = combo_replay(d6_outcomes, other_legs)
    assert_formal_baseline(d6_metrics, baseline_combo_metrics)

    LOGGER.info("执行Dx独立、Dx+D6、ACDE三层复利门禁")
    search, artifacts = evaluate_rules(
        events, d6_outcomes, other_legs, OutcomeCache(daily_data)
    )
    combinations, selected, final_d_detail, final_combo_detail = evaluate_final_combinations(
        search, artifacts, d6_outcomes, other_legs
    )
    final_d_metrics = executed_metrics(final_d_detail)
    final_combo_metrics = strict.combo_metrics(final_combo_detail)
    formal_decision = (
        "KEEP_D6_ONLY_NO_OTHER_SCHOOL_PASSED_FINAL_MUTUAL_OCCUPANCY_GATES"
        if not selected
        else "RESEARCH_CANDIDATE_REQUIRES_D1_D3_COMPLETION_AND_FORMAL_RECERTIFICATION"
    )

    style_summary: dict[str, Any] = {}
    for style in ("D2", "D4", "D5"):
        sample = search[search["style"].eq(style)].copy()
        passed = sample[sample["triple_gate_passed"]].sort_values(
            ["acde_equity_multiple", "merged_d_equity_multiple"], ascending=False
        )
        best = sample.sort_values(
            ["acde_equity_multiple", "merged_d_equity_multiple"], ascending=False
        ).head(1)
        style_summary[style] = {
            "rule_count": int(len(sample)),
            "triple_gate_pass_count": int(len(passed)),
            "best_rule_by_acde": str(best.iloc[0]["rule"]) if len(best) else "",
            "best_rule_acde_multiple": float(best.iloc[0]["acde_equity_multiple"]) if len(best) else 1.0,
            "best_passed_rule": str(passed.iloc[0]["rule"]) if len(passed) else "",
            "retained_for_final_subset_search": bool(len(passed)),
        }

    summary = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat(),
        "protocol": STRICT_DISCOVERY,
        "window": f"{START}~{END}",
        "formal_strategy_modified": False,
        "release_eligible": False,
        "scope": {
            "D1": "NOT_RESEARCHED_HERE_REQUIRES_0924_ASOF_AUCTION_HISTORY",
            "D2": "COMPLETED",
            "D3": "NOT_RESEARCHED_HERE_REQUIRES_ALL_7_TO_9_PERCENT_FAILURE_DENOMINATOR",
            "D4": "COMPLETED",
            "D5": "COMPLETED",
            "D6": "FROZEN_FORMAL_BASELINE_AND_FALLBACK",
        },
        "input_audit": {
            **ledger_audit,
            "minute_path": str(minute_path.relative_to(ROOT)),
            "minute_sha256": sha256(minute_path),
            **event_audit,
            "strict_source_audit_passed": True,
        },
        "frozen_baseline": {
            "d6": d6_metrics,
            "acde": baseline_combo_metrics,
            "expected_d6": {"trade_count": BASELINE_D_TRADE_COUNT, "equity_multiple": BASELINE_D_MULTIPLE},
            "expected_acde": {
                "trade_count": BASELINE_ACDE_TRADE_COUNT,
                "equity_multiple": BASELINE_ACDE_MULTIPLE,
                "leg_counts": BASELINE_ACDE_LEG_COUNTS,
            },
            "priority": "D>A>E>C",
            "position_pct": 0.825,
            "d_fill_stress": 0.80,
            "fees_slippage_limit_rules_t1_unchanged": True,
        },
        "gate": {
            "minimum_dx_trade_count": MIN_RETAIN_TRADE_COUNT,
            "dx_independent_multiple_must_exceed": 1.0,
            "merged_d_multiple_must_exceed": BASELINE_D_MULTIPLE,
            "acde_multiple_must_exceed": BASELINE_ACDE_MULTIPLE,
            "unresolved_exit_count_must_equal": 0,
            "selection_never_uses_fill_or_future_return": True,
        },
        "style_results": style_summary,
        "final_subset": {
            "selected_rules": list(selected),
            "d_metrics": final_d_metrics,
            "acde_metrics": final_combo_metrics,
            "formal_decision": formal_decision,
        },
        "formal_decision": formal_decision,
        "limitations": [
            "D2/D4始终封板时缺少历史队列深度，主结果保守记为未成交，乐观上界单列。",
            "一分钟OHLC不能还原同一分钟内先触板还是先下探，排队确认只使用信号后的后续分钟。",
            "D5固定折价单只在信号后分钟最低价穿透时确认成交，成交价保守按委托价而非更优价。",
            "当前24个月内多规则搜索存在过拟合风险，结果仅属研究候选，不直接发布实盘。",
        ],
    }

    events.to_csv(output_dir / "d2_d4_d5_signal_events.csv", index=False, encoding="utf-8-sig")
    search.to_csv(output_dir / "rule_triple_gates.csv", index=False, encoding="utf-8-sig")
    combinations.to_csv(output_dir / "retained_style_subset_gates.csv", index=False, encoding="utf-8-sig")
    d6_detail.to_csv(output_dir / "baseline_d6_standalone_detail.csv", index=False, encoding="utf-8-sig")
    baseline_combo_detail.to_csv(output_dir / "baseline_acde_detail.csv", index=False, encoding="utf-8-sig")
    final_d_detail.to_csv(output_dir / "final_candidate_d_standalone_detail.csv", index=False, encoding="utf-8-sig")
    final_combo_detail.to_csv(output_dir / "final_candidate_acde_detail.csv", index=False, encoding="utf-8-sig")
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
