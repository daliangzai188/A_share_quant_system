"""14:55收盘平仓与看门狗：主平仓、14:56:20撤单交接、集合竞价兜底、15:00后核对回写。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import threading
import time
from typing import Any

from src.utils.config import load_json_config
from src.utils.time_utils import BEIJING_TZ, now_beijing, today_beijing
from src.daemon.common import (
    EXIT_CLOSE_SUBMIT_CLOCK_GUARD_DEFAULT_SEC,
    PROJECT_ROOT,
    SCHED_AFTERNOON_CLOSE,
    SCHED_CLOSE_AUCTION_HANDOFF,
    TIMEOUT_COMBINED_PLAN_STEP,
    _EXIT_POV_REMARK_PREFIX,
    _critical_execution_state_failure,
    _exit_sell_lock,
    _notify,
    _pause_pipeline_for_trade,
    _positions_file_lock,
    _resume_pipeline_after_trade,
    is_trade_day,
    logger,
    market_is_open,
    run_script,
)
from src.daemon.state import (
    _exit_account_fingerprint,
    _load_exit_execution_state,
    _mark_live_position_closed,
    _position_exit_filled_qty_on,
    _position_manual_exit_only,
    _reduce_live_position_shares,
    load_positions,
    mark_position_closed,
    save_positions,
)
from src.daemon.plans import _has_due_close_plan_now, _maybe_resume_pipeline_after_trade
from src.daemon.qmt import _qmt_get, _qmt_lock
from src.daemon.orders import _confirm_fill
from src.daemon.sell import (
    _abc_place_sell_order_direct,
    _apply_known_exit_fill,
    _exit_batch_key,
    _exit_commitments_by_code,
    _exit_intent_by_broker_order_id,
    _order_commitment,
    _order_is_sell,
    _order_is_system_exit,
    _pick_sell_limit_price,
    _place_exit_order_with_intent,
    _queue_watchdog_pending,
    _remove_watchdog_pending,
    _resolve_exit_intent_by_broker_order_id,
    _safe_new_exit_order_quantity,
    _split_sell_order_quantities,
    _watchdog_pending,
    _watchdog_pending_lock,
)
from src.daemon.sell_pov import (
    T2_CLOSE_STRATEGY_LEGS,
    _cancel_active_exit_pov_orders,
    _exit_auction_bypass_decision,
)


# ── 卖出端执行引擎（普通D只在容量不足时加入，接力D仍走09:23）──────────
# 目标不是预测精确盘口冲击，而是避免1000万级仓位全部挤在14:55：
#   ① 13:00/14:30按5分钟真实流量×25%拆单；
#   ② 14:45复查最近15分钟流速，容量不足则14:46~14:52切1分钟×35%；
#   ③ 14:53撤销POV交权，14:55主链路按券商实际余量卖出；14:56:20起撤销
#      未成系统单，14:57:05只把真实残仓重新提交到收盘集合竞价兜底。
# 分钟总成交额不等于买盘深度；离线报告另用35%/50%/65%容量折扣做敏感性
# 测试。极端跌停或缩量仍可能无法退出，必须保留未成交告警与隔夜清理。

_exit_bypass_day = ""   # 收盘竞价旁路生效日(看门狗14:56岗据此让路)


def _cancel_active_close_orders_for_auction(
    broker_cfg: dict, due_codes: set[str], *, wait_sec: float = 12.0
) -> bool:
    """串行化14:56:20撤单交接；成功前其他系统SELL不得插入。"""
    with _exit_sell_lock:
        return _cancel_active_close_orders_for_auction_locked(
            broker_cfg,
            due_codes,
            wait_sec=wait_sec,
        )


def _cancel_active_close_orders_for_auction_locked(
    broker_cfg: dict, due_codes: set[str], *, wait_sec: float = 12.0
) -> bool:
    """14:56:20起撤销系统活跃平仓单，让真实余量能在14:57按集合竞价价重报。"""
    from src.qmt_adapter import first_present, object_to_dict, to_int

    active_states = {48, 49, 50, 51, 52, 55}
    prefixes = (_EXIT_POV_REMARK_PREFIX, "ABC平仓-", "看门狗补挂-", "盘中止盈")
    shorts = {str(code).upper().split(".")[0] for code in due_codes if str(code)}
    found: list[str] = []
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        for order in adapter.query_orders() or []:
            data = object_to_dict(order)
            remark = str(first_present(data, ["order_remark", "m_strRemark", "remark"], ""))
            status = to_int(first_present(data, ["order_status", "m_nOrderStatus", "status"], -1), -1)
            code = str(first_present(
                data, ["stock_code", "m_strInstrumentID", "ts_code"], ""
            )).upper().split(".")[0]
            if not remark.startswith(prefixes) or status not in active_states or code not in shorts:
                continue
            oid = str(first_present(data, ["order_id", "m_nOrderID"], ""))
            if oid:
                adapter.cancel_order(oid)
                found.append(oid)
    if not found:
        return True

    logger().warning(
        "[收盘竞价交接] 对%d笔连续竞价平仓余单发出撤单；成交部分保留，仅按真实余仓重报。",
        len(found),
    )
    pending = set(found)
    deadline = time.monotonic() + max(wait_sec, 0.0)
    while pending and time.monotonic() < deadline:
        time.sleep(0.5)
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            statuses: dict[str, int] = {}
            terminal_fills: dict[str, int] = {}
            for order in adapter.query_orders() or []:
                data = object_to_dict(order)
                oid = str(first_present(data, ["order_id", "m_nOrderID"], ""))
                if oid in pending:
                    status = to_int(
                        first_present(data, ["order_status", "m_nOrderStatus", "status"], -1), -1
                    )
                    statuses[oid] = status
                    if status in {53, 54, 56, 57}:
                        ordered = max(to_int(first_present(
                            data, ["order_volume", "m_nOrderVolume", "volume", "order_qty"], 0
                        ), 0), 0)
                        traded = max(to_int(first_present(
                            data, ["traded_volume", "m_nTradedVolume", "traded_qty", "deal_volume"], 0
                        ), 0), 0)
                        terminal_fills[oid] = ordered if status == 56 else min(traded, ordered)
        for oid, filled_qty in terminal_fills.items():
            _resolve_exit_intent_by_broker_order_id(
                oid, filled_qty=filled_qty, terminal_known=True
            )
        # 缺OID=未知，不能当作撤单成功；否则旧单晚成后14:57会重复补卖。
        pending = {
            oid for oid in pending
            if oid not in statuses or statuses[oid] in active_states
        }
    if pending:
        logger().error(
            "[收盘竞价交接] %d笔连续竞价余单未在14:57前确认撤销；禁止重复覆盖，"
            "集合竞价核查将以QMT活单和可卖量为准。",
            len(pending),
        )
        return False
    logger().info("[收盘竞价交接] 连续竞价余单已终态，14:57只需补真实余仓。")
    return True


def _do_sell(pos: dict[str, Any], qmt_enabled: bool) -> None:
    """对单个持仓执行卖出动作，完全独立、单独 try/except。"""
    if _position_manual_exit_only(pos):
        logger().warning(
            "自动平仓硬阻断：%s %s 策略=%s 已标记仅人工退出，系统不提交卖单。",
            pos.get("ts_code", ""),
            pos.get("name", ""),
            pos.get("strategy_leg", ""),
        )
        return
    ts_code = pos["ts_code"]
    name = pos["name"]
    order_id = pos["order_id"]
    shares = int(pos.get("shares", 0))
    today_str = today_beijing().strftime("%Y%m%d")

    try:
        if qmt_enabled:
            logger().warning("⏳ [准备平仓] %s %s  %d股  计划平仓日 %s",
                             ts_code, name, shares, pos.get("planned_exit_date", ""))
            config = load_json_config(PROJECT_ROOT / "config" / "config.json")
            confirm = config.get("live_trade", {}).get(
                "real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED")
            broker_cfg = config.get("broker", {})
            strategy_leg = str(pos.get("strategy_leg", "")).upper()
            if strategy_leg in T2_CLOSE_STRATEGY_LEGS:
                # A/C/D/E 均存在 T+N 收盘卖出口径：
                # planned_orders 文件通常只负责买入计划，平仓时直接按买10/买5挂限价卖出。
                # 普通D同样必须复用该链路；只有状态机明确给出接力动作时，D才会
                # 在T+1的09:23提前卖出，不能把普通D误认为已经在早盘平仓。
                # E也必须走这里：早晨combined SELL保留的是分批前原始股数，POV部成后
                # 再执行会超卖并触发SELL_VOLUME_NOT_AVAILABLE。直接链路每次从QMT校准余量。
                _abc_place_sell_order_direct(
                    ts_code, name, shares, order_id, confirm, config, broker_cfg
                )
            else:
                logger().warning(
                    "[平仓] 策略=%s 不匹配已知分支（%s %s），跳过，请手动确认。",
                    strategy_leg, ts_code, name,
                )
        else:
            logger().info("[平仓] 模拟盘：%s %s 标记已平仓", ts_code, name)
            mark_position_closed(order_id, today_str)
    except Exception as e:
        logger().error("❌ [平仓] 执行异常（%s %s）：%s —— 请立即手动检查！", ts_code, name, e)
        _notify("sell_fail", "❌ 平仓执行异常",
                f"{ts_code} {name} 平仓过程出现异常，持仓可能未正确卖出，请立即回终端检查。",
                level="critical", call=True)


def _reconcile_watchdog_pending_once(
    broker_cfg: dict,
    log: Any,
    *,
    source: str,
) -> int:
    """单轮查询尾盘卖单真实成交并立即投影本地持仓。

    14:55主单过去只登记到 ``_watchdog_pending``，直到15:00:30才确认，导致
    券商已经空仓时本地仍显示open、误发sell_fail并阻塞收盘流水线。现在看门狗
    每轮都读取券商成交回报；全成、部成和撤单均按broker_order_id幂等处理。
    """

    with _watchdog_pending_lock:
        pending_snapshot = list(_watchdog_pending)
    if not pending_snapshot:
        return 0

    today_str = today_beijing().strftime("%Y%m%d")
    applied_total = 0
    closed_positions: list[tuple[str, str, int, float]] = []
    for position, order_ids in pending_snapshot:
        all_terminal = True
        group_seen_qty = 0
        group_seen_amount = 0.0
        group_applied = 0
        query_succeeded = False
        for broker_order_id, expected_qty in order_ids:
            try:
                with _qmt_lock:
                    adapter = _qmt_get(broker_cfg)
                    fill = adapter.get_order_fill(broker_order_id)
                query_succeeded = True
            except Exception as exc:
                all_terminal = False
                log.warning(
                    "[%s] 尾盘成交确认查询失败 order_id=%s：%s",
                    source,
                    broker_order_id,
                    exc,
                )
                continue

            filled_qty = min(
                max(int(getattr(fill, "filled_qty", 0) or 0), 0),
                max(int(expected_qty or 0), 0),
            )
            # QMT偶尔先返回“全部成交”而成交数量字段晚一拍；终态56可安全按
            # 本系统原始委托量认定，统一执行器也使用同一口径。
            if bool(getattr(fill, "is_filled", False)) and filled_qty <= 0:
                filled_qty = max(int(expected_qty or 0), 0)
            fill_price = max(float(getattr(fill, "avg_price", 0.0) or 0.0), 0.0)
            intent = _exit_intent_by_broker_order_id(broker_order_id)
            if fill_price <= 0:
                fill_price = max(float((intent or {}).get("price", 0.0) or 0.0), 0.0)
            local_order_id = str(
                (intent or {}).get("local_order_id", "")
                or position.get("order_id", "")
            )
            current_row = next(
                (
                    row for row in load_positions()
                    if str(row.get("order_id", "")) == local_order_id
                ),
                None,
            )
            if filled_qty > 0 and current_row is not None:
                group_applied += _apply_known_exit_fill(
                    local_order_id,
                    broker_order_id=broker_order_id,
                    current_shares=max(int(current_row.get("shares", 0) or 0), 0),
                    filled_qty=filled_qty,
                    fill_price=fill_price,
                    fill_date=today_str,
                )
            is_terminal = bool(getattr(fill, "is_terminal", False))
            if is_terminal:
                _resolve_exit_intent_by_broker_order_id(
                    broker_order_id,
                    filled_qty=filled_qty,
                    terminal_known=True,
                )
            else:
                all_terminal = False
            if filled_qty > 0:
                group_seen_qty += filled_qty
                group_seen_amount += filled_qty * fill_price

        applied_total += group_applied
        local_order_id = str(position.get("order_id", ""))
        latest = next(
            (
                row for row in load_positions()
                if str(row.get("order_id", "")) == local_order_id
            ),
            None,
        )
        is_closed = latest is None or (
            str(latest.get("status", "")).lower() == "closed"
            or max(int(latest.get("shares", 0) or 0), 0) == 0
        )
        if all_terminal and query_succeeded:
            _remove_watchdog_pending(position, order_ids)
        if group_applied > 0:
            average_price = (
                group_seen_amount / group_seen_qty if group_seen_qty > 0 else 0.0
            )
            if is_closed:
                closed_positions.append(
                    (
                        str(position.get("ts_code", "")),
                        str(position.get("name", "")),
                        group_seen_qty,
                        average_price,
                    )
                )
                log.warning(
                    "✅ [%s] %s 券商确认全部成交%d股，均价%.2f；"
                    "本地持仓已立即关闭。",
                    source,
                    position.get("ts_code", ""),
                    group_seen_qty,
                    average_price,
                )
            else:
                log.warning(
                    "[%s] %s 券商确认新增成交并回写%d股，本地仍余%d股。",
                    source,
                    position.get("ts_code", ""),
                    group_applied,
                    max(int((latest or {}).get("shares", 0) or 0), 0),
                )

    for ts_code, name, quantity, average_price in closed_positions:
        _notify(
            "sell_success",
            "✅ 平仓成交已确认",
            f"{ts_code} {name} 已确认卖出{quantity}股，成交均价{average_price:.2f}；"
            "本地持仓和收盘流水线状态已同步。",
            level="timeSensitive",
        )
    # 即使成交已由另一条完整快照路径先行投影，本轮也要复核并解除可能遗留的
    # 交易优先暂停；_maybe_resume内部只有“确实无到期仓”才会恢复，调用安全。
    _maybe_resume_pipeline_after_trade()
    return applied_total


def _active_sell_outstanding_by_code(orders: Any) -> dict[str, int]:
    """只汇总本系统活跃SELL的未成交量；人工卖单不能冒充策略覆盖。"""
    from src.qmt_adapter import object_to_dict, first_present, to_int

    active_states = {48, 49, 50, 51, 52, 55}
    outstanding: dict[str, int] = {}
    for order in orders or []:
        data = object_to_dict(order)
        status = to_int(first_present(data, ["order_status", "m_nOrderStatus", "status"], -1), -1)
        if status not in active_states:
            continue
        side = str(first_present(data, ["side", "order_side", "direction"], "")).upper()
        order_type = to_int(first_present(data, ["order_type", "m_nOrderType"], -1), -1)
        if order_type != 24 and side not in {"SELL", "24", "卖出"}:
            continue
        if not _order_is_system_exit(data):
            continue
        code = str(first_present(
            data, ["stock_code", "m_strInstrumentID", "ts_code"], ""
        )).upper().split(".")[0]
        if not code:
            continue
        ordered = to_int(first_present(
            data, ["order_volume", "m_nOrderVolume", "volume", "order_qty"], 0
        ), 0)
        traded = to_int(first_present(
            data, ["traded_volume", "m_nTradedVolume", "traded_qty", "deal_volume"], 0
        ), 0)
        outstanding[code] = outstanding.get(code, 0) + max(ordered - traded, 0)
    return outstanding


def _exit_uncovered_by_code(
    due: list[dict[str, Any]],
    *,
    broker_volume: dict[str, int],
    active_outstanding: dict[str, int],
) -> dict[str, int]:
    """按股票代码聚合策略余仓后，只扣一次活跃卖单覆盖。

    同一代码可能有多条本地策略记录。逐条减同一个活卖余量会漏算，因此先
    汇总本地股数，再以券商同代码实际总持仓封顶；这里只计算缺口，不负责把
    券商混仓自动归属到某条策略记录。
    """
    local_by_code: dict[str, int] = {}
    for pos in due:
        short = str(pos.get("ts_code", "")).upper().split(".")[0]
        if not short:
            continue
        local_by_code[short] = local_by_code.get(short, 0) + max(
            int(pos.get("shares", 0) or 0), 0
        )

    uncovered: dict[str, int] = {}
    for short, local_qty in local_by_code.items():
        # 券商列表暂时缺代码时保留本地数量作为待核查上限；真正补单函数会再次
        # 查询can_use_volume并在0时拒单，因此不会凭本地账盲卖。
        broker_qty = max(int(broker_volume.get(short, local_qty) or 0), 0)
        strategy_owned = min(local_qty, broker_qty)
        gap = max(strategy_owned - max(int(active_outstanding.get(short, 0) or 0), 0), 0)
        if gap > 0:
            uncovered[short] = gap
    return uncovered


def _strategy_exit_trade_vwap_by_code(
    orders: Any, trades: Any
) -> dict[str, tuple[int, float]]:
    """按本系统卖出委托ID聚合当日成交股数/VWAP，供收盘最终回账使用。"""
    from src.qmt_adapter import object_to_dict, first_present, to_float, to_int

    remark_prefixes = (
        _EXIT_POV_REMARK_PREFIX,
        "ABC平仓-",
        "看门狗补挂-",
        "盘中止盈",
    )
    id_names = ["order_id", "m_nOrderID", "order_sysid", "m_strOrderSysID"]
    order_to_code: dict[str, str] = {}
    for order in orders or []:
        data = object_to_dict(order)
        remark = str(first_present(data, ["order_remark", "m_strRemark", "remark"], ""))
        if not remark.startswith(remark_prefixes):
            continue
        code = str(first_present(
            data, ["stock_code", "m_strInstrumentID", "ts_code"], ""
        )).upper().split(".")[0]
        if code:
            for id_name in id_names:
                oid = str(data.get(id_name, "") or "").strip()
                if oid:
                    order_to_code[oid] = code

    totals: dict[str, tuple[int, float]] = {}
    for trade in trades or []:
        data = object_to_dict(trade)
        code = next(
            (
                order_to_code.get(str(data.get(id_name, "") or "").strip())
                for id_name in id_names
                if str(data.get(id_name, "") or "").strip() in order_to_code
            ),
            None,
        )
        if not code:
            continue
        qty = max(to_int(first_present(
            data, ["traded_volume", "m_nVolume", "volume", "traded_qty"], 0
        ), 0), 0)
        price = max(to_float(first_present(
            data, ["traded_price", "m_dPrice", "price"], 0.0
        ), 0.0), 0.0)
        if qty <= 0 or price <= 0:
            continue
        old_qty, old_amt = totals.get(code, (0, 0.0))
        totals[code] = (old_qty + qty, old_amt + qty * price)
    return {
        code: (qty, amount / qty)
        for code, (qty, amount) in totals.items()
        if qty > 0
    }


def _strategy_exit_order_vwap_by_code(orders: Any) -> dict[str, tuple[int, float]]:
    """QMT成交明细暂空时，从系统终态委托行提取成交量/价格代理。"""
    from src.qmt_adapter import first_present, object_to_dict, to_float

    totals: dict[str, tuple[int, float]] = {}
    for order in orders or []:
        data = object_to_dict(order)
        if not _order_is_sell(data) or not _order_is_system_exit(data):
            continue
        code = str(
            first_present(data, ["stock_code", "m_strInstrumentID", "ts_code"], "")
        ).upper().split(".")[0]
        if not code:
            continue
        filled, active, unknown = _order_commitment(data)
        if filled <= 0 or active > 0 or unknown > 0:
            continue
        price = max(
            to_float(
                first_present(
                    data,
                    [
                        "avg_price",
                        "traded_price",
                        "m_dTradedPrice",
                        "deal_price",
                        "price",
                        "order_price",
                        "m_dPrice",
                    ],
                    0.0,
                ),
                0.0,
            ),
            0.0,
        )
        if price <= 0:
            continue
        old_qty, old_amount = totals.get(code, (0, 0.0))
        totals[code] = (old_qty + filled, old_amount + filled * price)
    return {
        code: (qty, amount / qty)
        for code, (qty, amount) in totals.items()
        if qty > 0
    }


def _watchdog_rescue_sell(
    pos: dict,
    log: Any,
    max_qty: int | None = None,
    *,
    rescue_wave: str = "",
) -> list[tuple[str, int]]:
    """补挂安全账本确认的未覆盖策略余量，集合竞价只作最后兜底。"""
    if _position_manual_exit_only(pos):
        log.warning(
            "[平仓看门狗] %s 仅人工退出，禁止自动补挂卖单。",
            pos.get("ts_code", ""),
        )
        return []
    from src.broker_adapter import OrderRequest
    from src.qmt_adapter import object_to_dict, first_present, to_int
    ts_code = str(pos.get("ts_code", ""))
    try:
        with _exit_sell_lock:
            config = load_json_config(PROJECT_ROOT / "config" / "config.json")
            broker_cfg = config.get("broker", {})
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                positions = adapter.query_positions()
                orders = adapter.query_orders()
                broker_total = 0
                broker_can_use = 0
                for qp in positions or []:
                    qd = object_to_dict(qp)
                    code = str(
                        first_present(qd, ["stock_code", "m_strInstrumentID", "ts_code"], "")
                    ).upper()
                    if code.split(".")[0] == ts_code.split(".")[0]:
                        broker_total += max(
                            to_int(first_present(qd, ["volume", "m_nVolume", "total_volume"], 0), 0),
                            0,
                        )
                        broker_can_use += max(
                            to_int(first_present(qd, ["can_use_volume", "m_nCanUseVolume"], 0), 0),
                            0,
                        )
                if broker_can_use <= 0:
                    log.warning(
                        "[平仓看门狗] %s QMT实际可用持仓为0，无需补挂（可能已成交/被冻结）。",
                        ts_code,
                    )
                    return []
                # 每个看门狗pending组必须与一条本地持仓一一绑定，
                # 否则同代码多策略时，一张大单的成交量无法用
                # broker_order_id幂等回写到正确的本地行。
                local_row_qty = max(int(pos.get("shares", 0) or 0), 0)
                requested = min(broker_can_use, local_row_qty)
                if max_qty is not None:
                    requested = min(requested, max(int(max_qty), 0))
                requested = _safe_new_exit_order_quantity(
                    ts_code,
                    requested_qty=requested,
                    broker_total=broker_total,
                    broker_can_use=broker_can_use,
                    orders=orders,
                    trade_date=today_beijing().strftime("%Y%m%d"),
                    log=log,
                    phase="平仓看门狗",
                )
                if requested <= 0:
                    return []
                quote_map = adapter.get_full_tick([ts_code])
                quote = quote_map.get(ts_code)
                sell_price, price_label = _pick_sell_limit_price(quote)
                if sell_price <= 0:
                    log.error("[平仓看门狗] %s 无法取得任何卖出价格，补挂放弃。", ts_code)
                    return []
                chunks = _split_sell_order_quantities(ts_code, requested)
                accepted: list[tuple[str, int]] = []
                wave_key = str(rescue_wave or "unspecified").replace(":", "")
                for chunk_no, chunk in enumerate(chunks, start=1):
                    request = OrderRequest(
                        ts_code=ts_code,
                        broker_code=ts_code,
                        side="SELL",
                        quantity=chunk,
                        price_type="FIXED_PRICE",
                        price=sell_price,
                        strategy_name="A_SYSTEM_ABC",
                        remark=(
                            f"看门狗补挂-{price_label}-{today_beijing().strftime('%Y%m%d')}"
                            f"-{chunk_no}/{len(chunks)}"
                        ),
                        strategy_leg=str(pos.get("strategy_leg", "")).upper(),
                        business_date=today_beijing().strftime("%Y%m%d"),
                        signal_date=str(pos.get("signal_date", "")),
                        planned_exit_date=str(pos.get("planned_exit_date", "")),
                        purpose="CLOSE",
                        source_key=(
                            f"EXIT_WATCHDOG|{today_beijing().strftime('%Y%m%d')}|"
                            f"{pos.get('order_id', '')}|{ts_code}|{wave_key}|"
                            f"{chunk_no}/{len(chunks)}"
                        ),
                        metadata={"execution_channel": "平仓看门狗", "name": pos.get("name", "")},
                    )
                    result, _intent_token = _place_exit_order_with_intent(
                        adapter,
                        request,
                        phase="平仓看门狗",
                        local_order_id=str(pos.get("order_id", "")),
                    )
                    if result.accepted:
                        accepted.append((str(result.order_id or ""), chunk))
                    else:
                        log.error(
                            "[平仓看门狗] %s 未覆盖余量子单%d/%d %d股@%.2f被拒：%s",
                            ts_code,
                            chunk_no,
                            len(chunks),
                            chunk,
                            sell_price,
                            result.message,
                        )
                if accepted:
                    log.warning(
                        "🚑 [平仓看门狗] %s 安全未覆盖余量%d股拆%d张按%s %.2f补挂，"
                        "已受理%d股。",
                        ts_code,
                        requested,
                        len(chunks),
                        price_label,
                        sell_price,
                        sum(qty for _, qty in accepted),
                    )
                    return accepted
            return []
    except Exception as e:
        log.error("[平仓看门狗] %s 补挂异常：%s", ts_code, e)
        return []


def _watchdog_reconcile_after_close(broker_cfg: dict, log: Any) -> None:
    """15:00:30 两次核对券商余仓，并回写竞价后才成交的主单/补挂单。

    单次空持仓快照不可信；只有相隔两秒的两次成功查询都确认某代码为0，才把
    本地到期仓标记为已平。其余情况只用本系统委托的真实成交量补记策略账，
    绝不把QMT同代码总持仓（可能含人工/其他策略仓）直接灌入本地策略余量。
    """
    all_positions = load_positions()
    due = [
        pos for pos in all_positions
        if str(pos.get("status", "")).lower() in {"open", "sell_pending"}
        and str(pos.get("planned_exit_date", "99991231")) <= today_beijing().strftime("%Y%m%d")
        and not _position_manual_exit_only(pos)
    ]
    if not due:
        return

    from src.qmt_adapter import object_to_dict, first_present, to_int

    def _volume_map(items: Any) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in items or []:
            data = object_to_dict(item)
            code = str(first_present(
                data, ["stock_code", "m_strInstrumentID", "ts_code"], ""
            )).upper().split(".")[0]
            if not code:
                continue
            volume = max(to_int(first_present(
                data, ["volume", "m_nVolume", "total_volume"], 0
            ), 0), 0)
            result[code] = result.get(code, 0) + volume
        return result

    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        first_positions = adapter.query_positions()
        orders = adapter.query_orders()
        trades = adapter.query_trades()
    time.sleep(2.0)
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        second_positions = adapter.query_positions()

    first_map = _volume_map(first_positions)
    second_map = _volume_map(second_positions)
    vwap_by_code = _strategy_exit_trade_vwap_by_code(orders, trades)
    order_vwap_by_code = _strategy_exit_order_vwap_by_code(orders)
    by_code: dict[str, list[dict[str, Any]]] = {}
    for pos in due:
        by_code.setdefault(str(pos.get("ts_code", "")).split(".")[0], []).append(pos)

    today_str = today_beijing().strftime("%Y%m%d")
    for short, local_rows in by_code.items():
        first_qty = max(first_map.get(short, 0), 0)
        second_qty = max(second_map.get(short, 0), 0)
        local_qty = sum(max(int(pos.get("shares", 0)), 0) for pos in local_rows)
        trade_qty, trade_vwap = vwap_by_code.get(short, (0, 0.0))

        # QMT成交明细可能包含确认超时后才在集合竞价成交的数量。只补“尚未应用
        # 到本地账”的差额，避免把POV/主单已回写过的成交重复扣减。
        applied_today = sum(
            _position_exit_filled_qty_on(pos, today_str)
            for pos in all_positions
            if str(pos.get("ts_code", "")).upper().split(".")[0] == short
        )
        unapplied = min(max(int(trade_qty) - applied_today, 0), local_qty)
        if unapplied > 0:
            remaining_fill = unapplied
            for pos in local_rows:
                if remaining_fill <= 0:
                    break
                held = max(int(pos.get("shares", 0) or 0), 0)
                apply_qty = min(held, remaining_fill)
                if apply_qty <= 0:
                    continue
                if apply_qty >= held:
                    _mark_live_position_closed(
                        pos.get("order_id", ""),
                        today_str,
                        trade_vwap,
                        recovery_context="收盘看门狗成交对账",
                    )
                else:
                    _reduce_live_position_shares(
                        pos.get("order_id", ""),
                        held - apply_qty,
                        fill_price=trade_vwap,
                        fill_date=today_str,
                        recovery_context="收盘看门狗成交对账",
                    )
                remaining_fill -= apply_qty
            log.warning(
                "[平仓看门狗] %s 按系统成交明细补记%d股（当日系统成交%d股，"
                "此前已回写%d股），未引用人工仓数量。",
                short, unapplied - remaining_fill, trade_qty, applied_today,
            )

        row_ids = {str(pos.get("order_id", "")) for pos in local_rows}
        remaining_rows = [
            pos for pos in load_positions()
            if str(pos.get("order_id", "")) in row_ids
            and str(pos.get("status", "")).lower() in {"open", "sell_pending"}
        ]
        remaining_local = sum(max(int(pos.get("shares", 0) or 0), 0) for pos in remaining_rows)

        # query_trades在收盘后也可能短暂返回空，但委托终态和券商
        # 持仓已经先更新。若只信成交明细，本地会保留已卖股数，
        # 翌日又因QMT持仓小于本地账而永久fail-closed。这里只在：
        # 1) 账户指纹匹配；2) 没有活跃/未知卖单；3) 两次QMT快照一致；
        # 4) QMT总仓精确等于floor+target-系统已成 时，才用安全批次
        # 目标补记本地成交。人工买卖/异账户会破坏等式并立即阻断。
        try:
            state = _load_exit_execution_state()
            batch = state.get("batches", {}).get(_exit_batch_key(today_str, short))
            if (
                isinstance(batch, dict)
                and str(batch.get("account_fingerprint", "")) == _exit_account_fingerprint()
            ):
                consumed, active_reserved, unknown_reserved = _exit_commitments_by_code(
                    short,
                    trade_date=today_str,
                    orders=orders,
                )
                target_qty = max(int(batch.get("target_qty", 0) or 0), 0)
                floor_qty = max(int(batch.get("non_target_floor", 0) or 0), 0)
                expected_broker_qty = floor_qty + max(target_qty - consumed, 0)
                desired_due_remaining = max(target_qty - consumed, 0)
                correction = max(remaining_local - desired_due_remaining, 0)
                if (
                    correction > 0
                    and active_reserved == 0
                    and unknown_reserved == 0
                    and first_qty == second_qty == expected_broker_qty
                ):
                    _, order_vwap = order_vwap_by_code.get(short, (0, 0.0))
                    reconcile_price = trade_vwap if trade_vwap > 0 else order_vwap
                    still_to_apply = correction
                    for pos in remaining_rows:
                        if still_to_apply <= 0:
                            break
                        held = max(int(pos.get("shares", 0) or 0), 0)
                        apply_qty = min(held, still_to_apply)
                        if apply_qty <= 0:
                            continue
                        if apply_qty >= held:
                            _mark_live_position_closed(
                                pos.get("order_id", ""),
                                today_str,
                                reconcile_price,
                                recovery_context="收盘安全批次对账",
                            )
                        else:
                            _reduce_live_position_shares(
                                pos.get("order_id", ""),
                                held - apply_qty,
                                fill_price=reconcile_price,
                                fill_date=today_str,
                                recovery_context="收盘安全批次对账",
                            )
                        still_to_apply -= apply_qty
                    applied_from_batch = correction - still_to_apply
                    log.warning(
                        "[平仓看门狗] %s query_trades未完整；两次QMT余仓%d股"
                        "与安全批次一致，补记已知系统成交%d股，本地到期余仓%d股。",
                        short,
                        second_qty,
                        applied_from_batch,
                        desired_due_remaining,
                    )
                    remaining_rows = [
                        pos for pos in load_positions()
                        if str(pos.get("order_id", "")) in row_ids
                        and str(pos.get("status", "")).lower() in {"open", "sell_pending"}
                    ]
                    remaining_local = sum(
                        max(int(pos.get("shares", 0) or 0), 0) for pos in remaining_rows
                    )
        except Exception as exc:
            log.error("[平仓看门狗] %s 安全批次收盘对账失败:%s", short, exc)

        if first_qty == 0 and second_qty == 0:
            for pos in remaining_rows:
                _mark_live_position_closed(
                    pos.get("order_id", ""),
                    today_str,
                    trade_vwap,
                    recovery_context="收盘双空仓确认",
                )
            log.warning(
                "✅ [平仓看门狗] %s 两次确认QMT余仓为0；本地%d股已回写平仓，"
                "系统卖出成交%d股、VWAP %.2f。",
                short, local_qty, trade_qty, trade_vwap,
            )
            continue
        if remaining_local == 0:
            log.warning(
                "✅ [平仓看门狗] %s 本系统成交已覆盖全部策略仓；QMT同代码仍有%d股，"
                "视为人工/其他策略仓，不写回本策略。",
                short, second_qty,
            )
            continue
        if first_qty != second_qty:
            log.error(
                "🛑 [平仓看门狗] %s 两次收盘快照不一致(%d→%d股)，本地策略仍记%d股；"
                "未自动改账，请人工核对。",
                short, first_qty, second_qty, remaining_local,
            )
        else:
            log.error(
                "⚠️ [平仓看门狗] %s 收盘后QMT同代码仍有%d股，本地策略成交账仍剩%d股"
                "（%d条）；为避免混入人工仓，未用QMT总量覆盖本地账，次日仅处理策略余量。",
                short, second_qty, remaining_local, len(remaining_rows),
            )
        _notify(
            "sell_result", "⚠️ 收盘后策略仍有余量",
            f"{short} 本系统成交账仍剩{remaining_local}股；QMT同代码总持仓"
            f"{second_qty}股。系统未把人工混仓写入策略账，次日将仅按策略余量继续处理。",
            level="critical",
        )


def _due_positions_for_close_handoff(
    today_str: str,
    positions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """返回今天必须参与收盘交接的本系统持仓。

    看门狗线程可能在14:57后随daemon一起重启。此时进程内的``fired``标记
    必然为空，不能把“没有内存标记”直接解释成“撤单失败”。只有仍存在到期
    open/sell_pending持仓时，才需要恢复撤单交接状态并可能发送人工告警。
    """
    source = load_positions() if positions is None else positions
    return [
        position
        for position in source
        if str(position.get("status", "")).lower() in {"open", "sell_pending"}
        and str(position.get("planned_exit_date", "99991231")) <= today_str
        and not _position_manual_exit_only(position)
    ]


def _close_position_watchdog() -> None:
    """14:56/14:57:05/14:57:40/14:59 按实际持仓与活跃SELL核查补差额。

    不能再用“存在任意一张委托”作为有人接管的判据：已成单不覆盖当前余仓，
    部成单只覆盖尚未成交的挂单余量，小额活单也不能覆盖整仓。补挂前再次读取
    can_use_volume 并只卖未覆盖量，避免重复委托和超卖；15:00:30 对补挂成交及
    券商实际余仓做最终回写。
    """
    log = logger()
    fired: set[str] = set()
    handoff_alerted: set[str] = set()
    reconciled_day = ""
    # 14:56先补连续竞价未覆盖量；14:56:20起撤掉尚未成交的系统余单；
    # 14:57:05只按QMT真实余仓挂集合竞价兜底，14:57:40/14:59再核查覆盖。
    checkpoints = (
        datetime.time(14, 56),
        datetime.time(14, 57, 5),
        datetime.time(14, 57, 40),
        datetime.time(14, 59),
    )
    while True:
        try:
            now = now_beijing(); t = now.time()
            today_str = today_beijing().strftime("%Y%m%d")
            if is_trade_day(now.date()) and t < datetime.time(15, 1):
                # 14:55主单和后续补挂一经券商返回成交，立即按真实成交量回写；
                # 不能等到15:00:30，更不能让已经空仓的本地open状态持续阻塞流水线。
                with _watchdog_pending_lock:
                    has_pending_confirmation = bool(_watchdog_pending)
                if (
                    has_pending_confirmation
                    and datetime.time(14, 55) <= t < datetime.time(15, 1)
                ):
                    pending_cfg = load_json_config(
                        PROJECT_ROOT / "config" / "config.json"
                    )
                    _reconcile_watchdog_pending_once(
                        pending_cfg.get("broker", {}),
                        log,
                        source="尾盘成交即时确认",
                    )
                auction_handoff_key = f"{today_str}-auction-handoff"
                due_for_handoff = _due_positions_for_close_handoff(today_str)
                if (
                    auction_handoff_key not in fired
                    and t >= datetime.time(14, 57)
                    and not due_for_handoff
                ):
                    # daemon在14:57后重启时fired为空属于正常内存状态。若本地已经
                    # 没有到期仓，则既没有旧平仓单需要撤，也没有余仓需要补挂；
                    # 直接恢复“交接完成”，避免空仓误报并避免看门狗每秒空转阻断。
                    fired.add(auction_handoff_key)
                    log.info(
                        "[收盘竞价交接] 14:57后启动检查：当前无到期持仓，"
                        "已按安全空状态恢复交接完成标记。"
                    )
                if (
                    auction_handoff_key not in fired
                    and SCHED_CLOSE_AUCTION_HANDOFF <= t < datetime.time(14, 57)
                ):
                    if due_for_handoff:
                        handoff_cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
                        seconds_to_auction = (
                            now_beijing().replace(
                                hour=14, minute=57, second=0, microsecond=0
                            ) - now_beijing()
                        ).total_seconds()
                        handoff_ok = _cancel_active_close_orders_for_auction(
                            handoff_cfg.get("broker", {}),
                            {str(p.get("ts_code", "")) for p in due_for_handoff},
                            # 单轮最多4秒，失败后仍能在14:57前进行下一轮复核。
                            wait_sec=max(0.5, min(4.0, seconds_to_auction - 1.0)),
                        )
                    else:
                        handoff_ok = True
                    if handoff_ok:
                        fired.add(auction_handoff_key)
                    elif (
                        now_beijing().time() >= datetime.time(14, 56, 50)
                        and auction_handoff_key not in handoff_alerted
                    ):
                        handoff_alerted.add(auction_handoff_key)
                        _notify(
                            "sell_fail",
                            "🛑 收盘竞价交接尚未完成",
                            "14:57前仍有系统连续竞价卖单未确认撤销；系统会继续复核，"
                            "请立即查看QMT委托状态，避免残仓被旧价格覆盖。",
                            level="critical",
                            call=True,
                        )
                if (
                    auction_handoff_key not in fired
                    and auction_handoff_key not in handoff_alerted
                    and t >= datetime.time(14, 57)
                ):
                    handoff_alerted.add(auction_handoff_key)
                    _notify(
                        "sell_fail",
                        "🛑 收盘竞价撤单交接超时",
                        "14:57前未确认连续竞价平仓余单已撤销；看门狗将禁止重复覆盖，"
                        "但该活单可能无法按收盘价成交，请立即人工核查。",
                        level="critical",
                        call=True,
                    )
                if (
                    datetime.time(14, 57) <= t < datetime.time(15, 0)
                    and auction_handoff_key not in fired
                ):
                    # 旧连续竞价卖单是否已撤仍是未知状态；此时任何集合竞价补单
                    # 都可能与晚成旧单叠加。明确交接成功前硬退出本轮，宁可告警
                    # 人工处理，也不允许自动重复覆盖。
                    log.error(
                        "[平仓看门狗] 14:57后交接尚未明确成功，禁止自动补挂；"
                        "等待人工/QMT明确订单终态。"
                    )
                    time.sleep(1.0)
                    continue
                # 15:00后绝不能补发新单；只继续下方15:00:30成交确认/双快照对账。
                for chk in (() if t >= datetime.time(15, 0) else checkpoints):
                    key = f"{today_str}-{chk}"
                    if key in fired or t < chk:
                        continue
                    if chk == checkpoints[0] and _exit_bypass_day == today_str:
                        # 收盘竞价旁路日:14:55主流程按计划在14:57:05才申报,
                        # 14:56岗此时无委托属正常,不补挂(补挂会即时成交丢右尾)。
                        log.info("[平仓看门狗] 14:56岗:收盘竞价旁路生效,跳过;14:57:40岗核查申报。")
                        fired.add(key)
                        continue
                    if chk == checkpoints[1] and _exit_bypass_day == today_str:
                        log.info("[平仓看门狗] 14:57:05岗:旁路主流程正在申报,留到14:57:40核查。")
                        fired.add(key)
                        continue
                    due = [p for p in load_positions()
                           if str(p.get("status", "")).lower() in {"open", "sell_pending"}
                           and str(p.get("planned_exit_date", "99991231")) <= today_str
                           and not _position_manual_exit_only(p)]
                    if not due:
                        fired.add(key)
                        continue
                    if not _qmt_lock.acquire(timeout=2):
                        log.info("[平仓看门狗] %s QMT忙（主流程可能正在平仓），本次跳过。", chk)
                        continue
                    try:
                        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
                        adapter = _qmt_get(config.get("broker", {}))
                        orders = adapter.query_orders()
                        broker_positions = adapter.query_positions()
                    except Exception as e:
                        log.error("[平仓看门狗] 查询委托失败：%s", e)
                        continue
                    finally:
                        _qmt_lock.release()
                    from src.qmt_adapter import object_to_dict, first_present, to_int
                    active_outstanding = _active_sell_outstanding_by_code(orders)
                    broker_volume: dict[str, int] = {}
                    for bp in broker_positions or []:
                        bd = object_to_dict(bp)
                        code = str(first_present(
                            bd, ["stock_code", "m_strInstrumentID", "ts_code"], ""
                        )).upper().split(".")[0]
                        broker_volume[code] = broker_volume.get(code, 0) + max(
                            to_int(first_present(
                                bd, ["volume", "m_nVolume", "total_volume"], 0
                            ), 0),
                            0,
                        )
                    uncovered_by_code = _exit_uncovered_by_code(
                        due,
                        broker_volume=broker_volume,
                        active_outstanding=active_outstanding,
                    )
                    due_by_code: dict[str, list[dict[str, Any]]] = {}
                    for pos in due:
                        short = str(pos.get("ts_code", "")).upper().split(".")[0]
                        due_by_code.setdefault(short, []).append(pos)
                    missing: list[tuple[dict[str, Any], int]] = []
                    for short, uncovered in uncovered_by_code.items():
                        remaining_gap = max(int(uncovered or 0), 0)
                        for pos in due_by_code.get(short, []):
                            if remaining_gap <= 0:
                                break
                            row_qty = max(int(pos.get("shares", 0) or 0), 0)
                            row_gap = min(row_qty, remaining_gap)
                            if row_gap > 0:
                                missing.append((pos, row_gap))
                                remaining_gap -= row_gap
                    if missing:
                        codes = "、".join(
                            f"{p.get('ts_code','')} {p.get('name','')}未覆盖{qty}股" for p, qty in missing
                        )
                        log.error("🛑 [平仓看门狗] %s 核查：%s，启动余量补挂！", chk, codes)
                        rescued = []
                        for pos, uncovered in missing:
                            oids = _watchdog_rescue_sell(
                                pos,
                                log,
                                max_qty=uncovered,
                                rescue_wave=chk.strftime("%H%M%S"),
                            )
                            if oids:
                                rescued.append((pos, oids))
                        if rescued:
                            for rescued_position, rescued_orders in rescued:
                                _queue_watchdog_pending(
                                    rescued_position,
                                    rescued_orders,
                                )
                            _notify("sell_result", "🚑 平仓看门狗已自动补挂",
                                    f"{chk.strftime('%H:%M:%S')}发现{codes}未被活跃卖单覆盖，"
                                    f"已按当前交易阶段的合法限价补挂；成交不作保证，请留意成交确认。",
                                    level="critical")
                        if len(rescued) < len(missing):
                            _notify("sell_fail", "🛑 收盘平仓补挂失败",
                                    f"{chk.strftime('%H:%M')}：{codes} 部分补挂失败，"
                                    f"请立即人工核对并手动平仓（15:00收盘前最后机会）！",
                                    level="critical", call=True)
                    else:
                        due_codes = {
                            str(pos.get("ts_code", "")).upper().split(".")[0]
                            for pos in due
                            if str(pos.get("ts_code", ""))
                        }
                        zero_broker_codes = sorted(
                            code for code in due_codes
                            if max(int(broker_volume.get(code, 0) or 0), 0) == 0
                        )
                        if zero_broker_codes:
                            log.info(
                                "[平仓看门狗] %s 券商持仓快照已不含%s；"
                                "本轮不重复卖出，等待成交回报即时回写/收盘双快照确认。",
                                chk,
                                "、".join(zero_broker_codes),
                            )
                        else:
                            log.info(
                                "[平仓看门狗] %s 核查通过：%d笔到期持仓的"
                                "券商真实余仓均有活跃SELL完整覆盖。",
                                chk,
                                len(due),
                            )
                    # 查询、归属核算和补单尝试均已完成后才记为fired。QMT忙/异常
                    # 会保留同一checkpoint重试机会，尤其不能丢掉14:57:05首岗。
                    fired.add(key)
            # 15:00:30 对补挂单确认成交并回写（防幽灵账目）
            if _watchdog_pending and t >= datetime.time(15, 0, 30):
                config = load_json_config(PROJECT_ROOT / "config" / "config.json")
                broker_cfg = config.get("broker", {})
                for pos, oids in list(_watchdog_pending):
                    all_terminal = True
                    processed = False
                    try:
                        total_qty, total_amt, applied_qty_total = 0, 0.0, 0
                        for oid, expected_qty in oids:
                            fill = _confirm_fill(
                                broker_cfg, oid, expected_qty, "看门狗补挂确认", timeout_sec=10
                            )
                            filled_qty = min(
                                max(int(fill.filled_qty or 0), 0), expected_qty
                            )
                            fill_price_one = max(float(fill.avg_price or 0.0), 0.0)
                            intent = _exit_intent_by_broker_order_id(oid)
                            if fill_price_one <= 0:
                                # 成交状态常比成交均价字段先到；暂用该intent
                                # 的委托限价作记账代理，后续query_trades可再复核。
                                fill_price_one = max(
                                    float((intent or {}).get("price", 0.0) or 0.0), 0.0
                                )
                            local_order_id = str(
                                (intent or {}).get("local_order_id", "")
                                or pos.get("order_id", "")
                            )
                            if filled_qty > 0 and local_order_id:
                                current_row = next(
                                    (
                                        row for row in load_positions()
                                        if str(row.get("order_id", "")) == local_order_id
                                    ),
                                    None,
                                )
                                if current_row is not None:
                                    applied_qty_total += _apply_known_exit_fill(
                                        local_order_id,
                                        broker_order_id=oid,
                                        current_shares=max(
                                            int(current_row.get("shares", 0) or 0), 0
                                        ),
                                        filled_qty=filled_qty,
                                        fill_price=fill_price_one,
                                        fill_date=today_str,
                                    )
                            if bool(getattr(fill, "is_terminal", False)):
                                _resolve_exit_intent_by_broker_order_id(
                                    oid,
                                    filled_qty=filled_qty,
                                    terminal_known=True,
                                )
                            else:
                                all_terminal = False
                            if filled_qty > 0:
                                total_qty += filled_qty
                                total_amt += filled_qty * fill_price_one
                        price = total_amt / total_qty if total_qty > 0 else 0.0
                        if total_qty > 0:
                            log.warning(
                                "[平仓看门狗] %s 补挂确认成交%d股 均价%.2f；"
                                "按broker_order_id幂等回写%d股，并继续双快照对账。",
                                pos.get("ts_code"), total_qty, price, applied_qty_total,
                            )
                            _notify(
                                "sell_result", "✅ 看门狗成交已回写",
                                f"{pos.get('ts_code')} {pos.get('name','')} 补挂确认成交"
                                f"{total_qty}股，已幂等回写{applied_qty_total}股并进行双快照复核。",
                                level="timeSensitive",
                            )
                        else:
                            _notify("sell_fail", "🛑 补挂全部未成交",
                                    f"{pos.get('ts_code')} {pos.get('name','')} 余量补挂无成交，"
                                    f"持仓过夜，明日开盘务必人工处理！", level="critical", call=True)
                        processed = True
                    except Exception as e:
                        log.error("[平仓看门狗] 补挂单确认异常：%s", e)
                    if processed and all_terminal:
                        _remove_watchdog_pending(pos, oids)
            if (
                is_trade_day(now.date())
                and t >= datetime.time(15, 0, 30)
                and t < datetime.time(15, 5)
                and reconciled_day != today_str
            ):
                close_cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
                qmt_on = bool(close_cfg.get("broker_adapter_enabled")) and bool(
                    close_cfg.get("qmt_enabled")
                ) and bool(close_cfg.get("broker", {}).get("enabled"))
                if qmt_on:
                    _watchdog_reconcile_after_close(close_cfg.get("broker", {}), log)
                reconciled_day = today_str
        except Exception as e:
            logger().error("平仓看门狗异常：%s", e)
        # 距下一个14:52超过10分钟就长睡，尾盘窗口保持10秒粒度。
        now2 = now_beijing()
        nxt = now2.replace(hour=14, minute=52, second=0, microsecond=0)
        if now2 >= now2.replace(hour=15, minute=0, second=0, microsecond=0):
            nxt += datetime.timedelta(days=1)
        gap = (nxt - now2).total_seconds()
        if SCHED_CLOSE_AUCTION_HANDOFF <= now2.time() < datetime.time(14, 57):
            sleep_seconds = 1  # 撤单交接失败时保留多次状态复核机会
        elif 0 <= gap <= 600 or datetime.time(14, 52) <= now2.time() < datetime.time(15, 1):
            sleep_seconds = 10
        else:
            sleep_seconds = min(max(gap - 600, 60), 3600)
        time.sleep(sleep_seconds)


def check_and_close_positions(in_close_window: bool = False) -> None:
    """
    扫描所有持仓，对满足平仓条件的立即处理。
    此函数是最高优先级，任何情况下都应被调用，绝不被数据错误拦截。

    in_close_window=True：调用方就是 14:55 收盘平仓任务，不必拿墙钟猜窗口。
    调度唤醒有亚秒级抖动，2026-08-12 就因 14:54:59.99x 触发使 `now < 14:55`
    成立而整天未平仓。
    """
    try:
        positions = load_positions()
    except Exception as e:
        # 读失败重试一次：瞬时IO抖动不该让整轮平仓检查作废
        try:
            time.sleep(1)
            positions = load_positions()
        except Exception as retry_error:
            logger().error("平仓检查：读取持仓两次失败 %s / %s", e, retry_error)
            _notify("sell_fail", "❌ 平仓检查无法读取持仓",
                    f"positions.json两次读取失败（{retry_error}），本轮自动平仓未执行，请立即人工检查持仓！",
                    level="timeSensitive")
            _critical_execution_state_failure("平仓检查读取持仓", retry_error)

    if not positions:
        logger().info("平仓检查：无持仓")
        return

    today_str = today_beijing().strftime("%Y%m%d")

    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    except Exception as e:
        # 绝不能因config瞬时读取失败静默降级成"模拟盘"：模拟分支会把实盘持仓
        # 本地标closed而真实股票没卖，账实立刻脱节。重试一次，再失败就告警停手。
        try:
            time.sleep(1)
            config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        except Exception as retry_error:
            logger().error("平仓检查：读取config两次失败 %s / %s，无法确认实盘模式，本轮平仓停手。", e, retry_error)
            _notify("sell_fail", "❌ 平仓检查无法读取配置",
                    f"config.json两次读取失败（{retry_error}），无法确认实盘/模拟模式，本轮自动平仓未执行，"
                    f"请立即人工检查持仓！",
                    level="timeSensitive")
            _critical_execution_state_failure("平仓检查读取实盘配置", retry_error)
    qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))

    for pos in positions:
        try:
            status = pos.get("status", "open")
            planned_exit = pos.get("planned_exit_date", "99991231")

            if status == "closed":
                continue
            if _position_manual_exit_only(pos):
                logger().warning(
                    "平仓检查跳过：%s %s 策略=%s 仅人工退出，系统不会标记sell_pending或自动卖出。",
                    pos.get("ts_code", ""),
                    pos.get("name", ""),
                    pos.get("strategy_leg", ""),
                )
                continue

            overdue = planned_exit <= today_str
            pending = status == "sell_pending"

            if not overdue and not pending:
                continue

            ts_code = pos.get("ts_code", "")
            name = pos.get("name", "")
            strategy_leg = str(pos.get("strategy_leg", "")).upper()
            logger().warning("需要平仓：%s %s  计划平仓日 %s  状态 %s  市场开盘 %s",
                             ts_code, name, planned_exit, status, market_is_open())

            t2_close_leg = strategy_leg in T2_CLOSE_STRATEGY_LEGS
            due_today = planned_exit == today_str
            # 不用时间容差：容差会把窗口推到 POV 的 14:53 交接点之前。
            before_close_sell_window = (
                not in_close_window and now_beijing().time() < SCHED_AFTERNOON_CLOSE
            )
            if t2_close_leg and due_today and before_close_sell_window:
                logger().warning(
                    "T2收盘卖门禁：%s %s 策略=%s 今日到期，但当前未到14:55收盘平仓窗口，保持持仓不提前平仓。",
                    ts_code,
                    name,
                    strategy_leg,
                )
                continue

            # 逾期持仓（计划平仓日已过=事故残留，如2026-07-08皇氏平仓失败过夜）
            # 必须第一时间清理：09:15后立即按当前交易阶段合法限价申报，不等14:55、
            # 也不依赖上一轮先标记sell_pending；跌停/停牌/流动性不足时仍不保证成交。
            overdue_past = planned_exit < today_str
            sellable_now = now_beijing().time() >= datetime.time(9, 15) and now_beijing().time() <= datetime.time(15, 0)
            if market_is_open() or pending or (overdue_past and sellable_now):
                if overdue_past:
                    logger().warning("⚠️ 逾期持仓第一时间清理：%s %s 计划平仓日%s已过，立即挂单卖出。", ts_code, name, planned_exit)
                elif t2_close_leg and due_today:
                    # 中央卖出入口再做一次钟差保护，覆盖14:55定时任务以及
                    # 守护进程恰在14:55边界重启后触发的启动平仓检查。这样
                    # 任何调用路径都不能绕过“本机14:55+保护秒数”硬门禁。
                    _wait_for_exit_close_submit_clock_guard(config)
                _do_sell(pos, qmt_enabled)
            else:
                # 市场未开盘，标记 sell_pending，等开盘时处理
                with _positions_file_lock:
                    positions_fresh = load_positions()
                    for p in positions_fresh:
                        if p["order_id"] == pos["order_id"] and p["status"] == "open":
                            p["status"] = "sell_pending"
                    try:
                        save_positions(positions_fresh)
                    except Exception as exc:
                        _critical_execution_state_failure(
                            "平仓前sell_pending状态投影",
                            exc,
                        )
                logger().warning("市场未开盘，%s %s 标记 sell_pending，开盘后自动处理", ts_code, name)

        except Exception as e:
            logger().error("处理单个持仓异常（%s）：%s —— 跳过，继续检查其他持仓", pos.get("ts_code"), e)


def _exit_close_submit_clock_guard_seconds(config: dict[str, Any]) -> float:
    """读取14:55主平仓的跨时钟保护秒数，异常配置按2秒安全值处理。"""

    try:
        configured = float(
            config.get("broker", {}).get(
                "exit_close_submit_clock_guard_seconds",
                EXIT_CLOSE_SUBMIT_CLOCK_GUARD_DEFAULT_SEC,
            )
        )
    except (TypeError, ValueError, AttributeError):
        configured = EXIT_CLOSE_SUBMIT_CLOCK_GUARD_DEFAULT_SEC
    return min(max(configured, 0.0), 10.0)


def _wait_for_exit_close_submit_clock_guard(config: dict[str, Any]) -> float:
    """等到本机北京时间14:55再加保护秒数，吸收券商/交易所钟差。

    2026-08-18券商App把本机14:55:00提交显示为14:54:59成交。策略虽然仍在
    同一秒边界附近，但实盘审计不能出现“早于14:55”。因此默认额外等待2秒，
    并持续复核墙钟；任务如果本来就迟到则立即返回。
    """

    guard_seconds = _exit_close_submit_clock_guard_seconds(config)
    if guard_seconds <= 0:
        return 0.0
    started = now_beijing()
    target = datetime.datetime.combine(
        started.date(),
        SCHED_AFTERNOON_CLOSE,
        tzinfo=BEIJING_TZ,
    ) + datetime.timedelta(seconds=guard_seconds)
    while True:
        remaining = (target - now_beijing()).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 0.05))
    waited = max((now_beijing() - started).total_seconds(), 0.0)
    if waited > 0:
        logger().info(
            "14:55主平仓时钟保护：已等待%.3f秒，跨过本机14:55后保护%.1f秒再提交。",
            waited,
            guard_seconds,
        )
    return waited


def job_afternoon() -> None:
    # 调度器负责在14:55整唤醒；这里再加券商钟差保护。必须放在任何卖单查询/
    # 提交之前，避免Windows比券商服务器快约1秒时，券商回报落在14:54:59。
    try:
        guard_config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    except Exception:
        guard_config = {}
    _wait_for_exit_close_submit_clock_guard(guard_config)
    logger().info("===== 盘中任务（14:40已撤未成交买单；14:55主平仓）=====")
    # 本调用就是 14:55 收盘平仓任务，不靠墙钟判断（否则告警链路会整个静默）
    close_plan_exists = _has_due_close_plan_now(assume_close_window=True)
    if close_plan_exists:
        _pause_pipeline_for_trade("14:55收盘平仓计划")
    else:
        logger().info("14:55 未检测到到期平仓计划，流水线无需暂停。")

    # ① 平仓最高优先：任何情况下先执行，绝不被组合状态机刷新/取数/超时阻塞。
    #    E SELL 依赖的 combined_planned_orders 今日文件已在 09:00/09:20/09:26 生成
    #    （SELL 行仅按 planned_exit_date<=today 产生，与盘中这次刷新无关），
    #    且平仓价在执行时实时取买10/买5，不从文件读死，因此先平仓完全安全。
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        if config.get("broker_adapter_enabled") and config.get("qmt_enabled"):
            # POV配置交接线程已做一次；这里幂等再清场，覆盖daemon重启或线程异常。
            # 撤单函数自行持有卖出生命周期锁；不能把整批持仓提交/成交确认
            # 包进全局锁，否则第一只慢单会饿死14:56:20交接和后续标的。
            handoff_clean = _cancel_active_exit_pov_orders(
                config.get("broker", {}), wait_sec=20
            )
            if not handoff_clean:
                raise RuntimeError(
                    "POV活跃委托未完成撤单，已阻止14:55主平仓重复下单；"
                    "14:56看门狗将按实际未覆盖余量补单"
                )
        # 收盘竞价旁路默认关闭；只有配置显式打开才推迟主平仓。
        if close_plan_exists and _exit_auction_bypass_decision():
            global _exit_bypass_day
            _exit_bypass_day = today_beijing().strftime("%Y%m%d")
            target = now_beijing().replace(hour=14, minute=57, second=5, microsecond=0)
            wait_s = (target - now_beijing()).total_seconds()
            if wait_s > 0:
                time.sleep(wait_s)
        check_and_close_positions(in_close_window=True)
    except Exception as e:
        logger().error("平仓检查异常：%s —— 请立即手动检查持仓！", e)
        # 14:55主平仓被异常中断绝不能只留终端日志：手机必须知道
        # （14:57:05看门狗仍会按安全账本真实余量进收盘竞价兜底）。
        if close_plan_exists:
            _notify("sell_fail", "❌ 14:55主平仓被异常中断",
                    f"异常：{e}。今日有到期平仓计划但主平仓流程未完成，"
                    f"14:57收盘竞价看门狗将按真实余量兜底；请立即回终端核查！",
                    level="timeSensitive")
    finally:
        if close_plan_exists:
            # 本次调用身份仍是14:55收盘平仓任务。即使墙钟因亚秒级抖动仍显示
            # 14:54:59，也必须继续把“到期但未关账”视为待平仓；否则会误恢复
            # 流水线并吞掉失败提醒，正是2026-08-12热修复仍未闭环的一处。
            if _has_due_close_plan_now(assume_close_window=True):
                logger().warning(
                    "14:55平仓后仍检测到待平仓计划，流水线保持暂停；"
                    "14:56/14:57/14:59看门狗将继续按券商真实余仓兜底。"
                )
                _notify(
                    "sell_result",
                    "⏳ 14:55平仓委托已进入确认",
                    "主平仓任务结束时本地仍有到期余仓/待确认卖单。系统已保持交易优先暂停，"
                    "14:56、14:57和14:59看门狗会继续按QMT真实余仓补挂并在收盘后对账；"
                    "请留意后续成交或失败通知。",
                    level="timeSensitive",
                )
            else:
                _resume_pipeline_after_trade("14:55收盘平仓处理完成")

    # ②（2026-07-15 起）撤未成交买单已前移至 14:40 独立调度执行，
    #    平仓时点独占 QMT 通道，此处不再有任何撤单动作。

    # ③ 平仓完成后刷新组合状态机（后台线程，绝不阻塞调度/关键动作）。
    #    此步即便超时被强杀，也不影响上面已执行完的平仓与撤单。
    def _refresh_combined_plan() -> None:
        try:
            run_script("run_combined_live_plan.py", timeout=TIMEOUT_COMBINED_PLAN_STEP)
        except Exception as e:
            logger().error("刷新组合状态机失败：%s", e)

    threading.Thread(target=_refresh_combined_plan, daemon=True, name="combined-refresh-after-close").start()

    logger().info("===== 盘中任务完成 =====")
