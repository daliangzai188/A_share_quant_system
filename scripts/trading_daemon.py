"""
A_System 量化策略常驻守护进程。

安全设计原则：
1. 平仓逻辑完全独立于数据流水线 —— 数据步骤出错不影响平仓。
2. 每个操作单独 try/except，不因局部错误崩溃。
3. subprocess 设超时上限，防止某步骤挂死导致平仓被跳过。
4. 进程本身崩溃由 start.sh 的外部 watchdog 自动重启。
5. 心跳文件每分钟更新，外部可监控守护进程存活状态。

调度时间表（A 股交易日）：
    09:20  盘前  —— 平仓检查（优先） + 组合状态机 + 买入预览 / D监控
    14:50  盘中  —— 平仓检查（优先）
    15:10  收盘  —— 数据流水线 + 信号生成

持仓状态：data/processed/positions.json
心跳文件：logs/daemon_heartbeat.txt
"""
from __future__ import annotations

import datetime
import glob
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.logger import get_logger, setup_logger
from src.utils.config import load_json_config
from src.utils.time_utils import BEIJING_TZ, now_beijing, today_beijing

# ── 常量 ───────────────────────────────────────────────────────────────────────
SCHEDULE = [
    datetime.time(9, 20),   # 盘前：平仓检查 + 组合状态机
    datetime.time(14, 50),  # 盘中平仓检查
    datetime.time(15, 10),  # 收盘流水线
]
import sys as _sys
_venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
PYTHON = str(_venv_python) if _venv_python.exists() else _sys.executable
POSITIONS_FILE = PROJECT_ROOT / "data" / "processed" / "positions.json"
HEARTBEAT_FILE = PROJECT_ROOT / "logs" / "daemon_heartbeat.txt"
CALENDAR_STALE_WARNED: set[str] = set()

# subprocess 超时（秒）：防止某步骤挂死
TIMEOUT_DATA_STEP = 600      # 数据采集/清洗步骤：10 分钟
TIMEOUT_SIGNAL_STEP = 300    # 信号生成步骤：5 分钟
TIMEOUT_ORDER_STEP = 60      # 下单预览步骤：1 分钟


def setup() -> None:
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    logging_cfg = config.get("logging", {})
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    setup_logger(
        log_dir=PROJECT_ROOT / logging_cfg.get("log_dir", "logs"),
        log_file="trading_daemon.log",
        level=logging_cfg.get("level", "INFO"),
    )


def logger():
    return get_logger("trading_daemon")


