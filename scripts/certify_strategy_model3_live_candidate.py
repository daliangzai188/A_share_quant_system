"""
认证 model=3 最终候选规则的模拟实盘口径。

只做离线认证，不接实盘，不修改当前 mode=1 配置。

候选规则：
  - mode=1 默认优先。
  - L 通过稳健基础条件时才参与。
  - mode=1 空闲时允许 L 补位。
  - mode=1 有交易计划时，L 替换必须额外满足：
      market_segment = chi_next
      theme_limit_count >= 2
      first_time_detail_bucket != after_1430

输出：
  reports/strategy_model3/live_candidate/model3_live_candidate_summary.csv
  reports/strategy_model3/live_candidate/model3_live_candidate_trades.csv
  reports/strategy_model3/live_candidate/model3_live_candidate_drawdown.csv
  reports/strategy_model3/live_candidate/model3_live_candidate_yearly.csv
  reports/strategy_model3/live_candidate/model3_live_candidate_report.md
"""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.certify_strategy_l_live_execution import INITIAL_EQUITY, max_consecutive_losses, max_drawdown  # noqa: E402
from scripts.research_strategy_model3_switch import build_l_lookup, load_baseline_daily, selected_l2_source, to_numeric  # noqa: E402
from scripts.search_strategy_model3_occupancy_guards import (  # noqa: E402
    GuardRule,
    _theme_ge_2_not_after_1430,
    build_chi_next_base_rule,
    replay_guard,
)
from scripts.search_strategy_model3_safe_modes import markdown_table  # noqa: E402


OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_model3" / "live_candidate"
FINAL_GUARD = GuardRule(
    name="replace_theme_ge_2_not_after_1430",
    description="mode=3最终候选：L替换需创业板、题材涨停数>=2、且非after_1430。",
    replace_predicate=_theme_ge_2_not_after_1430,
)


def max_drawdown_window(daily: pd.DataFrame, return_col: str) -> dict[str, Any]:
    data = daily.sort_values("date").reset_index(drop=True).copy()
    returns = to_numeric(data[return_col])
    equity = INITIAL_EQUITY * (1.0 + returns).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    trough_idx = int(drawdown.idxmin())
    peak_value = float(peak.iloc[trough_idx])
    prev = equity.iloc[:trough_idx + 1]
    peak_idx = int(prev[prev.round(6).eq(round(peak_value, 6))].index[-1])

    recovery_idx = None
    for i in range(trough_idx + 1, len(equity)):
        if float(equity.iloc[i]) >= peak_value:
            recovery_idx = i
            break

    peak_date = pd.to_datetime(str(data.loc[peak_idx, "date"]))
    trough_date = pd.to_datetime(str(data.loc[trough_idx, "date"]))
    recovery_date = pd.to_datetime(str(data.loc[recovery_idx, "date"])) if recovery_idx is not None else None

    return {
        "peak_date": str(data.loc[peak_idx, "date"]),
        "trough_date": str(data.loc[trough_idx, "date"]),
        "recovery_date": "" if recovery_idx is None else str(data.loc[recovery_idx, "date"]),
        "peak_equity": peak_value,
        "trough_equity": float(equity.iloc[trough_idx]),
        "max_drawdown": float(drawdown.iloc[trough_idx]),
        "peak_to_trough_days": int((trough_date - peak_date).days),
        "trough_to_recovery_days": None if recovery_date is None else int((recovery_date - trough_date).days),
        "peak_to_trough_rows": int(trough_idx - peak_idx + 1),
        "trough_to_recovery_rows": None if recovery_idx is None else int(recovery_idx - trough_idx + 1),
    }


def trade_detail(daily: pd.DataFrame) -> pd.DataFrame:
    trades = daily[to_numeric(daily["model3_return"]).abs().gt(1e-12)].copy()
    trades["trade_return"] = to_numeric(trades["model3_return"])
    trades["trade_type"] = trades["model3_op"].astype(str).map(lambda x: "L" if x == "L" else "MODE1")
    trades["equity"] = INITIAL_EQUITY * (1.0 + trades["trade_return"]).cumprod()
    trades["is_win"] = trades["trade_return"] > 0
    return trades


