"""
对比两个已审计策略的稳定性。

文件作用：
1. 读取 audit_recent_2y_best_strategy.py 生成的 summary/yearly/monthly/segment/drawdown/trades 文件。
2. 对比资金倍数、胜率、最大回撤、负收益月份、最大连续亏损、年度表现和回撤窗口。
3. 输出稳定性汇总 CSV、月度对比 CSV、年度对比 CSV 和 Markdown 报告。

本脚本只读取本地报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对比两个策略审计报告的稳定性。")
    parser.add_argument("--left-name", required=True, help="左侧策略名称。")
    parser.add_argument("--left-prefix", required=True, help="左侧策略审计文件前缀。")
    parser.add_argument("--right-name", required=True, help="右侧策略名称。")
    parser.add_argument("--right-prefix", required=True, help="右侧策略审计文件前缀。")
    parser.add_argument(
        "--output-prefix",
        default="reports/strategy_stability_compare",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def load_report(prefix: Path) -> dict[str, pd.DataFrame]:
    return {
        "summary": pd.read_csv(prefix.with_name(prefix.name + "_summary.csv")),
        "yearly": pd.read_csv(prefix.with_name(prefix.name + "_yearly.csv")),
        "monthly": pd.read_csv(prefix.with_name(prefix.name + "_monthly.csv")),
        "segment": pd.read_csv(prefix.with_name(prefix.name + "_segment.csv")),
        "drawdown": pd.read_csv(prefix.with_name(prefix.name + "_drawdown_window.csv")),
        "trades": pd.read_csv(prefix.with_name(prefix.name + "_trades.csv"), low_memory=False),
    }


def summarize(name: str, report: dict[str, pd.DataFrame]) -> dict[str, object]:
    summary = report["summary"].iloc[0]
    monthly = report["monthly"].copy()
    trades = report["trades"].copy()
    returns = pd.to_numeric(trades["dynamic_account_return"], errors="coerce").fillna(0.0)
    negative_months = monthly[pd.to_numeric(monthly["period_return"], errors="coerce") < 0]
    positive_months = monthly[pd.to_numeric(monthly["period_return"], errors="coerce") > 0]
    return {
        "strategy": name,
        "equity_multiple": float(summary["equity_multiple"]),
        "final_equity": float(summary["final_equity"]),
        "executed_trade_count": int(summary["executed_trade_count"]),
        "win_rate": float(summary["win_rate"]),
        "avg_account_return": float(summary["avg_account_return"]),
        "median_account_return": float(summary["median_account_return"]),
        "max_drawdown": float(summary["max_drawdown"]),
        "max_profit": float(summary["max_profit"]),
        "max_loss": float(summary["max_loss"]),
        "max_consecutive_losses": int(summary["max_consecutive_losses"]),
        "negative_month_count": int(len(negative_months)),
        "positive_month_count": int(len(positive_months)),
        "worst_month": str(negative_months.sort_values("period_return").iloc[0]["month"]) if not negative_months.empty else "",
        "worst_month_return": float(negative_months["period_return"].min()) if not negative_months.empty else 0.0,
        "best_month": str(monthly.sort_values("period_return", ascending=False).iloc[0]["month"]) if not monthly.empty else "",
        "best_month_return": float(monthly["period_return"].max()) if not monthly.empty else 0.0,
        "negative_trade_count": int((returns <= 0).sum()),
        "positive_trade_count": int((returns > 0).sum()),
        "st_trade_count": int(summary.get("st_trade_count", 0)),
        "limit_down_blocked_trade_count": int(summary.get("limit_down_blocked_trade_count", 0)),
        "avg_buy_slippage": float(summary.get("avg_buy_slippage", 0.0)),
        "avg_sell_slippage": float(summary.get("avg_sell_slippage", 0.0)),
    }


def add_strategy(data: pd.DataFrame, strategy: str) -> pd.DataFrame:
    result = data.copy()
    result.insert(0, "strategy", strategy)
    return result


def write_markdown(
    output_path: Path,
    summary: pd.DataFrame,
    yearly: pd.DataFrame,
    monthly: pd.DataFrame,
    left_name: str,
    right_name: str,
) -> None:
    columns = [
        "strategy",
        "equity_multiple",
        "executed_trade_count",
        "win_rate",
        "max_drawdown",
        "max_loss",
        "max_consecutive_losses",
        "negative_month_count",
        "worst_month",
        "worst_month_return",
    ]
    yearly_columns = [
        "strategy",
        "year",
        "trade_count",
        "period_return",
        "compound_multiple",
        "win_rate",
        "max_drawdown",
        "max_consecutive_losses",
    ]
    monthly_columns = [
        "strategy",
        "month",
        "trade_count",
        "period_return",
        "compound_multiple",
        "win_rate",
        "max_drawdown",
        "max_consecutive_losses",
    ]
    content = f"""# 策略稳定性对比

对比对象：

- {left_name}
- {right_name}

本报告只基于本地审计报告，不调用外部接口，不接实盘。

## 总体稳定性

{summary[columns].to_markdown(index=False)}

## 年度对比

{yearly[yearly_columns].to_markdown(index=False)}

## 负收益月份

{monthly[monthly["period_return"] < 0][monthly_columns].to_markdown(index=False)}

## 结论提示

稳定性报告只用于判断哪个版本更适合进入模拟盘。实盘前仍需要分钟 K、盘口成交、集合竞价、滑点、跌停卖出和模拟盘连续运行验证。
"""
    output_path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    left_prefix = PROJECT_ROOT / args.left_prefix
    right_prefix = PROJECT_ROOT / args.right_prefix
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    left = load_report(left_prefix)
    right = load_report(right_prefix)

    summary = pd.DataFrame(
        [
            summarize(args.left_name, left),
            summarize(args.right_name, right),
        ]
    )
    yearly = pd.concat(
        [
            add_strategy(left["yearly"], args.left_name),
            add_strategy(right["yearly"], args.right_name),
        ],
        ignore_index=True,
    )
    monthly = pd.concat(
        [
            add_strategy(left["monthly"], args.left_name),
            add_strategy(right["monthly"], args.right_name),
        ],
        ignore_index=True,
    )
    segment = pd.concat(
        [
            add_strategy(left["segment"], args.left_name),
            add_strategy(right["segment"], args.right_name),
        ],
        ignore_index=True,
    )

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly.csv")
    monthly_path = output_prefix.with_name(output_prefix.name + "_monthly.csv")
    segment_path = output_prefix.with_name(output_prefix.name + "_segment.csv")
    markdown_path = output_prefix.with_suffix(".md")

    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    yearly.to_csv(yearly_path, index=False, encoding="utf-8-sig")
    monthly.to_csv(monthly_path, index=False, encoding="utf-8-sig")
    segment.to_csv(segment_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, yearly, monthly, args.left_name, args.right_name)

    print("策略稳定性对比完成：")
    print(f"- summary: {summary_path}")
    print(f"- yearly: {yearly_path}")
    print(f"- monthly: {monthly_path}")
    print(f"- segment: {segment_path}")
    print(f"- markdown: {markdown_path}")


if __name__ == "__main__":
    main()
