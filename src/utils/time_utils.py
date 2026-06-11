"""北京时间工具函数，项目内所有时间计算统一使用此模块。"""
from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def now_beijing() -> datetime.datetime:
    return datetime.datetime.now(tz=BEIJING_TZ)


def today_beijing() -> datetime.date:
    return datetime.datetime.now(tz=BEIJING_TZ).date()


def yesterday_beijing() -> datetime.date:
    return today_beijing() - datetime.timedelta(days=1)


def beijing_date_str(date: datetime.date | None = None) -> str:
    """返回 YYYYMMDD 格式的北京日期字符串，默认今天。"""
    return (date or today_beijing()).strftime("%Y%m%d")
