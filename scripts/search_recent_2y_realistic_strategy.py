from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.search_realistic_condition_strategy import RealisticConditionStrategySearch
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按最近 2 年滚动窗口搜索真实执行口径策略。")
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

    runner = Recent2YRealisticStrategySearch(config_path=args.config)
    outputs = runner.search()
    print("最近 2 年真实执行口径策略搜索完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class Recent2YRealisticStrategySearch(RealisticConditionStrategySearch):
    """最近 2 年滚动窗口策略搜索；仅覆盖窗口、目标和输出路径。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        super().__init__(config_path=config_path)
        recent_config = self.config.get("recent_2y_realistic_condition_search", {})
        self.start_date = self.normalize_date(recent_config.get("start_date", self.start_date))
        self.end_date = self.normalize_date(recent_config.get("end_date", self.end_date))
        self.target_equity_multiple = float(
            recent_config.get("target_equity_multiple", self.target_equity_multiple)
        )
        self.min_executed_trades = int(recent_config.get("min_executed_trades", self.min_executed_trades))
        self.min_condition_raw_count = int(
            recent_config.get("min_condition_raw_count", self.min_condition_raw_count)
        )
        self.top_single_conditions = int(
            recent_config.get("top_single_conditions", self.top_single_conditions)
        )
        self.top_pair_conditions = int(recent_config.get("top_pair_conditions", self.top_pair_conditions))
        self.top_triple_conditions = int(
            recent_config.get("top_triple_conditions", self.top_triple_conditions)
        )
        self.max_pair_sets = int(recent_config.get("max_pair_sets", self.max_pair_sets))
        self.max_triple_sets = int(recent_config.get("max_triple_sets", self.max_triple_sets))
        self.max_detail_scenarios = int(
            recent_config.get("max_detail_scenarios", self.max_detail_scenarios)
        )
        self.output_summary_path = self.project_root / recent_config.get(
            "output_summary_path",
            "reports/recent_2y_realistic_condition_search_summary.csv",
        )
        self.output_yearly_path = self.project_root / recent_config.get(
            "output_yearly_path",
            "reports/recent_2y_realistic_condition_search_yearly.csv",
        )
        self.output_detail_path = self.project_root / recent_config.get(
            "output_detail_path",
            "reports/recent_2y_realistic_condition_search_detail.csv",
        )
        self.output_four_factor_probe_path = self.project_root / recent_config.get(
            "output_four_factor_probe_path",
            "reports/recent_2y_realistic_condition_search_four_factor_probe.csv",
        )

    def search(self) -> dict[str, Path]:
        outputs = super().search()
        self.run_four_factor_probe()
        outputs["four_factor_probe"] = self.output_four_factor_probe_path
        return outputs

    def run_four_factor_probe(self) -> None:
        if not self.output_summary_path.exists():
            return
        summary = pd.read_csv(self.output_summary_path)
        if summary.empty or "conditions" not in summary.columns:
            return

        candidates = self.load_candidates()
        replayed = self.attach_daily_liquidity(self.replay_candidates(candidates))
        condition_candidates = self.build_condition_candidates(replayed)
        base_conditions = []
        for text in summary["conditions"].dropna().head(self.max_detail_scenarios):
            combo = []
            for part in str(text).split(";"):
                if "=" not in part:
                    continue
                combo.append(tuple(part.split("=", 1)))
            if combo:
                base_conditions.append(tuple(combo))

        sets = []
        seen = set()
        for base in base_conditions:
            base_factors = {condition[0] for condition in base}
            for condition in condition_candidates:
                if condition[0] in base_factors:
                    continue
                combo = tuple(list(base) + [condition])
                key = self.conditions_to_name(combo)
                if key in seen:
                    continue
                seen.add(key)
                sets.append(combo)

        results = self.evaluate_condition_sets(replayed, sets[: self.max_triple_sets])
        probe = pd.DataFrame([self.without_internal_fields(item[0]) for item in results])
        if not probe.empty:
            probe = probe.sort_values(
                ["hit_user_target", "ranking_score", "equity_multiple", "max_drawdown"],
                ascending=[False, False, False, True],
            )
        self.output_four_factor_probe_path.parent.mkdir(parents=True, exist_ok=True)
        probe.to_csv(self.output_four_factor_probe_path, index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
