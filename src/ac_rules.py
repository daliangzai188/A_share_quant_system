"""A/C选股规则的唯一出处：C条件分支读取、C配置副本、条件说明文字、按各腿自己的风险规则剔除候选。

2026-11-14 从收盘流水线⑥（scripts/run_paper_ab_filtered_daily_ops.py）和观察窗口脚本原样搬来，代码未改。
实盘收盘流水线⑥、影子账候选构造（src/acde_rolling_candidates.py）、研究脚本都从这里导入，
实盘和回测用的是同一份规则代码。
"""
from __future__ import annotations

import copy
from typing import Any

import pandas as pd


def condition_strategy_config(
    base_config: dict[str, Any],
    conditions: list[dict[str, str]],
    strategy_name: str,
    *,
    condition_profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    config = copy.deepcopy(base_config)
    config["strategy_name"] = strategy_name
    filters = config.setdefault("candidate_filters", {})
    filters["conditions"] = [dict(condition) for condition in conditions]
    # 自定义conditions/profile当前只用于生成C配置副本。A的“主池为空才补位”
    # 是A专属语义，绝不能随顶层candidate_filters复制到C，否则会在C自身
    # 无候选日错误启用A补位分支，造成跨腿污染。
    filters.pop("fallback_when_primary_empty", None)
    if condition_profiles:
        filters["condition_profiles"] = copy.deepcopy(condition_profiles)
    else:
        filters.pop("condition_profiles", None)
    # 当前所有传入自定义 conditions 的调用方都是 C。A 的排序优化不能被 C
    # 隐式继承；C 未通过本轮双复利门槛时，必须读取 c_strategy.ranking 冻结原排序。
    c_ranking = (
        base_config.get("paper_ab_filtered_strategy", {})
        .get("c_strategy", {})
        .get("ranking")
    )
    if isinstance(c_ranking, dict) and c_ranking:
        ranking = copy.deepcopy(c_ranking)
        if "score_rules" not in ranking:
            ranking["score_rules"] = copy.deepcopy(
                base_config.get("ranking", {}).get("score_rules", [])
            )
        config["ranking"] = ranking
    return config


def configured_c_conditions(config: dict[str, Any]) -> list[dict[str, str]]:
    ab_config = config.get("paper_ab_filtered_strategy", {})
    c_config = ab_config.get("c_strategy", {})
    if not bool(c_config.get("enabled", False)):
        return []
    conditions = []
    for condition in c_config.get("conditions", []):
        conditions.append(
            {
                "column": str(condition["column"]),
                "value": str(condition["value"]),
            }
        )
    return conditions


def configured_c_condition_profiles(config: dict[str, Any]) -> list[dict[str, Any]]:
    """读取正式C的冻结OR分支；每个分支内部仍是AND。"""

    c_config = config.get("paper_ab_filtered_strategy", {}).get("c_strategy", {})
    if not bool(c_config.get("enabled", False)):
        return []
    mode = str(c_config.get("condition_mode", "ALL_CONDITIONS")).upper()
    profiles = c_config.get("condition_profiles", [])
    if mode != "ANY_PROFILE":
        return []
    if not isinstance(profiles, list) or not profiles:
        raise RuntimeError("C配置声明ANY_PROFILE但没有condition_profiles")
    normalized_profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, raw in enumerate(profiles, 1):
        profile_id = str(raw.get("profile_id", "")).strip()
        if not profile_id or profile_id in seen:
            raise RuntimeError(f"C条件分支编号缺失或重复: {profile_id}")
        seen.add(profile_id)
        raw_conditions = raw.get("conditions", [])
        if not isinstance(raw_conditions, list) or not raw_conditions:
            raise RuntimeError(f"C条件分支不能为空: {profile_id}")
        conditions: list[dict[str, Any]] = []
        for condition in raw_conditions:
            operator = str(condition.get("operator", "=="))
            normalized_condition: dict[str, Any] = {
                "column": str(condition["column"]),
                "operator": operator,
            }
            if operator == "==":
                normalized_condition["value"] = str(condition["value"])
            elif operator == "in":
                values = [str(value) for value in condition.get("values", [])]
                if not values:
                    raise RuntimeError(f"C正式分支集合条件不能为空: {profile_id}")
                normalized_condition["values"] = values
            else:
                raise RuntimeError(
                    f"C正式分支只允许==或in条件: {profile_id} {operator}"
                )
            conditions.append(normalized_condition)
        normalized_profiles.append(
            {
                "profile_id": profile_id,
                "priority": int(raw.get("priority", position)),
                "branch_id": str(raw.get("branch_id", profile_id)),
                "branch_name": str(raw.get("branch_name", "")),
                "exit_rule_id": str(raw.get("exit_rule_id", "")),
                "conditions": conditions,
            }
        )
    return sorted(
        normalized_profiles,
        key=lambda item: (item["priority"], item["profile_id"]),
    )


def condition_text(conditions: list[dict[str, Any]]) -> str:
    if conditions and "conditions" in conditions[0]:
        return condition_profiles_text(conditions)
    return ";".join(f"{condition['column']}={condition['value']}" for condition in conditions)


def condition_profiles_text(profiles: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for position, profile in enumerate(profiles, 1):
        profile_id = str(profile.get("profile_id", f"PROFILE_{position}"))
        condition_parts: list[str] = []
        for condition in profile.get("conditions", []):
            operator = str(condition.get("operator", "=="))
            if operator == "in":
                values = ",".join(str(value) for value in condition.get("values", []))
                condition_parts.append(f"{condition['column']} IN [{values}]")
            else:
                condition_parts.append(
                    f"{condition['column']}={condition.get('value', '')}"
                )
        conditions = " AND ".join(condition_parts)
        parts.append(f"{profile_id}({conditions})")
    return " OR ".join(parts)


def _apply_numeric_condition(values: pd.Series, operator: str, threshold: float) -> pd.Series:
    if operator == ">=":
        return values >= threshold
    if operator == ">":
        return values > threshold
    if operator == "<=":
        return values <= threshold
    if operator == "<":
        return values < threshold
    if operator == "==":
        return values == threshold
    return pd.Series(False, index=values.index)


def reject_strategy_risk_mask(
    replayed: pd.DataFrame,
    config: dict[str, Any],
    strategy_key: str,
) -> pd.Series:
    """按指定策略自身的风险规则过滤，避免C继续依赖已删除的B配置。"""
    if replayed.empty:
        return pd.Series(False, index=replayed.index)
    ab_config = config.get("paper_ab_filtered_strategy", {})
    strategy_config = ab_config.get(strategy_key, {})
    rules = strategy_config.get("risk_reject_rules", [])
    mask = pd.Series(False, index=replayed.index)
    risk_flags = replayed.get("risk_flags", pd.Series("", index=replayed.index)).fillna("").astype(str)
    for rule in rules:
        for keyword in rule.get("risk_flags_contains_any", []):
            mask = mask | risk_flags.str.contains(str(keyword), regex=False)
        for condition in rule.get("numeric_conditions", []):
            column = str(condition.get("column", ""))
            operator = str(condition.get("operator", "==")).strip()
            threshold = pd.to_numeric(condition.get("value", 0), errors="coerce")
            if not column or pd.isna(threshold):
                continue
            values = pd.to_numeric(replayed.get(column, pd.Series(0.0, index=replayed.index)), errors="coerce")
            mask = mask | _apply_numeric_condition(values, operator, float(threshold))
        # compound_conditions: list of AND-groups, OR'd together
        for group in rule.get("compound_conditions", []):
            sub = pd.Series(True, index=replayed.index)
            for condition in group:
                column = str(condition.get("column", ""))
                operator = str(condition.get("operator", "==")).strip()
                threshold = pd.to_numeric(condition.get("value", 0), errors="coerce")
                if not column or pd.isna(threshold):
                    sub = pd.Series(False, index=replayed.index)
                    break
                values = pd.to_numeric(replayed.get(column, pd.Series(0.0, index=replayed.index)), errors="coerce")
                sub = sub & _apply_numeric_condition(values, operator, float(threshold))
            mask = mask | sub.fillna(False)
    return mask.fillna(False).astype(bool)


def c_selection_config(config: dict[str, Any]) -> dict[str, Any]:
    """正式C的选股配置副本：OR分支取规范化、按优先级排好的 condition_profiles。

    收盘流水线⑥和影子账都用它构造C的候选生成器（2026-11-14 合并两边各自的写法；
    冻结研究池 2019-01~2026-11 逐日核对，合并前两边选出的C已完全相同）。
    """
    return condition_strategy_config(
        config,
        configured_c_conditions(config),
        "backup_strategy_c_current",
        condition_profiles=configured_c_condition_profiles(config),
    )


def screen_c_candidates(generator: Any, daily: pd.DataFrame, config: dict[str, Any]) -> tuple[pd.DataFrame, pd.Series]:
    """C当日候选：排序、打风险标记、按C自身风险规则标出要剔除的候选。

    返回（排好序的全部候选，剔除掩码）。剔除后剩下的按原顺序递补，第一名就是当天的C。
    收盘流水线⑥和影子账共用这一份。
    """
    ranked = generator.rank_candidates(daily)
    ranked["risk_flags"] = [generator.build_risk_flags(row) for row in ranked.itertuples(index=False)]
    return ranked, reject_strategy_risk_mask(ranked, config, "c_strategy")
