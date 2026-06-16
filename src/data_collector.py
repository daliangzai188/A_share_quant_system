from __future__ import annotations

from pathlib import Path
import time
from typing import Iterable

import pandas as pd

from src.data_source import TushareDataSource
from src.trading_calendar import TradingCalendar
from src.utils.config import get_project_root, load_json_config, mkdir_p
from src.utils.logger import get_logger


class DataCollector:
    """本地数据采集模块，负责从 Tushare 拉取原始数据并保存 CSV。"""

    def __init__(
        self,
        data_source: TushareDataSource,
        config_path: str | Path = "config/config.json",
    ) -> None:
        self.data_source = data_source
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("data_collector")
        self.calendar = TradingCalendar(data_source=data_source, config_path=config_path)

        data_config = self.config["data"]
        self.daily_dir = self.project_root / data_config.get("daily_dir", "data/raw/daily")
        self.daily_basic_dir = self.project_root / data_config.get("daily_basic_dir", "data/raw/daily_basic")
        self.limit_list_dir = self.project_root / data_config.get("limit_list_dir", "data/raw/limit_list")
        self._mkdir_with_retry(self.daily_dir)
        self._mkdir_with_retry(self.daily_basic_dir)
        self._mkdir_with_retry(self.limit_list_dir)

    def collect_daily_data(
        self,
        start_date: str,
        end_date: str,
        overwrite: bool | None = None,
        include_daily_basic: bool = True,
    ) -> dict[str, int]:
        overwrite = self._resolve_overwrite(overwrite)
        trade_dates = self.calendar.get_open_dates(start_date=start_date, end_date=end_date)
        self.logger.info("开始采集日线数据，交易日数量: %s", len(trade_dates))

        stats = {"daily_saved": 0, "daily_skipped": 0, "daily_basic_saved": 0, "daily_basic_skipped": 0}
        for trade_date in trade_dates:
            daily_saved = self.collect_daily_by_date(trade_date=trade_date, overwrite=overwrite)
            stats["daily_saved" if daily_saved else "daily_skipped"] += 1

            if include_daily_basic:
                try:
                    basic_saved = self.collect_daily_basic_by_date(trade_date=trade_date, overwrite=overwrite)
                    stats["daily_basic_saved" if basic_saved else "daily_basic_skipped"] += 1
                except Exception as exc:
                    if self._is_permission_error(exc):
                        self.logger.warning(
                            "当前 Tushare 账号没有 daily_basic 权限，本次日线采集将跳过每日基本面。"
                            "后续可以使用 --no-daily-basic 显式跳过。原始错误: %s",
                            exc,
                        )
                        include_daily_basic = False
                    else:
                        raise

        self.logger.info("日线采集完成: %s", stats)
        return stats

    def collect_limit_data(
        self,
        start_date: str,
        end_date: str,
        overwrite: bool | None = None,
    ) -> dict[str, int]:
        overwrite = self._resolve_overwrite(overwrite)
        trade_dates = self.calendar.get_open_dates(start_date=start_date, end_date=end_date)
        self.logger.info("开始采集涨停池数据，交易日数量: %s", len(trade_dates))

        stats = {"limit_saved": 0, "limit_skipped": 0}
        for trade_date in trade_dates:
            saved = self.collect_limit_by_date(trade_date=trade_date, overwrite=overwrite)
            stats["limit_saved" if saved else "limit_skipped"] += 1

        self.logger.info("涨停池采集完成: %s", stats)
        return stats

    def collect_daily_by_date(self, trade_date: str, overwrite: bool = False) -> bool:
        output_path = self.daily_dir / f"{trade_date}.csv"
        if self._should_skip(output_path, overwrite):
            return False

        fields = self.config["collection"].get("daily_fields")
        daily = self.data_source.get_daily(trade_date=trade_date, fields=fields)
        if daily.empty:
            self.logger.warning("Tushare 未返回 %s 日线数据，不保存（下次重试可重新采集）", trade_date)
            return False
        self._save_dataframe(daily, output_path)
        self.logger.info("保存日线行情: %s, 行数: %s", output_path, len(daily))
        return True

    def collect_daily_basic_by_date(self, trade_date: str, overwrite: bool = False) -> bool:
        output_path = self.daily_basic_dir / f"{trade_date}.csv"
        if self._should_skip(output_path, overwrite):
            return False

        fields = self.config["collection"].get("daily_basic_fields")
        daily_basic = self.data_source.get_daily_basic(trade_date=trade_date, fields=fields)
        if daily_basic.empty:
            self.logger.warning("Tushare 未返回 %s 基本面数据，不保存（下次重试可重新采集）", trade_date)
            return False
        self._save_dataframe(daily_basic, output_path)
        self.logger.info("保存每日基本面: %s, 行数: %s", output_path, len(daily_basic))
        return True

    def collect_limit_by_date(self, trade_date: str, overwrite: bool = False) -> bool:
        output_path = self.limit_list_dir / f"{trade_date}.csv"
        if self._should_skip(output_path, overwrite):
            return False

        fields = self.config["collection"].get("limit_list_fields")
        limit_type = self.config["collection"].get("limit_type", "U")
        limit_list = self.data_source.get_limit_list(trade_date=trade_date, limit_type=limit_type, fields=fields)
        if limit_list.empty:
            self.logger.warning("Tushare 未返回 %s 涨停池数据，不保存（下次重试可重新采集）", trade_date)
            return False
        self._save_dataframe(limit_list, output_path)
        self.logger.info("保存涨停池: %s, 行数: %s", output_path, len(limit_list))
        return True

    def existing_dates(self, directory: Path) -> list[str]:
        return sorted(path.stem for path in directory.glob("*.csv"))

    @staticmethod
    def missing_dates(trade_dates: Iterable[str], existing_dates: Iterable[str]) -> list[str]:
        existing = set(existing_dates)
        return [trade_date for trade_date in trade_dates if trade_date not in existing]

    def _resolve_overwrite(self, overwrite: bool | None) -> bool:
        if overwrite is not None:
            return overwrite
        return bool(self.config.get("collection", {}).get("overwrite", False))

    def _should_skip(self, output_path: Path, overwrite: bool) -> bool:
        if overwrite:
            return False
        if not self._exists_with_retry(output_path):
            return False
        # 文件存在但无数据行（上次采集 Tushare 返回空）→ 允许重新采集
        try:
            with output_path.open(encoding="utf-8-sig") as f:
                f.readline()  # 跳过表头
                return bool(f.readline().strip())  # 有数据行才跳过
        except OSError:
            return True

    def _save_dataframe(self, data: pd.DataFrame, output_path: Path) -> None:
        self._mkdir_with_retry(output_path.parent)
        self._write_csv_with_retry(data, output_path)

    def _mkdir_with_retry(self, path: Path, retries: int = 3, delay: float = 2.0) -> None:
        mkdir_p(path)

    def _exists_with_retry(self, path: Path, retries: int = 3, delay: float = 1.0) -> bool:
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return path.exists()
            except OSError as exc:
                last_error = exc
                self.logger.warning(
                    "共享盘文件检查失败，第 %d/%d 次重试: %s; error=%s",
                    attempt,
                    retries,
                    path,
                    exc,
                )
                time.sleep(delay * attempt)
        if last_error is not None:
            raise last_error
        return False

    def _write_csv_with_retry(self, data: pd.DataFrame, output_path: Path, retries: int = 3, delay: float = 2.0) -> None:
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                data.to_csv(output_path, index=False, encoding="utf-8-sig")
                return
            except OSError as exc:
                last_error = exc
                self.logger.warning(
                    "共享盘CSV写入失败，第 %d/%d 次重试: %s; error=%s",
                    attempt,
                    retries,
                    output_path,
                    exc,
                )
                time.sleep(delay * attempt)
        if last_error is not None:
            raise last_error

    @staticmethod
    def _is_permission_error(exc: Exception) -> bool:
        message = str(exc)
        return "没有接口" in message or "访问权限" in message
