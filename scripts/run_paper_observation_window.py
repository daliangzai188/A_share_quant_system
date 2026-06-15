"""
运行最近 N 个交易日模拟盘历史观察回放。

文件作用：
1. 从本地候选数据中自动识别最近 N 个可用交易日。
2. 调用批量模拟盘流程生成区间候选、计划、人工复核、成交和资金数据。
3. 按模拟盘观察口径重算结果：人工复核日不计入已成交收益。
4. 输出最近 N 日观察回放报告。

本脚本只使用本地数据，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.paper_batch_flow import PaperBatchFlowRunner
from src.paper_candidate_generator import PaperCandidateGenerator
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行最近 N 个交易日模拟盘历史观察回放。")
    parser.add_argument(
        "--strategy-config",
        default="config/strategy_config.json",
        help="策略配置文件路径。",
    )
    parser.add_argument(
        "--runtime-config",
        default="config/config.json",
        help="运行时通用配置文件路径，仅用于日志配置。",
    )
    parser.add_argument("--recent-days", type=int, default=20, help="最近交易日数量。")
    parser.add_argument("--start-date", default=None, help="开始日期，格式 YYYYMMDD。用于限定候选日期下限。")
    parser.add_argument("--end-date", default=None, help="截止日期，格式 YYYYMMDD。不传则使用本地最新可用日期。")
    parser.add_argument("--limit", type=int, default=None, help="最近交易日数量，兼容旧命令；优先级高于 --recent-days。")
    parser.add_argument("--top-n", type=int, default=None, help="每日候选输出数量，不传则读取配置。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/observation_window/a_clean_exclude_star_prev0_3_bj_recent",
        help="观察回放输出文件前缀。",
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


def setup_runtime_logger(runtime_config_path: str | Path) -> None:
    runtime_config = load_json_config(runtime_config_path)
    logging_config = runtime_config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )


def resolve_recent_dates(
    strategy_config_path: str | Path,
    recent_days: int,
    start_date: str | None,
    end_date: str | None,
) -> list[str]:
    generator = PaperCandidateGenerator(strategy_config_path)
    all_candidates = generator.load_all_candidates()
    if "trade_date" not in all_candidates.columns:
        raise RuntimeError("候选数据缺少 trade_date 字段，无法识别最近交易日。")
    dates = sorted(all_candidates["trade_date"].dropna().map(normalize_date).unique().tolist())
    if start_date:
        dates = [date for date in dates if date >= str(start_date)]
    if end_date:
        dates = [date for date in dates if date <= str(end_date)]
    if not dates:
        raise RuntimeError("没有找到可用于观察回放的交易日。")
    return dates[-recent_days:]


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def run_batch_flow(
    strategy_config_path: str | Path,
    start_date: str,
    end_date: str,
    top_n: int | None,
) -> dict[str, Path]:
    return PaperBatchFlowRunner(strategy_config_path=strategy_config_path).run(
        start_date=start_date,
        end_date=end_date,
        top_n=top_n,
    )


def resolve_operation_status(row: pd.Series) -> str:
    if to_bool(row.get("manual_review_required", False)):
        return "REVIEW_REQUIRED_PLAN_ONLY"
    daily_status = str(row.get("daily_status", ""))
    if daily_status == "CLOSED_BY_HISTORICAL_SIM":
        return "HISTORICAL_SIM_FILLED"
    return daily_status or "UNKNOWN"


def build_observation_detail(batch_daily: pd.DataFrame, initial_cash: float) -> pd.DataFrame:
    if batch_daily.empty:
        return pd.DataFrame()
    rows = []
    equity = initial_cash
    for row in batch_daily.itertuples(index=False):
        row_dict = row._asdict()
        row_series = pd.Series(row_dict)
        operation_status = resolve_operation_status(row_series)
        original_return = to_float(row_dict.get("account_return", 0.0))
        counted_return = original_return if operation_status == "HISTORICAL_SIM_FILLED" else 0.0
        equity_before = equity
        equity_after = equity * (1.0 + counted_return)
        equity = equity_after
        rows.append(
            {
                "signal_date": normalize_date(row_dict.get("signal_date", "")),
                "daily_status": row_dict.get("daily_status", ""),
                "operation_status": operation_status,
                "top_ts_code": row_dict.get("top_ts_code", ""),
                "top_name": row_dict.get("top_name", ""),
                "candidate_count": int(to_float(row_dict.get("candidate_count", 0))),
                "selected_count": int(to_float(row_dict.get("selected_count", 0))),
                "planned_order_count": int(to_float(row_dict.get("planned_order_count", 0))),
                "manual_review_required": to_bool(row_dict.get("manual_review_required", False)),
                "manual_review_status": row_dict.get("manual_review_status", ""),
                "risk_flags": row_dict.get("top_risk_flags", ""),
                "historical_execution_found": to_bool(row_dict.get("historical_execution_found", False)),
                "original_account_return": original_return,
                "counted_account_return": counted_return,
                "observation_equity_before": equity_before,
                "observation_equity_after": equity_after,
                "live_order_enabled": False,
            }
        )
    detail = pd.DataFrame(rows)
    detail["peak_equity"] = detail["observation_equity_after"].cummax().clip(lower=initial_cash)
    detail["drawdown"] = detail["observation_equity_after"] / detail["peak_equity"] - 1.0
    return detail


def build_summary(detail: pd.DataFrame, recent_days: int, batch_paths: dict[str, Path]) -> pd.DataFrame:
    if detail.empty:
        return pd.DataFrame(
            [
                {
                    "requested_recent_days": recent_days,
                    "actual_day_count": 0,
                    "window_requirement_met": False,
                    "live_order_enabled": False,
                }
            ]
        )
    status_counts = detail["operation_status"].value_counts().to_dict()
    counted = detail[detail["operation_status"] == "HISTORICAL_SIM_FILLED"].copy()
    returns = pd.to_numeric(counted.get("counted_account_return", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    initial_equity = float(detail["observation_equity_before"].iloc[0])
    final_equity = float(detail["observation_equity_after"].iloc[-1])
    return pd.DataFrame(
        [
            {
                "start_date": str(detail["signal_date"].min()),
                "end_date": str(detail["signal_date"].max()),
                "requested_recent_days": int(recent_days),
                "actual_day_count": int(len(detail)),
                "window_requirement_met": bool(len(detail) >= recent_days),
                "historical_sim_filled_count": int(status_counts.get("HISTORICAL_SIM_FILLED", 0)),
                "review_required_count": int(status_counts.get("REVIEW_REQUIRED_PLAN_ONLY", 0)),
                "no_candidate_count": int(status_counts.get("NO_CANDIDATE", 0)),
                "position_occupied_skip_count": int(status_counts.get("POSITION_OCCUPIED_SKIP", 0)),
                "planned_order_total_count": int(detail["planned_order_count"].sum()),
                "manual_review_required_day_count": int(detail["manual_review_required"].sum()),
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
                "median_account_return": float(returns.median()) if len(returns) else 0.0,
                "max_profit": float(returns.max()) if len(returns) else 0.0,
                "max_loss": float(returns.min()) if len(returns) else 0.0,
                "initial_equity": initial_equity,
                "final_equity": final_equity,
                "equity_multiple": final_equity / initial_equity if initial_equity else 0.0,
                "max_drawdown": float(detail["drawdown"].min()),
                "batch_daily_path": str(batch_paths.get("daily", "")),
                "batch_summary_path": str(batch_paths.get("summary", "")),
                "live_order_enabled": False,
                "verification_status": "HISTORICAL_REPLAY_ONLY_NOT_FORWARD_PAPER",
            }
        ]
    )


def build_status_report(detail: pd.DataFrame) -> pd.DataFrame:
    if detail.empty:
        return pd.DataFrame(columns=["operation_status", "day_count"])
    return (
        detail["operation_status"]
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
    detail: pd.DataFrame,
) -> None:
    detail_columns = [
        "signal_date",
        "operation_status",
        "top_ts_code",
        "top_name",
        "manual_review_required",
        "planned_order_count",
        "counted_account_return",
        "drawdown",
        "risk_flags",
    ]
    detail_columns = [column for column in detail_columns if column in detail.columns]
    content = f"""# 最近交易日模拟盘历史观察回放

