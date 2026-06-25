"""
分析 L 龙头策略亏损来源并搜索降回撤过滤条件。

只研究，不接实盘，不修改 ABC/E2/D。

输入：
  reports/strategy_l/leader_strategy_trades.csv

输出：
  reports/strategy_l/leader_filter_loss_buckets.csv
  reports/strategy_l/leader_filter_candidates.csv
  reports/strategy_l/leader_filter_pair_candidates.csv
  reports/strategy_l/leader_filter_report.md
"""
from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
INPUT_PATH = PROJECT_ROOT / "reports" / "strategy_l" / "leader_strategy_trades.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_l"
TARGET_RULE = "L_theme_mainline_leader"
INITIAL_EQUITY = 500_000.0

ANALYZE_COLUMNS = [
    "theme_name",
    "market_segment",
    "limit_times_bucket",
    "first_time_bucket",
    "first_time_detail_bucket",
    "board_type",
    "open_times_bucket",
    "fd_ratio_bucket",
    "amount_ratio_bucket",
    "turnover_rate_bucket",
    "volume_ratio_bucket",
    "market_sentiment_level",
    "segment_market_sentiment_level",
    "market_chain_count_bucket",
    "segment_chain_count_bucket",
    "limit_up_count_bucket",
    "segment_limit_up_count_bucket",
    "segment_retreat_state_bucket",
    "retreat_state_bucket",
    "market_limit_down_count_bucket",
    "segment_limit_down_count_bucket",
    "theme_limit_count",
    "theme_chain_count",
    "theme_heat_rank",
    "theme_leader_rank",
    "theme_height_rank",
]


def to_numeric(series: pd.Series, default: float = 0.0) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(default)


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def max_consecutive_losses(returns: pd.Series) -> int:
    current = 0
    result = 0
    for value in returns.fillna(0.0):
        if float(value) < 0:
            current += 1
            result = max(result, current)
        else:
            current = 0
    return result


def equity_stats(trades: pd.DataFrame) -> dict[str, Any]:
    returns = to_numeric(trades.get("l_account_return", pd.Series(dtype=float)))
    if returns.empty:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_account_return": 0.0,
            "median_account_return": 0.0,
            "equity_multiple": 1.0,
            "max_drawdown": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "max_consecutive_losses": 0,
        }
    equity = INITIAL_EQUITY * (1.0 + returns).cumprod()
    return {
        "trade_count": int(len(returns)),
        "win_rate": float((returns > 0).mean()),
        "avg_account_return": float(returns.mean()),
        "median_account_return": float(returns.median()),
        "equity_multiple": float(equity.iloc[-1] / INITIAL_EQUITY),
        "max_drawdown": max_drawdown(equity),
        "max_profit": float(returns.max()),
        "max_loss": float(returns.min()),
        "max_consecutive_losses": max_consecutive_losses(returns),
    }


def load_trades() -> pd.DataFrame:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(f"找不到L交易明细: {INPUT_PATH}")
    trades = pd.read_csv(INPUT_PATH, dtype={"trade_date": str}, low_memory=False)
    trades = trades[trades["l_rule"].astype(str).eq(TARGET_RULE)].copy()
    trades = trades.sort_values("trade_date").reset_index(drop=True)
    trades["l_account_return"] = to_numeric(trades["l_account_return"])
    for column in ["theme_limit_count", "theme_chain_count", "theme_heat_rank", "theme_leader_rank", "theme_height_rank"]:
        if column in trades.columns:
            trades[column] = to_numeric(trades[column])
    return trades


def bucket_loss_report(trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for column in ANALYZE_COLUMNS:
        if column not in trades.columns:
            continue
        values = trades[column].fillna("missing").astype(str)
        for value, group in trades.groupby(values):
            if len(group) < 3:
                continue
            returns = group["l_account_return"]
            rows.append({
                "column": column,
                "value": value,
                "count": int(len(group)),
                "loss_count": int((returns < 0).sum()),
                "loss_rate": float((returns < 0).mean()),
                "avg_account_return": float(returns.mean()),
                "median_account_return": float(returns.median()),
                "sum_account_return": float(returns.sum()),
                "max_loss": float(returns.min()),
                "max_profit": float(returns.max()),
            })
    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows)
    return result.sort_values(["avg_account_return", "loss_rate", "count"], ascending=[True, False, False])


def filter_candidates(trades: pd.DataFrame, base: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    base_multiple = float(base["equity_multiple"])
    base_dd = float(base["max_drawdown"])
    base_count = int(base["trade_count"])
    for column in ANALYZE_COLUMNS:
        if column not in trades.columns:
            continue
        values = trades[column].fillna("missing").astype(str)
        for value, group in trades.groupby(values):
            if len(group) < 3:
                continue
            kept = trades[values.ne(str(value))].copy()
            if len(kept) < max(50, int(base_count * 0.4)):
                continue
            stats = equity_stats(kept)
            rows.append({
                "filter": f"{column}!={value}",
                "column": column,
                "value": value,
                "removed_count": int(len(group)),
                "removed_avg_return": float(group["l_account_return"].mean()),
                "removed_loss_rate": float((group["l_account_return"] < 0).mean()),
                **stats,
                "equity_multiple_change": float(stats["equity_multiple"] - base_multiple),
                "drawdown_improvement": float(stats["max_drawdown"] - base_dd),
            })
    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows)
    return result.sort_values(
        ["drawdown_improvement", "equity_multiple", "trade_count"],
        ascending=[False, False, False],
    )


