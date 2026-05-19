from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from src.data_source import TushareDataSource
from src.utils.config import get_project_root, load_json_config
from src.utils.logger import get_logger


class TradingCalendar:
    """交易日历模块，负责获取、缓存和读取 A股交易日。"""

    def __init__(
        self,
        data_source: TushareDataSource,
        config_path: str | Path = "config/config.json",
    ) -> None:
        self.data_source = data_source
        self.project_root = get_project_root()
        self.config = load_json_config(config_path)
        self.logger = get_logger("trading_calendar")
        calendar_path = self.config["data"].get("trade_calendar_path", "data/raw/trade_calendar.csv")
        self.calendar_path = self.project_root / calendar_path

    def fetch_and_save(self, start_date: str, end_date: str, overwrite: bool = False) -> pd.DataFrame:
        if self.calendar_path.exists() and not overwrite:
            cached = pd.read_csv(self.calendar_path, dtype={"cal_date": str})
            if self._covers_range(cached, start_date, end_date):
                if self._is_weekday_fallback_calendar(cached):
                    self.logger.info("发现本地交易日历是工作日 fallback 缓存，将重新尝试拉取正式交易日历。")
                else:
                    self.logger.info("交易日历缓存已覆盖 %s-%s: %s", start_date, end_date, self.calendar_path)
                    return cached

        self.logger.info("拉取交易日历: %s-%s", start_date, end_date)
        try:
            calendar = self.data_source.get_trade_calendar(start_date=start_date, end_date=end_date)
        except Exception as exc:
            if self._is_permission_error(exc):
                self.logger.warning(
                    "当前 Tushare 账号没有 trade_cal 权限，改用工作日日期作为采集候选范围。"
                    "节假日会在后续行情接口返回空数据时自然跳过。原始错误: %s",
                    exc,
                )
                calendar = self._build_weekday_calendar(start_date=start_date, end_date=end_date)
            else:
                raise
        self.calendar_path.parent.mkdir(parents=True, exist_ok=True)
        calendar.to_csv(self.calendar_path, index=False, encoding="utf-8-sig")
        self.logger.info("交易日历已保存: %s, 行数: %s", self.calendar_path, len(calendar))
        return calendar

    def get_open_dates(self, start_date: str, end_date: str) -> list[str]:
        calendar = self.fetch_and_save(start_date=start_date, end_date=end_date)
        open_days = calendar[calendar["is_open"] == 1].copy()
        open_days["cal_date"] = open_days["cal_date"].astype(str)
        open_dates = open_days["cal_date"].sort_values().tolist()
        return self.filter_dates(open_dates, start_date=start_date, end_date=end_date)

    @staticmethod
    def filter_dates(dates: Iterable[str], start_date: str, end_date: str) -> list[str]:
        return sorted(date for date in dates if start_date <= date <= end_date)

    @staticmethod
    def _covers_range(calendar: pd.DataFrame, start_date: str, end_date: str) -> bool:
        if calendar.empty or "cal_date" not in calendar.columns:
            return False
        cal_dates = calendar["cal_date"].astype(str)
        return cal_dates.min() <= start_date and cal_dates.max() >= end_date

    @staticmethod
    def _is_weekday_fallback_calendar(calendar: pd.DataFrame) -> bool:
        if "exchange" not in calendar.columns:
            return False
        return calendar["exchange"].astype(str).eq("LOCAL_WEEKDAY_FALLBACK").any()

    @staticmethod
    def _is_permission_error(exc: Exception) -> bool:
        message = str(exc)
        return "没有接口" in message or "访问权限" in message

    @staticmethod
    def _build_weekday_calendar(start_date: str, end_date: str) -> pd.DataFrame:
        dates = pd.date_range(
            start=pd.to_datetime(start_date, format="%Y%m%d"),
            end=pd.to_datetime(end_date, format="%Y%m%d"),
            freq="B",
        )
        calendar = pd.DataFrame(
            {
                "exchange": "LOCAL_WEEKDAY_FALLBACK",
                "cal_date": dates.strftime("%Y%m%d"),
                "is_open": 1,
                "pretrade_date": "",
            }
        )
        return calendar
