"""实盘逐笔对账的组装：影子账回放、停手日、M候选单参照 → 对账结论。

收盘流水线⑬（``scripts/update_equity_curve_stop.py``）和QMT外的每日检查
（``qmt_external_checks.py``）都从这里进，保证两处口径一致；判定规则本身在
``src.review_triggers.alignment_triggers``。
"""
from __future__ import annotations

import datetime as dt
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


def _alignment_inputs(shadow: Mapping[str, Any], *, transition: str, ma_window: int, lag: int):
    """仅对账按生效日拼计划和停手，保持跨切换日资金占用连续；不改正式新影子净值。"""

    nav = shadow["nav"]
    flags = allowed_flags(nav.to_numpy(), ma_window=ma_window, lag=lag)
    stops = {str(day) for day, allowed in zip(nav.index, flags) if not allowed}
    if not transition:
        return shadow, stops, None
    try:
        if dt.datetime.strptime(transition, "%Y%m%d").strftime("%Y%m%d") != transition:
            raise ValueError("日期必须为YYYYMMDD")
    except ValueError as exc:
        raise ValueError("ACE+M+L对账生效日无效：%s" % transition) from exc
    dates = [str(day) for day in shadow["action_dates"]]
    if not any(day < transition for day in dates):
        return shadow, stops, None
    legacy = shadow.get("legacy_alignment_shadow")
    if legacy is None:
        raise ValueError("历史对账缺少旧E计划和停手参照，请用include_legacy_alignment=True构建影子账")
    old_dates = [str(day) for day in legacy["action_dates"]]
    if (dates != sorted(set(dates)) or dates != old_dates or list(nav.index.astype(str)) != dates
            or list(legacy["nav"].index.astype(str)) != dates):
        raise ValueError("历史对账新旧影子账日期轴不一致，拒绝混用")
    if set(shadow["legs"]) != set(legacy["legs"]) or "E" not in legacy["legs"]:
        raise ValueError("历史对账新旧策略腿不完整，拒绝混用")
    if dict(shadow["execution"]) != dict(legacy["execution"]):
        raise ValueError("历史对账新旧执行参数不一致，拒绝混用")
    old_flags = allowed_flags(legacy["nav"].to_numpy(), ma_window=ma_window, lag=lag)
    allowed = {day for day, old_ok, new_ok in zip(dates, old_flags, flags)
               if (old_ok if day < transition else new_ok)}
    legs = {}
    for leg in dict.fromkeys([*legacy["legs"], *shadow["legs"]]):
        pieces = []
        for source, before in ((legacy, True), (shadow, False)):
            frame = source["legs"].get(leg)
            if frame is not None and not frame.empty:
                if "action_date" not in frame.columns:
                    raise ValueError("历史对账%s计划缺少行动日" % leg)
                mask = frame["action_date"].astype(str).lt(transition)
                pieces.append(frame.loc[mask if before else ~mask])
        legs[leg] = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    return {**shadow, "legs": legs}, set(dates) - allowed, allowed


def _m_reference(raw_dir: Path | str | None, *, enabled: bool, sort_by: str, transition: str):
    if not enabled or raw_dir is None:
        return None
    current = MReference(raw_dir, sort_by=sort_by)
    if not transition or sort_by == "amount":
        return current
    legacy = MReference(raw_dir, sort_by="amount")

    def by_action_date(day):
        return (legacy if str(day) < transition else current)(day)

    return by_action_date


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
    m_sort_by: str = "amount",
    ace_m_l_start: str = "",
    project_root: Path | str | None = None,
) -> list[ReviewTrigger]:
    """把实盘真实买入与正式规则逐笔对照，返回对不上的每一条。

    shadow：``build_shadow_replay`` 的结果；live：实盘成交汇总里真实买入的行。
    ace_m_start：ACE+M生效的第一个行动日（配置 review_triggers.ace_m_start_date），
    留空则全程按ACE+M之前的口径；raw_dir：原始数据目录，用来重算M候选单。
    ace_m_l_start：ACE+M+L生效的第一个行动日（配置 review_triggers.ace_m_l_start_date，2026-11-26），
    留空不做L的检查；project_root：读L历史首选缓存（reports/strategy_l/l_picks_cache.json）用。
    有旧日期时shadow须附带legacy_alignment_shadow；旧M排序/旧E计划与停手只用于生效日前的实盘对账。
    """

    rule_start = str(ace_m_start or "")
    transition = str(ace_m_l_start or "")
    aligned_shadow, stops, allowed = _alignment_inputs(
        shadow, transition=transition, ma_window=ma_window, lag=lag,
    )
    replay_options = {"allowed_dates_override": allowed} if allowed is not None else {}
    return alignment_triggers(
        gated_replay(aligned_shadow, ma_window=ma_window, lag=lag, ungated_from=rule_start, **replay_options),
        live,
        stop_dates=stops,
        start=str(start),
        end=str(end),
        ace_m_start=rule_start,
        m_reference=_m_reference(raw_dir, enabled=bool(rule_start), sort_by=m_sort_by, transition=transition),
        ace_plan_codes=plan_codes_by_date(aligned_shadow["legs"], rule_start, str(end)) if rule_start else None,
        ace_m_l_start=transition,
        l_reference=_l_reference(ace_m_l_start, project_root, raw_dir),
    )


def _l_reference(ace_m_l_start: str, project_root: Path | str | None, raw_dir: Path | str | None):
    if not ace_m_l_start or project_root is None:
        return None
    from src.strategy_l_reference import LReference

    return LReference(project_root, raw_dir)
