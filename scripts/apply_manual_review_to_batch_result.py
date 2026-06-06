"""
把人工复核决策应用到批量模拟盘结果。

文件作用：
1. 读取多日模拟盘 daily.csv。
2. 读取人工复核决策结果 detail.csv。
3. 将 review_decision=REJECTED 的已成交日按跳过处理。
4. 在不回填替代候选的保守口径下，重算资金曲线和汇总指标。
5. 输出过滤前后对比报告。

本脚本只处理本地 CSV，不接实盘，不调用 QMT，不下真实订单。
"""
from __future__ import annotations

import argparse
from itertools import product
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把人工复核决策应用到批量模拟盘结果。")
    parser.add_argument(
        "--daily",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_20240520_20260417_daily.csv",
        help="批量模拟盘每日状态文件。",
    )
    parser.add_argument(
        "--manual-review-result",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_manual_review_result_detail.csv",
        help="人工复核决策结果明细。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/paper_trade/batch_flow/a_clean_exclude_star_prev0_3_bj_manual_review_adjusted",
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


def normalize_dates(data: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    result = data.copy()
    for column in columns:
        if column in result.columns:
            result[column] = result[column].map(normalize_date)
    return result


def to_bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    curve = pd.to_numeric(equity, errors="coerce").ffill().fillna(0.0)
    peak = curve.cummax()
    drawdown = curve / peak - 1.0
    return float(drawdown.min())


def build_decision_map(review_result: pd.DataFrame) -> dict[tuple[str, str], dict[str, Any]]:
    normalized = normalize_dates(review_result, ["signal_date"])
    decisions: dict[tuple[str, str], dict[str, Any]] = {}
    if normalized.empty:
        return decisions
    for row in normalized.itertuples(index=False):
        signal_date = str(getattr(row, "signal_date", ""))
        ts_code = str(getattr(row, "ts_code", ""))
        decisions[(signal_date, ts_code)] = {
            "review_decision": str(getattr(row, "review_decision", "PENDING")).upper().strip(),
            "suggested_decision": str(getattr(row, "suggested_decision", "")),
            "suggestion_reason": str(getattr(row, "suggestion_reason", "")),
            "review_note": str(getattr(row, "review_note", "")),
        }
    return decisions


def apply_decision_overrides(
    decision_map: dict[tuple[str, str], dict[str, Any]],
    overrides: dict[tuple[str, str], str],
) -> dict[tuple[str, str], dict[str, Any]]:
    result = {key: value.copy() for key, value in decision_map.items()}
    for key, decision in overrides.items():
        if key not in result:
            continue
        result[key]["review_decision"] = decision
    return result


def rebuild_daily_after_review(daily: pd.DataFrame, decision_map: dict[tuple[str, str], dict[str, Any]]) -> pd.DataFrame:
    result = normalize_dates(daily, ["signal_date"]).copy()
    result["original_daily_status"] = result["daily_status"].fillna("").astype(str)
    result["original_account_return"] = pd.to_numeric(result["account_return"], errors="coerce").fillna(0.0)
    result["original_equity_before"] = pd.to_numeric(result["equity_before"], errors="coerce").fillna(0.0)
    result["original_equity_after"] = pd.to_numeric(result["equity_after"], errors="coerce").fillna(0.0)

    initial_equity = resolve_initial_equity(result)
    current_equity = initial_equity
    adjusted_rows = []

    for row in result.itertuples(index=False):
        row_dict = row._asdict()
        signal_date = str(row_dict.get("signal_date", ""))
        ts_code = str(row_dict.get("top_ts_code", ""))
        decision = decision_map.get((signal_date, ts_code), {})
        review_decision = str(decision.get("review_decision", "")).upper().strip()
        original_status = str(row_dict.get("original_daily_status", ""))
        original_return = float(row_dict.get("original_account_return", 0.0))
        rejected = review_decision == "REJECTED" and original_status == "CLOSED_BY_HISTORICAL_SIM"

        adjusted_return = 0.0 if rejected else original_return
        adjusted_status = "MANUAL_REJECTED_SKIP" if rejected else original_status
        adjusted_before = current_equity
        adjusted_after = current_equity * (1.0 + adjusted_return)
        current_equity = adjusted_after

        row_dict["review_decision"] = review_decision or ""
        row_dict["suggested_decision"] = decision.get("suggested_decision", "")
        row_dict["suggestion_reason"] = decision.get("suggestion_reason", "")
        row_dict["review_note"] = decision.get("review_note", "")
        row_dict["manual_review_rejected_applied"] = rejected
        row_dict["adjusted_daily_status"] = adjusted_status
        row_dict["adjusted_account_return"] = adjusted_return
        row_dict["adjusted_equity_before"] = adjusted_before
        row_dict["adjusted_equity_after"] = adjusted_after
        row_dict["adjusted_equity_delta_vs_original_after"] = adjusted_after - float(row_dict.get("original_equity_after", 0.0))
        adjusted_rows.append(row_dict)

    adjusted = pd.DataFrame(adjusted_rows)
    adjusted["adjusted_peak_equity"] = adjusted["adjusted_equity_after"].cummax()
    adjusted["adjusted_drawdown"] = adjusted["adjusted_equity_after"] / adjusted["adjusted_peak_equity"] - 1.0
    return adjusted


def resolve_initial_equity(daily: pd.DataFrame) -> float:
    for column in ["equity_start_of_day", "equity_before", "original_equity_before"]:
        if column in daily.columns and not daily.empty:
            value = pd.to_numeric(daily[column], errors="coerce").dropna()
            if not value.empty and float(value.iloc[0]) > 0:
                return float(value.iloc[0])
    return 0.0


def summarize_original(daily: pd.DataFrame) -> dict[str, Any]:
    returns = pd.to_numeric(daily.get("account_return", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    status = daily.get("daily_status", pd.Series(dtype=str)).fillna("").astype(str)
    closed_returns = returns[status == "CLOSED_BY_HISTORICAL_SIM"]
    initial_equity = resolve_initial_equity(daily)
    final_equity = resolve_original_final_equity(daily)
    equity_after = pd.to_numeric(daily.get("equity_after", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    return build_summary_row(
        scenario="原始模拟盘",
        initial_equity=initial_equity,
        final_equity=final_equity,
        trade_day_count=len(daily),
        closed_returns=closed_returns,
        no_candidate_day_count=int((status == "NO_CANDIDATE").sum()),
        position_occupied_skip_day_count=int((status == "POSITION_OCCUPIED_SKIP").sum()),
        manual_rejected_skip_count=0,
        equity_for_drawdown=equity_after,
    )


def resolve_original_final_equity(daily: pd.DataFrame) -> float:
    for column in ["equity_end_of_day", "equity_after"]:
        if column in daily.columns and not daily.empty:
            value = pd.to_numeric(daily[column], errors="coerce").dropna()
            if not value.empty:
                return float(value.iloc[-1])
    return resolve_initial_equity(daily)


def summarize_adjusted(adjusted: pd.DataFrame) -> dict[str, Any]:
    status = adjusted["adjusted_daily_status"].fillna("").astype(str)
    closed_returns = pd.to_numeric(
        adjusted.loc[status == "CLOSED_BY_HISTORICAL_SIM", "adjusted_account_return"],
        errors="coerce",
    ).fillna(0.0)
    return build_summary_row(
        scenario="人工复核过滤后",
        initial_equity=float(adjusted["adjusted_equity_before"].iloc[0]) if not adjusted.empty else 0.0,
        final_equity=float(adjusted["adjusted_equity_after"].iloc[-1]) if not adjusted.empty else 0.0,
        trade_day_count=len(adjusted),
        closed_returns=closed_returns,
        no_candidate_day_count=int((status == "NO_CANDIDATE").sum()),
        position_occupied_skip_day_count=int((status == "POSITION_OCCUPIED_SKIP").sum()),
        manual_rejected_skip_count=int(adjusted["manual_review_rejected_applied"].sum()),
        equity_for_drawdown=adjusted["adjusted_equity_after"],
    )


def build_summary_row(
    scenario: str,
    initial_equity: float,
    final_equity: float,
    trade_day_count: int,
    closed_returns: pd.Series,
    no_candidate_day_count: int,
    position_occupied_skip_day_count: int,
    manual_rejected_skip_count: int,
    equity_for_drawdown: pd.Series,
) -> dict[str, Any]:
    closed_returns = pd.to_numeric(closed_returns, errors="coerce").fillna(0.0)
    wins = closed_returns > 0
    losses = closed_returns < 0
    gross_profit = float(closed_returns[closed_returns > 0].sum()) if len(closed_returns) else 0.0
    gross_loss = abs(float(closed_returns[closed_returns < 0].sum())) if len(closed_returns) else 0.0
    return {
        "scenario": scenario,
        "trade_day_count": int(trade_day_count),
        "closed_trade_count": int(len(closed_returns)),
        "win_count": int(wins.sum()),
        "loss_count": int(losses.sum()),
        "no_candidate_day_count": int(no_candidate_day_count),
        "position_occupied_skip_day_count": int(position_occupied_skip_day_count),
        "manual_rejected_skip_count": int(manual_rejected_skip_count),
        "initial_equity": float(initial_equity),
        "final_equity": float(final_equity),
        "equity_multiple": float(final_equity / initial_equity) if initial_equity else 0.0,
        "total_return": float(final_equity / initial_equity - 1.0) if initial_equity else 0.0,
        "win_rate": float(wins.mean()) if len(closed_returns) else 0.0,
        "avg_account_return": float(closed_returns.mean()) if len(closed_returns) else 0.0,
        "median_account_return": float(closed_returns.median()) if len(closed_returns) else 0.0,
        "max_profit": float(closed_returns.max()) if len(closed_returns) else 0.0,
        "max_loss": float(closed_returns.min()) if len(closed_returns) else 0.0,
        "profit_loss_ratio": float(gross_profit / gross_loss) if gross_loss else 0.0,
        "max_drawdown": max_drawdown(equity_for_drawdown),
        "live_order_enabled": False,
    }


def build_rejected_impact(adjusted: pd.DataFrame) -> pd.DataFrame:
    rejected = adjusted[adjusted["manual_review_rejected_applied"]].copy()
    if rejected.empty:
        return pd.DataFrame(
            columns=[
                "signal_date",
                "ts_code",
                "name",
                "original_account_return",
                "avoided_original_loss_amount",
                "adjusted_equity_before",
                "suggestion_reason",
            ]
        )
    rejected["avoided_original_loss_amount"] = (
        rejected["adjusted_equity_before"]
        * pd.to_numeric(rejected["original_account_return"], errors="coerce").fillna(0.0).abs()
    )
    columns = [
        "signal_date",
        "top_ts_code",
        "top_name",
        "original_account_return",
        "avoided_original_loss_amount",
        "adjusted_equity_before",
        "suggested_decision",
        "suggestion_reason",
        "review_note",
    ]
    return rejected[columns].rename(columns={"top_ts_code": "ts_code", "top_name": "name"})


def build_pending_scenarios(
    daily: pd.DataFrame,
    review_result: pd.DataFrame,
    base_decision_map: dict[tuple[str, str], dict[str, Any]],
) -> pd.DataFrame:
    normalized_review = normalize_dates(review_result, ["signal_date"])
    pending = normalized_review[normalized_review["review_decision"].fillna("").astype(str).str.upper() == "PENDING"]
    if pending.empty:
        return pd.DataFrame(
            columns=[
                "scenario",
                "pending_rejected_count",
                "pending_kept_count",
                "pending_rejected_codes",
                "closed_trade_count",
                "final_equity",
                "equity_multiple",
                "win_rate",
                "max_drawdown",
                "avg_account_return",
                "max_loss",
                "profit_loss_ratio",
            ]
        )

    pending_keys = [
        (str(row.signal_date), str(row.ts_code), str(row.name))
        for row in pending.itertuples(index=False)
    ]
    rows = []
    for bits in product([False, True], repeat=len(pending_keys)):
        overrides = {
            (signal_date, ts_code): "REJECTED" if reject else "PENDING"
            for (signal_date, ts_code, _name), reject in zip(pending_keys, bits)
        }
        scenario_decision_map = apply_decision_overrides(base_decision_map, overrides)
        adjusted = rebuild_daily_after_review(daily, scenario_decision_map)
        summary = summarize_adjusted(adjusted)
        rejected_labels = [
            f"{signal_date} {ts_code} {name}"
            for (signal_date, ts_code, name), reject in zip(pending_keys, bits)
            if reject
        ]
        rows.append(
            {
                "scenario": "keep_all_pending" if not rejected_labels else "reject_" + "_".join(
                    signal_date for signal_date, _ts_code, _name in pending_keys if (signal_date + " " + _ts_code + " " + _name) in rejected_labels
                ),
                "pending_rejected_count": int(sum(bits)),
                "pending_kept_count": int(len(pending_keys) - sum(bits)),
                "pending_rejected_codes": ";".join(rejected_labels),
                "closed_trade_count": summary["closed_trade_count"],
                "final_equity": summary["final_equity"],
                "equity_multiple": summary["equity_multiple"],
                "win_rate": summary["win_rate"],
                "max_drawdown": summary["max_drawdown"],
                "avg_account_return": summary["avg_account_return"],
                "max_loss": summary["max_loss"],
                "profit_loss_ratio": summary["profit_loss_ratio"],
                "live_order_enabled": False,
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["equity_multiple", "max_drawdown", "closed_trade_count"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def write_markdown(
    path: Path,
    summary: pd.DataFrame,
    rejected_impact: pd.DataFrame,
    pending_scenarios: pd.DataFrame,
) -> None:
    scenario_columns = [
        "pending_rejected_count",
        "pending_rejected_codes",
        "closed_trade_count",
        "final_equity",
        "equity_multiple",
        "win_rate",
        "max_drawdown",
        "max_loss",
    ]
    scenario_columns = [column for column in scenario_columns if column in pending_scenarios.columns]
    content = f"""# 人工复核过滤后模拟盘对比

本报告只基于本地 CSV 重算资金曲线，不接实盘，不调用 QMT，不下真实订单。

口径说明：`REJECTED` 交易按跳过处理，不回填替代候选，因此这是保守口径。

## 汇总对比

{summary.to_markdown(index=False)}

## 被拒绝交易影响

{rejected_impact.to_markdown(index=False) if not rejected_impact.empty else "无被拒绝交易。"}

## PENDING 组合压力测试

{pending_scenarios[scenario_columns].head(20).to_markdown(index=False) if not pending_scenarios.empty else "无 PENDING 记录。"}
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    daily_path = resolve_path(args.daily)
    review_path = resolve_path(args.manual_review_result)
    output_prefix = resolve_path(args.output_prefix)

    daily = read_csv(daily_path)
    review_result = read_csv(review_path)
    decision_map = build_decision_map(review_result)
    adjusted_daily = rebuild_daily_after_review(daily, decision_map)
    summary = pd.DataFrame([summarize_original(daily), summarize_adjusted(adjusted_daily)])
    rejected_impact = build_rejected_impact(adjusted_daily)
    pending_scenarios = build_pending_scenarios(daily, review_result, decision_map)

    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    daily_path_out = output_prefix.with_name(output_prefix.name + "_daily.csv")
    rejected_path = output_prefix.with_name(output_prefix.name + "_rejected_impact.csv")
    pending_scenarios_path = output_prefix.with_name(output_prefix.name + "_pending_scenarios.csv")
    markdown_path = output_prefix.with_name(output_prefix.name + ".md")

    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    adjusted_daily.to_csv(daily_path_out, index=False, encoding="utf-8-sig")
    rejected_impact.to_csv(rejected_path, index=False, encoding="utf-8-sig")
    pending_scenarios.to_csv(pending_scenarios_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, rejected_impact, pending_scenarios)

    print("人工复核过滤后模拟盘对比完成：")
    print(f"- summary: {summary_path}")
    print(f"- daily: {daily_path_out}")
    print(f"- rejected_impact: {rejected_path}")
    print(f"- pending_scenarios: {pending_scenarios_path}")
    print(f"- markdown: {markdown_path}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
