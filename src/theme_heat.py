from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.factors import FactorAnalyzer
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class ThemeHeatBuilder:
    """从涨停池中按可用题材字段动态聚合题材热度。"""

    THEME_COLUMN_PRIORITY = ["lu_desc", "theme_name", "concept_name", "industry"]

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("theme_heat")
        feature_config = self.config.get("dynamic_features", {})
        cleaning_config = self.config.get("cleaning", {})
        self.limit_up_merged_path = self.project_root / cleaning_config.get(
            "limit_up_merged_path",
            "data/processed/limit_up_merged.csv",
        )
        self.output_path = self.project_root / feature_config.get(
            "theme_heat_features_path",
            "data/processed/theme_heat_features.csv",
        )

    def build(self) -> Path:
        limit_up = pd.read_csv(
            self.limit_up_merged_path,
            dtype={"trade_date": str, "ts_code": str},
            low_memory=False,
        )
        theme_column = self.resolve_theme_column(limit_up)
        if theme_column is None:
            features = self.build_unavailable_features(limit_up)
            self.logger.warning("没有可用题材字段，题材热度输出为 unavailable。")
        else:
            features = self.build_theme_features(limit_up, theme_column)
            self.logger.info("使用题材字段构建热度: %s", theme_column)

        mkdir_p(self.output_path.parent)
        features.to_csv(self.output_path, index=False, encoding="utf-8-sig")
        self.logger.info("题材热度特征已生成: %s, 行数: %s", self.output_path, len(features))
        return self.output_path

    def resolve_theme_column(self, data: pd.DataFrame) -> str | None:
        for column in self.THEME_COLUMN_PRIORITY:
            if column in data.columns and data[column].notna().any():
                non_empty = data[column].fillna("").astype(str).str.strip()
                if (non_empty != "").any():
                    return column
        return None

    @staticmethod
    def build_unavailable_features(limit_up: pd.DataFrame) -> pd.DataFrame:
        base = limit_up[["trade_date", "ts_code"]].copy()
        base["theme_data_available"] = False
        base["theme_source_column"] = "none"
        base["theme_name"] = "unknown"
        base["theme_limit_count"] = pd.NA
        base["theme_limit_height"] = pd.NA
        base["theme_chain_count"] = pd.NA
        base["theme_fd_amount_sum"] = pd.NA
        base["theme_open_times_sum"] = pd.NA
        base["theme_open_rate"] = pd.NA
        base["theme_heat_score"] = pd.NA
        base["theme_heat_rank"] = pd.NA
        base["theme_leader_rank"] = pd.NA
        base["theme_height_rank"] = pd.NA
        base["theme_is_mainline"] = False
        base["same_theme_limit_count"] = pd.NA
        return base

    def build_theme_features(self, limit_up: pd.DataFrame, theme_column: str) -> pd.DataFrame:
        data = limit_up.copy()
        data["theme_name"] = data[theme_column].fillna("unknown").astype(str).str.strip()
        data.loc[data["theme_name"] == "", "theme_name"] = "unknown"
        data["first_time_minutes_for_rank"] = data["first_time"].apply(FactorAnalyzer.parse_time_to_minutes)
        data["limit_times_numeric"] = pd.to_numeric(data.get("limit_times"), errors="coerce").fillna(0)
        data["fd_amount_numeric"] = pd.to_numeric(data.get("fd_amount"), errors="coerce").fillna(0)
        data["open_times_numeric"] = pd.to_numeric(data.get("open_times"), errors="coerce").fillna(0)

        grouped = data.groupby(["trade_date", "theme_name"], dropna=False)
        theme_stats = grouped.agg(
            theme_limit_count=("ts_code", "count"),
            theme_limit_height=("limit_times_numeric", "max"),
            theme_chain_count=("limit_times_numeric", lambda series: int((series >= 2).sum())),
            theme_fd_amount_sum=("fd_amount_numeric", "sum"),
            theme_open_times_sum=("open_times_numeric", "sum"),
            theme_opened_count=("open_times_numeric", lambda series: int((series > 0).sum())),
        ).reset_index()
        theme_stats["theme_open_rate"] = theme_stats["theme_opened_count"] / theme_stats["theme_limit_count"].replace(0, pd.NA)
        theme_stats["theme_heat_score"] = (
            theme_stats["theme_limit_count"] * 3
            + theme_stats["theme_limit_height"] * 2
            + theme_stats["theme_chain_count"] * 2
            + theme_stats["theme_fd_amount_sum"].rank(pct=True) * 2
            - theme_stats["theme_open_rate"].fillna(0) * 2
        )
        theme_stats["theme_heat_rank"] = theme_stats.groupby("trade_date")["theme_heat_score"].rank(
            method="dense",
            ascending=False,
        )
        theme_stats["theme_is_mainline"] = theme_stats["theme_heat_rank"] <= 3

        data = data.merge(theme_stats, on=["trade_date", "theme_name"], how="left", validate="many_to_one")
        data["theme_leader_rank"] = (
            data.sort_values(
                ["trade_date", "theme_name", "limit_times_numeric", "first_time_minutes_for_rank", "open_times_numeric", "amount"],
                ascending=[True, True, False, True, False, False],
            )
            .groupby(["trade_date", "theme_name"], dropna=False)
            .cumcount()
            + 1
        )
        data["theme_height_rank"] = data.groupby(["trade_date", "theme_name"], dropna=False)["limit_times_numeric"].rank(
            method="dense",
            ascending=False,
        )
        data["same_theme_limit_count"] = data["theme_limit_count"]
        output_columns = [
            "trade_date",
            "ts_code",
            "theme_name",
            "theme_limit_count",
            "theme_limit_height",
            "theme_chain_count",
            "theme_fd_amount_sum",
            "theme_open_times_sum",
            "theme_open_rate",
            "theme_heat_score",
            "theme_heat_rank",
            "theme_leader_rank",
            "theme_height_rank",
            "theme_is_mainline",
            "same_theme_limit_count",
        ]
        result = data[output_columns].copy()
        result["theme_data_available"] = True
        result["theme_source_column"] = theme_column
        return result[
            [
                "trade_date",
                "ts_code",
                "theme_data_available",
                "theme_source_column",
                "theme_name",
                "theme_limit_count",
                "theme_limit_height",
                "theme_chain_count",
                "theme_fd_amount_sum",
                "theme_open_times_sum",
                "theme_open_rate",
                "theme_heat_score",
                "theme_heat_rank",
                "theme_leader_rank",
                "theme_height_rank",
                "theme_is_mainline",
                "same_theme_limit_count",
            ]
        ]
