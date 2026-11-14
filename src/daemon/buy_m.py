"""策略M执行：09:20竞价预挂、09:26竞价结果、09:30~10:30买入POV。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.utils.config import load_json_config
from src.utils.time_utils import now_beijing, today_beijing
from src.portfolio_ace_m import M_LEG, ace_limits, m_budget, position_in_family
from src.daemon.common import (
    PROJECT_ROOT,
    _floor_buy_quantity_by_amount,
    _fmt_wan,
    _notify,
    _ts_code_aliases,
    logger,
    next_n_trade_days,
)
from src.daemon.state import _m_load_state, _m_save_state, _record_live_buy, load_positions
from src.daemon.plans import (
    _ace_gate_mode_for,
    _combined_open_action_for_current_mode,
    _limit_up_pct_for_stock,
    _round_stock_price,
    has_open_local_position,
    read_cached_combined_decisions,
    st_open_forbidden,
)
from src.daemon.qmt import (
    _broker_has_preexisting_strategy_position,
    _qmt_get,
    _qmt_lock,
    _strategy_only_market_value,
)
from src.daemon.orders import _confirm_fill, _try_cancel_order


# ══════════════════════════════════════════════════════════════════════════════
# 策略 M 执行路径（2026-11-12 用户确认上线的 ACE + M）
# 规则与资金唯一来源：src/strategy_m.py、src/portfolio_ace_m.py；研究回测见 docs/strategy_m.md。
#   09:20 集合竞价：第一只合格候选按“涨停价-0.01”预挂（开盘涨停不成交，等同回测“开盘涨停买不进”）；
#   09:26 竞价后：成交即记账；开盘涨停/未成交则撤单换下一只；余量交 M 买入POV；
#   09:30~10:30 M买入POV：每5分钟一片，价格不超过开盘价+2%、不追涨停，10:30收口；
#   卖出：planned_exit_date（买入后下一个交易日）随A/C/D/E同一收盘平仓链路执行。
# M 与 ACE 资金分开：ACE空闲时M用82.5%，ACE持仓或当日开仓时M只用闲置资金（≤17.5%），合计≤99.5%。
# ══════════════════════════════════════════════════════════════════════════════

M_SIGNAL_RECENT_PATH = PROJECT_ROOT / "reports" / "strategy_m" / "m_signals_recent.json"
M_POV_END_TIME = datetime.time(10, 30)
M_POV_START_TIME = datetime.time(9, 30, 10)
_m_state_lock = threading.RLock()
_m_pov_thread: threading.Thread | None = None


def _m_signal_for_today(today_str: str) -> dict[str, Any] | None:
    from src.rolling_signal_store import latest_signal_for_buy_date
    from src.strategy_m import M_VERSION

    signal = latest_signal_for_buy_date(M_SIGNAL_RECENT_PATH, today_str)
    if not signal:
        return None
    if str(signal.get("m_version", "")) != M_VERSION:
        logger().error("M信号版本%s与正式版本%s不一致，按fail-closed今日M不开仓。",
                       signal.get("m_version", ""), M_VERSION)
        return None
    if str(signal.get("status", "")) != "SIGNAL_READY" or not signal.get("candidates"):
        return None
    return signal


def _m_live_enabled(config: dict[str, Any]) -> bool:
    return (
        str(config.get("trade_mode", "")).lower() == "live"
        and bool(config.get("broker_adapter_enabled"))
        and bool(config.get("qmt_enabled"))
        and bool((config.get("strategy_m", {}) or {}).get("enabled", True))
    )


def _m_ace_context(today_str: str, broker_positions: Any, total_asset: float) -> dict[str, Any]:
    """ACE今日是否占用资金（有可执行开仓动作或已有ACE持仓）、ACE相关股票、需为ACE预留的未成交目标金额。"""

    ace_codes: set[str] = set()
    plan_active = False
    combined = read_cached_combined_decisions()
    decisions = combined[0] if combined is not None else None
    orders_path = combined[1] if combined is not None else None
    if decisions is not None and _combined_open_action_for_current_mode(decisions):
        plan_active = True
        for _, row in decisions.iterrows():
            if str(row.get("side", "")).upper() == "BUY" and str(row.get("ts_code", "")):
                ace_codes.update(_ts_code_aliases(row.get("ts_code", "")))
        try:
            import pandas as pd

            if orders_path is not None and Path(orders_path).exists():
                orders = pd.read_csv(orders_path, dtype={"ts_code": str})
                for code in orders.get("ts_code", pd.Series(dtype=str)).dropna().astype(str):
                    ace_codes.update(_ts_code_aliases(code))
        except Exception as exc:
            logger().warning("读取ACE今日计划单代码失败（M仍按决策表代码回避）：%s", exc)
    held_ace = [p for p in load_positions()
                if str(p.get("status", "")).lower() in {"open", "sell_pending"} and position_in_family(p, "ACE")]
    for p in held_ace:
        ace_codes.update(_ts_code_aliases(p.get("ts_code", "")))
    ace_value = _strategy_only_market_value(broker_positions, family="ACE")
    m_value = _strategy_only_market_value(broker_positions, family=M_LEG)
    reserve = 0.0
    if plan_active:
        limits = ace_limits(_ace_gate_mode_for(today_str), total_asset, m_value)
        bought_today = sum(
            float(p.get("shares", 0) or 0) * float(p.get("buy_price", 0) or 0)
            for p in held_ace if str(p.get("buy_date", "")) == today_str
        )
        reserve = max(0.0, limits["target_amount"] - bought_today)
    return {"active": plan_active or bool(held_ace), "plan_active": plan_active, "codes": ace_codes,
            "ace_value": ace_value, "m_value": m_value, "reserve": reserve}


def _m_buy_quantity(ts_code: str, amount: float, price: float) -> int:
    """科创板≥200股且可按1股递增；其余100股整数倍。保证 数量×价格 ≤ 金额。"""
    if amount <= 0 or price <= 0:
        return 0
    if str(ts_code).upper().startswith(("688", "689")):
        qty = int(Decimal(str(amount)) / Decimal(str(price)))
        while qty > 0 and qty * price > amount + 1e-6:
            qty -= 1
        return qty if qty >= 200 else 0
    return _floor_buy_quantity_by_amount(amount, price, 100)


def _m_upper_limit(quote: Any, ts_code: str, name: str) -> float:
    upper = float(getattr(quote, "upper_limit", 0.0) or 0.0) if quote is not None else 0.0
    if upper > 0:
        return upper
    pre_close = float(getattr(quote, "pre_close", 0.0) or 0.0) if quote is not None else 0.0
    if pre_close <= 0:
        return 0.0
    return _round_stock_price(pre_close * (1 + _limit_up_pct_for_stock(ts_code, name)))


def _m_submit_buy(broker_cfg: dict, *, ts_code: str, name: str, qty: int, price: float, signal_date: str,
                  today_str: str, exit_date: str, source_key: str, channel: str) -> tuple[bool, str, str]:
    from src.broker_adapter import OrderRequest

    if st_open_forbidden(ts_code, name):
        logger().error("⛔ [ST禁买] %s %s 命中ST/风险警示铁律，拦截M开仓委托。", ts_code, name)
        _notify("buy_result", "⛔ ST股禁买拦截",
                f"{ts_code} {name} 为ST/风险警示股，全局铁律禁止开仓，已拦截M开仓。", level="timeSensitive")
        return False, "", "ST禁买"
    request = OrderRequest(
        ts_code=ts_code, broker_code=ts_code, side="BUY", quantity=int(qty), price_type="FIXED_PRICE",
        price=float(price), strategy_name="A_SYSTEM_ABC", remark=f"M{channel}-{today_str}",
        strategy_leg=M_LEG, business_date=today_str, signal_date=signal_date, planned_exit_date=exit_date,
        purpose="OPEN", source_key=source_key,
        metadata={"execution_channel": f"M{channel}", "name": name, "exit_n_days": 1,
                  "planned_exit_date": exit_date},
    )
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        result = adapter.place_order(request)
    if not result.accepted:
        return False, "", str(result.message)
    return True, str(result.order_id or ""), ""


def _m_exit_date(today_str: str) -> str:
    return next_n_trade_days(datetime.datetime.strptime(today_str, "%Y%m%d").date(), 1).strftime("%Y%m%d")


def _m_pick_candidate(signal: dict[str, Any], quotes: dict[str, Any], skip: set[str], ace_codes: set[str],
                      *, require_open: bool) -> dict[str, Any] | None:
    """按信号排名取第一只可买候选；require_open=True 时（竞价后）要求开盘价低于涨停价。"""
    for cand in sorted(signal.get("candidates", []), key=lambda c: int(c.get("rank", 999))):
        code = str(cand.get("ts_code", ""))
        name = str(cand.get("name", ""))
        if not code or code in skip:
            continue
        if any(alias in ace_codes for alias in _ts_code_aliases(code)):
            logger().info("M候选 %s %s 与ACE今日持仓/计划相同，按规则跳过。", code, name)
            continue
        if st_open_forbidden(code, name):
            logger().warning("M候选 %s %s 名称含ST/退，跳过。", code, name)
            continue
        quote = quotes.get(code)
        if quote is None or bool(getattr(quote, "suspended", False)):
            logger().info("M候选 %s %s 无行情或停牌，跳过。", code, name)
            continue
        upper = _m_upper_limit(quote, code, name)
        if upper <= 0:
            continue
        if require_open:
            open_px = float(getattr(quote, "open_price", 0.0) or 0.0) or float(getattr(quote, "last_price", 0.0) or 0.0)
            if open_px <= 0 or open_px >= upper - 1e-6:
                logger().info("M候选 %s %s 开盘价%.2f已涨停或无开盘价，按回测口径买不进，跳过。", code, name, open_px)
                continue
        return dict(cand, upper_limit=upper)
    return None


def job_m_premarket_buy() -> None:
    """09:20 M集合竞价预挂（在ACE的09:20预挂之后执行）。"""
    log = logger()
    today_str = today_beijing().strftime("%Y%m%d")
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        if not _m_live_enabled(config):
            log.info("[M] 非实盘或M未启用，跳过M集合竞价预挂。")
            return
        if now_beijing().time() >= datetime.time(9, 25):
            log.warning("[M] 执行时已过09:25，M竞价预挂窗口关闭，交09:26竞价后路径处理。")
            return
        with _m_state_lock:
            state = _m_load_state()
            if str(state.get("date", "")) == today_str and state.get("items"):
                log.info("[M] 今日M已有执行记录，跳过重复预挂。")
                return
        if has_open_local_position(M_LEG):
            log.info("[M] 仍有M持仓（今日到期收盘卖出），M同一时间最多1只，今日不开新仓。")
            return
        signal = _m_signal_for_today(today_str)
        if signal is None:
            log.info("[M] 今日无M开仓信号。")
            return
        from src.live_order_gateway import LiveOrderGateway

        confirm = str(config.get("live_trade", {}).get("real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED"))
        LiveOrderGateway(PROJECT_ROOT / "config" / "config.json").assert_real_order_allowed(confirm, side="BUY")
        broker_cfg = config.get("broker", {})
        codes = [str(c.get("ts_code", "")) for c in signal.get("candidates", []) if str(c.get("ts_code", ""))]
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            account = adapter.query_account()
            positions_live = adapter.query_positions()
            quotes = adapter.get_full_tick(codes)
        if _broker_has_preexisting_strategy_position(positions_live, today_str, family=M_LEG):
            log.warning("[M] 券商仍有M旧仓，今日M不开新仓。")
            return
        total_asset = float(getattr(account, "total_asset", 0.0) or 0.0)
        available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
        ctx = _m_ace_context(today_str, positions_live, total_asset)
        budget = m_budget(total_asset=total_asset, available_cash=available_cash, ace_active_today=ctx["active"],
                          ace_value=ctx["ace_value"], ace_unfilled_target=ctx["reserve"],
                          cash_buffer=float(config.get("live_trade", {}).get("cash_buffer_amount", 1000)))
        exit_date = _m_exit_date(today_str)
        skip: set[str] = set()
        while True:
            cand = _m_pick_candidate(signal, quotes, skip, ctx["codes"], require_open=False)
            if cand is None:
                log.info("[M] 今日M候选均不可下单（ACE同票/ST/停牌/资金不足）。")
                return
            code, name = str(cand["ts_code"]), str(cand.get("name", ""))
            price = _round_stock_price(float(cand["upper_limit"]) - 0.01)
            qty = _m_buy_quantity(code, budget["amount"], price)
            if qty <= 0:
                log.warning("[M] %s %s 预算%.0f元不足一手（价%.2f），换下一只。", code, name, budget["amount"], price)
                skip.add(code)
                continue
            break
        item = {"ts_code": code, "name": name, "signal_date": str(signal.get("signal_date", "")),
                "exit_date": exit_date, "target_amount": budget["amount"], "budget": budget,
                "ace_active": ctx["active"], "stage": "auction", "order_ids": [], "auction_order_id": "",
                "auction_qty": qty, "filled_qty": 0, "cost_amt": 0.0, "tried": [code], "slice_no": 0,
                "open_price": 0.0, "done": False}
        ok, order_id, message = _m_submit_buy(broker_cfg, ts_code=code, name=name, qty=qty, price=price,
                                              signal_date=item["signal_date"], today_str=today_str,
                                              exit_date=exit_date, source_key=f"M_AUCTION|{today_str}|{code}",
                                              channel="竞价开仓")
        if not ok:
            item["stage"] = "pov"
            item["note"] = f"竞价预挂未受理：{message}"
            log.error("[M] %s %s 竞价预挂未受理：%s，交09:30 M买入POV。", code, name, message)
        else:
            item["auction_order_id"] = order_id
            item["order_ids"].append(order_id)
        with _m_state_lock:
            state = {"date": today_str, "items": [item], "state_revision": int(state.get("state_revision", 0) or 0)}
            _m_save_state(state)
        log.warning("⏳ [M竞价开仓] %s %s %d股 @涨停价-0.01=%.2f 预算%.0f元（%s，%.1f%%）",
                    code, name, qty, price, budget["amount"], "ACE同时开仓/持仓→闲置资金" if ctx["active"] else "ACE空闲",
                    budget["pct"] * 100)
        _notify("buy_result", "⏳ M集合竞价预挂",
                f"策略=M {code} {name} {qty}股 限价{price:.2f}（涨停价-0.01，开盘涨停不成交）；"
                f"预算{_fmt_wan(budget['amount'])}（{'ACE同时占用，M用闲置资金' if ctx['active'] else 'ACE空闲，M用82.5%'}）；"
                f"计划{exit_date[4:6]}-{exit_date[6:]}收盘卖出。")
    except Exception as exc:
        log.error("[M] 集合竞价预挂异常（M今日不开仓，不影响ACE）：%s", exc)


def job_m_after_auction() -> None:
    """09:26 竞价结果：记账成交；开盘涨停/未成交则撤单换下一只；余量交M买入POV。"""
    log = logger()
    today_str = today_beijing().strftime("%Y%m%d")
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        if not _m_live_enabled(config):
            return
        broker_cfg = config.get("broker", {})
        with _m_state_lock:
            state = _m_load_state()
        if str(state.get("date", "")) != today_str or not state.get("items"):
            # 09:20没跑成（重启/延迟）：竞价后直接按开盘价规则交给POV路径选股。
            if has_open_local_position(M_LEG):
                return
            signal = _m_signal_for_today(today_str)
            if signal is None:
                return
            state = {"date": today_str, "items": [{"ts_code": "", "stage": "select", "signal": signal,
                                                   "order_ids": [], "filled_qty": 0, "cost_amt": 0.0,
                                                   "tried": [], "slice_no": 0, "done": False}],
                     "state_revision": int(state.get("state_revision", 0) or 0)}
        item = state["items"][0]
        if item.get("done"):
            return
        if item.get("stage") == "auction" and item.get("auction_order_id"):
            fill = None
            try:
                with _qmt_lock:
                    adapter = _qmt_get(broker_cfg)
                    fill = adapter.get_order_fill(str(item["auction_order_id"]))
            except Exception as exc:
                log.warning("[M] 查询竞价委托成交异常：%s", exc)
            filled = int(getattr(fill, "filled_qty", 0) or 0) if fill is not None else 0
            avg = float(getattr(fill, "avg_price", 0.0) or 0.0) if fill is not None else 0.0
            if not (fill is not None and bool(getattr(fill, "is_terminal", False))) and filled < int(item.get("auction_qty", 0)):
                # 未全部成交的竞价残单（价格=涨停-0.01）不能带进连续竞价，否则可能远高于开盘价成交。
                _try_cancel_order(broker_cfg, str(item["auction_order_id"]), str(item["ts_code"]))
                time.sleep(2)
                try:
                    with _qmt_lock:
                        adapter = _qmt_get(broker_cfg)
                        fill = adapter.get_order_fill(str(item["auction_order_id"]))
                    filled = int(getattr(fill, "filled_qty", 0) or 0)
                    avg = float(getattr(fill, "avg_price", 0.0) or 0.0) or avg
                except Exception as exc:
                    log.warning("[M] 撤单后复查成交异常：%s", exc)
            if filled > 0:
                price = avg if avg > 0 else float(item.get("open_price", 0.0) or 0.0)
                _record_live_buy(
                    recovery_context="M竞价开仓", order_id=str(item["auction_order_id"]), ts_code=str(item["ts_code"]),
                    name=str(item.get("name", "")), signal_date=str(item.get("signal_date", "")), buy_date=today_str,
                    shares=filled, buy_price=price, strategy_leg=M_LEG, exit_n_days=1,
                    execution_channel="M竞价开仓", planned_order_qty=int(item.get("auction_qty", filled)),
                    planned_exit_date_override=str(item.get("exit_date", "")), exit_rule="FIXED_T2_CLOSE",
                )
                item["auction_filled_qty"] = filled
                item["auction_cost"] = filled * price
                _notify("buy_result", "✅ M竞价成交",
                        f"策略=M {item['ts_code']} {item.get('name', '')} 成交{filled}股 @{price:.2f}，"
                        f"金额{_fmt_wan(filled * price)}；计划{str(item.get('exit_date', ''))[4:]}收盘卖出。")
            # 竞价零成交：回到“按开盘价规则选股”。若该股开盘未涨停仍会被重新选中，在开盘价+2%以内买入
            # （与回测“开盘价买入”一致）；开盘涨停则跳过换下一只。
            item["stage"] = "pov" if filled > 0 else "select"
            if filled <= 0:
                item["tried"] = []
        with _m_state_lock:
            _m_save_state(state)
        _m_start_pov_worker()
    except Exception as exc:
        log.error("[M] 竞价后处理异常（不影响ACE）：%s", exc)


def _m_pov_round(state: dict[str, Any], broker_cfg: dict, config: dict, today_str: str) -> None:
    """M买入POV执行一片：stage=select 时按开盘规则选股；stage=pov 时补足剩余目标金额。"""
    log = logger()
    item = state["items"][0]
    lt = config.get("live_trade", {})
    chase = float(lt.get("pov_chase_cap", 0.02))
    part = float(lt.get("pov_participation", 0.10))
    signal = item.get("signal") or _m_signal_for_today(today_str)
    if item.get("stage") == "select":
        if signal is None:
            item["done"], item["done_reason"] = True, "无M信号"
            return
        codes = [str(c.get("ts_code", "")) for c in signal.get("candidates", []) if str(c.get("ts_code", ""))]
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            account = adapter.query_account()
            positions_live = adapter.query_positions()
            quotes = adapter.get_full_tick(codes)
        total_asset = float(getattr(account, "total_asset", 0.0) or 0.0)
        ctx = _m_ace_context(today_str, positions_live, total_asset)
        budget = m_budget(total_asset=total_asset, available_cash=float(getattr(account, "available_cash", 0.0) or 0.0),
                          ace_active_today=ctx["active"], ace_value=ctx["ace_value"], ace_unfilled_target=ctx["reserve"],
                          cash_buffer=float(lt.get("cash_buffer_amount", 1000)))
        cand = _m_pick_candidate(signal, quotes, set(item.get("tried", [])), ctx["codes"], require_open=True)
        if cand is None:
            item["done"], item["done_reason"] = True, "M候选均开盘涨停/停牌/与ACE同票，今日不买"
            log.info("[M] %s", item["done_reason"])
            return
        quote = quotes.get(str(cand["ts_code"]))
        item.update({"ts_code": str(cand["ts_code"]), "name": str(cand.get("name", "")),
                     "signal_date": str(signal.get("signal_date", "")), "exit_date": _m_exit_date(today_str),
                     "target_amount": budget["amount"], "budget": budget, "ace_active": ctx["active"],
                     "open_price": float(getattr(quote, "open_price", 0.0) or getattr(quote, "last_price", 0.0) or 0.0),
                     "stage": "pov"})
        item.setdefault("tried", []).append(item["ts_code"])
        item.pop("signal", None)
    code, name = str(item["ts_code"]), str(item.get("name", ""))
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        quote = adapter.get_full_tick([code]).get(code)
    if quote is None:
        return
    open_px = float(item.get("open_price", 0.0) or 0.0) or float(getattr(quote, "open_price", 0.0) or 0.0)
    item["open_price"] = open_px
    upper = _m_upper_limit(quote, code, name)
    last = float(getattr(quote, "last_price", 0.0) or 0.0)
    filled_amount = float(item.get("auction_cost", 0.0) or 0.0) + float(item.get("cost_amt", 0.0) or 0.0)
    remaining = float(item.get("target_amount", 0.0) or 0.0) - filled_amount
    ask1 = 0.0
    asks = getattr(quote, "ask_prices", None)
    if asks and len(asks) >= 1 and float(asks[0] or 0) > 0:
        ask1 = float(asks[0])
    price = _round_stock_price(min(x for x in (ask1 or last, open_px * (1 + chase), upper - 0.01) if x > 0))
    if open_px <= 0 or last <= 0 or upper <= 0:
        return
    if last >= upper - 1e-6 or price > open_px * (1 + chase) + 1e-6 or last > open_px * (1 + chase) + 1e-6:
        log.info("[M POV] %s 现价%.2f超过开盘价+%.0f%%或涨停，本片不追。", code, last, chase * 100)
        return
    prev_amt = float(item.get("last_market_amount", 0.0) or 0.0)
    cum_amt = float(getattr(quote, "amount", 0.0) or 0.0)
    flow = max(cum_amt - prev_amt, 0.0) if prev_amt > 0 else cum_amt
    item["last_market_amount"] = cum_amt
    slice_amount = min(remaining, max(part * flow, 0.0))
    qty = _m_buy_quantity(code, slice_amount, price)
    if qty <= 0:
        if _m_buy_quantity(code, remaining, price) <= 0:
            item["done"], item["done_reason"] = True, "剩余目标不足一手"
        return
    item["slice_no"] = int(item.get("slice_no", 0)) + 1
    ok, order_id, message = _m_submit_buy(
        broker_cfg, ts_code=code, name=name, qty=qty, price=price, signal_date=str(item.get("signal_date", "")),
        today_str=today_str, exit_date=str(item.get("exit_date", "")),
        source_key=f"M_POV|{today_str}|{code}|{item['slice_no']}", channel="买入POV")
    if not ok:
        log.error("[M POV] %s 第%d片未受理：%s", code, item["slice_no"], message)
        return
    item.setdefault("order_ids", []).append(order_id)
    # POV 片单号单独记：收口记账只合并 POV 片（竞价那笔已在09:26单独记账），
    # 否则组合意图成交量会把竞价成交重复计入，触发持仓对账保护。
    item.setdefault("pov_order_ids", []).append(order_id)
    fill = _confirm_fill(broker_cfg, order_id, qty, "M买入POV", timeout_sec=60)
    if int(getattr(fill, "filled_qty", 0) or 0) < qty and not bool(getattr(fill, "is_terminal", False)):
        _try_cancel_order(broker_cfg, order_id, code)
        time.sleep(2)
        try:
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                fill = adapter.get_order_fill(order_id)
        except Exception as exc:
            log.warning("[M POV] 撤单后复查异常：%s", exc)
    fq = int(getattr(fill, "filled_qty", 0) or 0)
    if fq > 0:
        avg = float(getattr(fill, "avg_price", 0.0) or 0.0) or price
        item["filled_qty"] = int(item.get("filled_qty", 0)) + fq
        item["cost_amt"] = float(item.get("cost_amt", 0.0)) + fq * avg
        log.warning("✅ [M POV] %s %s 第%d片成交%d股@%.2f", code, name, item["slice_no"], fq, avg)


def _m_pov_finalize(state: dict[str, Any], reason: str) -> None:
    item = state["items"][0]
    if item.get("finalized"):
        return
    item["done"], item["finalized"], item["done_reason"] = True, True, reason
    fq = int(item.get("filled_qty", 0) or 0)
    if fq > 0:
        today_str = today_beijing().strftime("%Y%m%d")
        avg = float(item["cost_amt"]) / fq
        _record_live_buy(
            recovery_context="M买入POV", order_id=f"mpov-{today_str}-{item['ts_code']}", ts_code=str(item["ts_code"]),
            name=str(item.get("name", "")), signal_date=str(item.get("signal_date", "")), buy_date=today_str,
            shares=fq, buy_price=avg, strategy_leg=M_LEG, exit_n_days=1, execution_channel="M买入POV",
            planned_order_qty=fq, planned_exit_date_override=str(item.get("exit_date", "")),
            component_order_ids=[str(o) for o in item.get("pov_order_ids", []) if str(o)], exit_rule="FIXED_T2_CLOSE",
        )
        _notify("buy_result", "✅ M开仓完成",
                f"策略=M {item['ts_code']} {item.get('name', '')} POV成交{fq}股 均价{avg:.2f}；"
                f"累计开仓{_fmt_wan(float(item.get('auction_cost', 0.0) or 0.0) + fq * avg)}（{reason}）。")
    elif not float(item.get("auction_cost", 0.0) or 0.0):
        _notify("buy_result", "ℹ️ M今日未开仓", f"M今日未成交：{reason}。")


def _m_pov_worker() -> None:
    log = logger()
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        broker_cfg = config.get("broker", {})
        today_str = today_beijing().strftime("%Y%m%d")
        while True:
            now = now_beijing()
            with _m_state_lock:
                state = _m_load_state()
            if str(state.get("date", "")) != today_str or not state.get("items"):
                return
            item = state["items"][0]
            if item.get("done"):
                with _m_state_lock:
                    _m_pov_finalize(state, str(item.get("done_reason", "完成")))
                    _m_save_state(state)
                return
            if now.time() >= M_POV_END_TIME:
                with _m_state_lock:
                    _m_pov_finalize(state, "10:30收口")
                    _m_save_state(state)
                return
            if now.time() < M_POV_START_TIME:
                time.sleep(min((datetime.datetime.combine(now.date(), M_POV_START_TIME, tzinfo=now.tzinfo) - now).total_seconds(), 60))
                continue
            with _m_state_lock:
                _m_pov_round(state, broker_cfg, config, today_str)
                remaining = float(item.get("target_amount", 0.0) or 0.0) - float(item.get("auction_cost", 0.0) or 0.0) - float(item.get("cost_amt", 0.0) or 0.0)
                if item.get("stage") == "pov" and item.get("ts_code") and remaining < 1.0:
                    item["done"], item["done_reason"] = True, "目标金额已买足"
                _m_save_state(state)
            now2 = now_beijing()
            base = datetime.datetime.combine(now2.date(), M_POV_START_TIME, tzinfo=now2.tzinfo)
            k = int((now2 - base).total_seconds() // 300) + 1
            nxt = min(base + datetime.timedelta(seconds=k * 300),
                      datetime.datetime.combine(now2.date(), M_POV_END_TIME, tzinfo=now2.tzinfo))
            time.sleep(max(1.0, (nxt - now_beijing()).total_seconds()))
    except Exception as exc:
        log.error("[M POV] 线程异常退出（不影响ACE；已成交部分以券商为准，收盘对账核对）：%s", exc)


def _m_start_pov_worker() -> None:
    global _m_pov_thread
    if _m_pov_thread is not None and _m_pov_thread.is_alive():
        return
    now = now_beijing().time()
    if now >= M_POV_END_TIME:
        with _m_state_lock:
            state = _m_load_state()
            if state.get("items") and str(state.get("date", "")) == today_beijing().strftime("%Y%m%d"):
                _m_pov_finalize(state, "重启时已过10:30，按已成交收口")
                _m_save_state(state)
        return
    _m_pov_thread = threading.Thread(target=_m_pov_worker, name="m-pov", daemon=True)
    _m_pov_thread.start()
