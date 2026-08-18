"""model=3 的 L 分支共用决策规则。

本模块只读取信号日已经确定的字段，不读取未来价格、未来收益或成交结果。
历史组合认证与实盘状态机必须共同调用这里，避免同一条规则在两处手写后漂移。
"""
from __future__ import annotations

from typing import Any, Mapping


DEFAULT_BASE_RULE = {
    "market_segment": "not_star",
    "segment_retreat_state_bucket": ["neutral", "warming_2day"],
    "market_chain_count_bucket": ["3_8", "8_15", "15_30", "gte_30"],
}

DEFAULT_REPLACE_GUARD = {
    "market_segment": "chi_next",
    "theme_limit_count_min": 2,
    "first_time_detail_bucket_exclude": ["after_1430"],
}


def _model3_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """同时兼容完整config.json和单独的strategy_model3配置。"""

    nested = config.get("strategy_model3")
    return nested if isinstance(nested, Mapping) else config


def _mapping_or_default(
    value: Any,
    default: Mapping[str, Any],
) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else default


def model3_l_base_rule_pass(
    signal: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[bool, str]:
    """判断L信号是否通过model=3基础规则。

    `market_chain_count_bucket=3_8`是本轮新增的可成交扩容环境；其余条件保持不变。
    所有字段都来自T日收盘后信号，缺字段时按安全口径拒绝。
    """

    model3 = _model3_config(config)
    rule = _mapping_or_default(model3.get("base_l_rule"), DEFAULT_BASE_RULE)
    segment = str(signal.get("market_segment", ""))
    retreat = str(signal.get("segment_retreat_state_bucket", ""))
    chain = str(signal.get("market_chain_count_bucket", ""))
    reasons: list[str] = []

    segment_rule = rule.get("market_segment", DEFAULT_BASE_RULE["market_segment"])
    if not segment:
        reasons.append("market_segment=missing，按安全口径拒绝")
    elif segment_rule == "not_star" and segment == "star":
        reasons.append("market_segment=star被策略认证规则排除（非权限拦截）")
    elif isinstance(segment_rule, list) and segment not in {
        str(value) for value in segment_rule
    }:
        reasons.append(f"market_segment={segment}不在允许范围")

    retreat_values = {
        str(value)
        for value in rule.get(
            "segment_retreat_state_bucket",
            DEFAULT_BASE_RULE["segment_retreat_state_bucket"],
        )
    }
    if not retreat or retreat not in retreat_values:
        reasons.append(
            f"segment_retreat_state_bucket={retreat or 'missing'}不在"
            + "/".join(sorted(retreat_values))
        )

    chain_values = {
        str(value)
        for value in rule.get(
            "market_chain_count_bucket",
            DEFAULT_BASE_RULE["market_chain_count_bucket"],
        )
    }
    if not chain or chain not in chain_values:
        reasons.append(
            f"market_chain_count_bucket={chain or 'missing'}不在"
            + "/".join(sorted(chain_values))
        )

    return (
        not reasons,
        "；".join(reasons) if reasons else "L通过model=3基础稳健条件",
    )


def model3_l_replace_guard_pass(
    signal: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[bool, str]:
    """判断L能否替换已经存在的A/C/E计划。"""

    model3 = _model3_config(config)
    guard = _mapping_or_default(
        model3.get("replace_guard"), DEFAULT_REPLACE_GUARD
    )
    segment = str(signal.get("market_segment", ""))
    first_time_bucket = str(signal.get("first_time_detail_bucket", ""))
    try:
        theme_limit_count = float(signal.get("theme_limit_count", 0) or 0)
    except (TypeError, ValueError):
        theme_limit_count = 0.0

    required_segment = str(
        guard.get("market_segment", DEFAULT_REPLACE_GUARD["market_segment"])
    )
    min_theme_count = float(
        guard.get(
            "theme_limit_count_min",
            DEFAULT_REPLACE_GUARD["theme_limit_count_min"],
        )
    )
    excluded_first_times = {
        str(value)
        for value in guard.get(
            "first_time_detail_bucket_exclude",
            DEFAULT_REPLACE_GUARD["first_time_detail_bucket_exclude"],
        )
    }
    reasons: list[str] = []
    if segment != required_segment:
        reasons.append(f"替换要求{required_segment}，当前market_segment={segment}")
    if theme_limit_count < min_theme_count:
        reasons.append(
            f"替换要求theme_limit_count>={min_theme_count:g}，当前={theme_limit_count:g}"
        )
    if first_time_bucket in excluded_first_times:
        reasons.append(
            f"替换排除first_time_detail_bucket={first_time_bucket}"
        )
    return (
        not reasons,
        "；".join(reasons) if reasons else "L通过model=3替换保护条件",
    )
