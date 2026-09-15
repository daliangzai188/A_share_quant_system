#!/usr/bin/env python3
"""认证策略D当前实盘规则、历史回放证据和前向执行证据。

本脚本只读配置、代码和本地账本，不连接券商、不下单。历史信号时L2字段缺失
时必须明确输出未完成认证；实时规则即使已对齐，也不能冒充历史收益认证。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.acde_rolling_framework import FIXED_PRIORITY
from src.strategy_d_factor_rules import load_factor_release, release_signal_clock


RELEASE_PATH = ROOT / "config/strategy_d_factor_release.json"
CONFIG_PATH = ROOT / "config/config.json"
HISTORICAL_EVENT_PATH = (
    ROOT
    / "data/research/monthly_acde/20260831/strategy_d_three_year"
    / "all_reseal_signal_events.csv"
)
SIGNAL_DIR = ROOT / "reports/strategy_d"
POSITIONS_PATH = ROOT / "data/processed/positions.json"
DEFAULT_OUTPUT = ROOT / "reports/strategy_d/runtime_alignment_latest.json"
REQUIRED_HISTORICAL_L2_FIELDS = (
    "estimated_turnover_amount",
    "current_queue_amount",
)
FINAL_ORDER_STATUSES = {
    "FILLED",
    "REJECTED_ENTRY_ALIGNMENT",
    "REJECTED_LOCAL_PRICE_GUARD",
    "REJECTED_EXISTING_STRATEGY_POSITION",
    "REJECTED_INSUFFICIENT_CASH",
    "REJECTED_FILL_PROBABILITY",
    "REJECTED_TERMINAL",
    "REJECTED_BY_QMT",
    "ORDER_EXCEPTION",
    "CANCEL_REQUESTED_NO_FILL",
    "PARTIAL_FILLED_CANCEL_REQUESTED",
    "PARTIAL_FILLED_CANCEL_FAILED",
    "CANCEL_FAILED",
    "TERMINAL_NO_FILL",
    "CANCELLED_NO_FILL",
    "PARTIAL_FILLED_CANCELLED",
}


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _signal_rows(runtime_enable_date: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in sorted(SIGNAL_DIR.glob("intraday_signals_*.csv")):
        trade_date = path.stem.rsplit("_", 1)[-1]
        if len(trade_date) != 8 or trade_date < runtime_enable_date:
            continue
        try:
            frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        except Exception:
            continue
        if frame.empty:
            continue
        frame["evidence_trade_date"] = trade_date
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True, sort=False)
    if "signal_type" in result.columns:
        result = result[result["signal_type"].astype(str).eq("BUY")].copy()
    return result.reset_index(drop=True)


def _forward_summary(runtime_enable_date: str) -> dict[str, Any]:
    signals = _signal_rows(runtime_enable_date)
    if signals.empty:
        return {
            "signal_count": 0,
            "submitted_order_count": 0,
            "reliable_fill_gate_count": 0,
            "sample_count_evidence_complete_count": 0,
            "terminal_order_evidence_count": 0,
            "filled_order_count": 0,
            "closed_trade_count": 0,
            "unresolved_order_count": 0,
            "status": "NO_FORWARD_SIGNAL_YET",
        }

    order_ids = signals.get("order_id", pd.Series("", index=signals.index)).astype(str).str.strip()
    submitted = order_ids.ne("")
    reliable = signals.get(
        "fill_reliable", pd.Series(False, index=signals.index)
    ).map(_as_bool)
    sample_counts = pd.to_numeric(
        signals.get("fill_sample_count", pd.Series(float("nan"), index=signals.index)),
        errors="coerce",
    )
    minimums = pd.to_numeric(
        signals.get("fill_min_group_samples", pd.Series(float("nan"), index=signals.index)),
        errors="coerce",
    )
    sample_complete = sample_counts.notna() & minimums.notna() & sample_counts.ge(minimums)
    statuses = signals.get(
        "order_status", pd.Series("", index=signals.index)
    ).astype(str)
    terminal = statuses.isin(FINAL_ORDER_STATUSES)
    filled_qty = pd.to_numeric(
        signals.get("filled_qty", pd.Series(0, index=signals.index)),
        errors="coerce",
    ).fillna(0)

    positions = _read_json(POSITIONS_PATH, [])
    closed_order_ids = {
        str(row.get("order_id", ""))
        for row in positions
        if isinstance(row, dict)
        and str(row.get("strategy_leg", "")).upper() == "D"
        and str(row.get("buy_date", "")) >= runtime_enable_date
        and str(row.get("status", "")).lower() == "closed"
    }
    submitted_ids = set(order_ids[submitted].tolist())
    unresolved = submitted & ~terminal
    status = (
        "FORWARD_CLOSED_TRADES_AVAILABLE"
        if closed_order_ids.intersection(submitted_ids)
        else (
            "FORWARD_ORDER_EVIDENCE_INCOMPLETE"
            if bool(unresolved.any())
            else "FORWARD_EXIT_SAMPLE_PENDING"
        )
    )
    return {
        "signal_count": int(len(signals)),
        "submitted_order_count": int(submitted.sum()),
        "reliable_fill_gate_count": int(reliable.sum()),
        "sample_count_evidence_complete_count": int(sample_complete.sum()),
        "terminal_order_evidence_count": int((submitted & terminal).sum()),
        "filled_order_count": int((submitted & filled_qty.gt(0)).sum()),
        "closed_trade_count": int(len(closed_order_ids.intersection(submitted_ids))),
        "unresolved_order_count": int(unresolved.sum()),
        "status": status,
    }


def build_certification() -> dict[str, Any]:
    release = load_factor_release(RELEASE_PATH)
    config = _read_json(CONFIG_PATH, {})
    strategy_d = config.get("strategy_d", {})
    live_trade = config.get("live_trade", {})
    fill_model = config.get("fill_model", {})
    entry_alignment = release.get("entry_alignment", {})
    enable_date = str(entry_alignment.get("runtime_enable_approved_at", "")).replace("-", "")[:8]
    if len(enable_date) != 8:
        enable_date = str(release.get("effective_from", ""))[:8]

    historical_columns = (
        pd.read_csv(HISTORICAL_EVENT_PATH, nrows=0).columns.tolist()
        if HISTORICAL_EVENT_PATH.exists()
        else []
    )
    missing_l2 = [
        field for field in REQUIRED_HISTORICAL_L2_FIELDS
        if field not in historical_columns
    ]
    monitor_source = (ROOT / "scripts/monitor_strategy_d_intraday.py").read_text(
        encoding="utf-8"
    )
    optimizer_source = (ROOT / "scripts/optimize_strategy_d_factor_union.py").read_text(
        encoding="utf-8"
    )
    release_clock = release_signal_clock(release, full_session_last_hhmm=1454)

    checks = {
        "priority_is_a_c_e_d": tuple(FIXED_PRIORITY) == ("A", "C", "E", "D"),
        "formal_release_is_v15": str(release.get("release_id", ""))
        == "D_ACTIVE_LT20_OPEN2_EXTENSION_20260831_V15",
        "formal_profile_count_is_15": len(release.get("profiles", [])) == 15,
        "selection_policy_aligned": release.get("selection_policy")
        == "EARLIEST_RESEAL_THEN_OPEN2_THEN_CODE",
        "completed_minute_clock_aligned": bool(
            release_clock.constrained_by_profiles
            and release_clock.last_signal_hhmm == 1000
            and "QMT_COMPLETED_1M_CLOSE" in monitor_source
        ),
        "shared_factor_rules_used_by_backtest_and_live": bool(
            "from src.strategy_d_factor_rules import" in monitor_source
            and "from src.strategy_d_factor_rules import" in optimizer_source
        ),
        "position_target_is_82_5pct": abs(float(strategy_d.get("position_pct", 0)) - 0.825) < 1e-12,
        "position_hard_cap_is_85pct": abs(float(live_trade.get("max_position_pct", 0)) - 0.85) < 1e-12,
        "fill_threshold_is_80pct": abs(float(strategy_d.get("min_fill_probability", 0)) - 0.8) < 1e-12,
        "fill_group_minimum_sample_enforced": bool(
            int(fill_model.get("min_group_samples", 0)) > 0
            and "result_is_reliable(result)" in monitor_source
        ),
        "actual_order_amount_used_by_fill_gate": bool(
            "planned_buy_amount=capacity.actual_amount" in monitor_source
        ),
        "t_plus_2_exit_recorded": "next_trade_day(buy_date, 2)" in monitor_source,
        "final_order_result_reconciled": "_update_signal_order_result(" in monitor_source,
        "runtime_new_buy_explicitly_enabled": bool(
            entry_alignment.get("runtime_new_buy_enabled", False)
        ),
        "historical_signal_time_l2_available": not missing_l2,
        "historical_release_gate_passed": bool(release.get("release_gate_passed", False)),
    }
    runtime_check_names = [
        name for name in checks
        if name not in {
            "historical_signal_time_l2_available",
            "historical_release_gate_passed",
        }
    ]
    runtime_rules_aligned = all(bool(checks[name]) for name in runtime_check_names)
    historical_certified = bool(
        runtime_rules_aligned
        and checks["historical_signal_time_l2_available"]
        and checks["historical_release_gate_passed"]
    )
    forward = _forward_summary(enable_date)
    forward_closed_samples = int(forward["closed_trade_count"])
    decision = (
        "FULL_HISTORICAL_AND_RUNTIME_ALIGNMENT_CERTIFIED"
        if historical_certified
        else (
            "RUNTIME_RULES_ALIGNED_FORWARD_VALIDATION_IN_PROGRESS"
            if runtime_rules_aligned
            else "RUNTIME_ALIGNMENT_FAILED"
        )
    )
    return {
        "schema_version": 1,
        "strategy": "D",
        "decision": decision,
        "formal_release_id": release.get("release_id", ""),
        "formal_rule_modified": False,
        "runtime_rules_aligned": runtime_rules_aligned,
        "historical_performance_certified": historical_certified,
        "historical_data": {
            "source": str(HISTORICAL_EVENT_PATH.relative_to(ROOT)),
            "required_signal_time_l2_fields": list(REQUIRED_HISTORICAL_L2_FIELDS),
            "missing_signal_time_l2_fields": missing_l2,
            "certified_d_trade_count": 0 if missing_l2 else None,
            "old_28_trade_metrics_reusable": False,
        },
        "forward_evidence_since": enable_date,
        "forward_evidence": forward,
        "checks": checks,
        "frozen_execution": {
            "priority": list(FIXED_PRIORITY),
            "position_pct": float(strategy_d.get("position_pct", 0)),
            "max_position_pct": float(live_trade.get("max_position_pct", 0)),
            "min_fill_probability": float(strategy_d.get("min_fill_probability", 0)),
            "min_group_samples": int(fill_model.get("min_group_samples", 0)),
            "signal_last_hhmm": int(release_clock.last_signal_hhmm),
            "exit": "T+2",
        },
        "evidence_hashes": {
            str(path.relative_to(ROOT)): _sha256(path)
            for path in (
                RELEASE_PATH,
                CONFIG_PATH,
                ROOT / "src/strategy_d_factor_rules.py",
                ROOT / "src/fill_model.py",
                ROOT / "scripts/monitor_strategy_d_intraday.py",
                ROOT / "src/acde_rolling_framework.py",
            )
        },
        "next_certification_condition": (
            "补齐三年信号时L2队列金额后重放，或积累冻结规则下真实前向完整平仓样本；"
            f"当前完整前向平仓样本={forward_closed_samples}。"
        ),
    }


def main() -> int:
    report = build_certification()
    DEFAULT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    print(f"报告：{DEFAULT_OUTPUT}")
    return 0 if report["runtime_rules_aligned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
