#!/usr/bin/env python3
"""认证当前收益优先ACDE正式规则与月度回测逐笔对齐。

本脚本不搜索参数，也不修改策略配置。它只做四件事：
1. 核对A/C/E/D正式配置确实写入用户选定的四条规则；
2. 用2023-09-01~2026-08-31同一严格as-of输入重建正式逐腿计划；
3. 比较正式计划与冻结候选账本，并重复两次精确现金回放；
4. 输出逻辑/回测对齐认证，同时明确不伪造独立样本外或容量认证。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.acde_monthly_research import (  # noqa: E402
    _build_plans,
    _context,
    _execution_kwargs,
    _metrics,
    _replay,
    _standalone,
    _variant_sets,
    load_monthly_config,
    monthly_paths,
)
from src.acde_rolling_candidates import plan_signature  # noqa: E402
from src.acde_rolling_framework import build_monthly_research_window  # noqa: E402
from src.live_certification import (  # noqa: E402
    certification_config_sha256,
    certification_file_sha256,
    certification_files_sha256,
)
from src.utils.config import load_json_config  # noqa: E402


CUTOFF = "20260831"
SCENARIO = "acde_d_active_lt20_open2_12483_20260831_v15"
RELEASE_ID = "ACDE_D_ACTIVE_LT20_OPEN2_12483_20260831_V15"
SOURCE_DIR = ROOT / "reports/monthly_acde_research/20260831/run_20260831_return_first_final"
RELEASE_LEDGER_DIR = ROOT / "reports/current_portfolio_alignment/acde_d_active_lt20_open2_v15"
DEFAULT_OUTPUT = ROOT / "reports/current_portfolio_alignment/return_first_live_certification.json"
DEFAULT_MARKDOWN = ROOT / "reports/current_portfolio_alignment/return_first_portfolio_report.md"
TOLERANCE = 1e-10

VARIANT_IDS = {
    "A": "A_RISK_EXCLUDE_AFTERNOON_FIRST_SEAL",
    "C": "C_RISK_EXCLUDE_SINGLE_OPEN",
    "E": "E_TURNOVER_FD_AMOUNT12_OPEN23_NO_TRADE",
    "D": "D_ACTIVE_LT20_OPEN2_EXTENSION",
}

EXPECTED_COMBO = {
    "trade_count": 177,
    "win_rate": 0.751412429378531,
    "avg_account_return": 0.05873599209363823,
    "median_account_return": 0.03893031151086923,
    "equity_multiple": 12483.978370389923,
    "max_drawdown": -0.25534081230210814,
    "max_profit": 0.4762529831005069,
    "max_loss": -0.17603457811871615,
    "profit_loss_ratio": 2.3118357302467425,
    "max_consecutive_losses": 3,
    "leg_counts": {"A": 85, "C": 52, "D": 11, "E": 29},
}

EXPECTED_STANDALONE = {
    "A": {"trade_count": 108, "equity_multiple": 155.0269020298712, "max_drawdown": -0.19372149239299818},
    "C": {"trade_count": 61, "equity_multiple": 17.90855770136303, "max_drawdown": -0.17589327975064117},
    "E": {"trade_count": 59, "equity_multiple": 30.818622006506075, "max_drawdown": -0.09623820346548773},
    "D": {"trade_count": 28, "equity_multiple": 3.0910717887644705, "max_drawdown": -0.06952092036932622},
}

CODE_FILES = [
    "config/acde_rolling_optimization.json",
    "config/config.json",
    "config/strategy_config.json",
    "config/strategy_e_r1_scenarios.json",
    "config/strategy_d_factor_release.json",
    "config/strategy_release_freeze.json",
    "src/paper_candidate_generator.py",
    "src/acde_rolling_candidates.py",
    "src/acde_rolling_framework.py",
    "src/acde_monthly_research.py",
    "src/combined_live_engine.py",
    "src/live_certification.py",
    "src/strategy_e.py",
    "src/strategy_d_factor_rules.py",
    "src/strategy_identity.py",
    "scripts/optimize_acde_rolling_three_year.py",
    "scripts/run_paper_ab_filtered_daily_ops.py",
    "scripts/run_strategy_e_signal.py",
    "scripts/verify_strategy_e_alignment.py",
    "scripts/certify_current_executable_portfolio.py",
    "scripts/generate_live_limit_pool_daily_ops.py",
    "scripts/monitor_strategy_d_intraday.py",
    "scripts/trading_daemon.py",
    "scripts/verify_live_engine_matches_certify.py",
    "scripts/certify_acde_return_first_release.py",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="认证D弱广度二次回封V15与12483.978370倍ACDE回测对齐")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument(
        "--refresh-release-ledgers",
        action="store_true",
        help="仅在用户批准新版本时刷新V15逐腿与组合冻结账本；日常认证禁止使用。",
    )
    return parser.parse_args()


def close_enough(actual: object, expected: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, Mapping) and all(
            key in actual and close_enough(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, float):
        return abs(float(actual) - expected) <= TOLERANCE * max(1.0, abs(expected))
    return actual == expected


def raw_file_sha256(path: Path) -> str:
    """按研究流水线原始字节口径计算哈希，不做换行转换。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest_entries(
    entries: Mapping[str, Any],
    *,
    base_dir: Path,
    path_from_metadata: bool,
) -> dict[str, Any]:
    """逐文件验证研究清单，任何文件缺失、大小或哈希变化都失败。"""

    failures: list[dict[str, Any]] = []
    checked = 0
    for name, raw_meta in entries.items():
        meta = raw_meta if isinstance(raw_meta, Mapping) else {"sha256": raw_meta}
        raw_path = str(meta.get("path", name)) if path_from_metadata else str(name)
        path = Path(raw_path)
        path = path if path.is_absolute() else base_dir / path
        expected_hash = str(meta.get("sha256", ""))
        expected_size = meta.get("size_bytes")
        if not path.exists() or not path.is_file():
            failures.append({"file": raw_path, "reason": "MISSING"})
            continue
        checked += 1
        actual_size = path.stat().st_size
        actual_hash = raw_file_sha256(path)
        if expected_size is not None and actual_size != int(expected_size):
            failures.append({
                "file": raw_path,
                "reason": "SIZE_MISMATCH",
                "expected": int(expected_size),
                "actual": actual_size,
            })
        if not expected_hash or actual_hash != expected_hash:
            failures.append({
                "file": raw_path,
                "reason": "SHA256_MISMATCH",
                "expected": expected_hash,
                "actual": actual_hash,
            })
    return {
        "entry_count": len(entries),
        "checked_file_count": checked,
        "failure_count": len(failures),
        "failures": failures,
        "passed": checked == len(entries) and not failures,
    }


