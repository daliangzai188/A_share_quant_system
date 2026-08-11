#!/usr/bin/env python3
"""复核已固定账户风险候选；禁止在本脚本内重新搜索参数。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.account_risk_historical import (  # noqa: E402
    RiskOverlaySpec,
    performance_metrics,
    replay_risk_overlay,
    segment_masks,
    validate_inputs,
)
from src.account_risk_robustness import (  # noqa: E402
    random_contiguous_window_results,
    summarize_random_windows,
)
from src.release_compound_guard import evaluate_certification_candidate  # noqa: E402


CONFIG_PATH = PROJECT_ROOT / "config" / "account_risk_robustness.json"


def load_json(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.exists() and not required:
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}根节点不是对象")
    return value


def project_path(config: dict[str, Any], key: str) -> Path:
    value = str(config.get(key, "")).strip()
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{key}必须是项目内相对路径")
    return PROJECT_ROOT / path


def markdown(frame: pd.DataFrame) -> str:
    headers = list(frame.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(map(str, row)) + " |" for row in frame.values.tolist())
    return "\n".join(lines)


def main() -> None:
    config = load_json(CONFIG_PATH)
    if int(config.get("schema_version", 0) or 0) != 1:
        raise ValueError("固定候选验证配置版本必须为1")
    if str(config.get("status", "")).upper() != "FIXED_CANDIDATE_VALIDATION":
        raise ValueError("验证配置状态必须为FIXED_CANDIDATE_VALIDATION")
    candidate = dict(config.get("candidate", {}))
    spec = RiskOverlaySpec(
        None
        if candidate.get("daily_loss_pct") is None
        else float(candidate["daily_loss_pct"]),
        None
        if candidate.get("drawdown_pct") is None
        else float(candidate["drawdown_pct"]),
        None
        if candidate.get("consecutive_losses") is None
        else int(candidate["consecutive_losses"]),
        int(candidate["cooldown_trade_days"]),
    )
    if spec.candidate_id != str(candidate.get("candidate_id", "")):
        raise ValueError("固定候选ID与结构化参数不一致")

    raw_trades = pd.read_csv(
        project_path(config, "input_trades_path"), dtype=str, low_memory=False
    )
    raw_trades["account_return"] = pd.to_numeric(
        raw_trades["account_return"], errors="coerce"
    )
    raw_daily = pd.read_csv(
        project_path(config, "input_daily_path"), dtype=str, low_memory=False
    )
    trades, calendar = validate_inputs(raw_trades, raw_daily)
    selected, decisions, triggers = replay_risk_overlay(trades, calendar, spec)
    baseline = performance_metrics(trades)
    fixed = performance_metrics(selected)
    expected = float(candidate["expected_full_equity_multiple"])
    if abs(fixed["equity_multiple"] - expected) > 1e-6:
        raise RuntimeError("固定候选历史复利漂移，拒绝生成稳健性结论")

    certification = load_json(project_path(config, "certification_path"))
    runtime_config = load_json(project_path(config, "runtime_config_path"))
    compound_policy = load_json(project_path(config, "compound_floor_policy_path"))
    candidate_certification = dict(certification)
    candidate_certification["equity_multiple"] = fixed["equity_multiple"]
    compound = evaluate_certification_candidate(
        compound_policy, candidate_certification, runtime_config
    )

    random_results = random_contiguous_window_results(
        trades,
        calendar,
        spec,
        window_trade_counts=config["window_trade_counts"],
        samples_per_window=int(config["random_samples_per_window"]),
        seed=int(config["random_seed"]),
        retained_floor=float(config["minimum_window_retained_ratio"]),
    )
    random_summary = summarize_random_windows(random_results)
    random_summary["pass_rate_requirement"] = float(config["minimum_window_pass_rate"])
    random_summary["p10_requirement"] = float(config["minimum_p10_retained_ratio"])
    random_summary["drawdown_rate_requirement"] = float(
        config["minimum_drawdown_noninferior_rate"]
    )
    random_summary["window_passed"] = (
        random_summary["retained_floor_pass_rate"].ge(
            float(config["minimum_window_pass_rate"])
        )
        & random_summary["p10_retained_ratio"].ge(
            float(config["minimum_p10_retained_ratio"])
        )
        & random_summary["drawdown_noninferior_rate"].ge(
            float(config["minimum_drawdown_noninferior_rate"])
        )
    )

    split_date = "20250603"
    masks = segment_masks(trades, split_date)
    segment_rows = []
    for name, mask in masks.items():
        baseline_segment = trades[mask]
        selected_segment = selected[
            selected["signal_date"].isin(set(baseline_segment["signal_date"]))
        ]
        baseline_metrics = performance_metrics(baseline_segment)
        candidate_metrics = performance_metrics(selected_segment)
        segment_rows.append(
            {
                "segment": name,
                "baseline_trade_count": baseline_metrics["sample_count"],
                "candidate_trade_count": candidate_metrics["sample_count"],
                "baseline_equity_multiple": baseline_metrics["equity_multiple"],
                "candidate_equity_multiple": candidate_metrics["equity_multiple"],
                "retained_ratio": candidate_metrics["equity_multiple"]
                / max(baseline_metrics["equity_multiple"], 1e-12),
                "baseline_max_drawdown": baseline_metrics["max_drawdown"],
                "candidate_max_drawdown": candidate_metrics["max_drawdown"],
                "drawdown_change": candidate_metrics["max_drawdown"]
                - baseline_metrics["max_drawdown"],
            }
        )
    segments = pd.DataFrame(segment_rows)
    segments["segment_floor_passed"] = segments["retained_ratio"].ge(
        float(config["minimum_fixed_segment_retained_ratio"])
    )
    segments["segment_drawdown_passed"] = segments["drawdown_change"].ge(
        -float(config["maximum_fixed_segment_drawdown_degradation_pct"])
    )

    oos = load_json(project_path(config, "release_oos_status_path"), required=False)
    true_oos_samples = int(
        oos.get("actual_executed_sample_count", oos.get("actual_sample_count", 0)) or 0
    )
    historical_passed = bool(
        compound["hard_floor_passed"]
        and fixed["max_drawdown"] >= baseline["max_drawdown"] - 1e-12
        and random_summary["window_passed"].all()
        and segments["segment_floor_passed"].all()
        and segments["segment_drawdown_passed"].all()
    )
    summary = {
        "schema_version": 1,
        "validation_id": config["validation_id"],
        "status": "HISTORICAL_ROBUSTNESS_PASS_SHADOW_ONLY"
        if historical_passed
        else "HISTORICAL_ROBUSTNESS_FAIL",
        "candidate_id": spec.candidate_id,
        "parameter_search_performed": False,
        "baseline_trade_count": baseline["sample_count"],
        "candidate_trade_count": fixed["sample_count"],
        "baseline_equity_multiple": baseline["equity_multiple"],
        "candidate_equity_multiple": fixed["equity_multiple"],
        "retained_ratio": fixed["equity_multiple"] / baseline["equity_multiple"],
        "hard_floor_multiple": compound["hard_floor_multiple"],
        "hard_floor_passed": compound["hard_floor_passed"],
        "compound_guard_status": compound["status"],
        "baseline_max_drawdown": baseline["max_drawdown"],
        "candidate_max_drawdown": fixed["max_drawdown"],
        "drawdown_improvement": fixed["max_drawdown"] - baseline["max_drawdown"],
        "trigger_count": int(len(triggers)),
        "skipped_trade_count": int(
            decisions["risk_decision"].eq("SKIP_RISK_COOLDOWN").sum()
        ),
        "random_window_count": int(len(random_results)),
        "random_window_groups_passed": bool(random_summary["window_passed"].all()),
        "fixed_segments_passed": bool(segments["segment_floor_passed"].all()),
        "fixed_segment_drawdown_passed": bool(
            segments["segment_drawdown_passed"].all()
        ),
        "worst_segment_drawdown_change": float(segments["drawdown_change"].min()),
        "maximum_segment_drawdown_degradation_allowed": -float(
            config["maximum_fixed_segment_drawdown_degradation_pct"]
        ),
        "historical_robustness_passed": historical_passed,
        "true_oos_status": str(oos.get("status", "MISSING")),
        "true_oos_sample_count": true_oos_samples,
        "live_activation_allowed": False,
        "live_activation_block_reason": (
            "随机历史重采样仍不是冻结发布后的真正样本外；当前保持影子模式，"
            "不得因历史稳健性通过而自动接入实盘。"
        ),
        "capacity_certified": False,
        "note": "历史结果不代表未来收益；跳过交易后未回填其他候选，口径偏保守。",
    }

    output = project_path(config, "output_dir")
    output.mkdir(parents=True, exist_ok=True)
    random_results.to_csv(
        output / "random_window_results.csv", index=False, encoding="utf-8-sig"
    )
    random_summary.to_csv(
        output / "random_window_summary.csv", index=False, encoding="utf-8-sig"
    )
    segments.to_csv(output / "fixed_segments.csv", index=False, encoding="utf-8-sig")
    decisions.to_csv(
        output / "fixed_candidate_decisions.csv", index=False, encoding="utf-8-sig"
    )
    triggers.to_csv(
        output / "fixed_candidate_triggers.csv", index=False, encoding="utf-8-sig"
    )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    view = random_summary.copy()
    for column in (
        "retained_floor_pass_rate",
        "p10_retained_ratio",
        "median_retained_ratio",
        "minimum_retained_ratio",
        "drawdown_noninferior_rate",
        "candidate_positive_rate",
    ):
        view[column] = view[column].map(lambda value: f"{float(value):.2%}")
    report = [
        "# 固定账户风险候选随机历史复核",
        "",
        f"- 候选：`{spec.candidate_id}`；本步骤没有重新搜索参数。",
        f"- 总复利：{fixed['equity_multiple']:.2f}倍，基准{baseline['equity_multiple']:.2f}倍，保留{summary['retained_ratio']:.2%}。",
        f"- 最大回撤：{fixed['max_drawdown']:.2%}，基准{baseline['max_drawdown']:.2%}，改善{summary['drawdown_improvement']:.2%}。",
        f"- 随机连续窗口：{len(random_results)}个；全部窗口组通过={summary['random_window_groups_passed']}。",
        f"- 真正样本外：{summary['true_oos_status']}，样本{true_oos_samples}笔。",
        "- 结论：历史稳健性通过也只保留为影子候选，不修改实盘；随机重采样不能冒充真正OOS。",
        "",
        markdown(view),
    ]
    (output / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
