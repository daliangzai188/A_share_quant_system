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


WEEKLY_WINDOW_3M = 63
WEEKLY_WINDOW_6M = 126


@dataclass(frozen=True)
class WeeklyReportSettings:
    """空仓期周报与失败条件；阈值来自2026-09-21影子账样本外分布，事先写死。"""

    enabled: bool
    window_3m: int
    window_6m: int
    shadow_3m_fail: float
    shadow_6m_fail: float
    worst_stop_missed_gain: float


def load_weekly_report_settings(config: Mapping[str, Any]) -> WeeklyReportSettings:
    section = dict((config.get("equity_curve_stop", {}) or {}).get("weekly_report", {}) or {})
    return WeeklyReportSettings(
        enabled=bool(section.get("enabled", False)),
        window_3m=int(section.get("window_3m", WEEKLY_WINDOW_3M)),
        window_6m=int(section.get("window_6m", WEEKLY_WINDOW_6M)),
        shadow_3m_fail=float(section.get("shadow_3m_fail", -0.241)),
        shadow_6m_fail=float(section.get("shadow_6m_fail", -0.194)),
        worst_stop_missed_gain=float(section.get("worst_stop_missed_gain", 0.269)),
    )


@dataclass(frozen=True)
class RegimeBucket:
    """按当月全市场平均涨停数分档的样本外期望；档位与数值事先写死。"""

    limit_up_min: float
    limit_up_max: float | None
    oos_months: int
    oos_median: float | None
    oos_win_rate: float | None

    @property
    def label(self) -> str:
        return f"{self.limit_up_min:.0f}~{self.limit_up_max:.0f}" if self.limit_up_max else f"{self.limit_up_min:.0f}以上"

    def contains(self, count: float) -> bool:
        if count < self.limit_up_min:
            return False
        return True if self.limit_up_max is None else count < self.limit_up_max


def load_regime_buckets(config: Mapping[str, Any]) -> tuple[list[RegimeBucket], int]:
    section = dict((config.get("equity_curve_stop", {}) or {}).get("weekly_report", {}) or {})
    buckets = []
    for row in section.get("regime_buckets", []) or []:
        item = dict(row)
        upper = item.get("limit_up_max")
        buckets.append(
            RegimeBucket(
                limit_up_min=float(item.get("limit_up_min", 0.0)),
                limit_up_max=None if upper is None else float(upper),
                oos_months=int(item.get("oos_months", 0)),
                oos_median=None if item.get("oos_median") is None else float(item["oos_median"]),
                oos_win_rate=None if item.get("oos_win_rate") is None else float(item["oos_win_rate"]),
            )
        )
    return buckets, int(section.get("regime_min_months", 5))


def monthly_limit_up_mean(sentiment_path: Path, month: str) -> float | None:
    """当月至今的全市场平均涨停数；文件缺失或该月无数据时返回None。"""

    try:
        frame = pd.read_csv(sentiment_path, dtype={"trade_date": str}, low_memory=False)
    except (OSError, ValueError):
        return None
    if "trade_date" not in frame.columns or "limit_up_count" not in frame.columns:
        return None
    rows = frame[frame["trade_date"].astype(str).str[:6] == str(month)[:6]]
    counts = pd.to_numeric(rows["limit_up_count"], errors="coerce").dropna()
    return float(counts.mean()) if len(counts) else None


def completed_month_returns(
    dates: Sequence[str],
    nav: Sequence[float],
    *,
    count: int,
) -> list[tuple[str, float]]:
    """最近 ``count`` 个已结束自然月的影子账月收益（不含当前未结束的月）。"""

    labels = [str(date) for date in dates]
    values = np.asarray(nav, dtype=float)
    if not labels:
        return []
    current = labels[-1][:6]
    months: list[str] = []
    for label in labels:
        if label[:6] != current and (not months or months[-1] != label[:6]):
            months.append(label[:6])
    result: list[tuple[str, float]] = []
    for month in months[-int(count):]:
        index = [i for i, label in enumerate(labels) if label[:6] == month]
        if not index or index[0] == 0:
            continue
        result.append((month, float(values[index[-1]] / values[index[0] - 1] - 1.0)))
    return result


