#!/usr/bin/env python3
"""在冻结C新分支选股后，对T+2至T+5退出做隔离质量研究。"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_ac_daily_candidates import trade_return_details
from scripts.research_strategy_c_sample_compound import sha256_path
from scripts.validate_other_live_strategies_strict import account_return
from src.acde_monthly_research import (
    _context,
    _execution_kwargs,
    _stress_plans,
    load_monthly_config,
    monthly_paths,
)
from src.acde_rolling_candidates import plan_signature
from src.acde_rolling_framework import (
    FIXED_PRIORITY,
    action_metrics,
    build_monthly_research_window,
    replay_action_date_cash_portfolio,
)
from src.utils.config import load_json_config


DEFAULT_SPEC = ROOT / "config/strategy_c_new_branch_exit_research.json"


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def rebuild_new_branch_exit(
    source_plan: pd.DataFrame,
    *,
    hold_offset: int,
    cutoff: str,
) -> pd.DataFrame:
    """只重算新增分支退出；旧C计划的T+3价格、状态和占资完全不动。"""

    plan = source_plan.copy()
    for column in ("exit_rule", "status", "exit_date", "position_open_until"):
        plan[column] = plan[column].astype("object")
    branch_mask = (
        plan["matched_condition_profile_ids"]
        .fillna("")
        .astype(str)
        .str.contains("C_QUALITY_")
    )
    if int(hold_offset) == 3:
        return plan
    for index, row in plan.loc[branch_mask].iterrows():
        execution = trade_return_details(
            str(row["signal_date"]),
            str(row["ts_code"]),
            int(hold_offset),
            name=str(row.get("name", "")),
            cutoff_date=cutoff,
        )
        plan.at[index, "hold_offset"] = int(hold_offset)
        plan.at[index, "exit_rule"] = f"FIXED_T{int(hold_offset)}_CLOSE"
        plan.at[index, "status"] = str(execution.status)
        plan.at[index, "exit_date"] = str(execution.exit_date or "")
        plan.at[index, "position_open_until"] = str(
            execution.exit_date or cutoff
        )
        if execution.status == "OK" and execution.stock_return is not None:
            plan.at[index, "stock_return_before_fees"] = float(
                execution.stock_return
            )
            plan.at[index, "entry_reference_price"] = float(
                execution.entry_reference_price
            )
            plan.at[index, "entry_price"] = float(execution.entry_price)
            plan.at[index, "exit_reference_price"] = float(
                execution.exit_reference_price
            )
            plan.at[index, "exit_price"] = float(execution.exit_price)
            plan.at[index, "buy_adj_factor"] = float(execution.buy_adj_factor)
            plan.at[index, "sell_adj_factor"] = float(execution.sell_adj_factor)
            plan.at[index, "account_return"] = account_return(
                float(execution.stock_return), str(execution.exit_date)
            )
            plan.at[index, "entry_filled"] = True
            plan.at[index, "position_opened"] = True
            plan.at[index, "outcome_observable"] = True
        else:
            plan.at[index, "stock_return_before_fees"] = np.nan
            plan.at[index, "account_return"] = np.nan
            plan.at[index, "outcome_observable"] = False
    return plan.sort_values(["action_date", "ts_code"]).reset_index(drop=True)


def branch_detail(detail: pd.DataFrame, plan: pd.DataFrame) -> pd.DataFrame:
    labels = plan[
        ["action_date", "ts_code", "matched_condition_profile_ids"]
    ].copy()
    joined = detail[detail["status"].astype(str).eq("EXECUTED")].merge(
        labels,
        on=["action_date", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    return joined[
        joined["matched_condition_profile_ids"]
        .fillna("")
        .astype(str)
        .str.contains("C_QUALITY_")
    ].copy()


def period_metrics(detail: pd.DataFrame, scenario: str) -> pd.DataFrame:
    trades = detail[detail["status"].astype(str).eq("EXECUTED")].copy()
    if trades.empty:
        return pd.DataFrame()
    trades["year"] = trades["action_date"].astype(str).str[:4]
    rows: list[dict[str, Any]] = []
    for year, group in trades.groupby("year", sort=True):
        rows.append(
            {
                "scenario": scenario,
                "year": str(year),
                **action_metrics(
                    group,
                    str(group["action_date"].min()),
                    str(group["action_date"].max()),
                ),
            }
        )
    return pd.DataFrame(rows)


def run(spec_path: Path) -> dict[str, Any]:
    spec = load_json_config(spec_path)
    if spec.get("mode") != "research_only" or bool(
        spec.get("formal_strategy_auto_apply", True)
    ):
        raise ValueError("C新分支退出研究必须禁止自动落地")
    if tuple(spec["frozen_rules"]["priority"]) != FIXED_PRIORITY:
        raise ValueError("C新分支退出研究必须固定A>C>E>D")

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
    source_root = ROOT / str(spec["source_stage2_root"])
    source_summary = load_json_config(source_root / "summary.json")
    if source_summary["selected_variant"] != spec["frozen_rules"][
        "new_branch_factor_rule"
    ]:
        raise RuntimeError("第三阶段读取的新分支因子规则与第二阶段胜者不一致")
    source_plan = pd.read_csv(
        source_root / "selected_c_plan.csv",
        low_memory=False,
        dtype={"signal_date": str, "action_date": str, "buy_date": str, "ts_code": str},
    )

    formal_root = ROOT / str(spec["formal_baseline_root"])
    formal_paths = {
        leg: formal_root / f"{leg.lower()}_plans.csv" for leg in FIXED_PRIORITY
    }
    formal_hashes = {leg: sha256_path(path) for leg, path in formal_paths.items()}
    formal_legs = {
        leg: pd.read_csv(path, low_memory=False)
        for leg, path in formal_paths.items()
    }
    gates = spec["quality_gates"]
    tolerance = float(gates["comparison_tolerance"])
    rows: list[dict[str, Any]] = []
    stores: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    for hold in spec["candidate_new_branch_exit_offsets"]:
        plan = rebuild_new_branch_exit(
            source_plan, hold_offset=int(hold), cutoff=cutoff
        )
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
        variant_id = f"C_NEW_BRANCH_EXIT_T{int(hold)}"
        rows.append(
            {
                "variant_id": variant_id,
                "new_branch_hold_offset": int(hold),
                "plan_signature": plan_signature(plan),
                "eligible": not reasons,
                "gate_reasons": ";".join(reasons),
                **{f"branch_{key}": value for key, value in branch_metrics.items()},
                **{f"c_{key}": value for key, value in c_metrics.items()},
                **{f"portfolio_{key}": value for key, value in portfolio_metrics.items()},
            }
        )
        stores[variant_id] = (plan, c_detail, new_branch_detail, portfolio_detail)

    metrics = pd.DataFrame(rows)
    eligible = metrics[metrics["eligible"].astype(bool)].sort_values(
        [
            "portfolio_equity_multiple",
            "branch_win_rate",
            "branch_avg_account_return",
        ],
        ascending=False,
    )
    selected_id = str(eligible.iloc[0]["variant_id"]) if not eligible.empty else ""
    output_root = ROOT / str(spec["output_root"])
    output_root.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(output_root / "candidate_exit_metrics.csv", index=False)
    write_json(
        output_root / "frozen_manifest.json",
        {
            "spec_path": str(spec_path.relative_to(ROOT)),
            "spec_sha256": sha256_path(spec_path),
            "source_stage2_plan_sha256": sha256_path(
                source_root / "selected_c_plan.csv"
            ),
            "formal_plan_sha256": formal_hashes,
            "candidate_exit_offsets_declared_before_final_replay": list(
                spec["candidate_new_branch_exit_offsets"]
            ),
        },
    )

    selected_branch_metrics: dict[str, Any] = {}
    selected_c_metrics: dict[str, Any] = {}
    selected_portfolio_metrics: dict[str, Any] = {}
    checks: dict[str, bool] = {}
    if selected_id:
        plan, c_detail, new_branch_detail, portfolio_detail = stores[selected_id]
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
        pd.concat(
            [
                period_metrics(new_branch_detail, "NEW_BRANCH"),
                period_metrics(c_detail, "NEW_C"),
                period_metrics(portfolio_detail, "NEW_ACED"),
            ],
            ignore_index=True,
        ).to_csv(output_root / "year_metrics.csv", index=False)

        selected_hold = int(selected_id.rsplit("T", 1)[1])
        branch_mask = (
            plan["matched_condition_profile_ids"]
            .fillna("")
            .astype(str)
            .str.contains("C_QUALITY_")
        )
        source_branch_mask = (
            source_plan["matched_condition_profile_ids"]
            .fillna("")
            .astype(str)
            .str.contains("C_QUALITY_")
        )
        old_columns = sorted(set(source_plan.columns) & set(plan.columns))
        source_old = (
            source_plan.loc[~source_branch_mask, old_columns]
            .fillna("")
            .astype(str)
            .sort_values(["action_date", "ts_code"])
            .reset_index(drop=True)
        )
        selected_old = (
            plan.loc[~branch_mask, old_columns]
            .fillna("")
            .astype(str)
            .sort_values(["action_date", "ts_code"])
            .reset_index(drop=True)
        )
        source_entries = source_plan.loc[
            source_branch_mask, ["action_date", "ts_code", "entry_price"]
        ].copy()
        selected_entries = plan.loc[
            branch_mask, ["action_date", "ts_code", "entry_price"]
        ].copy()
        entry_comparison = source_entries.merge(
            selected_entries,
            on=["action_date", "ts_code"],
            suffixes=("_source", "_selected"),
            validate="one_to_one",
        )
        date_position = {
            str(date): position for position, date in enumerate(context["action_dates"])
        }
        selected_branch_rows = plan.loc[branch_mask]
        exit_not_before_target_ok = all(
            date_position.get(str(row["exit_date"]), -999)
            - date_position.get(str(row["signal_date"]), -999)
            >= selected_hold
            for _, row in selected_branch_rows.iterrows()
            if str(row["status"]) == "OK"
        )
        stress_rows: list[dict[str, Any]] = []
        selected_legs = {leg: frame.copy() for leg, frame in formal_legs.items()}
        selected_legs["C"] = plan
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
        stress = pd.DataFrame(stress_rows)
        stress.to_csv(output_root / "stress_metrics.csv", index=False)
        checks = {
            "formal_hashes_unchanged": formal_hashes
            == {leg: sha256_path(path) for leg, path in formal_paths.items()},
            "source_stage2_plan_unchanged": sha256_path(
                source_root / "selected_c_plan.csv"
            )
            == load_json_config(output_root / "frozen_manifest.json")[
                "source_stage2_plan_sha256"
            ],
            "old_c_rows_remain_t3": pd.to_numeric(
                plan.loc[~branch_mask, "hold_offset"], errors="raise"
            ).eq(3).all(),
            "old_c_rows_exactly_unchanged": source_old.equals(selected_old),
            "new_branch_rows_use_selected_exit": pd.to_numeric(
                plan.loc[branch_mask, "hold_offset"], errors="raise"
            ).eq(selected_hold).all(),
            "new_branch_entry_prices_unchanged": np.allclose(
                pd.to_numeric(
                    entry_comparison["entry_price_source"], errors="raise"
                ),
                pd.to_numeric(
                    entry_comparison["entry_price_selected"], errors="raise"
                ),
                rtol=0.0,
                atol=1e-12,
            ),
            "new_branch_exit_not_before_target": exit_not_before_target_ok,
            "t1_entry_action_equals_buy": plan["action_date"].astype(str).eq(
                plan["buy_date"].astype(str)
            ).all(),
            "plan_action_date_unique": not plan["action_date"].duplicated().any(),
            "stress_finite": np.isfinite(
                stress[["equity_multiple", "max_drawdown"]].to_numpy(float)
            ).all(),
        }

    summary = {
        "schema_version": 1,
        "decision": "USER_REVIEW" if selected_id else "KEEP_CURRENT",
        "research_protocol": str(spec["research_protocol"]),
        "formal_strategy_modified": False,
        "code_committed": False,
        "window": {"start": window.start, "end": window.end},
        "selected_variant": selected_id,
        "selected_new_branch": selected_branch_metrics,
        "selected_c": selected_c_metrics,
        "selected_portfolio": selected_portfolio_metrics,
        "validation_status": (
            "PASS"
            if selected_id and all(bool(value) for value in checks.values())
            else "FAIL"
        ),
        "validation_checks": {key: bool(value) for key, value in checks.items()},
        "risk_note": (
            "第三阶段在看过前两阶段结果后继续选择退出周期，仍是同窗STRICT_DISCOVERY；"
            "年度分布不完整，不能冒充独立样本外。"
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
