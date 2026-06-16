from __future__ import annotations

from itertools import combinations, product
from pathlib import Path

import pandas as pd

from src.factors import NextDayPremiumAnalyzer
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class RankingOptimizer:
    """在固定过滤条件下优化每日唯一候选的排序规则。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("ranking_optimizer")
        self.rank_config = self.config.get("ranking_optimization", {})
        self.input_trade_replay_path = self.project_root / self.rank_config.get(
            "input_trade_replay_path", "reports/trade_replay_report.csv"
        )
        self.output_report_path = self.project_root / self.rank_config.get(
            "output_report_path", "reports/ranking_optimization_report.csv"
        )
        self.output_yearly_path = self.project_root / self.rank_config.get(
            "output_yearly_path", "reports/ranking_optimization_yearly.csv"
        )
        self.replay_rule = self.rank_config.get("replay_rule", "fixed_t2_close")
        self.initial_cash = float(self.rank_config.get("initial_cash", 1000000))
        self.position_pct = float(self.rank_config.get("position_pct", 0.8))
        self.min_sample_count = int(self.rank_config.get("min_sample_count", 300))
        self.max_sort_factor_count = int(self.rank_config.get("max_sort_factor_count", 3))
        self.target_total_compound_return = float(self.rank_config.get("target_total_compound_return", 299.0))
        self.target_min_rolling_3y_return = float(self.rank_config.get("target_min_rolling_3y_return", 9.0))
        self.evaluation_years = [str(year) for year in self.rank_config.get("evaluation_years", [])]
        self.base_exclusions = dict(self.rank_config.get("base_exclusions", {}))
        self.candidate_sort_columns = list(self.rank_config.get("candidate_sort_columns", []))

    def optimize(self) -> dict[str, Path]:
        trades = self.load_trades()
        filtered = self.apply_base_exclusions(trades)
        sort_rules = list(self.generate_sort_rules(filtered))
        self.logger.info("开始扫描排序规则，过滤后样本: %s, 排序规则数量: %s", len(filtered), len(sort_rules))

        summary_rows = []
        yearly_rows = []
        for sort_columns, ascending in sort_rules:
            selected = self.select_daily_top(filtered, sort_columns, ascending)
            if len(selected) < self.min_sample_count:
                continue
            scenario_name = self.format_sort_rule(sort_columns, ascending)
            summary_rows.append(self.evaluate_selected(scenario_name, selected, sort_columns, ascending))
            yearly_rows.extend(self.build_yearly_rows(scenario_name, selected))

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
        mkdir_p(self.output_report_path.parent)
        report.to_csv(self.output_report_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        self.logger.info("排序优化报告已生成: %s, 行数: %s", self.output_report_path, len(report))
        self.logger.info("排序优化年度报告已生成: %s, 行数: %s", self.output_yearly_path, len(yearly))
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
            raise RuntimeError(f"没有可排序优化的样本: {self.replay_rule}")
        trades["daily_return"] = pd.to_numeric(trades["daily_return"], errors="coerce")
        trades["net_return"] = pd.to_numeric(trades["net_return"], errors="coerce")
        trades["year"] = trades["exit_trade_date"].astype(str).str[:4]
        for column in self.candidate_sort_columns:
            if column not in trades.columns:
                trades[column] = pd.NA
            trades[column] = pd.to_numeric(trades[column], errors="coerce")
        for column in self.base_exclusions:
            if column not in trades.columns:
                trades[column] = "missing"
            trades[column] = trades[column].fillna("missing").astype(str)
        return trades

    def apply_base_exclusions(self, trades: pd.DataFrame) -> pd.DataFrame:
        filtered = trades.copy()
        for column, values in self.base_exclusions.items():
            filtered = filtered[~filtered[column].astype(str).isin([str(value) for value in values])].copy()
        return filtered

    def generate_sort_rules(self, trades: pd.DataFrame) -> list[tuple[list[str], list[bool]]]:
        available_columns = [
            column
            for column in self.candidate_sort_columns
            if column in trades.columns and trades[column].notna().any()
        ]
        rules = []
        for count in range(1, self.max_sort_factor_count + 1):
            for columns in combinations(available_columns, count):
                for ascending_flags in product([True, False], repeat=count):
                    rules.append((list(columns), list(ascending_flags)))
        return rules

    @staticmethod
    def select_daily_top(trades: pd.DataFrame, sort_columns: list[str], ascending: list[bool]) -> pd.DataFrame:
        sort_by = ["trade_date"] + sort_columns
        sort_ascending = [True] + ascending
        selected = trades.sort_values(sort_by, ascending=sort_ascending).groupby("trade_date").head(1).copy()
        selected["selected_rank"] = 1
        selected["position_pct"] = selected["daily_return"].map(lambda _: 0.8)
        return selected

    def evaluate_selected(
        self,
        scenario_name: str,
        selected: pd.DataFrame,
        sort_columns: list[str],
        ascending: list[bool],
    ) -> dict[str, object]:
        daily_returns = selected.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        yearly_returns = self.calculate_yearly_returns(selected)
        rolling_3y = self.calculate_rolling_year_returns(yearly_returns)
        returns = selected["daily_return"].dropna()
        total_compound_return = self.compound_return(daily_returns)
        min_rolling_3y_return = min(rolling_3y.values()) if rolling_3y else 0.0
        max_rolling_3y_return = max(rolling_3y.values()) if rolling_3y else 0.0
        return {
            "scenario": scenario_name,
            "sort_columns": ",".join(sort_columns),
            "ascending": ",".join(str(value) for value in ascending),
            "sample_count": int(len(selected)),
            "signal_days": int(selected["trade_date"].nunique()),
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

    def calculate_yearly_returns(self, selected: pd.DataFrame) -> dict[str, float]:
        yearly_returns = {}
        for year, group in selected.groupby("year"):
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

    def build_yearly_rows(self, scenario_name: str, selected: pd.DataFrame) -> list[dict[str, object]]:
        rows = []
        for year, group in selected.groupby("year"):
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
    def format_sort_rule(sort_columns: list[str], ascending: list[bool]) -> str:
        parts = []
        for column, asc in zip(sort_columns, ascending):
            parts.append(f"{column}_{'asc' if asc else 'desc'}")
        return "sort_" + ";".join(parts)
