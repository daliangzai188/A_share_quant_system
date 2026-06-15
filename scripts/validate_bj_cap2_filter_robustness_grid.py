"""
验证 bj_cap_2pct 前视过滤规则的分桶鲁棒性。

文件作用：
1. 固定 bj_cap_2pct 完整回放口径。
2. 分别测试 fd_ratio_bucket 单独过滤、market_limit_down_count_bucket 单独过滤。
3. 测试 fd_ratio_bucket × market_limit_down_count_bucket 的全部交叉过滤。
4. 输出每个过滤组合的复利、回撤、年度表现和相对基准变化。

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

BASE_RULE_NAME = "bj_cap_2pct_full_replay"
TARGET_RULE_NAME = "filter_fd_1pct_2pct__market_down_lt_5"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证 bj_cap_2pct 分桶过滤鲁棒性。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_target50_top_scenarios_trades.csv",
        help="最近2年方案逐信号交易明细。",
    )
    parser.add_argument("--scenario-rank", type=int, default=2, help="默认回放方案2。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument(
        "--output-prefix",
        default="reports/bj_cap2_filter_robustness_grid",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def safe_name(value: str) -> str:
    return value.replace(".", "_").replace("%", "pct").replace("-", "_").replace(" ", "_")


def ordered_values(values: pd.Series, preferred_order: list[str]) -> list[str]:
    available = set(values.dropna().astype(str))
    ordered = [value for value in preferred_order if value in available]
    ordered.extend(sorted(available - set(ordered)))
    return ordered


def build_rules(rows: pd.DataFrame) -> list[dict[str, Any]]:
    fd_order = [
        "lt_0_1pct",
        "0_1pct_0_3pct",
        "0_3pct_0_5pct",
        "0_5pct_1pct",
        "1pct_2pct",
        "2pct_5pct",
        "gte_5pct",
    ]
    down_order = ["lt_5", "5_15", "15_30", "30_60", "gte_60"]
    fd_values = ordered_values(rows["fd_ratio_bucket"], fd_order)
    down_values = ordered_values(rows["market_limit_down_count_bucket"], down_order)

    rules: list[dict[str, Any]] = [
        {
            "rule_name": BASE_RULE_NAME,
            "rule_group": "baseline",
            "fd_ratio_bucket": "",
            "market_limit_down_count_bucket": "",
            "description": "完整回放基准：BJ容量2%，不额外过滤",
            "conditions": tuple(),
        }
    ]

    for fd_value in fd_values:
        rules.append(
            {
                "rule_name": f"filter_fd_{safe_name(fd_value)}",
                "rule_group": "fd_single",
                "fd_ratio_bucket": fd_value,
                "market_limit_down_count_bucket": "",
                "description": f"过滤 fd_ratio_bucket={fd_value}",
                "conditions": (("fd_ratio_bucket", fd_value),),
            }
        )

    for down_value in down_values:
        rules.append(
            {
                "rule_name": f"filter_market_down_{safe_name(down_value)}",
                "rule_group": "market_down_single",
                "fd_ratio_bucket": "",
                "market_limit_down_count_bucket": down_value,
                "description": f"过滤 market_limit_down_count_bucket={down_value}",
                "conditions": (("market_limit_down_count_bucket", down_value),),
            }
        )

    for fd_value in fd_values:
        for down_value in down_values:
            rules.append(
                {
                    "rule_name": f"filter_fd_{safe_name(fd_value)}__market_down_{safe_name(down_value)}",
                    "rule_group": "fd_x_market_down",
                    "fd_ratio_bucket": fd_value,
                    "market_limit_down_count_bucket": down_value,
                    "description": (
                        f"过滤 fd_ratio_bucket={fd_value} 且 "
                        f"market_limit_down_count_bucket={down_value}"
                    ),
                    "conditions": (
                        ("fd_ratio_bucket", fd_value),
                        ("market_limit_down_count_bucket", down_value),
                    ),
                }
            )
    return rules


def replay_all_rules(
    rows: pd.DataFrame,
    rules: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    risk = config.get("risk", {})
    opt = config.get("realistic_condition_strategy_search", {})
    initial_cash = float(opt.get("initial_cash", 500000))
    position_pct = float(opt.get("position_pct", 0.8))
    default_capacity = float(opt.get("max_buy_amount_ratio", 0.05))
    bj_capacity = 0.02
    slippage_tiers = list(opt.get("slippage_tiers", []))
    fee_rate_without_slippage = (
        float(risk.get("commission_rate", 0.0003))
        + float(risk.get("transfer_fee_rate", 0.00001))
        + float(risk.get("commission_rate", 0.0003))
        + float(risk.get("transfer_fee_rate", 0.00001))
        + float(risk.get("stamp_tax_rate", 0.001))
    )

    detail_frames = []
    summary_rows = []
    for rule in rules:
        detail = replay_rule(
            rows=rows,
            rule=rule,
            initial_cash=initial_cash,
            position_pct=position_pct,
            default_capacity=default_capacity,
            bj_capacity=bj_capacity,
            slippage_tiers=slippage_tiers,
            fee_rate_without_slippage=fee_rate_without_slippage,
        )
        summary = summarize_detail(detail, initial_cash)
        summary.update(
            {
                "rule_group": rule["rule_group"],
                "fd_ratio_bucket": rule["fd_ratio_bucket"],
                "market_limit_down_count_bucket": rule["market_limit_down_count_bucket"],
            }
        )
        detail_frames.append(detail)
        summary_rows.append(summary)
    return pd.DataFrame(summary_rows), pd.concat(detail_frames, ignore_index=True)


def add_baseline_deltas(summary: pd.DataFrame) -> pd.DataFrame:
    result = summary.copy()
    baseline = result[result["rule_name"] == BASE_RULE_NAME].iloc[0]
    result["equity_multiple_delta"] = result["equity_multiple"] - float(baseline["equity_multiple"])
    result["max_drawdown_delta"] = result["max_drawdown"] - float(baseline["max_drawdown"])
    result["win_rate_delta"] = result["win_rate"] - float(baseline["win_rate"])
    result["trade_count_delta"] = result["executed_trade_count"] - int(baseline["executed_trade_count"])
    result["beats_baseline_multiple"] = result["equity_multiple"] > float(baseline["equity_multiple"])
    result["improves_drawdown"] = result["max_drawdown"] > float(baseline["max_drawdown"])
    return result


def build_pair_matrix(summary: pd.DataFrame) -> pd.DataFrame:
    pairs = summary[summary["rule_group"] == "fd_x_market_down"].copy()
    if pairs.empty:
        return pairs
    return pairs[
        [
            "fd_ratio_bucket",
            "market_limit_down_count_bucket",
            "equity_multiple",
            "max_drawdown",
            "executed_trade_count",
            "win_rate",
            "filter_skip_count",
            "equity_multiple_delta",
            "max_drawdown_delta",
            "beats_baseline_multiple",
            "improves_drawdown",
        ]
    ].sort_values(["equity_multiple", "max_drawdown"], ascending=[False, False])


def build_target_neighborhood(summary: pd.DataFrame) -> pd.DataFrame:
    fd_neighbors = {"0_5pct_1pct", "1pct_2pct", "2pct_5pct"}
    down_neighbors = {"lt_5", "5_15"}
    pairs = summary[summary["rule_group"] == "fd_x_market_down"].copy()
    target_area = pairs[
        pairs["fd_ratio_bucket"].isin(fd_neighbors)
        & pairs["market_limit_down_count_bucket"].isin(down_neighbors)
    ].copy()
    return target_area.sort_values(["fd_ratio_bucket", "market_limit_down_count_bucket"])


def main() -> None:
    args = parse_args()
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    config = load_config(PROJECT_ROOT / args.config)
    rows = load_rows(PROJECT_ROOT / args.input, args.scenario_rank)
    rules = build_rules(rows)
    summary, detail = replay_all_rules(rows, rules, config)
    summary = add_baseline_deltas(summary).sort_values(
        ["equity_multiple", "max_drawdown"],
        ascending=[False, False],
    )
    yearly = build_yearly(detail)
    pair_matrix = build_pair_matrix(summary)
    target_neighborhood = build_target_neighborhood(summary)

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly.csv")
    pair_matrix_path = output_prefix.with_name(output_prefix.name + "_pair_matrix.csv")
    target_neighborhood_path = output_prefix.with_name(output_prefix.name + "_target_neighborhood.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    yearly.to_csv(yearly_path, index=False, encoding="utf-8-sig")
    pair_matrix.to_csv(pair_matrix_path, index=False, encoding="utf-8-sig")
    target_neighborhood.to_csv(target_neighborhood_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")

    top_columns = [
        "rule_name",
        "rule_group",
        "fd_ratio_bucket",
        "market_limit_down_count_bucket",
        "equity_multiple",
        "max_drawdown",
        "executed_trade_count",
        "win_rate",
        "filter_skip_count",
        "equity_multiple_delta",
        "max_drawdown_delta",
    ]
    print("bj_cap_2pct 分桶过滤鲁棒性验证完成")
    print(summary[top_columns].head(15).to_string(index=False))
    target_row = summary[summary["rule_name"] == TARGET_RULE_NAME]
    if not target_row.empty:
        print("目标规则：")
        print(target_row[top_columns].to_string(index=False))
    print("报告文件：")
    print(f"- summary: {summary_path}")
    print(f"- yearly: {yearly_path}")
    print(f"- pair_matrix: {pair_matrix_path}")
    print(f"- target_neighborhood: {target_neighborhood_path}")
    print(f"- detail: {detail_path}")


if __name__ == "__main__":
    main()
