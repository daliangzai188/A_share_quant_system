from __future__ import annotations

import argparse
import sys
from itertools import combinations
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.factors import NextDayPremiumAnalyzer
from src.strategy_optimizer import StrategyConditionOptimizer
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="围绕市场热度因子自动扩因子、缩因子和过滤组合。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--max-condition-count", type=int, default=None, help="最多组合几个市场热度条件。")
    parser.add_argument("--max-scenarios", type=int, default=None, help="最多评估多少个场景。")
    parser.add_argument("--max-candidates-per-mode", type=int, default=None, help="每种搜索模式最多保留多少个候选条件。")
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
    outputs = MarketHeatFactorOptimizer(args=args, config=config).optimize()
    print("市场热度因子优化完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class MarketHeatFactorOptimizer:
    """只针对市场热度相关因子做自动扩展、收缩和过滤验证。"""

    def __init__(self, args: argparse.Namespace, config: dict[str, object]) -> None:
        self.args = args
        self.config = config
        self.logger = get_logger("market_heat_factor_optimizer")
        self.opt_config = config.get("market_heat_optimization", {})
        self.replay_config = config.get("trade_replay", {})
        self.initial_cash = float(self.opt_config.get("initial_cash", 1000000))
        self.position_pct = float(self.opt_config.get("position_pct", 0.8))
        self.min_remaining_samples = int(self.opt_config.get("min_remaining_samples", 250))
        self.min_condition_samples = int(self.opt_config.get("min_condition_samples", 12))
        self.max_condition_count = int(
            args.max_condition_count
            if args.max_condition_count is not None
            else self.opt_config.get("max_condition_count", 2)
        )
        self.max_candidates_per_mode = int(
            args.max_candidates_per_mode
            if args.max_candidates_per_mode is not None
            else self.opt_config.get("max_candidates_per_mode", 40)
        )
        self.max_scenarios = int(
            args.max_scenarios
            if args.max_scenarios is not None
            else self.opt_config.get("max_scenarios", 5000)
        )
        self.evaluation_years = [str(year) for year in self.opt_config.get("evaluation_years", [])]
        self.factor_columns = list(self.opt_config.get("factor_columns", []))
        self.output_report_path = PROJECT_ROOT / self.opt_config.get(
            "output_report_path",
            "reports/market_heat_factor_optimization_report.csv",
        )
        self.output_yearly_path = PROJECT_ROOT / self.opt_config.get(
            "output_yearly_path",
            "reports/market_heat_factor_optimization_yearly.csv",
        )
        self.output_diagnostics_path = PROJECT_ROOT / self.opt_config.get(
            "output_diagnostics_path",
            "reports/market_heat_factor_diagnostics.csv",
        )

    def optimize(self) -> dict[str, Path]:
        optimizer = StrategyConditionOptimizer(config_path=self.args.config)
        base_candidates = self.load_base_candidates(optimizer)
        replayed_candidates = self.replay_candidates(base_candidates)
        selected_without_post = optimizer.select_daily_candidates(replayed_candidates, max_holding_count=1)
        baseline_selected = self.apply_post_exclusions(selected_without_post, self.current_post_exclusions())

        rows = []
        yearly_rows = []
        diagnostics = self.build_diagnostics(baseline_selected)
        rows.append(self.evaluate_scenario("baseline_current_a2", "baseline", (), baseline_selected))
        yearly_rows.extend(self.build_yearly_rows("baseline_current_a2", baseline_selected))

        scenario_count = 1
        scenario_count = self.evaluate_shrink_scenarios(
            rows=rows,
            yearly_rows=yearly_rows,
            selected_without_post=selected_without_post,
            scenario_count=scenario_count,
        )
        scenario_count = self.evaluate_condition_scenarios(
            rows=rows,
            yearly_rows=yearly_rows,
            replayed_candidates=replayed_candidates,
            selected_without_post=selected_without_post,
            baseline_selected=baseline_selected,
            optimizer=optimizer,
            scenario_count=scenario_count,
        )

        report = pd.DataFrame(rows).drop_duplicates("scenario")
        report = report.sort_values(
            ["total_compound_return", "sample_count", "max_drawdown"],
            ascending=[False, False, True],
        )
        yearly = pd.DataFrame(yearly_rows)
        yearly = yearly[yearly["scenario"].isin(set(report["scenario"]))].copy()
        diagnostics = diagnostics.sort_values(
            ["factor", "underperform_score", "sample_count"],
            ascending=[True, False, False],
        )

        self.output_report_path.parent.mkdir(parents=True, exist_ok=True)
        report.to_csv(self.output_report_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        diagnostics.to_csv(self.output_diagnostics_path, index=False, encoding="utf-8-sig")
        self.logger.info("市场热度因子优化报告已生成: %s, 行数: %s", self.output_report_path, len(report))
        self.logger.info("市场热度因子年度报告已生成: %s, 行数: %s", self.output_yearly_path, len(yearly))
        self.logger.info("市场热度因子诊断报告已生成: %s, 行数: %s", self.output_diagnostics_path, len(diagnostics))
        return {
            "report": self.output_report_path,
            "yearly": self.output_yearly_path,
            "diagnostics": self.output_diagnostics_path,
        }

    def load_base_candidates(self, optimizer: StrategyConditionOptimizer) -> pd.DataFrame:
        trades = optimizer.load_trades()
        for column, value in self.replay_config.get("base_conditions", {}).items():
            trades = trades[trades[column].astype(str) == str(value)].copy()
        for column, values in self.replay_config.get("base_exclusions", {}).items():
            excluded_values = {str(value) for value in values}
            trades = trades[~trades[column].astype(str).isin(excluded_values)].copy()
        self.logger.info("市场热度优化基础候选池: %s", len(trades))
        return trades

    @staticmethod
    def replay_candidates(candidates: pd.DataFrame) -> pd.DataFrame:
        replay_engine = ConservativeTradeReplay(config_path="config/config.json")
        replay_rule = ReplayRule(rule_name="fixed_t2_close", max_hold_days=2, exit_price_field="close")
        forward_prices = replay_engine.load_forward_prices()
        samples = candidates.merge(forward_prices, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
        return replay_engine.replay_rule(samples, replay_rule)

    def current_post_exclusions(self) -> tuple[tuple[str, str], ...]:
        exclusions = []
        for column, values in self.replay_config.get("post_selection_exclusions", {}).items():
            for value in values:
                exclusions.append((column, str(value)))
        return tuple(exclusions)

    @staticmethod
    def apply_post_exclusions(
        selected: pd.DataFrame,
        exclusions: tuple[tuple[str, str], ...],
    ) -> pd.DataFrame:
        result = selected.copy()
        for column, value in exclusions:
            if column in result.columns:
                result = result[result[column].astype(str) != str(value)].copy()
        return result

    def evaluate_shrink_scenarios(
        self,
        rows: list[dict[str, object]],
        yearly_rows: list[dict[str, object]],
        selected_without_post: pd.DataFrame,
        scenario_count: int,
    ) -> int:
        current = self.current_post_exclusions()
        for removed in current:
            if self.hit_scenario_limit(scenario_count):
                return scenario_count
            remaining = tuple(condition for condition in current if condition != removed)
            selected = self.apply_post_exclusions(selected_without_post, remaining)
            scenario = f"shrink_remove_current_post:{self.format_conditions((removed,))}"
            summary = self.evaluate_scenario(scenario, "shrink", (removed,), selected)
            if summary:
                rows.append(summary)
                yearly_rows.extend(self.build_yearly_rows(scenario, selected))
                scenario_count += 1
        return scenario_count

    def evaluate_condition_scenarios(
        self,
        rows: list[dict[str, object]],
        yearly_rows: list[dict[str, object]],
        replayed_candidates: pd.DataFrame,
        selected_without_post: pd.DataFrame,
        baseline_selected: pd.DataFrame,
        optimizer: StrategyConditionOptimizer,
        scenario_count: int,
    ) -> int:
        require_candidates = self.build_condition_candidates(
            data=replayed_candidates,
            mode="require",
            prefer_high_return=True,
        )
        pre_exclude_candidates = self.build_condition_candidates(
            data=replayed_candidates,
            mode="pre_exclude",
            prefer_high_return=False,
        )
        post_exclude_candidates = self.build_condition_candidates(
            data=baseline_selected,
            mode="post_exclude",
            prefer_high_return=False,
        )
        search_plans = [
            ("require", require_candidates),
            ("pre_exclude", pre_exclude_candidates),
            ("post_exclude", post_exclude_candidates),
        ]
        for mode, candidates in search_plans:
            for condition_count in range(1, self.max_condition_count + 1):
                for combo in combinations(candidates, condition_count):
                    if self.hit_scenario_limit(scenario_count):
                        return scenario_count
                    if self.has_duplicate_factor(combo):
                        continue
                    selected = self.build_selected_for_mode(
                        mode=mode,
                        combo=combo,
                        replayed_candidates=replayed_candidates,
                        selected_without_post=selected_without_post,
                        baseline_selected=baseline_selected,
                        optimizer=optimizer,
                    )
                    scenario = f"{mode}:{self.format_conditions(combo)}"
                    summary = self.evaluate_scenario(scenario, mode, combo, selected)
                    if not summary:
                        continue
                    rows.append(summary)
                    yearly_rows.extend(self.build_yearly_rows(scenario, selected))
                    scenario_count += 1
        return scenario_count

    def build_selected_for_mode(
        self,
        mode: str,
        combo: tuple[tuple[str, str], ...],
        replayed_candidates: pd.DataFrame,
        selected_without_post: pd.DataFrame,
        baseline_selected: pd.DataFrame,
        optimizer: StrategyConditionOptimizer,
    ) -> pd.DataFrame:
        if mode == "require":
            data = self.apply_require_conditions(replayed_candidates, combo)
            selected = self.select_daily_if_not_empty(optimizer, data)
            return self.apply_post_exclusions(selected, self.current_post_exclusions())
        if mode == "pre_exclude":
            data = self.apply_exclude_conditions(replayed_candidates, combo)
            selected = self.select_daily_if_not_empty(optimizer, data)
            return self.apply_post_exclusions(selected, self.current_post_exclusions())
        if mode == "post_exclude":
            return self.apply_exclude_conditions(baseline_selected, combo)
        raise ValueError(f"未知模式: {mode}")

    @staticmethod
    def select_daily_if_not_empty(
        optimizer: StrategyConditionOptimizer,
        data: pd.DataFrame,
    ) -> pd.DataFrame:
        if data.empty:
            return data.copy()
        return optimizer.select_daily_candidates(data, max_holding_count=1)

    def build_condition_candidates(
        self,
        data: pd.DataFrame,
        mode: str,
        prefer_high_return: bool,
    ) -> list[tuple[str, str]]:
        executed = self.select_executed(data)
        base_mean = executed["daily_return"].mean() if len(executed) else 0.0
        rows = []
        for factor in self.factor_columns:
            if factor not in executed.columns:
                continue
            for value, group in executed.groupby(executed[factor].fillna("missing").astype(str), dropna=False):
                if value in {"missing", "nan", "unknown"}:
                    continue
                if len(group) < self.min_condition_samples:
                    continue
                group_mean = float(group["daily_return"].mean())
                delta = group_mean - base_mean
                if prefer_high_return and delta <= 0:
                    continue
                if not prefer_high_return and delta >= 0:
                    continue
                score = abs(delta) * len(group)
                rows.append(
                    {
                        "mode": mode,
                        "condition": (factor, str(value)),
                        "sample_count": int(len(group)),
                        "score": float(score),
                    }
                )
        rows = sorted(rows, key=lambda item: (item["score"], item["sample_count"]), reverse=True)
        return [item["condition"] for item in rows[: self.max_candidates_per_mode]]

    def build_diagnostics(self, selected: pd.DataFrame) -> pd.DataFrame:
        executed = self.select_executed(selected)
        base_mean = executed["daily_return"].mean() if len(executed) else 0.0
        rows = []
        for factor in self.factor_columns:
            if factor not in executed.columns:
                continue
            for value, group in executed.groupby(executed[factor].fillna("missing").astype(str), dropna=False):
                returns = group["daily_return"].dropna()
                daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
                rows.append(
                    {
                        "factor": factor,
                        "value": str(value),
                        "sample_count": int(len(group)),
                        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                        "avg_daily_return": float(returns.mean()) if len(returns) else 0.0,
                        "median_daily_return": float(returns.median()) if len(returns) else 0.0,
                        "compound_return": self.compound_return(daily_returns),
                        "underperform_score": float(max(base_mean - returns.mean(), 0) * len(group)) if len(returns) else 0.0,
                    }
                )
        return pd.DataFrame(rows)

    def evaluate_scenario(
        self,
        scenario: str,
        mode: str,
        conditions: tuple[tuple[str, str], ...],
        selected: pd.DataFrame,
    ) -> dict[str, object] | None:
        executed = self.select_executed(selected)
        if len(executed) < self.min_remaining_samples:
            return None
        returns = executed["daily_return"].dropna()
        trade_returns = executed["net_return"].dropna()
        gains = trade_returns[trade_returns > 0]
        losses = trade_returns[trade_returns <= 0]
        daily_returns = executed.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        yearly_returns = self.calculate_yearly_returns(executed)
        rolling_3y = self.calculate_rolling_year_returns(yearly_returns, window=3)
        return {
            "scenario": scenario,
            "mode": mode,
            "conditions": self.format_conditions(conditions),
            "condition_count": len(conditions),
            "selected_signal_count": int(len(selected)),
            "sample_count": int(len(executed)),
            "signal_days": int(selected["trade_date"].nunique()) if "trade_date" in selected.columns else 0,
            "executed_days": int(executed["exit_trade_date"].nunique()),
            "win_rate": float((trade_returns > 0).mean()) if len(trade_returns) else 0.0,
            "avg_return_per_trade": float(trade_returns.mean()) if len(trade_returns) else 0.0,
            "median_return_per_trade": float(trade_returns.median()) if len(trade_returns) else 0.0,
            "avg_daily_return": float(returns.mean()) if len(returns) else 0.0,
            "total_compound_return": self.compound_return(daily_returns),
            "final_equity": self.initial_cash * (1 + self.compound_return(daily_returns)),
            "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
            "max_profit_per_trade": float(trade_returns.max()) if len(trade_returns) else 0.0,
            "max_loss_per_trade": float(trade_returns.min()) if len(trade_returns) else 0.0,
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(trade_returns),
            "min_rolling_3y_return": min(rolling_3y.values()) if rolling_3y else 0.0,
            "max_rolling_3y_return": max(rolling_3y.values()) if rolling_3y else 0.0,
            "return_2022": yearly_returns.get("2022", 0.0),
            "return_2026": yearly_returns.get("2026", 0.0),
        }

    def build_yearly_rows(self, scenario: str, selected: pd.DataFrame) -> list[dict[str, object]]:
        executed = self.select_executed(selected)
        rows = []
        data = executed.copy()
        data["year"] = data["exit_trade_date"].astype(str).str[:4]
        for year, group in data.groupby("year"):
            if self.evaluation_years and str(year) not in self.evaluation_years:
                continue
            trade_returns = group["net_return"].dropna()
            daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            rows.append(
                {
                    "scenario": scenario,
                    "year": str(year),
                    "sample_count": int(len(group)),
                    "year_return": self.compound_return(daily_returns),
                    "win_rate": float((trade_returns > 0).mean()) if len(trade_returns) else 0.0,
                    "avg_return_per_trade": float(trade_returns.mean()) if len(trade_returns) else 0.0,
                    "median_return_per_trade": float(trade_returns.median()) if len(trade_returns) else 0.0,
                }
            )
        return rows

    def calculate_yearly_returns(self, executed: pd.DataFrame) -> dict[str, float]:
        data = executed.copy()
        data["year"] = data["exit_trade_date"].astype(str).str[:4]
        yearly_returns = {}
        for year, group in data.groupby("year"):
            if self.evaluation_years and str(year) not in self.evaluation_years:
                continue
            daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            yearly_returns[str(year)] = self.compound_return(daily_returns)
        return dict(sorted(yearly_returns.items()))

    @staticmethod
    def select_executed(data: pd.DataFrame) -> pd.DataFrame:
        return data[
            (data["buy_executed"] == True)  # noqa: E712
            & (data["sell_executed"] == True)  # noqa: E712
            & data["daily_return"].notna()
        ].copy()

    @staticmethod
    def apply_require_conditions(data: pd.DataFrame, combo: tuple[tuple[str, str], ...]) -> pd.DataFrame:
        result = data.copy()
        for column, value in combo:
            result = result[result[column].fillna("missing").astype(str) == str(value)].copy()
        return result

    @staticmethod
    def apply_exclude_conditions(data: pd.DataFrame, combo: tuple[tuple[str, str], ...]) -> pd.DataFrame:
        result = data.copy()
        for column, value in combo:
            result = result[result[column].fillna("missing").astype(str) != str(value)].copy()
        return result

    @staticmethod
    def has_duplicate_factor(combo: tuple[tuple[str, str], ...]) -> bool:
        return len({factor for factor, _ in combo}) != len(combo)

    def hit_scenario_limit(self, scenario_count: int) -> bool:
        return bool(self.max_scenarios and scenario_count >= self.max_scenarios)

    @staticmethod
    def compound_return(returns: pd.Series) -> float:
        return float((1 + returns).prod() - 1) if len(returns) else 0.0

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

    @staticmethod
    def format_conditions(combo: tuple[tuple[str, str], ...]) -> str:
        return ";".join(f"{column}={value}" for column, value in combo)


if __name__ == "__main__":
    main()
