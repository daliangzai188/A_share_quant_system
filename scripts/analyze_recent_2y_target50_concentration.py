"""
拆解最近 2 年 50 倍候选方案的收益来源集中度。

文件作用：
1. 读取 recent_2y_target50_top_scenarios_trades.csv。
2. 按市场板块、个股、年份统计收益贡献。
3. 统计 TOP 盈利交易对总收益的贡献度。
4. 模拟剔除 BJ / 剔除单一股票后的复利倍数，判断是否过度依赖少数标的。

本脚本只做本地报告分析，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分析最近2年50倍候选方案收益集中度。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_target50_top_scenarios_trades.csv",
        help="逐笔交易报告路径。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/recent_2y_target50_concentration",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def compound_multiple(returns: pd.Series) -> float:
    if returns.empty:
        return 1.0
    return float((1.0 + returns.astype(float)).prod())


def prepare_executed(path: Path) -> pd.DataFrame:
    data = pd.read_csv(path, low_memory=False)
    executed = data[data["scenario_executed"] == True].copy()  # noqa: E712
    if executed.empty:
        raise RuntimeError(f"没有已成交交易: {path}")
    executed["exit_trade_date"] = executed["exit_trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    executed["trade_date"] = executed["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
    executed["year"] = executed["exit_trade_date"].str[:4]
    executed["market_segment"] = executed["market_segment"].fillna("unknown").astype(str)
    executed["name"] = executed["name"].fillna("").astype(str)
    executed["account_return_factor"] = 1.0 + executed["dynamic_account_return"].astype(float)
    return executed.sort_values(["scenario_rank", "selected_order"]).reset_index(drop=True)


def build_segment_report(executed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (rank, segment), group in executed.groupby(["scenario_rank", "market_segment"]):
        returns = group["dynamic_account_return"].astype(float)
        rows.append(
            {
                "scenario_rank": rank,
                "market_segment": segment,
                "trade_count": int(len(group)),
                "win_rate": float((returns > 0).mean()),
                "avg_account_return": float(returns.mean()),
                "median_account_return": float(returns.median()),
                "segment_compound_multiple": compound_multiple(returns),
                "sum_account_return": float(returns.sum()),
                "max_profit": float(returns.max()),
                "max_loss": float(returns.min()),
            }
        )
    return pd.DataFrame(rows).sort_values(["scenario_rank", "segment_compound_multiple"], ascending=[True, False])


def build_stock_report(executed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (rank, ts_code, name, segment), group in executed.groupby(["scenario_rank", "ts_code", "name", "market_segment"]):
        returns = group["dynamic_account_return"].astype(float)
        rows.append(
            {
                "scenario_rank": rank,
                "ts_code": ts_code,
                "name": name,
                "market_segment": segment,
                "trade_count": int(len(group)),
                "win_rate": float((returns > 0).mean()),
                "stock_compound_multiple": compound_multiple(returns),
                "sum_account_return": float(returns.sum()),
                "avg_account_return": float(returns.mean()),
                "max_profit": float(returns.max()),
                "max_loss": float(returns.min()),
            }
        )
    return pd.DataFrame(rows).sort_values(["scenario_rank", "stock_compound_multiple"], ascending=[True, False])


def build_top_trade_report(executed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for rank, group in executed.groupby("scenario_rank"):
        base_multiple = compound_multiple(group["dynamic_account_return"])
        sorted_group = group.sort_values("dynamic_account_return", ascending=False).copy()
        for top_n in [1, 3, 5, 10]:
            removed = sorted_group.iloc[top_n:]
            removed_multiple = compound_multiple(removed["dynamic_account_return"])
            top_returns = sorted_group.head(top_n)["dynamic_account_return"].astype(float)
            rows.append(
                {
                    "scenario_rank": rank,
                    "top_n": top_n,
                    "base_multiple": base_multiple,
                    "multiple_without_top_n": removed_multiple,
                    "multiple_drop_pct": 1 - removed_multiple / base_multiple if base_multiple else 0.0,
                    "top_n_sum_account_return": float(top_returns.sum()),
                    "top_n_max_account_return": float(top_returns.max()) if len(top_returns) else 0.0,
                }
            )
    return pd.DataFrame(rows)


def build_exclusion_report(executed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for rank, group in executed.groupby("scenario_rank"):
        base_multiple = compound_multiple(group["dynamic_account_return"])
        bj_removed = group[group["market_segment"] != "bj"]
        rows.append(
            {
                "scenario_rank": rank,
                "exclusion": "exclude_bj",
                "base_trade_count": int(len(group)),
                "remaining_trade_count": int(len(bj_removed)),
                "base_multiple": base_multiple,
                "remaining_multiple": compound_multiple(bj_removed["dynamic_account_return"]),
            }
        )
        for ts_code, stock_group in group.groupby("ts_code"):
            remaining = group[group["ts_code"] != ts_code]
            rows.append(
                {
                    "scenario_rank": rank,
                    "exclusion": f"exclude_stock_{ts_code}",
                    "base_trade_count": int(len(group)),
                    "remaining_trade_count": int(len(remaining)),
                    "base_multiple": base_multiple,
                    "remaining_multiple": compound_multiple(remaining["dynamic_account_return"]),
                    "removed_trade_count": int(len(stock_group)),
                    "removed_sum_account_return": float(stock_group["dynamic_account_return"].astype(float).sum()),
                }
            )
    report = pd.DataFrame(rows)
    report["multiple_drop_pct"] = 1 - report["remaining_multiple"] / report["base_multiple"]
    return report.sort_values(["scenario_rank", "multiple_drop_pct"], ascending=[True, False])


def build_year_segment_report(executed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (rank, year, segment), group in executed.groupby(["scenario_rank", "year", "market_segment"]):
        returns = group["dynamic_account_return"].astype(float)
        rows.append(
            {
                "scenario_rank": rank,
                "year": year,
                "market_segment": segment,
                "trade_count": int(len(group)),
                "win_rate": float((returns > 0).mean()),
                "compound_multiple": compound_multiple(returns),
                "sum_account_return": float(returns.sum()),
                "avg_account_return": float(returns.mean()),
                "max_profit": float(returns.max()),
                "max_loss": float(returns.min()),
            }
        )
    return pd.DataFrame(rows).sort_values(["scenario_rank", "year", "compound_multiple"], ascending=[True, True, False])


def main() -> None:
    args = parse_args()
    input_path = PROJECT_ROOT / args.input
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    executed = prepare_executed(input_path)
    segment = build_segment_report(executed)
    stock = build_stock_report(executed)
    top_trades = build_top_trade_report(executed)
    exclusion = build_exclusion_report(executed)
    year_segment = build_year_segment_report(executed)

    segment_path = output_prefix.with_name(output_prefix.name + "_by_segment.csv")
    stock_path = output_prefix.with_name(output_prefix.name + "_by_stock.csv")
    top_trades_path = output_prefix.with_name(output_prefix.name + "_top_trades.csv")
    exclusion_path = output_prefix.with_name(output_prefix.name + "_exclusion.csv")
    year_segment_path = output_prefix.with_name(output_prefix.name + "_year_segment.csv")

    segment.to_csv(segment_path, index=False, encoding="utf-8-sig")
    stock.to_csv(stock_path, index=False, encoding="utf-8-sig")
    top_trades.to_csv(top_trades_path, index=False, encoding="utf-8-sig")
    exclusion.to_csv(exclusion_path, index=False, encoding="utf-8-sig")
    year_segment.to_csv(year_segment_path, index=False, encoding="utf-8-sig")

    print("最近2年50倍候选收益集中度拆解完成")
    print("\n按市场板块 TOP：")
    print(segment.groupby("scenario_rank").head(5).to_string(index=False))
    print("\n剔除影响 TOP：")
    print(exclusion.groupby("scenario_rank").head(5).to_string(index=False))
    print("\n报告文件：")
    print(f"- segment: {segment_path}")
    print(f"- stock: {stock_path}")
    print(f"- top_trades: {top_trades_path}")
    print(f"- exclusion: {exclusion_path}")
    print(f"- year_segment: {year_segment_path}")


if __name__ == "__main__":
    main()
