"""
分析最近 2 年方案2的 BJ 成交容量分布。

文件作用：
1. 读取 recent_2y_target50_top_scenarios_trades.csv。
2. 统计每笔交易目标买入金额占当日成交额比例。
3. 重点分析 BJ 交易在 1% / 2% / 3% / 5% 容量阈值下会被压缩多少。
4. 输出最可能成交困难的交易清单。

本脚本只读本地报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = [0.01, 0.02, 0.03, 0.05]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分析 BJ 成交容量分布。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_target50_top_scenarios_trades.csv",
        help="逐笔交易报告路径。",
    )
    parser.add_argument("--scenario-rank", type=int, default=2, help="默认分析方案2。")
    parser.add_argument(
        "--output-prefix",
        default="reports/recent_2y_bj_capacity_distribution",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def load_executed(path: Path, scenario_rank: int) -> pd.DataFrame:
    data = pd.read_csv(path, low_memory=False)
    data = data[(data["scenario_rank"] == scenario_rank) & (data["scenario_executed"] == True)].copy()  # noqa: E712
    if data.empty:
        raise RuntimeError(f"没有已成交交易: scenario_rank={scenario_rank}, {path}")
    for column in [
        "target_buy_amount",
        "actual_buy_amount",
        "buy_day_amount_yuan",
        "sell_day_amount_yuan",
        "buy_amount_ratio",
        "sell_amount_ratio",
        "actual_position_pct",
        "dynamic_account_return",
        "equity_before",
        "equity_after",
    ]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data["target_buy_amount_ratio"] = data["target_buy_amount"] / data["buy_day_amount_yuan"]
    data["actual_buy_amount_ratio"] = data["actual_buy_amount"] / data["buy_day_amount_yuan"]
    data["trade_date"] = data["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    data["buy_trade_date"] = data["buy_trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    data["exit_trade_date"] = data["exit_trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    data["year"] = data["exit_trade_date"].str[:4]
    data["market_segment"] = data["market_segment"].fillna("unknown").astype(str)
    return data.sort_values("selected_order").reset_index(drop=True)


def build_summary(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for segment, group in data.groupby("market_segment"):
        target_ratio = group["target_buy_amount_ratio"]
        actual_ratio = group["actual_buy_amount_ratio"]
        row = {
            "market_segment": segment,
            "trade_count": int(len(group)),
            "avg_target_buy_amount": float(group["target_buy_amount"].mean()),
            "max_target_buy_amount": float(group["target_buy_amount"].max()),
            "avg_actual_buy_amount": float(group["actual_buy_amount"].mean()),
            "max_actual_buy_amount": float(group["actual_buy_amount"].max()),
            "avg_target_buy_ratio": float(target_ratio.mean()),
            "max_target_buy_ratio": float(target_ratio.max()),
            "avg_actual_buy_ratio": float(actual_ratio.mean()),
            "max_actual_buy_ratio": float(actual_ratio.max()),
            "avg_sell_ratio": float(group["sell_amount_ratio"].mean()),
            "max_sell_ratio": float(group["sell_amount_ratio"].max()),
            "avg_position_pct": float(group["actual_position_pct"].mean()),
            "avg_account_return": float(group["dynamic_account_return"].mean()),
            "win_rate": float((group["dynamic_account_return"] > 0).mean()),
        }
        for threshold in THRESHOLDS:
            row[f"target_over_{int(threshold * 100)}pct_count"] = int((target_ratio > threshold).sum())
            row[f"target_over_{int(threshold * 100)}pct_rate"] = float((target_ratio > threshold).mean())
            simulated_position_pct = (group["buy_day_amount_yuan"] * threshold / group["equity_before"]).clip(upper=0.8)
            row[f"avg_position_if_cap_{int(threshold * 100)}pct"] = float(simulated_position_pct.mean())
        rows.append(row)
    return pd.DataFrame(rows).sort_values("trade_count", ascending=False)


def build_threshold_report(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for threshold in THRESHOLDS:
        for segment, group in data.groupby("market_segment"):
            capped_buy_amount = (group["buy_day_amount_yuan"] * threshold).clip(upper=group["target_buy_amount"])
            capped_position_pct = capped_buy_amount / group["equity_before"]
            rows.append(
                {
                    "capacity_threshold": threshold,
                    "market_segment": segment,
                    "trade_count": int(len(group)),
                    "limited_trade_count": int((group["target_buy_amount_ratio"] > threshold).sum()),
                    "limited_trade_rate": float((group["target_buy_amount_ratio"] > threshold).mean()),
                    "avg_position_pct_if_capped": float(capped_position_pct.mean()),
                    "min_position_pct_if_capped": float(capped_position_pct.min()),
                    "avg_buy_amount_if_capped": float(capped_buy_amount.mean()),
                    "max_buy_amount_if_capped": float(capped_buy_amount.max()),
                }
            )
    return pd.DataFrame(rows).sort_values(["capacity_threshold", "market_segment"])


def build_trade_pressure_report(data: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "selected_order",
        "trade_date",
        "buy_trade_date",
        "exit_trade_date",
        "ts_code",
        "name",
        "market_segment",
        "equity_before",
        "target_buy_amount",
        "actual_buy_amount",
        "buy_day_amount_yuan",
        "sell_day_amount_yuan",
        "target_buy_amount_ratio",
        "actual_buy_amount_ratio",
        "sell_amount_ratio",
        "actual_position_pct",
        "dynamic_account_return",
        "equity_after",
    ]
    result = data[columns].copy()
    for threshold in THRESHOLDS:
        pct = int(threshold * 100)
        result[f"target_over_{pct}pct"] = result["target_buy_amount_ratio"] > threshold
        result[f"position_if_cap_{pct}pct"] = (
            result["buy_day_amount_yuan"] * threshold / result["equity_before"]
        ).clip(upper=0.8)
    return result.sort_values(["market_segment", "target_buy_amount_ratio"], ascending=[True, False])


def build_bj_pressure_top(data: pd.DataFrame) -> pd.DataFrame:
    bj = data[data["market_segment"] == "bj"].copy()
    if bj.empty:
        return bj
    return build_trade_pressure_report(bj).head(30)


def main() -> None:
    args = parse_args()
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    data = load_executed(PROJECT_ROOT / args.input, args.scenario_rank)

    summary = build_summary(data)
    threshold = build_threshold_report(data)
    pressure = build_trade_pressure_report(data)
    bj_top = build_bj_pressure_top(data)

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    threshold_path = output_prefix.with_name(output_prefix.name + "_thresholds.csv")
    pressure_path = output_prefix.with_name(output_prefix.name + "_trades.csv")
    bj_top_path = output_prefix.with_name(output_prefix.name + "_bj_top_pressure.csv")

    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    threshold.to_csv(threshold_path, index=False, encoding="utf-8-sig")
    pressure.to_csv(pressure_path, index=False, encoding="utf-8-sig")
    bj_top.to_csv(bj_top_path, index=False, encoding="utf-8-sig")

    print("BJ 成交容量分布分析完成")
    print("\n按市场板块汇总：")
    print(summary.to_string(index=False))
    print("\nBJ 压力最高交易 TOP：")
    if not bj_top.empty:
        show_cols = [
            "trade_date",
            "ts_code",
            "name",
            "equity_before",
            "target_buy_amount",
            "buy_day_amount_yuan",
            "target_buy_amount_ratio",
            "actual_buy_amount_ratio",
            "dynamic_account_return",
        ]
        print(bj_top[show_cols].to_string(index=False))
    print("\n报告文件：")
    print(f"- summary: {summary_path}")
    print(f"- thresholds: {threshold_path}")
    print(f"- trades: {pressure_path}")
    print(f"- bj_top_pressure: {bj_top_path}")


if __name__ == "__main__":
    main()
