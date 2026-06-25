"""
L 龙头策略训练/测试拆分验证。

只研究，不接实盘，不修改 ABC/E2/D。

验证对象：
  1. L原始行业主线龙头
  2. 过滤 early_morning
  3. 过滤 segment_limit_up_count_bucket=10_20
  4. 同时过滤 early_morning 与 segment_limit_up_count_bucket=10_20

输出：
  reports/strategy_l/leader_split_validation.csv
  reports/strategy_l/leader_split_validation_report.md
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
INPUT_PATH = PROJECT_ROOT / "reports" / "strategy_l" / "leader_strategy_trades.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_l"
TARGET_RULE = "L_theme_mainline_leader"
INITIAL_EQUITY = 500_000.0
SPLIT_DATES = ["20250101", "20250601", "20250701"]


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    predicate: Callable[[pd.DataFrame], pd.Series]


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
            "start_date": "",
            "end_date": "",
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
        "start_date": str(trades["trade_date"].min()) if "trade_date" in trades.columns and not trades.empty else "",
        "end_date": str(trades["trade_date"].max()) if "trade_date" in trades.columns and not trades.empty else "",
    }


def load_trades() -> pd.DataFrame:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(f"找不到L交易明细: {INPUT_PATH}")
    trades = pd.read_csv(INPUT_PATH, dtype={"trade_date": str}, low_memory=False)
    trades = trades[trades["l_rule"].astype(str).eq(TARGET_RULE)].copy()
    trades = trades.sort_values("trade_date").reset_index(drop=True)
    trades["l_account_return"] = to_numeric(trades["l_account_return"])
    return trades


def str_col(data: pd.DataFrame, column: str) -> pd.Series:
    if column not in data.columns:
        return pd.Series("", index=data.index)
    return data[column].fillna("").astype(str)


def build_variants() -> list[Variant]:
    return [
        Variant(
            "L_base",
            "原始行业主线龙头，不加额外过滤。",
            lambda df: pd.Series(True, index=df.index),
        ),
        Variant(
            "L_no_early_morning",
            "过滤 first_time_bucket=early_morning。",
            lambda df: str_col(df, "first_time_bucket").ne("early_morning"),
        ),
        Variant(
            "L_no_segment_limit_10_20",
            "过滤 segment_limit_up_count_bucket=10_20。",
            lambda df: str_col(df, "segment_limit_up_count_bucket").ne("10_20"),
        ),
        Variant(
            "L_no_early_and_no_segment_10_20",
            "同时过滤 early_morning 和 segment_limit_up_count_bucket=10_20。",
            lambda df: (
                str_col(df, "first_time_bucket").ne("early_morning")
                & str_col(df, "segment_limit_up_count_bucket").ne("10_20")
            ),
        ),
        Variant(
            "L_no_before_1000",
            "过滤 first_time_detail_bucket=before_1000。",
            lambda df: str_col(df, "first_time_detail_bucket").ne("before_1000"),
        ),
        Variant(
            "L_no_before_1000_and_no_segment_10_20",
            "同时过滤 before_1000 和 segment_limit_up_count_bucket=10_20。",
            lambda df: (
                str_col(df, "first_time_detail_bucket").ne("before_1000")
                & str_col(df, "segment_limit_up_count_bucket").ne("10_20")
            ),
        ),
    ]


def validate(trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for variant in build_variants():
        selected = trades[variant.predicate(trades)].copy()
        overall = equity_stats(selected)
        rows.append({
            "variant": variant.name,
            "description": variant.description,
            "split_date": "overall",
            "sample": "overall",
            **overall,
        })
        for split_date in SPLIT_DATES:
            train = selected[selected["trade_date"] < split_date].copy()
            test = selected[selected["trade_date"] >= split_date].copy()
            train_stats = equity_stats(train)
            test_stats = equity_stats(test)
            rows.append({
                "variant": variant.name,
                "description": variant.description,
                "split_date": split_date,
                "sample": "train",
                **train_stats,
            })
            rows.append({
                "variant": variant.name,
                "description": variant.description,
                "split_date": split_date,
                "sample": "test",
                **test_stats,
            })
    return pd.DataFrame(rows)


def markdown_table(data: pd.DataFrame, max_rows: int = 50) -> str:
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


def write_report(result: pd.DataFrame) -> None:
    key_cols = [
        "variant",
        "split_date",
        "sample",
        "trade_count",
        "win_rate",
        "avg_account_return",
        "median_account_return",
        "equity_multiple",
        "max_drawdown",
        "max_loss",
        "max_consecutive_losses",
    ]
    overall = result[result["sample"].eq("overall")].sort_values(
        ["max_drawdown", "equity_multiple"],
        ascending=[False, False],
    )
    main_split = result[result["split_date"].eq("20250601")].copy()
    lines = [
        "# L 龙头策略训练/测试拆分验证",
        "",
        "## 口径",
        "",
        f"- 目标规则：`{TARGET_RULE}`",
        "- 本报告只做研究，不接实盘。",
        "- 主拆分日期：`2025-06-01`；同时附带 `2025-01-01` 和 `2025-07-01` 稳健性检查。",
        "",
        "## 整体表现",
        "",
        markdown_table(overall[key_cols]),
        "",
        "## 主拆分 2025-06-01",
        "",
        markdown_table(main_split[key_cols].sort_values(["variant", "sample"])),
        "",
        "## 结论",
        "",
        "- 如果某个过滤只在整体有效，但测试集收益/回撤明显变差，不能接实盘。",
        "- 如果过滤后测试集交易数太少，也不能接实盘。",
        "- 下一步需要把通过拆分的 L 版本与 ABC/E2/D 做资金冲突叠加测算。",
        "",
    ]
    (OUTPUT_DIR / "leader_split_validation_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    result = validate(trades)
    result.to_csv(OUTPUT_DIR / "leader_split_validation.csv", index=False, encoding="utf-8-sig")
    write_report(result)

    print("L训练/测试拆分验证完成")
    print(result[result["split_date"].eq("20250601")][[
        "variant",
        "sample",
        "trade_count",
        "win_rate",
        "avg_account_return",
        "equity_multiple",
        "max_drawdown",
        "max_loss",
    ]].to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
