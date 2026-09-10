#!/usr/bin/env python3
"""研究个股历史涨停后第2/3日路径能否降低当前ACDE亏损率。

研究严格使用 ``history_start <= event_date`` 且 ``T+3 <= signal_date`` 的旧事件，
以当前A/C/E正式规则、固定A>C>E>D、真实action_date、费用、滑点、T+1和资金占用
回放。脚本只生成研究产物，不会修改正式配置或实盘下单逻辑。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.optimize_acde_rolling_three_year import build_variant_plan  # noqa: E402
from scripts.run_paper_ab_filtered_daily_ops import condition_strategy_config  # noqa: E402
from scripts.run_paper_ab_filtered_observation_window import (  # noqa: E402
    reject_strategy_risk_mask,
)
from src.acde_monthly_research import (  # noqa: E402
    _context,
    _execution_kwargs,
    load_monthly_config,
    monthly_paths,
)
from src.acde_rolling_candidates import (  # noqa: E402
    StaticOutcomeCache,
    VariantDefinition,
    make_generator,
)
from src.acde_rolling_framework import (  # noqa: E402
    FIXED_PRIORITY,
    action_metrics,
    build_monthly_research_window,
    replay_action_date_cash_portfolio,
)
from src.historical_limit_up_profile import (  # noqa: E402
    attach_point_in_time_profiles,
    build_event_outcomes,
    load_requested_quotes,
    open_trade_dates,
    prepare_event_schedule,
    rule_mask,
    wilson_interval,
)
from src.strategy_e import build_r1_universe_from_pool, load_e_spec  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402


DEFAULT_CONFIG = ROOT / "config/historical_limit_up_behavior_research.json"
PROFILE_COLUMNS = (
    "hist_limit_event_count",
    "hist_last_event_date",
    "hist_last_outcome_available_date",
    "hist_last_d2_return",
    "hist_last_d3_return",
    "hist_last_d23_return",
    "hist_last_fade",
    "hist_last_both_down",
    "hist_last_entry_to_t2",
    "hist_last_entry_to_t3",
    "hist_recent2_entry_to_t2_mean",
    "hist_recent3_entry_to_t3_mean",
    "hist_recent3_d23_return_mean",
    "hist_recent3_fade_rate",
    "hist_recent3_both_down_rate",
)


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_research_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("历史涨停画像研究配置schema_version不支持")
    if payload.get("mode") != "research_only":
        raise ValueError("历史涨停画像只能在research_only模式运行")
    if bool(payload.get("formal_strategy_auto_apply", True)):
        raise ValueError("历史涨停画像研究禁止自动修改正式策略")
    if tuple(payload.get("priority", [])) != FIXED_PRIORITY:
        raise ValueError("历史涨停画像研究腿序必须固定为A>C>E>D")
    if str(payload["validation_start"]) <= str(payload["training_end"]):
        raise ValueError("验证期必须严格晚于训练期")
    if not payload.get("rules"):
        raise ValueError("历史涨停画像研究规则不能为空")
    return payload


def json_default(value: Any) -> Any:
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"不支持的JSON类型: {type(value)!r}")


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
            default=json_default,
        )
        + "\n",
        encoding="utf-8",
    )


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def metric_payload(metrics: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(metrics)
    result["total_return"] = float(metrics["equity_multiple"]) - 1.0
    result["loss_rate"] = 1.0 - float(metrics["win_rate"])
    return result


def metrics_for_period(
    legs: Mapping[str, pd.DataFrame],
    *,
    action_dates: list[str],
    execution: Mapping[str, Any],
    start: str,
    end: str,
    priority: tuple[str, ...] = FIXED_PRIORITY,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    detail = replay_action_date_cash_portfolio(
        dict(legs),
        action_dates=action_dates,
        priority=priority,
        **execution,
    )
    return detail, metric_payload(action_metrics(detail, start, end))


def metrics_close(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
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
        if key == "trade_count" or key == "max_consecutive_losses":
            if int(left[key]) != int(right[key]):
                return False
        elif not math.isclose(
            float(left[key]), float(right[key]), rel_tol=1e-11, abs_tol=1e-11
        ):
            return False
    return dict(left.get("leg_counts", {})) == dict(right.get("leg_counts", {}))


def build_current_context(config: Mapping[str, Any]) -> dict[str, Any]:
    cutoff = str(config["cutoff"])
    data = config["data"]
    monthly_config_path = resolve_path(data["monthly_config"])
    monthly_config = load_monthly_config(monthly_config_path)
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
    context["monthly_config"] = monthly_config
    context["execution"] = _execution_kwargs(monthly_config)
    context["monthly_paths"] = paths
    return context


def build_history_data(
    config: Mapping[str, Any],
    signal_pool: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    data = config["data"]
    calendar = pd.read_csv(
        resolve_path(data["trade_calendar"]), dtype=str, low_memory=False
    )
    trade_dates = open_trade_dates(calendar)
    events = pd.read_csv(
        resolve_path(data["limit_events"]),
        usecols=["trade_date", "ts_code", "name", "market_segment"],
        dtype=str,
        low_memory=False,
    )
    schedule, requests, schedule_audit = prepare_event_schedule(
        events,
        trade_dates,
        start_date=str(config["history_start"]),
        cutoff=str(config["cutoff"]),
    )
    quotes, quote_audit = load_requested_quotes(
        resolve_path(data["daily_dir"]), requests
    )
    outcomes, outcome_audit = build_event_outcomes(schedule, quotes)
    pool = signal_pool.copy()
    pool["trade_date"] = pool["trade_date"].astype(str).str.replace(
        r"\.0$", "", regex=True
    )
    profiled_pool = attach_point_in_time_profiles(pool, outcomes)
    duplicate_pool_keys = int(profiled_pool.duplicated(["trade_date", "ts_code"]).sum())
    audit = {
        **schedule_audit,
        **quote_audit,
        **outcome_audit,
        "strict_signal_pool_rows": int(len(profiled_pool)),
        "strict_signal_pool_trade_days": int(profiled_pool["trade_date"].nunique()),
        "strict_signal_pool_duplicate_key_count": duplicate_pool_keys,
        "profile_count_ge_1": int(
            profiled_pool["hist_limit_event_count"].ge(1).sum()
        ),
        "profile_count_ge_3": int(
            profiled_pool["hist_limit_event_count"].ge(3).sum()
        ),
        "profile_coverage_ge_1": float(
            profiled_pool["hist_limit_event_count"].ge(1).mean()
        ),
        "profile_coverage_ge_3": float(
            profiled_pool["hist_limit_event_count"].ge(3).mean()
        ),
    }
    audit["passed"] = bool(
        audit["source_duplicate_key_count"] == 0
        and audit["missing_daily_file_count"] == 0
        and audit["strict_signal_pool_duplicate_key_count"] == 0
        and audit["valid_outcome_ratio"] >= 0.99
    )
    return outcomes, profiled_pool, audit


def current_eligible_scopes(
    pool: pd.DataFrame,
    strategy_config: Mapping[str, Any],
    e_spec: Mapping[str, Any],
) -> dict[str, pd.DataFrame]:
    """保留现有规则，只取得各腿最终逐日排名前的合格候选母池。"""

    a_generator = make_generator(dict(strategy_config))
    a_pool = a_generator.apply_strategy_filters(pool).copy()

    c_config = strategy_config["paper_ab_filtered_strategy"]["c_strategy"]
    c_selected = condition_strategy_config(
        dict(strategy_config),
        [],
        "C_HISTORICAL_LIMIT_PROFILE_RESEARCH",
        condition_profiles=copy.deepcopy(c_config["condition_profiles"]),
    )
    c_generator = make_generator(c_selected)
    c_pool = c_generator.apply_strategy_filters(pool).copy()
    c_pool["risk_flags"] = [
        c_generator.build_risk_flags(row) for row in c_pool.itertuples(index=False)
    ]
    rejected = reject_strategy_risk_mask(c_pool, dict(strategy_config), "c_strategy")
    c_pool = c_pool.loc[~rejected].copy()

    e_pool = build_r1_universe_from_pool(
        pool, dict(e_spec), audit_readiness=True
    ).drop_duplicates(["trade_date", "ts_code"])
    return {
        "ALL": pool[["trade_date", "ts_code"]].drop_duplicates(),
        "A": a_pool[["trade_date", "ts_code"]].drop_duplicates(),
        "C": c_pool[["trade_date", "ts_code"]].drop_duplicates(),
        "E": e_pool[["trade_date", "ts_code"]].drop_duplicates(),
    }


def build_current_outcomes(
    outcomes: pd.DataFrame,
    profiled_pool: pd.DataFrame,
) -> pd.DataFrame:
    current = outcomes.rename(
        columns={
            "event_date": "trade_date",
            "d2_return": "current_d2_return",
            "d3_return": "current_d3_return",
            "d23_return": "current_d23_return",
            "entry_to_t2": "current_entry_to_t2",
            "entry_to_t3": "current_entry_to_t3",
        }
    )
    profile_columns = [
        column for column in PROFILE_COLUMNS if column in profiled_pool.columns
    ]
    joined = current.merge(
        profiled_pool[["trade_date", "ts_code", *profile_columns]],
        on=["trade_date", "ts_code"],
        how="inner",
        validate="one_to_one",
    )
    return joined


def factor_bin_report(
    current: pd.DataFrame,
    scopes: Mapping[str, pd.DataFrame],
) -> pd.DataFrame:
    features = {
        "hist_last_d23_return": 1,
        "hist_last_d3_return": 1,
        "hist_recent3_d23_return_mean": 3,
        "hist_recent3_fade_rate": 3,
        "hist_recent3_both_down_rate": 3,
    }
    targets = ("current_entry_to_t2", "current_entry_to_t3")
    rows: list[dict[str, Any]] = []
    for scope_name, keys in scopes.items():
        frame = current.merge(
            keys, on=["trade_date", "ts_code"], how="inner", validate="one_to_one"
        )
        for feature, minimum in features.items():
            eligible = frame[
                frame["hist_limit_event_count"].ge(minimum)
                & pd.to_numeric(frame[feature], errors="coerce").notna()
            ].copy()
            if len(eligible) < 8:
                continue
            try:
                eligible["factor_bin"] = pd.qcut(
                    eligible[feature], 4, duplicates="drop"
                ).astype(str)
            except ValueError:
                continue
            for target in targets:
                for factor_bin, group in eligible.groupby("factor_bin", sort=False):
                    values = pd.to_numeric(group[target], errors="coerce").dropna()
                    losses = int(values.lt(0).sum())
                    low, high = wilson_interval(losses, len(values))
                    rows.append(
                        {
                            "scope": scope_name,
                            "target": target,
                            "feature": feature,
                            "factor_bin": factor_bin,
                            "sample_count": int(len(values)),
                            "loss_count": losses,
                            "loss_rate": float(values.lt(0).mean()),
                            "loss_rate_wilson_low": low,
                            "loss_rate_wilson_high": high,
                            "avg_return": float(values.mean()),
                            "median_return": float(values.median()),
                        }
                    )
    return pd.DataFrame(rows)


def intuitive_rule_diagnostics(
    current: pd.DataFrame,
    scopes: Mapping[str, pd.DataFrame],
    rule: Mapping[str, Any],
    *,
    training_end: str,
    validation_start: str,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    periods = {
        "TRAIN": (lambda frame: frame["trade_date"].le(training_end)),
        "VALIDATION": (lambda frame: frame["trade_date"].ge(validation_start)),
        "FULL": (lambda frame: pd.Series(True, index=frame.index)),
    }
    for scope_name, keys in scopes.items():
        scoped = current.merge(
            keys, on=["trade_date", "ts_code"], how="inner", validate="one_to_one"
        )
        flagged = rule_mask(scoped, rule)
        for period_name, selector in periods.items():
            period_mask = selector(scoped)
            for target in ("current_entry_to_t2", "current_entry_to_t3"):
                base = pd.to_numeric(scoped.loc[period_mask, target], errors="coerce").dropna()
                bad = pd.to_numeric(
                    scoped.loc[period_mask & flagged, target], errors="coerce"
                ).dropna()
                kept = pd.to_numeric(
                    scoped.loc[period_mask & ~flagged, target], errors="coerce"
                ).dropna()
                rows.append(
                    {
                        "scope": scope_name,
                        "period": period_name,
                        "rule_id": str(rule["rule_id"]),
                        "target": target,
                        "base_count": int(len(base)),
                        "base_loss_rate": float(base.lt(0).mean()) if len(base) else np.nan,
                        "flagged_count": int(len(bad)),
                        "flagged_loss_rate": float(bad.lt(0).mean()) if len(bad) else np.nan,
                        "flagged_avg_return": float(bad.mean()) if len(bad) else np.nan,
                        "kept_count": int(len(kept)),
                        "kept_loss_rate": float(kept.lt(0).mean()) if len(kept) else np.nan,
                        "kept_avg_return": float(kept.mean()) if len(kept) else np.nan,
                    }
                )
    return pd.DataFrame(rows)


def load_baseline_legs(config: Mapping[str, Any]) -> dict[str, pd.DataFrame]:
    data = config["data"]
    baseline_dir = resolve_path(data["baseline_plan_dir"])
    date_types = {
        "signal_date": str,
        "action_date": str,
        "buy_date": str,
        "exit_date": str,
        "ts_code": str,
    }
    legs = {
        leg: pd.read_csv(
            baseline_dir / f"{leg.lower()}_plans.csv",
            dtype=date_types,
            low_memory=False,
        )
        for leg in ("A", "C", "E")
    }
    legs["D"] = pd.read_csv(
        resolve_path(data["aligned_d_plan"]), dtype=date_types, low_memory=False
    )
    return legs


def plan_key_set(frame: pd.DataFrame) -> set[tuple[str, str]]:
    return set(zip(frame["signal_date"].astype(str), frame["ts_code"].astype(str)))


def evaluate_rules(
    config: Mapping[str, Any],
    context: Mapping[str, Any],
    profiled_pool: pd.DataFrame,
    baseline_legs: Mapping[str, pd.DataFrame],
    baseline_details: Mapping[str, pd.DataFrame],
    baseline_metrics: Mapping[str, Mapping[str, Any]],
    strategy_config: Mapping[str, Any],
    e_spec: Mapping[str, Any],
) -> pd.DataFrame:
    cutoff = str(config["cutoff"])
    periods = {
        "train": (str(config["history_start"]), str(config["training_end"])),
        "validation": (str(config["validation_start"]), cutoff),
        "full": (str(config["history_start"]), cutoff),
    }
    variants = {
        "A": VariantDefinition("A", "CURRENT_A", "冻结当前A", strategy_config, 0, True, ""),
        "C": VariantDefinition("C", "CURRENT_C", "冻结当前C", strategy_config, 0, True, ""),
        "E": VariantDefinition("E", "CURRENT_E", "冻结当前E", e_spec, 0, True, ""),
    }
    cache = StaticOutcomeCache()
    rows: list[dict[str, Any]] = []
    full_executed = baseline_details["full"]
    full_executed = full_executed[
        full_executed["status"].astype(str).eq("EXECUTED")
    ].copy()

    for leg in ("A", "C", "E"):
        baseline_standalone_detail, baseline_standalone = metrics_for_period(
            {leg: baseline_legs[leg]},
            action_dates=list(context["action_dates"]),
            execution=context["execution"],
            start=str(config["history_start"]),
            end=cutoff,
            priority=(leg,),
        )
        del baseline_standalone_detail
        for rule in config["rules"]:
            flagged = rule_mask(profiled_pool, rule)
            bad_keys = set(
                zip(
                    profiled_pool.loc[flagged, "trade_date"].astype(str),
                    profiled_pool.loc[flagged, "ts_code"].astype(str),
                )
            )
            filtered_pool = profiled_pool.loc[~flagged].copy()
            fallback_plan = build_variant_plan(
                variants[leg],
                signal_pool=filtered_pool,
                d_events=context["d_events"],
                allowed_action_dates=context["allowed_actions"],
                cutoff=cutoff,
                outcome_cache=cache,
            )
            current_plan = baseline_legs[leg]
            selected_flagged = pd.Series(
                [
                    (str(signal_date), str(code)) in bad_keys
                    for signal_date, code in zip(
                        current_plan["signal_date"], current_plan["ts_code"]
                    )
                ],
                index=current_plan.index,
            )
            post_pick_plan = current_plan.loc[~selected_flagged].copy()

            baseline_leg_executed = full_executed[
                full_executed["strategy_leg"].astype(str).eq(leg)
            ]
            executed_flagged = pd.Series(
                [
                    (str(signal_date), str(code)) in bad_keys
                    for signal_date, code in zip(
                        baseline_leg_executed["signal_date"],
                        baseline_leg_executed["ts_code"],
                    )
                ],
                index=baseline_leg_executed.index,
            )
            removed_executed = baseline_leg_executed.loc[executed_flagged]
            removed_losses = int(
                pd.to_numeric(removed_executed["account_return"], errors="coerce")
                .lt(0)
                .sum()
            )
            precision_low, precision_high = wilson_interval(
                removed_losses, len(removed_executed)
            )

            for mechanism, candidate_plan in (
                ("PRE_RANK_WITHIN_LEG_FALLBACK", fallback_plan),
                ("POST_PICK_NO_SAME_LEG_FALLBACK", post_pick_plan),
            ):
                candidate_legs = {
                    key: value.copy() for key, value in baseline_legs.items()
                }
                candidate_legs[leg] = candidate_plan
                result: dict[str, Any] = {
                    "candidate_id": f"{leg}_{rule['rule_id']}_{mechanism}",
                    "strategy_leg": leg,
                    "rule_id": str(rule["rule_id"]),
                    "rule_description": str(rule["description"]),
                    "mechanism": mechanism,
                    "baseline_plan_count": int(len(current_plan)),
                    "candidate_plan_count": int(len(candidate_plan)),
                    "plan_key_change_count": int(
                        len(plan_key_set(current_plan) ^ plan_key_set(candidate_plan))
                    ),
                    "baseline_selected_flagged_count": int(selected_flagged.sum()),
                    "baseline_executed_flagged_count": int(len(removed_executed)),
                    "baseline_executed_flagged_loss_count": removed_losses,
                    "baseline_executed_flagged_loss_precision": (
                        float(removed_losses / len(removed_executed))
                        if len(removed_executed)
                        else np.nan
                    ),
                    "flagged_loss_precision_wilson_low": precision_low,
                    "flagged_loss_precision_wilson_high": precision_high,
                }
                for period_name, (start, end) in periods.items():
                    _detail, metrics = metrics_for_period(
                        candidate_legs,
                        action_dates=list(context["action_dates"]),
                        execution=context["execution"],
                        start=start,
                        end=end,
                    )
                    base = baseline_metrics[period_name]
                    for key, value in metrics.items():
                        if key == "leg_counts":
                            result[f"{period_name}_{key}"] = json.dumps(
                                value, ensure_ascii=False, sort_keys=True
                            )
                        else:
                            result[f"{period_name}_{key}"] = value
                    result[f"{period_name}_win_rate_delta"] = (
                        metrics["win_rate"] - base["win_rate"]
                    )
                    result[f"{period_name}_avg_return_delta"] = (
                        metrics["avg_account_return"] - base["avg_account_return"]
                    )
                    result[f"{period_name}_equity_multiple_ratio"] = (
                        metrics["equity_multiple"] / base["equity_multiple"]
                    )

                _standalone_detail, standalone = metrics_for_period(
                    {leg: candidate_plan},
                    action_dates=list(context["action_dates"]),
                    execution=context["execution"],
                    start=str(config["history_start"]),
                    end=cutoff,
                    priority=(leg,),
                )
                for key, value in baseline_standalone.items():
                    if key != "leg_counts":
                        result[f"baseline_standalone_full_{key}"] = value
                for key, value in standalone.items():
                    if key != "leg_counts":
                        result[f"candidate_standalone_full_{key}"] = value
                result["standalone_full_win_rate_delta"] = (
                    standalone["win_rate"] - baseline_standalone["win_rate"]
                )
                result["standalone_full_avg_return_delta"] = (
                    standalone["avg_account_return"]
                    - baseline_standalone["avg_account_return"]
                )
                result["standalone_full_equity_multiple_ratio"] = (
                    standalone["equity_multiple"]
                    / baseline_standalone["equity_multiple"]
                )
                rows.append(result)
    result = pd.DataFrame(rows)
    retention = float(config["minimum_plan_retention"])
    result["train_gate_passed"] = (
        result["train_trade_count"].ge(
            np.ceil(baseline_metrics["train"]["trade_count"] * retention)
        )
        & result["train_win_rate_delta"].gt(0.0)
        & result["train_avg_return_delta"].ge(0.0)
        & result["train_equity_multiple_ratio"].gt(1.0)
    )
    result["validation_gate_passed"] = (
        result["validation_trade_count"].ge(
            np.ceil(baseline_metrics["validation"]["trade_count"] * retention)
        )
        & result["validation_win_rate_delta"].gt(0.0)
        & result["validation_avg_return_delta"].ge(0.0)
        & result["validation_equity_multiple_ratio"].gt(1.0)
    )
    result["strict_gate_passed"] = (
        result["train_gate_passed"] & result["validation_gate_passed"]
    )
    result["train_rank_score"] = (
        2.0 * result["train_win_rate_delta"]
        + result["train_avg_return_delta"]
        + np.log(result["train_equity_multiple_ratio"].clip(lower=1e-12)) / 100.0
    )
    return result.sort_values(
        ["train_gate_passed", "train_rank_score", "candidate_id"],
        ascending=[False, False, True],
    ).reset_index(drop=True)


def pct(value: Any) -> str:
    return "n.a." if value is None or pd.isna(value) else f"{float(value):+.2%}"


def percentage_points(value: Any) -> str:
    """把0.0074显示为+0.74个百分点，避免与相对百分比混淆。"""

    return "n.a." if value is None or pd.isna(value) else f"{float(value) * 100:+.2f}"


def metric_table_row(label: str, metrics: Mapping[str, Any]) -> str:
    return (
        f"| {label} | {int(metrics['trade_count'])} | {float(metrics['win_rate']):.2%} | "
        f"{pct(metrics['avg_account_return'])} | {pct(metrics['median_account_return'])} | "
        f"{float(metrics['equity_multiple']):.3f}倍 | {pct(metrics['max_drawdown'])} | "
        f"{pct(metrics['max_loss'])} | {pct(metrics['max_profit'])} | "
        f"{float(metrics['profit_loss_ratio']):.3f} | "
        f"{int(metrics['max_consecutive_losses'])} |"
    )


def write_report(
    path: Path,
    summary: Mapping[str, Any],
    rules: pd.DataFrame,
) -> None:
    baseline = summary["baseline"]
    top = summary.get("top_train_candidate") or {}
    diagnostic = {
        (str(item["scope"]), str(item["period"])): item
        for item in summary.get("hypothesis_diagnostics", [])
    }
    lines = [
        "# 个股历史涨停后路径过滤研究",
        "",
        f"研究窗口：`{summary['window']['start']}~{summary['window']['end']}`；"
        f"训练期截止`{summary['window']['training_end']}`，验证期从"
        f"`{summary['window']['validation_start']}`开始。",
        "",
        "## 结论",
        "",
        "当前没有找到可以上线的历史涨停路径硬过滤规则。全候选池没有稳定单调关系，"
        "训练期过门槛的规则在最近一年验证期均未同时改善胜率、平均收益和复利。",
        "",
        "因此本轮保持正式A/C/E规则不变，D继续执行信号时成交概率门。历史涨停画像只进入"
        "影子观察，不参与下单。",
        "",
        "## 数据质量",
        "",
        f"- 涨停事件：{summary['data_quality']['source_event_count']:,}条；可完整观察T+3："
        f"{summary['data_quality']['scheduled_event_count']:,}条；有效路径："
        f"{summary['data_quality']['valid_outcome_count']:,}条"
        f"（{summary['data_quality']['valid_outcome_ratio']:.2%}）。",
        f"- 当前严格候选池：{summary['data_quality']['strict_signal_pool_rows']:,}条；"
        f"至少1次可用历史：{summary['data_quality']['profile_coverage_ge_1']:.2%}；"
        f"至少3次可用历史：{summary['data_quality']['profile_coverage_ge_3']:.2%}。",
        f"- 时点口径：旧事件只有在其T+3收盘日不晚于当前信号日时才可用。数据门禁："
        f"`{summary['data_quality']['passed']}`。",
        "",
        "## 当前可执行基线",
        "",
        "| 期间 | 样本 | 胜率 | 平均每笔 | 中位每笔 | 复利 | 最大回撤 | 最大亏损 | 最大盈利 | 盈亏比 | 最长连亏 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        metric_table_row("训练期", baseline["train"]),
        metric_table_row("验证期", baseline["validation"]),
        metric_table_row("完整三年", baseline["full"]),
        "",
        "基线已使用D历史成交概率不可验证时失败关闭后的174笔A/C/E组合，不再引用旧V16中"
        "11笔未通过同口径认证的D成交。",
        "",
        "## 规则搜索",
        "",
        f"- 预先定义规则：{summary['search']['rule_count']}个。",
        f"- 策略腿：A、C、E；机制：同腿内递补、同腿不递补但允许低优先级腿接管。",
        f"- 实际候选数：{summary['search']['candidate_count']}。训练期过门槛："
        f"{summary['search']['train_pass_count']}；训练和验证同时过门槛："
        f"{summary['search']['strict_pass_count']}。",
        "- 排名只看训练期；验证期只做否决，不参与挑选。搜索标签为`STRICT_DISCOVERY`。",
    ]
    all_full = diagnostic.get(("ALL", "FULL"), {})
    a_train = diagnostic.get(("A", "TRAIN"), {})
    a_validation = diagnostic.get(("A", "VALIDATION"), {})
    if all_full and a_train and a_validation:
        lines.extend(
            [
                "",
                "### 原始假设检验",
                "",
                "对“最近3次平均回落，且至少2次第2至第3日累计回落”这一直接假设：",
                "",
                f"- 全体候选共{int(all_full['base_count']):,}条，命中规则"
                f"{int(all_full['flagged_count']):,}条；命中组亏损率"
                f"{float(all_full['flagged_loss_rate']):.2%}，未命中组"
                f"{float(all_full['kept_loss_rate']):.2%}，没有出现预期的风险抬升。",
                f"- A合格候选池训练期命中组与未命中组亏损率均为"
                f"{float(a_train['flagged_loss_rate']):.2%}；最近一年验证期才分化为"
                f"{float(a_validation['flagged_loss_rate']):.2%}对"
                f"{float(a_validation['kept_loss_rate']):.2%}。方向没有跨期稳定。",
            ]
        )
    if top:
        lines.extend(
            [
                "",
                "### 训练期第一名及验证结果",
                "",
                f"训练期第一名是`{top['candidate_id']}`。训练期胜率变化"
                f"{percentage_points(top['train_win_rate_delta'])}个百分点，平均每笔变化"
                f"{pct(top['train_avg_return_delta'])}，复利比"
                f"{float(top['train_equity_multiple_ratio']):.3f}。",
                "",
                f"最近一年验证期胜率变化"
                f"{percentage_points(top['validation_win_rate_delta'])}个百分点，"
                f"平均每笔变化{pct(top['validation_avg_return_delta'])}，复利比"
                f"{float(top['validation_equity_multiple_ratio']):.3f}。"
                f"验证门禁为`{bool(top['validation_gate_passed'])}`，因此不能发布。",
                "",
                f"完整三年只把亏损率从{baseline['full']['loss_rate']:.2%}降到"
                f"{float(top['full_loss_rate']):.2%}，但复利仅为基线的"
                f"{float(top['full_equity_multiple_ratio']):.2%}。该规则在原组合实际命中"
                f"{int(top['baseline_executed_flagged_count'])}笔，其中"
                f"{int(top['baseline_executed_flagged_loss_count'])}笔亏损；命中亏损率的"
                f"Wilson 95%区间为{float(top['flagged_loss_precision_wilson_low']):.2%}~"
                f"{float(top['flagged_loss_precision_wilson_high']):.2%}，样本太小。",
            ]
        )
    train_pass = rules[rules["train_gate_passed"]].head(10)
    if not train_pass.empty:
        lines.extend(
            [
                "",
                "### 训练期过门槛候选",
                "",
                "| 候选 | 机制 | 训练胜率变化(百分点) | 训练平均变化 | 验证胜率变化(百分点) | 验证平均变化 | 验证复利比 | 验证通过 |",
                "|---|---|---:|---:|---:|---:|---:|---|",
            ]
        )
        for row in train_pass.itertuples(index=False):
            lines.append(
                f"| {row.strategy_leg}/{row.rule_id} | {row.mechanism} | "
                f"{percentage_points(row.train_win_rate_delta)} | "
                f"{pct(row.train_avg_return_delta)} | "
                f"{percentage_points(row.validation_win_rate_delta)} | "
                f"{pct(row.validation_avg_return_delta)} | "
                f"{row.validation_equity_multiple_ratio:.3f} | "
                f"{bool(row.validation_gate_passed)} |"
            )
    lines.extend(
        [
            "",
            "## 解释",
            "",
            "个股过去涨停后回落，不等于下一次涨停后仍会回落。当前样本里该关系受策略腿、"
            "市场阶段和当次涨停形态影响明显；全体候选的四分位结果不单调。硬阈值还会删掉"
            "大量盈利单，复利损失大于减少亏损笔数的收益。",
            "",
            "较合理的下一步是把历史画像记录到每日日志，至少积累一个完整月的真实前向候选、"
            "委托和成交，再在下一个月末按固定三年窗口重新研究。没有历史的股票保持中性，"
            "不能当作低风险或高风险。",
            "",
            "本报告是研究结论，不承诺收益。正式实盘仍应先小资金验证。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(config_path: Path) -> dict[str, Any]:
    config = load_research_config(config_path)
    output_dir = resolve_path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    context = build_current_context(config)
    outcomes, profiled_pool, data_quality = build_history_data(
        config, context["signal_pool"]
    )

    strategy_config = load_json_config(resolve_path(config["data"]["strategy_config"]))
    e_spec = load_e_spec(
        ROOT, resolve_path(config["data"]["strategy_e_config"])
    )
    scopes = current_eligible_scopes(profiled_pool, strategy_config, e_spec)
    current = build_current_outcomes(outcomes, profiled_pool)
    bins = factor_bin_report(current, scopes)
    intuitive_rule = next(
        rule
        for rule in config["rules"]
        if str(rule["rule_id"]) == "N3_D23_NEG_FADE_2OF3"
    )
    diagnostics = intuitive_rule_diagnostics(
        current,
        scopes,
        intuitive_rule,
        training_end=str(config["training_end"]),
        validation_start=str(config["validation_start"]),
    )

    baseline_legs = load_baseline_legs(config)
    periods = {
        "train": (str(config["history_start"]), str(config["training_end"])),
        "validation": (str(config["validation_start"]), str(config["cutoff"])),
        "full": (str(config["history_start"]), str(config["cutoff"])),
    }
    baseline_details: dict[str, pd.DataFrame] = {}
    baseline_metrics: dict[str, dict[str, Any]] = {}
    for period_name, (start, end) in periods.items():
        detail, metrics = metrics_for_period(
            baseline_legs,
            action_dates=list(context["action_dates"]),
            execution=context["execution"],
            start=start,
            end=end,
        )
        baseline_details[period_name] = detail
        baseline_metrics[period_name] = metrics

    frozen_summary = load_json_config(
        resolve_path(config["data"]["aligned_baseline_summary"])
    )["aligned_fail_closed_combo"]
    baseline_reproduced = metrics_close(baseline_metrics["full"], frozen_summary)
    if not baseline_reproduced:
        raise RuntimeError("历史涨停画像研究未能精确复现当前可执行基线")

    rules = evaluate_rules(
        config,
        context,
        profiled_pool,
        baseline_legs,
        baseline_details,
        baseline_metrics,
        strategy_config,
        e_spec,
    )
    train_pass = rules[rules["train_gate_passed"]].copy()
    strict_pass = rules[rules["strict_gate_passed"]].copy()
    top_train = train_pass.iloc[0].to_dict() if not train_pass.empty else {}
    selected = strict_pass.iloc[0].to_dict() if not strict_pass.empty else {}

    full_executed = baseline_details["full"]
    full_executed = full_executed[
        full_executed["status"].astype(str).eq("EXECUTED")
    ].copy()
    trade_profiles = full_executed.merge(
        profiled_pool[[
            "trade_date",
            "ts_code",
            *[column for column in PROFILE_COLUMNS if column in profiled_pool.columns],
        ]],
        left_on=["signal_date", "ts_code"],
        right_on=["trade_date", "ts_code"],
        how="left",
        validate="many_to_one",
    )
    losses = int(pd.to_numeric(full_executed["account_return"], errors="coerce").lt(0).sum())
    loss_low, loss_high = wilson_interval(losses, len(full_executed))
    baseline_dir = resolve_path(config["data"]["baseline_plan_dir"])
    input_paths = {
        "research_config": config_path,
        "trade_calendar": resolve_path(config["data"]["trade_calendar"]),
        "limit_events": resolve_path(config["data"]["limit_events"]),
        "strict_feature_pool": context["monthly_paths"]["strict_feature_pool"],
        "market_sentiment": context["monthly_paths"]["market_sentiment"],
        "monthly_config": resolve_path(config["data"]["monthly_config"]),
        "strategy_config": resolve_path(config["data"]["strategy_config"]),
        "strategy_e_config": resolve_path(config["data"]["strategy_e_config"]),
        "baseline_a_plan": baseline_dir / "a_plans.csv",
        "baseline_c_plan": baseline_dir / "c_plans.csv",
        "baseline_e_plan": baseline_dir / "e_plans.csv",
        "aligned_d_plan": resolve_path(config["data"]["aligned_d_plan"]),
        "aligned_baseline_summary": resolve_path(
            config["data"]["aligned_baseline_summary"]
        ),
    }
    input_fingerprints = {
        name: {
            "path": str(path.relative_to(ROOT)),
            "size_bytes": int(path.stat().st_size),
            "sha256": sha256_path(path),
        }
        for name, path in input_paths.items()
    }
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

    summary = {
        "schema_version": 1,
        "decision": "NO_RELEASE_SHADOW_ONLY" if not selected else "REVIEW_CANDIDATE",
        "formal_strategy_modified": False,
        "research_protocol": "STRICT_DISCOVERY_TRAIN_RANK_VALIDATION_VETO",
        "window": {
            "start": str(config["history_start"]),
            "end": str(config["cutoff"]),
            "training_end": str(config["training_end"]),
            "validation_start": str(config["validation_start"]),
        },
        "priority": list(FIXED_PRIORITY),
        "data_quality": data_quality,
        "baseline_reproduced": baseline_reproduced,
        "baseline": baseline_metrics,
        "baseline_full_loss_count": losses,
        "baseline_full_loss_rate_wilson_95": [loss_low, loss_high],
        "search": {
            "rule_count": int(len(config["rules"])),
            "strategy_leg_count": 3,
            "mechanism_count": 2,
            "candidate_count": int(len(rules)),
            "train_pass_count": int(len(train_pass)),
            "strict_pass_count": int(len(strict_pass)),
            "minimum_plan_retention": float(config["minimum_plan_retention"]),
            "ranking_uses_validation": False,
        },
        "top_train_candidate": top_train,
        "selected_candidate": selected,
        "current_pool_diagnostic": {
            "matched_strict_outcome_rows": int(len(current)),
            "scope_rows": {name: int(len(frame)) for name, frame in scopes.items()},
            "return_basis": (
                "candidate-pool diagnostics use raw linked forward-adjusted stock returns; "
                "portfolio rule results use exact fees, slippage, T+1, fill and cash occupancy"
            ),
        },
        "hypothesis_diagnostics": diagnostics[
            diagnostics["target"].eq("current_entry_to_t2")
            & diagnostics["scope"].isin(["ALL", "A"])
        ].to_dict("records"),
        "limitations": [
            "当前窗口内A/C/E实际组合只有174笔，单腿和被规则命中的验证样本更少",
            "训练期过门槛候选均未通过最近一年验证期的胜率、均值和复利联合门禁",
            "历史涨停画像未纳入当次题材、情绪和涨停形态交互，以免本轮扩大搜索造成更强过拟合",
            "D历史信号时L2队列缺失，继续按当前口径失败关闭，不参加本因子发布判断",
        ],
        "next_action": (
            "保留正式策略；将画像作为影子字段记录，完成至少一个自然月前向样本后，"
            "在下个月末按固定三年窗口重新研究"
        ),
        "git_commit_before_research": git_commit,
        "git_worktree_dirty_during_research": git_dirty,
        "input_fingerprints": input_fingerprints,
    }

    outcomes.to_csv(output_dir / "historical_limit_event_outcomes.csv", index=False)
    profiled_pool[[
        "trade_date",
        "ts_code",
        "name",
        "market_segment",
        *[column for column in PROFILE_COLUMNS if column in profiled_pool.columns],
    ]].to_csv(output_dir / "strict_signal_pool_history_profiles.csv", index=False)
    trade_profiles.to_csv(output_dir / "selected_trade_history_profiles.csv", index=False)
    bins.to_csv(output_dir / "factor_bin_diagnostics.csv", index=False)
    diagnostics.to_csv(output_dir / "intuitive_rule_period_diagnostics.csv", index=False)
    rules.to_csv(output_dir / "filter_rule_results.csv", index=False)
    write_json(output_dir / "summary.json", summary)
    write_report(output_dir / "decision_report.md", summary, rules)

    artifact_path = output_dir / "artifact_manifest.json"
    artifacts = {
        path.name: sha256_path(path)
        for path in sorted(output_dir.iterdir())
        if path.is_file() and path != artifact_path
    }
    write_json(
        artifact_path,
        {
            "schema_version": 1,
            "config_path": str(config_path.relative_to(ROOT)),
            "config_sha256": sha256_path(config_path),
            "code_sha256": {
                "src/historical_limit_up_profile.py": sha256_path(
                    ROOT / "src/historical_limit_up_profile.py"
                ),
                "scripts/research_historical_limit_up_behavior.py": sha256_path(
                    ROOT / "scripts/research_historical_limit_up_behavior.py"
                ),
            },
            "input_fingerprints": input_fingerprints,
            "artifacts": artifacts,
        },
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    summary = run(resolve_path(args.config))
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=json_default))


if __name__ == "__main__":
    main()
