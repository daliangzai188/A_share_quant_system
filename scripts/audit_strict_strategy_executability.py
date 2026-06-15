"""
严格版策略实盘前可执行性审计脚本。

文件作用：
1. 读取当前严格版策略配置、最近批量模拟盘结果和逐笔审计交易。
2. 汇总买入成交、卖出成交、滑点、成交额占比、跌停阻塞、人工复核和仓位占用。
3. 标记每一笔交易的可执行性风险，输出后续分钟 K / 盘口验证的问题清单。
4. 全程只读取本地 CSV，不接实盘，不调用 QMT，不下真实订单。
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

from src.utils.config import load_json_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="严格版策略实盘前可执行性审计。")
    parser.add_argument("--strategy-config", default="config/strategy_config.json", help="策略配置文件路径。")
    parser.add_argument("--start-date", default=None, help="审计开始日期，默认读取 paper_batch_flow.default_start_date。")
    parser.add_argument("--end-date", default=None, help="审计结束日期，默认读取 paper_batch_flow.default_end_date。")
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/executability/a_clean_exclude_star_prev0_3_bj_executability",
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


def to_float(value: object, default: float = 0.0) -> float:
    result = pd.to_numeric(value, errors="coerce")
    if pd.isna(result):
        return default
    return float(result)


def to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        return False
    return str(value).strip().lower() in {"true", "1", "yes"}


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    return pd.read_csv(path, dtype={"trade_date": str, "signal_date": str, "ts_code": str})


def resolve_batch_path(config: dict[str, Any], start_date: str, end_date: str, suffix: str) -> Path:
    batch_config = config.get("paper_batch_flow", {})
    output_prefix = resolve_path(batch_config.get("output_prefix", "reports/paper_trade/batch_flow/current_strategy"))
    return output_prefix.with_name(output_prefix.name + f"_{start_date}_{end_date}_{suffix}.csv")


def selected_candidates(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty or "planned_action" not in candidates.columns:
        return pd.DataFrame()
    selected = candidates[candidates["planned_action"].astype(str) == "PLAN_BUY_T1_OPEN"].copy()
    selected["signal_date"] = selected["signal_date"].map(normalize_date)
    selected["ts_code"] = selected["ts_code"].astype(str)
    return selected


def build_trade_detail(
    trades: pd.DataFrame,
    selected: pd.DataFrame,
    config: dict[str, Any],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    data = trades.copy()
    data["trade_date"] = data["trade_date"].map(normalize_date)
    data["ts_code"] = data["ts_code"].astype(str)
    data = data[(data["trade_date"] >= start_date) & (data["trade_date"] <= end_date)].copy()
    data = data[data.get("scenario_executed", pd.Series(True, index=data.index)).map(to_bool)].copy()
    if data.empty:
        return pd.DataFrame()

    selected_columns = [
        "signal_date",
        "ts_code",
        "risk_flags",
        "candidate_rank",
        "profit_source_score",
        "fill_probability",
        "allow_buy_reliable",
        "is_fill_score_reliable",
    ]
    selected_columns = [column for column in selected_columns if column in selected.columns]
    if selected_columns:
        selected_map = selected[selected_columns].drop_duplicates(["signal_date", "ts_code"], keep="first")
        data = data.merge(
            selected_map,
            left_on=["trade_date", "ts_code"],
            right_on=["signal_date", "ts_code"],
            how="left",
            suffixes=("", "_candidate"),
        )

    paper_thresholds = config.get("paper_trade", {}).get("risk_thresholds", {})
    candidate_thresholds = config.get("paper_candidate", {}).get("risk_thresholds", {})
    max_buy_amount_ratio = float(paper_thresholds.get("max_buy_amount_ratio_warn", 0.03))
    max_sell_amount_ratio = float(paper_thresholds.get("max_sell_amount_ratio_warn", 0.03))
    max_buy_slippage = float(paper_thresholds.get("max_buy_slippage_warn", 0.005))
    max_sell_slippage = float(paper_thresholds.get("max_sell_slippage_warn", 0.005))
    min_fill_probability = float(candidate_thresholds.get("min_fill_probability_warn", 0.6))

    numeric_columns = [
        "equity_before",
        "target_buy_amount",
        "actual_buy_amount",
        "actual_position_pct",
        "buy_amount_ratio",
        "sell_amount_ratio",
        "dynamic_buy_slippage_rate",
        "dynamic_sell_slippage_rate",
        "dynamic_account_return",
        "dynamic_net_return",
        "limit_down_blocked_days",
        "fill_probability",
    ]
    for column in numeric_columns:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce").fillna(0.0)

    data["buy_executed_bool"] = data.get("buy_executed", pd.Series(False, index=data.index)).map(to_bool)
    data["sell_executed_bool"] = data.get("sell_executed", pd.Series(False, index=data.index)).map(to_bool)
    data["path_conflict_bool"] = data.get("path_conflict", pd.Series(False, index=data.index)).map(to_bool)
    data["allow_buy_reliable_bool"] = data.get("allow_buy_reliable", pd.Series(False, index=data.index)).map(to_bool)
    data["is_fill_score_reliable_bool"] = data.get(
        "is_fill_score_reliable", pd.Series(False, index=data.index)
    ).map(to_bool)

    data["issue_buy_not_executed"] = ~data["buy_executed_bool"]
    data["issue_sell_not_executed"] = ~data["sell_executed_bool"]
    data["issue_high_buy_amount_ratio"] = data.get("buy_amount_ratio", pd.Series(0.0, index=data.index)) > max_buy_amount_ratio
    data["issue_high_sell_amount_ratio"] = (
        data.get("sell_amount_ratio", pd.Series(0.0, index=data.index)) > max_sell_amount_ratio
    )
    data["issue_high_buy_slippage"] = (
        data.get("dynamic_buy_slippage_rate", pd.Series(0.0, index=data.index)) > max_buy_slippage
    )
    data["issue_high_sell_slippage"] = (
        data.get("dynamic_sell_slippage_rate", pd.Series(0.0, index=data.index)) > max_sell_slippage
    )
    data["issue_limit_down_blocked"] = data.get("limit_down_blocked_days", pd.Series(0.0, index=data.index)) > 0
    data["issue_path_conflict"] = data["path_conflict_bool"]
    data["issue_low_fill_probability"] = (
        data.get("fill_probability", pd.Series(1.0, index=data.index)) < min_fill_probability
    )
    data["issue_fill_score_unreliable"] = ~data["is_fill_score_reliable_bool"]
    data["issue_loss_overlay_watch"] = data.get("risk_flags", pd.Series("", index=data.index)).fillna("").astype(str).str.contains(
        "LOSS_OVERLAY_WATCH", na=False
    )

    issue_columns = [column for column in data.columns if column.startswith("issue_")]
    data["issue_count"] = data[issue_columns].sum(axis=1).astype(int)
    data["issue_labels"] = data[issue_columns].apply(
        lambda row: ";".join(column.replace("issue_", "") for column, flagged in row.items() if bool(flagged)),
        axis=1,
    )
    data["executability_status"] = data["issue_count"].map(lambda count: "PASS" if count == 0 else "REVIEW")

    keep_columns = [
        "trade_date",
        "ts_code",
        "name",
        "market_segment",
        "buy_trade_date",
        "exit_trade_date",
        "buy_executed_bool",
        "sell_executed_bool",
        "equity_before",
        "target_buy_amount",
        "actual_buy_amount",
        "actual_position_pct",
        "buy_amount_ratio",
        "sell_amount_ratio",
        "dynamic_buy_slippage_rate",
        "dynamic_sell_slippage_rate",
        "dynamic_buy_price",
        "dynamic_sell_price",
        "dynamic_account_return",
        "limit_down_blocked_days",
        "path_conflict_bool",
        "fill_probability",
        "allow_buy_reliable_bool",
        "is_fill_score_reliable_bool",
        "risk_flags",
        "issue_count",
        "issue_labels",
        "executability_status",
    ] + issue_columns
    keep_columns = [column for column in keep_columns if column in data.columns]
    return data[keep_columns].sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def build_issue_summary(detail: pd.DataFrame) -> pd.DataFrame:
    issue_columns = [
        column
        for column in detail.columns
        if column.startswith("issue_") and column not in {"issue_count", "issue_labels"}
    ]
    rows = []
    for column in issue_columns:
        count = int(detail[column].astype(bool).sum()) if column in detail.columns else 0
        rows.append(
            {
                "issue": column.replace("issue_", ""),
                "trade_count": count,
                "trade_pct": count / len(detail) if len(detail) else 0.0,
            }
        )
    if not rows:
        return pd.DataFrame(columns=["issue", "trade_count", "trade_pct"])
    return pd.DataFrame(rows).sort_values("trade_count", ascending=False).reset_index(drop=True)


def build_summary(
    detail: pd.DataFrame,
    daily: pd.DataFrame,
    manual_review: pd.DataFrame,
    config: dict[str, Any],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    safe_flags = {
        "trade_mode": config.get("trade_mode", ""),
        "live_trading_enabled": bool(config.get("live_trading_enabled", False)),
        "broker_adapter_enabled": bool(config.get("broker_adapter_enabled", False)),
        "qmt_enabled": bool(config.get("qmt_enabled", False)),
        "paper_allow_live_order": bool(config.get("paper_trade", {}).get("allow_live_order", False)),
        "batch_allow_live_order": bool(config.get("paper_batch_flow", {}).get("allow_live_order", False)),
    }
    returns = pd.to_numeric(detail.get("dynamic_account_return", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    review_count = int((detail.get("executability_status", pd.Series("", index=detail.index)) == "REVIEW").sum())
    return pd.DataFrame(
        [
            {
                "strategy_name": config.get("strategy_name", ""),
                "start_date": start_date,
                "end_date": end_date,
                "trade_day_count": int(len(daily)),
                "executed_trade_count": int(len(detail)),
                "pass_trade_count": int((detail.get("executability_status", pd.Series("", index=detail.index)) == "PASS").sum()),
                "review_trade_count": review_count,
                "review_trade_pct": review_count / len(detail) if len(detail) else 0.0,
                "manual_review_required_day_count": int(len(manual_review)),
                "no_candidate_day_count": int((daily.get("daily_status", pd.Series("", index=daily.index)).astype(str) == "NO_CANDIDATE").sum()),
                "position_occupied_skip_day_count": int(
                    (daily.get("daily_status", pd.Series("", index=daily.index)).astype(str) == "POSITION_OCCUPIED_SKIP").sum()
                ),
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
                "median_account_return": float(returns.median()) if len(returns) else 0.0,
                "max_profit": float(returns.max()) if len(returns) else 0.0,
                "max_loss": float(returns.min()) if len(returns) else 0.0,
                "avg_buy_amount_ratio": float(detail.get("buy_amount_ratio", pd.Series(dtype=float)).mean()) if len(detail) else 0.0,
                "max_buy_amount_ratio": float(detail.get("buy_amount_ratio", pd.Series(dtype=float)).max()) if len(detail) else 0.0,
                "avg_sell_amount_ratio": float(detail.get("sell_amount_ratio", pd.Series(dtype=float)).mean()) if len(detail) else 0.0,
                "max_sell_amount_ratio": float(detail.get("sell_amount_ratio", pd.Series(dtype=float)).max()) if len(detail) else 0.0,
                "avg_buy_slippage": float(detail.get("dynamic_buy_slippage_rate", pd.Series(dtype=float)).mean()) if len(detail) else 0.0,
                "max_buy_slippage": float(detail.get("dynamic_buy_slippage_rate", pd.Series(dtype=float)).max()) if len(detail) else 0.0,
                "avg_sell_slippage": float(detail.get("dynamic_sell_slippage_rate", pd.Series(dtype=float)).mean()) if len(detail) else 0.0,
                "max_sell_slippage": float(detail.get("dynamic_sell_slippage_rate", pd.Series(dtype=float)).max()) if len(detail) else 0.0,
                "limit_down_blocked_trade_count": int((detail.get("limit_down_blocked_days", pd.Series(0, index=detail.index)) > 0).sum()),
                "buy_not_executed_count": int((~detail.get("buy_executed_bool", pd.Series(True, index=detail.index))).sum()),
                "sell_not_executed_count": int((~detail.get("sell_executed_bool", pd.Series(True, index=detail.index))).sum()),
                "path_conflict_count": int(detail.get("path_conflict_bool", pd.Series(False, index=detail.index)).sum()),
                **safe_flags,
            }
        ]
    )


def write_markdown(path: Path, summary: pd.DataFrame, issue_summary: pd.DataFrame, review_trades: pd.DataFrame) -> None:
    review_columns = [
        "trade_date",
        "ts_code",
        "name",
        "dynamic_account_return",
        "buy_amount_ratio",
        "sell_amount_ratio",
        "dynamic_buy_slippage_rate",
        "dynamic_sell_slippage_rate",
        "limit_down_blocked_days",
        "issue_labels",
    ]
    review_columns = [column for column in review_columns if column in review_trades.columns]
    content = f"""# 严格版策略实盘前可执行性审计

