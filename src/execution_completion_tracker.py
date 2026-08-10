"""实盘成交完成率持久化与统一汇总。

本模块只做执行审计，不参与选股、仓位计算和下单决策。数据分成三层：

1. ``trade_execution_plan.csv``：开仓前冻结的原始目标；
2. ``buy_execution_slices.csv`` / ``sell_execution_slices.csv``：逐委托、逐片事件；
3. ``trade_completion_summary.csv``：一笔交易一行的最终汇总。

最终成交数量以 ``positions.json`` 及其中的 ``exit_fills_by_date`` 为权威口径，
逐片事件只负责拆分竞价/POV/尾盘渠道和保存流量、盘口、滑点等执行上下文，
因此重复写入同一委托不会把完成率重复累计。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any, Iterable


PLAN_FIELDS = [
    "trade_key", "entry_date", "ts_code", "name", "strategy_leg", "signal_date",
    "entry_target_qty", "entry_target_amount", "entry_reference_price",
    "auction_planned_qty", "pov_planned_qty", "pov_target_amount",
    "planned_exit_date", "entry_status", "created_at", "updated_at",
]

BUY_FIELDS = [
    "event_id", "trade_key", "entry_date", "time", "ts_code", "name",
    "strategy_leg", "signal_date", "channel", "slice_no", "order_id",
    "budget", "order_price", "order_qty", "filled_qty", "fill_price",
    "fill_amount", "benchmark_open", "remaining_amount", "status", "note",
    "recorded_at",
]

SELL_FIELDS = [
    "event_id", "trade_key", "entry_date", "exit_date", "time", "ts_code",
    "name", "strategy_leg", "signal_date", "channel", "slice_no",
    "local_order_id", "broker_order_id", "external_flow", "budget",
    "depth_limit_qty", "depth_note", "order_price", "order_qty", "filled_qty",
    "fill_price", "fill_amount", "benchmark_close", "remaining_qty", "status",
    "note", "recorded_at",
]

SUMMARY_FIELDS = [
    "trade_key", "entry_date", "ts_code", "name", "strategy_leg", "signal_date",
    "planned_exit_date", "entry_target_qty", "entry_target_amount",
    "auction_planned_qty", "pov_planned_qty", "pov_target_amount",
    "auction_filled_qty", "auction_fill_amount", "auction_completion_pct",
    "pov_filled_qty", "pov_fill_amount", "pov_completion_pct",
    "other_buy_filled_qty", "other_buy_fill_amount",
    "entry_filled_qty", "entry_fill_amount",
    "entry_unfilled_qty", "entry_unfilled_amount", "entry_qty_completion_pct",
    "entry_amount_completion_pct", "entry_vwap",
    "benchmark_open", "buy_slippage_bps", "exit_date", "exit_target_qty",
    "exit_pov_filled_qty", "exit_pov_fill_amount", "exit_other_filled_qty",
    "exit_other_fill_amount", "exit_filled_qty", "exit_fill_amount",
    "exit_completion_pct", "exit_vwap", "benchmark_close", "sell_slippage_bps",
    "total_slippage_bps", "exit_remaining_qty", "overnight_residual_qty",
    "execution_status", "data_quality_note", "updated_at",
]


_tracker_lock = threading.RLock()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _date(value: Any) -> str:
    return _text(value).replace("-", "")[:8]


def _code(value: Any) -> str:
    text = _text(value).upper()
    if not text:
        return ""
    if "." in text:
        return text
    if text.startswith(("6", "9")):
        return f"{text}.SH"
    if text.startswith(("0", "2", "3")):
        return f"{text}.SZ"
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return text


def _float(value: Any) -> float:
    try:
        number = float(value)
        return number if number == number else 0.0
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return max(int(float(value)), 0)
    except (TypeError, ValueError):
        return 0


def _now_text() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def make_trade_key(entry_date: Any, ts_code: Any, strategy_leg: Any, signal_date: Any) -> str:
    """生成跨竞价仓/POV子仓稳定一致的交易主键。"""
    return "|".join(
        [_date(entry_date), _code(ts_code), _text(strategy_leg).upper(), _date(signal_date)]
    )


def _valid_trade_key(value: Any) -> bool:
    parts = _text(value).split("|")
    return len(parts) == 4 and bool(parts[0] and parts[1] and parts[2])


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error):
        return []


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    tmp.replace(path)


def _upsert(path: Path, fields: list[str], key_field: str, row: dict[str, Any]) -> None:
    key = _text(row.get(key_field))
    if not key:
        raise ValueError(f"{key_field}不能为空")
    rows = _read_csv(path)
    found = False
    for index, old in enumerate(rows):
        if _text(old.get(key_field)) != key:
            continue
        merged = dict(old)
        for field, value in row.items():
            if value not in {None, ""}:
                merged[field] = value
        rows[index] = merged
        found = True
        break
    if not found:
        rows.append(dict(row))
    _write_csv(path, fields, rows)


def _dedupe_sell_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一券商委托只保留一条最完整事件，避免历史回填与实时事件重复计数。"""
    selected: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, event in enumerate(events):
        broker_order_id = _text(event.get("broker_order_id"))
        event_id = _text(event.get("event_id"))
        key = f"broker:{broker_order_id}" if broker_order_id else f"event:{event_id or index}"
        if key not in selected:
            order.append(key)
        old = selected.get(key)

        def quality(row: dict[str, Any]) -> tuple[int, int, int, int, str]:
            is_live_event = not _text(row.get("event_id")).startswith("历史退出|")
            has_price = _float(row.get("fill_price")) > 0
            has_amount = _float(row.get("fill_amount")) > 0
            has_execution_context = bool(
                _float(row.get("external_flow"))
                or _int(row.get("depth_limit_qty"))
                or _text(row.get("depth_note"))
            )
            return (
                int(is_live_event),
                int(has_price),
                int(has_amount),
                int(has_execution_context),
                _text(row.get("recorded_at")),
            )

        if old is None or quality(event) > quality(old):
            selected[key] = event
    return [selected[key] for key in order]


