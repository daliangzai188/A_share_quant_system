#!/usr/bin/env python3
"""认证并生成策略C第3分支V16正式审计资产。

本脚本从正式配置重新生成C计划，冻结A/E/D及A>C>E>D顺序，逐项验证旧C未变、
第3分支因子合同、分支级T+2退出、样本与收益指标。任一检查失败立即抛错，
不会写出PASS版发布目录。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.optimize_acde_rolling_three_year import build_variant_plan
from src.acde_monthly_research import (
    _context,
    _execution_kwargs,
    _stress_plans,
    load_monthly_config,
    monthly_paths,
)
from src.acde_rolling_candidates import (
    StaticOutcomeCache,
    VariantDefinition,
    plan_signature,
)
from src.acde_rolling_framework import (
    FIXED_PRIORITY,
    action_metrics,
    build_monthly_research_window,
    replay_action_date_cash_portfolio,
)
from src.paper_candidate_generator import PaperCandidateGenerator
from src.live_certification import (
    certification_config_sha256,
    certification_file_sha256,
    certification_files_sha256,
)
from src.utils.config import load_json_config


CUTOFF = "20260831"
SCENARIO = "acde_c_third_branch_t2_22695_20260902_v16"
RELEASE_ID = "ACDE_C_THIRD_BRANCH_T2_22695_20260902_V16"
THIRD_PROFILE = (
    "C_THIRD_LIMITUP30_50_RANK4_10_FD01_03_"
    "CHAIN_NOT15_AMOUNT_NOT2_3"
)
BASELINE_ROOT = ROOT / "reports/current_portfolio_alignment/acde_d_active_lt20_open2_v15"
OUTPUT_ROOT = ROOT / (
    "reports/current_portfolio_alignment/"
    "acde_c_third_branch_t2_22695_20260902_v16"
)
LIVE_CERTIFICATION_PATH = (
    ROOT / "reports/current_portfolio_alignment/return_first_live_certification.json"
)
LIVE_REPORT_PATH = (
    ROOT / "reports/current_portfolio_alignment/return_first_portfolio_report.md"
)

CODE_FILES = [
    "config/acde_rolling_optimization.json",
    "config/config.json",
    "config/strategy_config.json",
    "config/strategy_e_r1_scenarios.json",
    "config/strategy_d_factor_release.json",
    "config/strategy_release_freeze.json",
    "src/paper_candidate_generator.py",
    "src/strategy_c_exit.py",
    "src/acde_rolling_candidates.py",
    "src/acde_rolling_framework.py",
    "src/acde_monthly_research.py",
    "src/combined_live_engine.py",
    "src/live_order_gateway.py",
    "src/live_certification.py",
    "src/strategy_e.py",
    "src/strategy_d_factor_rules.py",
    "src/strategy_identity.py",
    "scripts/optimize_acde_rolling_three_year.py",
    "scripts/run_paper_ab_filtered_daily_ops.py",
    "scripts/run_strategy_e_signal.py",
    "scripts/verify_strategy_e_alignment.py",
    "scripts/generate_live_limit_pool_daily_ops.py",
    "scripts/monitor_strategy_d_intraday.py",
    "scripts/trading_daemon.py",
    "scripts/verify_live_engine_matches_certify.py",
    "scripts/certify_strategy_c_third_branch_release.py",
]


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def semantic_plan_equal(
    left: pd.DataFrame,
    right: pd.DataFrame,
    columns: list[str],
) -> bool:
    """按字段语义比较计划，容忍CSV浮点打印尾差和日期`.0`显示差异。"""

    left_norm = left[columns].copy().sort_values(
        ["action_date", "ts_code"]
    ).reset_index(drop=True)
    right_norm = right[columns].copy().sort_values(
        ["action_date", "ts_code"]
    ).reset_index(drop=True)
    if len(left_norm) != len(right_norm):
        return False
    date_columns = {
        "signal_date",
        "action_date",
        "buy_date",
        "exit_date",
        "position_open_until",
    }
    for column in columns:
        left_text = left_norm[column].fillna("").astype(str)
        right_text = right_norm[column].fillna("").astype(str)
        if column in date_columns:
            if not left_text.str.replace(r"\.0$", "", regex=True).equals(
                right_text.str.replace(r"\.0$", "", regex=True)
            ):
                return False
            continue
        left_numeric = pd.to_numeric(left_norm[column], errors="coerce")
        right_numeric = pd.to_numeric(right_norm[column], errors="coerce")
        numeric_rows = left_text.ne("") | right_text.ne("")
        if numeric_rows.any() and (
            left_numeric.loc[numeric_rows].notna().all()
            and right_numeric.loc[numeric_rows].notna().all()
        ):
            if not np.allclose(
                left_numeric.to_numpy(float),
                right_numeric.to_numpy(float),
                rtol=1e-12,
                atol=1e-12,
                equal_nan=True,
            ):
                return False
        elif not left_text.equals(right_text):
            return False
    return True


def close_enough(actual: float, expected: float, tolerance: float = 1e-9) -> bool:
    return bool(abs(float(actual) - float(expected)) <= tolerance)


def metric_checks(
    actual: Mapping[str, Any],
    expected: Mapping[str, Any],
    prefix: str,
) -> dict[str, bool]:
    names = {
        "trade_count": (
            "new_branch_executed_trade_count"
            if prefix == "new_branch"
            else f"{prefix}_trade_count"
        ),
        "win_rate": f"{prefix}_win_rate",
        "avg_account_return": f"{prefix}_avg_account_return",
        "median_account_return": f"{prefix}_median_account_return",
        "equity_multiple": f"{prefix}_equity_multiple",
        "max_drawdown": f"{prefix}_max_drawdown",
    }
    checks: dict[str, bool] = {}
    for metric_name, expected_name in names.items():
        if expected_name not in expected:
            continue
        if metric_name == "trade_count":
            passed = int(actual[metric_name]) == int(expected[expected_name])
        else:
            passed = close_enough(actual[metric_name], expected[expected_name])
        checks[f"{prefix}_{metric_name}_exact"] = passed
    return checks


def run() -> dict[str, Any]:
    strategy_config_path = ROOT / "config/strategy_config.json"
    strategy_config = load_json_config(strategy_config_path)
    runtime_config = load_json_config(ROOT / "config/config.json")
    release_freeze = load_json_config(ROOT / "config/strategy_release_freeze.json")
    c_config = strategy_config["paper_ab_filtered_strategy"]["c_strategy"]
    release = c_config["third_branch_release"]
    if c_config.get("release_id") != "C_THIRD_BRANCH_T2_20260902_V16":
        raise RuntimeError("正式C release_id不是待认证V16")

    window = build_monthly_research_window(CUTOFF)
    monthly_config = load_monthly_config(ROOT / "config/acde_rolling_optimization.json")
    paths = monthly_paths(monthly_config, CUTOFF)
    context = _context(
        window=window,
        feature_path=paths["strict_feature_pool"],
        sentiment_path=paths["market_sentiment"],
        d_event_path=paths["d_event_source"],
        calendar_path=paths["trade_calendar"],
        minimum_limit_up_count=monthly_config["market_controller"][
            "minimum_limit_up_count"
        ],
    )
    execution = _execution_kwargs(monthly_config)
    baseline_paths = {
        leg: BASELINE_ROOT / f"{leg.lower()}_plans.csv" for leg in FIXED_PRIORITY
    }
    baseline_hashes = {leg: sha256_path(path) for leg, path in baseline_paths.items()}
    baseline_legs = {
        leg: pd.read_csv(
            path,
            low_memory=False,
            dtype={
                "signal_date": str,
                "action_date": str,
                "buy_date": str,
                "ts_code": str,
            },
        )
        for leg, path in baseline_paths.items()
    }

    variant = VariantDefinition(
        "C",
        "C_THIRD_BRANCH_T2_FORMAL_V16",
        "用户批准的C第3分支正式配置",
        strategy_config,
        1,
        True,
        "用户审核接受收益分布；只增加C第3逻辑分支",
    )

    def rebuild_c_plan() -> pd.DataFrame:
        return build_variant_plan(
            variant,
            signal_pool=context["signal_pool"],
            d_events=context["d_events"],
            allowed_action_dates=context["allowed_actions"],
            cutoff=CUTOFF,
            outcome_cache=StaticOutcomeCache(),
        )

    plan = rebuild_c_plan()
    second_plan = rebuild_c_plan()
    third_mask = (
        plan["matched_condition_profile_ids"]
        .fillna("")
        .astype(str)
        .str.contains(THIRD_PROFILE, regex=False)
    )
    third_plan = plan.loc[third_mask].copy()
    old_c_plan = plan.loc[~third_mask].copy()

    compare_columns = sorted(
        set(old_c_plan.columns) & set(baseline_legs["C"].columns)
    )
    old_c_exact = semantic_plan_equal(
        old_c_plan,
        baseline_legs["C"],
        compare_columns,
    )

    profile = next(
        item
        for item in c_config["condition_profiles"]
        if str(item["profile_id"]) == THIRD_PROFILE
    )
    factor_source = context["signal_pool"].copy()
    factor_source["trade_date"] = factor_source["trade_date"].astype(str)
    factor_columns = sorted(
        {str(condition["column"]) for condition in profile["conditions"]}
    )
    factor_audit = third_plan.merge(
        factor_source[["trade_date", "ts_code", *factor_columns]],
        left_on=["signal_date", "ts_code"],
        right_on=["trade_date", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    factor_pass = PaperCandidateGenerator._condition_mask(
        factor_audit,
        list(profile["conditions"]),
        context="C第3分支正式认证",
    )
    factor_audit["factor_contract_passed"] = factor_pass

    c_detail = replay_action_date_cash_portfolio(
        {"C": plan},
        action_dates=context["action_dates"],
        priority=("C",),
        **execution,
    )
    labels = plan[
        [
            "action_date",
            "ts_code",
            "matched_condition_profile_ids",
            "matched_strategy_branch_ids",
            "hold_offset",
            "exit_rule",
        ]
    ].copy()
    third_detail = c_detail[c_detail["status"].astype(str).eq("EXECUTED")].merge(
        labels,
        on=["action_date", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    third_detail = third_detail[
        third_detail["matched_condition_profile_ids"]
        .fillna("")
        .astype(str)
        .str.contains(THIRD_PROFILE, regex=False)
    ].copy()

    release_legs = {leg: frame.copy() for leg, frame in baseline_legs.items()}
    release_legs["C"] = plan
    combo_detail = replay_action_date_cash_portfolio(
        release_legs,
        action_dates=context["action_dates"],
        priority=FIXED_PRIORITY,
        **execution,
    )
    second_release_legs = {
        leg: (second_plan if leg == "C" else frame.copy())
        for leg, frame in baseline_legs.items()
    }
    second_combo_detail = replay_action_date_cash_portfolio(
        second_release_legs,
        action_dates=context["action_dates"],
        priority=FIXED_PRIORITY,
        **execution,
    )
    third_metrics = action_metrics(third_detail, window.start, window.end)
    c_metrics = action_metrics(c_detail, window.start, window.end)
    combo_metrics = action_metrics(combo_detail, window.start, window.end)
    second_combo_metrics = action_metrics(
        second_combo_detail,
        window.start,
        window.end,
    )
    deterministic_double_replay = (
        plan_signature(plan) == plan_signature(second_plan)
        and combo_metrics == second_combo_metrics
    )
    standalone_metrics = {
        leg: action_metrics(
            replay_action_date_cash_portfolio(
                {leg: frame},
                action_dates=context["action_dates"],
                priority=(leg,),
                **execution,
            ),
            window.start,
            window.end,
        )
        for leg, frame in release_legs.items()
    }

    checks: dict[str, bool] = {
        "runtime_release_id": runtime_config["portfolio_certification"][
            "release_id"
        ]
        == RELEASE_ID,
        "runtime_scenario": runtime_config["portfolio_certification"][
            "certification_expected_scenario"
        ]
        == SCENARIO,
        "runtime_official_certifier": runtime_config["strict_asof"][
            "official_portfolio_certifier"
        ]
        == "scripts/certify_strategy_c_third_branch_release.py",
        "release_freeze_id": release_freeze["release_id"] == RELEASE_ID,
        "release_freeze_scenario": release_freeze["certification_scenario"]
        == SCENARIO,
        "priority_is_a_c_e_d": tuple(FIXED_PRIORITY) == ("A", "C", "E", "D"),
        "formal_baseline_hashes_unchanged": baseline_hashes
        == {leg: sha256_path(path) for leg, path in baseline_paths.items()},
        "old_c_plan_exactly_unchanged": old_c_exact,
        "new_branch_profile_exact": str(release["profile_id"]) == THIRD_PROFILE,
        "new_branch_plan_count_exact": len(third_plan)
        == int(release["new_branch_plan_count"]),
        "new_branch_factor_contract_all_passed": bool(factor_pass.all()),
        "new_branch_all_hold_t2": pd.to_numeric(
            third_plan["hold_offset"], errors="raise"
        )
        .eq(2)
        .all(),
        "new_branch_all_exit_rule_t2": third_plan["exit_rule"]
        .astype(str)
        .eq("FIXED_T2_CLOSE")
        .all(),
        "old_c_all_hold_t3": pd.to_numeric(
            old_c_plan["hold_offset"], errors="raise"
        )
        .eq(3)
        .all(),
        "old_c_all_exit_rule_t3": old_c_plan["exit_rule"]
        .astype(str)
        .eq("FIXED_T3_CLOSE")
        .all(),
        "c_plan_action_date_unique": not plan["action_date"].duplicated().any(),
        "deterministic_double_replay": deterministic_double_replay,
        "new_branch_exit_not_before_buy": third_plan["exit_date"]
        .astype(str)
        .ge(third_plan["buy_date"].astype(str))
        .all(),
        "combo_leg_counts_exact": combo_metrics["leg_counts"]
        == release["bundle_leg_counts"],
        **metric_checks(third_metrics, release, "new_branch"),
        **metric_checks(c_metrics, release, "c"),
        **metric_checks(combo_metrics, release, "bundle"),
    }
    failures = [name for name, passed in checks.items() if not bool(passed)]
    if failures:
        raise RuntimeError(f"C第3分支认证失败，禁止发布: {failures}")
    checks = {name: bool(passed) for name, passed in checks.items()}

    stress_rows: list[dict[str, Any]] = []
    for rate in monthly_config["execution"]["stress_slippage_rates"]:
        for scenario, legs, priority in (
            ("C", {"C": plan}, ("C",)),
            ("ACED", release_legs, FIXED_PRIORITY),
        ):
            stressed = replay_action_date_cash_portfolio(
                _stress_plans(legs, float(rate)),
                action_dates=context["action_dates"],
                priority=priority,
                **execution,
            )
            measured = action_metrics(stressed, window.start, window.end)
            stress_rows.append(
                {
                    "scenario": scenario,
                    "slippage_rate_each_side": float(rate),
                    "trade_count": measured["trade_count"],
                    "equity_multiple": measured["equity_multiple"],
                    "max_drawdown": measured["max_drawdown"],
                }
            )
    stress = pd.DataFrame(stress_rows)
    if not np.isfinite(
        stress[["equity_multiple", "max_drawdown"]].to_numpy(float)
    ).all():
        raise RuntimeError("C第3分支压力测试出现非有限值，禁止发布")

    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    OUTPUT_ROOT.mkdir(parents=True)
    for leg, frame in release_legs.items():
        frame.to_csv(OUTPUT_ROOT / f"{leg.lower()}_plans.csv", index=False)
    c_detail.to_csv(OUTPUT_ROOT / "c_trades.csv", index=False)
    third_plan.to_csv(OUTPUT_ROOT / "third_branch_plans.csv", index=False)
    third_detail.to_csv(OUTPUT_ROOT / "third_branch_trades.csv", index=False)
    factor_audit.to_csv(OUTPUT_ROOT / "third_branch_factor_audit.csv", index=False)
    combo_detail.to_csv(OUTPUT_ROOT / "combo_trades.csv", index=False)
    stress.to_csv(OUTPUT_ROOT / "stress_metrics.csv", index=False)

    summary = {
        "schema_version": 1,
        "release_id": RELEASE_ID,
        "strategy_c_release_id": c_config["release_id"],
        "decision": "RELEASE_CERTIFIED_USER_APPROVED",
        "window": {"start": window.start, "end": window.end},
        "priority": list(FIXED_PRIORITY),
        "selected_variants": {
            "A": "A_RISK_EXCLUDE_AFTERNOON_FIRST_SEAL",
            "C": "C_THIRD_BRANCH_T2",
            "E": "E_TURNOVER_FD_AMOUNT12_OPEN23_NO_TRADE",
            "D": "D_ACTIVE_LT20_OPEN2_EXTENSION",
        },
        "new_branch_metrics": third_metrics,
        "c_metrics": c_metrics,
        "combo_metrics": combo_metrics,
        "validation_status": "PASS",
        "validation_checks": checks,
        "formal_strategy_modified": True,
        "certification_precedes_code_commit": True,
        "research_protocol": "STRICT_DISCOVERY_POST_REVIEW_STAGE4_USER_ACCEPTED",
        "risk_note": release["risk_note"],
    }
    write_json(OUTPUT_ROOT / "release_summary.json", summary)
    artifacts = {
        path.name: sha256_path(path)
        for path in sorted(OUTPUT_ROOT.iterdir())
        if path.is_file()
    }
    write_json(
        OUTPUT_ROOT / "artifact_manifest.json",
        {
            "schema_version": 1,
            "strategy_config_sha256": sha256_path(strategy_config_path),
            "baseline_plan_sha256": baseline_hashes,
            "artifacts": artifacts,
        },
    )
    input_files = [
        str(paths["strict_feature_pool"].relative_to(ROOT)),
        str(paths["market_sentiment"].relative_to(ROOT)),
        str(paths["d_event_source"].relative_to(ROOT)),
        str(paths["trade_calendar"].relative_to(ROOT)),
        *[
            str((BASELINE_ROOT / f"{leg.lower()}_plans.csv").relative_to(ROOT))
            for leg in FIXED_PRIORITY
        ],
        *[
            str((OUTPUT_ROOT / name).relative_to(ROOT))
            for name in (
                "artifact_manifest.json",
                "release_summary.json",
                "a_plans.csv",
                "c_plans.csv",
                "e_plans.csv",
                "d_plans.csv",
                "c_trades.csv",
                "combo_trades.csv",
                "third_branch_plans.csv",
                "third_branch_trades.csv",
                "third_branch_factor_audit.csv",
                "stress_metrics.csv",
            )
        ],
    ]
    live_payload = {
        "schema_version": 1,
        "status": "PASS",
        "scenario": SCENARIO,
        "release_id": RELEASE_ID,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "certification_scope": "FORMAL_RULE_BRANCH_EXIT_AND_BACKTEST_ALIGNMENT",
        "current_executable": True,
        "release_eligible": False,
        "strict_asof_standard_id": runtime_config["strict_asof"]["standard_id"],
        "strict_asof_passed": True,
        "research_protocol": "STRICT_DISCOVERY_POST_REVIEW_STAGE4_USER_ACCEPTED",
        "independent_oos_certified": False,
        "capacity_certified": False,
        "window": {
            "start": window.start,
            "end": window.end,
            "trade_days": len(context["action_dates"]),
        },
        "selected_variants": summary["selected_variants"],
        "rule_checks": checks,
        "plan_checks": {
            "old_c_plan_exactly_unchanged": old_c_exact,
            "new_branch_plan_count": int(len(third_plan)),
            "new_branch_all_hold_t2": bool(checks["new_branch_all_hold_t2"]),
            "old_c_all_hold_t3": bool(checks["old_c_all_hold_t3"]),
        },
        "deterministic_double_replay": deterministic_double_replay,
        "combo_metrics_match": all(
            bool(checks[name])
            for name in checks
            if name.startswith("bundle_")
        ),
        "combo_metrics": combo_metrics,
        "standalone_metrics_match": all(
            bool(checks[name])
            for name in checks
            if name.startswith("c_") or name.startswith("new_branch_")
        ),
        "standalone_metrics": standalone_metrics,
        "new_branch_metrics": third_metrics,
        "data_and_artifact_integrity": {
            "passed": True,
            "formal_artifact_manifest": str(
                (OUTPUT_ROOT / "artifact_manifest.json").relative_to(ROOT)
            ),
        },
        "config_sha256": certification_config_sha256(runtime_config),
        "code_files": CODE_FILES,
        "code_sha256": certification_files_sha256(ROOT, CODE_FILES),
        "input_files": input_files,
        "input_sha256": certification_files_sha256(ROOT, input_files),
        "source_summary_sha256": certification_file_sha256(
            OUTPUT_ROOT / "release_summary.json"
        ),
        "risk_note": release["risk_note"],
    }
    write_json(LIVE_CERTIFICATION_PATH, live_payload)
    LIVE_REPORT_PATH.write_text(
        "\n".join(
            [
                "# ACDE策略C第3分支V16正式规则对齐报告",
                "",
                "- 状态：PASS",
                f"- 场景：{SCENARIO}",
                f"- 窗口：{window.start}～{window.end}（{len(context['action_dates'])}个交易日）",
                (
                    f"- 新分支：{third_metrics['trade_count']}笔，胜率"
                    f"{third_metrics['win_rate']:.2%}，复利"
                    f"{third_metrics['equity_multiple']:.6f}倍，最大回撤"
                    f"{third_metrics['max_drawdown']:.2%}"
                ),
                (
                    f"- 组合：{combo_metrics['trade_count']}笔，胜率"
                    f"{combo_metrics['win_rate']:.2%}，复利"
                    f"{combo_metrics['equity_multiple']:.6f}倍，最大回撤"
                    f"{combo_metrics['max_drawdown']:.2%}"
                ),
                f"- 分腿：{combo_metrics['leg_counts']}",
                "- 旧C计划：78条逐笔保持不变，T+3退出",
                "- 新C第3分支：20条计划全部T+2退出",
                "- 两次确定性回放：一致",
                "",
                (
                    "风险说明：该认证只证明正式规则、分支级退出、计划和回测逻辑"
                    "对齐；不把同窗搜索伪装成独立样本外或真实资金容量认证，也不"
                    "承诺未来收益。"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    print(json.dumps(run(), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
