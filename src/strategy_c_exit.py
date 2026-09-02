"""策略C分支级退出规则解析。

策略C的逻辑分支可以使用不同的平仓周期。该模块只根据正式配置和候选在
信号日已经命中的profile解析退出规则，不读取未来行情。解析结果必须随计划单
进入执行链，避免实盘层把C的T+2、T+3错误地统一成默认持有期。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class StrategyCExitDecision:
    """一只C候选最终采用的分支与退出规则。"""

    matched_condition_profile_ids: str
    matched_strategy_branch_ids: str
    resolved_exit_profile_id: str
    exit_rule: str
    exit_signal_offset: int
    exit_n_days: int
    exit_rule_resolution: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def split_profile_ids(value: Any) -> list[str]:
    """把候选输出中的分号分隔profile编号解析为稳定、去重后的列表。"""

    seen: set[str] = set()
    result: list[str] = []
    for item in str(value or "").split(";"):
        profile_id = item.strip()
        if not profile_id or profile_id.lower() == "nan" or profile_id in seen:
            continue
        seen.add(profile_id)
        result.append(profile_id)
    return result


def _exit_rules(c_config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_rules = c_config.get("exit_rules", {})
    if isinstance(raw_rules, Mapping) and raw_rules:
        rules = {str(key): dict(value) for key, value in raw_rules.items()}
    else:
        # 兼容旧版只有单一exit_rule的配置；正式多周期版必须显式配置exit_rules。
        legacy = dict(c_config.get("exit_rule", {}))
        rule_id = str(legacy.get("rule_name", "")).strip()
        rules = {rule_id: legacy} if rule_id else {}
    if not rules:
        raise RuntimeError("C配置缺少退出规则，拒绝生成计划")
    for rule_id, rule in rules.items():
        offset = int(rule.get("max_hold_days", rule.get("signal_exit_offset", 0)) or 0)
        if offset < 2:
            raise RuntimeError(f"C退出规则{rule_id}的信号日偏移非法: {offset}")
        price_field = str(rule.get("exit_price_field", "close"))
        if price_field != "close":
            raise RuntimeError(f"C退出规则{rule_id}目前只允许收盘退出: {price_field}")
    return rules


def resolve_c_exit_decision(
    c_config: Mapping[str, Any],
    matched_condition_profile_ids: Any,
) -> StrategyCExitDecision:
    """解析候选应使用的C退出周期。

    同一候选可能同时命中多个profile。正式规则冻结为：只要命中更早退出的分支，
    就按更早的信号日退出偏移执行；相同偏移时按profile priority和配置顺序裁决。
    这样第3分支与旧分支重叠时仍严格复现研究中的“T+2覆盖”口径。
    """

    rules = _exit_rules(c_config)
    default_rule_id = str(
        c_config.get(
            "default_exit_rule_id",
            c_config.get("exit_rule", {}).get("rule_name", ""),
        )
    ).strip()
    if default_rule_id not in rules:
        raise RuntimeError(f"C默认退出规则未定义: {default_rule_id}")

    configured_resolution = str(
        c_config.get(
            "multiple_profile_exit_resolution",
            "minimum_signal_exit_offset_then_profile_priority",
        )
    )
    if configured_resolution != "minimum_signal_exit_offset_then_profile_priority":
        raise RuntimeError(f"C多分支退出裁决口径不支持: {configured_resolution}")

    profiles = list(c_config.get("condition_profiles", []))
    profile_map = {
        str(profile.get("profile_id", "")): {
            **dict(profile),
            "_position": position,
        }
        for position, profile in enumerate(profiles)
        if str(profile.get("profile_id", "")).strip()
    }
    matched_ids = split_profile_ids(matched_condition_profile_ids)
    unknown = [profile_id for profile_id in matched_ids if profile_id not in profile_map]
    if unknown:
        raise RuntimeError(f"C候选命中了正式配置之外的profile: {unknown}")

    matched_profiles = [profile_map[profile_id] for profile_id in matched_ids]
    if not matched_profiles:
        selected_profile_id = "DEFAULT"
        selected_rule_id = default_rule_id
        branch_ids: list[str] = []
    else:
        candidates: list[tuple[int, int, int, str, str]] = []
        branch_ids = []
        for profile in matched_profiles:
            profile_id = str(profile["profile_id"])
            branch_id = str(profile.get("branch_id", profile_id)).strip() or profile_id
            if branch_id not in branch_ids:
                branch_ids.append(branch_id)
            rule_id = str(profile.get("exit_rule_id", default_rule_id)).strip()
            if rule_id not in rules:
                raise RuntimeError(f"C分支{profile_id}引用了不存在的退出规则: {rule_id}")
            offset = int(
                rules[rule_id].get(
                    "max_hold_days", rules[rule_id].get("signal_exit_offset", 0)
                )
            )
            candidates.append(
                (
                    offset,
                    int(profile.get("priority", profile["_position"] + 1)),
                    int(profile["_position"]),
                    profile_id,
                    rule_id,
                )
            )
        _, _, _, selected_profile_id, selected_rule_id = min(candidates)

    selected_rule = rules[selected_rule_id]
    signal_offset = int(
        selected_rule.get(
            "max_hold_days", selected_rule.get("signal_exit_offset", 0)
        )
    )
    return StrategyCExitDecision(
        matched_condition_profile_ids=";".join(matched_ids),
        matched_strategy_branch_ids=";".join(branch_ids),
        resolved_exit_profile_id=selected_profile_id,
        exit_rule=selected_rule_id,
        exit_signal_offset=signal_offset,
        # C在信号日T冻结计划、T+1买入；所以从买入日到退出日少一个交易日。
        exit_n_days=signal_offset - 1,
        exit_rule_resolution=configured_resolution,
    )
