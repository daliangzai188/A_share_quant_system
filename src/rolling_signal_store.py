"""策略信号与每日运行状态的滚动JSON存储。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


# 信号脚本每天必须落下四种状态之一。前3种都是已经正常完成的终态，只有ERROR
# 表示脚本执行失败。状态账本与信号账本分离，避免为了表达“今日无信号”而向实盘
# 信号文件写入空对象，进而影响现有开仓读取逻辑。
SIGNAL_READY = "SIGNAL_READY"
NO_SIGNAL_OCCUPIED = "NO_SIGNAL_OCCUPIED"
NO_CANDIDATE = "NO_CANDIDATE"
ERROR = "ERROR"
NORMAL_SIGNAL_RUN_STATUSES = frozenset(
    {SIGNAL_READY, NO_SIGNAL_OCCUPIED, NO_CANDIDATE}
)
ALL_SIGNAL_RUN_STATUSES = NORMAL_SIGNAL_RUN_STATUSES | {ERROR}


def load_recent_signals(path: Path) -> list[dict[str, Any]]:
    """Load rolling signal entries from ``path``.

    The current format is {"signals": [...]}. For safety, a bare list is also
    accepted so older hand-written files do not break live planning.
    """
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        signals = payload.get("signals", [])
        if isinstance(signals, list):
            return [x for x in signals if isinstance(x, dict)]
    return []


def save_recent_signal(
    path: Path,
    signal: dict[str, Any],
    *,
    strategy_leg: str,
    max_trade_days: int = 10,
) -> Path:
    """Append or replace one signal and keep only the latest N signal dates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    signals = load_recent_signals(path)
    signal_date = str(signal.get("signal_date", ""))

    merged: dict[str, dict[str, Any]] = {}
    for item in signals:
        item_date = str(item.get("signal_date", ""))
        if item_date:
            merged[item_date] = item
    if signal_date:
        merged[signal_date] = signal

    kept_dates = sorted(merged.keys())[-max_trade_days:]
    kept = [merged[d] for d in kept_dates]
    payload = {
        "strategy_leg": strategy_leg,
        "max_trade_days": max_trade_days,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "signals": kept,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def latest_signal_for_buy_date(path: Path, today: str) -> dict[str, Any] | None:
    """Return the latest signal whose planned_buy_date equals ``today``."""
    matches = [
        signal for signal in load_recent_signals(path)
        if str(signal.get("signal_date", "")) < today
        and str(signal.get("planned_buy_date", "")) == today
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda x: str(x.get("signal_date", "")))[-1]


def signal_by_signal_date(path: Path, signal_date: str) -> dict[str, Any] | None:
    """Return signal whose signal_date equals ``signal_date``."""
    for signal in reversed(load_recent_signals(path)):
        if str(signal.get("signal_date", "")) == signal_date:
            return signal
    return None


def load_recent_signal_runs(path: Path) -> list[dict[str, Any]]:
    """读取最近的信号脚本运行状态；损坏或旧格式文件按空账本处理。"""

    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(payload, dict):
        runs = payload.get("runs", [])
        if isinstance(runs, list):
            return [item for item in runs if isinstance(item, dict)]
    return []


def save_recent_signal_run(
    path: Path,
    run: dict[str, Any],
    *,
    strategy_leg: str,
    max_trade_days: int = 20,
) -> Path:
    """按信号日覆盖保存运行终态，并只保留最近N个交易日。

    同一天重跑时以后一次结果为准，因此可以把先前的ERROR修复为正常终态；同时
    不会在一个交易日堆积多条互相矛盾的审计记录。
    """

    signal_date = str(run.get("signal_date", "")).strip()
    status = str(run.get("status", "")).strip().upper()
    if not signal_date:
        raise ValueError("信号运行状态缺少signal_date")
    if status not in ALL_SIGNAL_RUN_STATUSES:
        raise ValueError(f"未知信号运行状态：{status}")

    path.parent.mkdir(parents=True, exist_ok=True)
    merged: dict[str, dict[str, Any]] = {}
    for item in load_recent_signal_runs(path):
        item_date = str(item.get("signal_date", "")).strip()
        if item_date:
            merged[item_date] = item

    normalized = dict(run)
    normalized["strategy_leg"] = strategy_leg
    normalized["signal_date"] = signal_date
    normalized["status"] = status
    normalized.setdefault("generated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    merged[signal_date] = normalized

    kept_dates = sorted(merged)[-max_trade_days:]
    payload = {
        "strategy_leg": strategy_leg,
        "max_trade_days": max_trade_days,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "runs": [merged[date] for date in kept_dates],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def signal_run_by_signal_date(path: Path, signal_date: str) -> dict[str, Any] | None:
    """返回指定信号日最近一次运行终态。"""

    for run in reversed(load_recent_signal_runs(path)):
        if str(run.get("signal_date", "")) == str(signal_date):
            return run
    return None


def cleanup_legacy_daily_signal_files(signal_dir: Path, pattern: str) -> int:
    """Remove old per-day signal JSON files after rolling store is updated."""
    removed = 0
    for path in signal_dir.glob(pattern):
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
    return removed


def migrate_legacy_daily_signal_files(
    signal_dir: Path,
    pattern: str,
    rolling_path: Path,
    *,
    strategy_leg: str,
    max_trade_days: int = 10,
) -> int:
    """Copy legacy per-day signal JSON files into the rolling store first."""
    migrated = 0
    for path in sorted(signal_dir.glob(pattern)):
        try:
            signal = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(signal, dict) or not signal.get("signal_date"):
            continue
        save_recent_signal(
            rolling_path,
            signal,
            strategy_leg=strategy_leg,
            max_trade_days=max_trade_days,
        )
        migrated += 1
    return migrated