def summary_metrics(daily: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    returns = to_numeric(daily["model3_return"])
    equity = INITIAL_EQUITY * (1.0 + returns).cumprod()
    trade_returns = to_numeric(trades["trade_return"]) if not trades.empty else pd.Series(dtype=float)
    l_trades = trades[trades["trade_type"].eq("L")]
    mode1_trades = trades[trades["trade_type"].eq("MODE1")]
    dd_window = max_drawdown_window(daily, "model3_return")

    rows = [{
        "scenario": "model3_live_candidate",
        "day_count": int(len(daily)),
        "trade_count": int(len(trades)),
        "l_trade_count": int(len(l_trades)),
        "mode1_trade_count": int(len(mode1_trades)),
        "win_rate": float((trade_returns > 0).mean()) if len(trade_returns) else 0.0,
        "avg_return": float(trade_returns.mean()) if len(trade_returns) else 0.0,
        "median_return": float(trade_returns.median()) if len(trade_returns) else 0.0,
        "equity_multiple": float(equity.iloc[-1] / INITIAL_EQUITY) if len(equity) else 1.0,
        "max_drawdown": max_drawdown(equity),
        "max_profit": float(trade_returns.max()) if len(trade_returns) else 0.0,
        "max_loss": float(trade_returns.min()) if len(trade_returns) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(trade_returns),
        "l_win_rate": float((to_numeric(l_trades["trade_return"]) > 0).mean()) if len(l_trades) else 0.0,
        "l_avg_return": float(to_numeric(l_trades["trade_return"]).mean()) if len(l_trades) else 0.0,
        "l_median_return": float(to_numeric(l_trades["trade_return"]).median()) if len(l_trades) else 0.0,
        "mode1_win_rate": float((to_numeric(mode1_trades["trade_return"]) > 0).mean()) if len(mode1_trades) else 0.0,
        "mode1_avg_return": float(to_numeric(mode1_trades["trade_return"]).mean()) if len(mode1_trades) else 0.0,
        "mode1_median_return": float(to_numeric(mode1_trades["trade_return"]).median()) if len(mode1_trades) else 0.0,
        **dd_window,
    }]
    return pd.DataFrame(rows)


def yearly_metrics(daily: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    data = daily.copy()
    data["year"] = data["date"].astype(str).str.slice(0, 4)
    for year, group in data.groupby("year"):
        trades = trade_detail(group)
        summary = summary_metrics(group, trades).iloc[0].to_dict()
        summary["year"] = year
        rows.append(summary)
    return pd.DataFrame(rows).sort_values("year").reset_index(drop=True)


def drawdown_series(daily: pd.DataFrame) -> pd.DataFrame:
    data = daily.sort_values("date").reset_index(drop=True).copy()
    returns = to_numeric(data["model3_return"])
    data["equity_model3"] = INITIAL_EQUITY * (1.0 + returns).cumprod()
    data["peak_model3"] = data["equity_model3"].cummax()
    data["drawdown_model3"] = data["equity_model3"] / data["peak_model3"] - 1.0
    data["equity_mode1"] = INITIAL_EQUITY * (1.0 + to_numeric(data["mode1_return"])).cumprod()
    data["peak_mode1"] = data["equity_mode1"].cummax()
    data["drawdown_mode1"] = data["equity_mode1"] / data["peak_mode1"] - 1.0
    return data[[
        "date",
        "model3_return",
        "mode1_return",
        "equity_model3",
        "drawdown_model3",
        "equity_mode1",
        "drawdown_mode1",
        "model3_op",
        "guard_rule",
        "conflict_type",
    ]]


def write_report(summary: pd.DataFrame, yearly: pd.DataFrame, trades: pd.DataFrame, dd: pd.DataFrame) -> None:
    summary_cols = [
        "scenario",
        "trade_count",
        "l_trade_count",
        "mode1_trade_count",
        "win_rate",
        "avg_return",
        "median_return",
        "equity_multiple",
        "max_drawdown",
        "max_profit",
        "max_loss",
        "max_consecutive_losses",
        "peak_date",
        "trough_date",
        "recovery_date",
        "peak_to_trough_days",
        "trough_to_recovery_days",
    ]
    yearly_cols = [
        "year",
        "trade_count",
        "l_trade_count",
        "win_rate",
        "avg_return",
        "median_return",
        "equity_multiple",
        "max_drawdown",
        "max_loss",
        "max_consecutive_losses",
    ]
    trade_cols = [
        "date",
        "trade_type",
        "model3_return",
        "mode1_return",
        "model3_op",
        "l_ts_code",
        "l_name",
        "l_exit_date",
        "conflict_type",
        "mode1_vs_model3_return_diff",
    ]
    trade_cols = [col for col in trade_cols if col in trades.columns]
    dd_worst = dd.sort_values("drawdown_model3", ascending=True).head(20)
    lines = [
        "# model=3 最终候选模拟实盘口径认证",
        "",
        "说明：本报告只做离线认证，不接实盘，不修改当前 mode=1 配置。",
        "",
        "## 候选规则",
        "",
        "- mode=1 默认优先。",
        "- L 通过稳健基础条件时才参与。",
        "- mode=1 空闲时允许 L 补位。",
        "- mode=1 有交易计划时，L 替换必须满足：创业板、theme_limit_count>=2、first_time_detail_bucket!=after_1430。",
        "",
        "## 总体认证",
        "",
        markdown_table(summary[summary_cols]),
        "",
        "## 年度表现",
        "",
        markdown_table(yearly[yearly_cols]),
        "",
        "## 最大回撤附近日期",
        "",
        markdown_table(dd_worst),
        "",
        "## 最大亏损交易前20",
        "",
        markdown_table(trades.sort_values("model3_return", ascending=True).head(20)[trade_cols]),
        "",
        "## 最大盈利交易前20",
        "",
        markdown_table(trades.sort_values("model3_return", ascending=False).head(20)[trade_cols]),
        "",
        "## 认证结论",
        "",
        "- 该规则通过当前离线模拟认证，但仍不是实盘许可。",
        "- 接入实盘前还需要写入配置且默认关闭，并在模拟盘/小资金阶段复核日志、委托、撤单、持仓释放和通知。",
        "- 不承诺固定收益；该结果依赖历史样本，仍存在过拟合和未来行情失效风险。",
    ]
    (OUTPUT_DIR / "model3_live_candidate_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = load_baseline_daily()
    l_lookup = build_l_lookup(selected_l2_source())
    base_rule = build_chi_next_base_rule()
    daily, _metrics = replay_guard(baseline, l_lookup, base_rule, FINAL_GUARD)
    trades = trade_detail(daily)
    summary = summary_metrics(daily, trades)
    yearly = yearly_metrics(daily)
    dd = drawdown_series(daily)

    summary.to_csv(OUTPUT_DIR / "model3_live_candidate_summary.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUTPUT_DIR / "model3_live_candidate_trades.csv", index=False, encoding="utf-8-sig")
    dd.to_csv(OUTPUT_DIR / "model3_live_candidate_drawdown.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(OUTPUT_DIR / "model3_live_candidate_yearly.csv", index=False, encoding="utf-8-sig")
    write_report(summary, yearly, trades, dd)

    print("model=3 最终候选模拟实盘口径认证完成")
    print(summary[[
        "trade_count",
        "l_trade_count",
        "mode1_trade_count",
        "win_rate",
        "avg_return",
        "median_return",
        "equity_multiple",
        "max_drawdown",
        "max_loss",
        "max_consecutive_losses",
        "peak_date",
        "trough_date",
        "recovery_date",
    ]].to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
