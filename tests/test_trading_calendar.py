"""交易日历（src/trading_calendar.py）和北京时间工具（src/utils/time_utils.py）：守护进程、数据采集按它们决定哪天是交易日。

2026-11-14 优化第1步“安全网”补的测试。
"""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.trading_calendar import TradingCalendar
from src.utils import time_utils


class FakeSource:
    def __init__(self, calendar=None, error=None):
        self.calendar, self.error, self.calls = calendar, error, []

    def get_trade_calendar(self, start_date, end_date):
        self.calls.append((start_date, end_date))
        if self.error:
            raise self.error
        return self.calendar[self.calendar["cal_date"].between(start_date, end_date)].reset_index(drop=True)


def official(dates_open):
    return pd.DataFrame({"exchange": "SSE", "cal_date": [d for d, _ in dates_open],
                         "is_open": [o for _, o in dates_open], "pretrade_date": ""})


class TradingCalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = Path(self.tmp.name) / "raw" / "trade_calendar.csv"
        self.config = Path(self.tmp.name) / "config.json"
        self.config.write_text(json.dumps({"data": {"trade_calendar_path": str(self.csv)}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def calendar(self, source):
        return TradingCalendar(source, config_path=self.config)

    def test_fetches_saves_and_filters_open_days(self):
        source = FakeSource(official([("20261113", 1), ("20261114", 0), ("20261115", 0), ("20261116", 1),
                                      ("20261117", 1)]))
        cal = self.calendar(source)
        self.assertEqual(cal.calendar_path, self.csv)
        self.assertEqual(cal.get_open_dates("20261113", "20261116"), ["20261113", "20261116"])
        self.assertTrue(self.csv.exists())
        self.assertEqual(cal.get_open_dates("20261114", "20261116"), ["20261116"])
        self.assertEqual(source.calls, [("20261113", "20261116")], "缓存覆盖了区间就不再拉取")
        self.assertEqual(cal.get_open_dates("20261114", "20261117"), ["20261116", "20261117"])
        self.assertEqual(source.calls[-1], ("20261114", "20261117"))

    def test_cache_not_covering_range_or_weekday_fallback_is_refetched(self):
        source = FakeSource(official([("20261113", 1), ("20261116", 1)]))
        cal = self.calendar(source)
        cal.fetch_and_save("20261113", "20261113")
        cal.fetch_and_save("20261113", "20261116")
        self.assertEqual(len(source.calls), 2, "缓存不覆盖区间要重拉")
        TradingCalendar._build_weekday_calendar("20261113", "20261116").to_csv(self.csv, index=False)
        cal.fetch_and_save("20261113", "20261116")
        self.assertEqual(len(source.calls), 3, "工作日占位日历要重新尝试拉正式日历")
        self.assertEqual(cal.fetch_and_save("20261113", "20261116", overwrite=False)["exchange"].iloc[0], "SSE")

    def test_permission_error_falls_back_to_weekdays(self):
        cal = self.calendar(FakeSource(error=RuntimeError("抱歉，您没有接口(trade_cal)访问权限")))
        frame = cal.fetch_and_save("20261113", "20261116")
        self.assertEqual(frame["cal_date"].tolist(), ["20261113", "20261116"])
        self.assertTrue(frame["exchange"].eq("LOCAL_WEEKDAY_FALLBACK").all())
        self.assertTrue(TradingCalendar._is_weekday_fallback_calendar(pd.read_csv(self.csv, dtype={"cal_date": str})))

    def test_other_errors_propagate(self):
        with self.assertRaisesRegex(RuntimeError, "network down"):
            self.calendar(FakeSource(error=RuntimeError("network down"))).fetch_and_save("20261113", "20261116")
        self.assertFalse(self.csv.exists())

    def test_static_helpers(self):
        self.assertEqual(TradingCalendar.filter_dates(["20261117", "20261112", "20261113"], "20261113", "20261117"),
                         ["20261113", "20261117"])
        self.assertFalse(TradingCalendar._covers_range(pd.DataFrame(), "20261113", "20261116"))
        self.assertFalse(TradingCalendar._is_weekday_fallback_calendar(pd.DataFrame({"cal_date": ["20261113"]})))


class BeijingTimeTests(unittest.TestCase):
    def test_beijing_zone_and_formats(self):
        self.assertEqual(str(time_utils.BEIJING_TZ), "Asia/Shanghai")
        self.assertEqual(time_utils.now_beijing().utcoffset(), datetime.timedelta(hours=8))
        self.assertEqual(time_utils.beijing_date_str(datetime.date(2026, 11, 14)), "20261114")
        today = time_utils.today_beijing()
        self.assertIn(time_utils.beijing_date_str(), {today.strftime("%Y%m%d"),
                                                      (today + datetime.timedelta(days=1)).strftime("%Y%m%d")})
        self.assertEqual(time_utils.today_beijing() - time_utils.yesterday_beijing(), datetime.timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
