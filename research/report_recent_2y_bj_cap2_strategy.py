"""
生成最近 2 年 bj_cap_2pct 候选策略报告。

文件作用：
1. 固定候选策略版本：方案2 + BJ 容量上限 2%。
2. 输出正式策略说明、全区间指标、2024-2025 观察期、2026 样本外验证。
3. 输出逐笔样本外交易和 Markdown 策略卡片。

本脚本只读取本地回测报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
TARGET_RULE = "bj_cap_2pct"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 bj_cap_2pct 候选策略报告。")
    parser.add_argument(
        "--summary",
        default="reports/recent_2y_bj_filter_rules_summary.csv",
        help="BJ 规则回测汇总报告。",
    )
    parser.add_argument(
        "--yearly",
        default="reports/recent_2y_bj_filter_rules_yearly.csv",
        help="BJ 规则年度报告。",
    )
    parser.add_argument(
        "--detail",
        default="reports/recent_2y_bj_filter_rules_detail.csv",
        help="BJ 规则逐笔明细。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/recent_2y_bj_cap2_strategy",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def max_consecutive_losses(returns: pd.Series) -> int:
    current = 0
    result = 0
    for value in returns:
        if value <= 0:
            current += 1
            result = max(result, current)
        else:
            current = 0
    return result


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def load_rule_detail(path: Path) -> pd.DataFrame:
    data = pd.read_csv(path, low_memory=False)
    data = data[data["rule_name"] == TARGET_RULE].copy()
    if data.empty:
        raise RuntimeError(f"没有找到 {TARGET_RULE} 明细: {path}")
    data["trade_date"] = data["trade_date"].map(normalize_date)
    data["buy_trade_date"] = data["buy_trade_date"].map(normalize_date)
    data["exit_trade_date"] = data["exit_trade_date"].map(normalize_date)
    data["year"] = data["exit_trade_date"].str[:4]
    data["market_segment"] = data["market_segment"].fillna("unknown").astype(str)
    return data.sort_values("selected_order").reset_index(drop=True)


def period_metrics(data: pd.DataFrame, period_name: str, start_year: str, end_year: str) -> dict:
    executed = data[
        (data["rule_executed"] == True)  # noqa: E712
        & (data["year"] >= start_year)
        & (data["year"] <= end_year)
    ].copy()
    skipped = data[
        (data["rule_executed"] != True)  # noqa: E712
        & (data["year"] >= start_year)
        & (data["year"] <= end_year)
    ].copy()
    if executed.empty:
        return {
            "period": period_name,
            "start_year": start_year,
            "end_year": end_year,
            "executed_trade_count": 0,
        }
    first_equity = float(executed["rule_equity_before"].iloc[0])
    last_equity = float(executed["rule_equity_after"].iloc[-1])
    returns = executed["rule_account_return"].astype(float)
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    return {
        "period": period_name,
        "start_year": start_year,
        "end_year": end_year,
        "first_equity": first_equity,
        "last_equity": last_equity,
        "equity_multiple": last_equity / first_equity if first_equity else 0.0,
        "period_return": last_equity / first_equity - 1 if first_equity else 0.0,
        "executed_trade_count": int(len(executed)),
        "skipped_count": int(len(skipped)),
        "win_rate": float((returns > 0).mean()),
        "avg_account_return": float(returns.mean()),
        "median_account_return": float(returns.median()),
        "max_profit": float(returns.max()),
        "max_loss": float(returns.min()),
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_drawdown": max_drawdown(executed["rule_equity_after"]),
        "max_consecutive_losses": max_consecutive_losses(returns),
        "avg_actual_position_pct": float(executed["rule_actual_position_pct"].mean()),
        "avg_buy_slippage": float(executed["rule_buy_slippage"].mean()),
        "avg_sell_slippage": float(executed["rule_sell_slippage"].mean()),
        "bj_trade_count": int((executed["market_segment"] == "bj").sum()),
        "bj_trade_rate": float((executed["market_segment"] == "bj").mean()),
    }


def build_segment_split(data: pd.DataFrame) -> pd.DataFrame:
    executed = data[data["rule_executed"] == True].copy()  # noqa: E712
    rows = []
    for (period, segment), group in executed.assign(
        period=executed["year"].map(lambda year: "train_2024_2025" if str(year) in {"2024", "2025"} else "test_2026")
    ).groupby(["period", "market_segment"]):
        returns = group["rule_account_return"].astype(float)
        rows.append(
            {
                "period": period,
                "market_segment": segment,
                "trade_count": int(len(group)),
                "win_rate": float((returns > 0).mean()),
                "compound_multiple": float((1.0 + returns).prod()),
                "avg_account_return": float(returns.mean()),
                "max_profit": float(returns.max()),
                "max_loss": float(returns.min()),
            }
        )
    return pd.DataFrame(rows).sort_values(["period", "compound_multiple"], ascending=[True, False])


def write_markdown(path: Path, summary_row: pd.Series, period: pd.DataFrame, yearly: pd.DataFrame) -> None:
    train = period[period["period"] == "train_2024_2025"].iloc[0]
    test = period[period["period"] == "test_2026"].iloc[0]
    content = f"""# bj_cap_2pct 候选策略报告

