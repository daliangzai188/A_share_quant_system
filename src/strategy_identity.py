from __future__ import annotations

"""全系统策略身份的唯一规则源。

正式策略腿只允许写 ``E``。历史账本中的 ``E2`` 仅作为只读兼容别名，读取时
立即归一为 ``E``；三套研究逻辑通过 ``strategy_variant`` 区分，禁止再借用腿名
表达不同规则。
"""

from typing import Any, Mapping

import pandas as pd


STRATEGY_E_LEG = "E"
ACTIVE_E_VARIANT = "E_CURRENT"
E_VARIANTS = frozenset({"E_JULY", "E_R1", "E_CURRENT"})
LEGACY_E_LEG_ALIASES = frozenset({"E2"})
CURRENT_E_START_DATE = "20260803"


def normalize_strategy_leg(value: Any) -> str:
    """把历史 E2 身份归一为 E，其他策略腿保持大写。"""

    leg = str(value or "").strip().upper()
    if leg in LEGACY_E_LEG_ALIASES:
        return STRATEGY_E_LEG
    if leg == "E2_STATUS":
        return "E_STATUS"
    return leg


def normalize_e_variant(value: Any, *, signal_date: Any = "") -> str:
    """返回 E 的明确规则版本；旧实盘记录按 2026-08-03 发布日切分。"""

    variant = str(value or "").strip().upper()
    if variant:
        if variant not in E_VARIANTS:
            raise ValueError(f"未知的E策略版本：{variant}")
        return variant
    date_text = "".join(ch for ch in str(signal_date or "") if ch.isdigit())[:8]
    if date_text and date_text < CURRENT_E_START_DATE:
        return "E_JULY"
    return ACTIVE_E_VARIANT


def normalize_strategy_record(
    record: Mapping[str, Any],
    *,
    default_e_variant: str | None = None,
) -> dict[str, Any]:
    """复制并归一单条账本/信号记录，不原地篡改调用方数据。"""

    result = dict(record)
    legacy_leg = str(result.get("strategy_leg", "") or "").strip().upper()
    leg = normalize_strategy_leg(legacy_leg)
    result["strategy_leg"] = leg
    if leg == STRATEGY_E_LEG:
        result["strategy_family"] = STRATEGY_E_LEG
        if legacy_leg and legacy_leg != STRATEGY_E_LEG:
            result.setdefault("legacy_strategy_leg", legacy_leg)
        signal_date = result.get("signal_date") or result.get("trade_date") or result.get("buy_date")
        result["strategy_variant"] = normalize_e_variant(
            default_e_variant or result.get("strategy_variant"),
            signal_date=signal_date,
        )
    return result


def normalize_strategy_frame(
    frame: pd.DataFrame,
    *,
    default_e_variant: str | None = None,
) -> pd.DataFrame:
    """归一表格中的策略腿，并为 E 样本补齐 family/variant 身份列。"""

    result = frame.copy()
    if "strategy_leg" not in result.columns:
        return result
    legacy = result["strategy_leg"].fillna("").astype(str).str.strip().str.upper()
    result["strategy_leg"] = legacy.map(normalize_strategy_leg)
    e_mask = result["strategy_leg"].eq(STRATEGY_E_LEG)
    if not bool(e_mask.any()):
        return result

    if "strategy_family" not in result.columns:
        result["strategy_family"] = ""
    result.loc[e_mask, "strategy_family"] = STRATEGY_E_LEG
    legacy_mask = e_mask & legacy.ne(STRATEGY_E_LEG)
    if bool(legacy_mask.any()):
        if "legacy_strategy_leg" not in result.columns:
            result["legacy_strategy_leg"] = ""
        result.loc[legacy_mask, "legacy_strategy_leg"] = legacy.loc[legacy_mask]

    if "strategy_variant" not in result.columns:
        result["strategy_variant"] = ""
    date_column = next(
        (column for column in ("signal_date", "trade_date", "buy_date") if column in result.columns),
        None,
    )
    for index in result.index[e_mask]:
        date_value = result.at[index, date_column] if date_column else ""
        explicit = default_e_variant or result.at[index, "strategy_variant"]
        result.at[index, "strategy_variant"] = normalize_e_variant(explicit, signal_date=date_value)
    return result
