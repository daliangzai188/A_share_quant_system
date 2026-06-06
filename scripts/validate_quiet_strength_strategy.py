"""
验证“安静上涨”收盘买入策略。

文件作用：
1. 使用本地 daily_merged.csv 验证外部 AI 给出的策略。
2. 信号口径固定为 T 日收盘后选股，T 日收盘价模拟买入，T+1 收盘价模拟卖出。
3. 按真实执行约束计算：手续费、印花税、过户费、动态滑点、成交额容量降仓。
4. 分 all / sh_main / sz_main 三个市场口径输出结果，并生成 Walk-Forward 报告。

重要说明：
日线数据无法证明 14:57-15:00 收盘集合竞价的真实盘口队列，本脚本只能做日线级别的保守成交模拟。
后续若要证明精确成交价，需要分钟数据或逐笔 / Level-2 数据。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


@dataclass(frozen=True)
class FeeConfig:
    commission_rate: float
    stamp_tax_rate: float
    transfer_fee_rate: float

    @property
    def round_trip_rate(self) -> float:
        return (
            self.commission_rate
            + self.transfer_fee_rate
            + self.commission_rate
            + self.transfer_fee_rate
            + self.stamp_tax_rate
        )


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def to_path(relative_path: str) -> Path:
    return PROJECT_ROOT / relative_path


def normalize_trade_date(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(8)


def estimate_slippage(amount_ratio: float, tiers: list[dict[str, Any]]) -> float:
    if amount_ratio <= 0 or np.isnan(amount_ratio):
        return 0.0
    for tier in tiers:
        threshold = tier.get("max_amount_ratio")
        if threshold is None or amount_ratio <= float(threshold):
            return float(tier["slippage_rate"])
    return float(tiers[-1]["slippage_rate"]) if tiers else 0.0


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
        if value < 0:
            current += 1
            max_count = max(max_count, current)
        else:
            current = 0
    return max_count


def load_daily_data(config: dict[str, Any]) -> pd.DataFrame:
    path = to_path(config["input_daily_merged_path"])
    required_columns = [
        "ts_code",
        "trade_date",
        "close",
        "pct_chg",
        "amount",
        "volume_ratio",
        "circ_mv",
        "market_segment",
        "limit_pct",
    ]
    daily = pd.read_csv(path, usecols=required_columns, dtype={"ts_code": str, "trade_date": str}, low_memory=False)
    daily["trade_date"] = normalize_trade_date(daily["trade_date"])
    numeric_columns = ["close", "pct_chg", "amount", "volume_ratio", "circ_mv", "limit_pct"]
    for column in numeric_columns:
        daily[column] = pd.to_numeric(daily[column], errors="coerce")

    # Tushare daily.amount 单位是千元，daily_basic.circ_mv 单位是万元。
    daily["amount_yuan"] = daily["amount"] * 1000.0
    daily["circ_mv_yuan"] = daily["circ_mv"] * 10000.0
    daily["market_segment"] = daily["market_segment"].fillna("unknown").astype(str)
    return daily.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def load_st_codes(config: dict[str, Any]) -> set[str]:
    path = to_path(config["input_limit_up_merged_path"])
    if not path.exists():
        return set()
    try:
        limit_up = pd.read_csv(path, usecols=["ts_code", "is_st"], dtype={"ts_code": str}, low_memory=False)
    except ValueError:
        return set()
    if "is_st" not in limit_up.columns:
        return set()
    is_st = limit_up["is_st"].astype(str).str.lower().isin(["true", "1", "yes"])
    return set(limit_up.loc[is_st, "ts_code"].dropna().astype(str).unique())


def attach_next_day(daily: pd.DataFrame) -> pd.DataFrame:
    result = daily.copy()
    grouped = result.groupby("ts_code", sort=False)
    result["sell_trade_date"] = grouped["trade_date"].shift(-1)
    result["sell_close_raw"] = grouped["close"].shift(-1)
    result["sell_pct_chg"] = grouped["pct_chg"].shift(-1)
    result["sell_amount_yuan"] = grouped["amount_yuan"].shift(-1)
    return result


def build_signal_pool(daily: pd.DataFrame, config: dict[str, Any], st_codes: set[str]) -> pd.DataFrame:
    start_date = str(config["start_date"])
    end_date = str(config["end_date"])
    signals = daily[
        (daily["trade_date"] >= start_date)
        & (daily["trade_date"] <= end_date)
        & (daily["pct_chg"] >= float(config["pct_chg_min"]))
        & (daily["pct_chg"] <= float(config["pct_chg_max"]))
        & (daily["volume_ratio"] < float(config["volume_ratio_max"]))
        & (daily["amount_yuan"] >= float(config["amount_min_yuan"]))
        & (daily["circ_mv_yuan"] >= float(config["circ_mv_min_yuan"]))
        & daily["close"].notna()
    ].copy()

    if st_codes:
        signals = signals[~signals["ts_code"].isin(st_codes)].copy()
    return signals


def attach_sell_execution(signals: pd.DataFrame, daily: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    """为每个信号匹配真实可卖日。

    默认卖出日是 T+1；如果 T+1 跌停，则顺延到后续第一个非跌停交易日。
    这里仍然是日线级模拟，不能替代逐笔盘口排队验证。
    """
    if signals.empty:
        return signals

    assume_limit_down_unsellable = bool(config.get("assume_limit_down_unsellable", True))
    tolerance = float(config.get("limit_price_tolerance", 0.002))

    lookup: dict[str, pd.DataFrame] = {}
    daily = daily.copy()
    daily["is_limit_down_for_sell"] = (
        daily["pct_chg"] <= -(daily["limit_pct"].fillna(0.1) * 100.0 * (1.0 - tolerance))
    )
    for ts_code, group in daily.sort_values(["ts_code", "trade_date"]).groupby("ts_code", sort=False):
        lookup[str(ts_code)] = group.reset_index(drop=True)

    rows: list[dict[str, Any]] = []
    for _, signal in signals.iterrows():
        row = signal.to_dict()
        stock_rows = lookup.get(str(signal["ts_code"]))
        if stock_rows is None:
            row.update(
                {
                    "sell_trade_date": np.nan,
                    "sell_close_raw": np.nan,
                    "sell_pct_chg": np.nan,
                    "sell_amount_yuan": np.nan,
                    "sell_delayed": False,
                    "sell_delay_days": np.nan,
                    "sell_status": "NO_STOCK_DATA",
                }
            )
            rows.append(row)
            continue

        future = stock_rows[stock_rows["trade_date"] > str(signal["trade_date"])].copy()
        if future.empty:
            row.update(
                {
                    "sell_trade_date": np.nan,
                    "sell_close_raw": np.nan,
                    "sell_pct_chg": np.nan,
                    "sell_amount_yuan": np.nan,
                    "sell_delayed": False,
                    "sell_delay_days": np.nan,
                    "sell_status": "NO_FUTURE_DAILY",
                }
            )
            rows.append(row)
            continue

        if assume_limit_down_unsellable:
            sell_candidates = future[~future["is_limit_down_for_sell"]].copy()
        else:
            sell_candidates = future

        if sell_candidates.empty:
            row.update(
                {
                    "sell_trade_date": np.nan,
                    "sell_close_raw": np.nan,
                    "sell_pct_chg": np.nan,
                    "sell_amount_yuan": np.nan,
                    "sell_delayed": True,
                    "sell_delay_days": int(len(future)),
                    "sell_status": "LIMIT_DOWN_NO_SELL_DAY",
                }
            )
            rows.append(row)
            continue

        sell = sell_candidates.iloc[0]
        first_future_date = str(future.iloc[0]["trade_date"])
        sell_date = str(sell["trade_date"])
        row.update(
            {
                "sell_trade_date": sell_date,
                "sell_close_raw": float(sell["close"]),
                "sell_pct_chg": float(sell["pct_chg"]),
                "sell_amount_yuan": float(sell["amount_yuan"]),
                "sell_delayed": sell_date != first_future_date,
                "sell_delay_days": int((future["trade_date"] <= sell_date).sum() - 1),
                "sell_status": "OK",
            }
        )
        rows.append(row)

    result = pd.DataFrame(rows)
    return result[result["sell_status"] == "OK"].copy()


def select_daily_candidates(signals: pd.DataFrame, segment: str, config: dict[str, Any]) -> pd.DataFrame:
    if segment == "all":
        segment_signals = signals.copy()
    else:
        segment_signals = signals[signals["market_segment"] == segment].copy()

    if segment_signals.empty:
        return segment_signals

    ranking_column = str(config["ranking_column"])
    ascending = bool(config["ranking_ascending"])
    return (
        segment_signals.sort_values(["trade_date", ranking_column], ascending=[True, ascending])
        .groupby("trade_date", as_index=False)
        .head(1)
        .sort_values("trade_date")
        .reset_index(drop=True)
    )


def simulate_segment(
    candidates: pd.DataFrame,
    segment: str,
    config: dict[str, Any],
    fee_config: FeeConfig,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    equity = float(config["initial_cash"])
    position_pct = float(config["position_pct"])
    max_buy_amount_ratio = float(config["max_buy_amount_ratio"])
    allow_same_close_rebalance = bool(config.get("allow_same_close_rebalance", True))
    slippage_tiers = list(config.get("slippage_tiers", []))

    occupied_until = ""
    skipped_position_occupied = 0
    skipped_capacity_zero = 0
    rows: list[dict[str, Any]] = []

    for _, signal in candidates.iterrows():
        buy_date = str(signal["trade_date"])
        sell_date = str(signal["sell_trade_date"])
        if occupied_until:
            occupied = buy_date < occupied_until if allow_same_close_rebalance else buy_date <= occupied_until
            if occupied:
                skipped_position_occupied += 1
                continue

        buy_day_amount_yuan = float(signal["amount_yuan"])
        sell_day_amount_yuan = float(signal["sell_amount_yuan"])
        if buy_day_amount_yuan <= 0 or sell_day_amount_yuan <= 0:
            skipped_capacity_zero += 1
            continue

        target_buy_amount = equity * position_pct
        capacity_buy_amount = buy_day_amount_yuan * max_buy_amount_ratio
        actual_buy_amount = min(target_buy_amount, capacity_buy_amount)
        if actual_buy_amount <= 0:
            skipped_capacity_zero += 1
            continue

        actual_position_pct = actual_buy_amount / equity
        buy_amount_ratio = actual_buy_amount / buy_day_amount_yuan
        buy_slippage_rate = estimate_slippage(buy_amount_ratio, slippage_tiers)
        buy_price_raw = float(signal["close"])
        buy_price = buy_price_raw * (1.0 + buy_slippage_rate)

        sell_close_raw = float(signal["sell_close_raw"])
        sell_value_before_slippage = actual_buy_amount * sell_close_raw / buy_price
        sell_amount_ratio = sell_value_before_slippage / sell_day_amount_yuan
        sell_slippage_rate = estimate_slippage(sell_amount_ratio, slippage_tiers)
        sell_price = sell_close_raw * (1.0 - sell_slippage_rate)

        gross_return = sell_price / buy_price - 1.0
        net_return = gross_return - fee_config.round_trip_rate
        account_return = net_return * actual_position_pct
        equity_before = equity
        equity = equity * (1.0 + account_return)

        rows.append(
            {
                "segment": segment,
                "seq": len(rows) + 1,
                "signal_trade_date": buy_date,
                "sell_trade_date": sell_date,
                "ts_code": str(signal["ts_code"]),
                "market_segment": str(signal["market_segment"]),
                "pct_chg": float(signal["pct_chg"]),
                "volume_ratio": float(signal["volume_ratio"]),
                "amount_yuan": buy_day_amount_yuan,
                "circ_mv_yuan": float(signal["circ_mv_yuan"]),
                "buy_price_raw": buy_price_raw,
                "sell_price_raw": sell_close_raw,
                "buy_price_executed": buy_price,
                "sell_price_executed": sell_price,
                "sell_delayed": bool(signal.get("sell_delayed", False)),
                "sell_delay_days": int(signal.get("sell_delay_days", 0)),
                "target_buy_amount": target_buy_amount,
                "actual_buy_amount": actual_buy_amount,
                "actual_position_pct": actual_position_pct,
                "capacity_limited": actual_buy_amount < target_buy_amount,
                "buy_amount_ratio": buy_amount_ratio,
                "sell_amount_ratio": sell_amount_ratio,
                "buy_slippage_rate": buy_slippage_rate,
                "sell_slippage_rate": sell_slippage_rate,
                "fee_rate": fee_config.round_trip_rate,
                "gross_return": gross_return,
                "net_return": net_return,
                "account_return": account_return,
                "equity_before": equity_before,
                "equity_after": equity,
            }
        )
        occupied_until = sell_date

    trades = pd.DataFrame(rows)
    diagnostics = {
        "segment": segment,
        "candidate_count": int(len(candidates)),
        "skipped_position_occupied": int(skipped_position_occupied),
        "skipped_capacity_zero": int(skipped_capacity_zero),
    }
    return trades, diagnostics


def summarize_trades(
    trades: pd.DataFrame,
    segment: str,
    config: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    initial_cash = float(config["initial_cash"])
    if trades.empty:
        return {
            "segment": segment,
            "candidate_count": diagnostics["candidate_count"],
            "executed_trades": 0,
            "final_equity": initial_cash,
            "equity_multiple": 1.0,
        }

    equity_multiple = float(trades["equity_after"].iloc[-1] / initial_cash)
    net_returns = trades["net_return"]
    wins = net_returns > 0
    losses = net_returns < 0
    profit_sum = float(net_returns[wins].sum())
    loss_sum = abs(float(net_returns[losses].sum()))
    wf = config.get("walk_forward", {})
    train = trades[
        (trades["signal_trade_date"] >= str(wf.get("train_start", "00000000")))
        & (trades["signal_trade_date"] <= str(wf.get("train_end", "99999999")))
    ]
    test = trades[
        (trades["signal_trade_date"] >= str(wf.get("test_start", "00000000")))
        & (trades["signal_trade_date"] <= str(wf.get("test_end", "99999999")))
    ]

    return {
        "segment": segment,
        "candidate_count": diagnostics["candidate_count"],
        "executed_trades": int(len(trades)),
        "skipped_position_occupied": diagnostics["skipped_position_occupied"],
        "skipped_capacity_zero": diagnostics["skipped_capacity_zero"],
        "win_rate": float(wins.mean()),
        "avg_net_return": float(net_returns.mean()),
        "median_net_return": float(net_returns.median()),
        "max_single_profit": float(net_returns.max()),
        "max_single_loss": float(net_returns.min()),
        "profit_loss_ratio": float(profit_sum / loss_sum) if loss_sum > 0 else np.nan,
        "max_consecutive_losses": max_consecutive_losses(net_returns),
        "max_drawdown": max_drawdown(trades["equity_after"]),
        "initial_cash": initial_cash,
        "final_equity": float(trades["equity_after"].iloc[-1]),
        "equity_multiple": equity_multiple,
        "total_compound_return": equity_multiple - 1.0,
        "avg_actual_buy_amount": float(trades["actual_buy_amount"].mean()),
        "avg_actual_position_pct": float(trades["actual_position_pct"].mean()),
        "capacity_limited_rate": float(trades["capacity_limited"].mean()),
        "avg_buy_amount_ratio": float(trades["buy_amount_ratio"].mean()),
        "avg_sell_amount_ratio": float(trades["sell_amount_ratio"].mean()),
        "avg_buy_slippage_rate": float(trades["buy_slippage_rate"].mean()),
        "avg_sell_slippage_rate": float(trades["sell_slippage_rate"].mean()),
        "sell_delayed_count": int(trades["sell_delayed"].sum()),
        "sell_delayed_rate": float(trades["sell_delayed"].mean()),
        "train_trade_count": int(len(train)),
        "train_equity_multiple": compound_multiple(train),
        "train_win_rate": float((train["net_return"] > 0).mean()) if not train.empty else np.nan,
        "train_max_drawdown": max_drawdown(train["equity_after"]) if not train.empty else np.nan,
        "test_trade_count": int(len(test)),
        "test_equity_multiple": compound_multiple(test),
        "test_win_rate": float((test["net_return"] > 0).mean()) if not test.empty else np.nan,
        "test_max_drawdown": max_drawdown(test["equity_after"]) if not test.empty else np.nan,
    }


def compound_multiple(trades: pd.DataFrame) -> float:
    if trades.empty:
        return np.nan
    returns = trades["account_return"].astype(float)
    return float((1.0 + returns).prod())


def build_yearly_report(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    result_rows: list[dict[str, Any]] = []
    for (segment, year), group in trades.assign(year=trades["signal_trade_date"].str[:4]).groupby(["segment", "year"]):
        year_multiple = float((1.0 + group["account_return"]).prod())
        result_rows.append(
            {
                "segment": segment,
                "year": year,
                "trade_count": int(len(group)),
                "win_rate": float((group["net_return"] > 0).mean()),
                "avg_net_return": float(group["net_return"].mean()),
                "median_net_return": float(group["net_return"].median()),
                "year_equity_multiple": year_multiple,
                "year_return": year_multiple - 1.0,
                "max_drawdown": max_drawdown(group["equity_after"]),
                "avg_actual_position_pct": float(group["actual_position_pct"].mean()),
                "capacity_limited_rate": float(group["capacity_limited"].mean()),
                "avg_buy_slippage_rate": float(group["buy_slippage_rate"].mean()),
                "avg_sell_slippage_rate": float(group["sell_slippage_rate"].mean()),
            }
        )
    return pd.DataFrame(result_rows)


def print_report(summary: pd.DataFrame, yearly: pd.DataFrame) -> None:
    print("\n安静上涨策略真实执行验证完成")
    print("口径：T日收盘买入，T+1收盘卖出；50万初始资金；每笔目标80%仓位；超过当日成交额5%则降仓。")
    print("注意：这是日线级别成交模拟，不是逐笔盘口验证。")
    print("\n汇总：")
    display_columns = [
        "segment",
        "candidate_count",
        "executed_trades",
        "win_rate",
        "equity_multiple",
        "max_drawdown",
        "avg_net_return",
        "avg_actual_position_pct",
        "capacity_limited_rate",
        "avg_buy_slippage_rate",
        "avg_sell_slippage_rate",
        "sell_delayed_rate",
        "test_equity_multiple",
        "test_win_rate",
        "test_max_drawdown",
    ]
    printable = summary[display_columns].copy()
    for column in [
        "win_rate",
        "max_drawdown",
        "avg_net_return",
        "avg_actual_position_pct",
        "capacity_limited_rate",
        "avg_buy_slippage_rate",
        "avg_sell_slippage_rate",
        "sell_delayed_rate",
        "test_win_rate",
        "test_max_drawdown",
    ]:
        printable[column] = printable[column].map(lambda value: "" if pd.isna(value) else f"{value:.2%}")
    for column in ["equity_multiple", "test_equity_multiple"]:
        printable[column] = printable[column].map(lambda value: "" if pd.isna(value) else f"{value:.2f}x")
    print(printable.to_string(index=False))

    if not yearly.empty:
        print("\n年度结果：")
        year_print = yearly.copy()
        for column in ["win_rate", "year_return", "max_drawdown", "avg_net_return"]:
            year_print[column] = year_print[column].map(lambda value: f"{value:.2%}")
        year_print["year_equity_multiple"] = year_print["year_equity_multiple"].map(lambda value: f"{value:.2f}x")
        print(
            year_print[
                ["segment", "year", "trade_count", "win_rate", "year_equity_multiple", "year_return", "max_drawdown", "avg_net_return"]
            ].to_string(index=False)
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证安静上涨收盘买入策略")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径")
    parser.add_argument("--start-date", help="覆盖配置里的开始日期，例如 20200101")
    parser.add_argument("--end-date", help="覆盖配置里的结束日期，例如 20260518")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root_config = load_config(to_path(args.config))
    config = dict(root_config["quiet_strength_strategy_validation"])
    if args.start_date:
        config["start_date"] = args.start_date
    if args.end_date:
        config["end_date"] = args.end_date

    risk_config = root_config.get("risk", {})
    fee_config = FeeConfig(
        commission_rate=float(risk_config.get("commission_rate", 0.0003)),
        stamp_tax_rate=float(risk_config.get("stamp_tax_rate", 0.001)),
        transfer_fee_rate=float(risk_config.get("transfer_fee_rate", 0.00001)),
    )

    daily = attach_next_day(load_daily_data(config))
    st_codes = load_st_codes(config)
    signals = build_signal_pool(daily, config, st_codes)
    signals = attach_sell_execution(signals, daily, config)

    all_trades: list[pd.DataFrame] = []
    summary_rows: list[dict[str, Any]] = []
    for segment in config["segments"]:
        candidates = select_daily_candidates(signals, str(segment), config)
        trades, diagnostics = simulate_segment(candidates, str(segment), config, fee_config)
        all_trades.append(trades)
        summary_rows.append(summarize_trades(trades, str(segment), config, diagnostics))

    trades_report = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    summary_report = pd.DataFrame(summary_rows)
    yearly_report = build_yearly_report(trades_report)

    output_summary = to_path(config["output_summary_path"])
    output_yearly = to_path(config["output_yearly_path"])
    output_trades = to_path(config["output_trades_path"])
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_yearly.parent.mkdir(parents=True, exist_ok=True)
    output_trades.parent.mkdir(parents=True, exist_ok=True)

    summary_report.to_csv(output_summary, index=False, encoding="utf-8-sig")
    yearly_report.to_csv(output_yearly, index=False, encoding="utf-8-sig")
    trades_report.to_csv(output_trades, index=False, encoding="utf-8-sig")

    print_report(summary_report, yearly_report)
    print("\n报告文件：")
    print(f"- summary: {output_summary}")
    print(f"- yearly: {output_yearly}")
    print(f"- trades: {output_trades}")


if __name__ == "__main__":
    main()
