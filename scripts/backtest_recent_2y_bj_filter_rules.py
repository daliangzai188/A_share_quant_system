"""
回测最近 2 年方案2的 BJ 白名单 / 黑名单规则。

文件作用：
1. 固定最近 2 年 50 倍候选方案2。
2. 测试 BJ 专项过滤规则：排除 ST、排除容量压力过高、排除成交额过低。
3. 测试 BJ 专项降仓规则：BJ 买入容量上限 3% / 2%。
4. 输出每个规则后的复利、回撤、成交笔数、年度收益和被过滤交易。

本脚本只读取本地逐笔交易报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回测 BJ 白名单 / 黑名单规则。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_target50_top_scenarios_trades.csv",
        help="逐笔交易报告路径。",
    )
    parser.add_argument("--scenario-rank", type=int, default=2, help="默认分析方案2。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument(
        "--output-prefix",
        default="reports/recent_2y_bj_filter_rules",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def estimate_slippage(amount_ratio: float, tiers: list[dict[str, Any]]) -> float:
    if pd.isna(amount_ratio) or amount_ratio <= 0:
        return 0.0
    for tier in tiers:
        threshold = tier.get("max_amount_ratio")
        if threshold is None or amount_ratio <= float(threshold) + 1e-12:
            return float(tier.get("slippage_rate", 0.0))
    return 0.0


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    return float(drawdown.min())


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


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def load_rows(path: Path, scenario_rank: int) -> pd.DataFrame:
    data = pd.read_csv(path, low_memory=False)
    data = data[data["scenario_rank"] == scenario_rank].copy()
    if data.empty:
        raise RuntimeError(f"没有找到 scenario_rank={scenario_rank}: {path}")
    data["selected_order"] = pd.to_numeric(data["selected_order"], errors="coerce")
    data["trade_date"] = data["trade_date"].map(normalize_date)
    data["buy_trade_date"] = data["buy_trade_date"].map(normalize_date)
    data["exit_trade_date"] = data["exit_trade_date"].map(normalize_date)
    data["market_segment"] = data["market_segment"].fillna("unknown").astype(str)
    data["name"] = data["name"].fillna("").astype(str)
    data["target_buy_amount_ratio"] = pd.to_numeric(data["target_buy_amount"], errors="coerce") / pd.to_numeric(
        data["buy_day_amount_yuan"], errors="coerce"
    )
    return data.sort_values("selected_order").reset_index(drop=True)


def is_st_name(name: str) -> bool:
    upper = name.upper()
    return "ST" in upper or "*ST" in upper


def should_skip_by_rule(row: pd.Series, rule: dict[str, Any]) -> str:
    if not bool(row.get("scenario_executed", False)):
        return str(row.get("skip_reason", "original_not_executed"))
    if str(row.get("market_segment", "")) != "bj":
        return ""

    name = str(row.get("name", ""))
    if bool(rule.get("exclude_bj_st", False)) and is_st_name(name):
        return "exclude_bj_st"

    max_target_ratio = rule.get("max_bj_target_buy_ratio")
    if max_target_ratio is not None and float(row.get("target_buy_amount_ratio", 0.0)) > float(max_target_ratio):
        return f"exclude_bj_target_ratio_gt_{max_target_ratio}"

    min_amount = rule.get("min_bj_buy_day_amount_yuan")
    if min_amount is not None and float(row.get("buy_day_amount_yuan", 0.0)) < float(min_amount):
        return f"exclude_bj_amount_lt_{int(float(min_amount))}"

    return ""


def simulate_rule(
    rows: pd.DataFrame,
    rule: dict[str, Any],
    initial_cash: float,
    position_pct: float,
    default_capacity: float,
    slippage_tiers: list[dict[str, Any]],
    fee_rate_without_slippage: float,
) -> tuple[dict[str, Any], pd.DataFrame]:
    equity = initial_cash
    details: list[dict[str, Any]] = []
    trade_count = 0

    for _, row in rows.iterrows():
        result = row.to_dict()
        result["rule_name"] = rule["rule_name"]
        result["rule_equity_before"] = equity

        skip_reason = should_skip_by_rule(row, rule)
        if skip_reason:
            result.update(
                {
                    "rule_executed": False,
                    "rule_skip_reason": skip_reason,
                    "rule_account_return": 0.0,
                    "rule_equity_after": equity,
                }
            )
            details.append(result)
            continue

        buy_amount_day = float(row.get("buy_day_amount_yuan", 0.0))
        sell_amount_day = float(row.get("sell_day_amount_yuan", 0.0))
        buy_price_raw = float(row.get("buy_price_before_slippage", 0.0))
        sell_price_raw = float(row.get("exit_price_before_slippage", 0.0))
        if buy_amount_day <= 0 or sell_amount_day <= 0 or buy_price_raw <= 0 or sell_price_raw <= 0:
            result.update(
                {
                    "rule_executed": False,
                    "rule_skip_reason": "missing_liquidity_or_price",
                    "rule_account_return": 0.0,
                    "rule_equity_after": equity,
                }
            )
            details.append(result)
            continue

        market_segment = str(row.get("market_segment", ""))
        bj_capacity = rule.get("bj_capacity")
        capacity = float(bj_capacity) if market_segment == "bj" and bj_capacity is not None else default_capacity
        target_buy_amount = equity * position_pct
        actual_buy_amount = min(target_buy_amount, buy_amount_day * capacity)
        actual_position_pct = actual_buy_amount / equity if equity > 0 else 0.0
        buy_amount_ratio = actual_buy_amount / buy_amount_day if buy_amount_day > 0 else 0.0
        buy_slippage = estimate_slippage(buy_amount_ratio, slippage_tiers)
        buy_price = buy_price_raw * (1.0 + buy_slippage)

        sell_value_before_slippage = actual_buy_amount * sell_price_raw / buy_price
        sell_amount_ratio = sell_value_before_slippage / sell_amount_day if sell_amount_day > 0 else 0.0
        sell_slippage = estimate_slippage(sell_amount_ratio, slippage_tiers)
        sell_price = sell_price_raw * (1.0 - sell_slippage)

        net_return = sell_price / buy_price - 1.0 - fee_rate_without_slippage
        account_return = net_return * actual_position_pct
        equity_after = equity * (1.0 + account_return)
        trade_count += 1

        result.update(
            {
                "rule_executed": True,
                "rule_trade_order": trade_count,
                "rule_skip_reason": "",
                "rule_capacity": capacity,
                "rule_target_buy_amount": target_buy_amount,
                "rule_actual_buy_amount": actual_buy_amount,
                "rule_actual_position_pct": actual_position_pct,
                "rule_buy_amount_ratio": buy_amount_ratio,
                "rule_sell_amount_ratio": sell_amount_ratio,
                "rule_buy_slippage": buy_slippage,
                "rule_sell_slippage": sell_slippage,
                "rule_net_return": net_return,
                "rule_account_return": account_return,
                "rule_equity_after": equity_after,
            }
        )
        equity = equity_after
        details.append(result)

    detail = pd.DataFrame(details)
    executed = detail[detail["rule_executed"] == True].copy()  # noqa: E712
    returns = executed["rule_account_return"].astype(float) if not executed.empty else pd.Series(dtype=float)
    skipped = detail[detail["rule_executed"] != True].copy()  # noqa: E712
    summary = {
        "rule_name": rule["rule_name"],
        "description": rule.get("description", ""),
        "final_equity": equity,
        "equity_multiple": equity / initial_cash if initial_cash else 0.0,
        "executed_trade_count": int(len(executed)),
        "skipped_count": int(len(skipped)),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "max_drawdown": max_drawdown(executed["rule_equity_after"]) if len(executed) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(returns),
        "avg_actual_position_pct": float(executed["rule_actual_position_pct"].mean()) if len(executed) else 0.0,
        "avg_buy_slippage": float(executed["rule_buy_slippage"].mean()) if len(executed) else 0.0,
        "avg_sell_slippage": float(executed["rule_sell_slippage"].mean()) if len(executed) else 0.0,
        "bj_executed_count": int((executed["market_segment"] == "bj").sum()) if len(executed) else 0,
        "bj_skipped_by_rule_count": int(
            skipped["rule_skip_reason"].astype(str).str.startswith("exclude_bj").sum()
        )
        if len(skipped)
        else 0,
    }
    return summary, detail


def build_yearly(detail: pd.DataFrame) -> pd.DataFrame:
    executed = detail[detail["rule_executed"] == True].copy()  # noqa: E712
    if executed.empty:
        return pd.DataFrame()
    executed["year"] = executed["exit_trade_date"].map(normalize_date).str[:4]
    rows = []
    for (rule_name, year), group in executed.groupby(["rule_name", "year"]):
        first_equity = float(group["rule_equity_before"].iloc[0])
        last_equity = float(group["rule_equity_after"].iloc[-1])
        returns = group["rule_account_return"].astype(float)
        rows.append(
            {
                "rule_name": rule_name,
                "year": year,
                "trade_count": int(len(group)),
                "year_return": last_equity / first_equity - 1 if first_equity else 0.0,
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "max_drawdown": max_drawdown(group["rule_equity_after"]),
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def build_skip_summary(detail: pd.DataFrame) -> pd.DataFrame:
    skipped = detail[detail["rule_executed"] != True].copy()  # noqa: E712
    if skipped.empty:
        return pd.DataFrame()
    result = (
        skipped.groupby(["rule_name", "rule_skip_reason"])
        .size()
        .reset_index(name="count")
        .sort_values(["rule_name", "count"], ascending=[True, False])
    )
    return result


def main() -> None:
    args = parse_args()
    config = load_config(PROJECT_ROOT / args.config)
    risk = config.get("risk", {})
    opt = config.get("realistic_condition_strategy_search", {})
    initial_cash = float(opt.get("initial_cash", 500000))
    position_pct = float(opt.get("position_pct", 0.8))
    default_capacity = float(opt.get("max_buy_amount_ratio", 0.05))
    slippage_tiers = list(opt.get("slippage_tiers", []))
    fee_rate_without_slippage = (
        float(risk.get("commission_rate", 0.0003))
        + float(risk.get("transfer_fee_rate", 0.00001))
        + float(risk.get("commission_rate", 0.0003))
        + float(risk.get("transfer_fee_rate", 0.00001))
        + float(risk.get("stamp_tax_rate", 0.001))
    )

    rows = load_rows(PROJECT_ROOT / args.input, args.scenario_rank)
    rules = [
        {"rule_name": "baseline", "description": "原始方案2：BJ容量5%，不额外过滤"},
        {"rule_name": "bj_cap_3pct", "description": "BJ容量降到3%", "bj_capacity": 0.03},
        {"rule_name": "bj_cap_2pct", "description": "BJ容量降到2%", "bj_capacity": 0.02},
        {"rule_name": "exclude_bj_st", "description": "排除 BJ ST/*ST", "exclude_bj_st": True},
        {
            "rule_name": "exclude_bj_target_gt_3pct",
            "description": "排除目标买入占BJ当日成交额超过3%的交易",
            "max_bj_target_buy_ratio": 0.03,
        },
        {
            "rule_name": "exclude_bj_target_gt_2pct",
            "description": "排除目标买入占BJ当日成交额超过2%的交易",
            "max_bj_target_buy_ratio": 0.02,
        },
        {
            "rule_name": "exclude_bj_amount_lt_3e8",
            "description": "排除BJ当日成交额低于3亿的交易",
            "min_bj_buy_day_amount_yuan": 300000000,
        },
        {
            "rule_name": "exclude_bj_amount_lt_5e8",
            "description": "排除BJ当日成交额低于5亿的交易",
            "min_bj_buy_day_amount_yuan": 500000000,
        },
        {
            "rule_name": "bj_cap_3pct_exclude_st_target_gt_3pct",
            "description": "BJ容量3%，排除BJ ST/*ST，排除目标占比超过3%",
            "bj_capacity": 0.03,
            "exclude_bj_st": True,
            "max_bj_target_buy_ratio": 0.03,
        },
        {
            "rule_name": "bj_cap_2pct_exclude_st_target_gt_2pct",
            "description": "BJ容量2%，排除BJ ST/*ST，排除目标占比超过2%",
            "bj_capacity": 0.02,
            "exclude_bj_st": True,
            "max_bj_target_buy_ratio": 0.02,
        },
    ]

    summaries = []
    detail_frames = []
    for rule in rules:
        summary, detail = simulate_rule(
            rows=rows,
            rule=rule,
            initial_cash=initial_cash,
            position_pct=position_pct,
            default_capacity=default_capacity,
            slippage_tiers=slippage_tiers,
            fee_rate_without_slippage=fee_rate_without_slippage,
        )
        summaries.append(summary)
        detail_frames.append(detail)

    summary_report = pd.DataFrame(summaries).sort_values("equity_multiple", ascending=False)
    detail_report = pd.concat(detail_frames, ignore_index=True)
    yearly_report = build_yearly(detail_report)
    skip_report = build_skip_summary(detail_report)

    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    skip_path = output_prefix.with_name(output_prefix.name + "_skips.csv")
    summary_report.to_csv(summary_path, index=False, encoding="utf-8-sig")
    yearly_report.to_csv(yearly_path, index=False, encoding="utf-8-sig")
    detail_report.to_csv(detail_path, index=False, encoding="utf-8-sig")
    skip_report.to_csv(skip_path, index=False, encoding="utf-8-sig")

    print("BJ 白名单/黑名单规则回测完成")
    print(
        summary_report[
            [
                "rule_name",
                "equity_multiple",
                "executed_trade_count",
                "bj_executed_count",
                "bj_skipped_by_rule_count",
                "win_rate",
                "max_drawdown",
                "avg_actual_position_pct",
            ]
        ].to_string(index=False)
    )
    print("报告文件：")
    print(f"- summary: {summary_path}")
    print(f"- yearly: {yearly_path}")
    print(f"- detail: {detail_path}")
    print(f"- skips: {skip_path}")


if __name__ == "__main__":
    main()
