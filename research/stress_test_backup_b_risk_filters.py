"""
备用策略 B 风险过滤压力测试。

文件作用：
1. 读取 B_0018 日线保守成交审计结果。
2. 对 B 新增交易应用多组事前可见风险过滤条件。
3. 重算 B 单独和 A+B 组合资金曲线。
4. 单独标记未来不可用的诊断过滤，避免把未来结果当成实盘条件。

本脚本只使用本地 CSV，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="备用策略 B 风险过滤压力测试。")
    parser.add_argument(
        "--b-replayed",
        default=(
            "reports/paper_trade/backup_strategy_b/"
            "a_clean_exclude_star_prev0_3_bj_b0018_audit_20251112_20260514_120d_b_replayed.csv"
        ),
        help="B 日线保守回放文件。",
    )
    parser.add_argument(
        "--a-plus-b-detail",
        default=(
            "reports/paper_trade/backup_strategy_b/"
            "a_clean_exclude_star_prev0_3_bj_b0018_audit_20251112_20260514_120d_a_plus_b_detail.csv"
        ),
        help="A+B 严格逐日明细。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/backup_strategy_b/a_clean_exclude_star_prev0_3_bj_b0018_risk_filter_stress",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    return pd.read_csv(path, low_memory=False)


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def to_bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    values = pd.to_numeric(equity, errors="coerce").ffill().fillna(0.0)
    peak = values.cummax()
    return float((values / peak - 1.0).min())


def filter_definitions() -> list[dict[str, Any]]:
    return [
        {
            "scenario": "baseline_no_extra_filter",
            "filter_type": "baseline",
            "description": "B_0018 原始日线保守审计结果，不额外过滤。",
            "predicate": lambda data: pd.Series(False, index=data.index),
        },
        {
            "scenario": "reject_loss_overlay_watch",
            "filter_type": "pre_trade",
            "description": "过滤 LOSS_OVERLAY_WATCH。",
            "predicate": lambda data: data["risk_flags"].fillna("").astype(str).str.contains("LOSS_OVERLAY_WATCH"),
        },
        {
            "scenario": "reject_fd_amount_warn",
            "filter_type": "pre_trade",
            "description": "过滤风险标记含 封单/流通市值偏高。",
            "predicate": lambda data: data["risk_flags"].fillna("").astype(str).str.contains("封单/流通市值偏高"),
        },
        {
            "scenario": "reject_fd_1pct_2pct",
            "filter_type": "pre_trade",
            "description": "过滤 fd_ratio_bucket=1pct_2pct。",
            "predicate": lambda data: data["fd_ratio_bucket"].fillna("").astype(str).eq("1pct_2pct"),
        },
        {
            "scenario": "reject_market_chain_15_30",
            "filter_type": "pre_trade",
            "description": "过滤 market_chain_count_bucket=15_30。",
            "predicate": lambda data: data["market_chain_count_bucket"].fillna("").astype(str).eq("15_30"),
        },
        {
            "scenario": "reject_market_chain_gte_30",
            "filter_type": "pre_trade",
            "description": "过滤 market_chain_count_bucket=gte_30。",
            "predicate": lambda data: data["market_chain_count_bucket"].fillna("").astype(str).eq("gte_30"),
        },
        {
            "scenario": "reject_fd_warn_or_loss_overlay",
            "filter_type": "pre_trade",
            "description": "过滤 封单/流通市值偏高 或 LOSS_OVERLAY_WATCH。",
            "predicate": lambda data: data["risk_flags"].fillna("").astype(str).str.contains(
                "封单/流通市值偏高|LOSS_OVERLAY_WATCH", regex=True
            ),
        },
        {
            "scenario": "reject_fd_warn_or_chain_15_30",
            "filter_type": "pre_trade",
            "description": "过滤 封单/流通市值偏高 或 market_chain_count_bucket=15_30。",
            "predicate": lambda data: data["risk_flags"].fillna("").astype(str).str.contains("封单/流通市值偏高")
            | data["market_chain_count_bucket"].fillna("").astype(str).eq("15_30"),
        },
        {
            "scenario": "reject_chain_15_30_or_gte_30",
            "filter_type": "pre_trade",
            "description": "过滤 market_chain_count_bucket=15_30 或 gte_30。",
            "predicate": lambda data: data["market_chain_count_bucket"].fillna("").astype(str).isin({"15_30", "gte_30"}),
        },
        {
            "scenario": "diagnostic_reject_limit_down_blocked",
            "filter_type": "diagnostic_future_not_allowed",
            "description": "诊断过滤：过滤未来发生跌停阻塞的交易。不能作为实盘事前条件。",
            "predicate": lambda data: pd.to_numeric(data["limit_down_blocked_days"], errors="coerce").fillna(0) > 0,
        },
        {
            "scenario": "diagnostic_reject_negative_b",
            "filter_type": "diagnostic_future_not_allowed",
            "description": "诊断过滤：过滤未来亏损 B 交易。不能作为实盘事前条件。",
            "predicate": lambda data: pd.to_numeric(data["daily_return"], errors="coerce").fillna(0) < 0,
        },
    ]


def rejected_key_set(b_replayed: pd.DataFrame, predicate: Callable[[pd.DataFrame], pd.Series]) -> set[tuple[str, str]]:
    mask = predicate(b_replayed).fillna(False).astype(bool)
    rejected = b_replayed[mask].copy()
    return {
        (normalize_date(row.trade_date), str(row.ts_code))
        for row in rejected.itertuples(index=False)
    }


def rebuild_combo(combo: pd.DataFrame, rejected_b_keys: set[tuple[str, str]], scenario: str) -> pd.DataFrame:
    rows = []
    current_equity = 0.0
    for row in combo.itertuples(index=False):
        row_dict = row._asdict()
        signal_date = normalize_date(row_dict.get("signal_date", ""))
        ts_code = str(row_dict.get("ts_code", ""))
        strategy_leg = str(row_dict.get("strategy_leg", ""))
        operation_status = str(row_dict.get("operation_status", ""))
        original_return = float(pd.to_numeric(row_dict.get("account_return", 0.0), errors="coerce") or 0.0)
        if not current_equity:
            current_equity = float(pd.to_numeric(row_dict.get("equity_before", 0.0), errors="coerce") or 0.0)

        rejected = strategy_leg == "B" and operation_status == "HISTORICAL_SIM_FILLED" and (signal_date, ts_code) in rejected_b_keys
        adjusted_status = "B_RISK_FILTERED_SKIP" if rejected else operation_status
        adjusted_return = 0.0 if rejected else original_return
        before = current_equity
        after = before * (1.0 + adjusted_return)
        current_equity = after

        row_dict["scenario"] = scenario
        row_dict["original_operation_status"] = operation_status
        row_dict["operation_status"] = adjusted_status
        row_dict["original_account_return"] = original_return
        row_dict["account_return"] = adjusted_return
        row_dict["risk_filter_rejected"] = rejected
        row_dict["equity_before"] = before
        row_dict["equity_after"] = after
        rows.append(row_dict)

    result = pd.DataFrame(rows)
    if result.empty:
        return result
    initial = float(result["equity_before"].iloc[0])
    result["initial_equity"] = initial
    result["peak_equity"] = result["equity_after"].cummax().clip(lower=initial)
    result["drawdown"] = result["equity_after"] / result["peak_equity"] - 1.0
    return result


def summarize_combo(detail: pd.DataFrame, scenario: str, filter_type: str, description: str, rejected_count: int) -> dict[str, Any]:
    trades = detail[detail["operation_status"].astype(str) == "HISTORICAL_SIM_FILLED"].copy()
    returns = pd.to_numeric(trades.get("account_return", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    initial = float(detail["initial_equity"].iloc[0]) if "initial_equity" in detail.columns and not detail.empty else 0.0
    final = float(detail["equity_after"].iloc[-1]) if not detail.empty else initial
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    status_counts = detail["operation_status"].value_counts().to_dict() if not detail.empty else {}
    return {
        "scenario": scenario,
        "filter_type": filter_type,
        "description": description,
        "rejected_b_trade_count": int(rejected_count),
        "executed_trade_count": int(len(trades)),
        "b_trade_count": int((trades.get("strategy_leg", pd.Series(dtype=str)) == "B").sum()),
        "a_trade_count": int((trades.get("strategy_leg", pd.Series(dtype=str)) == "A").sum()),
        "filtered_skip_count": int(status_counts.get("B_RISK_FILTERED_SKIP", 0)),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "initial_equity": initial,
        "final_equity": final,
        "equity_multiple": final / initial if initial else 0.0,
        "max_drawdown": float(detail["drawdown"].min()) if "drawdown" in detail.columns and not detail.empty else 0.0,
        "live_order_enabled": False,
    }


def write_markdown(path: Path, summary: pd.DataFrame, best_detail: pd.DataFrame) -> None:
    detail_columns = [
        "signal_date",
        "strategy_leg",
        "operation_status",
        "ts_code",
        "name",
        "account_return",
        "equity_after",
        "drawdown",
        "risk_filter_rejected",
        "risk_flags",
    ]
    detail_columns = [column for column in detail_columns if column in best_detail.columns]
    content = f"""# 备用策略 B 风险过滤压力测试

