from __future__ import annotations

import csv
import os
from pathlib import Path
import time
from typing import Iterable

import pandas as pd

from src.market_rules import market_segment, price_limit_pct
from src.utils.config import get_project_root, load_json_config, mkdir_p
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
        self.daily_merged_by_date_dir = self.project_root / cleaning_config.get(
            "daily_merged_by_date_dir", "data/processed/daily_merged_by_date"
        )
        self.limit_up_merged_path = self.project_root / cleaning_config.get(
            "limit_up_merged_path", "data/processed/limit_up_merged.csv"
        )
        self.market_sentiment_path = self.project_root / cleaning_config.get(
            "market_sentiment_path", "data/processed/market_sentiment.csv"
        )
        self.live_limit_up_path = self.project_root / cleaning_config.get(
            "live_limit_up_path", "data/processed/live_limit_up_merged.csv"
        )
        self.live_market_sentiment_path = self.project_root / cleaning_config.get(
            "live_market_sentiment_path", "data/processed/live_market_sentiment.csv"
        )

    def clean(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        overwrite: bool = True,
        incremental_replace: bool = False,
    ) -> dict[str, Path]:
        trade_dates = self.discover_trade_dates(start_date=start_date, end_date=end_date)
        if not trade_dates:
            raise RuntimeError("没有发现可清洗的日线 CSV 文件，请先采集数据。")

        self._mkdir_with_retry(self.processed_dir)
        if not incremental_replace:
            self._prepare_output_files(overwrite=overwrite)

        market_rows: list[dict[str, object]] = []
        daily_frames: list[pd.DataFrame] = []
        limit_frames: list[pd.DataFrame] = []
        daily_total_rows = 0
        limit_total_rows = 0

        for index, trade_date in enumerate(trade_dates, start=1):
            daily_merged = self.clean_daily_by_date(trade_date)
            if not daily_merged.empty:
                if incremental_replace:
                    daily_frames.append(daily_merged)
                else:
                    self._append_csv(daily_merged, self.daily_merged_path)
                daily_total_rows += len(daily_merged)

            limit_up_merged = self.clean_limit_up_by_date(trade_date, daily_merged)
            if not limit_up_merged.empty:
                if incremental_replace:
                    limit_frames.append(limit_up_merged)
                else:
                    self._append_csv(limit_up_merged, self.limit_up_merged_path)
                limit_total_rows += len(limit_up_merged)

            market_rows.append(self.build_market_sentiment_row(trade_date, daily_merged, limit_up_merged))

            progress_pct = index / len(trade_dates) * 100
            self.logger.info(
                "清洗进度: %.1f%% (%s/%s)，当前日期: %s，daily累计: %s，limit累计: %s",
                progress_pct,
                index,
                len(trade_dates),
                trade_date,
                daily_total_rows,
                limit_total_rows,
            )

        market_sentiment = pd.DataFrame(market_rows)
        if incremental_replace:
            daily_output = pd.concat(daily_frames, ignore_index=True) if daily_frames else pd.DataFrame()
            limit_output = pd.concat(limit_frames, ignore_index=True) if limit_frames else pd.DataFrame()
            self._write_daily_partitions(daily_output, trade_dates)
            # 实盘增量只写 live_* 文件。全量 limit_up_merged/market_sentiment 属于
            # 回测研究输入，只有手动全量清洗时更新，避免日常启动顺手维护历史大表。
            self._write_csv_with_retry(limit_output, self.live_limit_up_path)
            self._write_csv_with_retry(market_sentiment, self.live_market_sentiment_path)
        else:
            self._write_csv_with_retry(market_sentiment, self.market_sentiment_path)
        daily_output_path = self.daily_merged_by_date_dir if incremental_replace else self.daily_merged_path
        self.logger.info("日线%s已生成: %s, 行数: %s", "分片" if incremental_replace else "合并表", daily_output_path, daily_total_rows)
        limit_output_path = self.live_limit_up_path if incremental_replace else self.limit_up_merged_path
        sentiment_output_path = self.live_market_sentiment_path if incremental_replace else self.market_sentiment_path
        self.logger.info("涨停%s已生成: %s, 行数: %s", "实盘表" if incremental_replace else "合并表", limit_output_path, limit_total_rows)
        self.logger.info("市场情绪%s已生成: %s, 行数: %s", "实盘表" if incremental_replace else "表", sentiment_output_path, len(market_sentiment))

        return {
            "daily_merged": self.daily_merged_by_date_dir if incremental_replace else self.daily_merged_path,
            "limit_up_merged": limit_output_path,
            "market_sentiment": sentiment_output_path,
        }

    def clean_daily_by_date(self, trade_date: str) -> pd.DataFrame:
        daily_path = self.daily_dir / f"{trade_date}.csv"
        daily_basic_path = self.daily_basic_dir / f"{trade_date}.csv"
        if not daily_path.exists() or not daily_basic_path.exists():
            self.logger.warning("跳过 %s：daily 或 daily_basic 文件不存在", trade_date)
            return pd.DataFrame()

        try:
            daily = self._read_csv(daily_path)
            daily_basic = self._read_csv(daily_basic_path)
        except OSError as e:
            self.logger.warning("跳过 %s：读取 daily 文件失败 (%s)", trade_date, e)
            return pd.DataFrame()
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

        try:
            limit_up = self._read_csv(limit_path)
        except OSError as e:
            self.logger.warning("跳过 %s：读取涨停文件失败 (%s)", trade_date, e)
            return pd.DataFrame()
        if limit_up.empty:
            return pd.DataFrame()

        limit_up = self._normalize_trade_date(limit_up)
        if self.exclude_bj:
            limit_up = self._exclude_bj(limit_up)
        if "limit" in limit_up.columns:
            limit_up = limit_up[limit_up["limit"] == "U"].copy()
        if limit_up.empty:
            return pd.DataFrame()
        if "limit_data_source" not in limit_up.columns:
            limit_up["limit_data_source"] = "limit_list_d"
        if "limit_data_quality" not in limit_up.columns:
            limit_up["limit_data_quality"] = "full"
        if "strategy_compatible" not in limit_up.columns:
            limit_up["strategy_compatible"] = True

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
        enriched["is_st"] = enriched["name"].apply(self.is_st_or_delisting_name) if "name" in enriched.columns else False
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
            quality = self.resolve_limit_data_quality(limit_up_merged)
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
                **quality,
            }
            row.update(self.build_segment_sentiment_fields(daily_merged, limit_up_merged))
            return row

        pct_chg = daily_merged["pct_chg"].fillna(0)
        limit_times = limit_up_merged.get("limit_times", pd.Series(dtype=float))
        limit_up_count = len(limit_up_merged)
        quality = self.resolve_limit_data_quality(limit_up_merged)
        has_full_limit_data = quality["limit_data_quality"] == "full"
        row = {
            "trade_date": trade_date,
            "stock_count": len(daily_merged),
            "up_count": int((pct_chg > 0).sum()),
            "down_count": int((pct_chg < 0).sum()),
            "flat_count": int((pct_chg == 0).sum()),
            "limit_up_count": limit_up_count,
            "limit_up_max_height": int(limit_times.fillna(0).max()) if has_full_limit_data and not limit_times.empty else 0,
            "one_word_limit_count": int((limit_up_merged.get("open_times", pd.Series(dtype=float)).fillna(0) == 0).sum())
            if has_full_limit_data and not limit_up_merged.empty
            else 0,
            "opened_limit_count": int((limit_up_merged.get("open_times", pd.Series(dtype=float)).fillna(0) > 0).sum())
            if has_full_limit_data and not limit_up_merged.empty
            else 0,
            "total_amount": float(daily_merged["amount"].fillna(0).sum()),
            "limit_up_fd_amount_sum": float(limit_up_merged.get("fd_amount", pd.Series(dtype=float)).fillna(0).sum())
            if has_full_limit_data
            else 0.0,
            "market_sentiment_level": self.classify_market_sentiment(limit_up_count),
            **quality,
        }
        row.update(self.build_segment_sentiment_fields(daily_merged, limit_up_merged))
        return row

    @staticmethod
    def resolve_limit_data_quality(limit_up_merged: pd.DataFrame) -> dict[str, object]:
        if limit_up_merged.empty:
            return {
                "limit_data_source": "missing",
                "limit_data_quality": "unavailable",
                "strategy_compatible": False,
            }
        quality = (
            limit_up_merged.get("limit_data_quality", pd.Series("full", index=limit_up_merged.index))
            .fillna("full")
            .astype(str)
        )
        source = (
            limit_up_merged.get("limit_data_source", pd.Series("limit_list_d", index=limit_up_merged.index))
            .fillna("limit_list_d")
            .astype(str)
        )
        compatible = (
            limit_up_merged.get("strategy_compatible", pd.Series(True, index=limit_up_merged.index))
            .fillna(True)
            .astype(str)
            .str.lower()
            .isin({"true", "1"})
        )
        return {
            "limit_data_source": ",".join(sorted(source.unique().tolist())),
            "limit_data_quality": "full" if quality.eq("full").all() else "basic_limit_only",
            "strategy_compatible": bool(compatible.all() and quality.eq("full").all()),
        }

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
        return market_segment(ts_code)

    @classmethod
    def classify_limit_pct(cls, ts_code: object, name: object | None = None) -> float:
        stock_name = "" if name is None or pd.isna(name) else str(name)
        return float(price_limit_pct(ts_code, name=stock_name) or 0.10)

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
    def is_st_or_delisting_name(name: object) -> bool:
        if name is None or pd.isna(name):
            return False
        text = str(name).upper()
        return "ST" in text or "退" in text

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
        last_error: OSError | None = None
        for _ in range(3):
            try:
                return pd.read_csv(path, dtype={"trade_date": str, "ts_code": str})
            except OSError as exc:
                last_error = exc
                time.sleep(1)
        if last_error is not None:
            raise last_error
        return pd.DataFrame()

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
        DataCleaner._mkdir_with_retry(output_path.parent)
        last_error: OSError | None = None
        for _ in range(3):
            try:
                write_header = not output_path.exists()
                output = data.copy()
                if not write_header:
                    existing_header = pd.read_csv(output_path, nrows=0).columns.tolist()
                    output = output.reindex(columns=existing_header)
                output.to_csv(output_path, mode="a", header=write_header, index=False, encoding="utf-8-sig")
                return
            except OSError as exc:
                last_error = exc
                time.sleep(1)
        if last_error is not None:
            raise last_error

    def _prepare_output_files(self, overwrite: bool) -> None:
        for path in [self.daily_merged_path, self.limit_up_merged_path, self.market_sentiment_path]:
            self._mkdir_with_retry(path.parent)
            if path.exists():
                if overwrite:
                    path.unlink()
                else:
                    raise FileExistsError(f"输出文件已存在，如需重建请使用 overwrite=True: {path}")

    def _replace_trade_dates_in_output(self, output_path: Path, new_data: pd.DataFrame, trade_dates: Iterable[str]) -> None:
        target_dates = {str(date) for date in trade_dates}
        if not target_dates:
            return
        self._mkdir_with_retry(output_path.parent)
        new_frame = new_data.copy()
        if not new_frame.empty:
            new_frame["trade_date"] = new_frame["trade_date"].astype(str)

        temp_path = output_path.with_name(f"{output_path.name}.tmp.{os.getpid()}")
        if temp_path.exists():
            temp_path.unlink()

        existing_columns: list[str] | None = None
        total_existing = 0
        kept_rows = 0
        wrote_header = False

        if output_path.exists():
            last_copy_error: OSError | None = None
            for attempt in range(5):
                try:
                    total_existing, kept_rows, existing_columns = self._copy_existing_without_trade_dates(
                        source_path=output_path,
                        temp_path=temp_path,
                        target_dates=target_dates,
                    )
                    break
                except OSError as exc:
                    last_copy_error = exc
                    temp_path.unlink(missing_ok=True)
                    if attempt < 4:
                        self.logger.warning(
                            "增量清洗：读取旧文件失败，第%d/5次重试：%s，错误=%s",
                            attempt + 1,
                            output_path,
                            exc,
                        )
                        time.sleep(3)
            else:
                if last_copy_error is not None:
                    raise last_copy_error
            wrote_header = existing_columns is not None

        if existing_columns:
            new_frame = new_frame.reindex(columns=existing_columns)
        if not new_frame.empty or not wrote_header:
            new_frame.to_csv(
                temp_path,
                mode="a",
                header=not wrote_header,
                index=False,
                encoding="utf-8-sig",
            )
            wrote_header = True

        self._replace_file_with_retry(temp_path, output_path)
        removed = total_existing - kept_rows
        self.logger.info(
            "增量清洗：%s 已安全替换日期=%s，移除旧行=%s，写入新行=%s，总行数=%s",
            output_path,
            ",".join(sorted(target_dates)),
            removed,
            len(new_frame),
            kept_rows + len(new_frame),
        )

    def _write_daily_partitions(self, daily_data: pd.DataFrame, trade_dates: Iterable[str]) -> None:
        """实盘增量日线按交易日分片保存，避免维护巨大的 daily_merged.csv。

        daily_merged.csv 只适合离线研究/全量回测。Windows 的 Z: 映射盘打开和替换
        250万行级别单体 CSV 容易出现 OSError 22 或长时间阻塞。实盘日更只需要
        目标信号日当天的日线数据，因此写入 daily_merged_by_date/YYYYMMDD.csv，
        后续动态特征按日期读取分片，不再碰旧大文件。
        """
        self._mkdir_with_retry(self.daily_merged_by_date_dir)
        if daily_data.empty:
            self.logger.warning("增量清洗：本次没有可写入的日线分片，日期=%s", ",".join(map(str, trade_dates)))
            return
        data = daily_data.copy()
        data["trade_date"] = data["trade_date"].astype(str)
        written = 0
        for trade_date, group in data.groupby("trade_date", sort=True):
            output_path = self.daily_merged_by_date_dir / f"{trade_date}.csv"
            self._write_csv_with_retry(group.sort_values(["trade_date", "ts_code"]), output_path)
            written += len(group)
        self.logger.info(
            "增量清洗：日线分片已写入 %s，日期=%s，行数=%s",
            self.daily_merged_by_date_dir,
            ",".join(sorted(data["trade_date"].unique().tolist())),
            written,
        )

    @staticmethod
    def _copy_existing_without_trade_dates(
        source_path: Path,
        temp_path: Path,
        target_dates: set[str],
    ) -> tuple[int, int, list[str] | None]:
        """不用 pandas 读取旧大文件，避免 Windows/Z盘上 TextFileReader 打开大 CSV 失败。"""
        total_rows = 0
        kept_rows = 0
        with source_path.open("r", encoding="utf-8-sig", newline="") as source, temp_path.open(
            "w",
            encoding="utf-8-sig",
            newline="",
        ) as target:
            reader = csv.reader(source)
            writer = csv.writer(target)
            try:
                header = next(reader)
            except StopIteration:
                return 0, 0, None
            writer.writerow(header)
            if "trade_date" not in header:
                for row in reader:
                    total_rows += 1
                    kept_rows += 1
                    writer.writerow(row)
                return total_rows, kept_rows, header

            date_index = header.index("trade_date")
            for row in reader:
                total_rows += 1
                trade_date = row[date_index].strip().strip('"').replace("-", "")[:8] if len(row) > date_index else ""
                if trade_date in target_dates:
                    continue
                kept_rows += 1
                writer.writerow(row)
        return total_rows, kept_rows, header

    @staticmethod
    def _replace_file_with_retry(source: Path, target: Path) -> None:
        last_error: OSError | None = None
        for attempt in range(5):
            try:
                source.replace(target)
                return
            except OSError as exc:
                last_error = exc
                if attempt < 4:
                    time.sleep(2)
        if last_error is not None:
            raise last_error

    @staticmethod
    def _mkdir_with_retry(path: Path) -> None:
        # WebDAV(Z:盘)的 is_dir()/stat() 可能返回 WinError 58，
        # 即使目录实际存在也会抛出。以「能列目录」为成功判断。
        last_error: OSError | None = None
        for attempt in range(5):
            try:
                mkdir_p(path)
                return
            except OSError as exc:
                last_error = exc
            try:
                next(iter(path.iterdir()), None)
                return  # 能列出目录内容说明目录已存在且可用
            except OSError:
                pass
            if attempt < 4:
                time.sleep(2)
        if last_error is not None:
            raise last_error
        raise OSError(f"目录创建失败: {path}")

    @staticmethod
    def _write_csv_with_retry(data: pd.DataFrame, output_path: Path) -> None:
        DataCleaner._mkdir_with_retry(output_path.parent)
        last_error: OSError | None = None
        for _ in range(3):
            try:
                data.to_csv(output_path, index=False, encoding="utf-8-sig")
                return
            except OSError as exc:
                last_error = exc
                time.sleep(1)
        if last_error is not None:
            raise last_error
