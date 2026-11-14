from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.factors import NextDayPremiumAnalyzer
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


GROUP_DEFINITIONS = {
    "overall": [],
    "market_segment": ["market_segment"],
    "limit_pct_bucket": ["limit_pct_bucket"],
    "market_segment_x_limit_pct": ["market_segment", "limit_pct_bucket"],
    "segment_market_sentiment": ["market_segment", "segment_market_sentiment_level"],
    "segment_limit_up_count": ["market_segment", "segment_limit_up_count_bucket"],
    "segment_limit_up_ratio": ["market_segment", "segment_limit_up_ratio_bucket"],
    "segment_leader_rank": ["market_segment", "segment_market_leader_rank_bucket"],
    "segment_height_rank": ["market_segment", "segment_limit_height_rank_bucket"],
    "segment_retreat_state": ["market_segment", "segment_retreat_state_bucket"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="诊断当前策略在不同市场板块和涨停制度下的表现。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--input-report", default="reports/trade_replay_report.csv", help="保守成交回放明细。")
    parser.add_argument("--replay-rule", default="fixed_t2_close", help="要诊断的回放规则。")
    parser.add_argument("--initial-cash", type=float, default=1000000, help="单组独立复利的起始资金。")
    parser.add_argument("--output-summary", default="reports/segmented_market_diagnostics_summary.csv")
    parser.add_argument("--output-yearly", default="reports/segmented_market_diagnostics_yearly.csv")
    parser.add_argument("--output-weak-years", default="reports/segmented_market_diagnostics_weak_years.csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )
    logger = get_logger("segmented_market_diagnostics")

    input_path = PROJECT_ROOT / args.input_report
    trades = pd.read_csv(input_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
    trades = trades[trades["replay_rule"].astype(str) == args.replay_rule].copy()
    if trades.empty:
        raise RuntimeError(f"没有找到 replay_rule={args.replay_rule} 的回放记录: {input_path}")

    summary_rows: list[dict[str, object]] = []
    yearly_rows: list[dict[str, object]] = []
    for group_type, columns in GROUP_DEFINITIONS.items():
        if not columns:
            summary_rows.append(summarize_group(group_type, "all", trades, args.initial_cash))
            yearly_rows.extend(build_yearly_rows(group_type, "all", trades))
            continue
        missing_columns = [column for column in columns if column not in trades.columns]
        if missing_columns:
            logger.warning("跳过 %s，缺少字段: %s", group_type, missing_columns)
            continue
        grouped = trades.copy()
        for column in columns:
            grouped[column] = grouped[column].fillna("unknown").astype(str)
        for keys, group in grouped.groupby(columns, dropna=False):
            if not isinstance(keys, tuple):
                keys = (keys,)
            group_value = "|".join(f"{column}={value}" for column, value in zip(columns, keys))
            summary_rows.append(summarize_group(group_type, group_value, group, args.initial_cash))
            yearly_rows.extend(build_yearly_rows(group_type, group_value, group))

    summary = pd.DataFrame(summary_rows)
    yearly = pd.DataFrame(yearly_rows)
    weak_years = yearly[yearly["year_return"] < 0].sort_values(
        ["year_return", "sample_count"],
        ascending=[True, False],
    )

    summary = summary.sort_values(
        ["group_type", "total_compound_return", "sample_count"],
        ascending=[True, False, False],
    )

    output_summary = PROJECT_ROOT / args.output_summary
    output_yearly = PROJECT_ROOT / args.output_yearly
    output_weak_years = PROJECT_ROOT / args.output_weak_years
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(output_summary, index=False, encoding="utf-8-sig")
    yearly.to_csv(output_yearly, index=False, encoding="utf-8-sig")
    weak_years.to_csv(output_weak_years, index=False, encoding="utf-8-sig")

    logger.info("分板块诊断汇总已生成: %s, 行数: %s", output_summary, len(summary))
    logger.info("分板块年度诊断已生成: %s, 行数: %s", output_yearly, len(yearly))
    logger.info("分板块弱年份报告已生成: %s, 行数: %s", output_weak_years, len(weak_years))
    print("分板块诊断完成：")
    print(f"- summary: {output_summary}")
    print(f"- yearly: {output_yearly}")
    print(f"- weak_years: {output_weak_years}")


def summarize_group(group_type: str, group_value: str, trades: pd.DataFrame, initial_cash: float) -> dict[str, object]:
    executed = select_executed(trades)
    returns = executed["net_return"].dropna()
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    daily_returns = executed.groupby("exit_trade_date")["daily_return"].sum().sort_index()
    equity_curve = (1 + daily_returns).cumprod()
    final_equity = initial_cash * float(equity_curve.iloc[-1]) if len(equity_curve) else initial_cash
    return {
        "group_type": group_type,
        "group_value": group_value,
        "signal_count": int(len(trades)),
        "buy_executed_count": int(trades["buy_executed"].fillna(False).sum()),
        "sell_executed_count": int((trades["buy_executed"].fillna(False) & trades["sell_executed"].fillna(False)).sum()),
        "buy_rejected_count": int((~trades["buy_executed"].fillna(False)).sum()),
        "sell_unresolved_count": int((trades["buy_executed"].fillna(False) & ~trades["sell_executed"].fillna(False)).sum()),
        "sample_count": int(len(executed)),
        "signal_days": int(trades["trade_date"].nunique()) if "trade_date" in trades.columns else 0,
        "executed_days": int(executed["exit_trade_date"].nunique()) if not executed.empty else 0,
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "total_compound_return": final_equity / initial_cash - 1,
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
        "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
        "sum_daily_return": float(daily_returns.sum()) if len(daily_returns) else 0.0,
        "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
        "max_profit_per_trade": float(returns.max()) if len(returns) else 0.0,
        "max_loss_per_trade": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_consecutive_losses": int(NextDayPremiumAnalyzer.max_consecutive_losses(returns)),
        "limit_down_blocked_trades": int((trades.get("limit_down_blocked_days", pd.Series(0, index=trades.index)) > 0).sum()),
    }


def build_yearly_rows(group_type: str, group_value: str, trades: pd.DataFrame) -> list[dict[str, object]]:
    executed = select_executed(trades)
    if executed.empty:
        return []
    data = executed.copy()
    data["year"] = data["exit_trade_date"].astype(str).str[:4]
    rows = []
    for year, group in data.groupby("year"):
        returns = group["net_return"].dropna()
        daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        rows.append(
            {
                "group_type": group_type,
                "group_value": group_value,
                "year": str(year),
                "sample_count": int(len(group)),
                "year_return": float((1 + daily_returns).prod() - 1),
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
                "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
                "max_loss_per_trade": float(returns.min()) if len(returns) else 0.0,
                "max_profit_per_trade": float(returns.max()) if len(returns) else 0.0,
            }
        )
    return rows


def select_executed(trades: pd.DataFrame) -> pd.DataFrame:
    return trades[
        (trades["buy_executed"].fillna(False) == True)  # noqa: E712
        & (trades["sell_executed"].fillna(False) == True)  # noqa: E712
        & trades["daily_return"].notna()
    ].copy()


if __name__ == "__main__":
    main()
