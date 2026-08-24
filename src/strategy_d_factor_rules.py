"""策略D回封事件的公共因子值与多条件并集执行器。

研究脚本和盘中执行端只能使用这里声明的信号时点字段。因子值采用固定、可解释
区间，不使用收益分位数反推边界，避免每次研究时把未来结果写进条件定义。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


FACTOR_SCHEMA_ID = "D_RESEAL_FACTOR_VALUES_V1"
LEGACY_MODE = "LEGACY_FORMAL_D"
FACTOR_UNION_MODE = "FACTOR_UNION"
SUPPORTED_RELEASE_MODES = frozenset({LEGACY_MODE, FACTOR_UNION_MODE})
MISSING_FACTOR_VALUE = "MISSING"


@dataclass(frozen=True)
class NumericBucket:
    label: str
    lower: float | None = None
    upper: float | None = None

    def contains(self, value: float) -> bool:
        if self.lower is not None and value < self.lower:
            return False
        # 所有上界均为开区间；相邻桶不会重复命中。
        if self.upper is not None and value >= self.upper:
            return False
        return True


@dataclass(frozen=True)
class FactorDefinition:
    name: str
    source_field: str
    description: str
    buckets: tuple[NumericBucket, ...] = tuple()
    category_map: Mapping[str, str] | None = None

    @property
    def allowed_values(self) -> frozenset[str]:
        values = {bucket.label for bucket in self.buckets}
        if self.category_map:
            values.update(self.category_map.values())
        values.add(MISSING_FACTOR_VALUE)
        return frozenset(values)

    def classify(self, raw: Any) -> str:
        if self.category_map is not None:
            text = str(raw).strip()
            return self.category_map.get(text, MISSING_FACTOR_VALUE)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return MISSING_FACTOR_VALUE
        if not np.isfinite(value):
            return MISSING_FACTOR_VALUE
        for bucket in self.buckets:
            if bucket.contains(value):
                return bucket.label
        return MISSING_FACTOR_VALUE


FACTOR_DEFINITIONS: tuple[FactorDefinition, ...] = (
    FactorDefinition(
        "reseal_time_bucket",
        "signal_hhmm",
        "本次回封发生时间",
        (
            NumericBucket("0930_1000", 930, 1001),
            NumericBucket("1001_1030", 1001, 1031),
            NumericBucket("1031_1130", 1031, 1131),
            NumericBucket("1300_1359", 1300, 1400),
            NumericBucket("1400_1429", 1400, 1430),
            NumericBucket("1430_1454", 1430, 1455),
        ),
    ),
    FactorDefinition(
        "first_seal_time_bucket",
        "first_seal_hhmm",
        "首次封板时间",
        (
            NumericBucket("0930_1000", 930, 1001),
            NumericBucket("1001_1030", 1001, 1031),
            NumericBucket("1031_1130", 1031, 1131),
            NumericBucket("1300_1359", 1300, 1400),
            NumericBucket("1400_1429", 1400, 1430),
            NumericBucket("1430_1454", 1430, 1455),
        ),
    ),
    FactorDefinition(
        "open_count_bucket",
        "open_times_at_signal",
        "信号时累计炸板次数",
        (
            NumericBucket("1", 1, 2),
            NumericBucket("2", 2, 3),
            NumericBucket("3", 3, 4),
            NumericBucket("4", 4, 5),
            NumericBucket("GE5", 5, None),
        ),
    ),
    FactorDefinition(
        "reseal_speed_bucket",
        "last_break_to_signal_minutes",
        "最后炸板至本次回封的交易分钟数",
        (
            NumericBucket("LE1", 0, 2),
            NumericBucket("2_5", 2, 6),
            NumericBucket("6_10", 6, 11),
            NumericBucket("11_20", 11, 21),
            NumericBucket("GE21", 21, None),
        ),
    ),
    FactorDefinition(
        "first_to_reseal_bucket",
        "first_to_signal_minutes",
        "首次封板至本次回封的交易分钟数",
        (
            NumericBucket("LE15", 0, 16),
            NumericBucket("16_30", 16, 31),
            NumericBucket("31_60", 31, 61),
            NumericBucket("61_120", 61, 121),
            NumericBucket("GE121", 121, None),
        ),
    ),
    FactorDefinition(
        "previous_seal_hold_bucket",
        "previous_seal_to_break_minutes",
        "上一次封板维持至炸板的交易分钟数",
        (
            NumericBucket("LE1", 0, 2),
            NumericBucket("2_5", 2, 6),
            NumericBucket("6_10", 6, 11),
            NumericBucket("GE11", 11, None),
        ),
    ),
    FactorDefinition(
        "break_close_depth_bucket",
        "last_break_close_depth_pct",
        "最后炸板时相对涨停价的回落深度",
        (
            NumericBucket("LT0_2PCT", 0.0, 0.002),
            NumericBucket("0_2_0_5PCT", 0.002, 0.005),
            NumericBucket("0_5_1PCT", 0.005, 0.010),
            NumericBucket("1_2PCT", 0.010, 0.020),
            NumericBucket("GE2PCT", 0.020, None),
        ),
    ),
    FactorDefinition(
        "open_gap_bucket",
        "open_gap_pct",
        "开盘相对昨收涨幅",
        (
            NumericBucket("LT0", None, 0.0),
            NumericBucket("0_3PCT", 0.0, 0.03),
            NumericBucket("3_7PCT", 0.03, 0.07),
            NumericBucket("GE7PCT", 0.07, None),
        ),
    ),
    FactorDefinition(
        "pre_signal_low_bucket",
        "pre_signal_min_return",
        "信号前最低价相对昨收涨幅",
        (
            NumericBucket("LT0", None, 0.0),
            NumericBucket("0_3PCT", 0.0, 0.03),
            NumericBucket("3_7PCT", 0.03, 0.07),
            NumericBucket("GE7PCT", 0.07, None),
        ),
    ),
    FactorDefinition(
        "cumulative_amount_bucket",
        "signal_cumulative_amount_vs_prev_day",
        "信号前累计成交额相对前一交易日成交额",
        (
            NumericBucket("LT0_5", 0.0, 0.5),
            NumericBucket("0_5_1", 0.5, 1.0),
            NumericBucket("1_1_5", 1.0, 1.5),
            NumericBucket("GE1_5", 1.5, None),
        ),
    ),
    FactorDefinition(
        "market_touch_count_bucket",
        "market_ever_sealed_count",
        "信号时全市场累计首板触板数量",
        (
            NumericBucket("LT40", 0, 40),
            NumericBucket("40_70", 40, 71),
            NumericBucket("71_100", 71, 101),
            NumericBucket("101_150", 101, 151),
            NumericBucket("GE151", 151, None),
        ),
    ),
    FactorDefinition(
        "market_active_count_bucket",
        "market_active_sealed_count",
        "信号时全市场仍封住的首板数量",
        (
            NumericBucket("LT20", 0, 20),
            NumericBucket("20_40", 20, 41),
            NumericBucket("41_70", 41, 71),
            NumericBucket("71_100", 71, 101),
            NumericBucket("GE101", 101, None),
        ),
    ),
    FactorDefinition(
        "market_seal_rate_bucket",
        "market_seal_rate",
        "信号时全市场首板封住率",
        (
            NumericBucket("LT40PCT", 0.0, 0.40),
            NumericBucket("40_60PCT", 0.40, 0.60),
            NumericBucket("60_80PCT", 0.60, 0.80),
            NumericBucket("GE80PCT", 0.80, None),
        ),
    ),
    FactorDefinition(
        "market_break_rate_bucket",
        "market_break_event_rate",
        "信号时累计炸板事件数/累计首板触板数",
        (
            NumericBucket("LT25PCT", 0.0, 0.25),
            NumericBucket("25_50PCT", 0.25, 0.50),
            NumericBucket("50_75PCT", 0.50, 0.75),
            NumericBucket("GE75PCT", 0.75, None),
        ),
    ),
    FactorDefinition(
        "segment_bucket",
        "market_segment",
        "股票所属交易板块",
        category_map={
            "sh_main": "MAIN_BOARD",
            "sz_main": "MAIN_BOARD",
            "chi_next": "GROWTH_BOARD",
            "star": "GROWTH_BOARD",
            "bj": "BJ",
        },
    ),
    FactorDefinition(
        "segment_seal_rate_bucket",
        "same_segment_seal_rate",
        "信号时同交易板块首板封住率",
        (
            NumericBucket("LT40PCT", 0.0, 0.40),
            NumericBucket("40_60PCT", 0.40, 0.60),
            NumericBucket("60_80PCT", 0.60, 0.80),
            NumericBucket("GE80PCT", 0.80, None),
        ),
    ),
)

FACTOR_BY_NAME = {definition.name: definition for definition in FACTOR_DEFINITIONS}
FACTOR_COLUMNS = tuple(definition.name for definition in FACTOR_DEFINITIONS)
SOURCE_FIELDS = frozenset(definition.source_field for definition in FACTOR_DEFINITIONS)


def trading_minute_index(hhmm: int) -> int:
    hour, minute = divmod(int(hhmm), 100)
    absolute = hour * 60 + minute
    if absolute <= 11 * 60 + 30:
        return absolute - (9 * 60 + 30)
    return 121 + absolute - (13 * 60)


def trading_minutes_between(left: int, right: int) -> int:
    if int(left) <= 0 or int(right) <= 0:
        return 0
    return max(trading_minute_index(int(right)) - trading_minute_index(int(left)), 0)


def add_factor_values(frame: pd.DataFrame) -> pd.DataFrame:
    """把信号时点原始字段转换为固定因子值。"""

    missing = sorted(SOURCE_FIELDS.difference(frame.columns))
    if missing:
        raise ValueError(f"D因子化缺少信号时点字段: {','.join(missing)}")
    result = frame.copy()
    for definition in FACTOR_DEFINITIONS:
        result[definition.name] = result[definition.source_field].map(
            definition.classify
        )
    return result


def factor_values_from_raw(raw: Mapping[str, Any]) -> dict[str, str]:
    """供盘中监控把当前回封状态转换成与历史一致的因子值。"""

    return {
        definition.name: definition.classify(raw.get(definition.source_field))
        for definition in FACTOR_DEFINITIONS
    }


def normalize_conditions(conditions: Mapping[str, Any]) -> dict[str, str]:
    normalized = {str(key): str(value) for key, value in conditions.items()}
    if not normalized:
        raise ValueError("D因子条件不能为空")
    unknown = sorted(set(normalized).difference(FACTOR_BY_NAME))
    if unknown:
        raise ValueError(f"D因子条件包含未知因子: {','.join(unknown)}")
    invalid = {
        name: value
        for name, value in normalized.items()
        if value not in FACTOR_BY_NAME[name].allowed_values
        or value == MISSING_FACTOR_VALUE
    }
    if invalid:
        raise ValueError(f"D因子条件包含无效因子值: {invalid}")
    return dict(sorted(normalized.items()))


def profile_matches_values(
    factor_values: Mapping[str, Any], conditions: Mapping[str, Any]
) -> bool:
    normalized = normalize_conditions(conditions)
    return all(str(factor_values.get(name, "")) == value for name, value in normalized.items())


def matching_profile_ids(
    factor_values: Mapping[str, Any], profiles: Iterable[Mapping[str, Any]]
) -> list[str]:
    matched: list[str] = []
    for profile in profiles:
        conditions = profile.get("conditions", {})
        if not isinstance(conditions, Mapping):
            raise ValueError("D因子配置conditions必须是对象")
        if profile_matches_values(factor_values, conditions):
            matched.append(str(profile.get("profile_id", "")))
    return matched


def profile_mask(frame: pd.DataFrame, conditions: Mapping[str, Any]) -> pd.Series:
    normalized = normalize_conditions(conditions)
    result = pd.Series(True, index=frame.index)
    for name, value in normalized.items():
        if name not in frame.columns:
            raise ValueError(f"D因子数据缺少列: {name}")
        result &= frame[name].astype(str).eq(value)
    return result.fillna(False)


def validate_release(payload: Mapping[str, Any]) -> dict[str, Any]:
    release = dict(payload)
    if int(release.get("schema_version", 0) or 0) != 1:
        raise ValueError("D因子发布文件schema_version不支持")
    if str(release.get("factor_schema_id", "")) != FACTOR_SCHEMA_ID:
        raise ValueError("D因子发布文件factor_schema_id不匹配")
    mode = str(release.get("strategy_mode", ""))
    if mode not in SUPPORTED_RELEASE_MODES:
        raise ValueError(f"D因子发布模式不支持: {mode}")
    profiles = release.get("profiles", [])
    if not isinstance(profiles, list):
        raise ValueError("D因子发布文件profiles必须是数组")
    if mode == FACTOR_UNION_MODE and not profiles:
        raise ValueError("FACTOR_UNION发布至少需要一条条件")
    identifiers: set[str] = set()
    normalized_profiles: list[dict[str, Any]] = []
    for position, raw in enumerate(profiles, 1):
        if not isinstance(raw, Mapping):
            raise ValueError("D因子发布条件必须是对象")
        profile = dict(raw)
        profile_id = str(profile.get("profile_id", "")).strip()
        if not profile_id or profile_id in identifiers:
            raise ValueError(f"D因子发布条件ID缺失或重复: {profile_id}")
        identifiers.add(profile_id)
        profile["conditions"] = normalize_conditions(profile.get("conditions", {}))
        profile.setdefault("priority", position)
        normalized_profiles.append(profile)
    release["profiles"] = normalized_profiles
    return release


def load_factor_release(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("D因子发布文件根节点必须是对象")
    return validate_release(payload)


def release_uses_factor_union(release: Mapping[str, Any]) -> bool:
    return str(release.get("strategy_mode", "")) == FACTOR_UNION_MODE
