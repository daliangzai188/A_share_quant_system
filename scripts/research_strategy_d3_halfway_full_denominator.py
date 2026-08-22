#!/usr/bin/env python3
"""用完整7%失败分母研究D3半路买入，并执行三层复利门禁。

信号定义严格为：某分钟收盘首次达到昨收7%/8%/9%，该分钟结束后才做判断，
下一分钟以开盘价加0.1%滑点买入；若下一分钟已整分钟封死涨停，没有价格穿透
证据则保守记为未成交。日线最高价只用于决定历史分钟采集分母，绝不参与信号、
过滤或排序。

每条候选计算D3独立腿、D3优先+D6兜底的合并D、冻结A/E/C后的ACDE组合。
不同时通过独立盈利、合并D复利改善和ACDE复利改善，就不进入正式D。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import sys
from typing import Any, Callable, Iterator

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
    BASELINE_ACDE_MULTIPLE,
    BASELINE_D_MULTIPLE,
    END,
    FIRST_12M_END,
    SECOND_12M_START,
    START,
    TOLERANCE,
    assert_formal_baseline,
    load_ledger,
)
from scripts.research_strategy_d_six_schools import (  # noqa: E402
    MIN_RETAIN_TRADE_COUNT,
    OutcomeCache,
    flatten,
    merge_with_d6,
)
from src.strategy_d_intraday_ledger import (  # noqa: E402
    PRICE_TOLERANCE,
    normalize_minute_bars,
)
from src.strict_asof import STRICT_DISCOVERY  # noqa: E402


LOGGER = logging.getLogger("research_strategy_d3_halfway_full_denominator")
BASE_DIR = ROOT / "data/research/strategy_d3_halfway"
MOTHER_PATH = BASE_DIR / "mother_pool_high_ge_7pct_full_window.csv"
NEW_STATUS_PATH = BASE_DIR / "minute_1m_tushare_new_status.csv"
NEW_PARTS_DIR = BASE_DIR / "minute_1m_tushare_new_parts"
TOUCH_MINUTE_PATH = ROOT / "data/research/strategy_d_intraday/minute_1m_tushare.csv"
TOUCH_LEDGER_PATH = ROOT / "data/research/strategy_d_intraday/event_ledger_full_window.csv"
OUTPUT_DIR = ROOT / "reports/strategy_d3_halfway"
THRESHOLDS = (0.07, 0.08, 0.09)

ALLOWED_SIGNAL_FIELDS = frozenset(
    {
        "threshold",
        "signal_hhmm",
        "market_segment",
        "open_gap_pct",
        "signal_return_from_open",
        "slope_1m",
        "slope_3m",
        "slope_5m",
        "recent_3m_amount_vs_prev_day",
        "recent_5m_amount_vs_prev_day",
        "cumulative_amount_vs_prev_day",
        "amount_acceleration_3m_vs_prior10m",
        "pullback_from_intraday_high",
        "pre_signal_intraday_range",
        "market_same_threshold_hit_count",
        "same_segment_threshold_hit_count",
        "market_sealed_count",
    }
)


@dataclass(frozen=True)
class Rule:
    name: str
    description: str
    fields: tuple[str, ...]
    predicate: Callable[[pd.DataFrame], pd.Series]


def numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _all(frame: pd.DataFrame) -> pd.Series:
    return pd.Series(True, index=frame.index)


def _eq(column: str, value: float | str) -> Callable[[pd.DataFrame], pd.Series]:
    if isinstance(value, str):
        return lambda frame: frame[column].astype(str).eq(value)
    return lambda frame: numeric(frame[column]).eq(value)


def _ge(column: str, value: float) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: numeric(frame[column]).ge(value)


def _le(column: str, value: float) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: numeric(frame[column]).le(value)


def _between(column: str, low: float, high: float) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: numeric(frame[column]).between(low, high)


def _segment(values: set[str]) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda frame: frame["market_segment"].astype(str).isin(values)


def _and(*predicates: Callable[[pd.DataFrame], pd.Series]) -> Callable[[pd.DataFrame], pd.Series]:
    def predicate(frame: pd.DataFrame) -> pd.Series:
        result = pd.Series(True, index=frame.index)
        for item in predicates:
            result &= item(frame).fillna(False)
        return result

    return predicate


def rules() -> list[Rule]:
    result: list[Rule] = []
    for threshold in THRESHOLDS:
        label = int(round(threshold * 100))
        threshold_field = ("threshold",)
        threshold_predicate = _eq("threshold", threshold)
        result.extend(
            [
                Rule(f"d3_t{label}_all", f"首次分钟收盘达到{label}%", threshold_field, threshold_predicate),
                Rule(f"d3_t{label}_before_1000", f"{label}%信号在10:00前", ("threshold", "signal_hhmm"), _and(threshold_predicate, _between("signal_hhmm", 930, 1000))),
                Rule(f"d3_t{label}_1001_1130", f"{label}%信号在10:01~11:30", ("threshold", "signal_hhmm"), _and(threshold_predicate, _between("signal_hhmm", 1001, 1130))),
                Rule(f"d3_t{label}_1300_1400", f"{label}%信号在13:00~14:00", ("threshold", "signal_hhmm"), _and(threshold_predicate, _between("signal_hhmm", 1300, 1400))),
                Rule(f"d3_t{label}_after_1400", f"{label}%信号在14:00后", ("threshold", "signal_hhmm"), _and(threshold_predicate, _between("signal_hhmm", 1401, 1453))),
                Rule(f"d3_t{label}_slope3_ge_2", f"{label}%且近3分钟涨幅至少2%", ("threshold", "slope_3m"), _and(threshold_predicate, _ge("slope_3m", 0.02))),
                Rule(f"d3_t{label}_slope3_ge_3", f"{label}%且近3分钟涨幅至少3%", ("threshold", "slope_3m"), _and(threshold_predicate, _ge("slope_3m", 0.03))),
                Rule(f"d3_t{label}_amount3_ge_3pct", f"{label}%且近3分钟成交额至少前日3%", ("threshold", "recent_3m_amount_vs_prev_day"), _and(threshold_predicate, _ge("recent_3m_amount_vs_prev_day", 0.03))),
                Rule(f"d3_t{label}_amount3_ge_5pct", f"{label}%且近3分钟成交额至少前日5%", ("threshold", "recent_3m_amount_vs_prev_day"), _and(threshold_predicate, _ge("recent_3m_amount_vs_prev_day", 0.05))),
                Rule(f"d3_t{label}_accel_ge_2", f"{label}%且近3分钟量能加速至少2倍", ("threshold", "amount_acceleration_3m_vs_prior10m"), _and(threshold_predicate, _ge("amount_acceleration_3m_vs_prior10m", 2.0))),
                Rule(f"d3_t{label}_pullback_le_0_5", f"{label}%且距盘中高点回撤不超0.5%", ("threshold", "pullback_from_intraday_high"), _and(threshold_predicate, _ge("pullback_from_intraday_high", -0.005))),
                Rule(f"d3_t{label}_gap_le_3", f"{label}%且开盘涨幅不超3%", ("threshold", "open_gap_pct"), _and(threshold_predicate, _le("open_gap_pct", 0.03))),
                Rule(f"d3_t{label}_market_hit_20_60", f"{label}%且当时同阈值20~60只", ("threshold", "market_same_threshold_hit_count"), _and(threshold_predicate, _between("market_same_threshold_hit_count", 20, 60))),
                Rule(f"d3_t{label}_same_segment_ge_10", f"{label}%且同板块市场至少10只已到阈值", ("threshold", "same_segment_threshold_hit_count"), _and(threshold_predicate, _ge("same_segment_threshold_hit_count", 10))),
                Rule(f"d3_t{label}_sealed_ge_5", f"{label}%且当时全市场至少5只已封板", ("threshold", "market_sealed_count"), _and(threshold_predicate, _ge("market_sealed_count", 5))),
                Rule(f"d3_t{label}_main_board", f"{label}%仅沪深主板", ("threshold", "market_segment"), _and(threshold_predicate, _segment({"sh_main", "sz_main"}))),
                Rule(f"d3_t{label}_growth_board", f"{label}%仅创业板或科创板", ("threshold", "market_segment"), _and(threshold_predicate, _segment({"chi_next", "star"}))),
                Rule(f"d3_t{label}_slope_amount_sealed", f"{label}%、3分钟涨2%、量能3%且已有5只封板", ("threshold", "slope_3m", "recent_3m_amount_vs_prev_day", "market_sealed_count"), _and(threshold_predicate, _ge("slope_3m", 0.02), _ge("recent_3m_amount_vs_prev_day", 0.03), _ge("market_sealed_count", 5))),
            ]
        )
    for rule in result:
        unknown = set(rule.fields) - ALLOWED_SIGNAL_FIELDS
        if unknown:
            raise ValueError(f"D3规则{rule.name}使用未知/非as-of字段：{sorted(unknown)}")
    return result


def iter_part_groups(parts_dir: Path) -> Iterator[tuple[tuple[str, str], pd.DataFrame]]:
    seen: set[tuple[str, str]] = set()
    for path in sorted(parts_dir.glob("*.csv")):
        frame = pd.read_csv(
            path,
            dtype={"trade_date": str, "ts_code": str, "hhmm": str},
            low_memory=False,
        )
        for (date, code), group in frame.groupby(["trade_date", "ts_code"], sort=False):
            key = (str(date), str(code))
            if key in seen:
                raise RuntimeError(f"D3新增分钟分片重复股票日：{key}")
            seen.add(key)
            yield key, group


def bar_features(
    bars: pd.DataFrame,
    index: int,
    *,
    pre_close: float,
    previous_amount_yuan: float,
) -> dict[str, float]:
    before = bars.iloc[: index + 1]
    close = float(before.iloc[-1]["close"])
    open_price = float(before.iloc[0]["open"])
    def slope(lag: int) -> float:
        left_index = max(index - lag, 0)
        left = float(bars.iloc[left_index]["close"])
        return close / left - 1.0 if left > 0 else np.nan

    amounts = numeric(before["amount"]).fillna(0.0)
    recent3 = amounts.tail(3)
    recent5 = amounts.tail(5)
    prior10 = amounts.iloc[max(len(amounts) - 13, 0) : max(len(amounts) - 3, 0)]
    acceleration = (
        float(recent3.mean()) / float(prior10.mean())
        if len(prior10) and float(prior10.mean()) > 0
        else np.nan
    )
    intraday_high = float(numeric(before["high"]).max())
    intraday_low = float(numeric(before["low"]).min())
    return {
        "open_gap_pct": open_price / pre_close - 1.0 if pre_close > 0 else np.nan,
        "signal_return_from_open": close / open_price - 1.0 if open_price > 0 else np.nan,
        "slope_1m": slope(1),
        "slope_3m": slope(3),
        "slope_5m": slope(5),
        "recent_3m_amount_vs_prev_day": float(recent3.sum()) / previous_amount_yuan if previous_amount_yuan > 0 else np.nan,
        "recent_5m_amount_vs_prev_day": float(recent5.sum()) / previous_amount_yuan if previous_amount_yuan > 0 else np.nan,
        "cumulative_amount_vs_prev_day": float(amounts.sum()) / previous_amount_yuan if previous_amount_yuan > 0 else np.nan,
        "amount_acceleration_3m_vs_prior10m": acceleration,
        "pullback_from_intraday_high": close / intraday_high - 1.0 if intraday_high > 0 else np.nan,
        "pre_signal_intraday_range": intraday_high / intraday_low - 1.0 if intraday_low > 0 else np.nan,
    }


def extract_events(
    mother: pd.DataFrame,
    touch_ledger: pd.DataFrame,
    daily_data: strict.DailyData,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    lookup = mother.set_index(["trade_date", "ts_code"], drop=False)
    touch_ready = set(
        zip(
            touch_ledger.loc[touch_ledger["minute_status"].eq("READY_1M_PATH_NO_QUEUE_DEPTH"), "trade_date"].astype(str),
            touch_ledger.loc[touch_ledger["minute_status"].eq("READY_1M_PATH_NO_QUEUE_DEPTH"), "ts_code"].astype(str),
        )
    ) & set(lookup.index)
    status = pd.read_csv(
        NEW_STATUS_PATH,
        dtype={"trade_date": str, "ts_code": str, "target_key": str},
        low_memory=False,
    )
    new_complete = set(
        zip(
            status.loc[status["status"].eq("COMPLETE_1M_NO_QUEUE_DEPTH"), "trade_date"].astype(str),
            status.loc[status["status"].eq("COMPLETE_1M_NO_QUEUE_DEPTH"), "ts_code"].astype(str),
        )
    )
    new_empty = set(
        zip(
            status.loc[status["status"].eq("EMPTY"), "trade_date"].astype(str),
            status.loc[status["status"].eq("EMPTY"), "ts_code"].astype(str),
        )
    )
    if len(status) != 111_775 or set(status["status"].astype(str)) - {"COMPLETE_1M_NO_QUEUE_DEPTH", "EMPTY"}:
        raise RuntimeError(f"D3新增分钟状态未收口：rows={len(status)} counts={status['status'].value_counts().to_dict()}")

    rows: list[dict[str, Any]] = []
    processed: set[tuple[str, str]] = set()

    def consume(key: tuple[str, str], raw: pd.DataFrame) -> None:
        if key not in lookup.index or key in processed:
            return
        if key not in touch_ready and key not in new_complete:
            return
        bars = normalize_minute_bars(raw, ts_code=key[1], trade_date=key[0])
        if len(bars) != 241:
            raise RuntimeError(f"D3完整目标不是241根：{key} rows={len(bars)}")
        processed.add(key)
        meta = lookup.loc[key]
        pre_close = float(meta["pre_close"])
        limit_price = float(meta["limit_price"])
        previous = daily_data.day(str(meta["previous_trade_date"]))
        previous_amount_yuan = 0.0
        if not previous.empty and key[1] in previous.index:
            previous_amount_yuan = float(previous.loc[key[1]].get("amount", 0.0) or 0.0) * 1000.0
        close_returns = numeric(bars["close"]) / pre_close - 1.0
        at_limit = (numeric(bars["close"]) - limit_price).abs().le(PRICE_TOLERANCE)
        sealed = np.flatnonzero(at_limit.to_numpy())
        first_seal_hhmm = int(bars.iloc[int(sealed[0])]["hhmm"]) if len(sealed) else 0
        for threshold in THRESHOLDS:
            indexes = np.flatnonzero(close_returns.ge(threshold - 1e-12).to_numpy())
            if not len(indexes):
                continue
            signal_index = int(indexes[0])
            if signal_index + 1 >= len(bars):
                continue
            signal_hhmm = int(bars.iloc[signal_index]["hhmm"])
            next_bar = bars.iloc[signal_index + 1]
            next_hhmm = int(next_bar["hhmm"])
            if signal_hhmm >= 1454 or next_hhmm >= 1455:
                continue
            next_low = float(next_bar["low"])
            fill_confirmed = bool(next_low < limit_price - PRICE_TOLERANCE)
            entry_price = (
                min(float(next_bar["open"]) * 1.001, limit_price)
                if fill_confirmed
                else 0.0
            )
            rows.append(
                {
                    "trade_date": key[0],
                    "ts_code": key[1],
                    "name": str(meta["name"]),
                    "market_segment": str(meta["market_segment"]),
                    "pre_close": pre_close,
                    "limit_price": limit_price,
                    "threshold": threshold,
                    "signal_hhmm": signal_hhmm,
                    "next_minute_hhmm": next_hhmm,
                    "next_minute_fill_confirmed": fill_confirmed,
                    "next_minute_entry_price": entry_price,
                    "first_seal_hhmm": first_seal_hhmm,
                    "eventually_touched_limit_outcome_label": bool(meta["daily_high_touched_limit"]),
                    "failed_to_touch_limit_outcome_label": bool(meta["failed_to_touch_limit_after_reaching_7pct"]),
                    **bar_features(
                        bars,
                        signal_index,
                        pre_close=pre_close,
                        previous_amount_yuan=previous_amount_yuan,
                    ),
                }
            )

    for counter, (key, group) in enumerate(iter_minute_groups(TOUCH_MINUTE_PATH), 1):
        consume(key, group)
        if counter % 10_000 == 0:
            LOGGER.info("D3复用触板分钟扫描：%d组", counter)
    for counter, (key, group) in enumerate(iter_part_groups(NEW_PARTS_DIR), 1):
        consume(key, group)
        if counter % 20_000 == 0:
            LOGGER.info("D3新增失败分钟扫描：%d组", counter)

    expected_processed = touch_ready | new_complete
    missing = sorted(expected_processed - processed)
    if missing:
        raise RuntimeError(f"D3完整分钟目标未读到分片：{missing[:10]}")
    events = add_market_context(pd.DataFrame(rows))
    return events, {
        "mother_count": int(len(mother)),
        "touch_ready_count": int(len(touch_ready)),
        "new_complete_count": int(len(new_complete)),
        "new_empty_fail_closed_count": int(len(new_empty)),
        "total_processed_path_count": int(len(processed)),
        "event_count": int(len(events)),
    }


def add_market_context(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events
    threshold_arrays: dict[tuple[str, float], np.ndarray] = {}
    segment_arrays: dict[tuple[str, float, str], np.ndarray] = {}
    seal_arrays: dict[str, np.ndarray] = {}
    for (date, threshold), group in events.groupby(["trade_date", "threshold"], sort=False):
        threshold_arrays[(str(date), float(threshold))] = np.sort(numeric(group["signal_hhmm"]).to_numpy(int))
        for segment, sample in group.groupby("market_segment", sort=False):
            segment_arrays[(str(date), float(threshold), str(segment))] = np.sort(numeric(sample["signal_hhmm"]).to_numpy(int))
    seven = events[numeric(events["threshold"]).eq(0.07)].copy()
    for date, group in seven.groupby("trade_date", sort=False):
        values = numeric(group.loc[numeric(group["first_seal_hhmm"]).gt(0), "first_seal_hhmm"]).to_numpy(int)
        seal_arrays[str(date)] = np.sort(values)
    contexts: list[dict[str, int]] = []
    for row in events.itertuples(index=False):
        key = (str(row.trade_date), float(row.threshold))
        hhmm = int(row.signal_hhmm)
        market_values = threshold_arrays[key]
        segment_values = segment_arrays.get((*key, str(row.market_segment)), np.array([], dtype=int))
        seal_values = seal_arrays.get(str(row.trade_date), np.array([], dtype=int))
        contexts.append(
            {
                "market_same_threshold_hit_count": int(np.searchsorted(market_values, hhmm, side="right")),
                "same_segment_threshold_hit_count": int(np.searchsorted(segment_values, hhmm, side="right")),
                "market_sealed_count": int(np.searchsorted(seal_values, hhmm, side="right")),
            }
        )
    return pd.concat([events.reset_index(drop=True), pd.DataFrame(contexts)], axis=1)


def select_daily(events: pd.DataFrame, rule: Rule) -> pd.DataFrame:
    selected = events[rule.predicate(events).fillna(False)].copy()
    if selected.empty:
        return selected
    selected["_amount_rank"] = numeric(selected["recent_3m_amount_vs_prev_day"]).fillna(-np.inf)
    return (
        selected.sort_values(
            ["trade_date", "signal_hhmm", "_amount_rank", "ts_code"],
            ascending=[True, True, False, True],
        )
        .groupby("trade_date", as_index=False)
        .head(1)
        .reset_index(drop=True)
    )


def outcomes_for_picks(
    picks: pd.DataFrame,
    rule: Rule,
    cache: OutcomeCache,
) -> tuple[pd.DataFrame, dict[str, int]]:
    rows: list[dict[str, Any]] = []
    unresolved = 0
    for _, row in picks.iterrows():
        if not bool(row["next_minute_fill_confirmed"]):
            continue
        entry = float(row["next_minute_entry_price"])
        execution = cache.outcome(row, entry)
        unresolved += int(str(execution.get("status", "")) != "OK")
        rows.append(
            {
                **row.to_dict(),
                "signal_date": str(row["trade_date"]),
                "strategy_leg": "D",
                "d_school": "D3",
                "rule": rule.name,
                "entry_model": "NEXT_MINUTE_AFTER_COMPLETED_THRESHOLD_BAR",
                "entry_price": entry,
                **execution,
            }
        )
    if rows:
        result = pd.DataFrame(rows).sort_values("signal_date").reset_index(drop=True)
    else:
        result = pd.DataFrame(columns=["signal_date", "strategy_leg", "ts_code", "name", "status", "exit_date", "account_return"])
    return result, {
        "selected_day_count": int(len(picks)),
        "price_confirmed_fill_count": int(len(rows)),
        "unresolved_exit_count": unresolved,
    }


def evaluate(
    events: pd.DataFrame,
    d6_outcomes: pd.DataFrame,
    other_legs: dict[str, pd.DataFrame],
    cache: OutcomeCache,
) -> tuple[pd.DataFrame, dict[str, dict[str, pd.DataFrame]]]:
    rows: list[dict[str, Any]] = []
    artifacts: dict[str, dict[str, pd.DataFrame]] = {}
    all_rules = rules()
    for index, rule in enumerate(all_rules, 1):
        picks = select_daily(events, rule)
        outcomes, counts = outcomes_for_picks(picks, rule, cache)
        standalone = replay_d_only(outcomes, START, END)
        dx_metrics = executed_metrics(standalone)
        blocked = set(picks["trade_date"].astype(str))
        merged_outcomes = merge_with_d6(outcomes, blocked, d6_outcomes)
        merged_detail = replay_d_only(merged_outcomes, START, END)
        merged_metrics = executed_metrics(merged_detail)
        combo_detail, combo_metrics = combo_replay(merged_outcomes, other_legs)
        first = executed_metrics(standalone[standalone["signal_date"].between(START, FIRST_12M_END)])
        second = executed_metrics(standalone[standalone["signal_date"].between(SECOND_12M_START, END)])
        independent = int(dx_metrics["trade_count"]) >= MIN_RETAIN_TRADE_COUNT and float(dx_metrics["equity_multiple"]) > 1.0 + TOLERANCE
        d_improved = float(merged_metrics["equity_multiple"]) > BASELINE_D_MULTIPLE + TOLERANCE
        acde_improved = float(combo_metrics["equity_multiple"]) > BASELINE_ACDE_MULTIPLE + TOLERANCE
        passed = bool(independent and d_improved and acde_improved and counts["unresolved_exit_count"] == 0)
        rows.append(
            {
                "style": "D3",
                "rule": rule.name,
                "description": rule.description,
                "signal_time_fields": ",".join(rule.fields),
                "raw_event_count": int(rule.predicate(events).fillna(False).sum()),
                **counts,
                **flatten("dx", dx_metrics),
                **flatten("merged_d", merged_metrics),
                **flatten("acde", combo_metrics),
                "dx_first_12m_trade_count": int(first["trade_count"]),
                "dx_first_12m_multiple": float(first["equity_multiple"]),
                "dx_second_12m_trade_count": int(second["trade_count"]),
                "dx_second_12m_multiple": float(second["equity_multiple"]),
                "independent_profitable_and_sample_sufficient": independent,
                "merged_d_compound_improved": d_improved,
                "acde_compound_improved": acde_improved,
                "triple_gate_passed": passed,
                "formal_strategy_modified": False,
            }
        )
        artifacts[rule.name] = {
            "picks": picks,
            "outcomes": outcomes,
            "standalone": standalone,
            "merged_detail": merged_detail,
            "combo_detail": combo_detail,
        }
        LOGGER.info(
            "%s：D3=%d笔/%.6f倍，合并D=%.6f倍，ACDE=%.6f倍，保留=%s（%d/%d）",
            rule.name,
            int(dx_metrics["trade_count"]),
            float(dx_metrics["equity_multiple"]),
            float(merged_metrics["equity_multiple"]),
            float(combo_metrics["equity_multiple"]),
            passed,
            index,
            len(all_rules),
        )
    return pd.DataFrame(rows), artifacts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="D3完整失败分母严格研究")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    mother = pd.read_csv(MOTHER_PATH, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    touch_ledger, _ = load_ledger(TOUCH_LEDGER_PATH)
    daily_data = strict.daily_data()
    LOGGER.info("扫描D3完整152,098只次7%%失败分母分钟路径")
    events, input_audit = extract_events(mother, touch_ledger, daily_data)

    source, source_audit = strict.source_audit()
    if not bool(source_audit.get("passed")):
        raise RuntimeError("正式严格as-of源审计失败")
    d6_outcomes = strict.build_d(source, daily_data)
    d6_detail = replay_d_only(d6_outcomes, START, END)
    d6_metrics = executed_metrics(d6_detail)
    other_legs = build_current_other_legs()
    baseline_combo_detail, baseline_combo_metrics = combo_replay(d6_outcomes, other_legs)
    assert_formal_baseline(d6_metrics, baseline_combo_metrics)

    search, artifacts = evaluate(events, d6_outcomes, other_legs, OutcomeCache(daily_data))
    passed = search[search["triple_gate_passed"]].sort_values(
        ["acde_equity_multiple", "merged_d_equity_multiple"], ascending=False
    )
    selected_rule = str(passed.iloc[0]["rule"]) if len(passed) else ""
    if selected_rule:
        final_d = artifacts[selected_rule]["merged_detail"]
        final_combo = artifacts[selected_rule]["combo_detail"]
        decision = "D3_CANDIDATE_PASSED_REQUIRES_FORMAL_IMPLEMENTATION_AND_ALL_SCHOOL_FINAL_CERTIFICATION"
    else:
        final_d = d6_detail
        final_combo = baseline_combo_detail
        decision = "REJECT_D3_KEEP_D6_ONLY_NO_RULE_PASSED_TRIPLE_GATE"
    summary = {
        "schema_version": 1,
        "generated_at": pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat(),
        "protocol": STRICT_DISCOVERY,
        "window": f"{START}~{END}",
        "strategy_school": "D3_HALF_WAY_7_TO_9_PERCENT",
        "formal_strategy_modified": False,
        "release_eligible": False,
        "input_audit": input_audit,
        "signal_definition": "分钟收盘首次达到7%/8%/9%，下一分钟价格穿透确认成交；日线high只建采集分母",
        "rule_count": int(len(search)),
        "triple_gate_pass_count": int(len(passed)),
        "selected_rule": selected_rule,
        "frozen_d6": d6_metrics,
        "frozen_acde": baseline_combo_metrics,
        "final_d": executed_metrics(final_d),
        "final_acde": strict.combo_metrics(final_combo),
        "formal_decision": decision,
        "limitations": [
            "分钟收盘确认牺牲了分钟内更早的7%触价机会，以换取严格无未来函数。",
            "下一分钟整分钟封死涨停时缺队列深度，主结果保守记为未成交。",
            "当前24个月内54条结构规则存在多重搜索风险，只能按三层硬门禁决定是否继续。",
        ],
    }
    events.to_csv(output_dir / "d3_threshold_signal_events.csv", index=False, encoding="utf-8-sig")
    search.to_csv(output_dir / "d3_rule_triple_gates.csv", index=False, encoding="utf-8-sig")
    final_d.to_csv(output_dir / "d3_final_candidate_d_detail.csv", index=False, encoding="utf-8-sig")
    final_combo.to_csv(output_dir / "d3_final_candidate_acde_detail.csv", index=False, encoding="utf-8-sig")
    (output_dir / "d3_research_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
