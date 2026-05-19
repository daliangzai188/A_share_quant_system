from __future__ import annotations

from itertools import combinations, product
from pathlib import Path

import pandas as pd

from src.factors import FactorAnalyzer, NextDayPremiumAnalyzer
from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger


class StrategyConditionOptimizer:
    """扫描涨停候选因子组合，评估 70% 总仓位下的年度稳定性。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("strategy_optimizer")
        optimization_config = self.config.get("optimization", {})

        self.input_trades_path = self.project_root / optimization_config.get(
            "input_trades_path", "data/processed/next_day_premium_trades.csv"
        )
        self.output_report_path = self.project_root / optimization_config.get(
            "output_report_path", "reports/strategy_optimization_report.csv"
        )
        self.output_yearly_path = self.project_root / optimization_config.get(
            "output_yearly_path", "reports/strategy_optimization_yearly.csv"
        )
        self.factor_columns = optimization_config.get(
            "factor_columns",
            [
                "market_sentiment_level",
                "board_type",
                "first_time_bucket",
                "limit_times_bucket",
                "amount_bucket",
                "turnover_rate_bucket",
                "fd_ratio_bucket",
            ],
        )
        self.min_factor_count = int(optimization_config.get("min_factor_count", 1))
        self.max_factor_count = int(optimization_config.get("max_factor_count", 4))
        self.min_sample_count = int(optimization_config.get("min_sample_count", 80))
        self.max_holding_count = int(optimization_config.get("max_holding_count", 5))
        self.max_total_position_pct = float(optimization_config.get("max_total_position_pct", 0.7))
        self.evaluation_years = [str(year) for year in optimization_config.get("evaluation_years", [])]
        self.target_annual_return = float(optimization_config.get("target_annual_return", 2.0))
        self.position_pct_per_trade = self.max_total_position_pct / self.max_holding_count

    def optimize(self) -> dict[str, Path]:
        trades = self.load_trades()
        condition_sets = list(self.generate_condition_sets(trades))
        self.logger.info("开始扫描策略组合，组合数量: %s", len(condition_sets))

        summary_rows = []
        yearly_rows = []
        for index, conditions in enumerate(condition_sets, start=1):
            matched = self.apply_conditions(trades, conditions)
            if len(matched) < self.min_sample_count:
                continue

            selected = self.select_daily_candidates(matched)
            if len(selected) < self.min_sample_count:
                continue

            summary = self.evaluate_selected(selected, conditions)
            summary_rows.append(summary)
            yearly_rows.extend(self.build_yearly_rows(selected, summary["condition_name"]))

            if index % 1000 == 0:
                self.logger.info("策略组合扫描进度: %s/%s", index, len(condition_sets))

        if not summary_rows:
            raise RuntimeError("没有找到满足最小样本数的策略组合。")

        report = pd.DataFrame(summary_rows)
        yearly_report = pd.DataFrame(yearly_rows)
        report = report.sort_values(
            [
                "target_year_count",
                "positive_year_count",
                "min_year_return",
                "total_compound_return",
                "sample_count",
            ],
            ascending=[False, False, False, False, False],
        )

        self.output_report_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_yearly_path.parent.mkdir(parents=True, exist_ok=True)
        report.to_csv(self.output_report_path, index=False, encoding="utf-8-sig")
        yearly_report.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")

        self.logger.info("策略组合优化报告已生成: %s, 行数: %s", self.output_report_path, len(report))
        self.logger.info("策略组合年度报告已生成: %s, 行数: %s", self.output_yearly_path, len(yearly_report))
        return {"report": self.output_report_path, "yearly": self.output_yearly_path}

    def load_trades(self) -> pd.DataFrame:
        trades = pd.read_csv(self.input_trades_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        trades = FactorAnalyzer(config_path="config/config.json").add_factor_buckets(trades)
        trades = trades[
            (trades["allow_buy_reliable"] == True)  # noqa: E712
            & (trades["is_fill_score_reliable"] == True)  # noqa: E712
            & (trades["is_fd_amount_abnormal"] == False)  # noqa: E712
            & trades["net_return"].notna()
            & trades["exit_trade_date"].notna()
        ].copy()
        for column in self.factor_columns:
            trades[column] = trades[column].astype(str)
        return trades

    def generate_condition_sets(self, trades: pd.DataFrame) -> list[dict[str, str]]:
        values_by_factor = {
            factor: sorted(value for value in trades[factor].dropna().astype(str).unique() if value != "nan")
            for factor in self.factor_columns
        }
        conditions = []
        for factor_count in range(self.min_factor_count, self.max_factor_count + 1):
            for selected_factors in combinations(self.factor_columns, factor_count):
                value_lists = [values_by_factor[factor] for factor in selected_factors]
                for selected_values in product(*value_lists):
                    conditions.append(dict(zip(selected_factors, selected_values)))
        return conditions

    @staticmethod
    def apply_conditions(data: pd.DataFrame, conditions: dict[str, str]) -> pd.DataFrame:
        result = data
        for column, value in conditions.items():
            result = result[result[column] == value]
        return result.copy()

    def select_daily_candidates(self, trades: pd.DataFrame) -> pd.DataFrame:
        sort_columns = ["trade_date", "fill_probability", "sample_count", "amount", "turnover_rate"]
        existing_sort_columns = [column for column in sort_columns if column in trades.columns]
        ascending = [True] + [False] * (len(existing_sort_columns) - 1)
        selected = trades.sort_values(existing_sort_columns, ascending=ascending)
        selected = selected.groupby("trade_date").head(self.max_holding_count).copy()
        selected["selected_rank"] = selected.groupby("trade_date").cumcount() + 1
        selected["position_pct"] = self.position_pct_per_trade
        selected["weighted_return"] = selected["net_return"] * selected["position_pct"]
        return selected

    def evaluate_selected(self, selected: pd.DataFrame, conditions: dict[str, str]) -> dict[str, object]:
        returns = selected["net_return"].dropna()
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        daily_returns = self.build_daily_returns(selected)
        equity_curve = (1 + daily_returns["daily_return"]).cumprod()
        yearly_returns = self.calculate_yearly_returns(daily_returns)
        min_year_return = min(yearly_returns.values()) if yearly_returns else 0.0
        positive_year_count = sum(value > 0 for value in yearly_returns.values())
        negative_year_count = sum(value < 0 for value in yearly_returns.values())
        target_year_count = sum(value >= self.target_annual_return for value in yearly_returns.values())

        return {
            "condition_name": self.format_conditions(conditions),
            "factor_count": len(conditions),
            "sample_count": int(len(selected)),
            "signal_days": int(selected["trade_date"].nunique()),
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
            "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
            "total_compound_return": float(equity_curve.iloc[-1] - 1) if len(equity_curve) else 0.0,
            "avg_year_return": float(sum(yearly_returns.values()) / len(yearly_returns)) if yearly_returns else 0.0,
            "min_year_return": float(min_year_return),
            "max_year_return": float(max(yearly_returns.values())) if yearly_returns else 0.0,
            "positive_year_count": int(positive_year_count),
            "negative_year_count": int(negative_year_count),
            "target_year_count": int(target_year_count),
            "target_annual_return": self.target_annual_return,
            "max_drawdown": float(NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve)),
            "max_profit_per_trade": float(returns.max()) if len(returns) else 0.0,
            "max_loss_per_trade": float(returns.min()) if len(returns) else 0.0,
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": int(NextDayPremiumAnalyzer.max_consecutive_losses(returns)),
            "max_total_position_pct": self.max_total_position_pct,
            "max_holding_count": self.max_holding_count,
            "position_pct_per_trade": self.position_pct_per_trade,
        }

    def build_yearly_rows(self, selected: pd.DataFrame, condition_name: str) -> list[dict[str, object]]:
        daily_returns = self.build_daily_returns(selected)
        yearly_returns = self.calculate_yearly_returns(daily_returns)
        rows = []
        selected = selected.copy()
        selected["year"] = selected["trade_date"].astype(str).str[:4]
        for year in self.evaluation_years:
            year_trades = selected[selected["year"] == year]
            returns = year_trades["net_return"].dropna()
            rows.append(
                {
                    "condition_name": condition_name,
                    "year": year,
                    "sample_count": int(len(year_trades)),
                    "signal_days": int(year_trades["trade_date"].nunique()) if not year_trades.empty else 0,
                    "year_return": float(yearly_returns.get(year, 0.0)),
                    "is_positive": bool(yearly_returns.get(year, 0.0) > 0),
                    "is_target_reached": bool(yearly_returns.get(year, 0.0) >= self.target_annual_return),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
                    "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
                }
            )
        return rows

    def build_daily_returns(self, selected: pd.DataFrame) -> pd.DataFrame:
        daily_returns = selected.groupby("exit_trade_date")["weighted_return"].sum().reset_index()
        daily_returns = daily_returns.rename(columns={"exit_trade_date": "trade_date", "weighted_return": "daily_return"})
        daily_returns["year"] = daily_returns["trade_date"].astype(str).str[:4]
        return daily_returns.sort_values("trade_date")

    def calculate_yearly_returns(self, daily_returns: pd.DataFrame) -> dict[str, float]:
        returns = {}
        for year in self.evaluation_years:
            year_data = daily_returns[daily_returns["year"] == year]
            if year_data.empty:
                returns[year] = 0.0
            else:
                returns[year] = float((1 + year_data["daily_return"]).prod() - 1)
        return returns

    @staticmethod
    def format_conditions(conditions: dict[str, str]) -> str:
        return ";".join(f"{column}={value}" for column, value in conditions.items())
