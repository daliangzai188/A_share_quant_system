#!/usr/bin/env python3
"""系统搜索D回封板条件组合，并执行独立、合并D、ACDE三层复利比较。

当前正式D只是回封板策略族中的一组条件。本脚本从完整首板触板一分钟账本中
提取14:55前的全部回封事件，不预先限制回封时间或炸板次数，再对以下信号时点
可知条件做结构化组合搜索：

* 回封时段；
* 信号时累计炸板/回封次数；
* 最后一次炸板到回封的速度；
* 首封到本次回封的节奏、炸板深度；
* 信号前量能；
* 当时全市场首板封住率、触板数和炸板率；
* 市场板块及信号前价格路径。

每个组合依次计算：

1. 该回封条件组合的独立单账户机械复利；
2. 与当前正式D按真实信号分钟互斥后的合并D复利；
3. 将合并D逐腿放回冻结A/E/C后的ACDE总复利。

一分钟数据没有历史买一队列。主结果采用保守口径：先按信号时点选当日唯一
候选，只有信号后14:55前价格低于涨停价才确认排队成交；始终封板的未知队列
记为未成交，且该笔挂单在当日仍会阻断更晚的D信号。研究属于STRICT_DISCOVERY，
不会自动修改正式策略。

运行：

    python3 scripts/research_strategy_d_reseal_combinations.py
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
from typing import Any, Callable, Iterable

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
from scripts.research_strategy_d_six_schools import (  # noqa: E402
    OutcomeCache,
    signal_features,
)
from src.mechanical_compound import mechanical_compound  # noqa: E402
from src.strategy_d_intraday_ledger import PRICE_TOLERANCE, normalize_minute_bars  # noqa: E402
from src.strict_asof import STRICT_DISCOVERY  # noqa: E402


LOGGER = logging.getLogger("research_strategy_d_reseal_combinations")

LEDGER_PATH = ROOT / "data/research/strategy_d_intraday/event_ledger_full_window.csv"
MINUTE_PATH = ROOT / "data/research/strategy_d_intraday/minute_1m_tushare.csv"
OUTPUT_DIR = ROOT / "reports/strategy_d_reseal_combinations"
FIRST_12M_END = "20250630"
SECOND_12M_START = "20250701"
MIN_PROFITABLE_TRADE_COUNT = 20
EXPECTED_RESEAL_EVENT_COUNT = 33_821
MAX_FINAL_SUBSET_RULES = 12
MAX_FINAL_SUBSET_SIZE = 3

SELECTION_FIELDS = frozenset(
    {
        "signal_hhmm",
        "first_seal_hhmm",
        "open_times_at_signal",
        "first_to_signal_minutes",
        "last_break_to_signal_minutes",
        "previous_seal_to_break_minutes",
        "last_break_close_depth_pct",
        "last_break_low_depth_pct",
        "open_gap_pct",
        "pre_signal_min_return",
        "signal_cumulative_amount_vs_prev_day",
        "signal_recent_5m_amount_vs_prev_day",
        "signal_recent_5m_share_of_cumulative",
        "signal_limit_close_share",
        "market_ever_sealed_count",
        "market_active_sealed_count",
        "market_seal_rate",
        "market_break_event_count",
        "market_break_event_rate",
        "same_segment_ever_sealed_count",
        "same_segment_active_sealed_count",
        "same_segment_seal_rate",
        "market_segment",
    }
)

FORBIDDEN_SELECTION_FIELDS = frozenset(
    {
        "queue_price_confirmed",
        "queue_fill_hhmm",
        "closed_at_limit",
        "failed_to_close_at_limit",
        "daily_close",
        "execution_status",
        "exit_date",
        "account_return",
        "stock_return_before_fees",
    }
)


@dataclass(frozen=True)
class AxisOption:
    axis: str
    name: str
    description: str
    fields: tuple[str, ...]
    predicate: Callable[[pd.DataFrame], pd.Series]
    group: str = ""


@dataclass(frozen=True)
class RuleSpec:
    name: str
    family: str
    description: str
    options: tuple[AxisOption, ...]

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(sorted({field for option in self.options for field in option.fields}))


def numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def all_rows(frame: pd.DataFrame) -> pd.Series:
    return pd.Series(True, index=frame.index)


def between(column: str, low: float, high: float) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: numeric(frame[column]).between(low, high)


def ge(column: str, value: float) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: numeric(frame[column]).ge(value)


def le(column: str, value: float) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: numeric(frame[column]).le(value)


def lt(column: str, value: float) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: numeric(frame[column]).lt(value)


def segment(values: set[str]) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: frame["market_segment"].astype(str).isin(values)


def time_options() -> list[AxisOption]:
    return [
        AxisOption("time", "time_any", "回封时间不限", tuple(), all_rows),
        AxisOption("time", "time_0930_1000", "09:30~10:00回封", ("signal_hhmm",), between("signal_hhmm", 930, 1000)),
        AxisOption("time", "time_1001_1130", "10:01~11:30回封", ("signal_hhmm",), between("signal_hhmm", 1001, 1130)),
        AxisOption("time", "time_1300_1359", "13:00~13:59回封", ("signal_hhmm",), between("signal_hhmm", 1300, 1359)),
        AxisOption("time", "time_1400_1429", "14:00~14:29回封", ("signal_hhmm",), between("signal_hhmm", 1400, 1429)),
        AxisOption("time", "time_1430_1454", "14:30~14:54回封", ("signal_hhmm",), between("signal_hhmm", 1430, 1454)),
        AxisOption("time", "time_before_1400", "14:00前回封", ("signal_hhmm",), lt("signal_hhmm", 1400)),
        AxisOption("time", "time_1001_1359", "10:01~13:59回封", ("signal_hhmm",), between("signal_hhmm", 1001, 1359)),
        AxisOption("time", "time_1300_1454", "13:00~14:54回封", ("signal_hhmm",), between("signal_hhmm", 1300, 1454)),
        AxisOption("time", "time_1400_1454", "14:00~14:54回封", ("signal_hhmm",), between("signal_hhmm", 1400, 1454)),
    ]


def open_count_options() -> list[AxisOption]:
    return [
        AxisOption("open_count", "open_any", "炸板/回封次数不限", tuple(), all_rows),
        AxisOption("open_count", "open_eq1", "第1次炸板后的回封", ("open_times_at_signal",), lambda x: numeric(x["open_times_at_signal"]).eq(1)),
        AxisOption("open_count", "open_eq2", "第2次炸板后的回封", ("open_times_at_signal",), lambda x: numeric(x["open_times_at_signal"]).eq(2)),
        AxisOption("open_count", "open_eq3", "第3次炸板后的回封", ("open_times_at_signal",), lambda x: numeric(x["open_times_at_signal"]).eq(3)),
        AxisOption("open_count", "open_eq4", "第4次炸板后的回封", ("open_times_at_signal",), lambda x: numeric(x["open_times_at_signal"]).eq(4)),
        AxisOption("open_count", "open_ge5", "第5次及以后炸板后的回封", ("open_times_at_signal",), ge("open_times_at_signal", 5)),
        AxisOption("open_count", "open_1_2", "第1~2次炸板后的回封", ("open_times_at_signal",), between("open_times_at_signal", 1, 2)),
        AxisOption("open_count", "open_2_3", "第2~3次炸板后的回封", ("open_times_at_signal",), between("open_times_at_signal", 2, 3)),
        AxisOption("open_count", "open_3_5", "第3~5次炸板后的回封", ("open_times_at_signal",), between("open_times_at_signal", 3, 5)),
        AxisOption("open_count", "open_ge2", "第2次及以后炸板后的回封", ("open_times_at_signal",), ge("open_times_at_signal", 2)),
    ]


def speed_options() -> list[AxisOption]:
    return [
        AxisOption("speed", "speed_any", "回封速度不限", tuple(), all_rows),
        AxisOption("speed", "speed_le1", "最后炸板后1分钟内回封", ("last_break_to_signal_minutes",), le("last_break_to_signal_minutes", 1)),
        AxisOption("speed", "speed_le2", "最后炸板后2分钟内回封", ("last_break_to_signal_minutes",), le("last_break_to_signal_minutes", 2)),
        AxisOption("speed", "speed_2_5", "最后炸板后2~5分钟回封", ("last_break_to_signal_minutes",), between("last_break_to_signal_minutes", 2, 5)),
        AxisOption("speed", "speed_le5", "最后炸板后5分钟内回封", ("last_break_to_signal_minutes",), le("last_break_to_signal_minutes", 5)),
        AxisOption("speed", "speed_6_10", "最后炸板后6~10分钟回封", ("last_break_to_signal_minutes",), between("last_break_to_signal_minutes", 6, 10)),
        AxisOption("speed", "speed_ge11", "最后炸板11分钟后回封", ("last_break_to_signal_minutes",), ge("last_break_to_signal_minutes", 11)),
    ]


def quality_options() -> list[AxisOption]:
    return [
        AxisOption("quality", "quality_none", "不附加质量条件", tuple(), all_rows, "none"),
        AxisOption("quality", "duration_16_30", "首封后16~30分钟形成回封", ("first_to_signal_minutes",), between("first_to_signal_minutes", 16, 30), "duration"),
        AxisOption("quality", "duration_31_60", "首封后31~60分钟形成回封", ("first_to_signal_minutes",), between("first_to_signal_minutes", 31, 60), "duration"),
        AxisOption("quality", "duration_61_120", "首封后61~120分钟形成回封", ("first_to_signal_minutes",), between("first_to_signal_minutes", 61, 120), "duration"),
        AxisOption("quality", "duration_ge121", "首封121分钟后形成回封", ("first_to_signal_minutes",), ge("first_to_signal_minutes", 121), "duration"),
        AxisOption("quality", "break_close_le0_5", "最后炸板分钟收盘回落不超0.5%", ("last_break_close_depth_pct",), le("last_break_close_depth_pct", 0.005), "break"),
        AxisOption("quality", "break_close_le1", "最后炸板分钟收盘回落不超1%", ("last_break_close_depth_pct",), le("last_break_close_depth_pct", 0.010), "break"),
        AxisOption("quality", "break_low_le1", "最后炸板分钟最低回落不超1%", ("last_break_low_depth_pct",), le("last_break_low_depth_pct", 0.010), "break"),
        AxisOption("quality", "break_low_le2", "最后炸板分钟最低回落不超2%", ("last_break_low_depth_pct",), le("last_break_low_depth_pct", 0.020), "break"),
        AxisOption("quality", "amount_ge0_5", "信号前成交额至少前日50%", ("signal_cumulative_amount_vs_prev_day",), ge("signal_cumulative_amount_vs_prev_day", 0.50), "volume"),
        AxisOption("quality", "amount_ge1", "信号前成交额至少前日1倍", ("signal_cumulative_amount_vs_prev_day",), ge("signal_cumulative_amount_vs_prev_day", 1.00), "volume"),
        AxisOption("quality", "amount_ge1_5", "信号前成交额至少前日1.5倍", ("signal_cumulative_amount_vs_prev_day",), ge("signal_cumulative_amount_vs_prev_day", 1.50), "volume"),
        AxisOption("quality", "amount_ge2", "信号前成交额至少前日2倍", ("signal_cumulative_amount_vs_prev_day",), ge("signal_cumulative_amount_vs_prev_day", 2.00), "volume"),
        AxisOption("quality", "recent5_ge5pct", "回封前5分钟成交额至少前日5%", ("signal_recent_5m_amount_vs_prev_day",), ge("signal_recent_5m_amount_vs_prev_day", 0.05), "volume"),
        AxisOption("quality", "recent5_ge10pct", "回封前5分钟成交额至少前日10%", ("signal_recent_5m_amount_vs_prev_day",), ge("signal_recent_5m_amount_vs_prev_day", 0.10), "volume"),
        AxisOption("quality", "seal_rate_40_60", "信号时全市场首板封住率40%~60%", ("market_seal_rate",), between("market_seal_rate", 0.40, 0.60), "market"),
        AxisOption("quality", "seal_rate_ge60", "信号时全市场首板封住率至少60%", ("market_seal_rate",), ge("market_seal_rate", 0.60), "market"),
        AxisOption("quality", "seal_rate_ge70", "信号时全市场首板封住率至少70%", ("market_seal_rate",), ge("market_seal_rate", 0.70), "market"),
        AxisOption("quality", "ever_20_60", "信号时全市场已有20~60只首板触板", ("market_ever_sealed_count",), between("market_ever_sealed_count", 20, 60), "market"),
        AxisOption("quality", "ever_40_100", "信号时全市场已有40~100只首板触板", ("market_ever_sealed_count",), between("market_ever_sealed_count", 40, 100), "market"),
        AxisOption("quality", "ever_40_150", "信号时全市场已有40~150只首板触板", ("market_ever_sealed_count",), between("market_ever_sealed_count", 40, 150), "market"),
        AxisOption("quality", "break_rate_le50", "信号时累计炸板事件率不超50%", ("market_break_event_rate",), le("market_break_event_rate", 0.50), "market"),
        AxisOption("quality", "pre_low_ge0", "信号前最低价不低于昨收", ("pre_signal_min_return",), ge("pre_signal_min_return", 0.0), "path"),
        AxisOption("quality", "main_board", "仅沪深主板", ("market_segment",), segment({"sh_main", "sz_main"}), "segment"),
        AxisOption("quality", "growth_board", "仅创业板或科创板", ("market_segment",), segment({"chi_next", "star"}), "segment"),
    ]


def build_rule_space() -> list[RuleSpec]:
    """冻结组合空间；覆盖每个核心维度不受限时由其他条件补偿的结构。"""

    times = time_options()
    opens = open_count_options()
    speeds = speed_options()
    qualities = quality_options()
    none_quality = qualities[0]
    result: dict[str, RuleSpec] = {}

    def add(family: str, options: Iterable[AxisOption]) -> None:
        selected = tuple(option for option in options if option.name != "quality_none")
        name = "__".join(option.name for option in selected)
        if not name:
            name = "all_reseals"
        description = "；".join(option.description for option in selected)
        if not description:
            description = "全部14:55前回封事件"
        spec = RuleSpec(name=name, family=family, description=description, options=selected)
        unknown = set(spec.fields) - SELECTION_FIELDS
        forbidden = set(spec.fields) & FORBIDDEN_SELECTION_FIELDS
        if unknown or forbidden:
            raise ValueError(
                f"回封组合{name}字段非法：unknown={sorted(unknown)} "
                f"forbidden={sorted(forbidden)}"
            )
        result.setdefault(name, spec)

    # 全部时间×次数×速度结构单元；这部分不依赖事后诊断。
    for time, opens_, speed in itertools.product(times, opens, speeds):
        add("STRUCTURAL_GRID", (time, opens_, speed, none_quality))

    # 任一核心维度不受限时，用单个量价/情绪/路径条件补偿。
    for time, opens_, speed in itertools.product(times, opens, speeds):
        if not (
            time.name == "time_any"
            or opens_.name == "open_any"
            or speed.name == "speed_any"
        ):
            continue
        for quality in qualities[1:]:
            unrestricted = []
            if time.name == "time_any":
                unrestricted.append("TIME_FREE")
            if opens_.name == "open_any":
                unrestricted.append("OPEN_COUNT_FREE")
            if speed.name == "speed_any":
                unrestricted.append("SPEED_FREE")
            add("+".join(unrestricted), (time, opens_, speed, quality))

    # 时间、次数和速度全部不设限时，再研究两个不同质量维度的交叉。
    pair_groups = {
        frozenset({"volume", "market"}),
        frozenset({"break", "volume"}),
        frozenset({"duration", "market"}),
        frozenset({"segment", "market"}),
    }
    for left, right in itertools.combinations(qualities[1:], 2):
        if frozenset({left.group, right.group}) not in pair_groups:
            continue
        add("PURE_QUALITY_PAIR_ALL_TIME_AND_OPEN_COUNT", (times[0], opens[0], speeds[0], left, right))

    return list(result.values())


def add_market_context(
    reseals: pd.DataFrame, state_events: pd.DataFrame
) -> pd.DataFrame:
    """按每个分钟结束状态构建全市场及同市场板块的as-of情绪。"""

    if reseals.empty:
        return reseals
    state = state_events.copy()
    state["market_delta"] = numeric(state["market_delta"]).fillna(0).astype(int)
    state["first_increment"] = numeric(state["first_increment"]).fillna(0).astype(int)
    state["break_increment"] = numeric(state["break_increment"]).fillna(0).astype(int)

    market = (
        state.groupby(["trade_date", "hhmm"], as_index=False)
        .agg(
            market_delta=("market_delta", "sum"),
            first_increment=("first_increment", "sum"),
            break_increment=("break_increment", "sum"),
        )
        .sort_values(["trade_date", "hhmm"])
    )
    market["market_active_sealed_count"] = market.groupby("trade_date")["market_delta"].cumsum()
    market["market_ever_sealed_count"] = market.groupby("trade_date")["first_increment"].cumsum()
    market["market_break_event_count"] = market.groupby("trade_date")["break_increment"].cumsum()
    market["market_seal_rate"] = (
        market["market_active_sealed_count"]
        / market["market_ever_sealed_count"].replace(0, np.nan)
    ).fillna(0.0)
    market["market_break_event_rate"] = (
        market["market_break_event_count"]
        / market["market_ever_sealed_count"].replace(0, np.nan)
    ).fillna(0.0)

    segment_state = (
        state.groupby(["trade_date", "market_segment", "hhmm"], as_index=False)
        .agg(
            segment_delta=("market_delta", "sum"),
            segment_first_increment=("first_increment", "sum"),
        )
        .sort_values(["trade_date", "market_segment", "hhmm"])
    )
    keys = ["trade_date", "market_segment"]
    segment_state["same_segment_active_sealed_count"] = segment_state.groupby(keys)["segment_delta"].cumsum()
    segment_state["same_segment_ever_sealed_count"] = segment_state.groupby(keys)["segment_first_increment"].cumsum()
    segment_state["same_segment_seal_rate"] = (
        segment_state["same_segment_active_sealed_count"]
        / segment_state["same_segment_ever_sealed_count"].replace(0, np.nan)
    ).fillna(0.0)

    context = reseals.merge(
        market[
            [
                "trade_date",
                "hhmm",
                "market_active_sealed_count",
                "market_ever_sealed_count",
                "market_break_event_count",
                "market_seal_rate",
                "market_break_event_rate",
            ]
        ],
        left_on=["trade_date", "signal_hhmm"],
        right_on=["trade_date", "hhmm"],
        how="left",
        validate="many_to_one",
    ).drop(columns="hhmm")
    context = context.merge(
        segment_state[
            [
                "trade_date",
                "market_segment",
                "hhmm",
                "same_segment_active_sealed_count",
                "same_segment_ever_sealed_count",
                "same_segment_seal_rate",
            ]
        ],
        left_on=["trade_date", "market_segment", "signal_hhmm"],
        right_on=["trade_date", "market_segment", "hhmm"],
        how="left",
        validate="many_to_one",
    ).drop(columns="hhmm")
    required = [
        "market_active_sealed_count",
        "market_ever_sealed_count",
        "market_break_event_count",
        "market_seal_rate",
        "market_break_event_rate",
        "same_segment_active_sealed_count",
        "same_segment_ever_sealed_count",
        "same_segment_seal_rate",
    ]
    if context[required].isna().any().any():
        raise RuntimeError("回封事件的盘中市场情绪映射不完整")
    return context


def extract_reseal_events(
    ledger: pd.DataFrame,
    minute_path: Path,
    daily_data: strict.DailyData,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """流式扫描完整分钟文件，提取每一次14:55前回封及其时点特征。"""

    lookup = ledger.set_index(["trade_date", "ts_code"], drop=False)
    reseal_rows: list[dict[str, Any]] = []
    state_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    skipped = 0

    for counter, (key, raw) in enumerate(iter_minute_groups(minute_path), 1):
        if key not in lookup.index:
            raise RuntimeError(f"分钟股票日不在冻结回封账本：{key}")
        seen.add(key)
        meta = lookup.loc[key]
        if isinstance(meta, pd.DataFrame):
            raise RuntimeError(f"冻结回封账本日期+代码重复：{key}")
        if str(meta["minute_status"]) != "READY_1M_PATH_NO_QUEUE_DEPTH":
            skipped += 1
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
        was_sealed = False
        ever_sealed = False
        open_times = 0
        first_seal_index = -1
        previous_seal_index = -1
        last_break_index = -1

        for index in range(len(bars)):
            hhmm = int(bars.iloc[index]["hhmm"])
            sealed = bool(at_limit.iloc[index])
            if sealed and not was_sealed:
                is_first = not ever_sealed
                state_rows.append(
                    {
                        "trade_date": key[0],
                        "market_segment": str(meta["market_segment"]),
                        "hhmm": hhmm,
                        "market_delta": 1,
                        "first_increment": int(is_first),
                        "break_increment": 0,
                    }
                )
                if is_first:
                    first_seal_index = index
                    ever_sealed = True
                elif hhmm < 1455 and last_break_index >= 0:
                    feature = signal_features(
                        bars,
                        signal_index=index,
                        pre_close=pre_close,
                        previous_day_amount_yuan=previous_amount_yuan,
                        limit_price=limit_price,
                    )
                    after = bars.iloc[index + 1 :].copy()
                    after = after[numeric(after["hhmm"]).lt(1455)]
                    penetration = numeric(after["low"]).lt(limit_price - PRICE_TOLERANCE)
                    confirmed = bool(penetration.any())
                    fill_hhmm = int(after.loc[penetration].iloc[0]["hhmm"]) if confirmed else 0
                    break_bar = bars.iloc[last_break_index]
                    recent_share = (
                        float(feature["signal_recent_5m_amount_vs_prev_day"])
                        / float(feature["signal_cumulative_amount_vs_prev_day"])
                        if pd.notna(feature["signal_cumulative_amount_vs_prev_day"])
                        and float(feature["signal_cumulative_amount_vs_prev_day"]) > 0
                        else np.nan
                    )
                    reseal_rows.append(
                        {
                            "event_id": len(reseal_rows),
                            "trade_date": key[0],
                            "ts_code": key[1],
                            "name": str(meta["name"]),
                            "market_segment": str(meta["market_segment"]),
                            "pre_close": pre_close,
                            "limit_price": limit_price,
                            "daily_close": float(meta["daily_close"]),
                            "closed_at_limit": bool(meta["closed_at_limit"]),
                            "failed_to_close_at_limit": bool(meta["failed_to_close_at_limit"]),
                            "first_seal_hhmm": int(bars.iloc[first_seal_index]["hhmm"]),
                            "signal_hhmm": hhmm,
                            "open_times_at_signal": open_times,
                            "first_to_signal_minutes": trading_minutes_between(
                                int(bars.iloc[first_seal_index]["hhmm"]), hhmm
                            ),
                            "last_break_hhmm": int(break_bar["hhmm"]),
                            "last_break_to_signal_minutes": trading_minutes_between(
                                int(break_bar["hhmm"]), hhmm
                            ),
                            "previous_seal_to_break_minutes": trading_minutes_between(
                                int(bars.iloc[previous_seal_index]["hhmm"]),
                                int(break_bar["hhmm"]),
                            ) if previous_seal_index >= 0 else 0,
                            "last_break_close_depth_pct": max(
                                limit_price / float(break_bar["close"]) - 1.0, 0.0
                            ),
                            "last_break_low_depth_pct": max(
                                limit_price / float(break_bar["low"]) - 1.0, 0.0
                            ),
                            **feature,
                            "signal_recent_5m_share_of_cumulative": recent_share,
                            "queue_price_confirmed": confirmed,
                            "queue_fill_hhmm": fill_hhmm,
                        }
                    )
                previous_seal_index = index
            elif not sealed and was_sealed:
                open_times += 1
                last_break_index = index
                state_rows.append(
                    {
                        "trade_date": key[0],
                        "market_segment": str(meta["market_segment"]),
                        "hhmm": hhmm,
                        "market_delta": -1,
                        "first_increment": 0,
                        "break_increment": 1,
                    }
                )
            was_sealed = sealed

        if counter % 5000 == 0:
            LOGGER.info("全部回封分钟路径扫描进度：%d组", counter)

    expected_ready = set(
        zip(
            ledger.loc[ledger["minute_status"].eq("READY_1M_PATH_NO_QUEUE_DEPTH"), "trade_date"].astype(str),
            ledger.loc[ledger["minute_status"].eq("READY_1M_PATH_NO_QUEUE_DEPTH"), "ts_code"].astype(str),
        )
    )
    missing = sorted(expected_ready - seen)
    if missing:
        raise RuntimeError(f"分钟文件缺少已认证回封路径：{missing[:10]}")
    reseals = pd.DataFrame(reseal_rows)
    if len(reseals) != EXPECTED_RESEAL_EVENT_COUNT:
        raise RuntimeError(
            f"14:55前回封事件数漂移：expected={EXPECTED_RESEAL_EVENT_COUNT} actual={len(reseals)}"
        )
    reseals = add_market_context(reseals, pd.DataFrame(state_rows))
    reseals = reseals.sort_values(
        ["trade_date", "signal_hhmm", "open_times_at_signal", "ts_code", "event_id"]
    ).reset_index(drop=True)
    return reseals, {
        "ready_path_count": len(expected_ready),
        "skipped_or_mismatch_count": skipped,
        "reseal_event_count_before_1455": int(len(reseals)),
        "reseal_stock_day_count": int(reseals[["trade_date", "ts_code"]].drop_duplicates().shape[0]),
        "reseal_trade_day_count": int(reseals["trade_date"].nunique()),
        "open_times_min": int(reseals["open_times_at_signal"].min()),
        "open_times_max": int(reseals["open_times_at_signal"].max()),
    }


def load_cached_reseal_events(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    frame = pd.read_csv(
        path,
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    required = set(SELECTION_FIELDS) | {
        "event_id",
        "trade_date",
        "ts_code",
        "name",
        "limit_price",
        "queue_price_confirmed",
        "queue_fill_hhmm",
        "execution_status",
        "exit_date",
        "account_return",
    }
    missing = sorted(required - set(frame.columns))
    failures: list[str] = []
    if missing:
        failures.append(f"缺字段={missing}")
    if len(frame) != EXPECTED_RESEAL_EVENT_COUNT:
        failures.append(f"事件数={len(frame)}")
    if frame["event_id"].duplicated().any():
        failures.append("event_id重复")
    if not numeric(frame["signal_hhmm"]).lt(1455).all():
        failures.append("含14:55及以后回封")
    if failures:
        raise RuntimeError("缓存回封事件账本校验失败：" + "；".join(failures))
    frame["queue_price_confirmed"] = frame["queue_price_confirmed"].astype(str).str.lower().isin(
        {"true", "1", "yes"}
    )
    return frame, {
        "ready_path_count": 40_328,
        "skipped_or_mismatch_count": 8,
        "reseal_event_count_before_1455": int(len(frame)),
        "reseal_stock_day_count": int(frame[["trade_date", "ts_code"]].drop_duplicates().shape[0]),
        "reseal_trade_day_count": int(frame["trade_date"].nunique()),
        "open_times_min": int(numeric(frame["open_times_at_signal"]).min()),
        "open_times_max": int(numeric(frame["open_times_at_signal"]).max()),
        "loaded_from_completed_intermediate_ledger": True,
        "cache_path": str(path.relative_to(ROOT)),
        "cache_sha256": sha256(path),
    }


def attach_outcomes(events: pd.DataFrame, cache: OutcomeCache) -> pd.DataFrame:
    """同一股票日所有回封均以涨停价买入，退出结果只计算一次后复用。"""

    rows: list[dict[str, Any]] = []
    unique = events.drop_duplicates(["trade_date", "ts_code"], keep="first")
    for counter, (_, row) in enumerate(unique.iterrows(), 1):
        execution = cache.outcome(row, float(row["limit_price"]))
        rows.append(
            {
                "trade_date": str(row["trade_date"]),
                "ts_code": str(row["ts_code"]),
                "execution_status": str(execution.get("status", "")),
                "exit_date": str(execution.get("exit_date", "")),
                "stock_return_before_fees": execution.get("stock_return_before_fees"),
                "account_return": execution.get("account_return"),
            }
        )
        if counter % 5000 == 0:
            LOGGER.info("回封股票日退出结果计算进度：%d/%d", counter, len(unique))
    return events.merge(
        pd.DataFrame(rows),
        on=["trade_date", "ts_code"],
        how="left",
        validate="many_to_one",
    )


def max_consecutive_losses(values: np.ndarray) -> int:
    maximum = current = 0
    for value in values:
        current = current + 1 if float(value) <= 0 else 0
        maximum = max(maximum, current)
    return maximum


def basic_metrics(values: Iterable[float]) -> dict[str, Any]:
    array = np.asarray(list(values), dtype=float)
    if len(array) == 0:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_account_return": 0.0,
            "median_account_return": 0.0,
            "equity_multiple": 1.0,
            "max_drawdown": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "profit_loss_ratio": 0.0,
            "max_consecutive_losses": 0,
        }
    positive = array[array > 0]
    negative = array[array < 0]
    compound = mechanical_compound(array)
    return {
        "trade_count": int(len(array)),
        "win_rate": float((array > 0).mean()),
        "avg_account_return": float(array.mean()),
        "median_account_return": float(np.median(array)),
        "equity_multiple": float(compound.equity_multiple),
        "max_drawdown": float(compound.max_drawdown),
        "max_profit": float(array.max()),
        "max_loss": float(array.min()),
        "profit_loss_ratio": (
            float(positive.mean() / abs(negative.mean()))
            if len(positive) and len(negative)
            else 0.0
        ),
        "max_consecutive_losses": max_consecutive_losses(array),
    }


def flatten(prefix: str, metrics: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}_{key}": value for key, value in metrics.items()}


def fast_standalone_records(
    outcomes: pd.DataFrame, calendar: list[str]
) -> list[dict[str, Any]]:
    valid = outcomes[outcomes["status"].astype(str).eq("OK")].copy()
    candidates = {
        str(row["signal_date"]): row.to_dict()
        for _, row in valid.drop_duplicates("signal_date", keep="last").iterrows()
    }
    occupied_until = occupied_code = occupied_name = ""
    records: list[dict[str, Any]] = []
    for signal_date in calendar:
        if occupied_until and signal_date < occupied_until:
            continue
        blocking_handoff = bool(
            occupied_until
            and signal_date == occupied_until
            and not strict.cert.hit_limit_up(signal_date, occupied_code, occupied_name)
        )
        occupied_until = occupied_code = occupied_name = ""
        selected = candidates.get(signal_date)
        if selected is None or blocking_handoff:
            continue
        occupied_until = str(selected["exit_date"])
        occupied_code = str(selected["ts_code"])
        occupied_name = str(selected.get("name", ""))
        records.append(selected)
    return records


def outcome_frame_from_picks(picks: pd.DataFrame) -> pd.DataFrame:
    filled = picks[picks["queue_price_confirmed"].astype(bool)].copy()
    if filled.empty:
        return pd.DataFrame(
            columns=[
                "signal_date", "strategy_leg", "ts_code", "name", "status",
                "exit_date", "account_return", "signal_hhmm", "event_id",
            ]
        )
    filled["signal_date"] = filled["trade_date"].astype(str)
    filled["strategy_leg"] = "D"
    filled["status"] = filled["execution_status"].astype(str)
    return filled.sort_values("signal_date").reset_index(drop=True)


def hhmm_from_source_time(value: object) -> int:
    digits = "".join(character for character in str(value).replace(".0", "") if character.isdigit())
    if len(digits) < 4:
        return 0
    return int(digits[:4])


def prepare_baseline_signal_picks(
    baseline_outcomes: pd.DataFrame,
    source: pd.DataFrame,
    events: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """用冻结正式源的last_time标记正式D委托分钟。

    正式D历史基线由涨停池静态炸板次数和最后封板时间构造；部分股票日的静态
    open_times与一分钟收盘路径计数不同，不能强行把正式基线改写成分钟路径规则。
    ``last_time``发生时已经可知，因此用于D家族内部信号先后比较；差异单独审计。
    """

    keys = source[["trade_date", "ts_code", "last_time", "open_times"]].copy()
    keys["trade_date"] = keys["trade_date"].astype(str)
    keys["ts_code"] = keys["ts_code"].astype(str)
    keys = keys.drop_duplicates(["trade_date", "ts_code"], keep="last")
    baseline = baseline_outcomes.copy()
    baseline["trade_date"] = baseline["signal_date"].astype(str)
    result = baseline.merge(
        keys,
        on=["trade_date", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    if result["last_time"].isna().any():
        missing = result.loc[result["last_time"].isna(), ["trade_date", "ts_code"]]
        raise RuntimeError(f"正式D候选缺少冻结源last_time：{missing.to_dict('records')[:10]}")
    result["source_last_hhmm"] = result["last_time"].map(hhmm_from_source_time)
    if not result["source_last_hhmm"].ge(1400).all():
        bad = result.loc[
            ~result["source_last_hhmm"].ge(1400),
            ["trade_date", "ts_code", "last_time", "source_last_hhmm"],
        ]
        raise RuntimeError(f"正式D last_time早于14:00：{bad.to_dict('records')[:10]}")
    minute_reseal_map = (
        events[numeric(events["signal_hhmm"]).lt(1455)]
        .groupby(["trade_date", "ts_code"])["signal_hhmm"]
        .max()
        .to_dict()
    )
    fallback_rows: list[dict[str, Any]] = []
    resolved_hhmm: list[int] = []
    for row in result.itertuples(index=False):
        source_hhmm = int(row.source_last_hhmm)
        if source_hhmm < 1455:
            resolved_hhmm.append(source_hhmm)
            continue
        key = (str(row.trade_date), str(row.ts_code))
        minute_hhmm = int(minute_reseal_map.get(key, 0) or 0)
        resolved = minute_hhmm if 1400 <= minute_hhmm < 1455 else 1454
        resolved_hhmm.append(resolved)
        fallback_rows.append(
            {
                "trade_date": key[0],
                "ts_code": key[1],
                "source_last_time": str(row.last_time),
                "source_last_hhmm": source_hhmm,
                "arbitration_hhmm": resolved,
                "handling": (
                    "USE_LATEST_MINUTE_RESEAL_BEFORE_CANCEL"
                    if minute_hhmm
                    else "CLAMP_TO_LAST_VALID_ORDER_MINUTE_KEEP_FROZEN_BASELINE"
                ),
            }
        )
    result["signal_hhmm"] = resolved_hhmm
    result["event_id"] = -np.arange(1, len(result) + 1)
    result["source_rule"] = "FORMAL_D_BASELINE"
    result["source_priority"] = 0
    result["queue_price_confirmed"] = True
    minute_keys = set(
        zip(
            events["trade_date"].astype(str),
            events["ts_code"].astype(str),
            numeric(events["signal_hhmm"]).astype(int),
        )
    )
    exact_match_count = sum(
        (str(row.trade_date), str(row.ts_code), int(row.signal_hhmm)) in minute_keys
        for row in result.itertuples(index=False)
    )
    audit = {
        "baseline_candidate_count": int(len(result)),
        "signal_time_source": "FROZEN_OFFICIAL_LIMIT_LIST_LAST_TIME_HHMMSS",
        "exact_minute_reseal_match_count": int(exact_match_count),
        "static_minute_path_mismatch_count": int(len(result) - exact_match_count),
        "handling": "KEEP_FORMAL_BASELINE_IDENTITY_USE_ASOF_LAST_TIME_FOR_FAMILY_ARBITRATION",
        "last_time_at_or_after_cancel_count": len(fallback_rows),
        "last_time_at_or_after_cancel_rows": fallback_rows,
    }
    return result, audit


def merge_picks_with_baseline(
    candidate_picks: pd.DataFrame,
    baseline_picks: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidate = candidate_picks.copy()
    candidate["source_rule"] = candidate.get("source_rule", "RESEAL_CANDIDATE")
    candidate["source_priority"] = 1
    baseline = baseline_picks.copy()
    columns = sorted(set(candidate.columns) | set(baseline.columns))
    chosen = (
        pd.concat(
            [candidate.reindex(columns=columns), baseline.reindex(columns=columns)],
            ignore_index=True,
        )
        .sort_values(
            ["trade_date", "signal_hhmm", "source_priority", "ts_code", "event_id"],
            ascending=[True, True, True, True, True],
        )
        .groupby("trade_date", as_index=False)
        .head(1)
        .reset_index(drop=True)
    )
    candidate_chosen = chosen[chosen["source_priority"].eq(1)].copy()
    baseline_chosen = chosen[chosen["source_priority"].eq(0)].copy()
    candidate_outcomes = outcome_frame_from_picks(candidate_chosen)
    baseline_outcomes = baseline_chosen[
        [
            "signal_date", "strategy_leg", "ts_code", "name", "status",
            "exit_date", "account_return", "signal_hhmm", "event_id",
        ]
    ].copy()
    outcomes = pd.concat([candidate_outcomes, baseline_outcomes], ignore_index=True)
    return outcomes.sort_values("signal_date").reset_index(drop=True), chosen


def detail_metrics(detail: pd.DataFrame) -> dict[str, Any]:
    trades = detail[detail["status"].eq("EXECUTED")]
    result = basic_metrics(pd.to_numeric(trades["account_return"], errors="raise"))
    result["leg_counts"] = trades["strategy_leg"].value_counts().sort_index().to_dict()
    return result


def combo_replay_fast(
    d_outcomes: pd.DataFrame,
    other_legs: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """复用正式组合状态机，但搜索阶段不为每个组合重复做bootstrap。"""

    legs = {"D": d_outcomes, **other_legs}
    maps = {leg: strict.candidate_map(frame) for leg, frame in legs.items()}
    detail = strict.replay(maps, set(legs))
    return detail, detail_metrics(detail)


def selection_order(events: pd.DataFrame) -> np.ndarray:
    ranked = events.copy()
    ranked["_open2_priority"] = numeric(ranked["open_times_at_signal"]).eq(2).astype(int)
    ranked["_recent_amount_rank"] = numeric(
        ranked["signal_recent_5m_amount_vs_prev_day"]
    ).fillna(-np.inf)
    return ranked.sort_values(
        [
            "trade_date", "signal_hhmm", "_open2_priority",
            "_recent_amount_rank", "ts_code", "event_id",
        ],
        ascending=[True, True, False, False, True, True],
    ).index.to_numpy(dtype=int)


def daily_first_indexes(
    mask: np.ndarray, order: np.ndarray, dates: np.ndarray
) -> np.ndarray:
    eligible = order[mask[order]]
    if len(eligible) == 0:
        return eligible
    eligible_dates = dates[eligible]
    first = np.r_[True, eligible_dates[1:] != eligible_dates[:-1]]
    return eligible[first]


def signature(indexes: np.ndarray) -> str:
    return hashlib.sha1(np.asarray(indexes, dtype=np.int64).tobytes()).hexdigest()


def evaluate_rule_space(
    events: pd.DataFrame,
    rules: list[RuleSpec],
    baseline_outcomes: pd.DataFrame,
    baseline_picks: pd.DataFrame,
    other_legs: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, np.ndarray], dict[str, dict[str, Any]]]:
    """先搜索独立赚钱组合，再把赚钱组合代入合并D和ACDE。"""

    calendar = [
        date for date in strict.baseline_dates()
        if START <= str(date) <= END
    ]
    dates = events["trade_date"].astype(str).to_numpy()
    order = selection_order(events)
    option_by_name = {
        option.name: option
        for options in (time_options(), open_count_options(), speed_options(), quality_options())
        for option in options
    }
    mask_cache = {
        name: option.predicate(events).fillna(False).to_numpy(dtype=bool)
        for name, option in option_by_name.items()
    }

    signature_indexes: dict[str, np.ndarray] = {}
    signature_results: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for counter, rule in enumerate(rules, 1):
        mask = np.ones(len(events), dtype=bool)
        for option in rule.options:
            mask &= mask_cache[option.name]
        indexes = daily_first_indexes(mask, order, dates)
        key = signature(indexes)
        signature_indexes.setdefault(key, indexes)
        if key not in signature_results:
            picks = events.iloc[indexes].copy()
            outcomes = outcome_frame_from_picks(picks)
            records = fast_standalone_records(outcomes, calendar)
            values = [float(row["account_return"]) for row in records]
            first_values = [
                float(row["account_return"])
                for row in records
                if START <= str(row["signal_date"]) <= FIRST_12M_END
            ]
            second_values = [
                float(row["account_return"])
                for row in records
                if SECOND_12M_START <= str(row["signal_date"]) <= END
            ]
            signature_results[key] = {
                "candidate_day_count": int(len(picks)),
                "selected_price_confirmed_count": int(picks["queue_price_confirmed"].sum()),
                "selected_queue_unknown_count": int((~picks["queue_price_confirmed"].astype(bool)).sum()),
                "unresolved_exit_count": int(
                    (
                        picks["queue_price_confirmed"].astype(bool)
                        & ~picks["execution_status"].astype(str).eq("OK")
                    ).sum()
                ),
                **flatten("independent", basic_metrics(values)),
                "first_12m_trade_count": len(first_values),
                "first_12m_multiple": basic_metrics(first_values)["equity_multiple"],
                "second_12m_trade_count": len(second_values),
                "second_12m_multiple": basic_metrics(second_values)["equity_multiple"],
            }
        result = signature_results[key]
        rows.append(
            {
                "rule": rule.name,
                "family": rule.family,
                "description": rule.description,
                "signal_time_fields": ",".join(rule.fields),
                "uses_only_signal_time_known_fields": True,
                "selection_signature": key,
                "raw_event_count": int(mask.sum()),
                **result,
            }
        )
        if counter % 500 == 0:
            LOGGER.info("回封条件组合独立复利进度：%d/%d", counter, len(rules))

    search = pd.DataFrame(rows)
    search["equivalent_rule_count"] = search.groupby("selection_signature")["rule"].transform("size")
    search["independent_profitable"] = (
        search["independent_trade_count"].ge(MIN_PROFITABLE_TRADE_COUNT)
        & search["independent_equity_multiple"].gt(1.0 + TOLERANCE)
        & search["unresolved_exit_count"].eq(0)
    )

    profitable_signatures = list(
        search.loc[search["independent_profitable"], "selection_signature"].drop_duplicates()
    )
    LOGGER.info(
        "独立赚钱组合：%d条规则/%d个唯一当日选择，开始代入合并D和ACDE",
        int(search["independent_profitable"].sum()),
        len(profitable_signatures),
    )
    integration: dict[str, dict[str, Any]] = {}
    for counter, key in enumerate(profitable_signatures, 1):
        picks = events.iloc[signature_indexes[key]].copy()
        merged_outcomes, chosen = merge_picks_with_baseline(picks, baseline_picks)
        merged_detail = replay_d_only(merged_outcomes, START, END)
        merged_metrics = basic_metrics(
            pd.to_numeric(
                merged_detail.loc[merged_detail["status"].eq("EXECUTED"), "account_return"],
                errors="raise",
            )
        )
        combo_detail, combo_metrics = combo_replay_fast(merged_outcomes, other_legs)
        combo_without_counts = {
            metric: value
            for metric, value in combo_metrics.items()
            if metric != "leg_counts"
        }
        integration[key] = {
            **flatten("merged_d", merged_metrics),
            **flatten("acde", combo_without_counts),
            "merged_chosen_candidate_day_count": int(chosen["source_priority"].eq(1).sum()),
            "merged_chosen_baseline_day_count": int(chosen["source_priority"].eq(0).sum()),
            "acde_d_count": int(combo_metrics["leg_counts"].get("D", 0)),
            "acde_a_count": int(combo_metrics["leg_counts"].get("A", 0)),
            "acde_e_count": int(combo_metrics["leg_counts"].get("E", 0)),
            "acde_c_count": int(combo_metrics["leg_counts"].get("C", 0)),
        }
        if counter % 100 == 0:
            LOGGER.info("赚钱回封组合三层代入进度：%d/%d", counter, len(profitable_signatures))

    for key, values in integration.items():
        mask = search["selection_signature"].eq(key)
        for column, value in values.items():
            search.loc[mask, column] = value
    expected_integration_columns = [
        "merged_d_trade_count",
        "merged_d_win_rate",
        "merged_d_avg_account_return",
        "merged_d_median_account_return",
        "merged_d_equity_multiple",
        "merged_d_max_drawdown",
        "merged_d_max_profit",
        "merged_d_max_loss",
        "merged_d_profit_loss_ratio",
        "merged_d_max_consecutive_losses",
        "acde_trade_count",
        "acde_win_rate",
        "acde_avg_account_return",
        "acde_median_account_return",
        "acde_equity_multiple",
        "acde_max_drawdown",
        "acde_max_profit",
        "acde_max_loss",
        "acde_profit_loss_ratio",
        "acde_max_consecutive_losses",
        "merged_chosen_candidate_day_count",
        "merged_chosen_baseline_day_count",
        "acde_d_count",
        "acde_a_count",
        "acde_e_count",
        "acde_c_count",
    ]
    for column in expected_integration_columns:
        if column not in search:
            search[column] = np.nan
    search["merged_d_compound_improved"] = search.get(
        "merged_d_equity_multiple", pd.Series(np.nan, index=search.index)
    ).gt(BASELINE_D_MULTIPLE + TOLERANCE)
    search["acde_compound_improved"] = search.get(
        "acde_equity_multiple", pd.Series(np.nan, index=search.index)
    ).gt(BASELINE_ACDE_MULTIPLE + TOLERANCE)
    search["triple_gate_passed"] = (
        search["independent_profitable"]
        & search["merged_d_compound_improved"]
        & search["acde_compound_improved"]
    )
    search["formal_strategy_modified"] = False
    return search, signature_indexes, signature_results


def candidate_picks_for_rules(
    rule_names: tuple[str, ...],
    search: pd.DataFrame,
    signature_indexes: dict[str, np.ndarray],
    events: pd.DataFrame,
) -> pd.DataFrame:
    pieces: list[pd.DataFrame] = []
    for priority, name in enumerate(rule_names, 1):
        row = search[search["rule"].eq(name)].iloc[0]
        picks = events.iloc[signature_indexes[str(row["selection_signature"])]].copy()
        picks["source_rule"] = name
        picks["candidate_rule_priority"] = priority
        pieces.append(picks)
    if not pieces:
        return events.iloc[0:0].copy()
    all_picks = pd.concat(pieces, ignore_index=True).drop_duplicates("event_id", keep="first")
    return (
        all_picks.sort_values(
            ["trade_date", "signal_hhmm", "candidate_rule_priority", "ts_code", "event_id"]
        )
        .groupby("trade_date", as_index=False)
        .head(1)
        .reset_index(drop=True)
    )


def evaluate_passed_subsets(
    search: pd.DataFrame,
    signature_indexes: dict[str, np.ndarray],
    events: pd.DataFrame,
    baseline_picks: pd.DataFrame,
    other_legs: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, tuple[str, ...], pd.DataFrame, pd.DataFrame]:
    unique_passed = (
        search[search["triple_gate_passed"]]
        .sort_values(
            ["acde_equity_multiple", "merged_d_equity_multiple", "independent_equity_multiple"],
            ascending=False,
        )
        .drop_duplicates("selection_signature", keep="first")
        .head(MAX_FINAL_SUBSET_RULES)
    )
    names = list(unique_passed["rule"].astype(str))
    rows: list[dict[str, Any]] = []
    details: dict[tuple[str, ...], tuple[pd.DataFrame, pd.DataFrame]] = {}
    subsets: list[tuple[str, ...]] = [tuple()]
    for size in range(1, min(MAX_FINAL_SUBSET_SIZE, len(names)) + 1):
        subsets.extend(itertools.combinations(names, size))

    for subset in subsets:
        candidate_picks = candidate_picks_for_rules(
            subset, search, signature_indexes, events
        )
        merged_outcomes, chosen = merge_picks_with_baseline(candidate_picks, baseline_picks)
        d_detail = replay_d_only(merged_outcomes, START, END)
        d_metrics = basic_metrics(
            pd.to_numeric(
                d_detail.loc[d_detail["status"].eq("EXECUTED"), "account_return"],
                errors="raise",
            )
        )
        combo_detail, combo_metrics = combo_replay_fast(merged_outcomes, other_legs)
        passed = (
            float(d_metrics["equity_multiple"]) > BASELINE_D_MULTIPLE + TOLERANCE
            and float(combo_metrics["equity_multiple"]) > BASELINE_ACDE_MULTIPLE + TOLERANCE
        ) if subset else True
        rows.append(
            {
                "rules": ";".join(subset) if subset else "FORMAL_D_BASELINE_ONLY",
                "added_substyle_count": len(subset),
                "candidate_chosen_day_count": int(chosen["source_priority"].eq(1).sum()),
                **flatten("d", d_metrics),
                **flatten("acde", combo_metrics),
                "final_dual_gate_passed": bool(passed),
            }
        )
        details[subset] = (d_detail, combo_detail)
    result = pd.DataFrame(rows)
    passed_nonempty = result[
        result["final_dual_gate_passed"] & result["added_substyle_count"].gt(0)
    ]
    if passed_nonempty.empty:
        selected: tuple[str, ...] = tuple()
    else:
        text = str(
            passed_nonempty.sort_values(
                ["acde_equity_multiple", "d_equity_multiple"], ascending=False
            ).iloc[0]["rules"]
        )
        selected = tuple(text.split(";"))
    return result, selected, details[selected][0], details[selected][1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="系统研究D回封板条件组合")
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    parser.add_argument("--minute-bars", type=Path, default=MINUTE_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--rebuild-events",
        action="store_true",
        help="忽略已完成的全部回封中间账本并重新扫描873MB分钟文件",
    )
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

    LOGGER.info("加载并冻结40,336只次首板触板母池")
    ledger, ledger_audit = load_ledger(ledger_path)
    daily_data = strict.daily_data()
    event_cache_path = output_dir / "all_reseal_signal_events.csv"
    if event_cache_path.exists() and not args.rebuild_events:
        LOGGER.info("读取已完成的全部回封中间账本：%s", event_cache_path)
        events, event_audit = load_cached_reseal_events(event_cache_path)
    else:
        LOGGER.info("提取全部14:55前回封事件，不限制时间或炸板次数")
        events, event_audit = extract_reseal_events(ledger, minute_path, daily_data)
        LOGGER.info("计算全部回封股票日的T+2严格退出结果")
        events = attach_outcomes(events, OutcomeCache(daily_data))
        # 全量分钟扫描成本较高；完成即落中间账本，后续门禁若主动失败也保留审计证据。
        events.to_csv(event_cache_path, index=False, encoding="utf-8-sig")

    source, source_audit = strict.source_audit()
    if not bool(source_audit.get("passed")):
        raise RuntimeError("严格as-of正式源审计未通过")
    baseline_outcomes = strict.build_d(source, daily_data)
    baseline_detail = replay_d_only(baseline_outcomes, START, END)
    baseline_metrics = executed_metrics(baseline_detail)
    other_legs = build_current_other_legs()
    baseline_combo_detail, baseline_combo_metrics = combo_replay(
        baseline_outcomes, other_legs
    )
    assert_formal_baseline(baseline_metrics, baseline_combo_metrics)
    baseline_picks, baseline_signal_time_audit = prepare_baseline_signal_picks(
        baseline_outcomes, source, events
    )

    rules = build_rule_space()
    LOGGER.info("开始系统搜索%d条回封条件组合", len(rules))
    search, signature_indexes, _ = evaluate_rule_space(
        events, rules, baseline_outcomes, baseline_picks, other_legs
    )
    profitable = search[search["independent_profitable"]].copy()
    passed = search[search["triple_gate_passed"]].copy()
    LOGGER.info(
        "回封组合搜索完成：独立赚钱%d条，三层门禁通过%d条",
        len(profitable),
        len(passed),
    )

    subset_search, selected, final_d_detail, final_combo_detail = evaluate_passed_subsets(
        search, signature_indexes, events, baseline_picks, other_legs
    )
    final_d_metrics = executed_metrics(final_d_detail)
    final_combo_metrics = strict.combo_metrics(final_combo_detail)
    decision = (
        "KEEP_FORMAL_D_NO_RESEAL_COMBINATION_PASSED_ALL_GATES"
        if not selected
        else "RESEAL_COMBINATION_CANDIDATE_FOUND_KEEP_FORMAL_D_PENDING_ROBUSTNESS_VALIDATION"
    )

    unique_profitable = (
        profitable.sort_values(
            ["independent_equity_multiple", "acde_equity_multiple"],
            ascending=False,
        )
        .drop_duplicates("selection_signature", keep="first")
    )
    unique_passed = (
        passed.sort_values(
            ["acde_equity_multiple", "merged_d_equity_multiple"], ascending=False
        )
        .drop_duplicates("selection_signature", keep="first")
    )
    summary = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat(),
        "protocol": STRICT_DISCOVERY,
        "window": f"{START}~{END}",
        "strategy": "D_RESEAL_FAMILY",
        "research_objective": "把D视为回封板策略族，系统寻找多种可赚钱条件组合并逐一代入复利比较",
        "formal_strategy_modified": False,
        "release_eligible": False,
        "input_audit": {
            **ledger_audit,
            **event_audit,
            "minute_path": str(minute_path.relative_to(ROOT)),
            "minute_sha256": sha256(minute_path),
            "strict_source_audit_passed": True,
            "formal_d_signal_time_mapping": baseline_signal_time_audit,
        },
        "search_space": {
            "rule_count": len(search),
            "unique_daily_selection_count": int(search["selection_signature"].nunique()),
            "independent_profitable_rule_count": int(len(profitable)),
            "independent_profitable_unique_selection_count": int(len(unique_profitable)),
            "triple_gate_passed_rule_count": int(len(passed)),
            "triple_gate_passed_unique_selection_count": int(len(unique_passed)),
            "minimum_profitable_trade_count": MIN_PROFITABLE_TRADE_COUNT,
            "axes": ["回封时间", "炸板/回封次数", "回封速度", "炸板深度", "量能", "盘中市场情绪", "市场板块", "价格路径"],
            "multiple_testing_warning": "全部组合在同一24个月内发现，属于多重比较；前后12个月只作稳定性披露，不能冒充未来样本外。",
        },
        "frozen_baseline": {
            "d": baseline_metrics,
            "acde": baseline_combo_metrics,
            "priority": "D>A>E>C",
            "position_pct": 0.825,
            "fees_slippage_limit_rules_t1_t2_unchanged": True,
        },
        "gate": {
            "combination_independent_trade_count_at_least": MIN_PROFITABLE_TRADE_COUNT,
            "combination_independent_multiple_must_exceed": 1.0,
            "merged_d_multiple_must_exceed": BASELINE_D_MULTIPLE,
            "acde_multiple_must_exceed": BASELINE_ACDE_MULTIPLE,
            "unresolved_exit_count_must_equal": 0,
        },
        "best_independent_profitable": unique_profitable.head(20).to_dict("records"),
        "all_passed_unique": unique_passed.to_dict("records"),
        "final_subset": {
            "selected_rules": list(selected),
            "d_metrics": final_d_metrics,
            "acde_metrics": final_combo_metrics,
            "decision": decision,
        },
        "formal_decision": decision,
        "limitations": [
            "一分钟K不能还原同一分钟内逐笔顺序；只使用信号后分钟价格穿透确认成交。",
            "始终封板缺少历史买一队列，主口径记为未成交，且挂单阻断更晚D信号。",
            "组合搜索只使用信号时点字段；最终封板、失败收盘和未来收益均未参与条件形成。",
            "当前24个月组合数量较多，任何通过项仍需按策略更新框架做更早6个月旁证和未来6个月前向观察。",
            "机械复利只用于同口径比较，不代表真实容量或未来收益。",
        ],
    }

    search.sort_values(
        ["triple_gate_passed", "independent_profitable", "acde_equity_multiple", "independent_equity_multiple"],
        ascending=[False, False, False, False],
    ).to_csv(output_dir / "all_combination_metrics.csv", index=False, encoding="utf-8-sig")
    profitable.sort_values(
        ["triple_gate_passed", "acde_equity_multiple", "independent_equity_multiple"],
        ascending=[False, False, False],
    ).to_csv(output_dir / "profitable_combinations.csv", index=False, encoding="utf-8-sig")
    unique_profitable.to_csv(
        output_dir / "profitable_unique_daily_selections.csv", index=False, encoding="utf-8-sig"
    )
    unique_passed.to_csv(
        output_dir / "triple_gate_passed_unique_combinations.csv", index=False, encoding="utf-8-sig"
    )
    subset_search.to_csv(
        output_dir / "passed_substyle_subset_comparison.csv", index=False, encoding="utf-8-sig"
    )
    baseline_detail.to_csv(
        output_dir / "baseline_d_standalone_detail.csv", index=False, encoding="utf-8-sig"
    )
    baseline_combo_detail.to_csv(
        output_dir / "baseline_acde_detail.csv", index=False, encoding="utf-8-sig"
    )
    final_d_detail.to_csv(
        output_dir / "final_candidate_d_standalone_detail.csv", index=False, encoding="utf-8-sig"
    )
    final_combo_detail.to_csv(
        output_dir / "final_candidate_acde_detail.csv", index=False, encoding="utf-8-sig"
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
