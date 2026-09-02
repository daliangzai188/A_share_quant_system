#!/usr/bin/env python3
"""精修C的30~50只涨停新分支，并严格冻结其他策略与旧C分支。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.optimize_acde_rolling_three_year import build_variant_plan
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


DEFAULT_SPEC = ROOT / "config/strategy_c_new_branch_quality_research.json"


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _expand_condition_clause(
    base_conditions: Iterable[Mapping[str, Any]],
    extra_conditions: Iterable[Mapping[str, Any]],
    *,
    profile_prefix: str,
    first_priority: int,
) -> list[dict[str, Any]]:
    definitions = [*base_conditions, *extra_conditions]
    choices = [
        [(str(item["column"]), value) for value in item["values"]]
        for item in definitions
    ]
    profiles: list[dict[str, Any]] = []
    for position, combination in enumerate(itertools.product(*choices), first_priority):
        profiles.append(
            {
                "profile_id": f"C_QUALITY_{profile_prefix}_{position:03d}",
                "priority": position,
                "conditions": [
                    {"column": column, "operator": "==", "value": value}
                    for column, value in combination
                ],
            }
        )
    return profiles


def build_candidate_config(
    base_config: Mapping[str, Any],
    spec: Mapping[str, Any],
    template: Mapping[str, Any],
) -> dict[str, Any]:
    config = copy.deepcopy(dict(base_config))
    profiles = config["paper_ab_filtered_strategy"]["c_strategy"][
        "condition_profiles"
    ]
    for clause_position, clause in enumerate(template["or_clauses"], 1):
        added = _expand_condition_clause(
            spec["new_branch_base_conditions"],
            clause["conditions"],
            profile_prefix=f"{template['id']}_CLAUSE{clause_position}",
            first_priority=len(profiles) + 1,
        )
        profiles.extend(added)
    return config


def _branch_metrics(
    detail: pd.DataFrame,
    plan: pd.DataFrame,
    *,
    start: str,
    end: str,
) -> tuple[dict[str, Any], pd.DataFrame]:
    executed = detail[detail["status"].astype(str).eq("EXECUTED")].copy()
    labels = plan[
        ["action_date", "ts_code", "matched_condition_profile_ids"]
    ].copy()
    joined = executed.merge(
        labels,
        on=["action_date", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    branch = joined[
        joined["matched_condition_profile_ids"]
        .fillna("")
        .astype(str)
        .str.contains("C_QUALITY_")
    ].copy()
    return action_metrics(branch, start, end), branch


def _template_contract_mask(
    frame: pd.DataFrame,
    spec: Mapping[str, Any],
    template: Mapping[str, Any],
) -> pd.Series:
    base_mask = pd.Series(True, index=frame.index, dtype="bool")
    for condition in spec["new_branch_base_conditions"]:
        base_mask &= frame[str(condition["column"])].isin(condition["values"])
    clause_union = pd.Series(False, index=frame.index, dtype="bool")
    for clause in template["or_clauses"]:
        clause_mask = pd.Series(True, index=frame.index, dtype="bool")
        for condition in clause["conditions"]:
            clause_mask &= frame[str(condition["column"])].isin(condition["values"])
        clause_union |= clause_mask
    return base_mask & clause_union


def run(spec_path: Path) -> dict[str, Any]:
    spec = load_json_config(spec_path)
    if spec.get("mode") != "research_only" or bool(
        spec.get("formal_strategy_auto_apply", True)
    ):
        raise ValueError("C新分支精修必须保持research_only且禁止自动落地")
    if tuple(spec["frozen_rules"]["priority"]) != FIXED_PRIORITY:
        raise ValueError("C新分支精修必须固定A>C>E>D")

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
    hashes_before = {leg: sha256_path(path) for leg, path in formal_paths.items()}
    formal_legs = {
        leg: pd.read_csv(path, low_memory=False)
        for leg, path in formal_paths.items()
    }
    base_config = load_json_config(ROOT / "config/strategy_config.json")
    cache = StaticOutcomeCache()
    tolerance = float(spec["quality_gates"]["comparison_tolerance"])

    catalog: list[dict[str, Any]] = []
    variants: list[VariantDefinition] = []
    for template in spec["candidate_templates"]:
        payload = build_candidate_config(base_config, spec, template)
        variant = VariantDefinition(
            "C",
            f"C_NEW_QUALITY_{template['id']}",
            str(template["description"]),
            payload,
            1,
            True,
            "只精修新增30~50只涨停分支，旧C分支与执行规则冻结",
        )
        variants.append(variant)
        catalog.append(
            {
                "variant_id": variant.variant_id,
                "description": variant.description,
                "template": template,
                "payload_sha256": hashlib.sha256(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(
                        "utf-8"
                    )
                ).hexdigest(),
            }
        )

    seen: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    stores: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    for position, variant in enumerate(variants, 1):
        plan = build_variant_plan(
            variant,
            signal_pool=context["signal_pool"],
            d_events=context["d_events"],
            allowed_action_dates=context["allowed_actions"],
            cutoff=cutoff,
            outcome_cache=cache,
        )
        signature = plan_signature(plan)
        duplicate_of = seen.get(signature, "")
        if duplicate_of:
            rows.append(
                {
                    "variant_id": variant.variant_id,
                    "description": variant.description,
                    "plan_signature": signature,
                    "duplicate_of": duplicate_of,
                    "eligible": False,
                    "gate_reasons": "DUPLICATE_PLAN_SIGNATURE",
                }
            )
            print(
                f"[{position}/{len(variants)}] {variant.variant_id}: 重复于{duplicate_of}",
                flush=True,
            )
            continue
        seen[signature] = variant.variant_id

        c_detail = replay_action_date_cash_portfolio(
            {"C": plan},
            action_dates=context["action_dates"],
            priority=("C",),
            **execution,
        )
        branch_metrics, branch_detail = _branch_metrics(
            c_detail, plan, start=window.start, end=window.end
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
        gates = spec["quality_gates"]
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
            > tolerance,
            "c_sample": int(c_metrics["trade_count"])
            > int(gates["c_total_trade_count_strictly_above_formal"]),
            "c_compound": float(c_metrics["equity_multiple"])
            > float(gates["c_equity_multiple_strictly_above_formal"]) + tolerance,
            "portfolio_compound": float(portfolio_metrics["equity_multiple"])
            > float(gates["portfolio_equity_multiple_strictly_above_formal"])
            + tolerance,
        }
        reasons = [name for name, value in passed.items() if not value]
        row = {
            "variant_id": variant.variant_id,
            "description": variant.description,
            "plan_signature": signature,
            "duplicate_of": "",
            "plan_count": int(len(plan)),
            "eligible": not reasons,
            "gate_reasons": ";".join(reasons),
            **{f"branch_{key}": value for key, value in branch_metrics.items()},
            **{f"c_{key}": value for key, value in c_metrics.items()},
            **{f"portfolio_{key}": value for key, value in portfolio_metrics.items()},
        }
        rows.append(row)
        stores[variant.variant_id] = (plan, c_detail, branch_detail, portfolio_detail)
        print(
            f"[{position}/{len(variants)}] {variant.variant_id}: "
            f"新分支{branch_metrics['trade_count']}笔/"
            f"胜率{branch_metrics['win_rate']:.2%}/"
            f"均值{branch_metrics['avg_account_return']:.2%}，"
            f"C={c_metrics['trade_count']}笔/{c_metrics['equity_multiple']:.6f}倍，"
            f"组合={portfolio_metrics['equity_multiple']:.6f}倍，"
            f"{'PASS' if not reasons else 'FAIL'}",
            flush=True,
        )

    metrics = pd.DataFrame(rows)
    eligible = metrics[metrics["eligible"].fillna(False).astype(bool)].copy()
    eligible = eligible.sort_values(
        [
            "portfolio_equity_multiple",
            "branch_avg_account_return",
            "branch_win_rate",
            "branch_trade_count",
            "variant_id",
        ],
        ascending=[False, False, False, False, True],
    )
    selected_id = str(eligible.iloc[0]["variant_id"]) if not eligible.empty else ""

    output_root = ROOT / str(spec["output_root"])
    output_root.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(output_root / "candidate_metrics.csv", index=False)
    write_json(
        output_root / "frozen_manifest.json",
        {
            "spec_path": str(spec_path.relative_to(ROOT)),
            "spec_sha256": sha256_path(spec_path),
            "declared_before_replay": True,
            "catalog": catalog,
            "formal_plan_sha256": hashes_before,
        },
    )

    selected_c_metrics: dict[str, Any] = {}
    selected_branch_metrics: dict[str, Any] = {}
    selected_portfolio_metrics: dict[str, Any] = {}
    validation_checks: dict[str, bool] = {}
    stress_rows: list[dict[str, Any]] = []
    if selected_id:
        selected_plan, selected_c_detail, selected_branch_detail, selected_portfolio_detail = stores[selected_id]
        selected_plan.to_csv(output_root / "selected_c_plan.csv", index=False)
        selected_c_detail.to_csv(output_root / "selected_c_ledger.csv", index=False)
        selected_branch_detail.to_csv(
            output_root / "selected_new_branch_trades.csv", index=False
        )
        selected_portfolio_detail.to_csv(
            output_root / "selected_portfolio_ledger.csv", index=False
        )
        selected_c_metrics = action_metrics(
            selected_c_detail, window.start, window.end
        )
        selected_branch_metrics = action_metrics(
            selected_branch_detail, window.start, window.end
        )
        selected_portfolio_metrics = action_metrics(
            selected_portfolio_detail, window.start, window.end
        )

        template_id = selected_id.replace("C_NEW_QUALITY_", "", 1)
        template = next(
            item for item in spec["candidate_templates"] if item["id"] == template_id
        )
        feature = pd.read_csv(paths["strict_feature_pool"], low_memory=False)
        feature["trade_date"] = feature["trade_date"].astype(str).str.replace(
            r"\.0$", "", regex=True
        )
        required_factor_columns = sorted(
            {
                str(condition["column"])
                for condition in spec["new_branch_base_conditions"]
            }
            | {
                str(condition["column"])
                for clause in template["or_clauses"]
                for condition in clause["conditions"]
            }
        )
        selected_profiles = selected_plan[
            selected_plan["matched_condition_profile_ids"]
            .fillna("")
            .astype(str)
            .str.contains("C_QUALITY_")
        ].copy()
        factor_audit = selected_profiles.merge(
            feature[["trade_date", "ts_code", *required_factor_columns]],
            left_on=["signal_date", "ts_code"],
            right_on=["trade_date", "ts_code"],
            how="left",
            validate="one_to_one",
        )
        factor_mask = _template_contract_mask(factor_audit, spec, template)
        factor_audit["factor_contract_passed"] = factor_mask
        factor_audit.to_csv(
            output_root / "selected_new_branch_factor_audit.csv", index=False
        )

        selected_legs = {leg: frame.copy() for leg, frame in formal_legs.items()}
        selected_legs["C"] = selected_plan
        for rate in monthly_config["execution"]["stress_slippage_rates"]:
            for scenario, legs, priority in (
                ("NEW_C", {"C": selected_plan}, ("C",)),
                ("NEW_ACED", selected_legs, FIXED_PRIORITY),
            ):
                detail = replay_action_date_cash_portfolio(
                    _stress_plans(legs, float(rate)),
                    action_dates=context["action_dates"],
                    priority=priority,
                    **execution,
                )
                metric = action_metrics(detail, window.start, window.end)
                stress_rows.append(
                    {
                        "scenario": scenario,
                        "slippage_rate_each_side": float(rate),
                        "trade_count": metric["trade_count"],
                        "equity_multiple": metric["equity_multiple"],
                        "max_drawdown": metric["max_drawdown"],
                    }
                )
        pd.DataFrame(stress_rows).to_csv(
            output_root / "stress_metrics.csv", index=False
        )
        validation_checks = {
            "factor_contract_all_passed": bool(factor_mask.all()),
            "selected_plan_action_date_unique": not selected_plan[
                "action_date"
            ].duplicated().any(),
            "t1_action_equals_buy": selected_plan["action_date"].astype(str).eq(
                selected_plan["buy_date"].astype(str)
            ).all(),
            "t3_hold": pd.to_numeric(
                selected_plan.loc[
                    selected_plan["strategy_leg"].astype(str).eq("C"),
                    "hold_offset",
                ],
                errors="raise",
            ).eq(3).all(),
            "formal_hashes_unchanged": hashes_before
            == {leg: sha256_path(path) for leg, path in formal_paths.items()},
            "stress_finite": np.isfinite(
                pd.DataFrame(stress_rows)[["equity_multiple", "max_drawdown"]]
                .to_numpy(float)
            ).all(),
        }

    summary = {
        "schema_version": 1,
        "decision": "USER_REVIEW" if selected_id else "KEEP_CURRENT",
        "research_protocol": str(spec["research_protocol"]),
        "formal_strategy_modified": False,
        "code_committed": False,
        "window": {"start": window.start, "end": window.end},
        "candidate_declared_count": int(len(variants)),
        "candidate_unique_count": int(
            metrics["duplicate_of"].fillna("").eq("").sum()
        ),
        "eligible_count": int(len(eligible)),
        "selected_variant": selected_id,
        "selected_new_branch": selected_branch_metrics,
        "selected_c": selected_c_metrics,
        "selected_portfolio": selected_portfolio_metrics,
        "validation_status": (
            "PASS"
            if selected_id and all(bool(value) for value in validation_checks.values())
            else "FAIL"
        ),
        "validation_checks": {
            key: bool(value) for key, value in validation_checks.items()
        },
        "risk_note": (
            "第二轮是在已看到第一轮结果后继续搜索，属于更强的STRICT_DISCOVERY；"
            "达到历史硬门也不能视为独立样本外。"
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
