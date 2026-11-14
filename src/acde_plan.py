"""A>C>E>D 历史计划生成的唯一入口：影子账、停手判定、逐笔对账、三级警告、认证和研究共用。

内容：计划上下文（交易日、上一交易日市场门禁、严格信号池、D事件）、正式基线规则集合、
按规则生成各腿计划、按固定腿序做单账户现金回放所用的执行参数。
2026-11-14 从 scripts/optimize_acde_rolling_three_year.py（build_variant_plan）和
src/acde_monthly_research.py（两者现移到 research/；_context、_execution_kwargs、_variant_sets、_build_plans、_replay、load_monthly_config）
原样搬来，代码未改，只把下划线开头的内部名字改成正式名字。
"""
from __future__ import annotations

from pathlib import Path
import json
from typing import Any, Mapping, Sequence

import pandas as pd

from src.acde_rolling_candidates import (
    StaticOutcomeCache,
    VariantDefinition,
    a_variants,
    build_a_picks,
    build_c_picks,
    build_d_plans,
    build_e_picks,
    c_variants,
    d_variants,
    e_variants,
    previous_close_market_gate,
    static_plan_outcomes,
    strict_signal_pool,
)
from src.acde_rolling_framework import (
    FIXED_PRIORITY,
    ResearchWindow,
    normalize_date,
    open_dates,
    replay_action_date_cash_portfolio,
)
from src.strategy_d_factor_rules import load_factor_release
from src.strategy_e import load_e_spec
from src.utils.config import load_json_config


ROOT = Path(__file__).resolve().parents[1]
TOLERANCE = 1e-12


def _compact_date(value: object) -> str:
    return str(value or "").replace(".0", "").replace("-", "")[:8]


def load_monthly_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != 2:
        raise ValueError("月度ACDE配置schema_version必须为2")
    if payload.get("mode") != "research_only" or bool(
        payload.get("formal_strategy_auto_apply", True)
    ):
        raise ValueError("月度ACDE必须保持research_only且禁止自动落地")
    if tuple(payload.get("priority", [])) != FIXED_PRIORITY:
        raise ValueError("月度ACDE腿序必须固定为A>C>E>D")
    if payload.get("schedule", {}).get("frequency") != "monthly":
        raise ValueError("月度ACDE配置不得继续使用半年节点")
    windows = payload.get("windows", {})
    if windows.get("selection_window") != "main_only" or windows.get("metric_date") != "action_date":
        raise ValueError("月度ACDE只能按真实action_date使用唯一三年主窗口")
    return payload


def execution_kwargs(config: Mapping[str, Any]) -> dict[str, Any]:
    execution = config["execution"]
    system = load_json_config(ROOT / "config/config.json")
    analysis = system["analysis"]
    if abs(float(execution["commission_rate"]) - float(analysis["commission_rate"])) > TOLERANCE:
        raise ValueError("月度配置与正式佣金率不一致")
    if abs(float(execution["transfer_fee_rate"]) - float(analysis["transfer_fee_rate"])) > TOLERANCE:
        raise ValueError("月度配置与正式过户费率不一致")
    if abs(float(execution["minimum_commission"]) - float(analysis["minimum_commission"])) > TOLERANCE:
        raise ValueError("月度配置与正式最低佣金不一致")
    return {
        "initial_cash": float(execution["initial_cash"]),
        "position_pct": float(execution["position_pct"]),
        "max_position_pct": float(execution["max_position_pct"]),
        "commission_rate": float(execution["commission_rate"]),
        "transfer_fee_rate": float(execution["transfer_fee_rate"]),
        "minimum_commission": float(execution["minimum_commission"]),
        "stamp_tax_schedule": analysis.get("stamp_tax_schedule"),
    }