def write_heartbeat(status: str = "running") -> None:
    try:
        HEARTBEAT_FILE.write_text(
            f"{now_beijing().isoformat()} {status}\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def post_market_marker_path(date: datetime.date) -> Path:
    return PROJECT_ROOT / "logs" / f"post_market_done_{date.strftime('%Y%m%d')}.marker"


def mark_post_market_done(date: datetime.date) -> None:
    path = post_market_marker_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(now_beijing().isoformat(), encoding="utf-8")


def has_post_market_run_today(date: datetime.date) -> bool:
    if post_market_marker_path(date).exists():
        return True

    cutoff = datetime.datetime.combine(date, SCHEDULE[2], tzinfo=BEIJING_TZ).timestamp()
    pattern = str(PROJECT_ROOT / "reports" / "paper_trade" / "ab_filtered_daily_ops" / "*")
    for file_path in glob.glob(pattern):
        try:
            if Path(file_path).is_file() and Path(file_path).stat().st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


# ── 交易日历 ───────────────────────────────────────────────────────────────────

def _load_calendar() -> tuple[set[str], str]:
    try:
        import pandas as pd
        cal_path = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
        if not cal_path.exists():
            return set(), ""
        cal = pd.read_csv(cal_path, dtype={"cal_date": str})
        open_days = cal[cal["is_open"].astype(str).isin({"1", "1.0", "True", "true"})].copy()
        open_dates = set(open_days["cal_date"].astype(str).tolist())
        max_date = str(cal["cal_date"].astype(str).max()) if "cal_date" in cal.columns and not cal.empty else ""
        return open_dates, max_date
    except Exception:
        return set(), ""


def is_trade_day(date: datetime.date) -> bool:
    cal, max_date = _load_calendar()
    if cal:
        date_str = date.strftime("%Y%m%d")
        if date_str <= max_date:
            return date_str in cal
        warn_key = f"{max_date}->{date_str}"
        if warn_key not in CALENDAR_STALE_WARNED:
            CALENDAR_STALE_WARNED.add(warn_key)
            logger().warning("交易日历数据只到 %s，%s 超出范围，用周历代替", max_date, date_str)
        return date.weekday() < 5
    return date.weekday() < 5


def next_n_trade_days(date: datetime.date, n: int) -> datetime.date:
    cal, max_cal_date = _load_calendar()
    count, d = 0, date
    while count < n:
        d += datetime.timedelta(days=1)
        d_str = d.strftime("%Y%m%d")
        if cal and d_str <= max_cal_date:
            is_open = d_str in cal
        else:
            is_open = d.weekday() < 5  # 超出日历范围，降级用周历
        if is_open:
            count += 1
    return d


def next_trade_date_on_or_after(date: datetime.date) -> datetime.date:
    current = date
    while not is_trade_day(current):
        current += datetime.timedelta(days=1)
    return current


def market_is_open() -> bool:
    now = now_beijing()
    if not is_trade_day(now.date()):
        return False
    return datetime.time(9, 30) <= now.time() <= datetime.time(15, 0)


# ── 持仓状态文件 ──────────────────────────────────────────────────────────────

def load_positions() -> list[dict[str, Any]]:
    try:
        if not POSITIONS_FILE.exists():
            return []
        return json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger().error("读取持仓文件失败：%s", e)
        return []


def save_positions(positions: list[dict[str, Any]]) -> None:
    try:
        POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = POSITIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(positions, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(POSITIONS_FILE)  # 原子替换，防止写一半损坏
    except Exception as e:
        logger().error("保存持仓文件失败：%s", e)


def record_buy(order_id: str, ts_code: str, name: str, signal_date: str,
               buy_date: str, shares: int, buy_price: float, strategy_leg: str) -> None:
    positions = load_positions()
    if any(p["order_id"] == order_id for p in positions):
        return
    exit_date = next_n_trade_days(
        datetime.datetime.strptime(buy_date, "%Y%m%d").date(), n=2
    )
    positions.append({
        "order_id": order_id,
        "ts_code": ts_code,
        "name": name,
        "signal_date": signal_date,
        "buy_date": buy_date,
        "planned_exit_date": exit_date.strftime("%Y%m%d"),
        "shares": shares,
        "buy_price": buy_price,
        "strategy_leg": strategy_leg,
        "status": "open",
        "sell_date": None,
        "sell_price": None,
    })
    save_positions(positions)
    logger().info("持仓记录：%s %s 买入日 %s 计划平仓日 %s",
                  ts_code, name, buy_date, exit_date.strftime("%Y%m%d"))


def mark_position_closed(order_id: str, sell_date: str, sell_price: float = 0.0) -> None:
    positions = load_positions()
    for p in positions:
        if p["order_id"] == order_id and p["status"] != "closed":
            p["status"] = "closed"
            p["sell_date"] = sell_date
            p["sell_price"] = sell_price
    save_positions(positions)


# ── 平仓检查（最高优先级，独立运行，绝不因其他错误跳过）────────────────────

def _do_sell(pos: dict[str, Any], qmt_enabled: bool) -> None:
    """对单个持仓执行卖出动作，完全独立、单独 try/except。"""
    ts_code = pos["ts_code"]
    name = pos["name"]
    order_id = pos["order_id"]
    today_str = today_beijing().strftime("%Y%m%d")

    try:
        if qmt_enabled:
            logger().warning("[平仓] QMT 模式：触发实盘预览 %s %s", ts_code, name)
            ok = run_script("preview_live_orders.py", "--planned-orders", "latest",
                            timeout=TIMEOUT_ORDER_STEP)
            if ok:
                logger().warning("[平仓] 预览完成，请手动执行 submit_live_orders.py 确认下单")
            else:
                logger().error("[平仓] 预览失败，请立即手动检查 %s %s 持仓！", ts_code, name)
        else:
            logger().info("[平仓] 模拟盘：%s %s 标记已平仓", ts_code, name)
            mark_position_closed(order_id, today_str)
    except Exception as e:
        logger().error("[平仓] 执行异常（%s %s）：%s —— 请立即手动检查！", ts_code, name, e)


def check_and_close_positions() -> None:
    """
    扫描所有持仓，对满足平仓条件的立即处理。
    此函数是最高优先级，任何情况下都应被调用，绝不被数据错误拦截。
    """
    try:
        positions = load_positions()
    except Exception as e:
        logger().error("平仓检查：读取持仓失败 %s", e)
        return

    if not positions:
        logger().info("平仓检查：无持仓")
        return

    today_str = today_beijing().strftime("%Y%m%d")

    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))
    except Exception:
        qmt_enabled = False

    for pos in positions:
        try:
            status = pos.get("status", "open")
            planned_exit = pos.get("planned_exit_date", "99991231")

            if status == "closed":
                continue

            overdue = planned_exit <= today_str
            pending = status == "sell_pending"

            if not overdue and not pending:
                continue

            ts_code = pos.get("ts_code", "")
            name = pos.get("name", "")
            logger().warning("需要平仓：%s %s  计划平仓日 %s  状态 %s  市场开盘 %s",
                             ts_code, name, planned_exit, status, market_is_open())

            if market_is_open() or pending:
                _do_sell(pos, qmt_enabled)
            else:
                # 市场未开盘，标记 sell_pending，等开盘时处理
                positions_fresh = load_positions()
                for p in positions_fresh:
                    if p["order_id"] == pos["order_id"] and p["status"] == "open":
                        p["status"] = "sell_pending"
                save_positions(positions_fresh)
                logger().warning("市场未开盘，%s %s 标记 sell_pending，开盘后自动处理", ts_code, name)

        except Exception as e:
            logger().error("处理单个持仓异常（%s）：%s —— 跳过，继续检查其他持仓", pos.get("ts_code"), e)