def regime_stall_streak(
    month_rows: Sequence[Mapping[str, Any]],
    *,
    min_limit_up: float,
    need_months: int,
) -> dict[str, Any]:
    """行情正常却连续不赚钱的月数。

    只数最近连续的月份：某月行情属于冰点（涨停数低于 ``min_limit_up``）或影子账为正，
    计数即归零——冰点亏损是这套规则的已知常态，不能算失效证据。
    """

    streak: list[str] = []
    for row in reversed(list(month_rows)):
        limit_up = row.get("limit_up_mean")
        shadow = row.get("shadow_return")
        if limit_up is None or shadow is None:
            break
        if float(limit_up) < float(min_limit_up) or float(shadow) > 0:
            break
        streak.append(str(row.get("ym", "")))
    streak.reverse()
    return {
        "streak": len(streak),
        "need_months": int(need_months),
        "months": streak,
        "min_limit_up": float(min_limit_up),
        "triggered": len(streak) >= int(need_months),
    }


def regime_calibration(
    limit_up_mean: float | None,
    shadow_month_return: float | None,
    buckets: Sequence[RegimeBucket],
    *,
    min_months: int,
) -> dict[str, Any] | None:
    """把"行情好不好"和"策略行不行"分开：当月涨停数落在哪一档、该档样本外期望、实际差多少。"""

    if limit_up_mean is None or not buckets:
        return None
    bucket = next((item for item in buckets if item.contains(float(limit_up_mean))), None)
    if bucket is None:
        return None
    enough = bucket.oos_months >= int(min_months) and bucket.oos_median is not None
    gap = (
        float(shadow_month_return) - float(bucket.oos_median)
        if enough and isinstance(shadow_month_return, (int, float))
        else None
    )
    return {
        "limit_up_mean": float(limit_up_mean),
        "bucket": bucket.label,
        "oos_months": bucket.oos_months,
        "oos_median": bucket.oos_median,
        "oos_win_rate": bucket.oos_win_rate,
        "actual": shadow_month_return,
        "gap": gap,
        "sufficient_sample": bool(enough),
    }


def _iso_week(date: str) -> tuple[int, int]:
    day = dt.date(int(str(date)[:4]), int(str(date)[4:6]), int(str(date)[6:8]))
    year, week, _ = day.isocalendar()
    return int(year), int(week)


def is_week_last_open_day(open_dates: Sequence[str], signal_date: str) -> bool:
    """收盘日是否为所在自然周的最后一个开市日；遇假期自动前移到实际最后一天。"""

    dates = sorted(str(date) for date in open_dates)
    signal = str(signal_date)
    if signal not in dates:
        return False
    later = [date for date in dates if date > signal]
    if not later:
        return True
    return _iso_week(later[0]) != _iso_week(signal)


def current_stop_episode(
    dates: Sequence[str],
    nav: Sequence[float],
    *,
    ma_window: int,
    lag: int,
) -> dict[str, Any] | None:
    """最新行动日仍在停手时，返回本段停手的起点、交易日数与期间影子账涨跌。

    ``shadow_return`` 为正表示这段停手踏空了行情，为负表示躲掉了下跌。
    """

    flags = allowed_flags(nav, ma_window=ma_window, lag=lag)
    if len(flags) == 0 or bool(flags[-1]):
        return None
    start = len(flags) - 1
    while start > 0 and not bool(flags[start - 1]):
        start -= 1
    values = np.asarray(nav, dtype=float)
    base = float(values[start - 1]) if start > 0 else float(values[start])
    return {
        "start_date": str(dates[start]),
        "trading_days": int(len(flags) - start),
        "shadow_return": float(values[-1] / base - 1.0) if base else 0.0,
    }


