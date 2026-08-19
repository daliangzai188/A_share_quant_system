"""策略N唯一规则源（信号日无前视、固定T+2退出）。

N只在D/A/M/E/C均未占用资金后参与组合选择，正式腿序为
``D > A > M > E > C > N``。本模块只做无副作用选股计算；历史认证、
每日信号和实盘组合必须共同调用这里，禁止各自复制条件。

锁定规则：
1. 第一分支：``segment_limit_max_height_bucket == "1"``且退潮状态为
   ``retreat_weak/retreat_2day``，按首次涨停分钟、流通市值、代码升序取第一名；
2. 仅在第一分支当日无候选时，第二分支要求全市场连板数``3_8``且市场情绪
   ``mixed``，按成交额降序、流通市值升序、代码升序取第一名；
3. 两分支都通过成交可靠性、异常封单、策略兼容和成交概率>=60%的共同底线；
4. 第一名次日不可买时当日放弃，不回补第二名；T+1开盘买、T+2收盘卖。
"""
from __future__ import annotations

from typing import Any, Mapping

import pandas as pd


N_VERSION = "N_two_branch_retreat_plus_mixed_amount_v2"

DEFAULT_SPEC: dict[str, Any] = {
    "enabled": False,
    "live_order_enabled": False,
    "height_column": "segment_limit_max_height_bucket",
    "height_values": ["1"],
    "retreat_column": "segment_retreat_state_bucket",
    "retreat_values": ["retreat_weak", "retreat_2day"],
    "rank_columns": ["first_time_minutes", "circ_mv", "ts_code"],
    "rank_ascending": [True, True, True],
    "supplement_enabled": False,
    "supplement_filter_columns": [
        "market_chain_count_bucket",
        "market_emotion_state_bucket",
    ],
    "supplement_filter_values": [["3_8"], ["mixed"]],
    "supplement_rank_columns": ["amount", "circ_mv", "ts_code"],
    "supplement_rank_ascending": [False, True, True],
    "min_fill_probability": 0.60,
    "exit_hold_offset": 2,
    "position_pct": 0.825,
    "fallback_to_second_candidate": False,
}

FORBIDDEN_SELECTION_TOKENS = (
    "next_", "future_", "exit_", "return", "profit",
    "d1_", "d2_", "d3_", "d4_", "d5_",
)
FORBIDDEN_SELECTION_FIELDS = {
    "buy_executed", "sell_executed", "scenario_executed", "net_return",
    "gross_return", "exit_trade_date", "exit_close", "is_win", "equity",
}


def load_n_spec(config: Mapping[str, Any]) -> dict[str, Any]:
    """从完整config.json或strategy_n段读取并严格校验锁定规则。"""

    raw = config.get("strategy_n") if isinstance(config.get("strategy_n"), Mapping) else config
    spec = dict(DEFAULT_SPEC)
    for key in DEFAULT_SPEC:
        if key in raw:
            spec[key] = raw[key]

    columns = [
        str(spec["height_column"]),
        str(spec["retreat_column"]),
        *[str(value) for value in spec["rank_columns"]],
    ]
    if bool(spec.get("supplement_enabled", False)):
        columns.extend(str(value) for value in spec["supplement_filter_columns"])
        columns.extend(str(value) for value in spec["supplement_rank_columns"])
    forbidden = sorted(
        column for column in columns
        if column in FORBIDDEN_SELECTION_FIELDS
        or any(token in column.lower() for token in FORBIDDEN_SELECTION_TOKENS)
    )
    if forbidden:
        raise ValueError(f"N规则含未来字段：{forbidden}")
    if len(spec["rank_columns"]) != len(spec["rank_ascending"]):
        raise ValueError("N排序字段与方向数量不一致")
    if len(spec["supplement_filter_columns"]) != len(spec["supplement_filter_values"]):
        raise ValueError("N补充分支筛选字段与取值数量不一致")
    if len(spec["supplement_rank_columns"]) != len(spec["supplement_rank_ascending"]):
        raise ValueError("N补充分支排序字段与方向数量不一致")
    if bool(spec.get("fallback_to_second_candidate", False)):
        raise ValueError("N禁止第一名不可买后回补第二名")
    if int(spec.get("exit_hold_offset", 0)) != 2:
        raise ValueError("N当前锁定为T+2收盘退出")
    return spec


def required_signal_fields(spec: Mapping[str, Any]) -> set[str]:
    fields = {
        "trade_date", "ts_code", "name", "limit_close", "market_segment",
        "allow_buy_reliable", "is_fill_score_reliable", "is_fd_amount_abnormal",
        "strategy_compatible", "fill_probability", str(spec["height_column"]),
        str(spec["retreat_column"]), *[str(value) for value in spec["rank_columns"]],
    }
    if bool(spec.get("supplement_enabled", False)):
        fields.update(str(value) for value in spec["supplement_filter_columns"])
        fields.update(str(value) for value in spec["supplement_rank_columns"])
    return fields


def _bool_series(frame: pd.DataFrame, column: str, default: bool) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype="bool")
    return frame[column].fillna(default).astype(str).str.strip().str.lower().isin(
        {"1", "true", "yes"}
    )


