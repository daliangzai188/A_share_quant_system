"""
把 L 龙头策略候选与当前组合做交易日冲突/补充测算。

本脚本只做离线研究：
  - 不接实盘
  - 不修改 ABC/E2/D
  - 不改变任何下单配置

输出：
  reports/strategy_l/leader_combo_overlay_summary.csv
  reports/strategy_l/leader_combo_overlay_daily.csv
  reports/strategy_l/leader_combo_overlay_conflicts.csv
  reports/strategy_l/leader_combo_overlay_report.md
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
L_TRADES_PATH = PROJECT_ROOT / "reports" / "strategy_l" / "leader_strategy_trades.csv"
L_FILTERS_PATH = PROJECT_ROOT / "reports" / "strategy_l" / "leader_return_preserving_filters.csv"
BASELINE_DAILY_PATH = PROJECT_ROOT / "reports" / "current_live_abce2_audit" / "current_live_abce2_detail.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_l"

TARGET_L_RULE = "L_theme_mainline_leader"
INITIAL_EQUITY = 500_000.0
IDLE_STRATEGY_LEGS = {"NONE"}


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


def calc_metrics(name: str, daily: pd.DataFrame, return_column: str, op_column: str) -> dict[str, Any]:
    returns = to_numeric(daily[return_column])
    equity = INITIAL_EQUITY * (1.0 + returns).cumprod()
    active = daily[daily[op_column].astype(str).ne("NO_TRADE")].copy()
    active_returns = to_numeric(active[return_column])
    return {
        "scenario": name,
        "day_count": int(len(daily)),
        "trade_count": int(len(active)),
        "win_rate": float((active_returns > 0).mean()) if len(active_returns) else 0.0,
        "avg_account_return": float(active_returns.mean()) if len(active_returns) else 0.0,
        "median_account_return": float(active_returns.median()) if len(active_returns) else 0.0,
        "final_equity": float(equity.iloc[-1]) if len(equity) else INITIAL_EQUITY,
        "equity_multiple": float(equity.iloc[-1] / INITIAL_EQUITY) if len(equity) else 1.0,
        "max_drawdown": max_drawdown(equity),
        "max_profit": float(active_returns.max()) if len(active_returns) else 0.0,
        "max_loss": float(active_returns.min()) if len(active_returns) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(active_returns),
    }


def load_best_filter() -> str:
    if not L_FILTERS_PATH.exists():
        raise FileNotFoundError(f"找不到 L 收益优先过滤结果: {L_FILTERS_PATH}")
    filters = pd.read_csv(L_FILTERS_PATH, low_memory=False)
    if filters.empty:
        raise ValueError("L 收益优先过滤结果为空")
    return str(filters.iloc[0]["filter"])


def apply_filter_expression(data: pd.DataFrame, expression: str) -> pd.DataFrame:
    result = data.copy()
    for raw_part in expression.split(" AND "):
        part = raw_part.strip()
        if "!=" not in part:
            raise ValueError(f"暂不支持的过滤表达式: {part}")
        column, value = [item.strip() for item in part.split("!=", 1)]
        if column not in result.columns:
            raise KeyError(f"L交易明细缺少过滤字段: {column}")
        result = result[result[column].fillna("missing").astype(str).ne(value)].copy()
    return result


def load_l_trades() -> tuple[pd.DataFrame, str]:
    if not L_TRADES_PATH.exists():
        raise FileNotFoundError(f"找不到 L 交易明细: {L_TRADES_PATH}")
    trades = pd.read_csv(L_TRADES_PATH, dtype={"trade_date": str}, low_memory=False)
    trades = trades[trades["l_rule"].astype(str).eq(TARGET_L_RULE)].copy()
    trades["l_account_return"] = to_numeric(trades["l_account_return"])
    best_filter = load_best_filter()
    filtered = apply_filter_expression(trades, best_filter)
    filtered = filtered.sort_values(["trade_date", "l_account_return"], ascending=[True, False])
    filtered = filtered.drop_duplicates("trade_date", keep="first").copy()
    return filtered, best_filter


def load_baseline_daily() -> pd.DataFrame:
    if not BASELINE_DAILY_PATH.exists():
        raise FileNotFoundError(f"找不到当前组合审计明细: {BASELINE_DAILY_PATH}")
    daily = pd.read_csv(BASELINE_DAILY_PATH, dtype={"signal_date": str}, low_memory=False)
    daily = daily.sort_values("signal_date").reset_index(drop=True)
    daily["baseline_return"] = to_numeric(daily.get("account_return_recalc", pd.Series(0.0, index=daily.index)))
    daily["baseline_strategy_leg"] = daily.get("strategy_leg", pd.Series("NONE", index=daily.index)).fillna("NONE").astype(str)
    daily["baseline_operation_status"] = daily.get("operation_status", pd.Series("", index=daily.index)).fillna("").astype(str)
    daily["baseline_ts_code"] = daily.get("ts_code", pd.Series("", index=daily.index)).fillna("").astype(str)
    daily["baseline_name"] = daily.get("name", pd.Series("", index=daily.index)).fillna("").astype(str)
    daily["is_idle_day"] = daily["baseline_strategy_leg"].isin(IDLE_STRATEGY_LEGS)
    return daily


def build_overlay_daily(baseline: pd.DataFrame, l_trades: pd.DataFrame) -> pd.DataFrame:
    l_cols = [
        "trade_date",
        "ts_code",
        "name",
        "l_account_return",
        "market_segment",
        "theme_name",
        "segment_retreat_state_bucket",
        "theme_limit_count",
    ]
    available_cols = [column for column in l_cols if column in l_trades.columns]
    l_daily = l_trades[available_cols].copy()
    l_daily = l_daily.rename(columns={
        "trade_date": "signal_date",
        "ts_code": "l_ts_code",
        "name": "l_name",
        "l_account_return": "l_return",
    })
    merged = baseline.merge(l_daily, on="signal_date", how="left")
    merged["has_l_signal"] = merged["l_return"].notna()
    merged["l_return"] = to_numeric(merged["l_return"])

    merged["abce2_return"] = merged["baseline_return"]
    merged["abce2_op"] = merged["baseline_strategy_leg"].where(merged["baseline_return"].ne(0), "NO_TRADE")

    use_l_supplement = merged["is_idle_day"] & merged["has_l_signal"]
    merged["abce2_plus_l_idle_return"] = merged["baseline_return"].where(~use_l_supplement, merged["l_return"])
    merged["abce2_plus_l_idle_op"] = merged["baseline_strategy_leg"]
    merged.loc[use_l_supplement, "abce2_plus_l_idle_op"] = "L_SUPPLEMENT"
    merged.loc[~use_l_supplement & merged["baseline_return"].eq(0), "abce2_plus_l_idle_op"] = "NO_TRADE"

    use_l_priority = merged["has_l_signal"]
    merged["l_priority_return"] = merged["baseline_return"].where(~use_l_priority, merged["l_return"])
    merged["l_priority_op"] = merged["baseline_strategy_leg"]
    merged.loc[use_l_priority, "l_priority_op"] = "L_PRIORITY"
    merged.loc[~use_l_priority & merged["baseline_return"].eq(0), "l_priority_op"] = "NO_TRADE"
    return merged


def build_conflicts(daily: pd.DataFrame) -> pd.DataFrame:
    conflict = daily[daily["has_l_signal"] & ~daily["is_idle_day"]].copy()
    if conflict.empty:
        return conflict
    conflict["return_diff_l_minus_baseline"] = conflict["l_return"] - conflict["baseline_return"]
    columns = [
        "signal_date",
        "baseline_strategy_leg",
        "baseline_operation_status",
        "baseline_ts_code",
        "baseline_name",
        "baseline_return",
        "l_ts_code",
        "l_name",
        "l_return",
        "return_diff_l_minus_baseline",
        "market_segment",
        "theme_name",
        "segment_retreat_state_bucket",
        "theme_limit_count",
    ]
    return conflict[[column for column in columns if column in conflict.columns]].copy()


def write_report(summary: pd.DataFrame, conflicts: pd.DataFrame, best_filter: str) -> None:
    report_path = OUTPUT_DIR / "leader_combo_overlay_report.md"
    best = summary.sort_values("equity_multiple", ascending=False).iloc[0]
    lines = [
        "# L 龙头策略组合叠加测算",
        "",
        "说明：本报告只做离线研究，不接实盘，不修改 ABC/E2/D。",
        "",
        f"- L 使用规则：{TARGET_L_RULE}",
        f"- L 收益优先过滤：{best_filter}",
        f"- 最优叠加方案：{best['scenario']}，复利 {best['equity_multiple']:.2f} 倍，最大回撤 {best['max_drawdown']:.2%}",
        f"- L 与现有组合冲突交易日：{len(conflicts)} 个",
        "",
        "## 汇总",
        "",
        summary.to_markdown(index=False),
    ]
    if not conflicts.empty:
        top = conflicts.sort_values("return_diff_l_minus_baseline", ascending=False).head(20)
        lines.extend([
            "",
            "## 冲突样本前20",
            "",
            top.to_markdown(index=False),
        ])
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = load_baseline_daily()
    l_trades, best_filter = load_l_trades()
    daily = build_overlay_daily(baseline, l_trades)
    conflicts = build_conflicts(daily)

    summary_rows = [
        calc_metrics("当前ABCE2审计口径", daily, "abce2_return", "abce2_op"),
        calc_metrics("ABCE2优先，L只补空闲日", daily, "abce2_plus_l_idle_return", "abce2_plus_l_idle_op"),
        calc_metrics("L优先，有L信号则替换当日原组合", daily, "l_priority_return", "l_priority_op"),
    ]
    summary = pd.DataFrame(summary_rows)
    summary.to_csv(OUTPUT_DIR / "leader_combo_overlay_summary.csv", index=False)
    daily.to_csv(OUTPUT_DIR / "leader_combo_overlay_daily.csv", index=False)
    conflicts.to_csv(OUTPUT_DIR / "leader_combo_overlay_conflicts.csv", index=False)
    write_report(summary, conflicts, best_filter)

    print("L组合叠加测算完成")
    print(f"L过滤条件: {best_filter}")
    print(summary.to_string(index=False))
    print(f"冲突交易日: {len(conflicts)}")
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
