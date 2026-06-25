"""
复盘 model=3 稳健规则的逐日冲突。

只做离线复盘，不接实盘，不修改当前 mode=1 配置。

目标：
  - 固定 model=3 稳健第一名规则，不再搜索。
  - 找出 model=3 选择 L 的每一天，mode=1 原本是什么状态。
  - 统计 L 替换/补充 mode=1 后，哪些交易贡献收益，哪些交易加深回撤。

输出：
  reports/strategy_model3/conflict_audit/model3_conflict_daily.csv
  reports/strategy_model3/conflict_audit/model3_conflict_summary.csv
  reports/strategy_model3/conflict_audit/model3_conflict_yearly.csv
  reports/strategy_model3/conflict_audit/model3_conflict_report.md
"""
from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.research_strategy_model3_switch import (  # noqa: E402
    build_l_lookup,
    build_switch_atoms,
    calc_metrics,
    load_baseline_daily,
    replay_model3,
    selected_l2_source,
)
from scripts.validate_strategy_model3_switch import parse_rule, rule_text  # noqa: E402


ROBUST_RULES_PATH = PROJECT_ROOT / "reports" / "strategy_model3" / "robust" / "model3_robust_rules.csv"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "strategy_model3" / "conflict_audit"


def load_robust_rule() -> tuple[str, tuple]:
    if not ROBUST_RULES_PATH.exists():
        raise FileNotFoundError(f"找不到稳健规则结果: {ROBUST_RULES_PATH}")
    rules = pd.read_csv(ROBUST_RULES_PATH, low_memory=False)
    if rules.empty:
        raise ValueError("稳健规则结果为空")
    text = str(rules.iloc[0]["rule"])
    atom_map = {atom.name: atom for atom in build_switch_atoms()}
    parsed = parse_rule(text, atom_map)
    if parsed is None:
        raise ValueError(f"无法解析稳健规则: {text}")
    return text, parsed


def classify_conflict(row: pd.Series) -> str:
    mode1_return = float(row.get("mode1_return", 0.0) or 0.0)
    status = str(row.get("mode1_operation_status", ""))
    if abs(mode1_return) > 1e-12:
        return "L_REPLACE_MODE1_RETURN_DAY"
    if status == "POSITION_OCCUPIED_SKIP":
        return "L_WHILE_MODE1_POSITION_OCCUPIED"
    if status == "NO_CANDIDATE":
        return "L_SUPPLEMENT_MODE1_IDLE"
    return "L_OTHER"


def max_drawdown_window(daily: pd.DataFrame, return_col: str) -> dict[str, object]:
    equity = 500_000.0 * (1.0 + pd.to_numeric(daily[return_col], errors="coerce").fillna(0.0)).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    trough_idx = int(drawdown.idxmin())
    peak_value = float(peak.iloc[trough_idx])
    prev = equity.iloc[:trough_idx + 1]
    peak_idx = int(prev[prev.round(6).eq(round(peak_value, 6))].index[-1])
    return {
        "peak_date": str(daily.loc[peak_idx, "date"]),
        "trough_date": str(daily.loc[trough_idx, "date"]),
        "peak_equity": peak_value,
        "trough_equity": float(equity.iloc[trough_idx]),
        "max_drawdown": float(drawdown.iloc[trough_idx]),
    }


def build_audit_daily() -> tuple[str, pd.DataFrame]:
    rule_text_value, rule = load_robust_rule()
    baseline = load_baseline_daily()
    l_source = selected_l2_source()
    l_lookup = build_l_lookup(l_source)
    model3_daily, _metrics = replay_model3(baseline, l_lookup, rule)

    l_cols = [
        "trade_date",
        "ts_code",
        "name",
        "market_segment",
        "theme_name",
        "market_emotion_state_bucket",
        "segment_retreat_state_bucket",
        "market_chain_count_bucket",
        "market_limit_down_count_bucket",
        "first_time_detail_bucket",
        "open_times_bucket",
        "theme_limit_count",
        "same_theme_limit_count",
        "l_account_return",
    ]
    keep = [col for col in l_cols if col in l_source.columns]
    l_info = l_source[keep].rename(columns={
        "trade_date": "date",
        "ts_code": "l_source_ts_code",
        "name": "l_source_name",
        "l_account_return": "l_theory_account_return",
    })
    merged = model3_daily.merge(l_info, on="date", how="left")
    merged["mode1_vs_model3_return_diff"] = (
        pd.to_numeric(merged["model3_return"], errors="coerce").fillna(0.0)
        - pd.to_numeric(merged["mode1_return"], errors="coerce").fillna(0.0)
    )
    merged["conflict_type"] = merged.apply(
        lambda row: classify_conflict(row) if str(row.get("model3_op", "")) == "L" else "NOT_L_DAY",
        axis=1,
    )
    merged["selected_rule"] = rule_text_value
    return rule_text_value, merged