# ── subprocess 执行（带超时）──────────────────────────────────────────────────

def run_script(name: str, *args: str, timeout: int = TIMEOUT_DATA_STEP) -> bool:
    cmd = [PYTHON, "-B", str(PROJECT_ROOT / "scripts" / name)] + list(args)
    logger().info("执行: %s  (超时 %ds)", " ".join(cmd), timeout)
    try:
        result = subprocess.run(cmd, cwd=PROJECT_ROOT, timeout=timeout)
        if result.returncode != 0:
            logger().error("%s 退出码 %d", name, result.returncode)
            return False
        return True
    except subprocess.TimeoutExpired:
        logger().error("%s 超时（%ds），已强制终止", name, timeout)
        return False
    except Exception as e:
        logger().error("%s 执行异常：%s", name, e)
        return False


# ── 定时任务 ───────────────────────────────────────────────────────────────────

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
        logger().error("组合状态机决策生成失败，早盘不启动D，也不执行A/B/C买入预览。")
        return

    # ② D 待卖持仓最高优先级。未确认D卖出前，不执行A/B/C买入，不启动D新监控。
    if has_combined_action(decisions, "PLAN_SELL_D_FIRST"):
        handle_combined_order_preview(combined_orders_path, reason="D持仓优先卖出")
        logger().info("组合状态机要求先卖D，早盘流程到此结束。")
        return

    # ③ A/B/C 买入信号 —— 只有组合状态机允许时才处理
    if has_combined_action(decisions, "ALLOW_ABC_BUY_PREVIEW"):
        handle_combined_order_preview(combined_orders_path, reason="A/B/C买入预览")
    else:
        logger().info("组合状态机未允许A/B/C买入，跳过。")

    # ④ 策略D监控 —— 只有无持仓且无A/B/C买入计划时才启动
    if has_combined_action(decisions, "ALLOW_D_INTRADAY_MONITOR"):
        job_strategy_d()
    else:
        logger().info("组合状态机未允许D盘中监控，跳过。")

    logger().info("===== 盘前任务完成 =====")


