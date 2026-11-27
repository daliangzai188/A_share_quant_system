"""策略L执行（ACE+M+L 新框架，2026-11-26）：09:20竞价预挂、09:26竞价结果、09:30~10:30买入POV。

结构照 src/daemon/buy_m.py（M 的执行代码不动）；差别：
- 计划来自 QMT 外算好、经注册表进来的 reports/strategy_l/l_signals_recent.json（docs/strategy_l.md 第二节第5小节），
  只有一只首选，买不到当天就不买（不递补）；
- 仓位按 src/portfolio_ace_m.py：ACE 停手期 60%、正常期 25%，L 自身停手 ×20/82.5，连亏降仓 ×0.3，
  不超过 99.5% − ACE − M（给 ACE 今日未成交目标留足）；
- 不买 ACE、M 持有或今日计划买入的股票；ST/上市前5日不买；开盘涨停不买（09:20 按“涨停价−0.01”预挂，开盘涨停不成交）；
- 卖出：planned_exit_date（买入后下一个交易日）随 A/C/D/E/M 同一收盘平仓链路。
只在 portfolio_framework=ace_m_l 且实盘时执行；现行框架下所有入口直接返回。
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
from src.market_rules import BOARD_LOT_SHARES, STAR_MIN_ORDER_SHARES, market_segment
from src.portfolio_ace_m import (
    FRAMEWORK_ACE_M_L,
    L_LEG,
    M_LEG,
    ace_limits,
    l_budget,
    l_target_pct,
    position_in_family,
)
from src.daemon.common import (
    PROJECT_ROOT,
    _load_calendar,
    _entry_amount_text,
    _floor_buy_quantity_by_amount,
    _fmt_wan,
    _notify,
    _track_execution,
    _ts_code_aliases,
    logger,
    next_n_trade_days,
)
from src.daemon.state import _l_load_state, _l_save_state, _record_live_buy, load_pending_buys, load_positions
from src.daemon.plans import (
    _ace_gate_mode_for,
    _combined_open_action_for_current_mode,
    _limit_up_pct_for_stock,
    _round_stock_price,
    framework_from_config,
    has_open_local_position,
    loss_streak_multiplier_for,
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

L_SIGNAL_RECENT_PATH = PROJECT_ROOT / "reports" / "strategy_l" / "l_signals_recent.json"
L_POV_END_TIME = datetime.time(10, 30)
L_POV_START_TIME = datetime.time(9, 30, 10)
_l_state_lock = threading.RLock()
_l_pov_thread: threading.Thread | None = None


def _l_signal_for_today(today_str: str) -> dict[str, Any] | None:
    from src.rolling_signal_store import latest_signal_for_buy_date
    from src.strategy_l import L_VERSION

    signal = latest_signal_for_buy_date(L_SIGNAL_RECENT_PATH, today_str)
    if not signal:
        return None
    if str(signal.get("l_version", "")) != L_VERSION:
        logger().error("L信号版本%s与正式版本%s不一致，按fail-closed今日L不开仓。", signal.get("l_version", ""), L_VERSION)
        return None
    open_days, _ = _load_calendar()
    previous = max((day for day in open_days if day < today_str), default="")
    if today_str not in open_days or not previous or str(signal.get("signal_date", "")) != previous:
        logger().error("L信号日与交易日历不符，今日L不开仓。")
        return None
    if str(signal.get("status", "")) != "SIGNAL_READY" or not str(signal.get("ts_code", "")):
        return None
    return signal


def _l_live_enabled(config: dict[str, Any]) -> bool:
    return (
        framework_from_config(config) == FRAMEWORK_ACE_M_L
        and str(config.get("trade_mode", "")).lower() == "live"
        and bool(config.get("broker_adapter_enabled"))
        and bool(config.get("qmt_enabled"))
        and bool((config.get("strategy_l", {}) or {}).get("enabled", True))
    )


def _l_context(today_str: str, broker_positions: Any, total_asset: float, streak_mult: float) -> dict[str, Any]:
    """L 要回避的股票（ACE、M 持有或今日计划买入）、ACE/M 当前市值、需给 ACE 预留的未成交目标与已冻结的钱。"""

    avoid: set[str] = set()
    plan_active = False
    combined = read_cached_combined_decisions()
    decisions = combined[0] if combined is not None else None
    orders_path = combined[1] if combined is not None else None
    if decisions is not None and _combined_open_action_for_current_mode(decisions):
        plan_active = True
        for _, row in decisions.iterrows():
            if str(row.get("side", "")).upper() == "BUY" and str(row.get("ts_code", "")):
                avoid.update(_ts_code_aliases(row.get("ts_code", "")))
        try:
            import pandas as pd

            if orders_path is not None and Path(orders_path).exists():
                orders = pd.read_csv(orders_path, dtype={"ts_code": str})
                for code in orders.get("ts_code", pd.Series(dtype=str)).dropna().astype(str):
                    avoid.update(_ts_code_aliases(code))
        except Exception as exc:
            logger().warning("读取ACE今日计划单代码失败（L仍按决策表代码回避）：%s", exc)
    held = [p for p in load_positions() if str(p.get("status", "")).lower() in {"open", "sell_pending"}]
    held_ace = [p for p in held if position_in_family(p, "ACE")]
    for p in held:
        if position_in_family(p, "ACE") or position_in_family(p, M_LEG):
            avoid.update(_ts_code_aliases(p.get("ts_code", "")))
    ace_value = _strategy_only_market_value(broker_positions, family="ACE")
    m_value = _strategy_only_market_value(broker_positions, family=M_LEG)
    reserve = 0.0
    if plan_active:
        limits = ace_limits(_ace_gate_mode_for(today_str), total_asset, m_value, streak_mult=streak_mult)
        bought_today = sum(float(p.get("shares", 0) or 0) * float(p.get("buy_price", 0) or 0)
                           for p in held_ace if str(p.get("buy_date", "")) == today_str)
        reserve = max(0.0, limits["target_amount"] - bought_today)
    frozen = 0.0
    try:
        frozen = sum(float(o.get("qty", 0) or 0) * float(o.get("ref_price", 0) or 0) for o in load_pending_buys()
                     if str(o.get("strategy_leg", "")).strip().upper() not in (M_LEG, L_LEG))
    except Exception as exc:
        logger().warning("读取ACE待成交买单失败，L按现金定仓时照旧全额给ACE预留：%s", exc)
    return {"avoid": avoid, "ace_value": ace_value, "m_value": m_value, "reserve": reserve, "frozen": frozen,
            "ace_active": plan_active or bool(held_ace)}


def l_commitment_for_m(today_str: str, broker_positions: Any) -> dict[str, Any]:
    """新框架里 M 定仓要扣掉的 L 占用（同日顺序 ACE→L→M）：

    value=L 当前持仓市值；unfilled=L 今日目标还没成交、也还没收口的金额（M 要给它留着）；
    frozen=L 竞价预挂单还在挂、已从可用资金冻结的钱（按现金定仓时不重复预留）；codes=M 不能买的 L 股票。
    """
    value = _strategy_only_market_value(broker_positions, family=L_LEG)
    codes: set[str] = set()
    for p in load_positions():
        if str(p.get("status", "")).lower() in {"open", "sell_pending"} and position_in_family(p, L_LEG):
            codes.update(_ts_code_aliases(p.get("ts_code", "")))
    unfilled = frozen = 0.0
    with _l_state_lock:
        state = _l_load_state()
    if str(state.get("date", "")) == today_str and state.get("items"):
        item = state["items"][0]
        if item.get("ts_code"):
            codes.update(_ts_code_aliases(item.get("ts_code", "")))
        if not item.get("finalized"):
            done_amount = float(item.get("auction_cost", 0.0) or 0.0) + float(item.get("cost_amt", 0.0) or 0.0)
            unfilled = max(float(item.get("target_amount", 0.0) or 0.0) - done_amount, 0.0)
            if item.get("stage") == "auction" and item.get("auction_order_id"):
                frozen = int(item.get("auction_qty", 0) or 0) * float(item.get("auction_price", 0.0) or 0.0)
    return {"value": value, "unfilled": unfilled, "frozen": frozen, "codes": codes}


def _l_budget(config: dict[str, Any], account: Any, ctx: dict[str, Any], signal: dict[str, Any], today_str: str,
              streak_mult: float) -> dict[str, float]:
    target = l_target_pct(ace_mode=_ace_gate_mode_for(today_str), l_self_allowed=bool(signal.get("l_allowed", False)),
                          streak_mult=streak_mult)
    return l_budget(total_asset=float(getattr(account, "total_asset", 0.0) or 0.0),
                    available_cash=float(getattr(account, "available_cash", 0.0) or 0.0), target_pct=target,
                    ace_value=ctx["ace_value"], m_value=ctx["m_value"], ace_unfilled_target=ctx["reserve"],
                    ace_frozen_pending=ctx["frozen"],
                    cash_buffer=float(config.get("live_trade", {}).get("cash_buffer_amount", 1000)))


def _l_buy_quantity(ts_code: str, amount: float, price: float) -> int:
    """科创板≥200股且可按1股递增；北交所≥100股可按1股递增；其余100股整数倍。保证 数量×价格 ≤ 金额。"""
    if amount <= 0 or price <= 0:
        return 0
    seg = market_segment(ts_code)
    if seg in ("star", "bj"):
        qty = int(Decimal(str(amount)) / Decimal(str(price)))
        while qty > 0 and qty * price > amount + 1e-6:
            qty -= 1
        return qty if qty >= (STAR_MIN_ORDER_SHARES if seg == "star" else BOARD_LOT_SHARES) else 0
    return _floor_buy_quantity_by_amount(amount, price, BOARD_LOT_SHARES)


def _l_upper_limit(quote: Any, ts_code: str, name: str) -> float:
    upper = float(getattr(quote, "upper_limit", 0.0) or 0.0) if quote is not None else 0.0
    if upper > 0:
        return upper
    pre_close = float(getattr(quote, "pre_close", 0.0) or 0.0) if quote is not None else 0.0
    if pre_close <= 0:
        return 0.0
    return _round_stock_price(pre_close * (1 + _limit_up_pct_for_stock(ts_code, name)))


def _l_submit_buy(broker_cfg: dict, *, ts_code: str, name: str, qty: int, price: float, signal_date: str,
                  today_str: str, exit_date: str, source_key: str, channel: str) -> tuple[bool, str, str]:
    from src.broker_adapter import OrderRequest

    if st_open_forbidden(ts_code, name):
        logger().error("⛔ [ST禁买] %s %s 命中ST/风险警示铁律，拦截L开仓委托。", ts_code, name)
        _notify("buy_result", "⛔ ST股禁买拦截",
                f"{ts_code} {name} 为ST/风险警示股，全局铁律禁止开仓，已拦截L开仓。", level="timeSensitive")
        return False, "", "ST禁买"
    request = OrderRequest(
        ts_code=ts_code, broker_code=ts_code, side="BUY", quantity=int(qty), price_type="FIXED_PRICE",
        price=float(price), strategy_name="A_SYSTEM_ABC", remark=f"L{channel}-{today_str}",
        strategy_leg=L_LEG, business_date=today_str, signal_date=signal_date, planned_exit_date=exit_date,
        purpose="OPEN", source_key=source_key,
        metadata={"execution_channel": f"L{channel}", "name": name, "exit_n_days": 1, "planned_exit_date": exit_date},
    )
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        result = adapter.place_order(request)
    if not result.accepted:
        return False, "", str(result.message)
    return True, str(result.order_id or ""), ""


def _l_register_plan(item: dict[str, Any], today_str: str, status: str) -> None:
    code = str(item.get("ts_code", ""))
    target = float(item.get("target_amount", 0.0) or 0.0)
    reference = float(item.get("reference_price", 0.0) or 0.0)
    target_qty = _l_buy_quantity(code, target, reference) if reference > 0 else int(item.get("auction_qty", 0) or 0)
    auction_qty = int(item.get("auction_qty", 0) or 0)
    _track_execution(
        "register_entry_plan", entry_date=today_str, ts_code=code, name=str(item.get("name", "")),
        strategy_leg=L_LEG, signal_date=str(item.get("signal_date", "")), target_qty=target_qty,
        target_amount=target, reference_price=reference, auction_planned_qty=auction_qty,
        pov_planned_qty=max(target_qty - auction_qty, 0), planned_exit_date=str(item.get("exit_date", "")),
        status=status,
    )


def _l_exit_date(today_str: str) -> str:
    return next_n_trade_days(datetime.datetime.strptime(today_str, "%Y%m%d").date(), 1).strftime("%Y%m%d")


def _l_check(signal: dict[str, Any], quotes: dict[str, Any], avoid: set[str], *, require_open: bool) -> tuple[dict | None, str]:
    """检查当日唯一的 L 首选能不能下单；返回（候选, 不能买的原因）。不递补第二名。"""
    code, name = str(signal.get("ts_code", "")), str(signal.get("name", ""))
    if any(alias in avoid for alias in _ts_code_aliases(code)):
        return None, "与ACE/M今日持仓或计划同票"
    if st_open_forbidden(code, name):
        return None, "名称含ST/退"
    quote = quotes.get(code)
    if quote is None or bool(getattr(quote, "suspended", False)):
        return None, "无行情或停牌"
    upper = _l_upper_limit(quote, code, name)
    if upper <= 0:
        return None, "算不出涨停价"
    if require_open:
        open_px = float(getattr(quote, "open_price", 0.0) or 0.0) or float(getattr(quote, "last_price", 0.0) or 0.0)
        if open_px <= 0 or open_px >= upper - 1e-6:
            return None, "开盘涨停或无开盘价（按回测口径买不进）"
    return {"ts_code": code, "name": name, "upper_limit": upper,
            "close": float(getattr(quote, "pre_close", 0.0) or 0.0)}, ""


def job_l_premarket_buy() -> None:
    """09:20 L集合竞价预挂（在ACE预挂之后、M预挂之前；新框架顺序 ACE→L→M）。"""
    log = logger()
    today_str = today_beijing().strftime("%Y%m%d")
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        if not _l_live_enabled(config):
            return
        if now_beijing().time() >= datetime.time(9, 25):
            log.warning("[L] 执行时已过09:25，L竞价预挂窗口关闭，交09:26竞价后路径处理。")
            return
        with _l_state_lock:
            state = _l_load_state()
            if str(state.get("date", "")) == today_str and state.get("items"):
                log.info("[L] 今日L已有执行记录，跳过重复预挂。")
                return
        if has_open_local_position(L_LEG):
            log.info("[L] 仍有L持仓（今日到期收盘卖出），L同一时间最多1只，今日不开新仓。")
            return
        signal = _l_signal_for_today(today_str)
        if signal is None:
            log.info("[L] 今日无L开仓信号。")
            return
        from src.live_order_gateway import LiveOrderGateway

        confirm = str(config.get("live_trade", {}).get("real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED"))
        LiveOrderGateway(PROJECT_ROOT / "config" / "config.json").assert_real_order_allowed(confirm, side="BUY")
        broker_cfg = config.get("broker", {})
        code = str(signal["ts_code"])
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            account = adapter.query_account()
            positions_live = adapter.query_positions()
            quotes = adapter.get_full_tick([code])
        if _broker_has_preexisting_strategy_position(positions_live, today_str, family=L_LEG):
            log.warning("[L] 券商仍有L旧仓，今日L不开新仓。")
            return
        total_asset = float(getattr(account, "total_asset", 0.0) or 0.0)
        streak = loss_streak_multiplier_for(today_str, config)
        ctx = _l_context(today_str, positions_live, total_asset, streak)
        budget = _l_budget(config, account, ctx, signal, today_str, streak)
        cand, why = _l_check(signal, quotes, ctx["avoid"], require_open=False)
        if cand is None:
            log.info("[L] 首选 %s %s 今日不能下单：%s（不递补）。", code, signal.get("name", ""), why)
            return
        name = str(cand.get("name", ""))
        price = _round_stock_price(float(cand["upper_limit"]) - 0.01)
        qty = _l_buy_quantity(code, budget["amount"], price)
        if qty <= 0:
            log.warning("[L] %s %s 预算%.0f元不足最小申报量（价%.2f），今日不买。", code, name, budget["amount"], price)
            return
        exit_date = _l_exit_date(today_str)
        item = {"ts_code": code, "name": name, "signal_date": str(signal.get("signal_date", "")),
                "reference_price": float(cand.get("close", 0.0) or 0.0), "exit_date": exit_date,
                "target_amount": budget["amount"], "budget": budget, "streak_mult": streak, "total_asset": total_asset,
                "l_allowed": bool(signal.get("l_allowed", False)), "stage": "auction", "order_ids": [],
                "auction_order_id": "", "auction_qty": qty, "auction_price": price, "filled_qty": 0, "cost_amt": 0.0,
                "slice_no": 0, "open_price": 0.0, "done": False}
        ok, order_id, message = _l_submit_buy(broker_cfg, ts_code=code, name=name, qty=qty, price=price,
                                              signal_date=item["signal_date"], today_str=today_str, exit_date=exit_date,
                                              source_key=f"L_AUCTION|{today_str}|{code}", channel="竞价开仓")
        if not ok:
            item["stage"] = "pov"
            item["note"] = f"竞价预挂未受理：{message}"
            log.error("[L] %s %s 竞价预挂未受理：%s，交09:30 L买入POV。", code, name, message)
        else:
            item["auction_order_id"] = order_id
            item["order_ids"].append(order_id)
        with _l_state_lock:
            _l_save_state({"date": today_str, "items": [item], "state_revision": int(state.get("state_revision", 0) or 0)})
        _l_register_plan(item, today_str, "L竞价预挂" if ok else "L竞价未受理，转开盘后POV")
        note = ("方案甲停手期" if _ace_gate_mode_for(today_str) == "REDUCED" else "ACE正常期") + \
            ("" if item["l_allowed"] else "、L自身停手") + ("" if streak >= 1.0 else "、连亏降仓×%.1f" % streak)
        log.warning("⏳ [L竞价开仓] %s %s %d股 @涨停价-0.01=%.2f 预算%s（%s，规则比例上限%.2f%%）",
                    code, name, qty, price, _entry_amount_text(budget["amount"], total_asset), note, budget["pct"] * 100)
        _notify("buy_result", "⏳ L集合竞价预挂",
                f"策略=L {code} {name} {qty}股 限价{price:.2f}（涨停价-0.01，开盘涨停不成交）；"
                f"预算{_entry_amount_text(budget['amount'], total_asset)}（{note}，规则比例上限{budget['pct']:.2%}）；"
                f"计划{exit_date[4:6]}-{exit_date[6:]}收盘卖出。")
    except Exception as exc:
        log.error("[L] 集合竞价预挂异常（L今日不开仓，不影响ACE和M）：%s", exc)


def job_l_after_auction() -> None:
    """09:26 竞价结果：记账成交；开盘涨停/未成交则撤单；未成交且开盘没涨停的交POV按开盘价+2%以内补；余量交POV。"""
    log = logger()
    today_str = today_beijing().strftime("%Y%m%d")
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        if not _l_live_enabled(config):
            return
        broker_cfg = config.get("broker", {})
        with _l_state_lock:
            state = _l_load_state()
        if str(state.get("date", "")) != today_str or not state.get("items"):
            if has_open_local_position(L_LEG):
                return
            signal = _l_signal_for_today(today_str)
            if signal is None:
                return
            state = {"date": today_str, "items": [{"ts_code": "", "stage": "select", "signal": signal, "order_ids": [],
                                                   "filled_qty": 0, "cost_amt": 0.0, "slice_no": 0, "done": False}],
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
                log.warning("[L] 查询竞价委托成交异常：%s", exc)
            filled = int(getattr(fill, "filled_qty", 0) or 0) if fill is not None else 0
            avg = float(getattr(fill, "avg_price", 0.0) or 0.0) if fill is not None else 0.0
            if not (fill is not None and bool(getattr(fill, "is_terminal", False))) and filled < int(item.get("auction_qty", 0)):
                _try_cancel_order(broker_cfg, str(item["auction_order_id"]), str(item["ts_code"]))
                time.sleep(2)
                try:
                    with _qmt_lock:
                        adapter = _qmt_get(broker_cfg)
                        fill = adapter.get_order_fill(str(item["auction_order_id"]))
                    filled = int(getattr(fill, "filled_qty", 0) or 0)
                    avg = float(getattr(fill, "avg_price", 0.0) or 0.0) or avg
                except Exception as exc:
                    log.warning("[L] 撤单后复查成交异常：%s", exc)
            if filled > 0:
                price = avg if avg > 0 else float(item.get("open_price", 0.0) or 0.0)
                _record_live_buy(
                    recovery_context="L竞价开仓", order_id=str(item["auction_order_id"]), ts_code=str(item["ts_code"]),
                    name=str(item.get("name", "")), signal_date=str(item.get("signal_date", "")), buy_date=today_str,
                    shares=filled, buy_price=price, strategy_leg=L_LEG, exit_n_days=1, benchmark_open=price,
                    execution_channel="L竞价开仓", planned_order_qty=int(item.get("auction_qty", filled)),
                    planned_exit_date_override=str(item.get("exit_date", "")), exit_rule="FIXED_T2_CLOSE",
                )
                item["auction_filled_qty"] = filled
                item["auction_cost"] = filled * price
                _notify("buy_result", "✅ L竞价成交",
                        f"策略=L {item['ts_code']} {item.get('name', '')} 成交{filled}股 @{price:.2f}，"
                        f"金额{_fmt_wan(filled * price)}；计划{str(item.get('exit_date', ''))[4:]}收盘卖出。")
            # 竞价零成交：开盘没涨停则在开盘价+2%以内补（与回测“开盘价买入”一致）；开盘涨停则今日不买（不递补）
            item["stage"] = "pov" if filled > 0 else "select"
            if filled > 0:
                try:
                    _l_prefill_remaining(item, broker_cfg, config, today_str)
                except Exception as exc:
                    log.warning("[L] 开盘前预挂剩余股数异常，改为09:30:10起补（不影响已成交部分）：%s", exc)
        with _l_state_lock:
            _l_save_state(state)
        _l_start_pov_worker()
    except Exception as exc:
        log.error("[L] 竞价后处理异常（不影响ACE和M）：%s", exc)


def _l_prefill_remaining(item: dict[str, Any], broker_cfg: dict, config: dict, today_str: str) -> None:
    """09:26 竞价成交后、09:30 前：按开盘价算出回测还差的股数先挂，09:30 一开盘就撮合（2026-11-27 用户要求）。

    竞价单按“涨停价−0.01”定股数，开盘价离涨停价越远少买得越多（今天信息发展少买 200 股），差额 09:25 就知道。
    限价=开盘价×(1+追价上限)取整、不超过涨停价−0.01；股数不超过竞价成交额×参与率（同原来第一片的参与上限）。
    没挂上（券商不收单、竞价残单还没撤完等）只记一条，09:30:10 照原来补仓。
    """
    log = logger()
    if item.get("prefill_order_id") or item.get("prefill_tried") or now_beijing().time() >= datetime.time(9, 30):
        return
    item["prefill_tried"] = True
    code, name = str(item.get("ts_code", "")), str(item.get("name", ""))
    lt = config.get("live_trade", {})
    chase = float(lt.get("pov_chase_cap", 0.02))
    part = float(lt.get("pov_participation", 0.10))
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        quote = adapter.get_full_tick([code]).get(code)
    a_qty, a_amt = int(item.get("auction_filled_qty", 0) or 0), float(item.get("auction_cost", 0.0) or 0.0)
    open_px = float(getattr(quote, "open_price", 0.0) or 0.0) if quote is not None else 0.0
    open_px = open_px or (a_amt / a_qty if a_qty > 0 else 0.0)
    upper = _l_upper_limit(quote, code, name) if quote is not None else 0.0
    if open_px <= 0 or upper <= 0:
        _l_note_slice(item, "prefill_none", why="取不到开盘价或涨停价")
        return
    item["open_price"] = open_px
    price = _round_stock_price(min(_round_stock_price(open_px * (1 + chase)), upper - 0.01))
    target = float(item.get("target_amount", 0.0) or 0.0)
    remaining = target - a_amt - float(item.get("cost_amt", 0.0) or 0.0)
    gap = max(_l_buy_quantity(code, target, open_px) - a_qty - int(item.get("filled_qty", 0) or 0), 0)
    auction_amount = float(getattr(quote, "amount", 0.0) or 0.0)
    qty = min(gap, _l_buy_quantity(code, min(remaining, part * auction_amount), price))
    if qty <= 0:
        _l_note_slice(item, "prefill_none", why="竞价已买足" if gap <= 0 else "竞价成交额太少，按%.0f%%参与不到一手" % (part * 100))
        return
    item["slice_no"] = int(item.get("slice_no", 0)) + 1
    ok, order_id, message = _l_submit_buy(
        broker_cfg, ts_code=code, name=name, qty=qty, price=price, signal_date=str(item.get("signal_date", "")),
        today_str=today_str, exit_date=str(item.get("exit_date", "")),
        source_key=f"L_PREFILL|{today_str}|{code}", channel="开盘前预挂")
    if not ok:
        log.warning("[L] %s 开盘前预挂%d股@%.2f未受理（%s），改为09:30:10起补。", code, qty, price, message)
        why = ("竞价残单还没撤完，同一策略同时只能有一张买单" if "ACTIVE_BUY_INTENT_EXISTS" in str(message)
               else str(message)[:40])
        _l_note_slice(item, "prefill_rejected", qty=qty, price=price, message=why)
        return
    item.setdefault("order_ids", []).append(order_id)
    item.setdefault("pov_order_ids", []).append(order_id)
    item.update({"prefill_order_id": order_id, "prefill_qty": qty, "prefill_price": price,
                 "last_market_amount": auction_amount})   # 09:30:10 那片只按开盘后新增成交额参与
    log.warning("⏳ [L] %s %s 开盘前预挂剩余%d股@%.2f（回测还差%d股，开盘价%.2f），09:30开盘撮合。",
                code, name, qty, price, gap, open_px)
    _l_note_slice(item, "prefill_placed", qty=qty, price=price)


def _l_settle_prefill(item: dict[str, Any], broker_cfg: dict) -> None:
    """09:30:10 第一片前：查开盘前预挂单成交多少，没成交的撤掉，成交计入补仓。"""
    log = logger()
    order_id, code = str(item.get("prefill_order_id", "")), str(item.get("ts_code", ""))
    qty, price = int(item.get("prefill_qty", 0) or 0), float(item.get("prefill_price", 0.0) or 0.0)
    fill = None
    try:
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            fill = adapter.get_order_fill(order_id)
    except Exception as exc:
        log.warning("[L] 查询开盘前预挂单成交异常：%s", exc)
    if fill is None or (int(getattr(fill, "filled_qty", 0) or 0) < qty and not bool(getattr(fill, "is_terminal", False))):
        _try_cancel_order(broker_cfg, order_id, code)
        time.sleep(2)
        try:
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                fill = adapter.get_order_fill(order_id)
        except Exception as exc:
            log.warning("[L] 预挂单撤单后复查异常：%s", exc)
    fq = int(getattr(fill, "filled_qty", 0) or 0) if fill is not None else 0
    if fq > 0:
        avg = float(getattr(fill, "avg_price", 0.0) or 0.0) or price
        item["filled_qty"] = int(item.get("filled_qty", 0) or 0) + fq
        item["cost_amt"] = float(item.get("cost_amt", 0.0) or 0.0) + fq * avg
        log.warning("✅ [L] %s %s 开盘前预挂成交%d股@%.2f", code, item.get("name", ""), fq, avg)
        _l_note_slice(item, "prefill_filled", qty=fq, price=avg)
    else:
        _l_note_slice(item, "prefill_unfilled", price=price)
    item["prefill_settled"] = True


def _l_pov_round(state: dict[str, Any], broker_cfg: dict, config: dict, today_str: str) -> None:
    """L买入POV执行一片：stage=select 时按开盘规则检查首选；stage=pov 时补足剩余目标金额。"""
    log = logger()
    item = state["items"][0]
    lt = config.get("live_trade", {})
    chase = float(lt.get("pov_chase_cap", 0.02))
    part = float(lt.get("pov_participation", 0.10))
    if item.get("stage") == "select":
        signal = item.get("signal") or _l_signal_for_today(today_str)
        if signal is None:
            item["done"], item["done_reason"] = True, "无L信号"
            return
        code = str(signal["ts_code"])
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            account = adapter.query_account()
            positions_live = adapter.query_positions()
            quotes = adapter.get_full_tick([code])
        streak = float(item.get("streak_mult", 0.0) or 0.0) or loss_streak_multiplier_for(today_str, config)
        ctx = _l_context(today_str, positions_live, float(getattr(account, "total_asset", 0.0) or 0.0), streak)
        budget = _l_budget(config, account, ctx, signal, today_str, streak)
        cand, why = _l_check(signal, quotes, ctx["avoid"], require_open=True)
        if cand is None:
            item["done"], item["done_reason"] = True, "L首选今日买不进（%s），不递补" % why
            log.info("[L] %s", item["done_reason"])
            return
        quote = quotes.get(code)
        item.update({"ts_code": code, "name": str(cand.get("name", "")),
                     "reference_price": float(cand.get("close", 0.0) or 0.0), "auction_qty": 0,
                     "signal_date": str(signal.get("signal_date", "")), "exit_date": _l_exit_date(today_str),
                     "target_amount": budget["amount"], "budget": budget, "streak_mult": streak,
                     "total_asset": float(getattr(account, "total_asset", 0.0) or 0.0),
                     "open_price": float(getattr(quote, "open_price", 0.0) or getattr(quote, "last_price", 0.0) or 0.0),
                     "stage": "pov"})
        item.pop("signal", None)
        _l_register_plan(item, today_str, "L开盘后POV买入")
    code, name = str(item["ts_code"]), str(item.get("name", ""))
    if item.get("prefill_order_id") and not item.get("prefill_settled"):
        _l_settle_prefill(item, broker_cfg)
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        quote = adapter.get_full_tick([code]).get(code)
    if quote is None:
        _l_note_slice(item, "no_quote")
        return
    open_px = float(item.get("open_price", 0.0) or 0.0) or float(getattr(quote, "open_price", 0.0) or 0.0)
    item["open_price"] = open_px
    upper = _l_upper_limit(quote, code, name)
    last = float(getattr(quote, "last_price", 0.0) or 0.0)
    filled_amount = float(item.get("auction_cost", 0.0) or 0.0) + float(item.get("cost_amt", 0.0) or 0.0)
    remaining = float(item.get("target_amount", 0.0) or 0.0) - filled_amount
    ask1 = 0.0
    asks = getattr(quote, "ask_prices", None)
    if asks and len(asks) >= 1 and float(asks[0] or 0) > 0:
        ask1 = float(asks[0])
    if open_px <= 0 or last <= 0 or upper <= 0:
        _l_note_slice(item, "no_quote")
        return
    # 追价上限取整到分后当委托价上限，现价超过取整后的上限才跳过（同 A 的 POV；2026-11-27 修复：原来拿取整后的
    # 委托价和没取整的上限比，开盘价×1.02 向上取整时卖一价一到上限就整片跳过，现价在上限内也补不上）
    cap_price = _round_stock_price(open_px * (1 + chase))
    price = _round_stock_price(min(x for x in (ask1 or last, cap_price, upper - 0.01) if x > 0))
    if last >= upper - 1e-6 or last > cap_price + 1e-6:
        log.info("[L POV] %s 现价%.2f超过开盘价+%.0f%%（上限%.2f）或涨停，本片不追。", code, last, chase * 100, cap_price)
        _l_note_slice(item, "over_chase", last=last, cap=cap_price, pct=chase, limit_up=last >= upper - 1e-6)
        return
    prev_amt = float(item.get("last_market_amount", 0.0) or 0.0)
    cum_amt = float(getattr(quote, "amount", 0.0) or 0.0)
    flow = max(cum_amt - prev_amt, 0.0) if prev_amt > 0 else cum_amt
    item["last_market_amount"] = cum_amt
    slice_amount = min(remaining, max(part * flow, 0.0))
    qty = _l_buy_quantity(code, slice_amount, price)
    if qty <= 0:
        if _l_buy_quantity(code, remaining, price) <= 0:
            item["done"], item["done_reason"] = True, "剩余目标不足最小申报量"
        else:
            _l_note_slice(item, "small_flow", flow=flow, pct=part)
        return
    item["slice_no"] = int(item.get("slice_no", 0)) + 1
    ok, order_id, message = _l_submit_buy(
        broker_cfg, ts_code=code, name=name, qty=qty, price=price, signal_date=str(item.get("signal_date", "")),
        today_str=today_str, exit_date=str(item.get("exit_date", "")),
        source_key=f"L_POV|{today_str}|{code}|{item['slice_no']}", channel="买入POV")
    if not ok:
        log.error("[L POV] %s 第%d片未受理：%s", code, item["slice_no"], message)
        _l_note_slice(item, "rejected", message=str(message)[:40])
        return
    item.setdefault("order_ids", []).append(order_id)
    item.setdefault("pov_order_ids", []).append(order_id)
    fill = _confirm_fill(broker_cfg, order_id, qty, "L买入POV", timeout_sec=60)
    if int(getattr(fill, "filled_qty", 0) or 0) < qty and not bool(getattr(fill, "is_terminal", False)):
        _try_cancel_order(broker_cfg, order_id, code)
        time.sleep(2)
        try:
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                fill = adapter.get_order_fill(order_id)
        except Exception as exc:
            log.warning("[L POV] 撤单后复查异常：%s", exc)
    fq = int(getattr(fill, "filled_qty", 0) or 0)
    if fq > 0:
        avg = float(getattr(fill, "avg_price", 0.0) or 0.0) or price
        item["filled_qty"] = int(item.get("filled_qty", 0)) + fq
        item["cost_amt"] = float(item.get("cost_amt", 0.0)) + fq * avg
        log.warning("✅ [L POV] %s %s 第%d片成交%d股@%.2f", code, name, item["slice_no"], fq, avg)
        _l_note_slice(item, "filled", qty=fq, price=avg)
    else:
        _l_note_slice(item, "unfilled", price=price)


def _l_note_slice(item: dict[str, Any], result: str, **detail: Any) -> None:
    """记下这一片补仓的结果（收口时汇总告诉用户每片为什么没补）。"""
    item.setdefault("slices", []).append(dict(detail, t=now_beijing().strftime("%H:%M"), result=result))


def _l_prefill_text(slices: list[dict[str, Any]]) -> str:
    """开盘前预挂那张单的说明（09:26 挂、09:30 开盘撮合、09:30:10 结算）。"""
    marks = {str(x.get("result", "")): x for x in slices if str(x.get("result", "")).startswith("prefill")}
    if "prefill_rejected" in marks:
        x = marks["prefill_rejected"]
        return "开盘前预挂%d股@%.2f未受理（%s），改为开盘后补" % (int(x.get("qty", 0)), float(x.get("price", 0)),
                                                       x.get("message", ""))
    if "prefill_none" in marks:
        return "开盘前没有预挂：%s" % marks["prefill_none"].get("why", "")
    if "prefill_placed" not in marks:
        return ""
    placed = marks["prefill_placed"]
    head = "%s开盘前预挂%d股@%.2f" % (placed.get("t", ""), int(placed.get("qty", 0)), float(placed.get("price", 0)))
    if "prefill_filled" in marks:
        got = marks["prefill_filled"]
        done = int(got.get("qty", 0))
        return head + "，开盘成交%d股@%.2f%s" % (done, float(got.get("price", 0)),
                                             "，其余撤单" if done < int(placed.get("qty", 0)) else "")
    if "prefill_unfilled" in marks:
        return head + "，开盘没成交，已撤单"
    return head + "，还没结算"


_L_SLICE_WHY = {
    "no_quote": "取不到行情",
    "small_flow": "这5分钟成交太少，按成交额%.0f%%参与买不到一手",
    "rejected": "委托未受理",
    "unfilled": "委托后没成交，已撤单",
}


def _l_fill_summary(item: dict[str, Any], reason: str) -> str:
    """L 收口说明：目标、竞价和补仓各买了多少、按回测还差多少、每片为什么没补、怎么收口的。"""
    code, name = str(item.get("ts_code", "")), str(item.get("name", ""))
    target = float(item.get("target_amount", 0.0) or 0.0)
    budget = item.get("budget") or {}
    total = float(item.get("total_asset", 0.0) or 0.0) or (
        float(budget.get("by_pct", 0.0) or 0.0) / float(budget["pct"]) if float(budget.get("pct", 0.0) or 0.0) > 0 else 0.0)
    share = (lambda amount: "/%.1f%%" % (100 * amount / total) if total > 0 else "")
    a_qty, a_amt = int(item.get("auction_filled_qty", 0) or 0), float(item.get("auction_cost", 0.0) or 0.0)
    p_qty, p_amt = int(item.get("filled_qty", 0) or 0), float(item.get("cost_amt", 0.0) or 0.0)
    have_qty, have_amt = a_qty + p_qty, a_amt + p_amt
    if have_qty <= 0 and (target <= 0 or not code):
        return "L今日未开仓：%s。" % reason
    parts = ["策略=L %s %s：目标%s%s" % (code, name, _fmt_wan(target), share(target)),
             "竞价成交%d股%s" % (a_qty, _fmt_wan(a_amt)) if a_qty else "竞价没成交",
             "开盘后补仓成交%d股%s" % (p_qty, _fmt_wan(p_amt)) if p_qty else "开盘后补仓0股",
             "累计%d股%s%s" % (have_qty, _fmt_wan(have_amt), share(have_amt))]
    # 开盘价在第一片补仓时才记下；补仓一片都没执行到时用竞价成交均价（集合竞价统一价成交，即开盘价）
    open_px = float(item.get("open_price", 0.0) or 0.0) or (a_amt / a_qty if a_qty > 0 else 0.0)
    gap = 0
    if open_px > 0 and target > 0 and code:
        should = _l_buy_quantity(code, target, open_px)
        gap = max(should - have_qty, 0)
        parts.append("按回测（目标÷开盘价%.2f，取整到一手）应买%d股，%s" % (
            open_px, should, "还差%d股约%s" % (gap, _fmt_wan(gap * open_px)) if gap else "已买足"))
    text = "；".join(parts) + "。"
    every = list(item.get("slices") or [])
    pre = _l_prefill_text(every)
    if pre:
        text += pre + "。"
    slices = [x for x in every if not str(x.get("result", "")).startswith("prefill")]
    if gap and slices:
        why = []
        chase = [x for x in slices if x.get("result") == "over_chase"]
        if chase:
            top = max(float(x.get("last", 0.0) or 0.0) for x in chase)
            why.append("%s现价超过开盘价+%.0f%%的上限%.2f元（最高%.2f元）%s，不追" % (
                "、".join(x["t"] for x in chase), 100 * float(chase[0].get("pct", 0.0) or 0.0),
                float(chase[0].get("cap", 0.0) or 0.0), top, "或涨停" if any(x.get("limit_up") for x in chase) else ""))
        for key, words in _L_SLICE_WHY.items():
            hit = [x for x in slices if x.get("result") == key]
            if hit:
                why.append("%s%s" % ("、".join(x["t"] for x in hit),
                                     words % (100 * float(hit[0].get("pct", 0.0) or 0.0)) if "%" in words else words))
        got = [x for x in slices if x.get("result") == "filled"]
        if got:
            why.append("成交的片：" + "、".join("%s %d股@%.2f" % (x["t"], int(x.get("qty", 0)), float(x.get("price", 0)))
                                             for x in got))
        text += "没补上的原因（09:30~10:30每5分钟一片，共%d片）：%s。" % (len(slices), "；".join(why))
    elif gap and item.get("stage") == "pov":
        text += "开盘后补仓一片都没执行到（可能是程序重启或行情异常），请看日志。"
    return text + "收口：%s。" % reason


def _l_pov_finalize(state: dict[str, Any], reason: str) -> None:
    item = state["items"][0]
    if item.get("finalized"):
        return
    item["done"], item["finalized"], item["done_reason"] = True, True, reason
    fq = int(item.get("filled_qty", 0) or 0)
    if fq > 0:
        today_str = today_beijing().strftime("%Y%m%d")
        avg = float(item["cost_amt"]) / fq
        _record_live_buy(
            recovery_context="L买入POV", order_id=f"lpov-{today_str}-{item['ts_code']}", ts_code=str(item["ts_code"]),
            name=str(item.get("name", "")), signal_date=str(item.get("signal_date", "")), buy_date=today_str,
            shares=fq, buy_price=avg, strategy_leg=L_LEG, exit_n_days=1, execution_channel="L买入POV",
            benchmark_open=float(item.get("open_price", 0.0) or 0.0), planned_order_qty=fq,
            planned_exit_date_override=str(item.get("exit_date", "")),
            component_order_ids=[str(o) for o in item.get("pov_order_ids", []) if str(o)], exit_rule="FIXED_T2_CLOSE",
        )
    # 2026-11-27（用户要求）：收口一定推一条，像A一样写清楚目标、竞价、补仓、还差多少、每片为什么没补
    title = ("✅ L开仓完成" if fq > 0 else
             "ℹ️ L开仓金额校准结束" if float(item.get("auction_cost", 0.0) or 0.0) else "ℹ️ L今日未开仓")
    _notify("buy_result", title, _l_fill_summary(item, reason))


def _l_pov_worker() -> None:
    log = logger()
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        broker_cfg = config.get("broker", {})
        today_str = today_beijing().strftime("%Y%m%d")
        while True:
            now = now_beijing()
            with _l_state_lock:
                state = _l_load_state()
            if str(state.get("date", "")) != today_str or not state.get("items"):
                return
            item = state["items"][0]
            if item.get("done"):
                with _l_state_lock:
                    _l_pov_finalize(state, str(item.get("done_reason", "完成")))
                    _l_save_state(state)
                return
            if now.time() >= L_POV_END_TIME:
                with _l_state_lock:
                    _l_pov_finalize(state, "10:30收口")
                    _l_save_state(state)
                return
            if now.time() < L_POV_START_TIME:
                time.sleep(min((datetime.datetime.combine(now.date(), L_POV_START_TIME, tzinfo=now.tzinfo) - now).total_seconds(), 60))
                continue
            with _l_state_lock:
                _l_pov_round(state, broker_cfg, config, today_str)
                remaining = (float(item.get("target_amount", 0.0) or 0.0) - float(item.get("auction_cost", 0.0) or 0.0)
                             - float(item.get("cost_amt", 0.0) or 0.0))
                if item.get("stage") == "pov" and item.get("ts_code") and remaining < 1.0:
                    item["done"], item["done_reason"] = True, "目标金额已买足"
                if item.get("done"):
                    # 当场收口：M 按 L 今日占用定仓，L 一结束就要让出没用到的预留（不等下一片）
                    _l_pov_finalize(state, str(item.get("done_reason", "完成")))
                _l_save_state(state)
                if item.get("done"):
                    return
            now2 = now_beijing()
            base = datetime.datetime.combine(now2.date(), L_POV_START_TIME, tzinfo=now2.tzinfo)
            k = int((now2 - base).total_seconds() // 300) + 1
            nxt = min(base + datetime.timedelta(seconds=k * 300),
                      datetime.datetime.combine(now2.date(), L_POV_END_TIME, tzinfo=now2.tzinfo))
            time.sleep(max(1.0, (nxt - now_beijing()).total_seconds()))
    except Exception as exc:
        log.error("[L POV] 线程异常退出（不影响ACE和M；已成交部分以券商为准，收盘对账核对）：%s", exc)


def _l_start_pov_worker() -> None:
    global _l_pov_thread
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    except Exception as exc:
        logger().warning("[L] 读取配置失败，L买入POV不启动（L今日不开仓，不影响ACE和M）：%s", exc)
        return
    if not _l_live_enabled(config):
        return
    if _l_pov_thread is not None and _l_pov_thread.is_alive():
        return
    if now_beijing().time() >= L_POV_END_TIME:
        with _l_state_lock:
            state = _l_load_state()
            if state.get("items") and str(state.get("date", "")) == today_beijing().strftime("%Y%m%d"):
                _l_pov_finalize(state, "重启时已过10:30，按已成交收口")
                _l_save_state(state)
        return
    _l_pov_thread = threading.Thread(target=_l_pov_worker, name="l-pov", daemon=True)
    _l_pov_thread.start()
