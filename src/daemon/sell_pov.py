"""卖出POV：容量型分批平仓（14:15大额跑道、14:53交接给14:55主平仓）。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import time
from typing import Any

from src.utils.config import load_json_config
from src.utils.time_utils import BEIJING_TZ, now_beijing, today_beijing
from src.daemon.common import (
    PROJECT_ROOT,
    SCHED_EXIT_LARGE_FORCE_START,
    _EXIT_POV_REMARK_PREFIX,
    _exit_sell_lock,
    _fmt_wan,
    _notify,
    _track_execution,
    is_trade_day,
    logger,
)
from src.daemon.state import (
    _mark_live_position_closed,
    _position_manual_exit_only,
    _reduce_live_position_shares,
    load_positions,
)
from src.daemon.plans import _configured_exit_pov_handoff_time, _round_stock_price, _signal_day_amount
from src.daemon.qmt import _qmt_get, _qmt_lock
from src.daemon.orders import _confirm_fill, _try_cancel_order
from src.daemon.sell import (
    _assert_exit_live_allowed,
    _broker_position_quantities,
    _cancel_own_takeprofit_orders,
    _max_sell_order_shares,
    _normalize_exit_slice_quantity,
    _place_exit_order_with_intent,
    _resolve_exit_intent_by_broker_order_id,
    _safe_new_exit_order_quantity,
    _update_exit_order_intent,
)


def _large_exit_force_eligible(
    position: dict[str, Any], last_price: float, live_cfg: dict[str, Any]
) -> tuple[bool, float, float]:
    """判断是否启用“千万级且高流动性”14:15强制退出跑道。

    返回(是否启用, 仓位参考金额, 信号日成交额)。当前市值下跌时仍以入场成本
    识别原本的千万级仓位，避免恰在亏损日因市值跌破阈值而关闭流动性保护。
    """
    if not bool(live_cfg.get("exit_pov_large_force_enabled", True)):
        return False, 0.0, 0.0
    shares = max(int(position.get("shares", 0) or 0), 0)
    entry_shares = max(int(position.get("entry_shares", shares) or shares), shares)
    buy_price = max(float(position.get("buy_price", 0.0) or 0.0), 0.0)
    reference_amount = max(shares * max(float(last_price or 0.0), 0.0), entry_shares * buy_price)
    min_amount = float(live_cfg.get("exit_pov_large_force_min_amount", 9_500_000) or 0.0)
    signal_date = str(position.get("signal_date", "")).replace("-", "")[:8]
    signal_amount = _signal_day_amount(str(position.get("ts_code", "")), signal_date)
    min_signal_amount = float(
        live_cfg.get("exit_pov_large_force_min_signal_amount", 2_000_000_000) or 0.0
    )
    eligible = bool(
        reference_amount >= min_amount
        and signal_amount >= min_signal_amount
        and min_amount > 0
        and min_signal_amount > 0
    )
    return eligible, reference_amount, signal_amount


# 这里只列“按T+N收盘退出”的策略腿。D接力的计划平仓日仍在T+2，接力日为T+1，
# 因此不会被下面“planned_exit_date == today”选中，仍由09:23专用流程先卖后买。
T2_CLOSE_STRATEGY_LEGS = frozenset({"A", "C", "D", "E", "M"})


def _configured_exit_pov_strategy_legs(live_cfg: dict[str, Any]) -> frozenset[str]:
    """读取允许容量型卖出POV的收盘退出策略腿。

    配置只能在已知收盘退出腿内取子集，避免拼写错误把未知持仓送入自动卖出。
    缺省值包含普通D；是否真正启动POV仍须通过实时成交容量门禁。
    """
    raw = live_cfg.get("exit_pov_strategy_legs")
    if not isinstance(raw, list):
        return T2_CLOSE_STRATEGY_LEGS
    normalized = {str(value).strip().upper() for value in raw}
    return frozenset(normalized & T2_CLOSE_STRATEGY_LEGS)


def _fixed_large_runway_allowed(position: dict[str, Any], live_cfg: dict[str, Any]) -> bool:
    """固定金额14:15跑道是否适用于该策略腿。

    普通D明确排除：D只能由实时成交容量触发，不能仅因绝对仓位达到950万元
    就提前卖出，以免在流动性充足时无谓偏离T+2收盘收益口径。
    """
    raw = live_cfg.get("exit_pov_large_force_strategy_legs", ["A", "C", "E"])
    if not isinstance(raw, list):
        raw = ["A", "C", "E"]
    allowed = {str(value).strip().upper() for value in raw}
    return str(position.get("strategy_leg", "")).strip().upper() in allowed


def _exit_pov_due_positions(
    positions: list[dict[str, Any]], today_str: str, live_cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """筛出今天到期且允许做容量型POV的真实策略仓。

    普通D的planned_exit_date在T+2，所以T+2可进入；D接力发生在T+1，日期不等，
    不会误进本函数。这里只接受open，已有卖单占用的sell_pending继续交给原卖出链路。
    """
    allowed_legs = _configured_exit_pov_strategy_legs(live_cfg)
    return [
        position
        for position in positions
        if str(position.get("status", "")).lower() == "open"
        and str(position.get("planned_exit_date", "")) == today_str
        and str(position.get("strategy_leg", "")).upper() in allowed_legs
        and not _position_manual_exit_only(position)
    ]


def _exit_pov_capacity_triggered(
    sell_amount: float, cumulative_amount: float, trigger_pct: float
) -> bool:
    """判断仓位是否大于实时成交容量门槛；等于门槛仍按小仓位处理。

    参数异常时回退到已锁定的1%，避免错误配置把所有普通D都提前拆卖。
    """
    try:
        pct = float(trigger_pct)
    except (TypeError, ValueError):
        pct = 0.01
    if not 0 < pct <= 0.10:
        pct = 0.01
    return bool(
        float(sell_amount) > 0
        and float(cumulative_amount) > 0
        and float(sell_amount) > float(cumulative_amount) * pct
    )


def _exit_auction_bypass_decision() -> bool:
    """14:55 决策:今日到期仓位是否小到可全额进收盘集合竞价拿收盘价。"""
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        lt = config.get("live_trade", {})
        if not bool(lt.get("exit_auction_bypass_enabled", True)):
            return False
        if not (config.get("broker_adapter_enabled") and config.get("qmt_enabled")):
            return False
        today_str = today_beijing().strftime("%Y%m%d")
        due = [p for p in load_positions()
               if str(p.get("status", "")).lower() in {"open", "sell_pending"}
               and str(p.get("planned_exit_date", "99991231")) <= today_str
               and not _position_manual_exit_only(p)]
        if not due:
            return False
        cap_pct = float(lt.get("exit_auction_capacity_pct", 0.008))
        ratio = float(lt.get("exit_auction_bypass_ratio", 0.2))
        codes = [str(p.get("ts_code", "")) for p in due]
        with _qmt_lock:
            adapter = _qmt_get(config.get("broker", {}))
            quote_map = adapter.get_full_tick(codes)
        for pos in due:
            q = quote_map.get(str(pos.get("ts_code", "")))
            last = float(getattr(q, "last_price", 0.0) or 0.0) if q else 0.0
            cum = float(getattr(q, "amount", 0.0) or 0.0) if q else 0.0
            if last <= 0 or cum <= 0:
                return False   # 行情不可得,走常规14:55路径(确定性优先)
            sell_amt = int(pos.get("shares", 0)) * last
            if sell_amt > cum * cap_pct * ratio:
                return False
        logger().warning("💤 [收盘竞价旁路] 到期仓位≤收盘竞价容量×%.0f%%,推迟至14:57:05申报进"
                         "收盘集合竞价:成交价=收盘价=回测口径,最后5分钟右尾全收。", ratio * 100)
        return True
    except Exception as e:
        logger().error("收盘竞价旁路决策异常(退回常规14:55路径):%s", e)
        return False


def _pov_depth_limit(
    quote: Any,
    *,
    depth_levels: int,
    depth_haircut: float,
    max_slippage_bps: float,
    bid_volume_unit: int,
) -> tuple[int, float, str]:
    """按可见买盘和最大扫档限制，返回(安全股数, 最差限价, 描述)。

    POV阶段绝不挂跌停价扫全盘口。只有价格不低于最新价允许滑点下限的买盘档位
    才计入，并对可见数量再打折；盘口缺失时返回0，由14:55主链路兜底。
    """
    last = float(getattr(quote, "last_price", 0.0) or 0.0) if quote else 0.0
    prices = list(getattr(quote, "bid_prices", None) or []) if quote else []
    volumes = list(getattr(quote, "bid_volumes", None) or []) if quote else []
    if last <= 0 or not prices or not volumes:
        return 0, 0.0, "盘口缺失"
    best_bid = float(prices[0] or 0.0)
    if best_bid <= 0:
        return 0, 0.0, "买一缺失"
    # 扫档冲击以当下真正可执行的买一为基准，不使用可能滞后的最新成交价。
    floor_price = best_bid * (1.0 - max(max_slippage_bps, 0.0) / 10000.0)
    accepted: list[tuple[float, int]] = []
    for price_raw, volume_raw in zip(prices[:max(depth_levels, 1)], volumes[:max(depth_levels, 1)]):
        price = float(price_raw or 0.0)
        volume = int(float(volume_raw or 0.0) * max(bid_volume_unit, 1))
        if price <= 0 or volume <= 0 or price + 1e-9 < floor_price:
            continue
        accepted.append((price, volume))
    if not accepted:
        return 0, 0.0, f"允许滑点{max_slippage_bps:.0f}bp内无买盘"
    safe_qty = int(sum(volume for _, volume in accepted) * min(max(depth_haircut, 0.0), 1.0))
    safe_qty = max(safe_qty, 0)
    worst_price = min(price for price, _ in accepted)
    return safe_qty, _round_stock_price(worst_price), f"{len(accepted)}档可见买盘折后"


def _cancel_active_exit_pov_orders(broker_cfg: dict, *, wait_sec: float = 25.0) -> bool:
    """串行化POV撤单交权，避免与主平仓/看门狗交叉。"""
    with _exit_sell_lock:
        return _cancel_active_exit_pov_orders_locked(broker_cfg, wait_sec=wait_sec)


def _cancel_active_exit_pov_orders_locked(
    broker_cfg: dict, *, wait_sec: float = 25.0
) -> bool:
    """按remark找回并撤净所有活跃POV卖单；daemon重启后同样有效。"""
    from src.qmt_adapter import first_present, object_to_dict, to_int

    active_states = {48, 49, 50, 51, 52, 55}
    deadline = time.time() + max(wait_sec, 0.0)
    found: list[tuple[str, str]] = []
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        for order in adapter.query_orders() or []:
            data = object_to_dict(order)
            remark = str(first_present(data, ["order_remark", "m_strRemark", "remark"], ""))
            status = to_int(first_present(data, ["order_status", "m_nOrderStatus", "status"], -1), -1)
            if not remark.startswith(_EXIT_POV_REMARK_PREFIX) or status not in active_states:
                continue
            oid = str(first_present(data, ["order_id", "m_nOrderID"], ""))
            code = str(first_present(data, ["stock_code", "m_strInstrumentID", "ts_code"], ""))
            if oid:
                adapter.cancel_order(oid)
                found.append((oid, code))
    if not found:
        return True
    logger().warning("[卖出POV交接] 已向%d笔活跃POV委托发出撤单，等待终态。", len(found))
    pending = {oid for oid, _ in found}
    while pending and time.time() < deadline:
        time.sleep(1.0)
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            statuses = {}
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
        # 订单从查询结果中消失不等于已撤：可能是QMT短暂不同步。缺失必须继续
        # 视为未知占用，只有明确终态才能交权。
        pending = {
            oid for oid in pending
            if oid not in statuses or statuses[oid] in active_states
        }
    if pending:
        logger().error("[卖出POV交接] 撤单超时，仍有活跃委托:%s", "、".join(sorted(pending)))
        _notify("sell_fail", "🛑 POV交接撤单未完成",
                f"14:55主平仓前仍有{len(pending)}笔POV卖单未确认撤销，请立即检查QMT。",
                level="critical", call=True)
        return False
    logger().info("[卖出POV交接] 活跃POV委托已全部终态，14:55主平仓可安全接管。")
    return True


def _record_exit_pov_slice(plan: dict[str, Any], **values: Any) -> None:
    """持久化卖出POV逐片明细；记录失败不改变平仓流程。"""
    pos = plan.get("pos", {})
    if not all(
        str(pos.get(field, "") or "").strip()
        for field in ("buy_date", "ts_code", "strategy_leg", "signal_date")
    ):
        # 缺少交易身份时不能生成可能串单的坏主键；真实持仓均应具备这些字段。
        return
    slice_no = int(plan.get("slice_no", 0) or 0)
    broker_order_id = str(values.get("broker_order_id", "") or "")
    event_id = (
        f"卖出POV|{today_beijing().strftime('%Y%m%d')}|"
        f"{pos.get('order_id', '')}|{slice_no}|{broker_order_id or values.get('status', '')}"
    )
    _track_execution(
        "record_sell_slice",
        event_id=event_id,
        entry_date=pos.get("buy_date", ""),
        exit_date=today_beijing().strftime("%Y%m%d"),
        time=now_beijing().strftime("%H:%M:%S"),
        ts_code=pos.get("ts_code", ""), name=pos.get("name", ""),
        strategy_leg=pos.get("strategy_leg", ""), signal_date=pos.get("signal_date", ""),
        channel="卖出POV", slice_no=slice_no,
        local_order_id=pos.get("order_id", ""), broker_order_id=broker_order_id,
        external_flow=values.get("external_flow", 0), budget=values.get("budget", 0),
        depth_limit_qty=values.get("depth_limit_qty", 0), depth_note=values.get("depth_note", ""),
        order_price=values.get("order_price", 0), order_qty=values.get("order_qty", 0),
        filled_qty=values.get("filled_qty", 0), fill_price=values.get("fill_price", 0),
        remaining_qty=values.get("remaining_qty", plan.get("remain_sh", 0)),
        status=values.get("status", ""), note=values.get("note", ""),
    )


def _exit_pov_slice(
    plan: dict[str, Any],
    broker_cfg: dict,
    part: float,
    log: Any,
    *,
    cadence_sec: int = 300,
    confirm_timeout_sec: int = 60,
) -> None:
    """卖出POV单片：流量、盘口、实际可卖数量和最大扫档四重约束。"""
    from src.broker_adapter import OrderRequest

    pos = plan["pos"]
    ts_code = str(pos.get("ts_code", ""))
    today_str = today_beijing().strftime("%Y%m%d")
    plan["slice_no"] = int(plan.get("slice_no", 0) or 0) + 1
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        lt = config.get("live_trade", {})
        handoff_time = _configured_exit_pov_handoff_time(lt)
        with _exit_sell_lock:
            if now_beijing().time() >= handoff_time:
                return
            _assert_exit_live_allowed(config)
            late_phase = cadence_sec <= 60
            depth_levels = int(lt.get("exit_pov_depth_levels", 3))
            depth_haircut = float(lt.get("exit_pov_depth_haircut", 0.50))
            max_slippage_bps = float(lt.get(
                "exit_pov_late_max_slippage_bps" if late_phase else "exit_pov_max_slippage_bps",
                35 if late_phase else 20,
            ))
            bid_volume_unit = int(lt.get("exit_pov_bid_volume_unit", 1))
            max_slice_pct = float(lt.get("exit_pov_max_slice_position_pct", 0.10))

            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                quote = adapter.get_full_tick([ts_code]).get(ts_code)
                broker_total, broker_can_use = _broker_position_quantities(adapter, ts_code)
                orders = adapter.query_orders()
            if quote is None:
                _record_exit_pov_slice(plan, status="跳过", note="行情为空")
                return
            last = float(getattr(quote, "last_price", 0.0) or 0.0)
            cum = float(getattr(quote, "amount", 0.0) or 0.0)
            prev = float(plan.get("prev_cum", 0.0) or 0.0)
            sampled_at = now_beijing()
            last_sample_at = plan.get("last_sample_at")
            own_fill = float(plan.pop("own_fill_since_sample", 0.0) or 0.0)
            plan["prev_cum"] = cum
            plan["last_sample_at"] = sampled_at
            if last <= 0 or cum <= 0:
                log.warning("[卖出POV] %s 行情缺少价格或累计成交额，跳过本片。", ts_code)
                _record_exit_pov_slice(plan, status="跳过", note="行情缺少价格或累计成交额")
                return
            if broker_total <= 0:
                # QMT 在客户端切换/数据同步瞬间可能“查询成功但返回空持仓”。项目已有
                # 连续两轮确认才清幽灵仓的事故保护，这里绝不能凭一次空快照直接平账。
                missing_hits = int(plan.get("broker_missing_hits", 0)) + 1
                plan["broker_missing_hits"] = missing_hits
                log.error(
                    "[卖出POV] %s 第%d次未查到券商实际持仓；本片拒绝下单且不改本地账，"
                    "等待后续QMT复核。",
                    ts_code,
                    missing_hits,
                )
                if missing_hits == 1:
                    _notify(
                        "sell_fail",
                        "⚠️ POV持仓查询异常",
                        f"{ts_code} {pos.get('name','')} QMT本轮未返回实际持仓；"
                        "系统已停止本片且未误标平仓，请检查QMT连接。",
                        level="timeSensitive",
                    )
                _record_exit_pov_slice(plan, status="跳过", note="券商实际持仓查询为空")
                return
            plan["broker_missing_hits"] = 0
            plan["remain_sh"] = min(int(plan.get("remain_sh", broker_total)), broker_total)
            if broker_can_use <= 0:
                log.warning("[卖出POV] %s 券商可卖股数为0（可能仍有冻结卖单），跳过本片。", ts_code)
                _record_exit_pov_slice(plan, status="跳过", note="券商可卖股数为0")
                return
            # 计划可能13:00建立、之后才启动；普通计划的陈旧基线不能作为单片
            # 流量。千万级强制计划是唯一例外：14:15首片必须真正开始卸载，不能
            # 因13:00基线陈旧而平白拖到14:20。该首片仍同时受盘口50%折扣、
            # 20bp扫档、实际可卖量和“最多余仓10%”四重上限约束；首调之后基线
            # 已刷新，14:20起恢复严格使用相邻5分钟外部流量。
            stale_baseline = bool(
                isinstance(last_sample_at, datetime.datetime)
                and (sampled_at - last_sample_at).total_seconds() > cadence_sec * 1.8
            )
            start_at = plan.get("start")
            force_first_slice = bool(
                stale_baseline
                and plan.get("large_force")
                and not plan.get("force_first_slice_attempted")
                and isinstance(start_at, datetime.datetime)
                and start_at <= sampled_at < start_at + datetime.timedelta(seconds=90)
            )
            if force_first_slice:
                plan["force_first_slice_attempted"] = True
                log.info(
                    "[卖出POV] %s 千万级14:15首片启用；首片仍受盘口/滑点/10%%余仓上限。",
                    ts_code,
                )
            elif stale_baseline:
                log.info("[卖出POV] %s 启动/切频前刷新流量基线，等待下一片。", ts_code)
                _record_exit_pov_slice(plan, status="跳过", note="刷新流量基线")
                return
            if cum < prev:
                log.warning("[卖出POV] %s 累计成交额发生回退(%.0f<%.0f)，仅重置基线。", ts_code, cum, prev)
                _record_exit_pov_slice(plan, status="跳过", note="累计成交额回退")
                return
            # 累计成交额包含我方上一片成交；扣掉后避免“自己成交→放大下一片”的自激。
            external_flow = max(cum - prev - own_fill, 0.0)
            budget = external_flow * min(max(part, 0.0), 1.0)
            depth_qty, price, depth_note = _pov_depth_limit(
                quote,
                depth_levels=depth_levels,
                depth_haircut=depth_haircut,
                max_slippage_bps=max_slippage_bps,
                bid_volume_unit=bid_volume_unit,
            )
            if depth_qty <= 0 or price <= 0:
                log.info("[卖出POV] %s %s，跳过本片，不用跌停价扫盘。", ts_code, depth_note)
                _record_exit_pov_slice(
                    plan, external_flow=external_flow, budget=budget,
                    depth_limit_qty=depth_qty, depth_note=depth_note,
                    status="跳过", note="盘口深度或价格不足",
                )
                return
            remain = int(plan["remain_sh"])
            flow_qty = int(budget / max(last, 0.01))
            slice_cap = max(1, int(remain * min(max(max_slice_pct, 0.0), 1.0)))
            qty = min(
                remain,
                broker_can_use,
                depth_qty,
                flow_qty,
                slice_cap,
                _max_sell_order_shares(ts_code),
            )
            qty = _normalize_exit_slice_quantity(ts_code, qty, remain)
            qty = _safe_new_exit_order_quantity(
                ts_code,
                requested_qty=qty,
                broker_total=broker_total,
                broker_can_use=broker_can_use,
                orders=orders,
                trade_date=today_str,
                log=log,
                phase="卖出POV",
            )
            qty = _normalize_exit_slice_quantity(ts_code, qty, remain)
            if qty <= 0:
                _record_exit_pov_slice(
                    plan, external_flow=external_flow, budget=budget,
                    depth_limit_qty=depth_qty, depth_note=depth_note,
                    order_price=price, status="跳过", note="安全可卖数量不足申报单位",
                )
                return
            request = OrderRequest(
                ts_code=ts_code, broker_code=ts_code, side="SELL",
                quantity=qty, price_type="FIXED_PRICE", price=price,
                strategy_name="A_SYSTEM_ABC", remark=f"{_EXIT_POV_REMARK_PREFIX}-{today_str}",
                strategy_leg=str(pos.get("strategy_leg", "")).upper(),
                business_date=today_str,
                signal_date=str(pos.get("signal_date", "")),
                planned_exit_date=str(pos.get("planned_exit_date", "")),
                purpose="CLOSE",
                source_key=(
                    f"EXIT_POV|{today_str}|{pos.get('order_id', '')}|"
                    f"{ts_code}|{plan.get('slice_no', 0)}"
                ),
                metadata={"execution_channel": "卖出POV", "name": pos.get("name", "")},
            )
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                result, intent_token = _place_exit_order_with_intent(
                    adapter,
                    request,
                    phase="卖出POV",
                    local_order_id=str(pos.get("order_id", "")),
                )
            if not result.accepted:
                log.error("❌ [卖出POV] %s提交失败:%s", ts_code, result.message)
                _record_exit_pov_slice(
                    plan, external_flow=external_flow, budget=budget,
                    depth_limit_qty=depth_qty, depth_note=depth_note,
                    order_price=price, order_qty=qty, status="提交失败", note=result.message,
                )
                return
            oid = str(result.order_id or "")
            fill = _confirm_fill(
                broker_cfg, oid, qty, "卖出POV片",
                timeout_sec=confirm_timeout_sec, poll_sec=3 if late_phase else 5,
            )
            if fill.filled_qty < qty and not fill.is_terminal:
                _try_cancel_order(broker_cfg, oid, ts_code)
                # 尾段必须在配置时刻前交权，终态确认不再固定等待20秒。
                seconds_left = max(
                    2.0,
                    (datetime.datetime.combine(sampled_at.date(), handoff_time,
                                               tzinfo=BEIJING_TZ) - now_beijing()).total_seconds() - 2.0,
                )
                fill = _confirm_fill(
                    broker_cfg, oid, qty, "卖出POV片终态",
                    timeout_sec=min(12.0, seconds_left), poll_sec=2,
                )
            fq = min(int(fill.filled_qty or 0), qty)
            fp = float(fill.avg_price) if float(getattr(fill, "avg_price", 0) or 0) > 0 else price
            if bool(getattr(fill, "is_terminal", False)):
                _update_exit_order_intent(
                    intent_token,
                    status="RESOLVED",
                    broker_order_id=oid,
                    filled_qty=fq,
                    terminal_known=True,
                )
            if fq > 0:
                plan["remain_sh"] = max(remain - fq, 0)
                plan["sold_qty"] = int(plan.get("sold_qty", 0)) + fq
                plan["sold_amt"] = float(plan.get("sold_amt", 0.0)) + fq * fp
                plan["own_fill_since_sample"] = float(plan.get("own_fill_since_sample", 0.0)) + fq * fp
                if plan["remain_sh"] == 0:
                    avg = plan["sold_amt"] / max(plan["sold_qty"], 1)
                    _mark_live_position_closed(
                        pos.get("order_id", ""),
                        today_str,
                        avg,
                        recovery_context="卖出POV",
                    )
                    plan["done"] = True
                    log.warning("✅ [卖出POV] %s %s 分批平仓完成:%d股 均价%.2f",
                                ts_code, pos.get("name", ""), plan["sold_qty"], avg)
                    _notify("sell_success", "✅ 卖出POV分批平仓完成",
                            f"{ts_code} {pos.get('name','')} 分批卖出{plan['sold_qty']}股 "
                            f"均价{avg:.2f} 市值{_fmt_wan(plan['sold_amt'])}")
                else:
                    _reduce_live_position_shares(
                        pos.get("order_id", ""),
                        int(plan["remain_sh"]),
                        fill_price=fp,
                        recovery_context="卖出POV",
                    )
            log.info(
                "[卖出POV] %s 外部流量%.1f万 预算%.1f万 %s 挂%d股@%.2f 成%d股@%.2f 剩%d股",
                ts_code, external_flow / 1e4, budget / 1e4, depth_note,
                qty, price, fq, fp, int(plan["remain_sh"]),
            )
            _record_exit_pov_slice(
                plan, broker_order_id=oid, external_flow=external_flow, budget=budget,
                depth_limit_qty=depth_qty, depth_note=depth_note,
                order_price=price, order_qty=qty, filled_qty=fq, fill_price=fp,
                remaining_qty=int(plan["remain_sh"]),
                status="已成交" if fq > 0 else "未成交",
                note="终态已确认" if bool(getattr(fill, "is_terminal", False)) else "终态待后续核对",
            )
    except Exception as e:
        log.error("卖出POV片异常(%s):%s", ts_code, e)
        _record_exit_pov_slice(plan, status="异常", note=str(e))


def _exit_pov_monitor() -> None:
    """卖出端POV分批平仓线程：5分钟常规卸载，14:46后1分钟加速。

    普通D与A/C/E一样仅在实时容量不足时介入；小仓位零介入并继续由
    14:55主链路卖出。D接力日不是planned_exit_date，不会进入本线程。
    启动前撤止盈预挂单释放冻结股份。每片均受外部流量、盘口深度、滑点
    和QMT实际可卖量约束，涨停也不再假设买盘无限或直接全量卖出。
    """
    log = logger()
    done_day = ""
    while True:
        try:
            now = now_beijing(); t = now.time()
            today_str = today_beijing().strftime("%Y%m%d")
            if (not is_trade_day(now.date())) or done_day == today_str:
                time.sleep(300); continue
            config = load_json_config(PROJECT_ROOT / "config" / "config.json")
            lt = config.get("live_trade", {})
            handoff_time = _configured_exit_pov_handoff_time(lt)
            if t >= handoff_time:
                if config.get("broker_adapter_enabled") and config.get("qmt_enabled"):
                    with _exit_sell_lock:
                        _cancel_active_exit_pov_orders(config.get("broker", {}), wait_sec=20)
                done_day = today_str
                time.sleep(300); continue
            if t < datetime.time(13, 0, 5):
                time.sleep(min(120, max(5, (now.replace(hour=13, minute=0, second=5) - now).total_seconds()))); continue
            qmt_on = (config.get("broker_adapter_enabled") and config.get("qmt_enabled")
                      and config.get("broker", {}).get("enabled"))
            if not bool(lt.get("exit_pov_enabled", True)) or not qmt_on:
                done_day = today_str; continue
            due = _exit_pov_due_positions(load_positions(), today_str, lt)
            if not due:
                # 13:00账户/本地恢复可能暂时还没有识别出到期仓。不能把当天
                # 永久标记完成；交接前持续重扫，恢复出来的仓位仍须参加容量
                # 门禁与14:30/14:45流速复查。
                handoff_at = datetime.datetime.combine(
                    now.date(), handoff_time, tzinfo=BEIJING_TZ
                )
                retry_after = min(
                    30.0,
                    max(1.0, (handoff_at - now_beijing()).total_seconds()),
                )
                time.sleep(retry_after)
                continue
            part = float(lt.get("exit_pov_participation", 0.25))
            late_part = float(lt.get("exit_pov_late_participation", 0.35))
            late_timeout = int(lt.get("exit_pov_late_confirm_timeout_sec", 20))
            buffer_k = float(lt.get("exit_pov_runway_buffer", 1.5))
            pm_k = float(lt.get("exit_pov_pm_extrapolate", 0.44))
            broker_cfg = config.get("broker", {})
            # ── 13:00 跑道决策(实时已见量外推下午容量,自动捕捉缩量日) ──
            codes = [str(p.get("ts_code", "")) for p in due]
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                quote_map = adapter.get_full_tick(codes)
            plans: list[dict[str, Any]] = []
            cum_1300: dict[str, float] = {}
            for pos in due:
                q = quote_map.get(str(pos.get("ts_code", "")))
                last = float(getattr(q, "last_price", 0.0) or 0.0) if q else 0.0
                cum = float(getattr(q, "amount", 0.0) or 0.0) if q else 0.0
                if last <= 0 or cum <= 0:
                    continue
                cum_1300[str(pos.get("ts_code", ""))] = cum
                sell_amt = int(pos.get("shares", 0)) * last
                if _fixed_large_runway_allowed(pos, lt):
                    force_large, force_ref_amount, force_signal_amount = (
                        _large_exit_force_eligible(pos, last, lt)
                    )
                else:
                    # 普通D只服从下方实时容量比较，绝不因固定950万元门槛提前卖。
                    force_large = False
                    force_ref_amount = 0.0
                    force_signal_amount = 0.0
                # 分批触发线(2026-07-16 θ扫描定稿):卖出市值>当日13:00累计成交×1%
                # 才分批。更低阈值会把"一把梭不痛"的仓位拉去吃时间成本(100万档
                # 实测-0.08%/笔);θ=1%各档不受伤,1000万档+0.23%/笔最优。
                if not force_large and not _exit_pov_capacity_triggered(
                    sell_amt,
                    cum,
                    lt.get("exit_pov_trigger_pct", 0.01),
                ):
                    continue    # 小仓位:今日不分批,14:55主链路处理
                slice_est = cum * pm_k / 21.0        # 13:05~14:45约21片
                need = int(sell_amt * buffer_k / (slice_est * part)) + 1 if slice_est > 0 else 99
                slots = min(need, 20)
                if force_large:
                    # 只对经过20亿信号日成交额门禁的千万级仓位固定14:15启动。
                    # 17笔中15笔竞价前完成；验证集仅8笔，仍由实时流量/盘口兜底，
                    # 不把该小样本结论扩散到小仓位或低流动性标的。
                    start_dt = now.replace(
                        hour=SCHED_EXIT_LARGE_FORCE_START.hour,
                        minute=SCHED_EXIT_LARGE_FORCE_START.minute,
                        second=0,
                        microsecond=0,
                    )
                    slots = 6  # 14:15/20/25/30/35/40六个5分钟信号片
                else:
                    start_dt = now.replace(hour=14, minute=40, second=0, microsecond=0) - datetime.timedelta(minutes=5 * slots)
                plans.append(dict(pos=pos, prev_cum=cum, last_sample_at=now_beijing(), start=start_dt,
                                  remain_sh=int(pos.get("shares", 0)), sold_qty=0, sold_amt=0.0,
                                  done=False, large_force=force_large, slice_no=0))
                log.warning("🏃 [卖出POV] %s %s 仓位%.0f万%s,启动分批:%d片,%s起,片参与率%.0f%%",
                            pos.get("ts_code"), pos.get("name"), sell_amt / 1e4,
                            (
                                f"（千万级强制跑道；入场参考{force_ref_amount / 1e4:.0f}万/"
                                f"信号日成交{force_signal_amount / 1e8:.1f}亿）"
                                if force_large else ">单片容量"
                            ),
                            slots, start_dt.strftime("%H:%M"), part * 100)
            if plans:
                # 撤止盈预挂单，释放被冻结的可卖股份。
                try:
                    with _qmt_lock:
                        adapter = _qmt_get(broker_cfg)
                        _cancel_own_takeprofit_orders(adapter, {str(p["pos"].get("ts_code", "")) for p in plans})
                except Exception as e:
                    log.error("卖出POV撤止盈单异常:%s", e)
                _notify("sell_result", "🏃 大仓位分批平仓启动",
                        "；".join(f"{p['pos'].get('ts_code','')} {p['pos'].get('name','')} 分批卖出" for p in plans)
                        + f"。常规片=5分钟成交×{part:.0%};14:46后切1分钟×{late_part:.0%},"
                          f"{handoff_time.strftime('%H:%M:%S')}前停止新片并完成交接,"
                          "剩余由14:55主平仓单处理。")
            # ── 13:00~14:40按5分钟；14:45复查；14:46~14:52按1分钟 ──
            checked_1430 = False
            checked_1445 = False
            cum_1430: dict[str, float] = {}
            planned_ids = {str(p["pos"].get("order_id", "")) for p in plans}
            slice_stop_at = datetime.datetime.combine(
                now.date(), handoff_time, tzinfo=BEIJING_TZ
            ) - datetime.timedelta(seconds=40)
            while now_beijing() < slice_stop_at:
                # 14:30按“最近实际流速×剩余分钟”重算跑道。旧版再次比较累计额×1%
                # 永远无法新增触发（累计额只增），这里改为真正能识别午后缩量的容量条件。
                if not checked_1430 and now_beijing().time() >= datetime.time(14, 30):
                    checked_1430 = True
                    retry = [p for p in due if str(p.get("order_id", "")) not in planned_ids
                             and str(p.get("status", "")).lower() == "open"]
                    for pos in retry:
                        try:
                            ts_r = str(pos.get("ts_code", ""))
                            with _qmt_lock:
                                adapter = _qmt_get(broker_cfg)
                                q = adapter.get_full_tick([ts_r]).get(ts_r)
                            last = float(getattr(q, "last_price", 0.0) or 0.0) if q else 0.0
                            cum = float(getattr(q, "amount", 0.0) or 0.0) if q else 0.0
                            if last <= 0 or cum <= 0:
                                continue
                            cum_1430[ts_r] = cum
                            sell_amt = int(pos.get("shares", 0)) * last
                            base_1300 = float(cum_1300.get(ts_r, 0.0) or 0.0)
                            observed = max(cum - base_1300, 0.0)
                            observed_minutes = 90.0
                            per_minute = observed / observed_minutes if observed > 0 else 0.0
                            projected = per_minute * (15.0 * part + 7.0 * late_part)
                            if projected > 0 and sell_amt * buffer_k <= projected:
                                continue
                            try:
                                with _qmt_lock:
                                    adapter = _qmt_get(broker_cfg)
                                    _cancel_own_takeprofit_orders(adapter, {ts_r})
                            except Exception as exc:
                                log.warning("[卖出POV] 14:30跑道复查前撤止盈卖单失败：%s", exc)
                            plans.append(dict(pos=pos, prev_cum=cum, last_sample_at=now_beijing(),
                                              start=now_beijing(),
                                              remain_sh=int(pos.get("shares", 0)),
                                              sold_qty=0, sold_amt=0.0, done=False, slice_no=0))
                            planned_ids.add(str(pos.get("order_id", "")))
                            log.warning("🏃 [卖出POV] 14:30跑道复查触发:%s %s 仓位%.0f万，"
                                        "预计剩余安全容量仅%.0f万，14:35起补充分批。",
                                        ts_r, pos.get("name"), sell_amt / 1e4, projected / 1e4)
                        except Exception as e:
                            log.error("14:30缩量复查异常(%s):%s", pos.get("ts_code"), e)
                # 14:45尾段容量复查：即使日累计量足以让θ门禁不触发，也要检查最近
                # 15分钟流速是否足以承接实际余量。容量不足才加入1分钟卸载，避免
                # 1000万仓位全部挤到14:55；该阶段不依赖收盘集合竞价。
                if not checked_1445 and now_beijing().time() >= datetime.time(14, 45):
                    checked_1445 = True
                    fresh_by_id = {str(p.get("order_id", "")): p for p in load_positions()}
                    retry = []
                    for original in due:
                        oid = str(original.get("order_id", ""))
                        current = fresh_by_id.get(oid, original)
                        if oid not in planned_ids and str(current.get("status", "")).lower() == "open":
                            retry.append(current)
                    for pos in retry:
                        try:
                            ts_r = str(pos.get("ts_code", ""))
                            with _qmt_lock:
                                adapter = _qmt_get(broker_cfg)
                                q = adapter.get_full_tick([ts_r]).get(ts_r)
                            last = float(getattr(q, "last_price", 0.0) or 0.0) if q else 0.0
                            cum = float(getattr(q, "amount", 0.0) or 0.0) if q else 0.0
                            base = float(cum_1430.get(ts_r, 0.0) or 0.0)
                            if last <= 0 or cum <= 0:
                                continue
                            sell_amt = int(pos.get("shares", 0)) * last
                            # 守护进程若14:30后重启会缺少基线，此时 projected=0，
                            # 保守触发尾段卸载，不能因缺数据把整仓拖到14:55。
                            projected = max(cum - base, 0.0) / 15.0 * 7.0 * late_part if base > 0 else 0.0
                            if sell_amt * buffer_k <= projected:
                                continue
                            try:
                                with _qmt_lock:
                                    adapter = _qmt_get(broker_cfg)
                                    _cancel_own_takeprofit_orders(adapter, {ts_r})
                            except Exception as exc:
                                log.warning("[卖出POV] 撤止盈卖单失败：%s", exc)
                            start_late = now_beijing() + datetime.timedelta(minutes=1)
                            plans.append(dict(pos=pos, prev_cum=cum, last_sample_at=now_beijing(),
                                              start=start_late, remain_sh=int(pos.get("shares", 0)),
                                              sold_qty=0, sold_amt=0.0, done=False, slice_no=0))
                            planned_ids.add(str(pos.get("order_id", "")))
                            log.warning(
                                "🏃 [卖出POV] 14:45尾段容量触发:%s %s 仓位%.0f万，"
                                "预计14:46~14:52可平滑承接%.0f万，切1分钟×%.0f%%卸载。",
                                ts_r, pos.get("name"), sell_amt / 1e4, projected / 1e4,
                                late_part * 100,
                            )
                        except Exception as e:
                            log.error("14:45尾段容量复查异常(%s):%s", pos.get("ts_code"), e)
                if not plans:
                    time.sleep(30); continue
                now_time = now_beijing().time()
                if datetime.time(14, 45) <= now_time < datetime.time(14, 46):
                    time.sleep(min(20, max(1, (now_beijing().replace(
                        hour=14, minute=46, second=5, microsecond=0
                    ) - now_beijing()).total_seconds())))
                    continue
                late_phase = now_time >= datetime.time(14, 46)
                active_part = late_part if late_phase else part
                cadence_sec = 60 if late_phase else 300
                for plan in plans:
                    if not plan["done"] and now_beijing() >= plan["start"]:
                        _exit_pov_slice(
                            plan, broker_cfg, active_part, log,
                            cadence_sec=cadence_sec,
                            confirm_timeout_sec=late_timeout if late_phase else 60,
                        )
                # 不能因早期计划已完成就提前退出：仍需等14:45复查此前未触发的
                # 其他到期仓位。完成尾段复查后，全部完成才可收工。
                if checked_1445 and all(p["done"] for p in plans):
                    break
                now2 = now_beijing()
                step = 60 if now2.time() >= datetime.time(14, 46) else 300
                nxt = (now2 + datetime.timedelta(seconds=step)).replace(second=10, microsecond=0)
                if step == 300:
                    nxt -= datetime.timedelta(minutes=nxt.minute % 5)
                sl = (nxt - now2).total_seconds()
                if sl > 0:
                    time.sleep(sl)
            # 配置交接点前强制撤净所有POV活单，再把控制权交给14:55主平仓。
            with _exit_sell_lock:
                _cancel_active_exit_pov_orders(broker_cfg, wait_sec=25)
            leftover = [p for p in plans if not p["done"]]
            if leftover:
                handoff_label = handoff_time.strftime("%H:%M:%S")
                for plan in leftover:
                    _record_exit_pov_slice(
                        plan,
                        remaining_qty=int(plan.get("remain_sh", 0) or 0),
                        status=f"{handoff_label}交接",
                        note="卖出POV停止新片，剩余股数交给14:55主平仓链路",
                    )
                log.warning(
                    "[卖出POV] %s交接:%d只未卖完(剩余%s),交14:55主平仓链路；"
                    "14:56:20起撤未成主单，14:57:05仅按真实残仓集合竞价兜底。",
                    handoff_label,
                    len(leftover),
                    "、".join(f"{p['pos'].get('ts_code','')}{p['remain_sh']}股" for p in leftover),
                )
            done_day = today_str
        except Exception as e:
            log.error("卖出POV线程异常:%s", e)
            time.sleep(60)
