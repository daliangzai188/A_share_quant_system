from __future__ import annotations

from itertools import combinations, product
from pathlib import Path

import pandas as pd

from src.factors import NextDayPremiumAnalyzer
from src.strategy_optimizer import StrategyConditionOptimizer
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger


class CandidateRankingOptimizer:
    """从原始候选池优化每日 top1 排序，并用日线保守成交回放复核。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config_path = config_path
        self.config = load_json_config(config_path)
        self.logger = get_logger("candidate_ranking_optimizer")
        self.rank_config = self.config.get("candidate_ranking_optimization", {})

        self.output_report_path = self.project_root / self.rank_config.get(
            "output_report_path", "reports/candidate_ranking_optimization_report.csv"
        )
        self.output_yearly_path = self.project_root / self.rank_config.get(
            "output_yearly_path", "reports/candidate_ranking_optimization_yearly.csv"
        )
        self.replay_rule_name = self.rank_config.get("replay_rule", "fixed_t2_close")
        self.replay_max_hold_days = int(self.rank_config.get("replay_max_hold_days", 2))
        self.replay_exit_price_field = self.rank_config.get("replay_exit_price_field", "close")
        self.initial_cash = float(self.rank_config.get("initial_cash", 1000000))
        self.position_pct = float(self.rank_config.get("position_pct", 0.8))
        self.min_sample_count = int(self.rank_config.get("min_sample_count", 900))
        self.min_signal_days = int(self.rank_config.get("min_signal_days", 1200))
        self.max_sort_factor_count = int(self.rank_config.get("max_sort_factor_count", 3))
        self.target_total_compound_return = float(self.rank_config.get("target_total_compound_return", 299.0))
        self.target_min_rolling_3y_return = float(self.rank_config.get("target_min_rolling_3y_return", 9.0))
        self.evaluation_years = [str(year) for year in self.rank_config.get("evaluation_years", [])]
        self.base_inclusions = dict(self.rank_config.get("base_inclusions", {}))
        self.base_exclusions = dict(self.rank_config.get("base_exclusions", {}))
        self.candidate_sort_columns = list(self.rank_config.get("candidate_sort_columns", []))

    def optimize(self) -> dict[str, Path]:
        candidates = self.load_candidates()
        filtered = self.apply_base_filters(candidates)
        sort_rules = self.generate_sort_rules(filtered)
        self.logger.info(
            "开始候选池级排序优化，过滤后候选: %s, 交易日: %s, 日均候选: %.2f, 排序规则: %s",
            len(filtered),
            filtered["trade_date"].nunique(),
            self.average_candidates_per_day(filtered),
            len(sort_rules),
        )

        replayed_candidates = self.replay_all_candidates(filtered)

        summary_rows = []
        yearly_rows = []
        for index, (sort_columns, ascending) in enumerate(sort_rules, start=1):
            selected = self.select_daily_top(replayed_candidates, sort_columns, ascending)
            if len(selected) < self.min_signal_days:
                continue

            executed = self.select_executed_trades(selected)
            if len(executed) < self.min_sample_count:
                continue

            scenario_name = self.format_sort_rule(sort_columns, ascending)
            summary_rows.append(
                self.evaluate_scenario(
                    scenario_name=scenario_name,
                    sort_columns=sort_columns,
                    ascending=ascending,
                    filtered_candidates=filtered,
                    selected=selected,
                    executed=executed,
                )
            )
            yearly_rows.extend(self.build_yearly_rows(scenario_name, executed))

            if index % 250 == 0:
                self.logger.info("候选池排序扫描进度: %s/%s", index, len(sort_rules))

        if not summary_rows:
            raise RuntimeError("没有找到满足最小样本数的候选池排序规则。")

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
        self.logger.info("候选池排序优化报告已生成: %s, 行数: %s", self.output_report_path, len(report))
        self.logger.info("候选池排序年度报告已生成: %s, 行数: %s", self.output_yearly_path, len(yearly))
        return {
            "report": self.output_report_path,
            "yearly": self.output_yearly_path,
        }

    def load_candidates(self) -> pd.DataFrame:
        optimizer = StrategyConditionOptimizer(
            config_path=self.config_path,
            optimization_config_key="candidate_ranking_optimization",
        )
        candidates = optimizer.load_trades()
        candidates["planned_position_pct"] = self.position_pct
        for column in self.candidate_sort_columns:
            if column not in candidates.columns:
                candidates[column] = pd.NA
            candidates[column] = pd.to_numeric(candidates[column], errors="coerce")
        for column in list(self.base_inclusions) + list(self.base_exclusions):
            if column not in candidates.columns:
                candidates[column] = "missing"
            candidates[column] = candidates[column].fillna("missing").astype(str)
        if candidates.empty:
            raise RuntimeError("候选池为空，请先检查次日溢价样本和成交概率过滤。")
        return candidates

    def apply_base_filters(self, candidates: pd.DataFrame) -> pd.DataFrame:
        filtered = candidates.copy()
        for column, values in self.base_inclusions.items():
            allowed = {str(value) for value in values}
            filtered = filtered[filtered[column].astype(str).isin(allowed)].copy()
        for column, values in self.base_exclusions.items():
            excluded = {str(value) for value in values}
            filtered = filtered[~filtered[column].astype(str).isin(excluded)].copy()
        if filtered.empty:
            raise RuntimeError("基础过滤后候选池为空，请放宽 candidate_ranking_optimization 过滤条件。")
        return filtered

    def generate_sort_rules(self, candidates: pd.DataFrame) -> list[tuple[list[str], list[bool]]]:
        available_columns = [
            column
            for column in self.candidate_sort_columns
            if column in candidates.columns and candidates[column].notna().any()
        ]
        rules = []
        for count in range(1, self.max_sort_factor_count + 1):
            for columns in combinations(available_columns, count):
                for ascending_flags in product([True, False], repeat=count):
                    rules.append((list(columns), list(ascending_flags)))
        if not rules:
            raise RuntimeError("没有可用排序字段，请检查 candidate_sort_columns。")
        return rules

    def replay_all_candidates(self, candidates: pd.DataFrame) -> pd.DataFrame:
        replay_engine = ConservativeTradeReplay(config_path=self.config_path)
        replay_engine.position_pct = self.position_pct
        forward_prices = replay_engine.load_forward_prices()
        replay_rule = ReplayRule(
            rule_name=self.replay_rule_name,
            max_hold_days=self.replay_max_hold_days,
            exit_price_field=self.replay_exit_price_field,
        )
        replayed = self.replay_selected(candidates, forward_prices, replay_engine, replay_rule)
        replayed["position_pct"] = self.position_pct
        self.logger.info(
            "候选池保守成交预回放完成，候选: %s, 可买可卖样本: %s",
            len(replayed),
            len(self.select_executed_trades(replayed)),
        )
        return replayed

    def select_daily_top(self, candidates: pd.DataFrame, sort_columns: list[str], ascending: list[bool]) -> pd.DataFrame:
        selected = candidates.sort_values(
            ["trade_date"] + sort_columns,
            ascending=[True] + ascending,
            na_position="last",
        )
        selected = selected.groupby("trade_date").head(1).copy()
        selected["selected_rank"] = 1
        selected["position_pct"] = self.position_pct
        return selected

    @staticmethod
    def select_executed_trades(trades: pd.DataFrame) -> pd.DataFrame:
        return trades[
            (trades["buy_executed"] == True)  # noqa: E712
            & (trades["sell_executed"] == True)  # noqa: E712
            & trades["daily_return"].notna()
        ].copy()

    @staticmethod
    def replay_selected(
        selected: pd.DataFrame,
        forward_prices: pd.DataFrame,
        replay_engine: ConservativeTradeReplay,
        replay_rule: ReplayRule,
    ) -> pd.DataFrame:
        samples = selected.merge(forward_prices, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
        return replay_engine.replay_rule(samples, replay_rule)

    def evaluate_scenario(
        self,
        scenario_name: str,
        sort_columns: list[str],
        ascending: list[bool],
        filtered_candidates: pd.DataFrame,
        selected: pd.DataFrame,
        executed: pd.DataFrame,
    ) -> dict[str, object]:
        daily_returns = executed.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        yearly_returns = self.calculate_yearly_returns(executed)
        rolling_3y = self.calculate_rolling_year_returns(yearly_returns)
        returns = executed["net_return"].dropna()
        total_compound_return = self.compound_return(daily_returns)
        min_rolling_3y_return = min(rolling_3y.values()) if rolling_3y else 0.0
        max_rolling_3y_return = max(rolling_3y.values()) if rolling_3y else 0.0
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        buy_rejected = selected[selected["buy_executed"] == False]  # noqa: E712
        sell_unresolved = selected[
            (selected["buy_executed"] == True)  # noqa: E712
            & (selected["sell_executed"] == False)  # noqa: E712
        ]
        return {
            "scenario": scenario_name,
            "sort_columns": ",".join(sort_columns),
            "ascending": ",".join(str(value) for value in ascending),
            "filtered_candidate_count": int(len(filtered_candidates)),
            "filtered_candidate_days": int(filtered_candidates["trade_date"].nunique()),
            "avg_candidates_per_day": self.average_candidates_per_day(filtered_candidates),
            "selected_signal_count": int(len(selected)),
            "selected_signal_days": int(selected["trade_date"].nunique()),
            "sample_count": int(len(executed)),
            "executed_days": int(executed["exit_trade_date"].nunique()),
            "buy_rejected_count": int(len(buy_rejected)),
            "sell_unresolved_count": int(len(sell_unresolved)),
            "path_conflict_count": int(selected["path_conflict"].sum()) if "path_conflict" in selected.columns else 0,
            "limit_down_blocked_trades": int((selected["limit_down_blocked_days"] > 0).sum())
            if "limit_down_blocked_days" in selected.columns
            else 0,
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
            "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
            "total_compound_return": total_compound_return,
            "final_equity": self.initial_cash * (1 + total_compound_return),
            "min_rolling_3y_return": min_rolling_3y_return,
            "max_rolling_3y_return": max_rolling_3y_return,
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_profit_per_trade": float(returns.max()) if len(returns) else 0.0,
            "max_loss_per_trade": float(returns.min()) if len(returns) else 0.0,
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
            "return_2022": yearly_returns.get("2022", 0.0),
            "return_2026": yearly_returns.get("2026", 0.0),
            "hit_total_target": total_compound_return >= self.target_total_compound_return,
            "hit_rolling_3y_target": min_rolling_3y_return >= self.target_min_rolling_3y_return,
        }

    def calculate_yearly_returns(self, executed: pd.DataFrame) -> dict[str, float]:
        yearly_returns = {}
        data = executed.copy()
        data["year"] = data["exit_trade_date"].astype(str).str[:4]
        for year, group in data.groupby("year"):
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

    def build_yearly_rows(self, scenario_name: str, executed: pd.DataFrame) -> list[dict[str, object]]:
        rows = []
        data = executed.copy()
        data["year"] = data["exit_trade_date"].astype(str).str[:4]
        for year, group in data.groupby("year"):
            if self.evaluation_years and str(year) not in self.evaluation_years:
                continue
            returns = group["net_return"].dropna()
            daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            rows.append(
                {
                    "scenario": scenario_name,
                    "year": str(year),
                    "sample_count": int(len(group)),
                    "year_return": self.compound_return(daily_returns),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
                    "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
                }
            )
        return rows

    @staticmethod
    def compound_return(returns: pd.Series) -> float:
        return float((1 + returns).prod() - 1) if len(returns) else 0.0

    @staticmethod
    def average_candidates_per_day(candidates: pd.DataFrame) -> float:
        if candidates.empty:
            return 0.0
        return float(candidates.groupby("trade_date")["ts_code"].count().mean())

    @staticmethod
    def format_sort_rule(sort_columns: list[str], ascending: list[bool]) -> str:
        parts = []
        for column, asc in zip(sort_columns, ascending):
            parts.append(f"{column}_{'asc' if asc else 'desc'}")
        return "candidate_sort_" + ";".join(parts)
