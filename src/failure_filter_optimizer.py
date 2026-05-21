from __future__ import annotations

from itertools import combinations
from pathlib import Path

import pandas as pd

from src.factors import NextDayPremiumAnalyzer
from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger


class FailureFilterOptimizer:
    """在保守成交回放样本上扫描危险条件排除组合。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("failure_filter_optimizer")
        self.opt_config = self.config.get("failure_filter_optimization", {})
        self.input_trade_replay_path = self.project_root / self.opt_config.get(
            "input_trade_replay_path", "reports/trade_replay_report.csv"
        )
        self.output_report_path = self.project_root / self.opt_config.get(
            "output_report_path", "reports/failure_filter_optimization_report.csv"
        )
        self.output_yearly_path = self.project_root / self.opt_config.get(
            "output_yearly_path", "reports/failure_filter_optimization_yearly.csv"
        )
        self.replay_rule = self.opt_config.get("replay_rule", "fixed_t2_close")
        self.initial_cash = float(self.opt_config.get("initial_cash", 1000000))
        self.min_remaining_samples = int(self.opt_config.get("min_remaining_samples", 300))
        self.min_excluded_samples = int(self.opt_config.get("min_excluded_samples", 10))
        self.max_filter_count = int(self.opt_config.get("max_filter_count", 3))
        self.target_total_compound_return = float(self.opt_config.get("target_total_compound_return", 99.0))
        self.target_min_rolling_3y_return = float(self.opt_config.get("target_min_rolling_3y_return", 9.0))
        self.evaluation_years = [str(year) for year in self.opt_config.get("evaluation_years", [])]
        self.factor_columns = list(self.opt_config.get("factor_columns", []))

    def optimize(self) -> dict[str, Path]:
        trades = self.load_trades()
        candidates = self.build_exclusion_candidates(trades)
        self.logger.info("开始扫描危险条件过滤组合，候选条件数量: %s", len(candidates))

        summary_rows = [self.evaluate_scenario("base_no_filter", trades, [])]
        yearly_rows = self.build_yearly_rows("base_no_filter", trades)

        for count in range(1, self.max_filter_count + 1):
            for combo in combinations(candidates, count):
                if self.has_duplicate_condition(combo):
                    continue
                filtered = self.apply_exclusions(trades, combo)
                if len(filtered) < self.min_remaining_samples:
                    continue
                scenario_name = self.format_combo_name(combo)
                summary_rows.append(self.evaluate_scenario(scenario_name, filtered, combo))
                yearly_rows.extend(self.build_yearly_rows(scenario_name, filtered))

        report = pd.DataFrame(summary_rows)
        yearly = pd.DataFrame(yearly_rows)
        report = report.sort_values(
            [
                "hit_total_target",
                "hit_rolling_3y_target",
                "total_compound_return",
                "min_rolling_3y_return",
                "max_drawdown",
            ],
            ascending=[False, False, False, False, True],
        )

        self.output_report_path.parent.mkdir(parents=True, exist_ok=True)
        report.to_csv(self.output_report_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        self.logger.info("危险条件过滤优化报告已生成: %s, 行数: %s", self.output_report_path, len(report))
        self.logger.info("危险条件过滤年度报告已生成: %s, 行数: %s", self.output_yearly_path, len(yearly))
        return {
            "report": self.output_report_path,
            "yearly": self.output_yearly_path,
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
            & trades["exit_trade_date"].notna()
            & trades["daily_return"].notna()
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有可优化的保守成交交易样本: {self.replay_rule}")
        trades["daily_return"] = pd.to_numeric(trades["daily_return"], errors="coerce")
        trades["net_return"] = pd.to_numeric(trades["net_return"], errors="coerce")
        trades["year"] = trades["exit_trade_date"].astype(str).str[:4]
        for column in self.factor_columns:
            if column not in trades.columns:
                trades[column] = "missing"
            trades[column] = trades[column].fillna("missing").astype(str)
        return trades

    def build_exclusion_candidates(self, trades: pd.DataFrame) -> list[tuple[str, str]]:
        candidates = []
        for factor in self.factor_columns:
            counts = trades[factor].value_counts(dropna=False)
            for value, count in counts.items():
                if int(count) >= self.min_excluded_samples:
                    candidates.append((factor, str(value)))
        return candidates

    @staticmethod
    def has_duplicate_condition(combo: tuple[tuple[str, str], ...]) -> bool:
        return len(set(combo)) != len(combo)

    @staticmethod
    def apply_exclusions(trades: pd.DataFrame, combo: tuple[tuple[str, str], ...]) -> pd.DataFrame:
        mask = pd.Series(True, index=trades.index)
        for factor, value in combo:
            mask &= trades[factor].astype(str) != str(value)
        return trades[mask].copy()

    def evaluate_scenario(
        self,
        scenario_name: str,
        trades: pd.DataFrame,
        combo: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        daily_returns = trades.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        yearly_returns = self.calculate_yearly_returns(trades)
        rolling_3y = self.calculate_rolling_year_returns(yearly_returns, window=3)
        returns = trades["daily_return"].dropna()
        total_compound_return = self.compound_return(daily_returns)
        min_rolling_3y_return = min(rolling_3y.values()) if rolling_3y else 0.0
        max_rolling_3y_return = max(rolling_3y.values()) if rolling_3y else 0.0
        return {
            "scenario": scenario_name,
            "excluded_conditions": self.format_excluded_conditions(combo),
            "excluded_condition_count": len(combo),
            "sample_count": int(len(trades)),
            "year_count": int(len(yearly_returns)),
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "avg_daily_return": float(returns.mean()) if len(returns) else 0.0,
            "median_daily_return": float(returns.median()) if len(returns) else 0.0,
            "total_compound_return": total_compound_return,
            "final_equity": self.initial_cash * (1 + total_compound_return),
            "min_rolling_3y_return": min_rolling_3y_return,
            "max_rolling_3y_return": max_rolling_3y_return,
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_loss": float(returns.min()) if len(returns) else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
            "return_2022": yearly_returns.get("2022", 0.0),
            "return_2026": yearly_returns.get("2026", 0.0),
            "hit_total_target": total_compound_return >= self.target_total_compound_return,
            "hit_rolling_3y_target": min_rolling_3y_return >= self.target_min_rolling_3y_return,
        }

    def calculate_yearly_returns(self, trades: pd.DataFrame) -> dict[str, float]:
        yearly_returns = {}
        for year, group in trades.groupby("year"):
            if self.evaluation_years and str(year) not in self.evaluation_years:
                continue
            daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            yearly_returns[str(year)] = self.compound_return(daily_returns)
        return dict(sorted(yearly_returns.items()))

    @staticmethod
    def calculate_rolling_year_returns(yearly_returns: dict[str, float], window: int = 3) -> dict[str, float]:
        years = sorted(yearly_returns)
        rolling = {}
        for index in range(0, len(years) - window + 1):
            selected_years = years[index : index + window]
            value = 1.0
            for year in selected_years:
                value *= 1 + yearly_returns[year]
            rolling["-".join(selected_years)] = value - 1
        return rolling

    def build_yearly_rows(self, scenario_name: str, trades: pd.DataFrame) -> list[dict[str, object]]:
        rows = []
        for year, group in trades.groupby("year"):
            if self.evaluation_years and str(year) not in self.evaluation_years:
                continue
            returns = group["daily_return"].dropna()
            daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            rows.append(
                {
                    "scenario": scenario_name,
                    "year": str(year),
                    "sample_count": int(len(group)),
                    "year_return": self.compound_return(daily_returns),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_daily_return": float(returns.mean()) if len(returns) else 0.0,
                    "median_daily_return": float(returns.median()) if len(returns) else 0.0,
                }
            )
        return rows

    @staticmethod
    def compound_return(returns: pd.Series) -> float:
        return float((1 + returns).prod() - 1) if len(returns) else 0.0

    @staticmethod
    def format_combo_name(combo: tuple[tuple[str, str], ...]) -> str:
        return "exclude_" + ";".join(f"{factor}={value}" for factor, value in combo)

    @staticmethod
    def format_excluded_conditions(combo: list[tuple[str, str]] | tuple[tuple[str, str], ...]) -> str:
        return ";".join(f"{factor}={value}" for factor, value in combo)
