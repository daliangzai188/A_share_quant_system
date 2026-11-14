"""ACE开仓执行：实盘定仓、普通开仓执行器、09:20预挂与补挂、09:30开盘POV、E竞价与E延迟补仓、
盘前买单成交确认。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import json
import threading
import time
from pathlib import Path
from typing import Any

from src.utils.config import load_json_config, mkdir_p
from src.utils.time_utils import now_beijing, today_beijing
from src.portfolio_ace_m import ACE_FULL_HARD_CAP_PCT, ACE_FULL_TARGET_PCT, M_LEG, ace_limits
from src.daemon.common import (
    E_CAPACITY_FILE,
    LEGACY_E_CAPACITY_FILE,
    PROJECT_ROOT,
    _floor_buy_quantity_by_amount,
    _fmt_wan,
    _notify,
    _notify_async,
    _track_execution,
    _ts_code_aliases,
    append_risk_flag,
    logger,
    market_is_open,
    next_n_trade_days,
    to_float,
    to_int,
)
from src.daemon.state import (
    _append_pending_buys,
    _has_pending_buy_for_code,
    _mark_live_position_closed,
    _pov_load_state,
    _pov_save_state,
    _record_live_buy,
    _reduce_live_position_shares,
    _replace_pending_buy_order,
    find_open_position_by_code,
    load_pending_buys,
    load_positions,
    record_buy,
)
from src.daemon.plans import (
    _ace_gate_mode_for,
    _estimate_limit_up_price,
    _find_local_prev_close,
    _limit_up_pct_for_stock,
    _round_stock_price,
    _select_unique_buy_order_for_action,
    _signal_day_amount,
    has_position_bought_today,
    st_open_forbidden,
)
from src.daemon.qmt import (
    _broker_has_preexisting_strategy_position,
    _qmt_get,
    _qmt_lock,
    _qmt_reset,
    _strategy_only_market_value,
)
from src.daemon.orders import _confirm_fill, _format_order_fill_detail, _try_cancel_order
from src.daemon.sell import _cancel_own_takeprofit_orders, _pick_sell_limit_price


_premarket_buy_monitor_thread: threading.Thread | None = None


# ── 平仓检查（最高优先级，独立运行，绝不因其他错误跳过）────────────────────

def _execute_orders_inprocess(
    planned_orders_path: Path | str,
    confirm: str,
    tag: str,
    *,
    allowed_sides: set[str] | None = None,
    allow_t2_close_sell_now: bool = False,
) -> bool:
    """普通开仓执行器；SELL只能走带退出批次/保护底线的唯一平仓状态机。"""
    import pandas as pd
    from dataclasses import asdict
    from src.live_order_gateway import LiveOrderGateway
    from src.broker_adapter import OrderRequest

    log = logger()
    if allowed_sides != {"BUY"}:
        log.critical(
            "[%s] 普通计划执行器拒绝SELL/未声明方向；所有平仓必须走唯一退出状态机。",
            tag,
        )
        return False
    config_path = PROJECT_ROOT / "config" / "config.json"
    gateway = LiveOrderGateway(config_path)

    try:
        gateway.assert_real_order_allowed(confirm, side="BUY")
    except RuntimeError as e:
        log.error("❌ [%s] 下单条件不满足：%s", tag, e)
        return False

    try:
        _, planned_orders = gateway.load_planned_orders(planned_orders_path)
    except Exception as e:
        log.error("❌ [%s] 读取计划单失败：%s", tag, e)
        return False

    if planned_orders.empty:
        log.info("[%s] 计划单为空，跳过", tag)
        return False

    if allowed_sides is not None and "side" in planned_orders.columns:
        allowed_upper = {side.upper() for side in allowed_sides}
        before = len(planned_orders)
        planned_orders = planned_orders[planned_orders["side"].astype(str).str.upper().isin(allowed_upper)].copy()
        skipped = before - len(planned_orders)
        if skipped:
            log.info("[%s] 跳过非本流程方向订单 %d 条，仅允许 side=%s", tag, skipped, sorted(allowed_upper))

    if not allow_t2_close_sell_now and {"side", "planned_action"}.issubset(planned_orders.columns):
        side_text = planned_orders["side"].astype(str).str.upper()
        action_text = planned_orders["planned_action"].astype(str).str.upper()
        risk_text = (
            planned_orders["risk_flags"].astype(str).str.upper()
            if "risk_flags" in planned_orders.columns
            else pd.Series("", index=planned_orders.index)
        )
        t2_close_sell = (
            side_text.eq("SELL")
            & (
                action_text.eq("PLAN_SELL_T2_CLOSE")
                | action_text.eq("PLAN_SELL_D_T2_CLOSE")
                | action_text.eq("PLAN_SELL_L_T2_CLOSE")
                | risk_text.str.contains("E_SELL_T2_CLOSE", na=False)
                | risk_text.str.contains("L_SELL_T2_CLOSE", na=False)
            )
        )
        if t2_close_sell.any():
            skipped = planned_orders[t2_close_sell]
            for _, row in skipped.iterrows():
                log.warning(
                    "⏸️ [%s] 跳过T2收盘卖计划：%s %s planned_action=%s。该类订单只允许14:55平仓流程执行。",
                    tag,
                    row.get("ts_code", ""),
                    row.get("name", ""),
                    row.get("planned_action", ""),
                )
            planned_orders = planned_orders[~t2_close_sell].copy()

    if planned_orders.empty:
        log.info("[%s] 过滤后无可执行订单，跳过", tag)
        return False

    broker_cfg = config_path and load_json_config(config_path).get("broker", {})
    with _qmt_lock:
      try:
        adapter = _qmt_get(broker_cfg)
        account = adapter.query_account()
        if "side" in planned_orders.columns:
            _sell_codes = set(
                planned_orders.loc[planned_orders["side"].astype(str).str.upper() == "SELL", "ts_code"]
                .dropna().astype(str)
            )
            if _sell_codes:
                # 先撤自家止盈单再取 positions/open_orders 快照：
                # 否则可用股数不足与重复单校验都会拒掉平仓单
                _cancel_own_takeprofit_orders(adapter, _sell_codes)
        positions = adapter.query_positions()
        open_orders = adapter.query_orders()
        ts_codes = sorted(
            planned_orders.get("ts_code", pd.Series(dtype=str))
            .dropna().astype(str).unique().tolist()
        )
        quote_map = adapter.get_full_tick(ts_codes) if ts_codes else {}
        # 旧策略仓未实际清空时取消衔接买入。这里是执行层兜底：即使磁盘上残留了
        # 旧版买入计划，也只保留卖单，绝不让历史计划绕过新的串行单仓口径。
        if _broker_has_preexisting_strategy_position(positions):
            buy_mask = planned_orders["side"].astype(str).str.upper().eq("BUY")
            if buy_mask.any():
                blocked = planned_orders[buy_mask]
                blocked_names = "、".join(
                    f"{row.get('ts_code', '')} {row.get('name', '')}" for _, row in blocked.iterrows()
                )
                log.warning(
                    "🚫 [%s] 券商仍有本系统旧策略仓，取消衔接开仓；已拦截买单：%s。",
                    tag,
                    blocked_names,
                )
                planned_orders = planned_orders[~buy_mask].copy()
                _notify(
                    "buy_result",
                    "🚫 旧仓未清空，已拦截新开仓",
                    f"{blocked_names} 未提交；券商仍实际持有本系统旧策略仓，确认清仓前不允许新买入。",
                    level="timeSensitive",
                )
        if planned_orders.empty:
            return False

        # 仓位口径只计策略持仓市值：打新中签/人工持仓不挤占策略额度。
        # 今日到期仓也必须计入，直到券商实际清空为止。
        entry_market_value = _strategy_only_market_value(
            positions,
            exclude_due_today=False,
            family="ACE",
        )
        planned_orders = resize_buy_orders_for_live_account(
            planned_orders=planned_orders,
            account=account,
            quote_map=quote_map,
            current_market_value=entry_market_value,
            transition_full_cash=False,
            m_market_value=_strategy_only_market_value(positions, family=M_LEG),
        )

        preview = gateway.validate_planned_orders(
            planned_orders,
            account.available_cash,
            open_orders,
            quote_map,
            positions=positions,
            account_total_asset=account.total_asset,
            current_market_value=entry_market_value,
            transition_full_cash=False,
        )

        # 保存 preview CSV 供审计
        preview_csv = PROJECT_ROOT / "reports" / "live_trade" / "qmt_live_order_preview.csv"
        mkdir_p(preview_csv.parent)
        preview.to_csv(preview_csv, index=False, encoding="utf-8-sig")

        executable = preview[
            (preview["validation_status"].astype(str) == "PASS")
            & (preview["real_order_enabled"].astype(str).str.lower().isin({"true", "1"}))
        ]

        if executable.empty:
            rejected = preview[preview["validation_status"].astype(str) != "PASS"]
            for _, r in rejected.iterrows():
                reject_reason = explain_reject_reasons(r)
                log.warning(
                    "⚠️ [%s] %s %s 被拒绝：%s",
                    tag,
                    r.get("side", ""),
                    r.get("ts_code", ""),
                    reject_reason,
                )
                side_text = str(r.get("side", "")).upper()
                code_text = str(r.get("ts_code", ""))
                name_text = str(r.get("name", ""))
                if side_text == "SELL":
                    _notify("sell_fail", "❌ 平仓校验被拒",
                            f"{code_text} {name_text} 平仓未提交：{reject_reason}。请立即回终端核对持仓。",
                            level="critical", call=True)
                elif side_text == "BUY":
                    _notify("buy_result", "⚠️ 开仓校验被拒",
                            f"{code_text} {name_text} 开仓未提交：{reject_reason}。",
                            level="timeSensitive")
            return False

        now_str = now_beijing().strftime("%H:%M:%S")
        results = []
        accepted_any = False
        submitted: list[dict[str, Any]] = []   # 待成交确认的已受理委托
        for _, row in executable.iterrows():
            side = str(row["side"]).upper()
            qty = int(row["quantity"])
            ref_price = float(row.get("last_price", 0.0) or row.get("reference_price", 0.0))
            order_price_type = str(row["price_type"])
            order_price = float(row.get("price", 0.0))
            order_remark = str(row.get("remark", ""))
            branch_id = str(row.get("matched_strategy_branch_ids", ""))
            profile_ids = str(row.get("matched_condition_profile_ids", ""))
            exit_rule = str(row.get("exit_rule", ""))
            planned_exit_date = str(row.get("planned_exit_date", "")).split(".")[0]
            if side == "BUY" and str(row.get("strategy_leg", "")).upper() == "C":
                log.info(
                    "[C分支退出裁决] %s %s 分支=%s profile=%s 规则=%s "
                    "exit_n_days=%s 计划平仓日=%s",
                    row.get("ts_code", ""),
                    row.get("name", ""),
                    branch_id,
                    profile_ids,
                    exit_rule,
                    row.get("exit_n_days", ""),
                    planned_exit_date,
                )
            # 卖出（E平仓）改用买10/买5挂限价，确保成交、避免被动过夜
            if side == "SELL":
                sell_price, sell_label = _pick_sell_limit_price(quote_map.get(str(row["ts_code"])))
                if sell_price > 0:
                    order_price_type = "FIXED_PRICE"
                    order_price = sell_price
                    ref_price = sell_price
                    order_remark = f"{order_remark}|平仓{sell_label}".strip("|")
                    log.info("[%s] %s 平仓挂单取价 %s=%.2f", tag, row["ts_code"], sell_label, sell_price)
            else:
                _track_execution(
                    "register_entry_plan",
                    entry_date=today_beijing().strftime("%Y%m%d"),
                    ts_code=str(row.get("ts_code", "")),
                    name=str(row.get("name", "")),
                    strategy_leg=str(row.get("strategy_leg", "")),
                    signal_date=str(row.get("signal_date", "")),
                    target_qty=qty,
                    target_amount=qty * ref_price,
                    reference_price=ref_price,
                    auction_planned_qty=0,
                    pov_planned_qty=0,
                    pov_target_amount=0,
                    status="待开盘补买执行",
                )
            request = OrderRequest(
                ts_code=str(row["ts_code"]),
                broker_code=str(row["broker_code"]),
                side=side,
                quantity=qty,
                price_type=order_price_type,
                price=order_price,
                strategy_name=str(row.get("strategy_name", "A_SYSTEM_ABC")),
                remark=order_remark,
                strategy_leg=str(row.get("strategy_leg", "")).upper(),
                business_date=today_beijing().strftime("%Y%m%d"),
                signal_date=str(row.get("signal_date", "")),
                planned_exit_date=str(row.get("planned_exit_date", "")),
                purpose="OPEN" if side == "BUY" else "CLOSE",
                source_key=(
                    f"{tag}|{today_beijing().strftime('%Y%m%d')}|{side}|"
                    f"{row['ts_code']}|{order_remark}|{qty}"
                ),
                metadata={
                    "execution_channel": tag,
                    "name": str(row.get("name", "")),
                    "strategy_branch_id": branch_id,
                    "condition_profile_ids": profile_ids,
                    "exit_rule": exit_rule,
                },
            )
            result = adapter.place_order(request)
            results.append(asdict(result))
            if result.accepted:
                accepted_any = True
                log.info("✅ [%s] %s 已受理 %s %s %d股 参考价%.2f元 金额%.0f元（待成交确认）",
                         tag, now_str, side, row["ts_code"], qty, ref_price, qty * ref_price)
                raw_exit_n = row.get("exit_n_days", None)
                exit_n = int(float(raw_exit_n)) if raw_exit_n is not None and str(raw_exit_n) not in {"", "nan"} else 2
                submitted.append({
                    "order_id": str(result.order_id or row.get("paper_order_id", f"live-{now_str}-{row['ts_code']}")),
                    "side": side,
                    "ts_code": str(row["ts_code"]),
                    "name": str(row.get("name", "")),
                    "signal_date": str(row.get("signal_date", "")),
                    "strategy_leg": str(row.get("strategy_leg", "")),
                    "qty": qty,
                    "ref_price": ref_price,
                    "exit_n": exit_n,
                    "planned_exit_date": planned_exit_date,
                    "strategy_branch_id": branch_id,
                    "condition_profile_ids": profile_ids,
                    "exit_rule": exit_rule,
                })
            else:
                log.error("❌ [%s] %s %s %s %d股 失败：%s",
                          tag, now_str, side, row["ts_code"], qty, result.message)

        # 保存提交结果 CSV
        result_csv = PROJECT_ROOT / "reports" / "live_trade" / "qmt_live_order_submitted_orders.csv"
        pd.DataFrame(results).to_csv(result_csv, index=False, encoding="utf-8-sig")

      except Exception as e:
        _qmt_reset()
        log.error("❌ [%s] 下单异常（已重置连接）：%s", tag, e)
        return False

    # ── 成交确认 + 持仓回写（在 QMT 锁外执行，避免轮询期间长时间占锁）──
    today_str = today_beijing().strftime("%Y%m%d")
    for s in submitted:
        try:
            fill = _confirm_fill(broker_cfg, s["order_id"], s["qty"], tag)
            fill_price = fill.avg_price if fill.avg_price > 0 else s["ref_price"]
            if s["side"] == "BUY":
                if fill.filled_qty > 0:
                    _record_live_buy(
                        recovery_context=tag,
                        order_id=s["order_id"],
                        ts_code=s["ts_code"],
                        name=s["name"],
                        signal_date=s["signal_date"],
                        buy_date=today_str,
                        shares=fill.filled_qty,
                        buy_price=fill_price,
                        strategy_leg=s["strategy_leg"],
                        exit_n_days=s["exit_n"],
                        traded_at=getattr(fill, "traded_at", ""),
                        execution_channel="开盘补买" if "09:30" in tag else "普通买入",
                        planned_order_qty=s["qty"],
                        benchmark_open=0.0,
                        planned_exit_date_override=s.get("planned_exit_date", ""),
                        strategy_branch_id=s.get("strategy_branch_id", ""),
                        condition_profile_ids=s.get("condition_profile_ids", ""),
                        exit_rule=s.get("exit_rule", ""),
                    )
                    amount = fill.filled_qty * fill_price
                    if fill.filled_qty < s["qty"]:
                        planned_price = float(s.get("ref_price", fill_price) or fill_price)
                        planned_amount = int(s["qty"]) * planned_price
                        unfilled_amount = max(planned_amount - amount, 0.0)
                        log.warning("⚠️ [%s] %s 买入部分成交 %d/%d股 @%.2f，按实际成交记录持仓。",
                                    tag, s["ts_code"], fill.filled_qty, s["qty"], fill_price)
                        _notify("buy_result", "⚠️ 开仓部分成交",
                                f"策略={s['strategy_leg']} {s['ts_code']} {s['name']} 成交{fill.filled_qty}/{s['qty']}股 "
                                f"@{fill_price:.2f}。总委托金额{_fmt_wan(planned_amount)}，"
                                f"已成交金额{_fmt_wan(amount)}，未成交金额{_fmt_wan(unfilled_amount)}")
                    else:
                        log.info("✅ [%s] 持仓信息：策略=%s %s %s 持仓%d股 成本%.2f 市值%s",
                                 tag, s["strategy_leg"], s["ts_code"], s["name"],
                                 fill.filled_qty, fill_price, _fmt_wan(amount))
                        _notify("buy_result", "✅ 持仓信息",
                                f"策略={s['strategy_leg']} {s['ts_code']} {s['name']} "
                                f"持仓{fill.filled_qty}股 成本{fill_price:.2f} 市值{_fmt_wan(amount)}")
                else:
                    log.error("❌ [%s] %s 买入未成交（状态=%s），不记录持仓，避免幽灵持仓。",
                              tag, s["ts_code"], fill.status_text)
                    _notify("buy_result", "❌ 开仓未成交",
                            f"{s['ts_code']} {s['name']} 买入委托未成交，未记账，请回终端确认。",
                            level="critical", call=True)
            else:  # SELL
                local_pos = find_open_position_by_code(s["ts_code"], s["strategy_leg"])
                local_oid = local_pos.get("order_id", "") if local_pos else ""
                held = int(local_pos.get("shares", s["qty"])) if local_pos else s["qty"]
                if not local_oid:
                    log.warning("⚠️ [%s] %s 卖出后未找到本地持仓记录，跳过回写。", tag, s["ts_code"])
                    _notify("sell_fail", "❌ 平仓回写缺失",
                            f"{s['ts_code']} {s['name']} 卖出后未找到本地持仓记录，请立即核对QMT和positions.json。",
                            level="critical", call=True)
                elif fill.filled_qty >= held:
                    _mark_live_position_closed(
                        local_oid, today_str, fill_price, recovery_context=tag
                    )
                    log.info("✅ [%s] %s 卖出全部成交 %d股 @%.2f，已平仓。",
                             tag, s["ts_code"], fill.filled_qty, fill_price)
                    _notify("sell_success", "✅ 平仓成交",
                            f"{s['ts_code']} {s['name']} 卖出{fill.filled_qty}股 "
                            f"@{fill_price:.2f} 金额{_fmt_wan(fill.filled_qty * fill_price)}")
                elif fill.filled_qty > 0:
                    _reduce_live_position_shares(
                        local_oid,
                        held - fill.filled_qty,
                        fill_price=fill_price,
                        recovery_context=tag,
                    )
                    log.warning("⚠️ [%s] %s 卖出部分成交 %d/%d股 @%.2f，剩余%d股保留持仓待下次卖出。",
                                tag, s["ts_code"], fill.filled_qty, held, fill_price, held - fill.filled_qty)
                    _notify("sell_fail", "⚠️ 平仓部分成交",
                            f"{s['ts_code']} {s['name']} 卖出{fill.filled_qty}/{held}股 "
                            f"@{fill_price:.2f}，剩余{held - fill.filled_qty}股仍持有，请回终端查看。",
                            level="timeSensitive")
                else:
                    log.error("❌ [%s] %s 卖出未成交（状态=%s），持仓保留，等待下次重试或手动处理。",
                              tag, s["ts_code"], fill.status_text)
                    _notify("sell_fail", "❌ 平仓未成交",
                            f"{s['ts_code']} {s['name']} 平仓委托未成交，可能被动过夜，请立即回终端处理。",
                            level="critical", call=True)
        except Exception as e:
            log.error("❌ [%s] %s 成交确认/回写异常：%s —— 请手动核对！", tag, s["ts_code"], e)
            _notify("sell_fail", "❌ 平仓回写异常",
                    f"{s['ts_code']} {s['name']} 平仓成交确认/回写出现异常，请立即回终端核对持仓。",
                    level="critical", call=True)

    return accepted_any


def resize_buy_orders_for_live_account(
    planned_orders: "pd.DataFrame",
    account: Any,
    quote_map: dict[str, Any],
    current_market_value: float,
    transition_full_cash: bool = False,
    log_adjust: bool = True,
    m_market_value: float = 0.0,
    action_date: str = "",
) -> "pd.DataFrame":
    """按实盘账户资金和风控上限缩放买入计划，避免用回测初始资金生成超额订单。

    log_adjust=False 供播报预演调用：那条"实盘定仓…调整为N股"是下单路径的语义，
    播报时打出来既是噪音，数字也和播报最终展示的股数（还要再按涨停价核冻结资金）
    不一致，会让人误以为要买N股(2026-07-31)。
    """
    if planned_orders.empty or "side" not in planned_orders.columns:
        return planned_orders

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    live_cfg = config.get("live_trade", {})
    # 统一语义：max_single_order_amount=0 → 取消固定金额限额，空仓日由82.5%
    # 目标总仓位接管，同时保留85%单票硬顶；>0 → 额外施加单笔固定限额（元）。
    # transition_full_cash 仅兼容旧调用签名，不再放宽总仓位上限。
    max_single_order_amount = _effective_single_order_cap(live_cfg)
    # 85%是代码级绝对上限，配置只能收紧不能放宽；82.5%同理固定为本版目标上限。
    max_position_pct = min(float(live_cfg.get("max_position_pct", ACE_FULL_HARD_CAP_PCT)), ACE_FULL_HARD_CAP_PCT)
    max_total_position_pct = min(
        float(live_cfg.get("max_total_position_pct", ACE_FULL_TARGET_PCT)), ACE_FULL_TARGET_PCT
    )
    transition_full_cash = False
    round_lot_size = int(live_cfg.get("round_lot_size", 100))
    cash_buffer = float(live_cfg.get("cash_buffer_amount", 1000))

    total_asset = float(getattr(account, "total_asset", 0.0) or getattr(account, "available_cash", 0.0) or 0.0)
    # 2026-11-12 ACE+M：ACE目标=82.5%（方案甲停手期20%），且不超过99.5%−M当前持仓占比；
    # 单票硬顶=目标+2.5个百分点（同受99.5%约束）；80%验收线按目标等比例缩放。
    # current_market_value 由调用方传入ACE族（不含M）策略市值。
    ace_mode = _ace_gate_mode_for(action_date)
    ace_cap = ace_limits(ace_mode, total_asset, float(m_market_value or 0.0))
    max_position_pct = min(max_position_pct, ace_cap["hard_pct"])
    effective_total_position_pct = min(max_total_position_pct, ace_cap["target_pct"])
    min_accept_scale = (effective_total_position_pct / max_total_position_pct) if max_total_position_pct > 0 else 0.0
    available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
    market_value = float(current_market_value or 0.0)
    adjusted = planned_orders.copy()
    # 2026-08-03 统一实际成交额口径：参考价股数只负责给竞价段提供“种子数量”，
    # 真正的开仓目标固定为09:20账户总资产快照×82.5%。竞价按涨停价预挂后，
    # 09:30起POV按已确认实际成交额动态补到目标；80%是验收下限，85%既是累计
    # 持仓硬顶，也是按最坏委托价计算的累计买单硬顶。三项金额必须随计划落盘，
    # 不能再从09:20参考价或预估股数倒推，否则20cm/30cm股票会系统性少买。
    if "live_cash_cap" not in adjusted.columns:
        adjusted["live_cash_cap"] = 0.0
    for amount_column in (
        "live_target_amount",
        "live_min_acceptable_amount",
        "live_hard_cap_amount",
        "live_equity_snapshot",
        "live_available_cash_snapshot",
    ):
        if amount_column not in adjusted.columns:
            adjusted[amount_column] = 0.0
    if "risk_flags" not in adjusted.columns:
        adjusted["risk_flags"] = ""
    else:
        # planned_orders 来自 CSV 时，空 risk_flags 会被 pandas 读成 float64 NaN。
        # 后续需要追加 LIVE_SIZE_ADJUSTED 等字符串标记，先转 object，避免09:20实盘定仓时报dtype错误。
        adjusted["risk_flags"] = adjusted["risk_flags"].astype("object")

    # 同票集中度防线仍保留为纵深保护：即使上游串行仓位门禁失效，新候选也不能
    # 与昨日及更早买入且未清仓的同一标的重合。
    # ⚠️ 身份区分(用户强调):当日买入的持仓=POV拆单在途(竞价段已成交、
    # 平滑段继续买同一只票),绝不算同票冲突——只拦"昨日及更早买入且未清仓"
    # 的仓与收盘后新候选的重合。
    held_aliases: set[str] = set()
    if bool(live_cfg.get("same_stock_skip_enabled", True)):
        try:
            _today_s = today_beijing().strftime("%Y%m%d")
            for p in load_positions():
                if str(p.get("status", "")).lower() not in {"open", "sell_pending"}:
                    continue
                if str(p.get("buy_date", "")) == _today_s:
                    continue  # 当日买入=本候选的拆单开仓,不是"既有持仓"
                held_aliases.update(_ts_code_aliases(p.get("ts_code", "")))
        except Exception as e:
            logger().warning("同票检查读取持仓失败(本轮不拦截):%s", e)

    for idx, row in adjusted.iterrows():
        side = str(row.get("side", "")).upper()
        if side != "BUY":
            continue

        ts_code = str(row.get("ts_code", "")).upper()
        if held_aliases and any(al in held_aliases for al in _ts_code_aliases(ts_code)):
            adjusted.at[idx, "round_lot_shares"] = 0
            adjusted.at[idx, "estimated_shares"] = 0
            adjusted.at[idx, "planned_amount_by_equity"] = 0.0
            adjusted.at[idx, "risk_flags"] = append_risk_flag(
                row.get("risk_flags", ""), "SAME_STOCK_ALREADY_HELD_SKIP")
            logger().warning("🚫 [同票防线] %s 今日候选与已持仓相同,放弃买入(防同票连续暴露/黑天鹅集中度)。", ts_code)
            _notify("buy_result", "🚫 同票候选已放弃",
                    f"{ts_code} 今日候选与当前持仓为同一只票,按集中度防线放弃买入"
                    f"(避免同票连续暴露4天全仓押注)。", level="timeSensitive")
            continue
        quote = quote_map.get(ts_code)
        reference_price = to_float(row.get("reference_price", 0.0))
        last_price = float(getattr(quote, "last_price", 0.0) or reference_price)
        price = last_price if last_price > 0 else reference_price
        if price <= 0:
            adjusted.at[idx, "round_lot_shares"] = 0
            adjusted.at[idx, "estimated_shares"] = 0
            adjusted.at[idx, "planned_amount_by_equity"] = 0.0
            adjusted.at[idx, "risk_flags"] = append_risk_flag(row.get("risk_flags", ""), "LIVE_SIZE_NO_PRICE")
            continue

        cash_cap = max(0.0, available_cash - cash_buffer)
        single_position_cap = max(0.0, total_asset * max_position_pct)
        target_position_cap = max(
            0.0,
            total_asset * effective_total_position_pct - market_value,
        )
        min_acceptable_amount = max(
            0.0,
            total_asset * float(live_cfg.get("entry_min_acceptable_position_pct", 0.80)) * min_accept_scale
            - market_value,
        )
        pre_liquidity_target = min(
            cash_cap, single_position_cap, target_position_cap, max_single_order_amount
        )
        pre_liquidity_hard_cap = min(
            cash_cap, single_position_cap, max_single_order_amount
        )
        # 大资金退出容量硬顶。81笔5m/1m回放没有找到可“保证”14:57前退出的
        # 信号日比例；0.5%仅作为风险上限：1000万仓位要求信号日成交额至少20亿。
        # 当数据缺失时必须fail-closed，不能让读数失败反而解除千万级门禁。
        liq_cap = float("inf")
        signal_day_amount = 0.0
        _sig_date = str(row.get("signal_date", "")).strip().split(".")[0]
        if _sig_date and _sig_date.isdigit():
            signal_day_amount = _signal_day_amount(ts_code, _sig_date)
        if signal_day_amount > 0:
            liq_cap = signal_day_amount * float(
                live_cfg.get("total_liquidity_cap_pct", 0.005)
            )
        elif bool(live_cfg.get("liquidity_cap_fail_closed", True)):
            liq_cap = 0.0
            adjusted.at[idx, "risk_flags"] = append_risk_flag(
                adjusted.at[idx, "risk_flags"], "LIQUIDITY_CAP_DATA_MISSING"
            )
            logger().error(
                "实盘开仓拒绝：%s 信号日%s成交额缺失，退出容量门禁fail-closed。",
                ts_code, _sig_date or "未知",
            )
        # target_amount是POV最终要追踪的累计实际成交额；hard_cap_amount是任何时点
        # “已确认实际成交额 + 新买单最坏成交额”都不得突破的85%绝对红线。
        target_amount = min(pre_liquidity_target, liq_cap)
        hard_cap_amount = min(pre_liquidity_hard_cap, liq_cap)
        if liq_cap < pre_liquidity_target and liq_cap > 0:
            adjusted.at[idx, "risk_flags"] = append_risk_flag(
                adjusted.at[idx, "risk_flags"],
                f"EXIT_CAPACITY_CAP:{liq_cap:.0f}",
            )

        # 现金/固定单笔限额在下单前就不足80%时直接拒开，避免明知资金不足仍开
        # 一个明显偏小的仓。退出容量是更高优先级的流动性硬风控：若它低于80%，
        # 继续沿用既有“按容量缩仓”行为，不能为了凑比例突破可安全退出的上限。
        if pre_liquidity_target + 0.01 < min_acceptable_amount:
            adjusted.at[idx, "risk_flags"] = append_risk_flag(
                adjusted.at[idx, "risk_flags"],
                f"ENTRY_MIN_80_PCT_UNREACHABLE:{pre_liquidity_target:.0f}<{min_acceptable_amount:.0f}",
            )
            target_amount = 0.0
        elif 0 < target_amount + 0.01 < min_acceptable_amount:
            adjusted.at[idx, "risk_flags"] = append_risk_flag(
                adjusted.at[idx, "risk_flags"],
                f"ENTRY_BELOW_80_BY_EXIT_CAP:{target_amount:.0f}",
            )
            min_acceptable_amount = target_amount

        old_qty = to_int(row.get("round_lot_shares", row.get("estimated_shares", 0)))
        max_qty = _floor_buy_quantity_by_amount(target_amount, price, round_lot_size)
        # 2026-07-15 起执行层实时定仓（可放大可缩小）：种子股数只承载"买什么"，
        # "买多少"由下单时刻的 min(现金,单票仓位,总仓位,单笔限额) 决定——
        # 修复"晚间种子固化+resize只缩不放"导致改限额不生效、需手动重跑的顽疾。
        # old_qty<=0 保持0（0股占位=策略不买的语义，绝不放大）。
        new_qty = max_qty if old_qty > 0 else 0
        if round_lot_size > 0:
            new_qty -= new_qty % round_lot_size
        new_qty = max(0, new_qty)
        new_amount = new_qty * price

        adjusted.at[idx, "estimated_shares"] = new_qty
        adjusted.at[idx, "round_lot_shares"] = new_qty
        adjusted.at[idx, "planned_amount_by_equity"] = new_amount
        adjusted.at[idx, "live_cash_cap"] = cash_cap
        adjusted.at[idx, "live_target_amount"] = target_amount
        adjusted.at[idx, "live_min_acceptable_amount"] = min_acceptable_amount
        adjusted.at[idx, "live_hard_cap_amount"] = hard_cap_amount
        adjusted.at[idx, "live_equity_snapshot"] = total_asset
        adjusted.at[idx, "live_available_cash_snapshot"] = available_cash
        adjusted.at[idx, "planned_equity"] = total_asset
        if total_asset > 0:
            adjusted.at[idx, "planned_position_pct"] = new_amount / total_asset
        adjusted.at[idx, "transition_full_cash"] = transition_full_cash

        if new_qty != old_qty:
            adjusted.at[idx, "risk_flags"] = append_risk_flag(
                adjusted.at[idx, "risk_flags"],
                f"LIVE_SIZE_ADJUSTED:{old_qty}->{new_qty}",
            )
        if new_qty != old_qty and log_adjust:
            logger().warning(
                "实盘定仓：%s 种子计划%d股，按资金/仓位/单笔/退出容量调整为%d股；"
                "可用资金%.0f元，总资产%.0f元，当前市值%.0f元，单笔上限%.0f元，"
                "信号日成交额%.0f元，退出容量上限%.0f元，参考成交价%.2f元",
                ts_code,
                old_qty,
                new_qty,
                available_cash,
                total_asset,
                market_value,
                max_single_order_amount,
                signal_day_amount,
                liq_cap,
                price,
            )

    return adjusted


def _effective_single_order_cap(live_cfg: dict[str, Any]) -> float:
    """统一单笔限额语义：配置<=0表示不限额，由现金、仓位和流动性上限接管。"""
    configured_cap = float(live_cfg.get("max_single_order_amount", 0) or 0)
    return configured_cap if configured_cap > 0 else float("inf")


def _calculate_pov_buy_quantity(
    *,
    target_amount: float,
    hard_cap_amount: float,
    confirmed_actual_amount: float,
    available_cash: float,
    cash_buffer: float,
    slice_budget: float,
    order_price: float,
    lot_size: int = 100,
) -> tuple[int, float, float, float]:
    """按累计实际成交额计算一片POV买单。

    返回(委托股数, 距82.5%目标的缺口, 距85%硬顶的空间, 本片金额上限)。
    股数使用本片真实委托价计算，而不是最新价或09:20参考价，因此即使整笔按
    委托上限价成交，“累计实际成交额+本片最坏成交额”也不会突破85%。
    """
    actual = max(float(confirmed_actual_amount or 0.0), 0.0)
    target_gap = max(float(target_amount or 0.0) - actual, 0.0)
    hard_room = max(float(hard_cap_amount or 0.0) - actual, 0.0)
    cash_room = max(float(available_cash or 0.0) - float(cash_buffer or 0.0), 0.0)
    order_amount_cap = min(
        target_gap,
        hard_room,
        cash_room,
        max(float(slice_budget or 0.0), 0.0),
    )
    quantity = _floor_buy_quantity_by_amount(order_amount_cap, order_price, lot_size)
    return quantity, target_gap, hard_room, order_amount_cap


def explain_reject_reasons(row: Any) -> str:
    raw = str(row.get("reject_reasons", "") or "")
    if not raw:
        return "未知原因，请查看 qmt_live_order_preview.csv"
    side = str(row.get("side", "")).upper()
    ts_code = str(row.get("ts_code", ""))
    quantity = to_int(row.get("quantity", 0))
    estimated_amount = to_float(row.get("estimated_live_amount", 0.0))
    available_cash = to_float(row.get("available_cash", 0.0))
    total_asset = to_float(row.get("total_asset", 0.0))
    current_market_value = to_float(row.get("current_market_value", 0.0))
    last_price = to_float(row.get("last_price", 0.0))
    max_cfg = load_json_config(PROJECT_ROOT / "config" / "config.json").get("live_trade", {})
    max_position_pct = float(max_cfg.get("max_position_pct", ACE_FULL_HARD_CAP_PCT))
    max_total_position_pct = float(max_cfg.get("max_total_position_pct", ACE_FULL_TARGET_PCT))
    effective_total_position_pct = to_float(
        row.get("effective_total_position_pct", max_total_position_pct),
        max_total_position_pct,
    )
    max_single_order_amount = float(max_cfg.get("max_single_order_amount", 100000))

    parts = []
    for code in [item for item in raw.split("|") if item]:
        if code == "OUTSIDE_TRADING_TIME":
            if side == "BUY":
                parts.append(f"不在允许交易时间内；BUY 允许 09:15-14:56，09:15前或14:56后会被拒")
            else:
                parts.append(f"不在允许交易时间内；SELL 允许 09:15-11:30、13:00-15:00")
        elif code == "EXCEED_POSITION_PCT":
            cap = total_asset * max_position_pct
            parts.append(f"超过单票仓位上限：订单约{estimated_amount:.0f}元，上限={total_asset:.0f}×{max_position_pct:.1%}={cap:.0f}元")
        elif code == "EXCEED_TOTAL_POSITION_PCT":
            cap = total_asset * effective_total_position_pct - current_market_value
            parts.append(
                f"超过总仓位上限：订单约{estimated_amount:.0f}元，"
                f"当前口径上限{effective_total_position_pct:.1%}，剩余额度约{cap:.0f}元"
            )
        elif code == "INSUFFICIENT_CASH":
            parts.append(f"可用资金不足：订单约{estimated_amount:.0f}元，可用资金{available_cash:.0f}元")
        elif code == "EXCEED_SINGLE_ORDER_AMOUNT":
            parts.append(f"超过单笔金额上限：订单约{estimated_amount:.0f}元，要求小于{max_single_order_amount:.0f}元")
        elif code == "LIMIT_UP_BUY_REJECTED":
            parts.append(f"买入价接近涨停，按配置拒绝涨停买入；最新价{last_price:.2f}元")
        elif code == "LIMIT_DOWN_SELL_REJECTED":
            parts.append(f"卖出价接近跌停，按配置拒绝跌停卖出；最新价{last_price:.2f}元")
        elif code == "SELL_VOLUME_NOT_AVAILABLE":
            parts.append(f"可卖数量不足：计划卖{quantity}股，请核对QMT持仓可用数量")
        elif code == "EMPTY_OR_ZERO_QUANTITY":
            parts.append(f"委托数量为0：{ts_code} 经资金/风控缩单后不足一手")
        else:
            parts.append(code)
    return "；".join(parts)


def _record_e_capacity_and_alert(records: list[dict[str, Any]], live_cfg: dict) -> None:
    """E 容量档案与关停预警。必须在 09:24 关键窗口结束后调用（含共享盘IO与推送）。

    E 仓位 = min(缩单额, 竞价额×参与率)：账户资金增长后若频繁被竞价盘钳制，
    E 的绝对收益不再随资金增长，而隔夜持仓风险与运维成本不变。
    钳制比 = 实际可用仓位/计划仓位；竞价额兜底(fallback)的记录不计入容量证据。
    最近 window 笔竞价记录中钳制 ≥ min_hits 次 → 强提醒建议关闭 E。
    """
    log = logger()
    alert_ratio = float(live_cfg.get("e_capacity_alert_clamp_ratio", 0.5))
    window = int(live_cfg.get("e_capacity_alert_window", 5))
    min_hits = int(live_cfg.get("e_capacity_alert_min_hits", 3))
    try:
        source_path = E_CAPACITY_FILE if E_CAPACITY_FILE.exists() else LEGACY_E_CAPACITY_FILE
        payload = json.loads(source_path.read_text(encoding="utf-8")) if source_path.exists() else {}
    except Exception:
        payload = {}
    history = payload.get("records", [])
    for rec in records:
        planned = float(rec.get("planned_amt", 0) or 0)
        rec["clamp_ratio"] = round(float(rec.get("cap", 0) or 0) / planned, 4) if planned > 0 else 1.0
        history.append(rec)
        if rec.get("source") == "auction" and rec["clamp_ratio"] < alert_ratio:
            _notify_async(
                "buy_result", "📉 E容量提示：仓位被竞价盘钳制",
                f"{rec.get('ts_code','')} {rec.get('name','')} 计划{planned / 1e4:.2f}万，"
                f"竞价容量只允许{float(rec.get('cap', 0)) / 1e4:.2f}万（{rec['clamp_ratio']:.0%}）。"
                f"资金规模已接近E标的的竞价容量上限。",
            )
    history = history[-30:]
    auction_recent = [r for r in history if r.get("source") == "auction"][-window:]
    hits = [r for r in auction_recent if float(r.get("clamp_ratio", 1.0)) < alert_ratio]
    today_str = today_beijing().strftime("%Y%m%d")
    if len(auction_recent) >= window and len(hits) >= min_hits and payload.get("last_capacity_alert_date") != today_str:
        payload["last_capacity_alert_date"] = today_str
        avg_clamp = sum(float(r["clamp_ratio"]) for r in hits) / len(hits)
        detail = "；".join(
            f"{r.get('name') or r.get('ts_code', '')} {r.get('date','')[-4:]} "
            f"竞价{float(r.get('auction_amt', 0)) / 1e4:.0f}万→仓位{float(r.get('clamp_ratio', 1.0)):.0%}"
            for r in hits[-3:]
        )
        _notify(
            "buy_result", "⚠️ E容量预警：建议评估关闭E",
            f"最近{len(auction_recent)}笔E竞价开仓中有{len(hits)}笔被竞价盘钳制"
            f"（被钳制笔平均只能用计划资金的{avg_clamp:.0%}）。明细：{detail}。"
            f"若竞价额普遍几十万级则是资金超容量（E绝对收益不再随资金增长，隔夜风险不变），"
            f"如决定停用：config的 live_trade.e_enabled 改为 false（信号照常生成，只停止下单）；"
            f"若竞价额只是近期偶发偏小，可以先观察。",
            level="timeSensitive",
        )
        log.warning("E容量预警已推送：最近%d笔中%d笔钳制（阈值%.0f%%）。", len(auction_recent), len(hits), alert_ratio * 100)
    payload["records"] = history
    try:
        mkdir_p(E_CAPACITY_FILE.parent)
        tmp = E_CAPACITY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(E_CAPACITY_FILE)
    except Exception as e:
        log.error("E容量档案写入失败：%s", e)


def _e_auction_buy_worker(e_rows: list[Any], broker_cfg: dict, today_str: str) -> None:
    """E 竞价动态买入：09:24 读实时竞价撮合量，按参与率上限定仓后挂涨停价。

    仓位 = min(资金缩单后金额, 实时竞价成交额 × e_auction_participation_ratio)。
    竞价额读不到（QMT竞价时段字段可能无效）时用 e_auction_fallback_amount 兜底。
    E 标的从不一字开盘，09:24 挂单无排队损失；挂单后并入 pending_buys，
    由既有的 09:30 确认链路和盘前监控线程统一收尾。
    """
    from src.broker_adapter import OrderRequest

    log = logger()
    try:
        # 所有共享盘IO(读配置)在09:20线程启动时就完成,09:24关键窗口内
        # 只剩两次短锁QMT调用(读tick+下单),把慢IO挡在时间窗之外。
        cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
        lt = cfg.get("live_trade", {})
        if not bool(lt.get("e_enabled", True)):
            log.warning("E已被 live_trade.e_enabled=false 关闭，今日%d只E候选不挂单。", len(e_rows))
            return
        ratio = float(lt.get("e_auction_participation_ratio", 0.10))
        fallback_amt = float(lt.get("e_auction_fallback_amount", 50000))
        capacity_records: list[dict[str, Any]] = []
        target = now_beijing().replace(hour=9, minute=24, second=0, microsecond=0)
        wait = (target - now_beijing()).total_seconds()
        if wait > 0:
            time.sleep(min(wait, 540))
        # 窗口预算监控:9:25撮合前必须完成挂单;超时由09:30补买路径兜底,
        # 这里只负责让日志能看出迟到。
        if now_beijing().time() >= datetime.time(9, 25, 0):
            log.warning("E竞价买入线程晚于09:25启动执行（可能被IO/锁延误），仍尝试挂单；若错过竞价将由09:30补买兜底。")
        # 09:20检查后到09:24仍可能发生人工成交或状态变化，提交E买单前再次读取
        # 券商持仓。只拦昨日及更早的策略仓，不拦当日本次开仓的部分成交。
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            positions_live = adapter.query_positions()
        if _broker_has_preexisting_strategy_position(positions_live):
            log.warning("🚫 [E竞价买入] 券商仍有本系统旧策略仓，取消衔接开仓；E本轮不挂单。")
            return
        pov_enabled_e = bool(lt.get("pov_enabled", True))
        actual_amount_rebalance_e = bool(
            lt.get("entry_actual_amount_rebalance_enabled", True)
        )
        pov_items_e: list[dict[str, Any]] = []
        new_pending: list[dict[str, Any]] = []
        for row in e_rows:
            try:
                ts_code = str(row["ts_code"]); name_s = str(row.get("name", ""))
                total_target_qty = int(row.get("round_lot_shares", 0) or 0)
                reference_price = float(row.get("reference_price", 0.0) or 0.0)
                planned_amt = to_float(
                    row.get("live_target_amount", total_target_qty * reference_price)
                )
                min_acceptable_amount = to_float(
                    row.get("live_min_acceptable_amount", 0.0)
                )
                hard_cap_amount = to_float(
                    row.get("live_hard_cap_amount", planned_amt)
                )
                equity_snapshot = to_float(row.get("live_equity_snapshot", 0.0))
                available_cash_snapshot = to_float(
                    row.get("live_available_cash_snapshot", 0.0)
                )
                if planned_amt <= 0 or total_target_qty <= 0:
                    log.warning(
                        "E竞价买入：%s 82.5%%目标不可执行或不足一手，跳过竞价与POV。",
                        ts_code,
                    )
                    continue
                signal_date_s = str(row.get("signal_date", ""))
                with _qmt_lock:
                    adapter = _qmt_get(broker_cfg)
                    quote_map = adapter.get_full_tick([ts_code])
                quote = quote_map.get(ts_code)
                auction_amt = float(getattr(quote, "amount", 0.0) or 0.0) if quote else 0.0
                if auction_amt <= 0 and quote is not None:
                    vol = float(getattr(quote, "volume", 0.0) or 0.0)
                    lp = float(getattr(quote, "last_price", 0.0) or 0.0)
                    auction_amt = vol * lp * 100 if vol > 0 and lp > 0 else 0.0
                if auction_amt > 0:
                    cap = min(planned_amt, auction_amt * ratio)
                    src = f"竞价额{auction_amt / 1e4:.0f}万×{ratio:.0%}"
                else:
                    cap = min(planned_amt, fallback_amt)
                    src = f"竞价额不可得,兜底{fallback_amt / 1e4:.0f}万"
                # 只在内存收集容量记录；落盘和推送在09:24关键窗口结束后统一执行
                capacity_records.append({
                    "date": today_str, "ts_code": ts_code, "name": name_s,
                    "planned_amt": planned_amt, "auction_amt": auction_amt, "cap": cap,
                    "source": "auction" if auction_amt > 0 else "fallback",
                })
                price, price_label = _premarket_buy_price(quote, ts_code, name_s, signal_date_s)
                if price <= 0:
                    _track_execution(
                        "register_entry_plan",
                        entry_date=today_str, ts_code=ts_code, name=name_s,
                        strategy_leg="E", signal_date=signal_date_s,
                        target_qty=total_target_qty, target_amount=planned_amt,
                        reference_price=reference_price, auction_planned_qty=0,
                        pov_planned_qty=total_target_qty if pov_enabled_e else 0,
                        pov_target_amount=planned_amt if pov_enabled_e else 0,
                        status="价格不可得",
                    )
                    log.warning("E竞价买入：%s 无法取得涨停价，跳过。", ts_code)
                    continue
                # 容量按撮合参考价计算股数；涨停委托价只计算冻结资金硬顶。
                # 竞价金额不可得时，5万元是参考成交金额兜底，不能再除以
                # 20%涨停委托价导致额外少一手。实际成交额仍由原POV校准。
                capacity_price = (
                    float(getattr(quote, "last_price", 0.0) or 0.0)
                    if auction_amt > 0 and quote is not None else reference_price
                )
                qty = _e_auction_seed_quantity(
                    capacity_amount=cap, capacity_price=capacity_price,
                    limit_price=price, hard_cap_amount=hard_cap_amount,
                    total_target_qty=total_target_qty,
                )
                expected_auction_amount = qty * capacity_price
                log.info("[E竞价定量] %s 目标%.2f元 容量%.2f元 参考价%.2f元 "
                         "委托价%.2f元 股数%d 参考成交额%.2f元 最坏冻结额%.2f元 硬顶%.2f元",
                         ts_code, planned_amt, cap, capacity_price, price, qty,
                         expected_auction_amount, qty * price, hard_cap_amount)
                pov_target_amount = max(planned_amt - expected_auction_amount, 0.0) if pov_enabled_e else 0.0
                pov_planned_qty = (
                    int(pov_target_amount // reference_price // 100) * 100
                    if pov_enabled_e and reference_price > 0 else 0
                )
                if st_open_forbidden(ts_code, name_s):
                    log.error("⛔ [ST禁买] %s %s 命中ST/风险警示铁律，拦截E竞价买入委托。", ts_code, name_s)
                    _notify("buy_result", "⛔ ST股禁买拦截",
                            f"{ts_code} {name_s} 为ST/风险警示股，全局铁律禁止开仓，已拦截E竞价买入。",
                            level="timeSensitive")
                    continue

                if pov_enabled_e and actual_amount_rebalance_e:
                    raw_exit_pov = row.get("exit_n_days", None)
                    exit_n_pov = int(float(raw_exit_pov)) if raw_exit_pov is not None and str(raw_exit_pov) not in {"", "nan"} else 1
                    baseline_qty, baseline_cost = _broker_position_quantity_and_cost(
                        positions_live, ts_code
                    )
                    pov_items_e.append({
                        "ts_code": ts_code, "name": name_s, "strategy_leg": "E",
                        "signal_date": signal_date_s,
                        "broker_code": str(row.get("broker_code", ts_code)),
                        "strategy_name": str(row.get("strategy_name", "A_SYSTEM_ABC")),
                        "exit_n": exit_n_pov,
                        "sig_amt": _signal_day_amount(ts_code, signal_date_s.strip().split(".")[0]),
                        "target_amt": planned_amt, "remain_amt": planned_amt,
                        "target_actual_amount": planned_amt,
                        "min_acceptable_amount": min_acceptable_amount,
                        "hard_cap_amount": hard_cap_amount,
                        "equity_snapshot": equity_snapshot,
                        "available_cash_snapshot": available_cash_snapshot,
                        "broker_baseline_qty": baseline_qty,
                        "broker_baseline_cost_amount": baseline_cost,
                        "total_target_qty": total_target_qty,
                        "total_target_amt": planned_amt,
                        "reference_price": reference_price,
                        "auction_planned_qty": qty,
                        "pov_planned_qty": pov_planned_qty,
                        "filled_qty": 0, "cost_amt": 0.0, "slice_no": 0,
                        "prev_cum_amount": 0.0, "done": False,
                    })
                _track_execution(
                    "register_entry_plan",
                    entry_date=today_str, ts_code=ts_code, name=name_s,
                    strategy_leg="E", signal_date=signal_date_s,
                    target_qty=total_target_qty, target_amount=planned_amt,
                    reference_price=reference_price, auction_planned_qty=qty,
                    pov_planned_qty=pov_planned_qty,
                    pov_target_amount=pov_target_amount,
                    status="待竞价确认并按实际成交额POV校准",
                )
                if qty <= 0:
                    if pov_enabled_e and actual_amount_rebalance_e:
                        _track_execution(
                            "register_entry_plan",
                            entry_date=today_str, ts_code=ts_code, name=name_s,
                            strategy_leg="E", signal_date=signal_date_s,
                            target_qty=total_target_qty, target_amount=planned_amt,
                            reference_price=reference_price, auction_planned_qty=0,
                            pov_planned_qty=total_target_qty,
                            pov_target_amount=planned_amt,
                            status="全部转买入POV",
                        )
                        log.warning("E竞价买入：%s 竞价段不足一手（%s），82.5%%目标%.1f万全额转POV平滑段。",
                                    ts_code, src, planned_amt / 1e4)
                    else:
                        log.warning("E竞价买入：%s 动态仓位不足一手（%s，计划%.0f元），放弃本笔。", ts_code, src, planned_amt)
                        _notify("buy_result", "⚠️ E流动性不足放弃开仓",
                                f"{ts_code} {name_s} 竞价盘过小（{src}），动态仓位不足一手，今日放弃。", level="timeSensitive")
                    continue
                log.warning("⏳ [E竞价买入] %s %s %d股 %s=%.2f元（%s；参考成交额%.2f万，最坏冻结额%.2f万）",
                            ts_code, name_s, qty, price_label, price, src,
                            expected_auction_amount / 1e4, qty * price / 1e4)
                raw_exit_n = row.get("exit_n_days", None)
                exit_n = int(float(raw_exit_n)) if raw_exit_n is not None and str(raw_exit_n) not in {"", "nan"} else 1
                planned_exit = str(row.get("planned_exit_date", "") or "")
                if not planned_exit:
                    planned_exit = next_n_trade_days(
                        datetime.datetime.strptime(today_str, "%Y%m%d").date(), n=exit_n
                    ).strftime("%Y%m%d")
                request = OrderRequest(
                    ts_code=ts_code, broker_code=str(row.get("broker_code", ts_code)),
                    side="BUY", quantity=qty, price_type="FIXED_PRICE", price=price,
                    strategy_name=str(row.get("strategy_name", "A_SYSTEM_ABC")),
                    remark=f"E竞价动态-{today_str}",
                    strategy_leg="E",
                    business_date=today_str,
                    signal_date=signal_date_s,
                    planned_exit_date=planned_exit,
                    purpose="OPEN",
                    source_key=f"E_AUCTION|{today_str}|{ts_code}",
                    metadata={"execution_channel": "E竞价动态", "name": name_s,
                              "exit_n_days": exit_n},
                )
                with _qmt_lock:
                    adapter = _qmt_get(broker_cfg)
                    result = adapter.place_order(request)
                if result.accepted:
                    pending_order = {
                        "order_id": str(result.order_id or f"eauction-{today_str}-{ts_code}"),
                        "ts_code": ts_code, "name": name_s,
                        "signal_date": signal_date_s,
                        "strategy_leg": "E", "qty": qty, "ref_price": price, "exit_n": exit_n,
                        "entry_target_qty": total_target_qty,
                        "entry_target_amount": planned_amt,
                        "entry_min_acceptable_amount": min_acceptable_amount,
                        "entry_hard_cap_amount": hard_cap_amount,
                        "entry_equity_snapshot": equity_snapshot,
                    }
                    # 券商受理后立即持久化，不把多标的循环结束当作安全边界。
                    _append_pending_buys([pending_order])
                    new_pending.append(pending_order)
                    log.info("✅ [E竞价买入] %s %s %d股 @%.2f 已受理（待09:30确认）", ts_code, name_s, qty, price)
                    _notify("buy_result", "📋 E竞价动态开仓已挂单",
                            f"{ts_code} {name_s} {qty}股@{price:.2f}（{src}），待09:30确认实际成交额后"
                            "按82.5%目标、85%硬顶由POV校准。", level="timeSensitive")
                else:
                    log.error("❌ [E竞价买入] %s 提交失败：%s", ts_code, result.message)
            except Exception as e:
                log.error("E竞价买入异常（%s）：%s", row.get("ts_code"), e)
        if new_pending:
            _start_premarket_buy_monitor()
        if pov_items_e:
            _pov_enqueue(pov_items_e)
        if capacity_records:
            try:
                _record_e_capacity_and_alert(capacity_records, lt)
            except Exception as e:
                log.error("E容量预警统计异常（不影响交易）：%s", e)
    except Exception as e:
        log.error("E竞价动态买入线程异常：%s", e)


# ── POV 贪心前置开仓执行引擎 ──────────────────────────────────────────────
# 2026-07-16 定稿(docs/two_phase_execution_design.md,83笔真实5m价格回放验证:
# 复利打平"开盘价满仓零冲击"理想口径,VWAP/TWAP铺满窗口方案被判死)。
#   竞价段 = 信号日成交额×pov_auction_share(0.1%),照旧09:20/09:24挂涨停参与竞价;
#   平滑段 = 09:30起每5分钟一片,片预算=min(剩余, 上一片实测成交额×pov_participation),
#            首片预算基数=信号日成交额×pov_first_slice_share(开盘5分钟量的实测25分位);
#   追价保护: 现价>开盘价×(1+pov_chase_cap)或涨停 → 该片跳过(涨了不追,等回落);
#   10:30收口,未完成放弃——宁可仓位不满,不追高破坏回测口径。
# 2026-08-03起，无论盘前估算是否需要拆分，开盘后都用同一POV状态按实际
# 成交额复核82.5%目标；已达目标则零下单结束，未达才分片补足。

def _pov_auction_share_for(sig_amt: float, lt: dict) -> float:
    """竞价段占信号日成交额的比例——按信号日成交额分档(2026-07-16 用户观察+数据证实:
    竞价占比随规模递减,75笔实测中位 2-5亿=2.41%/5-10亿=1.40%/>10亿=1.00%)。
    档位取25分位×10%参与率(保守:四分之三的日子实际竞价参与率≤10%红线)。
    tiers格式 [[信号日成交额上限(元), share], ...],兜底 pov_auction_share。"""
    tiers = lt.get("pov_auction_share_tiers")
    fallback = float(lt.get("pov_auction_share", 0.001))
    if not isinstance(tiers, list):
        return fallback
    try:
        for cap_amt, share in tiers:
            if sig_amt <= float(cap_amt):
                return float(share)
        return float(tiers[-1][1])
    except Exception:
        return fallback


def _e_auction_seed_quantity(*, capacity_amount: float, capacity_price: float,
                             limit_price: float, hard_cap_amount: float,
                             total_target_qty: int, lot_size: int = 100) -> int:
    """E竞价股数：参考成交容量、最坏冻结资金和原计划股数分别约束。"""
    if capacity_price <= 0 or limit_price <= 0 or lot_size <= 0:
        return 0
    return min(
        _floor_buy_quantity_by_amount(capacity_amount, capacity_price, lot_size),
        _floor_buy_quantity_by_amount(hard_cap_amount, limit_price, lot_size),
        max(int(total_target_qty), 0) // lot_size * lot_size,
    )


def _pov_auction_seed_quantity(
    *,
    total_target_qty: int,
    reference_price: float,
    signal_day_amount: float,
    live_cfg: dict[str, Any],
    lot_size: int = 100,
) -> tuple[int, float, float]:
    """计算正常09:20竞价种子单，返回(竞价股数、竞价金额容量、参与比例)。

    这里只负责“竞价买多少”；82.5%目标减去竞价真实成交额后的缺口由POV负责。
    信号日成交额缺失时无法可靠计算竞价容量，竞价段按0股fail-closed，完整目标
    交给开盘后POV；绝不能把82.5%目标股数误当成竞价挂单量。异常错过整个竞价
    窗口的场景同样以auction_planned_qty=0把全部实际金额缺口交给POV。
    """

    target_qty = max(int(total_target_qty or 0), 0)
    ref_price = max(float(reference_price or 0.0), 0.0)
    sig_amount = max(float(signal_day_amount or 0.0), 0.0)
    lot = max(int(lot_size or 100), 1)
    if target_qty <= 0 or ref_price <= 0 or sig_amount <= 0:
        return 0, 0.0, 0.0
    share = _pov_auction_share_for(sig_amount, live_cfg)
    auction_cap = sig_amount * share
    if target_qty * ref_price <= auction_cap:
        return target_qty, auction_cap, share
    auction_qty = int(auction_cap // ref_price // lot) * lot
    return max(auction_qty, 0), auction_cap, share


POV_EXEC_LOG = PROJECT_ROOT / "reports" / "pov_execution_log.csv"
_pov_thread_lock = threading.Lock()
_pov_thread: threading.Thread | None = None


def _pov_enqueue(items: list[dict[str, Any]]) -> None:
    """把实际成交额校准任务并入当日队列并确保执行线程在跑。"""
    if not items:
        return
    today_str = today_beijing().strftime("%Y%m%d")
    state = _pov_load_state() or {"date": today_str, "items": []}
    existing = {it["ts_code"]: it for it in state.get("items", [])}
    for it in items:
        if it["ts_code"] not in existing:
            state.setdefault("items", []).append(it)
        else:
            # 同日重复触发只更新固定目标/风控快照，不重置已成交额和分片进度。
            old = existing[it["ts_code"]]
            for key in (
                "target_actual_amount", "target_amt", "total_target_amt",
                "min_acceptable_amount", "hard_cap_amount", "equity_snapshot",
                "available_cash_snapshot", "broker_baseline_qty",
                "broker_baseline_cost_amount", "sig_amt", "reference_price",
            ):
                if key in it:
                    old[key] = it[key]
    _pov_save_state(state)
    for it in items:
        logger().warning(
            "📥 [POV] %s %s 实际成交额校准入队：82.5%%目标%.1f万、80%%下限%.1f万、"
            "85%%硬顶%.1f万；09:30确认竞价成交后按真实缺口分片执行。",
            it["ts_code"], it.get("name", ""),
            float(it.get("target_actual_amount", it.get("target_amt", 0))) / 1e4,
            float(it.get("min_acceptable_amount", 0)) / 1e4,
            float(it.get("hard_cap_amount", 0)) / 1e4,
        )
    _start_pov_worker()


def _enqueue_opening_pov_from_plan(
    planned_orders_path: Path | None,
    *,
    open_action: str,
    reason: str,
) -> bool:
    """09:30竞价主链缺失时，把普通正式候选恢复为同一套持久化POV任务。

    这里不再用普通计划执行器一次性整单追买。账户快照、82.5%目标、80%验收
    下限、85%硬顶、退出容量、ST/旧仓门禁都与09:20主路径共用；POV状态先
    落盘再启动线程，daemon重启后仍能按券商真实成交额继续，不会重复建仓。
    """

    log = logger()
    if planned_orders_path is None or not planned_orders_path.exists():
        log.error("09:30 %s失败：组合计划单不存在。", reason)
        return False
    try:
        import pandas as pd
        from src.live_order_gateway import LiveOrderGateway

        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        lt_cfg = config.get("live_trade", {})
        if not (
            bool(lt_cfg.get("pov_enabled", True))
            and bool(lt_cfg.get("entry_actual_amount_rebalance_enabled", True))
        ):
            log.error(
                "09:30 %s失败：买入POV/实际成交额校准未启用；为避免整单追买，"
                "本轮fail-closed不提交。",
                reason,
            )
            return False
        confirm = str(
            lt_cfg.get("real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED")
        )
        try:
            LiveOrderGateway(PROJECT_ROOT / "config" / "config.json").assert_real_order_allowed(
                confirm, side="BUY"
            )
        except RuntimeError as exc:
            log.error("09:30 %s失败：实盘门禁未通过：%s", reason, exc)
            return False

        try:
            orders = pd.read_csv(planned_orders_path)
        except (OSError, pd.errors.EmptyDataError) as exc:
            log.error("09:30 %s失败：计划单读取失败：%s", reason, exc)
            return False
        buy_orders = _select_unique_buy_order_for_action(
            orders,
            open_action,
            context=f"09:30 {reason}",
        )
        if buy_orders.empty:
            return False

        broker_cfg = config.get("broker", {})
        ts_codes = buy_orders["ts_code"].dropna().astype(str).tolist()
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            account = adapter.query_account()
            positions_live = adapter.query_positions()
            quote_map = adapter.get_full_tick(ts_codes) if ts_codes else {}
        if _broker_has_preexisting_strategy_position(positions_live):
            log.warning("🚫 [09:30 POV恢复] 券商仍有本系统旧策略仓，全部新开仓取消。")
            return False

        buy_orders = resize_buy_orders_for_live_account(
            planned_orders=buy_orders,
            account=account,
            quote_map=quote_map,
            current_market_value=_strategy_only_market_value(
                positions_live, exclude_due_today=False, family="ACE"
            ),
            transition_full_cash=False,
            m_market_value=_strategy_only_market_value(positions_live, family=M_LEG),
        )
        today_str = today_beijing().strftime("%Y%m%d")
        pov_items: list[dict[str, Any]] = []
        for _, row in buy_orders.iterrows():
            ts_code = str(row.get("ts_code", ""))
            name_s = str(row.get("name", ""))
            qty = int(row.get("round_lot_shares", 0) or 0)
            target_amount = to_float(row.get("live_target_amount", 0.0))
            min_amount = to_float(row.get("live_min_acceptable_amount", 0.0))
            hard_amount = to_float(row.get("live_hard_cap_amount", 0.0))
            reference_price = to_float(row.get("reference_price", 0.0))
            if qty <= 0 or target_amount <= 0 or hard_amount <= 0:
                log.error(
                    "09:30 %s %s无法建立POV：股数=%d、目标=%.2f、硬顶=%.2f。",
                    ts_code,
                    name_s,
                    qty,
                    target_amount,
                    hard_amount,
                )
                continue
            if st_open_forbidden(ts_code, name_s):
                log.error("⛔ [ST禁买] %s %s 命中风险警示，09:30 POV恢复已拦截。", ts_code, name_s)
                continue
            signal_date_s = str(row.get("signal_date", "")).strip().split(".")[0]
            raw_exit_n = row.get("exit_n_days", None)
            exit_n = (
                int(float(raw_exit_n))
                if raw_exit_n is not None and str(raw_exit_n) not in {"", "nan"}
                else 2
            )
            baseline_qty, baseline_cost = _broker_position_quantity_and_cost(
                positions_live, ts_code
            )
            pov_items.append({
                "ts_code": ts_code,
                "name": name_s,
                "strategy_leg": str(row.get("strategy_leg", "")),
                "signal_date": signal_date_s,
                "broker_code": str(row.get("broker_code", ts_code)),
                "strategy_name": str(row.get("strategy_name", "A_SYSTEM_ABC")),
                "exit_n": exit_n,
                "planned_exit_date": str(row.get("planned_exit_date", "")).split(".")[0],
                "strategy_branch_id": str(row.get("matched_strategy_branch_ids", "")),
                "condition_profile_ids": str(row.get("matched_condition_profile_ids", "")),
                "exit_rule": str(row.get("exit_rule", "")),
                "sig_amt": _signal_day_amount(ts_code, signal_date_s),
                "target_amt": target_amount,
                "remain_amt": target_amount,
                "target_actual_amount": target_amount,
                "min_acceptable_amount": min_amount,
                "hard_cap_amount": hard_amount,
                "equity_snapshot": to_float(row.get("live_equity_snapshot", 0.0)),
                "available_cash_snapshot": to_float(
                    row.get("live_available_cash_snapshot", 0.0)
                ),
                "broker_baseline_qty": baseline_qty,
                "broker_baseline_cost_amount": baseline_cost,
                "total_target_qty": qty,
                "total_target_amt": target_amount,
                "reference_price": reference_price,
                "auction_planned_qty": 0,
                "pov_planned_qty": qty,
                "filled_qty": 0,
                "cost_amt": 0.0,
                "slice_no": 0,
                "prev_cum_amount": 0.0,
                "done": False,
            })
            _track_execution(
                "register_entry_plan",
                entry_date=today_str,
                ts_code=ts_code,
                name=name_s,
                strategy_leg=str(row.get("strategy_leg", "")),
                signal_date=signal_date_s,
                target_qty=qty,
                target_amount=target_amount,
                reference_price=reference_price,
                auction_planned_qty=0,
                pov_planned_qty=qty,
                pov_target_amount=target_amount,
                status="09:30降级为持久化买入POV",
            )
        if not pov_items:
            log.error("09:30 %s未生成任何可执行POV任务，fail-closed不开仓。", reason)
            return False
        _pov_enqueue(pov_items)
        log.warning(
            "⚠️ 09:30 %s已转入持久化POV：%d只；不使用一次性整单追买。",
            reason,
            len(pov_items),
        )
        return True
    except Exception as exc:
        log.exception("09:30 %s建立POV异常：%s", reason, exc)
        return False


def _start_pov_worker() -> None:
    global _pov_thread
    with _pov_thread_lock:
        if _pov_thread is not None and _pov_thread.is_alive():
            return
        _pov_thread = threading.Thread(target=_pov_worker, daemon=True, name="pov-smooth-buy")
        _pov_thread.start()


def _pov_log_slice(row: dict[str, Any]) -> None:
    """逐片执行记录落盘——上线后据此滚动校准参数(竞价占比/首片占比/参与率)。"""
    try:
        import csv as _csv
        mkdir_p(POV_EXEC_LOG.parent)
        new_file = not POV_EXEC_LOG.exists()
        with open(POV_EXEC_LOG, "a", newline="", encoding="utf-8-sig") as f:
            w = _csv.DictWriter(f, fieldnames=[
                "date", "time", "ts_code", "name", "slice_no", "last_price", "open_price",
                "budget", "order_amt", "order_qty", "filled_qty", "fill_price", "remain_amt", "note"],
                extrasaction="ignore")
            if new_file:
                w.writeheader()
            w.writerow(row)
        _track_execution(
            "record_buy_slice",
            event_id=row.get("event_id", ""),
            entry_date=row.get("date", ""), time=row.get("time", ""),
            ts_code=row.get("ts_code", ""), name=row.get("name", ""),
            strategy_leg=row.get("strategy_leg", ""), signal_date=row.get("signal_date", ""),
            channel="买入POV", slice_no=row.get("slice_no", 0),
            order_id=row.get("order_id", ""), budget=row.get("budget", 0),
            order_price=row.get("order_price", 0), order_qty=row.get("order_qty", 0),
            filled_qty=row.get("filled_qty", 0), fill_price=row.get("fill_price", 0),
            benchmark_open=row.get("open_price", 0), remaining_amount=row.get("remain_amt", 0),
            status="已成交" if int(row.get("filled_qty", 0) or 0) > 0 else "未成交",
            note=row.get("note", ""),
        )
    except Exception as e:
        logger().warning("POV执行日志写入失败（不影响交易）：%s", e)


def _pov_slice_row(it: dict[str, Any], **kw: Any) -> dict[str, Any]:
    base = dict(date=today_beijing().strftime("%Y%m%d"), time=now_beijing().strftime("%H:%M:%S"),
                ts_code=it.get("ts_code", ""), name=it.get("name", ""), slice_no=it.get("slice_no", 0),
                last_price="", open_price="", budget="", order_amt="", order_qty="",
                filled_qty="", fill_price="", remain_amt=round(float(it.get("remain_amt", 0)), 2), note="",
                strategy_leg=it.get("strategy_leg", ""), signal_date=it.get("signal_date", ""),
                order_id="", order_price="", event_id="")
    base.update(kw)
    return base


def _broker_position_quantity_and_cost(
    broker_positions: Any, ts_code: str
) -> tuple[int, float]:
    """读取券商同代码持仓数量与成本额；兼容快照对象和字典测试桩。"""
    aliases = _ts_code_aliases(ts_code)
    quantity = 0
    cost_amount = 0.0
    for position in broker_positions or []:
        p_code = (
            position.get("ts_code", "")
            if isinstance(position, dict)
            else getattr(position, "ts_code", "")
        )
        if not any(alias in aliases for alias in _ts_code_aliases(p_code)):
            continue
        volume = int(
            (position.get("volume", 0) if isinstance(position, dict)
             else getattr(position, "volume", 0)) or 0
        )
        cost_price = float(
            (position.get("cost_price", 0.0) if isinstance(position, dict)
             else getattr(position, "cost_price", 0.0)) or 0.0
        )
        if volume > 0:
            quantity += volume
            cost_amount += volume * max(cost_price, 0.0)
    return quantity, cost_amount


def _today_local_entry_cost(ts_code: str) -> float:
    """汇总本地账本中同一标的今天已确认的竞价/POV实际买入成本。"""
    aliases = _ts_code_aliases(ts_code)
    today_str = today_beijing().strftime("%Y%m%d")
    total = 0.0
    for position in load_positions():
        if str(position.get("status", "")).lower() not in {"open", "sell_pending"}:
            continue
        buy_date = "".join(ch for ch in str(position.get("buy_date", "")) if ch.isdigit())[:8]
        if buy_date != today_str:
            continue
        if not any(alias in aliases for alias in _ts_code_aliases(position.get("ts_code", ""))):
            continue
        shares = int(position.get("entry_shares", position.get("shares", 0)) or 0)
        buy_price = float(position.get("buy_price", 0.0) or 0.0)
        total += max(shares, 0) * max(buy_price, 0.0)
    return total


def _confirmed_entry_actual_amount(it: dict[str, Any], broker_positions: Any) -> float:
    """取本次开仓累计实际成交额；优先本地成交明细，券商持仓成本兜底。

    09:20已存在的人工同票持仓通过baseline成本扣除，不把人工仓误计为策略开仓。
    券商成本回报短暂为0时，使用已落盘竞价成本+本线程已确认POV成本兜底。
    """
    _qty, broker_total_cost = _broker_position_quantity_and_cost(
        broker_positions, str(it.get("ts_code", ""))
    )
    baseline_cost = max(float(it.get("broker_baseline_cost_amount", 0.0) or 0.0), 0.0)
    broker_incremental_cost = max(broker_total_cost - baseline_cost, 0.0)
    local_and_running_cost = (
        _today_local_entry_cost(str(it.get("ts_code", "")))
        + max(float(it.get("cost_amt", 0.0) or 0.0), 0.0)
    )
    # 本地竞价成交价和POV逐片成交价都是不含手续费的真实成交额，口径最精确；
    # QMT cost_price在部分券商版本会把佣金摊入成本，只有本地尚无已确认成交时
    # 才用券商增量成本兜底，避免几十元费用误差导致少补一手。
    return local_and_running_cost if local_and_running_cost > 0 else broker_incremental_cost


def _pov_execute_slice(
    it: dict[str, Any],
    broker_cfg: dict,
    part: float,
    first_share: float,
    chase: float,
    today_str: str,
    log: Any,
    cash_buffer: float = 1000.0,
    max_position_pct: float = ACE_FULL_HARD_CAP_PCT,
    lot_size: int = 100,
) -> None:
    """按累计实际成交额执行一片POV，并在下单前锁死85%最坏委托金额。"""
    from src.broker_adapter import OrderRequest

    ts_code = str(it["ts_code"])
    name_s = str(it.get("name", ""))
    slice_no = int(it.get("slice_no", 0))
    try:
        # 竞价原单尚未终态时禁止叠加POV。否则残单和新单可能同时成交，任何单路
        # 资金计算都无法保证二者合计不超过85%。
        if _has_pending_buy_for_code(ts_code):
            _pov_log_slice(_pov_slice_row(it, note="竞价残单未确认终态,本片禁止叠加"))
            return

        # 账户、持仓和行情在同一把QMT短锁中读取，形成该片一致的资金快照；任一
        # 查询失败即fail-closed，本片不下单，绝不用过期可用余额冒险生成废单。
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            account = adapter.query_account()
            broker_positions = adapter.query_positions()
            quote = adapter.get_full_tick([ts_code]).get(ts_code)
        if quote is None:
            _pov_log_slice(_pov_slice_row(it, note="无行情,本片跳过"))
            return
        it["slice_no"] = slice_no + 1

        target_amount = float(
            it.get("target_actual_amount", it.get("total_target_amt", it.get("target_amt", 0.0)))
            or 0.0
        )
        stored_hard_cap = float(it.get("hard_cap_amount", 0.0) or 0.0)
        current_total_asset = float(getattr(account, "total_asset", 0.0) or 0.0)
        current_85_cap = current_total_asset * max_position_pct if current_total_asset > 0 else 0.0
        # 85%必须“无论如何”都不突破：同时受09:20固定快照硬顶和当前账户
        # 总资产85%约束。账户盘中缩水时取更小值，不用旧快照放大风险。
        hard_cap_amount = min(
            cap for cap in (stored_hard_cap, current_85_cap) if cap > 0
        ) if (stored_hard_cap > 0 or current_85_cap > 0) else 0.0
        actual_amount = _confirmed_entry_actual_amount(it, broker_positions)
        it["confirmed_actual_amount"] = actual_amount
        it["remain_amt"] = max(target_amount - actual_amount, 0.0)

        if hard_cap_amount <= 0 or target_amount <= 0:
            it["done"] = True
            it["done_reason"] = "目标或85%硬顶不可得"
            _pov_log_slice(_pov_slice_row(it, note="目标或85%硬顶不可得,停止买入"))
            return
        if actual_amount > hard_cap_amount + 1.0:
            it["done"] = True
            it["done_reason"] = "累计实际成交额已超过85%硬顶"
            log.critical(
                "🚨 [POV] %s 累计实际成交额%.2f万已超过85%%硬顶%.2f万，停止全部补仓！",
                ts_code, actual_amount / 1e4, hard_cap_amount / 1e4,
            )
            if not it.get("hard_cap_breach_notified"):
                it["hard_cap_breach_notified"] = True
                _notify(
                    "buy_result", "🚨 开仓金额超过85%硬顶",
                    f"{ts_code} {name_s} QMT确认累计实际成交额{actual_amount / 1e4:.2f}万，"
                    f"超过当前85%硬顶{hard_cap_amount / 1e4:.2f}万。系统已停止全部补仓，请立即核对。",
                    level="critical", call=True,
                )
            return
        if actual_amount + 0.01 >= target_amount:
            it["done"] = True
            it["done_reason"] = "累计实际成交额已达到82.5%目标"
            _pov_log_slice(_pov_slice_row(
                it, note=f"累计实际成交额{actual_amount:.2f}元已达82.5%目标,不补仓"
            ))
            return

        last = float(getattr(quote, "last_price", 0.0) or 0.0)
        open_p = float(
            getattr(quote, "open_price", 0.0)
            or getattr(quote, "open", 0.0)
            or 0.0
        ) or last
        upper = float(getattr(quote, "upper_limit", 0.0) or 0.0)
        cum_amt = float(getattr(quote, "amount", 0.0) or 0.0)
        prev_cum = float(it.get("prev_cum_amount", 0.0) or 0.0)
        it["prev_cum_amount"] = cum_amt

        sig_amt = float(it.get("sig_amt", 0.0) or 0.0)
        if slice_no == 0:
            # 首片:盘中量差分尚不可用,用信号日成交额×实测首片占比;读不到就等第二片
            budget = sig_amt * first_share * part if sig_amt > 0 else 0.0
        else:
            budget = max(cum_amt - prev_cum, 0.0) * part
        if last <= 0 or budget <= 0:
            _pov_log_slice(_pov_slice_row(it, last_price=last, budget=round(budget, 0),
                                          note="无现价或预算为0,本片跳过"))
            return
        if upper > 0 and last >= upper - 1e-6:
            _pov_log_slice(_pov_slice_row(it, last_price=last, note="涨停,不追,本片跳过"))
            return
        cap_price = _round_stock_price(open_p * (1 + chase))
        if last > cap_price:
            _pov_log_slice(_pov_slice_row(it, last_price=last, open_price=open_p,
                                          note=f"追价保护:现价>开盘×{1 + chase:.2f},本片跳过"))
            return
        # 挂追价上限价：按这一个“最坏成交价”而非最新价折算股数，确保真实成交
        # 价格即使恰好等于委托上限，累计金额也不会越过82.5%目标或85%硬顶。
        price = min(cap_price, upper) if upper > 0 else cap_price
        available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
        qty, target_gap, hard_room, order_amount_cap = _calculate_pov_buy_quantity(
            target_amount=target_amount,
            hard_cap_amount=hard_cap_amount,
            confirmed_actual_amount=actual_amount,
            available_cash=available_cash,
            cash_buffer=cash_buffer,
            slice_budget=budget,
            order_price=price,
            lot_size=lot_size,
        )
        it["remain_amt"] = target_gap
        if qty < lot_size:
            one_lot_amount = price * lot_size
            if target_gap + 0.01 < one_lot_amount:
                it["done"] = True
                it["done_reason"] = "距82.5%目标不足一手"
                note = "目标缺口不足一手,在允许误差内结束"
            elif hard_room + 0.01 < one_lot_amount:
                it["done"] = True
                it["done_reason"] = "85%硬顶空间不足一手"
                note = "85%硬顶空间不足一手,停止补仓"
            elif max(available_cash - cash_buffer, 0.0) + 0.01 < one_lot_amount:
                it["done"] = True
                it["done_reason"] = "真实可用现金不足一手"
                note = "QMT真实可用现金不足一手,停止补仓"
            else:
                note = "本片流动性预算不足一手,等待下一片"
            _pov_log_slice(_pov_slice_row(
                it, last_price=last, open_price=open_p, budget=round(budget, 0), note=note
            ))
            return
        if actual_amount + qty * price > hard_cap_amount + 1e-6:
            # 理论上纯函数已经保证；保留下单前最后断言，未来改计算也不能穿线。
            log.critical("POV下单前85%%硬顶断言失败：%s", ts_code)
            _pov_log_slice(_pov_slice_row(it, note="85%硬顶断言失败,拒绝下单"))
            return
        if st_open_forbidden(ts_code, it.get("name", "")):
            if not it.get("st_blocked_notified"):
                it["st_blocked_notified"] = True
                log.error("⛔ [ST禁买] %s %s 命中ST/风险警示铁律，POV全部买入片拦截。", ts_code, it.get("name", ""))
                _notify("buy_result", "⛔ ST股禁买拦截",
                        f"{ts_code} {it.get('name','')} 为ST/风险警示股，全局铁律禁止开仓，POV买入片已全部拦截。",
                        level="timeSensitive")
            return
        request = OrderRequest(
            ts_code=ts_code, broker_code=str(it.get("broker_code", ts_code)),
            side="BUY", quantity=qty, price_type="FIXED_PRICE", price=price,
            strategy_name=str(it.get("strategy_name", "A_SYSTEM_ABC")),
            remark=f"POV实际金额校准-{today_str}",
            strategy_leg=str(it.get("strategy_leg", "")).upper(),
            business_date=today_str,
            signal_date=str(it.get("signal_date", "")),
            planned_exit_date=str(it.get("planned_exit_date", "")),
            purpose="OPEN",
            source_key=f"BUY_POV|{today_str}|{ts_code}|{slice_no + 1}",
            metadata={
                "execution_channel": "买入POV",
                "name": name_s,
                "strategy_branch_id": str(it.get("strategy_branch_id", "")),
                "condition_profile_ids": str(it.get("condition_profile_ids", "")),
                "exit_rule": str(it.get("exit_rule", "")),
            },
        )
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            result = adapter.place_order(request)
        if not result.accepted:
            log.error("❌ [POV] %s 片%d提交失败：%s", ts_code, slice_no + 1, result.message)
            _pov_log_slice(_pov_slice_row(it, last_price=last, order_qty=qty, note=f"提交失败:{result.message}"))
            return
        oid = str(result.order_id or "")
        fill = _confirm_fill(
            broker_cfg,
            oid,
            qty,
            f"POV片{slice_no + 1}",
            timeout_sec=90,
            poll_sec=5,
        )
        if fill.filled_qty < qty and not fill.is_terminal:
            _try_cancel_order(broker_cfg, oid, ts_code)
            # 撤单成败以订单终态为准(2026-07-10口径),再确认一次拿最终成交数
            fill = _confirm_fill(
                broker_cfg,
                oid,
                qty,
                f"POV片{slice_no + 1}终态",
                timeout_sec=20,
                poll_sec=3,
            )
        fq = int(fill.filled_qty or 0)
        fp = float(fill.avg_price) if float(getattr(fill, "avg_price", 0) or 0) > 0 else last
        confirmed_after = actual_amount
        if fq > 0:
            it["filled_qty"] = int(it.get("filled_qty", 0)) + fq
            it["cost_amt"] = float(it.get("cost_amt", 0.0)) + fq * fp
            confirmed_after = actual_amount + fq * fp
            it["confirmed_actual_amount"] = confirmed_after
            it["remain_amt"] = max(target_amount - confirmed_after, 0.0)
            it.setdefault("order_ids", []).append(oid)
        log.info(
            "[POV] %s %s 片%d：累计实际%.2f万/目标%.2f万，预算%.1f万，"
            "挂%d股@%.2f（最坏%.2f万）成交%d股@%.2f，剩余%.2f万",
            ts_code, name_s, slice_no + 1, actual_amount / 1e4, target_amount / 1e4,
            budget / 1e4, qty, price, qty * price / 1e4, fq, fp,
            float(it["remain_amt"]) / 1e4,
        )
        _pov_log_slice(_pov_slice_row(
            it, event_id=f"买入POV|{today_str}|{ts_code}|{slice_no + 1}|{oid}",
            order_id=oid, order_price=price,
            last_price=last, open_price=open_p, budget=round(order_amount_cap, 0),
            order_amt=round(qty * price, 0), order_qty=qty,
            filled_qty=fq, fill_price=round(fp, 3),
            note=f"成交后累计实际{confirmed_after:.2f}元" if fq else "未成撤单",
        ))
        if confirmed_after + 0.01 >= target_amount:
            it["done"] = True
            it["done_reason"] = "累计实际成交额已达到82.5%目标"
        elif target_amount - confirmed_after < price * lot_size:
            it["done"] = True
            it["done_reason"] = "距82.5%目标不足一手"
    except Exception as e:
        log.error("POV片执行异常（%s）：%s", ts_code, e)
        _pov_log_slice(_pov_slice_row(it, note=f"异常:{e}"))


def _pov_finalize_item(it: dict[str, Any], log: Any, reason: str) -> None:
    """单标的金额校准完结：落地POV成交，并按80%~85%区间验收。"""
    it["done"] = True
    it["finalized"] = True
    it["done_reason"] = reason
    today_str = today_beijing().strftime("%Y%m%d")
    fq = int(it.get("filled_qty", 0))
    ts_code = str(it["ts_code"])
    name_s = str(it.get("name", ""))
    target_amount = float(
        it.get("target_actual_amount", it.get("total_target_amt", it.get("target_amt", 0.0)))
        or 0.0
    )
    min_acceptable_amount = float(it.get("min_acceptable_amount", 0.0) or 0.0)
    hard_cap_amount = float(it.get("hard_cap_amount", 0.0) or 0.0)
    equity_snapshot = float(it.get("equity_snapshot", 0.0) or 0.0)
    actual_total = max(
        float(it.get("confirmed_actual_amount", 0.0) or 0.0),
        _today_local_entry_cost(ts_code) + float(it.get("cost_amt", 0.0) or 0.0),
    )
    it["confirmed_actual_amount"] = actual_total
    actual_pct = actual_total / equity_snapshot if equity_snapshot > 0 else 0.0
    if fq > 0:
        avg = float(it["cost_amt"]) / fq
        _record_live_buy(
            recovery_context="买入POV",
            order_id=f"pov-{today_str}-{ts_code}",
            ts_code=ts_code, name=name_s,
            signal_date=str(it.get("signal_date", "")),
            buy_date=today_str, shares=fq, buy_price=avg,
            strategy_leg=str(it.get("strategy_leg", "")),
            exit_n_days=int(it.get("exit_n", 2)),
            execution_channel="买入POV",
            planned_order_qty=int(it.get("pov_planned_qty", fq) or fq),
            planned_exit_date_override=str(it.get("planned_exit_date", "") or ""),
            component_order_ids=[str(oid) for oid in it.get("order_ids", []) if str(oid)],
            strategy_branch_id=str(it.get("strategy_branch_id", "")),
            condition_profile_ids=str(it.get("condition_profile_ids", "")),
            exit_rule=str(it.get("exit_rule", "")),
        )
        _notify("buy_result", "✅ POV平滑段买入完成",
                f"策略={it.get('strategy_leg', '')} {ts_code} {name_s} 平滑段成交{fq}股 "
                f"成本{avg:.2f} 市值{_fmt_wan(fq * avg)}；累计实际开仓"
                f"{actual_total / 1e4:.2f}万（占09:20总资产{actual_pct:.2%}，目标82.5%，"
                f"85%硬顶{hard_cap_amount / 1e4:.2f}万；{reason}）。")
        log.warning(
            "✅ [POV] %s %s 金额校准完结（%s）：POV成交%d股@%.2f，"
            "累计实际%.2f万/目标%.2f万（%.2f%%）",
            ts_code, name_s, reason, fq, avg, actual_total / 1e4,
            target_amount / 1e4, actual_pct * 100,
        )
        _track_execution(
            "update_entry_status",
            entry_date=today_str, ts_code=ts_code,
            strategy_leg=str(it.get("strategy_leg", "")),
            signal_date=str(it.get("signal_date", "")),
            status="买入POV已结束",
        )
    else:
        _notify("buy_result", "ℹ️ 开仓金额校准结束",
                f"{ts_code} {name_s} POV未新增成交（{reason}）；集合竞价等渠道累计实际开仓"
                f"{actual_total / 1e4:.2f}万，占09:20总资产{actual_pct:.2%}。")
        log.warning("[POV] %s %s 金额校准完结（%s）：POV零成交，累计实际%.2f万（%.2f%%）",
                    ts_code, name_s, reason, actual_total / 1e4, actual_pct * 100)
        _track_execution(
            "update_entry_status",
            entry_date=today_str, ts_code=ts_code,
            strategy_leg=str(it.get("strategy_leg", "")),
            signal_date=str(it.get("signal_date", "")),
            status="买入POV零成交",
        )

    # 80%不是强行追价线：涨停、超过开盘2%、流动性不足时仍须停止。但收口低于
    # 80%必须明确告警，不能把明显少仓静默当作完成；超过85%则升级为最高风险。
    if hard_cap_amount > 0 and actual_total > hard_cap_amount + 1.0:
        _notify(
            "buy_result", "🚨 开仓金额超过85%硬顶",
            f"{ts_code} {name_s} 最终累计实际开仓{actual_total / 1e4:.2f}万"
            f"（{actual_pct:.2%}），超过85%硬顶{hard_cap_amount / 1e4:.2f}万。"
            "系统已停止补仓，请立即核对QMT持仓与当日成交。",
            level="critical", call=True,
        )
    elif min_acceptable_amount > 0 and actual_total + 1.0 < min_acceptable_amount:
        _notify(
            "buy_result", "⚠️ 实际开仓低于80%验收线",
            f"{ts_code} {name_s} 10:30收口累计实际开仓{actual_total / 1e4:.2f}万"
            f"（占09:20总资产{actual_pct:.2%}），低于80%下限"
            f"{min_acceptable_amount / 1e4:.2f}万。原因：{reason}；系统不会突破+2%追价保护强补。",
            level="timeSensitive",
        )


def _finalize_expired_pov_for_startup() -> int:
    """交易对账及持仓投影成功后，对超时的当天POV按真实成交收口。

    原工作线程异常退出后，10:30之后重启不会再恢复买入线程。这里仅调用
    原收口及少仓告警路径，不查询券商，不补买，不撤单，不改持仓退出规则。
    """
    now = now_beijing()
    if now.time() < datetime.time(10, 30):
        return 0
    state = _pov_load_state()
    if state.get("date") != now.strftime("%Y%m%d"):
        return 0
    pending = [it for it in state.get("items", []) if not it.get("finalized")]
    for it in pending:
        _pov_finalize_item(it, logger(), "故障恢复已超过10:30，按已确认成交收口，放弃剩余买入")
    if pending:
        _pov_save_state(state)
        logger().warning("[POV恢复收口] 已结算%d项，不补买、不撤单。", len(pending))
    return len(pending)


def _pov_worker() -> None:
    """POV平滑段执行线程:09:30:10起每5分钟一片,10:30收口。

    礼让式QMT访问(短锁),与止盈线程同款;只做BUY,与卖出链路零交叠
    (平滑段10:30前结束,14:40撤买单/14:55平仓在其之后)。
    """
    log = logger()
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        broker_cfg = config.get("broker", {})
        lt = config.get("live_trade", {})
        # 实盘下单闸门:worker可能由断点恢复独立启动,必须自带校验(09:20路径的
        # gateway.assert_real_order_allowed 不覆盖这里)。
        try:
            from src.live_order_gateway import LiveOrderGateway
            LiveOrderGateway(PROJECT_ROOT / "config" / "config.json").assert_real_order_allowed(
                str(lt.get("real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED")),
                side="BUY",
            )
        except RuntimeError as e:
            log.error("❌ [POV] 实盘下单条件不满足,平滑段执行线程退出：%s", e)
            return
        # POV线程支持断点恢复，不能假设一定经过当天09:20的计划门禁；恢复后先用
        # 券商真实持仓做一次串行单仓校验，防止旧状态文件在旧仓未清时继续买入。
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            positions_live = adapter.query_positions()
        if _broker_has_preexisting_strategy_position(positions_live):
            log.warning("🚫 [POV] 券商仍有本系统旧策略仓，取消剩余买入POV，线程退出。")
            return
        part = float(lt.get("pov_participation", 0.10))
        first_share = float(lt.get("pov_first_slice_share", 0.18))
        chase = float(lt.get("pov_chase_cap", 0.02))
        cash_buffer = float(lt.get("cash_buffer_amount", 1000.0))
        max_position_pct = min(float(lt.get("max_position_pct", ACE_FULL_HARD_CAP_PCT)), ACE_FULL_HARD_CAP_PCT)
        lot_size = int(lt.get("round_lot_size", 100))
        today_str = today_beijing().strftime("%Y%m%d")
        end_dt = now_beijing().replace(hour=10, minute=30, second=0, microsecond=0)
        base_dt = now_beijing().replace(hour=9, minute=30, second=10, microsecond=0)
        log.info("POV平滑执行线程启动：每5分钟一片,参与率%.0f%%,首片基数%.0f%%,追价保护+%.0f%%,10:30收口。",
                 part * 100, first_share * 100, chase * 100)
        while True:
            now = now_beijing()
            state = _pov_load_state()
            todo = [it for it in state.get("items", []) if not it.get("done")]
            if not todo:
                log.info("POV平滑段全部完成,线程退出。")
                return
            if now >= end_dt:
                for it in todo:
                    _pov_finalize_item(it, log, "10:30收口,放弃剩余")
                _pov_save_state(state)
                return
            if now < base_dt:
                time.sleep(min((base_dt - now).total_seconds(), 60))
                continue
            # 当前处于某片时刻(或恢复启动晚到):立即执行一片
            for it in todo:
                _pov_execute_slice(
                    it, broker_cfg, part, first_share, chase, today_str, log,
                    cash_buffer=cash_buffer,
                    max_position_pct=max_position_pct,
                    lot_size=lot_size,
                )
                if it.get("done") and not it.get("finalized"):
                    _pov_finalize_item(it, log, str(it.get("done_reason", "完成")))
            _pov_save_state(state)
            # 对齐下一个5分钟网格
            now2 = now_beijing()
            k = int((now2 - base_dt).total_seconds() // 300) + 1
            next_dt = min(base_dt + datetime.timedelta(seconds=k * 300), end_dt)
            sleep_s = (next_dt - now2).total_seconds()
            if sleep_s > 0:
                time.sleep(sleep_s)
    except Exception as e:
        log.error("POV平滑执行线程异常退出：%s", e)
        _notify("system_error", "❌ POV平滑执行线程异常",
                f"平滑段买入线程异常:{e}。剩余买入已停止,今日仓位可能不满,请回终端核对。",
                level="critical", call=True)


def _new_buy_execution_gate() -> tuple[bool, str]:
    """只读预检新增BUY总门，供播报和09:30降级路径共用。

    非实盘/未接券商场景继续保留模拟计划；真正启用QMT实盘时必须经过与最终
    下单完全相同的LiveOrderGateway门禁。该函数不连接QMT、不查账户、不下单。
    """

    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        if not (
            str(config.get("trade_mode", "")).lower() == "live"
            and bool(config.get("broker_adapter_enabled"))
            and bool(config.get("qmt_enabled"))
        ):
            return True, ""
        from src.live_order_gateway import LiveOrderGateway

        confirm = str(
            config.get("live_trade", {}).get(
                "real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED"
            )
        )
        LiveOrderGateway(
            PROJECT_ROOT / "config" / "config.json"
        ).assert_real_order_allowed(confirm, side="BUY")
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _notify_new_buy_gate_block(stage: str, reason: str) -> None:
    message = (
        f"{stage}发现正式候选，但新增BUY安全门禁未通过：{reason}。"
        "系统按fail-closed保持空仓，不会转POV、不会切换其他策略腿，也不建议手动追买；"
        "请先完成并冻结当前组合的严格样本外/逐折发布认证。"
    )
    logger().error("⛔ %s", message)
    _notify(
        "buy_result",
        "⛔ 候选存在但新增BUY被安全门禁阻断",
        message,
        level="critical",
    )


def _premarket_buy_price(
    quote: Any,
    ts_code: str = "",
    name: str = "",
    signal_date: str = "",
) -> tuple[float, str]:
    ask_prices = getattr(quote, "ask_prices", None) if quote else None
    upper_limit = float(getattr(quote, "upper_limit", 0.0) or 0.0) if quote else 0.0
    if upper_limit > 0:
        return _round_stock_price(upper_limit), "涨停价"
    estimated_limit = _estimate_limit_up_price(quote, ts_code, name)
    if estimated_limit > 0:
        return estimated_limit, "估算涨停价（QMT未返回涨停价）"
    local_close, source = _find_local_prev_close(ts_code, signal_date)
    if local_close > 0:
        local_limit = _round_stock_price(local_close * (1 + _limit_up_pct_for_stock(ts_code, name)))
        return local_limit, f"本地收盘价估算涨停价（{source}）"

    # 最后兜底：如果完全拿不到涨停价/昨收价依据，但QMT仍有盘口或最新价，
    # 按“当前可见价格 × 1.099”挂单，尽量贴近10cm涨停，避免再次挂成卖一低价。
    fallback_base = 0.0
    fallback_source = ""
    if quote and getattr(quote, "last_price", 0) > 0:
        fallback_base = float(quote.last_price)
        fallback_source = "最新价"
    elif ask_prices and len(ask_prices) >= 1 and ask_prices[0] > 0:
        fallback_base = float(ask_prices[0])
        fallback_source = "卖1"
    elif ask_prices and len(ask_prices) >= 5 and ask_prices[4] > 0:
        fallback_base = float(ask_prices[4])
        fallback_source = "卖5"
    if fallback_base > 0:
        return _round_stock_price(fallback_base * 1.099), f"{fallback_source}×1.099兜底价（涨停价/昨收价不可用）"

    return 0.0, "无可用价格"


def _record_premarket_buy_fill(s: dict[str, Any], fill: Any, fallback_price: float) -> None:
    today_str = today_beijing().strftime("%Y%m%d")
    fill_price = fill.avg_price if getattr(fill, "avg_price", 0) > 0 else fallback_price
    _record_live_buy(
        recovery_context="盘前买入监控",
        order_id=str(s.get("order_id", "")),
        ts_code=str(s.get("ts_code", "")),
        name=str(s.get("name", "")),
        signal_date=str(s.get("signal_date", "")),
        buy_date=today_str,
        shares=int(fill.filled_qty),
        buy_price=float(fill_price),
        strategy_leg=str(s.get("strategy_leg", "")),
        exit_n_days=int(s.get("exit_n", 2)),
        traded_at=getattr(fill, "traded_at", ""),
        execution_channel="集合竞价买入",
        planned_order_qty=int(s.get("qty", 0) or 0),
        benchmark_open=0.0,
        planned_exit_date_override=str(s.get("planned_exit_date", "") or ""),
        strategy_branch_id=str(s.get("strategy_branch_id", "")),
        condition_profile_ids=str(s.get("condition_profile_ids", "")),
        exit_rule=str(s.get("exit_rule", "")),
    )
    amount = int(fill.filled_qty) * float(fill_price)
    logger().info("✅ [盘前买入监控] 持仓信息：策略=%s %s %s 持仓%d股 成本%.2f 市值%s",
                  s.get("strategy_leg", ""), s.get("ts_code", ""), s.get("name", ""),
                  int(fill.filled_qty), fill_price, _fmt_wan(amount))
    _notify("buy_result", "✅ 盘前持仓信息",
            f"策略={s.get('strategy_leg', '')} {s.get('ts_code', '')} {s.get('name', '')} "
            f"持仓{int(fill.filled_qty)}股 成本{fill_price:.2f} 市值{_fmt_wan(amount)}")


def _resubmit_premarket_buy(s: dict[str, Any], broker_cfg: dict, config: dict) -> dict[str, Any] | None:
    """9:20-9:30内发现预挂单被撤/废单后，重新按涨停价/卖档价补挂。"""
    from src.broker_adapter import OrderRequest

    ts_code = str(s.get("ts_code", ""))
    name_s = str(s.get("name", ""))
    planned_qty = int(s.get("qty", 0))
    live_cfg = config.get("live_trade", {})
    retry_count = int(s.get("retry_count", 0))
    max_retry = int(live_cfg.get("premarket_resubmit_max_count", 3))
    if retry_count >= max_retry:
        logger().warning("盘前买入监控：%s %s 已补挂%d次仍失败，停止集合竞价补挂，等待09:30流程。",
                         ts_code, name_s, retry_count)
        return None
    if not ts_code or planned_qty <= 0:
        return None

    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        account = adapter.query_account()
        positions_live = adapter.query_positions()
        quote = adapter.get_full_tick([ts_code]).get(ts_code)

    if _broker_has_preexisting_strategy_position(positions_live):
        logger().warning("🚫 [盘前买入补挂] 券商仍有本系统旧策略仓，取消本次补挂。")
        return None

    price, price_label = _premarket_buy_price(quote, ts_code, name_s, str(s.get("signal_date", "")))
    if price <= 0:
        logger().warning("盘前买入监控：%s %s 无法获取补挂价格，等待下一轮。", ts_code, name_s)
        return None

    available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
    total_asset = float(getattr(account, "total_asset", 0.0) or available_cash)
    cash_buffer = float(live_cfg.get("cash_buffer_amount", 1000))
    max_single = _effective_single_order_cap(live_cfg)
    max_position_pct = min(float(live_cfg.get("max_position_pct", ACE_FULL_HARD_CAP_PCT)), ACE_FULL_HARD_CAP_PCT)
    stored_hard_cap = float(s.get("entry_hard_cap_amount", 0.0) or 0.0)
    hard_cap = min(
        cap for cap in (stored_hard_cap, total_asset * max_position_pct) if cap > 0
    ) if (stored_hard_cap > 0 or total_asset > 0) else 0.0
    usable = min(
        available_cash - cash_buffer,
        hard_cap,
        max_single,
    )
    max_qty_by_cash = _floor_buy_quantity_by_amount(usable, price, 100)
    qty = max(0, min(planned_qty, max_qty_by_cash))
    if qty <= 0:
        logger().warning("盘前买入监控：%s 可用资金%.0f元不足以补挂（价格%.2f）。", ts_code, available_cash, price)
        return None

    today_str = today_beijing().strftime("%Y%m%d")
    logger().warning("⏳ [盘前买入补挂] %s %s  %d股  %s=%.2f元", ts_code, name_s, qty, price_label, price)
    request = OrderRequest(
        ts_code=ts_code,
        broker_code=ts_code,
        side="BUY",
        quantity=qty,
        price_type="FIXED_PRICE",
        price=price,
        strategy_name="A_SYSTEM_ABC",
        remark=f"盘前买入补挂-{price_label}-{today_str}",
        strategy_leg=str(s.get("strategy_leg", "")).upper(),
        business_date=today_str,
        signal_date=str(s.get("signal_date", "")),
        planned_exit_date=str(s.get("planned_exit_date", "")),
        purpose="OPEN",
        source_key=(
            f"PREMARKET_RETRY|{today_str}|{s.get('strategy_leg', '')}|"
            f"{ts_code}|{retry_count + 1}"
        ),
        metadata={"execution_channel": "盘前买入补挂", "name": name_s},
    )
    if st_open_forbidden(ts_code, s.get("name", "")):
        logger().error("⛔ [ST禁买] %s %s 命中ST/风险警示铁律，拦截盘前买入补挂。", ts_code, s.get("name", ""))
        _notify("buy_result", "⛔ ST股禁买拦截",
                f"{ts_code} {s.get('name','')} 为ST/风险警示股，全局铁律禁止开仓，已拦截补挂买入。",
                level="timeSensitive")
        return None
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        result = adapter.place_order(request)
    if not result.accepted:
        logger().error("❌ [盘前买入补挂] %s %s 提交失败：%s", ts_code, name_s, result.message)
        return None

    new_order = dict(s)
    new_order.update({
        "order_id": str(result.order_id or f"premarket-retry-{today_str}-{ts_code}"),
        "qty": qty,
        "ref_price": price,
        "retry_count": retry_count + 1,
        "last_retry_at": now_beijing().strftime("%H:%M:%S"),
    })
    logger().info("✅ [盘前买入补挂] %s %s %d股 @%.2f 委托已受理（继续等待09:30确认）",
                  ts_code, name_s, qty, price)
    return new_order


def _premarket_buy_monitor_loop() -> None:
    """监控09:20预挂买单；若券商撤单/废单，按涨停价补挂，直到撤单窗口前。"""
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    broker_cfg = config.get("broker", {})
    cutoff = datetime.time(14, 55)
    poll_seconds = 15
    logger().info("盘前买入监控已启动：09:20-14:56 每%d秒检查一次成交/撤单/废单。", poll_seconds)
    while now_beijing().time() < cutoff:
        pending = load_pending_buys()
        if not pending:
            return
        for s in pending:
            order_id = str(s.get("order_id", ""))
            if not order_id:
                continue
            try:
                with _qmt_lock:
                    adapter = _qmt_get(broker_cfg)
                    fill = adapter.get_order_fill(order_id)
                if int(getattr(fill, "filled_qty", 0) or 0) > 0 and (
                    getattr(fill, "is_terminal", False) or getattr(fill, "is_filled", False)
                ):
                    _record_premarket_buy_fill(s, fill, float(s.get("ref_price", 0.0) or 0.0))
                    _replace_pending_buy_order(order_id, None)
                    continue
                if getattr(fill, "is_terminal", False):
                    if now_beijing().time() >= datetime.time(9, 30):
                        logger().warning(
                            "[盘前买入监控] %s %s 原委托%s终态未成交，移交实际金额POV。",
                            s.get("ts_code", ""), s.get("name", ""), order_id,
                        )
                        _replace_pending_buy_order(order_id, None)
                        continue
                    logger().warning(
                        "⚠️ [盘前买入监控] %s %s 原委托%s已终态未成交（状态=%s），准备补挂。",
                        s.get("ts_code", ""), s.get("name", ""), order_id, getattr(fill, "status_text", ""),
                    )
                    new_order = _resubmit_premarket_buy(s, broker_cfg, config)
                    if new_order is not None:
                        _replace_pending_buy_order(order_id, new_order)
            except Exception as e:
                logger().warning("盘前买入监控异常 order_id=%s：%s", order_id, e)
        time.sleep(poll_seconds)
    logger().info("盘前买入监控结束：已到14:56撤单窗口。")


def _start_premarket_buy_monitor() -> None:
    global _premarket_buy_monitor_thread
    if _premarket_buy_monitor_thread is not None and _premarket_buy_monitor_thread.is_alive():
        return
    if now_beijing().time() >= datetime.time(14, 55):
        return
    _premarket_buy_monitor_thread = threading.Thread(
        target=_premarket_buy_monitor_loop,
        daemon=True,
        name="premarket-buy-monitor",
    )
    _premarket_buy_monitor_thread.start()


def confirm_pending_premarket_buys(confirm_source: str = "09:30") -> None:
    """确认09:20盘前买单成交。

    09:30后必须撤销仍在排队的竞价残单并确认终态，再把真实成交额交给POV。
    若残单终态仍无法确认就继续保留pending，POV会fail-closed跳过，避免两路
    买单叠加后突破85%硬顶。
    """
    pending = load_pending_buys()
    if not pending:
        return

    logger().info("===== 确认盘前买单成交（%s）共%d笔 =====", confirm_source, len(pending))
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    broker_cfg = config.get("broker", {})
    today_str = today_beijing().strftime("%Y%m%d")
    handoff_to_pov = (
        confirm_source != "09:26" or now_beijing().time() >= datetime.time(9, 30)
    )

    for s in pending:
        try:
            order_id = str(s.get("order_id", ""))
            ts_code = str(s.get("ts_code", ""))
            qty = int(s.get("qty", 0))
            ref_price = float(s.get("ref_price", 0.0))
            fill = _confirm_fill(
                broker_cfg,
                order_id,
                qty,
                f"盘前买入确认-{confirm_source}",
                timeout_sec=8,
                poll_sec=2,
            )
            if fill.filled_qty < qty and not fill.is_terminal:
                if not handoff_to_pov:
                    logger().info(
                        "[盘前买入确认] %s 09:26成交%d/%d股，原单仍有效；保留至09:30再撤残单并移交POV。",
                        ts_code, fill.filled_qty, qty,
                    )
                    continue
                logger().warning(
                    "[盘前买入确认] %s %s成交%d/%d股但原单未终态，先撤残单再移交POV。",
                    ts_code, confirm_source, fill.filled_qty, qty,
                )
                _try_cancel_order(broker_cfg, order_id, ts_code)
                fill = _confirm_fill(
                    broker_cfg,
                    order_id,
                    qty,
                    f"盘前买入撤单终态确认-{confirm_source}",
                    timeout_sec=20,
                    poll_sec=3,
                )
                if not fill.is_terminal:
                    logger().error(
                        "❌ [盘前买入确认] %s 残单终态仍不明确，保留pending并禁止POV叠加。",
                        ts_code,
                    )
                    _notify(
                        "buy_result", "⚠️ 竞价残单终态未确认",
                        f"{ts_code} {s.get('name','')} 竞价残单撤单后仍未确认终态。"
                        "系统已暂停该票POV补仓，避免累计委托突破85%，请核对QMT委托。",
                        level="timeSensitive",
                    )
                    continue
            fill_price = fill.avg_price if fill.avg_price > 0 else ref_price
            if fill.filled_qty > 0:
                _record_live_buy(
                    recovery_context=f"盘前买入确认-{confirm_source}",
                    order_id=order_id,
                    ts_code=ts_code,
                    name=str(s.get("name", "")),
                    signal_date=str(s.get("signal_date", "")),
                    buy_date=today_str,
                    shares=fill.filled_qty,
                    buy_price=fill_price,
                    strategy_leg=str(s.get("strategy_leg", "")),
                    exit_n_days=int(s.get("exit_n", 2)),
                    traded_at=getattr(fill, "traded_at", ""),
                    execution_channel="集合竞价买入",
                    planned_order_qty=qty,
                    benchmark_open=0.0,
                    planned_exit_date_override=str(
                        s.get("planned_exit_date", "") or ""
                    ),
                    strategy_branch_id=str(s.get("strategy_branch_id", "")),
                    condition_profile_ids=str(
                        s.get("condition_profile_ids", "")
                    ),
                    exit_rule=str(s.get("exit_rule", "")),
                )
                name_s = str(s.get("name", ""))
                amount = fill.filled_qty * fill_price
                if fill.filled_qty < qty:
                    planned_price = float(s.get("ref_price", fill_price) or fill_price)
                    planned_amount = qty * planned_price
                    unfilled_amount = max(planned_amount - amount, 0.0)
                    logger().warning("⚠️ [盘前买入确认] %s 部分成交 %d/%d股 @%.2f，残单已确认终态。",
                                     ts_code, fill.filled_qty, qty, fill_price)
                    _notify("buy_result", "⚠️ 盘前开仓部分成交",
                            f"策略={s.get('strategy_leg', '')} {ts_code} {name_s} 成交{fill.filled_qty}/{qty}股 "
                            f"@{fill_price:.2f}。总委托金额{_fmt_wan(planned_amount)}，"
                            f"已成交金额{_fmt_wan(amount)}，未成交金额{_fmt_wan(unfilled_amount)}")
                else:
                    strategy_leg_s = str(s.get("strategy_leg", ""))
                    logger().info("✅ [盘前买入确认] 持仓信息：策略=%s %s %s 持仓%d股 成本%.2f 市值%s",
                                  strategy_leg_s, ts_code, name_s, fill.filled_qty,
                                  fill_price, _fmt_wan(amount))
                    _notify("buy_result", "✅ 盘前持仓信息",
                            f"策略={strategy_leg_s} {ts_code} {name_s} "
                            f"持仓{fill.filled_qty}股 成本{fill_price:.2f} 市值{_fmt_wan(amount)}")
            else:
                if not fill.is_terminal:
                    logger().warning(
                        "⚠️ [盘前买入确认] %s %s暂未成交（状态=%s），撤单终态不明确，禁止POV并继续监控。",
                        ts_code, confirm_source, fill.status_text,
                    )
                else:
                    logger().warning(
                        "[盘前买入确认] %s %s未成交且已终态（状态=%s），清除pending并移交实际金额POV。",
                        ts_code, confirm_source, fill.status_text,
                    )
            if fill.is_terminal:
                _replace_pending_buy_order(order_id, None)
        except Exception as e:
            logger().error("❌ [盘前买入确认] %s 异常：%s —— 请手动核对！", s.get("ts_code"), e)
            _notify("buy_result", "❌ 盘前开仓确认异常",
                    f"{s.get('ts_code','')} 盘前买单成交确认出现异常，请回终端核对持仓。",
                    level="critical", call=True)

    if load_pending_buys():
        _start_premarket_buy_monitor()
    logger().info("===== 盘前买单成交确认完成 =====")


def handle_combined_order_preview(
    planned_orders_path: Path | None,
    reason: str,
    *,
    allowed_sides: set[str] | None = None,
    allow_t2_close_sell_now: bool = False,
) -> bool:
    # 组合计划单预览 —— 次优先级，出错只记录
    try:
        import pandas as pd
        if planned_orders_path is None or not planned_orders_path.exists():
            logger().info("无组合 planned_orders，跳过：%s", reason)
            return False

        try:
            orders = pd.read_csv(planned_orders_path)
        except pd.errors.EmptyDataError:
            logger().info("组合 planned_orders 文件为空，跳过：%s", reason)
            return False
        if "side" not in orders.columns:
            logger().info("组合 planned_orders 无 side 列，跳过：%s", reason)
            return False

        executable_orders = orders[orders["side"].astype(str).str.upper().isin({"BUY", "SELL"})]
        if executable_orders.empty:
            logger().info("组合计划单无买卖计划，跳过：%s", reason)
            return False

        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))
        today_str = today_beijing().strftime("%Y%m%d")

        logger().info("发现组合计划 %d 条：%s", len(executable_orders), reason)

        if qmt_enabled:
            confirm = config.get("live_trade", {}).get(
                "real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED")
            buy_rows = executable_orders[executable_orders["side"].astype(str).str.upper() == "BUY"]
            for _, row in buy_rows.iterrows():
                code = str(row.get("ts_code", ""))
                name_s = str(row.get("name", ""))
                qty = int(row.get("round_lot_shares", 0))
                ref_price = float(row.get("reference_price", 0.0))
                amount = qty * ref_price
                logger().warning("⏳ [准备开仓] %s %s  %d股  参考价%.2f元  预估金额%.0f元",
                                 code, name_s, qty, ref_price, amount)
            execution_tag = "平仓" if allowed_sides == {"SELL"} else "开仓"
            return _execute_orders_inprocess(
                planned_orders_path,
                confirm,
                execution_tag,
                allowed_sides=allowed_sides,
                allow_t2_close_sell_now=allow_t2_close_sell_now,
            )
        else:
            recorded_any = False
            buy_orders = executable_orders[executable_orders["side"].astype(str).str.upper() == "BUY"]
            for _, row in buy_orders.iterrows():
                try:
                    raw_exit_n = row.get("exit_n_days", None)
                    exit_n = int(float(raw_exit_n)) if raw_exit_n is not None and str(raw_exit_n) not in {"", "nan"} else 2
                    record_buy(
                        order_id=str(row.get("paper_order_id",
                                             f"paper-{today_str}-{row.get('ts_code', '')}")),
                        ts_code=str(row.get("ts_code", "")),
                        name=str(row.get("name", "")),
                        signal_date=str(row.get("signal_date", "")),
                        buy_date=today_str,
                        shares=int(row.get("round_lot_shares", 0)),
                        buy_price=float(row.get("reference_price", 0.0)),
                        strategy_leg=str(row.get("strategy_leg", "")),
                        exit_n_days=exit_n,
                        planned_exit_date_override=str(
                            row.get("planned_exit_date", "")
                        ).split(".")[0],
                        strategy_branch_id=str(
                            row.get("matched_strategy_branch_ids", "")
                        ),
                        condition_profile_ids=str(
                            row.get("matched_condition_profile_ids", "")
                        ),
                        exit_rule=str(row.get("exit_rule", "")),
                    )
                    recorded_any = True
                except Exception as e:
                    logger().error("记录持仓异常：%s", e)
            return recorded_any

    except Exception as e:
        logger().error("买入信号处理异常：%s", e)
        return False


_e_retry_thread: threading.Thread | None = None
_e_retry_resolved_date = ""
_e_retry_resolution_reason = ""


def _e_retry_running() -> bool:
    global _e_retry_thread
    return _e_retry_thread is not None and _e_retry_thread.is_alive()


def _mark_e_retry_resolved(reason: str) -> None:
    """记录E候选已明确零成交并结束补仓机会；当日D仍保持关闭。"""

    global _e_retry_resolved_date, _e_retry_resolution_reason
    _e_retry_resolved_date = today_beijing().strftime("%Y%m%d")
    _e_retry_resolution_reason = str(reason or "E延迟开仓已结束且零成交")
    logger().warning(
        "E候选延迟执行已结束：%s；原候选不追买，但按全日互斥规则D今日仍保持关闭。",
        _e_retry_resolution_reason,
    )


def _e_retry_resolved_today() -> tuple[bool, str]:
    today = today_beijing().strftime("%Y%m%d")
    return (
        _e_retry_resolved_date == today,
        _e_retry_resolution_reason if _e_retry_resolved_date == today else "",
    )


def _get_e_buy_ts_codes(decisions) -> list[str]:
    """从组合决策中提取 ALLOW_E_BUY 行对应的标的代码。"""
    if decisions is None or decisions.empty or "action" not in decisions.columns:
        return []
    rows = decisions[decisions["action"].astype(str) == "ALLOW_E_BUY"]
    codes = rows["ts_code"].dropna().astype(str).str.strip().tolist()
    return [c for c in codes if c and c.lower() != "nan"]


def _e_open_price_ok(ts_code: str, tolerance: float = 0.02) -> bool:
    """当前价相比今日开盘价的涨幅是否在 tolerance 以内。"""
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        broker_cfg = config.get("broker", {})
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            quote_map = adapter.get_full_tick([ts_code])
        quote = quote_map.get(ts_code)
        if quote is None:
            logger().warning("E延迟开仓：无法获取 %s 行情数据，跳过价格检查", ts_code)
            return False
        open_price = float(getattr(quote, "open_price", 0.0) or 0.0)
        last_price = float(getattr(quote, "last_price", 0.0) or 0.0)
        if open_price <= 0:
            logger().warning("E延迟开仓：%s 开盘价异常（%.2f），跳过", ts_code, open_price)
            return False
        pct = (last_price - open_price) / open_price
        ok = pct <= tolerance
        logger().info(
            "E延迟开仓价格检查：%s 开盘%.2f 当前%.2f 涨幅%+.2f%% —— %s",
            ts_code, open_price, last_price, pct * 100,
            f"允许（≤{tolerance * 100:.0f}%%）" if ok else f"拒绝（>{tolerance * 100:.0f}%%）",
        )
        return ok
    except Exception as e:
        logger().error("E延迟开仓价格检查异常：%s", e)
        return False


def _e_place_order_direct(ts_code: str, name: str, planned_qty: int, signal_date: str,
                            strategy_leg: str, exit_n_days: int,
                            config: dict, broker_cfg: dict, confirm: str) -> bool:
    """按ask5/ask1/last_price挂FIXED_PRICE限价买单，不走CSV→validate流水线。

    返回 True 仅表示已真实成交并记账；False 表示本次未成交/未提交。
    终态废单会在调用方通过 _last_e_terminal_reject 判断后停止重试。
    """
    from src.broker_adapter import OrderRequest
    from src.live_order_gateway import LiveOrderGateway

    today_str = today_beijing().strftime("%Y%m%d")
    log = logger()
    _e_place_order_direct.last_terminal_reject = False  # type: ignore[attr-defined]

    gateway = LiveOrderGateway(PROJECT_ROOT / "config" / "config.json")
    try:
        gateway.assert_real_order_allowed(confirm, side="BUY")
    except RuntimeError as e:
        log.error("❌ [E延迟开仓] 下单条件不满足：%s", e)
        return False

    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        account = adapter.query_account()
        positions_live = adapter.query_positions()
        quote_map = adapter.get_full_tick([ts_code])

    if _broker_has_preexisting_strategy_position(positions_live):
        log.warning("🚫 [E延迟开仓] 券商仍有本系统旧策略仓，取消本次延迟买入。")
        return False

    quote = quote_map.get(ts_code)
    ask_prices = getattr(quote, "ask_prices", None) if quote else None

    if ask_prices and len(ask_prices) >= 5 and ask_prices[4] > 0:
        price = round(float(ask_prices[4]), 2)
        price_label = "卖5"
    elif ask_prices and len(ask_prices) >= 1 and ask_prices[0] > 0:
        price = round(float(ask_prices[0]), 2)
        price_label = "卖1（卖5不可用）"
    elif quote and getattr(quote, "last_price", 0) > 0:
        price = round(float(quote.last_price), 2)
        price_label = "最新价（五档不可用）"
    else:
        log.warning("E延迟开仓：%s 无法获取价格，跳过本次。", ts_code)
        return False

    # 按账户可用资金计算可买股数，上限为计划股数
    available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
    total_asset = float(getattr(account, "total_asset", 0.0) or available_cash)
    live_cfg = config.get("live_trade", {})
    cash_buffer = float(live_cfg.get("cash_buffer_amount", 1000))
    max_single = _effective_single_order_cap(live_cfg)
    max_position_pct = min(float(live_cfg.get("max_position_pct", ACE_FULL_HARD_CAP_PCT)), ACE_FULL_HARD_CAP_PCT)
    max_total_position_pct = min(
        float(live_cfg.get("max_total_position_pct", ACE_FULL_TARGET_PCT)), ACE_FULL_TARGET_PCT
    )
    # 2026-11-12 ACE+M：E延迟开仓同样受方案甲停手期20%目标与M已占资金约束。
    ace_cap = ace_limits(
        _ace_gate_mode_for(today_str), total_asset,
        _strategy_only_market_value(positions_live, family=M_LEG),
    )
    ace_value = _strategy_only_market_value(positions_live, family="ACE")
    usable = min(
        available_cash - cash_buffer,
        total_asset * min(max_position_pct, ace_cap["hard_pct"]) - ace_value,
        total_asset * min(max_total_position_pct, ace_cap["target_pct"]) - ace_value,
        max_single,
    )
    max_qty_by_cash = int(usable / price) if usable > 0 and price > 0 else 0
    max_qty_by_cash = max_qty_by_cash - (max_qty_by_cash % 100)
    qty = max(0, min(planned_qty, max_qty_by_cash))

    if qty <= 0:
        log.warning("E延迟开仓：%s 可用资金%.0f元不足以购买（价格%.2f），跳过。",
                    ts_code, available_cash, price)
        return False

    log.warning("⏳ [E延迟开仓] %s %s  %d股  %s=%.2f元  可用%.0f元",
                ts_code, name, qty, price_label, price, available_cash)

    request = OrderRequest(
        ts_code=ts_code,
        broker_code=ts_code,
        side="BUY",
        quantity=qty,
        price_type="FIXED_PRICE",
        price=price,
        strategy_name="A_SYSTEM_ABC",
        remark=f"E延迟开仓-{price_label}-{today_str}",
        strategy_leg=str(strategy_leg or "E").upper(),
        business_date=today_str,
        signal_date=signal_date,
        purpose="OPEN",
        source_key=f"E_DELAYED|{today_str}|{ts_code}",
        metadata={"execution_channel": "E延迟开仓", "name": name},
    )
    if st_open_forbidden(ts_code, name):
        log.error("⛔ [ST禁买] %s %s 命中ST/风险警示铁律，拦截E延迟开仓委托。", ts_code, name)
        _notify("buy_result", "⛔ ST股禁买拦截",
                f"{ts_code} {name} 为ST/风险警示股，全局铁律禁止开仓，已拦截E延迟开仓。",
                level="timeSensitive")
        return None
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        result = adapter.place_order(request)

    if not result.accepted:
        log.error("❌ [E延迟开仓] %s %s 提交失败：%s", ts_code, name, result.message)
        return False

    log.info("✅ [E延迟开仓] %s %s %d股 @%.2f 委托已受理（待成交确认）", ts_code, name, qty, price)
    order_id_broker = str(result.order_id or f"eretry-{today_str}-{ts_code}")
    fill = _confirm_fill(broker_cfg, order_id_broker, qty, "E延迟开仓")
    fill_price = fill.avg_price if fill.avg_price > 0 else price
    if fill.filled_qty > 0:
        _record_live_buy(
            recovery_context="E延迟开仓",
            order_id=order_id_broker,
            ts_code=ts_code,
            name=name,
            signal_date=signal_date,
            buy_date=today_str,
            shares=fill.filled_qty,
            buy_price=fill_price,
            strategy_leg=strategy_leg,
            exit_n_days=exit_n_days,
            traded_at=getattr(fill, "traded_at", ""),
        )
        amount = fill.filled_qty * fill_price
        if fill.filled_qty < qty:
            planned_amount = qty * price
            unfilled_amount = max(planned_amount - amount, 0.0)
            log.warning("⚠️ [E延迟开仓] %s 部分成交 %d/%d股 @%.2f，按实际成交记录持仓。",
                        ts_code, fill.filled_qty, qty, fill_price)
            _notify("buy_result", "⚠️ E开仓部分成交",
                    f"策略={strategy_leg} {ts_code} {name} 成交{fill.filled_qty}/{qty}股 "
                    f"@{fill_price:.2f}。总委托金额{_fmt_wan(planned_amount)}，"
                    f"已成交金额{_fmt_wan(amount)}，未成交金额{_fmt_wan(unfilled_amount)}")
        else:
            log.info("✅ [E延迟开仓] 持仓信息：策略=%s %s %s 持仓%d股 成本%.2f 市值%s",
                     strategy_leg, ts_code, name, fill.filled_qty, fill_price, _fmt_wan(amount))
            _notify("buy_result", "✅ E持仓信息",
                    f"策略={strategy_leg} {ts_code} {name} "
                    f"持仓{fill.filled_qty}股 成本{fill_price:.2f} 市值{_fmt_wan(amount)}")
        return True
    else:
        detail = _format_order_fill_detail(fill)
        log.error("❌ [E延迟开仓] %s %s 未成交（%s），不记录持仓，避免幽灵持仓。",
                  ts_code, name, detail)
        if fill.is_terminal:
            _e_place_order_direct.last_terminal_reject = True  # type: ignore[attr-defined]
        return False


def _e_delayed_buy_loop(combined_orders_path, decisions) -> None:
    """后台线程：9:31-13:30 内每60秒检查价格条件，满足则用ask5/last_price挂FIXED_PRICE单。

    价格条件：当前价（或最新价）≤ 今日开盘价 × 1.02。
    午间11:30-13:00不提交委托，避免券商返回废单；13:00后继续检查。
    超出条件或超过截止时间时放弃；E候选当日仍按全日互斥规则关闭D。
    """
    log = logger()
    ts_codes = _get_e_buy_ts_codes(decisions)
    if not ts_codes:
        log.warning("E延迟开仓：无法获取标的代码，停止E重试；D今日保持关闭。")
        _mark_e_retry_resolved("E候选缺少可执行标的代码")
        return

    # 从decisions中提取买入元信息（stock details）
    import pandas as pd
    rows = decisions[decisions["action"].astype(str) == "ALLOW_E_BUY"] if not decisions.empty else pd.DataFrame()

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    broker_cfg = config.get("broker", {})
    confirm = config.get("live_trade", {}).get("real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED")
    qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))

    log.info("E延迟开仓线程启动：标的 %s，每60秒检查一次，截止13:30。", ts_codes)
    RETRY_INTERVAL_S = 60
    CUTOFF = datetime.time(13, 30)
    TOLERANCE = 0.02

    while True:
        time.sleep(RETRY_INTERVAL_S)
        now = now_beijing()

        if has_position_bought_today("ACE"):
            log.info("E延迟开仓：今日买入已成交，退出重试线程。")
            return

        if now.time() >= CUTOFF:
            log.warning("E延迟开仓：已过13:30截止时间，放弃开仓；D今日保持关闭。")
            _mark_e_retry_resolved("E已过13:30延迟开仓截止且零成交")
            return

        if not market_is_open():
            log.info("E延迟开仓：当前不在连续竞价时段，等待下一轮检查，不提交委托。")
            continue

        if not all(_e_open_price_ok(code, TOLERANCE) for code in ts_codes):
            log.warning("E延迟开仓：%s 涨幅已超开盘价2%%，放弃开仓；D今日保持关闭。", ts_codes)
            _mark_e_retry_resolved("E标的已超过开盘价2%追价上限，补仓机会结束且零成交")
            return

        log.info("E延迟开仓 %s：价格满足条件，尝试提交...", now.strftime("%H:%M:%S"))

        if not qmt_enabled:
            log.info("[E延迟开仓] 模拟盘，跳过实盘下单。")
            _mark_e_retry_resolved("E处于模拟/未启用QMT模式，未形成实盘成交")
            return

        placed_any = False
        terminal_reject_any = False
        for _, row in rows.iterrows():
            code = str(row.get("ts_code", ""))
            if code not in ts_codes:
                continue
            ok = _e_place_order_direct(
                ts_code=code,
                name=str(row.get("name", "")),
                planned_qty=int(row.get("quantity", 0)),
                signal_date=str(row.get("signal_date", today_beijing().strftime("%Y%m%d"))),
                strategy_leg=str(row.get("strategy_leg", "E")),
                exit_n_days=1,
                config=config,
                broker_cfg=broker_cfg,
                confirm=confirm,
            )
            if ok:
                placed_any = True
            if bool(getattr(_e_place_order_direct, "last_terminal_reject", False)):
                terminal_reject_any = True

        if placed_any:
            log.info("E延迟开仓：提交成功，退出重试线程。")
            return
        if terminal_reject_any:
            log.warning(
                "E延迟开仓：券商已返回终态废单/终态未成交，停止重复提交同一计划；D今日保持关闭。"
            )
            _mark_e_retry_resolved("E券商委托已终态废单/终态未成交")
            return
        log.warning("E延迟开仓：本次提交未成功，%d秒后重试。", RETRY_INTERVAL_S)


def _start_e_retry_thread(combined_orders_path, decisions) -> None:
    global _e_retry_thread
    if _e_retry_running():
        logger().info("E延迟开仓线程已在运行，跳过重复启动。")
        return
    resolved, reason = _e_retry_resolved_today()
    if resolved:
        logger().info("E延迟开仓今日已明确结束，跳过重复启动：%s", reason)
        return
    _e_retry_thread = threading.Thread(
        target=_e_delayed_buy_loop,
        args=(combined_orders_path, decisions),
        daemon=True,
        name="e-retry",
    )
    _e_retry_thread.start()
    logger().info("E延迟开仓线程已启动（后台daemon线程，每60秒检查，最晚13:30放弃）。")
