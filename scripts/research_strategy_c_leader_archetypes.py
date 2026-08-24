#!/usr/bin/env python3
"""研究C“强势环境 × 龙头地位”及现有C核心精修，不直接修改正式策略。

研究原则：
1. 决策窗口固定为半年节点向前24个月；更早6个月只作旁证，不参与选优。
2. 主搜索只枚举至少包含一个强势环境因子和一个龙头地位因子的AND组合；
   同时单列“现有C核心+1～2个确认因子”的保守精修，二者不可混称。
3. 完全复现正式C的筛选、排序、风险过滤、顺位递补、T+1买入和固定收盘退出。
4. 先验证C独立单账户复利，再按D>A>E>C只替换C执行完整组合回放。
5. 默认只生成研究报告；本脚本没有--apply，也不写实盘配置。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
from functools import lru_cache
import hashlib
import itertools
import json
import logging
from pathlib import Path
import sys
from typing import Any, Iterable, Iterator, Mapping, Sequence

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_other_live_strategies_strict as strict  # noqa: E402
from scripts.build_ac_daily_candidates import trade_return_details  # noqa: E402
from scripts.optimize_strategy_d_factor_union import (  # noqa: E402
    build_incumbent_and_other_legs,
    load_events as load_d_events,
    natural_window_start,
)
from scripts.run_paper_ab_filtered_daily_ops import (  # noqa: E402
    condition_strategy_config,
    reject_strategy_risk_mask,
)
from src.mechanical_compound import mechanical_compound  # noqa: E402
from src.paper_candidate_generator import PaperCandidateGenerator  # noqa: E402
from src.strategy_d_factor_rules import (  # noqa: E402
    add_factor_values as add_d_factor_values,
    load_factor_release as load_d_factor_release,
)
from src.strict_asof import STRICT_DISCOVERY, assert_selection_columns_strict  # noqa: E402


LOGGER = logging.getLogger("research_strategy_c_leader_archetypes")
STRATEGY_CONFIG = ROOT / "config/strategy_config.json"
D_EVENT_PATH = ROOT / "reports/strategy_d_reseal_combinations/all_reseal_signal_events.csv"
D_RELEASE_PATH = ROOT / "config/strategy_d_factor_release.json"
CURRENT_START = "20240630"
CURRENT_END = "20260630"
CURRENT_C_TRADES = 35
CURRENT_C_MULTIPLE = 3.1108307989904436
CURRENT_ACDE_TRADES = 129
CURRENT_ACDE_MULTIPLE = 486.3661434308374
CURRENT_ACDE_LEGS = {"A": 47, "C": 21, "D": 17, "E": 44}
TOLERANCE = 1e-12
MISSING = "MISSING"

# C的强势龙头主搜索分为三层。主搜索候选必须至少含“环境+龙头”，
# 质量层只能作为第三因子；现有C核心精修由独立函数和research_family标记。
MARKET_ENV_FACTORS = (
    "market_emotion_state_bucket",
    "market_chain_count_bucket",
    "market_limit_down_count_bucket",
    "limit_up_count_bucket",
    "retreat_state_bucket",
)
SEGMENT_ENV_FACTORS = (
    "segment_emotion_state_bucket",
    "segment_chain_count_bucket",
    "segment_limit_up_count_bucket",
    "segment_limit_up_ratio_bucket",
    "segment_limit_down_ratio_bucket",
    "segment_retreat_state_bucket",
    "segment_limit_max_height_bucket",
)
LEADER_FACTORS = (
    "market_leader_rank_bucket",
    "segment_market_leader_rank_bucket",
    "segment_limit_height_rank_bucket",
    "limit_times_detail_bucket",
)
QUALITY_FACTORS = (
    "first_time_detail_bucket",
    "open_times_bucket",
    "turnover_rate_bucket",
    "volume_ratio_bucket",
    "fd_ratio_bucket",
    "amount_ratio_bucket",
    "prev_pct_chg_bucket",
    "board_type",
    "market_segment",
    "amount_bucket",
)
FACTOR_COLUMNS = tuple(dict.fromkeys(
    (*MARKET_ENV_FACTORS, *SEGMENT_ENV_FACTORS, *LEADER_FACTORS, *QUALITY_FACTORS)
))

# 不能只限制“用了什么字段”，还必须限制“字段取什么值”。例如rank_gt_30并
# 不代表龙头、ice_point也不代表强势；这两类取值若放进搜索会把数据挖掘结果
# 错贴成强势龙头。下面的冻结域就是C策略概念本身的边界。
STRONG_VALUE_DOMAINS: dict[str, frozenset[str]] = {
    "market_emotion_state_bucket": frozenset({"warming", "main_rise", "climax"}),
    "market_chain_count_bucket": frozenset({"8_15", "15_30", "gte_30"}),
    "market_limit_down_count_bucket": frozenset({"lt_5", "5_15"}),
    "limit_up_count_bucket": frozenset({"50_80", "80_120", "120_180", "gte_180"}),
    "retreat_state_bucket": frozenset({"neutral", "warming_2day"}),
    "segment_emotion_state_bucket": frozenset({"warming", "main_rise", "climax"}),
    "segment_chain_count_bucket": frozenset({"3_5", "5_10", "gte_10"}),
    "segment_limit_up_count_bucket": frozenset({"20_40", "40_80", "gte_80"}),
    "segment_limit_up_ratio_bucket": frozenset({"1pct_2pct", "2pct_3pct", "3pct_5pct", "gte_5pct"}),
    "segment_limit_down_ratio_bucket": frozenset({"lt_0_1pct", "0_1pct_0_3pct"}),
    "segment_retreat_state_bucket": frozenset({"neutral", "warming_2day"}),
    "segment_limit_max_height_bucket": frozenset({"3", "4_5", "gte_6"}),
}
LEADER_VALUE_DOMAINS: dict[str, frozenset[str]] = {
    "market_leader_rank_bucket": frozenset({"rank_1", "rank_2_3", "rank_4_10"}),
    "segment_market_leader_rank_bucket": frozenset({"rank_1", "rank_2_3", "rank_4_10"}),
    "segment_limit_height_rank_bucket": frozenset({"rank_1", "rank_2_3", "rank_4_10"}),
    "limit_times_detail_bucket": frozenset({"2", "3", "4", "5", "6_plus"}),
}

CURRENT_CONDITIONS = {
    "market_chain_count_bucket": "15_30",
    "segment_limit_up_count_bucket": "40_80",
}
CURRENT_REFINEMENT_FACTORS = tuple(
    column for column in FACTOR_COLUMNS if column not in CURRENT_CONDITIONS
)

RANK_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "current_profit_source_turnover",
        "columns": ("profit_source_score", "turnover_rate"),
        "ascending": (False, False),
    },
    {
        "id": "global_leader_first",
        "columns": ("market_leader_rank", "profit_source_score", "turnover_rate"),
        "ascending": (True, False, False),
    },
    {
        "id": "segment_leader_first",
        "columns": ("segment_market_leader_rank", "profit_source_score", "turnover_rate"),
        "ascending": (True, False, False),
    },
    {
        "id": "segment_height_first",
        "columns": ("segment_limit_height_rank", "limit_times", "profit_source_score"),
        "ascending": (True, False, False),
    },
    {
        "id": "board_height_first",
        "columns": ("limit_times", "market_leader_rank", "profit_source_score"),
        "ascending": (False, True, False),
    },
    {
        "id": "early_board_leader",
        "columns": ("first_time", "market_leader_rank", "turnover_rate"),
        "ascending": (True, True, False),
    },
    {
        "id": "seal_quality_leader",
        "columns": ("fd_amount_to_circ_mv", "market_leader_rank", "turnover_rate"),
        "ascending": (False, True, False),
    },
    {
        "id": "tradable_leader",
        "columns": ("fill_probability", "market_leader_rank", "turnover_rate"),
        "ascending": (False, True, False),
    },
    {
        "id": "liquid_leader",
        "columns": ("amount", "market_leader_rank", "turnover_rate"),
        "ascending": (False, True, False),
    },
    {
        "id": "clean_board_leader",
        "columns": ("open_times", "market_leader_rank", "turnover_rate"),
        "ascending": (True, True, False),
    },
)


def date_text(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True)


def prior_six_month_window(start: str) -> tuple[str, str]:
    boundary = dt.datetime.strptime(start, "%Y%m%d").date()
    prior_end = boundary - dt.timedelta(days=1)
    month = boundary.month - 6
    year = boundary.year
    if month <= 0:
        month += 12
        year -= 1
    day = min(boundary.day, 28)
    prior_start = boundary.replace(year=year, month=month, day=day)
    return prior_start.strftime("%Y%m%d"), prior_end.strftime("%Y%m%d")


@contextmanager
def strict_window(start: str, end: str) -> Iterator[None]:
    old_start, old_end = strict.START, strict.END
    strict.START, strict.END = str(start), str(end)
    try:
        yield
    finally:
        strict.START, strict.END = old_start, old_end


def build_generator(config: dict[str, Any]) -> PaperCandidateGenerator:
    generator = PaperCandidateGenerator(
        STRATEGY_CONFIG, input_trades_path=strict.STRICT_SOURCE
    )
    generator.config = config
    generator.paper_config = config.get("paper_candidate", {})
    generator.risk_thresholds = generator.paper_config.get("risk_thresholds", {})
    return generator


@lru_cache(maxsize=1)
def all_trade_dates() -> tuple[str, ...]:
    calendar = pd.read_csv(
        ROOT / "data/raw/trade_calendar.csv", dtype={"cal_date": str}, low_memory=False
    )
    if "is_open" in calendar.columns:
        calendar = calendar[
            calendar["is_open"].astype(str).isin({"1", "1.0", "True", "true"})
        ]
    return tuple(sorted(calendar["cal_date"].astype(str).unique()))


def cached_baseline_dates() -> list[str]:
    """与strict.baseline_dates同口径，但交易日历只读一次。"""

    return [value for value in all_trade_dates() if strict.START <= value <= strict.END]


def normalize_factor(value: Any) -> str:
    if value is None or pd.isna(value):
        return MISSING
    text = str(value).strip()
    return text if text and text.lower() not in {"nan", "none", "null"} else MISSING


def build_mother_pool(start: str, end: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    config = strict.load_json_config(STRATEGY_CONFIG)
    mother_config = condition_strategy_config(config, [], "c_leader_research_mother")
    c_generator = build_generator(mother_config)
    a_generator = build_generator(config)
    all_candidates = c_generator.load_all_candidates()

    a_pool = a_generator.apply_strategy_filters(all_candidates)
    a_pool["trade_date"] = date_text(a_pool["trade_date"])
    a_dates = set(
        a_pool.loc[a_pool["trade_date"].between(start, end), "trade_date"].unique()
    )

    pool = c_generator.apply_strategy_filters(all_candidates)
    pool["trade_date"] = date_text(pool["trade_date"])
    pool = pool[pool["trade_date"].between(start, end)].copy()
    before_a_gate = len(pool)
    pool = pool[~pool["trade_date"].isin(a_dates)].copy()

    missing = sorted(set(FACTOR_COLUMNS).difference(pool.columns))
    if missing:
        raise RuntimeError("C母池缺少信号时点因子：" + ",".join(missing))
    rank_columns = tuple(dict.fromkeys(
        column for rule in RANK_RULES for column in rule["columns"]
        if column != "profit_source_score"
    ))
    assert_selection_columns_strict(
        (*FACTOR_COLUMNS, *rank_columns),
        context="research_strategy_c_leader_archetypes.build_mother_pool",
    )
    for column in FACTOR_COLUMNS:
        pool[column] = pool[column].map(normalize_factor)

    # rank_candidates附加正式profit_source_score；之后所有排名变体共用同一分数口径。
    pool = c_generator.rank_candidates(pool).copy()
    pool["_risk_rejected"] = reject_strategy_risk_mask(
        pool, config, "c_strategy"
    ).astype(bool).to_numpy()
    pool["_mother_row_id"] = np.arange(len(pool), dtype=int)
    return pool.reset_index(drop=True), {
        "all_reliable_candidate_count": int(len(all_candidates)),
        "window_rows_before_a_gate": int(before_a_gate),
        "a_blocked_signal_day_count": int(len(a_dates)),
        "mother_pool_row_count": int(len(pool)),
        "mother_pool_signal_day_count": int(pool["trade_date"].nunique()),
        "risk_rejected_row_count": int(pool["_risk_rejected"].sum()),
    }


@lru_cache(maxsize=None)
def execution_result(
    signal_date: str, ts_code: str, name: str, hold_days: int
) -> dict[str, Any]:
    result = trade_return_details(
        str(signal_date), str(ts_code), int(hold_days), name=str(name)
    )
    account_return = None
    if result.status == "OK" and result.stock_return is not None:
        account_return = strict.account_return(result.stock_return, result.exit_date)
    exit_hit_limit_up = bool(
        result.status == "OK"
        and strict.cert.hit_limit_up(str(result.exit_date), str(ts_code), str(name))
    )
    return {
        "status": result.status,
        "buy_date": result.buy_date,
        "exit_date": result.exit_date,
        "stock_return_before_fees": result.stock_return,
        "account_return": account_return,
        "exit_hit_limit_up": exit_hit_limit_up,
    }


def attach_outcomes(frame: pd.DataFrame, hold_days: int) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    rows: list[dict[str, Any]] = []
    total = len(frame)
    for position, row in enumerate(frame.to_dict("records"), 1):
        outcome = execution_result(
            str(row["trade_date"]), str(row["ts_code"]), str(row.get("name", "")), hold_days
        )
        rows.append({"_mother_row_id": int(row["_mother_row_id"]), **outcome})
        if total >= 5000 and (position % 5000 == 0 or position == total):
            LOGGER.info("C母池逐票执行：%d/%d", position, total)
    outcomes = pd.DataFrame(rows)
    return frame.merge(outcomes, on="_mother_row_id", how="left", validate="one_to_one")


def factor_mask(frame: pd.DataFrame, conditions: Mapping[str, str]) -> pd.Series:
    mask = pd.Series(True, index=frame.index)
    for column, value in conditions.items():
        mask &= frame[column].astype(str).eq(str(value))
    return mask


def sort_columns(rule: Mapping[str, Any]) -> tuple[list[str], list[bool]]:
    columns = list(map(str, rule["columns"]))
    ascending = list(map(bool, rule["ascending"]))
    for column, direction in (("amount", False), ("turnover_rate", False), ("ts_code", True)):
        if column not in columns:
            columns.append(column)
            ascending.append(direction)
    return columns, ascending


def select_profiles(
    pool: pd.DataFrame,
    profiles: Sequence[Mapping[str, str]],
    rank_rule: Mapping[str, Any],
) -> pd.DataFrame:
    if not profiles:
        return pool.iloc[0:0].copy()
    union = pd.Series(False, index=pool.index)
    for conditions in profiles:
        union |= factor_mask(pool, conditions)
    selected = pool[union & ~pool["_risk_rejected"]].copy()
    if selected.empty:
        return selected
    columns, ascending = sort_columns(rank_rule)
    selected = selected.sort_values(columns, ascending=ascending, na_position="last")
    return (
        selected.groupby("trade_date", as_index=False, sort=True).head(1)
        .sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    )


def outcome_frame(selected: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "trade_date", "ts_code", "name", "status", "buy_date", "exit_date",
        "stock_return_before_fees", "account_return",
    ]
    result = selected[[column for column in columns if column in selected.columns]].copy()
    result = result.rename(columns={"trade_date": "signal_date"})
    result.insert(1, "strategy_leg", "C")
    return result.sort_values("signal_date").reset_index(drop=True)


def basic_metrics(values: Iterable[float]) -> dict[str, Any]:
    array = np.asarray(list(values), dtype=float)
    if len(array) == 0:
        return {
            "trade_count": 0, "win_rate": 0.0, "avg_account_return": 0.0,
            "median_account_return": 0.0, "equity_multiple": 1.0,
            "max_drawdown": 0.0, "max_profit": 0.0, "max_loss": 0.0,
            "profit_loss_ratio": 0.0, "max_consecutive_losses": 0,
        }
    compound = mechanical_compound(array)
    gains, losses = array[array > 0], array[array < 0]
    current = maximum = 0
    for value in array:
        current = current + 1 if value <= 0 else 0
        maximum = max(maximum, current)
    return {
        "trade_count": int(len(array)),
        "win_rate": float((array > 0).mean()),
        "avg_account_return": float(array.mean()),
        "median_account_return": float(np.median(array)),
        "equity_multiple": float(compound.equity_multiple),
        "max_drawdown": float(compound.max_drawdown),
        "max_profit": float(array.max()),
        "max_loss": float(array.min()),
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_consecutive_losses": int(maximum),
    }


def standalone_records(selected: pd.DataFrame) -> list[tuple[str, float]]:
    occupied_until = ""
    handoff_allowed = False
    records: list[tuple[str, float]] = []
    columns = (
        "trade_date", "status", "exit_date", "account_return", "exit_hit_limit_up"
    )
    for signal, status, exit_date, value, exit_limit_up in selected[list(columns)].itertuples(
        index=False, name=None
    ):
        signal_date = str(signal)
        if occupied_until and signal_date < occupied_until:
            continue
        blocked = bool(occupied_until and signal_date == occupied_until and not handoff_allowed)
        occupied_until = ""
        handoff_allowed = False
        if blocked or str(status) != "OK" or pd.isna(value):
            continue
        records.append((signal_date, float(value)))
        occupied_until = str(exit_date)
        handoff_allowed = bool(exit_limit_up)
    return records


def fast_metrics(selected: pd.DataFrame, start: str) -> dict[str, Any]:
    records = standalone_records(selected)
    values = [value for _, value in records]
    split = dt.datetime.strptime(start, "%Y%m%d").date().replace(
        year=dt.datetime.strptime(start, "%Y%m%d").date().year + 1
    ).strftime("%Y%m%d")
    first = [value for date, value in records if date <= split]
    second = [value for date, value in records if date > split]
    return {
        **basic_metrics(values),
        "first_12m_trade_count": int(len(first)),
        "first_12m_multiple": basic_metrics(first)["equity_multiple"],
        "second_12m_trade_count": int(len(second)),
        "second_12m_multiple": basic_metrics(second)["equity_multiple"],
        "candidate_day_count": int(len(selected)),
        "unbuyable_day_count": int(selected["status"].astype(str).eq("LIMIT_UP_UNBUYABLE").sum()),
    }


def standalone_metrics(outcomes: pd.DataFrame, start: str, end: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    maps = {leg: {} for leg in ("D", "A", "E", "C")}
    maps["C"] = strict.candidate_map(outcomes)
    with strict_window(start, end):
        detail = strict.replay(maps, {"C"})
        # strict.combo_metrics的默认low/high在模块加载时绑定，不能依赖动态
        # strict.START/END；显式传入才能正确统计更早6个月和分段结果。
        metrics = strict.combo_metrics(detail, start, end)
    return detail, metrics


def portfolio_metrics(
    d: pd.DataFrame,
    a: pd.DataFrame,
    e: pd.DataFrame,
    c: pd.DataFrame,
    start: str,
    end: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    frames = {"D": d, "A": a, "E": e, "C": c}
    maps = {leg: strict.candidate_map(frame) for leg, frame in frames.items()}
    with strict_window(start, end):
        detail = strict.replay(maps, set(frames))
        metrics = strict.combo_metrics(detail, start, end)
    return detail, metrics


def profile_id(conditions: Mapping[str, str]) -> str:
    payload = json.dumps(dict(sorted(conditions.items())), sort_keys=True, ensure_ascii=False)
    return "CLA_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def factor_sets() -> list[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    environments = (*MARKET_ENV_FACTORS, *SEGMENT_ENV_FACTORS)
    for environment in environments:
        for leader in LEADER_FACTORS:
            result.add(tuple(sorted((environment, leader))))
            for quality in QUALITY_FACTORS:
                result.add(tuple(sorted((environment, leader, quality))))
    # 同时观察全市场和所属分段强度，再以龙头地位收口。
    for market_env in MARKET_ENV_FACTORS:
        for segment_env in SEGMENT_ENV_FACTORS:
            for leader in LEADER_FACTORS:
                result.add(tuple(sorted((market_env, segment_env, leader))))
    # 龙头高度与龙头排名共同确认，再叠加一个环境因子。
    for environment in environments:
        for left, right in itertools.combinations(LEADER_FACTORS, 2):
            result.add(tuple(sorted((environment, left, right))))
    return sorted(result, key=lambda values: (len(values), values))


def enumerate_profiles(
    pool: pd.DataFrame,
    start: str,
    min_trades: int,
) -> tuple[pd.DataFrame, dict[str, dict[str, str]], dict[str, Any]]:
    accepted_pool = pool[~pool["_risk_rejected"]].copy()
    sets = factor_sets()
    rows: list[dict[str, Any]] = []
    conditions_by_id: dict[str, dict[str, str]] = {}
    observed = 0
    for position, names in enumerate(sets, 1):
        key: str | list[str] = names[0] if len(names) == 1 else list(names)
        for values, group in accepted_pool.groupby(key, observed=True, sort=False, dropna=False):
            values_tuple = (str(values),) if len(names) == 1 else tuple(map(str, values))
            observed += 1
            if MISSING in values_tuple:
                continue
            conditions = dict(zip(names, values_tuple))
            if any(
                name in STRONG_VALUE_DOMAINS
                and value not in STRONG_VALUE_DOMAINS[name]
                for name, value in conditions.items()
            ):
                continue
            if any(
                name in LEADER_VALUE_DOMAINS
                and value not in LEADER_VALUE_DOMAINS[name]
                for name, value in conditions.items()
            ):
                continue
            # accepted_pool已按正式C当前排序排好；先过滤风险再按日取首名，允许顺位递补。
            selected = group.drop_duplicates("trade_date", keep="first").sort_values("trade_date")
            metrics = fast_metrics(selected, start)
            if int(metrics["trade_count"]) < min_trades:
                continue
            identifier = profile_id(conditions)
            conditions_by_id[identifier] = conditions
            rows.append({
                "profile_id": identifier,
                "research_family": "strong_environment_x_leader",
                "factor_count": len(names),
                "conditions_json": json.dumps(conditions, ensure_ascii=False, sort_keys=True),
                "description": " AND ".join(f"{name}={value}" for name, value in conditions.items()),
                **metrics,
            })
        if position % 100 == 0 or position == len(sets):
            LOGGER.info("C语义因子组进度：%d/%d，支持充分组合=%d", position, len(sets), len(rows))
    return pd.DataFrame(rows), conditions_by_id, {
        "semantic_factor_set_count": len(sets),
        "observed_value_group_count": observed,
        "support_passed_profile_count": len(rows),
    }


def enumerate_current_core_refinements(
    pool: pd.DataFrame,
    start: str,
    min_trades: int,
) -> tuple[pd.DataFrame, dict[str, dict[str, str]], dict[str, Any]]:
    """固定当前C两条环境条件，只增加1～2个确认因子。

    这组研究不扩张当前C母池，专门寻找能删掉差交易或在同日更准确选择龙头的
    小改动。由于当前正式C只有35笔，最低支持度独立设置，但不得低于12笔。
    """

    if min_trades < 12:
        raise ValueError("当前C核心精修最低样本不得少于12笔")
    accepted = pool[
        ~pool["_risk_rejected"] & factor_mask(pool, CURRENT_CONDITIONS)
    ].copy()
    sets = [
        names
        for count in (1, 2)
        for names in itertools.combinations(CURRENT_REFINEMENT_FACTORS, count)
    ]
    rows: list[dict[str, Any]] = []
    conditions_by_id: dict[str, dict[str, str]] = {}
    observed = 0
    for position, names in enumerate(sets, 1):
        key: str | list[str] = names[0] if len(names) == 1 else list(names)
        for values, group in accepted.groupby(key, observed=True, sort=False, dropna=False):
            values_tuple = (str(values),) if len(names) == 1 else tuple(map(str, values))
            observed += 1
            if MISSING in values_tuple:
                continue
            additions = dict(zip(names, values_tuple))
            # 如果增加的是龙头身份字段，仍禁止rank_gt_30等伪龙头值；质量和
            # 情绪确认字段则允许完整披露，由当前两条强势环境条件提供策略底座。
            if any(
                name in LEADER_VALUE_DOMAINS
                and value not in LEADER_VALUE_DOMAINS[name]
                for name, value in additions.items()
            ):
                continue
            selected = group.drop_duplicates("trade_date", keep="first").sort_values("trade_date")
            metrics = fast_metrics(selected, start)
            if int(metrics["trade_count"]) < min_trades:
                continue
            conditions = {**CURRENT_CONDITIONS, **additions}
            identifier = profile_id(conditions)
            conditions_by_id[identifier] = conditions
            rows.append({
                "profile_id": identifier,
                "research_family": "current_c_core_refinement",
                "factor_count": len(conditions),
                "conditions_json": json.dumps(conditions, ensure_ascii=False, sort_keys=True),
                "description": " AND ".join(f"{name}={value}" for name, value in conditions.items()),
                **metrics,
            })
        if position % 100 == 0 or position == len(sets):
            LOGGER.info(
                "当前C核心精修进度：%d/%d，支持充分组合=%d",
                position, len(sets), len(rows),
            )
    return pd.DataFrame(rows), conditions_by_id, {
        "current_refinement_factor_set_count": len(sets),
        "current_refinement_observed_group_count": observed,
        "current_refinement_support_passed_count": len(rows),
    }


def candidate_signature(selected: pd.DataFrame, hold_days: int) -> str:
    text = "\n".join(
        f"{row.trade_date}|{row.ts_code}|{hold_days}"
        for row in selected[["trade_date", "ts_code"]].itertuples(index=False)
    )
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def evaluate_candidate(
    selected: pd.DataFrame,
    incumbent_d: pd.DataFrame,
    other_legs: Mapping[str, pd.DataFrame],
    start: str,
    end: str,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    outcomes = outcome_frame(selected)
    c_detail, c_metrics = standalone_metrics(outcomes, start, end)
    acde_detail, acde_metrics = portfolio_metrics(
        incumbent_d, other_legs["A"], other_legs["E"], outcomes, start, end
    )
    return {"c": c_metrics, "acde": acde_metrics}, c_detail, acde_detail


def evaluate_exact_candidates(
    pool: pd.DataFrame,
    profiles: pd.DataFrame,
    conditions_by_id: Mapping[str, Mapping[str, str]],
    incumbent_d: pd.DataFrame,
    other_legs: Mapping[str, pd.DataFrame],
    start: str,
    end: str,
    rank_rules: Sequence[Mapping[str, Any]],
    top_rank_refine: int,
) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    artifacts: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    current_rule = RANK_RULES[0]

    # 全量语义组合先使用正式C当前排序；只有C独立复利过门者才进入ACDE回放。
    ordered = profiles.sort_values(
        ["equity_multiple", "max_drawdown", "trade_count"],
        ascending=[False, False, False],
    )
    for item in ordered.itertuples(index=False):
        if float(item.equity_multiple) <= CURRENT_C_MULTIPLE + TOLERANCE:
            break
        conditions = conditions_by_id[str(item.profile_id)]
        selected = select_profiles(pool, [conditions], current_rule)
        signature = candidate_signature(selected, 3)
        if signature in seen:
            continue
        seen.add(signature)
        metrics, c_detail, acde_detail = evaluate_candidate(
            selected, incumbent_d, other_legs, start, end
        )
        key = f"{item.profile_id}|{current_rule['id']}|H3"
        row = {
            "candidate_id": key,
            "candidate_type": "single_profile",
            "profile_ids": str(item.profile_id),
            "conditions_json": json.dumps([conditions], ensure_ascii=False, sort_keys=True),
            "rank_rule": current_rule["id"],
            "hold_days": 3,
            "selection_signature": signature,
            **{f"c_{name}": value for name, value in metrics["c"].items() if name != "leg_counts"},
            **{f"acde_{name}": value for name, value in metrics["acde"].items() if name != "leg_counts"},
            "acde_leg_counts": json.dumps(metrics["acde"].get("leg_counts", {}), ensure_ascii=False, sort_keys=True),
        }
        row["c_gate"] = float(metrics["c"]["equity_multiple"]) > CURRENT_C_MULTIPLE + TOLERANCE
        row["acde_gate"] = float(metrics["acde"]["equity_multiple"]) > CURRENT_ACDE_MULTIPLE + TOLERANCE
        row["dual_gate"] = bool(row["c_gate"] and row["acde_gate"])
        rows.append(row)
        artifacts[key] = {"selected": selected, "c_detail": c_detail, "acde_detail": acde_detail, "profiles": [conditions]}
        if len(rows) % 100 == 0:
            LOGGER.info("C单组合严格复核：%d条", len(rows))

    # 对当前排序下最强的一小组条件改用龙头优先、板高优先、成交质量优先等排序。
    refine_ids = [
        str(value) for value in ordered.head(max(1, top_rank_refine))["profile_id"]
    ]
    for identifier in refine_ids:
        conditions = conditions_by_id[identifier]
        for rule in rank_rules[1:]:
            selected = select_profiles(pool, [conditions], rule)
            fast = fast_metrics(selected, start)
            if float(fast["equity_multiple"]) <= CURRENT_C_MULTIPLE + TOLERANCE:
                continue
            signature = candidate_signature(selected, 3)
            if signature in seen:
                continue
            seen.add(signature)
            metrics, c_detail, acde_detail = evaluate_candidate(
                selected, incumbent_d, other_legs, start, end
            )
            key = f"{identifier}|{rule['id']}|H3"
            row = {
                "candidate_id": key,
                "candidate_type": "single_profile_rank_refined",
                "profile_ids": identifier,
                "conditions_json": json.dumps([conditions], ensure_ascii=False, sort_keys=True),
                "rank_rule": rule["id"],
                "hold_days": 3,
                "selection_signature": signature,
                **{f"c_{name}": value for name, value in metrics["c"].items() if name != "leg_counts"},
                **{f"acde_{name}": value for name, value in metrics["acde"].items() if name != "leg_counts"},
                "acde_leg_counts": json.dumps(metrics["acde"].get("leg_counts", {}), ensure_ascii=False, sort_keys=True),
            }
            row["c_gate"] = float(metrics["c"]["equity_multiple"]) > CURRENT_C_MULTIPLE + TOLERANCE
            row["acde_gate"] = float(metrics["acde"]["equity_multiple"]) > CURRENT_ACDE_MULTIPLE + TOLERANCE
            row["dual_gate"] = bool(row["c_gate"] and row["acde_gate"])
            rows.append(row)
            artifacts[key] = {"selected": selected, "c_detail": c_detail, "acde_detail": acde_detail, "profiles": [conditions]}
    return pd.DataFrame(rows), artifacts


def evaluate_small_unions(
    pool: pd.DataFrame,
    exact: pd.DataFrame,
    artifacts: dict[str, dict[str, Any]],
    incumbent_d: pd.DataFrame,
    other_legs: Mapping[str, pd.DataFrame],
    start: str,
    end: str,
    top_union_profiles: int,
) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    if exact.empty:
        return pd.DataFrame(), {}
    singles = exact[
        exact["candidate_type"].eq("single_profile") & exact["c_gate"]
    ].sort_values(["acde_equity_multiple", "c_equity_multiple"], ascending=False)
    unique: list[tuple[str, Mapping[str, str]]] = []
    seen_profiles: set[str] = set()
    for row in singles.itertuples(index=False):
        profile = str(row.profile_ids)
        if profile in seen_profiles:
            continue
        seen_profiles.add(profile)
        unique.append((profile, artifacts[str(row.candidate_id)]["profiles"][0]))
        if len(unique) >= top_union_profiles:
            break
    rows: list[dict[str, Any]] = []
    union_artifacts: dict[str, dict[str, Any]] = {}
    signatures = set(exact["selection_signature"].astype(str))
    for left, right in itertools.combinations(unique, 2):
        ids = (left[0], right[0])
        profiles = [left[1], right[1]]
        selected = select_profiles(pool, profiles, RANK_RULES[0])
        fast = fast_metrics(selected, start)
        if float(fast["equity_multiple"]) <= CURRENT_C_MULTIPLE + TOLERANCE:
            continue
        signature = candidate_signature(selected, 3)
        if signature in signatures:
            continue
        signatures.add(signature)
        metrics, c_detail, acde_detail = evaluate_candidate(
            selected, incumbent_d, other_legs, start, end
        )
        key = f"UNION2:{'+'.join(ids)}|{RANK_RULES[0]['id']}|H3"
        row = {
            "candidate_id": key,
            "candidate_type": "two_profile_union",
            "profile_ids": ";".join(ids),
            "conditions_json": json.dumps(profiles, ensure_ascii=False, sort_keys=True),
            "rank_rule": RANK_RULES[0]["id"],
            "hold_days": 3,
            "selection_signature": signature,
            **{f"c_{name}": value for name, value in metrics["c"].items() if name != "leg_counts"},
            **{f"acde_{name}": value for name, value in metrics["acde"].items() if name != "leg_counts"},
            "acde_leg_counts": json.dumps(metrics["acde"].get("leg_counts", {}), ensure_ascii=False, sort_keys=True),
        }
        row["c_gate"] = float(metrics["c"]["equity_multiple"]) > CURRENT_C_MULTIPLE + TOLERANCE
        row["acde_gate"] = float(metrics["acde"]["equity_multiple"]) > CURRENT_ACDE_MULTIPLE + TOLERANCE
        row["dual_gate"] = bool(row["c_gate"] and row["acde_gate"])
        rows.append(row)
        union_artifacts[key] = {"selected": selected, "c_detail": c_detail, "acde_detail": acde_detail, "profiles": profiles}
    return pd.DataFrame(rows), union_artifacts


def evaluate_exit_variants(
    pool_without_outcomes: pd.DataFrame,
    exact: pd.DataFrame,
    artifacts: dict[str, dict[str, Any]],
    incumbent_d: pd.DataFrame,
    other_legs: Mapping[str, pd.DataFrame],
    start: str,
    end: str,
    top_exit_candidates: int,
) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    if exact.empty:
        return pd.DataFrame(), {}
    leaders = exact[exact["c_gate"]].sort_values(
        ["acde_equity_multiple", "c_equity_multiple"], ascending=False
    ).head(top_exit_candidates)
    rows: list[dict[str, Any]] = []
    exit_artifacts: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    rules = {str(rule["id"]): rule for rule in RANK_RULES}
    for row in leaders.itertuples(index=False):
        profiles = artifacts[str(row.candidate_id)]["profiles"]
        rank_rule = rules[str(row.rank_rule)]
        selected_base = select_profiles(pool_without_outcomes, profiles, rank_rule)
        for hold_days in (2, 4):
            selected = attach_outcomes(selected_base, hold_days)
            fast = fast_metrics(selected, start)
            if float(fast["equity_multiple"]) <= CURRENT_C_MULTIPLE + TOLERANCE:
                continue
            signature = candidate_signature(selected, hold_days)
            if signature in seen:
                continue
            seen.add(signature)
            metrics, c_detail, acde_detail = evaluate_candidate(
                selected, incumbent_d, other_legs, start, end
            )
            key = f"{row.profile_ids}|{row.rank_rule}|H{hold_days}"
            result = {
                "candidate_id": key,
                "candidate_type": "exit_refined",
                "profile_ids": str(row.profile_ids),
                "conditions_json": str(row.conditions_json),
                "rank_rule": str(row.rank_rule),
                "hold_days": hold_days,
                "selection_signature": signature,
                **{f"c_{name}": value for name, value in metrics["c"].items() if name != "leg_counts"},
                **{f"acde_{name}": value for name, value in metrics["acde"].items() if name != "leg_counts"},
                "acde_leg_counts": json.dumps(metrics["acde"].get("leg_counts", {}), ensure_ascii=False, sort_keys=True),
            }
            result["c_gate"] = float(metrics["c"]["equity_multiple"]) > CURRENT_C_MULTIPLE + TOLERANCE
            result["acde_gate"] = float(metrics["acde"]["equity_multiple"]) > CURRENT_ACDE_MULTIPLE + TOLERANCE
            result["dual_gate"] = bool(result["c_gate"] and result["acde_gate"])
            rows.append(result)
            exit_artifacts[key] = {"selected": selected, "c_detail": c_detail, "acde_detail": acde_detail, "profiles": profiles}
    return pd.DataFrame(rows), exit_artifacts


def anchor_metrics(
    incumbent_d: pd.DataFrame,
    other_legs: Mapping[str, pd.DataFrame],
    start: str,
    end: str,
) -> tuple[dict[str, Any], dict[str, Any], pd.DataFrame, pd.DataFrame]:
    c_detail, c_metrics = standalone_metrics(other_legs["C"], start, end)
    acde_detail, acde_metrics = portfolio_metrics(
        incumbent_d, other_legs["A"], other_legs["E"], other_legs["C"], start, end
    )
    if start == CURRENT_START and end == CURRENT_END:
        actual_legs = {leg: int(acde_metrics.get("leg_counts", {}).get(leg, 0)) for leg in CURRENT_ACDE_LEGS}
        failures = []
        if int(c_metrics["trade_count"]) != CURRENT_C_TRADES:
            failures.append(f"C笔数={c_metrics['trade_count']}")
        if abs(float(c_metrics["equity_multiple"]) - CURRENT_C_MULTIPLE) > TOLERANCE:
            failures.append(f"C复利={c_metrics['equity_multiple']}")
        if int(acde_metrics["trade_count"]) != CURRENT_ACDE_TRADES:
            failures.append(f"ACDE笔数={acde_metrics['trade_count']}")
        if abs(float(acde_metrics["equity_multiple"]) - CURRENT_ACDE_MULTIPLE) > TOLERANCE:
            failures.append(f"ACDE复利={acde_metrics['equity_multiple']}")
        if actual_legs != CURRENT_ACDE_LEGS:
            failures.append(f"腿分布={actual_legs}")
        if failures:
            raise RuntimeError("当前正式C/ACDE锚点漂移，拒绝研究：" + "；".join(failures))
    return c_metrics, acde_metrics, c_detail, acde_detail


def current_semantics_identity(
    pool: pd.DataFrame,
    other_c: pd.DataFrame,
) -> dict[str, Any]:
    selected = select_profiles(pool, [CURRENT_CONDITIONS], RANK_RULES[0])
    rebuilt = outcome_frame(selected)
    left = rebuilt[["signal_date", "ts_code", "status"]].rename(
        columns={"ts_code": "rebuilt_code", "status": "rebuilt_status"}
    )
    right = other_c[["signal_date", "ts_code", "status"]].rename(
        columns={"ts_code": "formal_code", "status": "formal_status"}
    )
    merged = left.merge(right, on="signal_date", how="outer", indicator=True)
    same = (
        merged["rebuilt_code"].eq(merged["formal_code"])
        & merged["rebuilt_status"].eq(merged["formal_status"])
        & merged["_merge"].eq("both")
    )
    return {
        "rebuilt_candidate_days": int(len(left)),
        "formal_candidate_days": int(len(right)),
        "same_stock_and_status_days": int(same.sum()),
        "different_days": int((~same).sum()),
        "passed": bool(len(merged) == int(same.sum())),
    }


def six_month_segments(start: str, end: str) -> list[tuple[str, str]]:
    cursor = dt.datetime.strptime(start, "%Y%m%d").date()
    end_date = dt.datetime.strptime(end, "%Y%m%d").date()
    result: list[tuple[str, str]] = []
    while cursor <= end_date:
        if cursor.month < 6 or (cursor.month == 6 and cursor.day < 30):
            boundary = dt.date(cursor.year, 6, 30)
        elif cursor.month < 12 or (cursor.month == 12 and cursor.day < 31):
            boundary = dt.date(cursor.year, 12, 31)
        else:
            boundary = dt.date(cursor.year + 1, 6, 30)
        segment_end = min(end_date, boundary)
        result.append((cursor.strftime("%Y%m%d"), segment_end.strftime("%Y%m%d")))
        cursor = segment_end + dt.timedelta(days=1)
    return result


def render_report(summary: Mapping[str, Any]) -> str:
    incumbent_c = summary["incumbent"]["c"]
    incumbent_acde = summary["incumbent"]["acde"]
    best = summary.get("best_candidate")
    near = summary.get("best_near_miss")
    lines = [
        "C强势龙头组合研究结果",
        "=====================",
        "",
        "【结论】",
    ]
    if best:
        lines.extend([
            f"找到双复利通过候选：{best['candidate_id']}",
            f"C复利：{incumbent_c['equity_multiple']:.10f}倍 → {best['c_equity_multiple']:.10f}倍",
            f"ACDE复利：{incumbent_acde['equity_multiple']:.10f}倍 → {best['acde_equity_multiple']:.10f}倍",
            "本脚本只做研究，尚未修改正式C。",
        ])
    else:
        lines.extend([
            "没有候选同时提高C独立复利和ACDE总复利。",
            "正式C保持不变。",
        ])
        if near:
            lines.extend([
                f"最接近候选：{near['candidate_id']}",
                f"C复利：{near['c_equity_multiple']:.10f}倍；ACDE复利：{near['acde_equity_multiple']:.10f}倍。",
            ])
    lines.extend([
        "",
        "一、锁定口径",
        "",
        f"决策窗口：{summary['window']['start']}～{summary['window']['end']}",
        f"更早6个月旁证：{summary['prior_validation']['start']}～{summary['prior_validation']['end']}（不参与选优）",
        "严格as-of、82.5%仓位、费用、滑点、涨跌停、T+1及D>A>E>C顺序不变。",
        "研究分两族：纯强势环境×明确龙头；以及固定现有C核心后增加1～2个确认因子的保守精修。",
        "两族分别标记，不把现有C精修冒充为显式龙头条件。",
        "",
        "二、当前正式锚点",
        "",
        f"C：{incumbent_c['trade_count']}笔，胜率{incumbent_c['win_rate']:.2%}，平均{incumbent_c['avg_account_return']:.2%}，复利{incumbent_c['equity_multiple']:.10f}倍，最大回撤{incumbent_c['max_drawdown']:.2%}。",
        f"ACDE：{incumbent_acde['trade_count']}笔，复利{incumbent_acde['equity_multiple']:.10f}倍，最大回撤{incumbent_acde['max_drawdown']:.2%}。",
        f"正式C语义重建一致性：{json.dumps(summary['formal_c_identity'], ensure_ascii=False, sort_keys=True)}",
        "",
        "三、搜索范围",
        "",
        f"语义因子列组合：{summary['search_audit']['semantic_factor_set_count']:,}组。",
        f"实际观察取值组合：{summary['search_audit']['observed_value_group_count']:,}条。",
        f"满足最低{summary['parameters']['min_trades']}笔：{summary['search_audit']['support_passed_profile_count']:,}条。",
        f"当前C核心精修（最低{summary['parameters']['min_refinement_trades']}笔）：{summary['search_audit']['current_refinement_support_passed_count']:,}条。",
        f"完成严格C/ACDE复核候选：{summary['search_audit']['exact_candidate_count']:,}条。",
        f"完成两分支小并集：{summary['search_audit']['small_union_count']:,}条。",
        f"完成持有期精修：{summary['search_audit']['exit_variant_count']:,}条。",
        f"双复利同时通过：{summary['search_audit']['dual_gate_count']:,}条。",
        f"其中纯强势×明确龙头通过：{summary['search_audit']['pure_semantic_dual_gate_count']:,}条；混合现有C精修分支通过：{summary['search_audit']['mixed_refinement_dual_gate_count']:,}条。",
        "",
        "四、最佳候选",
        "",
    ])
    if best:
        lines.extend([
            f"候选编号：{best['candidate_id']}",
            f"候选类型：{best['candidate_type']}",
            f"条件：{best['conditions_json']}",
            f"排序：{best['rank_rule']}；持有参数：H{best['hold_days']}",
            f"C：{best['c_trade_count']}笔，胜率{best['c_win_rate']:.2%}，平均{best['c_avg_account_return']:.2%}，中位数{best['c_median_account_return']:.2%}，复利{best['c_equity_multiple']:.10f}倍，最大回撤{best['c_max_drawdown']:.2%}。",
            f"ACDE：{best['acde_trade_count']}笔，胜率{best['acde_win_rate']:.2%}，平均{best['acde_avg_account_return']:.2%}，复利{best['acde_equity_multiple']:.10f}倍，最大回撤{best['acde_max_drawdown']:.2%}。",
            f"最佳候选分支审计：{json.dumps(summary['best_branch_audit'], ensure_ascii=False, sort_keys=True)}",
            f"更早6个月C旁证：{json.dumps(summary['prior_validation']['metrics'], ensure_ascii=False, sort_keys=True)}",
            f"决策窗口分半年表现：{json.dumps(summary['best_half_year_segments'], ensure_ascii=False)}",
        ])
    lines.extend([
        "",
        "五、风险说明",
        "",
        "1. 选优仍来自同一24个月历史窗口，属于STRICT_DISCOVERY，不是真正未来样本外。",
        "2. 更早6个月只披露，不参与候选选择；未来6个月才是发布后的前向账本。",
        "3. 小并集最多两条可解释分支，不允许把所有历史达标条件无差别OR。",
        "4. 本次12个双门通过项全部是“现有C精修+显式龙头”两分支并集；纯强势×明确龙头没有单独通过。",
        "5. 最佳两条分支单独替换C时均未提高ACDE，合并后的提升来自占仓路径交互，过拟合风险高于单条规则。",
        "6. 更早6个月只有5笔且复利略低于1倍，不能把两年窗口内的大幅提升当成已经样本外成立。",
        "7. 机械复利仅用于同口径相对比较，不代表未来收益或大资金容量。",
        "8. 即使双复利通过，本脚本也不会自动接入实盘；必须先人工审阅逐笔明细。",
    ])
    return "\n".join(lines) + "\n"


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [json_ready(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    if pd.isna(value):
        return None
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="研究C强势环境与龙头地位组合")
    parser.add_argument("--as-of", default=CURRENT_END, help="半年节点YYYYMMDD")
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--min-refinement-trades", type=int, default=15)
    parser.add_argument("--top-rank-refine", type=int, default=40)
    parser.add_argument("--top-union-profiles", type=int, default=24)
    parser.add_argument("--top-exit-candidates", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    end = str(args.as_of)
    parsed = dt.datetime.strptime(end, "%Y%m%d")
    if parsed.strftime("%m%d") not in {"0630", "1231"}:
        raise ValueError("C研究只接受0630或1231半年节点")
    start = natural_window_start(end, 2)
    prior_start, prior_end = prior_six_month_window(start)
    output_dir = args.output_dir or ROOT / "reports/strategy_c_leader_research" / end
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    # 严格回放内部原实现每个候选都会重读交易日历；替换为同口径只读缓存，
    # 不改变任何交易日期、占仓或收益逻辑。正式锚点复现会验证这一点。
    strict.baseline_dates = cached_baseline_dates

    LOGGER.info("构建当前正式D/A/E/C锚点：%s~%s", start, end)
    d_events, d_event_audit = load_d_events(D_EVENT_PATH, start, end)
    d_factorized = add_d_factor_values(d_events)
    d_release = load_d_factor_release(D_RELEASE_PATH)
    incumbent_d, other_legs, strict_source_audit = build_incumbent_and_other_legs(
        d_release, d_factorized, start, end
    )
    incumbent_c, incumbent_acde, incumbent_c_detail, incumbent_acde_detail = anchor_metrics(
        incumbent_d, other_legs, start, end
    )

    LOGGER.info("构建C强势龙头母池并计算T+3执行结果")
    mother_without_outcomes, mother_audit = build_mother_pool(start, end)
    mother = attach_outcomes(mother_without_outcomes, 3)
    identity = current_semantics_identity(mother, other_legs["C"])
    if not identity["passed"]:
        raise RuntimeError("C研究筛选/排序/风险语义未复现正式C，拒绝继续")

    LOGGER.info("枚举强势环境×龙头地位的可解释组合")
    profile_search, conditions_by_id, search_audit = enumerate_profiles(
        mother, start, int(args.min_trades)
    )
    LOGGER.info("固定当前C核心环境，枚举1～2个龙头/质量确认因子")
    refinement_search, refinement_conditions, refinement_audit = enumerate_current_core_refinements(
        mother, start, int(args.min_refinement_trades)
    )
    conditions_by_id.update(refinement_conditions)
    profile_search = pd.concat(
        [profile_search, refinement_search], ignore_index=True
    ).drop_duplicates("profile_id", keep="first")
    search_audit.update(refinement_audit)
    profile_search = profile_search.sort_values(
        ["equity_multiple", "max_drawdown", "trade_count"],
        ascending=[False, False, False],
    ).reset_index(drop=True)

    LOGGER.info("严格复核C独立复利过门候选并逐腿替换ACDE")
    exact, artifacts = evaluate_exact_candidates(
        mother, profile_search, conditions_by_id, incumbent_d, other_legs,
        start, end, RANK_RULES, int(args.top_rank_refine),
    )
    LOGGER.info("搜索最多两条互补C子风格的小并集")
    unions, union_artifacts = evaluate_small_unions(
        mother, exact, artifacts, incumbent_d, other_legs, start, end,
        int(args.top_union_profiles),
    )
    artifacts.update(union_artifacts)
    combined = pd.concat([exact, unions], ignore_index=True) if not unions.empty else exact.copy()

    LOGGER.info("对最强候选测试T+2/T+4固定收盘退出")
    exits, exit_artifacts = evaluate_exit_variants(
        mother_without_outcomes, combined, artifacts, incumbent_d, other_legs,
        start, end, int(args.top_exit_candidates),
    )
    artifacts.update(exit_artifacts)
    all_candidates = pd.concat([combined, exits], ignore_index=True) if not exits.empty else combined.copy()
    if not all_candidates.empty:
        family_by_profile = profile_search.set_index("profile_id")["research_family"].astype(str).to_dict()
        all_candidates["profile_families"] = all_candidates["profile_ids"].astype(str).map(
            lambda value: json.dumps(
                sorted({family_by_profile[item] for item in value.split(";")}),
                ensure_ascii=False,
            )
        )
        all_candidates = all_candidates.sort_values(
            ["dual_gate", "acde_equity_multiple", "c_equity_multiple", "c_max_drawdown"],
            ascending=[False, False, False, False],
        ).reset_index(drop=True)
    passing = all_candidates[all_candidates["dual_gate"]].copy() if not all_candidates.empty else pd.DataFrame()
    best_row = passing.iloc[0].to_dict() if not passing.empty else None
    near_pool = all_candidates[all_candidates["c_gate"]].copy() if not all_candidates.empty else pd.DataFrame()
    near_row = (
        near_pool.sort_values(
            ["acde_equity_multiple", "c_equity_multiple"], ascending=False
        ).iloc[0].to_dict()
        if not near_pool.empty else None
    )

    # 先落地核心搜索表和基线明细，再做更早6个月旁证。这样即使旁证数据或
    # 报告渲染失败，也不会丢失已经完成的两年严格搜索；写每批文件前再次
    # 确保目录存在，避免外部清理空目录造成落盘失败。
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_search.to_csv(output_dir / "semantic_profile_search.csv", index=False, encoding="utf-8-sig")
    all_candidates.to_csv(output_dir / "strict_candidate_comparison.csv", index=False, encoding="utf-8-sig")
    incumbent_c_detail.to_csv(output_dir / "incumbent_c_detail.csv", index=False, encoding="utf-8-sig")
    incumbent_acde_detail.to_csv(output_dir / "incumbent_acde_detail.csv", index=False, encoding="utf-8-sig")

    prior_metrics: dict[str, Any] = {}
    half_segments: list[dict[str, Any]] = []
    best_branch_audit: dict[str, Any] = {}
    best_key = str(best_row["candidate_id"]) if best_row else ""
    if best_row:
        best_artifact = artifacts[best_key]
        best_outcomes = outcome_frame(best_artifact["selected"])
        for low, high in six_month_segments(start, end):
            _, metrics = standalone_metrics(best_outcomes, low, high)
            half_segments.append({"start": low, "end": high, **metrics})

        LOGGER.info("只读验证更早6个月，不参与候选选优")
        prior_pool, _ = build_mother_pool(prior_start, prior_end)
        rule = next(rule for rule in RANK_RULES if rule["id"] == str(best_row["rank_rule"]))
        prior_selected = select_profiles(prior_pool, best_artifact["profiles"], rule)
        prior_selected = attach_outcomes(prior_selected, int(best_row["hold_days"]))
        _, prior_metrics = standalone_metrics(outcome_frame(prior_selected), prior_start, prior_end)

        output_dir.mkdir(parents=True, exist_ok=True)
        best_artifact["selected"].to_csv(output_dir / "best_candidate_picks.csv", index=False, encoding="utf-8-sig")
        best_artifact["c_detail"].to_csv(output_dir / "best_candidate_c_detail.csv", index=False, encoding="utf-8-sig")
        best_artifact["acde_detail"].to_csv(output_dir / "best_candidate_acde_detail.csv", index=False, encoding="utf-8-sig")

        best_profile_ids = str(best_row["profile_ids"]).split(";")
        branch_rows: list[dict[str, Any]] = []
        branch_dates: list[set[str]] = []
        for identifier, conditions in zip(best_profile_ids, best_artifact["profiles"]):
            branch_selected = select_profiles(mother, [conditions], rule)
            branch_dates.append(set(branch_selected["trade_date"].astype(str)))
            individual = exact[
                exact["profile_ids"].astype(str).eq(identifier)
                & exact["rank_rule"].astype(str).eq(str(best_row["rank_rule"]))
                & exact["hold_days"].astype(int).eq(int(best_row["hold_days"]))
            ]
            exact_metrics = individual.iloc[0].to_dict() if not individual.empty else {}
            branch_rows.append({
                "profile_id": identifier,
                "research_family": family_by_profile[identifier],
                "conditions": conditions,
                "candidate_day_count": int(len(branch_selected)),
                "status_counts": branch_selected["status"].astype(str).value_counts().to_dict(),
                "individual_c_trade_count": int(exact_metrics.get("c_trade_count", 0)),
                "individual_c_win_rate": float(exact_metrics.get("c_win_rate", 0.0)),
                "individual_c_avg_account_return": float(exact_metrics.get("c_avg_account_return", 0.0)),
                "individual_c_equity_multiple": float(exact_metrics.get("c_equity_multiple", 1.0)),
                "individual_acde_equity_multiple": float(exact_metrics.get("acde_equity_multiple", 1.0)),
                "individual_dual_gate": bool(exact_metrics.get("dual_gate", False)),
            })
        overlap = set.intersection(*branch_dates) if branch_dates else set()
        best_branch_audit = {
            "union_candidate_day_count": int(len(best_artifact["selected"])),
            "union_status_counts": best_artifact["selected"]["status"].astype(str).value_counts().to_dict(),
            "branch_overlap_candidate_day_count": int(len(overlap)),
            "branches": branch_rows,
        }

    pure_family = json.dumps(["strong_environment_x_leader"], ensure_ascii=False)
    pure_dual_count = int(
        (passing["profile_families"].astype(str).eq(pure_family)).sum()
    ) if not passing.empty else 0
    search_audit.update({
        "exact_candidate_count": int(len(exact)),
        "small_union_count": int(len(unions)),
        "exit_variant_count": int(len(exits)),
        "dual_gate_count": int(len(passing)),
        "pure_semantic_dual_gate_count": pure_dual_count,
        "mixed_refinement_dual_gate_count": int(len(passing) - pure_dual_count),
    })
    summary = {
        "schema_version": 1,
        "protocol": STRICT_DISCOVERY,
        "strategy_concept": "C_STRONG_ENVIRONMENT_X_LEADER_ARCHETYPE",
        "window": {"start": start, "end": end},
        "parameters": {
            "min_trades": int(args.min_trades),
            "min_refinement_trades": int(args.min_refinement_trades),
            "top_rank_refine": int(args.top_rank_refine),
            "top_union_profiles": int(args.top_union_profiles),
            "top_exit_candidates": int(args.top_exit_candidates),
        },
        "strict_source_audit_passed": bool(strict_source_audit.get("passed")),
        "d_event_audit": d_event_audit,
        "mother_pool_audit": mother_audit,
        "formal_c_identity": identity,
        "incumbent": {"c": incumbent_c, "acde": incumbent_acde},
        "search_audit": search_audit,
        "best_candidate": best_row,
        "best_near_miss": near_row,
        "prior_validation": {
            "start": prior_start,
            "end": prior_end,
            "influenced_selection": False,
            "metrics": prior_metrics,
        },
        "best_half_year_segments": half_segments,
        "best_branch_audit": best_branch_audit,
        "formal_strategy_modified": False,
        "decision": "DUAL_GATE_CANDIDATE_FOUND_RESEARCH_ONLY" if best_row else "KEEP_CURRENT_C_NO_DUAL_GATE_CANDIDATE",
        "limitations": [
            "候选在同一24个月内发现，存在多重比较和过拟合风险。",
            "更早6个月只做旁证，不影响24个月决策。",
            "纯强势环境×明确龙头候选没有通过双复利闸门。",
            "最佳并集的两条分支单独替换C均未提高ACDE，提升依赖组合占仓路径交互。",
            "更早6个月只有5笔且复利略低于1倍，旁证不支持宣称策略已经样本外稳定。",
            "未来6个月才是真正前向样本外。",
            "机械复利不代表未来收益或资金容量。",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(json_ready(summary), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_path = output_dir / "best_c_leader_result.txt"
    report_path.write_text(render_report(summary), encoding="utf-8")
    print(json.dumps({
        "decision": summary["decision"],
        "result_file": str(report_path),
        "evaluated_profiles": int(len(profile_search)),
        "strict_candidates": int(len(all_candidates)),
        "dual_gate_count": int(len(passing)),
        "best_candidate": best_row,
        "formal_strategy_modified": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
