from __future__ import annotations

import argparse
import itertools
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
    parser = argparse.ArgumentParser(description="回测 A5-R1 剩余风险候选过滤条件。")
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
    outputs = A5R1FilterCandidateBacktester(config_path=args.config).backtest()
    print("A5-R1 候选过滤回测完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class A5R1FilterCandidateBacktester:
    """对 A5-R1 剩余风险因子做候选过滤回测，不直接修改正式策略。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("a5_r1_filter_candidate_backtest")
        self.backtest_config = self.config.get("a5_r1_filter_candidate_backtest", {})
        self.input_overlay_trade_path = self.project_root / self.backtest_config.get(
            "input_overlay_trade_path",
            "reports/dynamic_risk_overlay_trades.csv",
        )
        self.input_focus_group_report_path = self.project_root / self.backtest_config.get(
            "input_focus_group_report_path",
            "reports/a5_r1_failure_focus_group.csv",
        )
        self.output_candidate_path = self.project_root / self.backtest_config.get(
            "output_candidate_path",
            "reports/a5_r1_filter_candidates.csv",
        )
        self.output_summary_path = self.project_root / self.backtest_config.get(
            "output_summary_path",
            "reports/a5_r1_filter_backtest_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.backtest_config.get(
            "output_yearly_path",
            "reports/a5_r1_filter_backtest_yearly.csv",
        )
        self.output_split_path = self.project_root / self.backtest_config.get(
            "output_split_path",
            "reports/a5_r1_filter_backtest_split.csv",
        )
        self.target_policy = str(self.backtest_config.get("target_policy", "weak_and_segment_neutral_skip"))
        self.min_candidate_samples = int(self.backtest_config.get("min_candidate_samples", 3))
        self.top_single_candidates = int(self.backtest_config.get("top_single_candidates", 18))
        self.max_combo_candidates = int(self.backtest_config.get("max_combo_candidates", 10))
        self.focus_names = {str(name) for name in self.backtest_config.get("focus_names", [])}
        self.excluded_candidate_factors = {
            str(name) for name in self.backtest_config.get("excluded_candidate_factors", [])
        }
        self.splits = list(self.backtest_config.get("splits", []))

    def backtest(self) -> dict[str, Path]:
        trades = self.load_target_policy_rows()
        candidates = self.load_candidates()
        scenarios = self.build_scenarios(candidates)
        summary_rows = []
        yearly_rows = []
        split_rows = []

        for scenario in scenarios:
            simulated = self.apply_scenario(trades, scenario)
            summary_rows.append(self.summarize_scenario(simulated, scenario))
            yearly_rows.extend(self.build_yearly_rows(simulated, scenario))
            split_rows.extend(self.build_split_rows(simulated, scenario))

        summary = pd.DataFrame(summary_rows).sort_values(
            ["total_compound_return", "max_drawdown", "traded_count"],
            ascending=[False, True, False],
        )
        yearly = pd.DataFrame(yearly_rows)
        split = pd.DataFrame(split_rows)

        self.output_candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidates.to_csv(self.output_candidate_path, index=False, encoding="utf-8-sig")
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        split.to_csv(self.output_split_path, index=False, encoding="utf-8-sig")
        self.logger.info("A5-R1 候选过滤条件已生成: %s, 行数: %s", self.output_candidate_path, len(candidates))
        self.logger.info("A5-R1 候选过滤汇总已生成: %s, 行数: %s", self.output_summary_path, len(summary))
        self.logger.info("A5-R1 候选过滤年度报告已生成: %s, 行数: %s", self.output_yearly_path, len(yearly))
        self.logger.info("A5-R1 候选过滤拆分报告已生成: %s, 行数: %s", self.output_split_path, len(split))
        return {
            "candidates": self.output_candidate_path,
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "split": self.output_split_path,
        }

    def load_target_policy_rows(self) -> pd.DataFrame:
        trades = pd.read_csv(
            self.input_overlay_trade_path,
            dtype={"trade_date": str, "exit_trade_date": str, "ts_code": str, "risk_policy": str},
            low_memory=False,
        )
        trades = trades[
            (trades["risk_policy"].astype(str) == self.target_policy)
            & trades["exit_trade_date"].notna()
            & trades["adjusted_daily_return"].notna()
        ].copy()
        if trades.empty:
            raise RuntimeError(f"没有找到 A5-R1 样本: {self.input_overlay_trade_path}")
        trades["adjusted_daily_return"] = pd.to_numeric(trades["adjusted_daily_return"], errors="coerce")
        trades["year"] = trades["exit_trade_date"].astype(str).str[:4]
        trades["is_base_trade"] = trades["overlay_action"].astype(str) == "trade"
        return trades.sort_values(["exit_trade_date", "trade_date", "ts_code"]).reset_index(drop=True)

    def load_candidates(self) -> pd.DataFrame:
        report = pd.read_csv(self.input_focus_group_report_path, low_memory=False)
        if self.focus_names:
            report = report[report["focus_name"].astype(str).isin(self.focus_names)].copy()
        report = report[
            (report["focus_sample_count"] >= self.min_candidate_samples)
            & (report["focus_compound_return"] < 0)
            & (~report["factor"].astype(str).isin(self.excluded_candidate_factors))
        ].copy()
        if report.empty:
            raise RuntimeError(f"没有可回测的候选过滤条件: {self.input_focus_group_report_path}")
        report["candidate_key"] = report["factor"].astype(str) + "=" + report["factor_value"].astype(str)
        report["risk_score"] = (
            report["focus_compound_return"].abs()
            * report["focus_sample_count"]
            * (1 + report["focus_loss_contribution"].fillna(0))
        )
        report = report.sort_values(
            ["risk_score", "focus_compound_return", "focus_sample_count"],
            ascending=[False, True, False],
        )
        return report.drop_duplicates(["factor", "factor_value"]).head(self.top_single_candidates).reset_index(drop=True)

    def build_scenarios(self, candidates: pd.DataFrame) -> list[dict[str, object]]:
        scenarios: list[dict[str, object]] = [
            {
                "scenario": "a5_r1_base",
                "description": "A5-R1 原始规则，不增加候选过滤。",
                "conditions": [],
            }
        ]
        for row in candidates.itertuples(index=False):
            factor = str(row.factor)
            value = str(row.factor_value)
            scenarios.append(
                {
                    "scenario": f"exclude_{factor}={value}",
                    "description": f"在 A5-R1 基础上跳过 {factor}={value}",
                    "conditions": [(factor, value)],
                }
            )

        combo_candidates = [
            (str(row.factor), str(row.factor_value))
            for row in candidates.head(self.max_combo_candidates).itertuples(index=False)
        ]
        for left, right in itertools.combinations(combo_candidates, 2):
            if left[0] == right[0]:
                continue
            scenarios.append(
                {
                    "scenario": f"exclude_{left[0]}={left[1]}__or__{right[0]}={right[1]}",
                    "description": f"在 A5-R1 基础上跳过 {left[0]}={left[1]} 或 {right[0]}={right[1]}",
                    "conditions": [left, right],
                }
            )
        return scenarios

    def apply_scenario(self, trades: pd.DataFrame, scenario: dict[str, object]) -> pd.DataFrame:
        result = trades.copy()
        skip_mask = pd.Series(False, index=result.index)
        for factor, value in scenario["conditions"]:
            if factor not in result.columns:
                continue
            skip_mask = skip_mask | (result[factor].fillna("missing").astype(str) == str(value))
        result["candidate_filter_skip"] = skip_mask
        result["scenario_action"] = result["overlay_action"].astype(str)
        result.loc[result["candidate_filter_skip"], "scenario_action"] = "skip"
        result["scenario_daily_return"] = result["adjusted_daily_return"]
        result.loc[result["scenario_action"] != "trade", "scenario_daily_return"] = 0.0
        return result

    def summarize_scenario(self, trades: pd.DataFrame, scenario: dict[str, object]) -> dict[str, object]:
        traded = trades[trades["scenario_action"].astype(str) == "trade"].copy()
        returns = traded["scenario_daily_return"].dropna()
        daily_returns = traded.groupby("exit_trade_date")["scenario_daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        yearly_returns = self.calculate_yearly_returns(traded)
        return {
            "scenario": scenario["scenario"],
            "description": scenario["description"],
            "signal_count": int(len(trades)),
            "traded_count": int(len(traded)),
            "base_skipped_count": int((trades["overlay_action"].astype(str) == "skip").sum()),
            "candidate_skipped_count": int(trades["candidate_filter_skip"].sum()),
            "additional_skipped_count": int((trades["is_base_trade"] & trades["candidate_filter_skip"]).sum()),
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

    def build_yearly_rows(self, trades: pd.DataFrame, scenario: dict[str, object]) -> list[dict[str, object]]:
        traded = trades[trades["scenario_action"].astype(str) == "trade"].copy()
        rows = []
        for year, group in traded.groupby("year"):
            returns = group["scenario_daily_return"].dropna()
            daily_returns = group.groupby("exit_trade_date")["scenario_daily_return"].sum().sort_index()
            rows.append(
                {
                    "scenario": scenario["scenario"],
                    "year": year,
                    "sample_count": int(len(returns)),
                    "year_return": self.compound_return(daily_returns),
                    "win_rate": self.win_rate(returns),
                    "avg_daily_return": self.mean(returns),
                    "median_daily_return": self.median(returns),
                    "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
                }
            )
        return rows

    def build_split_rows(self, trades: pd.DataFrame, scenario: dict[str, object]) -> list[dict[str, object]]:
        rows = []
        for split in self.splits:
            sample = trades[
                (trades["year"] >= str(split["start_year"]))
                & (trades["year"] <= str(split["end_year"]))
                & (trades["scenario_action"].astype(str) == "trade")
            ].copy()
            returns = sample["scenario_daily_return"].dropna()
            daily_returns = sample.groupby("exit_trade_date")["scenario_daily_return"].sum().sort_index()
            equity_curve = (1 + daily_returns).cumprod()
            rows.append(
                {
                    "scenario": scenario["scenario"],
                    "split_name": split["split_name"],
                    "start_year": split["start_year"],
                    "end_year": split["end_year"],
                    "sample_count": int(len(returns)),
                    "split_return": self.compound_return(daily_returns),
                    "max_drawdown": NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve),
                    "win_rate": self.win_rate(returns),
                    "avg_daily_return": self.mean(returns),
                    "median_daily_return": self.median(returns),
                    "max_consecutive_losses": NextDayPremiumAnalyzer.max_consecutive_losses(returns),
                }
            )
        return rows

    def calculate_yearly_returns(self, trades: pd.DataFrame) -> dict[str, float]:
        yearly_returns = {}
        for year, group in trades.groupby("year"):
            daily_returns = group.groupby("exit_trade_date")["scenario_daily_return"].sum().sort_index()
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
