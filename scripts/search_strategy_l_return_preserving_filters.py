"""
搜索 L 龙头策略的收益优先过滤条件。

目标不是把回撤压到最低，而是在不牺牲原始 L 复利的前提下降低回撤：
  - equity_multiple >= 原始 L
  - max_drawdown 优于原始 L
  - 保留交易数不少于原始的 70%

只研究，不接实盘，不修改 ABC/E2/D。

输出：
  reports/strategy_l/leader_return_preserving_filters.csv
  reports/strategy_l/leader_return_preserving_filters_report.md
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
SPLIT_DATE = "20250601"
MIN_KEEP_RATIO = 0.70
MAX_COMBO_SIZE = 3

FILTER_COLUMNS = [
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
    return trades


def str_values(data: pd.DataFrame, column: str) -> pd.Series:
    return data[column].fillna("missing").astype(str)


def build_atomic_filters(trades: pd.DataFrame) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    for column in FILTER_COLUMNS:
        if column not in trades.columns:
            continue
        values = str_values(trades, column)
        for value, group in trades.groupby(values):
            if len(group) < 3:
                continue
            filters.append({
                "column": column,
                "value": str(value),
                "label": f"{column}!={value}",
                "removed_count": int(len(group)),
                "removed_avg_return": float(group["l_account_return"].mean()),
                "removed_loss_rate": float((group["l_account_return"] < 0).mean()),
            })
    return filters


def apply_filter_combo(trades: pd.DataFrame, combo: tuple[dict[str, Any], ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    remove_mask = pd.Series(False, index=trades.index)
    for item in combo:
        remove_mask = remove_mask | str_values(trades, item["column"]).eq(item["value"])
    return trades[~remove_mask].copy(), trades[remove_mask].copy()


def evaluate_combo(
    trades: pd.DataFrame,
    combo: tuple[dict[str, Any], ...],
    base: dict[str, Any],
) -> dict[str, Any] | None:
    kept, removed = apply_filter_combo(trades, combo)
    min_keep = int(len(trades) * MIN_KEEP_RATIO)
    if len(kept) < min_keep or len(removed) < 3:
        return None

    stats = equity_stats(kept)
    if stats["equity_multiple"] < float(base["equity_multiple"]):
        return None
    if stats["max_drawdown"] <= float(base["max_drawdown"]):
        return None

    train = kept[kept["trade_date"] < SPLIT_DATE].copy()
    test = kept[kept["trade_date"] >= SPLIT_DATE].copy()
    train_stats = equity_stats(train)
    test_stats = equity_stats(test)
    labels = [item["label"] for item in combo]
    return {
        "filter": " AND ".join(labels),
        "filter_count": len(combo),
        "removed_count": int(len(removed)),
        "removed_avg_return": float(removed["l_account_return"].mean()),
        "removed_loss_rate": float((removed["l_account_return"] < 0).mean()),
        **stats,
        "equity_multiple_change": float(stats["equity_multiple"] - float(base["equity_multiple"])),
        "drawdown_improvement": float(stats["max_drawdown"] - float(base["max_drawdown"])),
        "train_trade_count": train_stats["trade_count"],
        "train_equity_multiple": train_stats["equity_multiple"],
        "train_max_drawdown": train_stats["max_drawdown"],
        "test_trade_count": test_stats["trade_count"],
        "test_equity_multiple": test_stats["equity_multiple"],
        "test_max_drawdown": test_stats["max_drawdown"],
        "test_avg_account_return": test_stats["avg_account_return"],
    }


def search_filters(trades: pd.DataFrame) -> pd.DataFrame:
    base = equity_stats(trades)
    atomic = build_atomic_filters(trades)
    rows: list[dict[str, Any]] = []

    # 先评估单条件，全部保留。
    for item in atomic:
        row = evaluate_combo(trades, (item,), base)
        if row:
            rows.append(row)

    # 多条件只用单条件中至少不是明显正收益桶的过滤项，减少过拟合搜索空间。
    pool = [
        item for item in atomic
        if item["removed_avg_return"] <= base["avg_account_return"]
    ]
    for size in range(2, MAX_COMBO_SIZE + 1):
        for combo in combinations(pool, size):
            labels = {item["label"] for item in combo}
            if len(labels) != size:
                continue
            row = evaluate_combo(trades, combo, base)
            if row:
                rows.append(row)

    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows).drop_duplicates("filter")
    return result.sort_values(
        ["equity_multiple", "drawdown_improvement", "test_equity_multiple"],
        ascending=[False, False, False],
    )


def markdown_table(data: pd.DataFrame, max_rows: int = 30) -> str:
    if data.empty:
        return "无符合条件的数据。"
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


def write_report(base: dict[str, Any], result: pd.DataFrame) -> None:
    columns = [
        "filter",
        "filter_count",
        "removed_count",
        "trade_count",
        "equity_multiple",
        "max_drawdown",
        "equity_multiple_change",
        "drawdown_improvement",
        "test_trade_count",
        "test_equity_multiple",
        "test_max_drawdown",
        "test_avg_account_return",
    ]
    lines = [
        "# L 龙头策略收益优先过滤搜索",
        "",
        "## 搜索目标",
        "",
        f"- 基准规则：`{TARGET_RULE}`",
        f"- 基准复利：{base['equity_multiple']:.2f}倍",
        f"- 基准最大回撤：{base['max_drawdown']:.2%}",
        f"- 约束：过滤后复利必须不低于基准，最大回撤必须改善，保留交易数不少于 {MIN_KEEP_RATIO:.0%}。",
        "",
        "## 符合收益优先约束的候选",
        "",
        markdown_table(result[columns] if not result.empty else result),
        "",
        "## 结论",
        "",
        "这些结果仍是全样本探索，不能直接接实盘。",
        "下一步应优先选全样本复利不降、测试集不崩的版本，再与 ABC/E2/D 做资金冲突叠加。",
        "",
    ]
    (OUTPUT_DIR / "leader_return_preserving_filters_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    base = equity_stats(trades)
    result = search_filters(trades)
    result.to_csv(OUTPUT_DIR / "leader_return_preserving_filters.csv", index=False, encoding="utf-8-sig")
    write_report(base, result)
    print("L收益优先过滤搜索完成")
    print("基准:")
    print(pd.DataFrame([base]).to_string(index=False))
    print("\n候选Top20:")
    print(result.head(20).to_string(index=False) if not result.empty else "无符合约束的候选")
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
