"""
分析 bj_cap_2pct 策略在 2026 样本外的最大回撤来源。

文件作用：
1. 读取 recent_2y_bj_cap2_strategy_2026_trades.csv。
2. 计算 2026 样本外资金曲线、最大回撤区间和连续亏损段。
3. 输出最大回撤区间内逐笔交易、板块贡献、最大亏损/最大盈利交易。
4. 用于判断 2026 样本外收益是否稳定，最大回撤是否可接受。

本脚本只读取本地报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分析 bj_cap_2pct 2026 样本外回撤。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_bj_cap2_strategy_2026_trades.csv",
        help="2026 样本外逐笔交易报告。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/bj_cap2_2026_drawdown",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def load_trades(path: Path) -> pd.DataFrame:
    data = pd.read_csv(path, low_memory=False)
    data = data[data["rule_executed"] == True].copy()  # noqa: E712
    if data.empty:
        raise RuntimeError(f"没有 2026 已成交交易: {path}")
    data["trade_date"] = data["trade_date"].map(normalize_date)
    data["buy_trade_date"] = data["buy_trade_date"].map(normalize_date)
    data["exit_trade_date"] = data["exit_trade_date"].map(normalize_date)
    data["market_segment"] = data["market_segment"].fillna("unknown").astype(str)
    data["name"] = data["name"].fillna("").astype(str)
    data["rule_account_return"] = pd.to_numeric(data["rule_account_return"], errors="coerce")
    data["rule_equity_before"] = pd.to_numeric(data["rule_equity_before"], errors="coerce")
    data["rule_equity_after"] = pd.to_numeric(data["rule_equity_after"], errors="coerce")
    data["selected_order"] = pd.to_numeric(data["selected_order"], errors="coerce")
    return data.sort_values("selected_order").reset_index(drop=True)


def add_equity_metrics(trades: pd.DataFrame) -> pd.DataFrame:
    result = trades.copy()
    result["equity_peak"] = result["rule_equity_after"].cummax()
    result["drawdown"] = result["rule_equity_after"] / result["equity_peak"] - 1.0
    result["is_loss"] = result["rule_account_return"] <= 0
    loss_group = []
    current = 0
    for is_loss in result["is_loss"]:
        if is_loss:
            current += 1
        else:
            current = 0
        loss_group.append(current)
    result["consecutive_loss_count"] = loss_group
    return result


def find_max_drawdown_window(trades: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    trough_idx = trades["drawdown"].idxmin()
    trough = trades.loc[trough_idx]
    before_trough = trades.loc[:trough_idx]
    peak_idx = before_trough["rule_equity_after"].idxmax()
    peak = trades.loc[peak_idx]
    window = trades.loc[peak_idx:trough_idx].copy()
    return peak, trough, window


def build_summary(trades: pd.DataFrame, peak: pd.Series, trough: pd.Series, window: pd.DataFrame) -> pd.DataFrame:
    returns = trades["rule_account_return"]
    window_returns = window["rule_account_return"]
    rows = [
        {
            "metric": "trade_count",
            "value": len(trades),
        },
        {
            "metric": "win_rate",
            "value": float((returns > 0).mean()),
        },
        {
            "metric": "total_multiple_2026",
            "value": float(trades["rule_equity_after"].iloc[-1] / trades["rule_equity_before"].iloc[0]),
        },
        {
            "metric": "max_drawdown",
            "value": float(trough["drawdown"]),
        },
        {
            "metric": "max_drawdown_peak_date",
            "value": peak["exit_trade_date"],
        },
        {
            "metric": "max_drawdown_trough_date",
            "value": trough["exit_trade_date"],
        },
        {
            "metric": "max_drawdown_peak_equity",
            "value": float(peak["rule_equity_after"]),
        },
        {
            "metric": "max_drawdown_trough_equity",
            "value": float(trough["rule_equity_after"]),
        },
        {
            "metric": "drawdown_window_trade_count",
            "value": len(window),
        },
        {
            "metric": "drawdown_window_win_rate",
            "value": float((window_returns > 0).mean()) if len(window_returns) else 0.0,
        },
        {
            "metric": "max_single_loss",
            "value": float(returns.min()),
        },
        {
            "metric": "max_single_profit",
            "value": float(returns.max()),
        },
        {
            "metric": "max_consecutive_losses",
            "value": int(trades["consecutive_loss_count"].max()),
        },
    ]
    return pd.DataFrame(rows)


def build_segment_report(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for segment, group in trades.groupby("market_segment"):
        returns = group["rule_account_return"]
        rows.append(
            {
                "market_segment": segment,
                "trade_count": int(len(group)),
                "win_rate": float((returns > 0).mean()),
                "sum_account_return": float(returns.sum()),
                "avg_account_return": float(returns.mean()),
                "compound_multiple": float((1.0 + returns).prod()),
                "max_profit": float(returns.max()),
                "max_loss": float(returns.min()),
            }
        )
    return pd.DataFrame(rows).sort_values("compound_multiple", ascending=False)


def build_loss_streak_report(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    current_rows = []
    streak_id = 0
    for _, row in trades.iterrows():
        if bool(row["is_loss"]):
            current_rows.append(row)
        else:
            if current_rows:
                streak_id += 1
                rows.append(summarize_streak(streak_id, current_rows))
                current_rows = []
    if current_rows:
        streak_id += 1
        rows.append(summarize_streak(streak_id, current_rows))
    return pd.DataFrame(rows).sort_values("streak_length", ascending=False) if rows else pd.DataFrame()


def summarize_streak(streak_id: int, rows: list[pd.Series]) -> dict:
    data = pd.DataFrame(rows)
    return {
        "streak_id": streak_id,
        "streak_length": int(len(data)),
        "start_trade_date": data["trade_date"].iloc[0],
        "end_trade_date": data["trade_date"].iloc[-1],
        "start_exit_date": data["exit_trade_date"].iloc[0],
        "end_exit_date": data["exit_trade_date"].iloc[-1],
        "sum_account_return": float(data["rule_account_return"].sum()),
        "compound_multiple": float((1.0 + data["rule_account_return"]).prod()),
        "max_loss": float(data["rule_account_return"].min()),
        "symbols": ",".join(data["ts_code"].astype(str).tolist()),
        "names": ",".join(data["name"].astype(str).tolist()),
        "segments": ",".join(data["market_segment"].astype(str).tolist()),
    }


def main() -> None:
    args = parse_args()
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    trades = add_equity_metrics(load_trades(PROJECT_ROOT / args.input))
    peak, trough, window = find_max_drawdown_window(trades)
    summary = build_summary(trades, peak, trough, window)
    segment = build_segment_report(trades)
    window_segment = build_segment_report(window)
    loss_streak = build_loss_streak_report(trades)
    top_losses = trades.sort_values("rule_account_return").head(10).copy()
    top_profits = trades.sort_values("rule_account_return", ascending=False).head(10).copy()

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    trades_path = output_prefix.with_name(output_prefix.name + "_trades.csv")
    window_path = output_prefix.with_name(output_prefix.name + "_window_trades.csv")
    segment_path = output_prefix.with_name(output_prefix.name + "_by_segment.csv")
    window_segment_path = output_prefix.with_name(output_prefix.name + "_window_by_segment.csv")
    streak_path = output_prefix.with_name(output_prefix.name + "_loss_streaks.csv")
    top_losses_path = output_prefix.with_name(output_prefix.name + "_top_losses.csv")
    top_profits_path = output_prefix.with_name(output_prefix.name + "_top_profits.csv")

    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    trades.to_csv(trades_path, index=False, encoding="utf-8-sig")
    window.to_csv(window_path, index=False, encoding="utf-8-sig")
    segment.to_csv(segment_path, index=False, encoding="utf-8-sig")
    window_segment.to_csv(window_segment_path, index=False, encoding="utf-8-sig")
    loss_streak.to_csv(streak_path, index=False, encoding="utf-8-sig")
    top_losses.to_csv(top_losses_path, index=False, encoding="utf-8-sig")
    top_profits.to_csv(top_profits_path, index=False, encoding="utf-8-sig")

    print("bj_cap_2pct 2026 样本外回撤复盘完成")
    print("\n摘要：")
    print(summary.to_string(index=False))
    print("\n最大回撤区间交易：")
    show_cols = [
        "trade_date",
        "exit_trade_date",
        "ts_code",
        "name",
        "market_segment",
        "rule_account_return",
        "rule_equity_before",
        "rule_equity_after",
        "drawdown",
    ]
    print(window[show_cols].to_string(index=False))
    print("\n报告文件：")
    print(f"- summary: {summary_path}")
    print(f"- trades: {trades_path}")
    print(f"- window_trades: {window_path}")
    print(f"- by_segment: {segment_path}")
    print(f"- window_by_segment: {window_segment_path}")
    print(f"- loss_streaks: {streak_path}")
    print(f"- top_losses: {top_losses_path}")
    print(f"- top_profits: {top_profits_path}")


if __name__ == "__main__":
    main()
