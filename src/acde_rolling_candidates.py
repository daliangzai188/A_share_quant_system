"""A/C/E/D三年滚动研究的风格安全候选与严格计划结果构造器。"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from scripts.build_ac_daily_candidates import (
    DATES,
    DIDX,
    fixed_open_entry_details,
    trade_return_details,
)
from scripts.run_paper_ab_filtered_daily_ops import condition_strategy_config
from scripts.run_paper_ab_filtered_observation_window import reject_strategy_risk_mask
from scripts.validate_other_live_strategies_strict import account_return
from src.paper_candidate_generator import PaperCandidateGenerator
from src.strategy_d_factor_rules import add_factor_values, profile_mask
from src.strategy_e import (
    build_r1_universe_from_pool,
    resolve_exit_offset,
    select_e_daily_picks,
)


ROOT = Path(__file__).resolve().parents[1]
STRATEGY_CONFIG_PATH = ROOT / "config/strategy_config.json"


@dataclass(frozen=True)
class VariantDefinition:
    strategy_leg: str
    variant_id: str
    description: str
    payload: Any
    changed_axis_count: int
    style_gate_passed: bool = False
    style_gate_reason: str = "未显式完成固定风格审核，默认禁止进入候选排名"


def _bool_series(frame: pd.DataFrame, column: str, default: bool = False) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype="bool")
    return (
        frame[column]
        .fillna(default)
        .astype(str)
        .str.strip()
        .str.lower()
        .isin({"true", "1", "yes"})
    )


def strict_signal_pool(
    pool: pd.DataFrame,
    *,
    signal_dates: set[str],
    allowed_signal_dates: set[str],
) -> pd.DataFrame:
    """应用四腿共用的严格数据质量与前一收盘市场硬门禁。"""

    result = pool.copy()
    result["trade_date"] = (
        result["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    )
    result = result[
        result["trade_date"].isin(signal_dates)
        & result["trade_date"].isin(allowed_signal_dates)
    ].copy()
    reliable = (
        _bool_series(result, "allow_buy_reliable")
        & _bool_series(result, "is_fill_score_reliable")
        & ~_bool_series(result, "is_fd_amount_abnormal", True)
        & _bool_series(result, "strategy_compatible")
    )
    if "limit_data_quality" in result.columns:
        reliable &= result["limit_data_quality"].fillna("").astype(str).eq("full")
    return result.loc[reliable].sort_values(
        ["trade_date", "ts_code"]
    ).reset_index(drop=True)


def previous_close_market_gate(
    *,
    calendar: pd.DataFrame,
    sentiment: pd.DataFrame,
    action_dates: Sequence[str],
    minimum_limit_up_count: int,
) -> tuple[set[str], set[str], pd.DataFrame]:
    """用开仓日前一交易日收盘状态统一裁决A/C/E/D，避免D使用当日收盘未来值。"""

    opened = calendar.copy()
    opened["cal_date"] = (
        opened["cal_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    )
    if "is_open" in opened.columns:
        opened = opened[
            opened["is_open"].astype(str).str.lower().isin({"1", "1.0", "true"})
        ]
    dates = sorted(opened["cal_date"].unique())
    positions = {date: index for index, date in enumerate(dates)}

    state = sentiment.copy()
    state["trade_date"] = (
        state["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    )
    if state["trade_date"].duplicated().any():
        raise ValueError("统一市场门禁的情绪表交易日重复")
    count_map = dict(
        zip(
            state["trade_date"],
            pd.to_numeric(state["limit_up_count"], errors="coerce"),
        )
    )
    rows: list[dict[str, Any]] = []
    allowed_actions: set[str] = set()
    allowed_signals: set[str] = set()
    for action_date in sorted(set(map(str, action_dates))):
        index = positions.get(action_date)
        if index is None or index <= 0:
            raise RuntimeError(f"市场门禁无法找到{action_date}的前一交易日")
        signal_date = dates[index - 1]
        value = count_map.get(signal_date)
        if value is None or pd.isna(value):
            raise RuntimeError(f"市场门禁缺少{signal_date}涨停数")
        passed = float(value) >= int(minimum_limit_up_count)
        if passed:
            allowed_actions.add(action_date)
            allowed_signals.add(signal_date)
        rows.append(
            {
                "action_date": action_date,
                "state_date": signal_date,
                "limit_up_count": int(value),
                "minimum_limit_up_count": int(minimum_limit_up_count),
                "hard_gate_passed": bool(passed),
            }
        )
    return allowed_actions, allowed_signals, pd.DataFrame(rows)


def make_generator(config: dict[str, Any]) -> PaperCandidateGenerator:
    generator = PaperCandidateGenerator(STRATEGY_CONFIG_PATH)
    generator.config = copy.deepcopy(config)
    generator.paper_config = generator.config.get("paper_candidate", {})
    generator.risk_thresholds = generator.paper_config.get("risk_thresholds", {})
    return generator


def _profiles_from_axes(
    *,
    prefix: str,
    segment_values: Iterable[str],
    chain_values: Iterable[str],
    fd_values: Iterable[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for position, (segment_value, chain_value, fd_value) in enumerate(
        (
            (segment_value, chain_value, fd_value)
            for segment_value in segment_values
            for chain_value in chain_values
            for fd_value in fd_values
        ),
        1,
    ):
        rows.append(
            {
                "profile_id": f"{prefix}_{position:02d}",
                "priority": position,
                "conditions": [
                    {"column": "segment_limit_up_count_bucket", "operator": "==", "value": segment_value},
                    {"column": "market_chain_count_bucket", "operator": "==", "value": chain_value},
                    {"column": "fd_ratio_bucket", "operator": "==", "value": fd_value},
                ],
            }
        )
    return rows


def a_variants(base_config: dict[str, Any]) -> tuple[VariantDefinition, list[VariantDefinition]]:
    baseline = VariantDefinition(
        "A", "A_CURRENT", "当前正式A", copy.deepcopy(base_config), 0, True,
        "冻结正式A基线，仅用于比较",
    )

    def configured(
        variant_id: str,
        description: str,
        *,
        segments: tuple[str, ...] = ("lt_5",),
        chains: tuple[str, ...] = ("8_15",),
        fds: tuple[str, ...] = ("0_3pct_0_5pct", "0_5pct_1pct"),
        fallback: bool = True,
        ranking: tuple[list[str], list[bool]] | None = None,
    ) -> VariantDefinition:
        config = copy.deepcopy(base_config)
        config["candidate_filters"]["condition_profiles"] = _profiles_from_axes(
            prefix=variant_id,
            segment_values=segments,
            chain_values=chains,
            fd_values=fds,
        )
        config["candidate_filters"]["fallback_when_primary_empty"]["enabled"] = fallback
        if ranking is not None:
            config["ranking"]["columns"] = list(ranking[0])
            config["ranking"]["ascending"] = list(ranking[1])
        return VariantDefinition(
            "A", variant_id, description, config, 1, True,
            "只调整A内部热度、连板、封单、fallback或排序，保持低热度首板强度风格",
        )

    def risk_exclusion_variant(
        variant_id: str,
        description: str,
        *,
        column: str,
        values: tuple[str, ...],
    ) -> VariantDefinition:
        """对A每日第一名增加无回补的信号日尾部风险门禁。"""

        config = copy.deepcopy(base_config)
        config["rolling_research_post_pick_exclude"] = {
            "column": column,
            "values": list(values),
            "fallback_to_second_candidate": False,
        }
        return VariantDefinition(
            "A", variant_id, description, config, 1, True,
            "冻结A每日第一名后再做信号日尾部风险门禁；命中时空仓且不回补第二名",
        )

    candidates = [
        configured(
            "A_SEGMENT_ADJACENT",
            "低分段热度从lt_5相邻放宽到lt_5或5_10",
            segments=("lt_5", "5_10"),
        ),
        configured(
            "A_CHAIN_LOWER_ADJACENT",
            "连板环境向下相邻放宽到3_8或8_15",
            chains=("3_8", "8_15"),
        ),
        configured(
            "A_CHAIN_UPPER_ADJACENT",
            "连板环境向上相邻放宽到8_15或15_30",
            chains=("8_15", "15_30"),
        ),
        configured(
            "A_FD_NARROW",
            "主分支封单比例收窄为0.5%~1%",
            fds=("0_5pct_1pct",),
        ),
        configured(
            "A_FD_LOWER_ADJACENT",
            "主分支封单比例相邻放宽为0.1%~1%",
            fds=("0_1pct_0_3pct", "0_3pct_0_5pct", "0_5pct_1pct"),
        ),
        configured("A_NO_FALLBACK", "关闭主池空缺日的1%~2%非一字补位", fallback=False),
        configured(
            "A_RANK_SCORE_TURNOVER",
            "保持条件，仅改为利润源得分后按换手率降序",
            ranking=(["profit_source_score", "turnover_rate"], [False, False]),
        ),
        configured(
            "A_RANK_SCORE_FD",
            "保持条件，仅改为利润源得分后按封单比例降序",
            ranking=(["profit_source_score", "fd_amount_to_circ_mv"], [False, False]),
        ),
        configured(
            "A_RANK_SCORE_FIRST_TIME",
            "保持条件，仅改为利润源得分后按首次封板时间早优先",
            ranking=(["profit_source_score", "first_time"], [False, True]),
        ),
        configured(
            "A_RANK_SCORE_AMOUNT",
            "保持条件，仅改为利润源得分后按成交额降序",
            ranking=(["profit_source_score", "amount"], [False, False]),
        ),
        risk_exclusion_variant(
            "A_RISK_EXCLUDE_MARKET_DOWN_30_60",
            "排除全市场跌停数30~60的高压力信号日",
            column="market_limit_down_count_bucket",
            values=("30_60",),
        ),
        risk_exclusion_variant(
            "A_RISK_EXCLUDE_MARKET_DOWN_GE30",
            "排除全市场跌停数达到30只以上的极端压力信号日",
            column="market_limit_down_count_bucket",
            values=("30_60", "gte_60"),
        ),
        risk_exclusion_variant(
            "A_RISK_EXCLUDE_LIMIT_UP_GE120",
            "排除全市场涨停数达到120只以上的过热信号日",
            column="limit_up_count_bucket",
            values=("120_180", "gte_180"),
        ),
        risk_exclusion_variant(
            "A_RISK_EXCLUDE_TURNOVER_3_6",
            "排除换手率3%~6%的低换手弱承接候选",
            column="turnover_rate_bucket",
            values=("3_6",),
        ),
        risk_exclusion_variant(
            "A_RISK_EXCLUDE_AFTERNOON_FIRST_SEAL",
            "每日第一名首次封板属于下午时段时空仓，不回补第二名",
            column="first_time_bucket",
            values=("afternoon",),
        ),
    ]
    return baseline, candidates


def apply_a_research_post_pick_gate(
    picks: pd.DataFrame,
    config: Mapping[str, Any],
) -> pd.DataFrame:
    """对已经冻结的A每日第一名执行研究风险门禁，不允许回补第二名。"""

    result = picks.copy().reset_index(drop=True)
    gate = config.get("rolling_research_post_pick_exclude", {})
    if result.empty or not gate:
        return result
    if bool(gate.get("fallback_to_second_candidate", True)):
        raise ValueError("A研究尾部门禁禁止回补当日第二名")
    column = str(gate.get("column", ""))
    values = {str(value) for value in gate.get("values", [])}
    if not column or not values or column not in result.columns:
        raise ValueError("A研究尾部门禁字段或排除值非法")
    return result.loc[
        ~result[column].fillna("").astype(str).isin(values)
    ].copy().reset_index(drop=True)


def build_a_picks(pool: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    generator = make_generator(config)
    filtered = generator.apply_strategy_filters(pool)
    picks: list[pd.Series] = []
    for _date, group in filtered.groupby("trade_date", sort=True):
        ranked = generator.rank_candidates(group.copy()).reset_index(drop=True)
        if not ranked.empty:
            picks.append(ranked.iloc[0])
    result = pd.DataFrame(picks).reset_index(drop=True) if picks else pd.DataFrame()
    return apply_a_research_post_pick_gate(result, config)


def _append_profile_value(
    profiles: list[dict[str, Any]],
    *,
    source_index: int,
    column: str,
    value: str,
    suffix: str,
) -> None:
    profile = copy.deepcopy(profiles[source_index])
    profile["profile_id"] = f"{profile['profile_id']}_{suffix}"
    profile["priority"] = max(int(item.get("priority", 0)) for item in profiles) + 1
    for condition in profile["conditions"]:
        if str(condition["column"]) == column:
            condition["value"] = value
            break
    else:
        raise KeyError(f"C分支缺少待调整字段：{column}")
    profiles.append(profile)


def c_variants(base_config: dict[str, Any]) -> tuple[VariantDefinition, list[VariantDefinition]]:
    baseline = VariantDefinition(
        "C", "C_CURRENT", "当前正式C", copy.deepcopy(base_config), 0, False,
        "冻结正式C基线仅用于比较；现有分支未被证明全部属于强势/主升状态",
    )

    def profile_variant(
        variant_id: str,
        description: str,
        *,
        source_index: int,
        column: str,
        value: str,
    ) -> VariantDefinition:
        config = copy.deepcopy(base_config)
        profiles = config["paper_ab_filtered_strategy"]["c_strategy"]["condition_profiles"]
        _append_profile_value(
            profiles,
            source_index=source_index,
            column=column,
            value=value,
            suffix=variant_id,
        )
        return VariantDefinition(
            "C",
            variant_id,
            description,
            config,
            1,
            False,
            (
                "未显式限定warming/main_rise/climax；只能诊断现有C规则，"
                "不满足固定的强势/主升龙头风格门禁"
            ),
        )

    def ranking_variant(
        variant_id: str,
        description: str,
        columns: list[str],
        ascending: list[bool],
    ) -> VariantDefinition:
        config = copy.deepcopy(base_config)
        ranking = config["paper_ab_filtered_strategy"]["c_strategy"]["ranking"]
        ranking["columns"] = columns
        ranking["ascending"] = ascending
        return VariantDefinition(
            "C",
            variant_id,
            description,
            config,
            1,
            False,
            (
                "排序仍覆盖未显式限定warming/main_rise/climax的分支；"
                "只能作为数学诊断候选"
            ),
        )

    def explicit_style_values_variant(
        variant_id: str,
        description: str,
        *,
        column: str,
        values: tuple[str, ...],
        reason: str,
    ) -> VariantDefinition:
        config = copy.deepcopy(base_config)
        source_profiles = config["paper_ab_filtered_strategy"]["c_strategy"][
            "condition_profiles"
        ]
        profiles: list[dict[str, Any]] = []
        for source in source_profiles:
            for value in values:
                profile = copy.deepcopy(source)
                profile["profile_id"] = (
                    f"{source['profile_id']}_{variant_id}_{value.upper()}"
                )
                profile["priority"] = len(profiles) + 1
                profile["conditions"].append(
                    {
                        "column": column,
                        "operator": "==",
                        "value": value,
                    }
                )
                profiles.append(profile)
        config["paper_ab_filtered_strategy"]["c_strategy"][
            "condition_profiles"
        ] = profiles
        return VariantDefinition(
            "C",
            variant_id,
            description,
            config,
            1,
            True,
            reason,
        )

    def risk_exclusion_variant(
        variant_id: str,
        description: str,
        *,
        column: str,
        values: tuple[str, ...],
    ) -> VariantDefinition:
        """保留C双分支与排序，仅增加一个信号日尾部风险排除轴。"""

        config = copy.deepcopy(base_config)
        exclusions = config["candidate_filters"].setdefault(
            "exclude_conditions", []
        )
        exclusions.extend(
            {"column": column, "operator": "==", "value": value}
            for value in values
        )
        return VariantDefinition(
            "C",
            variant_id,
            description,
            config,
            1,
            True,
            "冻结C双分支、排序和T+3退出，只增加信号日已知的尾部风险排除",
        )

    def guarded_profile_variant(
        variant_id: str,
        description: str,
        *,
        source_index: int,
        changed_column: str,
        changed_value: str,
        guard_column: str,
        guard_values: tuple[str, ...],
    ) -> VariantDefinition:
        """仅在信号日风险环境合格时扩展C的相邻龙头桶。"""

        config = copy.deepcopy(base_config)
        profiles = config["paper_ab_filtered_strategy"]["c_strategy"][
            "condition_profiles"
        ]
        source = copy.deepcopy(profiles[source_index])
        next_priority = max(int(item.get("priority", 0)) for item in profiles) + 1
        for guard_value in guard_values:
            profile = copy.deepcopy(source)
            profile["profile_id"] = (
                f"{source['profile_id']}_{variant_id}_{guard_value.upper()}"
            )
            profile["priority"] = next_priority
            next_priority += 1
            for condition in profile["conditions"]:
                if str(condition["column"]) == changed_column:
                    condition["value"] = changed_value
                    break
            else:
                raise KeyError(f"C分支缺少待调整字段：{changed_column}")
            profile["conditions"].append(
                {
                    "column": guard_column,
                    "operator": "==",
                    "value": guard_value,
                }
            )
            profiles.append(profile)
        return VariantDefinition(
            "C",
            variant_id,
            description,
            config,
            2,
            True,
            "只在信号日跌停压力非极端或分段非冰点时扩展龙头第2~3名，保持C进攻身份",
        )

    candidates = [
        explicit_style_values_variant(
            "C_STRONG_REGIME_ONLY",
            "固定原双分支，只允许全市场warming/main_rise/climax强势状态",
            column="market_emotion_state",
            values=("warming", "main_rise", "climax"),
            reason="显式限定全市场warming/main_rise/climax，保持C强势/主升龙头身份",
        ),
        explicit_style_values_variant(
            "C_SEGMENT_STRONG_REGIME_ONLY",
            "固定原双分支，只允许所属分段warming/main_rise/climax强势状态",
            column="segment_emotion_state",
            values=("warming", "main_rise", "climax"),
            reason="显式限定所属分段warming/main_rise/climax，保持C强势/主升龙头身份",
        ),
        explicit_style_values_variant(
            "C_SEGMENT_HEIGHT_GE4",
            "固定原双分支，只允许所属分段最高板达到4板以上",
            column="segment_limit_max_height_bucket",
            values=("4_5", "gte_6"),
            reason="用信号日所属分段最高板至少4板确认强势高度，保持C龙头进攻身份",
        ),
        profile_variant(
            "C_CORE_CHAIN_ADJACENT",
            "承接分支连板环境相邻加入8_15",
            source_index=0,
            column="market_chain_count_bucket",
            value="8_15",
        ),
        profile_variant(
            "C_CORE_SEGMENT_ADJACENT",
            "承接分支分段热度相邻加入gte_80",
            source_index=0,
            column="segment_limit_up_count_bucket",
            value="gte_80",
        ),
        profile_variant(
            "C_LEADER_MARKET_ADJACENT",
            "龙头分支全市场热度相邻加入80_120",
            source_index=1,
            column="limit_up_count_bucket",
            value="80_120",
        ),
        profile_variant(
            "C_LEADER_RANK_ADJACENT",
            "龙头分支排名相邻加入2_3",
            source_index=1,
            column="market_leader_rank_bucket",
            value="rank_2_3",
        ),
        profile_variant(
            "C_LEADER_FD_ADJACENT",
            "龙头分支封单比例相邻加入0.3%~0.5%",
            source_index=1,
            column="fd_ratio_bucket",
            value="0_3pct_0_5pct",
        ),
        ranking_variant(
            "C_RANK_LEADER_SCORE_TURNOVER",
            "龙头排名优先，再按利润源得分和换手率",
            ["market_leader_rank", "profit_source_score", "turnover_rate"],
            [True, False, False],
        ),
        ranking_variant(
            "C_RANK_HEIGHT_LEADER_SCORE",
            "板高排名、龙头排名优先，再按利润源得分",
            ["limit_height_rank", "market_leader_rank", "profit_source_score"],
            [True, True, False],
        ),
        ranking_variant(
            "C_RANK_FILL_LEADER_TURNOVER",
            "成交概率优先，再按龙头排名和换手率",
            ["fill_probability", "market_leader_rank", "turnover_rate"],
            [False, True, False],
        ),
        explicit_style_values_variant(
            "C_SEGMENT_NON_ICE_POINT",
            "保留C双分支，但排除所属分段ice_point",
            column="segment_emotion_state",
            values=("mixed", "retreat", "warming", "main_rise", "climax"),
            reason="显式排除所属分段冰点，保留C强势承接与龙头身份",
        ),
        explicit_style_values_variant(
            "C_MARKET_LIMIT_DOWN_LT30",
            "保留C双分支，只允许全市场跌停数少于30只",
            column="market_limit_down_count_bucket",
            values=("lt_5", "5_15", "15_30"),
            reason="用信号日全市场跌停压力排除极弱环境，保留C进攻身份",
        ),
        explicit_style_values_variant(
            "C_SEGMENT_LIMIT_DOWN_LT15",
            "保留C双分支，只允许所属分段跌停数少于15只",
            column="segment_limit_down_count_bucket",
            values=("lt_1", "1_3", "3_8", "8_15"),
            reason="用信号日所属分段跌停压力排除极弱环境，保留C进攻身份",
        ),
        risk_exclusion_variant(
            "C_RISK_EXCLUDE_SINGLE_OPEN",
            "排除炸板次数恰为1次的弱回封候选",
            column="open_times_bucket",
            values=("1",),
        ),
        guarded_profile_variant(
            "C_LEADER_RANK_2_3_LIMIT_DOWN_LT30",
            "仅在全市场跌停少于30只时，将C龙头分支扩展至市场第2~3名",
            source_index=1,
            changed_column="market_leader_rank_bucket",
            changed_value="rank_2_3",
            guard_column="market_limit_down_count_bucket",
            guard_values=("lt_5", "5_15", "15_30"),
        ),
        guarded_profile_variant(
            "C_LEADER_RANK_2_3_SEGMENT_NON_ICE",
            "仅在所属分段非冰点时，将C龙头分支扩展至市场第2~3名",
            source_index=1,
            changed_column="market_leader_rank_bucket",
            changed_value="rank_2_3",
            guard_column="segment_emotion_state",
            guard_values=("mixed", "retreat", "warming", "main_rise", "climax"),
        ),
    ]
    return baseline, candidates


def build_c_picks(pool: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    c_config = config["paper_ab_filtered_strategy"]["c_strategy"]
    selected = condition_strategy_config(
        config,
        [],
        "C_ROLLING_RESEARCH",
        condition_profiles=copy.deepcopy(c_config["condition_profiles"]),
    )
    generator = make_generator(selected)
    filtered = generator.apply_strategy_filters(pool)
    picks: list[pd.Series] = []
    for _date, group in filtered.groupby("trade_date", sort=True):
        ranked = generator.rank_candidates(group.copy()).reset_index(drop=True)
        ranked["risk_flags"] = [
            generator.build_risk_flags(row) for row in ranked.itertuples(index=False)
        ]
        rejected = reject_strategy_risk_mask(ranked, config, "c_strategy")
        accepted = ranked.loc[~rejected].reset_index(drop=True)
        if not accepted.empty:
            picks.append(accepted.iloc[0])
    return pd.DataFrame(picks).reset_index(drop=True) if picks else pd.DataFrame()


def e_variants(base_spec: dict[str, Any]) -> tuple[VariantDefinition, list[VariantDefinition]]:
    baseline = VariantDefinition(
        "E", "E_CURRENT", "当前正式E", copy.deepcopy(base_spec), 0, False,
        "冻结正式E基线仅用于比较；现有40条R1并集未被证明每笔都属于冰点/退潮修复",
    )

    def ranking_variant(
        variant_id: str,
        description: str,
        columns: list[str],
        ascending: list[bool],
        weights: list[float],
    ) -> VariantDefinition:
        spec = copy.deepcopy(base_spec)
        spec["final_ranking"] = {
            **spec["final_ranking"],
            "method": "daily_percentile_weighted_score",
            "columns": columns,
            "ascending": ascending,
            "weights": weights,
        }
        return VariantDefinition(
            "E", variant_id, description, spec, 1, False,
            "排序仍覆盖未显式限定冰点/退潮修复的R1分支，只能作为数学诊断候选",
        )

    def style_filter_variant(
        variant_id: str,
        description: str,
        column: str,
        values: list[str],
    ) -> VariantDefinition:
        spec = copy.deepcopy(base_spec)
        spec["rolling_research_style_filter"] = {
            "column": column,
            "values": values,
        }
        return VariantDefinition(
            "E", variant_id, description, spec, 1, True,
            "显式限定冰点、退潮或回暖修复状态，保持E的修复风格",
        )

    def style_filter_any_variant(
        variant_id: str,
        description: str,
        filters: list[dict[str, Any]],
    ) -> VariantDefinition:
        spec = copy.deepcopy(base_spec)
        spec["rolling_research_style_filter_any"] = copy.deepcopy(filters)
        return VariantDefinition(
            "E", variant_id, description, spec, 1, True,
            "以信号日已知状态的并集限定冰点或修复阶段，保持E的修复风格",
        )

    def risk_gate_variant(
        variant_id: str,
        description: str,
        *,
        exclusions: Mapping[str, Sequence[str]],
    ) -> VariantDefinition:
        """在每日第一名确定后执行无回补风险门禁，避免偷偷换成第二名。"""

        spec = copy.deepcopy(base_spec)
        gate = spec.setdefault("entry_gate", {})
        excluded_values = gate.setdefault("exclude_values", {})
        for column, values in exclusions.items():
            current_values = excluded_values.setdefault(str(column), [])
            for value in values:
                if str(value) not in current_values:
                    current_values.append(str(value))
        gate["apply_after_daily_first_pick"] = True
        gate["fallback_to_second_candidate"] = False
        return VariantDefinition(
            "E", variant_id, description, spec, len(exclusions), True,
            "冻结E的R1并集、排序与退出；第一名命中信号日尾部风险后直接空仓且不回补",
        )

    gate_spec = copy.deepcopy(base_spec)
    excluded = gate_spec["entry_gate"]["exclude_values"].setdefault(
        "first_time_detail_bucket", []
    )
    if "after_1430" not in excluded:
        excluded.append("after_1430")

    candidates = [
        style_filter_any_variant(
            "E_ANY_REPAIR_STATE",
            "所属分段ice_point，或全市场处于ice_point/retreat/warming",
            [
                {"column": "segment_emotion_state", "values": ["ice_point"]},
                {
                    "column": "market_emotion_state",
                    "values": ["ice_point", "retreat", "warming"],
                },
            ],
        ),
        style_filter_variant(
            "E_MARKET_REPAIR_STATES",
            "只允许全市场ice_point/retreat/warming修复状态",
            "market_emotion_state",
            ["ice_point", "retreat", "warming"],
        ),
        style_filter_variant(
            "E_SEGMENT_ICE_POINT_ONLY",
            "只允许所属分段处于ice_point",
            "segment_emotion_state",
            ["ice_point"],
        ),
        ranking_variant("E_RANK_TURNOVER", "最终排序只用换手率高优先", ["turnover_rate"], [False], [1.0]),
        ranking_variant("E_RANK_AMOUNT_RATIO", "最终排序只用成交额倍率低优先", ["amount_ratio_1d"], [True], [1.0]),
        ranking_variant(
            "E_RANK_TURNOVER_2_AMOUNT_1",
            "换手率与成交额倍率按2:1加权",
            ["turnover_rate", "amount_ratio_1d"],
            [False, True],
            [2.0, 1.0],
        ),
        ranking_variant(
            "E_RANK_TURNOVER_1_AMOUNT_2",
            "换手率与成交额倍率按1:2加权",
            ["turnover_rate", "amount_ratio_1d"],
            [False, True],
            [1.0, 2.0],
        ),
        ranking_variant(
            "E_RANK_TURNOVER_FILL",
            "换手率与成交概率等权",
            ["turnover_rate", "fill_probability"],
            [False, False],
            [1.0, 1.0],
        ),
        ranking_variant(
            "E_RANK_TURNOVER_FD_LOW",
            "换手率高与封单比例低等权",
            ["turnover_rate", "fd_amount_to_circ_mv"],
            [False, True],
            [1.0, 1.0],
        ),
        VariantDefinition(
            "E",
            "E_GATE_AFTER_1430",
            "保持当前排序，第一名若13:30后首次封板则空仓且不回补",
            gate_spec,
            1,
            False,
            "时间门禁仍覆盖未显式限定冰点/退潮修复的R1分支，只能作为数学诊断候选",
        ),
        risk_gate_variant(
            "E_RISK_EXCLUDE_AMOUNT_RATIO_LT08",
            "每日第一名成交额倍率低于0.8时空仓，不回补第二名",
            exclusions={"amount_ratio_bucket": ("lt_0_8",)},
        ),
        risk_gate_variant(
            "E_RISK_EXCLUDE_LIMIT_UP_GE120",
            "每日第一名处于全市场120只以上涨停过热区时空仓",
            exclusions={"limit_up_count_bucket": ("120_180", "gte_180")},
        ),
        risk_gate_variant(
            "E_RISK_EXCLUDE_SEGMENT_DOWN_TAILS",
            "每日第一名所属分段跌停压力处于1~3只或15只以上时空仓",
            exclusions={
                "segment_limit_down_count_bucket": ("1_3", "gte_15")
            },
        ),
        risk_gate_variant(
            "E_RISK_AMOUNT_LT08_OR_LIMIT_UP_GE120",
            "每日第一名命中成交额倍率过低或全市场过热任一风险时空仓",
            exclusions={
                "amount_ratio_bucket": ("lt_0_8",),
                "limit_up_count_bucket": ("120_180", "gte_180"),
            },
        ),
        risk_gate_variant(
            "E_RISK_EXCLUDE_LEADER_RANK_11_30",
            "每日第一名市场龙头排名在11~30名时空仓，不回补第二名",
            exclusions={"market_leader_rank_bucket": ("rank_11_30",)},
        ),
        risk_gate_variant(
            "E_RISK_EXCLUDE_LIMIT_UP_LT30",
            "每日第一名处于全市场涨停少于30只的弱环境时空仓",
            exclusions={"limit_up_count_bucket": ("lt_30",)},
        ),
        risk_gate_variant(
            "E_RISK_EXCLUDE_AMOUNT_RATIO_3_5",
            "每日第一名成交额倍率处于3~5倍异常放量区时空仓",
            exclusions={"amount_ratio_bucket": ("3_5",)},
        ),
        risk_gate_variant(
            "E_RISK_EXCLUDE_FD_RATIO_2_5PCT",
            "每日第一名封单比例处于2%~5%拥挤区时空仓",
            exclusions={"fd_ratio_bucket": ("2pct_5pct",)},
        ),
        risk_gate_variant(
            "E_RISK_LEADER_11_30_OR_LIMIT_UP_LT30",
            "每日第一名排名11~30名或全市场涨停少于30只时空仓",
            exclusions={
                "market_leader_rank_bucket": ("rank_11_30",),
                "limit_up_count_bucket": ("lt_30",),
            },
        ),
        risk_gate_variant(
            "E_RISK_LEADER_11_30_OR_LIMIT_UP_OUTSIDE_30_120",
            "每日第一名排名11~30名，或全市场涨停数不在30~120只时空仓",
            exclusions={
                "market_leader_rank_bucket": ("rank_11_30",),
                "limit_up_count_bucket": ("lt_30", "120_180", "gte_180"),
            },
        ),
        risk_gate_variant(
            "E_RISK_LEADER_11_30_OR_LIMIT_UP_LT30_OR_120_180",
            "每日第一名排名11~30名，或全市场涨停少于30只/处于120~180只时空仓",
            exclusions={
                "market_leader_rank_bucket": ("rank_11_30",),
                "limit_up_count_bucket": ("lt_30", "120_180"),
            },
        ),
        risk_gate_variant(
            "E_RISK_LEADER_11_30_OR_LIMIT_UP_120_180",
            "每日第一名排名11~30名，或全市场涨停处于120~180只时空仓",
            exclusions={
                "market_leader_rank_bucket": ("rank_11_30",),
                "limit_up_count_bucket": ("120_180",),
            },
        ),
    ]
    return baseline, candidates


def apply_e_research_style_filter(
    pool: pd.DataFrame,
    spec: Mapping[str, Any],
) -> pd.DataFrame:
    """只在滚动研究候选上应用E风格门禁；正式E配置不读取这些临时字段。"""

    single_filter = spec.get("rolling_research_style_filter", {})
    any_filters = spec.get("rolling_research_style_filter_any", [])
    if single_filter and any_filters:
        raise ValueError("E滚动研究不能同时配置单条件与任一条件风格过滤")
    if single_filter:
        any_filters = [single_filter]
    if not any_filters:
        return pool
    if not isinstance(any_filters, list):
        raise ValueError("E滚动研究任一条件风格过滤必须是列表")

    union = pd.Series(False, index=pool.index, dtype="bool")
    for item in any_filters:
        if not isinstance(item, Mapping):
            raise ValueError("E滚动研究风格过滤项必须是对象")
        column = str(item.get("column", ""))
        values = {str(value) for value in item.get("values", [])}
        if not column or not values or column not in pool.columns:
            raise ValueError("E滚动研究风格过滤字段或允许值非法")
        union |= pool[column].astype(str).isin(values)
    return pool.loc[union].copy()


def build_e_picks(pool: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    working = apply_e_research_style_filter(pool, spec)
    universe = build_r1_universe_from_pool(working, spec, audit_readiness=True)
    return select_e_daily_picks(universe, spec)


class StaticOutcomeCache:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str, int, str], Any] = {}

    def get(self, signal_date: str, ts_code: str, hold: int, name: str) -> Any:
        key = (str(signal_date), str(ts_code), int(hold), str(name))
        if key not in self._values:
            self._values[key] = trade_return_details(
                key[0], key[1], key[2], name=key[3]
            )
        return self._values[key]


def static_plan_outcomes(
    picks: pd.DataFrame,
    *,
    leg: str,
    cutoff: str,
    cache: StaticOutcomeCache,
    e_spec: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """构造计划、开仓与已实现结果三状态；更新节点后的收益完全不可见。"""

    rows: list[dict[str, Any]] = []
    if picks.empty:
        return pd.DataFrame(columns=["signal_date", "status", "ts_code"])
    for raw in picks.sort_values(["trade_date", "ts_code"]).to_dict("records"):
        signal_date = str(raw["trade_date"])
        code = str(raw["ts_code"])
        name = str(raw.get("name", ""))
        if leg == "A":
            hold, exit_rule = 2, "FIXED_T2_CLOSE"
        elif leg == "C":
            hold, exit_rule = 3, "FIXED_T3_CLOSE"
        elif leg == "E" and e_spec is not None:
            exit_rule = str(raw["exit_rule"])
            hold = resolve_exit_offset(e_spec, exit_rule)
        else:
            raise ValueError(f"静态计划腿或退出规则非法：{leg}")

        entry = fixed_open_entry_details(signal_date, code, name=name)
        base = {
            "signal_date": signal_date,
            "strategy_leg": leg,
            "ts_code": code,
            "name": name,
            "buy_date": entry.buy_date,
            "exit_rule": exit_rule,
            "hold_offset": hold,
            "matched_condition_profile_ids": str(
                raw.get("matched_condition_profile_ids", "")
            ),
        }
        if not entry.buy_date or entry.buy_date > cutoff:
            continue
        if entry.status != "OK":
            rows.append(
                {
                    **base,
                    "status": entry.status,
                    "exit_date": "",
                    "account_return": np.nan,
                    "entry_filled": False,
                    "position_opened": False,
                    "outcome_observable": False,
                    "position_open_until": "",
                }
            )
            continue

        signal_index = DIDX.get(signal_date, -1)
        intended_exit = (
            DATES[signal_index + hold]
            if signal_index >= 0 and signal_index + hold < len(DATES)
            else ""
        )
        if not intended_exit or intended_exit > cutoff:
            rows.append(
                {
                    **base,
                    "status": "OUTCOME_NOT_OBSERVABLE_AT_UPDATE",
                    "exit_date": "",
                    "account_return": np.nan,
                    "entry_filled": True,
                    "position_opened": True,
                    "outcome_observable": False,
                    "position_open_until": cutoff,
                }
            )
            continue

        execution = cache.get(signal_date, code, hold, name)
        exit_date = str(execution.exit_date or "")
        observable = (
            execution.status == "OK"
            and bool(exit_date)
            and exit_date <= cutoff
            and execution.stock_return is not None
        )
        if observable:
            value = account_return(execution.stock_return, exit_date)
            rows.append(
                {
                    **base,
                    "status": "OK",
                    "exit_date": exit_date,
                    "stock_return_before_fees": execution.stock_return,
                    "account_return": value,
                    "entry_filled": True,
                    "position_opened": True,
                    "outcome_observable": True,
                    "position_open_until": exit_date,
                }
            )
        else:
            rows.append(
                {
                    **base,
                    "status": (
                        "OUTCOME_NOT_OBSERVABLE_AT_UPDATE"
                        if exit_date > cutoff or execution.status in {"NO_CALENDAR", "SELL_UNRESOLVED"}
                        else str(execution.status)
                    ),
                    "exit_date": exit_date if exit_date <= cutoff else "",
                    "stock_return_before_fees": np.nan,
                    "account_return": np.nan,
                    "entry_filled": True,
                    "position_opened": True,
                    "outcome_observable": False,
                    "position_open_until": exit_date if exit_date and exit_date <= cutoff else cutoff,
                }
            )
    return pd.DataFrame(rows).sort_values("signal_date").reset_index(drop=True)


def plan_signature(frame: pd.DataFrame) -> str:
    if frame.empty:
        return hashlib.sha256(b"").hexdigest()
    columns = [
        "signal_date", "action_date", "buy_date", "ts_code", "exit_rule", "status",
        "entry_filled", "position_opened",
    ]
    normalized = frame.copy()
    for column in columns:
        if column not in normalized.columns:
            normalized[column] = ""
    text = normalized[columns].fillna("").astype(str).sort_values(
        ["signal_date", "ts_code"]
    ).to_csv(index=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def d_variants(release: Mapping[str, Any]) -> tuple[VariantDefinition, list[VariantDefinition]]:
    profiles = copy.deepcopy(list(release["profiles"]))
    baseline = VariantDefinition(
        "D", "D_CURRENT", "当前正式D因子发布", profiles, 0, False,
        "冻结正式D基线仅用于比较；现有因子未显式限定盘中强势市场广度",
    )
    current = copy.deepcopy(profiles[0])

    def widened(
        variant_id: str,
        description: str,
        column: str,
        value: str,
        *,
        replace: bool = False,
    ) -> VariantDefinition:
        changed = copy.deepcopy(current)
        changed["profile_id"] = variant_id + "_ALT"
        changed["conditions"][column] = value
        payload = [changed] if replace else [copy.deepcopy(current), changed]
        return VariantDefinition(
            "D", variant_id, description, payload, 1, False,
            "保持盘中回封形态但未显式限定强势市场，只能作为数学诊断候选",
        )

    def strong_market_values(
        variant_id: str,
        description: str,
        column: str,
        values: tuple[str, ...],
    ) -> VariantDefinition:
        payload: list[dict[str, Any]] = []
        for position, value in enumerate(values, 1):
            changed = copy.deepcopy(current)
            changed["profile_id"] = f"{variant_id}_{position:02d}"
            changed["conditions"][column] = value
            payload.append(changed)
        return VariantDefinition(
            "D", variant_id, description, payload, 1, True,
            "使用信号时已经可见的盘中市场质量或广度过滤弱势环境，保持D首板炸板回封风格",
        )

    def combined_strong_market_values(
        variant_id: str,
        description: str,
        first_column: str,
        first_values: tuple[str, ...],
        second_column: str,
        second_values: tuple[str, ...],
    ) -> VariantDefinition:
        payload: list[dict[str, Any]] = []
        for first_value in first_values:
            for second_value in second_values:
                changed = copy.deepcopy(current)
                changed["profile_id"] = f"{variant_id}_{len(payload) + 1:02d}"
                changed["conditions"][first_column] = first_value
                changed["conditions"][second_column] = second_value
                payload.append(changed)
        return VariantDefinition(
            "D", variant_id, description, payload, 2, True,
            "同时使用信号时盘中炸板质量与市场广度确认强势环境，保持D首板炸板回封风格",
        )

    candidates = [
        strong_market_values(
            "D_STRONG_BREAK_LT75",
            "信号时全市场累计炸板事件率低于75%",
            "market_break_rate_bucket",
            ("LT25PCT", "25_50PCT", "50_75PCT"),
        ),
        strong_market_values(
            "D_STRONG_BREAK_LT50",
            "信号时全市场累计炸板事件率低于50%",
            "market_break_rate_bucket",
            ("LT25PCT", "25_50PCT"),
        ),
        combined_strong_market_values(
            "D_STRONG_BREAK_LT75_ACTIVE_GE20",
            "累计炸板事件率低于75%，且信号时仍封首板不少于20只",
            "market_break_rate_bucket",
            ("LT25PCT", "25_50PCT", "50_75PCT"),
            "market_active_count_bucket",
            ("20_40", "41_70", "71_100", "GE101"),
        ),
        combined_strong_market_values(
            "D_STRONG_BREAK_LT75_TOUCH_GE40",
            "累计炸板事件率低于75%，且信号时累计首板触板不少于40只",
            "market_break_rate_bucket",
            ("LT25PCT", "25_50PCT", "50_75PCT"),
            "market_touch_count_bucket",
            ("40_70", "71_100", "101_150", "GE151"),
        ),
        strong_market_values(
            "D_STRONG_ACTIVE_GE20",
            "信号时全市场仍封住的首板不少于20只",
            "market_active_count_bucket",
            ("20_40", "41_70", "71_100", "GE101"),
        ),
        strong_market_values(
            "D_STRONG_TOUCH_GE40",
            "信号时全市场累计首板触板不少于40只",
            "market_touch_count_bucket",
            ("40_70", "71_100", "101_150", "GE151"),
        ),
        strong_market_values(
            "D_QUALITY_TOUCH_LT40",
            "信号时累计首板触板少于40只，避开40~70只历史亏损拥挤区",
            "market_touch_count_bucket",
            ("LT40",),
        ),
        strong_market_values(
            "D_QUALITY_BREAK_25_75",
            "信号时累计炸板事件率保持25%~75%，排除过低价格发现和极端炸板",
            "market_break_rate_bucket",
            ("25_50PCT", "50_75PCT"),
        ),
        combined_strong_market_values(
            "D_QUALITY_BREAK_25_75_TOUCH_LT40",
            "累计炸板率25%~75%且累计首板触板少于40只",
            "market_break_rate_bucket",
            ("25_50PCT", "50_75PCT"),
            "market_touch_count_bucket",
            ("LT40",),
        ),
        widened(
            "D_TIME_ADJACENT",
            "当前早盘回封时段相邻加入10:01~10:30",
            "reseal_time_bucket",
            "1001_1030",
        ),
        widened(
            "D_DEPTH_ADJACENT",
            "当前浅炸深度相邻加入0.2%~0.5%",
            "break_close_depth_bucket",
            "0_2_0_5PCT",
        ),
        widened(
            "D_SEGMENT_ADJACENT",
            "当前成长板范围相邻加入沪深主板",
            "segment_bucket",
            "MAIN_BOARD",
        ),
        widened(
            "D_TIME_SHIFT_1001_1030",
            "仅将回封时段平移到10:01~10:30",
            "reseal_time_bucket",
            "1001_1030",
            replace=True,
        ),
    ]
    return baseline, candidates


def _d_selection_sorted(events: pd.DataFrame) -> pd.DataFrame:
    ranked = events.copy()
    ranked["_open2_priority"] = (
        pd.to_numeric(ranked["open_times_at_signal"], errors="coerce").eq(2).astype(int)
    )
    return ranked.sort_values(
        ["trade_date", "signal_hhmm", "_open2_priority", "ts_code", "event_id"],
        ascending=[True, True, False, True, True],
    )


def build_d_plans(
    events: pd.DataFrame,
    profiles: Sequence[Mapping[str, Any]],
    *,
    allowed_action_dates: set[str],
    cutoff: str,
) -> pd.DataFrame:
    factorized = add_factor_values(events)
    union = pd.Series(False, index=factorized.index)
    for profile in profiles:
        union |= profile_mask(factorized, profile["conditions"])
    selected = factorized.loc[
        union & factorized["trade_date"].astype(str).isin(allowed_action_dates)
    ].copy()
    if selected.empty:
        return pd.DataFrame(columns=["signal_date", "status", "ts_code"])
    selected = _d_selection_sorted(selected).drop_duplicates("trade_date", keep="first")
    rows: list[dict[str, Any]] = []
    for raw in selected.to_dict("records"):
        queue_confirmed = str(raw.get("queue_price_confirmed", "")).lower() in {
            "true", "1", "yes"
        }
        exit_date = str(raw.get("exit_date", "") or "").replace(".0", "")
        execution_status = str(raw.get("execution_status", ""))
        value = pd.to_numeric(raw.get("account_return"), errors="coerce")
        observable = bool(
            queue_confirmed
            and execution_status == "OK"
            and exit_date
            and exit_date <= cutoff
            and not pd.isna(value)
        )
        if not queue_confirmed:
            status = "QUEUE_UNCONFIRMED_NO_DEPTH"
        elif observable:
            status = "OK"
        else:
            status = "OUTCOME_NOT_OBSERVABLE_AT_UPDATE"
        rows.append(
            {
                "signal_date": str(raw["trade_date"]),
                "strategy_leg": "D",
                "ts_code": str(raw["ts_code"]),
                "name": str(raw.get("name", "")),
                "event_id": int(raw["event_id"]),
                "signal_hhmm": int(raw["signal_hhmm"]),
                "status": status,
                "exit_date": exit_date if exit_date <= cutoff else "",
                "account_return": float(value) if observable else np.nan,
                "entry_filled": bool(queue_confirmed),
                "position_opened": bool(queue_confirmed),
                "outcome_observable": observable,
                "position_open_until": (
                    exit_date if observable or (exit_date and exit_date <= cutoff) else cutoff
                ) if queue_confirmed else "",
            }
        )
    return pd.DataFrame(rows).sort_values("signal_date").reset_index(drop=True)


def variant_catalog_payload(variants: Iterable[VariantDefinition]) -> list[dict[str, Any]]:
    return [
        {
            "strategy_leg": item.strategy_leg,
            "variant_id": item.variant_id,
            "description": item.description,
            "changed_axis_count": item.changed_axis_count,
            "style_gate_passed": item.style_gate_passed,
            "style_gate_reason": item.style_gate_reason,
            "payload_sha256": hashlib.sha256(
                json.dumps(item.payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
        }
        for item in variants
    ]
