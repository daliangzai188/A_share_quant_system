"""发布版本的全策略影子候选与反事实收益账本。

本模块只读取各腿收盘产物、日线和冻结清单，只写 ``reports/oos_shadow``。
它不读取可用资金、不连接 QMT、不生成计划委托，也不参与任何实盘门禁。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from src.strategy_identity import normalize_strategy_frame, normalize_strategy_leg
from src.strategy_m import build_m_candidate, load_m_spec, resolve_exit_offset


LEGS = ("D", "A", "M", "E", "C", "N")
SCHEMA_VERSION = 1
METHODOLOGY_VERSION = "released_shadow_t1_open_fixed_exit_v1"
LEDGER_COLUMNS = [
    "schema_version", "methodology_version", "release_id", "oos_start_date",
    "signal_date", "strategy_leg", "priority_rank", "candidate_status",
    "ts_code", "name", "candidate_reason", "source_status", "source_path",
    "planned_buy_date", "planned_exit_date", "entry_rule", "exit_rule",
    "position_pct", "account_empty_winner", "live_selected", "live_block_reason",
    "raw_entry_price", "entry_price", "actual_exit_date", "raw_exit_price",
    "exit_price", "shares", "buy_fees", "sell_fees", "limit_down_blocked_days",
    "counterfactual_status", "stock_net_return", "account_net_return",
    "observed_at", "outcome_updated_at",
]


def _date(value: object) -> str:
    text = str(value or "").strip()
    return text[:-2] if text.endswith(".0") else text.replace("-", "")


def _float(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if pd.notna(result) else default
    except (TypeError, ValueError):
        return default


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, low_memory=False)
    except (pd.errors.EmptyDataError, UnicodeDecodeError):
        return pd.DataFrame()


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)


def load_release(root: Path) -> dict[str, Any]:
    path = root / "config" / "strategy_release_freeze.json"
    release = _read_json(path, {})
    required = ("release_id", "oos_start_date", "strategy_priority_order")
    missing = [key for key in required if not release.get(key)]
    if missing:
        raise RuntimeError(f"发布冻结清单缺少字段：{','.join(missing)}")
    if str(release.get("status", "")) != "FROZEN":
        raise RuntimeError("发布冻结清单不是FROZEN状态，拒绝混入样本外账本")
    return release


def load_open_dates(root: Path) -> list[str]:
    calendar = _read_csv(root / "data" / "raw" / "trade_calendar.csv")
    if calendar.empty or not {"cal_date", "is_open"}.issubset(calendar.columns):
        return []
    opened = calendar[pd.to_numeric(calendar["is_open"], errors="coerce").eq(1)].copy()
    return sorted({_date(value) for value in opened["cal_date"] if _date(value)})


def offset_trade_date(open_dates: list[str], signal_date: str, offset: int) -> str:
    dates = [date for date in open_dates if date >= signal_date]
    if signal_date not in dates:
        dates = [date for date in open_dates if date > signal_date]
        index = offset - 1
    else:
        index = offset
    return dates[index] if 0 <= index < len(dates) else ""


def _find_one(root: Path, pattern: str) -> Path | None:
    paths = sorted(root.glob(pattern))
    return paths[-1] if paths else None


def _run_for_date(path: Path, signal_date: str) -> dict[str, Any]:
    payload = _read_json(path, {})
    for row in payload.get("runs", []) if isinstance(payload, dict) else []:
        if _date(row.get("signal_date")) == signal_date:
            return dict(row)
    return {}


def _base_row(release: dict[str, Any], signal_date: str, leg: str, now: str) -> dict[str, Any]:
    priority = [normalize_strategy_leg(value) for value in release.get("strategy_priority_order", LEGS)]
    leg = normalize_strategy_leg(leg)
    rank = priority.index(leg) + 1 if leg in priority else len(priority) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "release_id": str(release["release_id"]),
        "oos_start_date": _date(release["oos_start_date"]),
        "signal_date": signal_date,
        "strategy_leg": leg,
        "priority_rank": rank,
        "candidate_status": "NOT_OBSERVED",
        "ts_code": "",
        "name": "",
        "candidate_reason": "尚未找到该腿当日影子产物",
        "source_status": "MISSING",
        "source_path": "",
        "planned_buy_date": "",
        "planned_exit_date": "",
        "entry_rule": "",
        "exit_rule": "",
        "position_pct": 0.825,
        "account_empty_winner": False,
        "live_selected": False,
        "live_block_reason": "",
        "counterfactual_status": "NOT_APPLICABLE",
        "observed_at": now,
        "outcome_updated_at": "",
    }


def _candidate_from_file(
    row: dict[str, Any],
    path: Path | None,
    signal_date: str,
    hold_offset: int,
    open_dates: list[str],
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run = run or {}
    if path is None:
        if run:
            count = int(_float(run.get("candidate_count"), 0))
            row.update({
                "candidate_status": "NO_CANDIDATE" if count == 0 else "NOT_OBSERVED",
                "candidate_reason": str(run.get("reason", run.get("note", "候选为空"))),
                "source_status": str(run.get("status", "UNKNOWN")),
            })
        return row
    frame = _read_csv(path)
    row["source_path"] = str(path)
    row["source_status"] = str(run.get("status", "FILE_READY"))
    if frame.empty:
        row["candidate_status"] = "NO_CANDIDATE"
        row["candidate_reason"] = str(run.get("reason", run.get("note", "候选文件为空")))
        return row
    picked = frame.sort_values("candidate_rank").iloc[0] if "candidate_rank" in frame else frame.iloc[0]
    row.update({
        "candidate_status": "CANDIDATE",
        "ts_code": str(picked.get("ts_code", "")),
        "name": str(picked.get("name", "")),
        "candidate_reason": str(run.get("reason", picked.get("selection_reason", "通过本腿规则"))),
        "planned_buy_date": offset_trade_date(open_dates, signal_date, 1),
        "planned_exit_date": offset_trade_date(open_dates, signal_date, hold_offset),
        "entry_rule": "T+1_OPEN",
        "exit_rule": f"T+{hold_offset}_CLOSE",
        "position_pct": _float(picked.get("planned_position_pct", picked.get("position_pct", 0.825)), 0.825),
        "counterfactual_status": "WAITING_ENTRY_DATA",
    })
    return row


def _collect_ac(root: Path, release: dict[str, Any], signal_date: str, leg: str, now: str,
                open_dates: list[str]) -> dict[str, Any]:
    row = _base_row(release, signal_date, leg, now)
    suffix = "a_candidates" if leg == "A" else "c_candidates"
    path = _find_one(root, f"reports/paper_trade/ab_filtered_daily_ops/*_{signal_date}_{suffix}.csv")
    offset = 2 if leg == "A" else 3
    row = _candidate_from_file(row, path, signal_date, offset, open_dates)
    if leg == "C" and row["candidate_status"] == "CANDIDATE":
        rejected = _find_one(root, f"reports/paper_trade/ab_filtered_daily_ops/*_{signal_date}_c_rejected_by_filter.csv")
        rejected_frame = _read_csv(rejected) if rejected else pd.DataFrame()
        if not rejected_frame.empty and str(row["ts_code"]) in set(rejected_frame["ts_code"].astype(str)):
            detail = str(rejected_frame.iloc[0].get("risk_reject_detail", "命中C自有风险过滤"))
            row.update({
                "candidate_status": "REJECTED",
                "candidate_reason": detail,
                "counterfactual_status": "NOT_APPLICABLE",
            })
    return row


def _collect_standard_leg(root: Path, release: dict[str, Any], signal_date: str, leg: str,
                          now: str, open_dates: list[str]) -> dict[str, Any]:
    row = _base_row(release, signal_date, leg, now)
    low = leg.lower()
    run = _run_for_date(root / "reports" / f"strategy_{low}" / f"{low}_signal_runs_recent.json", signal_date)
    path = root / "reports" / f"strategy_{low}" / f"{low}_signal_{signal_date}_candidates.csv"
    row = _candidate_from_file(row, path if path.exists() else None, signal_date, 2, open_dates, run)
    if row["candidate_status"] == "NOT_OBSERVED" and run.get("ts_code"):
        row.update({
            "candidate_status": "CANDIDATE",
            "ts_code": str(run.get("ts_code", "")),
            "name": str(run.get("name", "")),
            "planned_buy_date": offset_trade_date(open_dates, signal_date, 1),
            "planned_exit_date": offset_trade_date(open_dates, signal_date, 2),
            "entry_rule": "T+1_OPEN",
            "exit_rule": "T+2_CLOSE",
            "counterfactual_status": "WAITING_ENTRY_DATA",
        })
    return row


def _load_m_pool(root: Path, signal_date: str) -> tuple[pd.DataFrame, Path | None]:
    frames: list[pd.DataFrame] = []
    used: Path | None = None
    for path in (
        root / "data" / "processed" / "live_limit_up_fill_scored.csv",
        root / "data" / "processed" / "limit_up_fill_scored.csv",
    ):
        frame = _read_csv(path)
        if frame.empty or "trade_date" not in frame:
            continue
        matched = frame[frame["trade_date"].map(_date).eq(signal_date)]
        if not matched.empty:
            frames.append(matched)
            used = path
    if not frames:
        return pd.DataFrame(), used
    pool = pd.concat(frames, ignore_index=True, sort=False)
    return pool.drop_duplicates(["trade_date", "ts_code"], keep="last"), used


def _collect_m(root: Path, release: dict[str, Any], signal_date: str, now: str,
               open_dates: list[str]) -> dict[str, Any]:
    row = _base_row(release, signal_date, "M", now)
    config = _read_json(root / "config" / "config.json", {})
    spec = load_m_spec(config)
    pool, source = _load_m_pool(root, signal_date)
    row["source_path"] = str(source or "")
    if pool.empty:
        row.update({"candidate_reason": "当日涨停打分池尚未就绪", "source_status": "MISSING_POOL"})
        return row
    picked, reason = build_m_candidate(pool, spec)
    row.update({"source_status": "SHADOW_RULE_EVALUATED", "candidate_reason": reason})
    if picked.empty:
        row["candidate_status"] = "NO_CANDIDATE"
        return row
    selected = picked.iloc[0]
    offset = resolve_exit_offset(spec)
    row.update({
        "candidate_status": "CANDIDATE",
        "ts_code": str(selected.get("ts_code", "")),
        "name": str(selected.get("name", "")),
        "planned_buy_date": offset_trade_date(open_dates, signal_date, 1),
        "planned_exit_date": offset_trade_date(open_dates, signal_date, offset),
        "entry_rule": "T+1_OPEN",
        "exit_rule": f"T+{offset}_CLOSE",
        "position_pct": _float(spec.get("position_pct"), 0.825),
        "counterfactual_status": "WAITING_ENTRY_DATA",
    })
    return row


def _collect_d(root: Path, release: dict[str, Any], signal_date: str, now: str,
               open_dates: list[str]) -> dict[str, Any]:
    row = _base_row(release, signal_date, "D", now)
    path = root / "reports" / "strategy_d" / f"intraday_signals_{signal_date}.csv"
    frame = _read_csv(path)
    if not path.exists():
        row.update({
            "candidate_reason": "当天无D盘中观察文件；可能被持仓或收盘计划阻断，不能据此认定D无候选",
            "source_status": "NOT_MONITORED",
        })
        return row
    row.update({"source_path": str(path), "source_status": "INTRADAY_FILE_READY"})
    buys = frame[frame.get("signal_type", pd.Series(dtype=str)).astype(str).eq("BUY")] if not frame.empty else frame
    if buys.empty:
        row.update({"candidate_status": "NO_CANDIDATE", "candidate_reason": "D完成盘中观察但未出现BUY信号"})
        return row
    picked = buys.iloc[0]
    entry = _float(picked.get("filled_amount")) / _float(picked.get("filled_qty")) if _float(picked.get("filled_qty")) else _float(picked.get("upper_limit"))
    row.update({
        "candidate_status": "CANDIDATE",
        "ts_code": str(picked.get("ts_code", "")),
        "name": str(picked.get("name", "")),
        "candidate_reason": f"D盘中BUY，来源={picked.get('source', '')}",
        "planned_buy_date": signal_date,
        "planned_exit_date": offset_trade_date(open_dates, signal_date, 2),
        "entry_rule": "T日盘中回封涨停价",
        "exit_rule": "T+2_CLOSE",
        "position_pct": 0.825,
        "raw_entry_price": entry,
        "entry_price": entry,
        "live_selected": bool(_float(picked.get("filled_qty")) > 0 or str(picked.get("order_status", "")) == "FILLED"),
        "counterfactual_status": "WAITING_EXIT_DATA",
    })
    return row


def _combined_live_selection(root: Path, rows: list[dict[str, Any]], signal_date: str) -> None:
    del signal_date
    action_dates = sorted({_date(row.get("planned_buy_date")) for row in rows if _date(row.get("planned_buy_date"))})
    for action_date in action_dates:
        path = root / "reports" / "live_trade" / "combined" / f"combined_planned_orders_{action_date}.csv"
        frame = _read_csv(path)
        if frame.empty or "signal_date" not in frame:
            continue
        signal_dates = {_date(row.get("signal_date")) for row in rows}
        matched = frame[frame["signal_date"].map(_date).isin(signal_dates)]
        for planned in matched.itertuples(index=False):
            for row in rows:
                if row["strategy_leg"] == "D":
                    continue
                if str(row["strategy_leg"]) == str(getattr(planned, "strategy_leg", "")) and str(row["ts_code"]) == str(getattr(planned, "ts_code", "")):
                    row["live_selected"] = True
    reasons: list[str] = []
    for action_date in action_dates:
        path = root / "reports" / "live_trade" / "combined" / f"combined_decisions_{action_date}.csv"
        frame = _read_csv(path)
        if frame.empty or "reason" not in frame:
            continue
        text = "；".join(dict.fromkeys(frame["reason"].dropna().astype(str)))
        if text:
            reasons.append(text)
    block_reason = "；".join(dict.fromkeys(reasons))
    for row in rows:
        if row["candidate_status"] == "CANDIDATE" and not row["live_selected"]:
            row["live_block_reason"] = block_reason or "未进入组合实盘买单（可能因持仓、优先级或门禁让路）"


def collect_signal_date(root: Path, release: dict[str, Any], signal_date: str) -> list[dict[str, Any]]:
    signal_date = _date(signal_date)
    if signal_date < _date(release["oos_start_date"]):
        return []
    now = datetime.now(timezone.utc).isoformat()
    open_dates = load_open_dates(root)
    rows = [
        _collect_d(root, release, signal_date, now, open_dates),
        _collect_ac(root, release, signal_date, "A", now, open_dates),
        _collect_m(root, release, signal_date, now, open_dates),
        _collect_standard_leg(root, release, signal_date, "E", now, open_dates),
        _collect_ac(root, release, signal_date, "C", now, open_dates),
        _collect_standard_leg(root, release, signal_date, "N", now, open_dates),
    ]
    eligible = [row for row in rows if row["candidate_status"] == "CANDIDATE"]
    if eligible:
        min(eligible, key=lambda item: int(item["priority_rank"]))["account_empty_winner"] = True
    _combined_live_selection(root, rows, signal_date)
    return rows


def _daily_row(root: Path, trade_date: str, ts_code: str) -> pd.Series | None:
    path = root / "data" / "processed" / "daily_merged_by_date" / f"{trade_date}.csv"
    frame = _read_csv(path)
    if frame.empty or "ts_code" not in frame:
        return None
    matched = frame[frame["ts_code"].astype(str).eq(str(ts_code))]
    return None if matched.empty else matched.iloc[0]


def _price_limit(row: pd.Series, direction: int) -> float:
    pre_close = _float(row.get("pre_close"))
    pct = _float(row.get("limit_pct"), 0.10)
    value = Decimal(str(pre_close * (1 + direction * pct))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(value)


def _entry_unfillable(row: pd.Series) -> bool:
    upper = _price_limit(row, 1)
    return upper > 0 and _float(row.get("open")) >= upper - 0.005 and _float(row.get("low")) >= upper - 0.005


def _exit_unsellable(row: pd.Series) -> bool:
    lower = _price_limit(row, -1)
    return lower > 0 and (_float(row.get("open")) <= lower + 0.005 or _float(row.get("close")) <= lower + 0.005)


def _fee_config(root: Path) -> dict[str, float]:
    config = _read_json(root / "config" / "config.json", {})
    analysis = config.get("analysis", {}) if isinstance(config, dict) else {}
    live = config.get("live_performance_report", {}) if isinstance(config, dict) else {}
    portfolio = config.get("portfolio_certification", {}) if isinstance(config, dict) else {}
    return {
        "commission": _float(analysis.get("commission_rate"), 0.0003),
        "stamp": _float(analysis.get("stamp_tax_rate"), 0.001),
        "transfer": _float(analysis.get("transfer_fee_rate"), 0.00001),
        "slippage": _float(analysis.get("slippage_rate"), 0.001),
        "min_commission": _float(live.get("minimum_commission"), 5.0),
        "initial_equity": _float(portfolio.get("initial_equity"), 500000.0),
    }


def update_counterfactual_outcomes(root: Path, frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.copy()
    for column in ("signal_date", "oos_start_date", "planned_buy_date", "planned_exit_date", "actual_exit_date"):
        if column in result:
            result[column] = result[column].map(_date).astype("object")
    fees = _fee_config(root)
    open_dates = load_open_dates(root)
    now = datetime.now(timezone.utc).isoformat()
    for index, current in result.iterrows():
        if str(current.get("candidate_status")) != "CANDIDATE":
            continue
        leg = str(current.get("strategy_leg", ""))
        entry_date = _date(current.get("planned_buy_date"))
        exit_date = _date(current.get("planned_exit_date"))
        ts_code = str(current.get("ts_code", ""))
        entry_row = _daily_row(root, entry_date, ts_code)
        raw_entry = _float(current.get("raw_entry_price"))
        if leg != "D":
            if entry_row is None:
                result.at[index, "counterfactual_status"] = "WAITING_ENTRY_DATA"
                continue
            if _entry_unfillable(entry_row):
                result.at[index, "counterfactual_status"] = "ENTRY_UNFILLABLE"
                result.at[index, "outcome_updated_at"] = now
                continue
            raw_entry = _float(entry_row.get("open"))
        if raw_entry <= 0:
            result.at[index, "counterfactual_status"] = "WAITING_ENTRY_DATA"
            continue
        exit_row = _daily_row(root, exit_date, ts_code)
        if exit_row is None:
            result.at[index, "counterfactual_status"] = "WAITING_EXIT_DATA"
            continue
        blocked = 0
        actual_exit_date = exit_date
        while exit_row is not None and _exit_unsellable(exit_row):
            blocked += 1
            later = [date for date in open_dates if date > actual_exit_date]
            if not later:
                exit_row = None
                break
            actual_exit_date = later[0]
            exit_row = _daily_row(root, actual_exit_date, ts_code)
            if exit_row is None:
                break
        if exit_row is None:
            result.at[index, "counterfactual_status"] = "WAITING_SELLABLE_EXIT"
            result.at[index, "limit_down_blocked_days"] = blocked
            continue
        raw_exit = _float(exit_row.get("close"))
        entry_price = raw_entry if leg == "D" else raw_entry * (1 + fees["slippage"])
        exit_price = raw_exit * (1 - fees["slippage"])
        target = fees["initial_equity"] * _float(current.get("position_pct"), 0.825)
        shares = int(target / entry_price / 100) * 100 if entry_price > 0 else 0
        if shares <= 0:
            result.at[index, "counterfactual_status"] = "INVALID_POSITION_SIZE"
            continue
        buy_value = shares * entry_price
        sell_value = shares * exit_price
        buy_fees = max(fees["min_commission"], buy_value * fees["commission"]) + buy_value * fees["transfer"]
        sell_fees = max(fees["min_commission"], sell_value * fees["commission"]) + sell_value * (fees["transfer"] + fees["stamp"])
        net_pnl = sell_value - sell_fees - buy_value - buy_fees
        result.at[index, "raw_entry_price"] = raw_entry
        result.at[index, "entry_price"] = entry_price
        result.at[index, "actual_exit_date"] = actual_exit_date
        result.at[index, "raw_exit_price"] = raw_exit
        result.at[index, "exit_price"] = exit_price
        result.at[index, "shares"] = shares
        result.at[index, "buy_fees"] = buy_fees
        result.at[index, "sell_fees"] = sell_fees
        result.at[index, "limit_down_blocked_days"] = blocked
        result.at[index, "counterfactual_status"] = "RESOLVED"
        result.at[index, "stock_net_return"] = net_pnl / (buy_value + buy_fees)
        result.at[index, "account_net_return"] = net_pnl / fees["initial_equity"]
        result.at[index, "outcome_updated_at"] = now
    return result


def upsert_ledger(root: Path, new_rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    path = root / "reports" / "oos_shadow" / "shadow_candidates.csv"
    old = _read_csv(path)
    incoming = pd.DataFrame(list(new_rows))
    if incoming.empty and old.empty:
        result = pd.DataFrame(columns=LEDGER_COLUMNS)
    elif incoming.empty:
        result = old.copy()
    else:
        key = ["release_id", "signal_date", "strategy_leg"]
        if not old.empty:
            old["release_id"] = old["release_id"].fillna("").astype(str)
            old["signal_date"] = old["signal_date"].map(_date)
            old = normalize_strategy_frame(old)
            incoming["release_id"] = incoming["release_id"].fillna("").astype(str)
            incoming["signal_date"] = incoming["signal_date"].map(_date)
            incoming = normalize_strategy_frame(incoming)
            # 已形成明确候选结论的首次观察不可被后续文件修订覆盖，避免用未来
            # 结果回写当日候选；只有MISSING/NOT_OBSERVED可在同日数据补齐后更新。
            final_statuses = {"CANDIDATE", "NO_CANDIDATE", "REJECTED"}
            frozen = old[old["candidate_status"].astype(str).isin(final_statuses)]
            frozen_keys = set(map(tuple, frozen[key].astype(str).itertuples(index=False, name=None)))
            incoming_keys = incoming[key].astype(str).apply(tuple, axis=1)
            incoming = incoming[~incoming_keys.isin(frozen_keys)]
            combined = pd.concat([old, incoming], ignore_index=True, sort=False)
        else:
            combined = incoming
        combined["release_id"] = combined["release_id"].fillna("").astype(str)
        combined["signal_date"] = combined["signal_date"].map(_date)
        combined = normalize_strategy_frame(combined)
        result = combined.drop_duplicates(key, keep="last")
    for column in LEDGER_COLUMNS:
        if column not in result:
            result[column] = ""
    result = update_counterfactual_outcomes(root, result[LEDGER_COLUMNS])
    result = result.sort_values(["signal_date", "priority_rank"], kind="stable").reset_index(drop=True)
    _atomic_csv(result, path)
    return result