本报告使用过去 N 个本地历史交易日做模拟盘流程回放，不接实盘，不调用 QMT，不下真实订单。

## 汇总

{summary.to_markdown(index=False)}

## 状态分布

{status_report.to_markdown(index=False) if not status_report.empty else "无状态数据。"}

## 每日明细

{detail[detail_columns].to_markdown(index=False) if not detail.empty else "无每日明细。"}

## 口径限制

- 这是历史回放验证，不等同于未来 20 个交易日模拟盘。
- `REVIEW_REQUIRED_PLAN_ONLY` 不计入收益，避免把需要人工复核的交易默认成交。
- 即使最近 20 日结果通过，也还需要分钟 K、盘口五档、滑点和真实排队成交验证。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    setup_runtime_logger(args.runtime_config)

    recent_days = int(args.limit if args.limit is not None else args.recent_days)
    dates = resolve_recent_dates(args.strategy_config, recent_days, args.start_date, args.end_date)
    start_date = dates[0]
    end_date = dates[-1]
    batch_paths = run_batch_flow(
        strategy_config_path=args.strategy_config,
        start_date=start_date,
        end_date=end_date,
        top_n=args.top_n,
    )
    batch_daily = read_csv(batch_paths["daily"])
    detail = build_observation_detail(batch_daily, initial_cash=500000.0)
    summary = build_summary(detail, recent_days, batch_paths)
    status_report = build_status_report(detail)

    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    suffix = f"_{start_date}_{end_date}_{recent_days}d"
    summary_path = output_prefix.with_name(output_prefix.name + suffix + "_summary.csv")
    detail_path = output_prefix.with_name(output_prefix.name + suffix + "_detail.csv")
    status_path = output_prefix.with_name(output_prefix.name + suffix + "_status.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + suffix + ".md")

    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    status_report.to_csv(status_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, status_report, detail)

    print("最近交易日模拟盘历史观察回放完成：")
    print(f"- summary: {summary_path}")
    print(f"- detail: {detail_path}")
    print(f"- status: {status_path}")
    print(f"- markdown: {markdown_path}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
