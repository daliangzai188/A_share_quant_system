from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.search_recent_2y_realistic_strategy import Recent2YRealisticStrategySearch
from src.factors import NextDayPremiumAnalyzer
from src.trade_replay import ConservativeTradeReplay, ReplayRule
from src.utils.config import load_json_config
from src.utils.logger import get_logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="最近 2 年条件、排序、卖出规则完整策略优化。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--top-condition-sets", type=int, default=None, help="覆盖配置里的条件组合数量上限。")
    parser.add_argument("--limit-sort-rules", type=int, default=None, help="只扫描前 N 个排序规则，用于快速试跑。")
    parser.add_argument("--limit-exit-rules", type=int, default=None, help="只扫描前 N 个卖出规则，用于快速试跑。")
    parser.add_argument("--progress-interval", type=int, default=100, help="每 N 个组合输出一次进度。")
    parser.add_argument("--output-prefix", default=None, help="覆盖输出文件前缀，避免覆盖基准报告。")
    parser.add_argument("--exclude-st", action="store_true", help="事前排除 ST、*ST、退市风险名称。")
    parser.add_argument(
        "--penalize-limit-down-blocked",
        action="store_true",
        help="对发生跌停延迟卖出的交易使用保守股票收益惩罚，不事后跳过交易。",
    )
    parser.add_argument(
        "--limit-down-block-stock-return",
        type=float,
        default=-0.2,
        help="发生跌停延迟卖出时使用的保守股票收益，默认 -20%。",
    )
    parser.add_argument(
        "--exclude-condition",
        action="append",
        default=[],
        help="事前排除候选条件，格式 column=value，可重复传入。",
    )
    parser.add_argument(
        "--exclude-rule",
        action="append",
        default=[],
        help="事前排除候选复合规则，格式 column=value&&column2=value2，可重复传入；规则内为 AND，规则之间为 OR。",
    )
    parser.add_argument(
        "--include-condition",
        action="append",
        default=[],
        help="事前只保留候选条件，格式 column=value，可重复传入；多个条件为 AND。",
    )
    parser.add_argument(
        "--include-rule",
        action="append",
        default=[],
        help="事前只保留候选复合规则，格式 column=value&&column2=value2，可重复传入；规则内为 AND，规则之间为 OR。",
    )
    parser.add_argument(
        "--append-profit-source-sort-rules",
        action="store_true",
        help="追加收益来源加权排序规则，用于验证不硬过滤候选的增强排序。",
    )
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
    outputs = Recent2YFullStrategyOptimizer(
        config_path=args.config,
        top_condition_sets=args.top_condition_sets,
        limit_sort_rules=args.limit_sort_rules,
        limit_exit_rules=args.limit_exit_rules,
        progress_interval=args.progress_interval,
        output_prefix=args.output_prefix,
        exclude_st=args.exclude_st,
        penalize_limit_down_blocked=args.penalize_limit_down_blocked,
        limit_down_block_stock_return=args.limit_down_block_stock_return,
        exclude_conditions=args.exclude_condition,
        exclude_rules=args.exclude_rule,
        include_conditions=args.include_condition,
        include_rules=args.include_rule,
        append_profit_source_sort_rules=args.append_profit_source_sort_rules,
    ).optimize()
    print("最近 2 年完整策略优化完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


class Recent2YFullStrategyOptimizer(Recent2YRealisticStrategySearch):
    """穷举最近 2 年高分条件组合、排序规则和卖出规则矩阵。"""

    def __init__(
        self,
        config_path: str | Path = "config/config.json",
        top_condition_sets: int | None = None,
        limit_sort_rules: int | None = None,
        limit_exit_rules: int | None = None,
        progress_interval: int = 100,
        output_prefix: str | None = None,
        exclude_st: bool = False,
        penalize_limit_down_blocked: bool = False,
        limit_down_block_stock_return: float = -0.2,
        exclude_conditions: list[str] | None = None,
        exclude_rules: list[str] | None = None,
        include_conditions: list[str] | None = None,
        include_rules: list[str] | None = None,
        append_profit_source_sort_rules: bool = False,
    ) -> None:
        super().__init__(config_path=config_path)
        self.full_config = self.config.get("recent_2y_full_strategy_optimization", {})
        self.condition_summary_paths = [
            self.project_root / path
            for path in self.full_config.get("input_condition_summary_paths", [])
        ]
        self.output_summary_path = self.project_root / self.full_config.get(
            "output_summary_path",
            "reports/recent_2y_full_strategy_optimization_summary.csv",
        )
        self.output_yearly_path = self.project_root / self.full_config.get(
            "output_yearly_path",
            "reports/recent_2y_full_strategy_optimization_yearly.csv",
        )
        self.output_detail_path = self.project_root / self.full_config.get(
            "output_detail_path",
            "reports/recent_2y_full_strategy_optimization_detail.csv",
        )
        if output_prefix:
            output_path = self.project_root / output_prefix
            self.output_summary_path = output_path.with_name(output_path.name + "_summary.csv")
            self.output_yearly_path = output_path.with_name(output_path.name + "_yearly.csv")
            self.output_detail_path = output_path.with_name(output_path.name + "_detail.csv")
        self.target_equity_multiple = float(
            self.full_config.get("target_equity_multiple", self.target_equity_multiple)
        )
        self.top_condition_sets = top_condition_sets or int(self.full_config.get("top_condition_sets", 300))
        self.max_detail_scenarios = int(self.full_config.get("max_detail_scenarios", 40))
        self.sort_rules = list(self.full_config.get("sort_rules", []))
        if append_profit_source_sort_rules:
            self.sort_rules.extend(self.build_profit_source_sort_rules())
        self.exit_rules = list(self.full_config.get("exit_rules", []))
        if limit_sort_rules:
            self.sort_rules = self.sort_rules[:limit_sort_rules]
        if limit_exit_rules:
            self.exit_rules = self.exit_rules[:limit_exit_rules]
        self.progress_interval = max(1, int(progress_interval))
        self.exclude_st = exclude_st
        self.penalize_limit_down_blocked = penalize_limit_down_blocked
        self.limit_down_block_stock_return = float(limit_down_block_stock_return)
        self.exclude_conditions = self.parse_exclude_conditions(exclude_conditions or [])
        self.exclude_rules = self.parse_exclude_rules(exclude_rules or [])
        self.include_conditions = self.parse_exclude_conditions(include_conditions or [])
        self.include_rules = self.parse_exclude_rules(include_rules or [])

    def optimize(self) -> dict[str, Path]:
        candidates = self.load_candidates()
        candidates = self.apply_strict_candidate_filters(candidates)
        candidates = self.apply_candidate_inclusions(candidates)
        candidates = self.apply_candidate_exclusions(candidates)
        condition_sets = self.load_condition_sets()
        daily_amount_map = self.load_daily_amount_map()
        replay_engine = ConservativeTradeReplay(config_path=self.config_path)
        replay_engine.position_pct = self.position_pct
        replay_engine.max_hold_days = max(
            int(rule.get("max_hold_days", 2)) for rule in self.exit_rules
        )
        forward_prices = replay_engine.load_forward_prices()
        base_samples = candidates.merge(
            forward_prices,
            on=["trade_date", "ts_code"],
            how="left",
            validate="one_to_one",
        )
        replayed_by_exit_rule = self.precompute_exit_replays(
            replay_engine=replay_engine,
            samples=base_samples,
            daily_amount_map=daily_amount_map,
        )

        self.logger.info(
            "开始最近2年完整策略优化，候选: %s, 条件组合: %s, 排序规则: %s, 卖出规则: %s",
            len(candidates),
            len(condition_sets),
            len(self.sort_rules),
            len(self.exit_rules),
        )

        scored: list[tuple[dict[str, Any], pd.DataFrame]] = []
        total = len(condition_sets) * len(self.sort_rules) * len(self.exit_rules)
        progress = 0
        for conditions in condition_sets:
            for replay_rule_name, replayed_all in replayed_by_exit_rule.items():
                matched = self.apply_inclusion_conditions(replayed_all, conditions)
                if matched.empty:
                    continue
                for sort_rule in self.sort_rules:
                    progress += 1
                    selected = self.select_by_sort_rule(matched, sort_rule)
                    if selected.empty:
                        continue
                    scenario_name = self.format_scenario_name(conditions, sort_rule, replay_rule_name)
                    simulated = self.simulate_single_position(selected, [scenario_name], [False])
                    summary = self.summarize_scenario(simulated, [scenario_name], [False])
                    summary.update(
                        {
                            "scenario": scenario_name,
                            "conditions": self.conditions_to_name(conditions),
                            "condition_count": len(conditions),
                            "sort_rule": sort_rule.get("name", ""),
                            "exit_rule": replay_rule_name,
                            "matched_candidate_count": int(len(matched)),
                            "matched_signal_days": int(matched["trade_date"].nunique()),
                        }
                    )
                    scored.append((summary, simulated))
                    if progress % self.progress_interval == 0:
                        self.logger.info("完整策略优化进度: %s/%s", progress, total)

        if not scored:
            raise RuntimeError("没有可评估的完整策略组合。")

        summary = pd.DataFrame([item[0] for item in scored]).sort_values(
            ["hit_user_target", "ranking_score", "equity_multiple", "max_drawdown"],
            ascending=[False, False, False, True],
        ).reset_index(drop=True)
        keep_names = set(summary.head(self.max_detail_scenarios)["scenario"].astype(str))
        yearly_rows: list[dict[str, Any]] = []
        detail_frames: list[pd.DataFrame] = []
        for summary_row, simulated in scored:
            scenario_name = str(summary_row["scenario"])
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
        self.logger.info("最近2年完整策略优化汇总已生成: %s, 行数: %s", self.output_summary_path, len(summary))
        self.logger.info("最近2年完整策略优化年度报告已生成: %s, 行数: %s", self.output_yearly_path, len(yearly))
        self.logger.info("最近2年完整策略优化明细已生成: %s, 行数: %s", self.output_detail_path, len(detail))
        return {
            "summary": self.output_summary_path,
            "yearly": self.output_yearly_path,
            "detail": self.output_detail_path,
        }

    def apply_strict_candidate_filters(self, candidates: pd.DataFrame) -> pd.DataFrame:
        if not self.exclude_st:
            return candidates
        before = len(candidates)
        result = candidates.copy()
        name = result.get("name", pd.Series("", index=result.index)).fillna("").astype(str).str.upper()
        is_st = result.get("is_st", pd.Series(False, index=result.index)).astype(str).str.lower().isin({"true", "1"})
        blocked = is_st | name.str.contains("ST", na=False) | name.str.contains("退", na=False)
        result = result[~blocked].copy()
        self.logger.info("严格过滤 ST/*ST/退市风险: %s -> %s, 删除: %s", before, len(result), before - len(result))
        return result

    @staticmethod
    def parse_exclude_conditions(values: list[str]) -> list[tuple[str, str]]:
        conditions: list[tuple[str, str]] = []
        for value in values:
            if "=" not in value:
                raise ValueError(f"--exclude-condition 格式必须是 column=value: {value}")
            column, expected = value.split("=", 1)
            column = column.strip()
            expected = expected.strip()
            if not column or not expected:
                raise ValueError(f"--exclude-condition 格式必须是 column=value: {value}")
            conditions.append((column, expected))
        return conditions

    @classmethod
    def parse_exclude_rules(cls, values: list[str]) -> list[tuple[tuple[str, str], ...]]:
        rules: list[tuple[tuple[str, str], ...]] = []
        for value in values:
            parts = [part.strip() for part in value.split("&&") if part.strip()]
            if not parts:
                raise ValueError(f"--exclude-rule 不能为空: {value}")
            rules.append(tuple(cls.parse_exclude_conditions(parts)))
        return rules

    def apply_candidate_exclusions(self, candidates: pd.DataFrame) -> pd.DataFrame:
        if not self.exclude_conditions and not self.exclude_rules:
            return candidates
        result = candidates.copy()
        before = len(result)
        for column, expected in self.exclude_conditions:
            if column not in result.columns:
                self.logger.info("候选排除条件字段不存在，跳过: %s=%s", column, expected)
                continue
            result = result[result[column].fillna("missing").astype(str) != expected].copy()
        for rule in self.exclude_rules:
            mask = pd.Series(True, index=result.index)
            missing_columns = []
            for column, expected in rule:
                if column not in result.columns:
                    missing_columns.append(column)
                    continue
                mask &= result[column].fillna("missing").astype(str) == expected
            if missing_columns:
                self.logger.info(
                    "候选排除复合规则字段不存在，跳过: %s, 规则: %s",
                    ",".join(missing_columns),
                    self.format_exclude_rule(rule),
                )
                continue
            result = result[~mask].copy()
        self.logger.info(
            "事前候选排除条件已应用: %s -> %s, 删除: %s, 条件: %s",
            before,
            len(result),
            before - len(result),
            ";".join(
                [f"{column}={value}" for column, value in self.exclude_conditions]
                + [self.format_exclude_rule(rule) for rule in self.exclude_rules]
            ),
        )
        return result

    @staticmethod
    def format_exclude_rule(rule: tuple[tuple[str, str], ...]) -> str:
        return "&&".join(f"{column}={value}" for column, value in rule)

    def apply_candidate_inclusions(self, candidates: pd.DataFrame) -> pd.DataFrame:
        if not self.include_conditions and not self.include_rules:
            return candidates
        result = candidates.copy()
        before = len(result)
        for column, expected in self.include_conditions:
            if column not in result.columns:
                self.logger.info("候选保留条件字段不存在，跳过: %s=%s", column, expected)
                continue
            result = result[result[column].fillna("missing").astype(str) == expected].copy()
        if self.include_rules:
            keep_mask = pd.Series(False, index=result.index)
            applied_rules = []
            for rule in self.include_rules:
                mask = pd.Series(True, index=result.index)
                missing_columns = []
                for column, expected in rule:
                    if column not in result.columns:
                        missing_columns.append(column)
                        continue
                    mask &= result[column].fillna("missing").astype(str) == expected
                if missing_columns:
                    self.logger.info(
                        "候选保留复合规则字段不存在，跳过: %s, 规则: %s",
                        ",".join(missing_columns),
                        self.format_exclude_rule(rule),
                    )
                    continue
                keep_mask |= mask
                applied_rules.append(rule)
            if applied_rules:
                result = result[keep_mask].copy()
        self.logger.info(
            "事前候选保留条件已应用: %s -> %s, 删除: %s, 条件: %s",
            before,
            len(result),
            before - len(result),
            ";".join(
                [f"{column}={value}" for column, value in self.include_conditions]
                + [self.format_exclude_rule(rule) for rule in self.include_rules]
            ),
        )
        return result

    def precompute_exit_replays(
        self,
        replay_engine: ConservativeTradeReplay,
        samples: pd.DataFrame,
        daily_amount_map: dict[str, float],
    ) -> dict[str, pd.DataFrame]:
        replayed_by_exit_rule: dict[str, pd.DataFrame] = {}
        for exit_rule_config in self.exit_rules:
            replay_rule = self.build_replay_rule(exit_rule_config)
            replayed = replay_engine.replay_rule(samples, replay_rule)
            replayed = self.apply_limit_down_block_penalty(replayed)
            replayed = self.attach_daily_liquidity_from_cache(replayed, daily_amount_map)
            replayed_by_exit_rule[replay_rule.rule_name] = replayed
            self.logger.info(
                "卖出规则预回放完成: %s, 候选: %s, 买入被拒: %s, 卖出未完成: %s",
                replay_rule.rule_name,
                len(replayed),
                int((replayed["buy_executed"] == False).sum()),  # noqa: E712
                int(((replayed["buy_executed"] == True) & (replayed["sell_executed"] == False)).sum()),  # noqa: E712
            )
        return replayed_by_exit_rule

    def apply_limit_down_block_penalty(self, replayed: pd.DataFrame) -> pd.DataFrame:
        if not self.penalize_limit_down_blocked or "limit_down_blocked_days" not in replayed.columns:
            return replayed
        result = replayed.copy()
        blocked = (
            result["buy_executed"].fillna(False).astype(bool)
            & result["sell_executed"].fillna(False).astype(bool)
            & (pd.to_numeric(result["limit_down_blocked_days"], errors="coerce").fillna(0) > 0)
        )
        if not blocked.any():
            return result
        result.loc[blocked, "exit_price_before_slippage"] = (
            pd.to_numeric(result.loc[blocked, "buy_price_before_slippage"], errors="coerce")
            * (1.0 + self.limit_down_block_stock_return)
        )
        result.loc[blocked, "exit_price"] = result.loc[blocked, "exit_price_before_slippage"]
        result.loc[blocked, "exit_reason"] = (
            result.loc[blocked, "exit_reason"].fillna("").astype(str)
            + f"_penalized_limit_down_block_{self.limit_down_block_stock_return:.2f}"
        )
        self.logger.info(
            "跌停延迟卖出惩罚已应用: 笔数=%s, 保守股票收益=%.2f",
            int(blocked.sum()),
            self.limit_down_block_stock_return,
        )
        return result

    def load_condition_sets(self) -> list[tuple[tuple[str, str], ...]]:
        frames = []
        for path in self.condition_summary_paths:
            if path.exists():
                frames.append(pd.read_csv(path))
        if not frames:
            raise RuntimeError("缺少最近2年条件搜索报告，请先运行 search_recent_2y_realistic_strategy.py。")
        data = pd.concat(frames, ignore_index=True)
        data = data.dropna(subset=["conditions"]).copy()
        data = data.sort_values(["equity_multiple", "max_drawdown"], ascending=[False, True])
        condition_sets: list[tuple[tuple[str, str], ...]] = []
        seen = set()
        for text in data["conditions"].astype(str):
            combo = []
            for part in text.split(";"):
                if "=" not in part:
                    continue
                combo.append(tuple(part.split("=", 1)))
            if not combo:
                continue
            key = self.conditions_to_name(tuple(combo))
            if key in seen:
                continue
            seen.add(key)
            condition_sets.append(tuple(combo))
            if len(condition_sets) >= self.top_condition_sets:
                break
        return condition_sets

    def load_daily_amount_map(self) -> dict[str, float]:
        daily_amount_lookup_path = self.project_root / self.full_config.get(
            "daily_amount_lookup_path",
            "data/processed/daily_amount_lookup.csv",
        )
        if daily_amount_lookup_path.exists():
            self.logger.info("使用日成交额轻量查询表: %s", daily_amount_lookup_path)
            daily = pd.read_csv(
                daily_amount_lookup_path,
                dtype={"trade_date": str, "ts_code": str},
                usecols=["trade_date", "ts_code", "amount_yuan"],
                low_memory=False,
            )
        else:
            self.logger.info("日成交额轻量查询表不存在，回退读取日线合并表: %s", self.input_daily_merged_path)
            daily = pd.read_csv(
                self.input_daily_merged_path,
                dtype={"trade_date": str, "ts_code": str},
                usecols=["trade_date", "ts_code", "amount"],
                low_memory=False,
            )
            daily["amount_yuan"] = pd.to_numeric(daily["amount"], errors="coerce") * 1000
        daily["lookup_key"] = daily["trade_date"].astype(str) + "|" + daily["ts_code"].astype(str)
        return dict(zip(daily["lookup_key"], daily["amount_yuan"]))

    @staticmethod
    def attach_daily_liquidity_from_cache(trades: pd.DataFrame, daily_amount_map: dict[str, float]) -> pd.DataFrame:
        result = trades.copy()
        ts_code = result["ts_code"].astype(str)
        buy_keys = result["buy_trade_date"].astype(str) + "|" + ts_code
        sell_keys = result["exit_trade_date"].astype(str) + "|" + ts_code
        result["buy_day_amount_yuan"] = buy_keys.map(daily_amount_map)
        result["sell_day_amount_yuan"] = sell_keys.map(daily_amount_map)
        return result

    @staticmethod
    def build_replay_rule(config: dict[str, Any]) -> ReplayRule:
        return ReplayRule(
            rule_name=str(config["rule_name"]),
            max_hold_days=int(config["max_hold_days"]),
            exit_price_field=str(config.get("exit_price_field", "close")),
            stop_loss=float(config["stop_loss"]) if "stop_loss" in config else None,
            take_profit=float(config["take_profit"]) if "take_profit" in config else None,
        )

    @staticmethod
    def build_profit_source_sort_rules() -> list[dict[str, Any]]:
        return [
            {
                "name": "profit_source_balanced_turnover",
                "score_rules": [
                    {"column": "market_emotion_state_bucket", "values": ["mixed"], "weight": 2.0},
                    {"column": "market_limit_down_count_bucket", "values": ["5_15"], "weight": 1.5},
                    {"column": "amount_ratio_bucket", "values": ["1_2_2"], "weight": 1.5},
                    {"column": "turnover_rate_bucket", "values": ["gte_25", "10_15"], "weight": 1.0},
                    {"column": "market_segment", "values": ["chi_next"], "weight": 1.0},
                    {"column": "market_segment", "values": ["star"], "weight": -2.0},
                    {"column": "amount_bucket", "values": ["8e8_15e8"], "weight": -1.5},
                    {"column": "limit_height_rank_bucket", "values": ["rank_4_10"], "weight": -1.0},
                ],
                "columns": ["profit_source_score", "turnover_rate"],
                "ascending": [False, False],
            },
            {
                "name": "profit_source_oos_resilient",
                "score_rules": [
                    {"column": "retreat_state_bucket", "values": ["neutral"], "weight": 2.0},
                    {"column": "market_limit_down_count_bucket", "values": ["5_15"], "weight": 1.5},
                    {"column": "limit_up_count_bucket", "values": ["50_80"], "weight": 1.0},
                    {"column": "first_time_detail_bucket", "values": ["1100_1330"], "weight": 0.8},
                    {"column": "amount_ratio_bucket", "values": ["gte_5"], "weight": -1.5},
                    {"column": "market_segment", "values": ["star"], "weight": -1.5},
                    {"column": "turnover_rate_bucket", "values": ["6_10"], "weight": -1.0},
                ],
                "columns": ["profit_source_score", "turnover_rate"],
                "ascending": [False, False],
            },
            {
                "name": "profit_source_quality_liquidity",
                "score_rules": [
                    {"column": "amount_ratio_bucket", "values": ["1_2_2"], "weight": 2.0},
                    {"column": "turnover_rate_bucket", "values": ["gte_25", "10_15"], "weight": 1.5},
                    {"column": "open_times_bucket", "values": ["0"], "weight": 1.0},
                    {"column": "market_emotion_state_bucket", "values": ["mixed"], "weight": 1.0},
                    {"column": "volume_ratio_bucket", "values": ["2_4"], "weight": -1.0},
                    {"column": "amount_bucket", "values": ["1e8_3e8", "8e8_15e8"], "weight": -1.0},
                    {"column": "limit_height_rank_bucket", "values": ["rank_4_10"], "weight": -1.0},
                ],
                "columns": ["profit_source_score", "amount"],
                "ascending": [False, False],
            },
            {
                "name": "profit_source_positive_only",
                "score_rules": [
                    {"column": "market_emotion_state_bucket", "values": ["mixed"], "weight": 1.0},
                    {"column": "market_limit_down_count_bucket", "values": ["5_15"], "weight": 1.0},
                    {"column": "amount_ratio_bucket", "values": ["1_2_2"], "weight": 1.0},
                    {"column": "turnover_rate_bucket", "values": ["gte_25", "10_15"], "weight": 1.0},
                    {"column": "market_segment", "values": ["chi_next"], "weight": 1.0},
                ],
                "columns": ["profit_source_score", "turnover_rate"],
                "ascending": [False, False],
            },
            {
                "name": "profit_source_risk_penalty_only",
                "score_rules": [
                    {"column": "market_segment", "values": ["star"], "weight": -2.0},
                    {"column": "amount_bucket", "values": ["8e8_15e8"], "weight": -1.5},
                    {"column": "limit_height_rank_bucket", "values": ["rank_4_10"], "weight": -1.5},
                    {"column": "amount_ratio_bucket", "values": ["gte_5"], "weight": -1.0},
                    {"column": "turnover_rate_bucket", "values": ["6_10"], "weight": -1.0},
                ],
                "columns": ["profit_source_score", "turnover_rate"],
                "ascending": [False, False],
            },
            {
                "name": "profit_source_star_strong_penalty",
                "score_rules": [
                    {"column": "retreat_state_bucket", "values": ["neutral"], "weight": 2.0},
                    {"column": "market_limit_down_count_bucket", "values": ["5_15"], "weight": 1.5},
                    {"column": "limit_up_count_bucket", "values": ["50_80"], "weight": 1.0},
                    {"column": "first_time_detail_bucket", "values": ["1100_1330"], "weight": 0.8},
                    {"column": "amount_ratio_bucket", "values": ["gte_5"], "weight": -1.5},
                    {"column": "turnover_rate_bucket", "values": ["6_10"], "weight": -1.0},
                    {"column": "market_segment", "values": ["star"], "weight": -5.0},
                ],
                "columns": ["profit_source_score", "turnover_rate"],
                "ascending": [False, False],
            },
            {
                "name": "profit_source_star_extreme_penalty",
                "score_rules": [
                    {"column": "market_emotion_state_bucket", "values": ["mixed"], "weight": 2.0},
                    {"column": "market_limit_down_count_bucket", "values": ["5_15"], "weight": 1.5},
                    {"column": "amount_ratio_bucket", "values": ["1_2_2"], "weight": 1.5},
                    {"column": "turnover_rate_bucket", "values": ["gte_25", "10_15"], "weight": 1.0},
                    {"column": "market_segment", "values": ["chi_next"], "weight": 1.0},
                    {"column": "market_segment", "values": ["star"], "weight": -10.0},
                    {"column": "amount_bucket", "values": ["8e8_15e8"], "weight": -1.5},
                    {"column": "limit_height_rank_bucket", "values": ["rank_4_10"], "weight": -1.0},
                ],
                "columns": ["profit_source_score", "turnover_rate"],
                "ascending": [False, False],
            },
        ]

    @staticmethod
    def attach_profit_source_score(candidates: pd.DataFrame, sort_rule: dict[str, Any]) -> pd.DataFrame:
        score_rules = sort_rule.get("score_rules", [])
        if not score_rules:
            return candidates
        result = candidates.copy()
        result["profit_source_score"] = 0.0
        for rule in score_rules:
            column = str(rule.get("column", ""))
            if column not in result.columns:
                continue
            values = {str(value) for value in rule.get("values", [])}
            weight = float(rule.get("weight", 0.0))
            if not values or weight == 0:
                continue
            result.loc[result[column].fillna("missing").astype(str).isin(values), "profit_source_score"] += weight
        return result

    @classmethod
    def select_by_sort_rule(cls, candidates: pd.DataFrame, sort_rule: dict[str, Any]) -> pd.DataFrame:
        candidates = cls.attach_profit_source_score(candidates, sort_rule)
        columns = [column for column in sort_rule.get("columns", []) if column in candidates.columns]
        if not columns:
            columns = ["fill_probability"]
        ascending = list(sort_rule.get("ascending", []))[: len(columns)]
        if len(ascending) != len(columns):
            ascending = [False] * len(columns)
        selected = candidates.sort_values(
            ["trade_date"] + columns,
            ascending=[True] + ascending,
            na_position="last",
        )
        selected = selected.groupby("trade_date", as_index=False).head(1).copy()
        return selected.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    @staticmethod
    def format_scenario_name(
        conditions: tuple[tuple[str, str], ...],
        sort_rule: dict[str, Any],
        exit_rule: str,
    ) -> str:
        condition_name = ";".join(f"{factor}={value}" for factor, value in conditions)
        return f"{condition_name}|sort={sort_rule.get('name', '')}|exit={exit_rule}"


if __name__ == "__main__":
    main()
