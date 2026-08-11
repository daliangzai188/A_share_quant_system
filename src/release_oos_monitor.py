"""发布版本OOS日评日志、历史快照和周报提醒。"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from src.shadow_candidate_ledger import _date, _read_csv, _read_json, load_open_dates


NotifyFunction = Callable[..., bool]


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)


def _atomic_json(payload: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _iso_week(value: str) -> str:
    parsed = datetime.strptime(_date(value), "%Y%m%d").date()
    year, week, _ = parsed.isocalendar()
    return f"{year}-W{week:02d}"


def is_last_open_day_of_week(root: Path, signal_date: str) -> bool:
    signal_date = _date(signal_date)
    dates = load_open_dates(root)
    if signal_date not in dates:
        return False
    future = [date for date in dates if date > signal_date]
    return not future or _iso_week(future[0]) != _iso_week(signal_date)


def _float(value: object) -> float:
    try:
        result = float(value)
        return result if pd.notna(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _leg_summary(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for row in payload.get("by_leg_metrics", []) or []:
        leg = str(row.get("segment", "")).replace("策略", "")
        count = int(_float(row.get("sample_count")))
        avg = _float(row.get("avg_return"))
        parts.append(f"{leg}:{count}笔/{avg:+.2%}")
    return "，".join(parts) if parts else "各腿均无已完成样本"


def daily_log_lines(payload: Mapping[str, Any], signal_date: str) -> list[str]:
    priority = payload.get("priority_winner_metrics", {}) or {}
    minimum = int(_float(payload.get("minimum_samples_for_review")) or 20)
    winner_count = int(_float(payload.get("priority_winner_resolved_count")))
    actual_count = int(_float(payload.get("actual_complete_trade_count")))
    return [
        (
            f"[OOS日评] 发布={payload.get('release_id', '')} 信号日={_date(signal_date)} "
            f"状态={payload.get('status', '')} 评估覆盖={_float(payload.get('evaluated_coverage')):.0%} "
            f"候选={int(_float(payload.get('candidate_count')))} 已完成={int(_float(payload.get('resolved_candidate_count')))} "
            f"优先级样本={winner_count}/{minimum} 真实成交={actual_count}/{minimum}"
        ),
        (
            f"[OOS日评] 优先级组合：平均={_float(priority.get('avg_return')):+.2%} "
            f"中位数={_float(priority.get('median_return')):+.2%} "
            f"复利={_float(priority.get('compound_multiple')):.4f}倍 "
            f"最大回撤={_float(priority.get('max_drawdown')):.2%}；分腿={_leg_summary(payload)}"
        ),
        f"[OOS日评] 结论={payload.get('reason', '')}；动作={payload.get('optimization_decision', 'HOLD_RELEASE')}（不自动改策略/不阻断下单）",
    ]


def _snapshot_row(payload: Mapping[str, Any], signal_date: str) -> dict[str, Any]:
    priority = payload.get("priority_winner_metrics", {}) or {}
    actual = payload.get("actual_metrics", {}) or {}
    return {
        "release_id": str(payload.get("release_id", "")),
        "signal_date": _date(signal_date),
        "iso_week": _iso_week(signal_date),
        "status": str(payload.get("status", "")),
        "optimization_decision": str(payload.get("optimization_decision", "")),
        "signal_day_count": int(_float(payload.get("signal_day_count"))),
        "evaluated_coverage": _float(payload.get("evaluated_coverage")),
        "candidate_count": int(_float(payload.get("candidate_count"))),
        "resolved_candidate_count": int(_float(payload.get("resolved_candidate_count"))),
        "priority_winner_resolved_count": int(_float(payload.get("priority_winner_resolved_count"))),
        "priority_avg_return": _float(priority.get("avg_return")),
        "priority_median_return": _float(priority.get("median_return")),
        "priority_compound_multiple": _float(priority.get("compound_multiple")),
        "priority_max_drawdown": _float(priority.get("max_drawdown")),
        "actual_complete_trade_count": int(_float(payload.get("actual_complete_trade_count"))),
        "actual_avg_return": _float(actual.get("avg_return")),
        "actual_max_drawdown": _float(actual.get("max_drawdown")),
        "generated_at": str(payload.get("generated_at", "")),
    }


def _upsert_history(path: Path, row: Mapping[str, Any], keys: list[str]) -> pd.DataFrame:
    old = _read_csv(path)
    incoming = pd.DataFrame([dict(row)])
    combined = pd.concat([old, incoming], ignore_index=True, sort=False) if not old.empty else incoming
    for key in keys:
        combined[key] = combined[key].fillna("").astype(str)
    combined = combined.drop_duplicates(keys, keep="last").sort_values(keys).reset_index(drop=True)
    _atomic_csv(combined, path)
    return combined


def _weekly_body(payload: Mapping[str, Any], signal_date: str) -> str:
    priority = payload.get("priority_winner_metrics", {}) or {}
    pair_rows = payload.get("priority_pair_metrics", []) or []
    review_pairs = [str(row.get("challenger_leg", "")) for row in pair_rows if row.get("priority_change_evidence") == "REVIEW"]
    pair_text = "达到复核条件:" + "/".join(review_pairs) if review_pairs else "暂无腿序替换达到成对样本门槛"
    return (
        f"{_iso_week(signal_date)} 截至{_date(signal_date)}：观察{int(_float(payload.get('signal_day_count')))}个信号日，"
        f"评估覆盖{_float(payload.get('evaluated_coverage')):.0%}，候选{int(_float(payload.get('candidate_count')))}个/完成{int(_float(payload.get('resolved_candidate_count')))}个；"
        f"优先级样本{int(_float(payload.get('priority_winner_resolved_count')))}笔，平均{_float(priority.get('avg_return')):+.2%}，"
        f"中位数{_float(priority.get('median_return')):+.2%}，复利{_float(priority.get('compound_multiple')):.4f}倍，回撤{_float(priority.get('max_drawdown')):.2%}；"
        f"真实完整成交{int(_float(payload.get('actual_complete_trade_count')))}笔。{pair_text}。"
        f"结论:{payload.get('reason', '')} 动作:{payload.get('optimization_decision', 'HOLD_RELEASE')}，不会自动改策略。"
    )


def record_and_maybe_remind(
    root: Path,
    signal_date: str,
    payload: Mapping[str, Any],
    notify_func: NotifyFunction | None = None,
) -> dict[str, Any]:
    """每日落历史快照；每周最后交易日幂等推一次周报。"""

    output = root / "reports" / "oos_evaluation"
    row = _snapshot_row(payload, signal_date)
    _upsert_history(output / "release_oos_daily_history.csv", row, ["release_id", "signal_date"])
    weekly = is_last_open_day_of_week(root, signal_date)
    sent = False
    week_key = f"{row['release_id']}|{row['iso_week']}"
    if weekly:
        _upsert_history(output / "release_oos_weekly_history.csv", row, ["release_id", "iso_week"])
        state_path = root / "data" / "state" / "oos_analysis_reminder_state.json"
        state = _read_json(state_path, {})
        if str(state.get("last_week_key", "")) != week_key:
            if notify_func is None:
                from src.notify import notify as notify_func
            sent = bool(notify_func(
                "daily_summary",
                "📊 发布版本样本外周报",
                _weekly_body(payload, signal_date),
                level="active",
            ))
            if sent:
                _atomic_json({
                    "last_week_key": week_key,
                    "last_signal_date": _date(signal_date),
                    "updated_at": datetime.now().isoformat(),
                }, state_path)
    return {
        "log_lines": daily_log_lines(payload, signal_date),
        "is_weekly_report_day": weekly,
        "weekly_notification_sent": sent,
        "week_key": week_key,
    }
