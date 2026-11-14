"""盘中涨停止盈。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import hashlib
import threading
import time
from typing import Any

from src.utils.config import load_json_config
from src.utils.time_utils import now_beijing, today_beijing
from src.daemon.common import PROJECT_ROOT, _exit_sell_lock, _notify, is_trade_day, logger
from src.daemon.state import _position_manual_exit_only, load_positions
from src.daemon.plans import _configured_exit_pov_handoff_time
from src.daemon.qmt import _qmt_get, _qmt_lock
from src.daemon.orders import _confirm_fill
from src.daemon.sell import (
    _alert_exit_safety_once,
    _apply_known_exit_fill,
    _exit_intent_by_broker_order_id,
    _exit_order_submission_block_reason,
    _normalize_exit_slice_quantity,
    _prepare_exit_order_intent,
    _resolve_exit_intent_by_broker_order_id,
    _safe_new_exit_order_quantity,
    _update_exit_order_intent,
)


def _takeprofit_submission_label(submitted_at: datetime.time) -> str:
    """按真实发单时段描述止盈委托，禁止把盘中补挂写成09:20预挂。"""

    if datetime.time(9, 20) <= submitted_at < datetime.time(9, 25):
        return "集合竞价预挂"
    return "连续竞价补挂"


def _intraday_takeprofit_monitor() -> None:
    """当日到期持仓的涨停预挂止盈（常驻线程，2026-07-10 用户定稿规则）。

    规则（零轮询压力版）：
    - 今日有平仓计划(planned_exit_date==今日)的 open 持仓，09:20 无条件
      挂（涨停价-0.01）限价卖单（参与集合竞价）——不看涨幅、不做盘中判断；
    - 冲板/秒板/炸板前触及 → 自动成交在涨停-0.01（锁定强势卖出）；
    - 14:45 仍未成交 → 撤单（礼让式重试3次），14:55 收盘平仓主流程接管；
    - 非当日到期持仓不做任何操作。
    回测口径不变（成交集=触及涨停-0.01，E 62笔 8.8x→9.1x）。

    负载设计：一天只有两个动作点（09:30挂、14:45撤）+ 每5分钟一次
    礼让式成交检查（成交须及时回写，否则账户心跳会与本地账目失联）。
    无状态：每轮从 query_orders 按 remark 前缀认领自家单，重启自动接管
    不重复挂；所有QMT调用礼让式（拿不到锁/超时即放弃本轮），任何情况
    不堵塞主交易流程。
    """
    from src.broker_adapter import OrderRequest

    log = logger()
    REMARK_PREFIX = "盘中止盈"
    place_tries: dict[str, int] = {}   # 当日每标的发单次数（熔断用）
    tries_day = ""
    MAX_PLACE_TRIES = 5
    qmt_stall = {"n": 0, "alerted": False}   # 当日QMT调用超时计数（通道卡滞监测）
    while True:
        try:
            now = now_beijing(); t = now.time()
            config = load_json_config(PROJECT_ROOT / "config" / "config.json")
            lt = config.get("live_trade", {})
            takeprofit_quiet_time = _configured_exit_pov_handoff_time(lt)
            # 09:20起挂单：让止盈卖单参与集合竞价——开盘即涨停的极端日
            # 在09:25直接按开盘价(=涨停价)成交，消灭9:25~9:30静默期的
            # 炸板空窗；9:20后竞价不可撤单对本单无碍（本就挂到14:45）。
            if not is_trade_day(now.date()) or t < datetime.time(9, 20) or t >= takeprofit_quiet_time:
                time.sleep(60 if datetime.time(9, 0) <= t < datetime.time(9, 20) else 120); continue
            if not lt.get("intraday_takeprofit_enabled", True):
                time.sleep(300); continue
            offset = float(lt.get("intraday_takeprofit_offset", 0.01))
            qmt_on = (config.get("broker_adapter_enabled") and config.get("qmt_enabled")
                      and config.get("broker", {}).get("enabled"))
            if not qmt_on:
                time.sleep(300); continue
            broker_cfg = config.get("broker", {})
            today_str = today_beijing().strftime("%Y%m%d")
            if tries_day != today_str:
                place_tries.clear(); tries_day = today_str
                qmt_stall.update(n=0, alerted=False)
            due = [p for p in load_positions()
                   if str(p.get("status", "")).lower() == "open"
                   and str(p.get("planned_exit_date", "")) == today_str
                   and not _position_manual_exit_only(p)]
            if t < datetime.time(9, 30):
                # D接力仓09:23可能提交竞价安全部分，9:20抢先挂止盈会冻结股份并
                # 破坏接力安全账本。D一律等9:30后再挂止盈；接力余仓由专用成对POV
                # 管理，普通T+2到期仓再沿用原止盈流程。
                due = [p for p in due if str(p.get("strategy_leg", "")).upper() != "D"]
            if not due:
                time.sleep(300); continue   # 无当日到期持仓：不碰QMT，5分钟后再看

            def _polite_qmt(fn_name: str, args: tuple = (), lock_timeout: float = 3.0, call_timeout: float = 8.0):
                """礼让式QMT调用：拿不到锁或调用超时都返回None放弃，绝不堵塞他人。"""
                if not _qmt_lock.acquire(timeout=lock_timeout):
                    return None
                try:
                    result: list[Any] = []; err: list[BaseException] = []
                    def _run() -> None:
                        try:
                            adapter = _qmt_get(broker_cfg)
                            result.append(getattr(adapter, fn_name)(*args))
                        except BaseException as e:
                            err.append(e)
                    th = threading.Thread(target=_run, daemon=True)
                    th.start(); th.join(call_timeout)
                    if th.is_alive():
                        # 调用线程超时未归（泄漏）：QMT/共享盘IO变慢的早期信号
                        qmt_stall["n"] += 1
                        if qmt_stall["n"] >= 8 and not qmt_stall["alerted"]:
                            qmt_stall["alerted"] = True
                            _notify("system_error", "⚠️ QMT通道疑似卡滞",
                                    f"盘中止盈线程今日已{qmt_stall['n']}次QMT调用超时，"
                                    f"通道/共享盘IO疑似恶化，请关注14:55平仓链路是否正常。",
                                    level="critical")
                        return None
                    if err or not result:
                        return None
                    return result[0]
                finally:
                    _qmt_lock.release()

            # 认领自家止盈单（无状态，防重启丢单/重复挂；重启后查不到
            # 自家活单会自动走下方补挂分支——交易时间内重启即接管）
            active: dict[str, list[dict[str, Any]]] = {}
            cancelled_codes: set[str] = set()
            orders_raw = _polite_qmt("query_orders")
            if orders_raw is None:
                time.sleep(60); continue
            from src.qmt_adapter import object_to_dict, first_present, to_int
            for o in orders_raw:
                od = object_to_dict(o)
                remark = str(first_present(od, ["order_remark", "m_strRemark", "remark"], ""))
                if not remark.startswith(REMARK_PREFIX):
                    continue
                status = to_int(first_present(od, ["order_status", "m_nOrderStatus", "status"], -1), -1)
                code = str(first_present(od, ["stock_code", "m_strInstrumentID", "ts_code"], "")).upper()
                oid = str(first_present(od, ["order_id", "m_nOrderID"], ""))
                short_c = code.split(".")[0]
                if status in (53, 54):   # 部撤/已撤：死单不占坑，单独记录（区分人工撤单）
                    cancelled_codes.add(short_c); continue
                if status == 57:         # 废单：不占坑 → 下面走补挂重试
                    continue
                ordered_qty = max(to_int(first_present(
                    od, ["order_volume", "m_nOrderVolume", "volume", "order_qty"], 0
                ), 0), 0)
                traded_qty = max(to_int(first_present(
                    od, ["traded_volume", "m_nTradedVolume", "traded_qty", "deal_volume"], 0
                ), 0), 0)
                active.setdefault(short_c, []).append(
                    {
                        "order_id": oid,
                        "status": status,
                        "ordered_qty": ordered_qty,
                        "traded_qty": min(traded_qty, ordered_qty) if ordered_qty > 0 else traded_qty,
                    }
                )

            def _order_status(oid: str):
                """查单笔委托当前状态；None=查询失败，-2=本轮列表缺失(未知)。"""
                orders2 = _polite_qmt("query_orders")
                if orders2 is None:
                    return None
                for o2 in orders2:
                    od2 = object_to_dict(o2)
                    if str(first_present(od2, ["order_id", "m_nOrderID"], "")) == str(oid):
                        return to_int(first_present(od2, ["order_status", "m_nOrderStatus", "status"], -1), -1)
                return -2

            cancel_window = t >= datetime.time(14, 45)
            # 可发新委托的时段：9:20~9:25（竞价申报）、9:30~11:30、13:00~14:45。
            # 午休、9:25~9:30静默期等非交易时间不发委托（无论是否刚重启），
            # 已挂单的成交监控/撤单照常。
            can_place = ((datetime.time(9, 20) <= t < datetime.time(9, 25))
                         or (datetime.time(9, 30) <= t < datetime.time(11, 30))
                         or (datetime.time(13, 0) <= t < datetime.time(14, 45)))
            due_by_short: dict[str, list[dict[str, Any]]] = {}
            for due_pos in due:
                due_by_short.setdefault(
                    str(due_pos.get("ts_code", "")).split(".")[0], []
                ).append(due_pos)
            handled_active_orders: set[str] = set()
            for pos in due:
                ts_code = str(pos.get("ts_code", "")); short = ts_code.split(".")[0]
                shares = int(pos.get("shares", 0)); name_s = str(pos.get("name", ""))
                recs = active.get(short, [])
                if recs:
                    matched: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
                    for candidate in recs:
                        oid = str(candidate.get("order_id", ""))
                        try:
                            intent = _exit_intent_by_broker_order_id(oid)
                        except Exception as exc:
                            log.error("[盘中止盈] %s intent读取失败:%s", ts_code, exc)
                            intent = None
                        if intent is not None:
                            if str(intent.get("local_order_id", "")) == str(pos.get("order_id", "")):
                                matched.append((candidate, intent))
                        elif len(recs) == 1 and len(due_by_short.get(short, [])) == 1:
                            # 升级前旧单没有intent；仅在“同代码唯一订单+唯一本地仓”
                            # 时允许按订单真实数量归属，其他情况一律fail-closed。
                            matched.append((candidate, None))
                    if len(matched) != 1:
                        log.error(
                            "🛑 [盘中止盈] %s 同代码%d张系统单/%d条到期仓无法唯一归属，"
                            "禁止自动改账和叠单，等待14:45撤单及最终成交对账。",
                            ts_code,
                            len(recs),
                            len(due_by_short.get(short, [])),
                        )
                        _alert_exit_safety_once(
                            ts_code,
                            "takeprofit_ambiguous",
                            "🛑 止盈订单归属不明确",
                            f"{ts_code} 同代码存在多张止盈单或多条策略仓，系统已禁止自动改账/补单，"
                            "请核对QMT委托。",
                        )
                        continue
                    rec, intent = matched[0]
                    if str(rec.get("order_id", "")) in handled_active_orders:
                        continue
                    handled_active_orders.add(str(rec.get("order_id", "")))
                    status = rec["status"]
                    intent_qty = max(int((intent or {}).get("quantity", 0) or 0), 0)
                    order_qty = max(int(rec.get("ordered_qty", 0) or 0), intent_qty)
                    if order_qty <= 0:
                        log.error("[盘中止盈] %s 委托%s缺少有效订单股数，拒绝自动回写。", ts_code, rec["order_id"])
                        continue
                    if status == 56:  # 已成 → 及时回写（否则账户心跳与本地账目失联）
                        fill = _confirm_fill(
                            broker_cfg, rec["order_id"], order_qty, "盘中止盈确认", timeout_sec=10
                        )
                        price = (
                            fill.avg_price
                            if fill.avg_price > 0
                            else max(float((intent or {}).get("price", 0.0) or 0.0), 0.0)
                        )
                        applied_qty = _apply_known_exit_fill(
                            str(pos.get("order_id", "")),
                            broker_order_id=str(rec["order_id"]),
                            current_shares=shares,
                            filled_qty=order_qty,
                            fill_price=price,
                            fill_date=today_str,
                        )
                        # 先原子回写持仓，再解决intent。若持仓文件落盘失败，
                        # 保留SUBMITTED/订单占用并向上抛错，绝不先声称已平仓。
                        _resolve_exit_intent_by_broker_order_id(
                            rec["order_id"], filled_qty=order_qty, terminal_known=True
                        )
                        log.info(
                            "✅ [盘中止盈] %s %s 委托成交%d股 @%.2f，本地原持仓%d股。",
                            ts_code, name_s, applied_qty, price, shares,
                        )
                        _notify("sell_success", "✅ 盘中止盈成交",
                                f"{ts_code} {name_s} 涨停附近止盈卖出{applied_qty}股 @{price:.2f}。")
                    elif cancel_window and status in (48, 49, 50, 51, 52, 55):  # 14:45 撤单
                        # 撤单成败以订单最终状态为准，绝不信 cancel_order 的返回值
                        # 或调用超时（2026-07-10 事故：QMT慢→调用超时→撤单实际已
                        # 成功却连报三次失败；且超时的请求可能已送达，盲目重发无益）。
                        final_st = None
                        for _ in range(3):
                            _polite_qmt("cancel_order", (rec["order_id"],))  # 发出请求即可
                            time.sleep(2)
                            st = _order_status(rec["order_id"])
                            if st in (53, 54, 56):
                                final_st = st; break
                            time.sleep(1)
                        if final_st == 56:
                            fill = _confirm_fill(
                                broker_cfg, rec["order_id"], order_qty, "盘中止盈确认", timeout_sec=10
                            )
                            price = (
                                fill.avg_price
                                if fill.avg_price > 0
                                else max(float((intent or {}).get("price", 0.0) or 0.0), 0.0)
                            )
                            applied_qty = _apply_known_exit_fill(
                                str(pos.get("order_id", "")),
                                broker_order_id=str(rec["order_id"]),
                                current_shares=shares,
                                filled_qty=order_qty,
                                fill_price=price,
                                fill_date=today_str,
                            )
                            _resolve_exit_intent_by_broker_order_id(
                                rec["order_id"], filled_qty=order_qty, terminal_known=True
                            )
                            log.info(
                                "✅ [盘中止盈] %s %s 撤单前成交%d股 @%.2f。",
                                ts_code, name_s, applied_qty, price,
                            )
                            _notify("sell_success", "✅ 盘中止盈成交",
                                    f"{ts_code} {name_s} 止盈卖单在撤单前成交{applied_qty}股 @{price:.2f}。")
                        elif final_st in (53, 54):
                            log.info("[盘中止盈] %s 14:45未成交已撤单（订单状态%s确认），交回14:55收盘平仓。", ts_code, final_st)
                            fill = _confirm_fill(
                                broker_cfg, rec["order_id"], order_qty, "止盈撤单后确认", timeout_sec=8
                            )
                            applied_qty = min(
                                max(int(fill.filled_qty or 0), 0), order_qty, shares
                            )
                            price = (
                                fill.avg_price
                                if fill.avg_price > 0
                                else max(float((intent or {}).get("price", 0.0) or 0.0), 0.0)
                            )
                            applied_qty = _apply_known_exit_fill(
                                str(pos.get("order_id", "")),
                                broker_order_id=str(rec["order_id"]),
                                current_shares=shares,
                                filled_qty=applied_qty,
                                fill_price=price,
                                fill_date=today_str,
                            )
                            _resolve_exit_intent_by_broker_order_id(
                                rec["order_id"],
                                filled_qty=min(max(int(fill.filled_qty or 0), 0), order_qty),
                                terminal_known=True,
                            )
                            if 0 < applied_qty < shares:
                                log.warning("[盘中止盈] %s 部成%d/%d股后撤单，剩余%d股由14:55平仓。",
                                            ts_code, applied_qty, shares, shares - applied_qty)
                        else:
                            _notify("sell_fail", "⚠️ 止盈单撤单失败",
                                    f"{ts_code} 盘中止盈单14:45撤单后订单状态仍未确认为已撤（最后状态={final_st}），"
                                    f"请立即手动处理，避免与14:55平仓冲突。",
                                    level="critical", call=True)
                    continue
                if cancel_window or not can_place:
                    continue   # 14:45后/非交易时段（午休、竞价静默期）不发新委托
                if short in cancelled_codes:
                    # 14:45前出现已撤单只可能是人工撤的 → 尊重人工干预不补挂；
                    # 若撤前有部分成交，本地持仓不会自动扣减，请人工核对。
                    log.info("[盘中止盈] %s %s 止盈单已被撤销（疑人工干预），不再补挂；14:55收盘平仓仍生效。", ts_code, name_s)
                    continue
                if place_tries.get(short, 0) >= MAX_PLACE_TRIES:
                    continue   # 当日发单熔断：反复被拒/废单说明环境异常，放弃当日预挂
                # 未挂单 → 预挂（涨停价-0.01，参与集合竞价）；重启后由此补挂
                tick_map = _polite_qmt("get_full_tick", ([ts_code],), call_timeout=5.0)
                quote = tick_map.get(ts_code) if tick_map else None
                pre = float(getattr(quote, "pre_close", 0.0) or 0.0) if quote else 0.0
                upper = float(getattr(quote, "upper_limit", 0.0) or 0.0) if quote else 0.0
                if upper <= 0:
                    if pre <= 0:
                        log.warning("[盘中止盈] %s 无法取得涨停价（行情不可得），下轮重试。", ts_code)
                        continue
                    pcap = 0.30 if ts_code.endswith(".BJ") else (0.20 if short.startswith(("300", "301", "688", "689")) else 0.10)
                    upper = round(pre * (1 + pcap), 2)
                sell_price = round(upper - offset, 2)
                place_tries[short] = place_tries.get(short, 0) + 1
                if place_tries[short] >= MAX_PLACE_TRIES:
                    _notify("sell_fail", "⚠️ 盘中止盈熔断",
                            f"{ts_code} {name_s} 止盈委托当日已尝试{MAX_PLACE_TRIES}次仍无活单"
                            f"（反复被拒或废单，疑节假日/通道异常），今日放弃预挂，14:55收盘平仓照常兜底。",
                            level="critical")
                with _exit_sell_lock:
                    # 查询→归属/预算核算→PREPARED落盘→下单必须是同一卖出生命周期
                    # 临界区；否则14:55/看门狗可能在两次查询之间插入另一张单。
                    positions_now = _polite_qmt("query_positions")
                    orders_now = _polite_qmt("query_orders")
                    if positions_now is None or orders_now is None:
                        log.warning("[盘中止盈] %s QMT持仓/委托快照不可得，本轮不发单。", ts_code)
                        continue
                    broker_total = 0
                    broker_can_use = 0
                    for raw_pos in positions_now or []:
                        pos_data = object_to_dict(raw_pos)
                        pos_code = str(first_present(
                            pos_data, ["stock_code", "m_strInstrumentID", "ts_code"], ""
                        )).upper().split(".")[0]
                        if pos_code != short:
                            continue
                        broker_total += max(to_int(first_present(
                            pos_data, ["volume", "m_nVolume", "total_volume"], 0
                        ), 0), 0)
                        broker_can_use += max(to_int(first_present(
                            pos_data, ["can_use_volume", "m_nCanUseVolume"], 0
                        ), 0), 0)
                    safe_qty = _safe_new_exit_order_quantity(
                        ts_code,
                        requested_qty=shares,
                        broker_total=broker_total,
                        broker_can_use=broker_can_use,
                        orders=orders_now,
                        trade_date=today_str,
                        log=log,
                        phase="盘中止盈",
                    )
                    safe_qty = _normalize_exit_slice_quantity(ts_code, safe_qty, shares)
                    if safe_qty <= 0:
                        continue
                    request = OrderRequest(
                        ts_code=ts_code,
                        broker_code=ts_code,
                        side="SELL",
                        quantity=safe_qty,
                        price_type="FIXED_PRICE",
                        price=sell_price,
                        strategy_name="A_SYSTEM_ABC",
                        # 同代码可能同时有多条策略持仓。把本地持仓ID的短哈希
                        # 写入remark，使PREPARED在券商单号未回写前崩溃时，仍能
                        # 用“代码+remark+数量”唯一找回自己的QMT委托。
                        remark=(
                            f"{REMARK_PREFIX}-{today_str}-"
                            f"{hashlib.sha256(str(pos.get('order_id', '')).encode('utf-8')).hexdigest()[:8]}"
                        ),
                        strategy_leg=str(pos.get("strategy_leg", "")).upper(),
                        business_date=today_str,
                        signal_date=str(pos.get("signal_date", "")),
                        planned_exit_date=str(pos.get("planned_exit_date", "")),
                        purpose="CLOSE",
                        source_key=(
                            f"TAKEPROFIT|{today_str}|{pos.get('order_id', '')}|{ts_code}"
                        ),
                        metadata={"execution_channel": "盘中止盈", "name": name_s},
                    )
                    blocked = _exit_order_submission_block_reason()
                    if blocked:
                        log.error("[盘中止盈] %s 发单前触发时间门禁:%s", ts_code, blocked)
                        continue
                    intent_token = _prepare_exit_order_intent(
                        request,
                        phase="盘中止盈",
                        local_order_id=str(pos.get("order_id", "")),
                    )
                    blocked = _exit_order_submission_block_reason()
                    if blocked:
                        _update_exit_order_intent(
                            intent_token,
                            status="REJECTED",
                            terminal_known=True,
                        )
                        log.error("[盘中止盈] %s PREPARED后触发时间门禁:%s", ts_code, blocked)
                        continue
                    result = _polite_qmt("place_order", (request,))
                    if result is None:
                        # 调用可能已送达券商；PREPARED保持全量未知占用，严禁下轮
                        # 因query_orders暂时查不到就重复预挂。
                        log.error(
                            "[盘中止盈] %s 下单调用超时/未知；intent=%s保留占用，"
                            "停止自动重发并等待QMT明确状态。",
                            ts_code,
                            intent_token,
                        )
                        continue
                    if result.accepted:
                        _update_exit_order_intent(
                            intent_token,
                            status="SUBMITTED",
                            broker_order_id=str(result.order_id or ""),
                        )
                    else:
                        _update_exit_order_intent(
                            intent_token,
                            status="REJECTED",
                            terminal_known=True,
                        )
                if result.accepted:
                    submission_label = _takeprofit_submission_label(now_beijing().time())
                    log.warning("⏳ [盘中止盈] %s %s 当日到期，%s涨停-%.2f止盈卖单 %d股@%.2f（14:45未成交自动撤）",
                                ts_code, name_s, submission_label, offset, safe_qty, sell_price)
                    _notify("sell_success", "📈 止盈卖单已预挂",
                            f"{ts_code} {name_s} 今日到期，已挂{sell_price:.2f}（涨停-{offset:.2f}）止盈卖单，"
                            f"冲板即成交锁定强势；14:45未成交自动撤单，14:55照常收盘平仓。",
                            level="timeSensitive")
                else:
                    log.error("❌ [盘中止盈] %s 挂单失败：%s", ts_code, result.message)
        except Exception as e:
            log.error("盘中止盈监控异常：%s", e)
        # 5分钟一轮；若下一轮将跨过14:45撤单点，则对齐到14:45:10醒来，保证撤单及时
        _now = now_beijing()
        _cancel_at = _now.replace(hour=14, minute=45, second=10, microsecond=0)
        _gap = (_cancel_at - _now).total_seconds()
        time.sleep(_gap if 0 < _gap < 300 else 300)
