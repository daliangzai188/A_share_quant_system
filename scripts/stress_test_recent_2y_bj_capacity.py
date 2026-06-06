"""
北交所收益来源专项压力测试。

文件作用：
1. 固定最近 2 年 50 倍候选中的方案2。
2. 重新计算资金曲线，分别测试 BJ 成交容量上限 5% / 3% / 2% / 1%。
3. 测试滑点倍率 1.0 / 1.5 / 2.0 / 3.0。
4. 输出每组压力场景下的复利、回撤、成交笔数、平均仓位和年度收益。

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
    parser = argparse.ArgumentParser(description="北交所容量和滑点压力测试。")
    parser.add_argument(
        "--input",
        default="reports/recent_2y_target50_top_scenarios_trades.csv",
        help="逐笔交易报告路径。",
    )
    parser.add_argument("--scenario-rank", type=int, default=2, help="要测试的方案排名，默认方案2。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument(
        "--output-prefix",
        default="reports/recent_2y_bj_capacity_stress",
        help="输出文件前缀。",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def estimate_slippage(amount_ratio: float, tiers: list[dict[str, Any]], multiplier: float) -> float:
    if pd.isna(amount_ratio) or amount_ratio <= 0:
        return 0.0
    for tier in tiers:
        threshold = tier.get("max_amount_ratio")
        if threshold is None or amount_ratio <= float(threshold) + 1e-12:
            return float(tier.get("slippage_rate", 0.0)) * multiplier
    return 0.0


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    return float(drawdown.min())


def max_consecutive_losses(returns: pd.Series) -> int:
    max_count = 0
    current = 0
    for value in returns:
        if value <= 0:
            current += 1
            max_count = max(max_count, current)
        else:
            current = 0
    return max_count


def normalize_date(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def load_selected_rows(path: Path, scenario_rank: int) -> pd.DataFrame:
    data = pd.read_csv(path, low_memory=False)
    data = data[data["scenario_rank"] == scenario_rank].copy()
    if data.empty:
        raise RuntimeError(f"没有找到 scenario_rank={scenario_rank}: {path}")
    data["selected_order"] = pd.to_numeric(data["selected_order"], errors="coerce")
    return data.sort_values("selected_order").reset_index(drop=True)


def simulate(
    rows: pd.DataFrame,
    initial_cash: float,
    position_pct: float,
    default_capacity: float,
    bj_capacity: float,
    slippage_tiers: list[dict[str, Any]],
    slippage_multiplier: float,
    fee_rate_without_slippage: float,
) -> tuple[dict[str, Any], pd.DataFrame]:
    equity = initial_cash
    details: list[dict[str, Any]] = []
    trade_order = 0

    for _, row in rows.iterrows():
        result = row.to_dict()
        result["stress_bj_capacity"] = bj_capacity
        result["stress_slippage_multiplier"] = slippage_multiplier
        result["stress_equity_before"] = equity

        if not bool(row.get("scenario_executed", False)):
            result.update(
                {
                    "stress_executed": False,
                    "stress_skip_reason": str(row.get("skip_reason", "")),
                    "stress_account_return": 0.0,
                    "stress_equity_after": equity,
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
                    "stress_executed": False,
                    "stress_skip_reason": "missing_liquidity_or_price",
                    "stress_account_return": 0.0,
                    "stress_equity_after": equity,
                }
            )
            details.append(result)
            continue

        trade_order += 1
        market_segment = str(row.get("market_segment", ""))
        capacity = bj_capacity if market_segment == "bj" else default_capacity
        target_buy_amount = equity * position_pct
        actual_buy_amount = min(target_buy_amount, buy_amount_day * capacity)
        actual_position_pct = actual_buy_amount / equity if equity > 0 else 0.0

        buy_amount_ratio = actual_buy_amount / buy_amount_day if buy_amount_day > 0 else 0.0
        buy_slippage = estimate_slippage(buy_amount_ratio, slippage_tiers, slippage_multiplier)
        buy_price = buy_price_raw * (1.0 + buy_slippage)

        sell_value_before_slippage = actual_buy_amount * sell_price_raw / buy_price
        sell_amount_ratio = sell_value_before_slippage / sell_amount_day if sell_amount_day > 0 else 0.0
        sell_slippage = estimate_slippage(sell_amount_ratio, slippage_tiers, slippage_multiplier)
        sell_price = sell_price_raw * (1.0 - sell_slippage)

        net_return = sell_price / buy_price - 1.0 - fee_rate_without_slippage
        account_return = net_return * actual_position_pct
        equity_after = equity * (1.0 + account_return)

        result.update(
            {
                "stress_executed": True,
                "stress_trade_order": trade_order,
                "stress_skip_reason": "",
                "stress_capacity": capacity,
                "stress_target_buy_amount": target_buy_amount,
                "stress_actual_buy_amount": actual_buy_amount,
                "stress_actual_position_pct": actual_position_pct,
                "stress_buy_amount_ratio": buy_amount_ratio,
                "stress_sell_amount_ratio": sell_amount_ratio,
                "stress_buy_slippage": buy_slippage,
                "stress_sell_slippage": sell_slippage,
                "stress_net_return": net_return,
                "stress_account_return": account_return,
                "stress_equity_after": equity_after,
            }
        )
        equity = equity_after
        details.append(result)

    detail = pd.DataFrame(details)
    executed = detail[detail["stress_executed"] == True].copy()  # noqa: E712
    returns = executed["stress_account_return"].astype(float) if not executed.empty else pd.Series(dtype=float)
    summary = {
        "bj_capacity": bj_capacity,
        "slippage_multiplier": slippage_multiplier,
        "initial_cash": initial_cash,
        "final_equity": equity,
        "equity_multiple": equity / initial_cash if initial_cash else 0.0,
        "executed_trade_count": int(len(executed)),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
        "median_account_return": float(returns.median()) if len(returns) else 0.0,
        "max_profit": float(returns.max()) if len(returns) else 0.0,
        "max_loss": float(returns.min()) if len(returns) else 0.0,
        "max_drawdown": max_drawdown(executed["stress_equity_after"]) if len(executed) else 0.0,
        "max_consecutive_losses": max_consecutive_losses(returns),
        "avg_actual_position_pct": float(executed["stress_actual_position_pct"].mean()) if len(executed) else 0.0,
        "avg_buy_slippage": float(executed["stress_buy_slippage"].mean()) if len(executed) else 0.0,
        "avg_sell_slippage": float(executed["stress_sell_slippage"].mean()) if len(executed) else 0.0,
        "max_buy_amount_ratio": float(executed["stress_buy_amount_ratio"].max()) if len(executed) else 0.0,
        "max_sell_amount_ratio": float(executed["stress_sell_amount_ratio"].max()) if len(executed) else 0.0,
    }
    return summary, detail


def build_yearly(detail: pd.DataFrame, bj_capacity: float, slippage_multiplier: float) -> pd.DataFrame:
    executed = detail[detail["stress_executed"] == True].copy()  # noqa: E712
    if executed.empty:
        return pd.DataFrame()
    executed["year"] = executed["exit_trade_date"].map(normalize_date).str[:4]
    rows = []
    for year, group in executed.groupby("year"):
        first_equity = float(group["stress_equity_before"].iloc[0])
        last_equity = float(group["stress_equity_after"].iloc[-1])
        returns = group["stress_account_return"].astype(float)
        rows.append(
            {
                "bj_capacity": bj_capacity,
                "slippage_multiplier": slippage_multiplier,
                "year": year,
                "trade_count": int(len(group)),
                "year_return": last_equity / first_equity - 1 if first_equity else 0.0,
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_account_return": float(returns.mean()) if len(returns) else 0.0,
                "max_drawdown": max_drawdown(group["stress_equity_after"]),
            }
        )
    return pd.DataFrame(rows)


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

    rows = load_selected_rows(PROJECT_ROOT / args.input, args.scenario_rank)
    bj_capacities = [0.05, 0.03, 0.02, 0.01]
    slippage_multipliers = [1.0, 1.5, 2.0, 3.0]

    summaries = []
    yearly_frames = []
    detail_frames = []
    for bj_capacity in bj_capacities:
        for multiplier in slippage_multipliers:
            summary, detail = simulate(
                rows=rows,
                initial_cash=initial_cash,
                position_pct=position_pct,
                default_capacity=default_capacity,
                bj_capacity=bj_capacity,
                slippage_tiers=slippage_tiers,
                slippage_multiplier=multiplier,
                fee_rate_without_slippage=fee_rate_without_slippage,
            )
            summaries.append(summary)
            yearly_frames.append(build_yearly(detail, bj_capacity, multiplier))
            detail_frames.append(detail)

    output_prefix = PROJECT_ROOT / args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_report = pd.DataFrame(summaries)
    yearly_report = pd.concat(yearly_frames, ignore_index=True) if yearly_frames else pd.DataFrame()
    detail_report = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()

    summary_path = output_prefix.with_name(output_prefix.name + "_summary.csv")
    yearly_path = output_prefix.with_name(output_prefix.name + "_yearly.csv")
    detail_path = output_prefix.with_name(output_prefix.name + "_detail.csv")
    summary_report.to_csv(summary_path, index=False, encoding="utf-8-sig")
    yearly_report.to_csv(yearly_path, index=False, encoding="utf-8-sig")
    detail_report.to_csv(detail_path, index=False, encoding="utf-8-sig")

    print("北交所容量/滑点压力测试完成")
    print(summary_report.sort_values(["bj_capacity", "slippage_multiplier"]).to_string(index=False))
    print("报告文件：")
    print(f"- summary: {summary_path}")
    print(f"- yearly: {yearly_path}")
    print(f"- detail: {detail_path}")


if __name__ == "__main__":
    main()
