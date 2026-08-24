from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.strategy_optimizer import StrategyConditionOptimizer
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class PaperCandidateGenerator:
    """
    每日模拟盘候选生成器。

    文件作用：
    1. 读取当前策略配置，只使用本地 CSV 数据。
    2. 复用 StrategyConditionOptimizer.load_trades() 生成 T 日已知候选特征。
    3. 按 strategy_config.json 中的入选条件、排除条件和排序规则生成候选。
    4. 输出 T 日收盘后的 T+1 模拟买入计划候选，不接实盘，不调用 QMT。

    注意：
    - 排序不使用 next_open、exit_close、net_return 等未来字段。
    - 历史参考字段仅用于复盘验证，在报告中单独标注为 historical_reference_*。
    """

    SAFE_TRADE_MODES = {"paper", "simulation", "dry_run", "research"}
    FUTURE_REFERENCE_COLUMNS = {
        "next_trade_date",
        "next_open",
        "exit_trade_date",
        "exit_close",
        "gross_return",
        "net_return",
        "is_win",
    }

    def __init__(
        self,
        strategy_config_path: str | Path = "config/strategy_config.json",
        input_trades_path: str | Path | None = None,
        market_emotion_features_path: str | Path | None = None,
        theme_heat_features_path: str | Path | None = None,
    ) -> None:
        self.project_root = get_project_root()
        self.strategy_config_path = strategy_config_path
        self.config = load_json_config(strategy_config_path)
        self.logger = get_logger("paper_candidate_generator")
        self.paper_config = self.config.get("paper_candidate", {})
        self.runtime_config_path = self.paper_config.get("runtime_config", "config/config.json")
        self.optimization_config_key = self.paper_config.get(
            "optimization_config_key",
            "realistic_condition_strategy_search",
        )
        self.default_top_n = int(self.paper_config.get("default_top_n", 10))
        self.selected_count = int(self.paper_config.get("selected_count", 1))
        self.output_prefix = self.project_root / self.paper_config.get(
            "output_prefix",
            "reports/paper_trade/current_candidates",
        )
        self.include_historical_reference_columns = bool(
            self.paper_config.get("include_historical_reference_columns", True)
        )
        self.risk_thresholds = self.paper_config.get("risk_thresholds", {})
        self.input_trades_path = self.resolve_optional_path(input_trades_path)
        self.market_emotion_features_path = self.resolve_optional_path(market_emotion_features_path)
        self.theme_heat_features_path = self.resolve_optional_path(theme_heat_features_path)

    def generate(self, signal_date: str | None = None, top_n: int | None = None) -> dict[str, Path]:
        self.assert_safe_mode()
        candidates = self.load_all_candidates()
        filtered = self.apply_strategy_filters(candidates)
        if filtered.empty:
            raise RuntimeError("当前策略过滤后候选池为空，请检查配置或数据。")

        resolved_signal_date = self.resolve_signal_date(filtered, signal_date)
        daily_candidates = filtered[filtered["trade_date"].astype(str) == resolved_signal_date].copy()
        if daily_candidates.empty:
            raise RuntimeError(f"指定日期没有满足策略条件的候选: {resolved_signal_date}")

        ranked = self.rank_candidates(daily_candidates)
        output = self.build_output(ranked, resolved_signal_date, top_n or self.default_top_n)
        summary = self.build_summary(output, resolved_signal_date, len(daily_candidates), len(filtered))

        mkdir_p(self.output_prefix.parent)
        candidates_path = self.output_prefix.with_name(self.output_prefix.name + f"_{resolved_signal_date}.csv")
        summary_path = self.output_prefix.with_name(self.output_prefix.name + f"_{resolved_signal_date}_summary.csv")
        markdown_path = self.output_prefix.with_name(self.output_prefix.name + f"_{resolved_signal_date}.md")
        output.to_csv(candidates_path, index=False, encoding="utf-8-sig")
        summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
        self.write_markdown(markdown_path, summary, output)

        self.logger.info("模拟盘候选已生成: %s, 行数: %s", candidates_path, len(output))
        self.logger.info("模拟盘候选汇总已生成: %s", summary_path)
        return {
            "candidates": candidates_path,
            "summary": summary_path,
            "markdown": markdown_path,
        }

    def assert_safe_mode(self) -> None:
        trade_mode = str(self.config.get("trade_mode", "")).strip().lower()
        if trade_mode not in self.SAFE_TRADE_MODES:
            raise RuntimeError(f"拒绝生成模拟盘候选：trade_mode 不是安全模式: {trade_mode}")
        if bool(self.config.get("live_trading_enabled", False)):
            raise RuntimeError("拒绝生成模拟盘候选：live_trading_enabled=true")
        if bool(self.config.get("broker_adapter_enabled", False)):
            raise RuntimeError("拒绝生成模拟盘候选：broker_adapter_enabled=true")
        if bool(self.config.get("qmt_enabled", False)):
            raise RuntimeError("拒绝生成模拟盘候选：qmt_enabled=true")

    def load_all_candidates(self) -> pd.DataFrame:
        optimizer = StrategyConditionOptimizer(
            config_path=self.runtime_config_path,
            optimization_config_key=self.optimization_config_key,
        )
        if self.input_trades_path is not None:
            optimizer.input_trades_path = self.input_trades_path
        if self.market_emotion_features_path is not None:
            optimizer.optional_market_emotion_features_path = self.market_emotion_features_path
        if self.theme_heat_features_path is not None:
            optimizer.optional_theme_heat_features_path = self.theme_heat_features_path
        candidates = optimizer.load_trades(require_complete_exit=False)
        if candidates.empty:
            raise RuntimeError("本地候选数据为空，请先完成数据清洗、成交概率和因子计算。")
        return candidates

    def apply_strategy_filters(self, candidates: pd.DataFrame) -> pd.DataFrame:
        result = candidates.copy()
        result = self.apply_universe_filters(result)
        result = self.apply_include_conditions(result)
        result = self.apply_exclude_conditions(result)
        result = self.apply_exclude_rules(result)
        return result.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def apply_universe_filters(self, candidates: pd.DataFrame) -> pd.DataFrame:
        result = candidates.copy()
        universe = self.config.get("universe", {})
        if bool(universe.get("exclude_st", False)) or bool(universe.get("exclude_delisting_risk", False)):
            name = result.get("name", pd.Series("", index=result.index)).fillna("").astype(str).str.upper()
            is_st = result.get("is_st", pd.Series(False, index=result.index)).astype(str).str.lower().isin({"true", "1"})
            blocked = is_st | name.str.contains("ST", na=False) | name.str.contains("退", na=False)
            result = result[~blocked].copy()
        excluded_segments = {str(value) for value in universe.get("exclude_market_segments", [])}
        if excluded_segments and "market_segment" in result.columns:
            result = result[~result["market_segment"].astype(str).isin(excluded_segments)].copy()
        if bool(universe.get("exclude_bj", False)) and "market_segment" in result.columns:
            result = result[result["market_segment"].astype(str) != "bj"].copy()
        if bool(universe.get("exclude_chi_next", False)) and "market_segment" in result.columns:
            result = result[result["market_segment"].astype(str) != "chi_next"].copy()
        if bool(universe.get("exclude_sh_main", False)) and "market_segment" in result.columns:
            result = result[result["market_segment"].astype(str) != "sh_main"].copy()
        if bool(universe.get("exclude_sz_main", False)) and "market_segment" in result.columns:
            result = result[result["market_segment"].astype(str) != "sz_main"].copy()
        return result

    def apply_include_conditions(self, candidates: pd.DataFrame) -> pd.DataFrame:
        result = candidates.copy()
        for condition in self.config.get("candidate_filters", {}).get("conditions", []):
            column = str(condition.get("column", ""))
            expected = str(condition.get("value", ""))
            if column not in result.columns:
                raise RuntimeError(f"候选入选条件字段不存在: {column}")
            result = result[result[column].fillna("missing").astype(str) == expected].copy()
        return result

    def apply_exclude_conditions(self, candidates: pd.DataFrame) -> pd.DataFrame:
        result = candidates.copy()
        for condition in self.config.get("candidate_filters", {}).get("exclude_conditions", []):
            column = str(condition.get("column", ""))
            expected = str(condition.get("value", ""))
            if column not in result.columns:
                raise RuntimeError(f"候选排除条件字段不存在: {column}")
            result = result[result[column].fillna("missing").astype(str) != expected].copy()
        return result

    def apply_exclude_rules(self, candidates: pd.DataFrame) -> pd.DataFrame:
        result = candidates.copy()
        for rule in self.config.get("candidate_filters", {}).get("exclude_rules", []):
            mask = pd.Series(True, index=result.index)
            rule_conditions = rule.get("conditions", [])
            if not rule_conditions:
                continue
            for condition in rule_conditions:
                column = str(condition.get("column", ""))
                expected = str(condition.get("value", ""))
                if column not in result.columns:
                    raise RuntimeError(f"候选复合排除条件字段不存在: {column}")
                mask &= result[column].fillna("missing").astype(str) == expected
            result = result[~mask].copy()
        return result

    @staticmethod
    def resolve_signal_date(candidates: pd.DataFrame, signal_date: str | None) -> str:
        if signal_date:
            return str(signal_date)
        return str(candidates["trade_date"].astype(str).max())

    def rank_candidates(self, candidates: pd.DataFrame) -> pd.DataFrame:
        result = candidates.copy()
        result["profit_source_score"] = self.calculate_profit_source_score(result)
        ranking = self.config.get("ranking", {})
        configured_columns = [str(column) for column in ranking.get("columns", [])]
        missing_columns = [column for column in configured_columns if column not in result.columns]
        if missing_columns:
            raise RuntimeError(f"排序字段不存在，拒绝生成候选: {missing_columns}")
        columns = configured_columns
        if not columns:
            columns = ["fill_probability"]
        disallowed = [column for column in columns if column in self.FUTURE_REFERENCE_COLUMNS]
        if disallowed:
            raise RuntimeError(f"排序字段包含未来字段，拒绝生成候选: {disallowed}")
        ascending = list(ranking.get("ascending", []))[: len(columns)]
        if len(ascending) != len(columns):
            ascending = [False] * len(columns)
        result = result.sort_values(columns + ["amount", "turnover_rate"], ascending=ascending + [False, False])
        result["candidate_rank"] = range(1, len(result) + 1)
        return result

    def calculate_profit_source_score(self, candidates: pd.DataFrame) -> pd.Series:
        score = pd.Series(0.0, index=candidates.index)
        for rule in self.config.get("ranking", {}).get("score_rules", []):
            column = str(rule.get("column", ""))
            if column not in candidates.columns:
                raise RuntimeError(f"打分规则字段不存在，拒绝生成候选: {column}")
            values = {str(value) for value in rule.get("values", [])}
            weight = float(rule.get("weight", 0.0))
            if not values or weight == 0:
                continue
            score.loc[candidates[column].fillna("missing").astype(str).isin(values)] += weight
        return score

    def resolve_optional_path(self, path: str | Path | None) -> Path | None:
        if path is None:
            return None
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.project_root / candidate

    def build_output(self, ranked: pd.DataFrame, signal_date: str, top_n: int) -> pd.DataFrame:
        selected = ranked.head(top_n).copy()
        selected_count = min(self.selected_count, len(selected))
        rows = []
        for row in selected.itertuples(index=False):
            rank = int(row.candidate_rank)
            planned_action = (
                self.paper_config.get("planned_action_for_selected", "PLAN_BUY_T1_OPEN")
                if rank <= selected_count
                else self.paper_config.get("planned_action_for_watchlist", "WATCH_ONLY")
            )
            rows.append(self.build_candidate_row(row, signal_date, planned_action))
        return pd.DataFrame(rows)

    def build_candidate_row(self, row: object, signal_date: str, planned_action: str) -> dict[str, Any]:
        matched_c_profile_ids = str(getattr(row, "matched_c_profile_ids", "") or "")
        result: dict[str, Any] = {
            "signal_date": signal_date,
            "candidate_rank": int(row.candidate_rank),
            "planned_action": planned_action,
            "ts_code": row.ts_code,
            "name": row.name,
            "market_segment": getattr(row, "market_segment", ""),
            "profit_source_score": float(getattr(row, "profit_source_score", 0.0)),
            "planned_position_pct": float(self.config.get("position", {}).get("target_position_pct", 0.825))
            if planned_action == self.paper_config.get("planned_action_for_selected", "PLAN_BUY_T1_OPEN")
            else 0.0,
            "selection_reason": (
                f"C因子OR命中:{matched_c_profile_ids}"
                if matched_c_profile_ids else self.conditions_text()
            ),
            "matched_c_profile_ids": matched_c_profile_ids,
            "sort_rule": self.config.get("ranking", {}).get("sort_rule", ""),
            "exit_rule": self.config.get("exit_rule", {}).get("rule_name", ""),
            "risk_flags": self.build_risk_flags(row),
            "fill_probability": self.normalize_number(getattr(row, "fill_probability", 0.0)),
            "allow_buy_reliable": self.normalize_bool(getattr(row, "allow_buy_reliable", False)),
            "is_fill_score_reliable": self.normalize_bool(getattr(row, "is_fill_score_reliable", False)),
            "fd_ratio_bucket": getattr(row, "fd_ratio_bucket", ""),
            "fd_amount_to_circ_mv": self.normalize_number(getattr(row, "fd_amount_to_circ_mv", 0.0)),
            "segment_limit_up_count_bucket": getattr(row, "segment_limit_up_count_bucket", ""),
            "market_chain_count_bucket": getattr(row, "market_chain_count_bucket", ""),
            "market_limit_down_count_bucket": getattr(row, "market_limit_down_count_bucket", ""),
            "retreat_state_bucket": getattr(row, "retreat_state_bucket", ""),
            "market_emotion_state_bucket": getattr(row, "market_emotion_state_bucket", ""),
            "segment_emotion_state_bucket": getattr(row, "segment_emotion_state_bucket", ""),
            "first_time_detail_bucket": getattr(row, "first_time_detail_bucket", ""),
            "turnover_rate_bucket": getattr(row, "turnover_rate_bucket", ""),
            "amount_ratio_bucket": getattr(row, "amount_ratio_bucket", ""),
            "prev_pct_chg_bucket": getattr(row, "prev_pct_chg_bucket", ""),
            "theme_data_available": getattr(row, "theme_data_available", ""),
            "theme_name": getattr(row, "theme_name", ""),
            "theme_heat_score": self.normalize_number(getattr(row, "theme_heat_score", 0.0)),
            "theme_heat_rank": self.normalize_number(getattr(row, "theme_heat_rank", 0.0)),
            "theme_limit_count": self.normalize_number(getattr(row, "theme_limit_count", 0.0)),
            "theme_chain_count": self.normalize_number(getattr(row, "theme_chain_count", 0.0)),
            "theme_is_mainline": self.normalize_bool(getattr(row, "theme_is_mainline", False)),
            "same_theme_limit_count": self.normalize_number(getattr(row, "same_theme_limit_count", 0.0)),
            "auction_strength_score": self.normalize_number(getattr(row, "auction_strength_score", 0.0)),
            "open_5m_strength_score": self.normalize_number(getattr(row, "open_5m_strength_score", 0.0)),
            "sector_moneyflow_score": self.normalize_number(getattr(row, "sector_moneyflow_score", 0.0)),
            "top_list_net_buy_score": self.normalize_number(getattr(row, "top_list_net_buy_score", 0.0)),
            "amount": self.normalize_number(getattr(row, "amount", 0.0)),
            "turnover_rate": self.normalize_number(getattr(row, "turnover_rate", 0.0)),
            "volume_ratio": self.normalize_number(getattr(row, "volume_ratio", 0.0)),
            "open_times": self.normalize_number(getattr(row, "open_times", 0.0)),
            "limit_times": self.normalize_number(getattr(row, "limit_times", 0.0)),
            "first_time": getattr(row, "first_time", ""),
            "last_time": getattr(row, "last_time", ""),
            "paper_note": "该候选只用于模拟盘计划，不代表实盘可买入。",
        }
        if self.include_historical_reference_columns:
            result.update(
                {
                    "historical_reference_next_trade_date": getattr(row, "next_trade_date", ""),
                    "historical_reference_next_open": self.normalize_number(getattr(row, "next_open", 0.0)),
                    "historical_reference_exit_trade_date": getattr(row, "exit_trade_date", ""),
                    "historical_reference_exit_close": self.normalize_number(getattr(row, "exit_close", 0.0)),
                    "historical_reference_net_return": self.normalize_number(getattr(row, "net_return", 0.0)),
                    "historical_reference_is_win": self.normalize_bool(getattr(row, "is_win", False)),
                }
            )
        return result

    def build_risk_flags(self, row: object) -> str:
        flags = []
        min_fill_probability = float(self.risk_thresholds.get("min_fill_probability_warn", 0.6))
        max_fd_ratio = float(self.risk_thresholds.get("max_fd_ratio_warn", 0.01))
        fill_probability = self.normalize_number(getattr(row, "fill_probability", 0.0))
        fd_ratio = self.normalize_number(getattr(row, "fd_amount_to_circ_mv", 0.0))
        if fill_probability < min_fill_probability:
            flags.append("成交概率低于阈值")
        if fd_ratio > max_fd_ratio:
            flags.append("封单/流通市值偏高")
        if not self.normalize_bool(getattr(row, "allow_buy_reliable", False)):
            flags.append("成交模型不允许买入")
        if not self.normalize_bool(getattr(row, "is_fill_score_reliable", False)):
            flags.append("成交评分不可靠")
        if self.normalize_bool(getattr(row, "is_st", False)) or "ST" in str(getattr(row, "name", "")).upper():
            flags.append("ST风险")
        flags.extend(self.build_watch_rule_flags(row))
        return ";".join(flags) if flags else "无"

    def build_watch_rule_flags(self, row: object) -> list[str]:
        flags = []
        for watch_rule in self.paper_config.get("risk_watch_rules", []):
            if str(watch_rule.get("action", "watch_only")) != "watch_only":
                continue
            if self.watch_rule_hit(row, watch_rule):
                flags.append(str(watch_rule.get("name", "WATCH_RULE_HIT")))
        return flags

    @staticmethod
    def watch_rule_hit(row: object, watch_rule: dict[str, Any]) -> bool:
        # 同一个 watch_rule 下的 rules 是 OR；每个 rule 内部的 conditions 是 AND。
        for rule in watch_rule.get("rules", []):
            conditions = rule.get("conditions", [])
            if not conditions:
                continue
            matched = True
            for condition in conditions:
                column = str(condition.get("column", ""))
                expected = str(condition.get("value", ""))
                actual = str(getattr(row, column, "missing"))
                if actual != expected:
                    matched = False
                    break
            if matched:
                return True
        return False

    def build_summary(
        self,
        output: pd.DataFrame,
        signal_date: str,
        daily_candidate_count: int,
        filtered_candidate_count: int,
    ) -> pd.DataFrame:
        selected = output[output["planned_action"] == self.paper_config.get("planned_action_for_selected", "PLAN_BUY_T1_OPEN")]
        risk_flags = output.get("risk_flags", pd.Series("", index=output.index)).fillna("").astype(str)
        selected_risk_flags = selected.get("risk_flags", pd.Series("", index=selected.index)).fillna("").astype(str)
        loss_overlay_mask = risk_flags.str.contains("LOSS_OVERLAY_WATCH", na=False)
        selected_loss_overlay_mask = selected_risk_flags.str.contains("LOSS_OVERLAY_WATCH", na=False)
        loss_overlay_codes = (
            output.loc[loss_overlay_mask, "ts_code"].astype(str) + " " + output.loc[loss_overlay_mask, "name"].astype(str)
            if "ts_code" in output.columns and "name" in output.columns
            else pd.Series(dtype=str)
        )
        manual_review_required = bool(selected_loss_overlay_mask.any()) if not selected.empty else False
        return pd.DataFrame(
            [
                {
                    "strategy_name": self.config.get("strategy_name", ""),
                    "trade_mode": self.config.get("trade_mode", ""),
                    "signal_date": signal_date,
                    "filtered_candidate_count_all_dates": int(filtered_candidate_count),
                    "matched_candidate_count_on_signal_date": int(daily_candidate_count),
                    "output_candidate_count": int(len(output)),
                    "selected_count": int(len(selected)),
                    "watch_count": int(len(output) - len(selected)),
                    "top_ts_code": str(selected["ts_code"].iloc[0]) if not selected.empty else "",
                    "top_name": str(selected["name"].iloc[0]) if not selected.empty else "",
                    "top_profit_source_score": float(selected["profit_source_score"].iloc[0]) if not selected.empty else 0.0,
                    "top_risk_flags": str(selected["risk_flags"].iloc[0]) if not selected.empty else "",
                    "risk_warn_candidate_count": int((output["risk_flags"].astype(str) != "无").sum()) if not output.empty else 0,
                    "loss_overlay_watch_candidate_count": int(loss_overlay_mask.sum()) if not output.empty else 0,
                    "selected_loss_overlay_watch_count": int(selected_loss_overlay_mask.sum()) if not selected.empty else 0,
                    "selected_loss_overlay_watch": manual_review_required,
                    "loss_overlay_watch_top_codes": ";".join(loss_overlay_codes.head(10).tolist()),
                    "manual_review_required": manual_review_required,
                    "manual_review_status": "PENDING_MANUAL_REVIEW" if manual_review_required else "NOT_REQUIRED",
                    "manual_review_reason": "选中标的命中 LOSS_OVERLAY_WATCH，进入模拟买入观察前需要人工复核。"
                    if manual_review_required
                    else "",
                    "future_columns_used_for_ranking": False,
                    "live_order_enabled": False,
                }
            ]
        )

    def write_markdown(self, path: Path, summary: pd.DataFrame, candidates: pd.DataFrame) -> None:
        preview_columns = [
            "candidate_rank",
            "planned_action",
            "ts_code",
            "name",
            "market_segment",
            "profit_source_score",
            "risk_flags",
            "fill_probability",
            "fd_ratio_bucket",
            "market_chain_count_bucket",
            "segment_limit_up_count_bucket",
            "historical_reference_next_trade_date",
            "historical_reference_net_return",
        ]
        preview_columns = [column for column in preview_columns if column in candidates.columns]
        content = f"""# 每日模拟盘候选报告

本报告只基于本地 T 日已知因子生成候选，不接实盘，不下真实订单。

## 汇总

{summary.to_markdown(index=False)}

## 候选列表

{candidates[preview_columns].to_markdown(index=False) if not candidates.empty else "无候选。"}

## 口径说明

- `planned_action=PLAN_BUY_T1_OPEN` 表示计划在 T+1 开盘用模拟盘观察买入。
- `historical_reference_*` 字段只用于历史复盘，不参与候选排序。
- 当前仍未验证集合竞价、盘口五档、分钟 K 和真实排队成交，不能直接用于实盘。
"""
        path.write_text(content, encoding="utf-8")

    def conditions_text(self) -> str:
        filters = self.config.get("candidate_filters", {})
        include = [
            f"{condition.get('column')}={condition.get('value')}"
            for condition in filters.get("conditions", [])
        ]
        exclude = [
            f"exclude:{condition.get('column')}={condition.get('value')}"
            for condition in filters.get("exclude_conditions", [])
        ]
        for rule in filters.get("exclude_rules", []):
            text = "&&".join(
                f"{condition.get('column')}={condition.get('value')}"
                for condition in rule.get("conditions", [])
            )
            exclude.append(f"exclude:{text}")
        return ";".join(include + exclude)

    @staticmethod
    def normalize_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if pd.isna(value):
            return False
        return str(value).strip().lower() in {"true", "1", "yes"}

    @staticmethod
    def normalize_number(value: object, default: float = 0.0) -> float:
        result = pd.to_numeric(value, errors="coerce")
        if pd.isna(result):
            return default
        return float(result)
