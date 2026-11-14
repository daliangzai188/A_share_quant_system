"""策略D：盘中监控线程、09:23接力卖出、09:30~10:30资金中性接力、孤儿成交找回。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import csv
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from src.utils.config import load_json_config, mkdir_p
from src.utils.time_utils import now_beijing, today_beijing
from src.portfolio_ace_m import ACE_FULL_HARD_CAP_PCT, ACE_FULL_TARGET_PCT
from src.strategy_d_checkpoint import (
    StrategyDCheckpointCheck,
    clear_strategy_d_intraday_checkpoints,
    inspect_strategy_d_checkpoint,
    strategy_d_checkpoint_path,
    strategy_d_machine_fingerprint,
    strategy_d_runtime_fingerprint,
)
from src.strategy_d_factor_rules import ReleaseSignalClock, load_factor_release, release_signal_clock
from src.strategy_d_spec import D_CHECKPOINT_MAX_AGE_SECONDS, D_ORDER_CANCEL_HHMM, D_TRACKING_START_HHMM
from src.strategy_d_relay_execution import (
    calculate_auction_safe_sell_quantity,
    calculate_relay_buy_quantity,
    floor_lot as relay_floor_lot,
    relay_buy_limit_price,
)
from src.daemon.common import (
    D_MONITOR_PID_FILE,
    D_RELAY_PAIR_EXEC_LOG,
    PROJECT_ROOT,
    SCHED_AFTERNOON_CLOSE,
    _D_RELAY_SELL_REMARK_PREFIX,
    _critical_execution_state_failure,
    _d_relay_pair_thread_lock,
    _exit_sell_lock,
    _fmt_wan,
    _normalize_ts_code,
    _notify,
    _positions_file_lock,
    _request_process_recovery,
    _ts_code_aliases,
    logger,
    next_n_trade_days,
)
from src.daemon.state import (
    _d_relay_pair_load_state,
    _d_relay_pair_save_state,
    _exit_account_fingerprint,
    _mark_live_position_closed,
    _record_live_buy,
    _reduce_live_position_shares,
    load_positions,
    record_buy,
    save_positions,
)
from src.daemon.plans import (
    _combined_paths_for_today,
    _has_premarket_close_plan,
    _signal_day_amount,
    d_intraday_entry_gate,
    d_intraday_monitor_gate,
    load_combined_decisions,
    read_cached_combined_decisions,
    st_open_forbidden,
)
from src.daemon.qmt import SharedQMTBrokerProxy, _qmt_get, _qmt_lock
from src.daemon.orders import _confirm_fill, _try_cancel_order
from src.daemon.sell import (
    _assert_exit_live_allowed,
    _broker_position_quantities,
    _max_sell_order_shares,
    _normalize_exit_slice_quantity,
    _place_exit_order_with_intent,
    _resolve_exit_intent_by_broker_order_id,
    _safe_new_exit_order_quantity,
    _update_exit_order_intent,
)
from src.daemon.sell_pov import _pov_depth_limit
from src.daemon.close import check_and_close_positions


def _strategy_d_release_signal_clock(
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ReleaseSignalClock]:
    """读取D正式发布并由发布分支推导唯一的实盘信号时钟。"""

    runtime_config = config or load_json_config(
        PROJECT_ROOT / "config" / "config.json"
    )
    strategy_config = runtime_config.get("strategy_d", {})
    raw_path = str(
        strategy_config.get(
            "factor_release_path", "config/strategy_d_factor_release.json"
        )
    )
    release_path = Path(raw_path)
    if not release_path.is_absolute():
        release_path = PROJECT_ROOT / release_path
    release = load_factor_release(release_path)
    clock = release_signal_clock(
        release,
        full_session_last_hhmm=D_ORDER_CANCEL_HHMM - 1,
    )
    return release, clock


def _strategy_d_checkpoint_recovery_check(
    now: datetime.datetime,
    config: dict[str, Any] | None = None,
) -> StrategyDCheckpointCheck:
    """启动D线程前做轻量预检；逐票宇宙由监控器setup后再做最终校验。"""

    runtime_config = config or load_json_config(
        PROJECT_ROOT / "config" / "config.json"
    )
    path = strategy_d_checkpoint_path(
        PROJECT_ROOT, now.strftime("%Y%m%d")
    )
    try:
        configured_max_age = int(
            runtime_config.get("strategy_d", {}).get(
                "checkpoint_max_age_sec", D_CHECKPOINT_MAX_AGE_SECONDS
            )
        )
        if configured_max_age != D_CHECKPOINT_MAX_AGE_SECONDS:
            return StrategyDCheckpointCheck(
                False,
                "D检查点最大时效偏离认证值",
                path,
                {},
            )
        runtime_fingerprint = strategy_d_runtime_fingerprint(
            PROJECT_ROOT, runtime_config
        )
        return inspect_strategy_d_checkpoint(
            path,
            trade_date=now.strftime("%Y%m%d"),
            now=now,
            max_age_seconds=D_CHECKPOINT_MAX_AGE_SECONDS,
            expected_tracking_start_hhmm=D_TRACKING_START_HHMM,
            expected_machine_fingerprint=strategy_d_machine_fingerprint(),
            expected_runtime_fingerprint=runtime_fingerprint,
        )
    except Exception as exc:
        return StrategyDCheckpointCheck(
            False,
            f"D检查点预检异常:{exc}",
            path,
            {},
        )


def reconcile_d_orphan_fills() -> bool:
    """找回孤儿D成交：QMT当日 remark=D_FIRST_BOARD 已成交、但本地无记录的委托，自动回写持仓。

    场景（2026-07-23 北方长龙事故）：盘中重启daemon后，旧会话D监控的内存委托明细
    丢失，14:55确认流程认不出旧单——成交的股票落在账户里没人管（显示为外部持仓、
    次日9:23不会卖出），且本地空仓误判可能让D重复开仓。
    调用点：daemon启动检查前 + 15:10收盘对账后。幂等：按order_id与(当日+代码+D腿)双重去重。
    """
    log = logger()
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    if not (bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))):
        return True
    today_str = today_beijing().strftime("%Y%m%d")
    try:
        with _qmt_lock:
            adapter = _qmt_get(config.get("broker", {}))
            orders = adapter.query_orders() or []
    except Exception as e:
        log.warning("孤儿D成交恢复：查询当日委托失败（%s），本轮跳过。", e)
        return False

    def _pick(d: dict, keys: list, default: Any = "") -> Any:
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        return default

    positions = load_positions()
    known_ids = {str(p.get("order_id", "")) for p in positions}
    has_today_d = any(
        str(p.get("strategy_leg", "")).upper() == "D" and str(p.get("buy_date", "")) == today_str
        and str(p.get("status", "")).lower() in {"open", "sell_pending"}
        for p in positions
    )
    recovered = 0
    unresolved = 0
    for o in orders:
        if not isinstance(o, dict):
            o = getattr(o, "__dict__", {}) or {}
        remark = str(_pick(o, ["order_remark", "m_strRemark", "remark"], "")).strip()
        if not remark.startswith("D_FIRST_BOARD"):
            continue
        try:
            otype = int(_pick(o, ["order_type", "m_nOrderType", "order_side", "m_nDirection"], -1))
            traded = int(float(_pick(o, ["traded_volume", "m_nTradedVolume", "traded_qty", "deal_volume"], 0) or 0))
        except (TypeError, ValueError):
            continue
        if otype != 23 or traded <= 0:
            continue   # 只认买单且有实际成交（部成也算）
        oid = str(_pick(o, ["order_id", "m_nOrderID", "order_sysid", "m_strOrderSysID"], "")).strip()
        ts_code = _normalize_ts_code(_pick(o, ["stock_code", "m_strInstrumentID", "instrument_id", "ts_code"], ""))
        if not oid or not ts_code or oid in known_ids:
            continue
        if has_today_d and any(
            str(p.get("ts_code", "")) == ts_code and str(p.get("buy_date", "")) == today_str
            for p in positions
        ):
            continue   # 同票同日已有记录（可能旧会话已回写），不重复
        if has_today_d:
            log.critical(
                "孤儿D成交与已有D持仓不是同一标的：%s order_id=%s；"
                "无法猜测两笔归属，恢复不通过。",
                ts_code, oid,
            )
            unresolved += 1
            continue
        price = float(_pick(o, ["traded_price", "m_dTradedPrice", "avg_price", "deal_price"], 0) or 0)
        if price <= 0:
            price = float(_pick(o, ["price", "m_dPrice", "order_price"], 0) or 0)
        if price <= 0:
            log.critical(
                "孤儿D成交缺少成交价和委托价：%s order_id=%s，拒绝写0成本持仓。",
                ts_code, oid,
            )
            unresolved += 1
            continue
        name = str(_pick(o, ["stock_name", "m_strInstrumentName", "instrument_name"], "")).strip()
        try:
            planned_exit = next_n_trade_days(today_beijing(), 2).strftime("%Y%m%d")
        except Exception:
            planned_exit = today_str
        record_buy(
            order_id=oid,
            ts_code=ts_code,
            name=name,
            signal_date=today_str,
            buy_date=today_str,
            shares=traded,
            buy_price=round(price, 4),
            strategy_leg="D",
            execution_channel="孤儿D成交恢复",
            planned_order_qty=traded,
            planned_exit_date_override=planned_exit,
        )
        known_ids.add(oid)
        has_today_d = True
        recovered += 1
        log.warning(
            "🧷 [孤儿D恢复] %s %s %d股@%.2f 已自动回写持仓账本（order_id=%s，计划%s平仓）。",
            ts_code, name or "?", traded, price, oid, planned_exit,
        )
        _notify("buy_result", "🧷 已自动找回D成交持仓",
                f"策略D {ts_code} {name} {traded}股@{price:.2f}（daemon重启导致旧会话失忆，"
                f"已从QMT当日委托自动恢复），按持仓计划日执行普通D平仓；若出现接力候选则走安全竞价部分＋成对POV。",
                level="timeSensitive")
    if recovered:
        log.warning("孤儿D成交恢复完成：共找回 %d 笔，持仓账本已更新。", recovered)
    return unresolved == 0


_d_relay_pair_thread: threading.Thread | None = None


def _d_relay_pair_log(state: dict[str, Any], **values: Any) -> None:
    """记录每一片卖D/买新仓的真实成交，供后续按资金规模校准参数。"""
    try:
        mkdir_p(D_RELAY_PAIR_EXEC_LOG.parent)
        new_file = not D_RELAY_PAIR_EXEC_LOG.exists()
        row = {
            "date": state.get("date", ""),
            "time": now_beijing().strftime("%H:%M:%S"),
            "slice_no": state.get("slice_no", 0),
            "d_ts_code": state.get("d", {}).get("ts_code", ""),
            "next_ts_code": state.get("candidate", {}).get("ts_code", ""),
            "event": "",
            "order_id": "",
            "order_qty": 0,
            "filled_qty": 0,
            "fill_price": 0.0,
            "filled_amount": 0.0,
            "confirmed_sell_amount": state.get("confirmed_sell_amount", 0.0),
            "confirmed_buy_amount": state.get("confirmed_buy_amount", 0.0),
            "remaining_d_shares": state.get("d", {}).get("remaining_shares", 0),
            "note": "",
        }
        row.update(values)
        with open(D_RELAY_PAIR_EXEC_LOG, "a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row.keys()), extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:
        logger().warning("D接力逐片日志写入失败（不改变交易状态）：%s", exc)


def _load_d_relay_candidate_order() -> dict[str, Any] | None:
    """读取组合状态机锁定的D接力候选影子计划。"""
    import pandas as pd

    _decisions_path, orders_path = _combined_paths_for_today()
    if not orders_path.exists():
        return None
    orders = pd.read_csv(orders_path)
    if orders.empty or "planned_action" not in orders.columns:
        return None
    rows = orders[
        orders["planned_action"].astype(str).eq("PLAN_D_RELAY_PAIRED_BUY")
    ]
    if len(rows) != 1:
        if len(rows) > 1:
            raise RuntimeError(f"D接力候选必须唯一，实际{len(rows)}条")
        return None
    return rows.iloc[0].to_dict()


def _d_relay_prepare_state(
    pos: dict[str, Any],
    candidate: dict[str, Any],
    *,
    account: Any,
    broker_total: int,
    broker_can_use: int,
) -> dict[str, Any]:
    """建立一次D→A/C/E资金中性接力任务，不覆盖同日已有任务。"""
    old = _d_relay_pair_load_state()
    if old:
        old_d = str(old.get("d", {}).get("ts_code", ""))
        old_next = str(old.get("candidate", {}).get("ts_code", ""))
        if old_d != str(pos.get("ts_code", "")) or old_next != str(candidate.get("ts_code", "")):
            raise RuntimeError(
                f"同日已有另一D接力任务 {old_d}→{old_next}，禁止覆盖"
            )
        return old

    local_shares = max(int(pos.get("shares", 0) or 0), 0)
    if local_shares <= 0:
        raise RuntimeError("D本地持仓股数为0")
    if broker_total != local_shares or broker_can_use < local_shares:
        raise RuntimeError(
            f"D仓位归属/可卖量不一致：本地{local_shares}、券商总仓{broker_total}、可卖{broker_can_use}"
        )
    total_asset = float(getattr(account, "total_asset", 0.0) or 0.0)
    available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
    if total_asset <= 0 or available_cash < 0:
        raise RuntimeError("QMT总资产或可用余额无效")
    live_cfg = load_json_config(PROJECT_ROOT / "config" / "config.json").get("live_trade", {})
    target_pct = min(float(live_cfg.get("max_total_position_pct", ACE_FULL_TARGET_PCT)), ACE_FULL_TARGET_PCT)
    hard_pct = min(float(live_cfg.get("max_position_pct", ACE_FULL_HARD_CAP_PCT)), ACE_FULL_HARD_CAP_PCT)
    signal_date = str(candidate.get("signal_date", "")).strip().split(".")[0]
    candidate_code = str(candidate.get("ts_code", ""))
    signal_day_amount = _signal_day_amount(candidate_code, signal_date)
    if signal_day_amount <= 0 and bool(live_cfg.get("liquidity_cap_fail_closed", True)):
        raise RuntimeError(
            f"接力候选{candidate_code}信号日{signal_date}成交额缺失，禁止先卖D"
        )
    liquidity_cap = (
        signal_day_amount * float(live_cfg.get("total_liquidity_cap_pct", 0.02))
        if signal_day_amount > 0 else float("inf")
    )
    fixed_cap = float(live_cfg.get("max_single_order_amount", 0.0) or 0.0)
    if fixed_cap <= 0:
        fixed_cap = float("inf")
    try:
        exit_n_raw = float(candidate.get("exit_n_days", 2) or 2)
        exit_n_days = max(int(exit_n_raw), 1) if exit_n_raw == exit_n_raw else 2
    except (TypeError, ValueError, OverflowError):
        exit_n_days = 2
    try:
        reference_raw = float(candidate.get("reference_price", 0.0) or 0.0)
        reference_price = reference_raw if reference_raw == reference_raw else 0.0
    except (TypeError, ValueError):
        reference_price = 0.0
    target_amount = min(total_asset * target_pct, liquidity_cap, fixed_cap)
    hard_cap_amount = min(total_asset * hard_pct, liquidity_cap, fixed_cap)
    candidate_short = candidate_code.upper().split(".")[0]
    candidate_min_buy = 200 if candidate_short.startswith(("688", "689")) else int(
        live_cfg.get("round_lot_size", 100)
    )
    if reference_price <= 0 or min(target_amount, hard_cap_amount) < reference_price * candidate_min_buy:
        raise RuntimeError(
            f"接力候选{candidate_code}参考价/安全资金不足最小申报量，禁止先卖D"
        )
    state = {
        "version": 1,
        "date": today_beijing().strftime("%Y%m%d"),
        "status": "PREPARED",
        "created_at": now_beijing().isoformat(),
        "account_fingerprint": _exit_account_fingerprint(),
        "total_asset_snapshot": total_asset,
        "available_cash_baseline": available_cash,
        "target_amount": target_amount,
        "hard_cap_amount": hard_cap_amount,
        "confirmed_sell_qty": 0,
        "confirmed_sell_amount": 0.0,
        "confirmed_buy_qty": 0,
        "confirmed_buy_amount": 0.0,
        "slice_no": 0,
        "d_prev_cum_amount": 0.0,
        "next_prev_cum_amount": 0.0,
        "pending_sell": None,
        "pending_buy": None,
        "d": {
            "local_order_id": str(pos.get("order_id", "")),
            "ts_code": str(pos.get("ts_code", "")),
            "name": str(pos.get("name", "")),
            "signal_date": str(pos.get("signal_date", "")),
            "initial_shares": local_shares,
            "remaining_shares": local_shares,
            "planned_exit_date": str(pos.get("planned_exit_date", "")),
        },
        "candidate": {
            "paper_order_id": str(candidate.get("paper_order_id", "")),
            "ts_code": str(candidate.get("ts_code", "")),
            "name": str(candidate.get("name", "")),
            "strategy_leg": str(candidate.get("strategy_leg", "")),
            "signal_date": signal_date,
            "exit_n_days": exit_n_days,
            "reference_price": reference_price,
            "signal_day_amount": signal_day_amount,
        },
    }
    _d_relay_pair_save_state(state)
    return state


def _handoff_same_stock_d_relay(state: dict[str, Any]) -> None:
    """D与接力候选同票时直接变更策略归属，避免无意义卖出后原价买回。"""
    d = state["d"]
    candidate = state["candidate"]
    if not (_ts_code_aliases(d["ts_code"]) & _ts_code_aliases(candidate["ts_code"])):
        raise RuntimeError("同票接力方法收到不同股票")
    buy_date = state["date"]
    exit_date = next_n_trade_days(
        datetime.datetime.strptime(buy_date, "%Y%m%d").date(),
        n=max(int(candidate.get("exit_n_days", 2) or 2), 1),
    )
    with _positions_file_lock:
        positions = load_positions()
        matched = 0
        for position in positions:
            if str(position.get("order_id", "")) != str(d.get("local_order_id", "")):
                continue
            if str(position.get("status", "")).lower() not in {"open", "sell_pending"}:
                continue
            position["relay_from_strategy_leg"] = "D"
            position["relay_handoff_date"] = buy_date
            position["relay_handoff_reference_price"] = float(candidate.get("reference_price", 0.0) or 0.0)
            position["strategy_leg"] = str(candidate.get("strategy_leg", ""))
            position["signal_date"] = str(candidate.get("signal_date", ""))
            position["planned_exit_date"] = exit_date.strftime("%Y%m%d")
            position["planned_exit_time"] = datetime.datetime.combine(
                exit_date, SCHED_AFTERNOON_CLOSE
            ).strftime("%Y-%m-%d %H:%M")
            position["status"] = "open"
            matched += 1
        if matched != 1:
            raise RuntimeError(f"同票接力本地D持仓必须唯一，实际{matched}条")
        try:
            save_positions(positions)
        except Exception as exc:
            _critical_execution_state_failure("D同票接力持仓归属切换", exc)
    state["status"] = "DONE"
    state["done_reason"] = "D与接力候选同票，已直接变更策略归属，未产生卖买委托"
    state["completed_at"] = now_beijing().isoformat()
    _d_relay_pair_save_state(state)
    _d_relay_pair_log(state, event="同票策略交接", note=state["done_reason"])
    _notify(
        "buy_result", "✅ D同票接力已无交易切换",
        f"{d['ts_code']} {d.get('name','')} 与接力候选相同，未卖出再买回；"
        f"策略归属已切换为{candidate.get('strategy_leg','')}，计划{exit_date.strftime('%Y%m%d')}平仓。",
    )


def _d_relay_block(state: dict[str, Any], reason: str, *, notify: bool = True) -> None:
    """阻断本次接力后停止新增卖单和买单，保留已确认成交。"""
    state["status"] = "BLOCKED"
    state["blocked_reason"] = str(reason)
    state["blocked_at"] = now_beijing().isoformat()
    _d_relay_pair_save_state(state)
    logger().error("🛑 [D接力成对POV] %s", reason)
    if notify:
        _notify(
            "sell_fail", "🛑 D接力已停止",
            f"{state.get('d',{}).get('ts_code','')}→{state.get('candidate',{}).get('ts_code','')}：{reason}。"
            "系统不会继续卖D或透支买新仓，请核对QMT委托与持仓。",
            level="critical", call=True,
        )


def _d_relay_recover_order_id(adapter: Any, pending: dict[str, Any], *, side: str) -> str:
    """按唯一remark恢复崩溃窗口内已经受理、但尚未来得及回写单号的委托。"""
    existing = str(pending.get("order_id", "") or "").strip()
    if existing:
        return existing
    from src.qmt_adapter import first_present, object_to_dict, to_int

    remark = str(pending.get("remark", "") or "")
    target_short = str(pending.get("ts_code", "")).upper().split(".")[0]
    target_qty = max(int(pending.get("quantity", 0) or 0), 0)
    target_side = str(side).upper()
    matches: list[str] = []
    for order in adapter.query_orders() or []:
        data = object_to_dict(order)
        code = str(first_present(data, ["stock_code", "m_strInstrumentID", "ts_code"], "")).upper().split(".")[0]
        order_remark = str(first_present(data, ["order_remark", "m_strRemark", "remark"], ""))
        quantity = max(to_int(first_present(
            data, ["order_volume", "m_nOrderVolume", "volume", "order_qty"], 0
        ), 0), 0)
        order_type = to_int(first_present(data, ["order_type", "m_nOrderType"], -1), -1)
        side_text = str(first_present(data, ["side", "order_side", "direction"], "")).upper()
        side_ok = (
            (target_side == "BUY" and (order_type == 23 or side_text in {"BUY", "23", "买入"}))
            or (target_side == "SELL" and (order_type == 24 or side_text in {"SELL", "24", "卖出"}))
        )
        if code == target_short and order_remark == remark and quantity == target_qty and side_ok:
            order_id = str(first_present(data, ["order_id", "m_nOrderID", "order_sysid"], "") or "")
            if order_id:
                matches.append(order_id)
    return matches[0] if len(set(matches)) == 1 else ""


def _d_relay_apply_sell_fill(
    state: dict[str, Any],
    *,
    filled_qty: int,
    fill_price: float,
    order_id: str,
) -> None:
    """把一笔D卖出成交幂等写入本地持仓和接力资金账。"""
    pending = state.get("pending_sell") or {}
    if bool(pending.get("applied", False)):
        return
    qty = max(int(filled_qty or 0), 0)
    d = state["d"]
    old_confirmed = max(int(state.get("confirmed_sell_qty", 0) or 0), 0)
    total_confirmed = min(
        old_confirmed + qty,
        max(int(d.get("initial_shares", 0) or 0), 0),
    )
    remaining = max(int(d.get("initial_shares", 0) or 0) - total_confirmed, 0)
    if qty > 0:
        # remaining是根据“初始股数-累计确认成交”推导的绝对值；即使进程恰在
        # 本地回写后崩溃，重启重放也不会再次多减一次。
        if remaining > 0:
            _reduce_live_position_shares(
                str(d.get("local_order_id", "")),
                remaining,
                fill_price=fill_price,
                fill_date=state["date"],
                recovery_context="D接力卖出",
            )
        else:
            _mark_live_position_closed(
                str(d.get("local_order_id", "")),
                state["date"],
                fill_price,
                recovery_context="D接力卖出",
            )
        state["confirmed_sell_qty"] = total_confirmed
        state["confirmed_sell_amount"] = float(state.get("confirmed_sell_amount", 0.0) or 0.0) + qty * fill_price
        d["remaining_shares"] = remaining
    pending["applied"] = True
    pending["filled_qty"] = qty
    pending["fill_price"] = fill_price
    pending["status"] = "RESOLVED"
    state["pending_sell"] = pending
    _d_relay_pair_save_state(state)
    _d_relay_pair_log(
        state, event="卖D成交", order_id=order_id,
        order_qty=int(pending.get("quantity", 0) or 0), filled_qty=qty,
        fill_price=fill_price, filled_amount=qty * fill_price,
        note=str(pending.get("phase", "")),
    )


def _d_relay_reconcile_pending_sell(
    state: dict[str, Any], broker_cfg: dict, *, cancel_nonterminal: bool
) -> bool:
    """确认当前D卖单终态；状态未知时阻断后续接力。"""
    pending = state.get("pending_sell")
    if not isinstance(pending, dict) or str(pending.get("status", "")).upper() == "RESOLVED":
        return True
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        order_id = _d_relay_recover_order_id(adapter, pending, side="SELL")
    if not order_id:
        _d_relay_block(state, "D卖单处于PREPARED但无法从QMT唯一恢复，按全量未知占用停止")
        return False
    pending["order_id"] = order_id
    pending["status"] = "SUBMITTED"
    state["pending_sell"] = pending
    _d_relay_pair_save_state(state)
    fill = _confirm_fill(
        broker_cfg, order_id, int(pending.get("quantity", 0) or 0),
        f"D接力卖-{pending.get('phase','')}", timeout_sec=12, poll_sec=2,
    )
    if not fill.is_terminal and cancel_nonterminal:
        _try_cancel_order(broker_cfg, order_id, str(pending.get("ts_code", "")))
        fill = _confirm_fill(
            broker_cfg, order_id, int(pending.get("quantity", 0) or 0),
            "D接力卖撤单终态", timeout_sec=20, poll_sec=2,
        )
    if not fill.is_terminal:
        _d_relay_block(state, f"D卖单{order_id}未确认终态，禁止继续覆盖或买入")
        return False
    try:
        token = str(pending.get("intent_token", "") or "")
        if token:
            _update_exit_order_intent(
                token,
                status="RESOLVED",
                broker_order_id=order_id,
                filled_qty=int(fill.filled_qty or 0),
                terminal_known=True,
            )
        else:
            _resolve_exit_intent_by_broker_order_id(
                order_id,
                filled_qty=int(fill.filled_qty or 0),
                terminal_known=True,
            )
    except Exception as exc:
        _d_relay_block(state, f"D卖单退出安全账本无法收口:{exc}")
        return False
    fill_price = float(getattr(fill, "avg_price", 0.0) or 0.0)
    if fill_price <= 0:
        try:
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                quote = adapter.get_full_tick([str(pending.get("ts_code", ""))]).get(
                    str(pending.get("ts_code", ""))
                )
            fill_price = float(
                getattr(quote, "open_price", 0.0)
                or getattr(quote, "last_price", 0.0)
                or 0.0
            )
        except Exception:
            fill_price = 0.0
    if fill_price <= 0:
        # 最后兜底使用委托价只会低估D卖出可用金额，不会透支；同时记录告警，
        # 方便用户从QMT成交回报核对价格字段兼容性。
        fill_price = float(pending.get("price", 0.0) or 0.0)
        logger().warning(
            "[D接力] 卖单%s成交均价缺失，使用保守委托价%.2f核算释放资金。",
            order_id, fill_price,
        )
    _d_relay_apply_sell_fill(
        state,
        filled_qty=int(fill.filled_qty or 0),
        fill_price=fill_price,
        order_id=order_id,
    )
    state["pending_sell"] = None
    _d_relay_pair_save_state(state)
    return True


def _d_relay_apply_buy_fill(
    state: dict[str, Any],
    *,
    filled_qty: int,
    fill_price: float,
    order_id: str,
    traded_at: str = "",
) -> None:
    """把接力买入成交先写持仓、再写资金账；重启重放不会重复建仓。"""
    pending = state.get("pending_buy") or {}
    if bool(pending.get("applied", False)):
        return
    qty = max(int(filled_qty or 0), 0)
    candidate = state["candidate"]
    if qty > 0:
        _record_live_buy(
            recovery_context="D接力成对POV",
            order_id=f"d-relay-{state['date']}-{order_id}",
            ts_code=str(candidate.get("ts_code", "")),
            name=str(candidate.get("name", "")),
            signal_date=str(candidate.get("signal_date", "")),
            buy_date=state["date"],
            shares=qty,
            buy_price=fill_price,
            strategy_leg=str(candidate.get("strategy_leg", "")),
            exit_n_days=max(int(candidate.get("exit_n_days", 2) or 2), 1),
            traded_at=traded_at,
            execution_channel="D接力成对POV",
            planned_order_qty=int(pending.get("quantity", qty) or qty),
            benchmark_open=float(pending.get("open_price", 0.0) or 0.0),
            component_order_ids=[str(order_id)],
        )
        state["confirmed_buy_qty"] = int(state.get("confirmed_buy_qty", 0) or 0) + qty
        state["confirmed_buy_amount"] = float(state.get("confirmed_buy_amount", 0.0) or 0.0) + qty * fill_price
    pending["applied"] = True
    pending["filled_qty"] = qty
    pending["fill_price"] = fill_price
    pending["status"] = "RESOLVED"
    state["pending_buy"] = pending
    _d_relay_pair_save_state(state)
    _d_relay_pair_log(
        state, event="买接力仓成交", order_id=order_id,
        order_qty=int(pending.get("quantity", 0) or 0), filled_qty=qty,
        fill_price=fill_price, filled_amount=qty * fill_price,
        note="累计买入额不超过累计D卖出额",
    )


def _d_relay_reconcile_pending_buy(
    state: dict[str, Any], broker_cfg: dict, *, cancel_nonterminal: bool
) -> bool:
    """确认接力买单；未确认终态前不允许再卖下一片D。"""
    pending = state.get("pending_buy")
    if not isinstance(pending, dict) or str(pending.get("status", "")).upper() == "RESOLVED":
        return True
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        order_id = _d_relay_recover_order_id(adapter, pending, side="BUY")
    if not order_id:
        _d_relay_block(state, "接力买单处于PREPARED但无法从QMT唯一恢复，停止全部后续卖买")
        return False
    pending["order_id"] = order_id
    pending["status"] = "SUBMITTED"
    state["pending_buy"] = pending
    _d_relay_pair_save_state(state)
    fill = _confirm_fill(
        broker_cfg, order_id, int(pending.get("quantity", 0) or 0),
        "D接力买", timeout_sec=45, poll_sec=3,
    )
    if not fill.is_terminal and cancel_nonterminal:
        _try_cancel_order(broker_cfg, order_id, str(pending.get("ts_code", "")))
        fill = _confirm_fill(
            broker_cfg, order_id, int(pending.get("quantity", 0) or 0),
            "D接力买撤单终态", timeout_sec=20, poll_sec=2,
        )
    if not fill.is_terminal:
        _d_relay_block(state, f"接力买单{order_id}未确认终态；为防重复买入，停止后续接力")
        return False
    fill_price = float(getattr(fill, "avg_price", 0.0) or 0.0)
    if fill_price <= 0:
        fill_price = float(pending.get("price", 0.0) or 0.0)
    _d_relay_apply_buy_fill(
        state,
        filled_qty=int(fill.filled_qty or 0),
        fill_price=fill_price,
        order_id=order_id,
        traded_at=str(getattr(fill, "traded_at", "") or ""),
    )
    state["pending_buy"] = None
    _d_relay_pair_save_state(state)
    return True


def _d_relay_flow_budget(
    state: dict[str, Any], quote: Any, *, key: str, participation: float
) -> float:
    """按相邻快照真实成交额差计算单片预算；首片使用开盘累计成交额。"""
    current = float(getattr(quote, "amount", 0.0) or 0.0) if quote else 0.0
    previous = float(state.get(key, 0.0) or 0.0)
    state[key] = current
    if current <= 0:
        return 0.0
    external_flow = current if previous <= 0 else max(current - previous, 0.0)
    return external_flow * min(max(float(participation), 0.0), 1.0)


def _d_relay_submit_buy(
    state: dict[str, Any],
    broker_cfg: dict,
    *,
    account: Any,
    quote: Any,
    flow_budget: float,
    chase_cap: float,
    cash_buffer: float,
    lot_size: int,
) -> bool:
    """只用D已确认释放且尚未使用的资金提交一片接力买单。"""
    from src.broker_adapter import OrderRequest
    from src.live_order_gateway import LiveOrderGateway

    candidate = state["candidate"]
    ts_code = str(candidate.get("ts_code", ""))
    name = str(candidate.get("name", ""))
    short_code = ts_code.upper().split(".")[0]
    is_star_market = short_code.startswith(("688", "689"))
    buy_step = 1 if is_star_market else lot_size
    min_buy_qty = 200 if is_star_market else lot_size
    price, gate_reason = relay_buy_limit_price(quote, chase_cap=chase_cap)
    if price <= 0:
        _d_relay_pair_log(state, event="买入跳过", note=gate_reason)
        return False
    if st_open_forbidden(ts_code, name):
        _d_relay_block(state, f"接力候选{ts_code} {name}命中ST/退市禁买铁律")
        return False

    current_total_asset = float(getattr(account, "total_asset", 0.0) or 0.0)
    current_hard_cap = current_total_asset * ACE_FULL_HARD_CAP_PCT if current_total_asset > 0 else 0.0
    snapshot_hard_cap = float(state.get("hard_cap_amount", 0.0) or 0.0)
    hard_cap = min(
        cap for cap in (snapshot_hard_cap, current_hard_cap) if cap > 0
    ) if (snapshot_hard_cap > 0 or current_hard_cap > 0) else 0.0
    decision = calculate_relay_buy_quantity(
        target_amount=float(state.get("target_amount", 0.0) or 0.0),
        hard_cap_amount=hard_cap,
        confirmed_buy_amount=float(state.get("confirmed_buy_amount", 0.0) or 0.0),
        confirmed_sell_amount=float(state.get("confirmed_sell_amount", 0.0) or 0.0),
        available_cash=float(getattr(account, "available_cash", 0.0) or 0.0),
        cash_buffer=cash_buffer,
        flow_budget=flow_budget,
        order_price=price,
        lot_size=buy_step,
    )
    if decision.quantity < min_buy_qty:
        _d_relay_pair_log(
            state, event="买入等待",
            note=(
                f"{decision.reason}；D释放未用{decision.released_cash_room:.2f}元，"
                f"本片流量预算{flow_budget:.2f}元"
            ),
        )
        return False
    order_quantity = min(decision.quantity, _max_sell_order_shares(ts_code))
    if order_quantity < min_buy_qty:
        return False
    if float(state.get("confirmed_buy_amount", 0.0) or 0.0) + order_quantity * price > hard_cap + 1e-6:
        _d_relay_block(state, "接力买单下单前85%硬顶断言失败")
        return False

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    confirm = str(config.get("live_trade", {}).get(
        "real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED"
    ))
    LiveOrderGateway(PROJECT_ROOT / "config" / "config.json").assert_real_order_allowed(
        confirm, side="BUY"
    )
    if not bool(config.get("live_trade", {}).get("allow_buy", False)):
        raise RuntimeError("live_trade.allow_buy=false，禁止D接力买入")

    slice_no = int(state.get("slice_no", 0) or 0)
    remark = f"D接力买-{state['date']}-{slice_no:02d}"
    pending = {
        "status": "PREPARED",
        "side": "BUY",
        "ts_code": ts_code,
        "quantity": order_quantity,
        "price": price,
        "open_price": float(getattr(quote, "open_price", 0.0) or 0.0),
        "remark": remark,
        "order_id": "",
        "applied": False,
        "created_at": now_beijing().isoformat(),
    }
    state["pending_buy"] = pending
    state["status"] = "BUY_CREDIT_PENDING"
    _d_relay_pair_save_state(state)
    request = OrderRequest(
        ts_code=ts_code,
        broker_code=ts_code,
        side="BUY",
        quantity=order_quantity,
        price_type="FIXED_PRICE",
        price=price,
        strategy_name="A_SYSTEM_D_RELAY_PAIR",
        remark=remark,
        strategy_leg=str(candidate.get("strategy_leg", "")).upper(),
        business_date=str(state.get("date", "")),
        signal_date=str(candidate.get("signal_date", "")),
        planned_exit_date=str(candidate.get("planned_exit_date", "")),
        purpose="OPEN",
        source_key=remark,
        metadata={"execution_channel": "D接力成对POV", "name": name},
    )
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        result = adapter.place_order(request)
    if not result.accepted:
        pending["status"] = "REJECTED"
        pending["message"] = str(result.message)
        state["pending_buy"] = None
        state["status"] = "ACTIVE"
        _d_relay_pair_save_state(state)
        _d_relay_pair_log(state, event="买入拒单", order_qty=order_quantity, note=str(result.message))
        logger().error("❌ [D接力成对POV] %s买单拒绝：%s", ts_code, result.message)
        return False
    pending["status"] = "SUBMITTED"
    pending["order_id"] = str(result.order_id or "")
    state["pending_buy"] = pending
    _d_relay_pair_save_state(state)
    ok = _d_relay_reconcile_pending_buy(state, broker_cfg, cancel_nonterminal=True)
    if ok and str(state.get("status", "")).upper() != "BLOCKED":
        state["status"] = "ACTIVE"
        _d_relay_pair_save_state(state)
    return ok


def _d_relay_submit_sell(
    state: dict[str, Any],
    broker_cfg: dict,
    *,
    quote: Any,
    requested_qty: int,
    limit_price: float,
    phase: str,
) -> bool:
    """在退出安全账本保护下提交一片D卖单。"""
    from src.broker_adapter import OrderRequest

    d = state["d"]
    ts_code = str(d.get("ts_code", ""))
    with _exit_sell_lock:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        _assert_exit_live_allowed(config)
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            broker_total, broker_can_use = _broker_position_quantities(adapter, ts_code)
            orders = adapter.query_orders()
        safe_qty = _safe_new_exit_order_quantity(
            ts_code,
            requested_qty=requested_qty,
            broker_total=broker_total,
            broker_can_use=broker_can_use,
            orders=orders,
            trade_date=state["date"],
            log=logger(),
            phase=phase,
            target_qty_override=int(d.get("initial_shares", 0) or 0),
        )
        safe_qty = min(safe_qty, _max_sell_order_shares(ts_code))
        safe_qty = _normalize_exit_slice_quantity(
            ts_code, safe_qty, max(int(d.get("remaining_shares", 0) or 0), 0)
        )
        if safe_qty <= 0:
            _d_relay_pair_log(state, event="卖出跳过", note=f"{phase}安全可卖量为0")
            return False
        remark = f"{_D_RELAY_SELL_REMARK_PREFIX}{state['date']}-{int(state.get('slice_no',0)):02d}"
        request = OrderRequest(
            ts_code=ts_code,
            broker_code=ts_code,
            side="SELL",
            quantity=safe_qty,
            price_type="FIXED_PRICE",
            price=limit_price,
            strategy_name="A_SYSTEM_D_RELAY_PAIR",
            remark=remark,
            strategy_leg="D",
            business_date=str(state.get("date", "")),
            signal_date=str(d.get("signal_date", "")),
            planned_exit_date=str(d.get("planned_exit_date", "")),
            purpose="CLOSE",
            source_key=remark,
            metadata={"execution_channel": phase, "name": d.get("name", "")},
        )
        pending = {
            "status": "PREPARED",
            "side": "SELL",
            "phase": phase,
            "ts_code": ts_code,
            "quantity": safe_qty,
            "price": limit_price,
            "remark": remark,
            "order_id": "",
            "intent_token": "",
            "applied": False,
            "created_at": now_beijing().isoformat(),
        }
        state["pending_sell"] = pending
        _d_relay_pair_save_state(state)
        # 卖出intent先于券商委托落盘；即使进程在place_order回报前崩溃，也会按
        # 全量未知占用阻止重复卖出。
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            result, token = _place_exit_order_with_intent(
                adapter, request, phase=phase,
                local_order_id=str(d.get("local_order_id", "")),
            )
        pending["status"] = "SUBMITTED" if result.accepted else "REJECTED"
        pending["order_id"] = str(result.order_id or "")
        pending["intent_token"] = token
        if not result.accepted:
            state["pending_sell"] = None
            _d_relay_pair_save_state(state)
            _d_relay_pair_log(state, event="卖出拒单", order_qty=safe_qty, note=str(result.message))
            return False
        state["pending_sell"] = pending
        _d_relay_pair_save_state(state)
    return True


def _d_relay_execute_pair_slice(state: dict[str, Any], broker_cfg: dict) -> None:
    """执行一轮：先消化已释放资金，再安全卖D一片并立即买候选一片。"""
    status = str(state.get("status", "")).upper()
    if status not in {"PREPARED", "AUCTION_SUBMITTED", "ACTIVE", "BUY_CREDIT_PENDING"}:
        return
    if str(state.get("account_fingerprint", "")) != _exit_account_fingerprint():
        _d_relay_block(state, "QMT账户与09:23接力任务绑定账户不一致，禁止继续")
        return
    if not _d_relay_reconcile_pending_sell(state, broker_cfg, cancel_nonterminal=True):
        return
    if not _d_relay_reconcile_pending_buy(state, broker_cfg, cancel_nonterminal=True):
        return
    if str(state.get("status", "")).upper() not in {"BLOCKED", "DONE"}:
        state["status"] = "ACTIVE"
        _d_relay_pair_save_state(state)

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    live_cfg = config.get("live_trade", {})
    d = state["d"]
    candidate = state["candidate"]
    d_code = str(d.get("ts_code", ""))
    next_code = str(candidate.get("ts_code", ""))
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        account = adapter.query_account()
        quote_map = adapter.get_full_tick([d_code, next_code])
    d_quote = quote_map.get(d_code)
    next_quote = quote_map.get(next_code)
    if next_quote is None:
        _d_relay_pair_log(state, event="本片跳过", note="候选行情为空")
        return

    buy_participation = float(live_cfg.get("d_relay_pair_buy_participation", live_cfg.get("pov_participation", 0.10)))
    sell_participation = float(live_cfg.get("d_relay_pair_sell_participation", 0.10))
    chase_cap = float(live_cfg.get("d_relay_pair_chase_cap", live_cfg.get("pov_chase_cap", 0.02)))
    cash_buffer = float(live_cfg.get("cash_buffer_amount", 1000.0))
    lot_size = int(live_cfg.get("round_lot_size", 100))
    buy_flow_budget = _d_relay_flow_budget(
        state, next_quote, key="next_prev_cum_amount", participation=buy_participation
    )
    # 有已确认卖出而尚未买回的资金时，必须先尝试买新仓，绝不继续卖D扩大裸露现金。
    outstanding_credit = max(
        float(state.get("confirmed_sell_amount", 0.0) or 0.0)
        - float(state.get("confirmed_buy_amount", 0.0) or 0.0),
        0.0,
    )
    next_price, gate_reason = relay_buy_limit_price(next_quote, chase_cap=chase_cap)
    next_short = next_code.upper().split(".")[0]
    next_min_buy_qty = 200 if next_short.startswith(("688", "689")) else lot_size
    if outstanding_credit >= max(next_price, 0.0) * next_min_buy_qty and next_price > 0:
        _d_relay_submit_buy(
            state, broker_cfg, account=account, quote=next_quote,
            flow_budget=buy_flow_budget, chase_cap=chase_cap,
            cash_buffer=cash_buffer, lot_size=lot_size,
        )
        _d_relay_pair_save_state(state)
        return
    if next_price <= 0:
        _d_relay_pair_log(state, event="停止新增卖D", note=gate_reason)
        return

    remaining = max(int(d.get("remaining_shares", 0) or 0), 0)
    target_room = max(
        float(state.get("target_amount", 0.0) or 0.0)
        - float(state.get("confirmed_buy_amount", 0.0) or 0.0),
        0.0,
    )
    if remaining <= 0 or target_room < next_price * next_min_buy_qty:
        return
    if d_quote is None:
        _d_relay_pair_log(state, event="本片跳过", note="D仍有余仓但行情为空")
        return

    depth_qty, d_limit_price, depth_note = _pov_depth_limit(
        d_quote,
        depth_levels=int(live_cfg.get("d_relay_pair_depth_levels", live_cfg.get("exit_pov_depth_levels", 3))),
        depth_haircut=float(live_cfg.get("d_relay_pair_depth_haircut", live_cfg.get("exit_pov_depth_haircut", 0.5))),
        max_slippage_bps=float(live_cfg.get("d_relay_pair_max_slippage_bps", live_cfg.get("exit_pov_max_slippage_bps", 20))),
        bid_volume_unit=int(live_cfg.get("exit_pov_bid_volume_unit", 1)),
    )
    if depth_qty <= 0 or d_limit_price <= 0:
        _d_relay_pair_log(state, event="卖出跳过", note=depth_note)
        return
    d_flow_budget = _d_relay_flow_budget(
        state, d_quote, key="d_prev_cum_amount", participation=sell_participation
    )
    max_slice_pct = min(max(float(live_cfg.get("d_relay_pair_max_slice_position_pct", 0.10)), 0.0), 1.0)
    max_slice_qty = relay_floor_lot(
        int(d.get("initial_shares", 0) or 0) * max_slice_pct, lot_size
    )
    # 卖D本片同时受D自身流量、买盘深度、候选可承接流量和单片仓位比例约束。
    # 这样即便候选流动性更弱，也不会先卖出一大块后长期买不回来。
    d_flow_qty = relay_floor_lot(d_flow_budget / d_limit_price, lot_size)
    candidate_room_amount = min(buy_flow_budget, target_room)
    candidate_match_qty = relay_floor_lot(candidate_room_amount / d_limit_price, lot_size)
    requested = min(remaining, depth_qty, d_flow_qty, candidate_match_qty, max_slice_qty)
    requested = relay_floor_lot(requested, lot_size)
    requested = _normalize_exit_slice_quantity(d_code, requested, remaining)
    if requested <= 0:
        _d_relay_pair_log(
            state, event="卖出等待",
            note=(f"本片不足一手：深度{depth_qty}、D流量{d_flow_qty}、"
                  f"候选承接{candidate_match_qty}、单片上限{max_slice_qty}"),
        )
        _d_relay_pair_save_state(state)
        return

    state["slice_no"] = int(state.get("slice_no", 0) or 0) + 1
    _d_relay_pair_save_state(state)
    if not _d_relay_submit_sell(
        state, broker_cfg, quote=d_quote, requested_qty=requested,
        limit_price=d_limit_price, phase=f"D接力成对POV片{state['slice_no']}",
    ):
        return
    if not _d_relay_reconcile_pending_sell(state, broker_cfg, cancel_nonterminal=True):
        return
    # 卖出真实成交后重新查询QMT可用余额；绝不使用卖出前余额倒推，防止多1元废单。
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        account_after_sell = adapter.query_account()
        next_quote_after = adapter.get_full_tick([next_code]).get(next_code)
    if next_quote_after is None:
        _d_relay_pair_log(state, event="买入等待", note="卖D后候选行情为空，保留已释放资金下片重试")
        return
    _d_relay_submit_buy(
        state, broker_cfg, account=account_after_sell, quote=next_quote_after,
        flow_budget=buy_flow_budget, chase_cap=chase_cap,
        cash_buffer=cash_buffer, lot_size=lot_size,
    )
    if str(state.get("status", "")).upper() != "BLOCKED":
        state["status"] = "ACTIVE"
        _d_relay_pair_save_state(state)


def _d_relay_finalize(state: dict[str, Any], reason: str) -> None:
    """10:30收口：D余仓继续持有至原平仓日，不再强行追价接力。"""
    if str(state.get("status", "")).upper() == "BLOCKED":
        return
    state["status"] = "DONE"
    state["done_reason"] = reason
    state["completed_at"] = now_beijing().isoformat()
    _d_relay_pair_save_state(state)
    d = state.get("d", {})
    candidate = state.get("candidate", {})
    sell_amount = float(state.get("confirmed_sell_amount", 0.0) or 0.0)
    buy_amount = float(state.get("confirmed_buy_amount", 0.0) or 0.0)
    _d_relay_pair_log(state, event="接力收口", note=reason)
    _notify(
        "buy_result", "✅ D接力成对POV已收口",
        f"{d.get('ts_code','')}→{candidate.get('ts_code','')}：D确认卖出{_fmt_wan(sell_amount)}，"
        f"新仓确认买入{_fmt_wan(buy_amount)}，D剩余{int(d.get('remaining_shares',0) or 0)}股。"
        f"{reason}；未使用原有空闲现金，未突破85%单票硬顶。",
    )


def _d_relay_pair_worker() -> None:
    """09:30:10—10:30每5分钟执行一轮资金中性D接力。"""
    try:
        state = _d_relay_pair_load_state()
        if not state or str(state.get("status", "")).upper() not in {
            "ACTIVE", "BUY_CREDIT_PENDING", "AUCTION_SUBMITTED", "PREPARED"
        }:
            return
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        broker_cfg = config.get("broker", {})
        live_cfg = config.get("live_trade", {})
        cadence = max(int(live_cfg.get("d_relay_pair_cadence_sec", 300)), 60)
        base_dt = now_beijing().replace(hour=9, minute=30, second=10, microsecond=0)
        end_dt = now_beijing().replace(hour=10, minute=30, second=0, microsecond=0)
        while now_beijing() < base_dt:
            time.sleep(min((base_dt - now_beijing()).total_seconds(), 60))
        logger().warning(
            "[D接力成对POV] 启动：%s→%s，每%d秒一片，卖D确认后才买新仓。",
            state.get("d", {}).get("ts_code", ""),
            state.get("candidate", {}).get("ts_code", ""), cadence,
        )
        while True:
            state = _d_relay_pair_load_state()
            if str(state.get("status", "")).upper() in {"DONE", "BLOCKED"}:
                return
            if now_beijing() >= end_dt:
                _d_relay_reconcile_pending_sell(state, broker_cfg, cancel_nonterminal=True)
                _d_relay_reconcile_pending_buy(state, broker_cfg, cancel_nonterminal=True)
                if str(state.get("status", "")).upper() != "BLOCKED":
                    _d_relay_finalize(state, "10:30到时收口，停止追价；D余仓保留至原计划平仓日")
                return
            _d_relay_execute_pair_slice(state, broker_cfg)
            next_run = now_beijing() + datetime.timedelta(seconds=cadence)
            while now_beijing() < next_run:
                time.sleep(min((next_run - now_beijing()).total_seconds(), 60))
    except Exception as exc:
        logger().exception("D接力成对POV线程异常：%s", exc)
        try:
            state = _d_relay_pair_load_state()
            if state:
                _d_relay_block(state, f"执行线程异常:{exc}")
        except Exception:
            _notify(
                "system_error", "🛑 D接力线程与状态同时异常",
                f"{exc}。系统已停止继续下单，请立即核对QMT委托、持仓和本地状态文件。",
                level="critical", call=True,
            )


def _start_d_relay_pair_worker() -> None:
    global _d_relay_pair_thread
    with _d_relay_pair_thread_lock:
        if _d_relay_pair_thread is not None and _d_relay_pair_thread.is_alive():
            return
        _d_relay_pair_thread = threading.Thread(
            target=_d_relay_pair_worker, daemon=True, name="d-relay-paired-pov"
        )
        _d_relay_pair_thread.start()


def job_premarket_sell() -> None:
    """09:23 集合竞价：D接力只卖安全部分；历史sell_pending仍按原应急路径。

    D 默认平仓口径是 T+2 收盘卖，不在 09:23 提前卖。
    只有组合状态机给出 PLAN_SELL_D_FIRST（次日有 A/C/E 接力，需要 D 让路）时，
    才建立资金中性接力任务：竞价安全部分先卖，09:30后卖D一片、确认成交资金后
    再买候选一片。禁止再用跌停价整仓D直接冲击开盘集合竞价。
    E/ABC/D默认T+2收盘卖由 14:55 job_afternoon/check_and_close_positions 执行。
    """
    logger().info("===== 集合竞价平仓挂单（09:23）=====")
    # 平仓检查冗余点②：09:20被挤掉时由此兜底（见 job_premarket_buy 处注释）
    try:
        check_and_close_positions()
    except Exception as e:
        logger().error("09:23 平仓检查异常：%s", e)

    # 09:25之后已不是开盘集合竞价申报语义。若09:20任务拖慢导致本节点迟到，
    # 保留D原仓并让09:30后的成对/常规路径重新判断，禁止把跌停限价整仓单
    # 塞进静默段。逾期持仓已由上面的通用平仓检查优先处理并独立告警。
    if now_beijing().time() >= datetime.time(9, 25):
        had_plan = _has_premarket_close_plan()
        logger().error(
            "⛔ 09:23集合竞价平仓任务执行时已到%s，竞价窗口已关闭；"
            "不提交D接力/历史竞价卖单。",
            now_beijing().strftime("%H:%M:%S"),
        )
        if had_plan:
            _notify(
                "sell_fail",
                "⚠️ 09:23集合竞价平仓窗口已错过",
                "系统保留D原仓，未退回不安全的整仓跌停价竞价卖出；"
                "本日不再强行建立D接力，D继续按原计划平仓日管理，请留意后续通知。",
                level="timeSensitive",
            )
        return
    positions = load_positions()
    if not positions:
        logger().info("09:23 无持仓，跳过集合竞价平仓。")
        return

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))
    today_str = today_beijing().strftime("%Y%m%d")
    force_d_sell_codes: set[str] = set()
    combined = load_combined_decisions()
    decisions = combined[0] if combined is not None else None
    if decisions is not None and not decisions.empty and {"action", "strategy_leg"}.issubset(decisions.columns):
        rows = decisions[
            (decisions["action"].astype(str) == "PLAN_SELL_D_FIRST")
            & (decisions["strategy_leg"].astype(str).str.upper() == "D")
        ]
        force_d_sell_codes = set(rows.get("ts_code", []).dropna().astype(str).tolist())

    for pos in positions:
        try:
            if pos.get("status") == "closed":
                continue
            ts_code = pos.get("ts_code", "")
            name    = pos.get("name", "")
            shares  = int(pos.get("shares", 0))
            planned_exit = pos.get("planned_exit_date", "99991231")
            strategy_leg = str(pos.get("strategy_leg", "")).upper()

            # 只处理 D 策略：E/ABC 回测用收盘价，不在集合竞价提前卖出
            if strategy_leg != "D":
                logger().info(
                    "09:23 %s %s 策略=%s，回测用收盘价平仓，跳过集合竞价，等待14:55收盘平仓。",
                    ts_code, name, strategy_leg or "未知",
                )
                continue

            force_relay_sell = ts_code in force_d_sell_codes

            # 只处理历史 sell_pending，或因A/C/E接力需要T+1开盘先卖的D持仓。
            # D 默认 T+2 到期日也必须等 14:55 收盘平仓，不在09:23提前卖。
            if pos.get("status") != "sell_pending" and not force_relay_sell:
                if planned_exit <= today_str:
                    logger().info(
                        "09:23 D默认T+2平仓：%s %s 今日到期(%s)，等待14:55收盘平仓，不集合竞价卖出。",
                        ts_code, name, planned_exit,
                    )
                else:
                    logger().info("09:23 持仓 %s %s 计划平仓日 %s，今日无需平仓，跳过。", ts_code, name, planned_exit)
                continue
            if force_relay_sell and planned_exit > today_str:
                logger().warning(
                    "09:23 D接力准备：%s %s 默认计划平仓日%s，但今日有A/C/E接力候选；只卖竞价安全部分，其余09:30后成对POV。",
                    ts_code, name, planned_exit,
                )

            if not qmt_enabled:
                logger().info("[集合竞价平仓] 模拟盘：%s %s 将在开盘时平仓", ts_code, name)
                continue

            broker_cfg = config.get("broker", {})

            if force_relay_sell:
                # D接力必须先锁定唯一候选。计划缺失或多候选时宁可继续持有D，
                # 也不能先卖出后才临时决定买什么。
                try:
                    candidate = _load_d_relay_candidate_order()
                    if candidate is None:
                        raise RuntimeError("组合计划没有唯一PLAN_D_RELAY_PAIRED_BUY候选")
                    with _qmt_lock:
                        adapter = _qmt_get(broker_cfg)
                        account = adapter.query_account()
                        broker_total, broker_can_use = _broker_position_quantities(adapter, ts_code)
                        quote = adapter.get_full_tick([ts_code]).get(ts_code)
                    state = _d_relay_prepare_state(
                        pos, candidate, account=account,
                        broker_total=broker_total, broker_can_use=broker_can_use,
                    )
                    state_status = str(state.get("status", "")).upper()
                    if state_status in {"DONE", "BLOCKED"}:
                        logger().warning(
                            "[D接力] 当日任务已经%s（%s），09:23重复触发不再提交任何委托。",
                            state_status, state.get("done_reason", state.get("blocked_reason", "")),
                        )
                        continue
                    pending_sell = state.get("pending_sell")
                    if isinstance(pending_sell, dict) and str(pending_sell.get("status", "")).upper() != "RESOLVED":
                        logger().warning(
                            "[D接力] 已有未终态卖单%s，09:23重复触发不叠单。",
                            pending_sell.get("order_id", "PREPARED"),
                        )
                        continue
                    if bool(state.get("auction_decided", False)):
                        logger().warning("[D接力] 09:23竞价容量已经判定，重复触发不再提交第二张竞价卖单。")
                        continue
                    if _ts_code_aliases(ts_code) & _ts_code_aliases(candidate.get("ts_code", "")):
                        _handoff_same_stock_d_relay(state)
                        logger().warning(
                            "✅ [D接力] %s与候选同票，已直接切换策略归属，不提交卖出/买入委托。",
                            ts_code,
                        )
                        continue

                    live_cfg = config.get("live_trade", {})
                    if not bool(live_cfg.get("d_relay_paired_pov_enabled", False)):
                        raise RuntimeError("d_relay_paired_pov_enabled=false，禁止退回旧版整仓竞价卖D")
                    auction_decision = calculate_auction_safe_sell_quantity(
                        quote,
                        position_shares=shares,
                        max_participation=float(live_cfg.get("d_relay_auction_max_participation", 0.025)),
                        max_unmatched_sell_ratio=float(live_cfg.get("d_relay_auction_max_unmatched_sell_ratio", 0.05)),
                        book_volume_unit=int(live_cfg.get("d_relay_auction_book_volume_unit", 1)),
                        lot_size=int(live_cfg.get("round_lot_size", 100)),
                        max_reference_spread_pct=float(live_cfg.get("d_relay_auction_max_reference_spread_pct", 0.001)),
                    )
                    auction_safe_qty = _normalize_exit_slice_quantity(
                        ts_code, auction_decision.quantity, shares
                    )
                    _d_relay_pair_log(
                        state, event="竞价容量判定", order_qty=auction_safe_qty,
                        fill_price=auction_decision.reference_price,
                        note=(f"{auction_decision.reason}；匹配量{auction_decision.matched_quantity}股，"
                              f"卖方失衡{auction_decision.unmatched_sell_ratio:.2%}"),
                    )
                    state["auction_decided"] = True
                    if auction_safe_qty <= 0:
                        state["status"] = "ACTIVE"
                        state["auction_skip_reason"] = auction_decision.reason
                        _d_relay_pair_save_state(state)
                        logger().warning(
                            "🛡️ [D接力竞价保护] %s 本次竞价安全卖量为0（%s）；不卖D，"
                            "09:30后仅按盘口与流量做成对POV。",
                            ts_code, auction_decision.reason,
                        )
                        continue
                    lower_limit = float(getattr(quote, "lower_limit", 0.0) or 0.0) if quote else 0.0
                    if lower_limit <= 0:
                        state["status"] = "ACTIVE"
                        state["auction_skip_reason"] = "跌停申报价缺失"
                        _d_relay_pair_save_state(state)
                        logger().warning(
                            "🛡️ [D接力竞价保护] %s 跌停申报价缺失，竞价不提交；09:30后成对POV。",
                            ts_code,
                        )
                        continue
                    submitted = _d_relay_submit_sell(
                        state, broker_cfg, quote=quote,
                        requested_qty=auction_safe_qty,
                        limit_price=lower_limit, phase="D接力竞价安全部分",
                    )
                    state["status"] = "AUCTION_SUBMITTED" if submitted else "ACTIVE"
                    _d_relay_pair_save_state(state)
                    if submitted:
                        submitted_qty = int((state.get("pending_sell") or {}).get("quantity", 0) or 0)
                        logger().warning(
                            "⏳ [D接力竞价安全部分] %s %s 仅提交%d/%d股@跌停申报价%.2f；"
                            "实际按开盘撮合价成交，09:26确认后才计入可买资金。",
                            ts_code, name, submitted_qty, shares, lower_limit,
                        )
                    else:
                        logger().warning(
                            "[D接力竞价安全部分] %s 未提交成功，保留D，09:30后成对POV继续评估。",
                            ts_code,
                        )
                except Exception as exc:
                    logger().exception("D接力竞价准备失败（%s）：%s", ts_code, exc)
                    try:
                        state = _d_relay_pair_load_state()
                        if state:
                            _d_relay_block(state, f"09:23竞价准备失败:{exc}")
                    except Exception:
                        _notify(
                            "sell_fail", "🛑 D接力竞价准备失败",
                            f"{ts_code} {name}：{exc}。未执行旧版整仓卖出，请立即核对。",
                            level="critical", call=True,
                        )
                continue

            with _qmt_lock:
                adapter   = _qmt_get(broker_cfg)
                quote_map = adapter.get_full_tick([ts_code])

            quote       = quote_map.get(ts_code)
            lower_limit = float(getattr(quote, "lower_limit", 0.0) or 0.0) if quote else 0.0

            if lower_limit <= 0:
                logger().warning("09:22 %s %s 无法获取跌停价，跳过集合竞价平仓，等待09:30。", ts_code, name)
                continue

            logger().warning("⏳ [集合竞价平仓] %s %s  %d股  跌停价%.2f元", ts_code, name, shares, lower_limit)

            from src.broker_adapter import OrderRequest
            request = OrderRequest(
                ts_code=ts_code,
                broker_code=ts_code,
                side="SELL",
                quantity=shares,
                price_type="FIXED_PRICE",
                price=lower_limit,
                strategy_name="A_SYSTEM_PREMARKET_SELL",
                remark=f"集合竞价平仓-{today_str}",
                strategy_leg=strategy_leg,
                business_date=today_str,
                signal_date=str(pos.get("signal_date", "")),
                planned_exit_date=str(pos.get("planned_exit_date", "")),
                purpose="CLOSE",
                source_key=(
                    f"PREMARKET_CLOSE|{today_str}|{pos.get('order_id', '')}|{ts_code}"
                ),
                metadata={"execution_channel": "集合竞价平仓", "name": name},
            )
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                result  = adapter.place_order(request)

            if result.accepted:
                # 集合竞价9:25才撮合，此处不立即标记平仓，由09:26持仓同步按实盘实际成交确认
                logger().info(
                    "✅ [集合竞价平仓] %s %s 委托已受理（order_id=%s），等待09:25撮合，09:26按实盘确认。",
                    ts_code, name, result.order_id,
                )
            else:
                logger().error("❌ [集合竞价平仓] %s %s 提交失败：%s，等待后续平仓任务处理。", ts_code, name, result.message)

        except Exception as e:
            logger().error("集合竞价平仓异常（%s）：%s", pos.get("ts_code"), e)

    logger().info("===== 集合竞价平仓挂单完成 =====")


def job_strategy_d(decisions=None) -> None:
    """09:20 盘前任务后立即启动策略D监控（后台线程，不阻塞 daemon）。

    架构原则：QMT 连接只能由主守护进程持有。D监控在线程内运行，
    通过 SharedQMTBrokerProxy 复用主连接，禁止再启动独立 QMT 子进程。
    监控脚本内部从09:30开始完整跟踪；买入时点由当前正式D发布条件决定，
    旧版D为14:00后回封，因子版D由正式发布中的reseal_time_bucket自动决定
    最后扫描分钟；已有活动委托仍持续确认并在14:55撤销未成交残单。
    """
    global _d_monitor_thread
    logger().info("===== 策略D监控启动（盘中后台）=====")
    try:
        if _strategy_d_monitor_running():
            logger().info("策略D监控已在运行，跳过重复启动。")
            return

        gate_decisions = decisions
        if gate_decisions is None:
            cached = read_cached_combined_decisions()
            gate_decisions = cached[0] if cached is not None else None
        if gate_decisions is None:
            logger().error("D监控缺少当日组合状态机决策，禁止启动，避免绕过全日互斥门禁。")
            return

        monitor_allowed, monitor_gate_reason = d_intraday_monitor_gate(gate_decisions)
        if not monitor_allowed:
            logger().info("D全活动总门未通过，监控线程不启动：%s", monitor_gate_reason)
            return

        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        broker_config = config.get("broker", {})
        qmt_ready = (
            bool(config.get("broker_adapter_enabled"))
            and bool(config.get("qmt_enabled"))
            and bool(broker_config.get("enabled", False))
            and str(broker_config.get("adapter", "")).lower() == "qmt"
        )
        if not qmt_ready:
            logger().warning("QMT行情未启用，跳过D盘中监控；D策略需要实时行情。")
            return
        live_order = bool(config.get("trade_mode", "").lower() == "live" and config.get("live_trade", {}).get("enabled"))

        import importlib.util

        d_module_path = PROJECT_ROOT / "scripts" / "monitor_strategy_d_intraday.py"
        d_spec = importlib.util.spec_from_file_location("a_system_strategy_d_monitor", d_module_path)
        if d_spec is None or d_spec.loader is None:
            raise RuntimeError(f"无法加载D监控模块：{d_module_path}")
        d_module = importlib.util.module_from_spec(d_spec)
        sys.modules[d_spec.name] = d_module
        d_spec.loader.exec_module(d_module)
        StrategyDMonitor = d_module.StrategyDMonitor
        configured_allowed_segments = d_module.configured_allowed_segments
        configured_position_pct = d_module.configured_position_pct

        today_str = today_beijing().strftime("%Y%m%d")
        signal_dir = PROJECT_ROOT / "reports" / "strategy_d"
        mkdir_p(signal_dir)
        signal_csv = signal_dir / f"intraday_signals_{today_str}.csv"

        def _entry_gate() -> tuple[bool, str]:
            return d_intraday_entry_gate(gate_decisions)

        def _tracking_gate() -> tuple[bool, str]:
            return d_intraday_monitor_gate(gate_decisions)

        def _record_d_position(payload: dict[str, Any]) -> None:
            _record_live_buy(
                recovery_context="D盘中买入",
                order_id=str(payload["order_id"]),
                ts_code=str(payload["ts_code"]),
                name=str(payload.get("name", "")),
                signal_date=str(payload.get("signal_date", today_str)),
                buy_date=str(payload.get("buy_date", today_str)),
                shares=int(payload["shares"]),
                buy_price=float(payload["buy_price"]),
                strategy_leg="D",
                exit_n_days=2,
                execution_channel="D盘中买入",
                planned_order_qty=int(payload.get("planned_order_qty", payload["shares"])),
                planned_exit_date_override=str(payload.get("planned_exit_date", "")),
            )

        monitor = StrategyDMonitor(
            broker=SharedQMTBrokerProxy(broker_config),
            live_order=live_order,
            logger=logger(),
            signal_csv=signal_csv,
            allowed_segments=configured_allowed_segments(config),
            position_pct=configured_position_pct(config),
            config=config,
            position_recorder=_record_d_position,
            entry_gate=_entry_gate,
            tracking_gate=_tracking_gate,
        )

        def _run_monitor() -> None:
            retry_count = 0
            while True:
                try:
                    monitor.run()
                    return
                except Exception as exc:
                    retry_count += 1
                    logger().exception(
                        "D监控线程第%d次异常：%s", retry_count, exc
                    )
                    if monitor.order_placed or monitor.session_orders:
                        # 券商委托可能已经受理/成交，不能仅重启D子线程。
                        # 结束整个daemon，让新进程先重放意图并投影持仓。
                        reason = f"D委托后本地处理异常:{exc}"
                        _request_process_recovery(
                            reason=reason,
                            exit_code=EXIT_CODE_D_POSITION_RECOVERY,
                            heartbeat_status="d_order_recovery_restarting",
                            title="🔄 D委托状态待恢复，程序自动重启",
                            body=(
                                f"{reason}。新进程会先根据QMT委托、成交和持仓恢复，"
                                "不会重复买入。"
                            ),
                        )
                        return
                    if (
                        retry_count >= 3
                        or now_beijing().time() >= datetime.time(14, 55)
                        or monitor.path_integrity_failed
                    ):
                        _notify(
                            "system_error",
                            "⛔ D监控已按安全口径停止",
                            f"尚未下单，但监控连续{retry_count}次异常：{exc}。"
                            "今日D不再开仓；其他策略和平仓线程继续运行。",
                            level="critical",
                            call=True,
                        )
                        return
                    logger().warning(
                        "D监控将在5秒后用同一内存状态续跑（%d/3）。",
                        retry_count,
                    )
                    time.sleep(5.0)

        _d_monitor_thread = threading.Thread(
            target=_run_monitor,
            daemon=True,
            name="strategy-d-monitor",
        )
        _d_monitor_thread.start()
        if D_MONITOR_PID_FILE.exists():
            D_MONITOR_PID_FILE.unlink(missing_ok=True)
        logger().info("策略D监控已在线程内启动（live_order=%s，QMT连接复用主守护进程）", live_order)
    except Exception as e:
        logger().error("策略D监控启动失败：%s", e)
    logger().info("===== 策略D监控已在线程内运行，不阻塞主程序 =====")


def stop_strategy_d_monitor(reason: str = "") -> None:
    """停止 D 盘中监控。

    新架构下 D 监控是 daemon 线程，跟随主守护进程退出；这里主要兼容清理旧版子进程PID。
    """
    try:
        if _d_monitor_thread is not None and _d_monitor_thread.is_alive():
            logger().info("D策略监控为主进程内线程，将随主守护进程退出。%s", f"原因：{reason}" if reason else "")
        if not D_MONITOR_PID_FILE.exists():
            return
        pid_text = D_MONITOR_PID_FILE.read_text(encoding="utf-8").strip()
        D_MONITOR_PID_FILE.unlink(missing_ok=True)
        if not pid_text:
            return
        pid = int(pid_text)
        if not _pid_is_running(pid):
            logger().info("D策略监控已不在运行，清理PID记录。")
            return
        if sys.platform.startswith("win"):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:
            os.kill(pid, signal.SIGTERM)
        logger().info("D策略监控已关闭（PID %s）%s", pid, f"原因：{reason}" if reason else "")
    except Exception as exc:
        logger().warning("关闭D策略监控失败：%s", exc)


def clear_strategy_d_intraday_transient_data(
    reason: str,
    *,
    preserve_trade_date: str = "",
) -> int:
    """删除D盘中路径恢复文件，保证下一交易日不继承前一日状态。"""

    try:
        removed = clear_strategy_d_intraday_checkpoints(
            PROJECT_ROOT,
            preserve_trade_date=preserve_trade_date,
        )
    except Exception as exc:
        logger().error("D盘中临时数据清理失败（%s）：%s", reason, exc)
        return -1
    logger().info(
        "D盘中临时数据清理完成（%s）：删除%d个路径检查点；"
        "%s成交、持仓、信号CSV和运行日志继续保留。",
        reason,
        len(removed),
        (
            f"当天{preserve_trade_date}恢复检查点仍保留；"
            if preserve_trade_date
            else ""
        ),
    )
    return len(removed)


def _start_d_monitor_log_forwarder(proc: subprocess.Popen) -> None:
    """异步转发 D 扫描进程输出到主日志，避免子进程输出阻塞。"""
    def _forward() -> None:
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                text = line.rstrip()
                if text:
                    logger().info("[D扫描] %s", text)
            code = proc.wait(timeout=5)
            logger().info("[D扫描] 进程退出，PID=%s exit_code=%s", proc.pid, code)
        except Exception as exc:
            logger().warning("[D扫描] 输出转发异常：%s", exc)

    threading.Thread(
        target=_forward,
        daemon=True,
        name=f"d-monitor-log-forwarder-{proc.pid}",
    ).start()


def _start_d_monitor_health_probe(proc: subprocess.Popen) -> None:
    """启动后短延迟检查，确认 D 子进程没有静默退出。"""
    def _probe() -> None:
        time.sleep(5)
        code = proc.poll()
        if code is None:
            logger().info("[D扫描] 启动健康检查：进程仍在运行，PID=%s", proc.pid)
        else:
            logger().warning("[D扫描] 启动健康检查：进程已退出，PID=%s exit_code=%s", proc.pid, code)

    threading.Thread(
        target=_probe,
        daemon=True,
        name=f"d-monitor-health-probe-{proc.pid}",
    ).start()


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return str(pid) in output and "INFO:" not in output.upper()
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _strategy_d_monitor_running() -> bool:
    try:
        if _d_monitor_thread is not None and _d_monitor_thread.is_alive():
            return True
        if not D_MONITOR_PID_FILE.exists():
            return False
        pid_text = D_MONITOR_PID_FILE.read_text(encoding="utf-8").strip()
        pid = int(pid_text)
        if _pid_is_running(pid):
            return True
        D_MONITOR_PID_FILE.unlink(missing_ok=True)
    except Exception:
        return False
    return False


_d_monitor_thread: threading.Thread | None = None  # D监控线程；D不再独立连接QMT
EXIT_CODE_D_POSITION_RECOVERY = 89
