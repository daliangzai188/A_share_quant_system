"""策略C的固定因子值、OR条件并集与正式发布读取器。

本模块只接受T日收盘时已经存在的离散字段。研究脚本、每日候选和严格回放
共用同一套字段、缺失值和OR匹配语义，避免研究结果无法在实盘复现。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


FACTOR_SCHEMA_ID = "C_CLOSE_FACTOR_VALUES_V1"
LEGACY_MODE = "LEGACY_FORMAL_C"
FACTOR_UNION_MODE = "FACTOR_UNION"
SUPPORTED_RELEASE_MODES = frozenset({LEGACY_MODE, FACTOR_UNION_MODE})
MISSING_FACTOR_VALUE = "MISSING"

# 这些字段全部由FactorAnalyzer在T日数据上按固定边界生成。刻意不纳入竞价、
# 开盘后5分钟、收益、次日价格和退出结果等非C信号时点字段。
FACTOR_VALUE_DOMAINS: dict[str, frozenset[str]] = {
    "market_segment": frozenset({"sh_main", "sz_main", "chi_next", "bj"}),
    "market_emotion_state_bucket": frozenset(
        {"ice_point", "warming", "main_rise", "climax", "retreat", "mixed"}
    ),
    "segment_emotion_state_bucket": frozenset(
        {"ice_point", "warming", "main_rise", "climax", "retreat", "mixed"}
    ),
    "market_chain_count_bucket": frozenset({"lt_3", "3_8", "8_15", "15_30", "gte_30"}),
    "segment_chain_count_bucket": frozenset({"lt_1", "1_3", "3_5", "5_10", "gte_10"}),
    "market_limit_down_count_bucket": frozenset({"lt_5", "5_15", "15_30", "30_60", "gte_60"}),
    "segment_limit_down_ratio_bucket": frozenset(
        {"lt_0_1pct", "0_1pct_0_3pct", "0_3pct_0_8pct", "0_8pct_1_5pct", "gte_1_5pct"}
    ),
    "segment_limit_max_height_bucket": frozenset({"1", "2", "3", "4_5", "gte_6"}),
    "market_leader_rank_bucket": frozenset(
        {"rank_1", "rank_2_3", "rank_4_10", "rank_11_30", "rank_gt_30"}
    ),
    "segment_market_leader_rank_bucket": frozenset(
        {"rank_1", "rank_2_3", "rank_4_10", "rank_11_30", "rank_gt_30"}
    ),
    "segment_limit_height_rank_bucket": frozenset(
        {"rank_1", "rank_2_3", "rank_4_10", "rank_11_30", "rank_gt_30"}
    ),
    "first_time_detail_bucket": frozenset(
        {"open_auction", "before_1000", "1000_1100", "1100_1330", "1330_1430", "after_1430"}
    ),
    "limit_times_detail_bucket": frozenset({"0", "1", "2", "3", "4", "5", "6_plus"}),
    "open_times_bucket": frozenset({"0", "1", "2_3", "gte_4"}),
    "amount_bucket": frozenset({"lt_1e8", "1e8_3e8", "3e8_8e8", "8e8_15e8", "gte_15e8"}),
    "turnover_rate_bucket": frozenset({"lt_3", "3_6", "6_10", "10_15", "15_25", "gte_25"}),
    "volume_ratio_bucket": frozenset({"lt_1", "1_2", "2_4", "4_8", "gte_8"}),
    "fd_ratio_bucket": frozenset(
        {"lt_0_1pct", "0_1pct_0_3pct", "0_3pct_0_5pct", "0_5pct_1pct", "1pct_2pct", "2pct_5pct", "gte_5pct"}
    ),
    "prev_pct_chg_bucket": frozenset({"lt_neg3", "neg3_0", "0_3", "3_7", "7_10", "gte_10"}),
    "amount_ratio_bucket": frozenset({"lt_0_8", "0_8_1_2", "1_2_2", "2_3", "3_5", "gte_5"}),
    "limit_up_count_bucket": frozenset({"lt_30", "30_50", "50_80", "80_120", "120_180", "gte_180"}),
    "segment_limit_up_count_bucket": frozenset({"lt_5", "5_10", "10_20", "20_40", "40_80", "gte_80"}),
    "segment_limit_up_ratio_bucket": frozenset(
        {"lt_0_5pct", "0_5pct_1pct", "1pct_2pct", "2pct_3pct", "3pct_5pct", "gte_5pct"}
    ),
    "retreat_state_bucket": frozenset(
        {"neutral", "retreat_2day", "retreat_weak", "warming_2day", "weak_below_30"}
    ),
    "segment_retreat_state_bucket": frozenset(
        {"neutral", "retreat_2day", "retreat_weak", "warming_2day", "weak_below_3"}
    ),
    "board_type": frozenset({"one_word", "t_board", "multi_open"}),
}

FACTOR_COLUMNS = tuple(FACTOR_VALUE_DOMAINS)


def normalize_factor_value(value: Any) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return MISSING_FACTOR_VALUE
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "<na>", "unknown"}:
        return MISSING_FACTOR_VALUE
    return text


def add_factor_values(frame: pd.DataFrame) -> pd.DataFrame:
    """标准化C固定因子值，并拒绝未知分类边界。"""

    missing = sorted(set(FACTOR_COLUMNS).difference(frame.columns))
    if missing:
        raise ValueError(f"C因子化缺少T日字段: {','.join(missing)}")
    result = frame.copy()
    for column, domain in FACTOR_VALUE_DOMAINS.items():
        result[column] = result[column].map(normalize_factor_value)
        unknown = sorted(
            set(result[column].unique()).difference(domain | {MISSING_FACTOR_VALUE})
        )
        if unknown:
            raise ValueError(f"C因子值超出冻结域: {column}={','.join(unknown)}")
    return result


def normalize_conditions(conditions: Mapping[str, Any]) -> dict[str, str]:
    normalized = {str(name): normalize_factor_value(value) for name, value in conditions.items()}
    if not normalized:
        raise ValueError("C因子条件不能为空")
    unknown_columns = sorted(set(normalized).difference(FACTOR_VALUE_DOMAINS))
    if unknown_columns:
        raise ValueError(f"C因子条件包含未知字段: {','.join(unknown_columns)}")
    for name, value in normalized.items():
        if value == MISSING_FACTOR_VALUE or value not in FACTOR_VALUE_DOMAINS[name]:
            raise ValueError(f"C因子条件值非法: {name}={value}")
    return dict(sorted(normalized.items()))


def profile_mask(frame: pd.DataFrame, conditions: Mapping[str, Any]) -> pd.Series:
    normalized = normalize_conditions(conditions)
    mask = pd.Series(True, index=frame.index)
    for column, value in normalized.items():
        if column not in frame.columns:
            raise ValueError(f"C候选缺少正式条件字段: {column}")
        mask &= frame[column].fillna(MISSING_FACTOR_VALUE).astype(str).eq(value)
    return mask


def matching_profile_ids(
    factors: Mapping[str, Any], profiles: Iterable[Mapping[str, Any]]
) -> list[str]:
    normalized_factors = {str(key): normalize_factor_value(value) for key, value in factors.items()}
    matched: list[str] = []
    for profile in profiles:
        conditions = normalize_conditions(profile.get("conditions", {}))
        if all(normalized_factors.get(name) == value for name, value in conditions.items()):
            matched.append(str(profile.get("profile_id", "")))
    return [value for value in matched if value]


def apply_profile_union(
    frame: pd.DataFrame,
    profiles: Iterable[Mapping[str, Any]],
    *,
    include_match_ids: bool = True,
) -> pd.DataFrame:
    """保留命中任一if分支的C候选，并写入命中的分支编号。"""

    result = add_factor_values(frame)
    profile_list = list(profiles)
    if not profile_list:
        return result.iloc[0:0].copy()
    hits: list[list[str]] = [[] for _ in range(len(result))] if include_match_ids else []
    union = np.zeros(len(result), dtype=bool)
    for profile in profile_list:
        profile_id = str(profile["profile_id"])
        mask = profile_mask(result, profile["conditions"]).to_numpy(dtype=bool)
        union |= mask
        if include_match_ids:
            for position in np.flatnonzero(mask):
                hits[int(position)].append(profile_id)
    selected = result.loc[union].copy()
    if include_match_ids:
        selected["matched_c_profile_ids"] = [
            ";".join(hits[position]) for position in np.flatnonzero(union)
        ]
    return selected


def validate_release(payload: Mapping[str, Any]) -> dict[str, Any]:
    release = dict(payload)
    if int(release.get("schema_version", 0) or 0) != 1:
        raise ValueError("C因子发布schema_version不支持")
    if str(release.get("factor_schema_id", "")) != FACTOR_SCHEMA_ID:
        raise ValueError("C因子发布factor_schema_id不匹配")
    mode = str(release.get("strategy_mode", ""))
    if mode not in SUPPORTED_RELEASE_MODES:
        raise ValueError(f"C因子发布模式不支持: {mode}")
    profiles = release.get("profiles", [])
    if not isinstance(profiles, list):
        raise ValueError("C因子发布profiles必须是列表")
    if mode == FACTOR_UNION_MODE and not profiles:
        raise ValueError("C因子并集模式至少需要一个if分支")
    seen: set[str] = set()
    normalized_profiles: list[dict[str, Any]] = []
    for position, raw in enumerate(profiles, 1):
        profile = dict(raw)
        profile_id = str(profile.get("profile_id", "")).strip()
        if not profile_id or profile_id in seen:
            raise ValueError(f"C因子分支编号缺失或重复: {profile_id}")
        seen.add(profile_id)
        profile["profile_id"] = profile_id
        profile["priority"] = int(profile.get("priority", position))
        profile["conditions"] = normalize_conditions(profile.get("conditions", {}))
        normalized_profiles.append(profile)
    release["profiles"] = sorted(
        normalized_profiles, key=lambda item: (int(item["priority"]), item["profile_id"])
    )
    return release


def load_factor_release(path: str | Path) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.exists():
        raise FileNotFoundError(f"缺少C正式因子发布文件: {candidate}")
    payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("C正式因子发布文件根节点必须是对象")
    return validate_release(payload)
