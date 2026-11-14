"""卖出状态机：限价与价格笼子、退出批次与保护底线、委托意图（PREPARED→SUBMITTED）、在途覆盖计算、
A/C/D/E 直接卖出（含尾盘待确认登记）。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import os
import threading
import time
from dataclasses import replace
from typing import Any

from src.utils.time_utils import BEIJING_TZ, now_beijing, today_beijing
from src.daemon.common import (
    PROJECT_ROOT,
    SCHED_AFTERNOON_CLOSE,
    SCHED_CLOSE_AUCTION_HANDOFF,
    _SYSTEM_EXIT_REMARK_PREFIXES,
    _critical_execution_state_failure,
    _exit_execution_state_lock,
    _exit_sell_lock,
    _fmt_wan,
    _notify,
    _positions_file_lock,
    _ts_code_aliases,
    is_trade_day,
    logger,
)
from src.daemon.state import (
    _append_exit_fill_ledger,
    _exit_account_fingerprint,
    _load_exit_execution_state,
    _mark_live_position_closed,
    _recover_sell_price_from_fills,
    _reduce_live_position_shares,
    _save_exit_execution_state,
    load_positions,
    save_positions,
)
from src.daemon.plans import _ceil_stock_price, _configured_exit_pov_handoff_time, _round_stock_price
from src.daemon.qmt import _qmt_get, _qmt_lock
from src.daemon.orders import _confirm_fill


def _sell_price_cage_floor(quote: Any) -> float:
    """连续竞价卖出下限：科创板纯2%，沪深其他2%/十价位，北交所5%。"""
    if quote is None:
        return 0.0
    bids = list(getattr(quote, "bid_prices", None) or [])
    asks = list(getattr(quote, "ask_prices", None) or [])
    best_bid = float(bids[0] or 0.0) if bids else 0.0
    best_ask = float(asks[0] or 0.0) if asks else 0.0
    last = float(getattr(quote, "last_price", 0.0) or 0.0)
    pre_close = float(getattr(quote, "pre_close", 0.0) or 0.0)
    # 交易所卖出基准价顺序：最高买价→最低卖价→最新价→前收盘价。
    benchmark = next((value for value in (best_bid, best_ask, last, pre_close) if value > 0), 0.0)
    if benchmark <= 0:
        return 0.0
    code = str(getattr(quote, "ts_code", "") or "").upper()
    short = code.split(".")[0]
    is_star = short.startswith(("688", "689"))
    is_bse = code.endswith(".BJ") or short.startswith(("4", "8", "920"))
    # 科创板规则是纯2%边界；沪深其他股票及北交所还需兼顾十个最小价位。
    ratio = 0.95 if is_bse else 0.98
    exact_floor = benchmark * ratio if is_star else min(benchmark * ratio, benchmark - 0.10)
    # 这是卖出申报价的“下限”，必须向上取到最小价位；四舍五入可能向下穿越
    # 精确边界而被交易所判为无效申报（如5.1548只能报5.16，不能报5.15）。
    cage_floor = _ceil_stock_price(exact_floor)
    lower_limit = float(getattr(quote, "lower_limit", 0.0) or 0.0)
    return max(cage_floor, _round_stock_price(lower_limit) if lower_limit > 0 else 0.01, 0.01)


def _pick_sell_limit_price(
    quote: Any, *, continuous_auction: bool | None = None
) -> tuple[float, str]:
    """按交易阶段选择可执行卖出限价。

    2026-09-07曾直接用动态笼子下限申报，QMT在48毫秒后以“价格错误”
    返回废单。连续竞价优先报当前买五价，以便吃掉买一至买五的可见买盘；
    买五缺失或低于合法笼子下限时回退买一。14:57后仍用日跌停价兜底。
    """
    if continuous_auction is None:
        now_t = now_beijing().time()
        continuous_auction = (
            datetime.time(9, 30) <= now_t < datetime.time(11, 30)
            or datetime.time(13, 0) <= now_t < datetime.time(14, 57)
        )
    if continuous_auction:
        bids = list(getattr(quote, "bid_prices", None) or []) if quote else []
        asks = list(getattr(quote, "ask_prices", None) or []) if quote else []
        best_bid = float(bids[0] or 0.0) if bids else 0.0
        best_ask = float(asks[0] or 0.0) if asks else 0.0
        last = float(getattr(quote, "last_price", 0.0) or 0.0) if quote else 0.0
        pre_close = float(getattr(quote, "pre_close", 0.0) or 0.0) if quote else 0.0
        cage_floor = _sell_price_cage_floor(quote)
        bid5 = float(bids[4] or 0.0) if len(bids) >= 5 else 0.0
        if (
            best_bid > 0
            and 0 < bid5 <= best_bid
            and cage_floor > 0
            # 不使用恰好贴边的买五；盘口再上移一个价位就可能再次成为废单。
            and _round_stock_price(bid5) > cage_floor
        ):
            return _round_stock_price(bid5), "连续竞价买五价"
        for value, label in (
            (best_bid, "连续竞价买一价(买五不可用)"),
            (best_ask, "连续竞价卖一价(无买盘)"),
            (last, "连续竞价最新价(无盘口)"),
            (pre_close, "连续竞价前收盘价(无盘口)"),
        ):
            if value > 0:
                return _round_stock_price(value), label

    lower_limit = float(getattr(quote, "lower_limit", 0.0) or 0.0) if quote else 0.0
    if lower_limit > 0:
        return _round_stock_price(lower_limit), "集合竞价跌停限价"
    bid_prices = list(getattr(quote, "bid_prices", None) or []) if quote else []
    if len(bid_prices) >= 10 and bid_prices[9] > 0:
        return round(float(bid_prices[9]), 2), "买10"
    if len(bid_prices) >= 5 and bid_prices[4] > 0:
        return round(float(bid_prices[4]), 2), "买5"
    if bid_prices and bid_prices[0] > 0:
        return round(float(bid_prices[0]), 2), "买1(盘口深度不足)"
    last = float(getattr(quote, "last_price", 0.0) or 0.0) if quote else 0.0
    if last > 0:
        return round(last, 2), "最新价(无盘口)"
    return 0.0, ""


def _cancel_own_takeprofit_orders(adapter: Any, ts_codes: set[str], wait_sec: float = 6.0) -> None:
    """平仓前撤掉目标标的的"盘中止盈"活卖单，释放冻结股份。

    不先撤会两头堵：QMT可用股数不足拒卖（SELL_VOLUME_NOT_AVAILABLE），
    gateway 重复单校验也会拒（DUPLICATE_ACTIVE_ORDER）。
    撤单成败以订单状态为准（2026-07-10 事故口径），最长等 wait_sec 秒。
    调用者需持有 _qmt_lock（RLock 可重入）。
    """
    from src.qmt_adapter import object_to_dict, first_present, to_int
    log = logger()
    try:
        orders = adapter.query_orders()
    except Exception as e:
        log.warning("[平仓前置] 查询委托失败（%s），跳过止盈单清理。", e)
        return
    shorts = {str(c).split(".")[0] for c in ts_codes}
    targets = []
    for o in orders or []:
        od = object_to_dict(o)
        remark = str(first_present(od, ["order_remark", "m_strRemark", "remark"], ""))
        if not remark.startswith("盘中止盈"):
            continue
        code = str(first_present(od, ["stock_code", "m_strInstrumentID", "ts_code"], "")).upper().split(".")[0]
        st = to_int(first_present(od, ["order_status", "m_nOrderStatus", "status"], -1), -1)
        if code in shorts and st in (48, 49, 50, 51, 52, 55):
            targets.append((code, str(first_present(od, ["order_id", "m_nOrderID"], ""))))
    for code, oid in targets:
        log.warning("[平仓前置] %s 存在未撤的盘中止盈卖单(order_id=%s)，先撤单释放冻结股份。", code, oid)
        try:
            adapter.cancel_order(oid)
        except Exception as exc:
            log.warning("[平仓前置] 撤止盈卖单失败，继续等待撤单结果：%s", exc)
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                cur = adapter.query_orders()
            except Exception:
                continue
            st = -2
            terminal_filled = 0
            for o in cur or []:
                od = object_to_dict(o)
                if str(first_present(od, ["order_id", "m_nOrderID"], "")) == oid:
                    st = to_int(first_present(od, ["order_status", "m_nOrderStatus", "status"], -1), -1)
                    ordered = max(to_int(first_present(
                        od, ["order_volume", "m_nOrderVolume", "volume", "order_qty"], 0
                    ), 0), 0)
                    traded = max(to_int(first_present(
                        od, ["traded_volume", "m_nTradedVolume", "traded_qty", "deal_volume"], 0
                    ), 0), 0)
                    if st == 56:
                        try:
                            intent = _exit_intent_by_broker_order_id(oid)
                        except Exception:
                            intent = None
                        terminal_filled = max(
                            ordered,
                            max(int((intent or {}).get("quantity", 0) or 0), 0),
                        )
                    else:
                        terminal_filled = min(traded, ordered) if ordered > 0 else traded
                    break
            if st in (53, 54, 56, 57):
                _resolve_exit_intent_by_broker_order_id(
                    oid,
                    filled_qty=terminal_filled,
                    terminal_known=True,
                )
                log.info("[平仓前置] %s 止盈单已了结（状态%s），继续平仓。", code, st)
                break
        else:
            log.error("[平仓前置] %s 止盈单撤单超时未确认，平仓可能因股份冻结被拒！", code)


def _max_sell_order_shares(ts_code: str) -> int:
    """交易所限价卖单单笔上限：科创板10万、创业板30万、其余100万股。"""
    short = str(ts_code).upper().split(".")[0]
    if short.startswith(("688", "689")):
        return 100_000
    if short.startswith(("300", "301")):
        return 300_000
    return 1_000_000


def _split_sell_order_quantities(ts_code: str, quantity: int) -> list[int]:
    """把大额卖出拆到交易所单笔上限内，不丢失最终零股余量。"""
    remaining = max(int(quantity), 0)
    limit = _max_sell_order_shares(ts_code)
    short = str(ts_code).upper().split(".")[0]
    is_star = short.startswith(("688", "689"))
    chunks: list[int] = []
    while remaining > 0:
        chunk = min(remaining, limit)
        if remaining > limit:
            if is_star:
                # 科创板限价单最少200股、之后可按1股递增。避免先报满10万后
                # 留下1~199股尾单（多张同时提交时柜台可能不把它视为余额）。
                tail = remaining - chunk
                if 0 < tail < 200:
                    chunk -= 200 - tail
            else:
                chunk = chunk // 100 * 100
        if chunk <= 0:
            break
        chunks.append(chunk)
        remaining -= chunk
    return chunks


def _normalize_exit_slice_quantity(ts_code: str, desired_qty: int, remaining_qty: int) -> int:
    """把POV单片股数规范到板块卖出申报单位，并保护最后零股余额。"""
    desired = min(max(int(desired_qty or 0), 0), max(int(remaining_qty or 0), 0))
    remaining = max(int(remaining_qty or 0), 0)
    short = str(ts_code).upper().split(".")[0]
    if short.startswith(("688", "689")):
        if remaining < 200:
            return remaining if desired >= remaining else 0
        return desired if desired >= 200 else 0
    if remaining >= 100:
        return desired // 100 * 100
    return remaining if desired >= remaining else 0


def _abc_place_sell_order_direct(
    ts_code: str, name: str, shares: int, order_id: str,
    confirm: str, config: dict, broker_cfg: dict, strategy_leg: str = ""
) -> bool:
    """串行化A/C/D/E整段卖出生命周期；RLock允许14:55主任务重入。"""
    resolved_leg = str(strategy_leg or "").upper()
    if not resolved_leg:
        for row in load_positions():
            if (
                str(row.get("order_id", "")) == str(order_id)
                and str(row.get("ts_code", "")).upper() == str(ts_code).upper()
            ):
                resolved_leg = str(row.get("strategy_leg", "")).upper()
                break
    with _exit_sell_lock:
        return _abc_place_sell_order_direct_locked(
            ts_code,
            name,
            shares,
            order_id,
            confirm,
            config,
            broker_cfg,
            resolved_leg,
        )


def _abc_place_sell_order_direct_locked(
    ts_code: str, name: str, shares: int, order_id: str,
    confirm: str, config: dict, broker_cfg: dict, strategy_leg: str = ""
) -> bool:
    """A/C/D/E按交易阶段有效限价卖实际余仓，不走旧CSV卖出数量。

    超过交易所单笔股数上限时先拆成多张委托；成交按所有子单聚合，全成回写
    closed，部成只回写真实剩余股数。返回True仅表示本轮已全部成交。
    """
    from src.broker_adapter import OrderRequest
    from src.live_order_gateway import LiveOrderGateway

    today_str = today_beijing().strftime("%Y%m%d")
    log = logger()

    blocked = _exit_order_submission_block_reason()
    if blocked:
        log.error("[ABC平仓] %s 禁止提交：%s。", ts_code, blocked)
        return False

    gateway = LiveOrderGateway(PROJECT_ROOT / "config" / "config.json")
    try:
        gateway.assert_real_order_allowed(confirm, side="SELL")
    except RuntimeError as e:
        log.error("❌ [ABC平仓] 下单条件不满足：%s", e)
        _notify("sell_fail", "❌ ABC平仓条件不满足",
                f"{ts_code} {name} 平仓未提交：{e}。请立即回终端核对。",
                level="critical", call=True)
        return False

    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        _cancel_own_takeprofit_orders(adapter, {ts_code})
        broker_total, broker_can_use = _broker_position_quantities(adapter, ts_code)
        orders = adapter.query_orders()

    if broker_total <= 0:
        log.error("ABC平仓：%s 本地%d股但券商查询不到实际持仓，拒绝盲目回写，请核对。", ts_code, shares)
        _notify("sell_fail", "❌ ABC平仓股数异常",
                f"{ts_code} {name} 本地记录{shares}股但QMT未返回实际持仓，平仓未提交，请立即核对。",
                level="critical", call=True)
        return False
    if broker_can_use <= 0:
        log.error("ABC平仓：%s 券商总持仓%d股但可卖为0，可能仍有冻结委托。", ts_code, broker_total)
        _notify("sell_fail", "❌ 平仓股份仍被冻结",
                f"{ts_code} {name} QMT总持仓{broker_total}股、可卖0股，请立即检查未撤卖单。",
                level="critical", call=True)
        return False
    if shares <= 0:
        log.error("ABC平仓：%s 本地策略持仓股数为0，拒绝提交卖单。", ts_code)
        return False
    owned_before = shares
    shares = _safe_new_exit_order_quantity(
        ts_code,
        requested_qty=shares,
        broker_total=broker_total,
        broker_can_use=broker_can_use,
        orders=orders,
        trade_date=today_str,
        log=log,
        phase="ABC主平仓",
    )
    if shares <= 0:
        log.error("ABC平仓：%s 安全归属/意图预算后的可新发股数为0，拒绝提交卖单。", ts_code)
        return False

    now_t = now_beijing().time()
    if SCHED_CLOSE_AUCTION_HANDOFF <= now_t < datetime.time(14, 57):
        log.error(
            "[ABC平仓] %s 已进入14:56:20撤单交接窗，禁止新发连续竞价单；"
            "交由14:57:05看门狗按真实残仓申报。",
            ts_code,
        )
        return False

    chunks = _split_sell_order_quantities(ts_code, shares)
    submitted: list[tuple[str, int, str]] = []
    rejected_chunks = 0
    price = 0.0
    price_label = ""
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        # 持仓、活跃委托和安全数量检查完成后，紧邻真正申报重新取行情。
        # 避免用前面多次QMT查询之前的旧买一计算动态价格。
        quote = adapter.get_full_tick([ts_code]).get(ts_code)
        price, price_label = _pick_sell_limit_price(quote)
        for chunk_no, chunk in enumerate(chunks if price > 0 else [], start=1):
            blocked = _exit_order_submission_block_reason()
            if blocked:
                rejected_chunks += len(chunks) - chunk_no + 1
                log.error(
                    "[ABC平仓] %s 拆单过程触发时间门禁(%s)，停止提交剩余%d张；"
                    "已提交部分由intent/看门狗对账。",
                    ts_code,
                    blocked,
                    len(chunks) - chunk_no + 1,
                )
                break
            request = OrderRequest(
                ts_code=ts_code,
                broker_code=ts_code,
                side="SELL",
                quantity=chunk,
                price_type="FIXED_PRICE",
                price=price,
                strategy_name="A_SYSTEM_ABC",
                remark=f"ABC平仓-{price_label}-{today_str}-{chunk_no}/{len(chunks)}",
                strategy_leg=str(strategy_leg or "").upper(),
                business_date=today_str,
                purpose="CLOSE",
                source_key=(
                    f"ABC_CLOSE|{today_str}|{order_id}|{ts_code}|"
                    f"{chunk_no}/{len(chunks)}"
                ),
                metadata={"execution_channel": "ABC主平仓", "name": name},
            )
            result, intent_token = _place_exit_order_with_intent(
                adapter,
                request,
                phase="ABC主平仓",
                local_order_id=order_id,
            )
            if result.accepted:
                submitted.append((str(result.order_id or ""), chunk, intent_token))
            else:
                rejected_chunks += 1
                log.error(
                    "❌ [ABC平仓] %s %s 第%d/%d子单%d股提交失败：%s",
                    ts_code, name, chunk_no, len(chunks), chunk, result.message,
                )

    if price <= 0:
        log.warning("ABC平仓：%s 提交前无法获取价格，跳过本次。", ts_code)
        _notify("sell_fail", "❌ ABC平仓无报价",
                f"{ts_code} {name} 提交前无法获取有效卖出价格，平仓未提交，请立即回终端核对。",
                level="critical", call=True)
        return False

    if not submitted:
        _notify("sell_fail", "❌ ABC平仓提交失败",
                f"{ts_code} {name} 全部平仓子单均提交失败，请立即回终端处理。",
                level="critical", call=True)
        return False

    submitted_qty = sum(qty for _, qty, _ in submitted)
    log.info(
        "✅ [ABC平仓] %s %s %d股拆%d张 %s@%.2f 委托号已返回（拒绝%d张，待成交确认）",
        ts_code, name, submitted_qty, len(submitted), price_label, price, rejected_chunks,
    )
    confirmation_budget_sec = 60 + max(len(submitted) - 1, 0) * 5 + 10
    now_for_confirm = now_beijing()
    handoff_time = _configured_exit_pov_handoff_time(config.get("live_trade", {}))
    handoff_at = datetime.datetime.combine(
        now_for_confirm.date(), handoff_time, tzinfo=BEIJING_TZ
    )
    seconds_to_handoff = (handoff_at - now_for_confirm).total_seconds()
    if (
        now_for_confirm.time() >= SCHED_AFTERNOON_CLOSE
        or seconds_to_handoff <= confirmation_budget_sec
    ):
        # 不仅14:55主任务：任何在理论确认耗时内可能跨过配置交权点
        # 的调用（例如14:54启动补检），都只做“核仓→安全预算→
        # 拆单快速提交”。绝不在全局卖出锁内逐张等待，否则会饿死
        # 14:56:20撤单交接。成交回写交给看门狗逐轮即时确认，15:00:30
        # 双快照只保留作最终兜底。
        current_position = next(
            (
                row for row in load_positions()
                if str(row.get("order_id", "")) == str(order_id)
            ),
            None,
        ) or {
            "order_id": order_id,
            "ts_code": ts_code,
            "name": name,
            "shares": owned_before,
            "status": "open",
            "planned_exit_date": today_str,
            "strategy_leg": str(strategy_leg or "").upper(),
        }
        _queue_watchdog_pending(
            current_position,
            [
                (broker_order_id, expected_qty)
                for broker_order_id, expected_qty, _ in submitted
            ],
        )
        log.warning(
            "[ABC平仓] %s %d股已快速提交；成交确认移交收盘看门狗，"
            "卖出锁已释放以保证14:56:20交接和后续标的。",
            ts_code,
            submitted_qty,
        )
        return False
    total_filled = 0
    total_amount = 0.0
    last_status = "UNKNOWN"
    for idx, (broker_order_id, expected_qty, intent_token) in enumerate(submitted):
        fill = _confirm_fill(
            broker_cfg,
            broker_order_id,
            expected_qty,
            f"ABC平仓子单{idx + 1}/{len(submitted)}",
            timeout_sec=60 if idx == 0 else 5,
        )
        filled = min(max(int(fill.filled_qty or 0), 0), expected_qty)
        fill_price_one = float(fill.avg_price or 0.0) if float(fill.avg_price or 0.0) > 0 else price
        total_filled += filled
        total_amount += filled * fill_price_one
        last_status = fill.status_text
        if bool(getattr(fill, "is_terminal", False)):
            _update_exit_order_intent(
                intent_token,
                status="RESOLVED",
                broker_order_id=broker_order_id,
                filled_qty=filled,
                terminal_known=True,
            )
    fill_price = total_amount / total_filled if total_filled > 0 else price
    remaining_after = max(owned_before - total_filled, 0)
    if remaining_after == 0:
        _mark_live_position_closed(
            order_id, today_str, fill_price, recovery_context="ABC主平仓"
        )
        log.info("✅ [ABC平仓] %s %s 全部成交 %d股 @%.2f，已平仓。", ts_code, name, total_filled, fill_price)
        _notify("sell_success", "✅ 平仓成交",
                f"{ts_code} {name} 卖出{total_filled}股 "
                f"@{fill_price:.2f} 金额{_fmt_wan(total_filled * fill_price)}")
        return True
    elif total_filled > 0:
        _reduce_live_position_shares(
            order_id,
            remaining_after,
            fill_price=fill_price,
            recovery_context="ABC主平仓",
        )
        log.warning("⚠️ [ABC平仓] %s %s 部分成交 %d/%d股 @%.2f，券商预计剩余%d股。",
                    ts_code, name, total_filled, owned_before, fill_price, remaining_after)
        _notify("sell_fail", "⚠️ 平仓部分成交",
                f"{ts_code} {name} 卖出{total_filled}/{owned_before}股 @{fill_price:.2f}，"
                f"预计剩余{remaining_after}股，请回终端查看。", level="timeSensitive")
        return False
    else:
        log.error("❌ [ABC平仓] %s %s 未成交（状态=%s），持仓保留，等待下次重试或手动处理。",
                  ts_code, name, last_status)
        _notify("sell_fail", "❌ 平仓未成交",
                f"{ts_code} {name} 平仓委托未成交，可能被动过夜，请立即回终端处理。",
                level="critical", call=True)
        return False


def _broker_position_quantities(adapter: Any, ts_code: str) -> tuple[int, int]:
    """返回券商端该标的(总持仓, 可卖持仓)，找不到时返回(0, 0)。"""
    from src.qmt_adapter import first_present, object_to_dict, to_int

    aliases = _ts_code_aliases(ts_code)
    for item in adapter.query_positions() or []:
        data = object_to_dict(item)
        code = first_present(data, ["ts_code", "stock_code", "m_strInstrumentID"], "")
        if not (_ts_code_aliases(code) & aliases):
            continue
        volume = to_int(first_present(data, ["volume", "m_nVolume", "total_volume"], 0), 0)
        can_use = to_int(
            first_present(data, ["can_use_volume", "m_nCanUseVolume", "enable_amount"], 0), 0
        )
        return max(volume, 0), max(can_use, 0)
    return 0, 0


_exit_safety_alerted: set[str] = set()


def _exit_batch_key(trade_date: str, ts_code: str) -> str:
    return f"{str(trade_date).replace('-', '')[:8]}|{str(ts_code).upper().split('.')[0]}"


def _local_exit_quantities(ts_code: str, trade_date: str) -> tuple[int, int]:
    """返回(今日到期策略股数, 该代码全部本地未平策略股数)。"""
    short = str(ts_code).upper().split(".")[0]
    date_key = str(trade_date).replace("-", "")[:8]
    due_qty = 0
    all_open_qty = 0
    for pos in load_positions():
        if str(pos.get("status", "")).lower() not in {"open", "sell_pending"}:
            continue
        if str(pos.get("ts_code", "")).upper().split(".")[0] != short:
            continue
        qty = max(int(pos.get("shares", 0) or 0), 0)
        all_open_qty += qty
        planned = str(pos.get("planned_exit_date", "99991231")).replace("-", "")[:8]
        if planned <= date_key:
            due_qty += qty
    return due_qty, all_open_qty


def _alert_exit_safety_once(ts_code: str, reason_key: str, title: str, body: str) -> None:
    key = f"{today_beijing().strftime('%Y%m%d')}|{str(ts_code).upper()}|{reason_key}"
    if key in _exit_safety_alerted:
        return
    _exit_safety_alerted.add(key)
    _notify("sell_fail", title, body, level="critical", call=True)


def _ensure_exit_batch(
    ts_code: str,
    *,
    broker_total: int,
    trade_date: str,
    log: Any,
    target_qty_override: int = 0,
) -> dict[str, Any] | None:
    """建立不可变的当日退出目标与非目标仓保护底线。

    首笔系统卖单前，QMT总持仓必须与本地全部未平策略仓精确相等。由此确认
    账户没有同代码人工/其他策略混仓，并把非今日到期仓记为永不触碰的floor。
    状态一经建立持久化，daemon重启后沿用同一target，不能按滞后本地账重建。
    """
    date_key = str(trade_date).replace("-", "")[:8]
    key = _exit_batch_key(date_key, ts_code)
    account_fingerprint = _exit_account_fingerprint()
    try:
        state = _load_exit_execution_state()
    except Exception as exc:
        log.error("[退出安全账本] %s，阻断自动卖出。", exc)
        _alert_exit_safety_once(
            ts_code,
            "state_corrupt",
            "🛑 退出安全账本异常",
            f"{ts_code} 无法读取退出安全账本，系统已阻断自动卖出，请立即核查。",
        )
        return None
    existing = state["batches"].get(key)
    if isinstance(existing, dict):
        if str(existing.get("account_fingerprint", "")) != account_fingerprint:
            log.error(
                "🛑 [退出安全账本] %s 当日批次属于另一账户指纹；禁止复用后自动卖出。",
                ts_code,
            )
            _alert_exit_safety_once(
                ts_code,
                "account_switched",
                "🛑 检测到交易账户切换",
                f"{ts_code} 当日退出批次与当前QMT账户不一致；请人工核对并重建安全批次。",
            )
            return None
        requested_override = max(int(target_qty_override or 0), 0)
        if requested_override > 0 and int(existing.get("target_qty", 0) or 0) != requested_override:
            log.error(
                "🛑 [退出安全账本] %s 已有批次目标%d股，与D接力锁定目标%d股不一致，禁止复用。",
                ts_code,
                int(existing.get("target_qty", 0) or 0),
                requested_override,
            )
            return None
        return existing

    due_qty, all_open_qty = _local_exit_quantities(ts_code, date_key)
    # D接力发生在默认T+2到期日前，因此不能用“今日到期股数”建退出批次。
    # 调用方只能在已经锁定D本地订单、并核对券商同代码总仓等于本地全部策略仓后，
    # 显式传入接力目标。普通平仓调用不传值，继续沿用原来的到期股数口径。
    relay_target = max(int(target_qty_override or 0), 0)
    target_qty = relay_target if relay_target > 0 else due_qty
    if target_qty <= 0 or all_open_qty <= 0 or target_qty > all_open_qty:
        log.error(
            "[退出安全账本] %s 本地今日到期%d股/接力目标%d股/全部未平%d股，无法建立退出批次。",
            ts_code, due_qty, relay_target, all_open_qty,
        )
        return None
    if max(int(broker_total or 0), 0) != all_open_qty:
        log.error(
            "🛑 [退出安全账本] %s 首单前QMT总持仓%d股≠本地全部策略仓%d股；"
            "疑似同代码人工混仓、成交回写延迟或QMT快照异常，自动卖出fail-closed。",
            ts_code,
            broker_total,
            all_open_qty,
        )
        _alert_exit_safety_once(
            ts_code,
            "ownership_mismatch",
            "🛑 同代码仓位归属不清",
            f"{ts_code} QMT总持仓{broker_total}股、本地策略仓{all_open_qty}股不一致；"
            "为防误卖人工/其他策略仓，自动平仓已阻断，请立即人工核对。",
        )
        return None

    batch = {
        "trade_date": date_key,
        "ts_code": str(ts_code).upper(),
        "target_qty": target_qty,
        "non_target_floor": max(all_open_qty - target_qty, 0),
        "verified_broker_total": all_open_qty,
        "target_source": "D_RELAY" if relay_target > 0 else "DUE_EXIT",
        "account_fingerprint": account_fingerprint,
        "created_at": now_beijing().isoformat(),
    }
    # 与另一个卖出线程竞争时必须在同一把状态锁内重新读取、只创建一次。
    with _exit_execution_state_lock:
        state = _load_exit_execution_state()
        if key not in state["batches"]:
            state["batches"][key] = batch
            _save_exit_execution_state(state)
        else:
            batch = state["batches"][key]
            if str(batch.get("account_fingerprint", "")) != account_fingerprint:
                log.error(
                    "🛑 [退出安全账本] %s 并发建批时检测到另一账户批次，阻断卖出。",
                    ts_code,
                )
                return None
    log.info(
        "[退出安全账本] %s 建立批次：今日目标%d股，非目标仓保护底线%d股。",
        ts_code,
        int(batch.get("target_qty", 0) or 0),
        int(batch.get("non_target_floor", 0) or 0),
    )
    return batch


def _order_is_sell(data: dict[str, Any]) -> bool:
    from src.qmt_adapter import first_present, to_int

    side = str(first_present(data, ["side", "order_side", "direction"], "")).upper()
    order_type = to_int(first_present(data, ["order_type", "m_nOrderType"], -1), -1)
    return order_type == 24 or side in {"SELL", "24", "卖出"}


def _order_is_system_exit(data: dict[str, Any]) -> bool:
    from src.qmt_adapter import first_present

    remark = str(first_present(data, ["order_remark", "m_strRemark", "remark"], ""))
    return remark.startswith(_SYSTEM_EXIT_REMARK_PREFIXES)


def _order_commitment(data: dict[str, Any], fallback_qty: int = 0) -> tuple[int, int, int]:
    """返回(已耗用, 活跃预留, 未知预留)；未知永远按最坏情况占用。"""
    from src.qmt_adapter import first_present, to_int

    active_states = {48, 49, 50, 51, 52, 55}
    terminal_states = {53, 54, 56, 57}
    status = to_int(first_present(data, ["order_status", "m_nOrderStatus", "status"], -1), -1)
    ordered = max(
        to_int(
            first_present(
                data,
                ["order_volume", "m_nOrderVolume", "volume", "order_qty"],
                fallback_qty,
            ),
            fallback_qty,
        ),
        max(int(fallback_qty or 0), 0),
    )
    traded = min(
        max(
            to_int(
                first_present(
                    data,
                    ["traded_volume", "m_nTradedVolume", "traded_qty", "deal_volume"],
                    0,
                ),
                0,
            ),
            0,
        ),
        ordered,
    )
    # QMT状态56=全部成交；即使成交明细/字段尚未同步，也必须按整单已耗用，
    # 否则14:57会把“晚成但未回写”的数量再次补卖。
    if status == 56:
        return ordered, 0, 0
    if status in active_states:
        return traded, max(ordered - traded, 0), 0
    if status in terminal_states:
        return traded, 0, 0
    return traded, 0, max(ordered - traded, 0)


def _exit_commitments_by_code(
    ts_code: str,
    *,
    trade_date: str,
    orders: Any,
) -> tuple[int, int, int]:
    """合并持久化intent与QMT订单，且不重复计算同一broker order id。"""
    from src.qmt_adapter import first_present, object_to_dict, to_int

    short = str(ts_code).upper().split(".")[0]
    date_key = str(trade_date).replace("-", "")[:8]
    state = _load_exit_execution_state()
    account_fingerprint = _exit_account_fingerprint()
    order_rows: list[dict[str, Any]] = [object_to_dict(item) for item in (orders or [])]
    by_id: dict[str, dict[str, Any]] = {}
    for data in order_rows:
        for name in ("order_id", "m_nOrderID", "order_sysid", "m_strOrderSysID"):
            oid = str(data.get(name, "") or "").strip()
            if oid:
                by_id[oid] = data

    # 先一次性认领当前账户/日期/股票下所有已绑定委托，不能依赖intent遍历
    # 顺序。否则“无单号intent在前、已绑定intent在后”时，前者会把后者的
    # 同代码/同remark/同数量委托也纳入候选，造成歧义或错误归属。
    claimed_broker_order_ids = {
        str(intent.get("broker_order_id", "") or "").strip()
        for intent in state.get("intents", [])
        if isinstance(intent, dict)
        and str(intent.get("trade_date", "")).replace("-", "")[:8] == date_key
        and str(intent.get("account_fingerprint", "")) == account_fingerprint
        and str(intent.get("ts_code", "")).upper().split(".")[0] == short
        and str(intent.get("broker_order_id", "") or "").strip()
    }

    filled = active = unknown = 0
    matched_object_ids: set[int] = set()
    for intent in state.get("intents", []):
        if not isinstance(intent, dict):
            continue
        if str(intent.get("trade_date", "")) != date_key:
            continue
        if str(intent.get("account_fingerprint", "")) != account_fingerprint:
            continue
        if str(intent.get("ts_code", "")).upper().split(".")[0] != short:
            continue
        qty = max(int(intent.get("quantity", 0) or 0), 0)
        if qty <= 0 or str(intent.get("status", "")).upper() == "REJECTED":
            continue
        oid = str(intent.get("broker_order_id", "") or "").strip()
        data = by_id.get(oid) if oid else None
        if data is None and not oid:
            # 柜台可能已受理，但place_order返回时单号为空或进程在
            # 回写单号前崩溃。只在“代码+系统remark+精确数量”唯一
            # 匹配时恢复该单；多个候选时继续按PREPARED全量未知占用。
            intent_remark = str(intent.get("remark", "") or "")
            recovery_candidates: list[dict[str, Any]] = []
            for candidate in order_rows:
                if id(candidate) in matched_object_ids:
                    continue
                candidate_oid = str(
                    first_present(
                        candidate,
                        ["order_id", "m_nOrderID", "order_sysid", "m_strOrderSysID"],
                        "",
                    )
                    or ""
                ).strip()
                if candidate_oid and candidate_oid in claimed_broker_order_ids:
                    continue
                candidate_code = str(
                    first_present(candidate, ["stock_code", "m_strInstrumentID", "ts_code"], "")
                ).upper().split(".")[0]
                candidate_remark = str(
                    first_present(candidate, ["order_remark", "m_strRemark", "remark"], "")
                )
                candidate_qty = max(
                    to_int(
                        first_present(
                            candidate,
                            ["order_volume", "m_nOrderVolume", "volume", "order_qty"],
                            0,
                        ),
                        0,
                    ),
                    0,
                )
                if (
                    candidate_code == short
                    and intent_remark
                    and candidate_remark == intent_remark
                    and candidate_qty == qty
                    and _order_is_sell(candidate)
                    and _order_is_system_exit(candidate)
                ):
                    recovery_candidates.append(candidate)
            if len(recovery_candidates) == 1:
                data = recovery_candidates[0]
                recovered_oid = str(
                    first_present(
                        data,
                        ["order_id", "m_nOrderID", "order_sysid", "m_strOrderSysID"],
                        "",
                    )
                    or ""
                ).strip()
                if recovered_oid and str(intent.get("token", "")):
                    claimed_broker_order_ids.add(recovered_oid)
                    _update_exit_order_intent(
                        str(intent.get("token", "")),
                        status=str(intent.get("status", "PREPARED") or "PREPARED"),
                        broker_order_id=recovered_oid,
                    )
        if str(intent.get("status", "")).upper() == "RESOLVED" and bool(
            intent.get("terminal_known")
        ):
            # 本地已确认终态后必须保持单调；QMT列表里偶发的旧/回退字段不能
            # 把RESOLVED重新降级成active/unknown。但当QMT后续给出明确终态且
            # 成交量更大时，必须允许向上修正，否则“终态先到、成交字段
            # 后到”会永久少计成交。
            f = min(max(int(intent.get("filled_qty", 0) or 0), 0), qty)
            a = u = 0
            if data is not None:
                matched_object_ids.add(id(data))
                qmt_status = to_int(
                    first_present(data, ["order_status", "m_nOrderStatus", "status"], -1),
                    -1,
                )
                if qmt_status in {53, 54, 56, 57}:
                    qmt_filled, _, _ = _order_commitment(data, qty)
                    if qmt_filled > f:
                        f = min(qmt_filled, qty)
                        _update_exit_order_intent(
                            str(intent.get("token", "")),
                            status="RESOLVED",
                            broker_order_id=oid,
                            filled_qty=f,
                            terminal_known=True,
                        )
        elif data is not None:
            f, a, u = _order_commitment(data, qty)
            matched_object_ids.add(id(data))
        else:
            # PREPARED/SUBMITTED但QMT暂时查不到：可能已受理，必须全量占用。
            f = a = 0
            u = qty
        filled += f
        active += a
        unknown += u

    # 兼容升级前已经存在、尚未写入intent账本的本系统卖单。
    for data in order_rows:
        if id(data) in matched_object_ids or not _order_is_sell(data) or not _order_is_system_exit(data):
            continue
        code = str(
            first_present(data, ["stock_code", "m_strInstrumentID", "ts_code"], "")
        ).upper().split(".")[0]
        if code != short:
            continue
        f, a, u = _order_commitment(data)
        filled += f
        active += a
        unknown += u
    return filled, active, unknown


def _non_system_active_sell_exists(ts_code: str, orders: Any) -> bool:
    from src.qmt_adapter import first_present, object_to_dict, to_int

    short = str(ts_code).upper().split(".")[0]
    terminal_states = {53, 54, 56, 57}
    for order in orders or []:
        data = object_to_dict(order)
        status = to_int(first_present(data, ["order_status", "m_nOrderStatus", "status"], -1), -1)
        code = str(
            first_present(data, ["stock_code", "m_strInstrumentID", "ts_code"], "")
        ).upper().split(".")[0]
        if (
            code == short
            # 非系统SELL只有明确终态才可忽略。255/-1/缺字段等未知状态也必须
            # fail-closed，不能假设它不再占用或不会晚成。
            and status not in terminal_states
            and _order_is_sell(data)
            and not _order_is_system_exit(data)
        ):
            return True
    return False


def _safe_new_exit_order_quantity(
    ts_code: str,
    *,
    requested_qty: int,
    broker_total: int,
    broker_can_use: int,
    orders: Any,
    trade_date: str,
    log: Any,
    phase: str,
    target_qty_override: int = 0,
) -> int:
    """计算本轮最多可新发数量；归属、订单状态或账本不明确即返回0。"""
    requested = max(int(requested_qty or 0), 0)
    if requested <= 0:
        return 0
    batch = _ensure_exit_batch(
        ts_code,
        broker_total=max(int(broker_total or 0), 0),
        trade_date=trade_date,
        log=log,
        target_qty_override=target_qty_override,
    )
    if batch is None:
        return 0
    if _non_system_active_sell_exists(ts_code, orders):
        log.error(
            "🛑 [%s] %s 存在非本系统活跃SELL；无法判定其覆盖哪一批仓位，禁止自动叠单。",
            phase,
            ts_code,
        )
        _alert_exit_safety_once(
            ts_code,
            "foreign_active_sell",
            "🛑 检测到人工卖单",
            f"{ts_code} 存在非本系统活跃卖单；为防卖到后续仓或人工仓，自动补单已阻断。",
        )
        return 0
    try:
        consumed, active_reserved, unknown_reserved = _exit_commitments_by_code(
            ts_code,
            trade_date=trade_date,
            orders=orders,
        )
    except Exception as exc:
        log.error("🛑 [%s] %s 退出意图核算失败:%s", phase, ts_code, exc)
        return 0
    if unknown_reserved > 0:
        log.error(
            "🛑 [%s] %s 有%d股PREPARED/订单缺失的未知占用；按全量预留，绝不重复覆盖。",
            phase,
            ts_code,
            unknown_reserved,
        )
        _alert_exit_safety_once(
            ts_code,
            "unknown_exit_intent",
            "🛑 平仓订单状态未知",
            f"{ts_code} 有{unknown_reserved}股系统卖单处于PREPARED或QMT暂不可见状态；"
            "系统已按全量占用防重，请立即核对QMT委托。",
        )

    target = max(int(batch.get("target_qty", 0) or 0), 0)
    floor = max(int(batch.get("non_target_floor", 0) or 0), 0)
    if consumed > target:
        log.error(
            "🛑 [%s] %s 系统已成%d股超过批次目标%d股，禁止继续下单。",
            phase,
            ts_code,
            consumed,
            target,
        )
        return 0
    expected_broker_total = floor + target - consumed
    actual_total = max(int(broker_total or 0), 0)
    if actual_total != expected_broker_total:
        log.error(
            "🛑 [%s] %s QMT总持仓%d股≠安全账本期望%d股"
            "(目标%d-已成%d+保护底线%d)；疑似回写延迟/人工混仓/QMT不同步，fail-closed。",
            phase,
            ts_code,
            actual_total,
            expected_broker_total,
            target,
            consumed,
            floor,
        )
        _alert_exit_safety_once(
            ts_code,
            "runtime_ownership_mismatch",
            "🛑 平仓数量无法安全归属",
            f"{ts_code} QMT总持仓{actual_total}股与退出账本期望{expected_broker_total}股不一致；"
            "系统已停止自动卖出，请立即核对成交与人工仓。",
        )
        return 0
    target_budget = max(target - consumed - active_reserved - unknown_reserved, 0)
    broker_budget = max(actual_total - floor - active_reserved - unknown_reserved, 0)
    safe = min(
        requested,
        target_budget,
        broker_budget,
        max(int(broker_can_use or 0), 0),
    )
    if safe < requested:
        log.warning(
            "[%s] %s 新单%d股缩至%d股：目标%d/已成%d/活跃预留%d/未知预留%d/可卖%d。",
            phase,
            ts_code,
            requested,
            safe,
            target,
            consumed,
            active_reserved,
            unknown_reserved,
            broker_can_use,
        )
    return max(int(safe), 0)


def _prepare_exit_order_intent(
    request: Any,
    *,
    phase: str,
    local_order_id: str = "",
) -> str:
    """在place_order之前原子写PREPARED；写失败必须让下单失败。"""
    trade_date = today_beijing().strftime("%Y%m%d")
    key = _exit_batch_key(trade_date, str(request.ts_code))
    account_fingerprint = _exit_account_fingerprint()
    token = (
        f"{trade_date}-{str(request.ts_code).upper().split('.')[0]}-"
        f"{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    )
    with _exit_execution_state_lock:
        state = _load_exit_execution_state()
        batch = state.get("batches", {}).get(key)
        if not isinstance(batch, dict):
            raise RuntimeError(f"退出批次{key}尚未建立，拒绝写下单intent")
        if str(batch.get("account_fingerprint", "")) != account_fingerprint:
            raise RuntimeError(f"退出批次{key}与当前账户不匹配，拒绝下单")
        quantity = max(int(request.quantity or 0), 0)
        if quantity <= 0:
            raise RuntimeError("退出intent数量必须大于0")
        state["intents"].append(
            {
                "token": token,
                "batch_key": key,
                "trade_date": trade_date,
                "ts_code": str(request.ts_code).upper(),
                "account_fingerprint": account_fingerprint,
                "quantity": quantity,
                "price": float(getattr(request, "price", 0.0) or 0.0),
                "remark": str(getattr(request, "remark", "") or ""),
                "phase": phase,
                "local_order_id": str(local_order_id or ""),
                "status": "PREPARED",
                "broker_order_id": "",
                "filled_qty": 0,
                "terminal_known": False,
                "created_at": now_beijing().isoformat(),
            }
        )
        _save_exit_execution_state(state)
    return token


def _update_exit_order_intent(
    token: str,
    *,
    status: str,
    broker_order_id: str = "",
    filled_qty: int | None = None,
    terminal_known: bool | None = None,
) -> None:
    account_fingerprint = _exit_account_fingerprint()
    with _exit_execution_state_lock:
        state = _load_exit_execution_state()
        found = False
        for intent in state.get("intents", []):
            if str(intent.get("token", "")) != str(token):
                continue
            if str(intent.get("account_fingerprint", "")) != account_fingerprint:
                raise RuntimeError(f"退出intent属于另一账户:{token}")
            old_status = str(intent.get("status", "")).upper()
            requested_status = str(status).upper()
            quantity = max(int(intent.get("quantity", 0) or 0), 0)
            old_filled = min(max(int(intent.get("filled_qty", 0) or 0), 0), quantity)
            old_terminal = bool(intent.get("terminal_known"))

            # 委托状态只能单调前进。已确认的终态/成交量不能被
            # QMT滞后快照或其他线程回退，否则尾盘会把已成交数量重新补卖。
            if old_status in {"RESOLVED", "REJECTED"}:
                next_status = old_status
            else:
                next_status = requested_status
            next_terminal = old_terminal or bool(terminal_known)
            if next_terminal and next_status not in {"REJECTED", "RESOLVED"}:
                next_status = "RESOLVED"
            intent["status"] = next_status
            if broker_order_id:
                old_oid = str(intent.get("broker_order_id", "") or "").strip()
                if old_oid and old_oid != str(broker_order_id):
                    raise RuntimeError(
                        f"退出intent单号冲突:{token} {old_oid}!={broker_order_id}"
                    )
                intent["broker_order_id"] = str(broker_order_id)
            if filled_qty is not None:
                intent["filled_qty"] = min(
                    max(old_filled, max(int(filled_qty or 0), 0)),
                    quantity,
                )
            if terminal_known is not None:
                intent["terminal_known"] = next_terminal
            intent["updated_at"] = now_beijing().isoformat()
            found = True
            break
        if not found:
            raise RuntimeError(f"退出intent不存在:{token}")
        _save_exit_execution_state(state)


def _resolve_exit_intent_by_broker_order_id(
    broker_order_id: str,
    *,
    filled_qty: int,
    terminal_known: bool = True,
    business_date: str = "",
) -> bool:
    """按账户+交易日+券商单号释放终态intent，允许券商跨日复用单号。"""
    oid = str(broker_order_id or "").strip()
    if not oid:
        return False
    account_fingerprint = _exit_account_fingerprint()
    date_key = str(
        business_date or today_beijing().strftime("%Y%m%d")
    ).replace("-", "")[:8]
    with _exit_execution_state_lock:
        state = _load_exit_execution_state()
        matched = False
        for intent in reversed(state.get("intents", [])):
            if str(intent.get("broker_order_id", "") or "").strip() != oid:
                continue
            if str(intent.get("account_fingerprint", "")) != account_fingerprint:
                continue
            if str(intent.get("trade_date", "")).replace("-", "")[:8] != date_key:
                continue
            quantity = max(int(intent.get("quantity", 0) or 0), 0)
            old_filled = min(max(int(intent.get("filled_qty", 0) or 0), 0), quantity)
            intent["status"] = "RESOLVED"
            intent["filled_qty"] = min(
                max(old_filled, max(int(filled_qty or 0), 0)),
                quantity,
            )
            intent["terminal_known"] = bool(intent.get("terminal_known")) or bool(terminal_known)
            intent["updated_at"] = now_beijing().isoformat()
            matched = True
            break
        if matched:
            _save_exit_execution_state(state)
        return matched


def _exit_intent_by_broker_order_id(
    broker_order_id: str,
    *,
    business_date: str = "",
) -> dict[str, Any] | None:
    """按账户+交易日+券商单号返回intent；找不到时返回None。"""
    oid = str(broker_order_id or "").strip()
    if not oid:
        return None
    account_fingerprint = _exit_account_fingerprint()
    date_key = str(
        business_date or today_beijing().strftime("%Y%m%d")
    ).replace("-", "")[:8]
    state = _load_exit_execution_state()
    for intent in reversed(state.get("intents", [])):
        if (
            str(intent.get("broker_order_id", "") or "").strip() == oid
            and str(intent.get("account_fingerprint", "")) == account_fingerprint
            and str(intent.get("trade_date", "")).replace("-", "")[:8] == date_key
        ):
            return dict(intent)
    return None


def _apply_known_exit_fill(
    local_order_id: str,
    *,
    broker_order_id: str,
    current_shares: int,
    filled_qty: int,
    fill_price: float,
    fill_date: str,
) -> int:
    """幂等地把某张券商单的“新增成交量”应用到指定本地仓。

    QMT会在多轮查询中持续返回状态56的历史委托。如果每轮都把
    order_qty再扣一次，safe_qty小于本地持仓时会把真实余仓误标为
    closed。因此在positions.json内按broker_order_id持久化“已应用
    的累计成交量”，并在同一持仓文件事务中只扣增量。
    """
    oid = str(broker_order_id or "").strip()
    if not oid:
        raise RuntimeError("已知成交回写缺少broker_order_id，拒绝非幂等改账")
    confirmed_total = max(int(filled_qty or 0), 0)
    if confirmed_total <= 0:
        return 0

    with _positions_file_lock:
        positions = load_positions()
        target: dict[str, Any] | None = None
        for position in positions:
            if str(position.get("order_id", "")) == str(local_order_id):
                target = position
                break
        if target is None:
            raise RuntimeError(f"本地持仓不存在:{local_order_id}")

        applied_raw = target.get("applied_exit_fills_by_order")
        applied_map = dict(applied_raw) if isinstance(applied_raw, dict) else {}
        already_applied = max(int(applied_map.get(oid, 0) or 0), 0)
        delta = max(confirmed_total - already_applied, 0)
        if delta <= 0:
            return 0

        actual_held = max(int(target.get("shares", 0) or 0), 0)
        caller_held = max(int(current_shares or 0), 0)
        if caller_held != actual_held:
            logger().warning(
                "[成交回写] 持仓%s轮询快照%d股、文件实际%d股；以文件为准。",
                local_order_id,
                caller_held,
                actual_held,
            )
        applied = min(delta, actual_held)

        # 即使本地仓已为0，也记住该单累计已见成交量，
        # 防止状态回退后重复尝试改账。
        applied_map[oid] = max(already_applied, confirmed_total)
        target["applied_exit_fills_by_order"] = applied_map
        if applied > 0:
            remaining = actual_held - applied
            _append_exit_fill_ledger(
                target,
                fill_date=fill_date,
                filled_qty=applied,
                fill_price=fill_price,
            )
            target.setdefault("entry_shares", actual_held)
            target["shares"] = remaining
            if remaining == 0:
                target["status"] = "closed"
                target["sell_date"] = fill_date
                target["sell_price"] = _recover_sell_price_from_fills(target)
            else:
                target["status"] = "open"
        try:
            save_positions(positions)
        except Exception as exc:
            _critical_execution_state_failure("已知卖出成交投影", exc)
        return applied


def _exit_order_submission_block_reason(
    at: datetime.datetime | None = None,
) -> str:
    """返回平仓委托的硬时间门禁原因；空字符串表示允许。"""
    now = at or now_beijing()
    if not is_trade_day(now.date()):
        return "当前不是交易日"
    current = now.time()
    in_order_session = (
        datetime.time(9, 15) <= current <= datetime.time(11, 30)
        or datetime.time(13, 0) <= current < datetime.time(15, 0)
    )
    if not in_order_session:
        return f"当前{current.strftime('%H:%M:%S')}不在A股可申报窗口"
    if SCHED_CLOSE_AUCTION_HANDOFF <= current < datetime.time(14, 57):
        return "14:56:20-14:57:00为连续竞价撤单/收盘竞价交接静默窗"
    return ""


def _place_exit_order_with_intent(
    adapter: Any,
    request: Any,
    *,
    phase: str,
    local_order_id: str = "",
) -> tuple[Any, str]:
    """PREPARED→place→SUBMITTED/REJECTED；异常时保留PREPARED全量占用。"""
    blocked = _exit_order_submission_block_reason()
    if blocked:
        raise RuntimeError(f"[{phase}] 平仓委托已被时间门禁阻断:{blocked}")
    token = _prepare_exit_order_intent(
        request,
        phase=phase,
        local_order_id=local_order_id,
    )
    # 旧退出安全账本token同时作为统一SQLite意图的唯一执行片source_key；这样同一股票
    # 多张等量拆单也不会碰撞，崩溃后还能用本地token+券商remark双向找回。
    request = replace(
        request,
        purpose=str(getattr(request, "purpose", "") or "CLOSE"),
        source_key=str(getattr(request, "source_key", "") or token),
    )
    # 持久化PREPARED可能恰好跨过14:56:20或15:00。在紧邻券商
    # place_order前再取一次时钟；未发送的PREPARED明确收敛为REJECTED。
    blocked = _exit_order_submission_block_reason()
    if blocked:
        _update_exit_order_intent(token, status="REJECTED", terminal_known=True)
        raise RuntimeError(f"[{phase}] 平仓委托在发送前被时间门禁阻断:{blocked}")
    result = adapter.place_order(request)
    try:
        if bool(getattr(result, "accepted", False)):
            _update_exit_order_intent(
                token,
                status="SUBMITTED",
                broker_order_id=str(getattr(result, "order_id", "") or ""),
            )
        else:
            _update_exit_order_intent(token, status="REJECTED", terminal_known=True)
    except Exception as exc:
        # 券商调用已经发生，不能把本地绑定失败当普通单笔异常后继续拆单。
        # SQLite统一意图仍保留券商事实；整进程恢复后再唯一归属。
        _critical_execution_state_failure(f"{phase}券商结果绑定", exc)
    return result, token


def _assert_exit_live_allowed(config: dict) -> None:
    """POV虽直接走适配器，也必须复用实盘总门禁和SELL开关。"""
    from src.live_order_gateway import LiveOrderGateway

    gateway = LiveOrderGateway(PROJECT_ROOT / "config" / "config.json")
    confirm = config.get("live_trade", {}).get(
        "real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED"
    )
    gateway.assert_real_order_allowed(confirm, side="SELL")
    if not bool(config.get("live_trade", {}).get("allow_sell", False)):
        raise RuntimeError("live_trade.allow_sell=false，禁止POV卖出")


_watchdog_pending: list = []   # (position, [(order_id, expected_qty)]) 尾盘持续确认
_watchdog_pending_lock = threading.RLock()


def _queue_watchdog_pending(
    position: dict[str, Any],
    order_ids: list[tuple[str, int]],
) -> None:
    """登记已受理的尾盘卖单，供看门狗持续确认并幂等回写。"""

    normalized = [
        (str(order_id or "").strip(), max(int(quantity or 0), 0))
        for order_id, quantity in order_ids
        if str(order_id or "").strip() and int(quantity or 0) > 0
    ]
    if not normalized:
        return
    signature = tuple(normalized)
    with _watchdog_pending_lock:
        for _existing_position, existing_orders in _watchdog_pending:
            if tuple(existing_orders) == signature:
                return
        _watchdog_pending.append((dict(position), normalized))


def _remove_watchdog_pending(
    position: dict[str, Any],
    order_ids: list[tuple[str, int]],
) -> None:
    """按券商单号移除一组已终态的尾盘确认任务。"""

    signature = tuple(order_ids)
    with _watchdog_pending_lock:
        for item in list(_watchdog_pending):
            if tuple(item[1]) == signature:
                _watchdog_pending.remove(item)
                return
