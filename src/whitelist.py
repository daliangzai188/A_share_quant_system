from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.factors import FactorAnalyzer
from src.strict_asof import (
    PointInTimeContract,
    add_audit_columns,
    validate_strict_research_frame,
    write_audit_json,
)
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class CandidatePoolGenerator:
    """根据已验证的组合因子生成每日候选股票池。"""

    OUTPUT_COLUMNS = [
        "trade_date",
        "ts_code",
        "name",
        "rule_names",
        "rule_count",
        "market_sentiment_level",
        "board_type",
        "first_time_bucket",
        "limit_times_bucket",
        "fd_ratio_bucket",
        "amount_bucket",
        "turnover_rate_bucket",
        "fill_probability",
        "position_scale",
        "allow_buy_reliable",
        "is_fill_score_reliable",
        "is_fd_amount_abnormal",
        "sample_count",
        "matched_source",
        "suggested_turnover_rate",
        "fill_probability_method",
        "model_training_end_date",
        "as_of_date",
        "next_trade_date",
        "next_open",
        "exit_trade_date",
        "exit_close",
        "gross_return",
        "net_return",
        "is_win",
        "asof_standard_id",
        "asof_mode",
        "research_protocol",
        "strict_asof_passed",
        "release_eligible",
        "result_scope",
    ]

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("candidate_pool")
        pool_config = self.config.get("candidate_pool", {})
        self.pool_config = pool_config
        self.input_trades_path = self.project_root / pool_config.get(
            "input_trades_path", "data/processed/next_day_premium_trades.csv"
        )
        self.output_candidate_pool_path = self.project_root / pool_config.get(
            "output_candidate_pool_path", "data/processed/candidate_pool.csv"
        )
        self.output_strict_asof_audit_path = self.project_root / pool_config.get(
            "output_strict_asof_audit_path", "reports/strict_asof/candidate_pool_audit.json"
        )
        self.min_fill_probability = float(pool_config.get("min_fill_probability", 0.6))
        self.rules = pool_config.get("rules", [])

    def generate(self) -> Path:
        trades = pd.read_csv(self.input_trades_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        selection_columns = [
            "allow_buy_reliable",
            "is_fill_score_reliable",
            "fill_probability",
            *(
                column
                for rule in self.rules
                for column in rule.get("conditions", {})
            ),
            "rule_count",
            "sample_count",
        ]
        audit = validate_strict_research_frame(
            trades,
            contract=PointInTimeContract(dataset_name="candidate_pool_trade_samples"),
            selection_columns=selection_columns,
            section_config=self.pool_config,
            context="CandidatePoolGenerator.generate",
            project_root=self.project_root,
        )
        write_audit_json(self.output_strict_asof_audit_path, audit)
        trades = FactorAnalyzer(config_path="config/config.json").add_factor_buckets(trades)
        trades = trades[
            (trades["allow_buy_reliable"] == True)  # noqa: E712
            & (trades["fill_probability"] >= self.min_fill_probability)
            & (trades["is_fill_score_reliable"] == True)  # noqa: E712
        ].copy()

        matched_frames = []
        for rule in self.rules:
            matched = self.apply_conditions(trades, rule.get("conditions", {})).copy()
            if matched.empty:
                continue
            matched["rule_name"] = rule["rule_name"]
            matched["rule_description"] = rule.get("description", "")
            matched_frames.append(matched)

        if not matched_frames:
            raise RuntimeError("没有生成候选股票，请检查 candidate_pool.rules。")

        matched_all = pd.concat(matched_frames, ignore_index=True)
        candidates = self.merge_rule_hits(matched_all)
        candidates = candidates.sort_values(["trade_date", "rule_count", "fill_probability"], ascending=[True, False, False])
        candidates = add_audit_columns(candidates, audit)

        output_columns = [column for column in self.OUTPUT_COLUMNS if column in candidates.columns]
        mkdir_p(self.output_candidate_pool_path.parent)
        candidates[output_columns].to_csv(self.output_candidate_pool_path, index=False, encoding="utf-8-sig")
        self.logger.info("候选股票池已生成: %s, 行数: %s", self.output_candidate_pool_path, len(candidates))
        return self.output_candidate_pool_path

    @staticmethod
    def apply_conditions(data: pd.DataFrame, conditions: dict[str, list[str]]) -> pd.DataFrame:
        result = data
        for column, allowed_values in conditions.items():
            result = result[result[column].astype(str).isin([str(value) for value in allowed_values])]
        return result.copy()

    @staticmethod
    def merge_rule_hits(data: pd.DataFrame) -> pd.DataFrame:
        group_keys = ["trade_date", "ts_code"]
        rule_hits = (
            data.groupby(group_keys)
            .agg(
                rule_names=("rule_name", lambda values: "|".join(sorted(set(values)))),
                rule_count=("rule_name", lambda values: len(set(values))),
            )
            .reset_index()
        )
        base = data.drop_duplicates(subset=group_keys).drop(columns=["rule_name", "rule_description"])
        return base.merge(rule_hits, on=group_keys, how="left", validate="one_to_one")
