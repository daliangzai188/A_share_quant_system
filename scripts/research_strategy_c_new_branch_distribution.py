#!/usr/bin/env python3
"""按中位数、均值差和胜率硬门，联合研究C新分支因子与退出。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.optimize_acde_rolling_three_year import build_variant_plan
from scripts.research_strategy_c_new_branch_exit import (
    branch_detail,
    period_metrics,
    rebuild_new_branch_exit,
)
from scripts.research_strategy_c_new_branch_quality import (
    _template_contract_mask,
    build_candidate_config,
)
from scripts.research_strategy_c_sample_compound import sha256_path
from src.acde_monthly_research import (
    _context,
    _execution_kwargs,
    _stress_plans,
    load_monthly_config,
    monthly_paths,
)
from src.acde_rolling_candidates import StaticOutcomeCache, VariantDefinition, plan_signature
from src.acde_rolling_framework import (
    FIXED_PRIORITY,
    action_metrics,
    build_monthly_research_window,
    replay_action_date_cash_portfolio,
)
from src.utils.config import load_json_config


DEFAULT_SPEC = ROOT / "config/strategy_c_new_branch_distribution_research.json"


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def concentration_metrics(detail: pd.DataFrame) -> dict[str, Any]:
    values = pd.to_numeric(
        detail.loc[detail["status"].astype(str).eq("EXECUTED"), "account_return"],
        errors="raise",
    ).to_numpy(float)
    ordered = np.sort(values)[::-1]
    positive_logs = np.log1p(values[values > 0])
    total_positive_log = float(positive_logs.sum())
    return {
        "trade_count": int(len(values)),
        "simple_return_sum": float(values.sum()),
        "top5_simple_return_sum": float(ordered[:5].sum()),
        "remaining_after_top5_simple_return_sum": float(ordered[5:].sum()),
        "top1_positive_log_share": (
            float(np.sort(positive_logs)[::-1][:1].sum() / total_positive_log)
            if total_positive_log > 0
            else 0.0
        ),
        "top5_positive_log_share": (
            float(np.sort(positive_logs)[::-1][:5].sum() / total_positive_log)
            if total_positive_log > 0
            else 0.0
        ),
    }


def run(spec_path: Path) -> dict[str, Any]:
    spec = load_json_config(spec_path)
    if spec.get("mode") != "research_only" or bool(
        spec.get("formal_strategy_auto_apply", True)
    ):
        raise ValueError("C新分支收益分布研究必须禁止自动落地")
    if tuple(spec["frozen_rules"]["priority"]) != FIXED_PRIORITY:
        raise ValueError("C新分支收益分布研究必须固定A>C>E>D")

    factor_spec_path = ROOT / str(spec["factor_candidate_spec"])
    exit_spec_path = ROOT / str(spec["exit_candidate_spec"])
    factor_spec = load_json_config(factor_spec_path)
    exit_spec = load_json_config(exit_spec_path)
    templates = list(factor_spec["candidate_templates"])
    exit_offsets = [
        int(value) for value in exit_spec["candidate_new_branch_exit_offsets"]
    ]

    cutoff = str(spec["cutoff"])
    window = build_monthly_research_window(cutoff)
    monthly_config = load_monthly_config(ROOT / "config/acde_rolling_optimization.json")
    paths = monthly_paths(monthly_config, cutoff)
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
    formal_root = ROOT / str(spec["formal_baseline_root"])
    formal_paths = {
        leg: formal_root / f"{leg.lower()}_plans.csv" for leg in FIXED_PRIORITY
    }
    formal_hashes = {leg: sha256_path(path) for leg, path in formal_paths.items()}
    formal_legs = {
        leg: pd.read_csv(
            path,
            low_memory=False,
            dtype={"signal_date": str, "action_date": str, "buy_date": str, "ts_code": str},
        )
        for leg, path in formal_paths.items()
    }
    base_config = load_json_config(ROOT / "config/strategy_config.json")
    cache = StaticOutcomeCache()
    gates = spec["distribution_quality_gates"]
    tolerance = float(gates["comparison_tolerance"])

    rows: list[dict[str, Any]] = []
    stores: dict[
        str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, Mapping[str, Any]]
    ] = {}
    seen: dict[str, str] = {}
    declared_count = len(templates) * len(exit_offsets)
    completed = 0
    for template in templates:
        configured = build_candidate_config(base_config, factor_spec, template)
        definition = VariantDefinition(
            "C",
            f"C_DIST_{template['id']}",
            str(template["description"]),
            configured,
            2,
            True,
            "冻结因子模板后与冻结退出集合做笛卡尔积",
        )
        hold3_plan = build_variant_plan(
            definition,
            signal_pool=context["signal_pool"],
            d_events=context["d_events"],
            allowed_action_dates=context["allowed_actions"],
            cutoff=cutoff,
            outcome_cache=cache,
        )
        for hold in exit_offsets:
            completed += 1
            variant_id = f"C_DIST_{template['id']}__T{hold}"
            plan = rebuild_new_branch_exit(
                hold3_plan, hold_offset=hold, cutoff=cutoff
            )
            signature = plan_signature(plan)
            duplicate_of = seen.get(signature, "")
            if duplicate_of:
                rows.append(
                    {
                        "variant_id": variant_id,
                        "template_id": template["id"],
                        "new_branch_hold_offset": hold,
                        "plan_signature": signature,
                        "duplicate_of": duplicate_of,
                        "eligible": False,
                        "gate_reasons": "DUPLICATE_PLAN_SIGNATURE",
                    }
                )
                continue
            seen[signature] = variant_id

            c_detail = replay_action_date_cash_portfolio(
                {"C": plan},
                action_dates=context["action_dates"],
                priority=("C",),
                **execution,
            )
            new_branch_detail = branch_detail(c_detail, plan)
            branch_metrics = action_metrics(
                new_branch_detail, window.start, window.end
            )
            c_metrics = action_metrics(c_detail, window.start, window.end)
            legs = {leg: frame.copy() for leg, frame in formal_legs.items()}
            legs["C"] = plan
            portfolio_detail = replay_action_date_cash_portfolio(
                legs,
                action_dates=context["action_dates"],
                priority=FIXED_PRIORITY,
                **execution,
            )
            portfolio_metrics = action_metrics(
                portfolio_detail, window.start, window.end
            )
            mean_median_gap = float(branch_metrics["avg_account_return"]) - float(
                branch_metrics["median_account_return"]
            )
            passed = {
                "branch_sample": int(branch_metrics["trade_count"])
                >= int(gates["new_branch_minimum_executed_trades"]),
                "branch_win_rate": float(branch_metrics["win_rate"])
                + tolerance
                >= float(gates["new_branch_minimum_win_rate"]),
                "branch_average": float(branch_metrics["avg_account_return"])
                + tolerance
                >= float(gates["new_branch_minimum_average_return"]),
                "branch_median": float(branch_metrics["median_account_return"])
                + tolerance
                >= float(gates["new_branch_minimum_median_return"]),
                "branch_mean_median_gap": mean_median_gap
                <= float(gates["maximum_average_minus_median"]) + tolerance,
                "c_sample": int(c_metrics["trade_count"])
                > int(gates["c_total_trade_count_strictly_above_formal"]),
                "c_compound": float(c_metrics["equity_multiple"])
                > float(gates["c_equity_multiple_strictly_above_formal"])
                + tolerance,
                "portfolio_compound": float(portfolio_metrics["equity_multiple"])
                > float(gates["portfolio_equity_multiple_strictly_above_formal"])
                + tolerance,
            }
            reasons = [name for name, value in passed.items() if not value]
            rows.append(
                {
                    "variant_id": variant_id,
                    "template_id": template["id"],
                    "description": template["description"],
                    "new_branch_hold_offset": hold,
                    "plan_signature": signature,
                    "duplicate_of": "",
                    "eligible": not reasons,
                    "gate_reasons": ";".join(reasons),
                    "branch_average_minus_median": mean_median_gap,
                    **{f"branch_{key}": value for key, value in branch_metrics.items()},
                    **{f"c_{key}": value for key, value in c_metrics.items()},
                    **{f"portfolio_{key}": value for key, value in portfolio_metrics.items()},
                }
            )
            stores[variant_id] = (
                plan,
                c_detail,
                new_branch_detail,
                portfolio_detail,
                template,
            )
            print(
                f"[{completed}/{declared_count}] {variant_id}: "
                f"新分支{branch_metrics['trade_count']}笔/"
                f"胜率{branch_metrics['win_rate']:.2%}/"
                f"均值{branch_metrics['avg_account_return']:.2%}/"
                f"中位{branch_metrics['median_account_return']:.2%}/"
                f"差{mean_median_gap:.2%}，"
                f"C={c_metrics['equity_multiple']:.4f}倍，"
                f"组合={portfolio_metrics['equity_multiple']:.4f}倍，"
                f"{'PASS' if not reasons else 'FAIL'}",
                flush=True,
            )

    metrics = pd.DataFrame(rows)
    eligible = metrics[metrics["eligible"].fillna(False).astype(bool)].sort_values(
        [
            "portfolio_equity_multiple",
            "branch_median_account_return",
            "branch_win_rate",
            "branch_trade_count",
        ],
        ascending=False,
    )
    selected_id = str(eligible.iloc[0]["variant_id"]) if not eligible.empty else ""
    output_root = ROOT / str(spec["output_root"])
    output_root.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(output_root / "candidate_distribution_metrics.csv", index=False)
    write_json(
        output_root / "frozen_manifest.json",
        {
            "spec_path": str(spec_path.relative_to(ROOT)),
            "spec_sha256": sha256_path(spec_path),
            "factor_candidate_spec": str(factor_spec_path.relative_to(ROOT)),
            "factor_candidate_spec_sha256": sha256_path(factor_spec_path),
            "exit_candidate_spec": str(exit_spec_path.relative_to(ROOT)),
            "exit_candidate_spec_sha256": sha256_path(exit_spec_path),
            "candidate_templates": templates,
            "candidate_exit_offsets": exit_offsets,
            "candidate_declared_count": declared_count,
            "formal_plan_sha256": formal_hashes,
        },
    )

    unique_metrics = metrics[metrics["duplicate_of"].fillna("").eq("")].copy()
    gate_names = (
        "branch_sample",
        "branch_win_rate",
        "branch_average",
        "branch_median",
        "branch_mean_median_gap",
        "c_sample",
        "c_compound",
        "portfolio_compound",
    )
    gate_pass_counts = {
        gate: int(
            (~unique_metrics["gate_reasons"].fillna("").str.contains(gate)).sum()
        )
        for gate in gate_names
    }
    selected_branch_metrics: dict[str, Any] = {}
    selected_c_metrics: dict[str, Any] = {}
    selected_portfolio_metrics: dict[str, Any] = {}
    # “没有候选过门槛”是一个有效研究结论，不等于流水线运行失败。
    # 先验证冻结搜索本身完整、正式基线未被改动；只有选出候选时，
    # 才追加候选级合同、退出与压力测试检查。
    checks: dict[str, bool] = {
        "formal_hashes_unchanged": formal_hashes
        == {leg: sha256_path(path) for leg, path in formal_paths.items()},
        "candidate_declared_count_matches_rows": len(metrics) == declared_count,
        "candidate_unique_count_matches_signatures": len(unique_metrics) == len(seen),
    }
    selected_concentration: dict[str, Any] = {}
    if selected_id:
        plan, c_detail, new_branch_detail, portfolio_detail, template = stores[
            selected_id
        ]
        plan.to_csv(output_root / "selected_c_plan.csv", index=False)
        c_detail.to_csv(output_root / "selected_c_ledger.csv", index=False)
        new_branch_detail.to_csv(
            output_root / "selected_new_branch_trades.csv", index=False
        )
        portfolio_detail.to_csv(
            output_root / "selected_portfolio_ledger.csv", index=False
        )
        selected_branch_metrics = action_metrics(
            new_branch_detail, window.start, window.end
        )
        selected_c_metrics = action_metrics(c_detail, window.start, window.end)
        selected_portfolio_metrics = action_metrics(
            portfolio_detail, window.start, window.end
        )
        selected_concentration = concentration_metrics(new_branch_detail)
        write_json(output_root / "concentration_review.json", selected_concentration)
        pd.concat(
            [
                period_metrics(new_branch_detail, "NEW_BRANCH"),
                period_metrics(c_detail, "NEW_C"),
                period_metrics(portfolio_detail, "NEW_ACED"),
            ],
            ignore_index=True,
        ).to_csv(output_root / "year_metrics.csv", index=False)

        feature = pd.read_csv(paths["strict_feature_pool"], low_memory=False)
        feature["trade_date"] = feature["trade_date"].astype(str).str.replace(
            r"\.0$", "", regex=True
        )
        required_columns = sorted(
            {
                str(condition["column"])
                for condition in factor_spec["new_branch_base_conditions"]
            }
            | {
                str(condition["column"])
                for clause in template["or_clauses"]
                for condition in clause["conditions"]
            }
        )
        branch_mask = (
            plan["matched_condition_profile_ids"]
            .fillna("")
            .astype(str)
            .str.contains("C_QUALITY_")
        )
        factor_audit = plan.loc[branch_mask].merge(
            feature[["trade_date", "ts_code", *required_columns]],
            left_on=["signal_date", "ts_code"],
            right_on=["trade_date", "ts_code"],
            how="left",
            validate="one_to_one",
        )
        factor_pass = _template_contract_mask(factor_audit, factor_spec, template)
        factor_audit["factor_contract_passed"] = factor_pass
        factor_audit.to_csv(output_root / "selected_factor_audit.csv", index=False)

        selected_hold = int(selected_id.rsplit("T", 1)[1])
        date_positions = {
            str(date): position for position, date in enumerate(context["action_dates"])
        }
        exit_not_before = all(
            date_positions.get(str(row["exit_date"]), -999)
            - date_positions.get(str(row["signal_date"]), -999)
            >= selected_hold
            for _, row in plan.loc[branch_mask].iterrows()
            if str(row["status"]) == "OK"
        )
        old_plan = plan.loc[~branch_mask].copy()
        formal_c = formal_legs["C"].copy()
        compare_columns = sorted(set(old_plan.columns) & set(formal_c.columns))
        normalized_old = (
            old_plan[compare_columns]
            .fillna("")
            .astype(str)
            .sort_values(["action_date", "ts_code"])
            .reset_index(drop=True)
        )
        normalized_formal = (
            formal_c[compare_columns]
            .fillna("")
            .astype(str)
            .sort_values(["action_date", "ts_code"])
            .reset_index(drop=True)
        )

        selected_legs = {leg: frame.copy() for leg, frame in formal_legs.items()}
        selected_legs["C"] = plan
        stress_rows: list[dict[str, Any]] = []
        for rate in monthly_config["execution"]["stress_slippage_rates"]:
            for scenario, legs, priority in (
                ("NEW_C", {"C": plan}, ("C",)),
                ("NEW_ACED", selected_legs, FIXED_PRIORITY),
            ):
                detail = replay_action_date_cash_portfolio(
                    _stress_plans(legs, float(rate)),
                    action_dates=context["action_dates"],
                    priority=priority,
                    **execution,
                )
                measured = action_metrics(detail, window.start, window.end)
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
        stress.to_csv(output_root / "stress_metrics.csv", index=False)
        selected_gap = float(selected_branch_metrics["avg_account_return"]) - float(
            selected_branch_metrics["median_account_return"]
        )
        checks.update(
            {
                "factor_contract_all_passed": bool(factor_pass.all()),
                "old_c_rows_exactly_match_formal": normalized_old.equals(
                    normalized_formal
                ),
                "new_branch_exit_not_before_target": exit_not_before,
                "new_branch_hold_offset_matches_selected": pd.to_numeric(
                    plan.loc[branch_mask, "hold_offset"], errors="raise"
                )
                .eq(selected_hold)
                .all(),
                "t1_action_equals_buy": plan["action_date"]
                .astype(str)
                .eq(plan["buy_date"].astype(str))
                .all(),
                "plan_action_date_unique": not plan["action_date"]
                .duplicated()
                .any(),
                "selected_branch_median_gate": float(
                    selected_branch_metrics["median_account_return"]
                )
                + tolerance
                >= float(gates["new_branch_minimum_median_return"]),
                "selected_branch_mean_median_gap_gate": selected_gap
                <= float(gates["maximum_average_minus_median"]) + tolerance,
                "stress_finite": np.isfinite(
                    stress[["equity_multiple", "max_drawdown"]].to_numpy(float)
                ).all(),
            }
        )

    summary = {
        "schema_version": 1,
        "decision": "USER_REVIEW" if selected_id else "KEEP_CURRENT",
        "selection_status": (
            "ELIGIBLE_CANDIDATE_FOUND"
            if selected_id
            else "NO_ELIGIBLE_CANDIDATE"
        ),
        "research_protocol": str(spec["research_protocol"]),
        "formal_strategy_modified": False,
        "code_committed": False,
        "window": {"start": window.start, "end": window.end},
        "candidate_declared_count": declared_count,
        "candidate_unique_count": int(
            metrics["duplicate_of"].fillna("").eq("").sum()
        ),
        "eligible_count": int(len(eligible)),
        "gate_pass_counts_among_unique_candidates": gate_pass_counts,
        "selected_variant": selected_id,
        "selected_new_branch": selected_branch_metrics,
        "selected_concentration": selected_concentration,
        "selected_c": selected_c_metrics,
        "selected_portfolio": selected_portfolio_metrics,
        "validation_status": (
            "PASS" if all(bool(value) for value in checks.values()) else "FAIL"
        ),
        "validation_checks": {key: bool(value) for key, value in checks.items()},
        "risk_note": (
            "第四阶段联合枚举因子模板和退出周期，且发生在阅读前三阶段结果之后；"
            "属于更强的STRICT_DISCOVERY，不能冒充独立样本外。"
        ),
    }
    write_json(output_root / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.spec.resolve()),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
