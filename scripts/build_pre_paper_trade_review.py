"""
生成模拟盘前逐笔交易复盘清单。

文件作用：
1. 读取策略配置 config/strategy_config.json。
2. 读取最佳策略审计 trades.csv。
3. 逐笔输出买入原因、卖出原因、成交价格、动态滑点、成交额占比、涨停买入/跌停卖出约束和风险标记。
4. 生成 CSV 和 Markdown，供进入模拟盘前人工复盘。

本脚本只读取本地报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成模拟盘前逐笔交易复盘清单。")
    parser.add_argument("--config", default="config/strategy_config.json", help="策略配置文件。")
    parser.add_argument(
        "--trades",
        default="reports/a_clean_profit_source_exclude_star_best_audit_trades.csv",
        help="策略审计逐笔交易文件。",
    )
    parser.add_argument(
        "--output-prefix",
        default="reports/a_clean_exclude_star_pre_paper_review",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


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


def condition_hit(row: pd.Series, condition: dict[str, Any]) -> bool:
    column = str(condition["column"])
    expected = str(condition["value"])
    if column not in row.index:
        return False
    return str(row.get(column, "missing")) == expected


def rule_hit(row: pd.Series, rule: dict[str, Any]) -> bool:
    conditions = rule.get("conditions", [])
    if not conditions:
        return False
    return all(condition_hit(row, condition) for condition in conditions)


def score_rule_hit(row: pd.Series, rule: dict[str, Any]) -> bool:
    column = str(rule["column"])
    values = {str(value) for value in rule.get("values", [])}
    if column not in row.index:
        return False
    return str(row.get(column, "missing")) in values


def build_reason_text(row: pd.Series, config: dict[str, Any]) -> tuple[str, str, str]:
    include_hits = []
    for condition in config.get("candidate_filters", {}).get("conditions", []):
        status = "命中" if condition_hit(row, condition) else "未命中"
        include_hits.append(f"{condition['column']}={condition['value']}({status})")

    exclude_hits = []
    for condition in config.get("candidate_filters", {}).get("exclude_conditions", []):
        status = "触发" if condition_hit(row, condition) else "未触发"
        exclude_hits.append(f"{condition['column']}={condition['value']}({status})")
    for rule in config.get("candidate_filters", {}).get("exclude_rules", []):
        status = "触发" if rule_hit(row, rule) else "未触发"
        condition_text = "&&".join(
            f"{condition['column']}={condition['value']}" for condition in rule.get("conditions", [])
        )
        exclude_hits.append(f"{condition_text}({status})")

    score_hits = []
    for rule in config.get("ranking", {}).get("score_rules", []):
        if score_rule_hit(row, rule):
            values = "|".join(str(value) for value in rule.get("values", []))
            score_hits.append(f"{rule['column']} in {values}: {rule['weight']:+g}")

    return "; ".join(include_hits), "; ".join(exclude_hits), "; ".join(score_hits)


def build_risk_flags(row: pd.Series, config: dict[str, Any]) -> list[str]:
    flags = []
    market_segment = str(row.get("market_segment", ""))
    excluded_segments = set(config.get("universe", {}).get("exclude_market_segments", []))
    if market_segment in excluded_segments:
        flags.append(f"违反排除市场:{market_segment}")
    if normalize_bool(row.get("is_st", False)) or "ST" in str(row.get("name", "")).upper():
        flags.append("ST风险")
    if not normalize_bool(row.get("buy_executed", False)):
        flags.append(f"买入未成交:{row.get('buy_reject_reason', '')}")
    if not normalize_bool(row.get("sell_executed", False)):
        flags.append(f"卖出未成交:{row.get('sell_reject_reason', '')}")
    if normalize_number(row.get("limit_down_blocked_days", 0)) > 0:
        flags.append(f"跌停延迟卖出:{int(normalize_number(row.get('limit_down_blocked_days', 0)))}天")
    if normalize_number(row.get("buy_amount_ratio", 0)) > 0.03:
        flags.append("买入成交额占比偏高")
    if normalize_number(row.get("sell_amount_ratio", 0)) > 0.03:
        flags.append("卖出成交额占比偏高")
    if normalize_number(row.get("dynamic_buy_slippage_rate", 0)) > 0.005:
        flags.append("买入滑点偏高")
    if normalize_number(row.get("dynamic_sell_slippage_rate", 0)) > 0.005:
        flags.append("卖出滑点偏高")
    if normalize_number(row.get("dynamic_account_return", 0)) <= -0.08:
        flags.append("单笔亏损超过8%账户收益")
    return flags


def build_review_status(flags: list[str]) -> str:
    blocking_keywords = ["违反排除市场", "ST风险", "买入未成交", "卖出未成交", "跌停延迟卖出"]
    if any(any(keyword in flag for keyword in blocking_keywords) for flag in flags):
        return "FAIL"
    warning_keywords = ["偏高", "亏损超过"]
    if any(any(keyword in flag for keyword in warning_keywords) for flag in flags):
        return "WARN"
    return "PASS"


def load_trades(path: Path) -> pd.DataFrame:
    trades = pd.read_csv(path, low_memory=False)
    if "scenario_executed" in trades.columns:
        trades = trades[trades["scenario_executed"].astype(str).str.lower().isin({"true", "1"})].copy()
    return trades.sort_values(["trade_order", "trade_date", "ts_code"]).reset_index(drop=True)


def build_review(config: dict[str, Any], trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in trades.iterrows():
        include_reason, exclude_reason, score_reason = build_reason_text(row, config)
        flags = build_risk_flags(row, config)
        rows.append(
            {
                "review_status": build_review_status(flags),
                "risk_flags": "; ".join(flags) if flags else "无",
                "trade_order": int(normalize_number(row.get("trade_order", 0))),
                "signal_trade_date": str(row.get("trade_date", "")),
                "ts_code": str(row.get("ts_code", "")),
                "name": str(row.get("name", "")),
                "market_segment": str(row.get("market_segment", "")),
                "buy_trade_date": str(row.get("buy_trade_date", "")),
                "buy_price_before_slippage": normalize_number(row.get("buy_price_before_slippage", 0)),
                "dynamic_buy_price": normalize_number(row.get("dynamic_buy_price", row.get("buy_price", 0))),
                "buy_price_model": str(row.get("buy_price_model", "")),
                "buy_executed": normalize_bool(row.get("buy_executed", False)),
                "buy_reject_reason": str(row.get("buy_reject_reason", "")),
                "exit_trade_date": str(row.get("exit_trade_date", "")),
                "exit_price_before_slippage": normalize_number(row.get("exit_price_before_slippage", 0)),
                "dynamic_sell_price": normalize_number(row.get("dynamic_sell_price", row.get("exit_price", 0))),
                "sell_price_model": str(row.get("sell_price_model", "")),
                "sell_executed": normalize_bool(row.get("sell_executed", False)),
                "sell_reject_reason": str(row.get("sell_reject_reason", "")),
                "exit_reason": str(row.get("exit_reason", "")),
                "dynamic_account_return": normalize_number(row.get("dynamic_account_return", 0)),
                "equity_before": normalize_number(row.get("equity_before", 0)),
                "equity_after": normalize_number(row.get("equity_after", 0)),
                "actual_position_pct": normalize_number(row.get("actual_position_pct", 0)),
                "buy_amount_ratio": normalize_number(row.get("buy_amount_ratio", 0)),
                "sell_amount_ratio": normalize_number(row.get("sell_amount_ratio", 0)),
                "dynamic_buy_slippage_rate": normalize_number(row.get("dynamic_buy_slippage_rate", 0)),
                "dynamic_sell_slippage_rate": normalize_number(row.get("dynamic_sell_slippage_rate", 0)),
                "limit_down_blocked_days": int(normalize_number(row.get("limit_down_blocked_days", 0))),
                "profit_source_score": normalize_number(row.get("profit_source_score", 0)),
                "include_condition_review": include_reason,
                "exclude_condition_review": exclude_reason,
                "score_reason": score_reason if score_reason else "无加分/扣分项",
                "market_emotion_state_bucket": str(row.get("market_emotion_state_bucket", "")),
                "market_limit_down_count_bucket": str(row.get("market_limit_down_count_bucket", "")),
                "retreat_state_bucket": str(row.get("retreat_state_bucket", "")),
                "first_time_detail_bucket": str(row.get("first_time_detail_bucket", "")),
                "turnover_rate_bucket": str(row.get("turnover_rate_bucket", "")),
                "amount_ratio_bucket": str(row.get("amount_ratio_bucket", "")),
                "fd_ratio_bucket": str(row.get("fd_ratio_bucket", "")),
                "segment_limit_up_count_bucket": str(row.get("segment_limit_up_count_bucket", "")),
                "market_chain_count_bucket": str(row.get("market_chain_count_bucket", "")),
            }
        )
    return pd.DataFrame(rows)


def build_summary(review: pd.DataFrame) -> pd.DataFrame:
    status_counts = review["review_status"].value_counts().to_dict()
    returns = review["dynamic_account_return"].astype(float)
    return pd.DataFrame(
        [
            {
                "trade_count": int(len(review)),
                "pass_count": int(status_counts.get("PASS", 0)),
                "warn_count": int(status_counts.get("WARN", 0)),
                "fail_count": int(status_counts.get("FAIL", 0)),
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
                "median_account_return": float(returns.median()) if len(returns) else 0.0,
                "compound_multiple": float((1.0 + returns).prod()) if len(returns) else 0.0,
                "max_profit": float(returns.max()) if len(returns) else 0.0,
                "max_loss": float(returns.min()) if len(returns) else 0.0,
                "avg_buy_amount_ratio": float(review["buy_amount_ratio"].mean()) if len(review) else 0.0,
                "max_buy_amount_ratio": float(review["buy_amount_ratio"].max()) if len(review) else 0.0,
                "avg_sell_amount_ratio": float(review["sell_amount_ratio"].mean()) if len(review) else 0.0,
                "max_sell_amount_ratio": float(review["sell_amount_ratio"].max()) if len(review) else 0.0,
                "avg_buy_slippage": float(review["dynamic_buy_slippage_rate"].mean()) if len(review) else 0.0,
                "avg_sell_slippage": float(review["dynamic_sell_slippage_rate"].mean()) if len(review) else 0.0,
            }
        ]
    )


def write_markdown(path: Path, config: dict[str, Any], summary: pd.DataFrame, review: pd.DataFrame) -> None:
    preview_columns = [
        "review_status",
        "risk_flags",
        "signal_trade_date",
        "ts_code",
        "name",
        "market_segment",
        "dynamic_account_return",
        "buy_amount_ratio",
        "sell_amount_ratio",
        "profit_source_score",
    ]
    warning_rows = review[review["review_status"] != "PASS"].copy()
    content = f"""# 模拟盘前逐笔交易复盘清单

