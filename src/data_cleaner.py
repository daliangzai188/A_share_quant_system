from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger


class DataCleaner:
    """将原始采集 CSV 清洗为后续统计、成交概率模型和回测可用的标准表。"""

    def __init__(self, config_path: str | Path = "config/config.json") -> None:
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("data_cleaner")

        data_config = self.config["data"]
        cleaning_config = self.config.get("cleaning", {})
        self.daily_dir = self.project_root / data_config.get("daily_dir", "data/raw/daily")
        self.daily_basic_dir = self.project_root / data_config.get("daily_basic_dir", "data/raw/daily_basic")
        self.limit_list_dir = self.project_root / data_config.get("limit_list_dir", "data/raw/limit_list")
        self.processed_dir = self.project_root / data_config.get("processed_dir", "data/processed")

        self.exclude_bj = bool(cleaning_config.get("exclude_bj", True))
        self.drop_missing_daily_basic = bool(cleaning_config.get("drop_missing_daily_basic", True))
        self.limit_list_start_date = str(cleaning_config.get("limit_list_start_date", "20191128"))
        self.low_amount_threshold = float(cleaning_config.get("low_amount_threshold", 10000))

        self.daily_merged_path = self.project_root / cleaning_config.get(
            "daily_merged_path", "data/processed/daily_merged.csv"
        )
        self.limit_up_merged_path = self.project_root / cleaning_config.get(
            "limit_up_merged_path", "data/processed/limit_up_merged.csv"
        )
        self.market_sentiment_path = self.project_root / cleaning_config.get(
            "market_sentiment_path", "data/processed/market_sentiment.csv"
        )

    def clean(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        overwrite: bool = True,
    ) -> dict[str, Path]:
        trade_dates = self.discover_trade_dates(start_date=start_date, end_date=end_date)
        if not trade_dates:
            raise RuntimeError("没有发现可清洗的日线 CSV 文件，请先采集数据。")

        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self._prepare_output_files(overwrite=overwrite)

        market_rows: list[dict[str, object]] = []
        daily_total_rows = 0
        limit_total_rows = 0

        for index, trade_date in enumerate(trade_dates, start=1):
            daily_merged = self.clean_daily_by_date(trade_date)
            if not daily_merged.empty:
                self._append_csv(daily_merged, self.daily_merged_path)
                daily_total_rows += len(daily_merged)

            limit_up_merged = self.clean_limit_up_by_date(trade_date, daily_merged)
            if not limit_up_merged.empty:
                self._append_csv(limit_up_merged, self.limit_up_merged_path)
                limit_total_rows += len(limit_up_merged)

            market_rows.append(self.build_market_sentiment_row(trade_date, daily_merged, limit_up_merged))

            if index % 50 == 0 or index == len(trade_dates):
                self.logger.info(
                    "清洗进度: %s/%s, 当前日期: %s, daily累计: %s, limit累计: %s",
                    index,
                    len(trade_dates),
                    trade_date,
                    daily_total_rows,
                    limit_total_rows,
                )

        market_sentiment = pd.DataFrame(market_rows)
        market_sentiment.to_csv(self.market_sentiment_path, index=False, encoding="utf-8-sig")
        self.logger.info("日线合并表已生成: %s, 行数: %s", self.daily_merged_path, daily_total_rows)
        self.logger.info("涨停合并表已生成: %s, 行数: %s", self.limit_up_merged_path, limit_total_rows)
        self.logger.info("市场情绪表已生成: %s, 行数: %s", self.market_sentiment_path, len(market_sentiment))

        return {
            "daily_merged": self.daily_merged_path,
            "limit_up_merged": self.limit_up_merged_path,
            "market_sentiment": self.market_sentiment_path,
        }

    def clean_daily_by_date(self, trade_date: str) -> pd.DataFrame:
        daily_path = self.daily_dir / f"{trade_date}.csv"
        daily_basic_path = self.daily_basic_dir / f"{trade_date}.csv"
        if not daily_path.exists() or not daily_basic_path.exists():
            self.logger.warning("跳过 %s：daily 或 daily_basic 文件不存在", trade_date)
            return pd.DataFrame()

        daily = self._read_csv(daily_path)
        daily_basic = self._read_csv(daily_basic_path)
        if daily.empty or daily_basic.empty:
            self.logger.warning("跳过 %s：daily 或 daily_basic 为空", trade_date)
            return pd.DataFrame()

        daily = self._normalize_trade_date(daily)
        daily_basic = self._normalize_trade_date(daily_basic)

        if self.exclude_bj:
            daily = self._exclude_bj(daily)
            daily_basic = self._exclude_bj(daily_basic)

        how = "inner" if self.drop_missing_daily_basic else "left"
        merged = daily.merge(daily_basic, on=["trade_date", "ts_code"], how=how, validate="one_to_one")
        merged["is_low_amount"] = merged["amount"].fillna(0) < self.low_amount_threshold
        merged["amount_unit"] = "thousand_yuan"
        merged["is_bj"] = merged["ts_code"].str.endswith(".BJ")
        merged["market_segment"] = merged["ts_code"].apply(self.classify_market_segment)
        merged["limit_pct"] = merged["ts_code"].apply(self.classify_limit_pct)
        return merged.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def clean_limit_up_by_date(self, trade_date: str, daily_merged: pd.DataFrame) -> pd.DataFrame:
        if trade_date < self.limit_list_start_date:
            return pd.DataFrame()

        limit_path = self.limit_list_dir / f"{trade_date}.csv"
        if not limit_path.exists():
            return pd.DataFrame()

        limit_up = self._read_csv(limit_path)
        if limit_up.empty:
            return pd.DataFrame()

        limit_up = self._normalize_trade_date(limit_up)
        if self.exclude_bj:
            limit_up = self._exclude_bj(limit_up)
        if "limit" in limit_up.columns:
            limit_up = limit_up[limit_up["limit"] == "U"].copy()
        if limit_up.empty:
            return pd.DataFrame()

        limit_up = limit_up.rename(columns={"close": "limit_close", "pct_chg": "limit_pct_chg"})
        enrich_columns = [
            "trade_date",
            "ts_code",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "pct_chg",
            "vol",
            "amount",
            "turnover_rate",
            "turnover_rate_f",
            "volume_ratio",
            "float_share",
            "free_share",
            "total_mv",
            "circ_mv",
            "is_low_amount",
            "market_segment",
            "limit_pct",
        ]
        existing_columns = [column for column in enrich_columns if column in daily_merged.columns]
        enriched = limit_up.merge(
            daily_merged[existing_columns],
            on=["trade_date", "ts_code"],
            how="left",
            validate="one_to_one",
        )
        enriched["first_time_bucket"] = enriched["first_time"].apply(self.classify_limit_time_bucket)
        enriched["board_type"] = enriched.apply(self.classify_board_type, axis=1)
        if "market_segment" not in enriched.columns:
            enriched["market_segment"] = enriched["ts_code"].apply(self.classify_market_segment)
        enriched["limit_pct"] = enriched.apply(
            lambda row: self.classify_limit_pct(row.get("ts_code"), row.get("name")),
            axis=1,
        )
        enriched["limit_pct_bucket"] = enriched["limit_pct"].apply(self.classify_limit_pct_bucket)
        enriched["fd_amount_to_circ_mv"] = self._safe_ratio(
            enriched.get("fd_amount"),
            enriched.get("circ_mv"),
            denominator_multiplier=10000,
        )
        enriched["is_fd_amount_abnormal"] = enriched["fd_amount_to_circ_mv"] > self.config.get("fill_model", {}).get(
            "fd_amount_abnormal_ratio_threshold", 1.0
        )
        return enriched.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def build_market_sentiment_row(
        self,
        trade_date: str,
        daily_merged: pd.DataFrame,
        limit_up_merged: pd.DataFrame,
    ) -> dict[str, object]:
        if daily_merged.empty:
            row = {
                "trade_date": trade_date,
                "stock_count": 0,
                "up_count": 0,
                "down_count": 0,
                "flat_count": 0,
                "limit_up_count": len(limit_up_merged),
                "limit_up_max_height": 0,
                "one_word_limit_count": 0,
                "opened_limit_count": 0,
                "total_amount": 0.0,
                "limit_up_fd_amount_sum": float(limit_up_merged.get("fd_amount", pd.Series(dtype=float)).sum()),
                "market_sentiment_level": "unknown",
            }
            row.update(self.build_segment_sentiment_fields(daily_merged, limit_up_merged))
            return row

        pct_chg = daily_merged["pct_chg"].fillna(0)
        limit_times = limit_up_merged.get("limit_times", pd.Series(dtype=float))
        limit_up_count = len(limit_up_merged)
        row = {
            "trade_date": trade_date,
            "stock_count": len(daily_merged),
            "up_count": int((pct_chg > 0).sum()),
            "down_count": int((pct_chg < 0).sum()),
            "flat_count": int((pct_chg == 0).sum()),
            "limit_up_count": limit_up_count,
            "limit_up_max_height": int(limit_times.fillna(0).max()) if not limit_times.empty else 0,
            "one_word_limit_count": int((limit_up_merged.get("open_times", pd.Series(dtype=float)).fillna(0) == 0).sum())
            if not limit_up_merged.empty
            else 0,
            "opened_limit_count": int((limit_up_merged.get("open_times", pd.Series(dtype=float)).fillna(0) > 0).sum())
            if not limit_up_merged.empty
            else 0,
            "total_amount": float(daily_merged["amount"].fillna(0).sum()),
            "limit_up_fd_amount_sum": float(limit_up_merged.get("fd_amount", pd.Series(dtype=float)).fillna(0).sum()),
            "market_sentiment_level": self.classify_market_sentiment(limit_up_count),
        }
        row.update(self.build_segment_sentiment_fields(daily_merged, limit_up_merged))
        return row

    def discover_trade_dates(self, start_date: str | None = None, end_date: str | None = None) -> list[str]:
        dates = sorted(path.stem for path in self.daily_dir.glob("*.csv"))
        return [
            trade_date
            for trade_date in dates
            if (start_date is None or trade_date >= start_date) and (end_date is None or trade_date <= end_date)
        ]

    @staticmethod
    def classify_market_sentiment(limit_up_count: int) -> str:
        if limit_up_count > 150:
            return "very_strong"
        if limit_up_count >= 100:
            return "strong"
        if limit_up_count >= 50:
            return "neutral"
        return "weak"

    @classmethod
    def build_segment_sentiment_fields(
        cls,
        daily_merged: pd.DataFrame,
        limit_up_merged: pd.DataFrame,
    ) -> dict[str, object]:
        fields: dict[str, object] = {}
        segments = ["sh_main", "sz_main", "chi_next", "star", "bj", "other"]
        if "market_segment" not in daily_merged.columns and not daily_merged.empty:
            daily_merged = daily_merged.copy()
            daily_merged["market_segment"] = daily_merged["ts_code"].apply(cls.classify_market_segment)
        if "market_segment" not in limit_up_merged.columns and not limit_up_merged.empty:
            limit_up_merged = limit_up_merged.copy()
            limit_up_merged["market_segment"] = limit_up_merged["ts_code"].apply(cls.classify_market_segment)

        for segment in segments:
            stock_count = int((daily_merged.get("market_segment", pd.Series(dtype=object)) == segment).sum())
            limit_count = int((limit_up_merged.get("market_segment", pd.Series(dtype=object)) == segment).sum())
            ratio = float(limit_count / stock_count) if stock_count else 0.0
            fields[f"{segment}_stock_count"] = stock_count
            fields[f"{segment}_limit_up_count"] = limit_count
            fields[f"{segment}_limit_up_ratio"] = ratio
            fields[f"{segment}_market_sentiment_level"] = cls.classify_segment_sentiment(limit_count, stock_count)

        fields["main_board_limit_up_count"] = fields["sh_main_limit_up_count"] + fields["sz_main_limit_up_count"]
        fields["growth_board_limit_up_count"] = fields["chi_next_limit_up_count"] + fields["star_limit_up_count"]
        fields["limit_5cm_count"] = int((limit_up_merged.get("limit_pct", pd.Series(dtype=float)) == 0.05).sum())
        fields["limit_10cm_count"] = int((limit_up_merged.get("limit_pct", pd.Series(dtype=float)) == 0.10).sum())
        fields["limit_20cm_count"] = int((limit_up_merged.get("limit_pct", pd.Series(dtype=float)) == 0.20).sum())
        fields["limit_30cm_count"] = int((limit_up_merged.get("limit_pct", pd.Series(dtype=float)) == 0.30).sum())
        return fields

    @staticmethod
    def classify_segment_sentiment(limit_up_count: int, stock_count: int) -> str:
        if stock_count <= 0:
            return "unknown"
        ratio = limit_up_count / stock_count
        if ratio >= 0.03:
            return "very_strong"
        if ratio >= 0.02:
            return "strong"
        if ratio >= 0.01:
            return "neutral"
        return "weak"

    @staticmethod
    def classify_market_segment(ts_code: object) -> str:
        if pd.isna(ts_code):
            return "unknown"
        code = str(ts_code).upper()
        prefix = code.split(".")[0]
        if code.endswith(".BJ") or prefix.startswith(("4", "8", "9")):
            return "bj"
        if prefix.startswith(("688", "689")):
            return "star"
        if prefix.startswith(("300", "301")):
            return "chi_next"
        if code.endswith(".SH") and prefix.startswith("6"):
            return "sh_main"
        if code.endswith(".SZ") and prefix.startswith(("000", "001", "002", "003")):
            return "sz_main"
        return "other"

    @classmethod
    def classify_limit_pct(cls, ts_code: object, name: object | None = None) -> float:
        stock_name = "" if name is None or pd.isna(name) else str(name).upper()
        if "ST" in stock_name or "退" in stock_name:
            return 0.05
        segment = cls.classify_market_segment(ts_code)
        if segment == "bj":
            return 0.30
        if segment in {"chi_next", "star"}:
            return 0.20
        return 0.10

    @staticmethod
    def classify_limit_pct_bucket(value: object) -> str:
        if pd.isna(value):
            return "unknown"
        value = float(value)
        if value <= 0.051:
            return "5cm"
        if value <= 0.101:
            return "10cm"
        if value <= 0.201:
            return "20cm"
        if value <= 0.301:
            return "30cm"
        return "other"

    @staticmethod
    def classify_limit_time_bucket(value: object) -> str:
        if pd.isna(value):
            return "unknown"
        try:
            time_value = int(value)
        except (TypeError, ValueError):
            return "unknown"
        if time_value <= 93100:
            return "open_limit"
        if time_value < 100000:
            return "early_morning"
        if time_value < 140000:
            return "midday"
        if time_value < 143000:
            return "afternoon"
        return "late"

    @staticmethod
    def classify_board_type(row: pd.Series) -> str:
        open_times = row.get("open_times")
        first_time = row.get("first_time")
        last_time = row.get("last_time")
        if pd.isna(open_times):
            return "unknown"
        if int(open_times) == 0 and first_time == last_time:
            return "one_word"
        if int(open_times) == 1:
            return "t_board"
        if int(open_times) > 1:
            return "multi_open"
        return "unknown"

    @staticmethod
    def _safe_ratio(
        numerator: pd.Series | None,
        denominator: pd.Series | None,
        denominator_multiplier: float = 1.0,
    ) -> pd.Series:
        if numerator is None or denominator is None:
            return pd.Series(dtype=float)
        denominator = denominator.replace(0, pd.NA) * denominator_multiplier
        return numerator / denominator

    @staticmethod
    def _read_csv(path: Path) -> pd.DataFrame:
        return pd.read_csv(path, dtype={"trade_date": str, "ts_code": str})

    @staticmethod
    def _normalize_trade_date(data: pd.DataFrame) -> pd.DataFrame:
        if "trade_date" in data.columns:
            data["trade_date"] = data["trade_date"].astype(str)
        return data

    @staticmethod
    def _exclude_bj(data: pd.DataFrame) -> pd.DataFrame:
        if "ts_code" not in data.columns:
            return data
        return data[~data["ts_code"].astype(str).str.endswith(".BJ")].copy()

    @staticmethod
    def _append_csv(data: pd.DataFrame, output_path: Path) -> None:
        write_header = not output_path.exists()
        data.to_csv(output_path, mode="a", header=write_header, index=False, encoding="utf-8-sig")

    def _prepare_output_files(self, overwrite: bool) -> None:
        for path in [self.daily_merged_path, self.limit_up_merged_path, self.market_sentiment_path]:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if overwrite:
                    path.unlink()
                else:
                    raise FileExistsError(f"输出文件已存在，如需重建请使用 overwrite=True: {path}")
