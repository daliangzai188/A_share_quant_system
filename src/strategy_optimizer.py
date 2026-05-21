from __future__ import annotations

from itertools import combinations, product
from pathlib import Path

import pandas as pd

from src.factors import FactorAnalyzer, NextDayPremiumAnalyzer
from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger


class StrategyConditionOptimizer:
    """扫描涨停候选因子组合，评估 70% 总仓位下的年度稳定性。"""

    def __init__(
        self,
        config_path: str | Path = "config/config.json",
        optimization_config_key: str = "optimization",
    ) -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("strategy_optimizer")
        self.optimization_config_key = optimization_config_key
        optimization_config = self.config.get(optimization_config_key, {})

        self.input_trades_path = self.project_root / optimization_config.get(
            "input_trades_path", "data/processed/next_day_premium_trades.csv"
        )
        analysis_config = self.config.get("analysis", {})
        self.input_daily_merged_path = self.project_root / analysis_config.get(
            "input_daily_merged_path", "data/processed/daily_merged.csv"
        )
        self.output_report_path = self.project_root / optimization_config.get(
            "output_report_path", "reports/strategy_optimization_report.csv"
        )
        self.output_yearly_path = self.project_root / optimization_config.get(
            "output_yearly_path", "reports/strategy_optimization_yearly.csv"
        )
        self.optional_auction_features_path = self.project_root / optimization_config.get(
            "optional_auction_features_path", "data/processed/auction_features.csv"
        )
        self.optional_open_5m_features_path = self.project_root / optimization_config.get(
            "optional_open_5m_features_path", "data/processed/open_5m_features.csv"
        )
        self.optional_sector_moneyflow_features_path = self.project_root / optimization_config.get(
            "optional_sector_moneyflow_features_path", "data/processed/sector_moneyflow_features.csv"
        )
        self.optional_top_list_features_path = self.project_root / optimization_config.get(
            "optional_top_list_features_path", "data/processed/top_list_features.csv"
        )
        self.factor_columns = optimization_config.get(
            "factor_columns",
            [
                "market_sentiment_level",
                "board_type",
                "first_time_bucket",
                "limit_times_bucket",
                "amount_bucket",
                "turnover_rate_bucket",
                "fd_ratio_bucket",
            ],
        )
        self.required_factor_columns = optimization_config.get("required_factor_columns", [])
        self.min_factor_count = int(optimization_config.get("min_factor_count", 1))
        self.max_factor_count = int(optimization_config.get("max_factor_count", 4))
        self.min_sample_count = int(optimization_config.get("min_sample_count", 80))
        self.initial_cash = float(optimization_config.get("initial_cash", 1000000))
        self.max_holding_count = int(optimization_config.get("max_holding_count", 5))
        self.max_holding_count_options = [
            int(value)
            for value in optimization_config.get("max_holding_count_options", [self.max_holding_count])
        ]
        self.max_total_position_pct = float(optimization_config.get("max_total_position_pct", 0.7))
        self.rank_position_weights = [
            float(value)
            for value in optimization_config.get("rank_position_weights", [])
        ]
        self.evaluation_years = [str(year) for year in optimization_config.get("evaluation_years", [])]
        self.target_annual_return = float(optimization_config.get("target_annual_return", 2.0))

    def optimize(self) -> dict[str, Path]:
        trades = self.load_trades()
        condition_sets = list(self.generate_condition_sets(trades))
        self.logger.info("开始扫描策略组合，组合数量: %s", len(condition_sets))

        summary_rows = []
        yearly_rows = []
        for index, conditions in enumerate(condition_sets, start=1):
            matched = self.apply_conditions(trades, conditions)
            if len(matched) < self.min_sample_count:
                continue

            for max_holding_count in self.max_holding_count_options:
                selected = self.select_daily_candidates(matched, max_holding_count=max_holding_count)
                if len(selected) < self.min_sample_count:
                    continue

                summary = self.evaluate_selected(selected, conditions, max_holding_count=max_holding_count)
                summary_rows.append(summary)
                yearly_rows.extend(
                    self.build_yearly_rows(
                        selected,
                        strategy_name=summary["strategy_name"],
                        condition_name=summary["condition_name"],
                    )
                )

            if index % 1000 == 0:
                self.logger.info("策略组合扫描进度: %s/%s", index, len(condition_sets))

        if not summary_rows:
            raise RuntimeError("没有找到满足最小样本数的策略组合。")

        report = pd.DataFrame(summary_rows)
        yearly_report = pd.DataFrame(yearly_rows)
        report = report.sort_values(
            [
                "target_year_count",
                "positive_year_count",
                "min_year_return",
                "total_compound_return",
                "sample_count",
            ],
            ascending=[False, False, False, False, False],
        )

        self.output_report_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_yearly_path.parent.mkdir(parents=True, exist_ok=True)
        report.to_csv(self.output_report_path, index=False, encoding="utf-8-sig")
        yearly_report.to_csv(self.output_yearly_path, index=False, encoding="utf-8-sig")

        self.logger.info("策略组合优化报告已生成: %s, 行数: %s", self.output_report_path, len(report))
        self.logger.info("策略组合年度报告已生成: %s, 行数: %s", self.output_yearly_path, len(yearly_report))
        return {"report": self.output_report_path, "yearly": self.output_yearly_path}

    def load_trades(self) -> pd.DataFrame:
        trades = pd.read_csv(self.input_trades_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        trades = self.add_historical_features(trades)
        trades = self.add_leader_and_theme_features(trades)
        trades = self.add_optional_external_features(trades)
        trades = FactorAnalyzer(config_path="config/config.json").add_factor_buckets(trades)
        trades = trades[
            (trades["allow_buy_reliable"] == True)  # noqa: E712
            & (trades["is_fill_score_reliable"] == True)  # noqa: E712
            & (trades["is_fd_amount_abnormal"] == False)  # noqa: E712
            & trades["net_return"].notna()
            & trades["exit_trade_date"].notna()
        ].copy()
        if "is_executable_exit" in trades.columns:
            trades = trades[trades["is_executable_exit"] == True].copy()  # noqa: E712
        for column in self.factor_columns:
            trades[column] = trades[column].astype(str)
        return trades

    def add_historical_features(self, trades: pd.DataFrame) -> pd.DataFrame:
        daily = pd.read_csv(
            self.input_daily_merged_path,
            dtype={"trade_date": str, "ts_code": str},
            usecols=["trade_date", "ts_code", "pct_chg", "amount", "turnover_rate"],
            low_memory=False,
        )
        daily = daily.sort_values(["ts_code", "trade_date"]).copy()
        daily["prev_pct_chg"] = daily.groupby("ts_code")["pct_chg"].shift(1)
        daily["prev2_pct_chg"] = daily.groupby("ts_code")["pct_chg"].shift(2)
        daily["prev_amount"] = daily.groupby("ts_code")["amount"].shift(1)
        daily["prev_turnover_rate"] = daily.groupby("ts_code")["turnover_rate"].shift(1)
        daily["amount_ratio_1d"] = daily["amount"] / daily["prev_amount"]
        daily["turnover_ratio_1d"] = daily["turnover_rate"] / daily["prev_turnover_rate"]
        daily["two_day_pct_chg"] = daily["prev_pct_chg"].fillna(0) + daily["prev2_pct_chg"].fillna(0)
        feature_columns = [
            "trade_date",
            "ts_code",
            "prev_pct_chg",
            "prev2_pct_chg",
            "two_day_pct_chg",
            "prev_amount",
            "prev_turnover_rate",
            "amount_ratio_1d",
            "turnover_ratio_1d",
        ]
        return trades.merge(daily[feature_columns], on=["trade_date", "ts_code"], how="left", validate="many_to_one")

    def add_leader_and_theme_features(self, trades: pd.DataFrame) -> pd.DataFrame:
        trades = trades.copy()
        trades["first_time_minutes_for_rank"] = trades["first_time"].apply(FactorAnalyzer.parse_time_to_minutes)
        trades["market_leader_rank"] = (
            trades.sort_values(
                ["trade_date", "limit_times", "first_time_minutes_for_rank", "open_times", "amount"],
                ascending=[True, False, True, False, False],
            )
            .groupby("trade_date")
            .cumcount()
            + 1
        )
        if "market_segment" in trades.columns:
            trades["segment_market_leader_rank"] = (
                trades.sort_values(
                    ["trade_date", "market_segment", "limit_times", "first_time_minutes_for_rank", "open_times", "amount"],
                    ascending=[True, True, False, True, False, False],
                )
                .groupby(["trade_date", "market_segment"], dropna=False)
                .cumcount()
                + 1
            )
        else:
            trades["segment_market_leader_rank"] = pd.NA
        trades["limit_height_rank"] = trades.groupby("trade_date")["limit_times"].rank(
            method="dense",
            ascending=False,
        )
        if "market_segment" in trades.columns:
            trades["segment_limit_height_rank"] = trades.groupby(
                ["trade_date", "market_segment"],
                dropna=False,
            )["limit_times"].rank(
                method="dense",
                ascending=False,
            )
        else:
            trades["segment_limit_height_rank"] = pd.NA

        theme_column = self.resolve_theme_column(trades)
        if theme_column is None:
            trades["theme_heat_score"] = pd.NA
            trades["same_theme_limit_count"] = pd.NA
        else:
            theme_counts = (
                trades.groupby(["trade_date", theme_column], dropna=False)["ts_code"]
                .transform("count")
                .astype("float64")
            )
            trades["same_theme_limit_count"] = theme_counts
            trades["theme_heat_score"] = theme_counts

        market_by_date = (
            trades[["trade_date", "limit_up_count"]]
            .drop_duplicates("trade_date")
            .sort_values("trade_date")
            .copy()
        )
        market_by_date["limit_up_count_prev1"] = market_by_date["limit_up_count"].shift(1)
        market_by_date["limit_up_count_prev2"] = market_by_date["limit_up_count"].shift(2)
        market_by_date["retreat_state"] = market_by_date.apply(self.classify_retreat_state, axis=1)
        trades = trades.merge(
            market_by_date[["trade_date", "retreat_state"]],
            on="trade_date",
            how="left",
            validate="many_to_one",
        )
        if {"market_segment", "segment_limit_up_count"}.issubset(trades.columns):
            segment_market = (
                trades[["trade_date", "market_segment", "segment_limit_up_count"]]
                .drop_duplicates(["trade_date", "market_segment"])
                .sort_values(["market_segment", "trade_date"])
                .copy()
            )
            segment_market["segment_limit_up_count_prev1"] = segment_market.groupby("market_segment")[
                "segment_limit_up_count"
            ].shift(1)
            segment_market["segment_limit_up_count_prev2"] = segment_market.groupby("market_segment")[
                "segment_limit_up_count"
            ].shift(2)
            segment_market["segment_retreat_state"] = segment_market.apply(
                self.classify_segment_retreat_state,
                axis=1,
            )
            trades = trades.merge(
                segment_market[["trade_date", "market_segment", "segment_retreat_state"]],
                on=["trade_date", "market_segment"],
                how="left",
                validate="many_to_one",
            )
        else:
            trades["segment_retreat_state"] = "unknown"
        return trades.drop(columns=["first_time_minutes_for_rank"])

    @staticmethod
    def resolve_theme_column(trades: pd.DataFrame) -> str | None:
        for column in ["theme_name", "concept_name", "lu_desc", "industry"]:
            if column in trades.columns and trades[column].notna().any():
                return column
        return None

    @staticmethod
    def classify_retreat_state(row: pd.Series) -> str:
        current = row["limit_up_count"]
        prev1 = row["limit_up_count_prev1"]
        prev2 = row["limit_up_count_prev2"]
        if pd.isna(prev1) or pd.isna(prev2):
            return "unknown"
        if current < 30:
            return "weak_below_30"
        if current < prev1 < prev2:
            return "retreat_2day"
        if current < prev1 and current < 50:
            return "retreat_weak"
        if current > prev1 > prev2:
            return "warming_2day"
        return "neutral"

    @staticmethod
    def classify_segment_retreat_state(row: pd.Series) -> str:
        current = row["segment_limit_up_count"]
        prev1 = row["segment_limit_up_count_prev1"]
        prev2 = row["segment_limit_up_count_prev2"]
        if pd.isna(current) or pd.isna(prev1) or pd.isna(prev2):
            return "unknown"
        if current <= 3:
            return "weak_below_3"
        if current < prev1 < prev2:
            return "retreat_2day"
        if current < prev1 and current <= 5:
            return "retreat_weak"
        if current > prev1 > prev2:
            return "warming_2day"
        return "neutral"

    def add_optional_external_features(self, trades: pd.DataFrame) -> pd.DataFrame:
        trades = self.merge_optional_feature_file(
            trades,
            path=self.optional_auction_features_path,
            expected_columns=["trade_date", "ts_code", "auction_strength_score"],
            feature_name="竞价强度",
        )
        trades = self.merge_optional_feature_file(
            trades,
            path=self.optional_open_5m_features_path,
            expected_columns=["trade_date", "ts_code", "open_5m_strength_score"],
            feature_name="开盘5分钟强度",
        )
        trades = self.merge_optional_feature_file(
            trades,
            path=self.optional_sector_moneyflow_features_path,
            expected_columns=["trade_date", "ts_code", "sector_moneyflow_score"],
            feature_name="板块资金流",
        )
        trades = self.merge_optional_feature_file(
            trades,
            path=self.optional_top_list_features_path,
            expected_columns=["trade_date", "ts_code", "top_list_net_buy_score"],
            feature_name="龙虎榜资金",
        )
        return trades

    def merge_optional_feature_file(
        self,
        trades: pd.DataFrame,
        path: Path,
        expected_columns: list[str],
        feature_name: str,
    ) -> pd.DataFrame:
        if not path.exists():
            self.logger.info("%s 特征文件不存在，跳过: %s", feature_name, path)
            return trades
        feature = pd.read_csv(path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        missing_columns = [column for column in expected_columns if column not in feature.columns]
        if missing_columns:
            raise ValueError(f"{feature_name} 特征文件缺少字段: {missing_columns}, 文件: {path}")
        feature = feature[expected_columns].drop_duplicates(["trade_date", "ts_code"])
        self.logger.info("%s 特征已加载: %s, 行数: %s", feature_name, path, len(feature))
        return trades.merge(feature, on=["trade_date", "ts_code"], how="left", validate="one_to_one")

    def generate_condition_sets(self, trades: pd.DataFrame) -> list[dict[str, str]]:
        values_by_factor = {}
        skipped_unknown_factors = []
        for factor in self.factor_columns:
            values = sorted(value for value in trades[factor].dropna().astype(str).unique() if value != "nan")
            meaningful_values = [value for value in values if value != "unknown"]
            if not meaningful_values:
                skipped_unknown_factors.append(factor)
                continue
            values_by_factor[factor] = meaningful_values
        if skipped_unknown_factors:
            self.logger.info("以下因子当前全为 unknown，本轮扫描跳过: %s", ",".join(skipped_unknown_factors))

        active_factor_columns = list(values_by_factor)
        required_factor_columns = [factor for factor in self.required_factor_columns if factor in values_by_factor]
        optional_factor_columns = [factor for factor in active_factor_columns if factor not in required_factor_columns]
        if len(required_factor_columns) < len(self.required_factor_columns):
            missing_required = sorted(set(self.required_factor_columns) - set(required_factor_columns))
            raise RuntimeError(f"必选因子缺失或全为 unknown: {missing_required}")

        conditions = []
        required_value_lists = [values_by_factor[factor] for factor in required_factor_columns]
        required_products = list(product(*required_value_lists)) if required_factor_columns else [()]
        min_optional_count = max(0, self.min_factor_count - len(required_factor_columns))
        max_optional_count = max(0, self.max_factor_count - len(required_factor_columns))
        for required_values in required_products:
            required_conditions = dict(zip(required_factor_columns, required_values))
            for optional_count in range(min_optional_count, max_optional_count + 1):
                for selected_factors in combinations(optional_factor_columns, optional_count):
                    value_lists = [values_by_factor[factor] for factor in selected_factors]
                    for selected_values in product(*value_lists):
                        condition = required_conditions | dict(zip(selected_factors, selected_values))
                        conditions.append(condition)
        return conditions

    @staticmethod
    def apply_conditions(data: pd.DataFrame, conditions: dict[str, str]) -> pd.DataFrame:
        result = data
        for column, value in conditions.items():
            result = result[result[column] == value]
        return result.copy()

    def select_daily_candidates(self, trades: pd.DataFrame, max_holding_count: int) -> pd.DataFrame:
        sort_columns = ["trade_date", "fill_probability", "sample_count", "amount", "turnover_rate"]
        existing_sort_columns = [column for column in sort_columns if column in trades.columns]
        ascending = [True] + [False] * (len(existing_sort_columns) - 1)
        selected = trades.sort_values(existing_sort_columns, ascending=ascending)
        selected = selected.groupby("trade_date").head(max_holding_count).copy()
        selected["selected_rank"] = selected.groupby("trade_date").cumcount() + 1
        selected["daily_selected_count"] = selected.groupby("trade_date")["ts_code"].transform("count")
        selected["position_pct"] = selected.apply(
            lambda row: self.resolve_position_pct(
                selected_rank=int(row["selected_rank"]),
                daily_selected_count=int(row["daily_selected_count"]),
            ),
            axis=1,
        )
        selected["weighted_return"] = selected["net_return"] * selected["position_pct"]
        return selected

    def resolve_position_pct(self, selected_rank: int, daily_selected_count: int) -> float:
        if self.rank_position_weights:
            weight_index = selected_rank - 1
            if weight_index >= len(self.rank_position_weights):
                return 0.0
            return self.rank_position_weights[weight_index]
        return self.max_total_position_pct / daily_selected_count

    def evaluate_selected(
        self,
        selected: pd.DataFrame,
        conditions: dict[str, str],
        max_holding_count: int,
    ) -> dict[str, object]:
        returns = selected["net_return"].dropna()
        gains = returns[returns > 0]
        losses = returns[returns <= 0]
        daily_returns = self.build_daily_returns(selected)
        equity_curve = (1 + daily_returns["daily_return"]).cumprod()
        final_equity = self.initial_cash * float(equity_curve.iloc[-1]) if len(equity_curve) else self.initial_cash
        yearly_returns = self.calculate_yearly_returns(daily_returns)
        min_year_return = min(yearly_returns.values()) if yearly_returns else 0.0
        positive_year_count = sum(value > 0 for value in yearly_returns.values())
        negative_year_count = sum(value < 0 for value in yearly_returns.values())
        target_year_count = sum(value >= self.target_annual_return for value in yearly_returns.values())

        return {
            "strategy_name": self.format_strategy_name(conditions, max_holding_count=max_holding_count),
            "condition_name": self.format_conditions(conditions),
            "factor_count": len(conditions),
            "sample_count": int(len(selected)),
            "signal_days": int(selected["trade_date"].nunique()),
            "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
            "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
            "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
            "initial_cash": self.initial_cash,
            "final_equity": final_equity,
            "total_compound_return": float(equity_curve.iloc[-1] - 1) if len(equity_curve) else 0.0,
            "avg_year_return": float(sum(yearly_returns.values()) / len(yearly_returns)) if yearly_returns else 0.0,
            "min_year_return": float(min_year_return),
            "max_year_return": float(max(yearly_returns.values())) if yearly_returns else 0.0,
            "positive_year_count": int(positive_year_count),
            "negative_year_count": int(negative_year_count),
            "target_year_count": int(target_year_count),
            "target_annual_return": self.target_annual_return,
            "max_drawdown": float(NextDayPremiumAnalyzer.calculate_max_drawdown(equity_curve)),
            "max_profit_per_trade": float(returns.max()) if len(returns) else 0.0,
            "max_loss_per_trade": float(returns.min()) if len(returns) else 0.0,
            "profit_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else 0.0,
            "max_consecutive_losses": int(NextDayPremiumAnalyzer.max_consecutive_losses(returns)),
            "max_total_position_pct": self.max_total_position_pct,
            "max_holding_count": max_holding_count,
            "allocation_mode": self.describe_allocation_mode(),
        }

    def describe_allocation_mode(self) -> str:
        if self.rank_position_weights:
            return "rank_weights_" + "_".join(str(weight) for weight in self.rank_position_weights)
        return "equal_split_by_daily_selected_count"

    def build_yearly_rows(
        self,
        selected: pd.DataFrame,
        strategy_name: str,
        condition_name: str,
    ) -> list[dict[str, object]]:
        daily_returns = self.build_daily_returns(selected)
        yearly_returns = self.calculate_yearly_returns(daily_returns)
        rows = []
        selected = selected.copy()
        selected["year"] = selected["trade_date"].astype(str).str[:4]
        for year in self.evaluation_years:
            year_trades = selected[selected["year"] == year]
            returns = year_trades["net_return"].dropna()
            rows.append(
                {
                    "condition_name": condition_name,
                    "strategy_name": strategy_name,
                    "year": year,
                    "sample_count": int(len(year_trades)),
                    "signal_days": int(year_trades["trade_date"].nunique()) if not year_trades.empty else 0,
                    "year_return": float(yearly_returns.get(year, 0.0)),
                    "is_positive": bool(yearly_returns.get(year, 0.0) > 0),
                    "is_target_reached": bool(yearly_returns.get(year, 0.0) >= self.target_annual_return),
                    "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
                    "avg_return_per_trade": float(returns.mean()) if len(returns) else 0.0,
                    "median_return_per_trade": float(returns.median()) if len(returns) else 0.0,
                }
            )
        return rows

    def build_daily_returns(self, selected: pd.DataFrame) -> pd.DataFrame:
        daily_returns = selected.groupby("exit_trade_date")["weighted_return"].sum().reset_index()
        daily_returns = daily_returns.rename(columns={"exit_trade_date": "trade_date", "weighted_return": "daily_return"})
        daily_returns["year"] = daily_returns["trade_date"].astype(str).str[:4]
        return daily_returns.sort_values("trade_date")

    def calculate_yearly_returns(self, daily_returns: pd.DataFrame) -> dict[str, float]:
        returns = {}
        for year in self.evaluation_years:
            year_data = daily_returns[daily_returns["year"] == year]
            if year_data.empty:
                returns[year] = 0.0
            else:
                returns[year] = float((1 + year_data["daily_return"]).prod() - 1)
        return returns

    @staticmethod
    def format_conditions(conditions: dict[str, str]) -> str:
        return ";".join(f"{column}={value}" for column, value in conditions.items())

    @classmethod
    def format_strategy_name(cls, conditions: dict[str, str], max_holding_count: int) -> str:
        return f"top{max_holding_count}|" + cls.format_conditions(conditions)
