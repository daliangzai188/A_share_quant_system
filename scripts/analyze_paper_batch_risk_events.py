"""
分析多日模拟盘批量流程风险事件。

文件作用：
1. 读取批量模拟盘 risk_events、daily、candidates、executions 和审计逐笔交易。
2. 拆解 PENDING_NO_HISTORICAL_MATCH 是否更像持仓占用、审计缺失或排序口径不一致。
3. 拆解 SINGLE_TRADE_LOSS_WARN 的亏损交易因子、滑点、成交额占比和退出情况。
4. 拆解 POSITION_OCCUPIED_SKIP，确认有信号但因单仓规则不能开新仓的日期。
5. 输出 CSV 和 Markdown 报告，供后续决定过滤、降仓或保留预警。

本脚本只读取本地 CSV，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


FACTOR_COLUMNS = [
    "market_segment",
    "profit_source_score",
    "risk_flags",
    "first_time_detail_bucket",
    "amount_ratio_bucket",
    "prev_pct_chg_bucket",
    "market_limit_down_count_bucket",
    "retreat_state_bucket",
    "segment_emotion_state_bucket",
    "turnover_rate_bucket",
    "volume_ratio_bucket",
    "fd_ratio_bucket",
    "open_times_bucket",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分析多日模拟盘批量流程风险事件。")
    parser.add_argument(
        "--risk-events",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_20240520_20260417_risk_events.csv",
        help="批量模拟盘风险事件文件。",
    )
    parser.add_argument(
        "--daily",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_20240520_20260417_daily.csv",
        help="批量模拟盘每日状态文件。",
    )
    parser.add_argument(
        "--candidates",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_20240520_20260417_candidates.csv",
        help="批量模拟盘候选文件。",
    )
    parser.add_argument(
        "--executions",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_20240520_20260417_executions.csv",
        help="批量模拟盘成交更新文件。",
    )
    parser.add_argument(
        "--audit-trades",
        default="reports/a_clean_exclude_star_prev0_3_bj_best_audit_trades.csv",
        help="策略审计逐笔交易文件。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_risk_analysis",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def read_csv(path: str | Path, **kwargs: Any) -> pd.DataFrame:
    resolved = PROJECT_ROOT / path if not Path(path).is_absolute() else Path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"文件不存在: {resolved}")
    return pd.read_csv(resolved, low_memory=False, **kwargs)


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def normalize_number(value: object, default: float = 0.0) -> float:
    result = pd.to_numeric(value, errors="coerce")
    if pd.isna(result):
        return default
    return float(result)


def load_inputs(args: argparse.Namespace) -> dict[str, pd.DataFrame]:
    risk_events = read_csv(args.risk_events, dtype={"signal_date": str, "ts_code": str})
    daily = read_csv(args.daily, dtype={"signal_date": str, "top_ts_code": str})
    candidates = read_csv(args.candidates, dtype={"signal_date": str, "ts_code": str})
    executions = read_csv(args.executions, dtype={"signal_date": str, "ts_code": str})
    audit = read_csv(args.audit_trades, dtype={"trade_date": str, "ts_code": str})
    for frame in [risk_events, daily, candidates, executions]:
        for column in ["signal_date", "event_date"]:
            if column in frame.columns:
                frame[column] = frame[column].map(normalize_date)
    for column in ["trade_date", "buy_trade_date", "exit_trade_date"]:
        if column in audit.columns:
            audit[column] = audit[column].map(normalize_date)
    if "scenario_executed" in audit.columns:
        audit = audit[audit["scenario_executed"].astype(str).str.lower().isin({"true", "1"})].copy()
    return {
        "risk_events": risk_events,
        "daily": daily,
        "candidates": candidates,
        "executions": executions,
        "audit": audit,
    }


def active_audit_position(signal_date: str, audit: pd.DataFrame) -> pd.DataFrame:
    if audit.empty:
        return audit.copy()
    buy_date = audit.get("buy_trade_date", pd.Series("", index=audit.index)).astype(str)
    exit_date = audit.get("exit_trade_date", pd.Series("", index=audit.index)).astype(str)
    trade_date = audit.get("trade_date", pd.Series("", index=audit.index)).astype(str)
    mask = (trade_date < signal_date) & (buy_date <= signal_date) & (signal_date <= exit_date)
    return audit[mask].copy()


def build_pending_report(
    risk_events: pd.DataFrame,
    daily: pd.DataFrame,
    candidates: pd.DataFrame,
    audit: pd.DataFrame,
) -> pd.DataFrame:
    pending = risk_events[risk_events["risk_type"].astype(str) == "PENDING_NO_HISTORICAL_MATCH"].copy()
    rows: list[dict[str, Any]] = []
    for row in pending.itertuples(index=False):
        signal_date = str(getattr(row, "signal_date", ""))
        ts_code = str(getattr(row, "ts_code", ""))
        daily_match = daily[(daily["signal_date"].astype(str) == signal_date) & (daily["top_ts_code"].astype(str) == ts_code)]
        candidate_match = candidates[
            (candidates["signal_date"].astype(str) == signal_date)
            & (candidates["ts_code"].astype(str) == ts_code)
        ]
        same_day_audit = audit[audit["trade_date"].astype(str) == signal_date].copy()
        active_position = active_audit_position(signal_date, audit)
        candidate = candidate_match.iloc[0] if not candidate_match.empty else pd.Series(dtype=object)
        daily_row = daily_match.iloc[0] if not daily_match.empty else pd.Series(dtype=object)
        likely_reason = "UNKNOWN"
        if not active_position.empty:
            likely_reason = "POSITION_OCCUPIED_BY_PREVIOUS_AUDIT_TRADE"
        elif same_day_audit.empty:
            likely_reason = "NO_AUDIT_TRADE_ON_SIGNAL_DATE"
        else:
            likely_reason = "AUDIT_SELECTED_OTHER_TARGET"
        rows.append(
            {
                "signal_date": signal_date,
                "ts_code": ts_code,
                "name": getattr(row, "ts_code", ""),
                "daily_top_name": daily_row.get("top_name", ""),
                "likely_reason": likely_reason,
                "active_position_count": int(len(active_position)),
                "active_position_codes": ";".join(
                    (active_position["ts_code"].astype(str) + " " + active_position["name"].astype(str)).head(5)
                ),
                "active_position_exit_dates": ";".join(active_position.get("exit_trade_date", pd.Series(dtype=str)).astype(str).head(5)),
                "same_day_audit_count": int(len(same_day_audit)),
                "candidate_rank": normalize_number(candidate.get("candidate_rank", 0), 0.0),
                "market_segment": candidate.get("market_segment", ""),
                "profit_source_score": normalize_number(candidate.get("profit_source_score", 0), 0.0),
                "historical_reference_net_return": normalize_number(
                    candidate.get("historical_reference_net_return", 0), 0.0
                ),
                "historical_reference_is_win": candidate.get("historical_reference_is_win", ""),
                "amount": normalize_number(candidate.get("amount", 0), 0.0),
                "turnover_rate": normalize_number(candidate.get("turnover_rate", 0), 0.0),
                "volume_ratio": normalize_number(candidate.get("volume_ratio", 0), 0.0),
                "first_time_detail_bucket": candidate.get("first_time_detail_bucket", ""),
                "amount_ratio_bucket": candidate.get("amount_ratio_bucket", ""),
                "prev_pct_chg_bucket": candidate.get("prev_pct_chg_bucket", ""),
                "market_limit_down_count_bucket": candidate.get("market_limit_down_count_bucket", ""),
                "retreat_state_bucket": candidate.get("retreat_state_bucket", ""),
            }
        )
    return pd.DataFrame(rows)


def build_loss_report(risk_events: pd.DataFrame, audit: pd.DataFrame) -> pd.DataFrame:
    losses = risk_events[risk_events["risk_type"].astype(str) == "SINGLE_TRADE_LOSS_WARN"].copy()
    if losses.empty:
        return pd.DataFrame()
    merged = losses[["signal_date", "ts_code", "metric_value", "threshold"]].merge(
        audit,
        left_on=["signal_date", "ts_code"],
        right_on=["trade_date", "ts_code"],
        how="left",
    )
    columns = [
        "trade_order",
        "trade_date",
        "ts_code",
        "name",
        "market_segment",
        "dynamic_account_return",
        "dynamic_net_return",
        "equity_before",
        "equity_after",
        "buy_trade_date",
        "exit_trade_date",
        "dynamic_buy_price",
        "dynamic_sell_price",
        "buy_amount_ratio",
        "sell_amount_ratio",
        "dynamic_buy_slippage_rate",
        "dynamic_sell_slippage_rate",
        "limit_down_blocked_days",
        "first_time_detail_bucket",
        "amount_ratio_bucket",
        "prev_pct_chg_bucket",
        "market_limit_down_count_bucket",
        "retreat_state_bucket",
        "segment_retreat_state_bucket",
        "turnover_rate_bucket",
        "volume_ratio_bucket",
        "open_times_bucket",
        "fd_ratio_bucket",
    ]
    existing = [column for column in columns if column in merged.columns]
    return merged[existing].copy()


def build_position_skip_report(daily: pd.DataFrame, candidates: pd.DataFrame) -> pd.DataFrame:
    skips = daily[daily["daily_status"].astype(str) == "POSITION_OCCUPIED_SKIP"].copy()
    rows: list[dict[str, Any]] = []
    for row in skips.itertuples(index=False):
        signal_date = str(getattr(row, "signal_date", ""))
        ts_code = str(getattr(row, "top_ts_code", ""))
        candidate_match = candidates[
            (candidates["signal_date"].astype(str) == signal_date)
            & (candidates["ts_code"].astype(str) == ts_code)
        ]
        candidate = candidate_match.iloc[0] if not candidate_match.empty else pd.Series(dtype=object)
        rows.append(
            {
                "signal_date": signal_date,
                "ts_code": ts_code,
                "name": getattr(row, "top_name", ""),
                "candidate_count": int(normalize_number(getattr(row, "candidate_count", 0), 0.0)),
                "position_occupied_by": getattr(row, "position_occupied_by", ""),
                "position_occupied_exit_dates": normalize_date(getattr(row, "position_occupied_exit_dates", "")),
                "equity_end_of_day": normalize_number(getattr(row, "equity_end_of_day", 0), 0.0),
                "historical_reference_net_return": normalize_number(
                    candidate.get("historical_reference_net_return", 0), 0.0
                ),
                "historical_reference_is_win": candidate.get("historical_reference_is_win", ""),
                "market_segment": candidate.get("market_segment", ""),
                "profit_source_score": normalize_number(candidate.get("profit_source_score", 0), 0.0),
                "first_time_detail_bucket": candidate.get("first_time_detail_bucket", ""),
                "amount_ratio_bucket": candidate.get("amount_ratio_bucket", ""),
                "prev_pct_chg_bucket": candidate.get("prev_pct_chg_bucket", ""),
                "market_limit_down_count_bucket": candidate.get("market_limit_down_count_bucket", ""),
                "retreat_state_bucket": candidate.get("retreat_state_bucket", ""),
            }
        )
    return pd.DataFrame(rows)


def build_summary(
    risk_events: pd.DataFrame,
    pending_report: pd.DataFrame,
    loss_report: pd.DataFrame,
    position_skip_report: pd.DataFrame,
) -> pd.DataFrame:
    rows = [
        {
            "risk_event_count": int(len(risk_events)),
            "pending_no_historical_match_count": int(len(pending_report)),
            "single_trade_loss_warn_count": int(len(loss_report)),
            "position_occupied_skip_count": int(len(position_skip_report)),
            "pending_likely_position_occupied_count": int(
                (pending_report.get("likely_reason", pd.Series(dtype=str)) == "POSITION_OCCUPIED_BY_PREVIOUS_AUDIT_TRADE").sum()
            ),
            "pending_avg_historical_reference_return": float(
                pd.to_numeric(
                    pending_report.get("historical_reference_net_return", pd.Series(dtype=float)),
                    errors="coerce",
                ).mean()
            )
            if len(pending_report)
            else 0.0,
            "pending_win_reference_count": int(
                pending_report.get("historical_reference_is_win", pd.Series(dtype=object))
                .astype(str)
                .str.lower()
                .isin({"true", "1"})
                .sum()
            )
            if len(pending_report)
            else 0,
            "loss_avg_account_return": float(
                pd.to_numeric(loss_report.get("dynamic_account_return", pd.Series(dtype=float)), errors="coerce").mean()
            )
            if len(loss_report)
            else 0.0,
            "loss_max_account_loss": float(
                pd.to_numeric(loss_report.get("dynamic_account_return", pd.Series(dtype=float)), errors="coerce").min()
            )
            if len(loss_report)
            else 0.0,
            "position_skip_avg_reference_return": float(
                pd.to_numeric(
                    position_skip_report.get("historical_reference_net_return", pd.Series(dtype=float)),
                    errors="coerce",
                ).mean()
            )
            if len(position_skip_report)
            else 0.0,
            "position_skip_win_reference_count": int(
                position_skip_report.get("historical_reference_is_win", pd.Series(dtype=object))
                .astype(str)
                .str.lower()
                .isin({"true", "1"})
                .sum()
            )
            if len(position_skip_report)
            else 0,
        }
    ]
    return pd.DataFrame(rows)


def build_bucket_summary(
    pending_report: pd.DataFrame,
    loss_report: pd.DataFrame,
    position_skip_report: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for source_name, data in [
        ("pending", pending_report),
        ("loss", loss_report),
        ("position_skip", position_skip_report),
    ]:
        if data.empty:
            continue
        for column in FACTOR_COLUMNS:
            if column not in data.columns:
                continue
            for value, group in data.groupby(column, dropna=False):
                rows.append(
                    {
                        "source": source_name,
                        "factor": column,
                        "bucket": value,
                        "event_count": int(len(group)),
                        "avg_reference_return": float(
                            pd.to_numeric(
                                group.get("historical_reference_net_return", pd.Series(dtype=float)),
                                errors="coerce",
                            ).mean()
                        )
                        if "historical_reference_net_return" in group.columns
                        else 0.0,
                        "avg_account_return": float(
                            pd.to_numeric(
                                group.get("dynamic_account_return", pd.Series(dtype=float)),
                                errors="coerce",
                            ).mean()
                        )
                        if "dynamic_account_return" in group.columns
                        else 0.0,
                    }
                )
    if not rows:
        return pd.DataFrame(columns=["source", "factor", "bucket", "event_count"])
    return pd.DataFrame(rows).sort_values(["source", "event_count", "factor"], ascending=[True, False, True])


def write_markdown(
    path: Path,
    summary: pd.DataFrame,
    pending_report: pd.DataFrame,
    loss_report: pd.DataFrame,
    position_skip_report: pd.DataFrame,
    bucket_summary: pd.DataFrame,
) -> None:
    pending_preview_columns = [
        "signal_date",
        "ts_code",
        "daily_top_name",
        "likely_reason",
        "active_position_codes",
        "active_position_exit_dates",
        "historical_reference_net_return",
        "market_segment",
        "first_time_detail_bucket",
        "amount_ratio_bucket",
        "retreat_state_bucket",
    ]
    loss_preview_columns = [
        "trade_date",
        "ts_code",
        "name",
        "dynamic_account_return",
        "buy_trade_date",
        "exit_trade_date",
        "buy_amount_ratio",
        "sell_amount_ratio",
        "first_time_detail_bucket",
        "amount_ratio_bucket",
        "retreat_state_bucket",
    ]
    position_skip_columns = [
        "signal_date",
        "ts_code",
        "name",
        "position_occupied_by",
        "position_occupied_exit_dates",
        "historical_reference_net_return",
        "market_segment",
        "first_time_detail_bucket",
        "amount_ratio_bucket",
        "retreat_state_bucket",
    ]
    pending_preview_columns = [column for column in pending_preview_columns if column in pending_report.columns]
    loss_preview_columns = [column for column in loss_preview_columns if column in loss_report.columns]
    position_skip_columns = [column for column in position_skip_columns if column in position_skip_report.columns]
    top_bucket = bucket_summary.head(30)
    content = f"""# 批量模拟盘风险事件分析