class ExecutionCompletionTracker:
    """线程安全、失败不影响交易主流程的成交完成率记录器。"""

    def __init__(self, project_root: Path | str):
        self.root = Path(project_root).absolute()
        self.report_dir = self.root / "reports" / "execution_tracking"
        self.plan_path = self.report_dir / "trade_execution_plan.csv"
        self.buy_path = self.report_dir / "buy_execution_slices.csv"
        self.sell_path = self.report_dir / "sell_execution_slices.csv"
        self.summary_path = self.report_dir / "trade_completion_summary.csv"
        self.positions_path = self.root / "data" / "processed" / "positions.json"
        self.audit_path = self.root / "reports" / "live_execution_audit.csv"

    def register_entry_plan(
        self,
        *,
        entry_date: Any,
        ts_code: Any,
        name: Any,
        strategy_leg: Any,
        signal_date: Any,
        target_qty: Any,
        target_amount: Any,
        reference_price: Any,
        auction_planned_qty: Any = 0,
        pov_planned_qty: Any = 0,
        pov_target_amount: Any = 0,
        planned_exit_date: Any = "",
        status: str = "计划中",
    ) -> str:
        key = make_trade_key(entry_date, ts_code, strategy_leg, signal_date)
        now = _now_text()
        row = {
            "trade_key": key,
            "entry_date": _date(entry_date),
            "ts_code": _code(ts_code),
            "name": _text(name),
            "strategy_leg": _text(strategy_leg).upper(),
            "signal_date": _date(signal_date),
            "entry_target_qty": _int(target_qty),
            "entry_target_amount": round(_float(target_amount), 4),
            "entry_reference_price": round(_float(reference_price), 6),
            "auction_planned_qty": _int(auction_planned_qty),
            "pov_planned_qty": _int(pov_planned_qty),
            "pov_target_amount": round(_float(pov_target_amount), 4),
            "planned_exit_date": _date(planned_exit_date),
            "entry_status": _text(status) or "计划中",
            "created_at": now,
            "updated_at": now,
        }
        with _tracker_lock:
            _upsert(self.plan_path, PLAN_FIELDS, "trade_key", row)
            self.rebuild_summary()
        return key

    def update_entry_status(
        self, *, entry_date: Any, ts_code: Any, strategy_leg: Any, signal_date: Any, status: str
    ) -> None:
        key = make_trade_key(entry_date, ts_code, strategy_leg, signal_date)
        with _tracker_lock:
            _upsert(
                self.plan_path,
                PLAN_FIELDS,
                "trade_key",
                {"trade_key": key, "entry_status": _text(status), "updated_at": _now_text()},
            )
            self.rebuild_summary()

    def record_buy_slice(self, **values: Any) -> str:
        entry_date = _date(values.get("entry_date"))
        key = make_trade_key(
            entry_date,
            values.get("ts_code"),
            values.get("strategy_leg"),
            values.get("signal_date"),
        )
        if not _valid_trade_key(key):
            raise ValueError("买入执行事件缺少交易日期、股票代码或策略腿")
        order_id = _text(values.get("order_id"))
        channel = _text(values.get("channel")) or "其他买入"
        slice_no = _int(values.get("slice_no"))
        event_id = _text(values.get("event_id")) or (
            f"买入|{key}|{channel}|{order_id or slice_no}|{slice_no}"
        )
        filled_qty = _int(values.get("filled_qty"))
        fill_price = _float(values.get("fill_price"))
        row = {
            "event_id": event_id,
            "trade_key": key,
            "entry_date": entry_date,
            "time": _text(values.get("time")),
            "ts_code": _code(values.get("ts_code")),
            "name": _text(values.get("name")),
            "strategy_leg": _text(values.get("strategy_leg")).upper(),
            "signal_date": _date(values.get("signal_date")),
            "channel": channel,
            "slice_no": slice_no,
            "order_id": order_id,
            "budget": round(_float(values.get("budget")), 4),
            "order_price": round(_float(values.get("order_price")), 6),
            "order_qty": _int(values.get("order_qty")),
            "filled_qty": filled_qty,
            "fill_price": round(fill_price, 6),
            "fill_amount": round(
                _float(values.get("fill_amount")) or filled_qty * fill_price, 4
            ),
            "benchmark_open": round(_float(values.get("benchmark_open")), 6),
            "remaining_amount": round(_float(values.get("remaining_amount")), 4),
            "status": _text(values.get("status")),
            "note": _text(values.get("note")),
            "recorded_at": _text(values.get("recorded_at")) or _now_text(),
        }
        with _tracker_lock:
            _upsert(self.buy_path, BUY_FIELDS, "event_id", row)
            self.rebuild_summary()
        return event_id

    def record_sell_slice(self, **values: Any) -> str:
        entry_date = _date(values.get("entry_date"))
        key = make_trade_key(
            entry_date,
            values.get("ts_code"),
            values.get("strategy_leg"),
            values.get("signal_date"),
        )
        if not _valid_trade_key(key):
            raise ValueError("卖出执行事件缺少交易日期、股票代码或策略腿")
        broker_order_id = _text(values.get("broker_order_id"))
        local_order_id = _text(values.get("local_order_id"))
        channel = _text(values.get("channel")) or "其他平仓"
        slice_no = _int(values.get("slice_no"))
        event_id = _text(values.get("event_id")) or (
            f"卖出|{key}|{channel}|{broker_order_id or local_order_id or slice_no}|{slice_no}"
        )
        filled_qty = _int(values.get("filled_qty"))
        fill_price = _float(values.get("fill_price"))
        row = {
            "event_id": event_id,
            "trade_key": key,
            "entry_date": entry_date,
            "exit_date": _date(values.get("exit_date")),
            "time": _text(values.get("time")),
            "ts_code": _code(values.get("ts_code")),
            "name": _text(values.get("name")),
            "strategy_leg": _text(values.get("strategy_leg")).upper(),
            "signal_date": _date(values.get("signal_date")),
            "channel": channel,
            "slice_no": slice_no,
            "local_order_id": local_order_id,
            "broker_order_id": broker_order_id,
            "external_flow": round(_float(values.get("external_flow")), 4),
            "budget": round(_float(values.get("budget")), 4),
            "depth_limit_qty": _int(values.get("depth_limit_qty")),
            "depth_note": _text(values.get("depth_note")),
            "order_price": round(_float(values.get("order_price")), 6),
            "order_qty": _int(values.get("order_qty")),
            "filled_qty": filled_qty,
            "fill_price": round(fill_price, 6),
            "fill_amount": round(
                _float(values.get("fill_amount")) or filled_qty * fill_price, 4
            ),
            "benchmark_close": round(_float(values.get("benchmark_close")), 6),
            "remaining_qty": _int(values.get("remaining_qty")),
            "status": _text(values.get("status")),
            "note": _text(values.get("note")),
            "recorded_at": _text(values.get("recorded_at")) or _now_text(),
        }
        with _tracker_lock:
            _upsert(self.sell_path, SELL_FIELDS, "event_id", row)
            self.rebuild_summary()
        return event_id

    def _load_positions(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.positions_path.read_text(encoding="utf-8"))
            return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def rebuild_summary(self) -> Path:
        """以持仓成交账为权威口径重建统一汇总，函数可重复执行。"""
        with _tracker_lock:
            plans = {row.get("trade_key", ""): row for row in _read_csv(self.plan_path)}
            buy_events = _read_csv(self.buy_path)
            sell_events = _read_csv(self.sell_path)
            positions = self._load_positions()

            position_groups: dict[str, list[dict[str, Any]]] = {}
            order_to_position: dict[str, dict[str, Any]] = {}
            for position in positions:
                key = make_trade_key(
                    position.get("buy_date"),
                    position.get("ts_code"),
                    position.get("strategy_leg"),
                    position.get("signal_date"),
                )
                if not key.strip("|"):
                    continue
                position_groups.setdefault(key, []).append(position)
                order_id = _text(position.get("order_id"))
                if order_id:
                    order_to_position[order_id] = position

            buy_by_key: dict[str, list[dict[str, Any]]] = {}
            for event in buy_events:
                buy_by_key.setdefault(_text(event.get("trade_key")), []).append(event)
            sell_by_key: dict[str, list[dict[str, Any]]] = {}
            for event in sell_events:
                sell_by_key.setdefault(_text(event.get("trade_key")), []).append(event)

            audit_by_order: dict[str, dict[str, str]] = {
                _text(row.get("order_id")): row for row in _read_csv(self.audit_path)
                if _text(row.get("order_id"))
            }
            keys = sorted(set(plans) | set(position_groups) | set(buy_by_key) | set(sell_by_key))
            summary: list[dict[str, Any]] = []
            today = dt.date.today().strftime("%Y%m%d")

            for key in keys:
                if not key:
                    continue
                plan = plans.get(key, {})
                group = position_groups.get(key, [])
                buy_group = buy_by_key.get(key, [])
                sell_group = _dedupe_sell_events(sell_by_key.get(key, []))
                seed = group[0] if group else (buy_group[0] if buy_group else (sell_group[0] if sell_group else {}))

                entry_qty = sum(_int(pos.get("entry_shares", pos.get("shares", 0))) for pos in group)
                entry_amount = sum(
                    _int(pos.get("entry_shares", pos.get("shares", 0))) * _float(pos.get("buy_price"))
                    for pos in group
                )
                target_qty = _int(plan.get("entry_target_qty")) or entry_qty
                target_amount = _float(plan.get("entry_target_amount")) or entry_amount

                event_channel_by_order = {
                    _text(event.get("order_id")): _text(event.get("channel"))
                    for event in buy_group if _text(event.get("order_id"))
                }
                pov_positions = [
                    pos for pos in group
                    if event_channel_by_order.get(_text(pos.get("order_id"))) == "买入POV"
                    or _text(pos.get("order_id")).lower().startswith("pov-")
                ]
                pov_order_ids = {_text(pos.get("order_id")) for pos in pov_positions}
                auction_positions = [
                    pos for pos in group
                    if _text(pos.get("order_id")) not in pov_order_ids
                    and (
                        event_channel_by_order.get(_text(pos.get("order_id"))) == "集合竞价买入"
                        or (
                            not event_channel_by_order.get(_text(pos.get("order_id")))
                            and _int(plan.get("auction_planned_qty")) > 0
                        )
                    )
                ]
                pov_qty = sum(_int(pos.get("entry_shares", pos.get("shares", 0))) for pos in pov_positions)
                pov_amount = sum(
                    _int(pos.get("entry_shares", pos.get("shares", 0))) * _float(pos.get("buy_price"))
                    for pos in pov_positions
                )
                auction_qty = sum(
                    _int(pos.get("entry_shares", pos.get("shares", 0))) for pos in auction_positions
                )
                auction_amount = sum(
                    _int(pos.get("entry_shares", pos.get("shares", 0))) * _float(pos.get("buy_price"))
                    for pos in auction_positions
                )
                other_buy_qty = max(entry_qty - pov_qty - auction_qty, 0)
                other_buy_amount = max(entry_amount - pov_amount - auction_amount, 0.0)

                exit_qty = 0
                exit_amount = 0.0
                exit_dates: list[str] = []
                for pos in group:
                    ledger = pos.get("exit_fills_by_date")
                    pos_exit_qty = 0
                    pos_exit_amount = 0.0
                    if isinstance(ledger, dict):
                        for day_key, day in ledger.items():
                            if not isinstance(day, dict):
                                continue
                            qty_one = _int(day.get("qty"))
                            pos_exit_qty += qty_one
                            pos_exit_amount += _float(day.get("amount"))
                            if qty_one > 0:
                                exit_dates.append(_date(day_key))
                    if pos_exit_qty == 0 and _text(pos.get("status")).lower() == "closed":
                        pos_exit_qty = _int(pos.get("entry_shares", pos.get("shares", 0)))
                        pos_exit_amount = pos_exit_qty * _float(pos.get("sell_price"))
                        if pos_exit_qty > 0:
                            exit_dates.append(_date(pos.get("sell_date")))
                    exit_qty += pos_exit_qty
                    exit_amount += pos_exit_amount

                exit_target_qty = entry_qty
                exit_remaining = max(exit_target_qty - exit_qty, 0)
                pov_exit_qty_raw = sum(
                    _int(event.get("filled_qty"))
                    for event in sell_group if _text(event.get("channel")) == "卖出POV"
                )
                pov_exit_amount_raw = sum(
                    _float(event.get("fill_amount"))
                    for event in sell_group if _text(event.get("channel")) == "卖出POV"
                )
                exit_pov_qty = min(pov_exit_qty_raw, exit_qty)
                if pov_exit_qty_raw > 0 and exit_pov_qty < pov_exit_qty_raw:
                    exit_pov_amount = pov_exit_amount_raw * exit_pov_qty / pov_exit_qty_raw
                else:
                    exit_pov_amount = pov_exit_amount_raw

                benchmark_open_num = benchmark_open_den = 0.0
                benchmark_close_num = benchmark_close_den = 0.0
                for pos in group:
                    order_id = _text(pos.get("order_id"))
                    audit = audit_by_order.get(order_id, {})
                    weight = _int(pos.get("entry_shares", pos.get("shares", 0)))
                    open_price = _float(audit.get("bench_open"))
                    close_price = _float(audit.get("bench_close"))
                    if open_price > 0 and weight > 0:
                        benchmark_open_num += open_price * weight
                        benchmark_open_den += weight
                    if close_price > 0 and weight > 0:
                        benchmark_close_num += close_price * weight
                        benchmark_close_den += weight
                if benchmark_open_den == 0:
                    for event in buy_group:
                        price = _float(event.get("benchmark_open"))
                        weight = _int(event.get("filled_qty"))
                        if price > 0 and weight > 0:
                            benchmark_open_num += price * weight
                            benchmark_open_den += weight
                if benchmark_close_den == 0:
                    for event in sell_group:
                        price = _float(event.get("benchmark_close"))
                        weight = _int(event.get("filled_qty"))
                        if price > 0 and weight > 0:
                            benchmark_close_num += price * weight
                            benchmark_close_den += weight

                entry_vwap = entry_amount / entry_qty if entry_qty else 0.0
                exit_vwap = exit_amount / exit_qty if exit_qty else 0.0
                benchmark_open = benchmark_open_num / benchmark_open_den if benchmark_open_den else 0.0
                benchmark_close = benchmark_close_num / benchmark_close_den if benchmark_close_den else 0.0
                buy_slippage = (
                    (entry_vwap / benchmark_open - 1.0) * 10000
                    if entry_vwap > 0 and benchmark_open > 0 else 0.0
                )
                sell_slippage = (
                    (benchmark_close / exit_vwap - 1.0) * 10000
                    if exit_vwap > 0 and benchmark_close > 0 else 0.0
                )
                planned_exit_date = _date(plan.get("planned_exit_date")) or max(
                    [_date(pos.get("planned_exit_date")) for pos in group] or [""]
                )
                if entry_qty <= 0:
                    status = _text(plan.get("entry_status")) or "计划中"
                elif exit_target_qty > 0 and exit_remaining == 0:
                    status = "已平仓"
                elif exit_qty > 0:
                    status = "平仓中"
                else:
                    status = "持仓中"
                overnight = exit_remaining if planned_exit_date and planned_exit_date <= today else 0

                notes: list[str] = []
                if not plan:
                    notes.append("缺少原始计划，目标股数暂以真实买入股数回填")
                elif _text(plan.get("entry_status")) == "已回填":
                    notes.append("上线前原始计划未统一留档，目标股数由历史持仓/容量档案回填")
                if not group and (buy_group or plan):
                    notes.append("尚无持仓成交账")
                if benchmark_open <= 0:
                    notes.append("缺少开盘基准，暂不计算买入滑点")
                if exit_qty > 0 and benchmark_close <= 0:
                    notes.append("缺少收盘基准，暂不计算卖出滑点")

                summary.append({
                    "trade_key": key,
                    "entry_date": _date(plan.get("entry_date")) or _date(seed.get("buy_date", seed.get("entry_date"))),
                    "ts_code": _code(plan.get("ts_code")) or _code(seed.get("ts_code")),
                    "name": _text(plan.get("name")) or _text(seed.get("name")),
                    "strategy_leg": _text(plan.get("strategy_leg")).upper() or _text(seed.get("strategy_leg")).upper(),
                    "signal_date": _date(plan.get("signal_date")) or _date(seed.get("signal_date")),
                    "planned_exit_date": planned_exit_date,
                    "entry_target_qty": target_qty,
                    "entry_target_amount": round(target_amount, 2),
                    "auction_planned_qty": _int(plan.get("auction_planned_qty")),
                    "pov_planned_qty": _int(plan.get("pov_planned_qty")),
                    "pov_target_amount": round(_float(plan.get("pov_target_amount")), 2),
                    "auction_filled_qty": auction_qty,
                    "auction_fill_amount": round(auction_amount, 2),
                    "auction_completion_pct": round(
                        auction_qty / _int(plan.get("auction_planned_qty")) * 100, 4
                    ) if _int(plan.get("auction_planned_qty")) else 0.0,
                    "pov_filled_qty": pov_qty,
                    "pov_fill_amount": round(pov_amount, 2),
                    "pov_completion_pct": round(
                        pov_qty / _int(plan.get("pov_planned_qty")) * 100, 4
                    ) if _int(plan.get("pov_planned_qty")) else 0.0,
                    "other_buy_filled_qty": other_buy_qty,
                    "other_buy_fill_amount": round(other_buy_amount, 2),
                    "entry_filled_qty": entry_qty,
                    "entry_fill_amount": round(entry_amount, 2),
                    "entry_unfilled_qty": max(target_qty - entry_qty, 0),
                    "entry_unfilled_amount": round(max(target_amount - entry_amount, 0.0), 2),
                    "entry_qty_completion_pct": round(entry_qty / target_qty * 100, 4) if target_qty else 0.0,
                    "entry_amount_completion_pct": round(entry_amount / target_amount * 100, 4) if target_amount else 0.0,
                    "entry_vwap": round(entry_vwap, 6),
                    "benchmark_open": round(benchmark_open, 6),
                    "buy_slippage_bps": round(buy_slippage, 4),
                    "exit_date": max(exit_dates or [_date(group[0].get("sell_date")) if group else ""]),
                    "exit_target_qty": exit_target_qty,
                    "exit_pov_filled_qty": exit_pov_qty,
                    "exit_pov_fill_amount": round(exit_pov_amount, 2),
                    "exit_other_filled_qty": max(exit_qty - exit_pov_qty, 0),
                    "exit_other_fill_amount": round(max(exit_amount - exit_pov_amount, 0.0), 2),
                    "exit_filled_qty": exit_qty,
                    "exit_fill_amount": round(exit_amount, 2),
                    "exit_completion_pct": round(exit_qty / exit_target_qty * 100, 4) if exit_target_qty else 0.0,
                    "exit_vwap": round(exit_vwap, 6),
                    "benchmark_close": round(benchmark_close, 6),
                    "sell_slippage_bps": round(sell_slippage, 4),
                    "total_slippage_bps": round(buy_slippage + sell_slippage, 4),
                    "exit_remaining_qty": exit_remaining,
                    "overnight_residual_qty": overnight,
                    "execution_status": status,
                    "data_quality_note": "；".join(notes),
                    "updated_at": _now_text(),
                })

            _write_csv(self.summary_path, SUMMARY_FIELDS, summary)
            return self.summary_path

    def backfill_existing(self) -> Path:
        """把已有持仓、E2容量、旧买入POV日志和退出安全账本兼容回填。"""
        # 清理旧版本或异常中断留下的无身份事件，避免空日期/空策略串成伪交易。
        _write_csv(
            self.buy_path,
            BUY_FIELDS,
            (row for row in _read_csv(self.buy_path) if _valid_trade_key(row.get("trade_key"))),
        )
        _write_csv(
            self.sell_path,
            SELL_FIELDS,
            (row for row in _read_csv(self.sell_path) if _valid_trade_key(row.get("trade_key"))),
        )
        positions = self._load_positions()
        existing_plans = {
            _text(row.get("trade_key")): row for row in _read_csv(self.plan_path)
            if _text(row.get("trade_key"))
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for pos in positions:
            key = make_trade_key(
                pos.get("buy_date"), pos.get("ts_code"), pos.get("strategy_leg"), pos.get("signal_date")
            )
            grouped.setdefault(key, []).append(pos)

        capacity_records: list[dict[str, Any]] = []
        capacity_path = self.root / "data" / "processed" / "e2_capacity_history.json"
        try:
            payload = json.loads(capacity_path.read_text(encoding="utf-8"))
            capacity_records = [row for row in payload.get("records", []) if isinstance(row, dict)]
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

        e2_reference: dict[tuple[str, str], float] = {}
        e2_signal_path = self.root / "reports" / "strategy_e2" / "e2_signals_recent.json"
        try:
            payload = json.loads(e2_signal_path.read_text(encoding="utf-8"))
            for signal in payload.get("signals", []):
                if isinstance(signal, dict):
                    e2_reference[(_date(signal.get("signal_date")), _code(signal.get("ts_code")))] = _float(signal.get("limit_close"))
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

        pov_state_items: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            payload = json.loads((self.root / "data" / "state" / "pov_state.json").read_text(encoding="utf-8"))
            for item in payload.get("items", []):
                if isinstance(item, dict):
                    pov_state_items[(_date(payload.get("date")), _code(item.get("ts_code")))] = item
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

        capacity_by_key = {
            (_date(row.get("date")), _code(row.get("ts_code"))): row for row in capacity_records
        }
        for key, group in grouped.items():
            existing = existing_plans.get(key, {})
            if existing and _text(existing.get("entry_status")) != "已回填":
                # 实盘开仓前已经冻结过原始目标，历史回填绝不能用实际成交反向覆盖。
                continue
            seed = group[0]
            entry_qty = sum(_int(pos.get("entry_shares", pos.get("shares", 0))) for pos in group)
            entry_amount = sum(
                _int(pos.get("entry_shares", pos.get("shares", 0))) * _float(pos.get("buy_price"))
                for pos in group
            )
            cap = capacity_by_key.get((_date(seed.get("buy_date")), _code(seed.get("ts_code"))), {})
            target_amount = _float(cap.get("planned_amt")) or entry_amount
            ref = e2_reference.get((_date(seed.get("signal_date")), _code(seed.get("ts_code"))), 0.0)
            if ref <= 0:
                ref = _float(seed.get("buy_price"))
            target_qty = int(target_amount / ref / 100) * 100 if ref > 0 else entry_qty
            target_qty = max(target_qty, entry_qty)
            pov_item = pov_state_items.get((_date(seed.get("buy_date")), _code(seed.get("ts_code"))), {})
            pov_target_amount = _float(pov_item.get("target_amt"))
            has_pov_position = any(
                _text(pos.get("order_id")).lower().startswith("pov-") for pos in group
            )
            pov_planned_qty = 0
            if has_pov_position and ref > 0:
                pov_planned_qty = sum(
                    _int(pos.get("entry_shares", pos.get("shares", 0)))
                    for pos in group
                    if _text(pos.get("order_id")).lower().startswith("pov-")
                )
            if pov_target_amount > 0 and ref > 0:
                pov_planned_qty = int(pov_target_amount / ref / 100) * 100
            auction_planned_qty = max(target_qty - pov_planned_qty, 0)
            if pov_target_amount > 0:
                # 旧状态没有保存竞价委托股数；现有竞价子仓是唯一可核实口径。
                # 若当时发生竞价部分成交，完成率会在质量备注中保留“历史回填”提示。
                auction_planned_qty = sum(
                    _int(pos.get("entry_shares", pos.get("shares", 0)))
                    for pos in group
                    if not _text(pos.get("order_id")).lower().startswith("pov-")
                )
            self.register_entry_plan(
                entry_date=seed.get("buy_date"), ts_code=seed.get("ts_code"),
                name=seed.get("name"), strategy_leg=seed.get("strategy_leg"),
                signal_date=seed.get("signal_date"), target_qty=target_qty,
                target_amount=target_amount, reference_price=ref,
                auction_planned_qty=auction_planned_qty, pov_planned_qty=pov_planned_qty,
                pov_target_amount=pov_target_amount,
                planned_exit_date=seed.get("planned_exit_date"), status="已回填",
            )

        legacy_buy_path = self.root / "reports" / "pov_execution_log.csv"
        for index, old in enumerate(_read_csv(legacy_buy_path), 1):
            candidates = [
                rows for rows in grouped.values()
                if _date(rows[0].get("buy_date")) == _date(old.get("date"))
                and _code(rows[0].get("ts_code")) == _code(old.get("ts_code"))
            ]
            if not candidates:
                continue
            seed = candidates[0][0]
            self.record_buy_slice(
                event_id=f"历史买入POV|{_date(old.get('date'))}|{_code(old.get('ts_code'))}|{index}",
                entry_date=old.get("date"), time=old.get("time"), ts_code=old.get("ts_code"),
                name=old.get("name"), strategy_leg=seed.get("strategy_leg"),
                signal_date=seed.get("signal_date"), channel="买入POV",
                slice_no=old.get("slice_no"), budget=old.get("budget"),
                order_price=_float(old.get("order_amt")) / _int(old.get("order_qty")) if _int(old.get("order_qty")) else 0,
                order_qty=old.get("order_qty"), filled_qty=old.get("filled_qty"),
                fill_price=old.get("fill_price"), benchmark_open=old.get("open_price"),
                remaining_amount=old.get("remain_amt"), status=old.get("note"), note="旧版POV日志回填",
            )

        exit_state_path = self.root / "data" / "processed" / "exit_execution_state.json"
        try:
            exit_state = json.loads(exit_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            exit_state = {}
        by_order = {_text(pos.get("order_id")): pos for pos in positions}
        for intent in exit_state.get("intents", []) if isinstance(exit_state, dict) else []:
            if not isinstance(intent, dict):
                continue
            pos = by_order.get(_text(intent.get("local_order_id")))
            if pos is None:
                continue
            fill_price = _float(pos.get("sell_price"))
            created_at = _text(intent.get("created_at"))
            recorded_time = created_at[11:19] if len(created_at) >= 19 else ""
            self.record_sell_slice(
                event_id=f"历史退出|{_text(intent.get('token'))}",
                entry_date=pos.get("buy_date"), exit_date=intent.get("trade_date"),
                time=recorded_time, ts_code=pos.get("ts_code"),
                name=pos.get("name"), strategy_leg=pos.get("strategy_leg"),
                signal_date=pos.get("signal_date"), channel=intent.get("phase", "其他平仓"),
                local_order_id=pos.get("order_id"), broker_order_id=intent.get("broker_order_id"),
                order_price=intent.get("price"), order_qty=intent.get("quantity"),
                filled_qty=intent.get("filled_qty"), fill_price=fill_price,
                remaining_qty=max(_int(pos.get("entry_shares")) - _int(intent.get("filled_qty")), 0),
                status=intent.get("status"), note="退出安全账本回填",
            )
        return self.rebuild_summary()
