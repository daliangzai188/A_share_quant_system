"""
审计最近 2 年完整优化的最佳策略。

文件作用：
1. 自动读取 recent_2y_full_strategy_optimization_summary.csv 中排名第一的策略。
2. 从逐笔明细中筛出该策略的所有候选、成交和跳过记录。
3. 输出收益、年度/月度、流动性占比、市场分段、回撤区间、最差/最好交易和跳过原因。
4. 用于判断当前 68.50 倍策略是否值得进入分钟数据和模拟盘验证。

本脚本只读取本地报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审计最近 2 年完整优化最佳策略。")
    parser.add_argument(
        "--summary",
        default="reports/recent_2y_full_strategy_optimization_summary.csv",
        help="完整优化汇总报告。",
    )
    parser.add_argument(
        "--detail",
        default="reports/recent_2y_full_strategy_optimization_detail.csv",
        help="完整优化逐笔明细。",
    )
    parser.add_argument("--scenario", default=None, help="指定要审计的 scenario，默认取 summary 第一名。")
    parser.add_argument(
        "--output-prefix",
        default="reports/recent_2y_best_strategy_audit",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


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


def select_scenario(summary: pd.DataFrame, scenario: str | None) -> pd.Series:
    if scenario:
        matched = summary[summary["scenario"].astype(str) == scenario].copy()
        if matched.empty:
            raise RuntimeError(f"summary 中找不到指定 scenario: {scenario}")
        return matched.iloc[0]
    ranked = summary.sort_values(
        ["hit_user_target", "ranking_score", "equity_multiple", "max_drawdown"],
        ascending=[False, False, False, True],
    )
    if ranked.empty:
        raise RuntimeError("summary 为空，无法选择最佳策略。")
    return ranked.iloc[0]


def load_scenario_detail(path: Path, scenario: str) -> pd.DataFrame:
    detail = pd.read_csv(path, low_memory=False)
    scenario_values = detail["scenario"].astype(str)
    candidate_names = [
        scenario,
        f"large_universe_sort_{scenario}_desc",
        f"large_universe_sort_{scenario}_asc",
    ]
    matched_name = next((name for name in candidate_names if (scenario_values == name).any()), "")
    if not matched_name:
        raise RuntimeError(f"detail 中找不到 scenario: {scenario}")
    detail = detail[scenario_values == matched_name].copy()
    for column in ["trade_date", "buy_trade_date", "exit_trade_date"]:
        if column in detail.columns:
            detail[column] = detail[column].map(normalize_date)
    detail["year"] = detail["exit_trade_date"].astype(str).str[:4]
    detail["month"] = detail["exit_trade_date"].astype(str).str[:6]
    detail["market_segment"] = detail["market_segment"].fillna("unknown").astype(str)
    detail["name"] = detail["name"].fillna("").astype(str)
    detail["scenario_executed"] = detail["scenario_executed"] == True  # noqa: E712
    numeric_columns = [
        "dynamic_account_return",
        "dynamic_net_return",
        "equity_before",
        "equity_after",
        "actual_buy_amount",
        "actual_position_pct",
        "buy_amount_ratio",
        "sell_amount_ratio",
        "dynamic_buy_slippage_rate",
        "dynamic_sell_slippage_rate",
        "buy_day_amount_yuan",
        "sell_day_amount_yuan",
        "limit_down_blocked_days",
    ]
    for column in numeric_columns:
        if column in detail.columns:
            detail[column] = pd.to_numeric(detail[column], errors="coerce")
    return detail.sort_values(["scenario_executed", "trade_order", "trade_date"], ascending=[False, True, True])


def build_summary_row(summary_row: pd.Series, detail: pd.DataFrame) -> pd.DataFrame:
    executed = detail[detail["scenario_executed"]].copy()
    skipped = detail[~detail["scenario_executed"]].copy()
    returns = executed["dynamic_account_return"].dropna()
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    st_mask = executed.get("is_st", pd.Series(False, index=executed.index)).astype(str).str.lower().isin({"true", "1"})
    st_mask = st_mask | executed["name"].astype(str).str.contains("ST", na=False)
    blocked_mask = executed["limit_down_blocked_days"].fillna(0) > 0
    clean_returns = executed.loc[~st_mask & ~blocked_mask, "dynamic_account_return"].dropna()
    row = {
        "scenario": summary_row["scenario"],
        "conditions": summary_row.get("conditions", ""),
        "sort_rule": summary_row.get("sort_rule", ""),
        "exit_rule": summary_row.get("exit_rule", ""),
        "initial_cash": float(summary_row["initial_cash"]),
        "final_equity": float(summary_row["final_equity"]),
        "equity_multiple": float(summary_row["equity_multiple"]),
        "selected_signal_count": int(len(detail)),
        "executed_trade_count": int(len(executed)),
        "skipped_count": int(len(skipped)),
        "buy_rejected_count": int((skipped["skip_reason"].fillna("") == "open_limit_up_unbuyable").sum()),
        "position_occupied_skip_count": int((skipped["skip_reason"].fillna("") == "position_occupied").sum()),
        "sell_unresolved_count": int(skipped["skip_reason"].fillna("").str.contains("sell|missing_exit|limit", case=False).sum()),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_drawdown": max_drawdown(executed["equity_after"]),
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(returns),
        "avg_actual_position_pct": float(executed["actual_position_pct"].mean()) if not executed.empty else 0.0,
        "avg_buy_amount_ratio": float(executed["buy_amount_ratio"].mean()) if not executed.empty else 0.0,
        "max_buy_amount_ratio": float(executed["buy_amount_ratio"].max()) if not executed.empty else 0.0,
        "avg_sell_amount_ratio": float(executed["sell_amount_ratio"].mean()) if not executed.empty else 0.0,
        "max_sell_amount_ratio": float(executed["sell_amount_ratio"].max()) if not executed.empty else 0.0,
        "avg_buy_slippage": float(executed["dynamic_buy_slippage_rate"].mean()) if not executed.empty else 0.0,
        "avg_sell_slippage": float(executed["dynamic_sell_slippage_rate"].mean()) if not executed.empty else 0.0,
        "limit_down_blocked_trade_count": int((executed["limit_down_blocked_days"].fillna(0) > 0).sum()),
        "st_trade_count": int(st_mask.sum()),
        "st_trade_compound_multiple": float((1.0 + executed.loc[st_mask, "dynamic_account_return"]).prod()) if st_mask.any() else 0.0,
        "limit_down_blocked_compound_multiple": float((1.0 + executed.loc[blocked_mask, "dynamic_account_return"]).prod()) if blocked_mask.any() else 0.0,
        "non_st_no_limit_down_blocked_trade_count": int(len(clean_returns)),
        "non_st_no_limit_down_blocked_compound_multiple": float((1.0 + clean_returns).prod()) if len(clean_returns) else 0.0,
    }
    return pd.DataFrame([row])


def build_period_report(detail: pd.DataFrame, group_column: str) -> pd.DataFrame:
    executed = detail[detail["scenario_executed"]].copy()
    rows = []
    for value, group in executed.groupby(group_column):
        returns = group["dynamic_account_return"].dropna()
        rows.append(
            {
                group_column: value,
                "trade_count": int(len(group)),
                "first_equity": float(group["equity_before"].iloc[0]),
                "last_equity": float(group["equity_after"].iloc[-1]),
                "period_return": float(group["equity_after"].iloc[-1] / group["equity_before"].iloc[0] - 1.0),
                "compound_multiple": float(group["equity_after"].iloc[-1] / group["equity_before"].iloc[0]),
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
                "median_account_return": float(returns.median()) if len(returns) else 0.0,
                "max_drawdown": max_drawdown(group["equity_after"]),
                "max_profit": float(returns.max()) if len(returns) else 0.0,
                "max_loss": float(returns.min()) if len(returns) else 0.0,
                "max_consecutive_losses": max_consecutive_losses(returns),
            }
        )
    return pd.DataFrame(rows).sort_values(group_column)


def build_segment_report(detail: pd.DataFrame) -> pd.DataFrame:
    executed = detail[detail["scenario_executed"]].copy()
    rows = []
    for segment, group in executed.groupby("market_segment"):
        returns = group["dynamic_account_return"].dropna()
        rows.append(
            {
                "market_segment": segment,
                "trade_count": int(len(group)),
                "compound_multiple": float((1.0 + returns).prod()) if len(returns) else 0.0,
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
                "median_account_return": float(returns.median()) if len(returns) else 0.0,
                "max_profit": float(returns.max()) if len(returns) else 0.0,
                "max_loss": float(returns.min()) if len(returns) else 0.0,
                "avg_buy_amount_ratio": float(group["buy_amount_ratio"].mean()),
                "max_buy_amount_ratio": float(group["buy_amount_ratio"].max()),
                "avg_sell_amount_ratio": float(group["sell_amount_ratio"].mean()),
                "max_sell_amount_ratio": float(group["sell_amount_ratio"].max()),
            }
        )
    return pd.DataFrame(rows).sort_values("compound_multiple", ascending=False)


def build_drawdown_window(detail: pd.DataFrame) -> pd.DataFrame:
    executed = detail[detail["scenario_executed"]].copy().reset_index(drop=True)
    if executed.empty:
        return pd.DataFrame()
    executed["equity_peak"] = executed["equity_after"].cummax()
    executed["drawdown"] = executed["equity_after"] / executed["equity_peak"] - 1.0
    trough_idx = executed["drawdown"].idxmin()
    peak_idx = executed.loc[:trough_idx, "equity_after"].idxmax()
    return executed.loc[peak_idx:trough_idx].copy()


def build_skip_report(detail: pd.DataFrame) -> pd.DataFrame:
    skipped = detail[~detail["scenario_executed"]].copy()
    if skipped.empty:
        return pd.DataFrame(columns=["skip_reason", "count"])
    skipped["skip_reason"] = skipped["skip_reason"].fillna("unknown").astype(str)
    return skipped["skip_reason"].value_counts().rename_axis("skip_reason").reset_index(name="count")


def write_markdown(path: Path, summary: pd.DataFrame, yearly: pd.DataFrame, monthly: pd.DataFrame, segment: pd.DataFrame) -> None:
    row = summary.iloc[0]
    content = f"""# 最近 2 年最佳策略审计报告

