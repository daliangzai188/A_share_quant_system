#!/usr/bin/env python3
"""历史研究账户级风险暂停；只生成报告，不连接券商、不改实盘。"""
from __future__ import annotations

import itertools
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
from src.release_compound_guard import (  # noqa: E402
    evaluate_certification_candidate,
)


CONFIG_PATH = PROJECT_ROOT / "config" / "account_risk_research.json"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}根节点不是对象")
    return value


def path_of(config: dict[str, Any], key: str) -> Path:
    path = Path(str(config.get(key, "")))
    if not str(path) or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{key}必须是项目内相对路径")
    return PROJECT_ROOT / path


def optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def spec_row(spec: RiskOverlaySpec) -> dict[str, Any]:
    return {
        "candidate_id": spec.candidate_id,
        "daily_loss_pct": spec.daily_loss_pct,
        "drawdown_pct": spec.drawdown_pct,
        "consecutive_losses": spec.consecutive_losses,
        "cooldown_trade_days": spec.cooldown_trade_days,
    }


def markdown_table(frame: pd.DataFrame, columns: list[str]) -> str:
    view = frame[columns].copy()
    headers = list(view.columns)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(map(str, row)) + " |" for row in view.values.tolist())
    return "\n".join(lines)


def json_safe(value: Any) -> Any:
    """把pandas/numpy空值转换为严格JSON的null。"""

    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def main() -> None:
    config = load_json(CONFIG_PATH)
    if int(config.get("schema_version", 0) or 0) != 1:
        raise ValueError("研究配置版本必须为1")
    if str(config.get("status", "")).upper() != "RESEARCH_ONLY":
        raise ValueError("研究配置状态必须为RESEARCH_ONLY")
    trades_raw = pd.read_csv(path_of(config, "input_trades_path"), dtype=str, low_memory=False)
    daily_raw = pd.read_csv(path_of(config, "input_daily_path"), dtype=str, low_memory=False)
    trades_raw["account_return"] = pd.to_numeric(
        trades_raw["account_return"], errors="coerce"
    )
    trades, calendar = validate_inputs(trades_raw, daily_raw)
    certification = load_json(path_of(config, "certification_path"))
    runtime_config = load_json(path_of(config, "runtime_config_path"))
    compound_policy = load_json(path_of(config, "compound_floor_policy_path"))
    shadow_policy = load_json(path_of(config, "shadow_policy_path"))
    split_date = str(config.get("split_date", "")).replace("-", "")
    minimum_segment_ratio = float(config.get("minimum_segment_retained_ratio", 0.7))

    baseline_metrics = performance_metrics(trades)
    certified_multiple = float(certification.get("equity_multiple", 0.0))
    if abs(baseline_metrics["equity_multiple"] - certified_multiple) > 1e-6:
        raise RuntimeError("历史交易复利无法复现当前认证基准，拒绝继续研究")
    masks = segment_masks(trades, split_date)
    baseline_segments = {
        name: performance_metrics(trades[mask]) for name, mask in masks.items()
    }

    raw_specs: list[RiskOverlaySpec] = [RiskOverlaySpec(None, None, None, 1)]
    for daily_loss, drawdown, streak, cooldown in itertools.product(
        config["daily_realized_loss_thresholds"],
        config["account_drawdown_thresholds"],
        config["consecutive_loss_thresholds"],
        config["cooldown_trade_days"],
    ):
        daily_value = optional_float(daily_loss)
        drawdown_value = optional_float(drawdown)
        streak_value = optional_int(streak)
        if daily_value is None and drawdown_value is None and streak_value is None:
            continue
        effective_cooldown = int(cooldown) if (drawdown_value is not None or streak_value is not None) else 1
        raw_specs.append(
            RiskOverlaySpec(daily_value, drawdown_value, streak_value, effective_cooldown)
        )
    specs = list({spec.candidate_id: spec for spec in raw_specs}.values())

    grid_rows: list[dict[str, Any]] = []
    segment_rows: list[dict[str, Any]] = []
    decisions_by_id: dict[str, pd.DataFrame] = {}
    triggers_by_id: dict[str, pd.DataFrame] = {}
    for spec in specs:
        selected, decisions, triggers = replay_risk_overlay(trades, calendar, spec)
        decisions_by_id[spec.candidate_id] = decisions
        triggers_by_id[spec.candidate_id] = triggers
        metrics = performance_metrics(selected)
        candidate_certification = dict(certification)
        candidate_certification["equity_multiple"] = metrics["equity_multiple"]
        compound = evaluate_certification_candidate(
            compound_policy, candidate_certification, runtime_config
        )
        segment_ratios: list[float] = []
        for name, mask in masks.items():
            candidate_segment = selected[
                selected["signal_date"].isin(set(trades.loc[mask, "signal_date"]))
            ]
            candidate_metrics = performance_metrics(candidate_segment)
            baseline_segment = baseline_segments[name]
            ratio = candidate_metrics["equity_multiple"] / max(
                baseline_segment["equity_multiple"], 1e-12
            )
            segment_ratios.append(ratio)
            segment_rows.append(
                {
                    "candidate_id": spec.candidate_id,
                    "segment": name,
                    "sample_count": candidate_metrics["sample_count"],
                    "equity_multiple": candidate_metrics["equity_multiple"],
                    "baseline_equity_multiple": baseline_segment["equity_multiple"],
                    "retained_ratio": ratio,
                    "max_drawdown": candidate_metrics["max_drawdown"],
                    "baseline_max_drawdown": baseline_segment["max_drawdown"],
                }
            )
        trigger_count = int(len(triggers))
        skipped_count = int(decisions["risk_decision"].eq("SKIP_RISK_COOLDOWN").sum())
        row = {
            **spec_row(spec),
            **metrics,
            "trigger_count": trigger_count,
            "skipped_trade_count": skipped_count,
            "retained_ratio": metrics["equity_multiple"]
            / baseline_metrics["equity_multiple"],
            "drawdown_improvement": metrics["max_drawdown"]
            - baseline_metrics["max_drawdown"],
            "minimum_segment_retained_ratio": min(segment_ratios),
            "compound_guard_status": compound["status"],
            "hard_floor_passed": bool(compound["hard_floor_passed"]),
            "all_segments_floor_passed": min(segment_ratios) >= minimum_segment_ratio,
            "drawdown_noninferior": metrics["max_drawdown"]
            >= baseline_metrics["max_drawdown"] - 1e-12,
            "same_sample_research_only": True,
            "live_release_allowed": False,
        }
        row["robust_research_candidate"] = bool(
            row["hard_floor_passed"]
            and row["all_segments_floor_passed"]
            and row["drawdown_noninferior"]
            and trigger_count > 0
        )
        grid_rows.append(row)

    grid = pd.DataFrame(grid_rows)
    segments = pd.DataFrame(segment_rows)
    eligible = grid[grid["robust_research_candidate"]].copy()
    if not eligible.empty:
        eligible = eligible.sort_values(
            ["drawdown_improvement", "retained_ratio", "trigger_count"],
            ascending=[False, False, False],
        )
        selected_id = str(eligible.iloc[0]["candidate_id"])
    else:
        selected_id = ""

    shadow_spec = RiskOverlaySpec(
        float(shadow_policy["max_daily_realized_loss_pct"]),
        float(shadow_policy["max_account_drawdown_pct"]),
        int(shadow_policy["max_consecutive_losses"]),
        int(shadow_policy["suggested_cooldown_trade_days"]),
    )
    shadow_row = grid[grid["candidate_id"].eq(shadow_spec.candidate_id)]
    if shadow_row.empty:
        raise RuntimeError("当前影子阈值未包含在历史网格中")
    shadow_result = shadow_row.iloc[0].to_dict()
    selected_result = json_safe(
        grid[grid["candidate_id"].eq(selected_id)].iloc[0].to_dict()
        if selected_id
        else {}
    )

    output = path_of(config, "output_dir")
    output.mkdir(parents=True, exist_ok=True)
    grid.sort_values(
        ["robust_research_candidate", "drawdown_improvement", "retained_ratio"],
        ascending=[False, False, False],
    ).to_csv(output / "grid_results.csv", index=False, encoding="utf-8-sig")
    segments.to_csv(output / "segment_results.csv", index=False, encoding="utf-8-sig")
    if selected_id:
        decisions_by_id[selected_id].to_csv(
            output / "selected_trade_decisions.csv", index=False, encoding="utf-8-sig"
        )
        triggers_by_id[selected_id].to_csv(
            output / "selected_triggers.csv", index=False, encoding="utf-8-sig"
        )

    summary = {
        "schema_version": 1,
        "research_id": config["research_id"],
        "status": "RESEARCH_COMPLETE_NO_LIVE_CHANGE",
        "input_trade_count": int(len(trades)),
        "grid_candidate_count": int(len(grid)),
        "baseline_equity_multiple": baseline_metrics["equity_multiple"],
        "baseline_max_drawdown": baseline_metrics["max_drawdown"],
        "hard_floor_multiple": float(compound_policy["hard_floor_multiple"]),
        "robust_research_candidate_count": int(len(eligible)),
        "selected_research_candidate_id": selected_id,
        "selected_research_candidate": selected_result,
        "current_shadow_policy_result": json_safe(shadow_result),
        "live_change_recommended": False,
        "live_change_block_reason": (
            "全部结果来自当前冻结历史样本内搜索；当前影子阈值若未通过70%硬底线更不得接入。"
            "即使研究候选通过，也必须先固定规则并做独立样本外/随机区间复核。"
        ),
        "capacity_certified": False,
        "note": "跳过交易后未回填其他候选，收益估计偏保守；历史结果不代表未来收益。",
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    top = grid.sort_values(
        ["robust_research_candidate", "drawdown_improvement", "retained_ratio"],
        ascending=[False, False, False],
    ).head(12).copy()
    for column in ("retained_ratio", "max_drawdown", "drawdown_improvement"):
        top[column] = top[column].map(lambda value: f"{float(value):.2%}")
    top["equity_multiple"] = top["equity_multiple"].map(
        lambda value: f"{float(value):.2f}"
    )
    report = [
        "# 账户级风险总闸历史研究",
        "",
        f"- 冻结基准：{baseline_metrics['equity_multiple']:.2f}倍，最大回撤{baseline_metrics['max_drawdown']:.2%}，150笔。",
        f"- 复利硬底线：{float(compound_policy['hard_floor_multiple']):.2f}倍（当前基准70%）。",
        f"- 网格：{len(grid)}组；通过总复利、所有分段70%及回撤非劣的研究候选：{len(eligible)}组。",
        "- 结论：只完成历史研究，不修改实盘；样本内最优不能当作样本外证据。",
        "- 口径：风险暂停后不回填其他候选，结果偏保守；容量仍未认证。",
        "",
        "## 当前影子阈值",
        "",
        f"- 复利：{float(shadow_result['equity_multiple']):.2f}倍，保留率{float(shadow_result['retained_ratio']):.2%}。",
        f"- 最大回撤：{float(shadow_result['max_drawdown']):.2%}；硬底线通过={bool(shadow_result['hard_floor_passed'])}。",
        "- 当前影子阈值只可继续观测，不能接入实盘。",
        "",
        "## 优先复核的研究候选",
        "",
        (
            f"- 候选：`{selected_id}`；复利{float(selected_result['equity_multiple']):.2f}倍，"
            f"保留率{float(selected_result['retained_ratio']):.2%}，最大回撤"
            f"{float(selected_result['max_drawdown']):.2%}。"
            if selected_result
            else "- 没有候选同时通过硬底线、全部分段和回撤非劣检查。"
        ),
        "- 仍不建议直接实盘：需要下一步独立随机区间和前推验证，防止从420组中挑中偶然最优。",
        "",
        "## 排名前12组",
        "",
        markdown_table(
            top,
            [
                "candidate_id",
                "sample_count",
                "equity_multiple",
                "retained_ratio",
                "max_drawdown",
                "drawdown_improvement",
                "trigger_count",
                "skipped_trade_count",
                "minimum_segment_retained_ratio",
                "compound_guard_status",
                "robust_research_candidate",
            ],
        ),
    ]
    (output / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
