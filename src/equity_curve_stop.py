"""方案甲：策略自身影子净值均线停手门禁。

同一份定义供研究、认证和实盘三处调用，禁止在别处另写一份判定逻辑。

1. 影子账：正式A/C/E规则（不含D、从不停手）在严格as-of研究池上做A>C>E
   单账户回放，按action_date记录账户净值。逐笔收益记在开仓日，与认证回放
   完全同源（``replay_action_date_cash_portfolio``）。
2. 行动日序列第i天，取第 ``i - lag`` 天的影子净值 v，与截至该天（含）的
   ``ma_window`` 日简单均值比较；该天之前不足 ``ma_window`` 个交易日时视为允许。
3. v < 均值：该行动日A/C/E/D全部不开新仓；已有持仓照常按原规则退出。
   影子账停手期间照常记账，用于判断何时恢复。

参数（60日均线、滞后6个交易日）是2026-09-19研究时事先写死的，研究结论见
``docs/equity_curve_stop.md``；年度复核也不得调整。
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


SCHEMA_VERSION = 1
METHOD_ID = "shadow_nav_lag6_below_ma60_blocks_new_entries_v1"
DEFAULT_MA_WINDOW = 60
DEFAULT_LAG_TRADING_DAYS = 6
SHADOW_LEGS = ("A", "C", "E")
BLOCKED_LEGS = ("A", "C", "E", "D")


@dataclass(frozen=True)
class EquityCurveStopSettings:
    enabled: bool
    ma_window: int
    lag_trading_days: int
    history_start: str
    decision_path: Path
    shadow_nav_path: Path
    work_dir: Path


@dataclass(frozen=True)
class LiveGateCheck:
    allowed: bool
    reason: str
    source: str
    payload: dict[str, Any]


def load_settings(config: Mapping[str, Any], project_root: Path) -> EquityCurveStopSettings:
    section = dict(config.get("equity_curve_stop", {}) or {})

    def _path(key: str, default: str) -> Path:
        raw = str(section.get(key, "") or default)
        path = Path(raw)
        return path if path.is_absolute() else project_root / path

    work_raw = str(section.get("work_dir", "") or "")
    work_dir = (
        Path(work_raw)
        if work_raw and Path(work_raw).is_absolute()
        else (project_root / work_raw if work_raw else Path(tempfile.gettempdir()) / "a_system_equity_curve_stop")
    )
    return EquityCurveStopSettings(
        enabled=bool(section.get("enabled", False)),
        ma_window=int(section.get("ma_window", DEFAULT_MA_WINDOW)),
        lag_trading_days=int(section.get("lag_trading_days", DEFAULT_LAG_TRADING_DAYS)),
        history_start=str(section.get("history_start", "20190101")),
        decision_path=_path("decision_path", "data/state/equity_curve_stop_decision.json"),
        shadow_nav_path=_path("shadow_nav_path", "reports/equity_curve_stop/shadow_nav_latest.csv"),
        work_dir=work_dir,
    )


def allowed_flags(nav: Sequence[float], *, ma_window: int, lag: int) -> np.ndarray:
    """逐行动日是否允许开新仓；``nav[i]`` 是第i个行动日记账后的影子净值。"""

    values = np.asarray(nav, dtype=float)
    count = len(values)
    moving = pd.Series(values).rolling(ma_window).mean().to_numpy()
    allowed = np.ones(count, dtype=bool)
    lagged = np.arange(count) - int(lag)
    ready = lagged >= int(ma_window)
    allowed[ready] = values[lagged[ready]] >= moving[lagged[ready]]
    return allowed


def allowed_action_dates(
    dates: Sequence[str],
    nav: Sequence[float],
    *,
    ma_window: int,
    lag: int,
) -> set[str]:
    flags = allowed_flags(nav, ma_window=ma_window, lag=lag)
    return {str(date) for date, ok in zip(dates, flags) if ok}


def decision_for_next_action_date(
    dates: Sequence[str],
    nav: Sequence[float],
    next_action_date: str,
    *,
    ma_window: int,
    lag: int,
) -> dict[str, Any]:
    """用截至最新收盘的影子净值，判定紧接着的下一个行动日是否停手。

    与 ``allowed_flags`` 在“把下一个行动日追加到序列末尾”时的结果逐位一致。
    """

    dates = [str(date) for date in dates]
    values = np.asarray(nav, dtype=float)
    if len(dates) != len(values):
        raise ValueError("影子净值日期与数值长度不一致")
    if dates and str(next_action_date) <= dates[-1]:
        raise ValueError("下一个行动日必须晚于影子净值最后一个行动日")
    index = len(dates)
    lagged = index - int(lag)
    payload: dict[str, Any] = {
        "action_date": str(next_action_date),
        "ma_window": int(ma_window),
        "lag_trading_days": int(lag),
        "shadow_last_date": dates[-1] if dates else "",
        "shadow_last_nav": float(values[-1]) if len(values) else None,
    }
    if lagged < int(ma_window):
        payload.update(
            allowed=True,
            lag_date="",
            lag_nav=None,
            lag_ma=None,
            reason="影子净值历史不足均线窗口，按定义允许开仓",
        )
        return payload
    window = values[lagged - int(ma_window) + 1: lagged + 1]
    lag_nav = float(values[lagged])
    lag_ma = float(window.mean())
    allowed = bool(lag_nav >= lag_ma)
    payload.update(
        allowed=allowed,
        lag_date=dates[lagged],
        lag_nav=lag_nav,
        lag_ma=lag_ma,
        reason=(
            f"影子净值{dates[lagged]}={lag_nav:.4f}，不低于其{ma_window}日均线{lag_ma:.4f}，正常开仓"
            if allowed
            else f"影子净值{dates[lagged]}={lag_nav:.4f}，低于其{ma_window}日均线{lag_ma:.4f}，"
            "方案甲停手：A/C/E/D今日都不开新仓"
        ),
    )
    return payload


def filter_plans_to_allowed_dates(
    plans: Mapping[str, pd.DataFrame],
    allowed_dates: Iterable[str],
) -> dict[str, pd.DataFrame]:
    """把停手日的计划剔除；持仓的退出由回放引擎按原退出日照常处理。"""

    allowed = {str(date) for date in allowed_dates}
    result: dict[str, pd.DataFrame] = {}
    for leg, frame in plans.items():
        if frame is None or frame.empty or "action_date" not in frame.columns:
            result[leg] = frame
            continue
        result[leg] = frame[frame["action_date"].astype(str).isin(allowed)].copy()
    return result


def shadow_nav_from_detail(
    detail: pd.DataFrame,
    action_dates: Sequence[str],
    *,
    initial_cash: float,
) -> pd.Series:
    """把回放明细转换为按行动日对齐的影子净值（无记录日沿用前值）。"""

    series = pd.Series(
        detail["equity_after"].astype(float).to_numpy() / float(initial_cash),
        index=detail["action_date"].astype(str).to_numpy(),
    )
    series = series[~series.index.duplicated(keep="last")]
    return series.reindex([str(date) for date in action_dates]).ffill().fillna(1.0)


def build_shadow_nav(
    *,
    project_root: Path,
    feature_path: Path,
    sentiment_path: Path,
    calendar_path: Path,
    start: str,
    end: str,
    work_dir: Path,
) -> pd.Series:
    """在给定严格as-of研究池上计算影子净值（正式A/C/E、不含D、不停手）。"""

    from scripts.optimize_acde_rolling_three_year import build_variant_plan
    from src.acde_monthly_research import (
        _context,
        _execution_kwargs,
        _variant_sets,
        load_monthly_config,
    )
    from src.acde_rolling_candidates import StaticOutcomeCache
    from src.acde_rolling_framework import (
        FIXED_PRIORITY,
        ResearchWindow,
        replay_action_date_cash_portfolio,
    )

    monthly = load_monthly_config(project_root / "config/acde_rolling_optimization.json")
    work_dir.mkdir(parents=True, exist_ok=True)
    empty_d_events = work_dir / "empty_d_events.csv"
    pd.DataFrame(columns=["trade_date", "ts_code"]).to_csv(empty_d_events, index=False)
    # 市场门禁需要每个行动日的前一交易日；若start落在交易日历第一天，则从第二个交易日起回放。
    calendar = pd.read_csv(calendar_path, dtype={"cal_date": str}, low_memory=False)
    opened = sorted(
        calendar[pd.to_numeric(calendar["is_open"], errors="coerce").eq(1)]["cal_date"].astype(str)
    )
    eligible = [date for date in opened[1:] if date >= str(start)]
    if not eligible:
        raise RuntimeError(f"交易日历中没有不早于{start}且有前一交易日的开市日")
    window = ResearchWindow("EQUITY_CURVE_STOP_SHADOW", eligible[0], str(end), "shadow", False)
    context = _context(
        window=window,
        feature_path=feature_path,
        sentiment_path=sentiment_path,
        d_event_path=empty_d_events,
        calendar_path=calendar_path,
        minimum_limit_up_count=int(monthly["market_controller"]["minimum_limit_up_count"]),
    )
    baselines, _candidates = _variant_sets()
    cache = StaticOutcomeCache()
    legs: dict[str, pd.DataFrame] = {}
    for leg in FIXED_PRIORITY:
        if leg in SHADOW_LEGS:
            legs[leg] = build_variant_plan(
                baselines[leg],
                signal_pool=context["signal_pool"],
                d_events=context["d_events"],
                allowed_action_dates=context["allowed_actions"],
                cutoff=str(end),
                outcome_cache=cache,
            )
        else:
            legs[leg] = pd.DataFrame()
    execution = _execution_kwargs(monthly)
    detail = replay_action_date_cash_portfolio(
        legs,
        action_dates=context["action_dates"],
        priority=FIXED_PRIORITY,
        **execution,
    )
    return shadow_nav_from_detail(
        detail,
        context["action_dates"],
        initial_cash=float(execution["initial_cash"]),
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_decision(settings: EquityCurveStopSettings, payload: Mapping[str, Any]) -> None:
    body = dict(payload)
    body.setdefault("schema_version", SCHEMA_VERSION)
    body.setdefault("method_id", METHOD_ID)
    _atomic_write_text(
        settings.decision_path,
        json.dumps(body, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )


def write_shadow_nav(settings: EquityCurveStopSettings, nav: pd.Series) -> None:
    frame = pd.DataFrame({"action_date": list(nav.index), "shadow_nav": nav.to_numpy()})
    settings.shadow_nav_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings.shadow_nav_path.with_name(f".{settings.shadow_nav_path.name}.{os.getpid()}.tmp")
    frame.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, settings.shadow_nav_path)


def load_decision(settings: EquityCurveStopSettings) -> dict[str, Any]:
    try:
        payload = json.loads(settings.decision_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def live_gate(config: Mapping[str, Any], project_root: Path, action_date: str) -> LiveGateCheck:
    """实盘开新仓前的停手检查；启用时任何数据缺失、过期或格式错误都按停手处理。"""

    settings = load_settings(config, project_root)
    source = str(settings.decision_path)
    if not settings.enabled:
        return LiveGateCheck(True, "方案甲停手门禁未启用", source, {})
    payload = load_decision(settings)
    if not payload:
        return LiveGateCheck(
            False,
            "方案甲停手门禁缺少今日判定文件，按fail-closed不开新仓（请检查收盘流水线⑬）",
            source,
            {},
        )
    if str(payload.get("method_id", "")) != METHOD_ID:
        return LiveGateCheck(False, "方案甲停手判定文件方法版本不符，按fail-closed不开新仓", source, payload)
    if str(payload.get("action_date", "")) != str(action_date):
        return LiveGateCheck(
            False,
            f"方案甲停手判定对应{payload.get('action_date', '未知')}，不是今日{action_date}，"
            "按fail-closed不开新仓（请检查收盘流水线⑬）",
            source,
            payload,
        )
    if (
        int(payload.get("ma_window", -1)) != settings.ma_window
        or int(payload.get("lag_trading_days", -1)) != settings.lag_trading_days
    ):
        return LiveGateCheck(False, "方案甲停手判定参数与正式配置不一致，按fail-closed不开新仓", source, payload)
    allowed = payload.get("allowed")
    if allowed is True:
        return LiveGateCheck(True, str(payload.get("reason", "")), source, payload)
    if allowed is False:
        return LiveGateCheck(False, str(payload.get("reason", "方案甲停手")), source, payload)
    return LiveGateCheck(False, "方案甲停手判定缺少allowed字段，按fail-closed不开新仓", source, payload)


def next_open_date(calendar_path: Path, after: str) -> str:
    calendar = pd.read_csv(calendar_path, dtype={"cal_date": str}, low_memory=False)
    opened = calendar[pd.to_numeric(calendar["is_open"], errors="coerce").eq(1)]["cal_date"].astype(str)
    later = sorted(date for date in opened if date > str(after))
    if not later:
        raise RuntimeError(f"交易日历中没有{after}之后的开市日")
    return later[0]


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
