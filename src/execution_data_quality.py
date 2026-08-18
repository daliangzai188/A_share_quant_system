"""真实成交完成汇总的数据缺口分类与可恢复性审计。

本模块只读成交汇总、持仓账本和卖出事件。它不会回填价格、修改持仓或改变
实盘门禁。正常未到期持仓与真正的历史数据缺失必须分开统计，避免把“仍在持有”
误报成已平仓数据故障，也避免为提高完整率而猜测卖出价。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping

import pandas as pd

from src.strategy_identity import normalize_strategy_frame, normalize_strategy_leg


ACTIVE_LEGS = {"A", "C", "D", "E", "L", "M"}


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _date(value: Any) -> str:
    digits = "".join(char for char in _text(value) if char.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _code(value: Any) -> str:
    text = _text(value).upper()
    if not text or "." in text:
        return text
    if text.startswith(("6", "9")):
        return f"{text}.SH"
    if text.startswith(("0", "2", "3")):
        return f"{text}.SZ"
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return text


def _number(value: Any) -> float:
    number = pd.to_numeric(value, errors="coerce")
    return 0.0 if pd.isna(number) else float(number)


def _trade_key(entry_date: Any, ts_code: Any, strategy_leg: Any, signal_date: Any) -> str:
    return "|".join(
        [_date(entry_date), _code(ts_code), normalize_strategy_leg(strategy_leg), _date(signal_date)]
    )


def _position_exit_evidence(positions: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    evidence: dict[str, dict[str, float]] = {}
    for position in positions:
        key = _trade_key(
            position.get("buy_date"),
            position.get("ts_code"),
            position.get("strategy_leg"),
            position.get("signal_date"),
        )
        if not key.strip("|"):
            continue
        quantity = amount = 0.0
        ledger = position.get("exit_fills_by_date")
        if isinstance(ledger, Mapping):
            for value in ledger.values():
                if not isinstance(value, Mapping):
                    continue
                quantity += _number(value.get("qty"))
                amount += _number(value.get("amount"))
        if quantity <= 0 and _text(position.get("status")).lower() == "closed":
            quantity = _number(
                position.get("entry_shares", position.get("shares", 0))
            )
            sell_price = _number(position.get("sell_price"))
            if sell_price > 0:
                amount = quantity * sell_price
        item = evidence.setdefault(key, {"quantity": 0.0, "amount": 0.0})
        item["quantity"] += quantity
        item["amount"] += amount
    return evidence


def _sell_event_evidence(events: pd.DataFrame) -> dict[str, dict[str, float]]:
    if events.empty or "trade_key" not in events.columns:
        return {}
    selected: dict[str, dict[str, Any]] = {}
    for index, row in events.iterrows():
        broker_id = _text(row.get("broker_order_id"))
        event_id = _text(row.get("event_id"))
        stable = f"broker:{broker_id}" if broker_id else f"event:{event_id or index}"
        old = selected.get(stable)
        current_score = (
            _number(row.get("filled_qty")),
            _number(row.get("fill_amount")),
            _number(row.get("fill_price")),
        )
        old_score = (
            _number(old.get("filled_qty")),
            _number(old.get("fill_amount")),
            _number(old.get("fill_price")),
        ) if old is not None else (-1.0, -1.0, -1.0)
        if old is None or current_score > old_score:
            selected[stable] = row.to_dict()
    evidence: dict[str, dict[str, float]] = {}
    for event in selected.values():
        key = _text(event.get("trade_key"))
        if not key:
            continue
        quantity = _number(event.get("filled_qty"))
        amount = _number(event.get("fill_amount"))
        if amount <= 0:
            amount = quantity * _number(event.get("fill_price"))
        item = evidence.setdefault(key, {"quantity": 0.0, "amount": 0.0})
        item["quantity"] += quantity
        item["amount"] += amount
    return evidence


def _active_frame(raw: pd.DataFrame, config: Mapping[str, Any]) -> pd.DataFrame:
    required = {
        "trade_key",
        "entry_date",
        "planned_exit_date",
        "exit_date",
        "ts_code",
        "strategy_leg",
        "entry_plan_source",
        "entry_filled_qty",
        "entry_fill_amount",
        "exit_filled_qty",
        "exit_fill_amount",
        "execution_status",
    }
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError("成交缺口审计缺少字段：" + "、".join(missing))
    active_legs = {
        normalize_strategy_leg(value)
        for value in config.get("active_legs", sorted(ACTIVE_LEGS))
    }
    frame = normalize_strategy_frame(raw)
    frame = frame[frame["strategy_leg"].isin(active_legs)].copy()
    for column in (
        "entry_filled_qty",
        "entry_fill_amount",
        "exit_filled_qty",
        "exit_fill_amount",
        "exit_remaining_qty",
        "overnight_residual_qty",
    ):
        frame[column] = pd.to_numeric(
            frame.get(column, pd.Series(0.0, index=frame.index)), errors="coerce"
        ).fillna(0.0)
    for column in ("entry_date", "planned_exit_date", "exit_date"):
        frame[column] = frame[column].map(_date)
    return frame


def analyze_execution_data_quality(
    raw: pd.DataFrame,
    config: Mapping[str, Any],
    *,
    as_of_date: str,
    as_of_time: str = "",
    due_today_cutoff: str = "151000",
    positions: Iterable[Mapping[str, Any]] = (),
    sell_events: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """逐笔分类并汇总正常持仓、真实缺口及可恢复性。"""

    as_of = _date(as_of_date)
    if not as_of:
        raise ValueError("成交缺口审计as_of_date必须是有效日期")
    clock = "".join(char for char in str(as_of_time or "") if char.isdigit())[:6]
    cutoff = "".join(char for char in str(due_today_cutoff or "") if char.isdigit())[:6]
    if clock and len(clock) != 6:
        raise ValueError("成交缺口审计as_of_time必须是HHMMSS")
    if len(cutoff) != 6:
        raise ValueError("成交缺口审计due_today_cutoff必须是HHMMSS")
    frame = _active_frame(raw, config)
    position_evidence = _position_exit_evidence(positions)
    event_evidence = _sell_event_evidence(
        sell_events if sell_events is not None else pd.DataFrame()
    )
    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        key = _text(row.get("trade_key"))
        entry_qty = _number(row.get("entry_filled_qty"))
        entry_amount = _number(row.get("entry_fill_amount"))
        exit_qty = _number(row.get("exit_filled_qty"))
        exit_amount = _number(row.get("exit_fill_amount"))
        planned_exit = _date(row.get("planned_exit_date"))
        exit_date = _date(row.get("exit_date"))
        status = _text(row.get("execution_status"))
        plan_source = _text(row.get("entry_plan_source")).upper()
        complete = (
            entry_qty > 0
            and entry_amount > 0
            and exit_qty == entry_qty
            and exit_amount > 0
            and bool(exit_date)
        )
        category = "COMPLETE"
        severity = "INFO"
        is_gap = False
        normal_open = False
        recoverability = "NOT_NEEDED"
        action = "无需处理"
        reason = "真实买卖数量、金额和退出日期完整"

        if not complete:
            is_open_status = status in {"持仓中", "平仓中"}
            if entry_qty <= 0 or entry_amount <= 0:
                category = "ENTRY_FILL_MISSING"
                severity = "P1"
                is_gap = True
                recoverability = "BROKER_HISTORY_OR_LOCAL_LOG_REQUIRED"
                reason = "买入数量或买入金额缺失"
                action = "核对券商历史成交、买入事件和持仓账本，不得反推成交价"
            elif is_open_status and exit_qty < entry_qty and not planned_exit:
                category = "OPEN_EXIT_PLAN_MISSING"
                severity = "P0"
                is_gap = True
                recoverability = "BROKER_POSITION_AND_ORDERS_REQUIRED"
                reason = "持仓未闭合且缺少计划退出日"
                action = "立即核对券商实际持仓并补建退出计划；卖出安全优先于补数据"
            elif (
                is_open_status
                and exit_qty < entry_qty
                and (
                    planned_exit > as_of
                    or (planned_exit == as_of and (not clock or clock < cutoff))
                )
            ):
                normal_open = True
                category = (
                    "OPEN_DUE_TODAY" if planned_exit == as_of else "OPEN_NOT_DUE"
                )
                recoverability = "WAIT_FOR_PLANNED_EXIT"
                reason = (
                    "持仓今日到期，等待计划平仓完成"
                    if planned_exit == as_of
                    else f"正常持仓，计划{planned_exit}退出"
                )
                action = "继续按原计划监控卖出，当前不计入数据故障"
            elif is_open_status and exit_qty < entry_qty:
                category = "OPEN_OVERDUE"
                severity = "P0"
                is_gap = True
                recoverability = "BROKER_POSITION_AND_ORDERS_REQUIRED"
                reason = "计划退出日已过但仍显示未平仓或部分平仓"
                action = "立即核对券商实际持仓和活动卖单；卖出安全优先于补数据"
            elif exit_qty != entry_qty:
                category = "EXIT_QUANTITY_MISMATCH"
                severity = "P0" if planned_exit and planned_exit < as_of else "P1"
                is_gap = True
                recoverability = "BROKER_HISTORY_OR_LOCAL_LOG_REQUIRED"
                reason = f"卖出数量{exit_qty:.0f}与买入数量{entry_qty:.0f}不闭合"
                action = "核对券商成交、持仓余量和卖出事件，禁止计入收益"
            elif exit_amount <= 0:
                category = "CLOSED_EXIT_AMOUNT_MISSING"
                is_gap = True
                pos = position_evidence.get(key, {})
                event = event_evidence.get(key, {})
                if (
                    _number(pos.get("quantity")) >= entry_qty
                    and _number(pos.get("amount")) > 0
                ):
                    severity = "P1"
                    recoverability = "AUTO_REBUILD_FROM_POSITION_LEDGER"
                    reason = "汇总缺少卖出金额，但持仓退出成交账保留完整金额"
                    action = "先只读校验持仓退出账，确认一致后重建汇总"
                elif (
                    _number(event.get("quantity")) >= entry_qty
                    and _number(event.get("amount")) > 0
                ):
                    severity = "P1"
                    recoverability = "AUTO_REBUILD_FROM_SELL_EVENTS"
                    reason = "汇总缺少卖出金额，但卖出事件保留完整成交金额"
                    action = "先去重核验券商委托号，确认一致后重建汇总"
                elif plan_source == "BACKFILLED":
                    severity = "P2"
                    recoverability = "BROKER_STATEMENT_REQUIRED_LEGACY"
                    reason = "上线前回填交易已平仓，但本地未保存真实卖出金额"
                    action = "查询券商历史成交或交割单；查不到则永久标记旧数据缺失，禁止猜价"
                else:
                    severity = "P1"
                    recoverability = "BROKER_HISTORY_OR_LOCAL_LOG_REQUIRED"
                    reason = "真实冻结计划已平仓，但本地卖出事件和金额缺失"
                    action = "优先查询券商历史成交、守护日志和卖出安全账本"
            elif not exit_date:
                category = "CLOSED_EXIT_DATE_MISSING"
                severity = "P1"
                is_gap = True
                recoverability = "BROKER_HISTORY_OR_LOCAL_LOG_REQUIRED"
                reason = "卖出数量和金额完整，但退出日期缺失"
                action = "从券商成交日期或卖出事件补齐，未补齐前不计入滚动样本"

        rows.append(
            {
                "trade_key": key,
                "entry_date": _date(row.get("entry_date")),
                "planned_exit_date": planned_exit,
                "exit_date": exit_date,
                "ts_code": _code(row.get("ts_code")),
                "name": _text(row.get("name")),
                "strategy_leg": normalize_strategy_leg(row.get("strategy_leg")),
                "entry_plan_source": plan_source,
                "execution_status": status,
                "entry_filled_qty": entry_qty,
                "entry_fill_amount": entry_amount,
                "exit_filled_qty": exit_qty,
                "exit_fill_amount": exit_amount,
                "gap_category": category,
                "severity": severity,
                "is_data_gap": is_gap,
                "is_normal_open": normal_open,
                "recoverability": recoverability,
                "reason": reason,
                "recommended_action": action,
            }
        )

    detail = pd.DataFrame(rows)
    normal_open_count = int(detail["is_normal_open"].sum()) if len(detail) else 0
    gap_count = int(detail["is_data_gap"].sum()) if len(detail) else 0
    complete_count = int(detail["gap_category"].eq("COMPLETE").sum()) if len(detail) else 0
    settled_count = max(len(detail) - normal_open_count, 0)
    category_counts = Counter(detail["gap_category"].astype(str)) if len(detail) else Counter()
    recovery_counts = Counter(
        detail.loc[detail["is_data_gap"], "recoverability"].astype(str)
    ) if len(detail) else Counter()
    has_overdue_position = bool(
        detail["gap_category"].eq("OPEN_OVERDUE").any()
    ) if len(detail) else False
    has_other_p0 = bool(
        (detail["is_data_gap"] & detail["severity"].eq("P0")).any()
    ) if len(detail) else False
    if has_overdue_position:
        status = "P0_OVERDUE_POSITION"
        reason = "存在计划退出日已过但仍未闭合的持仓，必须立即核对券商。"
    elif has_other_p0:
        status = "P0_EXECUTION_STATE"
        reason = "存在P0级持仓或成交状态缺口，必须立即核对券商和退出计划。"
    elif gap_count:
        status = "DATA_GAP"
        reason = f"存在{gap_count}笔真实成交数据缺口；正常未到期/今日到期持仓{normal_open_count}笔不算故障。"
    elif normal_open_count:
        status = "OPEN_POSITION_ONLY"
        reason = f"仅有{normal_open_count}笔正常持仓尚未退出，没有已结算数据缺口。"
    else:
        status = "PASS"
        reason = "全部当前策略成交记录均已完整闭合。"
    summary = {
        "status": status,
        "reason": reason,
        "as_of_date": as_of,
        "as_of_time": clock,
        "due_today_cutoff": cutoff,
        "active_trade_rows": int(len(detail)),
        "complete_trade_rows": complete_count,
        "normal_open_trade_rows": normal_open_count,
        "true_data_gap_rows": gap_count,
        "settled_trade_rows": int(settled_count),
        "settled_data_complete_rate": (
            float(complete_count / settled_count) if settled_count else 1.0
        ),
        "auto_rebuild_candidate_rows": int(
            detail["recoverability"].astype(str).str.startswith("AUTO_REBUILD_").sum()
        ) if len(detail) else 0,
        "category_counts": dict(sorted(category_counts.items())),
        "recovery_counts": dict(sorted(recovery_counts.items())),
        "automatic_writeback": False,
        "note": "本报告只分类和给出行动建议，不会猜测卖出价或自动修改权威账本。",
    }
    return detail, summary