本报告只基于本地 CSV 拆解风险事件，不接实盘，不调用 QMT，不下真实订单。

## 汇总

{summary.to_markdown(index=False)}

## Pending 事件

{pending_report[pending_preview_columns].to_markdown(index=False) if not pending_report.empty else "无 Pending 事件。"}

## 单笔亏损预警

{loss_report[loss_preview_columns].to_markdown(index=False) if not loss_report.empty else "无单笔亏损预警。"}

## 持仓占用跳过

{position_skip_report[position_skip_columns].to_markdown(index=False) if not position_skip_report.empty else "无持仓占用跳过。"}

## 风险桶集中度

{top_bucket.to_markdown(index=False) if not top_bucket.empty else "无风险桶集中度。"}

## 初步处理建议

1. `PENDING_NO_HISTORICAL_MATCH` 优先按持仓占用处理，不能把这些候选强行计入收益。
2. 单笔亏损预警先作为风控观察项，不直接硬过滤；样本只有少数时，硬过滤容易过拟合。
3. 后续需要把持仓占用逻辑前置到候选批量流程，在已经有持仓未释放时直接标记 `POSITION_OCCUPIED_SKIP`。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    inputs = load_inputs(args)
    risk_events = inputs["risk_events"]
    daily = inputs["daily"]
    candidates = inputs["candidates"]
    audit = inputs["audit"]

    pending_report = build_pending_report(risk_events, daily, candidates, audit)
    loss_report = build_loss_report(risk_events, audit)
    position_skip_report = build_position_skip_report(daily, candidates)
    summary = build_summary(risk_events, pending_report, loss_report, position_skip_report)
    bucket_summary = build_bucket_summary(pending_report, loss_report, position_skip_report)

    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary": output_prefix.with_name(output_prefix.name + "_summary.csv"),
        "pending": output_prefix.with_name(output_prefix.name + "_pending.csv"),
        "loss": output_prefix.with_name(output_prefix.name + "_loss.csv"),
        "position_skip": output_prefix.with_name(output_prefix.name + "_position_skip.csv"),
        "bucket": output_prefix.with_name(output_prefix.name + "_bucket.csv"),
        "markdown": output_prefix.with_name(output_prefix.name + ".md"),
    }
    summary.to_csv(paths["summary"], index=False, encoding="utf-8-sig")
    pending_report.to_csv(paths["pending"], index=False, encoding="utf-8-sig")
    loss_report.to_csv(paths["loss"], index=False, encoding="utf-8-sig")
    position_skip_report.to_csv(paths["position_skip"], index=False, encoding="utf-8-sig")
    bucket_summary.to_csv(paths["bucket"], index=False, encoding="utf-8-sig")
    write_markdown(paths["markdown"], summary, pending_report, loss_report, position_skip_report, bucket_summary)

    print("批量模拟盘风险事件分析完成：")
    for name, path in paths.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
