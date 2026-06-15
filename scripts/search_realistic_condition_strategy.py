from __future__ import annotations

import argparse
from itertools import combinations
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.optimize_large_universe_realistic_strategy import LargeUniverseRealisticStrategyOptimizer
from src.strategy_optimizer import StrategyConditionOptimizer
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按真实执行口径搜索大候选池条件组合策略。")
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
    outputs = RealisticConditionStrategySearch(config_path=args.config).search()
    print("真实执行条件组合搜索完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class RealisticConditionStrategySearch(LargeUniverseRealisticStrategyOptimizer):
    """在大候选池中搜索条件组合；每个组合都按真实执行口径重新复利。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        super().__init__(config_path=config_path)
        self.opt_config = self.config.get("realistic_condition_strategy_search", {})
        self.output_summary_path = self.project_root / self.opt_config.get(
            "output_summary_path",
            "reports/realistic_condition_strategy_search_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.opt_config.get(
            "output_yearly_path",
            "reports/realistic_condition_strategy_search_yearly.csv",
        )
        self.output_detail_path = self.project_root / self.opt_config.get(
            "output_detail_path",
            "reports/realistic_condition_strategy_search_detail.csv",
        )
        self.input_daily_merged_path = self.project_root / self.opt_config.get(
            "input_daily_merged_path",
            "data/processed/daily_merged.csv",
        )
        self.replay_rule_name = str(self.opt_config.get("replay_rule", "fixed_t2_close"))
        self.replay_max_hold_days = int(self.opt_config.get("replay_max_hold_days", 2))
        self.replay_exit_price_field = str(self.opt_config.get("replay_exit_price_field", "close"))
        self.initial_cash = float(self.opt_config.get("initial_cash", 500000))
        self.position_pct = float(self.opt_config.get("position_pct", 0.8))
        self.max_buy_amount_ratio = float(self.opt_config.get("max_buy_amount_ratio", 0.05))
        self.min_executed_trades = int(self.opt_config.get("min_executed_trades", 350))
        self.target_equity_multiple = float(self.opt_config.get("target_equity_multiple", 300.0))
        self.min_condition_raw_count = int(self.opt_config.get("min_condition_raw_count", 350))
        self.max_condition_raw_ratio = float(self.opt_config.get("max_condition_raw_ratio", 0.85))
        self.top_single_conditions = int(self.opt_config.get("top_single_conditions", 90))
        self.top_pair_conditions = int(self.opt_config.get("top_pair_conditions", 120))
        self.top_triple_conditions = int(self.opt_config.get("top_triple_conditions", 160))
        self.max_pair_sets = int(self.opt_config.get("max_pair_sets", 12000))
        self.max_triple_sets = int(self.opt_config.get("max_triple_sets", 15000))
        self.max_detail_scenarios = int(self.opt_config.get("max_detail_scenarios", 30))
        self.factor_columns = list(self.opt_config.get("factor_columns", []))
        self.sort_columns = list(self.opt_config.get("sort_columns", []))
        self.sort_ascending = list(self.opt_config.get("sort_ascending", []))
        if len(self.sort_ascending) != len(self.sort_columns):
            self.sort_ascending = [False] * len(self.sort_columns)

    def load_candidates(self) -> pd.DataFrame:
        optimizer = StrategyConditionOptimizer(
            config_path=self.config_path,
            optimization_config_key="realistic_condition_strategy_search",
        )
        candidates = optimizer.load_trades()
        candidates = self.apply_date_filter(candidates)
        candidates = self.apply_base_filters(candidates)
        candidates = self.ensure_sort_columns(candidates)
        if candidates.empty:
            raise RuntimeError("真实执行条件搜索候选池为空，请检查 next_day_premium_trades.csv 和配置过滤条件。")
        return candidates.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def search(self) -> dict[str, Path]:
        candidates = self.load_candidates()
        replayed = self.attach_daily_liquidity(self.replay_candidates(candidates))
        condition_candidates = self.build_condition_candidates(replayed)
        self.logger.info(
            "开始真实执行条件组合搜索，候选条件: %s, 候选样本: %s",
            len(condition_candidates),
            len(replayed),
        )

        scored: list[tuple[dict[str, Any], pd.DataFrame]] = []
        single_results = self.evaluate_condition_sets(replayed, [(condition,) for condition in condition_candidates])
        scored.extend(single_results)
        top_single = [item[0]["conditions_tuple"] for item in single_results[: self.top_single_conditions]]
        pair_sets = self.build_combo_sets(top_single, combo_size=2)[: self.max_pair_sets]
        pair_results = self.evaluate_condition_sets(replayed, pair_sets)
        scored.extend(pair_results)
        top_pairs = [item[0]["conditions_tuple"] for item in pair_results[: self.top_pair_conditions]]
        pair_flat = list(dict.fromkeys(condition for combo in top_pairs for condition in combo))
        triple_sets = self.build_combo_sets(pair_flat, combo_size=3)[: self.max_triple_sets]
        triple_results = self.evaluate_condition_sets(replayed, triple_sets)
        scored.extend(triple_results[: self.top_triple_conditions])

        if not scored:
            raise RuntimeError("没有找到可评估的条件组合。")

        summary = pd.DataFrame([self.without_internal_fields(item[0]) for item in scored]).sort_values(
            [
                "hit_user_target",
                "ranking_score",
                "equity_multiple",
                "executed_trade_count",
                "max_drawdown",
            ],
            ascending=[False, False, False, False, True],
        ).reset_index(drop=True)

        keep_names = set(summary.head(self.max_detail_scenarios)["scenario"].astype(str))
        yearly_rows: list[dict[str, Any]] = []
        detail_frames: list[pd.DataFrame] = []
        for scenario_summary, simulated in scored:
            scenario_name = str(scenario_summary["scenario"])
            if scenario_name not in keep_names:
                continue
            scenario_rank = int(summary.index[summary["scenario"] == scenario_name][0]) + 1
            yearly_rows.extend(self.build_yearly_rows(simulated, scenario_name))
            detail = simulated.copy()
            detail["scenario_rank"] = scenario_rank
            detail_frames.append(detail)

        yearly = pd.DataFrame(yearly_rows)
        detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
        self.output_summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        detail.to_csv(self.output_detail_path, index=False, encoding="utf-8-sig")
        self.logger.info("真实执行条件组合汇总已生成: %s, 行数: %s", self.output_summary_path, len(summary))
        self.logger.info("真实执行条件组合年度报告已生成: %s, 行数: %s", self.output_yearly_path, len(yearly))
        self.logger.info("真实执行条件组合明细已生成: %s, 行数: %s", self.output_detail_path, len(detail))
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "detail": self.output_detail_path,
        }

    def build_condition_candidates(self, trades: pd.DataFrame) -> list[tuple[str, str]]:
        conditions: list[tuple[str, str]] = []
        max_count = len(trades) * self.max_condition_raw_ratio
        for factor in self.factor_columns:
            if factor not in trades.columns:
                continue
            values = trades[factor].fillna("missing").astype(str).value_counts()
            for value, count in values.items():
                if value in {"missing", "nan", "None", "unknown"}:
                    continue
                if count < self.min_condition_raw_count or count > max_count:
                    continue
                conditions.append((factor, str(value)))
        return conditions

    def evaluate_condition_sets(
        self,
        trades: pd.DataFrame,
        condition_sets: list[tuple[tuple[str, str], ...]],
    ) -> list[tuple[dict[str, Any], pd.DataFrame]]:
        scored: list[tuple[dict[str, Any], pd.DataFrame]] = []
        for index, conditions in enumerate(condition_sets, start=1):
            matched = self.apply_inclusion_conditions(trades, conditions)
            if matched.empty:
                continue
            selected = self.select_daily_top_by_config(matched)
            simulated = self.simulate_single_position(selected, [self.conditions_to_name(conditions)], [False])
            summary = self.summarize_scenario(simulated, [self.conditions_to_name(conditions)], [False])
            summary["scenario"] = "condition|" + self.conditions_to_name(conditions)
            summary["conditions"] = self.conditions_to_name(conditions)
            summary["condition_count"] = len(conditions)
            summary["matched_candidate_count"] = int(len(matched))
            summary["matched_signal_days"] = int(matched["trade_date"].nunique())
            summary["conditions_tuple"] = conditions
            scored.append((summary, simulated))
            if index % 300 == 0:
                self.logger.info("条件组合扫描进度: %s/%s", index, len(condition_sets))
        return sorted(
            scored,
            key=lambda item: (
                bool(item[0]["hit_user_target"]),
                float(item[0]["ranking_score"]),
                float(item[0]["equity_multiple"]),
                int(item[0]["executed_trade_count"]),
            ),
            reverse=True,
        )

    def select_daily_top_by_config(self, candidates: pd.DataFrame) -> pd.DataFrame:
        sort_columns = [column for column in self.sort_columns if column in candidates.columns]
        if not sort_columns:
            sort_columns = ["fill_probability"]
        ascending = self.sort_ascending[: len(sort_columns)]
        selected = candidates.sort_values(
            ["trade_date"] + sort_columns,
            ascending=[True] + ascending,
            na_position="last",
        )
        selected = selected.groupby("trade_date", as_index=False).head(1).copy()
        return selected.sort_values(["buy_trade_date", "trade_date", "ts_code"]).reset_index(drop=True)

    @staticmethod
    def apply_inclusion_conditions(
        trades: pd.DataFrame,
        conditions: tuple[tuple[str, str], ...],
    ) -> pd.DataFrame:
        result = trades
        for factor, value in conditions:
            if factor not in result.columns:
                return result.iloc[0:0].copy()
            result = result[result[factor].fillna("missing").astype(str) == str(value)]
        return result.copy()

    @staticmethod
    def build_combo_sets(
        condition_candidates: list[tuple[str, str]] | list[tuple[tuple[str, str], ...]],
        combo_size: int,
    ) -> list[tuple[tuple[str, str], ...]]:
        flat_conditions: list[tuple[str, str]] = []
        for item in condition_candidates:
            if not item:
                continue
            if isinstance(item[0], tuple):  # type: ignore[index]
                flat_conditions.extend(item)  # type: ignore[arg-type]
            else:
                flat_conditions.append(item)  # type: ignore[arg-type]
        flat_conditions = list(dict.fromkeys(flat_conditions))
        combos: list[tuple[tuple[str, str], ...]] = []
        for combo in combinations(flat_conditions, combo_size):
            factors = [condition[0] for condition in combo]
            if len(set(factors)) != len(factors):
                continue
            combos.append(combo)
        return combos

    @staticmethod
    def conditions_to_name(conditions: tuple[tuple[str, str], ...]) -> str:
        return ";".join(f"{factor}={value}" for factor, value in conditions)

    @staticmethod
    def without_internal_fields(summary: dict[str, Any]) -> dict[str, Any]:
        result = dict(summary)
        result.pop("conditions_tuple", None)
        return result


if __name__ == "__main__":
    main()