def load_combined_decisions():
    try:
        import pandas as pd
        ok = run_script("run_combined_live_plan.py", timeout=TIMEOUT_ORDER_STEP)
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
        return decisions, orders_path
    except Exception as e:
        logger().error("读取组合状态机决策失败：%s", e)
        return None


def has_combined_action(decisions, action: str) -> bool:
    if decisions is None or decisions.empty or "action" not in decisions.columns:
        return False
    return decisions["action"].astype(str).eq(action).any()


def handle_combined_order_preview(planned_orders_path: Path | None, reason: str) -> None:
    # 组合计划单预览 —— 次优先级，出错只记录
    try:
        import pandas as pd
        if planned_orders_path is None or not planned_orders_path.exists():
            logger().info("无组合 planned_orders，跳过：%s", reason)
            return

        try:
            orders = pd.read_csv(planned_orders_path)
        except pd.errors.EmptyDataError:
            logger().info("组合 planned_orders 文件为空，跳过：%s", reason)
            return
        if "side" not in orders.columns:
            logger().info("组合 planned_orders 无 side 列，跳过：%s", reason)
            return

        executable_orders = orders[orders["side"].astype(str).str.upper().isin({"BUY", "SELL"})]
        if executable_orders.empty:
            logger().info("组合计划单无买卖计划，跳过：%s", reason)
            return

        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))
        today_str = today_beijing().strftime("%Y%m%d")

        logger().info("发现组合计划 %d 条：%s", len(executable_orders), reason)

        if qmt_enabled:
            ok = run_script("preview_live_orders.py", "--planned-orders", str(planned_orders_path),
                            timeout=TIMEOUT_ORDER_STEP)
            if ok:
                logger().warning("组合预览完成，真实下单请手动执行 submit_live_orders.py")
        else:
            buy_orders = executable_orders[executable_orders["side"].astype(str).str.upper() == "BUY"]
            for _, row in buy_orders.iterrows():
                try:
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
                    )
                except Exception as e:
                    logger().error("记录持仓异常：%s", e)

    except Exception as e:
        logger().error("买入信号处理异常：%s", e)


def job_strategy_d() -> None:
    """09:20 盘前任务后立即启动策略D监控（后台子进程，不阻塞 daemon）。
    监控脚本内部等到09:30开始扫描，10:00起发WATCH提醒，14:00起发BUY信号，14:55自动撤单。
    """
    logger().info("===== 策略D监控启动（盘中后台）=====")
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        broker_config = config.get("broker", {})
        qmt_ready = (
            bool(config.get("broker_adapter_enabled"))
            and bool(config.get("qmt_enabled"))
            and bool(broker_config.get("enabled", False))
            and str(broker_config.get("adapter", "")).lower() == "qmt"
        )
        if not qmt_ready:
            logger().warning("QMT行情未启用，跳过D盘中监控；D策略需要实时行情。")
            return
        live_order = bool(config.get("trade_mode", "").lower() == "live" and config.get("live_trade", {}).get("enabled"))
        cmd = [PYTHON, "-B", str(PROJECT_ROOT / "scripts" / "monitor_strategy_d_intraday.py")]
        if live_order:
            cmd.append("--live-order")
        # Popen 非阻塞：D 监控脚本自管循环 + 14:55 自动撤单，daemon 继续正常运行
        proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT)
        logger().info("策略D监控已启动（PID %d，live_order=%s）", proc.pid, live_order)
    except Exception as e:
        logger().error("策略D监控启动失败：%s", e)
    logger().info("===== 策略D监控已移至后台 =====")


def job_afternoon() -> None:
    logger().info("===== 盘中任务（14:50）=====")
    try:
        check_and_close_positions()
    except Exception as e:
        logger().error("平仓检查异常：%s —— 请立即手动检查持仓！", e)
    logger().info("===== 盘中任务完成 =====")