def pair_filter_candidates(trades: pd.DataFrame, singles: pd.DataFrame, base: dict[str, Any]) -> pd.DataFrame:
    if singles.empty:
        return pd.DataFrame()
    top = singles.head(20).copy()
    rows: list[dict[str, Any]] = []
    base_multiple = float(base["equity_multiple"])
    base_dd = float(base["max_drawdown"])
    base_count = int(base["trade_count"])
    filters = top[["column", "value"]].to_dict("records")
    for left, right in combinations(filters, 2):
        if left == right:
            continue
        left_values = trades[left["column"]].fillna("missing").astype(str)
        right_values = trades[right["column"]].fillna("missing").astype(str)
        remove_mask = left_values.eq(str(left["value"])) | right_values.eq(str(right["value"]))
        removed = trades[remove_mask]
        kept = trades[~remove_mask].copy()
        if len(removed) < 3 or len(kept) < max(50, int(base_count * 0.35)):
            continue
        stats = equity_stats(kept)
        rows.append({
            "filter": f"{left['column']}!={left['value']} AND {right['column']}!={right['value']}",
            "removed_count": int(len(removed)),
            "removed_avg_return": float(removed["l_account_return"].mean()),
            "removed_loss_rate": float((removed["l_account_return"] < 0).mean()),
            **stats,
            "equity_multiple_change": float(stats["equity_multiple"] - base_multiple),
            "drawdown_improvement": float(stats["max_drawdown"] - base_dd),
        })
    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows).drop_duplicates("filter")
    return result.sort_values(
        ["drawdown_improvement", "equity_multiple", "trade_count"],
        ascending=[False, False, False],
    )


def markdown_table(data: pd.DataFrame, max_rows: int = 20) -> str:
    if data.empty:
        return "无数据。"
    view = data.head(max_rows).copy()
    for column in view.columns:
        if pd.api.types.is_float_dtype(view[column]):
            view[column] = view[column].map(lambda value: f"{float(value):.6g}" if pd.notna(value) else "")
        else:
            view[column] = view[column].fillna("").astype(str)
    headers = list(view.columns)
    rows = view.astype(str).values.tolist()
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def write_report(base: dict[str, Any], loss_buckets: pd.DataFrame, singles: pd.DataFrame, pairs: pd.DataFrame) -> None:
    lines = [
        "# L 龙头策略过滤研究报告",
        "",
        "## 基准规则",
        "",
        f"- 目标规则：`{TARGET_RULE}`",
        f"- 交易数：{base['trade_count']}",
        f"- 胜率：{base['win_rate']:.2%}",
        f"- 平均账户收益：{base['avg_account_return']:.2%}",
        f"- 中位账户收益：{base['median_account_return']:.2%}",
        f"- 复利：{base['equity_multiple']:.2f}倍",
        f"- 最大回撤：{base['max_drawdown']:.2%}",
        f"- 最大单笔亏损：{base['max_loss']:.2%}",
        f"- 连续亏损：{base['max_consecutive_losses']}次",
        "",
        "## 亏损来源桶",
        "",
        markdown_table(loss_buckets[[
            "column",
            "value",
            "count",
            "loss_rate",
            "avg_account_return",
            "median_account_return",
            "max_loss",
            "max_profit",
        ]]),
        "",
        "## 单条件过滤候选",
        "",
        markdown_table(singles[[
            "filter",
            "removed_count",
            "removed_avg_return",
            "removed_loss_rate",
            "trade_count",
            "equity_multiple",
            "max_drawdown",
            "drawdown_improvement",
            "equity_multiple_change",
        ]]),
        "",
        "## 双条件过滤候选",
        "",
        markdown_table(pairs[[
            "filter",
            "removed_count",
            "removed_avg_return",
            "removed_loss_rate",
            "trade_count",
            "equity_multiple",
            "max_drawdown",
            "drawdown_improvement",
            "equity_multiple_change",
        ]]),
        "",
        "## 结论",
        "",
        "当前过滤搜索只证明哪些风险桶值得继续验证，不能直接接实盘。",
        "下一步需要做训练/测试拆分和与 ABC/E2/D 的资金冲突叠加。",
        "",
    ]
    (OUTPUT_DIR / "leader_filter_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    base = equity_stats(trades)
    loss_buckets = bucket_loss_report(trades)
    singles = filter_candidates(trades, base)
    pairs = pair_filter_candidates(trades, singles, base)

    loss_buckets.to_csv(OUTPUT_DIR / "leader_filter_loss_buckets.csv", index=False, encoding="utf-8-sig")
    singles.to_csv(OUTPUT_DIR / "leader_filter_candidates.csv", index=False, encoding="utf-8-sig")
    pairs.to_csv(OUTPUT_DIR / "leader_filter_pair_candidates.csv", index=False, encoding="utf-8-sig")
    write_report(base, loss_buckets, singles, pairs)

    print("L过滤研究完成")
    print("基准:")
    print(pd.DataFrame([base]).to_string(index=False))
    print("\n单条件过滤Top10:")
    print(singles.head(10).to_string(index=False))
    print("\n双条件过滤Top10:")
    print(pairs.head(10).to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
