"""Rolling JSON store for daily strategy signals."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


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