def validate_data_and_artifact_integrity(paths: Mapping[str, Path]) -> dict[str, Any]:
    """验证数据底座、研究输入、输出产物、异常登记和严格时点状态。"""

    dataset_manifest_path = ROOT / "data/research/monthly_acde/20260831/dataset_manifest.json"
    dataset_manifest = load_json_config(dataset_manifest_path)
    research_manifest = load_json_config(SOURCE_DIR / "data_manifest.json")
    artifact_manifest = load_json_config(SOURCE_DIR / "artifact_manifest.json")
    return_first_manifest = load_json_config(SOURCE_DIR / "return_first_artifact_manifest.json")
    quality = load_json_config(SOURCE_DIR / "quality_summary.json")
    strict_audit = load_json_config(
        ROOT / "data/research/monthly_acde/20260831/strict_asof_audit.json"
    )
    reconciliation = load_json_config(SOURCE_DIR / "backtest_reconciliation.json")
    anomaly = load_json_config(SOURCE_DIR / "anomaly_review.json")
    risk_review = load_json_config(SOURCE_DIR / "return_first_risk_review.json")
    d_gaps = load_json_config(paths["d_known_gaps"])
    d_summary = load_json_config(paths["d_target_summary"])

    dataset_files = verify_manifest_entries(
        dataset_manifest.get("files", {}), base_dir=ROOT, path_from_metadata=True
    )
    research_inputs = verify_manifest_entries(
        research_manifest.get("inputs", {}), base_dir=ROOT, path_from_metadata=True
    )
    research_artifacts = verify_manifest_entries(
        artifact_manifest.get("artifacts", {}),
        base_dir=SOURCE_DIR,
        path_from_metadata=False,
    )
    return_first_artifacts = verify_manifest_entries(
        return_first_manifest.get("files", {}),
        base_dir=SOURCE_DIR,
        path_from_metadata=False,
    )

    anomaly_flags = set(str(value) for value in anomaly.get("flags", []))
    known_review_flags = {"RULE_COMPOUND_RATIO"}
    anomaly_reviewed = (
        anomaly.get("status") == "REVIEW_REQUIRED"
        and anomaly_flags == known_review_flags
        and float(anomaly.get("rule_compound_ratio", 0.0)) > 1.0
        and risk_review.get("status") == "RETURN_FIRST_WINNER_FOUND_RISK_REVIEW_REQUIRED"
        and int(risk_review.get("robustness", {}).get("searched_unique_combinations", 0)) == 32256
        and risk_review.get("robustness", {}).get("strict_discovery") is True
        and risk_review.get("capacity", {}).get("d_capacity_missing") is True
    )
    d_gap_rows = [
        *list(d_gaps.get("gaps", [])),
        *list(d_gaps.get("price_mismatches", [])),
    ]
    d_registered = (
        len(d_gap_rows) == 13
        and all(
            row.get("handling") == "FAIL_CLOSED_KEEP_IN_DENOMINATOR"
            for row in d_gap_rows
        )
        and int(d_summary.get("fail_closed_abnormal_count", -1)) == len(d_gap_rows)
        and d_summary.get("full_window_fail_closed_ready") is True
        and int(quality.get("d_audit", {}).get("abnormal_count", -1)) == len(d_gap_rows)
        and int(quality.get("d_audit", {}).get("registered_abnormal_count", -2))
        == len(d_gap_rows)
        and quality.get("d_audit", {}).get("fail_closed") is True
    )
    checks = {
        "quality_status_pass": quality.get("status") == "PASS"
        and quality.get("ready_token") == "READY_FOR_MONTHLY_ACDE_RESEARCH"
        and quality.get("hard_failures") == [],
        "strict_asof_pass": strict_audit.get("passed") is True
        and strict_audit.get("issues") == [],
        "backtest_reconciliation_pass": reconciliation.get("status") == "PASS",
        "d_abnormalities_registered_fail_closed": d_registered,
        "known_anomaly_reviewed_and_disclosed": anomaly_reviewed,
        "dataset_manifest_pass": dataset_files["passed"],
        "research_input_manifest_pass": research_inputs["passed"],
        "research_artifact_manifest_pass": research_artifacts["passed"],
        "return_first_artifact_manifest_pass": return_first_artifacts["passed"],
    }
    return {
        "checks": checks,
        "dataset_files": dataset_files,
        "research_inputs": research_inputs,
        "research_artifacts": research_artifacts,
        "return_first_artifacts": return_first_artifacts,
        "anomaly_review": {
            "source_status": anomaly.get("status"),
            "flags": sorted(anomaly_flags),
            "rule_compound_ratio": anomaly.get("rule_compound_ratio"),
            "searched_unique_combinations": risk_review.get("robustness", {}).get(
                "searched_unique_combinations"
            ),
            "strict_discovery": risk_review.get("robustness", {}).get("strict_discovery"),
            "capacity_certified": False,
            "reviewed_for_logic_and_data_release": anomaly_reviewed,
        },
        "passed": all(checks.values()),
    }


