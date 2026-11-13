"""实盘逐笔对账的组装：影子账回放、停手日、M候选单参照 → 对账结论。

收盘流水线⑬（``scripts/update_equity_curve_stop.py``）和QMT外的每日检查
（``qmt_external_checks.py``）都从这里进，保证两处口径一致；判定规则本身在
``src.review_triggers.alignment_triggers``。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from src.equity_curve_stop import allowed_flags, gated_replay
from src.review_triggers import ReviewTrigger, alignment_triggers
from src.strategy_m_reference import MReference


def plan_codes_by_date(legs: Mapping[str, pd.DataFrame], start: str, end: str) -> dict[str, set[str]]:
    """每个行动日A/C/E各腿计划过的全部股票（不管最后选了哪条腿、成没成交）。"""

    result: dict[str, set[str]] = {}
    for frame in legs.values():
        if frame is None or frame.empty or "action_date" not in frame.columns or "ts_code" not in frame.columns:
            continue
        dates = frame["action_date"].astype(str)
        rows = frame[dates.between(str(start), str(end))]
        for date, code in zip(rows["action_date"].astype(str), rows["ts_code"].astype(str)):
            result.setdefault(date, set()).add(code)
    return result


def run_alignment(
    shadow: Mapping[str, Any],
    live: pd.DataFrame,
    *,
    ma_window: int,
    lag: int,
    start: str,
    end: str,
    ace_m_start: str = "",
    raw_dir: Path | str | None = None,
) -> list[ReviewTrigger]:
    """把实盘真实买入与正式规则逐笔对照，返回对不上的每一条。

    shadow：``build_shadow_replay`` 的结果；live：实盘成交汇总里真实买入的行。
    ace_m_start：ACE+M生效的第一个行动日（配置 review_triggers.ace_m_start_date），
    留空则全程按ACE+M之前的口径；raw_dir：原始数据目录，用来重算M候选单。
    """

    nav = shadow["nav"]
    flags = allowed_flags(nav.to_numpy(), ma_window=ma_window, lag=lag)
    rule_start = str(ace_m_start or "")
    return alignment_triggers(
        gated_replay(shadow, ma_window=ma_window, lag=lag, ungated_from=rule_start),
        live,
        stop_dates={str(date) for date, allowed in zip(nav.index, flags) if not allowed},
        start=str(start),
        end=str(end),
        ace_m_start=rule_start,
        m_reference=MReference(raw_dir) if rule_start and raw_dir is not None else None,
        ace_plan_codes=plan_codes_by_date(shadow["legs"], rule_start, str(end)) if rule_start else None,
    )
