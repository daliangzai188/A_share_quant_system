"""
分析模拟盘前 WARN 交易的风险来源。

文件作用：
1. 读取模拟盘前逐笔复盘清单和策略审计逐笔交易明细。
2. 对 WARN/大亏交易做因子分桶集中度分析。
3. 生成单因子、二维因子和候选风险规则报告。
4. 帮助判断后续是做硬过滤、降仓、拆单，还是只做模拟盘预警。

本脚本只读取本地 CSV 报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
from itertools import combinations
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]


FEATURE_COLUMNS = [
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
    parser = argparse.ArgumentParser(description="分析模拟盘前 WARN 交易风险来源。")
    parser.add_argument(
        "--review-trades",
        default="reports/a_clean_exclude_star_pre_paper_review_trades.csv",
        help="模拟盘前逐笔复盘清单。",
    )
    parser.add_argument(
        "--audit-trades",
        default="reports/a_clean_profit_source_exclude_star_best_audit_trades.csv",
        help="策略审计逐笔交易明细。",
    )
    parser.add_argument(
        "--warn-loss-threshold",
        type=float,
        default=-0.08,
        help="大亏交易阈值，账户单笔收益小于等于该值纳入风险目标。",
    )
    parser.add_argument("--min-target-count", type=int, default=1, help="候选规则最少命中风险交易数。")
    parser.add_argument("--max-pair-buckets", type=int, default=30, help="二维组合最多使用的单因子风险桶数量。")
    parser.add_argument(
        "--output-prefix",
        default="reports/a_clean_exclude_star_warn_source_analysis",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def normalize_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        return False
    return str(value).strip().lower() in {"true", "1", "yes"}


def normalize_number(value: object, default: float = 0.0) -> float:
    result = pd.to_numeric(value, errors="coerce")
    if pd.isna(result):
        return default
    return float(result)


def max_drawdown_from_returns(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    equity = (1.0 + returns.astype(float)).cumprod()
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def max_consecutive_losses(returns: pd.Series) -> int:
    current = 0
    result = 0
    for value in returns.astype(float):
        if value <= 0:
            current += 1
            result = max(result, current)
        else:
            current = 0
    return result


def calc_metrics(data: pd.DataFrame) -> dict[str, float | int]:
    returns = data["dynamic_account_return"].astype(float)
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    return {
        "trade_count": int(len(data)),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "compound_multiple": float((1.0 + returns).prod()) if len(returns) else 0.0,
        "max_drawdown": max_drawdown_from_returns(returns),
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(returns),
    }


def load_review(path: Path) -> pd.DataFrame:
    review = pd.read_csv(path, low_memory=False)
    required = {"trade_order", "ts_code", "review_status", "risk_flags"}
    missing = sorted(required - set(review.columns))
    if missing:
        raise RuntimeError(f"复盘清单缺少字段 {missing}: {path}")
    review["trade_order"] = pd.to_numeric(review["trade_order"], errors="coerce").fillna(0).astype(int)
    review["ts_code"] = review["ts_code"].astype(str)
    return review


def load_audit(path: Path) -> pd.DataFrame:
    audit = pd.read_csv(path, low_memory=False)
    if "scenario_executed" in audit.columns:
        audit = audit[audit["scenario_executed"].astype(str).str.lower().isin({"true", "1"})].copy()
    required = {"trade_order", "ts_code", "dynamic_account_return"}
    missing = sorted(required - set(audit.columns))
    if missing:
        raise RuntimeError(f"审计明细缺少字段 {missing}: {path}")
    audit["trade_order"] = pd.to_numeric(audit["trade_order"], errors="coerce").fillna(0).astype(int)
    audit["ts_code"] = audit["ts_code"].astype(str)
    audit["dynamic_account_return"] = pd.to_numeric(
        audit["dynamic_account_return"], errors="coerce"
    ).fillna(0.0)
    for column in FEATURE_COLUMNS:
        if column in audit.columns:
            audit[column] = audit[column].fillna("missing").astype(str)
    return audit


def load_merged(review_path: Path, audit_path: Path, warn_loss_threshold: float) -> pd.DataFrame:
    review = load_review(review_path)
    audit = load_audit(audit_path)
    merged = audit.merge(
        review[["trade_order", "ts_code", "review_status", "risk_flags"]],
        on=["trade_order", "ts_code"],
        how="left",
        validate="one_to_one",
    )
    merged["review_status"] = merged["review_status"].fillna("MISSING_REVIEW")
    merged["risk_flags"] = merged["risk_flags"].fillna("未进入预审清单")
    merged["is_warn_trade"] = merged["review_status"].astype(str).ne("PASS")
    merged["is_large_loss"] = merged["dynamic_account_return"].astype(float) <= warn_loss_threshold
    merged["is_risk_target"] = merged["is_warn_trade"] | merged["is_large_loss"]
    return merged.sort_values(["trade_order", "trade_date", "ts_code"]).reset_index(drop=True)


def build_summary(data: pd.DataFrame, warn_loss_threshold: float) -> pd.DataFrame:
    risk = data[data["is_risk_target"]].copy()
    warn = data[data["is_warn_trade"]].copy()
    large_loss = data[data["is_large_loss"]].copy()
    return pd.DataFrame(
        [
            {
                "trade_count": int(len(data)),
                "warn_trade_count": int(len(warn)),
                "large_loss_threshold": float(warn_loss_threshold),
                "large_loss_count": int(len(large_loss)),
                "risk_target_count": int(len(risk)),
                **{f"all_{key}": value for key, value in calc_metrics(data).items()},
                **{f"risk_{key}": value for key, value in calc_metrics(risk).items()},
            }
        ]
    )


def build_warning_trades(data: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "review_status",
        "risk_flags",
        "trade_order",
        "trade_date",
        "ts_code",
        "name",
        "market_segment",
        "dynamic_account_return",
        "profit_source_score",
        "buy_amount_ratio",
        "sell_amount_ratio",
        "dynamic_buy_slippage_rate",
        "dynamic_sell_slippage_rate",
        "limit_height_rank_bucket",
        "first_time_detail_bucket",
        "amount_ratio_bucket",
        "turnover_rate_bucket",
        "retreat_state_bucket",
        "market_emotion_state_bucket",
        "market_limit_down_count_bucket",
        "open_times_bucket",
    ]
    existing = [column for column in columns if column in data.columns]
    return data[data["is_risk_target"]].copy()[existing]


def build_bucket_report(data: pd.DataFrame, min_target_count: int) -> pd.DataFrame:
    rows = []
    total_risk = max(int(data["is_risk_target"].sum()), 1)
    for column in FEATURE_COLUMNS:
        if column not in data.columns:
            continue
        for bucket, group in data.groupby(column):
            if str(bucket) in {"missing", "nan", "None", "unknown"}:
                continue
            risk_count = int(group["is_risk_target"].sum())
            if risk_count < min_target_count:
                continue
            returns = group["dynamic_account_return"].astype(float)
            rows.append(
                {
                    "condition": f"{column}={bucket}",
                    "feature": column,
                    "bucket": bucket,
                    "trade_count": int(len(group)),
                    "risk_target_count": risk_count,
                    "risk_rate_in_bucket": float(risk_count / len(group)),
                    "risk_target_coverage": float(risk_count / total_risk),
                    "avg_account_return": float(returns.mean()),
                    "median_account_return": float(returns.median()),
                    "compound_multiple": float((1.0 + returns).prod()),
                    "max_loss": float(returns.min()),
                    "max_profit": float(returns.max()),
                    "avg_buy_amount_ratio": float(group.get("buy_amount_ratio", pd.Series(dtype=float)).mean()),
                    "avg_sell_amount_ratio": float(group.get("sell_amount_ratio", pd.Series(dtype=float)).mean()),
                    "avg_buy_slippage": float(
                        group.get("dynamic_buy_slippage_rate", pd.Series(dtype=float)).mean()
                    ),
                    "avg_sell_slippage": float(
                        group.get("dynamic_sell_slippage_rate", pd.Series(dtype=float)).mean()
                    ),
                }
            )
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result["risk_score"] = (
        result["risk_rate_in_bucket"]
        * result["risk_target_coverage"]
        * (1.0 + result["max_loss"].abs())
        / (1.0 + result["compound_multiple"].clip(lower=0.0))
    )
    return result.sort_values(
        ["risk_score", "risk_target_count", "risk_rate_in_bucket"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def evaluate_rule(data: pd.DataFrame, condition: str, mask: pd.Series) -> dict[str, object]:
    removed = data[mask].copy()
    remaining = data[~mask].copy()
    removed_returns = removed["dynamic_account_return"].astype(float)
    remaining_metrics = calc_metrics(remaining)
    return {
        "exclude_rule": condition,
        "removed_trade_count": int(len(removed)),
        "removed_risk_target_count": int(removed["is_risk_target"].sum()) if len(removed) else 0,
        "removed_risk_rate": float(removed["is_risk_target"].mean()) if len(removed) else 0.0,
        "removed_compound_multiple": float((1.0 + removed_returns).prod()) if len(removed) else 0.0,
        "removed_avg_account_return": float(removed_returns.mean()) if len(removed) else 0.0,
        "remaining_trade_count": remaining_metrics["trade_count"],
        "remaining_compound_multiple": remaining_metrics["compound_multiple"],
        "remaining_win_rate": remaining_metrics["win_rate"],
        "remaining_avg_account_return": remaining_metrics["avg_account_return"],
        "remaining_median_account_return": remaining_metrics["median_account_return"],
        "remaining_max_drawdown": remaining_metrics["max_drawdown"],
        "remaining_max_loss": remaining_metrics["max_loss"],
        "remaining_max_consecutive_losses": remaining_metrics["max_consecutive_losses"],
    }


def build_candidate_rule_report(
    data: pd.DataFrame,
    bucket_report: pd.DataFrame,
    max_pair_buckets: int,
) -> pd.DataFrame:
    rows = []
    for item in bucket_report.to_dict("records"):
        mask = data[item["feature"]].astype(str).eq(str(item["bucket"]))
        rows.append(evaluate_rule(data, str(item["condition"]), mask))

    pair_candidates = bucket_report.head(max_pair_buckets).to_dict("records")
    for left, right in combinations(pair_candidates, 2):
        if left["feature"] == right["feature"]:
            continue
        mask = data[left["feature"]].astype(str).eq(str(left["bucket"])) & data[
            right["feature"]
        ].astype(str).eq(str(right["bucket"]))
        if not mask.any() or int(data.loc[mask, "is_risk_target"].sum()) == 0:
            continue
        condition = f"{left['condition']}&&{right['condition']}"
        rows.append(evaluate_rule(data, condition, mask))

    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result["rule_score"] = (
        result["remaining_compound_multiple"]
        * (1.0 + result["remaining_win_rate"])
        / (1.0 + result["remaining_max_drawdown"].abs())
        * (1.0 + result["removed_risk_target_count"])
        / (1.0 + result["removed_trade_count"])
    )
    return result.sort_values(
        ["rule_score", "remaining_compound_multiple", "removed_risk_target_count"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def write_markdown(
    path: Path,
    summary: pd.DataFrame,
    warning_trades: pd.DataFrame,
    bucket_report: pd.DataFrame,
    candidate_rules: pd.DataFrame,
) -> None:
    warning_preview_columns = [
        column
        for column in [
            "review_status",
            "risk_flags",
            "trade_date",
            "ts_code",
            "name",
            "dynamic_account_return",
            "limit_height_rank_bucket",
            "first_time_detail_bucket",
            "amount_ratio_bucket",
            "retreat_state_bucket",
        ]
        if column in warning_trades.columns
    ]
    bucket_columns = [
        "condition",
        "trade_count",
        "risk_target_count",
        "risk_rate_in_bucket",
        "risk_target_coverage",
        "avg_account_return",
        "compound_multiple",
        "max_loss",
    ]
    rule_columns = [
        "exclude_rule",
        "removed_trade_count",
        "removed_risk_target_count",
        "remaining_trade_count",
        "remaining_compound_multiple",
        "remaining_win_rate",
        "remaining_max_drawdown",
        "remaining_max_loss",
    ]
    content = [
        "# 模拟盘前 WARN 来源分析",
        "",
        "本报告只基于本地审计和预审文件，不调用外部接口，不接实盘。",
        "",
        "## 汇总",
        "",
        summary.to_markdown(index=False),
        "",
        "## 风险目标交易",
        "",
        warning_trades[warning_preview_columns].to_markdown(index=False)
        if not warning_trades.empty
        else "无 WARN 或大亏交易。",
        "",
        "## 风险集中单因子 Top 20",
        "",
        bucket_report[bucket_columns].head(20).to_markdown(index=False)
        if not bucket_report.empty
        else "无满足条件的风险分桶。",
        "",
        "## 候选排除规则 Top 20",
        "",
        candidate_rules[rule_columns].head(20).to_markdown(index=False)
        if not candidate_rules.empty
        else "无候选规则。",
        "",
        "## 使用说明",
        "",
        "候选排除规则只能作为下一轮回测输入，不能直接认定为最终规则。后续必须重新跑完整策略优化和审计，比较复利、回撤、样本数、滑点、手续费和成交约束。",
    ]
    path.write_text("\n".join(content), encoding="utf-8")


def main() -> None:
    args = parse_args()
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    data = load_merged(
        review_path=PROJECT_ROOT / args.review_trades,
        audit_path=PROJECT_ROOT / args.audit_trades,
        warn_loss_threshold=args.warn_loss_threshold,
    )
    summary = build_summary(data, args.warn_loss_threshold)
    warning_trades = build_warning_trades(data)
    bucket_report = build_bucket_report(data, args.min_target_count)
    candidate_rules = build_candidate_rule_report(data, bucket_report, args.max_pair_buckets)

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    warning_path = output_prefix.with_name(output_prefix.name + "_warning_trades.csv")
    bucket_path = output_prefix.with_name(output_prefix.name + "_bucket_risk.csv")
    rules_path = output_prefix.with_name(output_prefix.name + "_candidate_rules.csv")
    markdown_path = output_prefix.with_suffix(".md")

    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    warning_trades.to_csv(warning_path, index=False, encoding="utf-8-sig")
    bucket_report.to_csv(bucket_path, index=False, encoding="utf-8-sig")
    candidate_rules.to_csv(rules_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, summary, warning_trades, bucket_report, candidate_rules)

    print("模拟盘前 WARN 来源分析完成：")
    print(f"- summary: {summary_path}")
    print(f"- warning_trades: {warning_path}")
    print(f"- bucket_risk: {bucket_path}")
    print(f"- candidate_rules: {rules_path}")
    print(f"- markdown: {markdown_path}")
    if not candidate_rules.empty:
        print("候选规则 Top 5：")
        print(
            candidate_rules[
                [
                    "exclude_rule",
                    "removed_trade_count",
                    "removed_risk_target_count",
                    "remaining_compound_multiple",
                    "remaining_win_rate",
                    "remaining_max_drawdown",
                ]
            ]
            .head(5)
            .to_string(index=False)
        )


if __name__ == "__main__":
    main()