def earliest_resume_if_flat(
    nav: Sequence[float],
    future_open_dates: Sequence[str],
    *,
    ma_window: int,
    lag: int,
    max_days: int = 120,
) -> str | None:
    """影子账此后原地不动时，最早恢复开仓的行动日；窗口内不会恢复则返回None。

    只用于周报里给等待时间一个下限：影子账上涨会提前，继续下跌会更晚。
    """

    values = [float(x) for x in np.asarray(nav, dtype=float)]
    for date in list(future_open_dates)[: int(max_days)]:
        values.append(values[-1])
        lagged = len(values) - 1 - int(lag)
        if lagged >= int(ma_window):
            window = values[lagged - int(ma_window) + 1: lagged + 1]
            if values[lagged] >= float(np.mean(window)):
                return str(date)
    return None


def weekly_report_payload(
    dates: Sequence[str],
    nav: Sequence[float],
    decision: Mapping[str, Any],
    settings: WeeklyReportSettings,
    *,
    ma_window: int,
    lag: int,
    future_open_dates: Sequence[str] = (),
    regime: Mapping[str, Any] | None = None,
    regime_stall: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """空仓期周报的全部数字；失败条件逐条比对事先写死的阈值。"""

    labels = [str(date) for date in dates]
    values = np.asarray(nav, dtype=float)
    if not labels:
        raise ValueError("影子净值为空，无法生成周报")

    def _change(offset: int) -> float | None:
        return float(values[-1] / values[-1 - offset] - 1.0) if len(values) > offset else None

    def _change_from(index: int | None) -> float | None:
        return float(values[-1] / values[index] - 1.0) if index is not None else None

    last = labels[-1]
    week_start = next(
        (i for i in range(len(labels) - 1, -1, -1) if labels[i] < last and _iso_week(labels[i]) != _iso_week(last)),
        None,
    )
    month_start = next(
        (i for i in range(len(labels) - 1, -1, -1) if labels[i][:6] < last[:6]),
        None,
    )
    return_3m = _change(settings.window_3m)
    return_6m = _change(settings.window_6m)
    episode = current_stop_episode(labels, values, ma_window=ma_window, lag=lag)
    lag_nav = decision.get("lag_nav")
    lag_ma = decision.get("lag_ma")
    gap_to_resume = (
        float(lag_ma) / float(lag_nav) - 1.0
        if isinstance(lag_nav, (int, float)) and isinstance(lag_ma, (int, float)) and float(lag_nav) > 0
        else None
    )

    failures: list[str] = []
    if return_3m is not None and return_3m <= settings.shadow_3m_fail:
        failures.append(
            f"影子账近3个月{return_3m:+.1%}，跌破失败线{settings.shadow_3m_fail:+.1%}（选股逻辑可能失效）"
        )
    if return_6m is not None and return_6m <= settings.shadow_6m_fail:
        failures.append(
            f"影子账近6个月{return_6m:+.1%}，跌破失败线{settings.shadow_6m_fail:+.1%}（选股逻辑可能失效）"
        )
    if episode and episode["shadow_return"] >= settings.worst_stop_missed_gain:
        failures.append(
            f"本段停手踏空{episode['shadow_return']:+.1%}，超过历史最差{settings.worst_stop_missed_gain:+.1%}"
            "（停手参数留待年度复核，当年不改）"
        )
    if regime_stall and regime_stall.get("triggered"):
        months = "、".join(str(m) for m in regime_stall.get("months", []))
        failures.append(
            f"行情正常（涨停≥{float(regime_stall['min_limit_up']):.0f}）却连续{int(regime_stall['streak'])}个月不赚钱：{months}"
            "（选股规则可能已钝化）"
        )
    return {
        "signal_date": last,
        "next_action_date": str(decision.get("action_date", "")),
        "earliest_resume_if_flat": (
            earliest_resume_if_flat(values, future_open_dates, ma_window=ma_window, lag=lag)
            if episode and len(future_open_dates)
            else None
        ),
        "allowed": bool(decision.get("allowed", False)),
        "week_return": _change_from(week_start),
        "month_to_date_return": _change_from(month_start),
        "return_3m": return_3m,
        "return_6m": return_6m,
        "stop_episode": episode,
        "gap_to_resume": gap_to_resume,
        "failures": failures,
        "regime": dict(regime) if regime else None,
        "regime_stall": dict(regime_stall) if regime_stall else None,
        "thresholds": {
            "shadow_3m_fail": settings.shadow_3m_fail,
            "shadow_6m_fail": settings.shadow_6m_fail,
            "worst_stop_missed_gain": settings.worst_stop_missed_gain,
        },
    }


def format_weekly_report(payload: Mapping[str, Any]) -> tuple[str, str]:
    """把周报数字排成Bark的标题与正文。"""

    def _pct(value: Any) -> str:
        return f"{float(value):+.1%}" if isinstance(value, (int, float)) else "数据不足"

    episode = payload.get("stop_episode")
    if episode:
        title = f"📄 方案甲周报 {payload['signal_date']}：停手第{episode['trading_days']}个交易日"
    else:
        title = f"📄 方案甲周报 {payload['signal_date']}：正常开仓中"
    lines = [
        "影子账（同一套规则、从不停手）："
        f"本周{_pct(payload.get('week_return'))}，本月{_pct(payload.get('month_to_date_return'))}，"
        f"近3个月{_pct(payload.get('return_3m'))}，近6个月{_pct(payload.get('return_6m'))}。",
    ]
    if episode:
        missed = float(episode["shadow_return"])
        lines.append(
            f"本段停手自{episode['start_date']}起{episode['trading_days']}个交易日，期间影子账{missed:+.1%}"
            f"（{'踏空' if missed > 0 else '躲掉下跌' if missed < 0 else '持平'}）。"
        )
        gap = payload.get("gap_to_resume")
        if isinstance(gap, (int, float)):
            lines.append(f"恢复开仓还差：滞后6日口径的影子净值需再涨{float(gap):+.1%}。")
        flat = payload.get("earliest_resume_if_flat")
        lines.append(
            f"影子账若原地不动，最早{flat}恢复开仓；它上涨会提前，继续下跌会更晚。"
            if flat
            else "影子账若原地不动，未来120个交易日内都不会恢复开仓；要靠它自己涨回均线上方。"
        )
    else:
        lines.append(f"当前允许开仓，下一个行动日{payload.get('next_action_date', '')}。")
    regime = payload.get("regime")
    if regime:
        head = f"行情校准：本月全市场平均涨停{float(regime['limit_up_mean']):.0f}只（{regime['bucket']}档）"
        if regime.get("sufficient_sample"):
            lines.append(
                head
                + f"，该档样本外月收益中位数{float(regime['oos_median']):+.1%}、赚钱月{float(regime['oos_win_rate']):.0%}"
                + (f"；影子账本月{float(regime['actual']):+.1%}，差{float(regime['gap']):+.1%}。"
                   if isinstance(regime.get("gap"), (int, float))
                   else "。")
            )
        else:
            lines.append(head + f"，该档样本外只有{regime['oos_months']}个月，样本不足，不做对比。")
    stall = payload.get("regime_stall")
    if stall and not stall.get("triggered"):
        lines.append(
            f"正常行情连续不赚钱：{int(stall['streak'])}/{int(stall['need_months'])}个月"
            + (f"（{'、'.join(str(m) for m in stall.get('months', []))}）。" if stall.get("months") else "。")
        )
    lines.append(
        "失败条件：" + ("；".join(payload["failures"]) + "。请在Claude发送：甲·临时复核。"
                    if payload.get("failures")
                    else "未触发（连续3个月不赚钱属于正常范围）。")
    )
    return title, "".join(lines)


def open_dates_from_calendar(calendar_path: Path) -> list[str]:
    calendar = pd.read_csv(calendar_path, dtype={"cal_date": str}, low_memory=False)
    opened = calendar[pd.to_numeric(calendar["is_open"], errors="coerce").eq(1)]["cal_date"].astype(str)
    return sorted(str(date) for date in opened)


def next_open_date(calendar_path: Path, after: str) -> str:
    later = [date for date in open_dates_from_calendar(calendar_path) if date > str(after)]
    if not later:
        raise RuntimeError(f"交易日历中没有{after}之后的开市日")
    return later[0]


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