def plan_context(
    *,
    window: ResearchWindow,
    feature_path: Path,
    sentiment_path: Path,
    d_event_path: Path,
    calendar_path: Path,
    minimum_limit_up_count: int,
) -> dict[str, Any]:
    calendar = pd.read_csv(calendar_path, dtype={"cal_date": str}, low_memory=False)
    dates = open_dates(calendar, window.start, window.end)
    sentiment = pd.read_csv(sentiment_path, dtype={"trade_date": str}, low_memory=False)
    allowed_actions, allowed_signals, market_gate = previous_close_market_gate(
        calendar=calendar,
        sentiment=sentiment,
        action_dates=dates,
        minimum_limit_up_count=minimum_limit_up_count,
    )
    pool = pd.read_csv(feature_path, low_memory=False)
    signal_pool = strict_signal_pool(
        pool,
        signal_dates=set(market_gate["state_date"].astype(str)),
        allowed_signal_dates=allowed_signals,
    )
    d_events = pd.read_csv(d_event_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    d_events["trade_date"] = d_events["trade_date"].map(_compact_date)
    d_events = d_events[d_events["trade_date"].between(window.start, window.end)].copy()
    return {
        "calendar": calendar,
        "action_dates": dates,
        "allowed_actions": allowed_actions,
        "signal_pool": signal_pool,
        "d_events": d_events,
        "market_gate": market_gate,
    }


def variant_sets() -> tuple[dict[str, VariantDefinition], dict[str, list[VariantDefinition]]]:
    base_config = load_json_config(ROOT / "config/strategy_config.json")
    e_spec = load_e_spec(ROOT)
    d_release = load_factor_release(ROOT / "config/strategy_d_factor_release.json")
    pairs = (
        a_variants(base_config),
        c_variants(base_config),
        e_variants(e_spec),
        d_variants(d_release),
    )
    baselines = {pair[0].strategy_leg: pair[0] for pair in pairs}
    candidates = {pair[0].strategy_leg: pair[1] for pair in pairs}
    return baselines, candidates


def build_plans(
    variants: Mapping[str, VariantDefinition],
    *,
    context: Mapping[str, Any],
    cutoff: str,
    cache: StaticOutcomeCache | None = None,
) -> dict[str, pd.DataFrame]:
    outcome_cache = cache or StaticOutcomeCache()
    return {
        leg: build_variant_plan(
            variants[leg],
            signal_pool=context["signal_pool"],
            d_events=context["d_events"],
            allowed_action_dates=context["allowed_actions"],
            cutoff=cutoff,
            outcome_cache=outcome_cache,
        )
        for leg in FIXED_PRIORITY
    }


def replay_plans(
    legs: Mapping[str, pd.DataFrame],
    *,
    action_dates: Sequence[str],
    execution: Mapping[str, Any],
) -> pd.DataFrame:
    return replay_action_date_cash_portfolio(
        legs,
        action_dates=action_dates,
        priority=FIXED_PRIORITY,
        **execution,
    )


def build_variant_plan(
    variant: VariantDefinition,
    *,
    signal_pool: pd.DataFrame,
    d_events: pd.DataFrame,
    allowed_action_dates: set[str],
    cutoff: str,
    outcome_cache: StaticOutcomeCache,
) -> pd.DataFrame:
    leg = variant.strategy_leg
    if leg == "A":
        frame = static_plan_outcomes(
            build_a_picks(signal_pool, variant.payload),
            leg="A",
            cutoff=cutoff,
            cache=outcome_cache,
        )
    elif leg == "C":
        frame = static_plan_outcomes(
            build_c_picks(signal_pool, variant.payload),
            leg="C",
            cutoff=cutoff,
            cache=outcome_cache,
        )
    elif leg == "E":
        frame = static_plan_outcomes(
            build_e_picks(signal_pool, variant.payload),
            leg="E",
            cutoff=cutoff,
            cache=outcome_cache,
            e_spec=variant.payload,
        )
    elif leg == "D":
        frame = build_d_plans(
            d_events,
            variant.payload,
            allowed_action_dates=allowed_action_dates,
            cutoff=cutoff,
        )
    else:
        raise ValueError(f"未知策略腿：{leg}")
    frame = frame.copy()
    if frame.empty:
        frame["action_date"] = pd.Series(dtype="object")
        return frame
    source_column = "signal_date" if leg == "D" else "buy_date"
    if source_column not in frame.columns:
        raise RuntimeError(f"{leg}计划缺少显式action_date来源字段{source_column}")
    frame["action_date"] = frame[source_column].map(normalize_date)
    if frame["action_date"].eq("").any():
        raise RuntimeError(f"{leg}计划存在空action_date")
    return frame
