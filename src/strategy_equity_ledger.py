"""策略已实现净值账本。

首次启用时用一次券商总资产建立基线，并把当时已有成交标记为已包含；此后净值只按
本系统新增、完整平仓交易的真实成交盈亏和估算费用更新。这样后续入金、出金及系统外
持仓不会被误当成策略收益，也不会错误抬高M回撤闸的峰值。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
from typing import Any, Mapping

import pandas as pd

from src.live_performance import ACTIVE_LEGS, completed_live_trades


LEDGER_SCHEMA_VERSION = 2
_ledger_lock = threading.RLock()


@dataclass(frozen=True)
class StrategyEquitySnapshot:
    equity: float
    peak_equity: float
    realized_pnl: float
    new_trade_count: int
    pending_incomplete_trade_count: int
    initialized_now: bool
    ledger_ready: bool
    source: str


def load_equity_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def equity_ledger_requires_bootstrap(path: Path) -> bool:
    state = load_equity_ledger(path)
    return int(state.get("schema_version", 0) or 0) != LEDGER_SCHEMA_VERSION


def _report_config(config: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(config.get("live_performance_report", {}))
    analysis = config.get("analysis", {})
    for key in ("commission_rate", "stamp_tax_rate", "transfer_fee_rate"):
        result.setdefault(key, analysis.get(key))
    result.setdefault("active_legs", sorted(ACTIVE_LEGS))
    return result


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def update_strategy_equity_ledger(
    *,
    state_path: Path,
    completion_summary_path: Path,
    signal_date: str,
    config: Mapping[str, Any],
    bootstrap_equity: float | None = None,
) -> StrategyEquitySnapshot:
    """迁移或增量更新策略净值；未完成的新交易使账本fail-closed。"""

    try:
        raw = (
            pd.read_csv(completion_summary_path, dtype={"trade_key": str}, low_memory=False)
            if completion_summary_path.exists()
            else pd.DataFrame()
        )
    except pd.errors.EmptyDataError:
        raw = pd.DataFrame()
    report_config = _report_config(config)
    active_legs = {str(value).upper() for value in report_config.get("active_legs", ACTIVE_LEGS)}
    if not raw.empty and "strategy_leg" in raw.columns:
        active_raw = raw[raw["strategy_leg"].fillna("").astype(str).str.upper().isin(active_legs)].copy()
    else:
        active_raw = raw.copy()
    if not active_raw.empty:
        entry_qty = pd.to_numeric(active_raw.get("entry_filled_qty", 0), errors="coerce").fillna(0)
        active_raw = active_raw[entry_qty.gt(0)].copy()
    all_filled_keys = set(active_raw.get("trade_key", pd.Series(dtype=str)).astype(str))

    with _ledger_lock:
        state = load_equity_ledger(state_path)
        initialized = int(state.get("schema_version", 0) or 0) == LEDGER_SCHEMA_VERSION
        if not initialized:
            baseline = float(bootstrap_equity or 0.0)
            if baseline <= 0:
                raise ValueError("策略净值账本首次建立需要有效的券商总资产基线")
            state = {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "equity_source": "bootstrap_once_then_realized_strategy_pnl",
                "baseline_equity": baseline,
                "last_equity": baseline,
                "peak_equity": baseline,
                "realized_pnl": 0.0,
                "processed_trade_keys": sorted(all_filled_keys),
                "bootstrap_included_trade_count": len(all_filled_keys),
                "updated_signal_date": str(signal_date),
                "ledger_ready": True,
            }
            _atomic_write(state_path, state)
            return StrategyEquitySnapshot(
                baseline, baseline, 0.0, 0, 0, True, True,
                "策略净值账本（首次券商基线）",
            )

        processed = {str(value) for value in state.get("processed_trade_keys", [])}
        new_keys = all_filled_keys - processed
        complete, _quality = completed_live_trades(active_raw, report_config) if not active_raw.empty else (pd.DataFrame(), {})
        complete_keys = set(complete.get("trade_key", pd.Series(dtype=str)).astype(str))
        ready_new_keys = new_keys & complete_keys
        pending_keys = new_keys - complete_keys
        new_trades = complete[complete["trade_key"].astype(str).isin(ready_new_keys)].copy()
        new_pnl = float(new_trades["net_pnl"].sum()) if not new_trades.empty else 0.0
        equity = float(state.get("last_equity", 0.0) or 0.0) + new_pnl
        peak = max(float(state.get("peak_equity", 0.0) or 0.0), equity)
        realized = float(state.get("realized_pnl", 0.0) or 0.0) + new_pnl
        processed.update(ready_new_keys)
        ledger_ready = not pending_keys and equity > 0 and peak > 0
        state.update(
            {
                "last_equity": equity,
                "peak_equity": peak,
                "realized_pnl": realized,
                "processed_trade_keys": sorted(processed),
                "pending_incomplete_trade_keys": sorted(pending_keys),
                "pending_incomplete_trade_count": len(pending_keys),
                "last_incremental_trade_count": len(ready_new_keys),
                "last_incremental_net_pnl": new_pnl,
                "updated_signal_date": str(signal_date),
                "ledger_ready": ledger_ready,
            }
        )
        _atomic_write(state_path, state)
        return StrategyEquitySnapshot(
            equity,
            peak,
            realized,
            len(ready_new_keys),
            len(pending_keys),
            False,
            ledger_ready,
            "策略净值账本（真实完整平仓盈亏）",
        )