策略：`{config.get('strategy_name', '')}`

本报告只读取本地审计结果，不调用外部接口，不接实盘。

## 汇总

{summary.to_markdown(index=False)}

## 非 PASS 交易

{warning_rows[preview_columns].to_markdown(index=False) if not warning_rows.empty else "无"}

## 前 20 笔预览

{review[preview_columns].head(20).to_markdown(index=False)}

## 使用说明

进入模拟盘前，必须人工检查所有 WARN/FAIL 交易。特别关注成交额占比、动态滑点、是否跌停延迟卖出、是否违反排除市场、是否 ST、是否买卖未成交。
"""
    path.write_text(content, encoding="utf-8")


def main() -> None:
    args = parse_args()
    config = load_config(PROJECT_ROOT / args.config)
    trades = load_trades(PROJECT_ROOT / args.trades)
    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    review = build_review(config, trades)
    summary = build_summary(review)

    review_path = output_prefix.with_name(output_prefix.name + "_trades.csv")
    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    markdown_path = output_prefix.with_suffix(".md")

    review.to_csv(review_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    write_markdown(markdown_path, config, summary, review)

    print("模拟盘前逐笔交易复盘清单已生成：")
    print(f"- summary: {summary_path}")
    print(f"- trades: {review_path}")
    print(f"- markdown: {markdown_path}")


if __name__ == "__main__":
    main()
