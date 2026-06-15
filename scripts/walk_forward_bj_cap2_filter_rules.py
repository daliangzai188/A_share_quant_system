"""
对 bj_cap_2pct 分桶过滤规则做训练集/测试集验证。

文件作用：
1. 固定最近 2 年方案2候选信号和 bj_cap_2pct 执行口径。
2. 用 2024-2025 作为训练期，从 50 万重新完整回放每条过滤规则。
3. 用 2026 作为测试期，从 50 万重新完整回放同一条过滤规则。
4. 只按训练期指标排序，观察训练期选出的规则在 2026 是否仍然有效。

本脚本只读取本地报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backtest_bj_cap2_full_replay_with_forward_filters import (  # noqa: E402
    build_yearly,
    load_config,
    load_rows,
    replay_rule,
    summarize_detail,
)
from scripts.validate_bj_cap2_filter_robustness_grid import (  # noqa: E402
    BASE_RULE_NAME,
    TARGET_RULE_NAME,
    build_rules,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="bj_cap_2pct 过滤规则 walk-forward 验证。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_target50_top_scenarios_trades.csv",
        help="最近2年方案逐信号交易明细。",
    )
    parser.add_argument("--scenario-rank", type=int, default=2, help="默认回放方案2。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--train-start", default="20240101", help="训练开始日期。")
    parser.add_argument("--train-end", default="20251231", help="训练结束日期。")
    parser.add_argument("--test-start", default="20260101", help="测试开始日期。")
    parser.add_argument("--test-end", default="20260518", help="测试结束日期。")
    parser.add_argument(
        "--output-prefix",
        default="reports/bj_cap2_filter_walk_forward",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def prepare_execution_config(config: dict[str, Any]) -> dict[str, Any]:
    risk = config.get("risk", {})
    opt = config.get("realistic_condition_strategy_search", {})
    return {
        "initial_cash": float(opt.get("initial_cash", 500000)),
        "position_pct": float(opt.get("position_pct", 0.8)),
        "default_capacity": float(opt.get("max_buy_amount_ratio", 0.05)),
        "bj_capacity": 0.02,
        "slippage_tiers": list(opt.get("slippage_tiers", [])),
        "fee_rate_without_slippage": (
            float(risk.get("commission_rate", 0.0003))
            + float(risk.get("transfer_fee_rate", 0.00001))
            + float(risk.get("commission_rate", 0.0003))
            + float(risk.get("transfer_fee_rate", 0.00001))
            + float(risk.get("stamp_tax_rate", 0.001))
        ),
    }


def slice_by_trade_date(rows: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    trade_dates = rows["trade_date"].map(normalize_date)
    return rows[(trade_dates >= start_date) & (trade_dates <= end_date)].copy().reset_index(drop=True)


def replay_rules_for_period(
    rows: pd.DataFrame,
    rules: list[dict[str, Any]],
    execution_config: dict[str, Any],
    period_name: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    detail_frames = []
    summary_rows = []
    for rule in rules:
        detail = replay_rule(
            rows=rows,
            rule=rule,
            initial_cash=float(execution_config["initial_cash"]),
            position_pct=float(execution_config["position_pct"]),
            default_capacity=float(execution_config["default_capacity"]),
            bj_capacity=float(execution_config["bj_capacity"]),
            slippage_tiers=list(execution_config["slippage_tiers"]),
            fee_rate_without_slippage=float(execution_config["fee_rate_without_slippage"]),
        )
        detail["wf_period"] = period_name
        summary = summarize_detail(detail, float(execution_config["initial_cash"]))
        summary.update(
            {
                "wf_period": period_name,
                "rule_group": rule.get("rule_group", ""),
                "fd_ratio_bucket": rule.get("fd_ratio_bucket", ""),
                "market_limit_down_count_bucket": rule.get("market_limit_down_count_bucket", ""),
            }
        )
        detail_frames.append(detail)
        summary_rows.append(summary)
    detail_report = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    summary_report = pd.DataFrame(summary_rows)
    return summary_report, detail_report


def add_period_deltas(summary: pd.DataFrame, period_name: str) -> pd.DataFrame:
    result = summary.copy()
    baseline = result[(result["wf_period"] == period_name) & (result["rule_name"] == BASE_RULE_NAME)].iloc[0]
    result["equity_multiple_delta_vs_period_baseline"] = (
        result["equity_multiple"] - float(baseline["equity_multiple"])
    )
    result["max_drawdown_delta_vs_period_baseline"] = (
        result["max_drawdown"] - float(baseline["max_drawdown"])
    )
    result["win_rate_delta_vs_period_baseline"] = result["win_rate"] - float(baseline["win_rate"])
    result["beats_period_baseline_multiple"] = result["equity_multiple"] > float(baseline["equity_multiple"])
    result["improves_period_drawdown"] = result["max_drawdown"] > float(baseline["max_drawdown"])
    return result


def training_score(row: pd.Series) -> float:
    multiple = float(row["equity_multiple"])
    drawdown = abs(float(row["max_drawdown"]))
    trades = int(row["executed_trade_count"])
    if trades < 10:
        return -1.0
    return multiple / (1.0 + drawdown * 3.0)


def build_walk_forward_report(train: pd.DataFrame, test: pd.DataFrame) -> pd.DataFrame:
    train = add_period_deltas(train, "train")
    test = add_period_deltas(test, "test")
    train["train_score"] = train.apply(training_score, axis=1)
    train = train.sort_values(
        ["train_score", "equity_multiple", "max_drawdown"],
        ascending=[False, False, False],
    ).reset_index(drop=True)
    train["train_rank"] = train.index + 1

    train_columns = [
        "rule_name",
        "train_rank",
        "train_score",
        "rule_group",
        "fd_ratio_bucket",
        "market_limit_down_count_bucket",
        "equity_multiple",
        "executed_trade_count",
        "win_rate",
        "max_drawdown",
        "max_loss",
        "filter_skip_count",
        "equity_multiple_delta_vs_period_baseline",
        "max_drawdown_delta_vs_period_baseline",
        "beats_period_baseline_multiple",
        "improves_period_drawdown",
    ]
    test_columns = [
        "rule_name",
        "equity_multiple",
        "executed_trade_count",
        "win_rate",
        "max_drawdown",
        "max_loss",
        "filter_skip_count",
        "equity_multiple_delta_vs_period_baseline",
        "max_drawdown_delta_vs_period_baseline",
        "beats_period_baseline_multiple",
        "improves_period_drawdown",
    ]
    train_view = train[train_columns].rename(
        columns={
            "equity_multiple": "train_equity_multiple",
            "executed_trade_count": "train_trade_count",
            "win_rate": "train_win_rate",
            "max_drawdown": "train_max_drawdown",
            "max_loss": "train_max_loss",
            "filter_skip_count": "train_filter_skip_count",
            "equity_multiple_delta_vs_period_baseline": "train_multiple_delta_vs_baseline",
            "max_drawdown_delta_vs_period_baseline": "train_drawdown_delta_vs_baseline",
            "beats_period_baseline_multiple": "train_beats_baseline_multiple",
            "improves_period_drawdown": "train_improves_baseline_drawdown",
        }
    )
    test_view = test[test_columns].rename(
        columns={
            "equity_multiple": "test_equity_multiple",
            "executed_trade_count": "test_trade_count",
            "win_rate": "test_win_rate",
            "max_drawdown": "test_max_drawdown",
            "max_loss": "test_max_loss",
            "filter_skip_count": "test_filter_skip_count",
            "equity_multiple_delta_vs_period_baseline": "test_multiple_delta_vs_baseline",
            "max_drawdown_delta_vs_period_baseline": "test_drawdown_delta_vs_baseline",
            "beats_period_baseline_multiple": "test_beats_baseline_multiple",
            "improves_period_drawdown": "test_improves_baseline_drawdown",
        }
    )
    report = train_view.merge(test_view, on="rule_name", how="left", validate="one_to_one")
    report["passes_oos_multiple"] = report["test_beats_baseline_multiple"]
    report["passes_oos_drawdown"] = report["test_improves_baseline_drawdown"]
    report["passes_oos_both"] = report["passes_oos_multiple"] & report["passes_oos_drawdown"]
    return report


def main() -> None:
    args = parse_args()
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    config = load_config(PROJECT_ROOT / args.config)
    execution_config = prepare_execution_config(config)
    all_rows = load_rows(PROJECT_ROOT / args.input, args.scenario_rank)
    rules = build_rules(all_rows)
    train_rows = slice_by_trade_date(all_rows, args.train_start, args.train_end)
    test_rows = slice_by_trade_date(all_rows, args.test_start, args.test_end)

    train_summary, train_detail = replay_rules_for_period(train_rows, rules, execution_config, "train")
    test_summary, test_detail = replay_rules_for_period(test_rows, rules, execution_config, "test")
    wf_report = build_walk_forward_report(train_summary, test_summary)
    detail = pd.concat([train_detail, test_detail], ignore_index=True)
    yearly = build_yearly(detail)

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    train_path = output_prefix.with_name(output_prefix.name + "_train_summary.csv")
    test_path = output_prefix.with_name(output_prefix.name + "_test_summary.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly.csv")
    wf_report.to_csv(summary_path, index=False, encoding="utf-8-sig")
    train_summary.to_csv(train_path, index=False, encoding="utf-8-sig")
    test_summary.to_csv(test_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    yearly.to_csv(yearly_path, index=False, encoding="utf-8-sig")

    display_columns = [
        "train_rank",
        "rule_name",
        "rule_group",
        "train_equity_multiple",
        "train_max_drawdown",
        "train_trade_count",
        "test_equity_multiple",
        "test_max_drawdown",
        "test_trade_count",
        "passes_oos_both",
    ]
    print("bj_cap_2pct 过滤规则 walk-forward 验证完成")
    print(wf_report[display_columns].head(15).to_string(index=False))
    target = wf_report[wf_report["rule_name"] == TARGET_RULE_NAME]
    if not target.empty:
        print("上一轮目标规则在 walk-forward 中的表现：")
        print(target[display_columns].to_string(index=False))
    print("报告文件：")
    print(f"- summary: {summary_path}")
    print(f"- train_summary: {train_path}")
    print(f"- test_summary: {test_path}")
    print(f"- detail: {detail_path}")
    print(f"- yearly: {yearly_path}")


if __name__ == "__main__":
    main()
