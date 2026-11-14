"""
搜索 bj_cap_2pct 的前视有效风险过滤条件。

文件作用：
1. 固定 bj_cap_2pct 策略，不改变原始选股主条件和排序。
2. 只使用交易日前已经可见的分类字段，搜索单字段/双字段排除规则。
3. 保守处理：过滤掉原本可成交交易后，不补入原来因持仓占用而跳过的候选。
4. 输出每条过滤规则的全区间收益、2026收益、回撤、笔数和过滤样本数。

本脚本只读取本地报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
from itertools import combinations
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
TARGET_RULE = "bj_cap_2pct"


FORWARD_VALID_FEATURES = [
    "market_segment",
    "first_time_detail_bucket",
    "limit_times_detail_bucket",
    "open_times_bucket",
    "amount_bucket",
    "turnover_rate_bucket",
    "volume_ratio_bucket",
    "fd_ratio_bucket",
    "pct_chg_bucket",
    "prev_pct_chg_bucket",
    "amount_ratio_bucket",
    "limit_up_count_bucket",
    "segment_limit_up_count_bucket",
    "segment_limit_up_ratio_bucket",
    "market_emotion_state_bucket",
    "segment_emotion_state_bucket",
    "market_chain_count_bucket",
    "segment_chain_count_bucket",
    "market_limit_down_count_bucket",
    "segment_limit_down_count_bucket",
    "segment_limit_down_ratio_bucket",
    "segment_limit_max_height_bucket",
    "retreat_state_bucket",
    "segment_retreat_state_bucket",
    "market_leader_rank_bucket",
    "segment_market_leader_rank_bucket",
    "limit_height_rank_bucket",
    "segment_limit_height_rank_bucket",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="搜索 bj_cap_2pct 前视有效风险过滤条件。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_bj_filter_rules_detail.csv",
        help="bj_cap_2pct 逐笔明细报告。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/bj_cap2_forward_risk_filters",
        help="输出文件前缀。",
    )
    parser.add_argument("--min-filtered-trades", type=int, default=2, help="规则至少过滤的成交笔数。")
    parser.add_argument("--top", type=int, default=200, help="输出排名靠前的规则数量。")
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


def load_rows(path: Path) -> pd.DataFrame:
    rows = pd.read_csv(path, low_memory=False)
    rows = rows[rows["rule_name"] == TARGET_RULE].copy()
    if rows.empty:
        raise RuntimeError(f"没有找到 {TARGET_RULE}: {path}")
    rows["selected_order"] = pd.to_numeric(rows["selected_order"], errors="coerce")
    rows["trade_date"] = rows["trade_date"].map(normalize_date)
    rows["exit_trade_date"] = rows["exit_trade_date"].map(normalize_date)
    rows["year"] = rows["exit_trade_date"].str[:4]
    for feature in FORWARD_VALID_FEATURES:
        if feature in rows.columns:
            rows[feature] = rows[feature].fillna("missing").astype(str)
    return rows.sort_values("selected_order").reset_index(drop=True)


def build_filter_candidates(rows: pd.DataFrame, min_filtered_trades: int) -> list[dict[str, Any]]:
    executed = rows[rows["rule_executed"] == True].copy()  # noqa: E712
    candidates: list[dict[str, Any]] = [{"filter_name": "baseline", "conditions": tuple()}]

    available_features = [feature for feature in FORWARD_VALID_FEATURES if feature in rows.columns]
    single_conditions: list[tuple[str, str]] = []
    for feature in available_features:
        counts = executed[feature].value_counts(dropna=False)
        for value, count in counts.items():
            if int(count) < min_filtered_trades:
                continue
            condition = (feature, str(value))
            single_conditions.append(condition)
            candidates.append(
                {
                    "filter_name": f"{feature}={value}",
                    "conditions": (condition,),
                }
            )

    for left, right in combinations(single_conditions, 2):
        if left[0] == right[0]:
            continue
        mask = (executed[left[0]] == left[1]) & (executed[right[0]] == right[1])
        filtered_count = int(mask.sum())
        if filtered_count < min_filtered_trades:
            continue
        conditions = tuple(sorted([left, right]))
        name = ";".join(f"{feature}={value}" for feature, value in conditions)
        candidates.append({"filter_name": name, "conditions": conditions})

    deduped: list[dict[str, Any]] = []
    seen = set()
    for candidate in candidates:
        key = candidate["conditions"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def row_matches_filter(row: pd.Series, conditions: tuple[tuple[str, str], ...]) -> bool:
    if not conditions:
        return False
    for feature, value in conditions:
        if str(row.get(feature, "missing")) != value:
            return False
    return True


def summarize_returns(returns: pd.Series, initial_cash: float, prefix: str) -> dict[str, Any]:
    if returns.empty:
        return {
            f"{prefix}_final_equity": initial_cash,
            f"{prefix}_equity_multiple": 1.0,
            f"{prefix}_trade_count": 0,
            f"{prefix}_win_rate": 0.0,
            f"{prefix}_avg_account_return": 0.0,
            f"{prefix}_median_account_return": 0.0,
            f"{prefix}_max_profit": 0.0,
            f"{prefix}_max_loss": 0.0,
            f"{prefix}_max_drawdown": 0.0,
            f"{prefix}_max_consecutive_losses": 0,
        }

    equity = initial_cash * (1.0 + returns).cumprod()
    final_equity = float(equity.iloc[-1])
    return {
        f"{prefix}_final_equity": final_equity,
        f"{prefix}_equity_multiple": final_equity / initial_cash if initial_cash else 0.0,
        f"{prefix}_trade_count": int(len(returns)),
        f"{prefix}_win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        f"{prefix}_avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        f"{prefix}_median_account_return": float(returns.median()) if len(returns) else 0.0,
        f"{prefix}_max_profit": float(returns.max()) if len(returns) else 0.0,
        f"{prefix}_max_loss": float(returns.min()) if len(returns) else 0.0,
        f"{prefix}_max_drawdown": max_drawdown(equity),
        f"{prefix}_max_consecutive_losses": max_consecutive_losses(returns),
    }


def build_filter_mask(rows: pd.DataFrame, conditions: tuple[tuple[str, str], ...]) -> pd.Series:
    if not conditions:
        return pd.Series(False, index=rows.index)
    mask = pd.Series(True, index=rows.index)
    for feature, value in conditions:
        mask &= rows[feature].astype(str).eq(value)
    return mask


def simulate_filter(rows: pd.DataFrame, candidate: dict[str, Any]) -> dict[str, Any]:
    initial_cash = float(rows["rule_equity_before"].dropna().iloc[0])
    original_executed = rows["rule_executed"].astype(bool)
    filter_mask = original_executed & build_filter_mask(rows, candidate["conditions"])
    effective_executed = original_executed & ~filter_mask
    returns = pd.to_numeric(rows.loc[effective_executed, "rule_account_return"], errors="coerce").fillna(0.0)
    year_2026 = rows["exit_trade_date"].map(normalize_date).str[:4].eq("2026")
    returns_2026 = pd.to_numeric(rows.loc[effective_executed & year_2026, "rule_account_return"], errors="coerce").fillna(0.0)
    summary = {
        "filter_name": candidate["filter_name"],
        "condition_count": len(candidate["conditions"]),
        "conditions": ";".join(f"{feature}={value}" for feature, value in candidate["conditions"]),
        "filtered_executed_count": int(filter_mask.sum()),
        "filtered_2026_count": int((filter_mask & year_2026).sum()),
    }
    summary.update(summarize_returns(returns, initial_cash, "full"))
    summary.update(summarize_returns(returns_2026, initial_cash, "y2026"))
    return summary


def main() -> None:
    args = parse_args()
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    rows = load_rows(PROJECT_ROOT / args.input)
    candidates = build_filter_candidates(rows, args.min_filtered_trades)
    summaries = []
    for candidate in candidates:
        summaries.append(simulate_filter(rows, candidate))

    summary_report = pd.DataFrame(summaries)
    baseline = summary_report[summary_report["filter_name"] == "baseline"].iloc[0]
    summary_report["full_multiple_delta_vs_baseline"] = (
        summary_report["full_equity_multiple"] - float(baseline["full_equity_multiple"])
    )
    summary_report["y2026_dd_improvement_vs_baseline"] = (
        summary_report["y2026_max_drawdown"] - float(baseline["y2026_max_drawdown"])
    )
    summary_report["passes_50x"] = summary_report["full_equity_multiple"] >= 50.0
    summary_report = summary_report.sort_values(
        [
            "passes_50x",
            "y2026_dd_improvement_vs_baseline",
            "full_equity_multiple",
            "filtered_executed_count",
        ],
        ascending=[False, False, False, True],
    ).reset_index(drop=True)

    output_summary = summary_report.head(args.top).copy()
    baseline_rows = summary_report[summary_report["filter_name"] == "baseline"].copy()
    if not baseline_rows.empty and "baseline" not in set(output_summary["filter_name"].astype(str)):
        output_summary = pd.concat([baseline_rows, output_summary], ignore_index=True)

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    output_summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

    print("bj_cap_2pct 前视风险过滤搜索完成")
    print(
        summary_report.head(20)[
            [
                "filter_name",
                "full_equity_multiple",
                "full_trade_count",
                "full_max_drawdown",
                "y2026_equity_multiple",
                "y2026_trade_count",
                "y2026_max_drawdown",
                "filtered_executed_count",
                "filtered_2026_count",
                "passes_50x",
            ]
        ].to_string(index=False)
    )
    print("报告文件：")
    print(f"- summary: {summary_path}")


if __name__ == "__main__":
    main()
