"""
验证 bj_cap_2pct 前视过滤规则的稳定性。

文件作用：
1. 比较完整回放基准与最优前视过滤规则的年度、季度、月度表现。
2. 统计过滤规则在不同时间段是否稳定改善收益和回撤。
3. 展示被过滤交易在基准口径下原本的收益，判断是否只是事后规避亏损。
4. 展示释放持仓后新增交易，检查过滤后替换出来的交易质量。

本脚本只读取本地完整回放报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
BASE_RULE = "bj_cap_2pct_full_replay"
BEST_RULE = "filter_fd_1pct_2pct_market_down_lt5"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证 bj_cap_2pct 前视过滤规则稳定性。")
    parser.add_argument(
        "--input",
        default="reports/bj_cap2_full_replay_forward_filters_detail.csv",
        help="完整回放逐笔明细报告。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/bj_cap2_forward_filter_stability",
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


def period_label(date_text: str, period: str) -> str:
    if not date_text:
        return "unknown"
    year = date_text[:4]
    month = int(date_text[4:6])
    if period == "year":
        return year
    if period == "quarter":
        quarter = (month - 1) // 3 + 1
        return f"{year}Q{quarter}"
    if period == "month":
        return date_text[:6]
    raise ValueError(f"不支持的周期: {period}")


def load_detail(path: Path) -> pd.DataFrame:
    detail = pd.read_csv(path, low_memory=False)
    detail["trade_date"] = detail["trade_date"].map(normalize_date)
    detail["exit_trade_date"] = detail["exit_trade_date"].map(normalize_date)
    detail["selected_order"] = pd.to_numeric(detail["selected_order"], errors="coerce")
    detail["replay_account_return"] = pd.to_numeric(detail["replay_account_return"], errors="coerce").fillna(0.0)
    detail["replay_equity_before"] = pd.to_numeric(detail["replay_equity_before"], errors="coerce")
    detail["replay_equity_after"] = pd.to_numeric(detail["replay_equity_after"], errors="coerce")
    return detail


def summarize_period(group: pd.DataFrame, period_name: str) -> dict[str, object]:
    executed = group[group["replay_executed"] == True].copy()  # noqa: E712
    returns = executed["replay_account_return"].astype(float) if not executed.empty else pd.Series(dtype=float)
    if executed.empty:
        return {
            "period": period_name,
            "trade_count": 0,
            "period_return": 0.0,
            "win_rate": 0.0,
            "avg_account_return": 0.0,
            "median_account_return": 0.0,
            "max_profit": 0.0,
            "max_loss": 0.0,
            "max_drawdown": 0.0,
            "max_consecutive_losses": 0,
        }
    first_equity = float(executed["replay_equity_before"].iloc[0])
    last_equity = float(executed["replay_equity_after"].iloc[-1])
    return {
        "period": period_name,
        "trade_count": int(len(executed)),
        "period_return": last_equity / first_equity - 1 if first_equity else 0.0,
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "max_drawdown": max_drawdown(executed["replay_equity_after"].astype(float)),
        "max_consecutive_losses": max_consecutive_losses(returns),
    }


def build_period_compare(detail: pd.DataFrame, period: str) -> pd.DataFrame:
    rows = []
    for rule_name in [BASE_RULE, BEST_RULE]:
        rule_rows = detail[detail["replay_rule_name"] == rule_name].copy()
        rule_rows["period"] = rule_rows["exit_trade_date"].map(lambda value: period_label(value, period))
        for period_name, group in rule_rows.groupby("period"):
            if period_name == "unknown":
                continue
            summary = summarize_period(group, period_name)
            summary["rule_name"] = rule_name
            rows.append(summary)
    report = pd.DataFrame(rows)
    if report.empty:
        return report
    base = report[report["rule_name"] == BASE_RULE].set_index("period")
    best = report[report["rule_name"] == BEST_RULE].set_index("period")
    compare_rows = []
    for period_name in sorted(set(base.index) | set(best.index)):
        base_row = base.loc[period_name].to_dict() if period_name in base.index else {}
        best_row = best.loc[period_name].to_dict() if period_name in best.index else {}
        compare_rows.append(
            {
                "period": period_name,
                "base_trade_count": int(base_row.get("trade_count", 0)),
                "best_trade_count": int(best_row.get("trade_count", 0)),
                "base_return": float(base_row.get("period_return", 0.0)),
                "best_return": float(best_row.get("period_return", 0.0)),
                "return_delta": float(best_row.get("period_return", 0.0)) - float(base_row.get("period_return", 0.0)),
                "base_max_drawdown": float(base_row.get("max_drawdown", 0.0)),
                "best_max_drawdown": float(best_row.get("max_drawdown", 0.0)),
                "drawdown_delta": float(best_row.get("max_drawdown", 0.0)) - float(base_row.get("max_drawdown", 0.0)),
                "base_win_rate": float(base_row.get("win_rate", 0.0)),
                "best_win_rate": float(best_row.get("win_rate", 0.0)),
                "win_rate_delta": float(best_row.get("win_rate", 0.0)) - float(base_row.get("win_rate", 0.0)),
            }
        )
    return pd.DataFrame(compare_rows)


def build_filtered_trade_report(detail: pd.DataFrame) -> pd.DataFrame:
    base = detail[detail["replay_rule_name"] == BASE_RULE].copy()
    best = detail[detail["replay_rule_name"] == BEST_RULE].copy()
    base_by_order = base.set_index("selected_order")
    filtered = best[best["replay_skip_reason"].astype(str) == "forward_risk_filter"].copy()
    rows = []
    for _, row in filtered.iterrows():
        selected_order = row["selected_order"]
        base_row = base_by_order.loc[selected_order] if selected_order in base_by_order.index else None
        base_executed = bool(base_row["replay_executed"]) if base_row is not None else False
        base_return = float(base_row["replay_account_return"]) if base_row is not None and base_executed else 0.0
        rows.append(
            {
                "selected_order": selected_order,
                "trade_date": row["trade_date"],
                "exit_trade_date": row["exit_trade_date"],
                "ts_code": row["ts_code"],
                "name": row["name"],
                "market_segment": row["market_segment"],
                "base_executed": base_executed,
                "base_account_return": base_return,
                "base_was_loss": base_return <= 0 if base_executed else False,
                "fd_ratio_bucket": row.get("fd_ratio_bucket", ""),
                "market_limit_down_count_bucket": row.get("market_limit_down_count_bucket", ""),
                "base_skip_reason": str(base_row["replay_skip_reason"]) if base_row is not None else "",
            }
        )
    return pd.DataFrame(rows)


def build_replacement_report(detail: pd.DataFrame) -> pd.DataFrame:
    base_executed = detail[(detail["replay_rule_name"] == BASE_RULE) & (detail["replay_executed"] == True)].copy()  # noqa: E712
    best_executed = detail[(detail["replay_rule_name"] == BEST_RULE) & (detail["replay_executed"] == True)].copy()  # noqa: E712
    base_orders = set(base_executed["selected_order"])
    best_orders = set(best_executed["selected_order"])
    added = best_executed[best_executed["selected_order"].isin(best_orders - base_orders)].copy()
    removed = base_executed[base_executed["selected_order"].isin(base_orders - best_orders)].copy()
    added["change_type"] = "added_after_position_release"
    removed["change_type"] = "removed_by_filter_or_occupation_shift"
    report = pd.concat([added, removed], ignore_index=True)
    if report.empty:
        return report
    return report[
        [
            "change_type",
            "selected_order",
            "trade_date",
            "exit_trade_date",
            "ts_code",
            "name",
            "market_segment",
            "replay_account_return",
            "replay_equity_before",
            "replay_equity_after",
            "fd_ratio_bucket",
            "market_limit_down_count_bucket",
        ]
    ].sort_values(["change_type", "selected_order"])


def build_stability_summary(yearly: pd.DataFrame, quarterly: pd.DataFrame, monthly: pd.DataFrame) -> pd.DataFrame:
    reports = [("year", yearly), ("quarter", quarterly), ("month", monthly)]
    rows = []
    for period_type, report in reports:
        if report.empty:
            continue
        rows.append(
            {
                "period_type": period_type,
                "period_count": int(len(report)),
                "return_improved_count": int((report["return_delta"] > 0).sum()),
                "return_worsened_count": int((report["return_delta"] < 0).sum()),
                "drawdown_improved_count": int((report["drawdown_delta"] > 0).sum()),
                "drawdown_worsened_count": int((report["drawdown_delta"] < 0).sum()),
                "avg_return_delta": float(report["return_delta"].mean()),
                "avg_drawdown_delta": float(report["drawdown_delta"].mean()),
                "worst_return_delta": float(report["return_delta"].min()),
                "worst_drawdown_delta": float(report["drawdown_delta"].min()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    args = parse_args()
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    detail = load_detail(PROJECT_ROOT / args.input)

    yearly = build_period_compare(detail, "year")
    quarterly = build_period_compare(detail, "quarter")
    monthly = build_period_compare(detail, "month")
    filtered_trades = build_filtered_trade_report(detail)
    replacements = build_replacement_report(detail)
    stability = build_stability_summary(yearly, quarterly, monthly)

    stability_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly_compare.csv")
    quarterly_path = output_prefix.with_name(output_prefix.name + "_quarterly_compare.csv")
    monthly_path = output_prefix.with_name(output_prefix.name + "_monthly_compare.csv")
    filtered_path = output_prefix.with_name(output_prefix.name + "_filtered_trades.csv")
    replacement_path = output_prefix.with_name(output_prefix.name + "_replacement_trades.csv")

    stability.to_csv(stability_path, index=False, encoding="utf-8-sig")
    yearly.to_csv(yearly_path, index=False, encoding="utf-8-sig")
    quarterly.to_csv(quarterly_path, index=False, encoding="utf-8-sig")
    monthly.to_csv(monthly_path, index=False, encoding="utf-8-sig")
    filtered_trades.to_csv(filtered_path, index=False, encoding="utf-8-sig")
    replacements.to_csv(replacement_path, index=False, encoding="utf-8-sig")

    print("bj_cap_2pct 前视过滤稳定性验证完成")
    print(stability.to_string(index=False))
    print("报告文件：")
    print(f"- summary: {stability_path}")
    print(f"- yearly: {yearly_path}")
    print(f"- quarterly: {quarterly_path}")
    print(f"- monthly: {monthly_path}")
    print(f"- filtered_trades: {filtered_path}")
    print(f"- replacements: {replacement_path}")


if __name__ == "__main__":
    main()
