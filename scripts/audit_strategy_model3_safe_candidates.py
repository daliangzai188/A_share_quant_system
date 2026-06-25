"""
审计 model=3 安全候选规则。

只做离线研究，不接实盘，不修改当前 mode=1 配置。

审计对象：
  - idle_plus_replace_not_sh_main
  - idle_plus_replace_chi_next

目标：
  1. 对比两个候选规则的逐年、逐月收益和回撤。
  2. 找出最大回撤窗口、恢复前高耗时。
  3. 列出 L 切换相对 mode=1 的拖累样本和改善样本。
  4. 补充 L 信号的市场/题材字段，判断失败样本是否集中在某类环境。

输出：
  reports/strategy_model3/safe_candidates/model3_safe_candidate_summary.csv
  reports/strategy_model3/safe_candidates/model3_safe_candidate_yearly.csv
  reports/strategy_model3/safe_candidates/model3_safe_candidate_monthly.csv
  reports/strategy_model3/safe_candidates/model3_safe_candidate_l_trades.csv
  reports/strategy_model3/safe_candidates/model3_safe_candidate_factor_loss.csv
  reports/strategy_model3/safe_candidates/model3_safe_candidate_report.md
"""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.research_strategy_model3_switch import (  # noqa: E402
    INITIAL_EQUITY,
    calc_metrics,
    selected_l2_source,
    to_numeric,
)
from scripts.search_strategy_model3_safe_modes import markdown_table  # noqa: E402


SAFE_DETAIL_PATH = PROJECT_ROOT / "reports" / "strategy_model3" / "safe_modes" / "model3_safe_modes_detail.csv"
SAFE_SUMMARY_PATH = PROJECT_ROOT / "reports" / "strategy_model3" / "safe_modes" / "model3_safe_modes_summary.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_model3" / "safe_candidates"

CANDIDATES = [
    "idle_plus_replace_not_sh_main",
    "idle_plus_replace_chi_next",
]


def load_safe_detail() -> pd.DataFrame:
    if not SAFE_DETAIL_PATH.exists():
        raise FileNotFoundError(f"找不到安全模式明细，请先运行 search_strategy_model3_safe_modes.py: {SAFE_DETAIL_PATH}")
    detail = pd.read_csv(SAFE_DETAIL_PATH, dtype={"date": str}, low_memory=False)
    detail["date"] = detail["date"].astype(str)
    return detail[detail["safe_policy"].isin(CANDIDATES)].copy()


def load_l_features() -> pd.DataFrame:
    source = selected_l2_source().copy()
    keep = [
        "trade_date",
        "ts_code",
        "name",
        "market_segment",
        "theme_name",
        "market_emotion_state_bucket",
        "segment_retreat_state_bucket",
        "market_chain_count_bucket",
        "market_limit_down_count_bucket",
        "segment_limit_up_count_bucket",
        "segment_limit_down_count_bucket",
        "first_time_detail_bucket",
        "open_times_bucket",
        "theme_limit_count",
        "same_theme_limit_count",
        "l_account_return",
    ]
    keep = [col for col in keep if col in source.columns]
    features = source[keep].rename(columns={
        "trade_date": "date",
        "ts_code": "l_source_ts_code",
        "name": "l_source_name",
        "l_account_return": "l_theory_account_return",
    })
    features["date"] = features["date"].astype(str)
    return features


def enrich_l_trades(detail: pd.DataFrame) -> pd.DataFrame:
    l_days = detail[detail["model3_op"].astype(str).eq("L")].copy()
    features = load_l_features()
    merged = l_days.merge(features, on="date", how="left")
    return merged


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
        "peak_to_trough_trade_rows": int(trough_idx - peak_idx + 1),
        "trough_to_recovery_trade_rows": None if recovery_idx is None else int(recovery_idx - trough_idx + 1),
    }