def validate_formal_rules() -> dict[str, bool]:
    strategy = load_json_config(ROOT / "config/strategy_config.json")
    e_spec = load_json_config(ROOT / "config/strategy_e_r1_scenarios.json")
    d_release = load_json_config(ROOT / "config/strategy_d_factor_release.json")
    runtime = load_json_config(ROOT / "config/config.json")
    release_freeze = load_json_config(ROOT / "config/strategy_release_freeze.json")

    c_rules = strategy["paper_ab_filtered_strategy"]["c_strategy"]["risk_reject_rules"]
    c_single_open = any(
        any(
            condition.get("column") == "open_times"
            and condition.get("operator") == "=="
            and float(condition.get("value")) == 1.0
            for condition in rule.get("numeric_conditions", [])
        )
        for rule in c_rules
    )
    profiles = d_release["profiles"]
    d_condition_sets = [profile["conditions"] for profile in profiles]
    d_break_values = {conditions.get("market_break_rate_bucket") for conditions in d_condition_sets}
    d_core_profiles = [
        conditions
        for conditions in d_condition_sets
        if conditions.get("market_active_count_bucket") != "LT20"
    ]
    d_weak_profiles = [
        conditions
        for conditions in d_condition_sets
        if conditions.get("market_active_count_bucket") == "LT20"
    ]
    d_common_conditions = all(
        conditions.get("reseal_time_bucket") == "0930_1000"
        and conditions.get("break_close_depth_bucket") == "LT0_2PCT"
        and conditions.get("segment_bucket") == "GROWTH_BOARD"
        and conditions.get("market_touch_count_bucket") == "LT40"
        for conditions in d_condition_sets
    )

    return {
        "runtime_release_id": runtime["portfolio_certification"]["release_id"] == RELEASE_ID,
        "release_freeze_id": release_freeze["release_id"] == RELEASE_ID,
        "release_freeze_scenario": release_freeze["certification_scenario"] == SCENARIO,
        "priority_a_c_e_d": runtime["portfolio_certification"]["strategy_priority_order"] == ["A", "C", "E", "D"],
        "a_release_id": strategy["release_id"] == "A_RISK_EXCLUDE_AFTERNOON_FIRST_SEAL_20260831_V13",
        "a_no_fallback_gate": strategy["rolling_research_post_pick_exclude"] == {
            "column": "first_time_bucket",
            "values": ["afternoon"],
            "fallback_to_second_candidate": False,
            "description": strategy["rolling_research_post_pick_exclude"]["description"],
        },
        "c_release_id": strategy["paper_ab_filtered_strategy"]["c_strategy"]["release_id"] == "C_RISK_EXCLUDE_SINGLE_OPEN_20260831_V13",
        "c_excludes_open_times_equal_one": c_single_open,
        "e_release_id": e_spec["release_id"] == "E_TURNOVER_FD_AMOUNT12_OPEN23_NO_TRADE_20260831_V14",
        "e_turnover_fd_equal_weight_rank": e_spec["final_ranking"].get("columns")
        == ["turnover_rate", "fd_amount_to_circ_mv"]
        and e_spec["final_ranking"].get("ascending") == [False, True]
        and e_spec["final_ranking"].get("weights") == [0.5, 0.5],
        "e_post_pick_fd_gate": e_spec["entry_gate"]["exclude_values"].get("fd_ratio_bucket") == ["2pct_5pct"]
        and e_spec["entry_gate"].get("fallback_to_second_candidate") is False,
        "e_post_pick_joint_no_trade_gate": any(
            rule.get("conditions")
            == {
                "amount_ratio_bucket": ["1_2_2"],
                "open_times_bucket": ["2_3"],
            }
            and rule.get("action") == "NO_TRADE"
            for rule in e_spec["entry_gate"].get("exclude_all_conditions", [])
        ),
        "d_release_id": d_release["release_id"] == "D_ACTIVE_LT20_OPEN2_EXTENSION_20260831_V15",
        "d_has_15_or_profiles": len(profiles) == 15,
        "d_break_lt75": d_break_values == {"LT25PCT", "25_50PCT", "50_75PCT"},
        # 原12个强广度档位必须原样保留；新增档位不能反向改变旧D。
        "d_keeps_12_core_profiles": len(d_core_profiles) == 12
        and {conditions.get("market_active_count_bucket") for conditions in d_core_profiles}
        == {"20_40", "41_70", "71_100", "GE101"}
        and all("open_count_bucket" not in conditions for conditions in d_core_profiles),
        # 弱广度只允许恰好第2次回封，防止把回测中表现不稳的1/3/4次回封放进正式D。
        "d_has_exact_weak_active_open2_extension": len(d_weak_profiles) == 3
        and {conditions.get("market_break_rate_bucket") for conditions in d_weak_profiles}
        == {"LT25PCT", "25_50PCT", "50_75PCT"}
        and all(conditions.get("open_count_bucket") == "2" for conditions in d_weak_profiles),
        "d_common_shape_frozen": d_common_conditions,
        "d_keeps_touch_lt40": all(
            conditions.get("market_touch_count_bucket") == "LT40" for conditions in d_condition_sets
        ),
    }


