"""策略已实现净值账本。

首次启用时用一次券商总资产建立基线（此时账户必须空仓），并记下基线日期；此后净值
= 基线 + 基线日及以后买入、已完整平仓交易的真实成交盈亏（扣估算费用）。入金、出金和
系统外持仓不会被当成策略收益，也不会抬高账户级回撤风控的峰值。

每次更新都从成交汇总整体重算，不再按“已处理交易编号”增量累加：
- 2026-08 E2腿并入E后交易编号改变，旧增量账本把基线前的7笔E交易又加了一遍（约+4.85万）；
- 按当前在用腿过滤，已停用腿（如N）的真实亏损被静默丢掉；
- 已入账交易的卖出价事后更正（人工核验券商成交）永远进不了账本。
整体重算让交易编号、腿名和更正都不再影响结果；基线日之后任何一笔交易缺成交，
账本即为未就绪（fail-closed），并列出缺哪几笔。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import threading
from typing import Any, Mapping

import pandas as pd

from src.live_performance import ACTIVE_LEGS, completed_live_trades
from src.strategy_identity import normalize_strategy_leg


LEDGER_SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSIONS = {2}
_ledger_lock = threading.RLock()
_DATE8 = re.compile(r"^\d{8}$")


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
    pending_trade_keys: tuple[str, ...] = field(default_factory=tuple)
    peak_date: str = ""


def load_equity_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def equity_ledger_requires_bootstrap(path: Path) -> bool:
    """只有没有可用账本时才需要券商总资产建基线；旧版账本走迁移，不重新建基线。"""
    state = load_equity_ledger(path)
    version = int(state.get("schema_version", 0) or 0)
    return version != LEDGER_SCHEMA_VERSION and version not in LEGACY_SCHEMA_VERSIONS


def _report_config(config: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(config.get("live_performance_report", {}))
    analysis = config.get("analysis", {})
    for key in (
        "commission_rate", "stamp_tax_rate", "stamp_tax_schedule",
        "transfer_fee_rate", "minimum_commission",
    ):
        result.setdefault(key, analysis.get(key))
    result.setdefault("active_legs", sorted(ACTIVE_LEGS))
    return result


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _date8(value: Any) -> str:
    text = str(value or "").replace("-", "").strip()[:8]
    return text if _DATE8.match(text) else ""


def _load_filled_trades(path: Path) -> pd.DataFrame:
    """读取成交汇总中真实买入过的交易（不分腿；停用腿的真实盈亏同样属于账户）。"""
    try:
        raw = (
            pd.read_csv(path, dtype={"trade_key": str}, low_memory=False)
            if path.exists()
            else pd.DataFrame()
        )
    except pd.errors.EmptyDataError:
        raw = pd.DataFrame()
    if raw.empty:
        return raw
    entry_qty = pd.to_numeric(raw.get("entry_filled_qty", 0), errors="coerce").fillna(0)
    filled = raw[entry_qty.gt(0)].copy()
    entry_dates = filled.get("entry_date", pd.Series("", index=filled.index)).map(_date8)
    from_key = filled["trade_key"].astype(str).str[:8].map(_date8)
    filled["_entry_date"] = entry_dates.where(entry_dates.ne(""), from_key)
    return filled


def _migrate_legacy_state(state: dict[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """v2增量账本→v3整体重算：保留券商基线，基线日期必须由配置显式给出。"""
    section = config.get("strategy_equity_ledger", {}) or {}
    cutoff = _date8(section.get("legacy_v2_baseline_cutoff_entry_date"))
    baseline = float(state.get("baseline_equity", 0.0) or 0.0)
    if not cutoff or baseline <= 0:
        raise ValueError(
            "旧版策略净值账本迁移需要有效的券商基线和配置"
            " strategy_equity_ledger.legacy_v2_baseline_cutoff_entry_date"
        )
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "equity_source": "baseline_plus_recomputed_realized_strategy_pnl",
        "baseline_equity": baseline,
        "baseline_cutoff_entry_date": cutoff,
        "migrated_from": {
            "schema_version": int(state.get("schema_version", 0) or 0),
            "last_equity": state.get("last_equity"),
            "peak_equity": state.get("peak_equity"),
            "realized_pnl": state.get("realized_pnl"),
            "processed_trade_key_count": len(state.get("processed_trade_keys", []) or []),
            "pending_incomplete_trade_keys": state.get("pending_incomplete_trade_keys", []),
            "cutoff_source": "config.strategy_equity_ledger.legacy_v2_baseline_cutoff_entry_date",
        },
    }


def _equity_path(baseline: float, complete: pd.DataFrame) -> tuple[float, float, str]:
    """按卖出日先后累计净值，返回（当前净值，历史峰值，峰值日期）。"""
    if complete.empty:
        return baseline, baseline, ""
    daily = (
        complete.assign(_exit=complete["exit_date"].map(_date8))
        .groupby("_exit")["net_pnl"].sum()
        .sort_index()
    )
    path = baseline + daily.cumsum()
    peak_value = max(baseline, float(path.max()))
    peak_date = str(path.idxmax()) if float(path.max()) >= baseline else ""
    return float(path.iloc[-1]), peak_value, peak_date


def update_strategy_equity_ledger(
    *,
    state_path: Path,
    completion_summary_path: Path,
    signal_date: str,
    config: Mapping[str, Any],
    bootstrap_equity: float | None = None,
) -> StrategyEquitySnapshot:
    """建立、迁移或整体重算策略净值；基线日后任何交易缺成交都使账本fail-closed。"""

    filled = _load_filled_trades(completion_summary_path)
    report_config = _report_config(config)

    with _ledger_lock:
        state = load_equity_ledger(state_path)
        version = int(state.get("schema_version", 0) or 0)
        initialized_now = False
        if version in LEGACY_SCHEMA_VERSIONS:
            previous_included: set[str] = {str(v) for v in state.get("processed_trade_keys", [])}
            state = _migrate_legacy_state(state, config)
        elif version != LEDGER_SCHEMA_VERSION:
            baseline = float(bootstrap_equity or 0.0)
            cutoff = _date8(signal_date)
            if baseline <= 0 or not cutoff:
                raise ValueError("策略净值账本首次建立需要有效的券商总资产基线和日期")
            # 建基线时账户空仓：之前买入的交易都已平仓并含在总资产里，基线日及以后买入的才计入。
            state = {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "equity_source": "baseline_plus_recomputed_realized_strategy_pnl",
                "baseline_equity": baseline,
                "baseline_cutoff_entry_date": cutoff,
            }
            previous_included = set()
            initialized_now = True
        else:
            previous_included = {str(v) for v in state.get("included_trade_keys", [])}

        baseline = float(state["baseline_equity"])
        cutoff = str(state["baseline_cutoff_entry_date"])
        scoped = filled[filled["_entry_date"].ge(cutoff)].copy() if not filled.empty else filled
        if scoped.empty:
            complete = pd.DataFrame(columns=["trade_key", "exit_date", "net_pnl"])
        else:
            legs = {normalize_strategy_leg(v) for v in scoped["strategy_leg"].fillna("").astype(str)}
            all_legs_config = {**report_config, "active_legs": sorted(leg for leg in legs if leg)}
            complete, _quality = completed_live_trades(scoped, all_legs_config)
        scoped_keys = set(scoped.get("trade_key", pd.Series(dtype=str)).astype(str))
        complete_keys = set(complete.get("trade_key", pd.Series(dtype=str)).astype(str))
        pending_keys = sorted(scoped_keys - complete_keys)
        equity, peak, peak_date = _equity_path(baseline, complete)
        realized = equity - baseline
        ledger_ready = not pending_keys and equity > 0 and peak > 0
        new_keys = complete_keys - previous_included
        state.update(
            {
                "last_equity": equity,
                "peak_equity": peak,
                "peak_date": peak_date,
                "realized_pnl": realized,
                "included_trade_keys": sorted(complete_keys),
                "included_trade_count": len(complete_keys),
                "pending_incomplete_trade_keys": pending_keys,
                "pending_incomplete_trade_count": len(pending_keys),
                "last_new_trade_count": len(new_keys),
                "updated_signal_date": str(signal_date),
                "ledger_ready": ledger_ready,
            }
        )
        _atomic_write(state_path, state)
        source = (
            "策略净值账本（首次券商基线）"
            if initialized_now
            else "策略净值账本（基线+真实完整平仓盈亏，整体重算）"
        )
        return StrategyEquitySnapshot(
            equity,
            peak,
            realized,
            len(new_keys),
            len(pending_keys),
            initialized_now,
            ledger_ready,
            source,
            tuple(pending_keys),
            peak_date,
        )
