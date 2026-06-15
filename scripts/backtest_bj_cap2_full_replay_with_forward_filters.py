"""
完整回放 bj_cap_2pct 与前视风险过滤规则。

文件作用：
1. 读取最近 2 年方案2的全部候选信号，而不是只读取已成交交易。
2. 按单持仓规则重新模拟：释放被过滤交易占用的持仓后，后续候选可以重新参与。
3. 固定 bj_cap_2pct 执行口径：普通股票买入容量 5%，北交所买入容量 2%。
4. 叠加前视有效过滤条件，验证过滤规则是否真实改善复利、回撤和 2026 样本外表现。
5. 输出汇总、年度、逐笔明细和跳过原因统计。

本脚本只读取本地报告，不调用外部接口，不接实盘。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="完整回放 bj_cap_2pct 前视过滤规则。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_target50_top_scenarios_trades.csv",
        help="最近2年方案逐信号交易明细。",
    )
    parser.add_argument("--scenario-rank", type=int, default=2, help="默认回放方案2。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument(
        "--output-prefix",
        default="reports/bj_cap2_full_replay_forward_filters",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


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


def load_rows(path: Path, scenario_rank: int) -> pd.DataFrame:
    rows = pd.read_csv(path, low_memory=False)
    rows = rows[rows["scenario_rank"] == scenario_rank].copy()
    if rows.empty:
        raise RuntimeError(f"没有找到 scenario_rank={scenario_rank}: {path}")
    rows["selected_order"] = pd.to_numeric(rows["selected_order"], errors="coerce")
    rows["trade_date"] = rows["trade_date"].map(normalize_date)
    rows["buy_trade_date"] = rows["buy_trade_date"].map(normalize_date)
    rows["exit_trade_date"] = rows["exit_trade_date"].map(normalize_date)
    rows["market_segment"] = rows["market_segment"].fillna("unknown").astype(str)
    return rows.sort_values("selected_order").reset_index(drop=True)


def filter_rules() -> list[dict[str, Any]]:
    return [
        {
            "rule_name": "bj_cap_2pct_full_replay",
            "description": "完整回放基准：BJ容量2%，不额外过滤",
            "conditions": tuple(),
        },
        {
            "rule_name": "filter_amount_3e8_8e8_market_chain_3_8",
            "description": "前视过滤：排除 amount_bucket=3e8_8e8 且 market_chain_count_bucket=3_8",
            "conditions": (("amount_bucket", "3e8_8e8"), ("market_chain_count_bucket", "3_8")),
        },
        {
            "rule_name": "filter_fd_1pct_2pct_market_down_lt5",
            "description": "前视过滤：排除 fd_ratio_bucket=1pct_2pct 且 market_limit_down_count_bucket=lt_5",
            "conditions": (("fd_ratio_bucket", "1pct_2pct"), ("market_limit_down_count_bucket", "lt_5")),
        },
        {
            "rule_name": "filter_market_chain_3_8_segment_retreat_weak",
            "description": "前视过滤：排除 market_chain_count_bucket=3_8 且 segment_retreat_state_bucket=weak_below_3",
            "conditions": (("market_chain_count_bucket", "3_8"), ("segment_retreat_state_bucket", "weak_below_3")),
        },
        {
            "rule_name": "filter_market_down_lt5_pct_19_5_20_5",
            "description": "前视过滤：排除 market_limit_down_count_bucket=lt_5 且 pct_chg_bucket=19_5_20_5",
            "conditions": (("market_limit_down_count_bucket", "lt_5"), ("pct_chg_bucket", "19_5_20_5")),
        },
    ]


def matches_conditions(row: pd.Series, conditions: tuple[tuple[str, str], ...]) -> bool:
    for column, expected in conditions:
        if str(row.get(column, "missing")) != expected:
            return False
    return bool(conditions)


def build_skipped_result(row: pd.Series, rule: dict[str, Any], equity: float, skip_reason: str) -> dict[str, Any]:
    result = row.to_dict()
    result.update(
        {
            "replay_rule_name": rule["rule_name"],
            "replay_rule_description": rule["description"],
            "replay_executed": False,
            "replay_skip_reason": skip_reason,
            "replay_trade_order": pd.NA,
            "replay_equity_before": equity,
            "replay_target_buy_amount": 0.0,
            "replay_actual_buy_amount": 0.0,
            "replay_actual_position_pct": 0.0,
            "replay_buy_amount_ratio": 0.0,
            "replay_sell_amount_ratio": 0.0,
            "replay_buy_slippage": 0.0,
            "replay_sell_slippage": 0.0,
            "replay_buy_price": pd.NA,
            "replay_sell_price": pd.NA,
            "replay_net_return": 0.0,
            "replay_account_return": 0.0,
            "replay_equity_after": equity,
        }
    )
    return result


def build_trade_result(
    row: pd.Series,
    rule: dict[str, Any],
    equity: float,
    trade_order: int,
    position_pct: float,
    default_capacity: float,
    bj_capacity: float,
    slippage_tiers: list[dict[str, Any]],
    fee_rate_without_slippage: float,
) -> dict[str, Any]:
    buy_day_amount = float(row.get("buy_day_amount_yuan", 0.0)) if pd.notna(row.get("buy_day_amount_yuan")) else 0.0
    sell_day_amount = float(row.get("sell_day_amount_yuan", 0.0)) if pd.notna(row.get("sell_day_amount_yuan")) else 0.0
    buy_price_raw = (
        float(row.get("buy_price_before_slippage", 0.0))
        if pd.notna(row.get("buy_price_before_slippage"))
        else 0.0
    )
    sell_price_raw = (
        float(row.get("exit_price_before_slippage", 0.0))
        if pd.notna(row.get("exit_price_before_slippage"))
        else 0.0
    )
    if buy_day_amount <= 0 or sell_day_amount <= 0 or buy_price_raw <= 0 or sell_price_raw <= 0:
        return build_skipped_result(row, rule, equity, "missing_liquidity_or_price")

    capacity = bj_capacity if str(row.get("market_segment", "")) == "bj" else default_capacity
    target_buy_amount = equity * position_pct
    actual_buy_amount = min(target_buy_amount, buy_day_amount * capacity)
    actual_position_pct = actual_buy_amount / equity if equity > 0 else 0.0
    buy_amount_ratio = actual_buy_amount / buy_day_amount if buy_day_amount > 0 else 0.0
    buy_slippage = estimate_slippage(buy_amount_ratio, slippage_tiers)
    buy_price = buy_price_raw * (1.0 + buy_slippage)

    sell_value_before_slippage = actual_buy_amount * sell_price_raw / buy_price
    sell_amount_ratio = sell_value_before_slippage / sell_day_amount if sell_day_amount > 0 else 0.0
    sell_slippage = estimate_slippage(sell_amount_ratio, slippage_tiers)
    sell_price = sell_price_raw * (1.0 - sell_slippage)

    net_return = sell_price / buy_price - 1.0 - fee_rate_without_slippage
    account_return = net_return * actual_position_pct
    equity_after = equity * (1.0 + account_return)

    result = row.to_dict()
    result.update(
        {
            "replay_rule_name": rule["rule_name"],
            "replay_rule_description": rule["description"],
            "replay_executed": True,
            "replay_skip_reason": "",
            "replay_trade_order": trade_order,
            "replay_capacity": capacity,
            "replay_equity_before": equity,
            "replay_target_buy_amount": target_buy_amount,
            "replay_actual_buy_amount": actual_buy_amount,
            "replay_actual_position_pct": actual_position_pct,
            "replay_buy_amount_ratio": buy_amount_ratio,
            "replay_sell_amount_ratio": sell_amount_ratio,
            "replay_buy_slippage": buy_slippage,
            "replay_sell_slippage": sell_slippage,
            "replay_buy_price": buy_price,
            "replay_sell_price": sell_price,
            "replay_net_return": net_return,
            "replay_account_return": account_return,
            "replay_equity_after": equity_after,
        }
    )
    return result


def replay_rule(
    rows: pd.DataFrame,
    rule: dict[str, Any],
    initial_cash: float,
    position_pct: float,
    default_capacity: float,
    bj_capacity: float,
    slippage_tiers: list[dict[str, Any]],
    fee_rate_without_slippage: float,
) -> pd.DataFrame:
    equity = initial_cash
    occupied_until = ""
    trade_order = 0
    details: list[dict[str, Any]] = []

    for _, row in rows.iterrows():
        buy_trade_date = normalize_date(row.get("buy_trade_date", ""))
        if occupied_until and buy_trade_date <= occupied_until:
            details.append(build_skipped_result(row, rule, equity, "position_occupied"))
            continue
        if matches_conditions(row, rule["conditions"]):
            details.append(build_skipped_result(row, rule, equity, "forward_risk_filter"))
            continue
        if not bool(row.get("buy_executed", False)):
            reason = str(row.get("buy_reject_reason", "buy_not_executed"))
            details.append(build_skipped_result(row, rule, equity, reason))
            continue
        if not bool(row.get("sell_executed", False)) or pd.isna(row.get("exit_price_before_slippage")):
            reason = str(row.get("sell_reject_reason", "sell_not_executed"))
            details.append(build_skipped_result(row, rule, equity, reason))
            continue

        trade_order += 1
        result = build_trade_result(
            row=row,
            rule=rule,
            equity=equity,
            trade_order=trade_order,
            position_pct=position_pct,
            default_capacity=default_capacity,
            bj_capacity=bj_capacity,
            slippage_tiers=slippage_tiers,
            fee_rate_without_slippage=fee_rate_without_slippage,
        )
        if bool(result["replay_executed"]):
            equity = float(result["replay_equity_after"])
            occupied_until = normalize_date(row.get("exit_trade_date", ""))
        details.append(result)

    return pd.DataFrame(details)


def summarize_detail(detail: pd.DataFrame, initial_cash: float) -> dict[str, Any]:
    executed = detail[detail["replay_executed"] == True].copy()  # noqa: E712
    returns = executed["replay_account_return"].astype(float) if not executed.empty else pd.Series(dtype=float)
    final_equity = float(executed["replay_equity_after"].iloc[-1]) if not executed.empty else initial_cash
    equity_curve = executed["replay_equity_after"].astype(float) if not executed.empty else pd.Series(dtype=float)
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    return {
        "rule_name": str(detail["replay_rule_name"].iloc[0]) if not detail.empty else "",
        "description": str(detail["replay_rule_description"].iloc[0]) if not detail.empty else "",
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "equity_multiple": final_equity / initial_cash if initial_cash else 0.0,
        "selected_signal_count": int(len(detail)),
        "executed_trade_count": int(len(executed)),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_drawdown": max_drawdown(equity_curve),
        "max_consecutive_losses": max_consecutive_losses(returns),
        "avg_actual_position_pct": float(executed["replay_actual_position_pct"].mean()) if len(executed) else 0.0,
        "avg_buy_slippage": float(executed["replay_buy_slippage"].mean()) if len(executed) else 0.0,
        "avg_sell_slippage": float(executed["replay_sell_slippage"].mean()) if len(executed) else 0.0,
        "bj_trade_count": int((executed["market_segment"] == "bj").sum()) if len(executed) else 0,
        "filter_skip_count": int((detail["replay_skip_reason"].astype(str) == "forward_risk_filter").sum()),
        "position_occupied_skip_count": int((detail["replay_skip_reason"].astype(str) == "position_occupied").sum()),
        "buy_rejected_count": int((detail["replay_skip_reason"].astype(str) == "open_limit_up_unbuyable").sum()),
        "sell_unresolved_count": int(
            detail["replay_skip_reason"].astype(str).str.contains("limit_down|sell", regex=True).sum()
        ),
    }


def build_yearly(detail: pd.DataFrame) -> pd.DataFrame:
    executed = detail[detail["replay_executed"] == True].copy()  # noqa: E712
    if executed.empty:
        return pd.DataFrame()
    executed["year"] = executed["exit_trade_date"].map(normalize_date).str[:4]
    rows = []
    for (rule_name, year), group in executed.groupby(["replay_rule_name", "year"]):
        first_equity = float(group["replay_equity_before"].iloc[0])
        last_equity = float(group["replay_equity_after"].iloc[-1])
        returns = group["replay_account_return"].astype(float)
        rows.append(
            {
                "rule_name": rule_name,
                "year": year,
                "trade_count": int(len(group)),
                "year_return": last_equity / first_equity - 1 if first_equity else 0.0,
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "max_drawdown": max_drawdown(group["replay_equity_after"].astype(float)),
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def build_skip_summary(detail: pd.DataFrame) -> pd.DataFrame:
    skipped = detail[detail["replay_executed"] != True].copy()  # noqa: E712
    if skipped.empty:
        return pd.DataFrame()
    return (
        skipped.groupby(["replay_rule_name", "replay_skip_reason"])
        .size()
        .reset_index(name="count")
        .sort_values(["replay_rule_name", "count"], ascending=[True, False])
    )


def main() -> None:
    args = parse_args()
    config = load_config(PROJECT_ROOT / args.config)
    risk = config.get("risk", {})
    opt = config.get("realistic_condition_strategy_search", {})
    initial_cash = float(opt.get("initial_cash", 500000))
    position_pct = float(opt.get("position_pct", 0.8))
    default_capacity = float(opt.get("max_buy_amount_ratio", 0.05))
    bj_capacity = 0.02
    slippage_tiers = list(opt.get("slippage_tiers", []))
    fee_rate_without_slippage = (
        float(risk.get("commission_rate", 0.0003))
        + float(risk.get("transfer_fee_rate", 0.00001))
        + float(risk.get("commission_rate", 0.0003))
        + float(risk.get("transfer_fee_rate", 0.00001))
        + float(risk.get("stamp_tax_rate", 0.001))
    )

    rows = load_rows(PROJECT_ROOT / args.input, args.scenario_rank)
    detail_frames = [
        replay_rule(
            rows=rows,
            rule=rule,
            initial_cash=initial_cash,
            position_pct=position_pct,
            default_capacity=default_capacity,
            bj_capacity=bj_capacity,
            slippage_tiers=slippage_tiers,
            fee_rate_without_slippage=fee_rate_without_slippage,
        )
        for rule in filter_rules()
    ]
    detail = pd.concat(detail_frames, ignore_index=True)
    summary = pd.DataFrame([summarize_detail(frame, initial_cash) for frame in detail_frames])
    summary = summary.sort_values(["equity_multiple", "max_drawdown"], ascending=[False, True])
    yearly = build_yearly(detail)
    skips = build_skip_summary(detail)

    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    skips_path = output_prefix.with_name(output_prefix.name + "_skips.csv")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    yearly.to_csv(yearly_path, index=False, encoding="utf-8-sig")
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    skips.to_csv(skips_path, index=False, encoding="utf-8-sig")

    print("bj_cap_2pct 完整回放前视过滤验证完成")
    print(
        summary[
            [
                "rule_name",
                "equity_multiple",
                "executed_trade_count",
                "win_rate",
                "max_drawdown",
                "max_loss",
                "filter_skip_count",
                "position_occupied_skip_count",
                "bj_trade_count",
            ]
        ].to_string(index=False)
    )
    print("报告文件：")
    print(f"- summary: {summary_path}")
    print(f"- yearly: {yearly_path}")
    print(f"- detail: {detail_path}")
    print(f"- skips: {skips_path}")


if __name__ == "__main__":
    main()
