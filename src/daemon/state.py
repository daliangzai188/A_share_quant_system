"""交易状态文件：持仓（positions.json）、待确认买单、开仓POV/D接力/M执行状态、退出执行账本、
统一交易意图。读写失败一律按进程级恢复处理，不能在失忆状态下继续交易。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from src.utils.config import load_json_config, mkdir_p
from src.utils.time_utils import BEIJING_TZ, now_beijing, today_beijing
from src.trade_intent_store import TradeIntentStore
from src.strategy_identity import normalize_strategy_leg, normalize_strategy_record
from src.portfolio_ace_m import position_in_family
from src.daemon.common import (
    D_RELAY_PAIR_STATE_FILE,
    EXIT_EXECUTION_STATE_FILE,
    PENDING_BUY_FILE,
    POSITIONS_FILE,
    PROJECT_ROOT,
    SCHED_AFTERNOON_CLOSE,
    _critical_execution_state_failure,
    _d_relay_pair_state_lock,
    _execution_time_only,
    _exit_execution_state_lock,
    _normalize_ts_code,
    _notify,
    _pending_buy_lock,
    _positions_file_lock,
    _track_execution,
    _ts_code_aliases,
    logger,
    next_n_trade_days,
)


# ── 持仓状态文件 ──────────────────────────────────────────────────────────────

def load_positions() -> list[dict[str, Any]]:
    with _positions_file_lock:
        try:
            if not POSITIONS_FILE.exists():
                return []
            payload = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
            if not isinstance(payload, list) or any(
                not isinstance(row, dict) for row in payload
            ):
                raise ValueError("positions根节点必须是dict列表")
            return [normalize_strategy_record(row) for row in payload]
        except Exception as e:
            logger().error("读取持仓文件失败：%s", e)
            # 文件不存在才代表“本地无持仓记录”；已存在但损坏/不可读
            # 绝不能伪装成空仓，否则会误放行新开仓并让平仓链失明。
            raise RuntimeError(f"读取持仓文件失败:{e}") from e


def save_positions(positions: list[dict[str, Any]]) -> None:
    with _positions_file_lock:
        try:
            mkdir_p(POSITIONS_FILE.parent)
            tmp = POSITIONS_FILE.with_suffix(".tmp")
            normalized = [normalize_strategy_record(row) for row in positions]
            tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(POSITIONS_FILE)  # 原子替换，防止写一半损坏
        except Exception as e:
            logger().error("保存持仓文件失败：%s", e)
            # 持仓回写是平仓成功的一部分，不能吞掉异常后继续
            # 发“已平仓”通知。向上抛出后由调用方停止成功流程并告警。
            raise RuntimeError(f"保存持仓文件失败:{e}") from e


_ACTIVE_AUTO_STRATEGY_LEGS = frozenset({"A", "C", "D", "E", "M"})


def _position_exit_ledger_quantity(position: dict[str, Any]) -> int:
    """返回本地已经明确回写的退出成交量。"""

    total = 0
    fills = position.get("exit_fills_by_date")
    if isinstance(fills, dict):
        for value in fills.values():
            if not isinstance(value, dict):
                continue
            try:
                total += max(int(value.get("qty", 0) or 0), 0)
            except (TypeError, ValueError):
                continue
    return total


def _is_recent_auto_ghost_clear(
    position: dict[str, Any],
    *,
    as_of_date: str = "",
    max_age_days: int = 3,
) -> bool:
    """识别可以继续作为策略身份证据的近期自动误清候选。"""

    if str(position.get("status", "")).lower() != "closed":
        return False
    if str(position.get("strategy_leg", "")).upper() not in _ACTIVE_AUTO_STRATEGY_LEGS:
        return False
    if bool(position.get("manual_exit_only", False)) or bool(
        position.get("auto_exit_disabled", False)
    ):
        return False
    if position.get("manual_exit_evidence") or position.get("manual_sell_note"):
        return False
    try:
        shares = max(int(position.get("shares", 0) or 0), 0)
        sell_price = max(float(position.get("sell_price", 0.0) or 0.0), 0.0)
    except (TypeError, ValueError):
        return False
    if shares <= 0 or sell_price > 0 or _position_exit_ledger_quantity(position) > 0:
        return False
    cleared_at = str(position.get("ghost_cleared_at", "") or "")
    clear_reason = str(position.get("ghost_clear_reason", "") or "")
    clear_source = str(position.get("ghost_clear_source", "") or "")
    if not cleared_at or not (
        "QMT" in clear_reason
        or "券商连续" in clear_reason
        or "账户心跳" in clear_source
        or "逐票同步" in clear_source
    ):
        return False
    try:
        ghost_date = datetime.datetime.strptime(cleared_at[:10], "%Y-%m-%d").date()
        check_text = str(as_of_date or today_beijing().strftime("%Y%m%d")).replace("-", "")[:8]
        check_date = datetime.datetime.strptime(check_text, "%Y%m%d").date()
    except (TypeError, ValueError):
        return False
    age = (check_date - ghost_date).days
    return 0 <= age <= max(int(max_age_days), 0)


def _strategy_identity_aliases(
    *,
    include_recent_ghost: bool = False,
    preexisting_on: str = "",
    family: str | None = None,
) -> set[str]:
    """返回本系统策略持仓的代码身份集。

    family：None=全部策略；"ACE"=A/C/E/D（不含M）；"M"=只看M（2026-11-12 ACE+M 并行持仓）。
    """

    check_date = str(preexisting_on or "").replace("-", "")[:8]
    aliases: set[str] = set()
    for position in load_positions():
        if not position_in_family(position, family):
            continue
        status = str(position.get("status", "")).lower()
        active = status in {"open", "sell_pending"}
        recent_ghost = include_recent_ghost and _is_recent_auto_ghost_clear(
            position,
            as_of_date=check_date,
        )
        if not active and not recent_ghost:
            continue
        if check_date:
            buy_date = "".join(
                char for char in str(position.get("buy_date", "")) if char.isdigit()
            )[:8]
            if buy_date == check_date:
                continue
        aliases.update(_ts_code_aliases(position.get("ts_code", "")))
    return aliases


def restore_ghost_cleared_strategy_positions(
    broker_positions: Any,
    source: str,
) -> int:
    """用券商真实股数恢复刚被自动误清的策略仓身份。

    只在「同代码券商股数 - 仍然正常open的本地股数」与近期ghost候选股数
    完全相等时恢复。任何部分数量、多条归属或人工成交疑点都不猜测。
    """

    broker_qty: dict[str, int] = {}
    for raw in broker_positions or []:
        try:
            qty = max(int(getattr(raw, "volume", 0) or 0), 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        key = _normalize_ts_code(getattr(raw, "ts_code", ""))
        if key:
            broker_qty[key] = broker_qty.get(key, 0) + qty
    if not broker_qty:
        return 0

    restored = 0
    restored_desc: list[str] = []
    with _positions_file_lock:
        positions = load_positions()
        active_qty: dict[str, int] = {}
        candidates: dict[str, list[dict[str, Any]]] = {}
        for position in positions:
            key = _normalize_ts_code(position.get("ts_code", ""))
            if not key:
                continue
            if str(position.get("status", "")).lower() in {"open", "sell_pending"}:
                try:
                    active_qty[key] = active_qty.get(key, 0) + max(
                        int(position.get("shares", 0) or 0), 0
                    )
                except (TypeError, ValueError):
                    continue
            elif _is_recent_auto_ghost_clear(position):
                candidates.setdefault(key, []).append(position)

        now_text = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
        for key, rows in candidates.items():
            available = max(broker_qty.get(key, 0) - active_qty.get(key, 0), 0)
            candidate_qty = sum(max(int(row.get("shares", 0) or 0), 0) for row in rows)
            if available <= 0 or candidate_qty != available:
                if broker_qty.get(key, 0) > 0:
                    logger().error(
                        "🛑 [策略仓恢复] %s 券商未归属%d股与近期误清候选%d股不一致，"
                        "禁止自动猜测（来源=%s）。",
                        key, available, candidate_qty, source,
                    )
                continue
            for row in rows:
                row["status"] = "open"
                row["sell_date"] = None
                row["sell_price"] = None
                row["ghost_restored_at"] = now_text
                row["ghost_restore_source"] = source
                row["ghost_restore_reason"] = "券商真实持仓数量与近期自动误清记录完全匹配"
                restored += 1
                restored_desc.append(
                    f"{row.get('strategy_leg', '')} {row.get('ts_code', '')} "
                    f"{row.get('name', '')} {int(row.get('shares', 0) or 0)}股"
                )
        if restored:
            save_positions(positions)

    if restored:
        restored_text = "、".join(restored_desc)
        logger().critical(
            "✅ [策略仓恢复] 已按券商真实持仓恢复本地策略身份：%s（来源=%s）。",
            restored_text, source,
        )
        _notify(
            "system_error",
            "✅ 误清策略仓已自动恢复",
            f"{restored_text}。系统已恢复原策略归属、旧仓阻断和原计划平仓链路。",
            level="critical",
        )
    return restored


def _recover_sell_price_from_fills(pos: dict[str, Any]) -> float:
    """幽灵清理时尽力还原真实卖出均价，避免把 sell_price 写成 0。

    卖出分片会把每天的成交量与成交额记进 exit_fills_by_date，这里按
    总成交额/总成交量算加权均价。2026-06~08 已有乔治白、兴欣新材、普联软件
    三笔因写 0 而无法复盘，每次都要事后用日线收盘价手工补。

    只影响账本记录与事后复盘，不参与任何交易决策或下单。
    """

    existing_price = 0.0
    try:
        existing_price = max(float(pos.get("sell_price", 0.0) or 0.0), 0.0)
    except (TypeError, ValueError):
        existing_price = 0.0
    fills = pos.get("exit_fills_by_date")
    if not isinstance(fills, dict):
        return existing_price
    total_qty = total_amount = 0.0
    for value in fills.values():
        if not isinstance(value, dict):
            continue
        try:
            total_qty += float(value.get("qty", 0) or 0)
            total_amount += float(value.get("amount", 0) or 0)
        except (TypeError, ValueError):
            continue
    ledger_price = total_amount / total_qty if total_qty > 0 and total_amount > 0 else 0.0
    try:
        entry_shares = max(int(pos.get("entry_shares", 0) or 0), 0)
    except (TypeError, ValueError):
        entry_shares = 0

    # 完整成交账优先于旧sell_price。后者在分片卖出场景里可能只是最后一片
    # 的价格；完整账本才能给出整笔交易真实的成交额加权均价。
    if ledger_price > 0 and (entry_shares <= 0 or total_qty >= entry_shares):
        return ledger_price
    if existing_price > 0:
        return existing_price
    return ledger_price


def record_buy(order_id: str, ts_code: str, name: str, signal_date: str,
               buy_date: str, shares: int, buy_price: float, strategy_leg: str,
               exit_n_days: int = 2, traded_at: str = "",
               execution_channel: str = "其他买入", planned_order_qty: int = 0,
               benchmark_open: float = 0.0,
               planned_exit_date_override: str = "",
               component_order_ids: list[str] | None = None,
               strategy_branch_id: str = "",
               condition_profile_ids: str = "",
               exit_rule: str = "") -> None:
    """把券商买入累计成交幂等投影为本地策略持仓。

    同order_id的部分成交回报可能多次到达；这里接受的shares始终是
    “券商累计已成量”，因此只做单调增加，不会重复加仓。
    """
    changed = False
    component_ids = sorted({str(item) for item in (component_order_ids or []) if str(item)})
    shares = max(int(shares or 0), 0)
    if shares <= 0:
        raise ValueError(f"买入持仓回写数量必须大于0:{order_id}")
    normalized_exit = str(planned_exit_date_override or "").replace("-", "")[:8]
    if normalized_exit and (len(normalized_exit) != 8 or not normalized_exit.isdigit()):
        raise ValueError(f"非法计划平仓日:{planned_exit_date_override}")
    exit_date = (
        datetime.datetime.strptime(normalized_exit, "%Y%m%d").date()
        if normalized_exit
        else next_n_trade_days(
            datetime.datetime.strptime(buy_date, "%Y%m%d").date(), n=exit_n_days
        )
    )
    with _positions_file_lock:
        positions = load_positions()
        # 券商成交回报时间常是Unix秒（国金QMT实测），转成可读北京时间便于审计成交时点；
        # 其他格式（HHMMSS整数、已是字符串）原样保留，不做破坏性解析。
        if traded_at and str(traded_at).isdigit() and int(traded_at) > 1_000_000_000:
            try:
                traded_at = datetime.datetime.fromtimestamp(
                    int(traded_at), tz=BEIJING_TZ
                ).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass  # 可以忽略：成交时间只用于显示，解析不了保留原值
        buy_time = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
        planned_exit_time = datetime.datetime.combine(exit_date, SCHED_AFTERNOON_CLOSE).strftime("%Y-%m-%d %H:%M")
        existing = next(
            (p for p in positions if str(p.get("order_id", "")) == str(order_id)),
            None,
        )
        if existing is not None:
            old_shares = max(int(existing.get("entry_shares", existing.get("shares", 0)) or 0), 0)
            if shares <= old_shares:
                _mark_buy_intent_position_projected(order_id, buy_date, old_shares)
                return
            if str(existing.get("status", "")).lower() not in {"open", "sell_pending"}:
                raise RuntimeError(
                    f"已平仓记录出现更大买入累计成交量:{order_id} "
                    f"{old_shares}->{shares}"
                )
            existing["shares"] = shares
            existing["entry_shares"] = shares
            existing["buy_price"] = float(buy_price or existing.get("buy_price", 0.0) or 0.0)
            existing["traded_at"] = traded_at or existing.get("traded_at", "")
            existing["planned_exit_date"] = exit_date.strftime("%Y%m%d")
            existing["planned_exit_time"] = planned_exit_time
            if strategy_branch_id:
                existing["strategy_branch_id"] = strategy_branch_id
            if condition_profile_ids:
                existing["condition_profile_ids"] = condition_profile_ids
            if exit_rule:
                existing["exit_rule"] = exit_rule
            existing["position_updated_at"] = buy_time
            if component_ids:
                existing["component_order_ids"] = component_ids
            changed = True
        else:
            positions.append({
                "order_id": order_id,
                # buy_time=确认回写时刻；traded_at=券商成交回报的真实成交时间（审计成交时点用）
                "traded_at": traded_at,
                "ts_code": ts_code,
                "name": name,
                "signal_date": signal_date,
                "buy_date": buy_date,
                "buy_time": buy_time,
                "planned_exit_date": exit_date.strftime("%Y%m%d"),
                "planned_exit_time": planned_exit_time,
                "shares": shares,
                "entry_shares": shares,
                "buy_price": buy_price,
                "strategy_leg": strategy_leg,
                "strategy_branch_id": strategy_branch_id,
                "condition_profile_ids": condition_profile_ids,
                "exit_rule": exit_rule,
                "status": "open",
                "sell_date": None,
                "sell_price": None,
                "exit_fills_by_date": {},
                **({"component_order_ids": component_ids} if component_ids else {}),
            })
            changed = True
        if changed:
            save_positions(positions)
    if changed:
        _mark_buy_intent_position_projected(order_id, buy_date, shares)
        if component_ids:
            _mark_component_buy_intents_position_projected(
                component_ids, buy_date, shares
            )
        _track_execution(
            "record_buy_slice",
            event_id=f"买入成交|{order_id}",
            entry_date=buy_date,
            time=_execution_time_only(traded_at or buy_time),
            ts_code=ts_code,
            name=name,
            strategy_leg=strategy_leg,
            signal_date=signal_date,
            channel=execution_channel,
            order_id=order_id,
            order_qty=planned_order_qty or shares,
            filled_qty=shares,
            fill_price=buy_price,
            benchmark_open=benchmark_open,
            status="已成交",
            note="持仓成交回写",
        )
        logger().info(
            "持仓记录：策略=%s 分支=%s profile=%s 退出规则=%s %s %s "
            "累计成交%d股 买入日%s 计划平仓日%s",
            strategy_leg,
            strategy_branch_id or "未标注",
            condition_profile_ids or "未标注",
            exit_rule or f"BUY_DATE_PLUS_{exit_n_days}",
            ts_code,
            name,
            shares,
            buy_date,
            exit_date.strftime("%Y%m%d"),
        )


def _mark_buy_intent_position_projected(
    broker_order_id: str,
    business_date: str,
    quantity: int,
) -> None:
    """在本地持仓成功原子替换后，再标记事务意图投影进度。"""

    # 单元测试/纯离线工具可能只使用positions.json，没有启动实盘意图库。
    # 实盘路径在下单前必定已初始化_store。
    if _trade_intent_store_instance is None:
        return
    intent = _trade_intent_store_instance.get_by_broker_order_id(
        str(broker_order_id),
        account_fingerprint=_exit_account_fingerprint(),
        business_date=str(business_date),
    )
    if intent is None or str(intent.get("side", "")).upper() != "BUY":
        return
    _trade_intent_store_instance.mark_position_projected(
        str(intent["intent_id"]), max(int(quantity or 0), 0)
    )


def _mark_component_buy_intents_position_projected(
    broker_order_ids: list[str],
    business_date: str,
    aggregate_quantity: int,
) -> None:
    """将POV等多张券商买单归并到一条本地持仓后，逐笔确认投影。"""

    if _trade_intent_store_instance is None:
        return
    rows: list[dict[str, Any]] = []
    for order_id in broker_order_ids:
        row = _trade_intent_store_instance.get_by_broker_order_id(
            order_id,
            account_fingerprint=_exit_account_fingerprint(),
            business_date=str(business_date),
        )
        if row is not None and str(row.get("side", "")).upper() == "BUY":
            rows.append(row)
    required = sum(max(int(row.get("filled_qty", 0) or 0), 0) for row in rows)
    if required > max(int(aggregate_quantity or 0), 0):
        raise RuntimeError(
            f"组合买入意图成交量{required}超过本地聚合持仓{aggregate_quantity}"
        )
    for row in rows:
        _trade_intent_store_instance.mark_position_projected(
            str(row["intent_id"]), max(int(row.get("filled_qty", 0) or 0), 0)
        )


def _append_exit_fill_ledger(
    position: dict[str, Any], *, fill_date: str, filled_qty: int, fill_price: float = 0.0
) -> None:
    """记录已应用到本地账的退出成交量，供收盘成交明细做幂等补账。"""
    qty = max(int(filled_qty or 0), 0)
    if qty <= 0:
        return
    date_key = str(fill_date or today_beijing().strftime("%Y%m%d")).replace("-", "")[:8]
    ledger_raw = position.get("exit_fills_by_date")
    ledger = dict(ledger_raw) if isinstance(ledger_raw, dict) else {}
    day_raw = ledger.get(date_key)
    day = dict(day_raw) if isinstance(day_raw, dict) else {}
    day["qty"] = max(int(day.get("qty", 0) or 0), 0) + qty
    day["amount"] = max(float(day.get("amount", 0.0) or 0.0), 0.0) + qty * max(
        float(fill_price or 0.0), 0.0
    )
    ledger[date_key] = day
    position["exit_fills_by_date"] = ledger


def _position_exit_filled_qty_on(position: dict[str, Any], fill_date: str) -> int:
    ledger = position.get("exit_fills_by_date")
    if not isinstance(ledger, dict):
        return 0
    day = ledger.get(str(fill_date).replace("-", "")[:8])
    if not isinstance(day, dict):
        return 0
    return max(int(day.get("qty", 0) or 0), 0)


def mark_position_closed(order_id: str, sell_date: str, sell_price: float = 0.0) -> None:
    changed = False
    with _positions_file_lock:
        positions = load_positions()
        for p in positions:
            if p["order_id"] == order_id and p["status"] != "closed":
                remaining_before = max(int(p.get("shares", 0) or 0), 0)
                _append_exit_fill_ledger(
                    p,
                    fill_date=sell_date,
                    filled_qty=remaining_before,
                    fill_price=sell_price,
                )
                p.setdefault("entry_shares", remaining_before)
                p["shares"] = 0
                p["status"] = "closed"
                p["sell_date"] = sell_date
                p["sell_price"] = _recover_sell_price_from_fills(p)
                changed = True
        save_positions(positions)
    if changed:
        _track_execution("rebuild_summary")


def reduce_position_shares(
    order_id: str, remaining_shares: int, fill_price: float = 0.0, fill_date: str = ""
) -> None:
    """部分成交后保留持仓，仅把剩余未卖股数写回，status 维持 open 以便下次继续卖出。"""
    changed = False
    with _positions_file_lock:
        positions = load_positions()
        for p in positions:
            if p["order_id"] == order_id and str(p.get("status", "")).lower() in {"open", "sell_pending"}:
                current = max(int(p.get("shares", 0) or 0), 0)
                # 本地策略仓只能因卖出减少，绝不能把券商同代码人工仓反向灌入。
                remaining = min(max(int(remaining_shares or 0), 0), current)
                _append_exit_fill_ledger(
                    p,
                    fill_date=fill_date or today_beijing().strftime("%Y%m%d"),
                    filled_qty=current - remaining,
                    fill_price=fill_price,
                )
                p.setdefault("entry_shares", current)
                p["shares"] = remaining
                p["status"] = "open"
                changed = changed or remaining < current
        save_positions(positions)
    if changed:
        _track_execution("rebuild_summary")


def find_open_position_by_code(ts_code: str, strategy_leg: str | None = None) -> dict[str, Any] | None:
    """按 ts_code（可选 strategy_leg）查找一条未平持仓，用于卖出后回写。"""
    leg = normalize_strategy_leg(strategy_leg)
    target_aliases = _ts_code_aliases(ts_code)
    for p in load_positions():
        if str(p.get("status", "")).lower() not in {"open", "sell_pending"}:
            continue
        if not (_ts_code_aliases(p.get("ts_code", "")) & target_aliases):
            continue
        if leg and normalize_strategy_leg(p.get("strategy_leg")) != leg:
            continue
        return p
    return None


def _position_manual_exit_only(position: dict[str, Any]) -> bool:
    """人工退出仓位不得进入任何自动卖出链路。

    当前B已彻底删除；即使旧持仓记录缺少新字段，也按策略配置做第二层硬阻断。
    """
    if bool(position.get("manual_exit_only", False)) or bool(position.get("auto_exit_disabled", False)):
        return True
    if str(position.get("strategy_leg", "")).upper() != "B":
        return False
    try:
        strategy_config = load_json_config(PROJECT_ROOT / "config" / "strategy_config.json")
        b_config = strategy_config.get("paper_ab_filtered_strategy", {}).get("b_strategy", {})
        return bool(b_config.get("removed", False)) or not bool(b_config.get("auto_exit_enabled", False))
    except Exception:
        # B配置不可读时失败关闭自动卖出，防止与用户手工单重复。
        return True


def _empty_exit_execution_state() -> dict[str, Any]:
    return {"version": 1, "state_revision": 0, "batches": {}, "intents": []}


def _state_backup_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.backup{path.suffix}")


def _write_json_state_copy(path: Path, payload: dict[str, Any]) -> None:
    mkdir_p(path.parent)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _load_redundant_json_state(
    path: Path,
    *,
    label: str,
    empty_state: dict[str, Any],
    validator: Any,
) -> dict[str, Any]:
    """读取主/备双副本并选择最高revision；备份领先时自动修复主文件。"""

    candidates: list[tuple[int, int, Path, dict[str, Any]]] = []
    errors: list[str] = []
    backup = _state_backup_path(path)
    for priority, candidate in ((1, path), (0, backup)):
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("根节点不是object")
            validator(payload)
            revision = max(int(payload.get("state_revision", 0) or 0), 0)
            candidates.append((revision, priority, candidate, payload))
        except Exception as exc:
            errors.append(f"{candidate.name}:{exc}")
    if not candidates:
        if not path.exists() and not backup.exists():
            return dict(empty_state)
        raise RuntimeError(f"{label}主备副本均不可用:{'；'.join(errors)}")
    revision, _priority, selected_path, selected = max(
        candidates,
        key=lambda item: (item[0], item[1]),
    )
    valid_by_path = {
        candidate_path: (candidate_revision, payload)
        for candidate_revision, _candidate_priority, candidate_path, payload in candidates
    }
    repaired: list[str] = []
    for candidate_path in (path, backup):
        current = valid_by_path.get(candidate_path)
        if current is not None and current[0] == revision and current[1] == selected:
            continue
        _write_json_state_copy(candidate_path, selected)
        repaired.append(candidate_path.name)
    if repaired:
        logger().warning(
            "%s副本缺失、损坏、版本落后或同版本分歧，已按revision=%d修复:%s。",
            label,
            revision,
            ",".join(repaired),
        )
    return selected


def _save_redundant_json_state(
    path: Path,
    state: dict[str, Any],
    *,
    label: str,
    validator: Any,
) -> None:
    """先写新版本备份、再写主文件；任一步失败都禁止继续交易。"""

    try:
        payload = dict(state)
        payload["state_revision"] = max(
            int(payload.get("state_revision", 0) or 0), 0
        ) + 1
        payload["state_updated_at"] = now_beijing().isoformat()
        validator(payload)
        _write_json_state_copy(_state_backup_path(path), payload)
        _write_json_state_copy(path, payload)
        state.clear()
        state.update(payload)
    except Exception as exc:
        _critical_execution_state_failure(f"保存{label}", exc)


def _validate_exit_execution_state(state: dict[str, Any]) -> None:
    batches = state.get("batches")
    intents = state.get("intents")
    if not isinstance(batches, dict) or not isinstance(intents, list):
        raise ValueError("缺少batches/intents")
    tokens: set[str] = set()
    for intent in intents:
        if not isinstance(intent, dict):
            raise ValueError("intent不是object")
        token = str(intent.get("token", "") or "").strip()
        if not token or token in tokens:
            raise ValueError(f"intent token缺失或重复:{token}")
        tokens.add(token)


def _load_exit_execution_state() -> dict[str, Any]:
    """读取退出批次/意图账本；损坏时抛错并阻断卖单，绝不静默清空。"""
    with _exit_execution_state_lock:
        try:
            return _load_redundant_json_state(
                EXIT_EXECUTION_STATE_FILE,
                label="退出执行状态",
                empty_state=_empty_exit_execution_state(),
                validator=_validate_exit_execution_state,
            )
        except Exception as exc:
            _critical_execution_state_failure("读取退出执行状态", exc)


def _save_exit_execution_state(state: dict[str, Any]) -> None:
    """原子保存退出状态；PREPARED必须先于券商place_order落盘。"""
    with _exit_execution_state_lock:
        _save_redundant_json_state(
            EXIT_EXECUTION_STATE_FILE,
            state,
            label="退出执行状态",
            validator=_validate_exit_execution_state,
        )


def _exit_account_fingerprint() -> str:
    """用不可逆短指纹绑定退出批次到账户，不把账号明文写入状态文件。"""
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    broker = config.get("broker", {})
    env_name = str(broker.get("account_id_env", "QMT_ACCOUNT_ID") or "QMT_ACCOUNT_ID")
    account_id = str(os.getenv(env_name, "") or "").strip()
    identity = account_id or "|".join(
        [
            "UNSET",
            env_name,
            str(broker.get("broker_name", "")),
            str(broker.get("session_id", "")),
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


# ── 定时任务 ───────────────────────────────────────────────────────────────────

def _d_relay_pair_load_state() -> dict[str, Any]:
    """读取当日D接力成对POV状态；当日文件损坏时抛错并停止交易。"""
    with _d_relay_pair_state_lock:
        def _validate(state: dict[str, Any]) -> None:
            if state and "date" not in state:
                raise ValueError("缺少date")

        try:
            state = _load_redundant_json_state(
                D_RELAY_PAIR_STATE_FILE,
                label="D接力状态",
                empty_state={},
                validator=_validate,
            )
        except Exception as exc:
            _critical_execution_state_failure("读取D接力状态", exc)
        if str(state.get("date", "")) != today_beijing().strftime("%Y%m%d"):
            return {}
        return state


def _d_relay_pair_save_state(state: dict[str, Any]) -> None:
    """原子保存D接力状态；保存失败必须向上抛出，不能在失忆状态下继续下单。"""
    with _d_relay_pair_state_lock:
        def _validate(payload: dict[str, Any]) -> None:
            if not str(payload.get("date", "") or ""):
                raise ValueError("缺少date")

        _save_redundant_json_state(
            D_RELAY_PAIR_STATE_FILE,
            state,
            label="D接力状态",
            validator=_validate,
        )


def _d_relay_pair_active_today() -> bool:
    try:
        state = _d_relay_pair_load_state()
    except Exception as exc:
        logger().error("D接力状态异常，按活跃任务处理并阻断普通买入：%s", exc)
        return True
    return str(state.get("status", "")).upper() in {
        "PREPARED", "AUCTION_SUBMITTED", "ACTIVE", "BUY_CREDIT_PENDING"
    }


POV_STATE_FILE = PROJECT_ROOT / "data" / "state" / "pov_state.json"
_pov_state_lock = threading.RLock()


def _pov_load_state() -> dict[str, Any]:
    with _pov_state_lock:
        def _validate(payload: dict[str, Any]) -> None:
            if payload and not isinstance(payload.get("items", []), list):
                raise ValueError("items不是list")

        try:
            payload = _load_redundant_json_state(
                POV_STATE_FILE,
                label="买入POV状态",
                empty_state={},
                validator=_validate,
            )
        except Exception as exc:
            _critical_execution_state_failure("读取买入POV状态", exc)
        if payload.get("date") != today_beijing().strftime("%Y%m%d"):
            return {}
        return payload


def _pov_save_state(state: dict[str, Any]) -> None:
    with _pov_state_lock:
        def _validate(payload: dict[str, Any]) -> None:
            if not str(payload.get("date", "") or ""):
                raise ValueError("缺少date")
            if not isinstance(payload.get("items", []), list):
                raise ValueError("items不是list")

        _save_redundant_json_state(
            POV_STATE_FILE,
            state,
            label="买入POV状态",
            validator=_validate,
        )


def _pov_active_today() -> bool:
    """今日是否仍有未完成的POV平滑段——09:30补买路径的互斥判据。"""
    state = _pov_load_state()
    return any(not it.get("done") for it in state.get("items", []))


def _has_pending_buy_for_code(ts_code: str) -> bool:
    """竞价残单未确认终态时禁止POV叠加，避免两路委托合计越过85%硬顶。"""
    aliases = _ts_code_aliases(ts_code)
    return any(
        any(alias in aliases for alias in _ts_code_aliases(order.get("ts_code", "")))
        for order in load_pending_buys()
    )


M_EXEC_STATE_PATH = PROJECT_ROOT / "data" / "processed" / "m_execution_state.json"


def _m_validate_state(state: dict[str, Any]) -> None:
    if not isinstance(state.get("items", []), list):
        raise ValueError("items不是list")


def _m_load_state() -> dict[str, Any]:
    return _load_redundant_json_state(
        M_EXEC_STATE_PATH, label="M执行状态", empty_state={"date": "", "items": []},
        validator=_m_validate_state,
    )


def _m_save_state(state: dict[str, Any]) -> None:
    _save_redundant_json_state(M_EXEC_STATE_PATH, state, label="M执行状态", validator=_m_validate_state)


# ── 盘前买单待确认（09:20挂单→09:30开盘成交确认）────────────────────────────

def save_pending_buys(orders: list[dict[str, Any]]) -> None:
    """原子记录已受理买单；失败必须进入进程级交易恢复。"""
    try:
        with _pending_buy_lock:
            if not isinstance(orders, list) or any(not isinstance(row, dict) for row in orders):
                raise ValueError("pending orders必须是dict列表")
            order_ids = [str(row.get("order_id", "")).strip() for row in orders]
            if any(not order_id for order_id in order_ids):
                raise ValueError("pending order缺少券商委托号")
            if len(set(order_ids)) != len(order_ids):
                raise ValueError(f"pending order出现重复券商委托号:{order_ids}")
            mkdir_p(PENDING_BUY_FILE.parent)
            payload = {
                "schema_version": 1,
                "date": today_beijing().strftime("%Y%m%d"),
                "orders": orders,
            }
            tmp = PENDING_BUY_FILE.with_name(
                f".{PENDING_BUY_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, PENDING_BUY_FILE)
    except Exception as e:
        _critical_execution_state_failure("保存盘前待确认买单", e)
    finally:
        if "tmp" in locals():
            tmp.unlink(missing_ok=True)


def load_pending_buys() -> list[dict[str, Any]]:
    """读取当日已受理买单；损坏/不可读绝不能伪装成无订单。"""
    try:
        with _pending_buy_lock:
            if not PENDING_BUY_FILE.exists():
                return []
            payload = json.loads(PENDING_BUY_FILE.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("pending文件根节点不是对象")
            if payload.get("date") != today_beijing().strftime("%Y%m%d"):
                return []
            orders = payload.get("orders", [])
            if not isinstance(orders, list) or any(not isinstance(row, dict) for row in orders):
                raise ValueError("pending orders不是dict列表")
            order_ids = [str(row.get("order_id", "")).strip() for row in orders]
            if any(not order_id for order_id in order_ids):
                raise ValueError("pending order缺少券商委托号")
            if len(set(order_ids)) != len(order_ids):
                raise ValueError(f"pending order出现重复券商委托号:{order_ids}")
            return orders
    except Exception as e:
        _critical_execution_state_failure("读取盘前待确认买单", e)


def clear_pending_buys() -> None:
    try:
        with _pending_buy_lock:
            if PENDING_BUY_FILE.exists():
                PENDING_BUY_FILE.unlink()
    except Exception as e:
        _critical_execution_state_failure("清除盘前待确认买单", e)


def _append_pending_buys(new_orders: list[dict[str, Any]]) -> None:
    """在同一文件锁内追加已受理委托，避免竞价/E/确认线程互相覆盖。"""

    with _pending_buy_lock:
        existing = load_pending_buys()
        by_id = {str(row.get("order_id", "")): row for row in existing}
        for row in new_orders:
            order_id = str(row.get("order_id", "")).strip()
            if not order_id:
                _critical_execution_state_failure(
                    "追加盘前待确认买单",
                    ValueError("新增pending order缺少券商委托号"),
                )
            previous = by_id.get(order_id)
            if previous is not None and previous != row:
                _critical_execution_state_failure(
                    "追加盘前待确认买单",
                    ValueError(f"同日券商委托号碰撞且内容不同:{order_id}"),
                )
            by_id[order_id] = row
        save_pending_buys(list(by_id.values()))


def _replace_pending_buy_order(old_order_id: str, new_order: dict[str, Any] | None) -> None:
    """替换或移除某笔09:20待确认买单，供集合竞价监控线程使用。"""
    try:
        with _pending_buy_lock:
            orders = load_pending_buys()
            updated: list[dict[str, Any]] = []
            replaced = False
            for order in orders:
                if str(order.get("order_id", "")) == str(old_order_id):
                    replaced = True
                    if new_order is not None:
                        updated.append(new_order)
                else:
                    updated.append(order)
            if not replaced:
                # 并发确认已删除旧单时，重复删除是幂等的；但若补挂单已被券商
                # 受理却找不到旧记录，就不能让新单失去本地追踪。
                if new_order is not None:
                    raise RuntimeError(f"补挂已受理但旧pending不存在:{old_order_id}")
                return
            if updated:
                save_pending_buys(updated)
            else:
                clear_pending_buys()
    except Exception as e:
        _critical_execution_state_failure("更新盘前待确认买单", e)


_trade_intent_store_instance: TradeIntentStore | None = None
_qmt_execution_init_lock = threading.RLock()


def _trade_intent_store() -> TradeIntentStore:
    global _trade_intent_store_instance
    with _qmt_execution_init_lock:
        if _trade_intent_store_instance is None:
            _trade_intent_store_instance = TradeIntentStore(
                PROJECT_ROOT / "data" / "state" / "execution_events.sqlite3"
            )
        return _trade_intent_store_instance


def _record_live_buy(*, recovery_context: str, **record_kwargs: Any) -> None:
    """实盘成交统一投影入口；持仓回写失败必须触发进程级恢复。"""

    try:
        record_buy(**record_kwargs)
    except Exception as exc:
        _critical_execution_state_failure(
            f"{recovery_context}买入成交投影",
            exc,
        )


def _mark_live_position_closed(
    order_id: str,
    sell_date: str,
    sell_price: float = 0.0,
    *,
    recovery_context: str,
) -> None:
    """实盘全成回写统一入口；本地失败后禁止继续卖下一笔。"""

    try:
        mark_position_closed(order_id, sell_date, sell_price)
    except Exception as exc:
        _critical_execution_state_failure(
            f"{recovery_context}卖出全成投影",
            exc,
        )


def _reduce_live_position_shares(
    order_id: str,
    remaining_shares: int,
    fill_price: float = 0.0,
    fill_date: str = "",
    *,
    recovery_context: str,
) -> None:
    """实盘部成回写统一入口；本地失败后禁止继续卖下一笔。"""

    try:
        reduce_position_shares(
            order_id,
            remaining_shares,
            fill_price=fill_price,
            fill_date=fill_date,
        )
    except Exception as exc:
        _critical_execution_state_failure(
            f"{recovery_context}卖出部成投影",
            exc,
        )
