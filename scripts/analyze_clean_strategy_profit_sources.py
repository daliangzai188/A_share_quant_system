"""
分析 A-clean 严格策略的收益来源。

文件作用：
1. 读取最佳策略审计后的逐笔成交明细。
2. 对市场情绪、板块情绪、涨停时间、封单比例、成交额、换手率等因子分桶做盈利/亏损差异分析。
3. 分别统计全区间、训练期和测试期表现，避免只根据全样本追收益。
4. 输出单因子分桶、二维组合分桶、盈利亏损占比差异和 Markdown 摘要。

本脚本只读取本地 CSV 报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
from itertools import combinations
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]


FEATURE_COLUMNS = [
    "market_segment",
    "market_emotion_state_bucket",
    "segment_emotion_state_bucket",
    "market_chain_count_bucket",
    "segment_chain_count_bucket",
    "market_limit_down_count_bucket",
    "segment_limit_down_count_bucket",
    "segment_limit_down_ratio_bucket",
    "segment_limit_max_height_bucket",
    "market_leader_rank_bucket",
    "segment_market_leader_rank_bucket",
    "limit_height_rank_bucket",
    "segment_limit_height_rank_bucket",
    "first_time_detail_bucket",
    "limit_times_detail_bucket",
    "open_times_bucket",
    "amount_bucket",
    "turnover_rate_bucket",
    "volume_ratio_bucket",
    "fd_ratio_bucket",
    "pct_chg_bucket",
    "prev_pct_chg_bucket",
    "amount_ratio_bucket",
    "limit_up_count_bucket",
    "segment_limit_up_count_bucket",
    "segment_limit_up_ratio_bucket",
    "retreat_state_bucket",
    "segment_retreat_state_bucket",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分析 A-clean 策略收益来源。")
    parser.add_argument(
        "--trades",
        default="reports/recent_2y_strict_exclude_amount_ratio_best_audit_trades.csv",
        help="最佳策略审计逐笔交易明细。",
    )
    parser.add_argument("--train-start", default="20240101", help="训练期开始日期。")
    parser.add_argument("--train-end", default="20251231", help="训练期结束日期。")
    parser.add_argument("--test-start", default="20260101", help="测试期开始日期。")
    parser.add_argument("--test-end", default="20260518", help="测试期结束日期。")
    parser.add_argument("--min-count", type=int, default=3, help="分桶最小样本数。")
    parser.add_argument("--max-pair-buckets", type=int, default=60, help="用于组合分析的单桶数量上限。")
    parser.add_argument(
        "--output-prefix",
        default="reports/a_clean_profit_sources",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def max_drawdown_from_returns(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    equity = (1.0 + returns.astype(float)).cumprod()
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def max_consecutive_losses(returns: pd.Series) -> int:
    current = 0
    result = 0
    for value in returns.astype(float):
        if value <= 0:
            current += 1
            result = max(result, current)
        else:
            current = 0
    return result


def load_trades(path: Path) -> pd.DataFrame:
    data = pd.read_csv(path, low_memory=False)
    if "scenario_executed" not in data.columns:
        raise RuntimeError(f"缺少 scenario_executed 字段: {path}")
    executed_mask = data["scenario_executed"].astype(str).str.lower().isin({"true", "1"})
    data = data[executed_mask].copy()
    if data.empty:
        raise RuntimeError(f"没有已成交交易: {path}")

    for column in ["trade_date", "buy_trade_date", "exit_trade_date"]:
        if column in data.columns:
            data[column] = data[column].map(normalize_date)
    data["analysis_date"] = data.get("exit_trade_date", data.get("trade_date", "")).map(normalize_date)
    data["dynamic_account_return"] = pd.to_numeric(data["dynamic_account_return"], errors="coerce").fillna(0.0)
    data["market_segment"] = data.get("market_segment", "unknown")
    data["market_segment"] = data["market_segment"].fillna("unknown").astype(str)
    for column in FEATURE_COLUMNS:
        if column in data.columns:
            data[column] = data[column].fillna("missing").astype(str)
    return data.sort_values(["analysis_date", "trade_order", "trade_date"]).reset_index(drop=True)


def period_mask(data: pd.DataFrame, start: str, end: str) -> pd.Series:
    dates = data["analysis_date"].fillna("").astype(str)
    return (dates >= start) & (dates <= end)


def calc_metrics(data: pd.DataFrame, label: str) -> dict[str, object]:
    returns = data["dynamic_account_return"].astype(float)
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    return {
        "period": label,
        "trade_count": int(len(data)),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "compound_multiple": float((1.0 + returns).prod()) if len(returns) else 0.0,
        "max_drawdown": max_drawdown_from_returns(returns),
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(returns),
    }


def calc_period_metrics(
    data: pd.DataFrame,
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
) -> dict[str, object]:
    full = calc_metrics(data, "full")
    train = calc_metrics(data[period_mask(data, train_start, train_end)].copy(), "train")
    test = calc_metrics(data[period_mask(data, test_start, test_end)].copy(), "test")
    result = {}
    for prefix, metrics in [("full", full), ("train", train), ("test", test)]:
        for key, value in metrics.items():
            if key != "period":
                result[f"{prefix}_{key}"] = value
    return result


def build_single_bucket_report(
    data: pd.DataFrame,
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
    min_count: int,
) -> pd.DataFrame:
    rows = []
    winners = data[data["dynamic_account_return"] > 0]
    losers = data[data["dynamic_account_return"] <= 0]
    total_winners = max(len(winners), 1)
    total_losers = max(len(losers), 1)

    for column in FEATURE_COLUMNS:
        if column not in data.columns:
            continue
        for value, group in data.groupby(column):
            if len(group) < min_count or value in {"missing", "nan", "None", "unknown"}:
                continue
            win_count = int((winners[column] == value).sum())
            loss_count = int((losers[column] == value).sum())
            metrics = calc_period_metrics(group, train_start, train_end, test_start, test_end)
            rows.append(
                {
                    "feature": column,
                    "bucket": value,
                    "condition": f"{column}={value}",
                    "winner_count": win_count,
                    "loser_count": loss_count,
                    "winner_share": float(win_count / total_winners),
                    "loser_share": float(loss_count / total_losers),
                    "winner_loser_share_diff": float(win_count / total_winners - loss_count / total_losers),
                    **metrics,
                }
            )
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result["source_score"] = (
        result["full_compound_multiple"]
        * (1.0 + result["full_win_rate"])
        * (1.0 + result["winner_loser_share_diff"])
        / (1.0 + result["full_max_drawdown"].abs())
    )
    return result.sort_values(["source_score", "full_trade_count"], ascending=[False, False])


def build_pair_bucket_report(
    data: pd.DataFrame,
    single_report: pd.DataFrame,
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
    min_count: int,
    max_pair_buckets: int,
) -> pd.DataFrame:
    if single_report.empty:
        return pd.DataFrame()
    candidates = single_report.sort_values("source_score", ascending=False).head(max_pair_buckets)
    rows = []
    for left, right in combinations(candidates.to_dict("records"), 2):
        if left["feature"] == right["feature"]:
            continue
        mask = (data[left["feature"]].astype(str) == str(left["bucket"])) & (
            data[right["feature"]].astype(str) == str(right["bucket"])
        )
        group = data[mask].copy()
        if len(group) < min_count:
            continue
        metrics = calc_period_metrics(group, train_start, train_end, test_start, test_end)
        rows.append(
            {
                "condition": f"{left['condition']}&&{right['condition']}",
                "left_feature": left["feature"],
                "left_bucket": left["bucket"],
                "right_feature": right["feature"],
                "right_bucket": right["bucket"],
                **metrics,
            }
        )
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result["source_score"] = (
        result["full_compound_multiple"]
        * (1.0 + result["full_win_rate"])
        / (1.0 + result["full_max_drawdown"].abs())
    )
    return result.sort_values(["source_score", "full_trade_count"], ascending=[False, False])


def write_markdown(
    output_path: Path,
    baseline: pd.DataFrame,
    single_report: pd.DataFrame,
    pair_report: pd.DataFrame,
) -> None:
    top_single = single_report.head(15).copy()
    top_pair = pair_report.head(15).copy() if not pair_report.empty else pd.DataFrame()
    negative = single_report.sort_values(["full_avg_account_return", "full_compound_multiple"]).head(15)

    lines = [
        "# A-clean 收益来源分析",
        "",
        "本报告只基于本地已成交交易明细，不调用外部接口，不接实盘。",
        "",
        "## 基准表现",
        "",
        baseline.to_markdown(index=False),
        "",
        "## 正向单因子分桶 Top 15",
        "",
        top_single[
            [
                "condition",
                "full_trade_count",
                "full_compound_multiple",
                "full_win_rate",
                "full_avg_account_return",
                "full_median_account_return",
                "full_max_drawdown",
                "test_trade_count",
                "test_compound_multiple",
                "test_win_rate",
            ]
        ].to_markdown(index=False),
        "",
        "## 正向二维组合 Top 15",
        "",
        top_pair[
            [
                "condition",
                "full_trade_count",
                "full_compound_multiple",
                "full_win_rate",
                "full_avg_account_return",
                "full_max_drawdown",
                "test_trade_count",
                "test_compound_multiple",
                "test_win_rate",
            ]
        ].to_markdown(index=False)
        if not top_pair.empty
        else "无满足样本数要求的二维组合。",
        "",
        "## 负向单因子分桶 Top 15",
        "",
        negative[
            [
                "condition",
                "full_trade_count",
                "full_compound_multiple",
                "full_win_rate",
                "full_avg_account_return",
                "full_max_drawdown",
                "test_trade_count",
                "test_compound_multiple",
                "test_win_rate",
            ]
        ].to_markdown(index=False),
        "",
    ]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    trades_path = PROJECT_ROOT / args.trades
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    trades = load_trades(trades_path)
    baseline = pd.DataFrame(
        [
            {
                "scope": "A-clean baseline",
                **calc_period_metrics(
                    trades,
                    args.train_start,
                    args.train_end,
                    args.test_start,
                    args.test_end,
                ),
            }
        ]
    )
    single_report = build_single_bucket_report(
        trades,
        args.train_start,
        args.train_end,
        args.test_start,
        args.test_end,
        args.min_count,
    )
    pair_report = build_pair_bucket_report(
        trades,
        single_report,
        args.train_start,
        args.train_end,
        args.test_start,
        args.test_end,
        args.min_count,
        args.max_pair_buckets,
    )

    baseline_path = output_prefix.with_name(output_prefix.name + "_baseline.csv")
    single_path = output_prefix.with_name(output_prefix.name + "_single_buckets.csv")
    pair_path = output_prefix.with_name(output_prefix.name + "_pair_buckets.csv")
    markdown_path = output_prefix.with_suffix(".md")

    baseline.to_csv(baseline_path, index=False)
    single_report.to_csv(single_path, index=False)
    pair_report.to_csv(pair_path, index=False)
    write_markdown(markdown_path, baseline, single_report, pair_report)

    print("A-clean 收益来源分析完成：")
    print(f"- baseline: {baseline_path}")
    print(f"- single_buckets: {single_path}")
    print(f"- pair_buckets: {pair_path}")
    print(f"- markdown: {markdown_path}")


if __name__ == "__main__":
    main()
