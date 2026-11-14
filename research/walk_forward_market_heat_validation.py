from __future__ import annotations

import argparse
import math
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
    parser = argparse.ArgumentParser(description="滚动训练市场热度过滤条件，并验证后续样本外表现。")
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
    outputs = WalkForwardMarketHeatValidator(config_path=args.config).validate()
    print("市场热度 walk-forward 验证完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class WalkForwardMarketHeatValidator:
    """用训练期挑过滤条件，再应用到后续测试期。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("walk_forward_market_heat")
        self.validation_config = self.config.get("walk_forward_market_heat_validation", {})
        self.initial_cash = float(self.validation_config.get("initial_cash", 1000000))
        self.replay_rule = self.validation_config.get("replay_rule", "fixed_t2_close")
        self.min_train_samples = int(self.validation_config.get("min_train_samples", 120))
        self.min_test_samples = int(self.validation_config.get("min_test_samples", 15))
        self.min_condition_samples = int(self.validation_config.get("min_condition_samples", 8))
        self.max_condition_count = int(self.validation_config.get("max_condition_count", 2))
        self.max_candidates_per_mode = int(self.validation_config.get("max_candidates_per_mode", 25))
        self.train_top_n = int(self.validation_config.get("train_top_n", 10))
        self.stable_condition_min_windows = int(self.validation_config.get("stable_condition_min_windows", 2))
        self.stable_condition_min_positive_oos_ratio = float(
            self.validation_config.get("stable_condition_min_positive_oos_ratio", 0.5)
        )
        self.selection_metric = str(self.validation_config.get("selection_metric", "robust_score"))
        self.robust_score_weights = dict(self.validation_config.get("robust_score_weights", {}))
        self.base_conditions = dict(self.validation_config.get("base_conditions", {}))
        self.seed_base_exclusions = dict(self.validation_config.get("seed_base_exclusions", {}))
        self.seed_post_exclusions = dict(self.validation_config.get("seed_post_exclusions", {}))
        self.windows = list(self.validation_config.get("windows", []))
        self.factor_columns = list(self.validation_config.get("factor_columns", []))
        self.output_summary_path = self.project_root / self.validation_config.get(
            "output_summary_path",
            "reports/walk_forward_market_heat_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.validation_config.get(
            "output_yearly_path",
            "reports/walk_forward_market_heat_yearly.csv",
        )
        self.output_train_candidates_path = self.project_root / self.validation_config.get(
            "output_train_candidates_path",
            "reports/walk_forward_market_heat_train_candidates.csv",
        )
        self.output_oos_candidates_path = self.project_root / self.validation_config.get(
            "output_oos_candidates_path",
            "reports/walk_forward_market_heat_oos_candidates.csv",
        )
        self.output_condition_stability_path = self.project_root / self.validation_config.get(
            "output_condition_stability_path",
            "reports/walk_forward_market_heat_condition_stability.csv",
        )

    def validate(self) -> dict[str, Path]:
        optimizer = StrategyConditionOptimizer(config_path="config/config.json")
        base_candidates = self.load_base_candidates(optimizer)
        replayed_candidates = self.replay_candidates(base_candidates)

        summary_rows = []
        yearly_rows = []
        train_candidate_rows = []
        oos_candidate_rows = []
        for window in self.windows:
            window_name = str(window["window_name"])
            self.logger.info("开始 walk-forward 窗口: %s", window_name)
            train_pool = self.filter_year_range(
                replayed_candidates,
                start_year=str(window["train_start_year"]),
                end_year=str(window["train_end_year"]),
            )
            test_pool = self.filter_year_range(
                replayed_candidates,
                start_year=str(window["test_start_year"]),
                end_year=str(window["test_end_year"]),
            )
            train_baseline = self.build_selected(mode="baseline", pool=train_pool, combo=(), optimizer=optimizer)
            test_baseline = self.build_selected(mode="baseline", pool=test_pool, combo=(), optimizer=optimizer)
            train_summary = self.evaluate_selected(
                window_name=window_name,
                phase="train_baseline_seed",
                mode="baseline",
                combo=(),
                selected=train_baseline,
                min_samples=self.min_train_samples,
            )
            test_summary = self.evaluate_selected(
                window_name=window_name,
                phase="test_baseline_seed",
                mode="baseline",
                combo=(),
                selected=test_baseline,
                min_samples=self.min_test_samples,
            )
            if train_summary:
                summary_rows.append(train_summary)
                yearly_rows.extend(self.build_yearly_rows(window_name, "train_baseline_seed", train_baseline))
            if test_summary:
                summary_rows.append(test_summary)
                yearly_rows.extend(self.build_yearly_rows(window_name, "test_baseline_seed", test_baseline))

            train_ranked = self.find_best_train_scenarios(train_pool, train_baseline, optimizer, window_name)
            top_train_candidates = train_ranked[: self.train_top_n]
            for train_rank, train_candidate in enumerate(top_train_candidates, start=1):
                train_candidate = dict(train_candidate)
                train_candidate["train_rank"] = train_rank
                train_candidate_rows.append(train_candidate)
                combo = self.parse_conditions(str(train_candidate["conditions"]))
                test_candidate_selected = self.build_selected(
                    mode=str(train_candidate["mode"]),
                    pool=test_pool,
                    combo=combo,
                    optimizer=optimizer,
                )
                oos_candidate_summary = self.evaluate_selected(
                    window_name=window_name,
                    phase="test_oos_candidate",
                    mode=str(train_candidate["mode"]),
                    combo=combo,
                    selected=test_candidate_selected,
                    min_samples=self.min_test_samples,
                )
                if oos_candidate_summary:
                    oos_candidate_summary["train_rank"] = train_rank
                    oos_candidate_summary["train_total_compound_return"] = train_candidate.get(
                        "total_compound_return", 0.0
                    )
                    oos_candidate_summary["train_robust_score"] = train_candidate.get("robust_score", 0.0)
                    oos_candidate_rows.append(oos_candidate_summary)
            if not train_ranked:
                self.logger.warning("窗口无满足样本数的训练候选: %s", window_name)
                continue

            best = top_train_candidates[0]
            best_combo = self.parse_conditions(best["conditions"])
            train_selected = self.build_selected(
                mode=str(best["mode"]),
                pool=train_pool,
                combo=best_combo,
                optimizer=optimizer,
            )
            test_selected = self.build_selected(
                mode=str(best["mode"]),
                pool=test_pool,
                combo=best_combo,
                optimizer=optimizer,
            )
            train_best_summary = self.evaluate_selected(
                window_name=window_name,
                phase="train_best",
                mode=str(best["mode"]),
                combo=best_combo,
                selected=train_selected,
                min_samples=self.min_train_samples,
            )
            test_best_summary = self.evaluate_selected(
                window_name=window_name,
                phase="test_oos",
                mode=str(best["mode"]),
                combo=best_combo,
                selected=test_selected,
                min_samples=self.min_test_samples,
            )
            if train_best_summary:
                train_best_summary["selected_by_train_rank"] = 1
                summary_rows.append(train_best_summary)
                yearly_rows.extend(self.build_yearly_rows(window_name, "train_best", train_selected))
            if test_best_summary:
                test_best_summary["selected_by_train_rank"] = 1
                summary_rows.append(test_best_summary)
                yearly_rows.extend(self.build_yearly_rows(window_name, "test_oos", test_selected))

        summary = pd.DataFrame(summary_rows)
        yearly = pd.DataFrame(yearly_rows)
        train_candidates = pd.DataFrame(train_candidate_rows)
        oos_candidates = pd.DataFrame(oos_candidate_rows)
        condition_stability = self.build_condition_stability(oos_candidate_rows)
        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        train_candidates.to_csv(self.output_train_candidates_path, index=False, encoding="utf-8-sig")
        oos_candidates.to_csv(self.output_oos_candidates_path, index=False, encoding="utf-8-sig")
        condition_stability.to_csv(self.output_condition_stability_path, index=False, encoding="utf-8-sig")
        self.logger.info("walk-forward 汇总报告已生成: %s", self.output_summary_path)
        self.logger.info("walk-forward 年度报告已生成: %s", self.output_yearly_path)
        self.logger.info("walk-forward 训练候选报告已生成: %s", self.output_train_candidates_path)
        self.logger.info("walk-forward 样本外候选报告已生成: %s", self.output_oos_candidates_path)
        self.logger.info("walk-forward 条件稳定性报告已生成: %s", self.output_condition_stability_path)
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "train_candidates": self.output_train_candidates_path,
            "oos_candidates": self.output_oos_candidates_path,
            "condition_stability": self.output_condition_stability_path,
        }

    def load_base_candidates(self, optimizer: StrategyConditionOptimizer) -> pd.DataFrame:
        trades = optimizer.load_trades()
        for column, value in self.base_conditions.items():
            trades = trades[trades[column].astype(str) == str(value)].copy()
        for column, values in self.seed_base_exclusions.items():
            trades = trades[~trades[column].astype(str).isin({str(value) for value in values})].copy()
        self.logger.info("walk-forward 基础候选池: %s", len(trades))
        return trades

    def replay_candidates(self, candidates: pd.DataFrame) -> pd.DataFrame:
        replay_engine = ConservativeTradeReplay(config_path="config/config.json")
        replay_rule = ReplayRule(rule_name=self.replay_rule, max_hold_days=2, exit_price_field="close")
        forward_prices = replay_engine.load_forward_prices()
        samples = candidates.merge(forward_prices, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
        replayed = replay_engine.replay_rule(samples, replay_rule)
        replayed["year"] = replayed["exit_trade_date"].astype(str).str[:4]
        return replayed

    @staticmethod
    def filter_year_range(data: pd.DataFrame, start_year: str, end_year: str) -> pd.DataFrame:
        year = data["year"].astype(str)
        return data[(year >= start_year) & (year <= end_year)].copy()

    def find_best_train_scenarios(
        self,
        train_pool: pd.DataFrame,
        train_baseline: pd.DataFrame,
        optimizer: StrategyConditionOptimizer,
        window_name: str,
    ) -> list[dict[str, object]]:
        require_candidates = self.build_condition_candidates(train_pool, prefer_high_return=True)
        pre_exclude_candidates = self.build_condition_candidates(train_pool, prefer_high_return=False)
        post_exclude_candidates = self.build_condition_candidates(train_baseline, prefer_high_return=False)
        rows = []
        for mode, candidates in [
            ("require", require_candidates),
            ("pre_exclude", pre_exclude_candidates),
            ("post_exclude", post_exclude_candidates),
        ]:
            for condition_count in range(1, self.max_condition_count + 1):
                for combo in combinations(candidates, condition_count):
                    if self.has_duplicate_factor(combo):
                        continue
                    selected = self.build_selected(mode=mode, pool=train_pool, combo=combo, optimizer=optimizer)
                    summary = self.evaluate_selected(
                        window_name=window_name,
                        phase="train_candidate",
                        mode=mode,
                        combo=combo,
                        selected=selected,
                        min_samples=self.min_train_samples,
                    )
                    if summary:
                        rows.append(summary)
        return sorted(rows, key=self.train_sort_key, reverse=True)

    def train_sort_key(self, row: dict[str, object]) -> tuple[float, float, int, float]:
        if self.selection_metric == "total_compound_return":
            primary = float(row["total_compound_return"])
        else:
            primary = float(row.get("robust_score", 0.0))
        return (
            primary,
            float(row["total_compound_return"]),
            int(row["sample_count"]),
            -float(row["max_drawdown"]),
        )

    def build_condition_candidates(self, data: pd.DataFrame, prefer_high_return: bool) -> list[tuple[str, str]]:
        executed = self.select_executed(data)
        base_mean = executed["daily_return"].mean() if len(executed) else 0.0
        rows = []
        for factor in self.factor_columns:
            if factor not in executed.columns:
                continue
            grouped = executed.groupby(executed[factor].fillna("missing").astype(str), dropna=False)
            for value, group in grouped:
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
                rows.append(
                    {
                        "condition": (factor, str(value)),
                        "score": float(abs(delta) * len(group)),
                        "sample_count": int(len(group)),
                    }
                )
        rows = sorted(rows, key=lambda item: (item["score"], item["sample_count"]), reverse=True)
        return [row["condition"] for row in rows[: self.max_candidates_per_mode]]

    def build_selected(
        self,
        mode: str,
        pool: pd.DataFrame,
        combo: tuple[tuple[str, str], ...],
        optimizer: StrategyConditionOptimizer,
    ) -> pd.DataFrame:
        if mode == "require":
            filtered = self.apply_require_conditions(pool, combo)
            selected = self.select_daily_if_not_empty(optimizer, filtered)
            return self.apply_seed_post_exclusions(selected)
        if mode == "pre_exclude":
            filtered = self.apply_exclude_conditions(pool, combo)
            selected = self.select_daily_if_not_empty(optimizer, filtered)
            return self.apply_seed_post_exclusions(selected)
        if mode == "post_exclude":
            selected = self.select_daily_if_not_empty(optimizer, pool)
            selected = self.apply_seed_post_exclusions(selected)
            return self.apply_exclude_conditions(selected, combo)
        if mode == "baseline":
            selected = self.select_daily_if_not_empty(optimizer, pool)
            return self.apply_seed_post_exclusions(selected)
        raise ValueError(f"未知模式: {mode}")

    @staticmethod
    def select_daily_if_not_empty(optimizer: StrategyConditionOptimizer, data: pd.DataFrame) -> pd.DataFrame:
        if data.empty:
            return data.copy()
        return optimizer.select_daily_candidates(data, max_holding_count=1)

    def apply_seed_post_exclusions(self, selected: pd.DataFrame) -> pd.DataFrame:
        result = selected.copy()
        for column, values in self.seed_post_exclusions.items():
            if column not in result.columns:
                continue
            result = result[~result[column].astype(str).isin({str(value) for value in values})].copy()
        return result

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

    def evaluate_selected(
        self,
        window_name: str,
        phase: str,
        mode: str,
        combo: tuple[tuple[str, str], ...],
        selected: pd.DataFrame,
        min_samples: int,
    ) -> dict[str, object] | None:
        executed = self.select_executed(selected)
        if len(executed) < min_samples:
            return None
        returns = executed["net_return"].dropna()
        daily_returns = executed.groupby("exit_trade_date")["daily_return"].sum().sort_index()
        equity_curve = (1 + daily_returns).cumprod()
        yearly_returns = self.calculate_yearly_returns(executed)
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        max_consecutive_losses = NextDayPremiumAnalyzer.max_consecutive_losses(returns)
        total_compound_return = self.compound_return(daily_returns)
        max_drawdown = NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve)
        return {
            "window_name": window_name,
            "phase": phase,
            "mode": mode,
            "conditions": self.format_conditions(combo),
            "condition_count": len(combo),
            "sample_count": int(len(executed)),
            "trade_days": int(executed["exit_trade_date"].nunique()),
            "start_year": str(executed["year"].min()),
            "end_year": str(executed["year"].max()),
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
            "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
            "total_compound_return": total_compound_return,
            "final_equity": self.initial_cash * (1 + total_compound_return),
            "year_count": int(len(yearly_returns)),
            "positive_year_count": int(sum(value > 0 for value in yearly_returns.values())),
            "min_year_return": float(min(yearly_returns.values())) if yearly_returns else 0.0,
            "avg_year_return": float(sum(yearly_returns.values()) / len(yearly_returns)) if yearly_returns else 0.0,
            "max_drawdown": max_drawdown,
            "max_profit_per_trade": float(returns.max()) if len(returns) else 0.0,
            "max_loss_per_trade": float(returns.min()) if len(returns) else 0.0,
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": max_consecutive_losses,
            "robust_score": self.calculate_robust_score(
                total_compound_return=total_compound_return,
                sample_count=len(executed),
                win_rate=float((returns > 0).mean()) if len(returns) else 0.0,
                max_drawdown=max_drawdown,
                max_consecutive_losses=max_consecutive_losses,
                yearly_returns=yearly_returns,
            ),
        }

    def calculate_yearly_returns(self, executed: pd.DataFrame) -> dict[str, float]:
        yearly_returns = {}
        for year, group in executed.groupby("year"):
            daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            yearly_returns[str(year)] = self.compound_return(daily_returns)
        return dict(sorted(yearly_returns.items()))

    def calculate_robust_score(
        self,
        total_compound_return: float,
        sample_count: int,
        win_rate: float,
        max_drawdown: float,
        max_consecutive_losses: int,
        yearly_returns: dict[str, float],
    ) -> float:
        weights = self.robust_score_weights
        positive_year_ratio = (
            sum(value > 0 for value in yearly_returns.values()) / len(yearly_returns)
            if yearly_returns
            else 0.0
        )
        min_year_return = min(yearly_returns.values()) if yearly_returns else 0.0
        safe_return = max(total_compound_return, -0.99)
        return float(
            math.log1p(safe_return) * float(weights.get("log_return", 1.0))
            + min_year_return * float(weights.get("min_year_return", 1.0))
            + positive_year_ratio * float(weights.get("positive_year_ratio", 1.0))
            + min(sample_count / 500, 1.0) * float(weights.get("sample_count", 0.0))
            + win_rate * float(weights.get("win_rate", 0.0))
            - max_drawdown * float(weights.get("max_drawdown", 1.0))
            - max_consecutive_losses * float(weights.get("max_consecutive_losses", 0.0))
        )

    def build_yearly_rows(self, window_name: str, phase: str, selected: pd.DataFrame) -> list[dict[str, object]]:
        executed = self.select_executed(selected)
        rows = []
        for year, group in executed.groupby("year"):
            returns = group["net_return"].dropna()
            daily_returns = group.groupby("exit_trade_date")["daily_return"].sum().sort_index()
            rows.append(
                {
                    "window_name": window_name,
                    "phase": phase,
                    "year": str(year),
                    "sample_count": int(len(group)),
                    "year_return": self.compound_return(daily_returns),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
                    "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
                }
            )
        return rows

    def build_condition_stability(self, oos_candidate_rows: list[dict[str, object]]) -> pd.DataFrame:
        condition_rows = []
        for row in oos_candidate_rows:
            combo = self.parse_conditions(str(row.get("conditions", "")))
            for factor, value in combo:
                condition_rows.append(
                    {
                        "condition": f"{factor}={value}",
                        "factor": factor,
                        "value": value,
                        "window_name": str(row.get("window_name", "")),
                        "mode": str(row.get("mode", "")),
                        "train_rank": int(row.get("train_rank", 0)),
                        "sample_count": int(row.get("sample_count", 0)),
                        "total_compound_return": float(row.get("total_compound_return", 0.0)),
                        "win_rate": float(row.get("win_rate", 0.0)),
                        "avg_return_per_trade": float(row.get("avg_return_per_trade", 0.0)),
                        "max_drawdown": float(row.get("max_drawdown", 0.0)),
                        "max_consecutive_losses": int(row.get("max_consecutive_losses", 0)),
                        "train_robust_score": float(row.get("train_robust_score", 0.0)),
                    }
                )
        if not condition_rows:
            return pd.DataFrame()

        detail = pd.DataFrame(condition_rows)
        rows = []
        grouped = detail.groupby(["condition", "factor", "value"], dropna=False)
        for (condition, factor, value), group in grouped:
            returns = group["total_compound_return"].astype(float)
            positive_oos_ratio = float((returns > 0).mean()) if len(returns) else 0.0
            window_count = int(group["window_name"].nunique())
            rows.append(
                {
                    "condition": condition,
                    "factor": factor,
                    "value": value,
                    "window_count": window_count,
                    "candidate_count": int(len(group)),
                    "positive_oos_count": int((returns > 0).sum()),
                    "positive_oos_ratio": positive_oos_ratio,
                    "avg_oos_return": float(returns.mean()) if len(returns) else 0.0,
                    "median_oos_return": float(returns.median()) if len(returns) else 0.0,
                    "min_oos_return": float(returns.min()) if len(returns) else 0.0,
                    "max_oos_return": float(returns.max()) if len(returns) else 0.0,
                    "avg_oos_win_rate": float(group["win_rate"].mean()),
                    "avg_oos_drawdown": float(group["max_drawdown"].mean()),
                    "max_oos_drawdown": float(group["max_drawdown"].max()),
                    "avg_sample_count": float(group["sample_count"].mean()),
                    "avg_train_rank": float(group["train_rank"].mean()),
                    "avg_train_robust_score": float(group["train_robust_score"].mean()),
                    "is_stable": bool(
                        window_count >= self.stable_condition_min_windows
                        and positive_oos_ratio >= self.stable_condition_min_positive_oos_ratio
                        and float(returns.mean()) > 0
                    ),
                }
            )
        stability = pd.DataFrame(rows)
        return stability.sort_values(
            by=[
                "is_stable",
                "window_count",
                "positive_oos_ratio",
                "avg_oos_return",
                "max_oos_drawdown",
            ],
            ascending=[False, False, False, False, True],
        )

    @staticmethod
    def select_executed(data: pd.DataFrame) -> pd.DataFrame:
        if data.empty:
            return data.copy()
        return data[
            (data["buy_executed"] == True)  # noqa: E712
            & (data["sell_executed"] == True)  # noqa: E712
            & data["daily_return"].notna()
        ].copy()

    @staticmethod
    def has_duplicate_factor(combo: tuple[tuple[str, str], ...]) -> bool:
        return len({column for column, _ in combo}) != len(combo)

    @staticmethod
    def parse_conditions(text: str) -> tuple[tuple[str, str], ...]:
        if not text:
            return ()
        combo = []
        for item in text.split(";"):
            if "=" not in item:
                continue
            column, value = item.split("=", 1)
            combo.append((column, value))
        return tuple(combo)

    @staticmethod
    def format_conditions(combo: tuple[tuple[str, str], ...]) -> str:
        return ";".join(f"{column}={value}" for column, value in combo)

    @staticmethod
    def compound_return(returns: pd.Series) -> float:
        return float((1 + returns).prod() - 1) if len(returns) else 0.0


if __name__ == "__main__":
    main()