## 策略定义

- 条件：`{row['conditions']}`
- 排序：`{row['sort_rule']}`
- 卖出规则：`{row['exit_rule']}`
- 买入：T+1 开盘，涨停开盘不可买则拒绝
- 卖出：按卖出规则执行，跌停不可卖按保守规则处理
- 仓位：目标 80%，按成交额容量和动态滑点约束降仓

## 全区间结果

- 初始资金：{row['initial_cash']:.2f}
- 最终资金：{row['final_equity']:.2f}
- 资金倍数：{row['equity_multiple']:.2f} 倍
- 成交笔数：{int(row['executed_trade_count'])}
- 跳过次数：{int(row['skipped_count'])}
- 胜率：{row['win_rate']:.2%}
- 平均单笔账户收益：{row['avg_account_return']:.2%}
- 中位数单笔账户收益：{row['median_account_return']:.2%}
- 最大回撤：{row['max_drawdown']:.2%}
- 最大单笔亏损：{row['max_loss']:.2%}
- 最大单笔盈利：{row['max_profit']:.2%}
- 最大连续亏损：{int(row['max_consecutive_losses'])}
- 平均买入成交额占比：{row['avg_buy_amount_ratio']:.4%}
- 最大买入成交额占比：{row['max_buy_amount_ratio']:.4%}
- 平均卖出成交额占比：{row['avg_sell_amount_ratio']:.4%}
- 最大卖出成交额占比：{row['max_sell_amount_ratio']:.4%}

