"""计划与门禁（只读）：组合状态机决策、正式开仓动作、D全活动门、平仓计划判断、信号日数据检查、
涨停价估算、方案甲停手门禁。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import glob
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from src.utils.config import load_json_config
from src.utils.time_utils import now_beijing, today_beijing
from src.portfolio_ace_m import (
    GATE_BLOCKED as ACE_GATE_BLOCKED,
    GATE_REDUCED as ACE_GATE_REDUCED,
    ace_gate_mode,
    position_in_family,
)
from src.daemon.common import (
    PROJECT_ROOT,
    SCHED_AFTERNOON_CLOSE,
    SCHED_CANCEL_BUY_ORDERS,
    SCHED_EXIT_POV_HANDOFF,
    TIMEOUT_COMBINED_PLAN_STEP,
    _pipeline_paused,
    _resume_pipeline_after_trade,
    is_trade_day,
    logger,
    prev_n_trade_days,
    run_script,
)
from src.daemon.state import (
    _d_relay_pair_active_today,
    _position_manual_exit_only,
    _pov_active_today,
    load_pending_buys,
    load_positions,
)


def st_open_forbidden(ts_code: str, name: Any) -> bool:
    """ST/风险警示股全局禁买闸（2026-07-23 用户铁律：无论如何不允许开仓）。

    判据=名字带ST/退（*ST/ST/退市整理；A股正常中文名不含这些字样，零误伤）。
    信号层照常生成计划（保持与回测口径的对照透明），本闸插在 daemon 全部5个
    BUY 委托出口，是最后一道拦截；D腿另有涨停幅≈5%反推的加强判据
    （monitor_strategy_d_intraday，防名字缓存不带前缀的场景）。
    """
    text = str(name or "")
    return ("ST" in text.upper()) or ("退" in text)


def active_strategy_mode() -> int:
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        return int(config.get("active_strategy_profile", {}).get("mode", 1))
    except Exception:
        return 1


def _expected_signal_date() -> datetime.date:
    """当前时刻应当持有的最新信号日期。
    收盘后（>=15:10 且交易日）→ 今天；其余时间 → 上一个交易日。
    """
    now_bj = now_beijing()
    today = today_beijing()
    if is_trade_day(today) and now_bj.time() >= datetime.time(15, 10):
        return today
    return prev_n_trade_days(today, 1)


def _has_signal_for_date(date: datetime.date) -> bool:
    """指定日期的 planned_orders 文件是否存在（文件名含该日期的8位数字）。"""
    import re as _re
    date_str = date.strftime("%Y%m%d")
    pattern = str(PROJECT_ROOT / "reports" / "paper_trade" / "ab_filtered_daily_ops" / "*_planned_orders.csv")
    for f in glob.glob(pattern):
        m = _re.search(r"\d{8}", Path(f).stem)
        if m and m.group() == date_str:
            return True
    return False


def _date_in_scored(target_date: datetime.date) -> bool:
    """检查实盘打分表是否包含 target_date 的记录（不导入 pandas）。"""
    path = _prefer_live_processed_path("live_limit_up_fill_scored.csv", "limit_up_fill_scored.csv")
    return _date_in_csv(path, target_date, "trade_date")


def _prefer_live_processed_path(live_name: str, fallback_name: str) -> Path:
    live_path = PROJECT_ROOT / "data" / "processed" / live_name
    if live_path.exists():
        return live_path
    fallback_path = Path(fallback_name)
    if fallback_path.is_absolute():
        return fallback_path
    if len(fallback_path.parts) > 1:
        return PROJECT_ROOT / fallback_path
    return PROJECT_ROOT / "data" / "processed" / fallback_path


def _date_in_csv(path: Path, target_date: datetime.date, date_column: str = "trade_date") -> bool:
    """流式检查 CSV 是否包含目标交易日，避免为了启动审计读取大文件。"""
    if not path.exists():
        return False
    target_str = target_date.strftime("%Y%m%d")
    try:
        with path.open(encoding="utf-8-sig") as f:
            header = [column.strip().strip('"') for column in f.readline().strip().split(",")]
            if date_column not in header:
                return False
            idx = header.index(date_column)
            for line in f:
                parts = line.split(",")
                if len(parts) > idx:
                    td = parts[idx].strip().strip('"').replace("-", "")[:8]
                    if td == target_str:
                        return True
    except Exception:
        pass  # 可以忽略：读不了按没有这一天处理，调用方按数据未就绪（fail-closed）
    return False


def _checklist_data_quality_blocked(target_date: datetime.date) -> bool:
    """检查目标日期 A/C checklist 是否因数据质量不足被阻断。"""
    import re as _re
    path_pattern = str(
        PROJECT_ROOT
        / "reports"
        / "paper_trade"
        / "ab_filtered_daily_ops"
        / "*_checklist.csv"
    )
    target_str = target_date.strftime("%Y%m%d")
    for f in glob.glob(path_pattern):
        m = _re.search(r"\d{8}", Path(f).stem)
        if not m or m.group() != target_str:
            continue
        try:
            import pandas as pd

            checklist = pd.read_csv(f, dtype={"signal_date": str}, low_memory=False)
        except Exception:
            continue
        if checklist.empty or "operation_status" not in checklist.columns:
            continue
        operation_status = checklist["operation_status"].fillna("").astype(str)
        selection_status = checklist.get("selection_status", pd.Series("", index=checklist.index)).fillna("").astype(str)
        if (
            operation_status.eq("DATA_QUALITY_BLOCKED").any()
            or selection_status.eq("LIMIT_DATA_QUALITY_NOT_COMPATIBLE").any()
        ):
            return True
    return False


_sig_day_amount_cache: dict = {}


def _signal_day_amount(ts_code: str, signal_date: str) -> float:
    """信号日成交额（元）；0表示数据缺失，由调用方按fail-closed门禁处理。"""
    key = str(signal_date)
    if key not in _sig_day_amount_cache:
        try:
            import pandas as pd
            path = PROJECT_ROOT / "data" / "raw" / "daily" / f"{key}.csv"
            _sig_day_amount_cache[key] = (
                pd.read_csv(path).set_index("ts_code")["amount"] if path.exists() else None
            )
        except Exception:
            _sig_day_amount_cache[key] = None
    ser = _sig_day_amount_cache[key]
    try:
        if ser is not None and ts_code in ser.index:
            return float(ser.loc[ts_code]) * 1000.0   # 日线amount单位为千元
    except Exception:
        pass  # 可以忽略：查不到按缺失返回0，调用方按fail-closed处理
    return 0.0


def _ace_gate_mode_for(action_date: str = "") -> str:
    """ACE今日执行模式：FULL(82.5%)/REDUCED(方案甲停手期20%)/BLOCKED(判定缺失等fail-closed)。"""
    day = str(action_date or today_beijing().strftime("%Y%m%d")).replace("-", "")[:8]
    try:
        from src.equity_curve_stop import live_gate

        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        return ace_gate_mode(live_gate(config, PROJECT_ROOT, day))
    except Exception as exc:
        logger().error("读取方案甲判定失败，ACE按fail-closed不开新仓：%s", exc)
        return ACE_GATE_BLOCKED


def _configured_exit_pov_handoff_time(live_cfg: dict[str, Any]) -> datetime.time:
    """读取卖出POV交接时刻；非法配置回退到锁定的14:53安全值。

    交接必须晚于14:40撤买单且早于14:55主平仓。这样配置可以成为唯一
    事实源，同时不会因拼写错误把POV延伸到主平仓窗口或提前抢占撤单通道。
    """
    raw = str(live_cfg.get("exit_pov_handoff_time", "") or "").strip()
    try:
        parsed = datetime.time.fromisoformat(raw)
    except (TypeError, ValueError):
        return SCHED_EXIT_POV_HANDOFF
    if parsed.tzinfo is not None:
        return SCHED_EXIT_POV_HANDOFF
    if not SCHED_CANCEL_BUY_ORDERS < parsed < SCHED_AFTERNOON_CLOSE:
        return SCHED_EXIT_POV_HANDOFF
    return parsed


def _strategy_d_force_sell_codes_today() -> set[str]:
    today_str = today_beijing().strftime("%Y%m%d")
    try:
        combined = load_combined_decisions()
        decisions = combined[0] if combined is not None else None
        if decisions is None or decisions.empty or not {"action", "strategy_leg"}.issubset(decisions.columns):
            return set()
        rows = decisions[
            (decisions["action"].astype(str) == "PLAN_SELL_D_FIRST")
            & (decisions["strategy_leg"].astype(str).str.upper() == "D")
        ]
        return {
            str(code)
            for code in rows.get("ts_code", []).dropna().astype(str).tolist()
            if str(code)
        }
    except Exception as exc:
        logger().debug("读取D接力让路计划失败：%s", exc)
        return set()


def _has_premarket_close_plan() -> bool:
    force_d_codes = _strategy_d_force_sell_codes_today()
    for pos in load_positions():
        if _position_manual_exit_only(pos):
            continue
        status = str(pos.get("status", "")).lower()
        if status == "closed":
            continue
        strategy_leg = str(pos.get("strategy_leg", "")).upper()
        ts_code = str(pos.get("ts_code", ""))
        if status == "sell_pending":
            return True
        if strategy_leg == "D" and ts_code in force_d_codes:
            return True
    return False


def _has_due_close_plan_now(assume_close_window: bool = False) -> bool:
    """现在是否存在需要执行的平仓计划。

    assume_close_window=True 由 14:55 的 job_afternoon 传入。返回 False 会让
    job_afternoon 里所有 `if close_plan_exists:` 保护的告警推送全部关闭，
    平仓失败变成静默事故（2026-08-12 即如此）。
    """

    today_str = today_beijing().strftime("%Y%m%d")
    now_time = now_beijing().time()
    for pos in load_positions():
        if _position_manual_exit_only(pos):
            continue
        status = str(pos.get("status", "")).lower()
        if status == "closed":
            continue
        planned_exit = str(pos.get("planned_exit_date", "99991231"))
        if status == "sell_pending":
            return True
        if planned_exit < today_str:
            return True
        if planned_exit == today_str and (
            assume_close_window or now_time >= SCHED_AFTERNOON_CLOSE
        ):
            return True
    return False


def _maybe_resume_pipeline_after_trade() -> None:
    if _pipeline_paused() and not _has_due_close_plan_now() and not _has_premarket_close_plan():
        _resume_pipeline_after_trade("未检测到仍需执行的平仓计划")


def _combined_paths_for_today() -> tuple[Path, Path]:
    today_str = today_beijing().strftime("%Y%m%d")
    base = PROJECT_ROOT / "reports" / "live_trade" / "combined"
    return (
        base / f"combined_decisions_{today_str}.csv",
        base / f"combined_planned_orders_{today_str}.csv",
    )


def read_cached_combined_decisions():
    """只读取今天已经生成的组合状态机文件，不重新运行脚本。

    09:20 集合竞价挂单必须快：计划在09:00已生成，09:20复核后只读缓存并下单，
    避免重新计算导致错过集合竞价排队时间。
    """
    try:
        import pandas as pd

        path, orders_path = _combined_paths_for_today()
        if not path.exists():
            logger().info("今日组合状态机缓存不存在：%s", path)
            return None
        decisions = pd.read_csv(path)
        if not decisions.empty and "action" in decisions.columns:
            action_summary = decisions["action"].astype(str).value_counts().to_dict()
            logger().info("读取今日组合状态机缓存：%s", action_summary)
        return decisions, orders_path
    except Exception as e:
        logger().error("读取今日组合状态机缓存失败：%s", e)
        return None


def _equity_curve_stop_block_reason(action_date: str) -> str:
    """方案甲停手门禁（只读）；返回空串=允许开仓，否则为停手原因。

    判定定义只在 src.equity_curve_stop；组合状态机与播报/核查共用同一结果，
    保证推送的明日计划、09:20/09:30真实下单和开仓核查三处口径一致。
    """

    try:
        from src.equity_curve_stop import live_gate

        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        check = live_gate(config, PROJECT_ROOT, str(action_date))
        # 2026-11-12 ACE+M：判定有效且明确停手时ACE按20%目标仓位照常开仓，不再算阻断；
        # 判定缺失/过期等fail-closed情形仍阻断。
        if check.allowed or ace_gate_mode(check) == ACE_GATE_REDUCED:
            return ""
        return check.reason
    except Exception as exc:
        return f"方案甲停手门禁读取失败，按fail-closed不开新仓：{exc}"


def load_combined_decisions():
    try:
        import pandas as pd
        ok = run_script("run_combined_live_plan.py", timeout=TIMEOUT_COMBINED_PLAN_STEP)
        if not ok:
            return None
        today_str = today_beijing().strftime("%Y%m%d")
        path = PROJECT_ROOT / "reports" / "live_trade" / "combined" / f"combined_decisions_{today_str}.csv"
        orders_path = PROJECT_ROOT / "reports" / "live_trade" / "combined" / f"combined_planned_orders_{today_str}.csv"
        if not path.exists():
            logger().error("组合状态机决策文件不存在：%s", path)
            return None
        decisions = pd.read_csv(path)
        if not decisions.empty and "action" in decisions.columns:
            action_summary = decisions["action"].astype(str).value_counts().to_dict()
            logger().info("组合状态机决策：%s", action_summary)
            for _, row in decisions.iterrows():
                logger().info(
                    "  组合决策明细：%s/%s %s %s  原因：%s",
                    row.get("strategy_leg", ""),
                    row.get("action", ""),
                    row.get("ts_code", ""),
                    row.get("name", ""),
                    row.get("reason", ""),
                )
        return decisions, orders_path
    except Exception as e:
        logger().error("读取组合状态机决策失败：%s", e)
        return None


def has_combined_action(decisions, action: str) -> bool:
    if decisions is None or decisions.empty or "action" not in decisions.columns:
        return False
    return decisions["action"].astype(str).eq(action).any()


MODE1_OPEN_ACTION_PRIORITY = (
    "ALLOW_ABC_BUY_PREVIEW",
    "ALLOW_E_BUY",
)


def _combined_open_action_for_current_mode(decisions) -> str:
    """返回当前模式唯一可执行的正式开仓动作，供09:20与09:30共同使用。

    决策表异常地同时出现多条ALLOW时仍只取腿序最前的一条，避免同一资金被
    多个执行入口重复消费。当前固定按A>C>E；C与A共用
    ALLOW_ABC_BUY_PREVIEW动作和同一计划单执行通道。
    """

    return next(
        (action for action in MODE1_OPEN_ACTION_PRIORITY if has_combined_action(decisions, action)),
        "",
    )


OPEN_ACTION_LEG_PRIORITY: dict[str, tuple[str, ...]] = {
    "ALLOW_ABC_BUY_PREVIEW": ("A", "C"),
    "ALLOW_E_BUY": ("E",),
}


def _select_unique_buy_order_for_action(
    orders,
    open_action: str,
    *,
    context: str,
):
    """把正式动作收敛为唯一策略腿、唯一BUY行；异常计划一律fail-closed。"""

    if orders is None or orders.empty:
        logger().error("%s：组合计划为空。", context)
        return orders.iloc[0:0].copy() if orders is not None else orders
    required = {
        "side",
        "strategy_leg",
        "ts_code",
        "signal_date",
        "planned_order_date",
        "planned_action",
    }
    missing = sorted(required - set(orders.columns))
    if missing:
        logger().error("%s：组合计划缺少字段%s，禁止开仓。", context, missing)
        return orders.iloc[0:0].copy()
    leg_priority = OPEN_ACTION_LEG_PRIORITY.get(open_action, ())
    if not leg_priority:
        logger().error("%s：未知正式开仓动作%s，禁止开仓。", context, open_action)
        return orders.iloc[0:0].copy()
    buys = orders[
        orders["side"].astype(str).str.upper().eq("BUY")
    ].copy()
    today_str = today_beijing().strftime("%Y%m%d")
    planned_date = (
        buys["planned_order_date"].astype(str).str.strip().str.split(".").str[0]
    )
    planned_action = buys["planned_action"].astype(str).str.strip().str.upper()
    signal_date = buys["signal_date"].astype(str).str.strip().str.split(".").str[0]
    contract_ok = (
        planned_date.eq(today_str)
        & planned_action.eq("PLAN_BUY_T1_OPEN")
        & signal_date.str.fullmatch(r"\d{8}", na=False)
        & signal_date.lt(planned_date)
    )
    invalid = buys[~contract_ok]
    if not invalid.empty:
        detail = "；".join(
            f"{row.get('strategy_leg', '')}/{row.get('ts_code', '')} "
            f"signal={row.get('signal_date', '')} "
            f"plan={row.get('planned_order_date', '')} "
            f"action={row.get('planned_action', '')}"
            for _, row in invalid.iterrows()
        )
        logger().critical(
            "🛑 %s：BUY计划日期/动作契约不合法（today=%s；%s），本轮全部拒绝。",
            context,
            today_str,
            detail,
        )
        return orders.iloc[0:0].copy()
    leg_text = buys["strategy_leg"].astype(str).str.upper()
    for leg in leg_priority:
        selected = buys[leg_text.eq(leg)].copy()
        if selected.empty:
            continue
        if len(selected) != 1:
            codes = "、".join(selected["ts_code"].astype(str).tolist())
            logger().critical(
                "🛑 %s：正式动作%s对应策略%s却有%d条BUY（%s），违反串行单仓，"
                "本轮全部拒绝。",
                context,
                open_action,
                leg,
                len(selected),
                codes,
            )
            return orders.iloc[0:0].copy()
        return selected
    logger().error(
        "%s：正式动作%s没有匹配策略腿%s的BUY行，禁止开仓。",
        context,
        open_action,
        "/".join(leg_priority),
    )
    return orders.iloc[0:0].copy()


D_INTRADAY_BLOCKING_BUY_ACTIONS = frozenset({
    "ALLOW_ABC_BUY_PREVIEW",
    # 旧缓存若仍出现已退役M动作，保持fail-closed并阻断D，避免把异常缓存
    # 当作“全策略无候选”；正常状态机永远不会再产生此动作。
    "ALLOW_M_BUY",
    "ALLOW_E_BUY",
})


def _d_blocking_buy_actions(decisions) -> list[str]:
    """返回占用D资金的正式候选动作，兼容未来新增的ALLOW_*_BUY动作。"""

    if decisions is None or decisions.empty or "action" not in decisions.columns:
        return []
    blocking: set[str] = set()
    for _, row in decisions.iterrows():
        action = str(row.get("action", "")).strip()
        if action in D_INTRADAY_BLOCKING_BUY_ACTIONS:
            blocking.add(action)
            continue
        # 未来新增策略无需改D核心名单：只要决策行本身是可执行BUY，就按正式
        # 候选处理。单纯的ALLOW_*_BUY_TODAY状态播报行没有side=BUY，不会误伤。
        if (
            action.startswith("ALLOW_")
            and "BUY" in action
            and not action.startswith("ALLOW_D_")
            and str(row.get("side", "")).strip().upper() == "BUY"
        ):
            blocking.add(action)
    return sorted(blocking)


def _d_candidate_legs(decisions, blocking_actions: list[str]) -> list[str]:
    if (
        decisions is None
        or decisions.empty
        or "action" not in decisions.columns
        or "strategy_leg" not in decisions.columns
    ):
        return []
    rows = decisions[
        decisions["action"].astype(str).isin(set(blocking_actions))
    ]
    legs: set[str] = set()
    for value in rows["strategy_leg"].dropna().astype(str):
        for leg in value.upper().replace("+", "/").split("/"):
            normalized = leg.strip()
            if normalized and normalized != "D":
                legs.add(normalized)
    return sorted(legs)


def d_intraday_monitor_gate(decisions) -> tuple[bool, str]:
    """D全活动总门：持仓、非D候选或非D开仓计划任一存在即全日关闭。"""

    if has_position_bought_today() or has_open_local_position():
        return False, "已有策略买入成交或未平仓持仓，D不启动、不扫描、不记录、不下单"
    if decisions is None or decisions.empty or "action" not in decisions.columns:
        return False, "组合状态机决策为空，无法确认D全活动权限"
    if has_combined_action(decisions, "PLAN_SELL_D_FIRST"):
        return False, "组合状态机仍要求先处理D持仓"
    if _d_relay_pair_active_today():
        return False, "D接力成对POV仍在执行/待恢复，D新监控关闭"
    pending = load_pending_buys()
    if pending:
        return False, f"今日仍有{len(pending)}笔非D开仓买单待确认，D全活动关闭"
    if _pov_active_today():
        return False, "今日非D开仓POV仍在执行/待恢复，D全活动关闭"

    conflicting = _d_blocking_buy_actions(decisions)
    if conflicting:
        legs = _d_candidate_legs(decisions, conflicting)
        return False, (
            f"今日存在非D正式候选/开仓计划={','.join(legs) or '/'.join(conflicting)}；"
            "按全日互斥规则，D不启动、不扫描、不记录、不下单"
        )
    if has_combined_action(decisions, "BLOCK_D_INTRADAY_MONITOR"):
        reason = "组合状态机明确阻断D盘中活动"
        if "reason" in decisions.columns:
            blocked = decisions[
                decisions["action"].astype(str).eq("BLOCK_D_INTRADAY_MONITOR")
            ]
            reasons = blocked["reason"].dropna().astype(str).str.strip()
            reason_text = "；".join(value for value in reasons.tolist() if value)
            if reason_text:
                reason = reason_text
        return False, reason
    if has_combined_action(decisions, "ALLOW_D_INTRADAY_MONITOR"):
        return True, "无持仓、无非D正式候选且今日无非D开仓计划，D允许扫描、记录和入场"
    return False, "组合状态机未明确授权D盘中活动"


def d_intraday_entry_gate(decisions) -> tuple[bool, str]:
    """D每轮BUY门禁与扫描总门完全同源，不再存在候选过期后的动态放行。"""

    return d_intraday_monitor_gate(decisions)


def has_open_local_position(family: str | None = None) -> bool:
    """family=None 含全部策略（含M）；"ACE" 只看A/C/E/D；"M" 只看M。"""
    return any(
        str(p.get("status", "")).lower() in {"open", "sell_pending"} and position_in_family(p, family)
        for p in load_positions()
    )


def has_position_bought_today(family: str | None = None) -> bool:
    """今日已有买入成交的持仓——09:20/09:30 防重复买入的正确判据。

    该函数只识别“今天这笔开仓是否已成交”，供集合竞价/POV同一笔防重使用。
    旧仓是否阻断新开仓由 _broker_has_preexisting_strategy_position 单独负责，
    两种身份不能混用，否则会误伤当日POV部分成交后的继续拆单。"""
    today = today_beijing().strftime("%Y%m%d")
    return any(
        str(p.get("status", "")).lower() in {"open", "sell_pending"}
        and str(p.get("buy_date", "")) == today
        and position_in_family(p, family)
        for p in load_positions()
    )


def _limit_up_pct_for_stock(ts_code: str, name: str = "") -> float:
    """按A股交易板块估算涨停幅度，供QMT未返回upper_limit时兜底。

    这里不用卖一/卖五替代涨停价。盘前预挂的目标是尽量贴近回测的
    “T+1按涨停价排队/成交”口径，QMT行情缺涨停价时只能用昨收价估算。
    """
    code = str(ts_code).upper()
    name_s = str(name).upper()
    if "ST" in name_s:
        return 0.05
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("8", "4", "920", "830", "870")) or code.endswith(".BJ"):
        return 0.30
    return 0.10


def _round_stock_price(value: float) -> float:
    """A股价格保留2位小数，使用四舍五入到分，避免Python round的银行家舍入。"""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _ceil_stock_price(value: float) -> float:
    """把卖出价格下限向上取到0.01元，确保申报价不会低于精确笼子边界。"""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def _find_local_prev_close(ts_code: str, signal_date: str = "") -> tuple[float, str]:
    """从收盘流水线数据中读取上一交易日收盘价，供T+1涨停价估算兜底。

    优先使用 live_limit_up_fill_scored/live_limit_up_merged，因为它们是实盘收盘
    流水线生成的轻量文件；再兜底 daily_merged_by_date/{signal_date}.csv。
    """
    import pandas as pd

    code = str(ts_code)
    date_s = str(signal_date or "").strip()
    candidates: list[tuple[Path, str]] = [
        (PROJECT_ROOT / "data" / "processed" / "live_limit_up_fill_scored.csv", "live_limit_up_fill_scored.close"),
        (PROJECT_ROOT / "data" / "processed" / "live_limit_up_merged.csv", "live_limit_up_merged.close"),
    ]
    if date_s:
        candidates.append((
            PROJECT_ROOT / "data" / "processed" / "daily_merged_by_date" / f"{date_s}.csv",
            f"daily_merged_by_date/{date_s}.close",
        ))
    for path, source in candidates:
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path, dtype={"ts_code": str, "trade_date": str})
            if "ts_code" not in df.columns or "close" not in df.columns:
                continue
            sub = df[df["ts_code"].astype(str).eq(code)]
            if date_s and "trade_date" in sub.columns:
                sub = sub[sub["trade_date"].astype(str).eq(date_s)]
            if sub.empty:
                continue
            close = float(sub.iloc[-1].get("close", 0.0) or 0.0)
            if close > 0:
                return close, source
        except Exception as e:
            logger().warning("读取本地昨收价失败：%s %s", path, e)
    return 0.0, ""


def _estimate_limit_up_price(quote: Any, ts_code: str, name: str = "") -> float:
    """QMT未给涨停价时，用昨收价估算涨停价；没有昨收价则返回0。"""
    if not quote:
        return 0.0
    pre_close = float(
        getattr(quote, "pre_close", 0.0)
        or getattr(quote, "last_close", 0.0)
        or getattr(quote, "prev_close", 0.0)
        or 0.0
    )
    if pre_close <= 0:
        return 0.0
    return _round_stock_price(pre_close * (1 + _limit_up_pct_for_stock(ts_code, name)))


def blocks_d_for_opening_plan(decisions) -> bool:
    """识别D是否因当日任意非D正式候选/开仓计划而全活动关闭。"""
    if decisions is None or decisions.empty or "action" not in decisions.columns:
        return False
    actions = decisions["action"].astype(str)
    if _d_blocking_buy_actions(decisions):
        return True
    if not actions.eq("BLOCK_D_INTRADAY_MONITOR").any():
        return False
    reason_text = ""
    if "reason" in decisions.columns:
        reason_text = " ".join(decisions["reason"].fillna("").astype(str).tolist())
    return any(
        keyword in reason_text
        for keyword in ("开仓", "同一资金", "A/C", "A/B/C", "E")
    )


def _load_e_signal_for_signal_date(signal_date: str) -> dict[str, Any] | None:
    """读取 e_signals_recent.json 中对应 signal_date 的入选信号。"""
    try:
        import json

        path = PROJECT_ROOT / "reports" / "strategy_e" / "e_signals_recent.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        signals = data.get("signals", [])
        if not isinstance(signals, list):
            return None
        for signal in reversed(signals):
            if str(signal.get("signal_date", "")) == str(signal_date):
                return signal
    except Exception as exc:
        logger().debug("读取E信号失败：%s", exc)
    return None


def _latest_planned_orders_signal_date() -> str:
    """最新 planned_orders 文件名里的信号日期；无文件返回空串。"""
    try:
        import re as _re

        dates = []
        for f in glob.glob(str(PROJECT_ROOT / "reports/paper_trade/ab_filtered_daily_ops/*_planned_orders.csv")):
            m = _re.search(r"_(\d{8})_planned_orders\.csv$", f)
            if m:
                dates.append(m.group(1))
        return max(dates) if dates else ""
    except Exception:
        return ""