def compare_frozen_plan_signatures(plans: Mapping[str, pd.DataFrame]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for leg in VARIANT_IDS:
        path = RELEASE_LEDGER_DIR / f"{leg.lower()}_plans.csv"
        frozen = pd.read_csv(path, low_memory=False)
        formal_signature = plan_signature(plans[leg])
        frozen_signature = plan_signature(frozen)
        rows[leg] = {
            "formal_plan_count": int(len(plans[leg])),
            "frozen_plan_count": int(len(frozen)),
            "formal_signature": formal_signature,
            "frozen_signature": frozen_signature,
            "passed": formal_signature == frozen_signature,
        }
    return rows


def refresh_release_ledgers(
    plans: Mapping[str, pd.DataFrame],
    combo_detail: pd.DataFrame,
    combo_metrics: Mapping[str, Any],
    standalone_metrics: Mapping[str, Mapping[str, Any]],
) -> None:
    """在用户明确批准后封存V15逐腿与组合账本，供后续只读认证。"""

    RELEASE_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    for leg, frame in plans.items():
        frame.to_csv(
            RELEASE_LEDGER_DIR / f"{leg.lower()}_plans.csv",
            index=False,
            encoding="utf-8-sig",
        )
    combo_detail.to_csv(
        RELEASE_LEDGER_DIR / "combo_trades.csv",
        index=False,
        encoding="utf-8-sig",
    )
    summary = {
        "schema_version": 1,
        "scenario": SCENARIO,
        "release_id": RELEASE_ID,
        "window": {"start": "20230901", "end": CUTOFF},
        "selected_variants": VARIANT_IDS,
        "combo_metrics": dict(combo_metrics),
        "standalone_metrics": {
            leg: dict(metrics) for leg, metrics in standalone_metrics.items()
        },
        "e_plan_count": int(len(plans["E"])),
        "e_selection_counts": {
            "raw_daily_first": 148,
            "after_v13_single_value_gates": 81,
            "v14_joint_gate_removed": 12,
            "formal_plans": int(len(plans["E"])),
            "standalone_executed": int(standalone_metrics["E"]["trade_count"]),
        },
        "d_selection_counts": {
            "formal_plans": int(len(plans["D"])),
            "observable_plans": int(plans["D"]["status"].astype(str).eq("OK").sum()),
            "queue_unconfirmed_fail_closed": int(
                plans["D"]["status"].astype(str).eq("QUEUE_UNCONFIRMED_NO_DEPTH").sum()
            ),
            "standalone_executed": int(standalone_metrics["D"]["trade_count"]),
            "portfolio_executed": int(combo_metrics["leg_counts"].get("D", 0)),
        },
        "rule": {
            "ranking": "daily_percentile(turnover_rate:desc)*50% + daily_percentile(fd_amount_to_circ_mv:asc)*50%",
            "joint_no_trade": {
                "amount_ratio_bucket": ["1_2_2"],
                "open_times_bucket": ["2_3"],
                "logic": "AND",
                "fallback_to_second_candidate": False,
                "position_when_matched": 0.0,
            },
            "d_extension": {
                "market_active_count_bucket": ["LT20"],
                "open_count_bucket": ["2"],
                "logic": "OR_WITH_EXISTING_12_PROFILES",
                "fallback_to_later_candidate": False,
            },
        },
        "research_protocol": "STRICT_DISCOVERY",
        "risk_note": "177笔、12483.978370倍是最近三年同窗STRICT_DISCOVERY历史机械复利，不是收益承诺；D独立28笔、弱广度扩展独立14笔，且相邻炸板次数桶不稳定，尚无独立冻结样本外和真实资金容量认证。",
    }
    (RELEASE_LEDGER_DIR / "release_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def max_numeric_delta(actual: pd.DataFrame, expected: pd.DataFrame) -> float:
    columns = [
        "account_return", "cash_after", "equity_after", "entry_price", "exit_price",
        "total_fees", "total_slippage", "drawdown",
    ]
    maxima: list[float] = []
    for column in columns:
        left = pd.to_numeric(actual[column], errors="coerce")
        right = pd.to_numeric(expected[column], errors="coerce")
        maxima.append(float((left - right).abs().fillna(0.0).max()))
    return max(maxima, default=0.0)


def main() -> int:
    args = parse_args()
    runtime = load_json_config(ROOT / "config/config.json")
    current_release_id = str(
        runtime.get("portfolio_certification", {}).get("release_id", "")
    )
    if current_release_id != RELEASE_ID:
        raise RuntimeError(
            "该脚本只认证已归档的D-V15，当前正式发布为"
            f"{current_release_id or '未配置'}；拒绝覆盖当前认证文件，请运行"
            "scripts/certify_strategy_c_third_branch_release.py。"
        )
    monthly = load_monthly_config(ROOT / "config/acde_rolling_optimization.json")
    window = build_monthly_research_window(CUTOFF)
    paths = monthly_paths(monthly, CUTOFF)
    context = _context(
        window=window,
        feature_path=paths["strict_feature_pool"],
        sentiment_path=paths["market_sentiment"],
        d_event_path=paths["d_event_source"],
        calendar_path=paths["trade_calendar"],
        minimum_limit_up_count=0,
    )
    baselines, _candidates = _variant_sets()
    execution = _execution_kwargs(monthly)

    # 两次独立构建与回放用于发现非确定性或缓存泄漏。
    first_plans = _build_plans(baselines, context=context, cutoff=CUTOFF)
    second_plans = _build_plans(baselines, context=context, cutoff=CUTOFF)
    first_detail = _replay(first_plans, action_dates=context["action_dates"], execution=execution)
    second_detail = _replay(second_plans, action_dates=context["action_dates"], execution=execution)
    first_metrics = _metrics(first_detail, window)
    second_metrics = _metrics(second_detail, window)

    deterministic = all(
        plan_signature(first_plans[leg]) == plan_signature(second_plans[leg])
        for leg in VARIANT_IDS
    ) and close_enough(second_metrics, first_metrics)
    combo_match = close_enough(first_metrics, EXPECTED_COMBO)

    standalone_metrics = {
        leg: _metrics(
            _standalone(first_plans[leg], leg, action_dates=context["action_dates"], execution=execution),
            window,
        )
        for leg in VARIANT_IDS
    }
    standalone_match = all(
        close_enough(standalone_metrics[leg], EXPECTED_STANDALONE[leg])
        for leg in VARIANT_IDS
    )
    rule_checks = validate_formal_rules()
    if args.refresh_release_ledgers:
        if not (
            deterministic
            and combo_match
            and standalone_match
            and all(rule_checks.values())
        ):
            raise RuntimeError("V15正式规则或预期指标未对齐，拒绝覆盖冻结发布账本")
        refresh_release_ledgers(
            first_plans,
            first_detail,
            first_metrics,
            standalone_metrics,
        )

    plan_checks = compare_frozen_plan_signatures(first_plans)

    frozen_trades = pd.read_csv(RELEASE_LEDGER_DIR / "combo_trades.csv", low_memory=False)
    frozen_trade_count_match = len(first_detail) == len(frozen_trades)
    frozen_trade_max_numeric_delta = (
        max_numeric_delta(first_detail, frozen_trades) if frozen_trade_count_match else float("inf")
    )
    frozen_trade_values_match = frozen_trade_max_numeric_delta <= 1e-5

    source_summary = load_json_config(RELEASE_LEDGER_DIR / "release_summary.json")
    source_summary_match = (
        source_summary.get("selected_variants") == VARIANT_IDS
        and close_enough(source_summary.get("combo_metrics", {}), EXPECTED_COMBO)
    )
    data_and_artifact_integrity = validate_data_and_artifact_integrity(paths)
    passed = all(rule_checks.values()) and all(
        row["passed"] for row in plan_checks.values()
    ) and deterministic and combo_match and standalone_match and source_summary_match \
        and frozen_trade_values_match and data_and_artifact_integrity["passed"]

    input_files = [
        str(paths["strict_feature_pool"].relative_to(ROOT)),
        str(paths["market_sentiment"].relative_to(ROOT)),
        str(paths["d_event_source"].relative_to(ROOT)),
        str(paths["trade_calendar"].relative_to(ROOT)),
        str((RELEASE_LEDGER_DIR / "release_summary.json").relative_to(ROOT)),
        str((RELEASE_LEDGER_DIR / "combo_trades.csv").relative_to(ROOT)),
        *[
            str((RELEASE_LEDGER_DIR / f"{leg.lower()}_plans.csv").relative_to(ROOT))
            for leg in VARIANT_IDS
        ],
        str((SOURCE_DIR / "data_manifest.json").relative_to(ROOT)),
        str((SOURCE_DIR / "artifact_manifest.json").relative_to(ROOT)),
        str((SOURCE_DIR / "return_first_artifact_manifest.json").relative_to(ROOT)),
        str((SOURCE_DIR / "quality_summary.json").relative_to(ROOT)),
        str((SOURCE_DIR / "backtest_reconciliation.json").relative_to(ROOT)),
        str((SOURCE_DIR / "anomaly_review.json").relative_to(ROOT)),
        str((SOURCE_DIR / "return_first_risk_review.json").relative_to(ROOT)),
        "data/research/monthly_acde/20260831/dataset_manifest.json",
        "data/research/monthly_acde/20260831/strict_asof_audit.json",
        "data/research/monthly_acde/20260831/strategy_d_three_year/known_data_gaps.json",
    ]
    payload = {
        "schema_version": 1,
        "status": "PASS" if passed else "FAIL",
        "scenario": SCENARIO,
        "release_id": RELEASE_ID,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "certification_scope": "FORMAL_RULE_AND_BACKTEST_ALIGNMENT",
        "current_executable": passed,
        "release_eligible": False,
        "strict_asof_standard_id": runtime["strict_asof"]["standard_id"],
        "strict_asof_passed": True,
        "research_protocol": "STRICT_DISCOVERY",
        "independent_oos_certified": False,
        "capacity_certified": False,
        "window": {"start": window.start, "end": window.end, "trade_days": len(context["action_dates"])},
        "selected_variants": VARIANT_IDS,
        "rule_checks": rule_checks,
        "plan_checks": plan_checks,
        "deterministic_double_replay": deterministic,
        "combo_metrics_match": combo_match,
        "combo_metrics": first_metrics,
        "standalone_metrics_match": standalone_match,
        "standalone_metrics": standalone_metrics,
        "source_summary_match": source_summary_match,
        "data_and_artifact_integrity": data_and_artifact_integrity,
        "frozen_trade_row_count_match": frozen_trade_count_match,
        "frozen_trade_max_numeric_delta": frozen_trade_max_numeric_delta,
        "frozen_trade_values_match": frozen_trade_values_match,
        "config_sha256": certification_config_sha256(runtime),
        "code_files": CODE_FILES,
        "code_sha256": certification_files_sha256(ROOT, CODE_FILES),
        "input_files": input_files,
        "input_sha256": certification_files_sha256(ROOT, input_files),
        "source_summary_sha256": certification_file_sha256(RELEASE_LEDGER_DIR / "release_summary.json"),
        "risk_note": "177笔、12483.978370倍是最近三年同窗STRICT_DISCOVERY历史机械复利，不是收益承诺；D独立28笔、最大回撤-6.95%，弱广度第2次回封存在单桶邻域敏感，尚无独立冻结样本外和真实容量认证。",
    }

    output = args.output if args.output.is_absolute() else ROOT / args.output
    markdown = args.markdown if args.markdown.is_absolute() else ROOT / args.markdown
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown.write_text(
        "\n".join(
            [
                "# ACDE策略D弱广度第2次回封V15正式规则对齐报告",
                "",
                f"- 状态：{payload['status']}",
                f"- 场景：{SCENARIO}",
                f"- 窗口：{window.start}～{window.end}（{len(context['action_dates'])}个交易日）",
                f"- 组合：{first_metrics['trade_count']}笔，胜率{first_metrics['win_rate']:.2%}，复利{first_metrics['equity_multiple']:.6f}倍，最大回撤{first_metrics['max_drawdown']:.2%}",
                f"- 分腿：{first_metrics['leg_counts']}",
                f"- 正式计划与冻结候选账本：{'一致' if all(row['passed'] for row in plan_checks.values()) else '不一致'}",
                f"- 两次确定性回放：{'一致' if deterministic else '不一致'}",
                f"- 数据与研究产物清单：{'一致' if data_and_artifact_integrity['passed'] else '不一致'}",
                f"- 已知异常复核：{'通过' if data_and_artifact_integrity['checks']['known_anomaly_reviewed_and_disclosed'] else '失败'}",
                f"- 冻结逐日账本最大数值差：{frozen_trade_max_numeric_delta:.10f}",
                "",
                "风险说明：该认证只证明正式规则、逐腿计划和回测逻辑对齐；不把同窗搜索伪装成独立样本外或真实资金容量认证，也不承诺未来收益。",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": payload["status"], "output": str(output), "markdown": str(markdown)}, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
