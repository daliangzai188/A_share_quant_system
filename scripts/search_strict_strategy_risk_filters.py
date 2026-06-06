"""
搜索严格版策略的事前风险过滤规则。

文件作用：
1. 读取 recent_2y_full_strategy_strict_detail.csv 中已保存的前 40 个严格场景。
2. 基于交易信号当时已经存在的因子分桶，测试“过滤后不替换”的保守风控效果。
3. 分别输出全区间、2024-2025 训练期、2026 样本外表现。
4. 用于判断哪些市场/板块状态会拖累 2026，避免继续盲目追高收益。

注意：
本脚本只读取本地报告，不调用外部接口，不接实盘。
过滤是保守口径：被过滤交易直接空仓，不从同日候选中补选其他股票。
"""
from __future__ import annotations

import argparse
from itertools import combinations
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


FILTER_COLUMNS = [
    "market_segment",
    "market_emotion_state_bucket",
    "segment_emotion_state_bucket",
    "market_chain_count_bucket",
    "segment_chain_count_bucket",
    "market_limit_down_count_bucket",
    "segment_limit_down_count_bucket",
    "segment_limit_down_ratio_bucket",
    "segment_limit_max_height_bucket",
    "market_leader_rank_bucket",
    "segment_market_leader_rank_bucket",
    "limit_height_rank_bucket",
    "segment_limit_height_rank_bucket",
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
    "retreat_state_bucket",
    "segment_retreat_state_bucket",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="搜索严格版策略风险过滤规则。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_full_strategy_strict_detail.csv",
        help="严格版完整优化逐笔明细。",
    )
    parser.add_argument(
        "--summary",
        default="reports/recent_2y_full_strategy_strict_summary.csv",
        help="严格版完整优化汇总。",
    )
    parser.add_argument("--top-scenarios", type=int, default=20, help="只分析前 N 个场景。")
    parser.add_argument("--max-single-values", type=int, default=80, help="单因子过滤候选数量上限。")
    parser.add_argument("--max-pair-rules", type=int, default=300, help="双因子过滤候选数量上限。")
    parser.add_argument("--train-start", default="20240101", help="训练开始日期。")
    parser.add_argument("--train-end", default="20251231", help="训练结束日期。")
    parser.add_argument("--test-start", default="20260101", help="测试开始日期。")
    parser.add_argument("--test-end", default="20260518", help="测试结束日期。")
    parser.add_argument(
        "--output-prefix",
        default="reports/strict_strategy_risk_filters",
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


def load_detail(path: Path, top_scenarios: int) -> pd.DataFrame:
    data = pd.read_csv(path, low_memory=False)
    data["scenario_rank"] = pd.to_numeric(data["scenario_rank"], errors="coerce")
    data = data[data["scenario_rank"] <= top_scenarios].copy()
    if data.empty:
        raise RuntimeError(f"没有可分析的严格版场景明细: {path}")
    for column in ["trade_date", "buy_trade_date", "exit_trade_date"]:
        if column in data.columns:
            data[column] = data[column].map(normalize_date)
    data["scenario_executed"] = data["scenario_executed"] == True  # noqa: E712
    data["dynamic_account_return"] = pd.to_numeric(data["dynamic_account_return"], errors="coerce").fillna(0.0)
    data["equity_before"] = pd.to_numeric(data["equity_before"], errors="coerce")
    data["equity_after"] = pd.to_numeric(data["equity_after"], errors="coerce")
    data["year"] = data["exit_trade_date"].astype(str).str[:4]
    return data.sort_values(["scenario_rank", "trade_order", "trade_date"]).reset_index(drop=True)


def load_summary(path: Path) -> pd.DataFrame:
    data = pd.read_csv(path, low_memory=False)
    data["scenario_rank"] = range(1, len(data) + 1)
    return data


def build_filter_candidates(
    data: pd.DataFrame,
    max_single_values: int,
    max_pair_rules: int,
    train_start: str,
    train_end: str,
) -> list[dict[str, Any]]:
    date = data["exit_trade_date"].fillna("").astype(str)
    executed = data[
        (data["scenario_executed"])
        & (date >= train_start)
        & (date <= train_end)
    ].copy()
    if executed.empty:
        raise RuntimeError("训练期没有已成交交易，无法生成风险过滤候选。")
    candidates: list[dict[str, Any]] = [{"rule_name": "baseline", "conditions": tuple()}]
    single_conditions: list[tuple[str, str]] = []
    scored_conditions = []
    for column in FILTER_COLUMNS:
        if column not in executed.columns:
            continue
        values = executed[column].fillna("missing").astype(str)
        for value, count in values.value_counts().items():
            if value in {"missing", "nan", "None", "unknown"} or count < 3:
                continue
            group = executed[values == value]
            returns = group["dynamic_account_return"].astype(float)
            score = float(returns.mean()) - float((returns > 0).mean()) * 0.01
            scored_conditions.append((score, int(count), column, value))
    scored_conditions = sorted(scored_conditions, key=lambda item: (item[0], -item[1]))
    for _, _, column, value in scored_conditions[:max_single_values]:
        condition = (column, value)
        single_conditions.append(condition)
        candidates.append({"rule_name": f"exclude|{column}={value}", "conditions": (condition,)})

    pair_count = 0
    for left, right in combinations(single_conditions, 2):
        if left[0] == right[0]:
            continue
        candidates.append(
            {
                "rule_name": f"exclude|{left[0]}={left[1]}&&{right[0]}={right[1]}",
                "conditions": (left, right),
            }
        )
        pair_count += 1
        if pair_count >= max_pair_rules:
            break
    return candidates


def matches_filter(row: pd.Series, conditions: tuple[tuple[str, str], ...]) -> bool:
    if not conditions:
        return False
    for column, value in conditions:
        if str(row.get(column, "missing")) != value:
            return False
    return True


def apply_filter(rule_rows: pd.DataFrame, conditions: tuple[tuple[str, str], ...]) -> pd.DataFrame:
    rows = []
    equity = None
    for _, row in rule_rows.iterrows():
        result = row.to_dict()
        if equity is None:
            equity = float(row["equity_before"]) if pd.notna(row["equity_before"]) else 500000.0
        if bool(row["scenario_executed"]) and matches_filter(row, conditions):
            result["risk_filter_executed"] = False
            result["risk_filter_reason"] = "risk_filter_skip"
            result["risk_filter_equity_before"] = equity
            result["risk_filter_account_return"] = 0.0
            result["risk_filter_equity_after"] = equity
        elif bool(row["scenario_executed"]):
            account_return = float(row["dynamic_account_return"])
            result["risk_filter_executed"] = True
            result["risk_filter_reason"] = ""
            result["risk_filter_equity_before"] = equity
            result["risk_filter_account_return"] = account_return
            equity = equity * (1.0 + account_return)
            result["risk_filter_equity_after"] = equity
        else:
            result["risk_filter_executed"] = False
            result["risk_filter_reason"] = str(row.get("skip_reason", "original_skip"))
            result["risk_filter_equity_before"] = equity
            result["risk_filter_account_return"] = 0.0
            result["risk_filter_equity_after"] = equity
        rows.append(result)
    return pd.DataFrame(rows)


def summarize(filtered: pd.DataFrame, scenario: str, rule_name: str, period_name: str = "full") -> dict[str, Any]:
    executed = filtered[filtered["risk_filter_executed"] == True].copy()  # noqa: E712
    skipped_by_filter = filtered[filtered["risk_filter_reason"] == "risk_filter_skip"].copy()
    returns = executed["risk_filter_account_return"].astype(float) if not executed.empty else pd.Series(dtype=float)
    if executed.empty:
        first_equity = float(filtered["risk_filter_equity_before"].dropna().iloc[0]) if not filtered.empty else 500000.0
        last_equity = first_equity
    else:
        first_equity = float(executed["risk_filter_equity_before"].iloc[0])
        last_equity = float(executed["risk_filter_equity_after"].iloc[-1])
    return {
        "scenario": scenario,
        "rule_name": rule_name,
        "period": period_name,
        "first_equity": first_equity,
        "last_equity": last_equity,
        "equity_multiple": last_equity / first_equity if first_equity else 0.0,
        "period_return": last_equity / first_equity - 1.0 if first_equity else 0.0,
        "executed_trade_count": int(len(executed)),
        "risk_filtered_count": int(len(skipped_by_filter)),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_drawdown": max_drawdown(executed["risk_filter_equity_after"]) if not executed.empty else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(returns),
    }


def period_slice(filtered: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    date = filtered["exit_trade_date"].fillna("").astype(str)
    return filtered[(date >= start) & (date <= end)].copy()


def evaluate_rules(
    detail: pd.DataFrame,
    filter_rules: list[dict[str, Any]],
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    summary_rows = []
    detail_frames = []
    for scenario, group in detail.groupby("scenario"):
        group = group.sort_values(["trade_order", "trade_date"]).reset_index(drop=True)
        for rule in filter_rules:
            filtered = apply_filter(group, rule["conditions"])
            full = summarize(filtered, scenario, rule["rule_name"], "full")
            train = summarize(period_slice(filtered, train_start, train_end), scenario, rule["rule_name"], "train_2024_2025")
            test = summarize(period_slice(filtered, test_start, test_end), scenario, rule["rule_name"], "test_2026")
            baseline = rule["rule_name"] == "baseline"
            summary_rows.append(
                {
                    **full,
                    "train_equity_multiple": train["equity_multiple"],
                    "train_trade_count": train["executed_trade_count"],
                    "train_max_drawdown": train["max_drawdown"],
                    "train_win_rate": train["win_rate"],
                    "test_equity_multiple": test["equity_multiple"],
                    "test_trade_count": test["executed_trade_count"],
                    "test_max_drawdown": test["max_drawdown"],
                    "test_win_rate": test["win_rate"],
                    "is_baseline": baseline,
                    "conditions": ";".join(f"{column}={value}" for column, value in rule["conditions"]),
                }
            )
            if baseline or len(detail_frames) < 20:
                frame = filtered.copy()
                frame["risk_rule_name"] = rule["rule_name"]
                detail_frames.append(frame)
    return pd.DataFrame(summary_rows), pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()


def add_baseline_comparison(summary: pd.DataFrame) -> pd.DataFrame:
    result = summary.copy()
    baseline = result[result["is_baseline"]].set_index("scenario")
    deltas = []
    for _, row in result.iterrows():
        base = baseline.loc[row["scenario"]]
        deltas.append(
            {
                "baseline_equity_multiple": float(base["equity_multiple"]),
                "baseline_max_drawdown": float(base["max_drawdown"]),
                "baseline_test_equity_multiple": float(base["test_equity_multiple"]),
                "baseline_test_max_drawdown": float(base["test_max_drawdown"]),
                "equity_multiple_delta": float(row["equity_multiple"]) - float(base["equity_multiple"]),
                "drawdown_delta": float(row["max_drawdown"]) - float(base["max_drawdown"]),
                "test_equity_multiple_delta": float(row["test_equity_multiple"]) - float(base["test_equity_multiple"]),
                "test_drawdown_delta": float(row["test_max_drawdown"]) - float(base["test_max_drawdown"]),
            }
        )
    delta_frame = pd.DataFrame(deltas)
    return pd.concat([result.reset_index(drop=True), delta_frame], axis=1)


def main() -> None:
    args = parse_args()
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    detail = load_detail(PROJECT_ROOT / args.input, args.top_scenarios)
    filter_rules = build_filter_candidates(
        detail,
        args.max_single_values,
        args.max_pair_rules,
        args.train_start,
        args.train_end,
    )
    summary, detail_report = evaluate_rules(
        detail=detail,
        filter_rules=filter_rules,
        train_start=args.train_start,
        train_end=args.train_end,
        test_start=args.test_start,
        test_end=args.test_end,
    )
    summary = add_baseline_comparison(summary)
    summary = summary.sort_values(
        [
            "test_equity_multiple_delta",
            "test_max_drawdown",
            "equity_multiple",
            "max_drawdown",
        ],
        ascending=[False, True, False, True],
    ).reset_index(drop=True)

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    detail_report.to_csv(detail_path, index=False, encoding="utf-8-sig")

    print("严格版风险过滤搜索完成：")
    print(f"- summary: {summary_path}")
    print(f"- detail: {detail_path}")
    print(summary.head(20).to_string(index=False))


if __name__ == "__main__":
    main()
