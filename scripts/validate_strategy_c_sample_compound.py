#!/usr/bin/env python3
"""独立重建并校验C扩样候选，不修改配置、不发布、不提交。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.optimize_acde_rolling_three_year import build_variant_plan
from scripts.research_strategy_c_sample_compound import declared_variants, sha256_path
from src.acde_monthly_research import (
    _context,
    _execution_kwargs,
    _stress_plans,
    load_monthly_config,
    monthly_paths,
)
from src.acde_rolling_candidates import StaticOutcomeCache, plan_signature
from src.acde_rolling_framework import (
    FIXED_PRIORITY,
    action_metrics,
    build_monthly_research_window,
    replay_action_date_cash_portfolio,
)
from src.utils.config import load_json_config


DEFAULT_SPEC = ROOT / "config/strategy_c_sample_compound_research.json"


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _metrics_close(
    observed: Mapping[str, Any], expected: Mapping[str, Any], tolerance: float = 1e-9
) -> bool:
    for key in (
        "trade_count",
        "win_rate",
        "avg_account_return",
        "median_account_return",
        "equity_multiple",
        "max_drawdown",
        "max_profit",
        "max_loss",
        "profit_loss_ratio",
        "max_consecutive_losses",
    ):
        if key not in observed or key not in expected:
            return False
        if isinstance(observed[key], int) or key in {"trade_count", "max_consecutive_losses"}:
            if int(observed[key]) != int(expected[key]):
                return False
        elif not np.isclose(
            float(observed[key]), float(expected[key]), rtol=0.0, atol=tolerance
        ):
            return False
    return True


def _top_profit_removal(
    detail: pd.DataFrame, scenario: str, start: str, end: str
) -> list[dict[str, Any]]:
    executed = detail[detail["status"].astype(str).eq("EXECUTED")].copy()
    ranked = executed.sort_values("account_return", ascending=False)
    rows: list[dict[str, Any]] = []
    for count in (0, 1, 3, 5):
        changed = detail.copy()
        removed = ranked.head(count)
        if count:
            changed.loc[removed.index, "account_return"] = 0.0
        metric = action_metrics(changed, start, end)
        rows.append(
            {
                "scenario": scenario,
                "removed_top_profit_count": count,
                "removed_action_dates": ",".join(removed["action_date"].astype(str)),
                "removed_ts_codes": ",".join(removed["ts_code"].astype(str)),
                "trade_count": metric["trade_count"],
                "equity_multiple": metric["equity_multiple"],
                "max_drawdown": metric["max_drawdown"],
            }
        )
    return rows


def run(spec_path: Path) -> dict[str, Any]:
    spec = load_json_config(spec_path)
    output_root = ROOT / str(spec["output_root"])
    research_summary = load_json_config(output_root / "summary.json")
    selected_id = str(research_summary.get("selected_variant", ""))
    if not selected_id:
        raise RuntimeError("研究未选出通过三项目标的C候选")

    monthly_config = load_monthly_config(ROOT / "config/acde_rolling_optimization.json")
    cutoff = str(spec["cutoff"])
    paths = monthly_paths(monthly_config, cutoff)
    window = build_monthly_research_window(cutoff)
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
    formal_legs = {
        leg: pd.read_csv(formal_root / f"{leg.lower()}_plans.csv", low_memory=False)
        for leg in FIXED_PRIORITY
    }

    base_config = load_json_config(ROOT / "config/strategy_config.json")
    selected_definition = {
        item.variant_id: item for item in declared_variants(base_config, spec)
    }[selected_id]
    rebuilt_plan = build_variant_plan(
        selected_definition,
        signal_pool=context["signal_pool"],
        d_events=context["d_events"],
        allowed_action_dates=context["allowed_actions"],
        cutoff=cutoff,
        outcome_cache=StaticOutcomeCache(),
    )
    saved_plan = pd.read_csv(output_root / "selected_c_plan.csv", low_memory=False)

    candidate_legs = {leg: frame.copy() for leg, frame in formal_legs.items()}
    candidate_legs["C"] = rebuilt_plan
    standalone_detail = replay_action_date_cash_portfolio(
        {"C": rebuilt_plan},
        action_dates=context["action_dates"],
        priority=("C",),
        **execution,
    )
    portfolio_detail = replay_action_date_cash_portfolio(
        candidate_legs,
        action_dates=context["action_dates"],
        priority=FIXED_PRIORITY,
        **execution,
    )
    c_metrics = action_metrics(standalone_detail, window.start, window.end)
    portfolio_metrics = action_metrics(portfolio_detail, window.start, window.end)

    # 精确核对新增计划确实只来自声明的30~50只涨停、排名4~10、封单比0.1%~0.3%分支。
    current_plan = formal_legs["C"]
    current_keys = set(
        current_plan["action_date"].astype(str)
        + "|"
        + current_plan["ts_code"].astype(str)
    )
    rebuilt_keys = (
        rebuilt_plan["action_date"].astype(str)
        + "|"
        + rebuilt_plan["ts_code"].astype(str)
    )
    added_plan = rebuilt_plan.loc[~rebuilt_keys.isin(current_keys)].copy()
    feature = pd.read_csv(paths["strict_feature_pool"], low_memory=False)
    feature["trade_date"] = feature["trade_date"].astype(str).str.replace(
        r"\.0$", "", regex=True
    )
    factor_columns = [
        "trade_date",
        "ts_code",
        "limit_up_count_bucket",
        "market_leader_rank_bucket",
        "fd_ratio_bucket",
        "open_times_bucket",
        "limit_times",
        "segment_emotion_state",
        "market_limit_down_count_bucket",
    ]
    added_audit = added_plan.merge(
        feature[factor_columns],
        left_on=["signal_date", "ts_code"],
        right_on=["trade_date", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    added_factor_ok = bool(
        not added_audit.empty
        and added_audit["limit_up_count_bucket"].astype(str).eq("30_50").all()
        and added_audit["market_leader_rank_bucket"].astype(str).eq("rank_4_10").all()
        and added_audit["fd_ratio_bucket"].astype(str).eq("0_1pct_0_3pct").all()
        and ~added_audit["open_times_bucket"].astype(str).eq("1").any()
    )
    added_audit.to_csv(output_root / "selected_added_plan_factor_audit.csv", index=False)

    stress_rows: list[dict[str, Any]] = []
    for rate in monthly_config["execution"]["stress_slippage_rates"]:
        stress_inputs = {
            "OLD_C": ({"C": formal_legs["C"]}, ("C",)),
            "NEW_C": ({"C": rebuilt_plan}, ("C",)),
            "OLD_ACED": (formal_legs, FIXED_PRIORITY),
            "NEW_ACED": (candidate_legs, FIXED_PRIORITY),
        }
        for scenario, (legs, priority) in stress_inputs.items():
            stressed_legs = _stress_plans(legs, float(rate))
            detail = replay_action_date_cash_portfolio(
                stressed_legs,
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
                    "avg_account_return": metric["avg_account_return"],
                    "median_account_return": metric["median_account_return"],
                }
            )
    stress = pd.DataFrame(stress_rows)
    stress.to_csv(output_root / "stress_metrics.csv", index=False)

    removal = pd.DataFrame(
        [
            *_top_profit_removal(
                standalone_detail, "NEW_C", window.start, window.end
            ),
            *_top_profit_removal(
                portfolio_detail, "NEW_ACED", window.start, window.end
            ),
        ]
    )
    removal.to_csv(output_root / "top_profit_removal.csv", index=False)

    manifest = load_json_config(output_root / "frozen_manifest.json")
    current_hashes = {
        leg: sha256_path(formal_root / f"{leg.lower()}_plans.csv")
        for leg in FIXED_PRIORITY
    }
    checks = {
        "selected_plan_signature_reproduced": plan_signature(rebuilt_plan)
        == plan_signature(saved_plan),
        "selected_c_metrics_reproduced": _metrics_close(
            c_metrics, research_summary["selected_c"]
        ),
        "selected_portfolio_metrics_reproduced": _metrics_close(
            portfolio_metrics, research_summary["selected_portfolio"]
        ),
        "formal_plan_hashes_unchanged": current_hashes
        == manifest["formal_plan_sha256"],
        "a_e_d_unchanged": all(
            current_hashes[leg] == manifest["formal_plan_sha256"][leg]
            for leg in ("A", "E", "D")
        ),
        "added_branch_factor_contract": added_factor_ok,
        "no_duplicate_action_date": not rebuilt_plan["action_date"].duplicated().any(),
        "t1_action_date_equals_buy_date": rebuilt_plan["action_date"].astype(str).eq(
            rebuilt_plan["buy_date"].astype(str)
        ).all(),
        "t3_hold_offset": pd.to_numeric(
            rebuilt_plan["hold_offset"], errors="raise"
        ).eq(3).all(),
        "all_stress_results_finite": np.isfinite(
            stress[["equity_multiple", "max_drawdown"]].to_numpy(float)
        ).all(),
        "all_stress_equity_multiples_above_one": stress["equity_multiple"].gt(1.0).all(),
        "new_beats_old_at_each_stress_level": all(
            float(
                stress.loc[
                    stress["scenario"].eq("NEW_C")
                    & stress["slippage_rate_each_side"].eq(rate),
                    "equity_multiple",
                ].iloc[0]
            )
            > float(
                stress.loc[
                    stress["scenario"].eq("OLD_C")
                    & stress["slippage_rate_each_side"].eq(rate),
                    "equity_multiple",
                ].iloc[0]
            )
            and float(
                stress.loc[
                    stress["scenario"].eq("NEW_ACED")
                    & stress["slippage_rate_each_side"].eq(rate),
                    "equity_multiple",
                ].iloc[0]
            )
            > float(
                stress.loc[
                    stress["scenario"].eq("OLD_ACED")
                    & stress["slippage_rate_each_side"].eq(rate),
                    "equity_multiple",
                ].iloc[0]
            )
            for rate in monthly_config["execution"]["stress_slippage_rates"]
        ),
    }
    result = {
        "status": "PASS" if all(bool(value) for value in checks.values()) else "FAIL",
        "selected_variant": selected_id,
        "checks": {key: bool(value) for key, value in checks.items()},
        "rebuilt_c_metrics": c_metrics,
        "rebuilt_portfolio_metrics": portfolio_metrics,
        "added_plan_count": int(len(added_plan)),
        "stress_min_c_equity_multiple": float(
            stress.loc[stress["scenario"].eq("NEW_C"), "equity_multiple"].min()
        ),
        "stress_min_portfolio_equity_multiple": float(
            stress.loc[stress["scenario"].eq("NEW_ACED"), "equity_multiple"].min()
        ),
        "formal_strategy_modified": False,
        "code_committed": False,
    }
    write_json(output_root / "validation_summary.json", result)
    return result


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
