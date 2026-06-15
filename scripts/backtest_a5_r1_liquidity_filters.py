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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回测 A5-R1 1000万流动性过滤版本。")
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
    outputs = A5R1LiquidityFilterBacktester(config_path=args.config).backtest()
    print("A5-R1 1000万流动性过滤回测完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class A5R1LiquidityFilterBacktester:
    """基于 1000 万流动性审计结果，回测不同流动性过滤门槛。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("a5_r1_liquidity_filter_backtest")
        self.backtest_config = self.config.get("a5_r1_liquidity_filter_backtest", {})
        self.input_liquidity_detail_path = self.project_root / self.backtest_config.get(
            "input_liquidity_detail_path",
            "reports/a5_r1_buy_liquidity_detail.csv",
        )
        self.output_summary_path = self.project_root / self.backtest_config.get(
            "output_summary_path",
            "reports/a5_r1_liquidity_filter_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.backtest_config.get(
            "output_yearly_path",
            "reports/a5_r1_liquidity_filter_yearly.csv",
        )
        self.output_filtered_detail_path = self.project_root / self.backtest_config.get(
            "output_filtered_detail_path",
            "reports/a5_r1_liquidity_filter_filtered_detail.csv",
        )
        self.scenarios = list(self.backtest_config.get("scenarios", []))

    def backtest(self) -> dict[str, Path]:
        trades = self.load_liquidity_detail()
        summary_rows = []
        yearly_rows = []
        filtered_rows = []
        for scenario in self.scenarios:
            scenario_name = str(scenario["scenario"])
            allowed_buckets = {str(bucket) for bucket in scenario["allowed_buckets"]}
            selected, filtered = self.apply_scenario(trades, scenario_name, allowed_buckets)
            summary_rows.append(self.summarize_scenario(scenario_name, selected, filtered, allowed_buckets))
            yearly_rows.extend(self.build_yearly_rows(scenario_name, selected))
            filtered_rows.append(filtered)

        summary = pd.DataFrame(summary_rows).sort_values(
            ["total_compound_return", "max_drawdown", "executed_trade_count"],
            ascending=[False, True, False],
        )
        yearly = pd.DataFrame(yearly_rows)
        filtered_detail = pd.concat(filtered_rows, ignore_index=True) if filtered_rows else pd.DataFrame()

        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        filtered_detail.to_csv(self.output_filtered_detail_path, index=False, encoding="utf-8-sig")
        self.logger.info("A5-R1 1000万流动性过滤汇总已生成: %s", self.output_summary_path)
        self.logger.info("A5-R1 1000万流动性过滤年度报告已生成: %s", self.output_yearly_path)
        self.logger.info("A5-R1 1000万流动性过滤剔除明细已生成: %s, 行数: %s", self.output_filtered_detail_path, len(filtered_detail))
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "filtered_detail": self.output_filtered_detail_path,
        }

    def load_liquidity_detail(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_liquidity_detail_path,
            dtype={"trade_date": str, "ts_code": str, "exit_trade_date": str},
            low_memory=False,
        )
        required_columns = {
            "execution_state",
            "liquidity_bucket_10m",
            "liquidity_adjusted_daily_return_10m",
            "exit_trade_date",
        }
        missing = required_columns - set(trades.columns)
        if missing:
            raise RuntimeError(f"流动性明细缺少字段: {sorted(missing)}")
        numeric_columns = [
            "liquidity_adjusted_daily_return_10m",
            "current_daily_return",
            "planned_amount_to_buy_day_amount",
            "estimated_buy_slippage_rate_10m",
        ]
        for column in numeric_columns:
            if column in trades.columns:
                trades[column] = pd.to_numeric(trades[column], errors="coerce")
        return trades.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def apply_scenario(
        self,
        trades: pd.DataFrame,
        scenario_name: str,
        allowed_buckets: set[str],
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        executed = trades[trades["execution_state"].astype(str).eq("executed")].copy()
        selected = executed[executed["liquidity_bucket_10m"].astype(str).isin(allowed_buckets)].copy()
        filtered = executed[~executed["liquidity_bucket_10m"].astype(str).isin(allowed_buckets)].copy()
        filtered["scenario"] = scenario_name
        return selected, filtered

    def summarize_scenario(
        self,
        scenario_name: str,
        selected: pd.DataFrame,
        filtered: pd.DataFrame,
        allowed_buckets: set[str],
    ) -> dict[str, object]:
        returns = selected["liquidity_adjusted_daily_return_10m"].dropna()
        daily_returns = selected.groupby("exit_trade_date")["liquidity_adjusted_daily_return_10m"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        yearly_returns = self.calculate_yearly_returns(selected)
        return {
            "scenario": scenario_name,
            "allowed_buckets": ",".join(sorted(allowed_buckets)),
            "executed_trade_count": int(len(selected)),
            "filtered_trade_count": int(len(filtered)),
            "win_rate": self.win_rate(returns),
            "avg_daily_return": self.mean(returns),
            "median_daily_return": self.median(returns),
            "total_compound_return": self.compound_return(daily_returns),
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_profit": self.max_value(returns),
            "max_loss": self.min_value(returns),
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
            "year_count": len(yearly_returns),
            "positive_year_count": int(sum(value > 0 for value in yearly_returns.values())),
            "min_year_return": min(yearly_returns.values()) if yearly_returns else 0.0,
        }

    def build_yearly_rows(self, scenario_name: str, selected: pd.DataFrame) -> list[dict[str, object]]:
        rows = []
        sample = selected.copy()
        sample["year"] = sample["exit_trade_date"].astype(str).str[:4]
        for year, group in sample.groupby("year"):
            if not str(year).isdigit():
                continue
            returns = group["liquidity_adjusted_daily_return_10m"].dropna()
            daily_returns = group.groupby("exit_trade_date")["liquidity_adjusted_daily_return_10m"].sum().sort_index()
            equity_curve = (1 + daily_returns).cumprod()
            rows.append(
                {
                    "scenario": scenario_name,
                    "year": year,
                    "sample_count": int(len(returns)),
                    "year_return": self.compound_return(daily_returns),
                    "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
                    "win_rate": self.win_rate(returns),
                    "avg_daily_return": self.mean(returns),
                    "median_daily_return": self.median(returns),
                    "max_loss": self.min_value(returns),
                    "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
                }
            )
        return rows

    def calculate_yearly_returns(self, selected: pd.DataFrame) -> dict[str, float]:
        yearly_returns = {}
        sample = selected.copy()
        sample["year"] = sample["exit_trade_date"].astype(str).str[:4]
        for year, group in sample.groupby("year"):
            if not str(year).isdigit():
                continue
            daily_returns = group.groupby("exit_trade_date")["liquidity_adjusted_daily_return_10m"].sum().sort_index()
            yearly_returns[str(year)] = self.compound_return(daily_returns)
        return yearly_returns

    @staticmethod
    def win_rate(returns: pd.Series) -> float:
        return float((returns > 0).mean()) if len(returns) else 0.0

    @staticmethod
    def mean(returns: pd.Series) -> float:
        return float(returns.mean()) if len(returns) else 0.0

    @staticmethod
    def median(returns: pd.Series) -> float:
        return float(returns.median()) if len(returns) else 0.0

    @staticmethod
    def max_value(returns: pd.Series) -> float:
        return float(returns.max()) if len(returns) else 0.0

    @staticmethod
    def min_value(returns: pd.Series) -> float:
        return float(returns.min()) if len(returns) else 0.0

    @staticmethod
    def compound_return(returns: pd.Series) -> float:
        return float((1 + returns).prod() - 1) if len(returns) else 0.0


if __name__ == "__main__":
    main()
