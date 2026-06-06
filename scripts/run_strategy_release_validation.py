"""
运行策略发布前稳定性验证。

文件作用：
1. 使用当前 A严格策略 + B0018过滤版固定配置，验证发布前稳定性。
2. 一次性检查样本外区间、60/90/120日近端窗口，不做每日改策略。
3. 按配置里的收益、胜率、回撤、样本数、最大单笔亏损、B依赖度等阈值输出 PASS/FAIL。
4. 生成发布验证报告，明确当前策略是否只能研究、是否可进入下一阶段模拟/小资金人工确认。

本脚本只使用本地 CSV 和本地配置，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_backup_strategy_b import (
    replay_selected_b,
    selected_b_signals,
    simulate_a_plus_b_strict,
    simulate_b_strict,
    summarize,
)
from scripts.run_paper_ab_filtered_observation_window import (
    configured_b_conditions,
    condition_text,
    load_audit,
    reject_b_risk_mask,
)
from scripts.search_paper_backup_strategy_b import (
    backup_config,
    build_generator,
    normalize_date,
    scoped_no_candidate_dates,
    simulate_single_strategy,
)
from src.paper_candidate_generator import PaperCandidateGenerator
from src.utils.config import load_json_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行策略发布前稳定性验证。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json", help="策略配置文件路径。")
    parser.add_argument("--runtime-config", default="config/config.json", help="运行时配置文件路径。")
    parser.add_argument("--oos-start-date", default=None, help="样本外开始日期，默认读取配置。")
    parser.add_argument("--oos-end-date", default=None, help="样本外结束日期，默认读取配置。")
    parser.add_argument("--output-prefix", default=None, help="输出文件前缀，默认读取配置。")
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def resolve_dates(
    candidates: pd.DataFrame,
    start_date: str | None = None,
    end_date: str | None = None,
    recent_days: int | None = None,
) -> list[str]:
    dates = sorted(candidates["trade_date"].dropna().map(normalize_date).unique().tolist())
    if start_date:
        dates = [date for date in dates if date >= str(start_date)]
    if end_date:
        dates = [date for date in dates if date <= str(end_date)]
    if recent_days is not None:
        dates = dates[-int(recent_days) :]
    if not dates:
        raise RuntimeError("没有可用于策略发布验证的交易日。")
    return dates


def assert_safe_config(config: dict[str, Any]) -> None:
    safe_modes = {"paper", "simulation", "dry_run", "research"}
    trade_mode = str(config.get("trade_mode", "")).strip().lower()
    if trade_mode not in safe_modes:
        raise RuntimeError(f"拒绝运行发布验证：trade_mode不是安全模式: {trade_mode}")
    for key in ["live_trading_enabled", "broker_adapter_enabled", "qmt_enabled"]:
        if bool(config.get(key, False)):
            raise RuntimeError(f"拒绝运行发布验证：{key}=true")
    release_config = config.get("strategy_release_validation", {})
    if bool(release_config.get("live_order_enabled", False)):
        raise RuntimeError("拒绝运行发布验证：strategy_release_validation.live_order_enabled=true")


def build_context(strategy_config_path: str | Path, runtime_config_path: str | Path) -> dict[str, Any]:
    config = load_json_config(strategy_config_path)
    assert_safe_config(config)
    generator = PaperCandidateGenerator(strategy_config_path)
    all_candidates = generator.load_all_candidates()
    audit = load_audit(strategy_config_path)
    initial_equity = float(config.get("position", {}).get("initial_cash", 500000))
    position_pct = float(config.get("position", {}).get("target_position_pct", 0.8))
    b_conditions = configured_b_conditions(config)
    return {
        "config": config,
        "strategy_config_path": strategy_config_path,
        "runtime_config_path": runtime_config_path,
        "all_candidates": all_candidates,
        "audit": audit,
        "initial_equity": initial_equity,
        "position_pct": position_pct,
        "b_conditions": b_conditions,
    }


def run_strategy_window(context: dict[str, Any], label: str, dates: list[str]) -> tuple[dict[str, Any], pd.DataFrame]:
    config = context["config"]
    strategy_config_path = context["strategy_config_path"]
    all_candidates = context["all_candidates"]
    initial_equity = context["initial_equity"]
    position_pct = context["position_pct"]
    audit = context["audit"]

    a_config = copy.deepcopy(config)
    a_generator = build_generator(strategy_config_path, a_config)
    a_filtered = a_generator.apply_strategy_filters(all_candidates)
    a_detail = simulate_single_strategy(
        scenario="A_strict",
        generator=a_generator,
        filtered=a_filtered,
        audit=audit,
        dates=dates,
        initial_equity=initial_equity,
        position_pct=position_pct,
    )
    no_candidate_dates = scoped_no_candidate_dates(a_detail)

    b_config = backup_config(config, context["b_conditions"])
    b_generator = build_generator(strategy_config_path, b_config)
    b_filtered = b_generator.apply_strategy_filters(all_candidates)
    selected_b = selected_b_signals(b_generator, b_filtered, no_candidate_dates)
    replayed_b = replay_selected_b(selected_b, context["runtime_config_path"])
    rejected_mask = reject_b_risk_mask(replayed_b, config)
    rejected_b = replayed_b[rejected_mask].copy()
    replayed_b_filtered = replayed_b[~rejected_mask].copy()

    b_detail = simulate_b_strict(replayed_b_filtered, no_candidate_dates, initial_equity)
    combo_detail = simulate_a_plus_b_strict(a_detail, replayed_b_filtered, audit, dates, initial_equity)
    combo_summary = summarize(combo_detail, label, condition_text(context["b_conditions"]))
    b_summary = summarize(b_detail, label + "_b_only", condition_text(context["b_conditions"]))
    combo_summary.update(
        {
            "window_label": label,
            "start_date": dates[0],
            "end_date": dates[-1],
            "actual_day_count": int(len(dates)),
            "b_selected_signal_count_before_filter": int(len(selected_b)),
            "b_rejected_by_risk_filter_count": int(len(rejected_b)),
            "b_replayed_signal_count_after_filter": int(len(replayed_b_filtered)),
            "b_only_equity_multiple": float(b_summary["equity_multiple"]),
        }
    )
    combo_detail = combo_detail.copy()
    combo_detail["window_label"] = label
    return combo_summary, combo_detail


def build_gate_rows(window_summary: pd.DataFrame, gates: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for row in window_summary.itertuples(index=False):
        data = row._asdict()
        label = str(data["window_label"])
        executed = int(data.get("executed_trade_count", 0))
        b_trade_count = int(data.get("b_trade_count", 0))
        b_share = b_trade_count / executed if executed else 0.0
        checks = [
            (
                "min_equity_multiple",
                float(data.get("equity_multiple", 0.0)),
                ">=",
                float(gates.get("min_equity_multiple", 1.0)),
            ),
            ("min_win_rate", float(data.get("win_rate", 0.0)), ">=", float(gates.get("min_win_rate", 0.0))),
            (
                "max_drawdown_abs",
                abs(float(data.get("max_drawdown", 0.0))),
                "<=",
                float(gates.get("max_drawdown_abs", 1.0)),
            ),
            (
                "max_single_loss_abs",
                abs(float(data.get("max_loss", 0.0))),
                "<=",
                float(gates.get("max_single_loss_abs", 1.0)),
            ),
            (
                "min_executed_trade_count",
                executed,
                ">=",
                int(gates.get("min_executed_trade_count", 0)),
            ),
            ("max_b_trade_share", b_share, "<=", float(gates.get("max_b_trade_share", 1.0))),
            (
                "max_limit_down_blocked_trade_count",
                int(data.get("limit_down_blocked_trade_count", 0)),
                "<=",
                int(gates.get("max_limit_down_blocked_trade_count", 0)),
            ),
        ]
        for gate_name, actual, operator, threshold in checks:
            passed = actual >= threshold if operator == ">=" else actual <= threshold
            rows.append(
                {
                    "window_label": label,
                    "gate_name": gate_name,
                    "actual": actual,
                    "operator": operator,
                    "threshold": threshold,
                    "passed": bool(passed),
                }
            )
    return pd.DataFrame(rows)


def build_release_summary(
    config: dict[str, Any],
    window_summary: pd.DataFrame,
    gate_report: pd.DataFrame,
    oos_start_date: str,
    oos_end_date: str,
) -> pd.DataFrame:
    release_config = config.get("strategy_release_validation", {})
    gates_passed = bool(gate_report["passed"].all()) if not gate_report.empty else False
    minute_required = bool(release_config.get("gates", {}).get("require_minute_k_validation_before_live", True))
    live_allowed = False
    release_status = "PASS_PAPER_READY_REVIEW_ONLY" if gates_passed else "FAIL_RESEARCH_ONLY"
    if minute_required:
        release_status = release_status + "_MINUTE_K_REQUIRED"
    return pd.DataFrame(
        [
            {
                "strategy_label": release_config.get("strategy_label", config.get("strategy_name", "")),
                "strategy_name": config.get("strategy_name", ""),
                "validation_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "release_status": release_status,
                "all_gates_passed": gates_passed,
                "oos_start_date": oos_start_date,
                "oos_end_date": oos_end_date,
                "validated_window_count": int(len(window_summary)),
                "rebalance_cycle": release_config.get("rebalance_cycle", ""),
                "trade_mode": config.get("trade_mode", ""),
                "live_order_enabled": live_allowed,
                "minute_k_required_before_live": minute_required,
                "decision_note": "可进入下一阶段模拟/小资金人工确认前复核；仍不允许自动实盘。"
                if gates_passed
                else "未通过发布阈值，只能继续研究或重新优化。",
            }
        ]
    )


def write_release_json(path: Path, release_summary: pd.DataFrame, window_summary: pd.DataFrame, gate_report: pd.DataFrame) -> None:
    payload = {
        "release_summary": release_summary.iloc[0].to_dict() if not release_summary.empty else {},
        "windows": window_summary.to_dict(orient="records"),
        "gates": gate_report.to_dict(orient="records"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown(path: Path, release_summary: pd.DataFrame, window_summary: pd.DataFrame, gate_report: pd.DataFrame) -> None:
    window_columns = [
        "window_label",
        "start_date",
        "end_date",
        "executed_trade_count",
        "a_trade_count",
        "b_trade_count",
        "win_rate",
        "equity_multiple",
        "max_drawdown",
        "max_loss",
        "limit_down_blocked_trade_count",
    ]
    window_columns = [column for column in window_columns if column in window_summary.columns]
    content = f"""# 策略发布前稳定性验证

