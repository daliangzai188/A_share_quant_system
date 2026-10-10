"""账户门禁等待期间的数据维护；调用者只提供本地账本与数据流水线回调。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Any


@dataclass(frozen=True)
class MaintenanceSettings:
    enabled: bool = True
    poll_seconds: float = 30.0
    ledger_seconds: float = 300.0
    retry_seconds: float = 300.0

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> 'MaintenanceSettings':
        section = config.get('offline_data_maintenance', {}) or {}
        return cls(
            enabled=bool(section.get('enabled', True)),
            poll_seconds=max(1.0, float(section.get('poll_seconds', 30))),
            ledger_seconds=max(30.0, float(section.get('ledger_seconds', 300))),
            retry_seconds=max(30.0, float(section.get('retry_seconds', 300))),
        )


def run_maintenance(*, stop: threading.Event, settings: MaintenanceSettings,
                    expected_date: Callable[[], str], refresh_ledger: Callable[[str], None],
                    data_fresh: Callable[[str], bool], start_pipeline: Callable[[str], bool],
                    on_error: Callable[[Exception], None], clock: Callable[[], float] = time.monotonic) -> None:
    """交易验证成功或进程退出时stop唤醒；失败降频重试，不初始化任何券商对象。"""
    if not settings.enabled:
        return
    last_ledger = float('-inf')
    last_attempt: dict[str, float] = {}
    while not stop.is_set():
        try:
            date = expected_date()
            now = clock()
            if now - last_ledger >= settings.ledger_seconds:
                try:
                    refresh_ledger(date)
                except Exception as exc:
                    on_error(exc)
                last_ledger = now
            if (not stop.is_set() and not data_fresh(date)
                    and now - last_attempt.get(date, float('-inf')) >= settings.retry_seconds):
                if start_pipeline(date):
                    last_attempt[date] = now
        except Exception as exc:
            on_error(exc)
        stop.wait(settings.poll_seconds)
