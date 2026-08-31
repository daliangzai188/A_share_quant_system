#!/usr/bin/env python3
"""A>C>E>D三年主优化、两年确认、半年失效检查统一入口。

本入口首先执行数据与口径硬门禁。任何一条腿缺少主窗口所需的严格数据时，
只输出阻断报告，不启动参数搜索，也不改写生产配置。
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import logging
import math
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.acde_rolling_framework import (  # noqa: E402
    FIXED_PRIORITY,
    action_metrics,
    build_window_set,
    coverage_audit,
    evaluate_three_window_replacement,
    normalize_date,
    open_dates,
    prior_open_date,
    replay_action_date_portfolio,
    standalone_replay,
)
from src.acde_rolling_candidates import (  # noqa: E402
    StaticOutcomeCache,
    VariantDefinition,
    a_variants,
    build_a_picks,
    build_c_picks,
    build_d_plans,
    build_e_picks,
    c_variants,
    d_variants,
    e_variants,
    plan_signature,
    previous_close_market_gate,
    static_plan_outcomes,
    strict_signal_pool,
    variant_catalog_payload,
)
from src.strict_asof import (  # noqa: E402
    PointInTimeContract,
    audit_point_in_time_frame,
)
from src.strategy_d_factor_rules import load_factor_release  # noqa: E402
from src.strategy_e import load_e_spec  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402


LOGGER = logging.getLogger("optimize_acde_rolling_three_year")
DEFAULT_CONFIG = ROOT / "config/acde_rolling_optimization.json"
STRATEGY_CONFIG = ROOT / "config/strategy_config.json"
D_RELEASE_CONFIG = ROOT / "config/strategy_d_factor_release.json"
EXPECTED_FILL_METHOD = "asof_turnover_space_proxy_v2"


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def optional_text(value: Any) -> str:
    return "" if value is None or pd.isna(value) else str(value)


def optional_float(value: Any, default: float = 0.0) -> float:
    parsed = pd.to_numeric(value, errors="coerce")
    return float(default) if pd.isna(parsed) else float(parsed)


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("ACDE滚动优化配置schema_version不支持")
    if payload.get("mode") != "research_only":
        raise ValueError("ACDE滚动优化必须保持research_only")
    if bool(payload.get("formal_strategy_auto_apply", True)):
        raise ValueError("ACDE滚动优化禁止自动修改正式策略")
    if tuple(payload.get("priority", [])) != FIXED_PRIORITY:
        raise ValueError("ACDE滚动优化腿序必须固定为A>C>E>D")
    windows = payload.get("windows", {})
    if windows.get("metric_date") != "action_date":
        raise ValueError("三窗口指标必须按action_date归属")
    if windows.get("selection_window") != "main":
        raise ValueError("参数选择只能使用三年主窗口")
    if bool(windows.get("recent_confirmation_may_rank_candidates", True)):
        raise ValueError("最近两年只能确认，不能参与候选排名")
    if bool(windows.get("failure_check_may_rank_candidates", True)):
        raise ValueError("最近半年只能检查失效，不能参与候选排名")
    return payload


def latest_completed_update_node(today: dt.date | None = None) -> str:
    value = today or dt.date.today()
    june = value.replace(month=6, day=30)
    december = value.replace(month=12, day=31)
    if value >= december:
        selected = december
    elif value >= june:
        selected = june
    else:
        selected = value.replace(year=value.year - 1, month=12, day=31)
    return selected.strftime("%Y%m%d")


def read_dates(path: Path, column: str) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=[column])
    return pd.read_csv(path, usecols=[column], dtype={column: str}, low_memory=False)


def audit_d_target_denominator(
    *,
    paths: dict[str, Path],
    calendar: pd.DataFrame,
    event_source: pd.DataFrame,
    required_start: str,
    required_end: str,
    frozen_contract: dict[str, Any] | None,
) -> dict[str, Any]:
    """硬校验D完整触板母池、已登记异常和fail-closed路径。"""

    required_path_names = (
        "d_missing_year_target_ledger",
        "d_recent_target_ledger",
        "d_known_data_gaps",
        "d_missing_year_summary",
        "d_recent_summary",
        "d_merge_summary",
    )
    missing_files = [
        name for name in required_path_names
        if name not in paths or not paths[name].exists()
    ]
    if missing_files:
        return {
            "passed": False,
            "reason": "MISSING_D_QUALITY_SOURCE",
            "missing_files": missing_files,
        }

    ledgers: list[pd.DataFrame] = []
    ledger_counts: dict[str, int] = {}
    failures: list[str] = []
    for name in ("d_missing_year_target_ledger", "d_recent_target_ledger"):
        frame = pd.read_csv(paths[name], dtype=str, low_memory=False)
        required_columns = {
            "trade_date",
            "ts_code",
            "minute_status",
            "path_complete",
            "event_count",
            "execution_status",
        }
        missing = sorted(required_columns.difference(frame.columns))
        if missing:
            failures.append(f"{name}:MISSING_COLUMNS={missing}")
            continue
        frame["trade_date"] = frame["trade_date"].map(normalize_date)
        frame["ts_code"] = frame["ts_code"].astype(str)
        frame["target_key"] = frame["trade_date"] + "|" + frame["ts_code"]
        if frame["target_key"].duplicated().any():
            failures.append(f"{name}:DUPLICATE_TARGET_KEY")
        ledger_counts[name] = int(len(frame))
        ledgers.append(frame)
    if len(ledgers) != 2:
        return {
            "passed": False,
            "reason": "INVALID_D_TARGET_LEDGER",
            "failures": failures,
        }

    targets = pd.concat(ledgers, ignore_index=True)
    if targets["target_key"].duplicated().any():
        failures.append("CROSS_LEDGER_DUPLICATE_TARGET_KEY")
    expected_dates = set(open_dates(calendar, required_start, required_end))
    target_dates = set(targets["trade_date"].astype(str))
    missing_target_dates = sorted(expected_dates - target_dates)
    unexpected_target_dates = sorted(target_dates - expected_dates)
    if missing_target_dates:
        failures.append(f"MISSING_TARGET_DATES={missing_target_dates[:10]}")
    if unexpected_target_dates:
        failures.append(f"UNEXPECTED_TARGET_DATES={unexpected_target_dates[:10]}")

    known_payload = json.loads(paths["d_known_data_gaps"].read_text(encoding="utf-8"))
    registered_gaps = list(known_payload.get("gaps", []))
    registered_mismatches = list(known_payload.get("price_mismatches", []))
    registered_rows = [*registered_gaps, *registered_mismatches]
    registered_keys = {str(item.get("target_key", "")) for item in registered_rows}
    if "" in registered_keys:
        failures.append("KNOWN_GAP_EMPTY_TARGET_KEY")
    if any(
        str(item.get("handling", "")) != "FAIL_CLOSED_KEEP_IN_DENOMINATOR"
        for item in registered_rows
    ):
        failures.append("KNOWN_GAP_HANDLING_NOT_FAIL_CLOSED")

    ready_status = "READY_1M_PATH_NO_QUEUE_DEPTH"
    abnormal = targets.loc[~targets["minute_status"].astype(str).eq(ready_status)].copy()
    abnormal_keys = set(abnormal["target_key"].astype(str))
    if abnormal_keys != registered_keys:
        failures.append("REGISTERED_ABNORMAL_KEYS_MISMATCH")
    gap_keys = {str(item["target_key"]) for item in registered_gaps}
    mismatch_keys = {str(item["target_key"]) for item in registered_mismatches}
    actual_gap_keys = set(
        abnormal.loc[
            abnormal["minute_status"].astype(str).eq("MISSING_MINUTE_DATA"),
            "target_key",
        ].astype(str)
    )
    actual_mismatch_keys = set(
        abnormal.loc[
            abnormal["minute_status"].astype(str).eq(
                "MISMATCH_DAILY_TOUCH_NOT_CONFIRMED"
            ),
            "target_key",
        ].astype(str)
    )
    if actual_gap_keys != gap_keys:
        failures.append("VENDOR_GAP_KEYS_MISMATCH")
    if actual_mismatch_keys != mismatch_keys:
        failures.append("PRICE_MISMATCH_KEYS_MISMATCH")
    path_complete = (
        abnormal["path_complete"].fillna("").astype(str).str.lower().isin(
            {"true", "1", "yes"}
        )
    )
    if path_complete.any():
        failures.append("ABNORMAL_TARGET_PATH_NOT_FAIL_CLOSED")
    if not pd.to_numeric(abnormal["event_count"], errors="coerce").fillna(-1).eq(0).all():
        failures.append("ABNORMAL_TARGET_EVENT_COUNT_NOT_ZERO")
    if not abnormal["execution_status"].fillna("").astype(str).eq("NO_PATH_SIGNAL").all():
        failures.append("ABNORMAL_TARGET_EXECUTION_NOT_NO_PATH_SIGNAL")

    events = event_source.copy()
    events["trade_date"] = events["trade_date"].map(normalize_date)
    events["ts_code"] = events["ts_code"].astype(str)
    events["target_key"] = events["trade_date"] + "|" + events["ts_code"]
    if "event_id" not in events.columns or events["event_id"].duplicated().any():
        failures.append("D_EVENT_ID_MISSING_OR_DUPLICATE")
    if set(events["target_key"].astype(str)) - set(
        targets.loc[targets["minute_status"].astype(str).eq(ready_status), "target_key"]
        .astype(str)
    ):
        failures.append("D_EVENT_NOT_TRACEABLE_TO_READY_TARGET")
    if set(events["target_key"].astype(str)) & abnormal_keys:
        failures.append("D_ABNORMAL_TARGET_LEAKED_INTO_EVENT_SOURCE")

    missing_summary = json.loads(
        paths["d_missing_year_summary"].read_text(encoding="utf-8")
    )
    recent_summary = json.loads(paths["d_recent_summary"].read_text(encoding="utf-8"))
    merge_summary = json.loads(paths["d_merge_summary"].read_text(encoding="utf-8"))
    expected_ledger_counts = (
        int(missing_summary["mother_pool"]["first_board_touch_count"]),
        int(recent_summary["mother_pool"]["first_board_touch_count"]),
    )
    actual_ledger_counts = (
        ledger_counts["d_missing_year_target_ledger"],
        ledger_counts["d_recent_target_ledger"],
    )
    if actual_ledger_counts != expected_ledger_counts:
        failures.append("D_LEDGER_COUNT_DIFFERS_FROM_FROZEN_SUMMARY")
    if int(merge_summary.get("event_count", -1)) != len(events):
        failures.append("D_EVENT_COUNT_DIFFERS_FROM_MERGE_SUMMARY")
    if str(merge_summary.get("output_sha256", "")) != sha256_path(paths["d_event_source"]):
        failures.append("D_EVENT_SHA_DIFFERS_FROM_MERGE_SUMMARY")

    result = {
        "passed": not failures,
        "reason": "OK" if not failures else "D_TARGET_DENOMINATOR_AUDIT_FAILED",
        "failures": failures,
        "target_count": int(len(targets)),
        "target_trade_day_count": int(targets["trade_date"].nunique()),
        "required_trade_day_count": int(len(expected_dates)),
        "ready_path_target_count": int(
            targets["minute_status"].astype(str).eq(ready_status).sum()
        ),
        "registered_vendor_gap_count": int(len(gap_keys)),
        "registered_price_mismatch_count": int(len(mismatch_keys)),
        "fail_closed_abnormal_count": int(len(abnormal)),
        "event_count": int(len(events)),
        "event_trade_day_count": int(events["trade_date"].nunique()),
        "known_abnormal_keys": sorted(registered_keys),
        "source_sha256": {
            name: sha256_path(paths[name]) for name in required_path_names
        },
    }
    result["source_sha256"]["d_event_source"] = sha256_path(paths["d_event_source"])
    result["source_sha256"]["trade_calendar"] = sha256_path(paths["trade_calendar"])
    result["known_abnormal_keys_sha256"] = hashlib.sha256(
        json.dumps(
            result["known_abnormal_keys"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    contract_failures: list[str] = []
    if not frozen_contract:
        contract_failures.append("MISSING_FROZEN_D_QUALITY_CONTRACT")
    else:
        for key in (
            "target_count",
            "target_trade_day_count",
            "ready_path_target_count",
            "registered_vendor_gap_count",
            "registered_price_mismatch_count",
            "fail_closed_abnormal_count",
            "event_count",
            "event_trade_day_count",
            "known_abnormal_keys_sha256",
        ):
            if result.get(key) != frozen_contract.get(key):
                contract_failures.append(f"D_QUALITY_CONTRACT_{key}")
        for name, expected_sha in frozen_contract.get("source_sha256", {}).items():
            if result["source_sha256"].get(name) != str(expected_sha):
                contract_failures.append(f"D_QUALITY_CONTRACT_SHA256_{name}")
    if contract_failures:
        result["failures"].extend(contract_failures)
        result["passed"] = False
        result["reason"] = "D_FROZEN_QUALITY_CONTRACT_FAILED"
    result["frozen_contract_present"] = bool(frozen_contract)
    result["frozen_contract_passed"] = not contract_failures
    return result


def build_readiness(config: dict[str, Any], as_of: str) -> dict[str, Any]:
    window_config = config["windows"]
    windows = build_window_set(
        as_of,
        main_years=int(window_config["main_years"]),
        recent_years=int(window_config["recent_confirmation_years"]),
        failure_months=int(window_config["failure_check_months"]),
        allowed_nodes=config["allowed_update_nodes"],
    )
    paths = {name: resolve_path(value) for name, value in config["data"].items() if name != "report_root"}
    calendar = pd.read_csv(paths["trade_calendar"], dtype={"cal_date": str}, low_memory=False)
    action_dates = open_dates(calendar, windows.main.start, windows.main.end)
    previous_signal_date = prior_open_date(calendar, windows.main.start)

    daily_source = pd.read_csv(paths["strict_daily_source"], low_memory=False)
    for column in ("trade_date", "as_of_date", "model_training_end_date"):
        daily_source[column] = daily_source[column].astype(str).str.replace(r"\.0$", "", regex=True)
    strict_audit = audit_point_in_time_frame(
        daily_source,
        PointInTimeContract(
            dataset_name="acde_rolling_strict_daily_source",
            expected_method=EXPECTED_FILL_METHOD,
        ),
    ).to_dict()
    daily_coverage = coverage_audit(
        daily_source,
        date_column="trade_date",
        required_start=previous_signal_date,
        required_end=windows.main.end,
    )
    feature_dates = read_dates(paths["strict_feature_pool"], "trade_date")
    feature_coverage = coverage_audit(
        feature_dates,
        date_column="trade_date",
        required_start=previous_signal_date,
        required_end=windows.main.end,
    )
    sentiment_dates = read_dates(paths["market_sentiment"], "trade_date")
    sentiment_coverage = coverage_audit(
        sentiment_dates,
        date_column="trade_date",
        required_start=previous_signal_date,
        required_end=windows.main.end,
    )
    d_events = pd.read_csv(
        paths["d_event_source"],
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    d_coverage = coverage_audit(
        d_events,
        date_column="trade_date",
        required_start=windows.main.start,
        required_end=windows.main.end,
    )
    d_denominator_audit = audit_d_target_denominator(
        paths=paths,
        calendar=calendar,
        event_source=d_events,
        required_start=windows.main.start,
        required_end=windows.main.end,
        frozen_contract=config.get("d_quality_contracts", {}).get(as_of),
    )

    gates = {
        "strict_asof_source": bool(strict_audit.get("passed")),
        "strict_daily_three_year_coverage": bool(daily_coverage.get("passed")),
        "strict_feature_three_year_coverage": bool(feature_coverage.get("passed")),
        "market_sentiment_three_year_coverage": bool(sentiment_coverage.get("passed")),
        "d_minute_event_three_year_coverage": bool(d_coverage.get("passed")),
        "d_target_denominator_fail_closed": bool(d_denominator_audit.get("passed")),
        "action_date_calendar_nonempty": bool(action_dates),
    }
    blocking = [name for name, passed in gates.items() if not passed]
    return {
        "schema_version": 1,
        "mode": "research_only",
        "formal_strategy_modified": False,
        "priority": list(FIXED_PRIORITY),
        "windows": windows.to_dict(),
        "window_boundary_policy": {
            "metric_date": "action_date",
            "main_action_day_count": len(action_dates),
            "first_action_date": action_dates[0] if action_dates else "",
            "last_action_date": action_dates[-1] if action_dates else "",
            "previous_signal_date_loaded_for_static_plans": previous_signal_date,
        },
        "data_audits": {
            "strict_asof": strict_audit,
            "strict_daily": daily_coverage,
            "strict_feature_pool": feature_coverage,
            "market_sentiment": sentiment_coverage,
            "d_minute_events": d_coverage,
            "d_target_denominator": d_denominator_audit,
        },
        "input_fingerprints": {
            name: {"path": str(path.relative_to(ROOT)), "sha256": sha256_path(path)}
            for name, path in paths.items()
        },
        "readiness_gates": gates,
        "blocking_gates": blocking,
        "optimization_started": not blocking,
        "status": "READY_FOR_THREE_YEAR_OPTIMIZATION" if not blocking else "BLOCKED_BY_DATA_READINESS",
        "next_action": (
            "逐腿冻结其他三腿并开始候选搜索"
            if not blocking
            else "先补齐阻断门禁的数据；禁止用日线代理冒充D分钟回封事件"
        ),
    }


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_artifact_manifest(
    *,
    output_dir: Path,
    as_of: str,
    readiness: dict[str, Any],
) -> Path:
    """冻结本轮代码、配置、输入和产物哈希；不把manifest自身放入清单。"""

    # 不只记录最终入口；数据生成、候选构造、收益连乘和风险标记的
    # 直接依赖也必须入清单，否则工作树代码漂移时仍会伪装“可复现”。
    explicit_sources = [
        ROOT / "config/acde_rolling_optimization.json",
        ROOT / "config/config.json",
        ROOT / "config/strategy_config.json",
        ROOT / "config/strategy_e_r1_scenarios.json",
        ROOT / "config/strategy_d_factor_release.json",
        ROOT / "config/strategy_d_intraday_collection.json",
        ROOT / "config/strategy_d_intraday_known_data_gaps.json",
        ROOT / "scripts/optimize_acde_rolling_three_year.py",
        ROOT / "scripts/build_ac_daily_candidates.py",
        ROOT / "scripts/build_strategy_d_intraday_event_ledger.py",
        ROOT / "scripts/build_strategy_d_reseal_events_window.py",
        ROOT / "scripts/certify_current_executable_portfolio.py",
        ROOT / "scripts/collect_strategy_d_intraday_tushare_1m.py",
        ROOT / "scripts/collect_strategy_d_stk_limit_history.py",
        ROOT / "scripts/merge_strategy_d_reseal_event_windows.py",
        ROOT / "scripts/research_strategy_d_explosion_features.py",
        ROOT / "scripts/research_strategy_d_full_window_features_and_gates.py",
        ROOT / "scripts/research_strategy_d_reseal_combinations.py",
        ROOT / "scripts/research_strategy_d_six_schools.py",
        ROOT / "scripts/run_paper_ab_filtered_daily_ops.py",
        ROOT / "scripts/run_paper_ab_filtered_observation_window.py",
        ROOT / "scripts/validate_other_live_strategies_strict.py",
        ROOT / "src/acde_rolling_framework.py",
        ROOT / "src/acde_rolling_candidates.py",
        ROOT / "src/adjusted_returns.py",
        ROOT / "src/data_source.py",
        ROOT / "src/market_rules.py",
        ROOT / "src/mechanical_compound.py",
        ROOT / "src/paper_candidate_generator.py",
        ROOT / "src/secret_config.py",
        ROOT / "src/strategy_d_factor_rules.py",
        ROOT / "src/strategy_d_intraday_ledger.py",
        ROOT / "src/strategy_d_spec.py",
        ROOT / "src/strategy_e.py",
        ROOT / "src/strict_asof.py",
        ROOT / "src/trading_fees.py",
        ROOT / "src/utils/config.py",
        ROOT / "tests/test_acde_rolling_framework.py",
        ROOT / "tests/test_strategy_d_stk_limit_history.py",
        ROOT / "tests/test_strict_action_date_replay.py",
    ]
    # Python的间接导入链会随业务模块演进；在显式的数据生成链之外，
    # 自动纳入本次进程已加载的所有项目内Python源文件，防止间接依赖漏哈希。
    loaded_project_sources: set[Path] = set()
    for module in tuple(sys.modules.values()):
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        candidate = Path(str(module_file)).resolve()
        if candidate.suffix != ".py" or not candidate.exists():
            continue
        try:
            candidate.relative_to(ROOT.resolve())
        except ValueError:
            continue
        loaded_project_sources.add(candidate)
    tracked_sources = sorted(
        {path.resolve() for path in explicit_sources} | loaded_project_sources,
        key=lambda path: path.relative_to(ROOT.resolve()).as_posix(),
    )
    missing = [str(path.relative_to(ROOT)) for path in tracked_sources if not path.exists()]
    if missing:
        raise RuntimeError(f"产物清单缺少代码或配置：{missing}")
    try:
        git_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        git_dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        git_commit = ""
        git_dirty = True
    artifact_path = output_dir / "artifact_manifest.json"
    artifacts = {
        str(path.relative_to(output_dir)): sha256_path(path)
        for path in sorted(output_dir.rglob("*"))
        if path.is_file() and path != artifact_path
    }
    payload = {
        "schema_version": 1,
        "as_of": str(as_of),
        "mode": "research_only",
        "code_fingerprint_policy": (
            "EXPLICIT_DATA_BUILD_CHAIN_PLUS_LOADED_PROJECT_PYTHON_MODULES"
        ),
        "git_commit": git_commit,
        "git_worktree_dirty": git_dirty,
        "code_config_fingerprints": {
            str(path.relative_to(ROOT)): sha256_path(path) for path in tracked_sources
        },
        "input_fingerprints": readiness.get("input_fingerprints", {}),
        "artifact_fingerprints": artifacts,
    }
    artifact_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return artifact_path


def build_variant_plan(
    variant: VariantDefinition,
    *,
    signal_pool: pd.DataFrame,
    d_events: pd.DataFrame,
    allowed_action_dates: set[str],
    cutoff: str,
    outcome_cache: StaticOutcomeCache,
) -> pd.DataFrame:
    leg = variant.strategy_leg
    if leg == "A":
        frame = static_plan_outcomes(
            build_a_picks(signal_pool, variant.payload),
            leg="A",
            cutoff=cutoff,
            cache=outcome_cache,
        )
    elif leg == "C":
        frame = static_plan_outcomes(
            build_c_picks(signal_pool, variant.payload),
            leg="C",
            cutoff=cutoff,
            cache=outcome_cache,
        )
    elif leg == "E":
        frame = static_plan_outcomes(
            build_e_picks(signal_pool, variant.payload),
            leg="E",
            cutoff=cutoff,
            cache=outcome_cache,
            e_spec=variant.payload,
        )
    elif leg == "D":
        frame = build_d_plans(
            d_events,
            variant.payload,
            allowed_action_dates=allowed_action_dates,
            cutoff=cutoff,
        )
    else:
        raise ValueError(f"未知策略腿：{leg}")
    frame = frame.copy()
    if frame.empty:
        frame["action_date"] = pd.Series(dtype="object")
        return frame
    source_column = "signal_date" if leg == "D" else "buy_date"
    if source_column not in frame.columns:
        raise RuntimeError(f"{leg}计划缺少显式action_date来源字段{source_column}")
    frame["action_date"] = frame[source_column].map(normalize_date)
    if frame["action_date"].eq("").any():
        raise RuntimeError(f"{leg}计划存在空action_date")
    return frame


def flatten_evaluation(
    *,
    variant: VariantDefinition,
    signature: str,
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "strategy_leg": variant.strategy_leg,
        "variant_id": variant.variant_id,
        "description": variant.description,
        "changed_axis_count": variant.changed_axis_count,
        "plan_signature": signature,
        "style_gate_passed": bool(variant.style_gate_passed),
        "style_gate_reason": variant.style_gate_reason,
        "main_gate_passed": bool(evaluation["main_gate_passed"]),
        "main_gate_reasons": ";".join(evaluation["main_gate_reasons"]),
        "recent_confirmation_passed": bool(
            evaluation["recent_confirmation_passed"]
        ),
        "recent_confirmation_reasons": ";".join(
            evaluation["recent_confirmation_reasons"]
        ),
        "selection_gate_passed": bool(evaluation["selection_gate_passed"]),
        "replacement_gate_passed": bool(evaluation["replacement_gate_passed"]),
        "selection_gate_reasons": ";".join(evaluation["selection_gate_reasons"]),
        "failure_flags": ";".join(evaluation["failure_flags"]),
    }
    for window_name, scopes in evaluation["metrics"].items():
        for scope_name in (
            "baseline_standalone",
            "candidate_standalone",
            "baseline_portfolio",
            "candidate_portfolio",
        ):
            for key, value in scopes[scope_name].items():
                if key == "leg_counts":
                    row[f"{window_name}_{scope_name}_{key}"] = json.dumps(
                        value, ensure_ascii=False, sort_keys=True
                    )
                else:
                    row[f"{window_name}_{scope_name}_{key}"] = value
    main = evaluation["metrics"]["main"]
    ratios = []
    for scope in ("standalone", "portfolio"):
        baseline = float(main[f"baseline_{scope}"]["equity_multiple"])
        candidate = float(main[f"candidate_{scope}"]["equity_multiple"])
        ratios.append(math.log(candidate / baseline) if baseline > 0 and candidate > 0 else -math.inf)
    row["main_min_log_compound_improvement"] = min(ratios)
    return row


def candidate_rank_key(row: Mapping[str, Any]) -> tuple[float, int, float, int, str]:
    """候选只按三年主窗排序；两年和半年字段不得进入排序键。"""

    return (
        float(row["main_min_log_compound_improvement"]),
        -int(row["changed_axis_count"]),
        float(row.get("main_candidate_standalone_max_drawdown", -1.0)),
        int(row.get("main_candidate_standalone_trade_count", 0)),
        str(row["variant_id"]),
    )


def select_main_window_winner(
    rows: Sequence[Mapping[str, Any]],
    *,
    require_style_gate: bool,
) -> Mapping[str, Any] | None:
    """只依据三年主窗选唯一第一名；后续窗口无权改选或递补。"""

    eligible = [
        row for row in rows
        if row.get("evaluation_status") == "EVALUATED"
        and bool(row.get("main_gate_passed"))
        and (not require_style_gate or bool(row.get("style_gate_passed")))
    ]
    eligible.sort(key=candidate_rank_key, reverse=True)
    return eligible[0] if eligible else None


def compact_candidate_summary(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    result: dict[str, Any] = {
        "variant_id": str(row["variant_id"]),
        "description": str(row["description"]),
        "style_gate_passed": bool(row["style_gate_passed"]),
        "style_gate_reason": str(row["style_gate_reason"]),
        "main_gate_passed": bool(row.get("main_gate_passed", False)),
        "main_gate_reasons": str(row.get("main_gate_reasons", "")),
        "recent_confirmation_passed": bool(
            row.get("recent_confirmation_passed", False)
        ),
        "recent_confirmation_reasons": str(
            row.get("recent_confirmation_reasons", "")
        ),
        "selection_gate_passed": bool(row.get("selection_gate_passed", False)),
        "replacement_gate_passed": bool(row.get("replacement_gate_passed", False)),
        "selection_gate_reasons": str(row.get("selection_gate_reasons", "")),
        "failure_flags": str(row.get("failure_flags", "")),
    }
    for window in ("main", "recent", "failure_check"):
        for scope in ("standalone", "portfolio"):
            result[f"{window}_{scope}_trade_count"] = int(
                row[f"{window}_candidate_{scope}_trade_count"]
            )
            result[f"{window}_{scope}_equity_multiple"] = float(
                row[f"{window}_candidate_{scope}_equity_multiple"]
            )
            result[f"{window}_{scope}_max_drawdown"] = float(
                row[f"{window}_candidate_{scope}_max_drawdown"]
            )
    return result


def window_metrics(
    legs: dict[str, pd.DataFrame],
    *,
    calendar: pd.DataFrame,
    windows: Any,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, dict[str, Any]]]:
    dates = open_dates(calendar, windows.main.start, windows.main.end)
    detail = replay_action_date_portfolio(legs, action_dates=dates)
    portfolio = {
        window.name: action_metrics(detail, window.start, window.end)
        for window in (windows.main, windows.recent, windows.failure_check)
    }
    standalone: dict[str, dict[str, Any]] = {}
    for leg in FIXED_PRIORITY:
        leg_detail = standalone_replay(legs[leg], leg, action_dates=dates)
        standalone[leg] = {
            window.name: action_metrics(leg_detail, window.start, window.end)
            for window in (windows.main, windows.recent, windows.failure_check)
        }
    return detail, portfolio, standalone


def evaluate_selected_bundle(
    *,
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
    selected_legs: list[str],
    gate: dict[str, Any],
) -> dict[str, Any]:
    """多腿独立胜者合并后再做一次组合级门禁，防止交互抵消。"""

    if not selected_legs:
        return {
            "selected_legs": [],
            "selection_gate_passed": True,
            "replacement_gate_passed": True,
            "reasons": [],
            "status": "NO_RESEARCH_REPLACEMENT_SELECTED",
        }
    reasons: list[str] = []
    for window_name, retention_key, drawdown_key in (
        ("main", "minimum_main_sample_retention", "maximum_main_drawdown_worsening_pp"),
        ("recent", "minimum_recent_sample_retention", "maximum_recent_drawdown_worsening_pp"),
    ):
        old = baseline[window_name]
        new = candidate[window_name]
        if float(new["equity_multiple"]) <= float(old["equity_multiple"]) + 1e-12:
            reasons.append(f"{window_name.upper()}_PORTFOLIO_COMPOUND")
        if int(new["trade_count"]) < int(
            np.ceil(int(old["trade_count"]) * float(gate[retention_key]))
        ):
            reasons.append(f"{window_name.upper()}_PORTFOLIO_SAMPLE_RETENTION")
        if float(new["max_drawdown"]) < (
            float(old["max_drawdown"]) - float(gate[drawdown_key])
        ):
            reasons.append(f"{window_name.upper()}_PORTFOLIO_DRAWDOWN")
    selection_reasons = list(reasons)
    half = candidate["failure_check"]
    if int(half["trade_count"]) >= int(gate["failure_check_minimum_trades"]):
        equity_failed = float(half["equity_multiple"]) <= float(
            gate["failure_check_equity_floor"]
        )
        if bool(gate.get("failure_check_require_nonpositive_average_with_equity_failure", False)):
            equity_failed = equity_failed and float(half["avg_account_return"]) <= 0.0
        if equity_failed:
            reasons.append("HALF_YEAR_PORTFOLIO_EQUITY")
        if float(half["max_drawdown"]) < float(gate["failure_check_drawdown_floor"]):
            reasons.append("HALF_YEAR_PORTFOLIO_DRAWDOWN")
    return {
        "selected_legs": selected_legs,
        "selection_gate_passed": not selection_reasons,
        "replacement_gate_passed": not reasons,
        "reasons": reasons,
        "status": "BUNDLE_GATE_PASSED" if not reasons else "BUNDLE_GATE_FAILED",
    }


def period_breakdown(
    detail: pd.DataFrame,
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    """动态拆分主窗口内的自然半年，避免未来节点仍误用2023~2026固定表。"""

    start_date = dt.datetime.strptime(str(start), "%Y%m%d").date()
    end_date = dt.datetime.strptime(str(end), "%Y%m%d").date()
    if start_date > end_date:
        raise ValueError(f"半年分解窗口无效：{start}>{end}")
    cursor = dt.date(
        start_date.year,
        1 if start_date.month <= 6 else 7,
        1,
    )
    periods: dict[str, tuple[str, str]] = {}
    while cursor <= end_date:
        if cursor.month == 1:
            label = f"{cursor.year}H1"
            period_end = dt.date(cursor.year, 6, 30)
            next_cursor = dt.date(cursor.year, 7, 1)
        else:
            label = f"{cursor.year}H2"
            period_end = dt.date(cursor.year, 12, 31)
            next_cursor = dt.date(cursor.year + 1, 1, 1)
        low = max(cursor, start_date).strftime("%Y%m%d")
        high = min(period_end, end_date).strftime("%Y%m%d")
        periods[label] = (low, high)
        cursor = next_cursor
    return {
        name: action_metrics(detail, low, high)
        for name, (low, high) in periods.items()
    }


def selected_plan_change_ledger(
    baseline_legs: dict[str, pd.DataFrame],
    final_legs: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """逐信号日列出最终研究版相对当前基线真正改变的冻结计划。"""

    compare_columns = [
        "buy_date",
        "ts_code",
        "name",
        "matched_condition_profile_ids",
        "status",
        "exit_rule",
        "exit_date",
        "account_return",
        "entry_filled",
        "position_opened",
    ]
    ledgers: list[pd.DataFrame] = []
    for leg in FIXED_PRIORITY:
        sides: list[pd.DataFrame] = []
        for frame in (baseline_legs[leg], final_legs[leg]):
            normalized = frame.copy()
            if "signal_date" not in normalized.columns:
                normalized["signal_date"] = pd.Series(dtype="object")
            normalized["signal_date"] = normalized["signal_date"].map(normalize_date)
            if normalized["signal_date"].duplicated().any():
                raise RuntimeError(f"{leg}计划变更审计发现同一signal_date重复")
            for column in compare_columns:
                if column not in normalized.columns:
                    normalized[column] = ""
            sides.append(normalized[["signal_date", *compare_columns]].copy())
        merged = sides[0].merge(
            sides[1],
            on="signal_date",
            how="outer",
            suffixes=("_baseline", "_final"),
            indicator=True,
        )
        changed = ~merged["_merge"].eq("both")
        for column in compare_columns:
            left = merged[f"{column}_baseline"].fillna("").astype(str)
            right = merged[f"{column}_final"].fillna("").astype(str)
            changed |= ~left.eq(right)
        merged = merged.loc[changed].copy()
        if merged.empty:
            continue
        merged.insert(0, "strategy_leg", leg)
        merged["change_type"] = merged["_merge"].map(
            {"left_only": "REMOVED", "right_only": "ADDED", "both": "REPLACED"}
        ).astype(str)
        ledgers.append(merged.drop(columns="_merge"))
    if not ledgers:
        return pd.DataFrame(columns=["strategy_leg", "signal_date", "change_type"])
    return pd.concat(ledgers, ignore_index=True).sort_values(
        ["signal_date", "strategy_leg"]
    ).reset_index(drop=True)


def portfolio_decision_change_ledger(
    baseline_detail: pd.DataFrame,
    final_detail: pd.DataFrame,
) -> pd.DataFrame:
    """只比较当日裁决和收益，排除后续equity_after传播造成的伪差异。"""

    compare_columns = [
        "status",
        "execution_status",
        "strategy_leg",
        "ts_code",
        "name",
        "exit_date",
        "account_return",
    ]
    left = baseline_detail[["action_date", *compare_columns]].copy()
    right = final_detail[["action_date", *compare_columns]].copy()
    if left["action_date"].duplicated().any() or right["action_date"].duplicated().any():
        raise RuntimeError("组合裁决变更审计发现action_date重复")
    merged = left.merge(
        right,
        on="action_date",
        how="outer",
        suffixes=("_baseline", "_final"),
        indicator=True,
    )
    changed = ~merged["_merge"].eq("both")
    for column in compare_columns:
        old = merged[f"{column}_baseline"].fillna("").astype(str)
        new = merged[f"{column}_final"].fillna("").astype(str)
        changed |= ~old.eq(new)
    result = merged.loc[changed].copy().drop(columns="_merge")
    if result.empty:
        return result
    old_return = pd.to_numeric(result["account_return_baseline"], errors="coerce").fillna(0.0)
    new_return = pd.to_numeric(result["account_return_final"], errors="coerce").fillna(0.0)
    result["relative_equity_effect"] = (1.0 + new_return) / (1.0 + old_return)
    return result.sort_values("action_date").reset_index(drop=True)


def run_three_year_optimization(
    config: dict[str, Any],
    readiness: dict[str, Any],
    *,
    output_dir: Path,
) -> dict[str, Any]:
    if readiness["status"] != "READY_FOR_THREE_YEAR_OPTIMIZATION":
        raise RuntimeError("数据门禁未通过，禁止启动三年优化")
    windows_payload = readiness["windows"]
    windows = build_window_set(
        windows_payload["update_node"],
        main_years=int(config["windows"]["main_years"]),
        recent_years=int(config["windows"]["recent_confirmation_years"]),
        failure_months=int(config["windows"]["failure_check_months"]),
        allowed_nodes=config["allowed_update_nodes"],
    )
    paths = {
        name: resolve_path(value)
        for name, value in config["data"].items()
        if name != "report_root"
    }
    calendar = pd.read_csv(paths["trade_calendar"], dtype={"cal_date": str}, low_memory=False)
    sentiment = pd.read_csv(paths["market_sentiment"], dtype={"trade_date": str}, low_memory=False)
    action_dates = open_dates(calendar, windows.main.start, windows.main.end)
    controller = config["market_controller"]
    allowed_actions, allowed_signals, gate_frame = previous_close_market_gate(
        calendar=calendar,
        sentiment=sentiment,
        action_dates=action_dates,
        minimum_limit_up_count=int(controller["minimum_limit_up_count"]),
    )
    all_signal_dates = set(gate_frame["state_date"].astype(str))
    feature_pool = pd.read_csv(paths["strict_feature_pool"], low_memory=False)
    signal_pool = strict_signal_pool(
        feature_pool,
        signal_dates=all_signal_dates,
        allowed_signal_dates=allowed_signals,
    )
    d_events = pd.read_csv(
        paths["d_event_source"],
        dtype={"trade_date": str, "ts_code": str},
        low_memory=False,
    )
    d_events["trade_date"] = d_events["trade_date"].astype(str).str.replace(
        r"\.0$", "", regex=True
    )
    if d_events["event_id"].duplicated().any():
        raise RuntimeError("D三年事件账本event_id重复")
    d_events = d_events[d_events["trade_date"].between(windows.main.start, windows.main.end)].copy()

    base_config = load_json_config(STRATEGY_CONFIG)
    system_config = load_json_config(ROOT / "config/config.json")
    analysis_config = system_config["analysis"]
    certification_config = system_config["portfolio_certification"]
    e_spec = load_e_spec(ROOT)
    d_release = load_factor_release(D_RELEASE_CONFIG)
    baseline_a, candidates_a = a_variants(base_config)
    baseline_c, candidates_c = c_variants(base_config)
    baseline_e, candidates_e = e_variants(e_spec)
    baseline_d, candidates_d = d_variants(d_release)
    baselines = {
        item.strategy_leg: item
        for item in (baseline_a, baseline_c, baseline_e, baseline_d)
    }
    candidate_definitions = {
        "A": candidates_a,
        "C": candidates_c,
        "E": candidates_e,
        "D": candidates_d,
    }
    catalog = variant_catalog_payload(
        [*baselines.values(), *candidates_a, *candidates_c, *candidates_e, *candidates_d]
    )
    (output_dir / "variant_catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    gate_frame.to_csv(output_dir / "market_hard_gate_by_action_date.csv", index=False, encoding="utf-8-sig")

    outcome_cache = StaticOutcomeCache()
    baseline_frames: dict[str, pd.DataFrame] = {}
    candidate_frames: dict[str, dict[str, pd.DataFrame]] = {leg: {} for leg in FIXED_PRIORITY}
    for leg in FIXED_PRIORITY:
        LOGGER.info("构建%s当前基线计划", leg)
        baseline_frames[leg] = build_variant_plan(
            baselines[leg],
            signal_pool=signal_pool,
            d_events=d_events,
            allowed_action_dates=allowed_actions,
            cutoff=windows.main.end,
            outcome_cache=outcome_cache,
        )
        seen = {plan_signature(baseline_frames[leg])}
        for variant in candidate_definitions[leg]:
            LOGGER.info("构建%s候选：%s", leg, variant.variant_id)
            frame = build_variant_plan(
                variant,
                signal_pool=signal_pool,
                d_events=d_events,
                allowed_action_dates=allowed_actions,
                cutoff=windows.main.end,
                outcome_cache=outcome_cache,
            )
            signature = plan_signature(frame)
            if signature in seen:
                candidate_frames[leg][variant.variant_id] = frame
                continue
            seen.add(signature)
            candidate_frames[leg][variant.variant_id] = frame

    baseline_dir = output_dir / "baseline_plans"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    for leg, frame in baseline_frames.items():
        frame.to_csv(baseline_dir / f"{leg.lower()}_plans.csv", index=False, encoding="utf-8-sig")

    baseline_detail, baseline_portfolio, baseline_standalone = window_metrics(
        baseline_frames, calendar=calendar, windows=windows
    )

    # 每条腿都必须相对同一个不可变正式基线做“一次只替换一腿”评估。
    # 不能把前一条研究胜者串行写进后一条腿的基线，否则E/D等候选的组合门禁
    # 会依赖尚未正式发布的C/A候选。
    current_legs = {leg: frame.copy() for leg, frame in baseline_frames.items()}
    selected_frames: dict[str, pd.DataFrame] = {}
    evaluation_rows: list[dict[str, Any]] = []
    decisions: dict[str, Any] = {}
    for leg in FIXED_PRIORITY:
        baseline_leg = baseline_frames[leg]
        baseline_signature = plan_signature(baseline_leg)
        seen_signatures = {baseline_signature}
        leg_rows: list[dict[str, Any]] = []
        variant_by_id = {item.variant_id: item for item in candidate_definitions[leg]}
        for variant in candidate_definitions[leg]:
            candidate_leg = candidate_frames[leg][variant.variant_id]
            signature = plan_signature(candidate_leg)
            if signature in seen_signatures:
                row = {
                    "strategy_leg": leg,
                    "variant_id": variant.variant_id,
                    "description": variant.description,
                    "changed_axis_count": variant.changed_axis_count,
                    "plan_signature": signature,
                    "style_gate_passed": bool(variant.style_gate_passed),
                    "style_gate_reason": variant.style_gate_reason,
                    "evaluation_status": "DUPLICATE_PLAN_SIGNATURE",
                    "main_gate_passed": False,
                    "main_gate_reasons": "DUPLICATE_PLAN_SIGNATURE",
                    "recent_confirmation_passed": False,
                    "recent_confirmation_reasons": "DUPLICATE_PLAN_SIGNATURE",
                    "selection_gate_passed": False,
                    "replacement_gate_passed": False,
                    "main_min_log_compound_improvement": -math.inf,
                }
            else:
                seen_signatures.add(signature)
                evaluation = evaluate_three_window_replacement(
                    leg=leg,
                    baseline_leg=baseline_leg,
                    candidate_leg=candidate_leg,
                    frozen_other_legs={
                        name: frame for name, frame in baseline_frames.items() if name != leg
                    },
                    calendar=calendar,
                    windows=windows,
                    gate=config["replacement_gate"],
                )
                row = {
                    **flatten_evaluation(
                        variant=variant,
                        signature=signature,
                        evaluation=evaluation,
                    ),
                    "evaluation_status": "EVALUATED",
                }
            leg_rows.append(row)
            evaluation_rows.append(row)

        main_passed = [
            row for row in leg_rows
            if row.get("evaluation_status") == "EVALUATED"
            and bool(row.get("main_gate_passed"))
        ]
        mathematical_winner = select_main_window_winner(
            leg_rows, require_style_gate=False
        )
        style_main_eligible = [
            row for row in main_passed if bool(row.get("style_gate_passed"))
        ]
        style_evaluated = [
            row for row in leg_rows
            if row.get("evaluation_status") == "EVALUATED"
            and bool(row.get("style_gate_passed"))
        ]
        style_evaluated.sort(key=candidate_rank_key, reverse=True)
        best_style_candidate = style_evaluated[0] if style_evaluated else None
        winner = select_main_window_winner(leg_rows, require_style_gate=True)
        promoted = bool(winner and winner.get("replacement_gate_passed"))
        if promoted and winner is not None:
            selected_frames[leg] = candidate_frames[leg][str(winner["variant_id"])]
        mathematical_winner_summary = compact_candidate_summary(mathematical_winner)
        decisions[leg] = {
            "baseline_variant_id": baselines[leg].variant_id,
            "candidate_count": len(candidate_definitions[leg]),
            "unique_candidate_signature_count": len(seen_signatures) - 1,
            "main_gate_passed_count": len(main_passed),
            "style_eligible_main_gate_passed_count": len(style_main_eligible),
            "main_recent_gate_passed_count": sum(
                bool(row.get("recent_confirmation_passed")) for row in main_passed
            ),
            "style_eligible_main_recent_gate_passed_count": sum(
                bool(row.get("recent_confirmation_passed"))
                for row in style_main_eligible
            ),
            "mathematical_winner": mathematical_winner_summary,
            "best_style_candidate": compact_candidate_summary(best_style_candidate),
            "selected_variant_id": str(winner["variant_id"]) if winner else "",
            "selected_description": str(winner["description"]) if winner else "",
            "selected_recent_confirmation_passed": bool(
                winner and winner.get("recent_confirmation_passed")
            ),
            "selected_recent_confirmation_reasons": str(
                winner.get("recent_confirmation_reasons", "") if winner else ""
            ),
            "half_year_failure_flags": str(winner.get("failure_flags", "")) if winner else "",
            "promoted_in_research": promoted,
            "decision": (
                "RESEARCH_CANDIDATE_PROMOTED"
                if promoted
                else (
                    "BEST_MAIN_CANDIDATE_REJECTED_BY_RECENT_CONFIRMATION"
                    if winner and not bool(winner.get("recent_confirmation_passed"))
                    else (
                        "BEST_MAIN_CANDIDATE_REJECTED_BY_HALF_YEAR_FAILURE"
                        if winner and bool(winner.get("failure_flags"))
                        else (
                            "KEEP_CURRENT_STYLE_GATE"
                            if mathematical_winner is not None
                            else "KEEP_CURRENT_NO_CANDIDATE_PASSED_MAIN"
                        )
                    )
                )
            ),
        }

    for leg, frame in selected_frames.items():
        current_legs[leg] = frame.copy()

    evaluation_frame = pd.DataFrame(evaluation_rows)
    evaluated_mask = evaluation_frame["evaluation_status"].astype(str).eq("EVALUATED")
    for window_name in ("main", "recent", "failure_check"):
        portfolio_column = f"{window_name}_baseline_portfolio_equity_multiple"
        if evaluated_mask.any() and not np.allclose(
            pd.to_numeric(
                evaluation_frame.loc[evaluated_mask, portfolio_column], errors="raise"
            ),
            float(baseline_portfolio[window_name]["equity_multiple"]),
            rtol=0.0,
            atol=1e-12,
        ):
            raise RuntimeError(f"{window_name}候选未相对同一个不可变组合基线评估")
        for leg in FIXED_PRIORITY:
            leg_mask = evaluated_mask & evaluation_frame["strategy_leg"].astype(str).eq(leg)
            standalone_column = f"{window_name}_baseline_standalone_equity_multiple"
            if leg_mask.any() and not np.allclose(
                pd.to_numeric(
                    evaluation_frame.loc[leg_mask, standalone_column], errors="raise"
                ),
                float(baseline_standalone[leg][window_name]["equity_multiple"]),
                rtol=0.0,
                atol=1e-12,
            ):
                raise RuntimeError(f"{leg}/{window_name}候选未相对不可变单腿基线评估")
    evaluation_frame.to_csv(
        output_dir / "candidate_evaluations.csv", index=False, encoding="utf-8-sig"
    )
    proposed_detail, proposed_portfolio, proposed_standalone = window_metrics(
        current_legs, calendar=calendar, windows=windows
    )
    bundle_gate = evaluate_selected_bundle(
        baseline=baseline_portfolio,
        candidate=proposed_portfolio,
        selected_legs=sorted(selected_frames, key=FIXED_PRIORITY.index),
        gate=config["replacement_gate"],
    )
    proposed_bundle = {
        "portfolio": proposed_portfolio,
        "standalone": proposed_standalone,
    }
    if not bool(bundle_gate["replacement_gate_passed"]):
        rejected_dir = output_dir / "rejected_bundle_plans"
        rejected_dir.mkdir(parents=True, exist_ok=True)
        for leg, frame in current_legs.items():
            frame.to_csv(
                rejected_dir / f"{leg.lower()}_plans.csv",
                index=False,
                encoding="utf-8-sig",
            )
        for leg in selected_frames:
            decisions[leg]["promoted_in_research"] = False
            decisions[leg]["decision"] = "RESEARCH_CANDIDATE_REJECTED_BY_BUNDLE_GATE"
        current_legs = {leg: frame.copy() for leg, frame in baseline_frames.items()}
        final_detail = baseline_detail.copy()
        final_portfolio = copy.deepcopy(baseline_portfolio)
        final_standalone = copy.deepcopy(baseline_standalone)
    else:
        final_detail = proposed_detail
        final_portfolio = proposed_portfolio
        final_standalone = proposed_standalone
    plan_changes = selected_plan_change_ledger(baseline_frames, current_legs)
    portfolio_changes = portfolio_decision_change_ledger(
        baseline_detail, final_detail
    )
    baseline_detail.to_csv(output_dir / "baseline_portfolio_replay.csv", index=False, encoding="utf-8-sig")
    final_detail.to_csv(output_dir / "final_portfolio_replay.csv", index=False, encoding="utf-8-sig")
    plan_changes.to_csv(
        output_dir / "selected_plan_changes.csv", index=False, encoding="utf-8-sig"
    )
    portfolio_changes.to_csv(
        output_dir / "portfolio_decision_changes.csv", index=False, encoding="utf-8-sig"
    )
    if portfolio_changes.empty:
        direct_return_changes = portfolio_changes.copy()
    else:
        direct_return_changes = portfolio_changes.loc[
            ~np.isclose(
                pd.to_numeric(
                    portfolio_changes["relative_equity_effect"], errors="coerce"
                ).fillna(1.0),
                1.0,
                rtol=0.0,
                atol=1e-12,
            )
        ].copy()
    final_dir = output_dir / "final_research_plans"
    final_dir.mkdir(parents=True, exist_ok=True)
    for leg, frame in current_legs.items():
        frame.to_csv(final_dir / f"{leg.lower()}_plans.csv", index=False, encoding="utf-8-sig")

    baseline_half_year = period_breakdown(
        baseline_detail, start=windows.main.start, end=windows.main.end
    )
    final_half_year = period_breakdown(
        final_detail, start=windows.main.start, end=windows.main.end
    )
    baseline_standalone_half_year: dict[str, dict[str, Any]] = {}
    final_standalone_half_year: dict[str, dict[str, Any]] = {}
    for leg in FIXED_PRIORITY:
        baseline_leg_detail = standalone_replay(
            baseline_frames[leg], leg, action_dates=action_dates
        )
        final_leg_detail = standalone_replay(
            current_legs[leg], leg, action_dates=action_dates
        )
        baseline_standalone_half_year[leg] = period_breakdown(
            baseline_leg_detail, start=windows.main.start, end=windows.main.end
        )
        final_standalone_half_year[leg] = period_breakdown(
            final_leg_detail, start=windows.main.start, end=windows.main.end
        )

    style_rejected_math_winners = [
        f"{leg}:{item['mathematical_winner']['variant_id']}"
        for leg, item in decisions.items()
        if item.get("mathematical_winner")
        and not bool(item["mathematical_winner"].get("style_gate_passed"))
    ]
    strict_limits = [
        "三年主窗口与两年确认窗口重叠，两年确认不是独立样本外。",
        "两年确认只否决三年主窗选出的唯一第一名；否决后不得改选三年第二名。",
        "最近半年只在唯一胜者选出后执行失效否决，不参与排名，也不改选第二名。",
        "D一分钟OHLCV没有历史买一队列深度；始终封板的未知队列按未成交处理。",
        (
            "当前统一市场控制只实现前一交易日涨停数不少于50的硬门禁；"
            "完整冰点/修复/混沌/主升/高潮/退潮风格路由尚未形成可发布规则。"
        ),
        "V7候选空间在查看同一三年主窗后迭代扩充并于最终回放前冻结，属于样本内发现，不是事前独立验证。",
        "本轮属于STRICT_DISCOVERY；正式配置、发布冻结和实盘BUY出口均未修改。",
    ]
    if style_rejected_math_winners:
        strict_limits.insert(
            -1,
            (
                "以下数学胜者未通过固定风格门禁，只保留诊断，未进入最终研究组合："
                + "、".join(style_rejected_math_winners)
                + "。"
            ),
        )
    if len(direct_return_changes) > 0:
        strict_limits.insert(
            -1,
            (
                f"最终研究组合只有{len(direct_return_changes)}笔成交直接改变收益；"
                "收益改善存在明显增量样本集中，不能视为独立样本外证明。"
            ),
        )

    d_style_stress_ids = {
        "D_STRONG_BREAK_LT75_ACTIVE_GE20",
        "D_STRONG_BREAK_LT75_TOUCH_GE40",
    }
    d_style_stress_diagnostics = [
        compact_candidate_summary(row)
        for row in evaluation_rows
        if row.get("evaluation_status") == "EVALUATED"
        and str(row.get("variant_id")) in d_style_stress_ids
    ]

    result = {
        "schema_version": 1,
        "mode": "research_only",
        "formal_strategy_modified": False,
        "priority": list(FIXED_PRIORITY),
        "windows": windows.to_dict(),
        "readiness_sha256": sha256_path(output_dir / "readiness.json"),
        "readiness_d_target_denominator": readiness["data_audits"][
            "d_target_denominator"
        ],
        "market_controller": {
            **controller,
            "action_day_count": len(action_dates),
            "allowed_action_day_count": len(allowed_actions),
            "blocked_action_day_count": len(action_dates) - len(allowed_actions),
        },
        "data_fingerprints": {
            name: {"path": str(path.relative_to(ROOT)), "sha256": sha256_path(path)}
            for name, path in paths.items()
        },
        "candidate_space_version": config["candidate_space"]["version"],
        "candidate_space": copy.deepcopy(config["candidate_space"]),
        "d_style_stress_diagnostics": d_style_stress_diagnostics,
        "decisions": decisions,
        "selected_bundle_gate": bundle_gate,
        "formal_update_recommendation": {
            leg: (
                "MANUAL_REVIEW_RESEARCH_CANDIDATE"
                if bool(decisions[leg]["promoted_in_research"])
                else "KEEP_CURRENT"
            )
            for leg in FIXED_PRIORITY
        },
        "proposed_bundle_before_gate": proposed_bundle,
        "baseline": {
            "portfolio": baseline_portfolio,
            "standalone": baseline_standalone,
            "half_year_breakdown": baseline_half_year,
            "standalone_half_year_breakdown": baseline_standalone_half_year,
        },
        "final_research": {
            "portfolio": final_portfolio,
            "standalone": final_standalone,
            "half_year_breakdown": final_half_year,
            "standalone_half_year_breakdown": final_standalone_half_year,
        },
        "incremental_change_audit": {
            "selected_plan_change_count": int(len(plan_changes)),
            "portfolio_decision_state_change_day_count": int(len(portfolio_changes)),
            "portfolio_direct_return_change_trade_count": int(
                len(direct_return_changes)
            ),
            # 旧字段保留兼容，但它表示逐日状态差异行，不等于改变收益的成交笔数。
            "portfolio_action_change_count": int(len(portfolio_changes)),
            "changed_plan_counts_by_leg": (
                plan_changes["strategy_leg"].value_counts().sort_index().to_dict()
                if not plan_changes.empty else {}
            ),
            "direct_return_change_details": [
                {
                    "action_date": str(row["action_date"]),
                    "baseline_strategy_leg": optional_text(
                        row.get("strategy_leg_baseline", "")
                    ),
                    "baseline_ts_code": optional_text(
                        row.get("ts_code_baseline", "")
                    ),
                    "baseline_account_return": optional_float(
                        row.get("account_return_baseline")
                    ),
                    "final_strategy_leg": optional_text(
                        row.get("strategy_leg_final", "")
                    ),
                    "final_ts_code": optional_text(row.get("ts_code_final", "")),
                    "final_account_return": optional_float(
                        row.get("account_return_final")
                    ),
                }
                for row in direct_return_changes.to_dict("records")
            ],
            "main_portfolio_multiple_ratio": (
                float(final_portfolio["main"]["equity_multiple"])
                / float(baseline_portfolio["main"]["equity_multiple"])
            ),
            "recent_portfolio_multiple_ratio": (
                float(final_portfolio["recent"]["equity_multiple"])
                / float(baseline_portfolio["recent"]["equity_multiple"])
            ),
            "warning": (
                "增量变更笔数用于披露候选收益是否由极少数样本驱动；"
                "逐日状态变化包含持仓占用的衍生日，不能冒充收益变化成交；"
                "本轮预声明门禁未设置最少增量变更笔数，因此不得事后用该字段改选候选。"
            ),
        },
        "execution_assumptions": {
            "single_account": True,
            "position_pct": float(certification_config.get("position_pct", 0.825)),
            "static_buy_slippage_rate": 0.001,
            "static_sell_slippage_rate": 0.001,
            "commission_rate_each_side": float(analysis_config.get("commission_rate", 0.0003)),
            "transfer_fee_rate_each_side": float(analysis_config.get("transfer_fee_rate", 0.00001)),
            "stamp_tax_schedule_sell_side": analysis_config.get("stamp_tax_schedule", []),
            "minimum_commission_modeled": False,
            "d_fill_stress_multiplier": 0.80,
            "limit_up_open_buy_rejected": True,
            "limit_down_close_sell_delayed": True,
            "d_unknown_queue_fill": "FAIL_CLOSED_NOT_FILLED",
            "note": (
                "A/C/E按买卖各0.1%固定滑点并扣日期化费用；D按涨停价买、"
                "卖出价0.1%滑点、日期化费用及80%成交压力。未建模5元最低佣金，"
                "因此本结果不适合直接外推到很小单笔资金。"
            ),
        },
        "strict_limits": strict_limits,
        "status": "THREE_YEAR_RESEARCH_COMPLETED",
    }
    return result


def metric_text(metric: dict[str, Any]) -> str:
    return (
        f"{metric['trade_count']}笔 / {metric['equity_multiple']:.4f}倍 / "
        f"回撤{metric['max_drawdown']:.2%} / 胜率{metric['win_rate']:.2%}"
    )


def metric_table_row(label: str, metric: dict[str, Any]) -> str:
    return (
        f"| {label} | {metric['trade_count']} | {metric['win_rate']:.2%} | "
        f"{metric['avg_account_return']:.2%} | {metric['median_account_return']:.2%} | "
        f"{metric['equity_multiple']:.4f} | {metric['max_drawdown']:.2%} | "
        f"{metric['profit_loss_ratio']:.3f} | {metric['max_profit']:.2%} | "
        f"{metric['max_loss']:.2%} | {metric['max_consecutive_losses']} |"
    )


def render_optimization_report(payload: dict[str, Any]) -> str:
    lines = [
        "# A>C>E>D 三年滚动优化研究结果",
        "",
        "> 本报告为研究结果，未修改正式策略、发布冻结或实盘BUY出口。",
        "",
        "## 逐腿决定",
        "",
        "| 策略 | 候选数 | 唯一签名 | 三年通过 | 三年+风格通过 | 其中两年确认通过 | 三年唯一胜者 | 决定 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for leg in FIXED_PRIORITY:
        item = payload["decisions"][leg]
        lines.append(
            f"| {leg} | {item['candidate_count']} | {item['unique_candidate_signature_count']} | "
            f"{item['main_gate_passed_count']} | "
            f"{item['style_eligible_main_gate_passed_count']} | "
            f"{item['style_eligible_main_recent_gate_passed_count']} | "
            f"{item['selected_variant_id'] or '-'} | {item['decision']} |"
        )
    bundle = payload["selected_bundle_gate"]
    lines.extend(
        [
            "",
            f"- 独立胜者合并门禁：{bundle['status']}；入选腿："
            f"{','.join(bundle['selected_legs']) or '无'}。",
        ]
    )
    style_diagnostics = [
        (leg, payload["decisions"][leg]["mathematical_winner"])
        for leg in FIXED_PRIORITY
        if payload["decisions"][leg].get("mathematical_winner")
        and not bool(
            payload["decisions"][leg]["mathematical_winner"].get(
                "style_gate_passed"
            )
        )
    ]
    if style_diagnostics:
        lines.extend(
            [
                "",
                "## 数学胜者的固定风格门禁",
                "",
                "| 策略 | 数学胜者 | 三年单腿 | 三年组合 | 两年单腿 | 两年组合 | 结论 |",
                "| --- | --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for leg, item in style_diagnostics:
            baseline_standalone = payload["baseline"]["standalone"][leg]
            baseline_portfolio = payload["baseline"]["portfolio"]
            lines.append(
                f"| {leg} | {item['variant_id']} | "
                f"{baseline_standalone['main']['equity_multiple']:.4f}→"
                f"{item['main_standalone_equity_multiple']:.4f} | "
                f"{baseline_portfolio['main']['equity_multiple']:.4f}→"
                f"{item['main_portfolio_equity_multiple']:.4f} | "
                f"{baseline_standalone['recent']['equity_multiple']:.4f}→"
                f"{item['recent_standalone_equity_multiple']:.4f} | "
                f"{baseline_portfolio['recent']['equity_multiple']:.4f}→"
                f"{item['recent_portfolio_equity_multiple']:.4f} | "
                f"未通过：{item['style_gate_reason']} |"
            )
    best_style_diagnostics = [
        (leg, payload["decisions"][leg].get("best_style_candidate", {}))
        for leg in FIXED_PRIORITY
        if payload["decisions"][leg].get("best_style_candidate")
    ]
    if best_style_diagnostics:
        lines.extend(
            [
                "",
                "## 各腿最佳风格合法候选（包括未过收益门槛）",
                "",
                "| 策略 | 候选 | 三年单腿 | 三年组合 | 两年单腿 | 两年组合 | 门禁结论 |",
                "| --- | --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for leg, item in best_style_diagnostics:
            baseline_standalone = payload["baseline"]["standalone"][leg]
            baseline_portfolio = payload["baseline"]["portfolio"]
            if item["replacement_gate_passed"]:
                gate_result = "三窗口全部通过"
            elif not item["main_gate_passed"]:
                gate_result = "三年未通过：" + (
                    item["main_gate_reasons"] or "未记录原因"
                )
            elif not item["recent_confirmation_passed"]:
                gate_result = "两年确认否决：" + (
                    item["recent_confirmation_reasons"] or "未记录原因"
                )
            else:
                gate_result = "半年否决：" + (item["failure_flags"] or "未记录原因")
            lines.append(
                f"| {leg} | {item['variant_id']} | "
                f"{baseline_standalone['main']['equity_multiple']:.4f}→"
                f"{item['main_standalone_equity_multiple']:.4f} "
                f"({item['main_standalone_trade_count']}笔) | "
                f"{baseline_portfolio['main']['equity_multiple']:.4f}→"
                f"{item['main_portfolio_equity_multiple']:.4f} "
                f"({item['main_portfolio_trade_count']}笔) | "
                f"{baseline_standalone['recent']['equity_multiple']:.4f}→"
                f"{item['recent_standalone_equity_multiple']:.4f} "
                f"({item['recent_standalone_trade_count']}笔) | "
                f"{baseline_portfolio['recent']['equity_multiple']:.4f}→"
                f"{item['recent_portfolio_equity_multiple']:.4f} "
                f"({item['recent_portfolio_trade_count']}笔) | "
                f"{gate_result} |"
            )
    d_style_stress = payload.get("d_style_stress_diagnostics", [])
    if d_style_stress:
        lines.extend(
            [
                "",
                "## D强势环境纯度压力测试",
                "",
                "> 下表把炸板质量过滤再叠加盘中强势广度；仅用于检查D风格纯度，不参与改选第二名。",
                "",
                "| 候选 | 三年D | 三年组合 | 两年D | 两年组合 | 结论 |",
                "| --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        baseline_d = payload["baseline"]["standalone"]["D"]
        baseline_portfolio = payload["baseline"]["portfolio"]
        for item in d_style_stress:
            conclusion = (
                "通过"
                if item["replacement_gate_passed"]
                else "未通过：" + (
                    item["main_gate_reasons"]
                    or item["recent_confirmation_reasons"]
                    or item["failure_flags"]
                )
            )
            lines.append(
                f"| {item['variant_id']} | "
                f"{baseline_d['main']['equity_multiple']:.4f}→"
                f"{item['main_standalone_equity_multiple']:.4f} | "
                f"{baseline_portfolio['main']['equity_multiple']:.4f}→"
                f"{item['main_portfolio_equity_multiple']:.4f} | "
                f"{baseline_d['recent']['equity_multiple']:.4f}→"
                f"{item['recent_standalone_equity_multiple']:.4f} | "
                f"{baseline_portfolio['recent']['equity_multiple']:.4f}→"
                f"{item['recent_portfolio_equity_multiple']:.4f} | {conclusion} |"
            )
    lines.extend(
        [
            "",
            "## 三窗口组合完整指标",
            "",
            "| 版本/窗口 | 样本 | 胜率 | 平均 | 中位 | 复利倍数 | 最大回撤 | 盈亏比 | 最大盈利 | 最大亏损 | 最大连亏 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for window in ("main", "recent", "failure_check"):
        lines.append(
            metric_table_row(
                f"基线/{window}", payload["baseline"]["portfolio"][window]
            )
        )
        lines.append(
            metric_table_row(
                f"研究版/{window}", payload["final_research"]["portfolio"][window]
            )
        )
    lines.extend(
        [
            "",
            "## 最终研究版单腿三窗口",
            "",
            "| 策略/窗口 | 样本 | 胜率 | 平均 | 中位 | 复利倍数 | 最大回撤 | 盈亏比 | 最大盈利 | 最大亏损 | 最大连亏 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for leg in FIXED_PRIORITY:
        for window in ("main", "recent", "failure_check"):
            lines.append(
                metric_table_row(
                    f"{leg}/{window}",
                    payload["final_research"]["standalone"][leg][window],
                )
            )
    lines.extend(
        [
            "",
            "## 半年度稳定性（最终研究组合）",
            "",
            "| 半年度 | 样本 | 胜率 | 平均 | 中位 | 复利倍数 | 最大回撤 | 盈亏比 | 最大盈利 | 最大亏损 | 最大连亏 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for period, metric in payload["final_research"]["half_year_breakdown"].items():
        lines.append(metric_table_row(period, metric))
    lines.extend(
        [
            "",
            "## 半年度变更对照",
            "",
            "| 半年度 | 组合基线 | 组合研究版 |",
            "| --- | ---: | ---: |",
        ]
    )
    for period, baseline_metric in payload["baseline"]["half_year_breakdown"].items():
        final_metric = payload["final_research"]["half_year_breakdown"][period]
        lines.append(
            f"| {period} | {baseline_metric['trade_count']}笔 / "
            f"{baseline_metric['equity_multiple']:.4f}倍 / DD{baseline_metric['max_drawdown']:.2%} | "
            f"{final_metric['trade_count']}笔 / {final_metric['equity_multiple']:.4f}倍 / "
            f"DD{final_metric['max_drawdown']:.2%} |"
        )
    for leg in payload["selected_bundle_gate"].get("selected_legs", []):
        lines.extend(
            [
                "",
                f"### 入选腿{leg}的半年度单腿对照",
                "",
                "| 半年度 | 单腿基线 | 单腿研究版 |",
                "| --- | ---: | ---: |",
            ]
        )
        baseline_periods = payload["baseline"]["standalone_half_year_breakdown"][leg]
        final_periods = payload["final_research"]["standalone_half_year_breakdown"][leg]
        for period, baseline_metric in baseline_periods.items():
            final_metric = final_periods[period]
            lines.append(
                f"| {period} | {baseline_metric['trade_count']}笔 / "
                f"{baseline_metric['equity_multiple']:.4f}倍 / DD{baseline_metric['max_drawdown']:.2%} | "
                f"{final_metric['trade_count']}笔 / {final_metric['equity_multiple']:.4f}倍 / "
                f"DD{final_metric['max_drawdown']:.2%} |"
            )
    audit = payload["incremental_change_audit"]
    lines.extend(
        [
            "",
            "## 增量变更审计",
            "",
            f"- 冻结计划实际变更：{audit['selected_plan_change_count']}个信号日。",
            f"- 组合逐日裁决状态变化：{audit['portfolio_decision_state_change_day_count']}天"
            "（包含持仓占用的衍生变化）。",
            f"- 真正改变组合收益：{audit['portfolio_direct_return_change_trade_count']}笔成交。",
            f"- 三年组合倍数比：{audit['main_portfolio_multiple_ratio']:.4f}。",
            f"- 两年组合倍数比：{audit['recent_portfolio_multiple_ratio']:.4f}。",
            f"- 风险说明：{audit['warning']}",
        ]
    )
    if audit["direct_return_change_details"]:
        lines.extend(["", "直接收益变更明细：", ""])
        for item in audit["direct_return_change_details"]:
            lines.append(
                f"- {item['action_date']}：{item['baseline_strategy_leg']} "
                f"{item['baseline_ts_code']} {item['baseline_account_return']:.2%} → "
                f"{item['final_strategy_leg'] or '空仓'} "
                f"{item['final_ts_code']} {item['final_account_return']:.2%}。"
            )
    lines.extend(
        [
            "",
            "## 成本与成交口径",
            "",
            f"- {payload['execution_assumptions']['note']}",
        ]
    )
    lines.extend(["", "## 口径限制", ""])
    lines.extend(f"- {item}" for item in payload["strict_limits"])
    return "\n".join(lines) + "\n"


def render_report(payload: dict[str, Any]) -> str:
    windows = payload["windows"]
    audits = payload["data_audits"]
    lines = [
        "# A>C>E>D 三窗口滚动优化准备报告",
        "",
        f"- 状态：{payload['status']}",
        f"- 固定腿序：{' > '.join(payload['priority'])}",
        f"- 三年主优化：{windows['main']['start']}～{windows['main']['end']}",
        f"- 两年近期确认：{windows['recent']['start']}～{windows['recent']['end']}",
        f"- 半年失效检查：{windows['failure_check']['start']}～{windows['failure_check']['end']}",
        "- 指标归属：真实action_date；最近两年和最近半年均不得参与候选排名。",
        "- 正式策略：未修改。",
        "",
        "## 数据门禁",
        "",
        "| 门禁 | 结果 | 可用范围 | 目标范围 |",
        "| --- | --- | --- | --- |",
        (
            f"| 严格日频源 | {'通过' if payload['readiness_gates']['strict_daily_three_year_coverage'] else '失败'} | "
            f"{audits['strict_daily'].get('available_start', '')}～{audits['strict_daily'].get('available_end', '')} | "
            f"{audits['strict_daily'].get('required_start', '')}～{audits['strict_daily'].get('required_end', '')} |"
        ),
        (
            f"| 严格特征池 | {'通过' if payload['readiness_gates']['strict_feature_three_year_coverage'] else '失败'} | "
            f"{audits['strict_feature_pool'].get('available_start', '')}～{audits['strict_feature_pool'].get('available_end', '')} | "
            f"{audits['strict_feature_pool'].get('required_start', '')}～{audits['strict_feature_pool'].get('required_end', '')} |"
        ),
        (
            f"| 市场情绪门禁 | {'通过' if payload['readiness_gates']['market_sentiment_three_year_coverage'] else '失败'} | "
            f"{audits['market_sentiment'].get('available_start', '')}～{audits['market_sentiment'].get('available_end', '')} | "
            f"{audits['market_sentiment'].get('required_start', '')}～{audits['market_sentiment'].get('required_end', '')} |"
        ),
        (
            f"| D一分钟回封事件 | {'通过' if payload['readiness_gates']['d_minute_event_three_year_coverage'] else '失败'} | "
            f"{audits['d_minute_events'].get('available_start', '')}～{audits['d_minute_events'].get('available_end', '')} | "
            f"{audits['d_minute_events'].get('required_start', '')}～{audits['d_minute_events'].get('required_end', '')} |"
        ),
        (
            f"| D母池分母与异常fail-closed | {'通过' if payload['readiness_gates']['d_target_denominator_fail_closed'] else '失败'} | "
            f"{audits['d_target_denominator'].get('target_count', 0)}个目标 / "
            f"{audits['d_target_denominator'].get('fail_closed_abnormal_count', 0)}个异常 | "
            f"{audits['d_target_denominator'].get('required_trade_day_count', 0)}个交易日 |"
        ),
        "",
    ]
    if payload["blocking_gates"]:
        lines.extend(
            [
                "## 当前阻断",
                "",
                "- " + "；".join(payload["blocking_gates"]),
                "- D缺失段不能用收盘日线近似替代，否则会改变回封时间、炸板深度和盘中市场状态口径。",
                "- 数据补齐前保持A/C/E/D当前正式参数不变。",
                "",
            ]
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A>C>E>D三窗口半年滚动优化")
    parser.add_argument("--as-of", default=None, help="0630或1231更新节点，格式YYYYMMDD")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--readiness-only",
        action="store_true",
        help="只运行数据与时点门禁，不生成候选或执行参数搜索。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_path = resolve_path(args.config)
    config = load_config(config_path)
    as_of = str(args.as_of or latest_completed_update_node())
    payload = build_readiness(config, as_of)
    output_dir = (
        resolve_path(args.output_dir)
        if args.output_dir is not None
        else resolve_path(config["data"]["report_root"]) / as_of
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "readiness.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "readiness.md").write_text(render_report(payload), encoding="utf-8")
    if args.readiness_only or payload["status"] != "READY_FOR_THREE_YEAR_OPTIMIZATION":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["status"] == "READY_FOR_THREE_YEAR_OPTIMIZATION" else 2
    result = run_three_year_optimization(config, payload, output_dir=output_dir)
    (output_dir / "optimization_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "optimization_summary.md").write_text(
        render_optimization_report(result), encoding="utf-8"
    )
    write_artifact_manifest(
        output_dir=output_dir,
        as_of=as_of,
        readiness=payload,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