本报告只使用本地 CSV 做模拟盘风险过滤压力测试，不接实盘，不调用 QMT，不下真实订单。

## 汇总

{summary.to_markdown(index=False)}

## 最优事前过滤方案逐日明细

{best_detail[detail_columns].to_markdown(index=False) if not best_detail.empty else "无逐日明细。"}

## 口径限制

- `pre_trade` 过滤是事前可见字段，可以进入下一轮验证。
- `diagnostic_future_not_allowed` 是未来结果诊断，不能用于实盘或模拟盘事前过滤。
- 风险过滤后的结果仍然是日线保守口径，后续还需要分钟 K、集合竞价和五档盘口验证。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    b_replayed = read_csv(resolve_path(args.b_replayed))
    combo = read_csv(resolve_path(args.a_plus_b_detail))

    summaries = []
    detail_frames = []
    best_pre_trade_detail = pd.DataFrame()
    best_pre_trade_multiple = -1.0

    for definition in filter_definitions():
        rejected_keys = rejected_key_set(b_replayed, definition["predicate"])
        detail = rebuild_combo(combo, rejected_keys, definition["scenario"])
        summary = summarize_combo(
            detail,
            scenario=definition["scenario"],
            filter_type=definition["filter_type"],
            description=definition["description"],
            rejected_count=len(rejected_keys),
        )
        summaries.append(summary)
        detail_frames.append(detail)
        if (
            definition["filter_type"] == "pre_trade"
            and float(summary["equity_multiple"]) > best_pre_trade_multiple
        ):
            best_pre_trade_multiple = float(summary["equity_multiple"])
            best_pre_trade_detail = detail

    summary = pd.DataFrame(summaries).sort_values(
        ["filter_type", "equity_multiple", "max_drawdown"],
        ascending=[True, False, False],
    )
    detail_all = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()

    output_prefix = resolve_path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    best_detail_path = output_prefix.with_name(output_prefix.name + "_best_pre_trade_detail.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + ".md")

    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    detail_all.to_csv(detail_path, index=False, encoding="utf-8-sig")
    best_pre_trade_detail.to_csv(best_detail_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, best_pre_trade_detail)

    print("备用策略 B 风险过滤压力测试完成：")
    print(f"- summary: {summary_path}")
    print(f"- detail: {detail_path}")
    print(f"- best_pre_trade_detail: {best_detail_path}")
    print(f"- markdown: {markdown_path}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
