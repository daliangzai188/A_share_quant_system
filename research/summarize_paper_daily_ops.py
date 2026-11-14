"""
汇总模拟盘每日操作台结果。

文件作用：
1. 读取 reports/paper_trade/daily_ops 下的每日 checklist.csv。
2. 按日期区间汇总模拟盘观察天数、候选数、计划数、人工复核数和状态分布。
3. 统计历史模拟成交闭环的收益、胜率和回撤。
4. 判断是否达到连续模拟盘观察门槛。

本脚本只读取和生成本地 CSV/Markdown，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="汇总模拟盘每日操作台结果。")
    parser.add_argument(
        "--input-dir",
        default="reports/paper_trade/daily_ops",
        help="每日操作台 checklist.csv 所在目录。",
    )
    parser.add_argument(
        "--pattern",
        default="*_checklist.csv",
        help="每日 checklist 文件匹配规则。",
    )
    parser.add_argument("--start-date", default=None, help="开始日期，格式 YYYYMMDD。")
    parser.add_argument("--end-date", default=None, help="结束日期，格式 YYYYMMDD。")
    parser.add_argument(
        "--min-observation-days",
        type=int,
        default=20,
        help="模拟盘最少观察交易日数。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/daily_ops_summary/a_clean_exclude_star_prev0_3_bj",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def to_bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def to_float(value: object, default: float = 0.0) -> float:
    result = pd.to_numeric(value, errors="coerce")
    if pd.isna(result):
        return default
    return float(result)


def read_checklists(input_dir: Path, pattern: str) -> pd.DataFrame:
    files = sorted(input_dir.glob(pattern))
    frames = []
    for file_path in files:
        frame = pd.read_csv(file_path, low_memory=False)
        frame["source_file"] = str(file_path)
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    if "signal_date" in data.columns:
        data["signal_date"] = data["signal_date"].map(normalize_date)
    return data


def filter_by_date(data: pd.DataFrame, start_date: str | None, end_date: str | None) -> pd.DataFrame:
    if data.empty or "signal_date" not in data.columns:
        return data
    result = data.copy()
    if start_date:
        result = result[result["signal_date"].astype(str) >= str(start_date)].copy()
    if end_date:
        result = result[result["signal_date"].astype(str) <= str(end_date)].copy()
    return result.sort_values("signal_date").reset_index(drop=True)


def enrich_checklists(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    numeric_columns = [
        "candidate_count",
        "selected_count",
        "planned_order_count",
        "manual_review_count",
        "planned_position_pct",
        "planned_amount_by_equity",
        "reference_price",
        "round_lot_shares",
        "execution_event_count",
        "position_update_count",
        "equity_update_count",
    ]
    for column in numeric_columns:
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce").fillna(0.0)
    for column in [
        "manual_review_required",
        "paper_observation_allowed",
        "historical_execution_found",
        "live_order_enabled",
    ]:
        if column in result.columns:
            result[column] = result[column].map(to_bool)
    result["operation_status"] = result.get("operation_status", pd.Series("", index=result.index)).fillna("").astype(str)
    result["review_decision_status"] = (
        result.get("review_decision_status", pd.Series("NOT_REQUIRED", index=result.index)).fillna("NOT_REQUIRED").astype(str)
    )
    result["top_ts_code"] = result.get("top_ts_code", pd.Series("", index=result.index)).fillna("").astype(str)
    result["top_name"] = result.get("top_name", pd.Series("", index=result.index)).fillna("").astype(str)
    return result


def load_daily_flow_summary(row: pd.Series) -> dict[str, Any]:
    source_file = Path(str(row.get("source_file", "")))
    signal_date = str(row.get("signal_date", ""))
    if not source_file.name.endswith("_checklist.csv"):
        return {}
    prefix_name = source_file.name[: -len("_checklist.csv")]
    daily_summary_name = prefix_name + "_summary.csv"
    daily_summary_path = PROJECT_ROOT / "reports/paper_trade/daily_flow" / daily_summary_name
    if not daily_summary_path.exists():
        return {}
    summary = pd.read_csv(daily_summary_path, low_memory=False)
    if summary.empty:
        return {}
    summary_row = summary.iloc[0]
    if normalize_date(summary_row.get("signal_date", signal_date)) != signal_date:
        return {}
    return {
        "account_return": to_float(summary_row.get("account_return", 0.0)),
        "equity_before": to_float(summary_row.get("equity_before", 0.0)),
        "equity_after": to_float(summary_row.get("equity_after", 0.0)),
        "historical_execution_found": to_bool(summary_row.get("historical_execution_found", False)),
    }


def attach_daily_returns(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    rows = []
    for row in result.itertuples(index=False):
        row_dict = row._asdict()
        daily_summary = load_daily_flow_summary(pd.Series(row_dict))
        row_dict["account_return"] = daily_summary.get("account_return", 0.0)
        row_dict["equity_before"] = daily_summary.get("equity_before", 0.0)
        row_dict["equity_after"] = daily_summary.get("equity_after", 0.0)
        row_dict["return_source"] = "daily_flow_summary" if daily_summary else "missing"
        rows.append(row_dict)
    if not rows:
        return result
    return pd.DataFrame(rows)


def build_equity_curve(data: pd.DataFrame, initial_cash: float = 500000.0) -> pd.DataFrame:
    rows = []
    equity = initial_cash
    for row in data.itertuples(index=False):
        account_return = to_float(getattr(row, "account_return", 0.0))
        status = str(getattr(row, "operation_status", ""))
        has_execution = bool(getattr(row, "historical_execution_found", False))
        if status == "HISTORICAL_SIM_FILLED" and has_execution:
            equity *= 1.0 + account_return
        rows.append(
            {
                "signal_date": getattr(row, "signal_date", ""),
                "operation_status": status,
                "ts_code": getattr(row, "top_ts_code", ""),
                "name": getattr(row, "top_name", ""),
                "account_return": account_return if status == "HISTORICAL_SIM_FILLED" and has_execution else 0.0,
                "equity": equity,
            }
        )
    curve = pd.DataFrame(rows)
    if curve.empty:
        return pd.DataFrame(columns=["signal_date", "operation_status", "ts_code", "name", "account_return", "equity"])
    curve["peak_equity"] = curve["equity"].cummax()
    curve["drawdown"] = curve["equity"] / curve["peak_equity"] - 1.0
    return curve


def build_summary(data: pd.DataFrame, equity_curve: pd.DataFrame, min_observation_days: int) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame(
            [
                {
                    "observation_day_count": 0,
                    "min_observation_days": min_observation_days,
                    "observation_requirement_met": False,
                    "live_order_enabled": False,
                }
            ]
        )
    status_counts = data["operation_status"].value_counts().to_dict()
    review_status_counts = data.get("review_decision_status", pd.Series(dtype=str)).value_counts().to_dict()
    historical = data[data["operation_status"] == "HISTORICAL_SIM_FILLED"].copy()
    returns = pd.to_numeric(historical.get("account_return", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    final_equity = float(equity_curve["equity"].iloc[-1]) if not equity_curve.empty else 500000.0
    initial_equity = 500000.0
    return pd.DataFrame(
        [
            {
                "start_date": str(data["signal_date"].min()),
                "end_date": str(data["signal_date"].max()),
                "observation_day_count": int(len(data)),
                "min_observation_days": int(min_observation_days),
                "observation_requirement_met": bool(len(data) >= min_observation_days),
                "historical_sim_filled_count": int(status_counts.get("HISTORICAL_SIM_FILLED", 0)),
                "review_required_count": int(status_counts.get("REVIEW_REQUIRED_PLAN_ONLY", 0)),
                "plan_only_pending_count": int(status_counts.get("PLAN_ONLY_PENDING", 0)),
                "failed_or_no_candidate_count": int(status_counts.get("FAILED_OR_NO_CANDIDATE", 0)),
                "manual_review_required_day_count": int(data.get("manual_review_required", pd.Series(False)).sum()),
                "manual_review_approved_day_count": int(review_status_counts.get("APPROVED", 0)),
                "manual_review_rejected_day_count": int(review_status_counts.get("REJECTED", 0)),
                "manual_review_pending_day_count": int(review_status_counts.get("PENDING", 0)),
                "paper_observation_allowed_day_count": int(
                    data.get("paper_observation_allowed", pd.Series(False, index=data.index)).sum()
                ),
                "manual_review_total_count": int(
                    pd.to_numeric(data.get("manual_review_count", pd.Series(dtype=float)), errors="coerce")
                    .fillna(0.0)
                    .sum()
                ),
                "planned_order_total_count": int(
                    pd.to_numeric(data.get("planned_order_count", pd.Series(dtype=float)), errors="coerce")
                    .fillna(0.0)
                    .sum()
                ),
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
                "median_account_return": float(returns.median()) if len(returns) else 0.0,
                "max_profit": float(returns.max()) if len(returns) else 0.0,
                "max_loss": float(returns.min()) if len(returns) else 0.0,
                "initial_equity": initial_equity,
                "final_equity": final_equity,
                "equity_multiple": final_equity / initial_equity if initial_equity else 0.0,
                "max_drawdown": float(equity_curve["drawdown"].min()) if not equity_curve.empty else 0.0,
                "live_order_enabled": False,
                "verification_status": "PASS_OBSERVATION_DAYS_ONLY" if len(data) >= min_observation_days else "INSUFFICIENT_OBSERVATION_DAYS",
            }
        ]
    )


def build_status_report(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame(columns=["operation_status", "day_count"])
    return (
        data["operation_status"]
        .value_counts()
        .rename_axis("operation_status")
        .reset_index(name="day_count")
        .sort_values("operation_status")
        .reset_index(drop=True)
    )


def write_markdown(
    path: Path,
    summary: pd.DataFrame,
    status_report: pd.DataFrame,
    data: pd.DataFrame,
    equity_curve: pd.DataFrame,
) -> None:
    detail_columns = [
        "signal_date",
        "operation_status",
        "top_ts_code",
        "top_name",
        "manual_review_required",
        "review_decision_status",
        "paper_observation_allowed",
        "planned_order_count",
        "account_return",
        "risk_flags",
    ]
    detail_columns = [column for column in detail_columns if column in data.columns]
    content = f"""# 模拟盘每日操作台汇总