def job_post_market() -> None:
    logger().info("===== 收盘流水线（15:10）=====")

    # 每步独立 try，出错不影响后续步骤
    steps = [
        ("collect_all_data.py",               "① 采集日线 + 涨停池",   TIMEOUT_DATA_STEP),
        ("clean_collected_data.py",            "② 清洗合并数据",         TIMEOUT_DATA_STEP),
        ("build_dynamic_features.py",          "③ 市场情绪 / 题材热度",  TIMEOUT_DATA_STEP),
        ("score_limit_up_fill_probability.py", "④ 涨停成交概率打分",     TIMEOUT_DATA_STEP),
        ("analyze_next_day_premium.py",        "⑤ 次日溢价因子",         TIMEOUT_DATA_STEP),
        ("run_paper_ab_filtered_daily_ops.py", "⑥ A+B+C 信号生成",      TIMEOUT_SIGNAL_STEP),
    ]

    today_str = today_beijing().strftime("%Y%m%d")
    extra_args: dict[str, list[str]] = {
        # 收盘后采集到今天（15:35 后数据已落地），确保今日涨停数据纳入明日信号
        "collect_all_data.py": ["--end-date", today_str],
        "run_paper_ab_filtered_daily_ops.py": ["--top-n", "10"],
    }

    for script, desc, timeout in steps:
        try:
            logger().info(desc)
            args = extra_args.get(script, [])
            ok = run_script(script, *args, timeout=timeout)
            if not ok:
                logger().error("%s 失败，继续后续步骤", desc)
        except Exception as e:
            logger().error("%s 异常：%s，继续后续步骤", desc, e)

    logger().info("===== 收盘流水线完成 =====")
    report_next_day_candidates()
    mark_post_market_done(today_beijing())


def report_next_day_candidates() -> None:
    """读取最新 planned_orders，播报下一交易日开仓候选。"""
    try:
        import pandas as pd
        next_date = next_n_trade_days(today_beijing(), 1)
        next_date_str = next_date.strftime("%Y-%m-%d")
        pattern = str(PROJECT_ROOT / "reports/paper_trade/ab_filtered_daily_ops/*_planned_orders.csv")
        files = sorted(glob.glob(pattern))
        if not files:
            logger().warning("【明日候选】未找到 planned_orders 文件，信号生成可能失败")
            return
        try:
            orders = pd.read_csv(files[-1])
        except Exception:
            logger().info("【明日候选】%s 无开仓计划，A/B/C 均无符合条件标的", next_date_str)
            return
        buy_orders = orders[orders["side"].astype(str).str.upper() == "BUY"] if "side" in orders.columns else pd.DataFrame()
        if buy_orders.empty:
            logger().info("【明日候选】%s 无开仓计划，A/B/C 均无符合条件标的", next_date_str)
        else:
            names = buy_orders.apply(
                lambda r: f"{r.get('ts_code', '')} {r.get('name', '')}", axis=1
            ).tolist()
            logger().info("【明日候选】%s 共 %d 只：%s", next_date_str, len(names), " | ".join(names))
    except Exception as e:
        logger().error("播报明日候选异常：%s", e)


# ── 调度主循环 ─────────────────────────────────────────────────────────────────

def next_event(now: datetime.datetime) -> tuple[datetime.datetime, datetime.time]:
    day = next_trade_date_on_or_after(now.date())
    while True:
        for t in SCHEDULE:
            dt = datetime.datetime.combine(day, t, tzinfo=BEIJING_TZ)
            if dt > now + datetime.timedelta(seconds=30):
                return dt, t
        day = next_trade_date_on_or_after(day + datetime.timedelta(days=1))


def run_job(scheduled_time: datetime.time) -> None:
    today = today_beijing()
    trade_day = is_trade_day(today)
    if scheduled_time == SCHEDULE[0]:     # 09:20
        job_morning() if trade_day else logger().info("非交易日，跳过盘前任务")
    elif scheduled_time == SCHEDULE[1]:   # 14:50
        job_afternoon() if trade_day else logger().info("非交易日，跳过盘中任务")
    elif scheduled_time == SCHEDULE[2]:   # 15:10
        job_post_market() if trade_day else logger().info("非交易日，跳过收盘流水线")


