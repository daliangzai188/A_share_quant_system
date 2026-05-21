from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.factors import NextDayPremiumAnalyzer
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按训练集、测试集、样本外拆分复核当前策略。")
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
    outputs = StrategySplitValidator(config_path=args.config).validate()
    print("策略样本拆分验证完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class StrategySplitValidator:
    """固定当前策略规则后，复核训练期、测试期和样本外表现。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("strategy_split_validator")
        self.validation_config = self.config.get("strategy_split_validation", {})
        self.input_trade_replay_path = self.project_root / self.validation_config.get(
            "input_trade_replay_path",
            "reports/trade_replay_report.csv",
        )
        self.output_summary_path = self.project_root / self.validation_config.get(
            "output_summary_path",
            "reports/strategy_split_validation_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.validation_config.get(
            "output_yearly_path",
            "reports/strategy_split_validation_yearly.csv",
        )
        self.output_rolling_path = self.project_root / self.validation_config.get(
            "output_rolling_path",
            "reports/strategy_split_validation_rolling.csv",
        )
        self.replay_rule = self.validation_config.get("replay_rule", "fixed_t2_close")
        self.initial_cash = float(self.validation_config.get("initial_cash", 1000000))
        self.splits = list(self.validation_config.get("splits", []))
        self.rolling_windows = [int(value) for value in self.validation_config.get("rolling_windows", [2, 3])]

    def validate(self) -> dict[str, Path]:
        trades = self.load_trades()
        summary_rows = []
        yearly_rows = []
        rolling_rows = []

        summary_rows.append(self.build_summary_row("all", "全样本复核", trades))
        yearly_rows.extend(self.build_yearly_rows("all", trades))
        rolling_rows.extend(self.build_rolling_rows("all", trades))

        for split in self.splits:
            split_name = str(split["split_name"])
            description = str(split.get("description", ""))
            split_trades = self.filter_year_range(
                trades=trades,
                start_year=str(split["start_year"]),
                end_year=str(split["end_year"]),
            )
            summary_rows.append(self.build_summary_row(split_name, description, split_trades))
            yearly_rows.extend(self.build_yearly_rows(split_name, split_trades))
            rolling_rows.extend(self.build_rolling_rows(split_name, split_trades))

        summary = pd.DataFrame(summary_rows)
        yearly = pd.DataFrame(yearly_rows)
        rolling = pd.DataFrame(rolling_rows)
        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        rolling.to_csv(self.output_rolling_path, index=False, encoding="utf-8-sig")
        self.logger.info("策略拆分汇总已生成: %s", self.output_summary_path)
        self.logger.info("策略拆分年度报告已生成: %s", self.output_yearly_path)
        self.logger.info("策略滚动窗口报告已生成: %s", self.output_rolling_path)
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "rolling": self.output_rolling_path,
        }

    def load_trades(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_trade_replay_path,
            dtype={"trade_date": str, "ts_code": str, "exit_trade_date": str},
            low_memory=False,
        )
        trades = trades[
            (trades["replay_rule"].astype(str) == self.replay_rule)
            & (trades["buy_executed"] == True)  # noqa: E712
            & (trades["sell_executed"] == True)  # noqa: E712
            & trades["daily_return"].notna()
            & trades["exit_trade_date"].notna()
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有可验证的成交样本: {self.replay_rule}")
        trades["daily_return"] = pd.to_numeric(trades["daily_return"], errors="coerce")
        trades["net_return"] = pd.to_numeric(trades["net_return"], errors="coerce")
        trades["year"] = trades["exit_trade_date"].astype(str).str[:4]
        return trades

    @staticmethod
    def filter_year_range(trades: pd.DataFrame, start_year: str, end_year: str) -> pd.DataFrame:
        return trades[(trades["year"] >= start_year) & (trades["year"] <= end_year)].copy()

    def build_summary_row(self, split_name: str, description: str, trades: pd.DataFrame) -> dict[str, object]:
        returns = trades["net_return"].dropna()
        daily_returns = trades.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        yearly_returns = self.calculate_yearly_returns(trades)
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        return {
            "split_name": split_name,
            "description": description,
            "start_year": min(yearly_returns) if yearly_returns else "",
            "end_year": max(yearly_returns) if yearly_returns else "",
            "sample_count": int(len(trades)),
            "trade_days": int(trades["exit_trade_date"].nunique()) if len(trades) else 0,
            "year_count": int(len(yearly_returns)),
            "positive_year_count": int(sum(value > 0 for value in yearly_returns.values())),
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
            "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
            "total_compound_return": self.compound_return(daily_returns),
            "final_equity": self.initial_cash * (1 + self.compound_return(daily_returns)),
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_profit_per_trade": float(returns.max()) if len(returns) else 0.0,
            "max_loss_per_trade": float(returns.min()) if len(returns) else 0.0,
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
        }

    def build_yearly_rows(self, split_name: str, trades: pd.DataFrame) -> list[dict[str, object]]:
        rows = []
        for year, group in trades.groupby("year"):
            returns = group["net_return"].dropna()
            daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            rows.append(
                {
                    "split_name": split_name,
                    "year": str(year),
                    "sample_count": int(len(group)),
                    "year_return": self.compound_return(daily_returns),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
                    "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
                }
            )
        return rows

    def build_rolling_rows(self, split_name: str, trades: pd.DataFrame) -> list[dict[str, object]]:
        yearly_returns = self.calculate_yearly_returns(trades)
        years = sorted(yearly_returns)
        rows = []
        for window in self.rolling_windows:
            if len(years) < window:
                continue
            for index in range(0, len(years) - window + 1):
                selected_years = years[index : index + window]
                value = 1.0
                for year in selected_years:
                    value *= 1 + yearly_returns[year]
                rows.append(
                    {
                        "split_name": split_name,
                        "window_years": int(window),
                        "start_year": selected_years[0],
                        "end_year": selected_years[-1],
                        "rolling_return": float(value - 1),
                    }
                )
        return rows

    def calculate_yearly_returns(self, trades: pd.DataFrame) -> dict[str, float]:
        yearly_returns = {}
        for year, group in trades.groupby("year"):
            daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            yearly_returns[str(year)] = self.compound_return(daily_returns)
        return dict(sorted(yearly_returns.items()))

    @staticmethod
    def compound_return(returns: pd.Series) -> float:
        return float((1 + returns).prod() - 1) if len(returns) else 0.0


if __name__ == "__main__":
    main()