## 策略定义

- 策略版本：方案2 + BJ 容量上限 2%
- 条件：`market_leader_rank_bucket=rank_gt_30`
- 条件：`segment_emotion_state_bucket=ice_point`
- 条件：`volume_ratio_bucket=1_2`
- 排序：`turnover_desc`
- 买入：T+1 开盘买入，涨停不可买则拒绝
- 卖出：T+2 收盘卖出，跌停不可卖则按保守规则处理
- 仓位：目标 80%
- BJ 容量：最多占买入日成交额 2%
- 费用：佣金、过户费、印花税、动态滑点

## 全区间结果

- 初始资金：500,000
- 最终资金：{summary_row['final_equity']:.2f}
- 总复利：{summary_row['equity_multiple']:.2f} 倍
- 成交笔数：{int(summary_row['executed_trade_count'])}
- 胜率：{summary_row['win_rate']:.2%}
- 最大回撤：{summary_row['max_drawdown']:.2%}
- 平均仓位：{summary_row['avg_actual_position_pct']:.2%}
- 平均买入滑点：{summary_row['avg_buy_slippage']:.2%}
- 平均卖出滑点：{summary_row['avg_sell_slippage']:.2%}

## 样本拆分

- 观察期 2024-2025：{train['equity_multiple']:.2f} 倍，成交 {int(train['executed_trade_count'])} 笔，最大回撤 {train['max_drawdown']:.2%}
- 样本外 2026：{test['equity_multiple']:.2f} 倍，成交 {int(test['executed_trade_count'])} 笔，最大回撤 {test['max_drawdown']:.2%}

## 年度结果

{yearly.to_markdown(index=False)}

## 风险结论

该策略在 2026 样本外仍为正收益，但收益仍依赖 BJ 高弹性交易和较高波动。当前结论只能进入继续验证阶段，不能直接用于实盘。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    summary = pd.read_csv(PROJECT_ROOT / args.summary, low_memory=False)
    yearly = pd.read_csv(PROJECT_ROOT / args.yearly, low_memory=False)
    detail = load_rule_detail(PROJECT_ROOT / args.detail)

    summary_row = summary[summary["rule_name"] == TARGET_RULE].iloc[0]
    yearly_rule = yearly[yearly["rule_name"] == TARGET_RULE].copy()
    period = pd.DataFrame(
        [
            period_metrics(detail, "train_2024_2025", "2024", "2025"),
            period_metrics(detail, "test_2026", "2026", "2026"),
        ]
    )
    segment_split = build_segment_split(detail)
    test_trades = detail[(detail["rule_executed"] == True) & (detail["year"] == "2026")].copy()  # noqa: E712

    period_path = output_prefix.with_name(output_prefix.name + "_periods.csv")
    segment_path = output_prefix.with_name(output_prefix.name + "_segment_split.csv")
    test_trades_path = output_prefix.with_name(output_prefix.name + "_2026_trades.csv")
    markdown_path = output_prefix.with_suffix(".md")

    period.to_csv(period_path, index=False, encoding="utf-8-sig")
    segment_split.to_csv(segment_path, index=False, encoding="utf-8-sig")
    test_trades.to_csv(test_trades_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary_row, period, yearly_rule)

    print("bj_cap_2pct 候选策略报告已生成")
    print("\n样本拆分：")
    print(period.to_string(index=False))
    print("\n报告文件：")
    print(f"- periods: {period_path}")
    print(f"- segment_split: {segment_path}")
    print(f"- 2026_trades: {test_trades_path}")
    print(f"- markdown: {markdown_path}")


if __name__ == "__main__":
    main()