def audit_signal_data_readiness(pool: pd.DataFrame, spec: Mapping[str, Any]) -> list[str]:
    broken: list[str] = []
    for column in sorted(required_signal_fields(spec)):
        if column not in pool.columns:
            broken.append(f"{column}(字段缺失)")
            continue
        values = pool[column]
        text = values.fillna("").astype(str).str.strip()
        if bool((values.isna() | text.isin({"", "nan", "None", "<NA>"})).all()):
            broken.append(f"{column}(整列不可用)")
    return broken


def apply_n_base_filters(pool: pd.DataFrame, spec: Mapping[str, Any]) -> pd.DataFrame:
    """复现研究时锁定的五道可执行性底线。"""

    result = pool.copy()
    result = result[_bool_series(result, "allow_buy_reliable", False)]
    result = result[_bool_series(result, "is_fill_score_reliable", False)]
    result = result[~_bool_series(result, "is_fd_amount_abnormal", True)]
    result = result[_bool_series(result, "strategy_compatible", False)]
    fill = pd.to_numeric(result["fill_probability"], errors="coerce")
    result = result[fill.ge(float(spec.get("min_fill_probability", 0.60)))]
    return result.copy()


def select_n_daily_picks(
    pool: pd.DataFrame,
    spec: Mapping[str, Any],
    *,
    signal_date: str | None = None,
    audit_readiness: bool = True,
) -> pd.DataFrame:
    """从bucket池选出每日唯一N候选；只使用信号日字段。"""

    rows = pool.copy()
    if signal_date is not None:
        rows = rows[rows["trade_date"].astype(str).eq(str(signal_date))].copy()
    if rows.empty:
        return rows
    if audit_readiness:
        broken = audit_signal_data_readiness(rows, spec)
        if broken:
            raise RuntimeError("N信号日关键字段不可用：" + "、".join(broken))

    rows = apply_n_base_filters(rows, spec)
    if rows.empty:
        return rows

    def rank_branch(
        branch_rows: pd.DataFrame,
        *,
        rank_columns: list[str],
        rank_ascending: list[bool],
        branch: str,
        rule_id: str,
    ) -> pd.DataFrame:
        if branch_rows.empty:
            return branch_rows
        ranked = branch_rows.copy()
        for column in rank_columns:
            if column != "ts_code":
                ranked[column] = pd.to_numeric(ranked[column], errors="coerce")
        ranked = ranked.dropna(
            subset=[column for column in rank_columns if column != "ts_code"]
        )
        if ranked.empty:
            return ranked
        group_prefix = ["trade_date"] if signal_date is None else []
        ranked = ranked.sort_values(
            group_prefix + rank_columns,
            ascending=([True] if group_prefix else []) + rank_ascending,
            na_position="last",
        )
        ranked = (
            ranked.groupby("trade_date", as_index=False).head(1)
            if signal_date is None
            else ranked.head(1)
        )
        ranked = ranked.copy()
        ranked["n_branch"] = branch
        ranked["n_rule_id"] = rule_id
        return ranked

    height_values = {str(value) for value in spec["height_values"]}
    retreat_values = {str(value) for value in spec["retreat_values"]}
    current_rows = rows[
        rows[str(spec["height_column"])].astype(str).isin(height_values)
        & rows[str(spec["retreat_column"])].astype(str).isin(retreat_values)
    ].copy()
    current = rank_branch(
        current_rows,
        rank_columns=[str(value) for value in spec["rank_columns"]],
        rank_ascending=[bool(value) for value in spec["rank_ascending"]],
        branch="CURRENT",
        rule_id="LOW_HEIGHT_RETREAT_FIRST_TIME",
    )

    if not bool(spec.get("supplement_enabled", False)):
        return current.reset_index(drop=True)

    supplement_rows = rows.copy()
    for column, accepted in zip(
        spec["supplement_filter_columns"],
        spec["supplement_filter_values"],
    ):
        values = {str(value) for value in accepted}
        supplement_rows = supplement_rows[
            supplement_rows[str(column)].astype(str).isin(values)
        ]
    supplement = rank_branch(
        supplement_rows,
        rank_columns=[str(value) for value in spec["supplement_rank_columns"]],
        rank_ascending=[bool(value) for value in spec["supplement_rank_ascending"]],
        branch="SUPPLEMENT",
        rule_id="CHAIN_3_8_MIXED_AMOUNT_DESC",
    )

    # 当前N永远是第一分支；补充分支只能填当前分支完全无候选的日期。
    combined = pd.concat([current, supplement], ignore_index=True)
    if combined.empty:
        return combined
    combined["_branch_priority"] = combined["n_branch"].map(
        {"CURRENT": 0, "SUPPLEMENT": 1}
    ).fillna(9)
    combined = combined.sort_values(
        ["trade_date", "_branch_priority"], ascending=[True, True]
    ).drop_duplicates("trade_date", keep="first")
    return combined.drop(columns=["_branch_priority"]).reset_index(drop=True)


def resolve_exit_offset(spec: Mapping[str, Any]) -> int:
    return int(spec.get("exit_hold_offset", 2))
