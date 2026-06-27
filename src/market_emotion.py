from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class MarketEmotionBuilder:
    """构建全市场和分市场板块的动态情绪特征。"""

    SEGMENTS = ["sh_main", "sz_main", "chi_next", "star", "bj", "other"]

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("market_emotion")
        feature_config = self.config.get("dynamic_features", {})
        analysis_config = self.config.get("analysis", {})
        cleaning_config = self.config.get("cleaning", {})
        self.daily_merged_path = self.project_root / analysis_config.get(
            "input_daily_merged_path",
            "data/processed/daily_merged.csv",
        )
        self.daily_merged_by_date_dir = self.project_root / cleaning_config.get(
            "daily_merged_by_date_dir",
            "data/processed/daily_merged_by_date",
        )
        self.limit_up_merged_path = self.project_root / cleaning_config.get(
            "limit_up_merged_path",
            "data/processed/limit_up_merged.csv",
        )
        self.output_path = self.project_root / feature_config.get(
            "market_emotion_features_path",
            "data/processed/market_emotion_features.csv",
        )

    def build(self, start_date: str | None = None, end_date: str | None = None) -> Path:
        if start_date or end_date:
            return self.build_incremental(start_date=start_date, end_date=end_date)
        daily = pd.read_csv(
            self.daily_merged_path,
            dtype={"trade_date": str, "ts_code": str},
            low_memory=False,
        )
        limit_up = pd.read_csv(
            self.limit_up_merged_path,
            dtype={"trade_date": str, "ts_code": str},
            low_memory=False,
        )
        features = self.build_base_features(daily=daily, limit_up=limit_up)
        features = self.add_state_features(features)
        mkdir_p(self.output_path.parent)
        features.to_csv(self.output_path, index=False, encoding="utf-8-sig")
        self.logger.info("市场情绪特征已生成: %s, 行数: %s", self.output_path, len(features))
        return self.output_path

    def build_incremental(self, start_date: str | None = None, end_date: str | None = None) -> Path:
        """只更新指定交易日的市场情绪特征，不读取 daily_merged.csv 大文件。

        实盘收盘流水线只补目标信号日。日线数据来自
        data/processed/daily_merged_by_date/YYYYMMDD.csv 分片，避免每次打开
        250万行级别 daily_merged.csv。已有历史特征从 output_path 读取，
        移除目标日期后再合并新日期，并统一重算 prev1/prev2 状态字段。
        """
        trade_dates = self.discover_partition_dates(start_date=start_date, end_date=end_date)
        if not trade_dates:
            raise RuntimeError(f"没有找到可构建市场情绪的日线分片: {self.daily_merged_by_date_dir}")
        daily = self.load_daily_partitions(trade_dates)
        limit_up = self.load_limit_up_for_dates(trade_dates)
        new_features = self.build_base_features(daily=daily, limit_up=limit_up)
        existing = pd.DataFrame()
        if self.output_path.exists():
            existing = pd.read_csv(self.output_path, dtype={"trade_date": str}, low_memory=False)
            existing = existing[~existing["trade_date"].astype(str).isin(set(trade_dates))].copy()
            existing = self.drop_state_columns(existing)
        combined = pd.concat([existing, new_features], ignore_index=True, sort=False)
        features = self.add_state_features(combined)
        mkdir_p(self.output_path.parent)
        features.to_csv(self.output_path, index=False, encoding="utf-8-sig")
        self.logger.info(
            "市场情绪特征已增量更新: %s, 日期=%s, 行数: %s",
            self.output_path,
            ",".join(trade_dates),
            len(features),
        )
        return self.output_path

    def build_base_features(self, daily: pd.DataFrame, limit_up: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for trade_date, daily_group in daily.groupby("trade_date", sort=True):
            limit_group = limit_up[limit_up["trade_date"] == trade_date].copy()
            global_features = self.build_global_features(daily_group, limit_group)
            for segment in self.SEGMENTS:
                rows.append(
                    {
                        "trade_date": trade_date,
                        "market_segment": segment,
                        **global_features,
                        **self.build_segment_features(daily_group, limit_group, segment),
                    }
                )

        return pd.DataFrame(rows)

    def discover_partition_dates(self, start_date: str | None = None, end_date: str | None = None) -> list[str]:
        dates = sorted(path.stem for path in self.daily_merged_by_date_dir.glob("*.csv"))
        return [
            date
            for date in dates
            if (start_date is None or date >= str(start_date)) and (end_date is None or date <= str(end_date))
        ]

    def load_daily_partitions(self, trade_dates: list[str]) -> pd.DataFrame:
        frames = []
        for trade_date in trade_dates:
            path = self.daily_merged_by_date_dir / f"{trade_date}.csv"
            if path.exists():
                frames.append(pd.read_csv(path, dtype={"trade_date": str, "ts_code": str}, low_memory=False))
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True, sort=False)

    def load_limit_up_for_dates(self, trade_dates: list[str]) -> pd.DataFrame:
        if not self.limit_up_merged_path.exists():
            return pd.DataFrame()
        limit_up = pd.read_csv(self.limit_up_merged_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        return limit_up[limit_up["trade_date"].astype(str).isin(set(trade_dates))].copy()

    @staticmethod
    def drop_state_columns(features: pd.DataFrame) -> pd.DataFrame:
        state_columns = {
            "segment_emotion_score",
            "segment_emotion_state",
            "market_emotion_score",
            "market_emotion_state",
        }
        drop_columns = [
            column
            for column in features.columns
            if column in state_columns or column.endswith("_prev1") or column.endswith("_prev2")
        ]
        return features.drop(columns=drop_columns, errors="ignore")

    def build_global_features(self, daily: pd.DataFrame, limit_up: pd.DataFrame) -> dict[str, object]:
        limit_down = self.count_limit_down(daily)
        full_quality = self.has_full_limit_data(limit_up)
        limit_times = pd.to_numeric(limit_up.get("limit_times", pd.Series(dtype=float)), errors="coerce")
        open_times = pd.to_numeric(limit_up.get("open_times", pd.Series(dtype=float)), errors="coerce").fillna(0)
        return {
            "market_stock_count": int(len(daily)),
            "market_limit_up_count": int(len(limit_up)),
            "market_limit_up_ratio": float(len(limit_up) / len(daily)) if len(daily) else 0.0,
            "market_limit_down_count": int(limit_down),
            "market_limit_down_ratio": float(limit_down / len(daily)) if len(daily) else 0.0,
            "market_limit_max_height": int(limit_times.fillna(0).max()) if full_quality and not limit_times.empty else 0,
            "market_chain_count": int((limit_times.fillna(0) >= 2).sum()) if full_quality else 0,
            "market_height_2_count": int((limit_times.fillna(0) == 2).sum()) if full_quality else 0,
            "market_height_3_count": int((limit_times.fillna(0) == 3).sum()) if full_quality else 0,
            "market_height_gte4_count": int((limit_times.fillna(0) >= 4).sum()) if full_quality else 0,
            "market_one_word_limit_count": int((open_times == 0).sum()) if full_quality and not limit_up.empty else 0,
            "market_opened_limit_count": int((open_times > 0).sum()) if full_quality and not limit_up.empty else 0,
            "limit_data_quality": "full" if full_quality else "basic_limit_only",
            "strategy_compatible": bool(full_quality),
        }

    def build_segment_features(
        self,
        daily: pd.DataFrame,
        limit_up: pd.DataFrame,
        segment: str,
    ) -> dict[str, object]:
        daily_segment = daily[daily["market_segment"].astype(str) == segment].copy()
        limit_segment = limit_up[limit_up["market_segment"].astype(str) == segment].copy()
        stock_count = len(daily_segment)
        limit_down = self.count_limit_down(daily_segment)
        full_quality = self.has_full_limit_data(limit_segment)
        limit_times = pd.to_numeric(limit_segment.get("limit_times", pd.Series(dtype=float)), errors="coerce")
        open_times = pd.to_numeric(limit_segment.get("open_times", pd.Series(dtype=float)), errors="coerce").fillna(0)
        limit_count = len(limit_segment)
        opened_count = int((open_times > 0).sum()) if full_quality and not limit_segment.empty else 0
        return {
            "segment_stock_count_emotion": int(stock_count),
            "segment_limit_up_count_emotion": int(limit_count),
            "segment_limit_up_ratio_emotion": float(limit_count / stock_count) if stock_count else 0.0,
            "segment_limit_down_count": int(limit_down),
            "segment_limit_down_ratio": float(limit_down / stock_count) if stock_count else 0.0,
            "segment_limit_max_height": int(limit_times.fillna(0).max()) if full_quality and not limit_times.empty else 0,
            "segment_chain_count": int((limit_times.fillna(0) >= 2).sum()) if full_quality else 0,
            "segment_height_2_count": int((limit_times.fillna(0) == 2).sum()) if full_quality else 0,
            "segment_height_3_count": int((limit_times.fillna(0) == 3).sum()) if full_quality else 0,
            "segment_height_gte4_count": int((limit_times.fillna(0) >= 4).sum()) if full_quality else 0,
            "segment_one_word_limit_count": int((open_times == 0).sum()) if full_quality and not limit_segment.empty else 0,
            "segment_opened_limit_count": opened_count,
            "segment_open_rate": float(opened_count / limit_count) if limit_count else 0.0,
        }

    @staticmethod
    def has_full_limit_data(limit_up: pd.DataFrame) -> bool:
        if limit_up.empty:
            return False
        quality = (
            limit_up.get("limit_data_quality", pd.Series("full", index=limit_up.index))
            .fillna("full")
            .astype(str)
        )
        compatible = (
            limit_up.get("strategy_compatible", pd.Series(True, index=limit_up.index))
            .fillna(True)
            .astype(str)
            .str.lower()
            .isin({"true", "1"})
        )
        return bool(quality.eq("full").all() and compatible.all())

    @staticmethod
    def count_limit_down(daily: pd.DataFrame) -> int:
        if daily.empty or "pct_chg" not in daily.columns:
            return 0
        pct_chg = pd.to_numeric(daily["pct_chg"], errors="coerce")
        limit_pct = pd.to_numeric(daily.get("limit_pct", pd.Series(0.10, index=daily.index)), errors="coerce").fillna(0.10)
        threshold = -(limit_pct * 100 - 0.5)
        return int((pct_chg <= threshold).sum())

    def add_state_features(self, features: pd.DataFrame) -> pd.DataFrame:
        features = features.sort_values(["market_segment", "trade_date"]).reset_index(drop=True)
        for column in ["segment_limit_up_count_emotion", "segment_limit_down_count", "segment_limit_max_height"]:
            features[f"{column}_prev1"] = features.groupby("market_segment")[column].shift(1)
            features[f"{column}_prev2"] = features.groupby("market_segment")[column].shift(2)
        features["segment_emotion_score"] = features.apply(self.calculate_segment_score, axis=1)
        features["segment_emotion_state"] = features.apply(self.classify_segment_state, axis=1)

        market_unique = features.drop_duplicates("trade_date").sort_values("trade_date").copy()
        for column in ["market_limit_up_count", "market_limit_down_count", "market_limit_max_height"]:
            market_unique[f"{column}_prev1"] = market_unique[column].shift(1)
            market_unique[f"{column}_prev2"] = market_unique[column].shift(2)
        market_unique["market_emotion_score"] = market_unique.apply(self.calculate_market_score, axis=1)
        market_unique["market_emotion_state"] = market_unique.apply(self.classify_market_state, axis=1)
        merge_columns = [
            "trade_date",
            "market_limit_up_count_prev1",
            "market_limit_up_count_prev2",
            "market_limit_down_count_prev1",
            "market_limit_down_count_prev2",
            "market_limit_max_height_prev1",
            "market_limit_max_height_prev2",
            "market_emotion_score",
            "market_emotion_state",
        ]
        return features.merge(market_unique[merge_columns], on="trade_date", how="left", validate="many_to_one")

    @staticmethod
    def calculate_segment_score(row: pd.Series) -> float:
        return float(
            row["segment_limit_up_ratio_emotion"] * 100
            + row["segment_chain_count"] * 0.5
            + row["segment_limit_max_height"] * 0.8
            - row["segment_limit_down_ratio"] * 120
        )

    @staticmethod
    def calculate_market_score(row: pd.Series) -> float:
        return float(
            row["market_limit_up_ratio"] * 100
            + row["market_chain_count"] * 0.2
            + row["market_limit_max_height"] * 0.8
            - row["market_limit_down_ratio"] * 120
        )

    @classmethod
    def classify_segment_state(cls, row: pd.Series) -> str:
        return cls.classify_state(
            limit_count=row["segment_limit_up_count_emotion"],
            limit_ratio=row["segment_limit_up_ratio_emotion"],
            down_ratio=row["segment_limit_down_ratio"],
            max_height=row["segment_limit_max_height"],
            chain_count=row["segment_chain_count"],
            prev1=row["segment_limit_up_count_emotion_prev1"],
            prev2=row["segment_limit_up_count_emotion_prev2"],
        )

    @classmethod
    def classify_market_state(cls, row: pd.Series) -> str:
        return cls.classify_state(
            limit_count=row["market_limit_up_count"],
            limit_ratio=row["market_limit_up_ratio"],
            down_ratio=row["market_limit_down_ratio"],
            max_height=row["market_limit_max_height"],
            chain_count=row["market_chain_count"],
            prev1=row["market_limit_up_count_prev1"],
            prev2=row["market_limit_up_count_prev2"],
        )

    @staticmethod
    def classify_state(
        limit_count: float,
        limit_ratio: float,
        down_ratio: float,
        max_height: float,
        chain_count: float,
        prev1: float,
        prev2: float,
    ) -> str:
        if pd.isna(prev1) or pd.isna(prev2):
            return "unknown"
        if down_ratio >= 0.015 or limit_ratio < 0.005 or limit_count <= 3:
            return "ice_point"
        if limit_count < prev1 < prev2:
            return "retreat"
        if limit_count > prev1 > prev2 and chain_count >= 3:
            return "warming"
        if limit_ratio >= 0.03 and max_height >= 5:
            return "climax"
        if limit_ratio >= 0.015 and chain_count >= 3:
            return "main_rise"
        return "mixed"