本报告只汇总本地模拟盘观察结果，不接实盘，不调用 QMT，不下真实订单。

## 汇总

{summary.to_markdown(index=False)}

## 状态分布

{status_report.to_markdown(index=False) if not status_report.empty else "无状态数据。"}

## 每日明细

{data[detail_columns].to_markdown(index=False) if not data.empty else "无每日明细。"}

## 资金曲线

{equity_curve.tail(20).to_markdown(index=False) if not equity_curve.empty else "无资金曲线。"}

## 判断口径

- 未达到 `min_observation_days` 时，只能继续观察，不能实盘。
- `REVIEW_REQUIRED_PLAN_ONLY` 只能人工复核，不能自动买入。
- 本报告不包含分钟 K、盘口五档和真实排队成交验证。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    input_dir = resolve_path(args.input_dir)
    output_prefix = resolve_path(args.output_prefix)

    raw = read_checklists(input_dir, args.pattern)
    filtered = filter_by_date(raw, args.start_date, args.end_date)
    enriched = enrich_checklists(filtered)
    detailed = attach_daily_returns(enriched)
    equity_curve = build_equity_curve(detailed)
    summary = build_summary(detailed, equity_curve, args.min_observation_days)
    status_report = build_status_report(detailed)

    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    status_path = output_prefix.with_name(output_prefix.name + "_status.csv")
    equity_path = output_prefix.with_name(output_prefix.name + "_equity_curve.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + ".md")

    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    detailed.to_csv(detail_path, index=False, encoding="utf-8-sig")
    status_report.to_csv(status_path, index=False, encoding="utf-8-sig")
    equity_curve.to_csv(equity_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, status_report, detailed, equity_curve)

    print("模拟盘每日操作台汇总完成：")
    print(f"- summary: {summary_path}")
    print(f"- detail: {detail_path}")
    print(f"- status: {status_path}")
    print(f"- equity_curve: {equity_path}")
    print(f"- markdown: {markdown_path}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