## 年度结果

{yearly.to_markdown(index=False)}

## 月度结果

{monthly.to_markdown(index=False)}

## 市场分段

{segment.to_markdown(index=False)}

## 审计结论

该报告只证明该策略在当前日线保守成交口径下通过最近 2 年回测目标。尚未完成分钟 K、盘口档位、集合竞价、开盘 5 分钟、资金流、龙虎榜和模拟盘验证，不能直接用于实盘。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    summary_path = PROJECT_ROOT / args.summary
    detail_path = PROJECT_ROOT / args.detail
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    summary_data = pd.read_csv(summary_path, low_memory=False)
    summary_row = select_scenario(summary_data, args.scenario)
    detail = load_scenario_detail(detail_path, str(summary_row["scenario"]))

    executed = detail[detail["scenario_executed"]].copy()
    audit_summary = build_summary_row(summary_row, detail)
    yearly = build_period_report(detail, "year")
    monthly = build_period_report(detail, "month")
    segment = build_segment_report(detail)
    drawdown_window = build_drawdown_window(detail)
    skip_report = build_skip_report(detail)
    top_losses = executed.sort_values("dynamic_account_return").head(20)
    top_profits = executed.sort_values("dynamic_account_return", ascending=False).head(20)

    paths = {
        "summary": output_prefix.with_name(output_prefix.name + "_summary.csv"),
        "yearly": output_prefix.with_name(output_prefix.name + "_yearly.csv"),
        "monthly": output_prefix.with_name(output_prefix.name + "_monthly.csv"),
        "segment": output_prefix.with_name(output_prefix.name + "_segment.csv"),
        "trades": output_prefix.with_name(output_prefix.name + "_trades.csv"),
        "drawdown_window": output_prefix.with_name(output_prefix.name + "_drawdown_window.csv"),
        "skip_reasons": output_prefix.with_name(output_prefix.name + "_skip_reasons.csv"),
        "top_losses": output_prefix.with_name(output_prefix.name + "_top_losses.csv"),
        "top_profits": output_prefix.with_name(output_prefix.name + "_top_profits.csv"),
        "markdown": output_prefix.with_suffix(".md"),
    }

    audit_summary.to_csv(paths["summary"], index=False, encoding="utf-8-sig")
    yearly.to_csv(paths["yearly"], index=False, encoding="utf-8-sig")
    monthly.to_csv(paths["monthly"], index=False, encoding="utf-8-sig")
    segment.to_csv(paths["segment"], index=False, encoding="utf-8-sig")
    executed.to_csv(paths["trades"], index=False, encoding="utf-8-sig")
    drawdown_window.to_csv(paths["drawdown_window"], index=False, encoding="utf-8-sig")
    skip_report.to_csv(paths["skip_reasons"], index=False, encoding="utf-8-sig")
    top_losses.to_csv(paths["top_losses"], index=False, encoding="utf-8-sig")
    top_profits.to_csv(paths["top_profits"], index=False, encoding="utf-8-sig")
    write_markdown(paths["markdown"], audit_summary, yearly, monthly, segment)

    print("最近 2 年最佳策略审计完成：")
    print(audit_summary.to_string(index=False))
    print("\n输出文件：")
    for name, path in paths.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