本报告只使用本地历史数据和模拟盘审计文件，不接实盘，不调用 QMT，不下真实订单。

## 汇总

{summary.to_markdown(index=False)}

## 问题类型统计

{issue_summary.to_markdown(index=False) if not issue_summary.empty else "无可执行性问题。"}

## 需要复核的交易

{review_trades[review_columns].to_markdown(index=False) if not review_trades.empty else "无需要复核的交易。"}

## 解释限制

- 当前买入价格口径是 T+1 开盘价加动态滑点，不是逐笔盘口五档真实撮合。
- 当前卖出价格口径是 T+2 收盘价减动态滑点，不是逐笔盘口五档真实撮合。
- `PASS` 只代表当前日线审计口径下未触发预警，不代表可以实盘。
- 后续必须继续用分钟 K、集合竞价、盘口五档、跌停排队卖出验证关键交易。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    config = load_json_config(args.strategy_config)
    batch_config = config.get("paper_batch_flow", {})
    start_date = str(args.start_date or batch_config.get("default_start_date", ""))
    end_date = str(args.end_date or batch_config.get("default_end_date", ""))
    if not start_date or not end_date:
        raise RuntimeError("必须提供 start_date/end_date，或在 paper_batch_flow 中配置默认日期。")

    audit_path = resolve_path(config.get("paper_trade", {}).get("input_trades_path", ""))
    batch_candidates_path = resolve_batch_path(config, start_date, end_date, "candidates")
    batch_daily_path = resolve_batch_path(config, start_date, end_date, "daily")
    batch_manual_review_path = resolve_batch_path(config, start_date, end_date, "manual_review")

    trades = read_csv(audit_path)
    candidates = read_csv(batch_candidates_path)
    daily = read_csv(batch_daily_path)
    manual_review = read_csv(batch_manual_review_path)

    selected = selected_candidates(candidates)
    detail = build_trade_detail(trades, selected, config, start_date, end_date)
    issue_summary = build_issue_summary(detail)
    summary = build_summary(detail, daily, manual_review, config, start_date, end_date)
    review_trades = detail[detail["executability_status"] == "REVIEW"].copy() if not detail.empty else pd.DataFrame()

    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    suffix = f"_{start_date}_{end_date}"
    summary_path = output_prefix.with_name(output_prefix.name + suffix + "_summary.csv")
    detail_path = output_prefix.with_name(output_prefix.name + suffix + "_detail.csv")
    issue_path = output_prefix.with_name(output_prefix.name + suffix + "_issues.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + suffix + ".md")

    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    issue_summary.to_csv(issue_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, issue_summary, review_trades)

    print("严格版策略实盘前可执行性审计完成：")
    print(f"- summary: {summary_path}")
    print(f"- detail: {detail_path}")
    print(f"- issues: {issue_path}")
    print(f"- markdown: {markdown_path}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