def period_metrics(detail: pd.DataFrame, period: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for policy, policy_data in detail.groupby("safe_policy"):
        data = policy_data.copy()
        if period == "year":
            data["period"] = data["date"].astype(str).str.slice(0, 4)
        elif period == "month":
            data["period"] = data["date"].astype(str).str.slice(0, 6)
        else:
            raise ValueError(period)
        for value, group in data.groupby("period"):
            metrics = calc_metrics(policy, group, "model3_return", "model3_op")
            metrics["safe_policy"] = policy
            metrics["period"] = value
            rows.append(metrics)
    return pd.DataFrame(rows).sort_values(["safe_policy", "period"]).reset_index(drop=True)


def candidate_summary(detail: pd.DataFrame) -> pd.DataFrame:
    safe_summary = pd.read_csv(SAFE_SUMMARY_PATH, low_memory=False) if SAFE_SUMMARY_PATH.exists() else pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for policy, group in detail.groupby("safe_policy"):
        metrics = calc_metrics(policy, group, "model3_return", "model3_op")
        metrics["safe_policy"] = policy
        metrics.update(max_drawdown_window(group, "model3_return"))
        if not safe_summary.empty:
            source = safe_summary[safe_summary["policy"].astype(str).eq(policy)]
            if not source.empty:
                for col in [
                    "pass_multiple_count",
                    "pass_drawdown_count",
                    "pass_both_count",
                    "min_multiple_ratio",
                    "avg_multiple_ratio",
                    "l_supplement_count",
                    "l_replace_count",
                    "total_return_diff",
                ]:
                    if col in source.columns:
                        metrics[col] = source.iloc[0][col]
        rows.append(metrics)
    return pd.DataFrame(rows).sort_values(
        ["equity_multiple", "max_drawdown"],
        ascending=[False, False],
    ).reset_index(drop=True)


def factor_loss_summary(l_trades: pd.DataFrame) -> pd.DataFrame:
    if l_trades.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    factor_cols = [
        "safe_policy",
        "conflict_type",
        "market_segment",
        "theme_name",
        "market_emotion_state_bucket",
        "segment_retreat_state_bucket",
        "market_chain_count_bucket",
        "market_limit_down_count_bucket",
        "segment_limit_up_count_bucket",
        "segment_limit_down_count_bucket",
        "first_time_detail_bucket",
        "open_times_bucket",
    ]
    factor_cols = [col for col in factor_cols if col in l_trades.columns]
    for col in factor_cols:
        if col == "safe_policy":
            continue
        grouped = (
            l_trades.groupby(["safe_policy", col], dropna=False)
            .agg(
                count=("date", "count"),
                avg_diff=("mode1_vs_model3_return_diff", lambda s: float(pd.to_numeric(s, errors="coerce").mean())),
                total_diff=("mode1_vs_model3_return_diff", lambda s: float(pd.to_numeric(s, errors="coerce").sum())),
                min_diff=("mode1_vs_model3_return_diff", lambda s: float(pd.to_numeric(s, errors="coerce").min())),
                avg_l_return=("model3_return", lambda s: float(pd.to_numeric(s, errors="coerce").mean())),
            )
            .reset_index()
            .rename(columns={col: "factor_value"})
        )
        grouped["factor"] = col
        rows.append(grouped)
    result = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return result.sort_values(["min_diff", "count"], ascending=[True, False]).reset_index(drop=True)


def write_report(
    summary: pd.DataFrame,
    yearly: pd.DataFrame,
    monthly: pd.DataFrame,
    l_trades: pd.DataFrame,
    factor_loss: pd.DataFrame,
) -> None:
    top_loss_cols = [
        "date",
        "safe_policy",
        "l_ts_code",
        "l_name",
        "model3_return",
        "mode1_return",
        "mode1_vs_model3_return_diff",
        "conflict_type",
        "market_segment",
        "theme_name",
        "market_emotion_state_bucket",
        "segment_retreat_state_bucket",
        "market_chain_count_bucket",
    ]
    top_loss_cols = [col for col in top_loss_cols if col in l_trades.columns]
    summary_cols = [
        "safe_policy",
        "equity_multiple",
        "max_drawdown",
        "trade_count",
        "l_trade_count",
        "pass_both_count",
        "min_multiple_ratio",
        "peak_date",
        "trough_date",
        "recovery_date",
        "peak_to_trough_days",
        "trough_to_recovery_days",
        "l_supplement_count",
        "l_replace_count",
        "total_return_diff",
    ]
    summary_cols = [col for col in summary_cols if col in summary.columns]
    yearly_cols = [
        "safe_policy",
        "period",
        "equity_multiple",
        "max_drawdown",
        "trade_count",
        "l_trade_count",
        "win_rate",
        "avg_account_return",
        "max_loss",
    ]
    yearly_cols = [col for col in yearly_cols if col in yearly.columns]
    monthly_bad = monthly.sort_values(["max_drawdown", "equity_multiple"], ascending=[True, True]).head(20)
    factor_cols = [
        "safe_policy",
        "factor",
        "factor_value",
        "count",
        "avg_diff",
        "total_diff",
        "min_diff",
        "avg_l_return",
    ]
    factor_cols = [col for col in factor_cols if col in factor_loss.columns]

    lines = [
        "# model=3 安全候选规则细化审计",
        "",
        "说明：本报告只做离线研究，不接实盘，不修改当前 mode=1 配置。",
        "",
        "## 候选规则总览",
        "",
        markdown_table(summary[summary_cols]),
        "",
        "## 逐年表现",
        "",
        markdown_table(yearly[yearly_cols]),
        "",
        "## 最差月份前20",
        "",
        markdown_table(monthly_bad[yearly_cols]),
        "",
        "## L切换拖累最大前20",
        "",
        markdown_table(l_trades.sort_values("mode1_vs_model3_return_diff", ascending=True).head(20)[top_loss_cols]),
        "",
        "## L切换改善最大前20",
        "",
        markdown_table(l_trades.sort_values("mode1_vs_model3_return_diff", ascending=False).head(20)[top_loss_cols]),
        "",
        "## 失败/拖累因子聚合前30",
        "",
        markdown_table(factor_loss.head(30)[factor_cols]) if len(factor_loss) else "无结果。",
        "",
        "## 初步结论",
        "",
        "- 两个候选规则当前全样本复利相同，说明新增过滤主要是在规避少数拖累样本，而不是改变大部分 L 交易。",
        "- `idle_plus_replace_chi_next` 交易数更少，限制更窄；`idle_plus_replace_not_sh_main` 覆盖稍广。",
        "- 下一步不建议直接接实盘，应继续做“前半段定规则、后半段只验证”的候选二选一验证，并检查规则是否依赖单笔极端收益。",
    ]
    (OUTPUT_DIR / "model3_safe_candidate_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    detail = load_safe_detail()
    l_trades = enrich_l_trades(detail)
    summary = candidate_summary(detail)
    yearly = period_metrics(detail, "year")
    monthly = period_metrics(detail, "month")
    factor_loss = factor_loss_summary(l_trades)

    summary.to_csv(OUTPUT_DIR / "model3_safe_candidate_summary.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(OUTPUT_DIR / "model3_safe_candidate_yearly.csv", index=False, encoding="utf-8-sig")
    monthly.to_csv(OUTPUT_DIR / "model3_safe_candidate_monthly.csv", index=False, encoding="utf-8-sig")
    l_trades.to_csv(OUTPUT_DIR / "model3_safe_candidate_l_trades.csv", index=False, encoding="utf-8-sig")
    factor_loss.to_csv(OUTPUT_DIR / "model3_safe_candidate_factor_loss.csv", index=False, encoding="utf-8-sig")
    write_report(summary, yearly, monthly, l_trades, factor_loss)

    print("model=3 安全候选规则细化审计完成")
    print(summary[[
        "safe_policy",
        "equity_multiple",
        "max_drawdown",
        "trade_count",
        "l_trade_count",
        "peak_date",
        "trough_date",
        "recovery_date",
        "pass_both_count",
        "min_multiple_ratio",
    ]].to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
