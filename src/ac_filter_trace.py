"""A/C候选过滤漏斗：逐步记录候选在全市场范围、入选条件、排除规则、排序各环节剩多少、停在哪一步。

守护进程收盘后打印“A/C过滤漏斗”用它；运维工具 tools/audit_signal_readiness.py 也用它。
2026-11-14 从 scripts/audit_signal_readiness.py（脚本现移到 tools/）原样搬来，代码未改。
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from src.paper_candidate_generator import PaperCandidateGenerator


def condition_desc(conditions: list[dict[str, Any]]) -> str:
    if not conditions:
        return "无"
    parts = []
    for condition in conditions:
        column = condition.get("column", "")
        operator = condition.get("operator", "==")
        value = condition.get("value", "")
        description = condition.get("description", "")
        parts.append(f"{column} {operator} {value}" + (f"（{description}）" if description else ""))
    return "；".join(parts)


def condition_profiles_desc(profiles: list[dict[str, Any]]) -> str:
    if not profiles:
        return "无"
    parts = []
    for position, profile in enumerate(profiles, 1):
        profile_id = str(profile.get("profile_id", f"PROFILE_{position}"))
        conditions = " 且 ".join(
            f"{item.get('column', '')} {item.get('operator', '==')} {item.get('value', '')}"
            for item in profile.get("conditions", [])
        )
        parts.append(f"{profile_id}: {conditions}")
    return "；或 ".join(parts)


def exclude_rules_desc(rules: list[dict[str, Any]]) -> str:
    if not rules:
        return "无"
    parts = []
    for rule in rules:
        rule_name = rule.get("name", "")
        description = rule.get("description", "")
        conditions = " 且 ".join(
            f"{item.get('column', '')} {item.get('operator', '==')} {item.get('value', '')}"
            for item in rule.get("conditions", [])
        )
        parts.append(f"{rule_name}: {conditions}" + (f"（{description}）" if description else ""))
    return "；".join(parts)


def daily_count(data: pd.DataFrame, signal_date: str) -> int:
    if data.empty or "trade_date" not in data.columns:
        return 0
    return int((data["trade_date"].astype(str) == signal_date).sum())


def signal_slice(data: pd.DataFrame, signal_date: str) -> pd.DataFrame:
    if data.empty or "trade_date" not in data.columns:
        return pd.DataFrame()
    return data[data["trade_date"].astype(str) == signal_date].copy()


def top_counts(series: pd.Series, limit: int = 6) -> str:
    if series.empty:
        return "无"
    counts = series.fillna("missing").astype(str).value_counts().head(limit).to_dict()
    return "，".join(f"{key}={value}" for key, value in counts.items())


def universe_reason_detail(before: pd.DataFrame, after: pd.DataFrame, config: dict[str, Any], signal_date: str) -> str:
    daily_before = signal_slice(before, signal_date)
    if daily_before.empty:
        return "当日进入股票池过滤前已经没有候选。"
    daily_after_keys = set(zip(signal_slice(after, signal_date).get("trade_date", []), signal_slice(after, signal_date).get("ts_code", [])))
    removed = daily_before[
        ~daily_before.apply(lambda row: (row.get("trade_date"), row.get("ts_code")) in daily_after_keys, axis=1)
    ].copy()
    if removed.empty:
        return "当日没有候选被股票池过滤剔除。"

    universe = config.get("universe", {})
    parts: list[str] = []
    name = removed.get("name", pd.Series("", index=removed.index)).fillna("").astype(str).str.upper()
    is_st = removed.get("is_st", pd.Series(False, index=removed.index)).astype(str).str.lower().isin({"true", "1"})
    if bool(universe.get("exclude_st", False)) or bool(universe.get("exclude_delisting_risk", False)):
        st_count = int((is_st | name.str.contains("ST", na=False) | name.str.contains("退", na=False)).sum())
        if st_count:
            parts.append(f"ST/退市风险={st_count}")

    segment = removed.get("market_segment", pd.Series("missing", index=removed.index)).fillna("missing").astype(str)
    excluded_segments = {str(value) for value in universe.get("exclude_market_segments", [])}
    segment_rules = {
        "bj": bool(universe.get("exclude_bj", False)),
        "chi_next": bool(universe.get("exclude_chi_next", False)),
        "sh_main": bool(universe.get("exclude_sh_main", False)),
        "sz_main": bool(universe.get("exclude_sz_main", False)),
    }
    blocked_segments = set(excluded_segments) | {key for key, enabled in segment_rules.items() if enabled}
    if blocked_segments:
        blocked = segment[segment.isin(blocked_segments)]
        if not blocked.empty:
            parts.append(f"被排除市场分段={top_counts(blocked)}")
    if not parts:
        parts.append(f"剔除候选市场分段分布={top_counts(segment)}")
    return "；".join(parts)


def include_reason_detail(before: pd.DataFrame, conditions: list[dict[str, Any]], signal_date: str) -> str:
    current = signal_slice(before, signal_date)
    if current.empty:
        return "当日进入入选条件前已经没有候选。"
    if not conditions:
        return "没有配置入选条件。"
    parts: list[str] = []
    for condition in conditions:
        column = str(condition.get("column", ""))
        expected = str(condition.get("value", ""))
        if column not in current.columns:
            parts.append(f"{column}字段不存在")
            current = pd.DataFrame()
            continue
        values = current[column].fillna("missing").astype(str)
        passed = values == expected
        failed = current[~passed].copy()
        parts.append(
            f"{column}要求={expected}，通过={int(passed.sum())}，未通过={len(failed)}，未通过实际值：{top_counts(failed[column]) if not failed.empty else '无'}"
        )
        current = current[passed].copy()
        if current.empty:
            break
    return "；".join(parts)


def include_profiles_reason_detail(
    before: pd.DataFrame,
    profiles: list[dict[str, Any]],
    signal_date: str,
) -> str:
    current = signal_slice(before, signal_date)
    if current.empty:
        return "当日进入入选条件分支前已经没有候选。"
    if not profiles:
        return "没有配置入选条件分支。"
    parts: list[str] = []
    total_union = pd.Series(False, index=current.index)
    for position, profile in enumerate(profiles, 1):
        profile_id = str(profile.get("profile_id", f"PROFILE_{position}"))
        mask = pd.Series(True, index=current.index)
        for condition in profile.get("conditions", []):
            column = str(condition.get("column", ""))
            expected = str(condition.get("value", ""))
            if column not in current.columns:
                mask &= False
                parts.append(f"{profile_id}:{column}字段不存在")
                break
            mask &= current[column].fillna("missing").astype(str).eq(expected)
        total_union |= mask
        parts.append(f"{profile_id}命中={int(mask.sum())}")
    parts.append(f"OR并集命中={int(total_union.sum())}")
    return "；".join(parts)


def exclude_reason_detail(before: pd.DataFrame, conditions: list[dict[str, Any]], signal_date: str) -> str:
    current = signal_slice(before, signal_date)
    if current.empty:
        return "当日进入排除条件前已经没有候选。"
    if not conditions:
        return "没有配置单字段排除条件。"
    parts: list[str] = []
    for condition in conditions:
        column = str(condition.get("column", ""))
        expected = str(condition.get("value", ""))
        if column not in current.columns:
            parts.append(f"{column}字段不存在")
            continue
        values = current[column].fillna("missing").astype(str)
        removed = current[values == expected].copy()
        parts.append(f"排除 {column}={expected}，剔除={len(removed)}")
        current = current[values != expected].copy()
    return "；".join(parts)


def compound_exclude_reason_detail(before: pd.DataFrame, rules: list[dict[str, Any]], signal_date: str) -> str:
    current = signal_slice(before, signal_date)
    if current.empty:
        return "当日进入复合排除规则前已经没有候选。"
    if not rules:
        return "没有配置复合排除规则。"
    parts: list[str] = []
    for rule in rules:
        mask = pd.Series(True, index=current.index)
        texts: list[str] = []
        for condition in rule.get("conditions", []):
            column = str(condition.get("column", ""))
            expected = str(condition.get("value", ""))
            texts.append(f"{column}={expected}")
            if column not in current.columns:
                mask &= False
            else:
                mask &= current[column].fillna("missing").astype(str) == expected
        removed = current[mask].copy()
        parts.append(f"{rule.get('name', 'unnamed_rule')}（{' 且 '.join(texts)}）剔除={len(removed)}")
        current = current[~mask].copy()
    return "；".join(parts)


def rank_reason_detail(daily: pd.DataFrame, selected: pd.DataFrame, config: dict[str, Any]) -> str:
    if daily.empty:
        return "当日没有候选进入排序。"
    if selected.empty:
        return "当日有候选进入排序，但没有生成选中计划。"
    row = selected.iloc[0]
    ranking = config.get("ranking", {})
    return (
        f"排序后首选={row.get('ts_code', '')} {row.get('name', '')}，"
        f"profit_source_score={row.get('profit_source_score', '')}，"
        f"排序字段={ranking.get('columns', [])}"
    )


def stop_point_text(trace: pd.DataFrame, label: str) -> str:
    scoped = trace[trace["strategy_layer"].astype(str) == label].copy()
    if scoped.empty:
        return f"{label}: 无漏斗记录"
    for _, row in scoped.iterrows():
        if int(row.get("signal_date_after", 0)) == 0:
            return (
                f"{label}: 停在 {row.get('step', '')}；"
                f"原因={row.get('reason_detail', row.get('description', ''))}"
            )
    last = scoped.iloc[-1]
    if int(last.get("signal_date_after", 0)) > 0:
        return f"{label}: 已通过策略筛选并进入排序/首选；后续是否交易取决于风险过滤和成交回放。"
    return f"{label}: 未识别停止点"


def filter_trace(label: str, generator: PaperCandidateGenerator, all_candidates: pd.DataFrame, signal_date: str) -> pd.DataFrame:
    config = generator.config
    filters = config.get("candidate_filters", {})
    rows: list[dict[str, Any]] = []

    def add(step: str, description: str, before: pd.DataFrame, after: pd.DataFrame, reason_detail: str = "") -> None:
        rows.append(
            {
                "strategy_layer": label,
                "step": step,
                "description": description,
                "reason_detail": reason_detail,
                "all_dates_before": len(before),
                "all_dates_after": len(after),
                "signal_date_before": daily_count(before, signal_date),
                "signal_date_after": daily_count(after, signal_date),
                "removed_on_signal_date": daily_count(before, signal_date) - daily_count(after, signal_date),
            }
        )

    current = all_candidates.copy()
    rows.append(
        {
            "strategy_layer": label,
            "step": "0_load_base_candidates",
            "description": "加载实盘/配置候选输入，并复用 StrategyConditionOptimizer.load_trades 的基础可交易过滤：成交概率可靠、封单异常过滤、股票池过滤等。",
            "reason_detail": "此处已经过滤掉成交概率不可靠、封单异常、不可执行样本；当日剩余样本是后续 A/C 的共同基础池。",
            "all_dates_before": len(current),
            "all_dates_after": len(current),
            "signal_date_before": daily_count(current, signal_date),
            "signal_date_after": daily_count(current, signal_date),
            "removed_on_signal_date": 0,
        }
    )

    before = current
    current = generator.apply_universe_filters(current)
    universe = config.get("universe", {})
    universe_desc = (
        f"exclude_st={universe.get('exclude_st')}, exclude_delisting_risk={universe.get('exclude_delisting_risk')}, "
        f"exclude_market_segments={universe.get('exclude_market_segments')}, exclude_bj={universe.get('exclude_bj')}, "
        f"exclude_sh_main={universe.get('exclude_sh_main')}, exclude_chi_next={universe.get('exclude_chi_next')}, "
        f"exclude_sz_main={universe.get('exclude_sz_main')}"
    )
    add("1_universe_filters", universe_desc, before, current, universe_reason_detail(before, current, config, signal_date))

    before = current
    profiles = filters.get("condition_profiles", [])
    conditions = filters.get("conditions", [])
    current = generator.apply_include_conditions(current)
    add(
        "2_include_conditions",
        condition_profiles_desc(profiles) if profiles else condition_desc(conditions),
        before,
        current,
        include_profiles_reason_detail(before, profiles, signal_date)
        if profiles
        else include_reason_detail(before, conditions, signal_date),
    )

    before = current
    current = generator.apply_exclude_conditions(current)
    add(
        "3_exclude_conditions",
        condition_desc(filters.get("exclude_conditions", [])),
        before,
        current,
        exclude_reason_detail(before, filters.get("exclude_conditions", []), signal_date),
    )

    before = current
    current = generator.apply_exclude_rules(current)
    add(
        "4_compound_exclude_rules",
        exclude_rules_desc(filters.get("exclude_rules", [])),
        before,
        current,
        compound_exclude_reason_detail(before, filters.get("exclude_rules", []), signal_date),
    )

    daily = current[current["trade_date"].astype(str) == signal_date].copy() if "trade_date" in current.columns else pd.DataFrame()
    ranked = generator.rank_candidates(daily) if not daily.empty else pd.DataFrame()
    top_n = generator.default_top_n
    output = generator.build_output(ranked, signal_date, top_n) if not ranked.empty else pd.DataFrame()
    selected_action = generator.paper_config.get("planned_action_for_selected", "PLAN_BUY_T1_OPEN")
    selected = output[output["planned_action"].astype(str) == selected_action].copy() if not output.empty else pd.DataFrame()
    rows.append(
        {
            "strategy_layer": label,
            "step": "5_rank_and_select_first",
            "description": f"排序字段={config.get('ranking', {}).get('columns', [])}，ascending={config.get('ranking', {}).get('ascending', [])}；top_n={top_n}；第1名 planned_action={selected_action}",
            "reason_detail": rank_reason_detail(daily, selected, config),
            "all_dates_before": len(current),
            "all_dates_after": len(current),
            "signal_date_before": len(daily),
            "signal_date_after": len(selected),
            "removed_on_signal_date": len(daily) - len(selected),
        }
    )
    return pd.DataFrame(rows)
