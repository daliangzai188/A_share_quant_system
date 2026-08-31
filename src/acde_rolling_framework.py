"""A/C/E/D半年滚动研究的统一三窗口与真实开仓日回放框架。

本模块只提供研究口径，不读取或改写生产策略。候选生成器必须把每个信号计划
输出为逐笔结果表，再由这里统一按 ``A>C>E>D``、真实 ``action_date``、退出日
占资以及三窗口门禁评估。最近半年只产生失效告警，绝不参与候选排名。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import datetime as dt
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from src.mechanical_compound import mechanical_compound


FIXED_PRIORITY = ("A", "C", "E", "D")
TOLERANCE = 1e-12


def normalize_date(value: object) -> str:
    return str(value or "").replace(".0", "")


def normalize_bool(value: object, default: bool = False) -> bool:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


def _subtract_years(value: dt.date, years: int) -> dt.date:
    try:
        return value.replace(year=value.year - int(years))
    except ValueError:
        return value.replace(year=value.year - int(years), day=28)


def _subtract_months(value: dt.date, months: int) -> dt.date:
    month_index = value.year * 12 + value.month - 1 - int(months)
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    month_end = (
        dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
        if month == 12
        else dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    )
    return dt.date(year, month, min(value.day, month_end.day))


@dataclass(frozen=True)
class ResearchWindow:
    name: str
    start: str
    end: str
    role: str
    may_rank_candidates: bool


@dataclass(frozen=True)
class RollingWindowSet:
    update_node: str
    main: ResearchWindow
    recent: ResearchWindow
    failure_check: ResearchWindow

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_window_set(
    update_node: str,
    *,
    main_years: int = 3,
    recent_years: int = 2,
    failure_months: int = 6,
    allowed_nodes: Iterable[str] = ("0630", "1231"),
) -> RollingWindowSet:
    """按自然日边界建立主优化、近期确认和半年失效检查窗口。"""

    text = normalize_date(update_node)
    end = dt.datetime.strptime(text, "%Y%m%d").date()
    if end.strftime("%m%d") not in {str(value) for value in allowed_nodes}:
        raise ValueError("更新节点只允许6月30日或12月31日")
    if not 0 < int(failure_months) <= int(recent_years) * 12 <= int(main_years) * 12:
        raise ValueError("窗口长度必须满足：半年检查 <= 近期确认 <= 主优化")

    # 这些是包含首尾的自然日窗口，因此3年窗口在2026-06-30节点必须从
    # 2023-07-01开始，而不是把2023-06-30也算进去。
    main_start = _subtract_years(end, main_years) + dt.timedelta(days=1)
    recent_start = _subtract_years(end, recent_years) + dt.timedelta(days=1)
    first_of_end_month = end.replace(day=1)
    failure_start = _subtract_months(first_of_end_month, failure_months - 1)
    return RollingWindowSet(
        update_node=text,
        main=ResearchWindow(
            "main",
            main_start.strftime("%Y%m%d"),
            text,
            "参数筛选与跨行情稳定性",
            True,
        ),
        recent=ResearchWindow(
            "recent",
            recent_start.strftime("%Y%m%d"),
            text,
            "确认参数未被旧行情拖累",
            False,
        ),
        failure_check=ResearchWindow(
            "failure_check",
            failure_start.strftime("%Y%m%d"),
            text,
            "只检查是否明显失效",
            False,
        ),
    )


def open_dates(
    calendar: pd.DataFrame,
    start: str,
    end: str,
) -> list[str]:
    if "cal_date" not in calendar.columns:
        raise KeyError("交易日历缺少cal_date")
    sample = calendar.copy()
    sample["cal_date"] = sample["cal_date"].map(normalize_date)
    if "is_open" in sample.columns:
        sample = sample[
            sample["is_open"].astype(str).str.lower().isin({"1", "1.0", "true"})
        ]
    return sorted(
        sample.loc[sample["cal_date"].between(str(start), str(end)), "cal_date"]
        .astype(str)
        .unique()
    )


def prior_open_date(calendar: pd.DataFrame, start: str) -> str:
    dates = open_dates(calendar, "19000101", str(start))
    earlier = [value for value in dates if value < str(start)]
    if not earlier:
        raise RuntimeError(f"{start}之前没有可用交易日，无法加载冻结计划")
    return earlier[-1]


def _next_open_date(calendar_dates: Sequence[str], signal_date: str) -> str:
    for value in calendar_dates:
        if str(value) > str(signal_date):
            return str(value)
    return ""


def plan_map(
    frame: pd.DataFrame,
    leg: str,
    *,
    calendar_dates: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """把信号计划映射到开仓日；计划存在和最终成交是两个不同状态。"""

    normalized_leg = str(leg).strip().upper()
    if normalized_leg not in FIXED_PRIORITY:
        raise ValueError(f"未知策略腿：{leg}")
    if frame.empty:
        return {}
    required = {"signal_date", "status", "ts_code"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise KeyError(f"{normalized_leg}计划缺少字段：{missing}")

    rows: list[dict[str, Any]] = []
    for raw in frame.to_dict("records"):
        row = dict(raw)
        signal_date = normalize_date(row.get("signal_date"))
        if normalized_leg == "D":
            derived_action_date = signal_date
        else:
            derived_action_date = normalize_date(row.get("buy_date"))
            if not derived_action_date:
                derived_action_date = _next_open_date(calendar_dates, signal_date)
        declared_action_date = normalize_date(row.get("action_date"))
        if declared_action_date and declared_action_date != derived_action_date:
            raise ValueError(
                f"{normalized_leg}显式action_date与交易规则不一致："
                f"{declared_action_date}!={derived_action_date}"
            )
        action_date = derived_action_date
        if not action_date:
            raise ValueError(
                f"{normalized_leg}信号{signal_date}/{row.get('ts_code')}无法解析action_date"
            )
        row["signal_date"] = signal_date
        row["action_date"] = action_date
        row["exit_date"] = normalize_date(row.get("exit_date"))
        row["strategy_leg"] = normalized_leg
        rows.append(row)

    normalized = pd.DataFrame(rows).sort_values(
        ["action_date", "signal_date", "ts_code"],
        ascending=[True, True, True],
    )
    if normalized["action_date"].duplicated().any():
        duplicates = sorted(
            normalized.loc[normalized["action_date"].duplicated(False), "action_date"]
            .astype(str)
            .unique()
        )
        raise ValueError(
            f"{normalized_leg}同一action_date存在多个未裁决计划：{duplicates[:5]}"
        )
    return {
        str(row["action_date"]): row
        for row in normalized.to_dict("records")
    }


def replay_action_date_portfolio(
    legs: Mapping[str, pd.DataFrame],
    *,
    action_dates: Sequence[str],
    priority: Sequence[str] = FIXED_PRIORITY,
) -> pd.DataFrame:
    """按真实开仓日执行单账户回放，冻结计划即使未成交也会阻断低顺位。"""

    normalized_priority = tuple(str(value).strip().upper() for value in priority)
    normalized_legs = tuple(str(value).strip().upper() for value in legs)
    if len(normalized_legs) == len(FIXED_PRIORITY):
        if normalized_priority != FIXED_PRIORITY or set(normalized_legs) != set(FIXED_PRIORITY):
            raise ValueError("四腿组合priority必须严格固定为A>C>E>D")
    elif len(normalized_legs) == 1:
        if normalized_priority != normalized_legs:
            raise ValueError("单腿回放priority必须与唯一策略腿一致")
    else:
        raise ValueError("统一回放只接受完整A>C>E>D组合或单腿独立回放")
    dates = tuple(sorted({normalize_date(value) for value in action_dates}))
    maps = {
        leg: plan_map(legs[leg], leg, calendar_dates=dates)
        for leg in normalized_priority
    }

    equity = 1.0
    occupied_until = ""
    rows: list[dict[str, Any]] = []
    for action_date in dates:
        if occupied_until and action_date <= occupied_until:
            rows.append(
                {
                    "action_date": action_date,
                    "signal_date": "",
                    "status": "SKIP_OCCUPIED",
                    "execution_status": "",
                    "strategy_leg": "",
                    "account_return": 0.0,
                    "equity_after": equity,
                }
            )
            continue

        selected: dict[str, Any] | None = None
        for leg in normalized_priority:
            selected = maps[leg].get(action_date)
            if selected is not None:
                break
        if selected is None:
            rows.append(
                {
                    "action_date": action_date,
                    "signal_date": "",
                    "status": "NO_PLAN",
                    "execution_status": "",
                    "strategy_leg": "",
                    "account_return": 0.0,
                    "equity_after": equity,
                }
            )
            continue

        execution_status = str(selected.get("status", ""))
        value = pd.to_numeric(selected.get("account_return"), errors="coerce")
        exit_date = normalize_date(selected.get("exit_date"))
        entry_filled = normalize_bool(
            selected.get("entry_filled"), execution_status == "OK"
        )
        position_opened = normalize_bool(
            selected.get("position_opened"), entry_filled
        )
        outcome_observable = normalize_bool(
            selected.get("outcome_observable"),
            execution_status == "OK" and not pd.isna(value),
        )
        position_open_until = normalize_date(
            selected.get("position_open_until", exit_date)
        )
        base = {
            "action_date": action_date,
            "signal_date": normalize_date(selected.get("signal_date")),
            "execution_status": execution_status,
            "strategy_leg": str(selected.get("strategy_leg", "")),
            "ts_code": str(selected.get("ts_code", "")),
            "name": str(selected.get("name", "")),
            "exit_date": exit_date,
            "entry_filled": entry_filled,
            "position_opened": position_opened,
            "outcome_observable": outcome_observable,
        }
        if position_opened:
            occupied_until = position_open_until or dates[-1]
            if occupied_until < action_date:
                raise ValueError(
                    f"持仓占用结束日非法：{base['strategy_leg']} {action_date} {occupied_until}"
                )
        if not position_opened:
            rows.append(
                {
                    **base,
                    "status": "PLAN_NOT_EXECUTED",
                    "account_return": 0.0,
                    "equity_after": equity,
                }
            )
            continue
        if not outcome_observable or execution_status != "OK" or pd.isna(value):
            rows.append(
                {
                    **base,
                    "status": "POSITION_OPEN_OUTCOME_UNOBSERVABLE",
                    "account_return": 0.0,
                    "equity_after": equity,
                }
            )
            continue
        if not exit_date or exit_date < action_date:
            raise ValueError(
                f"成交计划退出日非法：{base['strategy_leg']} {action_date} {exit_date}"
            )

        account_return = float(value)
        equity *= 1.0 + account_return
        rows.append(
            {
                **base,
                "status": "EXECUTED",
                "account_return": account_return,
                "equity_after": equity,
            }
        )

    detail = pd.DataFrame(rows)
    if detail.empty:
        return detail
    detail["peak_equity"] = detail["equity_after"].cummax()
    detail["drawdown"] = detail["equity_after"] / detail["peak_equity"] - 1.0
    return detail


def _max_consecutive_losses(values: np.ndarray) -> int:
    current = maximum = 0
    for value in values:
        current = current + 1 if float(value) <= 0 else 0
        maximum = max(maximum, current)
    return maximum


def action_metrics(
    detail: pd.DataFrame,
    start: str,
    end: str,
) -> dict[str, Any]:
    """只按action_date切窗；signal_date不得参与窗口归属。"""

    if detail.empty:
        trades = pd.DataFrame(columns=["account_return", "strategy_leg"])
    else:
        if "action_date" not in detail.columns:
            raise KeyError("真实开仓日指标缺少action_date")
        trades = detail[
            detail["action_date"].astype(str).between(str(start), str(end))
            & detail["status"].astype(str).eq("EXECUTED")
        ].copy()
    values = pd.to_numeric(
        trades.get("account_return", pd.Series(dtype=float)), errors="raise"
    ).dropna().to_numpy(dtype=float)
    if len(values) == 0:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_account_return": 0.0,
            "median_account_return": 0.0,
            "equity_multiple": 1.0,
            "max_drawdown": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "profit_loss_ratio": 0.0,
            "max_consecutive_losses": 0,
            "leg_counts": {},
        }
    compound = mechanical_compound(values)
    gains = values[values > 0]
    losses = values[values < 0]
    return {
        "trade_count": int(len(values)),
        "win_rate": float((values > 0).mean()),
        "avg_account_return": float(values.mean()),
        "median_account_return": float(np.median(values)),
        "equity_multiple": float(compound.equity_multiple),
        "max_drawdown": float(compound.max_drawdown),
        "max_profit": float(values.max()),
        "max_loss": float(values.min()),
        "profit_loss_ratio": (
            float(gains.mean() / abs(losses.mean()))
            if len(gains) and len(losses)
            else 0.0
        ),
        "max_consecutive_losses": _max_consecutive_losses(values),
        "leg_counts": trades["strategy_leg"].value_counts().sort_index().to_dict(),
    }


def standalone_replay(
    frame: pd.DataFrame,
    leg: str,
    *,
    action_dates: Sequence[str],
) -> pd.DataFrame:
    return replay_action_date_portfolio(
        {str(leg).upper(): frame},
        action_dates=action_dates,
        priority=(str(leg).upper(),),
    )


def _improved(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> bool:
    return float(candidate["equity_multiple"]) > (
        float(baseline["equity_multiple"]) + TOLERANCE
    )


def _sample_retained(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any], minimum: float
) -> bool:
    baseline_count = int(baseline["trade_count"])
    if baseline_count <= 0:
        return int(candidate["trade_count"]) > 0
    return int(candidate["trade_count"]) >= int(np.ceil(baseline_count * minimum))


def evaluate_three_window_replacement(
    *,
    leg: str,
    baseline_leg: pd.DataFrame,
    candidate_leg: pd.DataFrame,
    frozen_other_legs: Mapping[str, pd.DataFrame],
    calendar: pd.DataFrame,
    windows: RollingWindowSet,
    gate: Mapping[str, Any],
) -> dict[str, Any]:
    """逐腿冻结评估；候选排名只能读取返回值中的main结果。"""

    normalized_leg = str(leg).upper()
    baseline_legs = {name: frame.copy() for name, frame in frozen_other_legs.items()}
    baseline_legs[normalized_leg] = baseline_leg.copy()
    candidate_legs = dict(baseline_legs)
    candidate_legs[normalized_leg] = candidate_leg.copy()
    if set(baseline_legs) != set(FIXED_PRIORITY):
        raise ValueError("逐腿替换必须同时提供A/C/E/D四条腿")

    dates = open_dates(calendar, windows.main.start, windows.main.end)
    baseline_leg_detail = standalone_replay(
        baseline_leg, normalized_leg, action_dates=dates
    )
    candidate_leg_detail = standalone_replay(
        candidate_leg, normalized_leg, action_dates=dates
    )
    baseline_combo_detail = replay_action_date_portfolio(
        baseline_legs, action_dates=dates
    )
    candidate_combo_detail = replay_action_date_portfolio(
        candidate_legs, action_dates=dates
    )

    metrics: dict[str, Any] = {}
    for window in (windows.main, windows.recent, windows.failure_check):
        metrics[window.name] = {
            "baseline_standalone": action_metrics(
                baseline_leg_detail, window.start, window.end
            ),
            "candidate_standalone": action_metrics(
                candidate_leg_detail, window.start, window.end
            ),
            "baseline_portfolio": action_metrics(
                baseline_combo_detail, window.start, window.end
            ),
            "candidate_portfolio": action_metrics(
                candidate_combo_detail, window.start, window.end
            ),
            "may_rank_candidates": bool(window.may_rank_candidates),
        }

    main = metrics["main"]
    recent = metrics["recent"]
    failure = metrics["failure_check"]
    window_reasons: dict[str, list[str]] = {"MAIN": [], "RECENT": []}
    for prefix, sample, minimum_key, drawdown_key in (
        (
            "MAIN",
            main,
            "minimum_main_sample_retention",
            "maximum_main_drawdown_worsening_pp",
        ),
        (
            "RECENT",
            recent,
            "minimum_recent_sample_retention",
            "maximum_recent_drawdown_worsening_pp",
        ),
    ):
        reasons = window_reasons[prefix]
        if not _improved(sample["candidate_standalone"], sample["baseline_standalone"]):
            reasons.append(f"{prefix}_STANDALONE_COMPOUND")
        if not _improved(sample["candidate_portfolio"], sample["baseline_portfolio"]):
            reasons.append(f"{prefix}_PORTFOLIO_COMPOUND")
        if not _sample_retained(
            sample["candidate_standalone"],
            sample["baseline_standalone"],
            float(gate[minimum_key]),
        ):
            reasons.append(f"{prefix}_SAMPLE_RETENTION")
        absolute_minimum = int(
            gate.get("minimum_absolute_trades", {}).get(normalized_leg, 0)
        )
        if int(sample["candidate_standalone"]["trade_count"]) < absolute_minimum:
            reasons.append(f"{prefix}_ABSOLUTE_SAMPLE")
        allowed_worsening = float(gate[drawdown_key])
        if float(sample["candidate_standalone"]["max_drawdown"]) < (
            float(sample["baseline_standalone"]["max_drawdown"]) - allowed_worsening
        ):
            reasons.append(f"{prefix}_STANDALONE_DRAWDOWN")
        if float(sample["candidate_portfolio"]["max_drawdown"]) < (
            float(sample["baseline_portfolio"]["max_drawdown"]) - allowed_worsening
        ):
            reasons.append(f"{prefix}_PORTFOLIO_DRAWDOWN")

    minimum_failure_trades = int(gate["failure_check_minimum_trades"])
    half_observable: dict[str, bool] = {}
    failure_flags: list[str] = []
    for scope in ("standalone", "portfolio"):
        half_observable[scope] = (
            int(failure[f"candidate_{scope}"]["trade_count"]) >= minimum_failure_trades
        )
        if half_observable[scope]:
            item = failure[f"candidate_{scope}"]
            equity_failed = float(item["equity_multiple"]) <= float(
                gate["failure_check_equity_floor"]
            )
            if bool(
                gate.get(
                    "failure_check_require_nonpositive_average_with_equity_failure",
                    False,
                )
            ):
                equity_failed = equity_failed and float(
                    item["avg_account_return"]
                ) <= 0.0
            if equity_failed:
                failure_flags.append(f"HALF_YEAR_{scope.upper()}_EQUITY")
            if float(item["max_drawdown"]) < float(gate["failure_check_drawdown_floor"]):
                failure_flags.append(f"HALF_YEAR_{scope.upper()}_DRAWDOWN")
    main_reasons = window_reasons["MAIN"]
    recent_reasons = window_reasons["RECENT"]
    all_reasons = [*main_reasons, *recent_reasons, *failure_flags]

    return {
        "strategy_leg": normalized_leg,
        "priority": list(FIXED_PRIORITY),
        "window_metric": "action_date",
        "selection_window": "main",
        "recent_confirmation_ranked_candidates": False,
        "failure_check_ranked_candidates": False,
        "metrics": metrics,
        "failure_check_observable": half_observable,
        "failure_flags": failure_flags,
        "main_gate_passed": not main_reasons,
        "main_gate_reasons": main_reasons,
        "recent_confirmation_passed": not recent_reasons,
        "recent_confirmation_reasons": recent_reasons,
        # 兼容旧研究读取方；selection在新框架中只代表三年主窗，绝不包含两年确认。
        "selection_gate_passed": not main_reasons,
        "selection_gate_reasons": main_reasons,
        "replacement_gate_passed": not all_reasons,
        "gate_reasons": all_reasons,
        "details": {
            "baseline_standalone": baseline_leg_detail,
            "candidate_standalone": candidate_leg_detail,
            "baseline_portfolio": baseline_combo_detail,
            "candidate_portfolio": candidate_combo_detail,
        },
    }


def coverage_audit(
    frame: pd.DataFrame,
    *,
    date_column: str,
    required_start: str,
    required_end: str,
    require_start_within_days: int = 7,
    require_end_within_days: int = 7,
) -> dict[str, Any]:
    if date_column not in frame.columns or frame.empty:
        return {
            "passed": False,
            "reason": "EMPTY_OR_MISSING_DATE_COLUMN",
            "required_start": required_start,
            "required_end": required_end,
        }
    dates = frame[date_column].map(normalize_date)
    available_start = str(dates.min())
    available_end = str(dates.max())
    required_start_date = dt.datetime.strptime(str(required_start), "%Y%m%d").date()
    required_end_date = dt.datetime.strptime(str(required_end), "%Y%m%d").date()
    available_start_date = dt.datetime.strptime(available_start, "%Y%m%d").date()
    available_end_date = dt.datetime.strptime(available_end, "%Y%m%d").date()
    start_gap = (available_start_date - required_start_date).days
    end_gap = (required_end_date - available_end_date).days
    # 数据可以早于/晚于目标窗口；只有目标首尾落在可用范围之外才是缺口。
    passed = (
        start_gap <= int(require_start_within_days)
        and end_gap <= int(require_end_within_days)
    )
    return {
        "passed": bool(passed),
        "required_start": str(required_start),
        "required_end": str(required_end),
        "available_start": available_start,
        "available_end": available_end,
        "start_gap_calendar_days": int(start_gap),
        "end_gap_calendar_days": int(end_gap),
        "row_count": int(len(frame)),
        "date_count": int(dates.nunique()),
        "reason": "OK" if passed else "DATE_COVERAGE_GAP",
    }
