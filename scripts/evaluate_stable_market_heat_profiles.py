from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy_optimizer import StrategyConditionOptimizer
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对比当前 A5 与稳定市场热度条件变体。")
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
    outputs = StableMarketHeatProfileEvaluator(config_path=args.config).evaluate()
    print("稳定市场热度条件变体对比完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class StableMarketHeatProfileEvaluator:
    """同一保守成交口径下，对比 A5 与稳定条件变体。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = PROJECT_ROOT
        self.config = load_json_config(config_path)
        self.logger = get_logger("stable_market_heat_profiles")
        self.replay_config = self.config.get("trade_replay", {})
        self.eval_config = self.config.get("stable_market_heat_profile_evaluation", {})
        self.output_trade_report_path = self.project_root / self.eval_config.get(
            "output_trade_report_path",
            "reports/stable_market_heat_profile_trades.csv",
        )
        self.output_summary_path = self.project_root / self.eval_config.get(
            "output_summary_path",
            "reports/stable_market_heat_profile_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.eval_config.get(
            "output_yearly_path",
            "reports/stable_market_heat_profile_yearly.csv",
        )
        self.replay_rule_name = str(self.eval_config.get("replay_rule", "fixed_t2_close"))

    def evaluate(self) -> dict[str, Path]:
        optimizer = StrategyConditionOptimizer(config_path="config/config.json")
        replay_engine = ConservativeTradeReplay(config_path="config/config.json")
        raw_trades = optimizer.load_trades()
        forward_prices = replay_engine.load_forward_prices()
        replay_rule = self.build_replay_rule()

        trade_frames = []
        summary_frames = []
        yearly_frames = []
        for profile in self.build_profiles():
            selected = self.build_selected(raw_trades, optimizer, profile)
            selected["planned_position_pct"] = replay_engine.position_pct
            samples = selected.merge(forward_prices, on=["trade_date", "ts_code"], how="left", validate="one_to_one")
            trades = replay_engine.replay_rule(samples, replay_rule)
            trades["profile_name"] = profile["profile_name"]
            trades["profile_description"] = profile["profile_description"]
            trade_frames.append(trades)

            summary = replay_engine.build_summary(trades)
            summary.insert(0, "profile_name", profile["profile_name"])
            summary.insert(1, "profile_description", profile["profile_description"])
            summary_frames.append(summary)

            yearly = replay_engine.build_yearly_report(trades)
            yearly.insert(0, "profile_name", profile["profile_name"])
            yearly.insert(1, "profile_description", profile["profile_description"])
            yearly_frames.append(yearly)
            self.logger.info("完成条件变体回放: %s, 信号数: %s", profile["profile_name"], len(selected))

        all_trades = pd.concat(trade_frames, ignore_index=True)
        all_summary = pd.concat(summary_frames, ignore_index=True)
        all_yearly = pd.concat(yearly_frames, ignore_index=True)
        all_summary = all_summary.sort_values(["final_equity", "max_drawdown"], ascending=[False, True])

        self.output_trade_report_path.parent.mkdir(parents=True, exist_ok=True)
        all_trades.to_csv(self.output_trade_report_path, index=False, encoding="utf-8-sig")
        all_summary.to_csv(self.output_summary_path, index=False, encoding="utf-8-sig")
        all_yearly.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")
        self.logger.info("稳定条件变体交易明细已生成: %s", self.output_trade_report_path)
        self.logger.info("稳定条件变体汇总已生成: %s", self.output_summary_path)
        self.logger.info("稳定条件变体年度报告已生成: %s", self.output_yearly_path)
        return {
            "trade_report": self.output_trade_report_path,
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
        }

    def build_profiles(self) -> list[dict[str, object]]:
        current_base_exclusions = deepcopy(self.replay_config.get("base_exclusions", {}))
        current_post_exclusions = deepcopy(self.replay_config.get("post_selection_exclusions", {}))
        stable_core_base_exclusions = {
            "limit_up_count_bucket": ["gte_180"],
            "market_chain_count_bucket": ["gte_30"],
            "market_leader_rank_bucket": ["rank_1"],
            "first_time_detail_bucket": ["1000_1100"],
            "turnover_rate_bucket": ["6_10"],
        }
        stable_core_post_exclusions = {
            "fd_ratio_bucket": ["2pct_5pct"],
            "market_segment": ["bj"],
        }
        stable_segment_base_exclusions = {
            **stable_core_base_exclusions,
            "segment_emotion_state_bucket": ["ice_point"],
            "segment_limit_up_ratio_bucket": ["1pct_2pct"],
            "segment_market_sentiment_level": ["neutral"],
        }
        no_retreat_post_exclusions = deepcopy(current_post_exclusions)
        no_retreat_post_exclusions.pop("segment_retreat_state_bucket", None)
        no_segment_chain_post_exclusions = deepcopy(current_post_exclusions)
        no_segment_chain_post_exclusions.pop("segment_chain_count_bucket", None)
        return [
            {
                "profile_name": "A5_current",
                "profile_description": "当前 A5 配置，作为基准。",
                "base_conditions": self.replay_config.get("base_conditions", {}),
                "base_exclusions": current_base_exclusions,
                "post_selection_exclusions": current_post_exclusions,
            },
            {
                "profile_name": "A5_without_segment_retreat_post_filter",
                "profile_description": "当前 A5 去掉板块退潮 weak_below_3 后置过滤。",
                "base_conditions": self.replay_config.get("base_conditions", {}),
                "base_exclusions": current_base_exclusions,
                "post_selection_exclusions": no_retreat_post_exclusions,
            },
            {
                "profile_name": "A5_without_segment_chain_post_filter",
                "profile_description": "当前 A5 去掉板块连板数 3_5 后置过滤。",
                "base_conditions": self.replay_config.get("base_conditions", {}),
                "base_exclusions": current_base_exclusions,
                "post_selection_exclusions": no_segment_chain_post_exclusions,
            },
            {
                "profile_name": "stable_core",
                "profile_description": "只保留 walk-forward 最稳定的市场总龙头和市场连板过热过滤。",
                "base_conditions": self.replay_config.get("base_conditions", {}),
                "base_exclusions": stable_core_base_exclusions,
                "post_selection_exclusions": stable_core_post_exclusions,
            },
            {
                "profile_name": "stable_core_plus_segment_heat_filters",
                "profile_description": "稳定核心条件叠加板块冰点、板块涨停占比和板块情绪过滤。",
                "base_conditions": self.replay_config.get("base_conditions", {}),
                "base_exclusions": stable_segment_base_exclusions,
                "post_selection_exclusions": stable_core_post_exclusions,
            },
        ]

    def build_selected(
        self,
        trades: pd.DataFrame,
        optimizer: StrategyConditionOptimizer,
        profile: dict[str, object],
    ) -> pd.DataFrame:
        filtered = trades.copy()
        for column, value in dict(profile["base_conditions"]).items():
            filtered = filtered[filtered[column].astype(str) == str(value)].copy()
        for column, values in dict(profile["base_exclusions"]).items():
            filtered = self.apply_exclusion(filtered, column, values)
        selected = optimizer.select_daily_candidates(filtered, max_holding_count=1)
        for column, values in dict(profile["post_selection_exclusions"]).items():
            selected = self.apply_exclusion(selected, column, values)
        return selected

    def build_replay_rule(self) -> ReplayRule:
        for rule in self.replay_config.get("fixed_exit_rules", []):
            if rule["rule_name"] == self.replay_rule_name:
                return ReplayRule(
                    rule_name=rule["rule_name"],
                    max_hold_days=int(rule["max_hold_days"]),
                    exit_price_field=rule.get("exit_price_field", "close"),
                )
        raise ValueError(f"未找到固定卖出规则: {self.replay_rule_name}")

    @staticmethod
    def apply_exclusion(data: pd.DataFrame, column: str, values: object) -> pd.DataFrame:
        if column not in data.columns:
            return data.copy()
        excluded_values = {str(value) for value in values}
        return data[~data[column].astype(str).isin(excluded_values)].copy()


if __name__ == "__main__":
    main()