def _print_status(log: Any) -> None:
    now_str = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        qmt_on = (config.get("broker_adapter_enabled") and config.get("qmt_enabled")
                  and config.get("broker", {}).get("enabled"))
        if qmt_on:
            from src.qmt_adapter import QMTBrokerAdapter
            adapter = QMTBrokerAdapter.from_config(config.get("broker", {}))
            adapter.connect()
            account = adapter.query_account()
            positions = adapter.query_positions()
            adapter.disconnect()
            if positions:
                pos_lines = "  ".join(
                    f"{p.ts_code}×{p.volume}股 市值{p.market_value:.0f}元"
                    for p in positions
                )
                log.info("✅ [状态] %s | 程序正常 | 账户%s 可用%.0f元 | 持仓：%s",
                         now_str, account.account_id, account.available_cash, pos_lines)
            else:
                log.info("✅ [状态] %s | 程序正常 | 账户%s 可用%.0f元 | 无持仓",
                         now_str, account.account_id, account.available_cash)
        else:
            log.info("✅ [状态] %s | 程序正常 | QMT未启用", now_str)
    except Exception as e:
        log.error("❌ [状态] %s | QMT连接异常：%s", now_str, e)


def check_qmt_connection() -> None:
    log = logger()
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        if not (config.get("broker_adapter_enabled") and config.get("qmt_enabled") and
                config.get("broker", {}).get("enabled")):
            log.info("QMT 未启用，跳过连接检查")
            return
        from src.qmt_adapter import QMTBrokerAdapter
        adapter = QMTBrokerAdapter.from_config(config.get("broker", {}))
        adapter.connect()
        account = adapter.query_account()
        adapter.disconnect()
        log.info("✅ QMT连接成功：账户 %s，可用资金 %.0f 元",
                 account.account_id, account.available_cash)
    except Exception as e:
        log.error("❌ QMT连接失败：%s", e)


def main() -> None:
    setup()
    log = logger()
    log.info("A_System 守护进程启动（PID %d）", os.getpid() if (os := __import__("os")) else 0)

    def _exit(signum, _frame):
        log.info("收到信号 %d，退出", signum)
        write_heartbeat("stopped")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT, _exit)

    check_qmt_connection()

    # ── 启动时立刻执行平仓检查 ────────────────────────────────────────────────
    log.info("启动检查：扫描逾期/待平仓持仓...")
    try:
        check_and_close_positions()
    except Exception as e:
        log.error("启动平仓检查异常：%s —— 请立即手动检查持仓！", e)

    # ── 启动时检查今日收盘流水线是否已跑，未跑则补跑 ────────────────────────
    try:
        now_bj = now_beijing()
        if is_trade_day(now_bj.date()) and now_bj.time() >= datetime.time(15, 10):
            if not has_post_market_run_today(now_bj.date()):
                log.info("检测到今日收盘流水线未运行，立即补跑...")
                job_post_market()  # 内部已调用 report_next_day_candidates
            else:
                log.info("今日收盘流水线已完成，无需补跑")
                report_next_day_candidates()
    except Exception as e:
        log.error("启动补跑检查异常：%s", e)

    # ── 主循环 ────────────────────────────────────────────────────────────────
    while True:
        write_heartbeat("running")
        now = now_beijing()
        wake_dt, sched_time = next_event(now)
        sleep_secs = (wake_dt - now).total_seconds()
        log.info("下次任务：%s（%.0f 秒后）", wake_dt.strftime("%Y-%m-%d %H:%M"), sleep_secs)

        # 分段睡眠，每5分钟打印一次状态
        slept = 0
        last_status = 0.0
        while slept < sleep_secs:
            chunk = min(60, sleep_secs - slept)
            time.sleep(chunk)
            slept += chunk
            write_heartbeat("sleeping")
            if slept - last_status >= 300:
                last_status = slept
                _print_status(log)

        try:
            run_job(sched_time)
        except Exception as e:
            log.exception("任务执行异常（守护进程继续）：%s", e)

        time.sleep(60)


if __name__ == "__main__":
    main()
