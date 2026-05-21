from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.factors import NextDayPremiumAnalyzer
from src.strategy_optimizer import StrategyConditionOptimizer
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="固定买入条件，比较卖出规则矩阵。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
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

    matrix_config = config.get("exit_rule_matrix", {})
    base_conditions = matrix_config.get("base_conditions", {})
    position_pct = float(matrix_config.get("position_pct", 0.8))
    initial_cash = float(matrix_config.get("initial_cash", 1000000))
    output_report_path = PROJECT_ROOT / matrix_config.get("output_report_path", "reports/exit_rule_matrix_report.csv")
    output_yearly_path = PROJECT_ROOT / matrix_config.get("output_yearly_path", "reports/exit_rule_matrix_yearly.csv")

    optimizer = StrategyConditionOptimizer(
        config_path=args.config,
        optimization_config_key="exit_rule_optimization",
    )
    trades = optimizer.load_trades()
    for column, value in base_conditions.items():
        trades = trades[trades[column].astype(str) == str(value)].copy()

    summary_rows = []
    yearly_rows = []
    for exit_rule, group in trades.groupby("exit_rule"):
        selected = select_top1(group, position_pct=position_pct)
        if selected.empty:
            continue
        summary_rows.append(summarize_rule(exit_rule, selected, initial_cash=initial_cash, position_pct=position_pct))
        yearly_rows.extend(build_yearly_rows(exit_rule, selected))

    report = pd.DataFrame(summary_rows).sort_values(
        ["final_equity", "max_drawdown"],
        ascending=[False, True],
    )
    yearly = pd.DataFrame(yearly_rows)
    output_report_path.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(output_report_path, index=False, encoding="utf-8-sig")
    yearly.to_csv(output_yearly_path, index=False, encoding="utf-8-sig")

    print("卖出规则矩阵完成：")
    print(f"- report: {output_report_path}")
    print(f"- yearly: {output_yearly_path}")


def select_top1(group: pd.DataFrame, position_pct: float) -> pd.DataFrame:
    sort_columns = ["trade_date", "fill_probability", "sample_count", "amount", "turnover_rate"]
    existing_sort_columns = [column for column in sort_columns if column in group.columns]
    ascending = [True] + [False] * (len(existing_sort_columns) - 1)
    selected = group.sort_values(existing_sort_columns, ascending=ascending)
    selected = selected.groupby("trade_date").head(1).copy()
    selected["position_pct"] = position_pct
    selected["daily_return"] = selected["net_return"] * selected["position_pct"]
    return selected


def summarize_rule(exit_rule: str, selected: pd.DataFrame, initial_cash: float, position_pct: float) -> dict[str, object]:
    returns = selected["net_return"].dropna()
    daily_returns = selected.groupby("exit_trade_date")["daily_return"].sum().sort_index()
    equity_curve = (1 + daily_returns).cumprod()
    final_equity = initial_cash * float(equity_curve.iloc[-1])
    gains = returns[returns > 0]
    losses = returns[returns <= 0]
    return {
        "exit_rule": exit_rule,
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "total_compound_return": final_equity / initial_cash - 1,
        "sample_count": int(len(selected)),
        "signal_days": int(selected["trade_date"].nunique()),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
        "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
        "max_profit_per_trade": float(returns.max()) if len(returns) else 0.0,
        "max_loss_per_trade": float(returns.min()) if len(returns) else 0.0,
        "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
        "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
        "max_consecutive_losses": int(NextDayPremiumAnalyzer.max_consecutive_losses(returns)),
        "position_pct": position_pct,
    }


def build_yearly_rows(exit_rule: str, selected: pd.DataFrame) -> list[dict[str, object]]:
    rows = []
    data = selected.copy()
    data["year"] = data["exit_trade_date"].astype(str).str[:4]
    for year, group in data.groupby("year"):
        returns = group["net_return"].dropna()
        daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        rows.append(
            {
                "exit_rule": exit_rule,
                "year": year,
                "sample_count": int(len(group)),
                "signal_days": int(group["trade_date"].nunique()),
                "year_return": float((1 + daily_returns).prod() - 1),
                "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
                "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
            }
        )
    return rows


if __name__ == "__main__":
    main()
