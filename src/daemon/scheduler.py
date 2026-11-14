"""调度主循环与启动：日程、任务执行、健康播报、交易日历晨检、QMT定时重启核对、main()。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from src.utils.logger import setup_logger
from src.utils.config import load_json_config, mkdir_p
from src.utils.time_utils import BEIJING_TZ, now_beijing, today_beijing
from src.daemon.common import (
    BROKER_HEALTH_FILE,
    HEARTBEAT_FILE,
    PROJECT_ROOT,
    SCHEDULE,
    SCHED_AFTERNOON_CLOSE,
    SCHED_CANCEL_BUY_ORDERS,
    SCHED_MORNING_PLAN_PUSH,
    SCHED_MORNING_REVIEW,
    SCHED_OPENING_BUY,
    SCHED_OPEN_PLAN_FILL_CHECK,
    SCHED_POST_MARKET,
    SCHED_PREMARKET_SELL,
    SCHED_PREMARKET_SYNC,
    SCHED_PREOPEN_PLAN,
    _load_calendar,
    _notify,
    _notify_once_per,
    _notify_with_retry_async,
    _pause_pipeline_for_trade,
    _resume_pipeline_after_trade,
    _sleep_until_beijing,
    ensure_trade_calendar_fresh,
    is_trade_day,
    logger,
    market_is_open,
    next_trade_date_on_or_after,
    write_broker_health,
    write_heartbeat,
)
from src.daemon.state import (
    _d_relay_pair_active_today,
    _d_relay_pair_load_state,
    _m_load_state,
    _pov_active_today,
)
from src.daemon.plans import (
    _expected_signal_date,
    _has_due_close_plan_now,
    _has_premarket_close_plan,
    _has_signal_for_date,
    has_open_local_position,
)
from src.daemon.qmt import (
    _print_account_status,
    qmt_is_critical_window,
    reconcile_positions_with_broker,
    wait_for_qmt_startup_gate,
    wait_for_trade_recovery_gate,
)
from src.daemon.buy import _finalize_expired_pov_for_startup, _start_pov_worker
from src.daemon.buy_m import _m_start_pov_worker, job_m_after_auction, job_m_premarket_buy
from src.daemon.sell_pov import _exit_pov_monitor
from src.daemon.close import _close_position_watchdog, check_and_close_positions, job_afternoon
from src.daemon.takeprofit import _intraday_takeprofit_monitor
from src.daemon.strategy_d import (
    _d_relay_finalize,
    _d_relay_reconcile_pending_buy,
    _d_relay_reconcile_pending_sell,
    _start_d_relay_pair_worker,
    clear_strategy_d_intraday_transient_data,
    job_premarket_sell,
    reconcile_d_orphan_fills,
    stop_strategy_d_monitor,
)
from src.daemon.reports import (
    _decision_chain_broadcast_loop,
    _notify_missed_open_window_if_needed,
    _retry_morning_plan_notification_if_needed,
    job_open_plan_fill_check,
    report_next_day_candidates,
    report_signal_readiness_summary,
)
from src.daemon.pipeline import (
    _offline_data_fresh,
    _start_offline_data_maintenance,
    _start_post_market_pipeline,
    snapshot_realized_strategy_equity,
)
from src.daemon.jobs import (
    job_cancel_unfilled_buy_orders,
    job_morning,
    job_morning_plan_push,
    job_opening_buy,
    job_premarket_buy,
    job_premarket_position_sync,
    job_preopen_plan,
    startup_catchup_strategy_d,
)
from src.daemon import qmt as _daemon_qmt  # 账户心跳改写的变量要读它所在模块的最新值


SYSTEM_READY_TITLE = "✅ 程序与账户已恢复正常"
SYSTEM_READY_BODY = (
    "daemon 调度心跳正常，当前进程已成功查询QMT账户与持仓；"
    "交易任务已恢复正常执行，无需人工干预。"
)


def _publish_system_ready() -> None:
    """调度器与账户门禁均就绪后，写心跳并发送唯一的启动/恢复成功通知。"""
    write_heartbeat("running")
    _notify_with_retry_async(
        "connection",
        SYSTEM_READY_TITLE,
        SYSTEM_READY_BODY,
        level="timeSensitive",
    )


def setup() -> None:
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    logging_cfg = config.get("logging", {})
    mkdir_p(HEARTBEAT_FILE.parent)
    setup_logger(
        log_dir=PROJECT_ROOT / logging_cfg.get("log_dir", "logs"),
        log_file="trading_daemon.log",
        level=logging_cfg.get("level", "INFO"),
    )


def _periodic_health_snapshot(
    *,
    now: datetime.datetime | None = None,
    heartbeat_path: Path | None = None,
    broker_health_path: Path | None = None,
    current_pid: int | None = None,
    heartbeat_fresh_sec: float = 45.0,
    broker_health_fresh_sec: float = 180.0,
) -> dict[str, Any]:
    """只读汇总daemon与QMT健康事实，绝不调用券商接口或获取交易锁。"""
    current = now or now_beijing()
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TZ)
    heartbeat_file = heartbeat_path or HEARTBEAT_FILE
    broker_file = broker_health_path or BROKER_HEALTH_FILE
    expected_pid = int(current_pid if current_pid is not None else os.getpid())
    errors: list[str] = []
    heartbeat_age = float("inf")
    heartbeat_status = "unknown"
    heartbeat_pid = 0
    broker_age = float("inf")
    broker_status = "unknown"
    broker_pid = 0
    account = ""

    try:
        heartbeat_text = heartbeat_file.read_text(encoding="utf-8").strip()
        heartbeat_parts = heartbeat_text.split()
        heartbeat_at = datetime.datetime.fromisoformat(heartbeat_parts[0])
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=BEIJING_TZ)
        heartbeat_pid = int(
            next(part.split("=", 1)[1] for part in heartbeat_parts if part.startswith("pid="))
        )
        heartbeat_status = heartbeat_parts[-1] if heartbeat_parts else "unknown"
        heartbeat_age = (current - heartbeat_at).total_seconds()
        if heartbeat_pid != expected_pid:
            errors.append(f"daemon心跳PID不匹配({heartbeat_pid}!={expected_pid})")
        if heartbeat_status not in {"running", "sleeping"}:
            errors.append(f"daemon心跳状态={heartbeat_status}")
        if heartbeat_age < -5:
            errors.append(f"daemon心跳时间超前{abs(heartbeat_age):.0f}秒")
        elif heartbeat_age > max(float(heartbeat_fresh_sec), 1.0):
            errors.append(f"daemon心跳已陈旧{heartbeat_age:.0f}秒")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"daemon心跳不可读({type(exc).__name__})")

    try:
        broker_payload = json.loads(broker_file.read_text(encoding="utf-8"))
        broker_pid = int(broker_payload.get("pid", 0) or 0)
        broker_status = str(broker_payload.get("status", "unknown") or "unknown")
        account = str(broker_payload.get("account", "") or "")
        updated_text = str(broker_payload.get("updated_at", "") or "").strip()
        if updated_text:
            broker_at = datetime.datetime.fromisoformat(updated_text)
            if broker_at.tzinfo is None:
                broker_at = broker_at.replace(tzinfo=BEIJING_TZ)
        else:
            broker_at = datetime.datetime.fromtimestamp(
                float(broker_payload.get("updated_ts", 0.0) or 0.0),
                tz=BEIJING_TZ,
            )
        broker_age = (current - broker_at).total_seconds()
        if broker_pid != expected_pid:
            errors.append(f"QMT健康PID不匹配({broker_pid}!={expected_pid})")
        if broker_status != "verified":
            errors.append(f"QMT接口状态={broker_status}")
        if broker_age < -5:
            errors.append(f"QMT验证时间超前{abs(broker_age):.0f}秒")
        elif broker_age > max(float(broker_health_fresh_sec), 1.0):
            errors.append(f"QMT验证已陈旧{broker_age:.0f}秒")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"QMT健康快照不可读({type(exc).__name__})")

    return {
        "ok": not errors,
        "checked_at": current,
        "pid": expected_pid,
        "heartbeat_age_sec": heartbeat_age,
        "heartbeat_status": heartbeat_status,
        "heartbeat_pid": heartbeat_pid,
        "broker_age_sec": broker_age,
        "broker_status": broker_status,
        "broker_pid": broker_pid,
        "account": account,
        "errors": errors,
    }


def _next_periodic_health_beacon_at(
    now: datetime.datetime,
    *,
    every_hours: int = 2,
    minute: int = 2,
    anchor_hour: int = 0,
) -> datetime.datetime:
    """计算下一个健康播报点；默认每天偶数小时的第02分。"""
    interval = min(max(int(every_hours or 2), 1), 12)
    target_minute = min(max(int(minute or 0), 0), 59)
    anchor = min(max(int(anchor_hour or 0), 0), 23)
    timezone = now.tzinfo or BEIJING_TZ
    for day_offset in range(2):
        target_day = now.date() + datetime.timedelta(days=day_offset)
        for hour in range(24):
            if (hour - anchor) % interval != 0:
                continue
            candidate = datetime.datetime.combine(
                target_day,
                datetime.time(hour, target_minute),
                tzinfo=timezone,
            )
            if candidate > now:
                return candidate
    return datetime.datetime.combine(
        now.date() + datetime.timedelta(days=1),
        datetime.time(anchor, target_minute),
        tzinfo=timezone,
    )


def _publish_periodic_health_beacon(
    config: dict[str, Any] | None = None,
    *,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """异步发送健康播报；本函数及其线程不触碰任何QMT/交易锁。"""
    runtime_config = config or load_json_config(PROJECT_ROOT / "config" / "config.json")
    beacon_cfg = runtime_config.get("notify", {}).get("health_beacon", {}) or {}
    checked_at = now or now_beijing()
    snapshot = _periodic_health_snapshot(
        now=checked_at,
        heartbeat_fresh_sec=float(beacon_cfg.get("heartbeat_fresh_sec", 45) or 45),
        broker_health_fresh_sec=float(beacon_cfg.get("broker_health_fresh_sec", 180) or 180),
    )
    if snapshot["ok"]:
        title = "✅ A_System运行正常"
        body = (
            f"北京时间{checked_at.strftime('%Y-%m-%d %H:%M:%S')}；"
            f"daemon PID {snapshot['pid']}，调度心跳{snapshot['heartbeat_age_sec']:.0f}秒前；"
            f"QMT账户{snapshot['account'] or '已脱敏'}接口验证"
            f"{snapshot['broker_age_sec']:.0f}秒前，状态正常。"
        )
        level = "active"
        logger().info("[定时健康播报] %s", body)
    else:
        title = "⚠️ A_System健康检查异常"
        body = (
            f"北京时间{checked_at.strftime('%Y-%m-%d %H:%M:%S')}；"
            + "；".join(snapshot["errors"])
            + "。健康播报只读检查未通过，请尽快检查Windows守护进程与QMT。"
        )
        level = "timeSensitive"
        logger().warning("[定时健康播报] %s", body)
    _notify_with_retry_async(
        "health_beacon",
        title,
        body,
        level=level,
        attempts=max(int(beacon_cfg.get("retry_attempts", 3) or 3), 1),
        retry_sec=max(float(beacon_cfg.get("retry_sec", 30) or 30), 0.0),
    )
    return snapshot


def _periodic_health_beacon_loop() -> None:
    """独立低优先级墙钟线程：每2小时第02分只读检查并异步推送。"""
    log = logger()
    while True:
        try:
            config = load_json_config(PROJECT_ROOT / "config" / "config.json")
            beacon_cfg = config.get("notify", {}).get("health_beacon", {}) or {}
            if not bool(beacon_cfg.get("enabled", True)):
                time.sleep(300)
                continue
            target = _next_periodic_health_beacon_at(
                now_beijing(),
                every_hours=int(beacon_cfg.get("every_hours", 2) or 2),
                minute=int(beacon_cfg.get("minute", 2) or 2),
                anchor_hour=int(beacon_cfg.get("anchor_hour", 0) or 0),
            )
            while True:
                remaining = (target - now_beijing()).total_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(max(remaining, 1.0), 60.0))
            _publish_periodic_health_beacon(config, now=now_beijing())
        except Exception as exc:  # noqa: BLE001
            log.warning("定时健康播报线程异常（不影响交易主流程）：%s", exc)
            time.sleep(60)


def _daily_calendar_sentinel() -> None:
    """每个自然日 08:30 交易日历晨检（独立常驻线程）。

    9:00 起就有盘前计划/挂单等交易动作，必须在此之前明确"今天是否交易日"。
    不能挂在 SCHEDULE 调度上——next_event 只在交易日历认定的交易日安排任务，
    日历误判时晨检自己就被跳过了。本线程按自然日无条件触发：
    - 日历覆盖不足自动刷新（ensure_trade_calendar_fresh 内含失败告警）；
    - 刷新后仍覆盖不到今天 → critical电话告警，9点前留人工确认窗口；
    - 正常时打一行明确结论日志（交易日/节假日休市）。
    """
    log = logger()
    last_checked = ""
    while True:
        try:
            now = now_beijing()
            today_str = today_beijing().strftime("%Y%m%d")
            if now.time() >= datetime.time(8, 30) and last_checked != today_str:
                last_checked = today_str
                try:
                    ensure_trade_calendar_fresh()
                except Exception as e:
                    log.error("晨检：日历刷新异常：%s", e)
                cal, max_date = _load_calendar()
                if not cal or today_str > max_date:
                    log.error("🛑 晨检：交易日历未覆盖今天 %s（最大=%s），将退化周历判断，节假日可能被误判！", today_str, max_date or "无")
                    _notify("system_error", "🛑 交易日历晨检失败",
                            f"日历数据缺失或未覆盖今天（最大={max_date or '无'}），今日交易日判断退化为周历，"
                            f"非周末节假日会被误判为交易日。请在9:00前人工确认今天({today_str})是否交易日，"
                            f"必要时停止daemon。", level="critical", call=True)
                elif is_trade_day(now.date()):
                    log.info("✅ 晨检(08:30)：今天 %s 是交易日（真实日历确认），今日交易任务照常。", today_str)
                else:
                    log.info("💤 晨检(08:30)：今天 %s 非交易日（周末/节假日休市），全天交易任务自动跳过。", today_str)
                # 节假日也查：定时重启叠加券商服务器停机正是长时间掉线的场景。
                _check_qmt_scheduled_restart(log)
        except Exception as e:
            logger().error("晨检线程异常：%s", e)
        # 睡到下一个08:30（当天已过则次日），上限1小时防时钟跳变
        now = now_beijing()
        nxt = now.replace(hour=8, minute=30, second=0, microsecond=0)
        if now >= nxt:
            nxt += datetime.timedelta(days=1)
        time.sleep(min(max((nxt - now).total_seconds(), 60), 3600))


def _running_on_windows() -> bool:
    return os.name == "nt"


def _live_qmt_account_id() -> str:
    """实盘正在使用的资金账号（内置执行端配置）；读不到时返回空串，改为核对全部账号目录。"""
    try:
        from qmt_inner.protocol import load_settings

        return str(load_settings().get("account_id", "") or "")
    except Exception:
        return ""


def _qmt_install_root() -> Path | None:
    """QMT安装目录 = QMT_PATH（userdata_mini）的上一级；未配置时返回None。"""
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        env_name = str(config.get("broker", {}).get("qmt_path_env", "QMT_PATH") or "QMT_PATH")
    except Exception:
        env_name = "QMT_PATH"
    raw = os.getenv(env_name, "").strip()
    return Path(raw).parent if raw else None


def _check_qmt_scheduled_restart(log: Any) -> bool:
    """每天晨检和daemon启动时核对QMT客户端定时重启必须关闭；打开或无法确认就报警。

    2026-09-15~09-28 QMT按定时重启设置每天08:50/20:52整进程重启并自动登录，
    节假日券商服务器不可用或需要验证码时停在登录框，内置桥接4次断到人工登录
    （两次跨过开盘）。9/28后该设置为关闭，再未发生。QMT升级或误操作可能把它
    重新打开，必须第一时间知道。返回是否确认已关闭。
    """
    if not _running_on_windows():
        return True
    try:
        from src.qmt_client_probe import read_scheduled_restart_settings, scheduled_restart_problem

        root = _qmt_install_root()
        problem = (
            "未配置QMT_PATH，无法找到QMT安装目录确认定时重启已关闭"
            if root is None
            else scheduled_restart_problem(read_scheduled_restart_settings(root, _live_qmt_account_id()))
        )
    except Exception as exc:
        problem = f"读取QMT定时重启设置失败：{exc}"
    if not problem:
        log.info("✅ QMT客户端定时重启已关闭（不会在08:50/20:52自行重启）。")
        return True
    log.error("⚠️ %s", problem)
    _notify_once_per(
        "qmt_scheduled_restart_on",
        12 * 3600,
        "⚠️ QMT客户端定时重启没有关闭",
        f"{problem}。重启后自动登录一旦失败（节假日服务器不可用、需要验证码），"
        "客户端会停在登录框、内置桥接一直断开。请在QMT设置里关闭定时重启，"
        "并告诉Claude核对。",
        level="timeSensitive",
    )
    return False


# ── 调度主循环 ─────────────────────────────────────────────────────────────────

def next_event(now: datetime.datetime) -> tuple[datetime.datetime, datetime.time]:
    """返回严格晚于 ``now`` 的首个交易任务。

    旧版使用 ``dt > now + 30秒`` 防重复，结果是在08:49:31之后启动时直接
    跳过08:50通知，在任务点前30秒内重启时也会跳过开/平仓节点。主循环现在
    用“执行完后沿日程表推进”防重复，不再牺牲临近任务。
    """
    day = next_trade_date_on_or_after(now.date())
    while True:
        for t in SCHEDULE:
            dt = datetime.datetime.combine(day, t, tzinfo=BEIJING_TZ)
            if dt > now:
                return dt, t
        day = next_trade_date_on_or_after(day + datetime.timedelta(days=1))


def next_event_after(
    completed_at: datetime.datetime,
    completed_time: datetime.time,
) -> tuple[datetime.datetime, datetime.time]:
    """沿固定日程返回刚完成任务的下一项，不因前一任务耗时而漏项。

    09:20、09:23、09:26、09:30之间只有数分钟。旧主循环每项完成后固定再睡
    60秒，并重新按墙钟寻找“未来任务”；只要某项稍慢，紧随其后的开仓/平仓
    复核就会永久被跳过。本函数以已执行的日程位置推进：若下一项已经迟到，
    主循环会立即执行，由各交易方法自身的时段门禁决定合法动作。
    """
    try:
        index = SCHEDULE.index(completed_time)
    except ValueError as exc:
        raise ValueError(f"未知调度时间:{completed_time}") from exc
    if index + 1 < len(SCHEDULE):
        next_time = SCHEDULE[index + 1]
        return (
            datetime.datetime.combine(completed_at.date(), next_time, tzinfo=BEIJING_TZ),
            next_time,
        )
    next_day = next_trade_date_on_or_after(completed_at.date() + datetime.timedelta(days=1))
    return (
        datetime.datetime.combine(next_day, SCHEDULE[0], tzinfo=BEIJING_TZ),
        SCHEDULE[0],
    )


def run_job(scheduled_time: datetime.time) -> None:
    today = today_beijing()
    trade_day = is_trade_day(today)
    if scheduled_time == SCHED_MORNING_PLAN_PUSH:   # 08:50 早盘推送今日开仓计划
        job_morning_plan_push() if trade_day else logger().info("非交易日，跳过早盘计划推送")
    elif scheduled_time == SCHED_PREOPEN_PLAN:   # 09:00
        job_preopen_plan() if trade_day else logger().info("非交易日，跳过盘前计划生成")
    elif scheduled_time == SCHED_MORNING_REVIEW:   # 09:20
        if trade_day:
            job_morning()
            job_premarket_buy()
            job_m_premarket_buy()   # 2026-11-12 ACE+M：M在ACE预挂之后预挂
        else:
            logger().info("非交易日，跳过盘前复核与买入")
    elif scheduled_time == SCHED_PREMARKET_SELL:   # 09:23
        if trade_day:
            if _has_premarket_close_plan():
                _pause_pipeline_for_trade("09:23集合竞价平仓计划")
                try:
                    job_premarket_sell()
                finally:
                    if _d_relay_pair_active_today():
                        # 接力任务已有独立持久化状态和专用线程，不需要为了等待10:30
                        # 一直占用“平仓优先”暂停门；数据流水线恢复不影响QMT卖买锁。
                        _resume_pipeline_after_trade("D接力成对POV已接管后续执行")
                    elif _has_due_close_plan_now() or _has_premarket_close_plan():
                        logger().warning("09:23平仓后仍检测到待平仓计划，流水线保持暂停；等待后续平仓确认或持仓清理。")
                    else:
                        _resume_pipeline_after_trade("09:23集合竞价平仓处理完成")
            else:
                logger().info("09:23 未检测到集合竞价平仓计划，流水线无需暂停。")
                job_premarket_sell()
        else:
            logger().info("非交易日，跳过集合竞价平仓")
    elif scheduled_time == SCHED_PREMARKET_SYNC:   # 09:26
        if trade_day:
            job_premarket_position_sync()
            job_m_after_auction()   # 2026-11-12 ACE+M：M竞价结果处理/换候选/启动M买入POV
        else:
            logger().info("非交易日，跳过盘前持仓同步")
    elif scheduled_time == SCHED_OPENING_BUY:   # 09:30
        if trade_day:
            job_opening_buy()
            _m_start_pov_worker()   # 2026-11-12 ACE+M：确保M买入POV在运行（重启恢复）
        else:
            logger().info("非交易日，跳过开盘买入任务")
    elif scheduled_time == SCHED_OPEN_PLAN_FILL_CHECK:   # 09:35 开仓计划成交核查
        job_open_plan_fill_check() if trade_day else logger().info("非交易日，跳过开仓核查")
    elif scheduled_time == SCHED_CANCEL_BUY_ORDERS:   # 14:40 独立撤未成交买单
        job_cancel_unfilled_buy_orders() if trade_day else logger().info("非交易日，跳过撤买单")
    elif scheduled_time == SCHED_AFTERNOON_CLOSE:   # 14:55 收盘平仓
        job_afternoon() if trade_day else logger().info("非交易日，跳过盘中任务")
    elif scheduled_time == SCHED_POST_MARKET:   # 15:10
        if trade_day:
            # D盘中分钟路径和重启检查点仅服务当日执行。收盘后先清理，避免历史
            # 临时文件逐日累积或被下一交易日误用；实盘审计记录不在清理范围。
            clear_strategy_d_intraday_transient_data("15:10收盘任务")
            # 收盘全量对账（只读+告警，独立于数据流水线，先跑）
            try:
                reconcile_positions_with_broker()
            except Exception as e:
                logger().error("收盘对账异常：%s", e)
            try:
                reconcile_d_orphan_fills()   # 兜底：盘中重启导致的孤儿D成交，收盘再扫一次
            except Exception as e:
                logger().error("收盘孤儿D恢复异常：%s", e)
            _start_post_market_pipeline(reason="15:10收盘流水线")
        else:
            logger().info("非交易日，跳过收盘流水线")


def _should_run_startup_signal_audit(now: datetime.datetime) -> bool:
    """启动时是否自动跑重量信号审计。

    信号审计会读取成交打标、市场情绪、A/C逐层漏斗等文件，纯属人工复盘展示；
    交易决策由09:00/09:20定时任务和组合状态机负责，不依赖启动审计输出。
    为避免启动后抢 CPU/磁盘、拖慢 QMT 心跳，只有盘前关键窗口自动跑。
    """
    if not is_trade_day(now.date()):
        return False
    return datetime.time(8, 50) <= now.time() <= datetime.time(9, 35)


def main() -> None:
    setup()
    log = logger()
    log.info("A_System 守护进程启动（PID %d）", os.getpid() if (os := __import__("os")) else 0)
    startup_now = now_beijing()
    # 若前一日15:10任务因关机而没执行，下一次启动必须补清历史D检查点。
    # 盘中重启时保留当天文件供严格恢复；收盘后重启则连当天文件一并清除。
    clear_strategy_d_intraday_transient_data(
        "守护进程启动兜底",
        preserve_trade_date=(
            startup_now.strftime("%Y%m%d")
            if startup_now.time() < datetime.time(15, 0)
            else ""
        ),
    )
    # 启动即覆盖上个进程遗留的 verified，防 keeper 在本进程尚未验证账户时误报恢复。
    write_broker_health("starting")

    offline_stop = threading.Event()

    def _exit(signum, _frame):
        offline_stop.set()
        log.info("收到信号 %d，退出", signum)
        _notify("system_error", "🔌 守护进程已停止",
                "守护进程被主动停止，实盘自动交易已暂停。如需恢复请重新启动。")
        stop_strategy_d_monitor("主守护进程退出")
        write_broker_health("stopped")
        write_heartbeat("stopped")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT, _exit)

    # 仅数据维护在门禁前运行；任何交易/真实账户定仓/持续交易线程仍在门禁后。
    offline_worker = _start_offline_data_maintenance(offline_stop)
    wait_for_qmt_startup_gate()

    # QMT连接成功不等于可以启动交易。必须先用券商真实仓位、委托、
    # 成交恢复事务意图；没有唯一证据时停在这里，不让任何定时函数抢跑。
    wait_for_trade_recovery_gate()
    offline_stop.set()
    if offline_worker is not None:
        offline_worker.join(timeout=2)

    ensure_trade_calendar_fresh()

    resume_d_relay_worker = False
    resume_pov_worker = False

    # D接力成对POV断点恢复：状态文件锁定D、候选和所有确认金额；重启后先恢复
    # 未知委托终态。这里只识别恢复任务，不启动执行线程；必须等孤儿成交恢复和
    # 启动平仓核对都结束，才继续“卖一片、买一片”。
    try:
        if is_trade_day(now_beijing().date()) and _d_relay_pair_active_today():
            if datetime.time(9, 25) <= now_beijing().time() < datetime.time(10, 30):
                log.warning("检测到未完成的D接力成对POV状态；待全部启动恢复完成后续跑。")
                resume_d_relay_worker = True
            elif now_beijing().time() >= datetime.time(10, 30):
                # 进程可能恰在10:30收口前崩溃。重启后先处理未知委托终态，再把
                # D余仓留到原计划日，不能让ACTIVE状态悬空到收盘。
                relay_state = _d_relay_pair_load_state()
                relay_broker_cfg = load_json_config(
                    PROJECT_ROOT / "config" / "config.json"
                ).get("broker", {})
                _d_relay_reconcile_pending_sell(
                    relay_state, relay_broker_cfg, cancel_nonterminal=True
                )
                _d_relay_reconcile_pending_buy(
                    relay_state, relay_broker_cfg, cancel_nonterminal=True
                )
                if str(relay_state.get("status", "")).upper() != "BLOCKED":
                    _d_relay_finalize(relay_state, "daemon在10:30后重启，已恢复委托终态并收口")
    except Exception as e:
        log.critical("D接力成对POV状态恢复检查异常：%s", e)
        raise RuntimeError("D接力恢复未完成，禁止启动其他交易线程") from e

    # 普通POV平滑执行断点恢复：同样只登记待恢复任务，不能抢在持仓/平仓恢复前下单。
    try:
        if (is_trade_day(now_beijing().date())
                and datetime.time(9, 25) <= now_beijing().time() < datetime.time(10, 30)
                and _pov_active_today()):
            log.warning("检测到未完成的POV平滑执行状态；待全部启动恢复完成后续跑。")
            resume_pov_worker = True
    except Exception as e:
        log.critical("POV状态恢复检查异常：%s", e)
        raise RuntimeError("POV恢复未完成，禁止启动其他交易线程") from e

    # ── 启动时恢复孤儿D成交（必须在平仓检查/持仓判定之前）─────────────────────
    # 2026-07-23 事故：盘中重启daemon后,旧会话D已成交委托(北方长龙301357)失忆——
    # 本地无记录→被当外部持仓→无人平仓,且D补启动误判空仓可能重复买入。
    # 此步从QMT当日委托(remark=D_FIRST_BOARD)自动找回已成交、本地无记录的D单,
    # 回写持仓账本;回写后has_open_local_position()=True,D补启动自然不再拉起。
    try:
        if not reconcile_d_orphan_fills():
            raise RuntimeError("孤儿D成交查询/归属未完成")
    except Exception as e:
        log.critical("孤儿D成交恢复异常：%s", e)
        raise RuntimeError("孤儿D成交恢复未完成，禁止启动交易线程") from e

    # 原生交易对账、分仓投影及孤儿成交恢复成功后，超出买入时段的POV也必须
    # 写入终态和真实金额；不能把异常退出留下的任务永久展示为仍在执行。
    _finalize_expired_pov_for_startup()

    _check_qmt_scheduled_restart(log)

    # ── 启动时立刻执行平仓检查 ────────────────────────────────────────────────
    log.info("启动检查：扫描逾期/待平仓持仓...")
    try:
        check_and_close_positions()
    except Exception as e:
        log.critical("启动平仓检查异常：%s —— 交易线程不会带病启动！", e)
        raise RuntimeError("启动平仓检查未完成，禁止启动交易线程") from e
    startup_has_position = has_open_local_position()

    # 全策略已实现净值在启动时同步一次，避免账户级风险影子状态等到收盘才刷新。
    try:
        snapshot_realized_strategy_equity(today_beijing().strftime("%Y%m%d"))
    except Exception as e:
        log.warning("启动时全策略净值快照异常：%s", e)

    # 只有在事务意图、D接力/POV断点、孤儿成交和到期持仓全部完成恢复后，
    # 才允许启动任何持续交易线程。这个顺序防止重启瞬间看门狗、止盈和POV
    # 同时抢旧状态，导致重复委托或漏平仓。
    if resume_d_relay_worker:
        log.warning("全部启动恢复已完成，现恢复D接力成对POV执行线程。")
        _start_d_relay_pair_worker()
    if resume_pov_worker:
        log.warning("全部启动恢复已完成，现恢复普通买入POV执行线程。")
        _start_pov_worker()
    # 2026-11-12 ACE+M：交易日09:26后重启时恢复M买入POV（10:30后只按已成交收口）。
    try:
        _now_m = now_beijing()
        if is_trade_day(_now_m.date()) and _now_m.time() >= datetime.time(9, 26):
            _m_state = _m_load_state()
            if (str(_m_state.get("date", "")) == _now_m.strftime("%Y%m%d") and _m_state.get("items")
                    and not _m_state["items"][0].get("finalized")):
                log.warning("全部启动恢复已完成，现恢复M买入POV执行线程。")
                _m_start_pov_worker()
    except Exception as e:
        log.error("恢复M买入POV失败（不影响ACE）：%s", e)
    threading.Thread(
        target=_daily_calendar_sentinel,
        daemon=True,
        name="calendar-sentinel",
    ).start()
    threading.Thread(
        target=_close_position_watchdog,
        daemon=True,
        name="close-watchdog",
    ).start()
    threading.Thread(
        target=_intraday_takeprofit_monitor,
        daemon=True,
        name="intraday-takeprofit",
    ).start()
    threading.Thread(
        target=_exit_pov_monitor,
        daemon=True,
        name="exit-pov",
    ).start()
    if startup_has_position:
        log.info("启动检查：检测到已有持仓，仍会检查/补跑收盘数据与候选信号；仅跳过D/E盘中补开仓逻辑。")
        threading.Thread(
            target=_print_account_status,
            args=(log,),
            daemon=True,
            name="startup-account-status",
        ).start()

    # ── 启动时先播报当前缓存候选，再按需后台补采 ─────────────────────────────
    try:
        expected = _expected_signal_date()
        expected_str = expected.strftime("%Y%m%d")
    except Exception as e:
        log.error("启动数据检查异常：%s", e)
        expected = today_beijing()
        expected_str = expected.strftime("%Y%m%d")

    # 账户风险账本同步后再启动决策播报，确保展示状态与最新已实现权益一致。
    threading.Thread(
        target=_decision_chain_broadcast_loop,
        daemon=True,
        name="decision-chain-broadcast",
    ).start()

    # 候选播报放后台线程：纯展示，不影响开仓关键路径。
    # 重量信号审计只在盘前关键窗口自动跑；盘中/夜间启动只播报当前组合候选。
    # 如果收盘数据缺失，先让收盘流水线补齐，流水线完成后会自行播报，避免启动时先打印旧审计。
    def _startup_report() -> None:
        try:
            report_next_day_candidates()
            # 08:50后重启补发尚未成功的早盘计划；09:35后若原计划因整机宕机
            # 没执行，则明确告警但不追补过期普通开盘单。
            _retry_morning_plan_notification_if_needed("daemon启动补检")
            _notify_missed_open_window_if_needed("daemon启动补检")
            if _should_run_startup_signal_audit(now_beijing()):
                report_signal_readiness_summary(expected_str)
            else:
                log.info(
                    "启动信号审计：当前不在盘前关键窗口，已跳过重量A/C逐层审计；"
                    "当前组合候选已播报，完整审计会在盘前/收盘流水线按需执行。"
                )
        except Exception as exc:
            log.error("启动候选播报异常：%s", exc)

    # 若缓存或 processed 审计数据不是最新交易日数据，后台线程补采。
    # raw 缓存存在但 processed 缺日时也必须补跑，否则盘前审计会误报旧口径。
    if not _has_signal_for_date(expected):
        log.warning("未找到 %s 收盘数据缓存，后台启动收盘/采集流水线补齐；若遇到平仓窗口会自动暂停让路。", expected_str)
        _start_post_market_pipeline(expected_str, reason=f"启动补齐缺失收盘信号 {expected_str}")
    elif not _offline_data_fresh(expected_str):
        log.warning("已有 %s 收盘信号缓存，但审计数据/停手产物来源尚未更新，后台启动流水线补齐；raw缓存命中会直接跳过采集。", expected_str)
        _start_post_market_pipeline(expected_str, reason=f"启动补齐processed审计数据 {expected_str}")
    else:
        log.info("缓存命中：已有 %s 收盘数据缓存且 processed 数据齐全，直接使用缓存，不重复采集/计算。", expected_str)
        threading.Thread(target=_startup_report, daemon=True, name="startup-report").start()

    if not startup_has_position:
        try:
            startup_catchup_strategy_d()
        except Exception as e:
            log.error("启动补检D策略异常：%s", e)
    else:
        log.info("启动补检：已有持仓，跳过D监控/E延迟开仓补启动。")

    # 到这里才代表账户门禁、启动持仓核对、任务线程和补检全部完成。
    # 统一在“程序调度+账户查询”双就绪后通知；不再在QMT刚握手成功时提前通知。
    _publish_system_ready()
    # 健康播报是完全独立的只读墙钟线程：只读daemon心跳和现有broker_health，
    # 异步发Bark，不调用QMT、不获取任何交易锁，也不进入核心SCHEDULE。
    threading.Thread(
        target=_periodic_health_beacon_loop,
        daemon=True,
        name="periodic-health-beacon",
    ).start()

    # ── 主循环 ────────────────────────────────────────────────────────────────
    # 首项按当前墙钟选择；此后严格沿SCHEDULE推进。这样临近任务不会被30秒
    # 护栏跳过，前一任务稍慢也不会吞掉09:23/09:26/09:30等紧邻节点。
    scheduled_dt, scheduled_time = next_event(now_beijing())
    while True:
        write_heartbeat("running")
        now = now_beijing()
        sleep_secs = max((scheduled_dt - now).total_seconds(), 0.0)
        log.info(
            "下次任务：%s（%.0f 秒后）",
            scheduled_dt.strftime("%Y-%m-%d %H:%M"),
            sleep_secs,
        )

        # 账户轮询：只有“交易时间 + 有持仓”才高频，其余统一60秒，避免无持仓时刷屏。
        _ACCT_DEFAULT = 60        # 无持仓、非交易时段、交易日前后：每60秒打印一次
        _ACCT_POSITION_TRADING = 10  # 交易时间且有持仓：每10秒打印一次
        _RETRY_INTERVAL = 15      # 关键窗口掉线重连间隔
        deadline = time.monotonic() + sleep_secs
        last_acct_ts = time.monotonic()
        last_trade_check_ts = 0.0  # 交易时段状态每5秒刷新一次，避免频繁计算
        is_trading = False
        last_heartbeat_ts = time.monotonic()
        _acct_thread: threading.Thread | None = None
        if has_open_local_position() or _daemon_qmt._last_account_has_position:
            _acct_thread = threading.Thread(
                target=_print_account_status, args=(log,), daemon=True
            )
            _acct_thread.start()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(2, remaining))
            now_ts = time.monotonic()
            if now_ts - last_heartbeat_ts >= 10:
                write_heartbeat("sleeping")
                last_heartbeat_ts = now_ts
            if now_ts - last_trade_check_ts >= 5:
                is_trading = market_is_open()
                last_trade_check_ts = now_ts
            qmt_critical = qmt_is_critical_window()
            if _daemon_qmt._qmt_reconnect_count == 0:
                has_position_for_poll = has_open_local_position() or _daemon_qmt._last_account_has_position
                if is_trading and has_position_for_poll:
                    interval = _ACCT_POSITION_TRADING
                else:
                    interval = _ACCT_DEFAULT
            else:
                interval = _RETRY_INTERVAL if qmt_critical else _ACCT_DEFAULT
            if now_ts - last_acct_ts >= interval:
                if _acct_thread is None or not _acct_thread.is_alive():
                    last_acct_ts = now_ts
                    _acct_thread = threading.Thread(
                        target=_print_account_status, args=(log,), daemon=True
                    )
                    _acct_thread.start()

        # monotonic等待偶尔会比Windows墙钟早数毫秒唤醒；交易任务提交前再按
        # 北京墙钟对齐一次。任务身份仍会传入具体job，不靠墙钟猜是不是14:55。
        _sleep_until_beijing(scheduled_time, max_wait=5.0)
        try:
            run_job(scheduled_time)
        except Exception as e:
            log.exception("任务执行异常（守护进程继续）：%s", e)
            _notify("system_error", "❌ 定时任务异常",
                    "守护进程某个定时任务执行异常（进程未退出），请回终端查看日志。",
                    level="timeSensitive")
        scheduled_dt, scheduled_time = next_event_after(scheduled_dt, scheduled_time)
