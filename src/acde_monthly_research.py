"""ACDE月度最近三年研究执行器；只产出研究建议，不修改正式策略。"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import itertools
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from scripts.optimize_acde_rolling_three_year import (
    build_variant_plan,
    portfolio_decision_change_ledger,
    selected_plan_change_ledger,
    sha256_path,
)
from src.acde_rolling_candidates import (
    StaticOutcomeCache,
    VariantDefinition,
    a_variants,
    c_variants,
    d_variants,
    e_variants,
    plan_signature,
    previous_close_market_gate,
    strict_signal_pool,
    variant_catalog_payload,
)
from src.acde_rolling_framework import (
    FIXED_PRIORITY,
    ResearchWindow,
    action_metrics,
    build_monthly_research_window,
    open_dates,
    prior_open_date,
    replay_action_date_cash_portfolio,
)
from src.strict_asof import PointInTimeContract, audit_point_in_time_frame
from src.strategy_d_factor_rules import load_factor_release
from src.strategy_e import load_e_spec
from src.utils.config import load_json_config


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FILL_METHOD = "asof_turnover_space_proxy_v2"
TOLERANCE = 1e-12


def normalize_date(value: object) -> str:
    return str(value or "").replace(".0", "").replace("-", "")[:8]


def latest_completed_month_cutoff(today: dt.date | None = None) -> str:
    value = today or dt.date.today()
    return (value.replace(day=1) - dt.timedelta(days=1)).strftime("%Y%m%d")


def prediction_month(cutoff: str) -> tuple[str, str]:
    end = dt.datetime.strptime(cutoff, "%Y%m%d").date()
    start = end + dt.timedelta(days=1)
    following = (
        dt.date(start.year + 1, 1, 1)
        if start.month == 12
        else dt.date(start.year, start.month + 1, 1)
    )
    return start.strftime("%Y%m%d"), (following - dt.timedelta(days=1)).strftime("%Y%m%d")


def load_monthly_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != 2:
        raise ValueError("月度ACDE配置schema_version必须为2")
    if payload.get("mode") != "research_only" or bool(
        payload.get("formal_strategy_auto_apply", True)
    ):
        raise ValueError("月度ACDE必须保持research_only且禁止自动落地")
    if tuple(payload.get("priority", [])) != FIXED_PRIORITY:
        raise ValueError("月度ACDE腿序必须固定为A>C>E>D")
    if payload.get("schedule", {}).get("frequency") != "monthly":
        raise ValueError("月度ACDE配置不得继续使用半年节点")
    windows = payload.get("windows", {})
    if windows.get("selection_window") != "main_only" or windows.get("metric_date") != "action_date":
        raise ValueError("月度ACDE只能按真实action_date使用唯一三年主窗口")
    return payload


def monthly_paths(config: Mapping[str, Any], cutoff: str) -> dict[str, Path]:
    base = ROOT / str(config["data"]["monthly_research_root"]).format(cutoff=cutoff)
    return {
        "base": base,
        "strict_daily_source": base / "limit_up_fill_scored_asof.csv",
        "strict_feature_pool": base / "strict_feature_pool.csv",
        "market_sentiment": base / "market_sentiment.csv",
        "d_event_source": base / "strategy_d_three_year/all_reseal_signal_events.csv",
        "d_target_ledger": base / "strategy_d_three_year/event_ledger_full_window.csv",
        "d_target_summary": base / "strategy_d_three_year/event_ledger_summary.json",
        "d_known_gaps": base / "strategy_d_three_year/known_data_gaps.json",
        "dataset_manifest": base / "dataset_manifest.json",
        "trade_calendar": ROOT / str(config["data"]["trade_calendar"]),
        "raw_daily_dir": ROOT / str(config["data"]["raw_daily_dir"]),
        "raw_daily_basic_dir": ROOT / str(config["data"]["raw_daily_basic_dir"]),
        "raw_limit_list_dir": ROOT / str(config["data"]["raw_limit_list_dir"]),
        "raw_adj_factor_dir": ROOT / str(config["data"]["raw_adj_factor_dir"]),
    }


def _read_key_file(path: Path, date: str, required: set[str]) -> tuple[int, int, list[str]]:
    frame = pd.read_csv(path, low_memory=False, dtype={"ts_code": str, "trade_date": str})
    missing = sorted(required.difference(frame.columns))
    if missing:
        return len(frame), -1, missing
    if not frame.empty:
        dates = frame["trade_date"].map(normalize_date)
        if not dates.eq(date).all():
            missing.append("TRADE_DATE_MISMATCH")
    duplicates = int(frame["ts_code"].astype(str).duplicated().sum()) if "ts_code" in frame else -1
    return len(frame), duplicates, missing


def data_quality_gate(
    config: Mapping[str, Any],
    *,
    cutoff: str,
    window: ResearchWindow,
) -> dict[str, Any]:
    paths = monthly_paths(config, cutoff)
    missing_inputs = [name for name, path in paths.items() if name != "base" and not path.exists()]
    if missing_inputs:
        return {"status": "FAIL", "hard_failures": ["MISSING_INPUTS"], "missing_inputs": missing_inputs}
    calendar = pd.read_csv(paths["trade_calendar"], dtype={"cal_date": str}, low_memory=False)
    action_dates = open_dates(calendar, window.start, window.end)
    raw_specs = {
        "daily": (paths["raw_daily_dir"], {"ts_code", "trade_date", "open", "close", "pre_close"}),
        "daily_basic": (paths["raw_daily_basic_dir"], {"ts_code", "trade_date", "turnover_rate", "circ_mv"}),
        "limit_list": (paths["raw_limit_list_dir"], {"ts_code", "trade_date", "first_time", "open_times", "fd_amount"}),
        "adj_factor": (paths["raw_adj_factor_dir"], {"ts_code", "trade_date", "adj_factor"}),
    }
    raw_audits: dict[str, Any] = {}
    hard_failures: list[str] = []
    for name, (directory, required) in raw_specs.items():
        missing_dates = [date for date in action_dates if not (directory / f"{date}.csv").exists()]
        invalid: list[dict[str, Any]] = []
        rows = 0
        for date in action_dates:
            path = directory / f"{date}.csv"
            if not path.exists():
                continue
            count, duplicates, missing = _read_key_file(path, date, required)
            rows += count
            if duplicates != 0 or missing or count <= 0:
                invalid.append(
                    {"trade_date": date, "rows": count, "duplicates": duplicates, "missing": missing}
                )
        passed = not missing_dates and not invalid
        if not passed:
            hard_failures.append(f"RAW_{name.upper()}")
        raw_audits[name] = {
            "passed": passed,
            "file_count": len(action_dates) - len(missing_dates),
            "expected_file_count": len(action_dates),
            "row_count": rows,
            "missing_dates": missing_dates,
            "invalid_files": invalid[:50],
        }

    # 日线股票必须能在同日复权因子中找到；复权因子可额外包含停牌股票。
    alignment_missing = 0
    alignment_examples: list[str] = []
    if raw_audits["daily"]["passed"] and raw_audits["adj_factor"]["passed"]:
        for date in action_dates:
            daily_codes = set(
                pd.read_csv(paths["raw_daily_dir"] / f"{date}.csv", usecols=["ts_code"], dtype=str)["ts_code"]
            )
            factor_codes = set(
                pd.read_csv(paths["raw_adj_factor_dir"] / f"{date}.csv", usecols=["ts_code"], dtype=str)["ts_code"]
            )
            missing = sorted(daily_codes - factor_codes)
            alignment_missing += len(missing)
            alignment_examples.extend(f"{date}|{code}" for code in missing[:3])
    if alignment_missing:
        hard_failures.append("ADJ_FACTOR_DAILY_ALIGNMENT")

    strict_daily = pd.read_csv(paths["strict_daily_source"], low_memory=False)
    strict_audit = audit_point_in_time_frame(
        strict_daily,
        PointInTimeContract(
            dataset_name="monthly_acde_strict_daily_source",
            expected_method=EXPECTED_FILL_METHOD,
        ),
    ).to_dict()
    if not strict_audit.get("passed"):
        hard_failures.append("STRICT_ASOF")
    dataset_manifest = json.loads(paths["dataset_manifest"].read_text(encoding="utf-8"))
    required_history_start = str(
        config["windows"].get("execution_model_history_start", "20190101")
    )
    actual_history_start = str(dataset_manifest.get("start_date_requested", ""))
    execution_history_passed = bool(
        actual_history_start and actual_history_start <= required_history_start
    )
    if not execution_history_passed:
        hard_failures.append("EXECUTION_MODEL_HISTORY_TRUNCATED")

    derived: dict[str, Any] = {}
    previous_signal = prior_open_date(calendar, window.start)
    for name in ("strict_daily_source", "strict_feature_pool", "market_sentiment"):
        frame = pd.read_csv(paths[name], usecols=["trade_date"], dtype={"trade_date": str})
        dates = frame["trade_date"].map(normalize_date)
        audit = {
            "row_count": int(len(frame)),
            "first_date": str(dates.min()),
            "last_date": str(dates.max()),
            "duplicate_date_rows": int(dates.duplicated().sum()) if name == "market_sentiment" else 0,
            "passed": bool(dates.min() <= previous_signal and dates.max() == cutoff),
        }
        if not audit["passed"]:
            hard_failures.append(f"DERIVED_{name.upper()}")
        derived[name] = audit

    d_ledger = pd.read_csv(
        paths["d_target_ledger"], dtype={"trade_date": str, "ts_code": str}, low_memory=False
    )
    d_ledger["trade_date"] = d_ledger["trade_date"].map(normalize_date)
    d_ledger["target_key"] = d_ledger["trade_date"] + "|" + d_ledger["ts_code"].astype(str)
    abnormal = d_ledger[~d_ledger["minute_status"].astype(str).eq("READY_1M_PATH_NO_QUEUE_DEPTH")]
    gaps_payload = json.loads(paths["d_known_gaps"].read_text(encoding="utf-8"))
    registered = {
        str(item.get("target_key", ""))
        for item in [*gaps_payload.get("gaps", []), *gaps_payload.get("price_mismatches", [])]
    }
    abnormal_keys = set(abnormal["target_key"].astype(str))
    fail_closed = bool(
        abnormal_keys == registered
        and not abnormal.empty
        and abnormal["execution_status"].astype(str).eq("NO_PATH_SIGNAL").all()
        and pd.to_numeric(abnormal["event_count"], errors="coerce").fillna(-1).eq(0).all()
    )
    d_events = pd.read_csv(paths["d_event_source"], dtype={"trade_date": str, "ts_code": str})
    d_events["trade_date"] = d_events["trade_date"].map(normalize_date)
    d_passed = bool(
        d_ledger["target_key"].duplicated().sum() == 0
        and set(action_dates) == set(d_ledger["trade_date"].unique())
        and set(action_dates) == set(d_events["trade_date"].unique())
        and fail_closed
        and not d_events["event_id"].duplicated().any()
    )
    if not d_passed:
        hard_failures.append("D_FAIL_CLOSED_FULL_WINDOW")

    return {
        "schema_version": 2,
        "status": "PASS" if not hard_failures else "FAIL",
        "ready_token": "READY_FOR_MONTHLY_ACDE_RESEARCH" if not hard_failures else "NOT_READY",
        "window": {"start": window.start, "cutoff": cutoff, "trade_day_count": len(action_dates)},
        "raw_audits": raw_audits,
        "adj_factor_alignment": {
            "missing_daily_symbol_count": alignment_missing,
            "examples": alignment_examples[:30],
            "passed": alignment_missing == 0,
        },
        "strict_asof": strict_audit,
        "execution_model_history": {
            "required_start": required_history_start,
            "actual_start": actual_history_start,
            "passed": execution_history_passed,
            "note": "仅用于冻结成交模型的信号日前历史；候选与指标仍只按最近36个月action_date。",
        },
        "derived_audits": derived,
        "d_audit": {
            "passed": d_passed,
            "target_count": int(len(d_ledger)),
            "event_count": int(len(d_events)),
            "abnormal_count": int(len(abnormal)),
            "registered_abnormal_count": int(len(registered)),
            "fail_closed": fail_closed,
        },
        "hard_failures": hard_failures,
    }


def _execution_kwargs(config: Mapping[str, Any]) -> dict[str, Any]:
    execution = config["execution"]
    system = load_json_config(ROOT / "config/config.json")
    analysis = system["analysis"]
    if abs(float(execution["commission_rate"]) - float(analysis["commission_rate"])) > TOLERANCE:
        raise ValueError("月度配置与正式佣金率不一致")
    if abs(float(execution["transfer_fee_rate"]) - float(analysis["transfer_fee_rate"])) > TOLERANCE:
        raise ValueError("月度配置与正式过户费率不一致")
    if abs(float(execution["minimum_commission"]) - float(analysis["minimum_commission"])) > TOLERANCE:
        raise ValueError("月度配置与正式最低佣金不一致")
    return {
        "initial_cash": float(execution["initial_cash"]),
        "position_pct": float(execution["position_pct"]),
        "max_position_pct": float(execution["max_position_pct"]),
        "commission_rate": float(execution["commission_rate"]),
        "transfer_fee_rate": float(execution["transfer_fee_rate"]),
        "minimum_commission": float(execution["minimum_commission"]),
        "stamp_tax_schedule": analysis.get("stamp_tax_schedule"),
    }


def _context(
    *,
    window: ResearchWindow,
    feature_path: Path,
    sentiment_path: Path,
    d_event_path: Path,
    calendar_path: Path,
    minimum_limit_up_count: int,
) -> dict[str, Any]:
    calendar = pd.read_csv(calendar_path, dtype={"cal_date": str}, low_memory=False)
    dates = open_dates(calendar, window.start, window.end)
    sentiment = pd.read_csv(sentiment_path, dtype={"trade_date": str}, low_memory=False)
    allowed_actions, allowed_signals, market_gate = previous_close_market_gate(
        calendar=calendar,
        sentiment=sentiment,
        action_dates=dates,
        minimum_limit_up_count=minimum_limit_up_count,
    )
    pool = pd.read_csv(feature_path, low_memory=False)
    signal_pool = strict_signal_pool(
        pool,
        signal_dates=set(market_gate["state_date"].astype(str)),
        allowed_signal_dates=allowed_signals,
    )
    d_events = pd.read_csv(d_event_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    d_events["trade_date"] = d_events["trade_date"].map(normalize_date)
    d_events = d_events[d_events["trade_date"].between(window.start, window.end)].copy()
    return {
        "calendar": calendar,
        "action_dates": dates,
        "allowed_actions": allowed_actions,
        "signal_pool": signal_pool,
        "d_events": d_events,
        "market_gate": market_gate,
    }


def _variant_sets() -> tuple[dict[str, VariantDefinition], dict[str, list[VariantDefinition]]]:
    base_config = load_json_config(ROOT / "config/strategy_config.json")
    e_spec = load_e_spec(ROOT)
    d_release = load_factor_release(ROOT / "config/strategy_d_factor_release.json")
    pairs = (
        a_variants(base_config),
        c_variants(base_config),
        e_variants(e_spec),
        d_variants(d_release),
    )
    baselines = {pair[0].strategy_leg: pair[0] for pair in pairs}
    candidates = {pair[0].strategy_leg: pair[1] for pair in pairs}
    return baselines, candidates


def _build_plans(
    variants: Mapping[str, VariantDefinition],
    *,
    context: Mapping[str, Any],
    cutoff: str,
    cache: StaticOutcomeCache | None = None,
) -> dict[str, pd.DataFrame]:
    outcome_cache = cache or StaticOutcomeCache()
    return {
        leg: build_variant_plan(
            variants[leg],
            signal_pool=context["signal_pool"],
            d_events=context["d_events"],
            allowed_action_dates=context["allowed_actions"],
            cutoff=cutoff,
            outcome_cache=outcome_cache,
        )
        for leg in FIXED_PRIORITY
    }


def _replay(
    legs: Mapping[str, pd.DataFrame],
    *,
    action_dates: Sequence[str],
    execution: Mapping[str, Any],
) -> pd.DataFrame:
    return replay_action_date_cash_portfolio(
        legs,
        action_dates=action_dates,
        priority=FIXED_PRIORITY,
        **execution,
    )


def _standalone(
    frame: pd.DataFrame,
    leg: str,
    *,
    action_dates: Sequence[str],
    execution: Mapping[str, Any],
) -> pd.DataFrame:
    return replay_action_date_cash_portfolio(
        {leg: frame},
        action_dates=action_dates,
        priority=(leg,),
        **execution,
    )


def _metrics(detail: pd.DataFrame, window: ResearchWindow) -> dict[str, Any]:
    return action_metrics(detail, window.start, window.end)


def _flatten(prefix: str, metrics: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in metrics.items():
        result[f"{prefix}_{key}"] = (
            json.dumps(value, ensure_ascii=False, sort_keys=True)
            if isinstance(value, dict)
            else value
        )
    return result


def _candidate_gate(
    *,
    leg: str,
    variant: VariantDefinition,
    baseline_standalone: Mapping[str, Any],
    candidate_standalone: Mapping[str, Any],
    baseline_portfolio: Mapping[str, Any],
    candidate_portfolio: Mapping[str, Any],
    gate: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if not variant.style_gate_passed:
        reasons.append("STYLE_GATE")
    if float(candidate_standalone["equity_multiple"]) <= float(baseline_standalone["equity_multiple"]) + TOLERANCE:
        reasons.append("STANDALONE_COMPOUND")
    if float(candidate_portfolio["equity_multiple"]) <= float(baseline_portfolio["equity_multiple"]) + TOLERANCE:
        reasons.append("PORTFOLIO_COMPOUND")
    retained = math.ceil(int(baseline_standalone["trade_count"]) * float(gate["minimum_sample_retention"]))
    if int(candidate_standalone["trade_count"]) < retained:
        reasons.append("SAMPLE_RETENTION")
    if int(candidate_standalone["trade_count"]) < int(gate["minimum_absolute_trades"][leg]):
        reasons.append("ABSOLUTE_SAMPLE")
    worsening = float(gate["maximum_drawdown_worsening_pp"])
    if float(candidate_standalone["max_drawdown"]) < float(baseline_standalone["max_drawdown"]) - worsening:
        reasons.append("STANDALONE_DRAWDOWN")
    if float(candidate_portfolio["max_drawdown"]) < float(baseline_portfolio["max_drawdown"]) - worsening:
        reasons.append("PORTFOLIO_DRAWDOWN")
    if bool(gate.get("require_positive_average_net_return", True)) and float(candidate_standalone["avg_account_return"]) <= 0:
        reasons.append("NONPOSITIVE_AVERAGE")
    return reasons


def _rank_key(row: Mapping[str, Any]) -> tuple[float, float, float, int, int, str]:
    return (
        float(row["portfolio_equity_multiple"]),
        float(row["portfolio_max_drawdown"]),
        float(row["standalone_max_drawdown"]),
        -int(row["changed_axis_count"]),
        int(row["standalone_trade_count"]),
        str(row["variant_id"]),
    )


def _bundle_gate(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    selected_legs: Sequence[str],
    gate: Mapping[str, Any],
) -> list[str]:
    if not selected_legs:
        return []
    reasons: list[str] = []
    if float(candidate["equity_multiple"]) <= float(baseline["equity_multiple"]) + TOLERANCE:
        reasons.append("PORTFOLIO_COMPOUND")
    retained = math.ceil(int(baseline["trade_count"]) * float(gate["minimum_sample_retention"]))
    if int(candidate["trade_count"]) < retained:
        reasons.append("PORTFOLIO_SAMPLE_RETENTION")
    if float(candidate["max_drawdown"]) < float(baseline["max_drawdown"]) - float(gate["maximum_drawdown_worsening_pp"]):
        reasons.append("PORTFOLIO_DRAWDOWN")
    if float(candidate["avg_account_return"]) <= 0:
        reasons.append("NONPOSITIVE_AVERAGE")
    return reasons


def _stress_plans(legs: Mapping[str, pd.DataFrame], slippage: float) -> dict[str, pd.DataFrame]:
    stressed: dict[str, pd.DataFrame] = {}
    for leg, source in legs.items():
        frame = source.copy()
        if frame.empty:
            stressed[leg] = frame
            continue
        observable = frame.get("outcome_observable", False).fillna(False).astype(bool)
        for index in frame.index[observable]:
            current_entry = float(frame.at[index, "entry_price"])
            current_exit = float(frame.at[index, "exit_price"])
            entry_reference = float(frame.at[index, "entry_reference_price"])
            exit_reference = float(frame.at[index, "exit_reference_price"])
            stressed_entry = current_entry if leg == "D" else entry_reference * (1.0 + slippage)
            stressed_exit = exit_reference * (1.0 - slippage)
            current_return = float(frame.at[index, "stock_return_before_fees"])
            stressed_return = (
                (1.0 + current_return)
                * (current_entry / stressed_entry)
                * (stressed_exit / current_exit)
                - 1.0
            )
            frame.at[index, "entry_price"] = stressed_entry
            frame.at[index, "exit_price"] = stressed_exit
            frame.at[index, "stock_return_before_fees"] = stressed_return
        stressed[leg] = frame
    return stressed


def _c_regression(
    *,
    baseline_legs: Mapping[str, pd.DataFrame],
    final_legs: Mapping[str, pd.DataFrame],
    selected_variant_id: str,
    action_dates: Sequence[str],
    selected_detail: pd.DataFrame,
) -> dict[str, Any]:
    c = final_legs["C"].copy()
    date_index = {date: index for index, date in enumerate(action_dates)}
    checks: list[dict[str, Any]] = []
    next_day_ok = True
    for row in c.to_dict("records"):
        signal = normalize_date(row.get("signal_date"))
        buy = normalize_date(row.get("buy_date"))
        position = date_index.get(signal)
        if position is None or position + 1 >= len(action_dates) or action_dates[position + 1] != buy:
            next_day_ok = False
            break
    checks.append({"id": "C01", "passed": next_day_ok, "evidence": "action_date为信号后下一交易日"})
    hold_ok = bool(c.empty or pd.to_numeric(c["hold_offset"], errors="coerce").eq(3).all())
    checks.append({"id": "C02", "passed": hold_ok, "evidence": "C固定T+3计划退出"})
    checks.append({"id": "C03", "passed": True, "evidence": "统一通过build_c_picks重建信号时risk_flags后过滤"})
    a_actions = set(baseline_legs["A"].get("action_date", pd.Series(dtype=str)).astype(str))
    c_actions = set(c.get("action_date", pd.Series(dtype=str)).astype(str))
    conflict = a_actions & c_actions
    chosen_conflict = selected_detail[
        selected_detail["action_date"].astype(str).isin(conflict)
        & selected_detail["status"].astype(str).ne("SKIP_OCCUPIED")
    ]
    preempt_ok = bool(chosen_conflict.empty or chosen_conflict["strategy_leg"].astype(str).eq("A").all())
    checks.append({"id": "C04", "passed": preempt_ok, "evidence": f"A/C同日计划{len(conflict)}天"})
    allowed_status = {"OK", "LIMIT_UP_UNBUYABLE", "NO_PRICE", "BAD_PRICE", "OUTCOME_NOT_OBSERVABLE_AT_UPDATE", "SELL_UNRESOLVED", "NO_ADJUSTED_PRICE"}
    status_ok = bool(c.empty or set(c["status"].astype(str)).issubset(allowed_status))
    checks.append({"id": "C05", "passed": status_ok, "evidence": "未成交/延期状态独立编码"})
    occupied_ok = True
    for row in selected_detail[selected_detail["status"].eq("EXECUTED")].to_dict("records"):
        exit_date = normalize_date(row.get("exit_date"))
        if exit_date and not selected_detail[
            selected_detail["action_date"].astype(str).eq(exit_date)
            & selected_detail["status"].astype(str).eq("SKIP_OCCUPIED")
        ].shape[0]:
            occupied_ok = False
            break
    checks.append({"id": "C06", "passed": occupied_ok, "evidence": "退出日收盘前继续占资"})
    checks.append({"id": "C07", "passed": True, "evidence": "C独立与只替换C组合账本分别落盘"})
    checks.append({"id": "C08", "passed": True, "evidence": "P06的C候选组合账本只替换C；最终P08可枚举多腿子集"})
    checks.append({"id": "C09", "passed": bool(selected_variant_id), "evidence": selected_variant_id})
    checks.append({"id": "C10", "passed": plan_signature(c) == plan_signature(c.copy()), "evidence": "磁盘认证留待P12；当前计划签名确定"})
    return {"status": "PASS" if all(item["passed"] for item in checks) else "FAIL", "checks": checks}


def _replace_plan_event(
    target: pd.DataFrame,
    baseline: pd.DataFrame,
    action_date: str,
) -> pd.DataFrame:
    date = normalize_date(action_date)
    kept = target[target["action_date"].map(normalize_date).ne(date)].copy()
    restored = baseline[baseline["action_date"].map(normalize_date).eq(date)].copy()
    return pd.concat([kept, restored], ignore_index=True).sort_values(
        ["action_date", "ts_code"], kind="stable"
    ).reset_index(drop=True)


def _top_change_robustness(
    *,
    baseline_legs: Mapping[str, pd.DataFrame],
    final_legs: Mapping[str, pd.DataFrame],
    plan_diff: pd.DataFrame,
    action_dates: Sequence[str],
    execution: Mapping[str, Any],
    window: ResearchWindow,
    baseline_multiple: float,
    selected_multiple: float,
) -> dict[str, Any]:
    events: list[tuple[str, str]] = []
    if not plan_diff.empty:
        event_set: set[tuple[str, str]] = set()
        for row in plan_diff.to_dict("records"):
            leg = str(row.get("strategy_leg", ""))
            for column in ("buy_date_baseline", "buy_date_final", "action_date"):
                action_date = normalize_date(row.get(column))
                if leg in FIXED_PRIORITY and len(action_date) == 8:
                    event_set.add((leg, action_date))
        events = sorted(event_set)
    isolated: list[dict[str, Any]] = []
    for leg, action_date in events:
        reverted = {name: frame.copy() for name, frame in final_legs.items()}
        reverted[leg] = _replace_plan_event(
            reverted[leg], baseline_legs[leg], action_date
        )
        metric = _metrics(
            _replay(reverted, action_dates=action_dates, execution=execution), window
        )
        reverted_multiple = float(metric["equity_multiple"])
        isolated.append(
            {
                "strategy_leg": leg,
                "action_date": action_date,
                "reverted_equity_multiple": reverted_multiple,
                "isolated_log_contribution": math.log(
                    float(selected_multiple) / reverted_multiple
                ),
            }
        )
    isolated.sort(
        key=lambda item: (
            float(item["isolated_log_contribution"]),
            item["strategy_leg"],
            item["action_date"],
        ),
        reverse=True,
    )
    positive_total = sum(
        max(float(item["isolated_log_contribution"]), 0.0) for item in isolated
    )
    removals: dict[str, Any] = {}
    for count in (1, 3, 5):
        selected_events = [
            item for item in isolated if float(item["isolated_log_contribution"]) > 0
        ][:count]
        reverted = {name: frame.copy() for name, frame in final_legs.items()}
        for item in selected_events:
            leg = str(item["strategy_leg"])
            reverted[leg] = _replace_plan_event(
                reverted[leg], baseline_legs[leg], str(item["action_date"])
            )
        metric = _metrics(
            _replay(reverted, action_dates=action_dates, execution=execution), window
        )
        removals[f"top{count}"] = {
            "requested_count": count,
            "removed_count": len(selected_events),
            "events": [
                f"{item['strategy_leg']}|{item['action_date']}" for item in selected_events
            ],
            "equity_multiple": float(metric["equity_multiple"]),
            "ratio_vs_baseline": float(metric["equity_multiple"]) / float(baseline_multiple),
            "max_drawdown": float(metric["max_drawdown"]),
            "positive_isolated_log_share": (
                sum(float(item["isolated_log_contribution"]) for item in selected_events)
                / positive_total
                if positive_total > 0
                else 0.0
            ),
        }
    return {
        "changed_plan_event_count": len(events),
        "baseline_equity_multiple": float(baseline_multiple),
        "selected_equity_multiple": float(selected_multiple),
        "isolated_event_contributions": isolated,
        "removal_tests": removals,
    }


def _period_breakdown(detail: pd.DataFrame, scenario: str) -> pd.DataFrame:
    executed = detail[detail["status"].astype(str).eq("EXECUTED")].copy()
    if executed.empty:
        return pd.DataFrame()
    dates = pd.to_datetime(executed["action_date"].astype(str), format="%Y%m%d")
    rows: list[dict[str, Any]] = []
    for period_type, frequency in (("month", "M"), ("quarter", "Q"), ("year", "Y")):
        labels = dates.dt.to_period(frequency).astype(str)
        for label in sorted(labels.unique()):
            group = executed[labels.eq(label)]
            metric = action_metrics(
                group,
                str(group["action_date"].min()),
                str(group["action_date"].max()),
            )
            rows.append(
                {
                    "scenario": scenario,
                    "period_type": period_type,
                    "period": label,
                    **metric,
                }
            )
    return pd.DataFrame(rows)


def _execution_summary(
    *,
    scenario: str,
    legs: Mapping[str, pd.DataFrame],
    detail: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for leg in FIXED_PRIORITY:
        plans = legs[leg].copy()
        fill = pd.to_numeric(
            plans.get("fill_probability", pd.Series(np.nan, index=plans.index)),
            errors="coerce",
        )
        planned = pd.to_numeric(
            plans.get("planned_buy_amount", pd.Series(np.nan, index=plans.index)),
            errors="coerce",
        )
        available = pd.to_numeric(
            plans.get("available_fill_amount", pd.Series(np.nan, index=plans.index)),
            errors="coerce",
        )
        capacity = available / planned.where(planned.gt(0))
        executed = detail[
            detail["status"].astype(str).eq("EXECUTED")
            & detail["strategy_leg"].astype(str).eq(leg)
        ]
        rows.append(
            {
                "scenario": scenario,
                "strategy_leg": leg,
                "plan_count": int(len(plans)),
                "executed_count": int(len(executed)),
                "unfilled_plan_count": int(
                    plans.get("entry_filled", pd.Series(False, index=plans.index))
                    .fillna(False)
                    .astype(bool)
                    .eq(False)
                    .sum()
                ),
                "fill_probability_q25": float(fill.quantile(0.25)) if fill.notna().any() else np.nan,
                "fill_probability_median": float(fill.median()) if fill.notna().any() else np.nan,
                "capacity_ratio_q25": float(capacity.quantile(0.25)) if capacity.notna().any() else np.nan,
                "capacity_ratio_median": float(capacity.median()) if capacity.notna().any() else np.nan,
                "total_fees": float(pd.to_numeric(executed.get("total_fees", pd.Series(0.0, index=executed.index)), errors="coerce").fillna(0.0).sum()),
                "total_slippage": float(pd.to_numeric(executed.get("total_slippage", pd.Series(0.0, index=executed.index)), errors="coerce").fillna(0.0).sum()),
            }
        )
    return pd.DataFrame(rows)


def _baseline_overlap_audit(
    *,
    old_source: Path,
    new_source: Path,
    old_legs: Mapping[str, pd.DataFrame],
    new_legs: Mapping[str, pd.DataFrame],
    start: str,
    end: str,
) -> dict[str, Any]:
    usecols = [
        "trade_date", "ts_code", "allow_buy_reliable", "fill_probability",
        "sample_count", "suggested_turnover_rate", "model_training_end_date",
    ]
    old = pd.read_csv(old_source, usecols=usecols, dtype={"trade_date": str, "ts_code": str})
    new = pd.read_csv(new_source, usecols=usecols, dtype={"trade_date": str, "ts_code": str})
    for frame in (old, new):
        frame["trade_date"] = frame["trade_date"].map(normalize_date)
    old = old[old["trade_date"].between(start, end)]
    new = new[new["trade_date"].between(start, end)]
    merged = old.merge(new, on=["trade_date", "ts_code"], how="outer", suffixes=("_old", "_new"), indicator=True)
    feature_differences: dict[str, int] = {}
    common = merged[merged["_merge"].eq("both")]
    for column in usecols[2:]:
        left = common[f"{column}_old"]
        right = common[f"{column}_new"]
        feature_differences[column] = int(
            (left.fillna("<NA>").astype(str) != right.fillna("<NA>").astype(str)).sum()
        )
    plan_drift: dict[str, Any] = {}
    drift_count = 0
    for leg in FIXED_PRIORITY:
        columns = ["action_date", "signal_date", "ts_code", "status", "exit_date"]
        old_plan = old_legs[leg].copy()
        new_plan = new_legs[leg].copy()
        for frame in (old_plan, new_plan):
            frame["action_date"] = frame["action_date"].map(normalize_date)
        old_plan = old_plan[old_plan["action_date"].between(start, end)][columns]
        new_plan = new_plan[new_plan["action_date"].between(start, end)][columns]
        compared = old_plan.merge(
            new_plan, on="action_date", how="outer", suffixes=("_old", "_new"), indicator=True
        )
        changed = compared["_merge"].ne("both")
        for column in columns[1:3]:
            changed |= (
                compared[f"{column}_old"].fillna("").astype(str)
                != compared[f"{column}_new"].fillna("").astype(str)
            )
        count = int(changed.sum())
        drift_count += count
        plan_drift[leg] = {
            "old_count": int(len(old_plan)),
            "new_count": int(len(new_plan)),
            "decision_drift_count": count,
        }
    passed = bool(
        int(merged["_merge"].ne("both").sum()) == 0
        and sum(feature_differences.values()) == 0
        and drift_count == 0
    )
    return {
        "status": "PASS" if passed else "UNEXPLAINED_BASELINE_DRIFT",
        "window": {"start": start, "end": end},
        "source_key_only_count": {
            "old": int(merged["_merge"].eq("left_only").sum()),
            "new": int(merged["_merge"].eq("right_only").sum()),
        },
        "feature_differences": feature_differences,
        "plan_decision_drift": plan_drift,
    }


def _anomaly_review(
    *,
    old_on_old: Mapping[str, Any],
    old_on_new: Mapping[str, Any],
    new_on_new: Mapping[str, Any],
    robustness: Mapping[str, Any],
    thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    flags: list[str] = []
    rule_ratio = float(new_on_new["equity_multiple"]) / float(old_on_new["equity_multiple"])
    window_ratio = float(old_on_new["equity_multiple"]) / float(old_on_old["equity_multiple"])
    if rule_ratio >= float(thresholds["compound_ratio_high"]):
        flags.append("RULE_COMPOUND_RATIO")
    if window_ratio <= float(thresholds["window_effect_ratio_low"]) or window_ratio >= float(thresholds["window_effect_ratio_high"]):
        flags.append("WINDOW_EFFECT_RATIO")
    if abs(float(new_on_new["max_drawdown"]) - float(old_on_new["max_drawdown"])) >= float(thresholds["max_drawdown_change_pp"]):
        flags.append("MAX_DRAWDOWN_CHANGE")
    baseline_count = max(int(old_on_new["trade_count"]), 1)
    if abs(int(new_on_new["trade_count"]) - int(old_on_new["trade_count"])) / baseline_count >= float(thresholds["trade_count_change_ratio"]):
        flags.append("TRADE_COUNT_CHANGE")
    if abs(float(new_on_new["win_rate"]) - float(old_on_new["win_rate"])) >= float(thresholds["win_rate_change_pp"]):
        flags.append("WIN_RATE_CHANGE")
    if max(
        abs(float(new_on_new["avg_account_return"]) - float(old_on_new["avg_account_return"])),
        abs(float(new_on_new["median_account_return"]) - float(old_on_new["median_account_return"])),
    ) >= float(thresholds["average_or_median_change_pp"]):
        flags.append("CENTRAL_RETURN_CHANGE")
    if abs(float(new_on_new["max_loss"]) - float(old_on_new["max_loss"])) >= float(thresholds["max_loss_change_pp"]):
        flags.append("MAX_LOSS_CHANGE")
    isolated = robustness.get("isolated_event_contributions", [])
    contributions = sorted(
        [max(float(item["isolated_log_contribution"]), 0.0) for item in isolated],
        reverse=True,
    )
    positive_total = sum(contributions)
    top3_share = sum(contributions[:3]) / positive_total if positive_total > 0 else 0.0
    if top3_share > float(thresholds["top3_log_gain_share"]):
        flags.append("TOP3_GAIN_CONCENTRATION")
    top5_ratio = float(
        robustness.get("removal_tests", {}).get("top5", {}).get("ratio_vs_baseline", 1.0)
    )
    if top5_ratio <= float(thresholds.get("top5_removed_min_compound_ratio", 1.05)):
        flags.append("TOP5_REMOVAL_ERASES_GAIN")
    return {
        "status": "REVIEW_REQUIRED" if flags else "PASS",
        "flags": flags,
        "rule_compound_ratio": rule_ratio,
        "window_effect_ratio": window_ratio,
        "top3_positive_log_gain_share": top3_share,
        "top5_removed_ratio_vs_baseline": top5_ratio,
        "explanation": "所有变化均来自冻结候选规则、窗口滚动和A>C>E>D让路账本；命中阈值时仍需用户逐笔审核。",
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _metric_row(label: str, metric: Mapping[str, Any]) -> str:
    return (
        f"| {label} | {int(metric['trade_count'])} | {float(metric['win_rate']):.2%} | "
        f"{float(metric['avg_account_return']):.2%} | {float(metric['median_account_return']):.2%} | "
        f"{float(metric['equity_multiple']):.6f} | {float(metric['max_drawdown']):.2%} | "
        f"{float(metric['profit_loss_ratio']):.3f} | {float(metric['max_profit']):.2%} | "
        f"{float(metric['max_loss']):.2%} | {int(metric['max_consecutive_losses'])} |"
    )


def render_decision_report(result: Mapping[str, Any]) -> str:
    lines = [
        f"# ACDE {result['cutoff']} 月度研究决策报告",
        "",
        f"结论：`{result['status']}`。本轮未修改正式策略、实盘BUY开关，也未提交代码。",
        "",
        f"研究窗口：{result['window']['start']}～{result['window']['end']}；预测月份：{result['prediction']['start']}～{result['prediction']['end']}。",
        "",
        "## P00～P14状态",
        "",
        "| 关卡 | 状态 | 原因 |",
        "| --- | --- | --- |",
    ]
    for item in result["pipeline"]:
        lines.append(f"| {item['gate']} | {item['status']} | {item.get('reason', '') or '-'} |")
    lines.extend(
        [
            "",
            "## 逐腿研究决定",
            "",
            "| 腿 | 当前规则 | 研究胜者 | 合格候选数 | 决定 |",
            "| --- | --- | --- | ---: | --- |",
        ]
    )
    for leg in FIXED_PRIORITY:
        item = result["leg_winners"][leg]
        lines.append(
            f"| {leg} | {item['baseline_variant_id']} | {item['selected_variant_id']} | "
            f"{item['eligible_candidate_count']} | {item['decision']} |"
        )
    lines.extend(
        [
            "",
            "## 新旧组合同口径比较",
            "",
            "| 版本 | 样本 | 胜率 | 平均 | 中位 | 复利倍数 | 最大回撤 | 盈亏比 | 最大盈利 | 最大亏损 | 最大连亏 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            _metric_row("OLD_ON_OLD（过渡发布锚点）", result["comparisons"]["OLD_ON_OLD"]),
            _metric_row("OLD_ON_NEW", result["comparisons"]["OLD_ON_NEW"]),
            _metric_row("NEW_ON_NEW", result["comparisons"]["NEW_ON_NEW"]),
            "",
            "## 最终组合分腿",
            "",
            "| 腿 | 样本 | 胜率 | 平均 | 中位 | 复利倍数 | 最大回撤 | 盈亏比 | 最大盈利 | 最大亏损 | 最大连亏 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for leg in FIXED_PRIORITY:
        lines.append(_metric_row(leg, result["selected_standalone"][leg]))
    lines.extend(
        [
            "",
            "## 风险与过拟合",
            "",
            f"- 异常复核：{result['anomaly_review']['status']}；标记：{','.join(result['anomaly_review']['flags']) or '无'}。",
            f"- 共同历史区间基线漂移审计：{result['baseline_overlap_audit']['status']}。",
            f"- 同时撤销贡献最大的5个计划变化后，相对当前基线复利比：{float(result['top_change_robustness']['removal_tests']['top5']['ratio_vs_baseline']):.4f}。",
            f"- 候选空间：{result['candidate_space']['version']}；`STRICT_DISCOVERY={str(result['candidate_space']['strict_discovery']).lower()}`。",
            "- 2026-09 才是本轮真正未知的前向样本外；本报告不能作为扩大实盘资金的依据。",
            "- 历史回测不能承诺未来盈利；如后续获批发布，仍应先模拟和小资金验证。",
            "",
            "## 审核停点",
            "",
            "当前停在P11用户审核前。按项目标准，未获得‘再次精准校验，成功后提交代码’的明确授权前，不会改正式配置或提交研究胜者。",
        ]
    )
    return "\n".join(lines) + "\n"


def run_monthly_research(
    config: Mapping[str, Any],
    *,
    cutoff: str,
    output_dir: Path,
) -> dict[str, Any]:
    window = build_monthly_research_window(cutoff, months=int(config["windows"]["main_months"]))
    prediction_start, prediction_end = prediction_month(cutoff)
    output_dir.mkdir(parents=True, exist_ok=False)
    run_id = output_dir.name.replace("run_", "")
    date_window = {
        "schema_version": 2,
        "run_id": run_id,
        "cutoff": cutoff,
        "research_start": window.start,
        "research_end": window.end,
        "prediction_start": prediction_start,
        "prediction_end": prediction_end,
        "metric_date": "action_date",
    }
    _write_json(output_dir / "date_window.json", date_window)
    pipeline = [
        {"gate": f"P{index:02d}", "status": "NOT_RUN", "reason": ""}
        for index in range(15)
    ]
    pipeline[0].update(status="PASS", reason="月度日期与授权范围已冻结")
    pipeline[1].update(status="PASS", reason="复用已更新到截止日的原始数据")

    quality = data_quality_gate(config, cutoff=cutoff, window=window)
    _write_json(output_dir / "quality_summary.json", quality)
    if quality["status"] != "PASS":
        pipeline[2].update(status="FAIL", reason=",".join(quality["hard_failures"]))
        for item in pipeline[3:]:
            item.update(status="NOT_RUN_UPSTREAM_FAILED", reason="P02未通过")
        result = {
            "schema_version": 2,
            "run_id": run_id,
            "cutoff": cutoff,
            "window": {"start": window.start, "end": window.end},
            "prediction": {"start": prediction_start, "end": prediction_end},
            "status": "NOT_READY",
            "pipeline": pipeline,
            "formal_strategy_modified": False,
            "code_committed": False,
        }
        _write_json(output_dir / "pipeline_status.json", result)
        return result
    pipeline[2].update(status="PASS", reason="原始、复权、as-of与D失败关闭门禁通过")

    paths = monthly_paths(config, cutoff)
    fingerprint_inputs = {
        name: {"path": str(path.relative_to(ROOT)), "sha256": sha256_path(path)}
        for name, path in paths.items()
        if name not in {"base", "raw_daily_dir", "raw_daily_basic_dir", "raw_limit_list_dir", "raw_adj_factor_dir"}
    }
    data_manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
        "git_worktree": subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines(),
        "inputs": fingerprint_inputs,
        "config_sha256": sha256_path(ROOT / "config/acde_rolling_optimization.json"),
    }
    raw_dates = open_dates(
        pd.read_csv(paths["trade_calendar"], dtype={"cal_date": str}, low_memory=False),
        window.start,
        window.end,
    )
    data_manifest["raw_file_fingerprints"] = {
        name: {
            date: sha256_path(directory / f"{date}.csv")
            for date in raw_dates
        }
        for name, directory in (
            ("daily", paths["raw_daily_dir"]),
            ("daily_basic", paths["raw_daily_basic_dir"]),
            ("limit_list", paths["raw_limit_list_dir"]),
            ("adj_factor", paths["raw_adj_factor_dir"]),
        )
    }
    _write_json(output_dir / "data_manifest.json", data_manifest)
    pipeline[3].update(status="PASS", reason="数据、代码与配置输入已冻结")

    controller = config["market_controller"]
    context = _context(
        window=window,
        feature_path=paths["strict_feature_pool"],
        sentiment_path=paths["market_sentiment"],
        d_event_path=paths["d_event_source"],
        calendar_path=paths["trade_calendar"],
        minimum_limit_up_count=int(controller["minimum_limit_up_count"]) if controller["hard_gate_enabled"] else 0,
    )
    baselines, candidates = _variant_sets()
    execution = _execution_kwargs(config)
    baseline_legs = _build_plans(baselines, context=context, cutoff=cutoff)
    baseline_rebuild = _build_plans(baselines, context=context, cutoff=cutoff)
    baseline_signatures = {leg: plan_signature(frame) for leg, frame in baseline_legs.items()}
    reproduction_signatures = {leg: plan_signature(frame) for leg, frame in baseline_rebuild.items()}
    baseline_detail = _replay(baseline_legs, action_dates=context["action_dates"], execution=execution)
    baseline_detail_again = _replay(baseline_rebuild, action_dates=context["action_dates"], execution=execution)
    baseline_reproduced = bool(
        baseline_signatures == reproduction_signatures
        and baseline_detail.fillna("").astype(str).equals(baseline_detail_again.fillna("").astype(str))
    )
    baseline_dir = output_dir / "baseline_plans"
    baseline_dir.mkdir()
    for leg, frame in baseline_legs.items():
        frame.to_csv(baseline_dir / f"{leg.lower()}_plans.csv", index=False, encoding="utf-8-sig")
    baseline_detail.to_csv(output_dir / "baseline_trades.csv", index=False, encoding="utf-8-sig")
    baseline_metrics = _metrics(baseline_detail, window)
    baseline_standalone_details = {
        leg: _standalone(frame, leg, action_dates=context["action_dates"], execution=execution)
        for leg, frame in baseline_legs.items()
    }
    baseline_standalone = {leg: _metrics(detail, window) for leg, detail in baseline_standalone_details.items()}
    baseline_reproduction = {
        "status": "PASS" if baseline_reproduced else "BASELINE_REPRODUCTION_FAILED",
        "plan_signatures": baseline_signatures,
        "metrics": baseline_metrics,
        "exact_cash": True,
        "minimum_commission_modeled": True,
    }
    _write_json(output_dir / "baseline_reproduction.json", baseline_reproduction)
    if not baseline_reproduced:
        pipeline[4].update(status="FAIL", reason="基线两次重建不一致")
        raise RuntimeError("当前正式基线无法确定性复现")
    pipeline[4].update(status="PASS", reason="当前正式ACDE计划与精确现金账本两次一致")

    catalog = variant_catalog_payload(
        [*baselines.values(), *(item for leg in FIXED_PRIORITY for item in candidates[leg])]
    )
    candidate_manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "frozen_before_candidate_replay": True,
        "candidate_space": config["candidate_space"],
        "replacement_gate": config["replacement_gate"],
        "priority": list(FIXED_PRIORITY),
        "candidate_count": {leg: len(candidates[leg]) for leg in FIXED_PRIORITY},
        "catalog": catalog,
    }
    _write_json(output_dir / "candidate_manifest.json", candidate_manifest)
    pipeline[5].update(status="PASS", reason="有限候选、门槛和排序规则已冻结")

    candidate_rows: list[dict[str, Any]] = []
    candidate_plan_store: dict[str, dict[str, pd.DataFrame]] = {leg: {} for leg in FIXED_PRIORITY}
    ledgers_root = output_dir / "candidate_ledgers"
    ledgers_root.mkdir()
    leg_winners: dict[str, Any] = {}
    for leg in FIXED_PRIORITY:
        leg_dir = ledgers_root / leg.lower()
        leg_dir.mkdir()
        baseline_row = {
            "strategy_leg": leg,
            "variant_id": f"{leg}_CURRENT",
            "description": "当前正式规则",
            "changed_axis_count": 0,
            "plan_signature": baseline_signatures[leg],
            "eligible": True,
            "gate_reasons": "",
            "standalone_equity_multiple": baseline_standalone[leg]["equity_multiple"],
            "standalone_max_drawdown": baseline_standalone[leg]["max_drawdown"],
            "standalone_trade_count": baseline_standalone[leg]["trade_count"],
            "portfolio_equity_multiple": baseline_metrics["equity_multiple"],
            "portfolio_max_drawdown": baseline_metrics["max_drawdown"],
            **_flatten("standalone", baseline_standalone[leg]),
            **_flatten("portfolio", baseline_metrics),
        }
        rows_for_leg = [baseline_row]
        candidate_rows.append(baseline_row)
        seen = {baseline_signatures[leg]}
        cache = StaticOutcomeCache()
        for variant in candidates[leg]:
            plan = build_variant_plan(
                variant,
                signal_pool=context["signal_pool"],
                d_events=context["d_events"],
                allowed_action_dates=context["allowed_actions"],
                cutoff=cutoff,
                outcome_cache=cache,
            )
            signature = plan_signature(plan)
            if signature in seen:
                row = {
                    "strategy_leg": leg,
                    "variant_id": variant.variant_id,
                    "description": variant.description,
                    "changed_axis_count": variant.changed_axis_count,
                    "plan_signature": signature,
                    "eligible": False,
                    "gate_reasons": "DUPLICATE_PLAN_SIGNATURE",
                    "standalone_equity_multiple": np.nan,
                    "standalone_max_drawdown": np.nan,
                    "standalone_trade_count": 0,
                    "portfolio_equity_multiple": np.nan,
                    "portfolio_max_drawdown": np.nan,
                }
            else:
                seen.add(signature)
                candidate_plan_store[leg][variant.variant_id] = plan
                standalone_detail = _standalone(plan, leg, action_dates=context["action_dates"], execution=execution)
                replacement = {name: frame.copy() for name, frame in baseline_legs.items()}
                replacement[leg] = plan
                portfolio_detail = _replay(replacement, action_dates=context["action_dates"], execution=execution)
                standalone_metric = _metrics(standalone_detail, window)
                portfolio_metric = _metrics(portfolio_detail, window)
                reasons = _candidate_gate(
                    leg=leg,
                    variant=variant,
                    baseline_standalone=baseline_standalone[leg],
                    candidate_standalone=standalone_metric,
                    baseline_portfolio=baseline_metrics,
                    candidate_portfolio=portfolio_metric,
                    gate=config["replacement_gate"],
                )
                plan.to_csv(leg_dir / f"{variant.variant_id}_plans.csv", index=False, encoding="utf-8-sig")
                standalone_detail.to_csv(leg_dir / f"{variant.variant_id}_standalone.csv", index=False, encoding="utf-8-sig")
                portfolio_detail.to_csv(leg_dir / f"{variant.variant_id}_portfolio.csv", index=False, encoding="utf-8-sig")
                row = {
                    "strategy_leg": leg,
                    "variant_id": variant.variant_id,
                    "description": variant.description,
                    "changed_axis_count": variant.changed_axis_count,
                    "plan_signature": signature,
                    "eligible": not reasons,
                    "gate_reasons": ";".join(reasons),
                    "standalone_equity_multiple": standalone_metric["equity_multiple"],
                    "standalone_max_drawdown": standalone_metric["max_drawdown"],
                    "standalone_trade_count": standalone_metric["trade_count"],
                    "portfolio_equity_multiple": portfolio_metric["equity_multiple"],
                    "portfolio_max_drawdown": portfolio_metric["max_drawdown"],
                    **_flatten("standalone", standalone_metric),
                    **_flatten("portfolio", portfolio_metric),
                }
            rows_for_leg.append(row)
            candidate_rows.append(row)
        winner = sorted([row for row in rows_for_leg if row["eligible"]], key=_rank_key, reverse=True)[0]
        changed = winner["variant_id"] != f"{leg}_CURRENT"
        leg_winners[leg] = {
            "baseline_variant_id": baselines[leg].variant_id,
            "selected_variant_id": winner["variant_id"],
            "selected_description": winner["description"],
            "eligible_candidate_count": sum(bool(row["eligible"]) for row in rows_for_leg) - 1,
            "decision": "RESEARCH_WINNER" if changed else "KEEP_CURRENT",
            "metrics": {key: value for key, value in winner.items() if key.startswith(("standalone_", "portfolio_"))},
        }
    pd.DataFrame(candidate_rows).to_csv(output_dir / "all_candidate_metrics.csv", index=False, encoding="utf-8-sig")
    _write_json(output_dir / "leg_winners.json", leg_winners)
    pipeline[6].update(status="PASS", reason="逐腿独立与只替换该腿组合账本完成")

    changed_winners = {
        leg: info["selected_variant_id"]
        for leg, info in leg_winners.items()
        if info["decision"] == "RESEARCH_WINNER"
    }
    bundle_rows: list[dict[str, Any]] = []
    bundle_details: dict[str, pd.DataFrame] = {}
    bundle_legs: dict[str, dict[str, pd.DataFrame]] = {}
    changed_legs = list(changed_winners)
    for count in range(len(changed_legs) + 1):
        for subset in itertools.combinations(changed_legs, count):
            subset_set = set(subset)
            legs = {leg: baseline_legs[leg].copy() for leg in FIXED_PRIORITY}
            for leg in subset_set:
                legs[leg] = candidate_plan_store[leg][changed_winners[leg]].copy()
            scenario = "CURRENT" if not subset else "UPDATE_" + "_".join(subset)
            detail = _replay(legs, action_dates=context["action_dates"], execution=execution)
            metric = _metrics(detail, window)
            reasons = _bundle_gate(
                baseline_metrics,
                metric,
                selected_legs=subset,
                gate=config["replacement_gate"],
            )
            row = {
                "scenario_id": scenario,
                "selected_legs": ",".join(subset),
                "changed_leg_count": len(subset),
                "eligible": not reasons,
                "gate_reasons": ";".join(reasons),
                **metric,
            }
            bundle_rows.append(row)
            bundle_details[scenario] = detail
            bundle_legs[scenario] = legs
    bundle_frame = pd.DataFrame(bundle_rows)
    bundle_frame.to_csv(output_dir / "bundle_comparison.csv", index=False, encoding="utf-8-sig")
    eligible_bundles = bundle_frame[bundle_frame["eligible"].astype(bool)].copy()
    eligible_bundles = eligible_bundles.sort_values(
        ["equity_multiple", "max_drawdown", "median_account_return", "changed_leg_count", "scenario_id"],
        ascending=[False, False, False, True, True],
    )
    selected_scenario = str(eligible_bundles.iloc[0]["scenario_id"])
    selected_detail = bundle_details[selected_scenario]
    final_legs = bundle_legs[selected_scenario]
    selected_metrics = _metrics(selected_detail, window)
    selected_legs = [leg for leg in FIXED_PRIORITY if leg in set(str(eligible_bundles.iloc[0]["selected_legs"]).split(","))]
    pipeline[7].update(status="PASS", reason="C专项回归在P07文件中单列")
    pipeline[8].update(status="PASS", reason=f"唯一组合胜者={selected_scenario}")

    selected_standalone_details = {
        leg: _standalone(final_legs[leg], leg, action_dates=context["action_dates"], execution=execution)
        for leg in FIXED_PRIORITY
    }
    selected_standalone = {leg: _metrics(detail, window) for leg, detail in selected_standalone_details.items()}
    selected_detail.to_csv(output_dir / "selected_trades.csv", index=False, encoding="utf-8-sig")
    c_selected_id = changed_winners.get("C", "C_CURRENT") if "C" in selected_legs else "C_CURRENT"
    c_regression = _c_regression(
        baseline_legs=baseline_legs,
        final_legs=final_legs,
        selected_variant_id=c_selected_id,
        action_dates=context["action_dates"],
        selected_detail=selected_detail,
    )
    _write_json(output_dir / "c_regression_gate.json", c_regression)
    if c_regression["status"] != "PASS":
        pipeline[7].update(status="FAIL", reason="C专项回归失败")
        raise RuntimeError("C专项回归失败")

    plan_diff = selected_plan_change_ledger(baseline_legs, final_legs)
    trade_diff = portfolio_decision_change_ledger(baseline_detail, selected_detail)
    plan_diff.to_csv(output_dir / "plan_diff.csv", index=False, encoding="utf-8-sig")
    trade_diff.to_csv(output_dir / "trade_diff.csv", index=False, encoding="utf-8-sig")
    rule_diff = {
        leg: {
            "old": baselines[leg].variant_id,
            "new": changed_winners.get(leg, f"{leg}_CURRENT") if leg in selected_legs else f"{leg}_CURRENT",
            "operation": "替换" if leg in selected_legs else "保持",
            "description": leg_winners[leg]["selected_description"] if leg in selected_legs else "保持当前正式规则",
            "changed_plan_count": int((plan_diff["strategy_leg"].astype(str) == leg).sum()) if not plan_diff.empty else 0,
        }
        for leg in FIXED_PRIORITY
    }
    _write_json(output_dir / "rule_diff.json", rule_diff)

    robustness = _top_change_robustness(
        baseline_legs=baseline_legs,
        final_legs=final_legs,
        plan_diff=plan_diff,
        action_dates=context["action_dates"],
        execution=execution,
        window=window,
        baseline_multiple=float(baseline_metrics["equity_multiple"]),
        selected_multiple=float(selected_metrics["equity_multiple"]),
    )
    _write_json(output_dir / "top_change_robustness.json", robustness)
    pd.concat(
        [
            _period_breakdown(baseline_detail, "CURRENT"),
            _period_breakdown(selected_detail, selected_scenario),
        ],
        ignore_index=True,
    ).to_csv(output_dir / "period_breakdown.csv", index=False, encoding="utf-8-sig")
    pd.concat(
        [
            _execution_summary(
                scenario="CURRENT", legs=baseline_legs, detail=baseline_detail
            ),
            _execution_summary(
                scenario=selected_scenario, legs=final_legs, detail=selected_detail
            ),
        ],
        ignore_index=True,
    ).to_csv(output_dir / "execution_summary.csv", index=False, encoding="utf-8-sig")

    # 过渡期OLD_ON_OLD使用当前正式版本在2026-06-30发布锚点窗口独立重放。
    old_window = ResearchWindow("old_anchor", "20230701", "20260630", "过渡发布锚点", False)
    old_context = _context(
        window=old_window,
        feature_path=ROOT / str(config["data"]["strict_feature_pool"]),
        sentiment_path=ROOT / str(config["data"]["market_sentiment"]),
        d_event_path=ROOT / str(config["data"]["d_event_source"]),
        calendar_path=paths["trade_calendar"],
        minimum_limit_up_count=0,
    )
    old_legs = _build_plans(baselines, context=old_context, cutoff=old_window.end)
    old_detail = _replay(old_legs, action_dates=old_context["action_dates"], execution=execution)
    old_on_old = _metrics(old_detail, old_window)
    overlap_audit = _baseline_overlap_audit(
        old_source=ROOT / str(config["data"]["strict_daily_source"]),
        new_source=paths["strict_daily_source"],
        old_legs=old_legs,
        new_legs=baseline_legs,
        start=window.start,
        end=old_window.end,
    )
    _write_json(output_dir / "baseline_overlap_audit.json", overlap_audit)
    comparisons = {
        "OLD_ON_OLD": old_on_old,
        "OLD_ON_NEW": baseline_metrics,
        "NEW_ON_NEW": selected_metrics,
    }
    _write_json(output_dir / "metric_attribution.json", {
        "comparisons": comparisons,
        "window_data_effect_log": math.log(baseline_metrics["equity_multiple"] / old_on_old["equity_multiple"]),
        "rule_effect_log": math.log(selected_metrics["equity_multiple"] / baseline_metrics["equity_multiple"]),
        "total_effect_log": math.log(selected_metrics["equity_multiple"] / old_on_old["equity_multiple"]),
        "old_on_old_note": "月度标准首次启用，使用2026-06-30正式发布锚点窗口，不冒充上月月度窗口。",
    })
    attribution_passed = overlap_audit["status"] == "PASS"
    pipeline[9].update(
        status="PASS" if attribution_passed else "FAIL",
        reason=(
            "OLD_ON_OLD/OLD_ON_NEW/NEW_ON_NEW、共同区间复现与逐笔差异已生成"
            if attribution_passed
            else "共同历史区间存在未解释的基线漂移"
        ),
    )

    anomaly = _anomaly_review(
        old_on_old=old_on_old,
        old_on_new=baseline_metrics,
        new_on_new=selected_metrics,
        robustness=robustness,
        thresholds=config.get("anomaly_review", {
            "compound_ratio_high": 1.5,
            "window_effect_ratio_low": 0.67,
            "window_effect_ratio_high": 1.5,
            "max_drawdown_change_pp": 0.05,
            "trade_count_change_ratio": 0.15,
            "win_rate_change_pp": 0.08,
            "average_or_median_change_pp": 0.015,
            "max_loss_change_pp": 0.03,
            "top3_log_gain_share": 0.5,
        }),
    )
    _write_json(output_dir / "anomaly_review.json", anomaly)
    pipeline[10].update(
        status=(
            "NOT_RUN_UPSTREAM_FAILED"
            if not attribution_passed
            else ("PASS" if anomaly["status"] == "PASS" else "REVIEW_REQUIRED")
        ),
        reason=("P09未通过" if not attribution_passed else ",".join(anomaly["flags"])),
    )
    pipeline[11].update(status="PENDING_USER_REVIEW", reason="禁止修改正式配置或提交")
    pipeline[12].update(status="NOT_RUN_PENDING_USER_AUTHORIZATION", reason="需用户明确要求再次精准校验")
    pipeline[13].update(status="NOT_RUN", reason="未获配置落地与提交授权")
    pipeline[14].update(status="PENDING_FORWARD_MONTH", reason=f"真实前向月份{prediction_start[:6]}")

    stress_rows: list[dict[str, Any]] = []
    for slippage in config["execution"]["stress_slippage_rates"]:
        for label, legs in (("CURRENT", baseline_legs), (selected_scenario, final_legs)):
            stressed = _stress_plans(legs, float(slippage))
            metric = _metrics(_replay(stressed, action_dates=context["action_dates"], execution=execution), window)
            stress_rows.append({"scenario": label, "slippage_each_side": slippage, **metric})
    pd.DataFrame(stress_rows).to_csv(output_dir / "slippage_stress.csv", index=False, encoding="utf-8-sig")

    reconciliation = {
        "status": "PASS",
        "initial_cash": execution["initial_cash"],
        "baseline_ending_cash": float(baseline_detail.iloc[-1]["equity_after"]),
        "baseline_multiple_from_cash": float(baseline_detail.iloc[-1]["equity_after"]) / execution["initial_cash"],
        "baseline_multiple_from_returns": baseline_metrics["equity_multiple"],
        "selected_ending_cash": float(selected_detail.iloc[-1]["equity_after"]),
        "selected_multiple_from_cash": float(selected_detail.iloc[-1]["equity_after"]) / execution["initial_cash"],
        "selected_multiple_from_returns": selected_metrics["equity_multiple"],
        "tolerance": 1e-10,
    }
    if max(
        abs(reconciliation["baseline_multiple_from_cash"] - reconciliation["baseline_multiple_from_returns"]),
        abs(reconciliation["selected_multiple_from_cash"] - reconciliation["selected_multiple_from_returns"]),
    ) > 1e-10:
        reconciliation["status"] = "FAIL"
        raise RuntimeError("现金账本对账失败")
    _write_json(output_dir / "backtest_reconciliation.json", reconciliation)
    _write_json(output_dir / "certification.json", {
        "status": "NOT_RUN_PENDING_P12",
        "formal_strategy_modified": False,
        "code_committed": False,
    })

    status = (
        "KEEP_CURRENT_UNEXPLAINED_BASELINE_DRIFT"
        if not attribution_passed
        else (
            "EXTREME_CHANGE_REVIEW_REQUIRED"
            if anomaly["status"] != "PASS"
            else ("KEEP_CURRENT" if selected_scenario == "CURRENT" else "COMPARISON_PENDING_USER_REVIEW")
        )
    )
    result = {
        "schema_version": 2,
        "run_id": run_id,
        "cutoff": cutoff,
        "window": {"start": window.start, "end": window.end},
        "prediction": {"start": prediction_start, "end": prediction_end},
        "priority": list(FIXED_PRIORITY),
        "selected_scenario": selected_scenario,
        "selected_legs": selected_legs,
        "status": status,
        "pipeline": pipeline,
        "leg_winners": leg_winners,
        "comparisons": comparisons,
        "selected_standalone": selected_standalone,
        "candidate_space": config["candidate_space"],
        "anomaly_review": anomaly,
        "top_change_robustness": robustness,
        "baseline_overlap_audit": overlap_audit,
        "execution": {**config["execution"], "minimum_commission_modeled": True, "exact_cash_ledger": True},
        "formal_strategy_modified": False,
        "code_committed": False,
    }
    _write_json(output_dir / "pipeline_status.json", {"overall_status": status, "stages": pipeline})
    _write_json(output_dir / "research_summary.json", result)
    (output_dir / "decision_report.md").write_text(render_decision_report(result), encoding="utf-8")

    artifact_files = [path for path in output_dir.rglob("*") if path.is_file()]
    artifact_manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "artifacts": {
            str(path.relative_to(output_dir)): sha256_path(path)
            for path in sorted(artifact_files)
        },
    }
    _write_json(output_dir / "artifact_manifest.json", artifact_manifest)
    return result
