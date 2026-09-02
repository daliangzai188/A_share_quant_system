#!/usr/bin/env python3
"""隔离研究C样本与复利扩展；只写研究报告，绝不修改正式策略。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.optimize_acde_rolling_three_year import build_variant_plan
from src.acde_monthly_research import (
    _context,
    _execution_kwargs,
    load_monthly_config,
    monthly_paths,
)
from src.acde_rolling_candidates import (
    StaticOutcomeCache,
    VariantDefinition,
    c_variants,
    plan_signature,
)
from src.acde_rolling_framework import (
    FIXED_PRIORITY,
    action_metrics,
    build_monthly_research_window,
    replay_action_date_cash_portfolio,
)
from src.utils.config import load_json_config


DEFAULT_SPEC = ROOT / "config/strategy_c_sample_compound_research.json"


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


def _profile_by_id(profiles: Iterable[Mapping[str, Any]], profile_id: str) -> dict[str, Any]:
    for profile in profiles:
        if str(profile.get("profile_id")) == profile_id:
            return copy.deepcopy(dict(profile))
    raise KeyError(f"找不到C源分支：{profile_id}")


def _replace_condition(profile: dict[str, Any], column: str, value: str) -> None:
    for condition in profile["conditions"]:
        if str(condition.get("column")) == column:
            condition["value"] = value
            return
    raise KeyError(f"C源分支缺少字段：{column}")


def _with_allowed_values(
    profiles: Iterable[Mapping[str, Any]],
    *,
    guard: Mapping[str, Any],
    id_prefix: str,
) -> list[dict[str, Any]]:
    """把OR允许值展开为多个AND分支，避免把未来信息或模糊集合塞进条件引擎。"""

    result: list[dict[str, Any]] = []
    for source in profiles:
        for value in guard["values"]:
            profile = copy.deepcopy(dict(source))
            profile["profile_id"] = (
                f"{id_prefix}_{source['profile_id']}_{str(value).upper()}"
            )
            profile["priority"] = len(result) + 1
            profile["conditions"].append(
                {
                    "column": str(guard["column"]),
                    "operator": "==",
                    "value": value,
                }
            )
            result.append(profile)
    return result


def build_candidate_config(
    base_config: Mapping[str, Any],
    *,
    expansion: Mapping[str, Any] | None,
    guard: Mapping[str, Any],
) -> dict[str, Any]:
    config = copy.deepcopy(dict(base_config))
    c_strategy = config["paper_ab_filtered_strategy"]["c_strategy"]
    original_profiles = copy.deepcopy(c_strategy["condition_profiles"])
    scope = str(guard["scope"])

    if scope == "global":
        profiles = _with_allowed_values(
            original_profiles,
            guard=guard,
            id_prefix=str(guard["id"]),
        )
    else:
        profiles = original_profiles

    if expansion is not None:
        added = _profile_by_id(
            original_profiles,
            str(expansion["source_profile_id"]),
        )
        _replace_condition(
            added,
            str(expansion["column"]),
            str(expansion["value"]),
        )
        added["profile_id"] = f"C_EXPAND_{expansion['id']}"
        if scope in {"global", "extension_only"}:
            added_profiles = _with_allowed_values(
                [added],
                guard=guard,
                id_prefix=str(guard["id"]),
            )
        else:
            added_profiles = [added]
        for added_profile in added_profiles:
            added_profile["priority"] = len(profiles) + 1
            profiles.append(added_profile)

    c_strategy["condition_profiles"] = profiles
    return config


def declared_variants(
    base_config: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[VariantDefinition]:
    variants = [
        VariantDefinition(
            "C",
            "C_CURRENT",
            "当前正式C基线",
            copy.deepcopy(dict(base_config)),
            0,
            True,
            "仅用于基线复现",
        )
    ]
    guards = list(spec["guards"])
    expansions = list(spec["expansion_axes"])
    for guard in guards:
        if str(guard["scope"]) == "global":
            variants.append(
                VariantDefinition(
                    "C",
                    f"C_{guard['id']}",
                    str(guard["description"]),
                    build_candidate_config(
                        base_config, expansion=None, guard=guard
                    ),
                    1,
                    True,
                    "冻结的C环境门禁诊断",
                )
            )
        for expansion in expansions:
            variants.append(
                VariantDefinition(
                    "C",
                    f"C_{expansion['id']}__{guard['id']}",
                    f"{expansion['description']}；{guard['description']}",
                    build_candidate_config(
                        base_config,
                        expansion=expansion,
                        guard=guard,
                    ),
                    1 if str(guard["scope"]) == "none" else 2,
                    True,
                    "只变更C内部相邻因子与冻结环境门禁",
                )
            )
    return variants


def _period_metrics(detail: pd.DataFrame, scenario: str) -> pd.DataFrame:
    trades = detail[detail["status"].astype(str).eq("EXECUTED")].copy()
    if trades.empty:
        return pd.DataFrame()
    dates = pd.to_datetime(trades["action_date"].astype(str), format="%Y%m%d")
    rows: list[dict[str, Any]] = []
    for period_type, frequency in (("year", "Y"), ("quarter", "Q"), ("month", "M")):
        labels = dates.dt.to_period(frequency).astype(str)
        for label in sorted(labels.unique()):
            group = trades.loc[labels.eq(label)].copy()
            rows.append(
                {
                    "scenario": scenario,
                    "period_type": period_type,
                    "period": label,
                    **action_metrics(
                        group,
                        str(group["action_date"].min()),
                        str(group["action_date"].max()),
                    ),
                }
            )
    return pd.DataFrame(rows)


def _leg_metrics(
    detail: pd.DataFrame, scenario: str, start: str, end: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for leg in FIXED_PRIORITY:
        selected = detail[
            detail["status"].astype(str).eq("EXECUTED")
            & detail["strategy_leg"].astype(str).eq(leg)
        ].copy()
        rows.append(
            {
                "scenario": scenario,
                "strategy_leg": leg,
                **action_metrics(selected, start, end),
            }
        )
    return rows


def _executed_keys(detail: pd.DataFrame) -> set[str]:
    rows = detail[detail["status"].astype(str).eq("EXECUTED")]
    return set(
        rows["action_date"].astype(str)
        + "|"
        + rows["strategy_leg"].astype(str)
        + "|"
        + rows["ts_code"].astype(str)
    )


def _sample_changes(old: pd.DataFrame, new: pd.DataFrame, scope: str) -> pd.DataFrame:
    old_exec = old[old["status"].astype(str).eq("EXECUTED")].copy()
    new_exec = new[new["status"].astype(str).eq("EXECUTED")].copy()
    key_columns = ["action_date", "strategy_leg", "ts_code"]
    old_keys = _executed_keys(old)
    new_keys = _executed_keys(new)
    frames: list[pd.DataFrame] = []
    for label, source, keys in (
        ("ADDED", new_exec, new_keys - old_keys),
        ("REMOVED", old_exec, old_keys - new_keys),
    ):
        if not keys:
            continue
        row_keys = (
            source["action_date"].astype(str)
            + "|"
            + source["strategy_leg"].astype(str)
            + "|"
            + source["ts_code"].astype(str)
        )
        selected = source.loc[row_keys.isin(keys)].copy()
        selected.insert(0, "change_type", label)
        selected.insert(0, "scope", scope)
        frames.append(selected)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _top_concentration(detail: pd.DataFrame) -> dict[str, Any]:
    trades = detail[detail["status"].astype(str).eq("EXECUTED")].copy()
    values = pd.to_numeric(trades["account_return"], errors="raise").to_numpy(float)
    positive_logs = np.log1p(values[values > 0])
    total = float(positive_logs.sum())
    ordered = np.sort(positive_logs)[::-1]
    return {
        "trade_count": int(len(values)),
        "top1_positive_log_share": float(ordered[:1].sum() / total) if total > 0 else 0.0,
        "top3_positive_log_share": float(ordered[:3].sum() / total) if total > 0 else 0.0,
        "top5_positive_log_share": float(ordered[:5].sum() / total) if total > 0 else 0.0,
        "return_over_30pct_count": int((values > 0.30).sum()),
        "return_below_minus_20pct_count": int((values < -0.20).sum()),
    }


def run(spec_path: Path) -> dict[str, Any]:
    spec = load_json_config(spec_path)
    if spec.get("mode") != "research_only" or bool(
        spec.get("formal_strategy_auto_apply", True)
    ):
        raise ValueError("C扩样研究必须保持research_only且禁止自动落地")
    if tuple(spec["frozen_rules"]["priority"]) != FIXED_PRIORITY:
        raise ValueError("C扩样研究腿序必须固定为A>C>E>D")

    cutoff = str(spec["cutoff"])
    window = build_monthly_research_window(cutoff)
    if (window.start, window.end) != (
        str(spec["window"]["start"]),
        str(spec["window"]["end"]),
    ):
        raise ValueError("研究窗口与月度最近三年窗口不一致")

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
    current_definition, _unused = c_variants(base_config)
    outcome_cache = StaticOutcomeCache()
    rebuilt_current_c = build_variant_plan(
        current_definition,
        signal_pool=context["signal_pool"],
        d_events=context["d_events"],
        allowed_action_dates=context["allowed_actions"],
        cutoff=cutoff,
        outcome_cache=outcome_cache,
    )
    if plan_signature(rebuilt_current_c) != plan_signature(formal_legs["C"]):
        raise RuntimeError("当前C由冻结数据重建后与正式C计划签名不一致")

    old_on_old_detail = replay_action_date_cash_portfolio(
        formal_legs,
        action_dates=context["action_dates"],
        priority=FIXED_PRIORITY,
        **execution,
    )
    old_on_new_legs = {leg: frame.copy() for leg, frame in formal_legs.items()}
    old_on_new_legs["C"] = rebuilt_current_c
    old_on_new_detail = replay_action_date_cash_portfolio(
        old_on_new_legs,
        action_dates=context["action_dates"],
        priority=FIXED_PRIORITY,
        **execution,
    )
    baseline_standalone_detail = replay_action_date_cash_portfolio(
        {"C": rebuilt_current_c},
        action_dates=context["action_dates"],
        priority=("C",),
        **execution,
    )
    baseline_c = action_metrics(baseline_standalone_detail, window.start, window.end)
    baseline_portfolio = action_metrics(old_on_new_detail, window.start, window.end)

    objective = spec["objective"]
    tolerance = float(objective["comparison_tolerance"])
    expected = (
        int(objective["baseline_c_trade_count"]),
        float(objective["baseline_c_equity_multiple"]),
        float(objective["baseline_portfolio_equity_multiple"]),
    )
    observed = (
        int(baseline_c["trade_count"]),
        float(baseline_c["equity_multiple"]),
        float(baseline_portfolio["equity_multiple"]),
    )
    if expected[0] != observed[0] or any(
        abs(left - right) > tolerance
        for left, right in zip(expected[1:], observed[1:])
    ):
        raise RuntimeError(f"冻结基线不一致：expected={expected}, observed={observed}")

    variants = declared_variants(base_config, spec)
    catalog = [
        {
            "variant_id": item.variant_id,
            "description": item.description,
            "changed_axis_count": item.changed_axis_count,
            "payload_sha256": hashlib.sha256(
                json.dumps(item.payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
        for item in variants
    ]
    seen_signatures: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    plan_store: dict[str, pd.DataFrame] = {}
    standalone_store: dict[str, pd.DataFrame] = {}
    portfolio_store: dict[str, pd.DataFrame] = {}

    for position, variant in enumerate(variants, 1):
        plan = build_variant_plan(
            variant,
            signal_pool=context["signal_pool"],
            d_events=context["d_events"],
            allowed_action_dates=context["allowed_actions"],
            cutoff=cutoff,
            outcome_cache=outcome_cache,
        )
        signature = plan_signature(plan)
        duplicate_of = seen_signatures.get(signature, "")
        if duplicate_of:
            rows.append(
                {
                    "variant_id": variant.variant_id,
                    "description": variant.description,
                    "changed_axis_count": variant.changed_axis_count,
                    "plan_signature": signature,
                    "duplicate_of": duplicate_of,
                    "eligible": False,
                    "gate_reasons": "DUPLICATE_PLAN_SIGNATURE",
                }
            )
            print(
                f"[{position}/{len(variants)}] {variant.variant_id}: "
                f"重复于{duplicate_of}",
                flush=True,
            )
            continue
        seen_signatures[signature] = variant.variant_id

        standalone_detail = replay_action_date_cash_portfolio(
            {"C": plan},
            action_dates=context["action_dates"],
            priority=("C",),
            **execution,
        )
        candidate_legs = {leg: frame.copy() for leg, frame in formal_legs.items()}
        candidate_legs["C"] = plan
        portfolio_detail = replay_action_date_cash_portfolio(
            candidate_legs,
            action_dates=context["action_dates"],
            priority=FIXED_PRIORITY,
            **execution,
        )
        c_metrics = action_metrics(standalone_detail, window.start, window.end)
        portfolio_metrics = action_metrics(portfolio_detail, window.start, window.end)
        gates = {
            "c_trade_count": int(c_metrics["trade_count"]) > int(baseline_c["trade_count"]),
            "c_equity_multiple": float(c_metrics["equity_multiple"])
            > float(baseline_c["equity_multiple"]) + tolerance,
            "portfolio_equity_multiple": float(portfolio_metrics["equity_multiple"])
            > float(baseline_portfolio["equity_multiple"]) + tolerance,
        }
        reasons = [name for name, passed in gates.items() if not passed]
        row = {
            "variant_id": variant.variant_id,
            "description": variant.description,
            "changed_axis_count": variant.changed_axis_count,
            "plan_signature": signature,
            "duplicate_of": "",
            "plan_count": int(len(plan)),
            "eligible": not reasons,
            "gate_reasons": ";".join(reasons),
            **{f"c_{key}": value for key, value in c_metrics.items()},
            **{f"portfolio_{key}": value for key, value in portfolio_metrics.items()},
        }
        rows.append(row)
        plan_store[variant.variant_id] = plan
        standalone_store[variant.variant_id] = standalone_detail
        portfolio_store[variant.variant_id] = portfolio_detail
        print(
            f"[{position}/{len(variants)}] {variant.variant_id}: "
            f"C={c_metrics['trade_count']}笔/{c_metrics['equity_multiple']:.6f}倍，"
            f"组合={portfolio_metrics['trade_count']}笔/"
            f"{portfolio_metrics['equity_multiple']:.6f}倍，"
            f"{'PASS' if not reasons else 'FAIL'}",
            flush=True,
        )

    metrics = pd.DataFrame(rows)
    eligible = metrics[metrics["eligible"].fillna(False).astype(bool)].copy()
    eligible = eligible.sort_values(
        [
            "portfolio_equity_multiple",
            "c_equity_multiple",
            "c_trade_count",
            "variant_id",
        ],
        ascending=[False, False, False, True],
    )
    selected_id = str(eligible.iloc[0]["variant_id"]) if not eligible.empty else ""

    output_root = ROOT / str(spec["output_root"])
    output_root.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(output_root / "candidate_metrics.csv", index=False)
    pd.DataFrame(catalog).to_csv(output_root / "candidate_catalog.csv", index=False)
    baseline_standalone_detail.to_csv(
        output_root / "old_c_standalone_ledger.csv", index=False
    )
    old_on_old_detail.to_csv(output_root / "old_on_old_portfolio_ledger.csv", index=False)
    old_on_new_detail.to_csv(output_root / "old_on_new_portfolio_ledger.csv", index=False)

    if selected_id:
        selected_plan = plan_store[selected_id]
        selected_standalone = standalone_store[selected_id]
        selected_portfolio = portfolio_store[selected_id]
        selected_plan.to_csv(output_root / "selected_c_plan.csv", index=False)
        selected_standalone.to_csv(
            output_root / "new_c_standalone_ledger.csv", index=False
        )
        selected_portfolio.to_csv(
            output_root / "new_on_new_portfolio_ledger.csv", index=False
        )
        sample_changes = pd.concat(
            [
                _sample_changes(
                    baseline_standalone_detail, selected_standalone, "C_STANDALONE"
                ),
                _sample_changes(old_on_new_detail, selected_portfolio, "ACED_PORTFOLIO"),
            ],
            ignore_index=True,
        )
        sample_changes.to_csv(output_root / "sample_changes.csv", index=False)
        period = pd.concat(
            [
                _period_metrics(baseline_standalone_detail, "OLD_C"),
                _period_metrics(selected_standalone, "NEW_C"),
                _period_metrics(old_on_new_detail, "OLD_ACED"),
                _period_metrics(selected_portfolio, "NEW_ACED"),
            ],
            ignore_index=True,
        )
        period.to_csv(output_root / "period_metrics.csv", index=False)
        leg_metrics = pd.DataFrame(
            [
                *_leg_metrics(old_on_new_detail, "OLD_ACED", window.start, window.end),
                *_leg_metrics(selected_portfolio, "NEW_ACED", window.start, window.end),
            ]
        )
        leg_metrics.to_csv(output_root / "portfolio_leg_metrics.csv", index=False)
        selected_c_metrics = action_metrics(selected_standalone, window.start, window.end)
        selected_portfolio_metrics = action_metrics(
            selected_portfolio, window.start, window.end
        )
    else:
        selected_plan = pd.DataFrame()
        selected_standalone = pd.DataFrame()
        selected_portfolio = pd.DataFrame()
        selected_c_metrics = {}
        selected_portfolio_metrics = {}
        sample_changes = pd.DataFrame()

    hashes_after = {leg: sha256_path(path) for leg, path in formal_paths.items()}
    formal_unchanged = hashes_before == hashes_after
    c_dates_ok = bool(
        not selected_id
        or (
            selected_plan["action_date"].astype(str).eq(
                selected_plan["buy_date"].astype(str)
            ).all()
            and pd.to_numeric(selected_plan["hold_offset"], errors="raise").eq(3).all()
            and selected_plan["signal_date"].astype(str).lt(
                selected_plan["action_date"].astype(str)
            ).all()
        )
    )
    finite_ok = bool(
        not selected_id
        or np.isfinite(
            pd.to_numeric(
                selected_standalone.loc[
                    selected_standalone["status"].astype(str).eq("EXECUTED"),
                    "account_return",
                ],
                errors="raise",
            )
        ).all()
    )
    anomaly_review = {
        "status": "PASS"
        if selected_id and formal_unchanged and c_dates_ok and finite_ok
        else "FAIL",
        "checks": {
            "formal_files_unchanged": formal_unchanged,
            "current_c_rebuild_matches_formal_signature": True,
            "old_on_old_equals_old_on_new": math.isclose(
                float(
                    action_metrics(old_on_old_detail, window.start, window.end)[
                        "equity_multiple"
                    ]
                ),
                float(baseline_portfolio["equity_multiple"]),
                rel_tol=0.0,
                abs_tol=tolerance,
            ),
            "c_t1_entry_t3_exit_contract": c_dates_ok,
            "selected_returns_finite": finite_ok,
            "selected_plan_action_date_unique": bool(
                not selected_id or not selected_plan["action_date"].duplicated().any()
            ),
            "a_e_d_plan_signatures_frozen": True,
            "fixed_priority": list(FIXED_PRIORITY),
        },
        "old_c_concentration": _top_concentration(baseline_standalone_detail),
        "new_c_concentration": (
            _top_concentration(selected_standalone) if selected_id else {}
        ),
        "old_portfolio_concentration": _top_concentration(old_on_new_detail),
        "new_portfolio_concentration": (
            _top_concentration(selected_portfolio) if selected_id else {}
        ),
    }
    write_json(output_root / "anomaly_review.json", anomaly_review)

    compatibility = {
        "OLD_ON_OLD": action_metrics(old_on_old_detail, window.start, window.end),
        "OLD_ON_NEW": baseline_portfolio,
        "NEW_ON_NEW": selected_portfolio_metrics,
        "scope_note": (
            "OLD_ON_NEW表示用当前冻结数据与代码重建旧C，不是新的时间样本；"
            "真正前向样本外只能从2026年9月开始封存。"
        ),
    }
    write_json(output_root / "compatibility_matrix.json", compatibility)

    summary = {
        "schema_version": 1,
        "decision": "USER_REVIEW" if selected_id else "KEEP_CURRENT",
        "formal_strategy_modified": False,
        "code_committed": False,
        "research_protocol": str(spec["research_protocol"]),
        "window": {"start": window.start, "end": window.end},
        "open_trade_days": int(len(context["action_dates"])),
        "candidate_declared_count": int(len(variants)),
        "candidate_unique_count": int(metrics["duplicate_of"].fillna("").eq("").sum()),
        "eligible_count": int(len(eligible)),
        "selected_variant": selected_id,
        "baseline_c": baseline_c,
        "selected_c": selected_c_metrics,
        "baseline_portfolio": baseline_portfolio,
        "selected_portfolio": selected_portfolio_metrics,
        "compatibility": compatibility,
        "anomaly_review_status": anomaly_review["status"],
        "sample_change_count": (
            sample_changes.groupby(["scope", "change_type"]).size().to_dict()
            if not sample_changes.empty
            else {}
        ),
        "risk_note": (
            "本轮候选使用同一最近三年窗口生成和排名，属于STRICT_DISCOVERY；"
            "即使三项目标同时通过，也不能冒充独立样本外或承诺未来收益。"
        ),
    }
    # JSON不支持元组键，样本变化统计改成可读字符串键。
    summary["sample_change_count"] = {
        "|".join(map(str, key)): int(value)
        for key, value in summary["sample_change_count"].items()
    }
    write_json(output_root / "summary.json", summary)
    write_json(
        output_root / "frozen_manifest.json",
        {
            "spec_path": str(spec_path.relative_to(ROOT)),
            "spec_sha256": sha256_path(spec_path),
            "declared_before_replay": True,
            "catalog": catalog,
            "formal_plan_sha256": hashes_before,
            "data_sources": {
                name: {"path": str(path.relative_to(ROOT)), "sha256": sha256_path(path)}
                for name, path in {
                    "strict_feature_pool": paths["strict_feature_pool"],
                    "market_sentiment": paths["market_sentiment"],
                    "d_event_source": paths["d_event_source"],
                    "trade_calendar": paths["trade_calendar"],
                }.items()
            },
        },
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    args = parser.parse_args()
    summary = run(args.spec.resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