def summarize_conflicts(daily: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    l_days = daily[daily["model3_op"].astype(str).eq("L")].copy()
    summary = (
        l_days.groupby("conflict_type", dropna=False)
        .agg(
            count=("date", "count"),
            win_rate=("model3_return", lambda s: float((pd.to_numeric(s, errors="coerce") > 0).mean())),
            avg_l_return=("model3_return", lambda s: float(pd.to_numeric(s, errors="coerce").mean())),
            median_l_return=("model3_return", lambda s: float(pd.to_numeric(s, errors="coerce").median())),
            total_return_diff=("mode1_vs_model3_return_diff", lambda s: float(pd.to_numeric(s, errors="coerce").sum())),
            avg_return_diff=("mode1_vs_model3_return_diff", lambda s: float(pd.to_numeric(s, errors="coerce").mean())),
            worst_l_return=("model3_return", lambda s: float(pd.to_numeric(s, errors="coerce").min())),
            best_l_return=("model3_return", lambda s: float(pd.to_numeric(s, errors="coerce").max())),
        )
        .reset_index()
        .sort_values("total_return_diff", ascending=False)
    )
    l_days["year"] = l_days["date"].astype(str).str.slice(0, 4)
    yearly = (
        l_days.groupby("year", dropna=False)
        .agg(
            l_count=("date", "count"),
            l_win_rate=("model3_return", lambda s: float((pd.to_numeric(s, errors="coerce") > 0).mean())),
            avg_l_return=("model3_return", lambda s: float(pd.to_numeric(s, errors="coerce").mean())),
            total_return_diff=("mode1_vs_model3_return_diff", lambda s: float(pd.to_numeric(s, errors="coerce").sum())),
        )
        .reset_index()
    )
    return summary, yearly


def write_report(rule_text_value: str, daily: pd.DataFrame, summary: pd.DataFrame, yearly: pd.DataFrame) -> None:
    model3_metrics = calc_metrics("model3_robust", daily, "model3_return", "model3_op")
    mode1_metrics = calc_metrics("mode1", daily, "mode1_return", "mode1_operation_status")
    model3_dd = max_drawdown_window(daily, "model3_return")
    mode1_dd = max_drawdown_window(daily, "mode1_return")
    l_days = daily[daily["model3_op"].astype(str).eq("L")].copy()
    top_win = l_days.sort_values("model3_return", ascending=False).head(15)
    top_loss = l_days.sort_values("model3_return", ascending=True).head(15)
    top_positive_diff = l_days.sort_values("mode1_vs_model3_return_diff", ascending=False).head(15)
    top_negative_diff = l_days.sort_values("mode1_vs_model3_return_diff", ascending=True).head(15)

    show_cols = [
        "date",
        "l_ts_code",
        "l_name",
        "model3_return",
        "mode1_return",
        "mode1_vs_model3_return_diff",
        "conflict_type",
        "market_segment",
        "theme_name",
        "segment_retreat_state_bucket",
        "market_chain_count_bucket",
        "market_emotion_state_bucket",
    ]
    show_cols = [col for col in show_cols if col in daily.columns]

    lines = [
        "# model=3 稳健规则逐日冲突复盘",
        "",
        "说明：本报告只做离线复盘，不接实盘，不修改当前 mode=1 配置。",
        "",
        f"- 固定规则：{rule_text_value}",
        f"- model=3 全样本复利：{model3_metrics['equity_multiple']:.2f}倍，最大回撤 {model3_metrics['max_drawdown']:.2%}",
        f"- mode=1 全样本复利：{mode1_metrics['equity_multiple']:.2f}倍，最大回撤 {mode1_metrics['max_drawdown']:.2%}",
        f"- L切换交易数：{int(model3_metrics['l_trade_count'])}",
        "",
        "## 最大回撤窗口",
        "",
        "| 方案 | 前高日期 | 低点日期 | 前高权益 | 低点权益 | 最大回撤 |",
        "|---|---:|---:|---:|---:|---:|",
        f"| model=3 | {model3_dd['peak_date']} | {model3_dd['trough_date']} | {model3_dd['peak_equity']:.2f} | {model3_dd['trough_equity']:.2f} | {model3_dd['max_drawdown']:.2%} |",
        f"| mode=1 | {mode1_dd['peak_date']} | {mode1_dd['trough_date']} | {mode1_dd['peak_equity']:.2f} | {mode1_dd['trough_equity']:.2f} | {mode1_dd['max_drawdown']:.2%} |",
        "",
        "## L冲突类型汇总",
        "",
        summary.to_markdown(index=False),
        "",
        "## 年度L切换贡献",
        "",
        yearly.to_markdown(index=False),
        "",
        "## L收益最高前15",
        "",
        top_win[show_cols].to_markdown(index=False),
        "",
        "## L亏损最大前15",
        "",
        top_loss[show_cols].to_markdown(index=False),
        "",
        "## 相对mode=1改善最大前15",
        "",
        top_positive_diff[show_cols].to_markdown(index=False),
        "",
        "## 相对mode=1拖累最大前15",
        "",
        top_negative_diff[show_cols].to_markdown(index=False),
    ]
    (OUTPUT_DIR / "model3_conflict_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rule_text_value, daily = build_audit_daily()
    summary, yearly = summarize_conflicts(daily)
    daily.to_csv(OUTPUT_DIR / "model3_conflict_daily.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUTPUT_DIR / "model3_conflict_summary.csv", index=False, encoding="utf-8-sig")
    yearly.to_csv(OUTPUT_DIR / "model3_conflict_yearly.csv", index=False, encoding="utf-8-sig")
    write_report(rule_text_value, daily, summary, yearly)

    print("model=3 冲突复盘完成")
    print(f"规则: {rule_text_value}")
    print(summary.to_string(index=False))
    print(yearly.to_string(index=False))
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