本报告用于策略版本发布前验证，不用于每日改策略。不接实盘，不调用 QMT，不下真实订单。

## 发布结论

{release_summary.to_markdown(index=False)}

## 窗口表现

{window_summary[window_columns].to_markdown(index=False) if not window_summary.empty else "无窗口结果。"}

## 阈值检查

{gate_report.to_markdown(index=False) if not gate_report.empty else "无阈值检查。"}

## 解释

- PASS 只代表当前固定口径通过本地发布前验证，不代表可以自动实盘。
- 仍需分钟 K、集合竞价、盘口五档和小资金人工确认验证。
- 发布后不应每日改参数；建议按季度或半年重新训练和重新发布。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    context = build_context(args.strategy_config, args.runtime_config)
    config = context["config"]
    release_config = config.get("strategy_release_validation", {})
    oos_start_date = str(args.oos_start_date or release_config.get("oos_start_date", ""))
    oos_end_date = str(args.oos_end_date or release_config.get("oos_end_date", ""))

    all_candidates = context["all_candidates"]
    windows: list[tuple[str, list[str]]] = []
    if oos_start_date and oos_end_date:
        windows.append(("oos_range", resolve_dates(all_candidates, oos_start_date, oos_end_date)))
    for recent_days in release_config.get("recent_windows", [60, 90, 120]):
        dates = resolve_dates(all_candidates, end_date=oos_end_date or None, recent_days=int(recent_days))
        windows.append((f"recent_{int(recent_days)}d", dates))

    summary_rows = []
    detail_frames = []
    for label, dates in windows:
        summary, detail = run_strategy_window(context, label, dates)
        summary_rows.append(summary)
        detail_frames.append(detail)

    window_summary = pd.DataFrame(summary_rows)
    detail_all = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    gate_report = build_gate_rows(window_summary, release_config.get("gates", {}))
    release_summary = build_release_summary(config, window_summary, gate_report, oos_start_date, oos_end_date)

    output_prefix = resolve_path(args.output_prefix or release_config.get("output_prefix", "reports/strategy_release/current"))
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    windows_path = output_prefix.with_name(output_prefix.name + "_windows.csv")
    gates_path = output_prefix.with_name(output_prefix.name + "_gates.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    json_path = output_prefix.with_name(output_prefix.name + ".json")
    markdown_path = output_prefix.with_name(output_prefix.name + ".md")

    release_summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    window_summary.to_csv(windows_path, index=False, encoding="utf-8-sig")
    gate_report.to_csv(gates_path, index=False, encoding="utf-8-sig")
    detail_all.to_csv(detail_path, index=False, encoding="utf-8-sig")
    write_release_json(json_path, release_summary, window_summary, gate_report)
    write_markdown(markdown_path, release_summary, window_summary, gate_report)

    print("策略发布前稳定性验证完成：")
    print(f"- summary: {summary_path}")
    print(f"- windows: {windows_path}")
    print(f"- gates: {gates_path}")
    print(f"- detail: {detail_path}")
    print(f"- json: {json_path}")
    print(f"- markdown: {markdown_path}")
    print(release_summary.to_string(index=False))
    print(window_summary.to_string(index=False))


if __name__ == "__main__":
    main()
