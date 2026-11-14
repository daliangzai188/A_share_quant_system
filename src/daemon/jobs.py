"""定时任务（跨模块编排）：08:50推送、09:00计划、09:20预挂、09:26同步、09:30开盘、14:40撤买单、
盘中重启补启动D。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import threading
from typing import Any

from src.utils.config import load_json_config
from src.utils.time_utils import now_beijing, today_beijing
from src.portfolio_ace_m import M_LEG
from src.strategy_d_factor_rules import release_uses_factor_union
from src.strategy_d_spec import intraday_history_is_complete
from src.daemon.common import (
    PROJECT_ROOT,
    _floor_buy_quantity_by_amount,
    _fmt_wan,
    _notify,
    _track_execution,
    is_trade_day,
    logger,
    to_float,
)
from src.daemon.state import (
    _append_pending_buys,
    _d_relay_pair_active_today,
    _d_relay_pair_load_state,
    _d_relay_pair_save_state,
    _pov_active_today,
    load_pending_buys,
)
from src.daemon.plans import (
    _combined_open_action_for_current_mode,
    _d_blocking_buy_actions,
    _select_unique_buy_order_for_action,
    _signal_day_amount,
    d_intraday_monitor_gate,
    has_combined_action,
    has_open_local_position,
    has_position_bought_today,
    load_combined_decisions,
    read_cached_combined_decisions,
    st_open_forbidden,
)
from src.daemon.qmt import (
    _broker_has_preexisting_strategy_position,
    _qmt_get,
    _qmt_lock,
    _qmt_query_account_positions,
    _strategy_only_market_value,
    reconcile_missing_local_positions,
)
from src.daemon.buy import (
    _broker_position_quantity_and_cost,
    _e_auction_buy_worker,
    _e_retry_running,
    _enqueue_opening_pov_from_plan,
    _new_buy_execution_gate,
    _notify_new_buy_gate_block,
    _pov_auction_seed_quantity,
    _pov_enqueue,
    _premarket_buy_price,
    _start_e_retry_thread,
    _start_pov_worker,
    _start_premarket_buy_monitor,
    confirm_pending_premarket_buys,
    resize_buy_orders_for_live_account,
)
from src.daemon.close import check_and_close_positions
from src.daemon.strategy_d import (
    _d_relay_block,
    _d_relay_reconcile_pending_sell,
    _start_d_relay_pair_worker,
    _strategy_d_checkpoint_recovery_check,
    _strategy_d_monitor_running,
    _strategy_d_release_signal_clock,
    job_strategy_d,
)
from src.daemon.reports import (
    OPEN_PLAN_DEFAULT_EXECUTION_CUTOFF,
    _retry_morning_plan_notification_if_needed,
    push_open_plan_notification,
    report_next_day_candidates,
)


def job_premarket_position_sync() -> None:
    """09:26 集合竞价成交确认：确认09:20买单 + 同步09:23平仓成交。

    9:25集合竞价撮合完成后，券商委托/持仓会更新：
    1. 先确认09:20预挂买单，成交则立刻记录本地持仓；未成交但仍排队则不撤单。
    2. D接力卖单按QMT真实成交股数/均价回写部分持仓与可买资金，不用“代码消失”猜测；
    3. 其他本地持仓统一按“连续3次完整快照缺少对应code”同步；接力候选由09:30专用成对POV接管。
    """
    logger().info("===== 盘前持仓同步（09:26）=====")

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))
    if not qmt_enabled:
        logger().info("[盘前持仓同步] 模拟盘，跳过实盘查询。")
        logger().info("===== 盘前持仓同步完成 =====")
        return

    try:
        confirm_pending_premarket_buys(confirm_source="09:26")
    except Exception as e:
        logger().error("09:26 盘前买单成交确认异常：%s —— 请手动核对！", e)

    broker_cfg = config.get("broker", {})
    try:
        relay_state = _d_relay_pair_load_state()
        if relay_state and str(relay_state.get("status", "")).upper() in {
            "PREPARED", "AUCTION_SUBMITTED", "ACTIVE", "BUY_CREDIT_PENDING"
        }:
            if not _d_relay_reconcile_pending_sell(
                relay_state, broker_cfg, cancel_nonterminal=True
            ):
                logger().error("09:26 D接力竞价卖单未安全收口，后续成对POV已停止。")
            elif str(relay_state.get("status", "")).upper() != "BLOCKED":
                relay_state["status"] = "ACTIVE"
                _d_relay_pair_save_state(relay_state)
                logger().info(
                    "✅ [D接力09:26] 竞价确认完成：D已卖%d股、释放%s，剩余%d股；"
                    "09:30后按确认资金成对POV。",
                    int(relay_state.get("confirmed_sell_qty", 0) or 0),
                    _fmt_wan(float(relay_state.get("confirmed_sell_amount", 0.0) or 0.0)),
                    int(relay_state.get("d", {}).get("remaining_shares", 0) or 0),
                )
    except Exception as exc:
        logger().exception("09:26 D接力竞价确认异常：%s", exc)
        try:
            relay_state = _d_relay_pair_load_state()
            if relay_state:
                _d_relay_block(relay_state, f"09:26竞价确认异常:{exc}")
        except Exception:
            pass  # 可以忽略：上面已记录完整异常；D接力2026-08-07起已关闭，标记阻断失败不影响后续流程

    try:
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            _, live_positions = _qmt_query_account_positions(adapter)
    except Exception as e:
        logger().error("[盘前持仓同步] 查询实盘持仓失败：%s", e)
        logger().info("===== 盘前持仓同步完成 =====")
        return

    synced_any = False
    cleared = reconcile_missing_local_positions(
        live_positions,
        "盘前持仓同步09:26",
        snapshot_complete=True,
    )
    synced_any = synced_any or cleared > 0

    if not synced_any:
        logger().info("[盘前持仓同步] 无需同步（本地与实盘持仓一致）。")
        logger().info("===== 盘前持仓同步完成 =====")
        return

    # 持仓已更新，重新运行组合状态机刷新今日决策
    logger().info("[盘前持仓同步] 持仓已更新，重新生成今日组合决策...")
    try:
        from src.combined_live_engine import CombinedLiveEngine
        engine = CombinedLiveEngine(PROJECT_ROOT / "config" / "config.json")
        engine.write_plan()
        logger().info("✅ [盘前持仓同步] 组合决策已刷新，后续09:30补充买入任务将使用最新决策。")
    except Exception as e:
        logger().error("[盘前持仓同步] 刷新组合决策失败：%s，09:30补充买入将沿用旧决策。", e)

    logger().info("===== 盘前持仓同步完成 =====")


def job_preopen_plan() -> None:
    """09:00 提前生成组合状态机计划。

    A/C/E 的盘前开仓信息都来自上个交易日收盘后已有数据，D策略除外。
    因此开仓计划不需要等到09:20才计算；09:00先生成，09:20复核后可以直接挂单。
    """
    logger().info("===== 盘前计划生成（09:00）=====")
    combined = load_combined_decisions()
    if combined is None:
        # 2026-07-09 事故链第一环：09:00生成失败（共享盘IO慢180秒超时）后
        # 干等到09:20才现场重算，可能耗时数分钟并错过集合竞价。
        # 失败后立即重试一次，把恢复动作留在09:00~09:06的富余时段。
        logger().warning("09:00 组合状态机生成失败，立即重试一次（避免拖累09:20预挂窗口）。")
        combined = load_combined_decisions()
    if combined is None:
        logger().error("09:00 组合状态机决策生成两次失败，09:20将现场重算（可能影响集合竞价排队）。")
        return
    logger().info("09:00 组合状态机计划已生成，09:20复核后如有开仓计划将直接按涨停价预挂。")
    # 早盘开仓计划推送已前移至 08:50 独立任务(job_morning_plan_push)：
    # 正常情况09:00不重复推；仅当08:50通知没有成功落标时，利用距09:20仍有
    # 20分钟的窗口补发，避免“交易能下单、用户却没收到早盘计划”的静默故障。
    try:
        _retry_morning_plan_notification_if_needed("09:00计划生成完成")
    except Exception as exc:
        logger().warning("09:00早盘计划通知兜底失败（不改变下单）：%s", exc)
    logger().info("===== 盘前计划生成完成 =====")


def job_morning_plan_push() -> None:
    """08:50 早盘推送今日开仓计划(2026-07-24 用户要求从09:00前移)。

    推送读的是昨晚收盘流水线已生成的信号缓存(planned_orders)，与 09:00 的
    计划生成无依赖——纯轻活，提前到 08:50 让 09:00 专心干正事。
    有计划推详细(策略/股票/股数/金额)，无计划明示"今日无新开仓计划"。
    """
    logger().info("===== 早盘开仓计划推送（08:50）=====")
    try:
        report_next_day_candidates()
        push_open_plan_notification("早盘")
    except Exception as e:
        logger().warning("早盘开仓计划推送异常(不影响交易)：%s", e)


def job_premarket_buy() -> None:
    """09:20集合竞价预挂当前组合计划；有旧仓时不提交新买单。"""
    logger().info("===== 集合竞价买入预挂（09:20）=====")

    # 平仓最高优先：逾期持仓（如昨日平仓失败残留）先于一切买入动作清理。
    # 2026-07-09 事故：共享盘IO慢曾导致盘前任务拖延、逾期仓无人处理。
    # 平仓检查冗余挂载到09:20/09:23/09:30，
    # 任一任务存活即可完成清理；无逾期仓时本调用毫秒级返回。
    try:
        check_and_close_positions()
    except Exception as e:
        logger().error("09:20 平仓检查异常：%s", e)

    # 09:25集合竞价撮合后不能再把“09:20涨停价预挂”伪装成竞价委托。
    # 调度器现在会补跑被前一任务拖延的节点，因此这里必须用真实市场时段硬挡：
    # 错过竞价就留给09:30确认/POV链路，绝不在09:25-09:30静默段盲发买单。
    if now_beijing().time() >= datetime.time(9, 25):
        logger().error(
            "⛔ 09:20集合竞价买入任务执行时已到%s，竞价预挂窗口已关闭；"
            "本轮禁止提交，交09:30开盘确认/补买链路处理。",
            now_beijing().strftime("%H:%M:%S"),
        )
        _notify(
            "buy_result",
            "⚠️ 集合竞价预挂窗口已错过",
            "09:20买入任务执行延迟到09:25之后，系统已禁止伪竞价下单；"
            "09:30会按真实持仓、在途委托和组合计划继续确认或补买。",
            level="timeSensitive",
        )
        return

    if has_position_bought_today("ACE"):
        logger().info("09:20 今日已有买入成交，跳过集合竞价买入预挂。")
        return

    combined = read_cached_combined_decisions()
    if combined is None:
        logger().warning("09:20 未找到今日组合状态机缓存，临时生成一次；若耗时较长可能影响集合竞价排队。")
        combined = load_combined_decisions()
    decisions          = combined[0] if combined is not None else None
    combined_orders_path = combined[1] if combined is not None else None
    if decisions is None:
        logger().error("09:20 组合状态机决策获取失败，跳过集合竞价买入预挂。")
        return

    open_action = _combined_open_action_for_current_mode(decisions)
    if not open_action:
        logger().info("09:20 今日无开仓计划，跳过集合竞价买入预挂。")
        return

    if has_combined_action(decisions, "PLAN_SELL_D_FIRST"):
        logger().info("09:20 组合状态机要求先卖D，跳过集合竞价买入预挂。")
        return

    config     = load_json_config(PROJECT_ROOT / "config" / "config.json")
    qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))
    if not qmt_enabled:
        logger().info("[盘前买入] 模拟盘，跳过实盘挂单。")
        return

    import pandas as pd
    if combined_orders_path is None or not combined_orders_path.exists():
        logger().error("09:20 找不到计划单文件，跳过集合竞价买入预挂。")
        return
    try:
        orders = pd.read_csv(combined_orders_path)
    except Exception as e:
        logger().error("09:20 读取计划单失败：%s", e)
        return

    buy_orders = _select_unique_buy_order_for_action(
        orders,
        open_action,
        context="09:20集合竞价预挂",
    )
    if buy_orders.empty:
        logger().info("09:20 没有与正式动作匹配的唯一买入行，跳过。")
        return

    broker_cfg = config.get("broker", {})
    confirm    = config.get("live_trade", {}).get("real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED")
    today_str  = today_beijing().strftime("%Y%m%d")

    from src.broker_adapter import OrderRequest
    from src.live_order_gateway import LiveOrderGateway
    gateway = LiveOrderGateway(PROJECT_ROOT / "config" / "config.json")
    try:
        gateway.assert_real_order_allowed(confirm, side="BUY")
    except RuntimeError as e:
        logger().error("❌ [盘前买入] 下单条件不满足：%s", e)
        _notify_new_buy_gate_block("09:20集合竞价预挂", str(e))
        return

    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        account = adapter.query_account()
        positions_live = adapter.query_positions()

    # 实盘执行层必须以券商真实持仓为准。旧策略仓（含今日到期/待卖）尚未清空时，
    # 即使组合计划文件因缓存或旧版本仍带BUY，也要在任何委托提交前直接拦截。
    # 当日集合竞价/POV已成交的同一笔新仓不属于“旧仓”，不会误伤后续拆单。
    if _broker_has_preexisting_strategy_position(positions_live):
        logger().warning("🚫 [盘前买入] 券商仍有本系统旧策略仓，取消衔接开仓；本轮全部买单跳过。")
        _notify(
            "buy_result",
            "🚫 旧仓未清空，今日不开新仓",
            "券商仍实际持有本系统旧策略仓（含今日到期或待卖仓）；按82.5%串行单仓新口径，本轮买单全部取消。",
            level="timeSensitive",
        )
        return

    ts_codes = buy_orders["ts_code"].dropna().astype(str).tolist()
    with _qmt_lock:
        adapter   = _qmt_get(broker_cfg)
        quote_map = adapter.get_full_tick(ts_codes)

    # 按账户资金缩放订单：只计策略持仓市值，打新中签/人工持仓不挤占策略额度；
    # 但所有未清空的策略仓都计入，不再排除今日到期仓。
    buy_orders = resize_buy_orders_for_live_account(
        planned_orders=buy_orders,
        account=account,
        quote_map=quote_map,
        current_market_value=_strategy_only_market_value(
            positions_live,
            exclude_due_today=False,
            family="ACE",
        ),
        transition_full_cash=False,
        m_market_value=_strategy_only_market_value(positions_live, family=M_LEG),
    )

    lt_cfg = config.get("live_trade", {})
    pov_enabled = bool(lt_cfg.get("pov_enabled", True))
    actual_amount_rebalance_enabled = bool(
        lt_cfg.get("entry_actual_amount_rebalance_enabled", True)
    )
    pov_items: list[dict[str, Any]] = []

    pending_buys: list[dict[str, Any]] = []
    e_rows: list[Any] = []
    for _, row in buy_orders.iterrows():
        try:
            # E 专属通道：不在09:20挂单，推迟到09:24读实时竞价撮合量后
            # 按 min(缩单金额, 竞价额×参与率) 动态定仓再挂涨停价参与竞价。
            # 原因：E虽已改为信号日换手率降序，但R1候选仍可能出现竞价流动性薄的
            # 标的；历史上曾出现竞价额仅几十万元。继续按实时竞价额限参与率，
            # 不能因为最终排序变化就假定大额固定仓位可无冲击成交。
            # E标的从不一字开盘，9:24挂单无排队损失；A/C/M从09:20开始排队。
            if str(row.get("strategy_leg", "")).upper() == "E":
                if not bool(config.get("live_trade", {}).get("e_enabled", True)):
                    logger().warning("E已被 live_trade.e_enabled=false 关闭，跳过E买入计划：%s", row.get("ts_code"))
                    continue
                e_rows.append(row.copy())
                continue
            ts_code  = str(row["ts_code"])
            name_s   = str(row.get("name", ""))
            qty      = int(row.get("round_lot_shares", 0))
            if qty <= 0:
                continue

            signal_date_s = str(row.get("signal_date", ""))

            # 取价必须在 register_entry_plan 之前，台账登记的才是真正挂出去的股数。
            # 82.5%是最终实际成交额目标，不是09:20参考价×股数；集合竞价种子单
            # 必须按涨停委托价核85%最坏成交硬顶，实际开盘价确定后的金额缺口
            # 交给09:30~10:30原有POV按真实成交额动态补足。
            quote = quote_map.get(ts_code)
            price, price_label = _premarket_buy_price(quote, ts_code, name_s, signal_date_s)
            if price <= 0:
                logger().warning("09:20 %s %s 无法获取涨停价/估算涨停价/卖档价格，跳过。", ts_code, name_s)
                continue
            target_actual_amount = to_float(row.get("live_target_amount", 0.0))
            min_acceptable_amount = to_float(row.get("live_min_acceptable_amount", 0.0))
            hard_cap_amount = to_float(row.get("live_hard_cap_amount", 0.0))
            equity_snapshot = to_float(row.get("live_equity_snapshot", 0.0))
            available_cash_snapshot = to_float(row.get("live_available_cash_snapshot", 0.0))
            qty_by_hard_cap = _floor_buy_quantity_by_amount(hard_cap_amount, price, 100)
            if qty_by_hard_cap < qty:
                logger().warning(
                    "🔻 [盘前买入] %s %s 按涨停委托价%.2f元核85%%硬顶：%d股→%d股"
                    "（最坏委托金额不超过%.2f万；不再按09:20参考价倒推最终仓位）。",
                    ts_code, name_s, price, qty, qty_by_hard_cap, hard_cap_amount / 1e4,
                )
                qty = qty_by_hard_cap
            if qty <= 0:
                logger().error(
                    "❌ [盘前买入] %s %s 82.5%%目标不可执行或按委托价%.2f元不足一手"
                    "（QMT可用现金%.2f万，85%%硬顶%.2f万），放弃本笔。",
                    ts_code, name_s, price, available_cash_snapshot / 1e4, hard_cap_amount / 1e4,
                )
                _notify("buy_result", "⚠️ 资金不足放弃开仓",
                        f"{ts_code} {name_s} 按委托价{price:.2f}元折算不足一手"
                        f"（可用现金{available_cash_snapshot / 1e4:.2f}万，85%硬顶"
                        f"{hard_cap_amount / 1e4:.2f}万），今日放弃开仓。",
                        level="timeSensitive")
                continue

            total_target_qty = qty
            reference_price = float(row.get("reference_price", 0.0) or 0.0)
            total_target_amount = target_actual_amount
            pov_planned_qty = 0
            pov_target_amount = 0.0
            sig_amt_pov = _signal_day_amount(ts_code, signal_date_s.strip().split(".")[0])

            # POV竞价段拆分仍保持原流动性规则：竞价单只买信号日成交额×0.1%
            # 左右（竞价池的10%），超出部分转09:30后平滑段。即使盘前估算
            # 不需要拆分，也会在开盘后复核实际成交金额；达到82.5%时零下单结束。
            if pov_enabled:
                ref_p = float(row.get("reference_price", 0.0) or 0.0)
                qty_auction, auction_cap, pov_auction_share = _pov_auction_seed_quantity(
                    total_target_qty=qty,
                    reference_price=ref_p,
                    signal_day_amount=sig_amt_pov,
                    live_cfg=lt_cfg,
                    lot_size=int(lt_cfg.get("round_lot_size", 100)),
                )
                if qty_auction < qty:
                    smooth_amt = max(total_target_amount - qty_auction * ref_p, 0.0)
                    if ref_p <= 0 or sig_amt_pov <= 0:
                        logger().error(
                            "⛔ [POV] %s %s 无法可靠计算集合竞价容量"
                            "（参考价%.3f、信号日成交额%.0f），竞价挂0股；"
                            "82.5%%完整目标%.1f万交给09:30后POV按实时成交量执行。",
                            ts_code, name_s, ref_p, sig_amt_pov,
                            total_target_amount / 1e4,
                        )
                    else:
                        logger().warning(
                            "✂️ [POV] %s %s 竞价段限%.1f万(信号日成交%.0f万×%.2f%%),"
                            "竞价挂%d股；09:30后按实际成交额重算目标缺口（当前预估%.1f万）。",
                            ts_code, name_s, auction_cap / 1e4, sig_amt_pov / 1e4,
                            pov_auction_share * 100, qty_auction, smooth_amt / 1e4,
                        )
                    qty = qty_auction
                    pov_planned_qty = max(total_target_qty - qty_auction, 0)
                    pov_target_amount = smooth_amt

            if st_open_forbidden(ts_code, name_s):
                logger().error("⛔ [ST禁买] %s %s 命中ST/风险警示铁律，拦截09:20盘前买入委托。", ts_code, name_s)
                _notify("buy_result", "⛔ ST股禁买拦截",
                        f"{ts_code} {name_s} 为ST/风险警示股，全局铁律禁止开仓，已拦截盘前买入。",
                        level="timeSensitive")
                continue

            # 无论竞价段是否按流动性拆分，只要启用实际金额校准就建立一个POV状态。
            # 09:30先读取竞价真实成交额；若已达82.5%立即结束，低于目标才使用
            # 原09:30~10:30分片框架补足。这样不新增独立补单系统，也不改变选股。
            if pov_enabled and actual_amount_rebalance_enabled:
                raw_exit_pov = row.get("exit_n_days", None)
                exit_n_pov = int(float(raw_exit_pov)) if raw_exit_pov is not None and str(raw_exit_pov) not in {"", "nan"} else 2
                baseline_qty, baseline_cost = _broker_position_quantity_and_cost(
                    positions_live, ts_code
                )
                pov_items.append({
                    "ts_code": ts_code, "name": name_s,
                    "strategy_leg": str(row.get("strategy_leg", "")),
                    "signal_date": signal_date_s,
                    "broker_code": str(row.get("broker_code", ts_code)),
                    "strategy_name": str(row.get("strategy_name", "A_SYSTEM_ABC")),
                    "exit_n": exit_n_pov, "sig_amt": sig_amt_pov,
                    "planned_exit_date": str(row.get("planned_exit_date", "")).split(".")[0],
                    "strategy_branch_id": str(row.get("matched_strategy_branch_ids", "")),
                    "condition_profile_ids": str(row.get("matched_condition_profile_ids", "")),
                    "exit_rule": str(row.get("exit_rule", "")),
                    "target_amt": total_target_amount, "remain_amt": total_target_amount,
                    "target_actual_amount": total_target_amount,
                    "min_acceptable_amount": min_acceptable_amount,
                    "hard_cap_amount": hard_cap_amount,
                    "equity_snapshot": equity_snapshot,
                    "available_cash_snapshot": available_cash_snapshot,
                    "broker_baseline_qty": baseline_qty,
                    "broker_baseline_cost_amount": baseline_cost,
                    "total_target_qty": total_target_qty,
                    "total_target_amt": total_target_amount,
                    "reference_price": reference_price,
                    "auction_planned_qty": qty,
                    "pov_planned_qty": pov_planned_qty,
                    "filled_qty": 0, "cost_amt": 0.0, "slice_no": 0,
                    "prev_cum_amount": 0.0, "done": False,
                })
                # 即使竞价段原本足以容纳全部参考股数，也必须由POV复核实际金额。
                pov_target_amount = max(total_target_amount - qty * reference_price, 0.0)

            _track_execution(
                "register_entry_plan",
                entry_date=today_str, ts_code=ts_code, name=name_s,
                strategy_leg=str(row.get("strategy_leg", "")),
                signal_date=signal_date_s,
                target_qty=total_target_qty,
                target_amount=total_target_amount,
                reference_price=reference_price,
                auction_planned_qty=qty,
                pov_planned_qty=pov_planned_qty,
                pov_target_amount=pov_target_amount,
                status="待竞价确认并按实际成交额POV校准" if (
                    pov_enabled and actual_amount_rebalance_enabled
                ) else "待竞价执行",
            )

            if qty <= 0:
                logger().warning(
                    "[POV] %s %s 竞价段不足一手，82.5%%目标%.2f万全部交给09:30后POV。",
                    ts_code, name_s, total_target_amount / 1e4,
                )
                continue
            logger().warning("⏳ [盘前买入] %s %s  %d股  %s=%.2f元", ts_code, name_s, qty, price_label, price)

            request = OrderRequest(
                ts_code=ts_code,
                broker_code=str(row.get("broker_code", ts_code)),
                side="BUY",
                quantity=qty,
                price_type="FIXED_PRICE",
                price=price,
                strategy_name=str(row.get("strategy_name", "A_SYSTEM_ABC")),
                remark=f"盘前买入-{price_label}-{today_str}",
                strategy_leg=str(row.get("strategy_leg", "")).upper(),
                business_date=today_str,
                signal_date=signal_date_s,
                planned_exit_date=str(row.get("planned_exit_date", "")),
                purpose="OPEN",
                source_key=(
                    f"PREMARKET_OPEN|{today_str}|{row.get('strategy_leg', '')}|{ts_code}"
                ),
                metadata={
                    "execution_channel": "盘前买入",
                    "name": name_s,
                    "strategy_branch_id": str(
                        row.get("matched_strategy_branch_ids", "")
                    ),
                    "condition_profile_ids": str(
                        row.get("matched_condition_profile_ids", "")
                    ),
                    "exit_rule": str(row.get("exit_rule", "")),
                },
            )
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                result  = adapter.place_order(request)

            if result.accepted:
                # 09:20集合竞价预挂单09:25开始撮合，此处不立即记录持仓，落盘待确认，09:30按实盘成交确认
                raw_exit_n = row.get("exit_n_days", None)
                exit_n = int(float(raw_exit_n)) if raw_exit_n is not None and str(raw_exit_n) not in {"", "nan"} else 2
                pending_order = {
                    "order_id": str(result.order_id or f"premarket-{today_str}-{ts_code}"),
                    "ts_code": ts_code,
                    "name": name_s,
                    "signal_date": signal_date_s,
                    "strategy_leg": str(row.get("strategy_leg", "")),
                    "qty": qty,
                    "ref_price": price,
                    "exit_n": exit_n,
                    "planned_exit_date": str(row.get("planned_exit_date", "")).split(".")[0],
                    "strategy_branch_id": str(row.get("matched_strategy_branch_ids", "")),
                    "condition_profile_ids": str(row.get("matched_condition_profile_ids", "")),
                    "exit_rule": str(row.get("exit_rule", "")),
                    "entry_target_qty": total_target_qty,
                    "entry_target_amount": total_target_amount,
                    "entry_min_acceptable_amount": min_acceptable_amount,
                    "entry_hard_cap_amount": hard_cap_amount,
                    "entry_equity_snapshot": equity_snapshot,
                }
                # 券商受理一笔就原子追加一笔。若落盘失败，统一进程恢复会在
                # 新进程启动交易线程前按SQLite意图和QMT真实状态接管。
                _append_pending_buys([pending_order])
                pending_buys.append(pending_order)
                logger().info("✅ [盘前买入] %s %s %d股 @%.2f 委托已受理（09:20预挂，待09:30确认成交）",
                              ts_code, name_s, qty, price)
            else:
                logger().error("❌ [盘前买入] %s %s 提交失败：%s", ts_code, name_s, result.message)

        except Exception as e:
            logger().error("盘前买入异常（%s）：%s", row.get("ts_code"), e)

    if pending_buys:
        _start_premarket_buy_monitor()
        # 开仓计划通知：09:20预挂成功即推送，让用户开盘前就知道今日买什么，
        # 不必等09:30成交确认。金额按预挂价（涨停价）估算，实际按集合竞价开盘价
        # 成交通常更低；预挂失败的场景由09:30补买路径的“开仓执行降级”告警覆盖。
        plan_parts = []
        for b in pending_buys:
            qty_b = int(b.get("qty", 0))
            rp_b = float(b.get("ref_price", 0.0))
            plan_parts.append(
                f"策略{b.get('strategy_leg','')} {b.get('ts_code','')} {b.get('name','')} "
                f"{qty_b}股（预挂{rp_b:.2f}元，预估约{qty_b * rp_b / 10000:.2f}万）"
            )
        _notify("buy_result", "📋 今日开仓计划已预挂",
                "；".join(plan_parts) + "。09:20集合竞价预挂完成，实际按开盘价成交（通常低于预挂价），"
                "09:30确认实际成交额后，低于82.5%目标的缺口由现有POV在85%绝对硬顶内校准。",
                level="timeSensitive")
    if e_rows:
        threading.Thread(
            target=_e_auction_buy_worker,
            args=(e_rows, broker_cfg, today_str),
            daemon=True,
            name="e-auction-buy",
        ).start()
        logger().info("E竞价动态买入线程已启动：09:24读实时竞价量后定仓挂单（%d只候选）。", len(e_rows))
    if pov_items:
        _pov_enqueue(pov_items)
    logger().info("===== 盘前买入挂单完成（受理%d笔，待开盘确认；E延迟至09:24=%d笔；POV平滑段=%d笔）=====",
                  len(pending_buys), len(e_rows), len(pov_items))


def job_morning() -> None:
    logger().info("===== 盘前任务（09:20）=====")

    # ① 平仓检查 —— 最高优先级，独立执行
    try:
        check_and_close_positions()
    except Exception as e:
        logger().error("平仓检查异常：%s —— 请立即手动检查持仓！", e)

    combined = load_combined_decisions()
    decisions = combined[0] if combined is not None else None
    combined_orders_path = combined[1] if combined is not None else None
    if decisions is None:
        logger().error("组合状态机决策生成失败，早盘不启动D，也不执行A/C买入预览。")
        return

    # ② D 待卖持仓最高优先级。09:20只播报，等待09:23集合竞价平仓入口执行。
    if has_combined_action(decisions, "PLAN_SELL_D_FIRST"):
        logger().info("组合状态机要求先卖D；等待09:23集合竞价平仓，不在09:20非交易时段提交委托。")
        return

    # ③ A/C 买入信号 —— 本方法先完成09:20复核，调度器随后调用 job_premarket_buy 提交。
    if has_combined_action(decisions, "ALLOW_ABC_BUY_PREVIEW"):
        logger().info("组合状态机允许A/C买入；09:20复核完成后立即集合竞价预挂，09:30确认成交/必要时补单。")
    else:
        logger().info("组合状态机未允许A/C买入，跳过。")

    # ④ E T+1 开仓 —— 09:20复核后启动延迟线程，09:24按竞价容量提交。
    if has_combined_action(decisions, "ALLOW_E_BUY"):
        logger().info("组合状态机允许E开仓；09:20复核后启动09:24竞价容量买入，09:30确认成交/必要时补单。")
    else:
        logger().info("组合状态机未允许E开仓，跳过。")

    # ⑤ 策略D监控 —— 与A/C/E全日互斥。只有“无持仓、无非D正式候选、
    # 无非D开仓计划”三项同时成立，D才从09:30启动扫描、记录和开仓。
    d_allowed, d_gate_reason = d_intraday_monitor_gate(decisions)
    if d_allowed:
        logger().info("D路径采集总门通过：%s", d_gate_reason)
        job_strategy_d(decisions)
    else:
        logger().info("组合状态机未允许D盘中监控，跳过：%s", d_gate_reason)

    logger().info("===== 盘前任务完成 =====")


def job_opening_buy(*, recovery_only: bool = False) -> None:
    logger().info("===== 开盘买入任务（09:30）=====")

    # 平仓检查冗余点③：确保逾期清理不因09:20/09:23被挤而丢失（见 job_premarket_buy 处注释）
    try:
        check_and_close_positions()
    except Exception as e:
        logger().error("09:30 平仓检查异常：%s", e)

    # 先确认09:20盘前买单是否在开盘成交，再决定是否需要补单
    try:
        confirm_pending_premarket_buys()
    except Exception as e:
        logger().error("盘前买单成交确认异常：%s —— 请手动核对！", e)

    if _d_relay_pair_active_today():
        # D接力拥有独立的资金中性状态账：卖D确认一片后才买候选一片。
        # 普通09:30补买和普通买入POV都必须让位，避免动用原有17.5%现金或重复建仓。
        logger().warning("09:30 D接力成对POV接管本次开仓，普通开盘补买路径跳过。")
        _start_d_relay_pair_worker()
        logger().info("===== 开盘买入任务完成 =====")
        return

    if _pov_active_today():
        # POV平滑段正在分片买入剩余仓位:补买路径必须让位,否则重复建仓。
        logger().info("09:30 POV平滑段执行中，剩余买入由POV线程接管，跳过开盘补买。")
        _start_pov_worker()
        logger().info("===== 开盘买入任务完成 =====")
        return

    if has_position_bought_today("ACE"):
        logger().info("09:30 检测到今日已有买入成交（09:20盘前买入已成交），跳过重复买入。")
        logger().info("===== 开盘买入任务完成 =====")
        return

    if load_pending_buys():
        logger().info("09:30 仍有09:20盘前买单在排队/待补挂，跳过新的开盘补买，避免重复委托。")
        logger().info("===== 开盘买入任务完成 =====")
        return

    if recovery_only:
        logger().warning(
            "开盘恢复确认已完成；当前已过普通开盘补买宽限，不新建A/C买单，"
            "后续只允许既有E延迟规则或D盘中兜底重新判断。"
        )
        logger().info("===== 开盘买入恢复任务完成 =====")
        return

    combined = load_combined_decisions()
    decisions = combined[0] if combined is not None else None
    combined_orders_path = combined[1] if combined is not None else None
    if decisions is None:
        logger().error("组合状态机决策生成失败，09:30不执行买入。")
        return

    if has_combined_action(decisions, "PLAN_SELL_D_FIRST"):
        logger().info("组合状态机要求先卖D，09:30不执行新的买入。")
        return

    # 与09:20共用同一个动作解析器，避免新增策略腿后两个入口名单漂移：
    # 当前正式腿序为 A > C > E > D，一个组合计划只执行最高优先动作一次。
    open_action = _combined_open_action_for_current_mode(decisions)

    # 正常路径在09:20已启动D线程；这里处理09:20任务遗漏或daemon盘中恢复。
    # 仍须重新通过“空仓+无其他腿候选/计划”总门。若已经错过09:30，监控器
    # 优先恢复原子检查点；检查点不可用时再严格回补QMT 09:30至当前1m路径。
    d_allowed, d_gate_reason = d_intraday_monitor_gate(decisions)
    if d_allowed:
        current = now_beijing()
        current_hhmm = current.hour * 100 + current.minute
        checkpoint_check = _strategy_d_checkpoint_recovery_check(current)
        if intraday_history_is_complete(current_hhmm):
            recovery_text = "从连续竞价起点启动"
        elif checkpoint_check.ok:
            recovery_text = f"从严格校验通过的原子路径恢复({checkpoint_check.reason})"
        else:
            recovery_text = (
                f"检查点不可恢复({checkpoint_check.reason})，"
                "启动后由监控器回补并逐根认证QMT 09:30至当前1m路径"
            )
        logger().info(
            "09:30开盘复核：D路径采集总门通过（%s），确认D监控%s；"
            "回补认证完成前禁止BUY。",
            d_gate_reason,
            recovery_text,
        )
        job_strategy_d(decisions)
    elif (
        has_combined_action(decisions, "ALLOW_D_INTRADAY_MONITOR")
        or bool(_d_blocking_buy_actions(decisions))
        or has_combined_action(decisions, "BLOCK_D_INTRADAY_MONITOR")
    ):
        logger().error("09:30 D启动总门未通过：%s", d_gate_reason)

    if open_action:
        buy_gate_ok, buy_gate_reason = _new_buy_execution_gate()
        if not buy_gate_ok:
            _notify_new_buy_gate_block("09:30开盘执行", buy_gate_reason)
            logger().info("===== 开盘买入任务完成（新增BUY门禁阻断）=====")
            return

    # 走到这里=今日有买入窗口但09:20预挂链路没有产生持仓、也没有待确认单
    # （预挂失败/未执行/被拒）。属于执行降级：失去集合竞价排队优势；普通
    # A/C普通候选不再一次性整单追买，而是建立同一套持久化POV任务。
    # （2026-07-03 德冠新材即此场景：预挂未生效，09:30补买20.33，事后才发现。）
    if open_action:
        if open_action == "ALLOW_E_BUY":
            logger().warning(
                "E竞价链未生效；先建立专用延迟执行，确认接管后再发送降级通知。"
            )
        else:
            logger().warning(
                "09:20盘前预挂未生效；先建立09:30持久化POV，确认接管后再发送降级通知。"
            )

    action_reason = {
        "ALLOW_ABC_BUY_PREVIEW": "A/C 09:30开仓",
        "ALLOW_E_BUY": "E 09:30开仓",
    }
    attempted_buy = bool(open_action)
    accepted_buy = False
    if open_action:
        action_label = action_reason.get(open_action, f"{open_action} 09:30开仓")
        if open_action == "ALLOW_E_BUY":
            # E保留按竞价容量与开盘涨幅≤2%的专用延迟重试，不能混入普通POV。
            accepted_buy = False
        elif now_beijing().time() >= OPEN_PLAN_DEFAULT_EXECUTION_CUTOFF:
            # 调度器如果在09:35后才补跑09:30任务，不再从旧计划新建POV。
            # 原候选虽不追买，但仍按全日互斥规则继续阻断D至当日结束。
            logger().warning(
                "%s实际执行时已过普通候选09:35恢复宽限；不再新建买入POV，"
                "不追买原候选；D今日保持关闭。",
                action_label,
            )
        else:
            accepted_buy = _enqueue_opening_pov_from_plan(
                combined_orders_path,
                open_action=open_action,
                reason=action_label,
            )

    if accepted_buy:
        _notify(
            "buy_result",
            "⚠️ 开仓执行已降级并接管",
            "09:20盘前预挂未生效；09:30已成功落盘并启动持久化POV，继续遵守"
            "82.5%目标、80%验收下限、+2%追价保护和85%硬顶。",
            level="timeSensitive",
        )

    if not attempted_buy:
        logger().info("09:30无A/C/E买入计划；D是否扫描已由空仓及全策略候选总门单独判定。")
    elif not accepted_buy and not has_position_bought_today("ACE"):
        if open_action == "ALLOW_E_BUY":
            logger().warning(
                "09:30 E开仓未提交成功，启动延迟重试（9:31-13:30，相对开盘涨幅≤2%%）。"
            )
            _start_e_retry_thread(combined_orders_path, decisions)
        else:
            logger().warning(
                "09:30 %s开仓未提交成功/未成交；不追补其他静态候选。"
                "该候选仍按全日互斥规则阻断D，今日D不扫描也不开仓。",
                open_action,
            )

    logger().info("===== 开盘买入任务完成 =====")


def startup_catchup_strategy_d() -> None:
    """盘中重启守护进程时，补启动错过 09:20 的 D 监控。

    09:20-09:25 属于本系统的集合竞价预挂窗口，若守护进程在此时段重启，
    这里会先复核再补挂计划买单；09:25之后不再补挂集合竞价单，等09:30开盘流程处理；
    只读取组合状态机；只有它明确证明今日无持仓、无非D候选/计划时才补启动D。
    09:30后优先恢复同版本原子检查点；检查点不可用时由D监控从QMT回补并严格
    认证09:30至当前的完整1m路径，认证完成前不允许BUY。
    对于 E 开仓：若 9:30 后市场仍开盘（14:00 前），允许延迟重试（涨幅≤2%%）。
    """
    now = now_beijing()
    if not is_trade_day(now.date()):
        return
    if datetime.time(9, 0) <= now.time() < datetime.time(9, 20):
        logger().info("启动补检：当前已过09:00未到09:20，先补生成今日组合计划。")
        job_preopen_plan()
        return
    if datetime.time(9, 20) <= now.time() < datetime.time(9, 25):
        logger().warning("启动补检：当前位于09:20-09:25，立即复核并补执行集合竞价买入预挂。")
        job_morning()
        job_premarket_buy()
        return
    if datetime.time(9, 25) <= now.time() < datetime.time(9, 30):
        logger().warning("启动补检：已过09:25集合竞价申报窗口，不再补挂，等09:30开盘流程。")
        return
    if datetime.time(9, 30) <= now.time() < datetime.time(9, 35):
        # 进程可能恰在开盘切换时重启。此窗口仍属于既有09:30确认/补单和
        # POV执行模型；先恢复竞价成交、在途单及真实金额缺口，不把它误判成
        # “整天已错过”并放弃已有成交。
        logger().warning("启动补检：当前位于09:30-09:35，立即恢复开盘成交确认/补单链路。")
        job_opening_buy()
        return
    if (
        datetime.time(9, 35) <= now.time() < datetime.time(10, 30)
        and load_pending_buys()
    ):
        # 只恢复09:20已提交委托的成交/撤残单/POV归属，绝不因重启在9:35后
        # 新建普通开盘买单。恢复后若确实零成交，再继续下方E/D兜底判断。
        logger().warning("启动补检：发现09:20遗留待确认买单，先恢复真实成交与终态，不追补新普通买单。")
        job_opening_buy(recovery_only=True)
        if has_position_bought_today() or has_open_local_position() or _pov_active_today():
            return
    if not (datetime.time(9, 20) <= now.time() < datetime.time(14, 55)):
        return
    if _d_relay_pair_active_today():
        logger().info("启动补检：今日D接力成对POV仍在执行/待恢复，恢复专用线程并阻断其他开仓路径。")
        if now.time() < datetime.time(10, 30):
            _start_d_relay_pair_worker()
        return
    if _pov_active_today():
        logger().info("启动补检：今日实际成交额POV仍在执行/待恢复，不启动E延迟买入或D兜底，避免重复开仓。")
        _start_pov_worker()
        return
    if _strategy_d_monitor_running():
        logger().info("启动补检：D盘中兜底监控已在运行，无需重复启动。")
        return
    logger().info("启动补检(daemon重启触发)：现在处于D盘中兜底时段，读取今日组合状态机缓存，判断是否需要补启动D。")
    # 启动补检不是正式计划生成点，不能在非关键时段再重跑一次组合状态机。
    # 09:00 会生成当天计划；09:20 复核后会直接读缓存挂单。这里若重新运行
    # run_combined_live_plan.py，容易和启动候选播报/信号审计同时抢 CPU 与文件，
    # 造成 180 秒超时，并拖慢 QMT 心跳。缓存不存在时保守跳过，等下一轮定时任务处理。
    combined = read_cached_combined_decisions()
    decisions = combined[0] if combined is not None else None
    combined_orders_path = combined[1] if combined is not None else None
    if decisions is None:
        # 整机宕机跨过09:00时不会存在今日缓存。此时已经错过A/C/M普通开盘
        # 窗口，再等待“下一轮定时任务”实际要到14:40，D兜底也会整天丢失。
        # 本地盘运行后组合生成不再依赖旧Z:共享盘；这里限一次同步重建，仅生成
        # 计划，不追补过期买单，然后继续走既有E延迟/D兜底判断。
        logger().warning(
            "启动补检：今日组合状态机缓存缺失/读取失败，限一次重建今日计划；"
            "不会追补已过期的普通开盘买单。"
        )
        combined = load_combined_decisions()
        decisions = combined[0] if combined is not None else None
        combined_orders_path = combined[1] if combined is not None else None
        if decisions is None:
            logger().error("启动补检：今日组合状态机重建仍失败，无法安全判断E/D兜底。")
            _notify(
                "buy_result",
                "🛑 启动后无法恢复今日开仓状态机",
                "09:00组合缓存缺失且现场重建失败；系统不会盲目开仓，请查看daemon日志。",
                level="critical",
            )
            return
    if has_combined_action(decisions, "PLAN_SELL_D_FIRST"):
        logger().info("启动补检：今日有D持仓待卖出(优先处理)，不再启动新的D买入监控。")
        return
    # E延迟开仓是原候选自己的恢复链，必须先于D互斥门处理；有E候选时D会被
    # 正确关闭，但不能因此连E自己的重试也一起跳过。
    if (
        has_combined_action(decisions, "ALLOW_E_BUY")
        and now.time() < datetime.time(13, 30)
        and not _e_retry_running()
    ):
        logger().warning(
            "启动补检：E候选仍在13:30延迟窗口内，恢复E重试；"
            "按全日互斥规则D今日不启动、不扫描、不记录、不下单。"
        )
        _start_e_retry_thread(combined_orders_path, decisions)

    d_tracking_allowed, d_tracking_reason = d_intraday_monitor_gate(decisions)
    if not d_tracking_allowed:
        logger().info("启动补检：D全活动总门未通过，跳过：%s", d_tracking_reason)
        return

    try:
        d_release, d_signal_clock = _strategy_d_release_signal_clock()
    except Exception as exc:
        logger().error(
            "启动补检：D正式发布时钟读取失败，按fail-closed不启动D：%s",
            exc,
        )
        return
    now_hhmm_value = now.hour * 100 + now.minute
    if (
        release_uses_factor_union(d_release)
        and now_hhmm_value > d_signal_clock.last_signal_hhmm
    ):
        logger().warning(
            "启动补检：D总门通过（%s），但release=%s的有效回封桶=%s、"
            "最后信号分钟=%02d:%02d，当前已过窗口；不启动全市场扫描或QMT分钟线"
            "回补，避免执行不可能产生正式候选的行情请求。",
            d_tracking_reason,
            str(d_release.get("release_id", "")),
            ",".join(d_signal_clock.reseal_time_buckets) or "全时段",
            d_signal_clock.last_signal_hhmm // 100,
            d_signal_clock.last_signal_hhmm % 100,
        )
        return

    d_history_complete = intraday_history_is_complete(now_hhmm_value)
    d_checkpoint_check = _strategy_d_checkpoint_recovery_check(now)
    if d_history_complete:
        recovery_text = "进程仍处于09:30完整路径起点"
    elif d_checkpoint_check.ok:
        recovery_text = f"原子路径检查点预检通过({d_checkpoint_check.reason})"
    else:
        recovery_text = (
            f"daemon于{now.strftime('%H:%M')}恢复且检查点不可恢复"
            f"({d_checkpoint_check.reason})；将从QMT回补09:30至当前完整1m路径"
        )
    logger().warning(
        "启动补检：D总门通过（%s）；%s。立即启动D监控，"
        "回补逐分钟认证完成前禁止BUY，恢复前已错过的信号不追买。",
        d_tracking_reason,
        recovery_text,
    )
    job_strategy_d(decisions)


def job_cancel_unfilled_buy_orders() -> None:
    """14:40 撤销所有未成交【买单/开仓单】。

    ⚠️ 只撤买单（order_type==STOCK_BUY/23）。挂出去还没成交的平仓卖单一律不撤，
       无法确定方向的委托也跳过（宁可漏撤买单，也绝不误撤卖单导致持仓被动过夜）。
    D策略买单由独立监控进程并行处理，此处兜底。
    """
    log = logger()
    log.info("===== 14:40 撤未成交买单（不动卖单）=====")

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))
    if not qmt_enabled:
        log.info("非实盘模式，跳过14:40撤买单")
        return

    broker_cfg = config.get("broker", {})
    # 终态状态码：部撤(53)、已撤(54)、已成(56)、废单(57)——这些无需撤
    TERMINAL_STATUS = {53, 54, 56, 57}
    BUY_ORDER_TYPE = 23   # STOCK_BUY（qmt_adapter：BUY->23, SELL->24）
    SELL_ORDER_TYPE = 24  # STOCK_SELL，用于日志区分，绝不撤
    _id_names = ["order_id", "m_nOrderID", "order_sysid", "m_strOrderSysID"]
    _ts_names = ["stock_code", "m_strInstrumentID", "instrument_id", "ts_code"]
    _type_names = ["order_type", "m_nOrderType", "order_side", "m_nDirection"]

    def _pick(d: dict, keys: list, default: Any = "") -> Any:
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        return default

    def _as_int(v: Any, default: int = -1) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    try:
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            orders = adapter.query_orders()
    except Exception as e:
        log.error("14:40撤买单：查询委托失败：%s", e)
        return

    if not orders:
        log.info("14:40撤买单：无委托记录")
        return

    # 自家买单 remark 白名单：只撤本系统下的单。手动单、新股/新债申购
    # (如733xxx申购代码, order_id常为0)、任何第三方委托一律不碰
    # （2026-07-14 事故：撤买单把可转债申购单当自家买单撤，order_id=0报错）。
    # "盘前买入"=09:20预挂单(一字板整日排队未成时必须在此撤掉,否则尾盘炸板
    # 会变成回测口径外的买入);"POV平滑"=POV片单(片内90秒自撤,此处兜底残单)。
    OWN_BUY_PREFIXES = ("A_SYSTEM", "E竞价动态", "盘前买入", "POV平滑")
    _remark_names = ["order_remark", "m_strRemark", "remark"]

    cancelled = failed = skipped = kept_sell = kept_foreign = 0
    for o in orders:
        order_id = str(_pick(o, _id_names, "")).strip()
        if not order_id or _as_int(order_id, 0) <= 0:
            continue   # 无效order_id（申购单常为0），无法撤也不该撤
        status_code = _as_int(_pick(o, ["order_status", "m_nOrderStatus", "status"], -1))
        if status_code in TERMINAL_STATUS:
            skipped += 1
            continue
        ts_code = str(_pick(o, _ts_names, "")).strip()
        order_type = _as_int(_pick(o, _type_names, -1))
        # 只撤能明确判定为买单的委托；卖单/未知方向一律保留，绝不误撤平仓单。
        if order_type != BUY_ORDER_TYPE:
            kept_sell += 1
            label = "卖单(平仓)" if order_type == SELL_ORDER_TYPE else f"未知方向(order_type={order_type})"
            log.warning("14:40撤买单：保留不撤 %s order_id=%s [%s]", ts_code, order_id, label)
            continue
        remark = str(_pick(o, _remark_names, "")).strip()
        if not remark.startswith(OWN_BUY_PREFIXES):
            kept_foreign += 1
            log.info("14:40撤买单：非本系统委托，保留不撤 %s order_id=%s remark=%r（手动/申购/第三方）",
                     ts_code, order_id, remark[:20])
            continue
        try:
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                ok = adapter.cancel_order(order_id)
            if ok:
                cancelled += 1
                log.warning("14:40撤买单已发: %s order_id=%s 状态码=%s", ts_code, order_id, status_code)
            else:
                failed += 1
                log.error("14:40撤买单失败: %s order_id=%s 状态码=%s", ts_code, order_id, status_code)
        except Exception as e:
            failed += 1
            log.error("14:40撤买单异常: %s order_id=%s: %s", ts_code, order_id, e)

    log.warning("14:40撤买单完成：撤买单=%d 失败=%d 保留卖单/未知=%d 非本系统保留=%d 已终态跳过=%d",
                cancelled, failed, kept_sell, kept_foreign, skipped)
    if cancelled > 0 or failed > 0:
        try:
            _notify(
                "sell_result",
                "14:40撤未成交买单" if failed == 0 else "⚠️ 14:40撤买单部分失败",
                f"撤买单={cancelled}笔 失败={failed}笔 保留卖单={kept_sell}笔 终态跳过={skipped}笔",
                level="timeSensitive" if failed == 0 else "critical",
            )
        except Exception:
            pass  # 可以忽略：_notify 自身已捕获并记录所有异常，这里只防意外

    log.info("===== 14:40 撤未成交买单完成 =====")
