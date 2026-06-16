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
import threading
import time
from pathlib import Path
from typing import Any

# resolve() 在 Windows WebDAV 盘符（Z:\）上会展开为 UNC 路径导致文件 I/O 失败，
# 用 absolute() 保留盘符路径。Mac/Linux 上行为相同。
PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.logger import get_logger, setup_logger
from src.utils.config import load_json_config, mkdir_p
from src.utils.time_utils import BEIJING_TZ, now_beijing, today_beijing

# ── 常量 ───────────────────────────────────────────────────────────────────────
SCHEDULE = [
    datetime.time(9, 20),   # 盘前：平仓检查 + 组合状态机
    datetime.time(14, 50),  # 盘中平仓检查
    datetime.time(15, 10),  # 收盘流水线
]
import sys as _sys
import platform as _platform
if _platform.system() == "Windows":
    # Z: 盘上的 .venv/bin/python 是 Mac ARM64 二进制，Windows 无法执行
    PYTHON = _sys.executable
else:
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
    mkdir_p(HEARTBEAT_FILE.parent)
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
    mkdir_p(path.parent)
    path.write_text(now_beijing().isoformat(), encoding="utf-8")


def has_post_market_run_today(date: datetime.date) -> bool:
    """判断今日收盘流水线是否成功产出信号文件。
    以 planned_orders 文件名中的日期为唯一判据——marker 文件在流水线失败时也会写入，不可信。
    """
    import re as _re
    today_str = date.strftime("%Y%m%d")
    pattern = str(PROJECT_ROOT / "reports" / "paper_trade" / "ab_filtered_daily_ops" / "*_planned_orders.csv")
    for f in glob.glob(pattern):
        m = _re.search(r"\d{8}", Path(f).stem)
        if m and m.group() == today_str:
            return True
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


def prev_n_trade_days(date: datetime.date, n: int) -> datetime.date:
    """返回 date 之前第 n 个交易日（不含 date 本身）。"""
    cal, max_cal_date = _load_calendar()
    count, d = 0, date
    while count < n:
        d -= datetime.timedelta(days=1)
        d_str = d.strftime("%Y%m%d")
        is_open = (d_str in cal) if (cal and d_str <= max_cal_date) else (d.weekday() < 5)
        if is_open:
            count += 1
    return d


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
    """检查 limit_up_fill_scored.csv 是否包含 target_date 的记录（不导入 pandas）。"""
    path = PROJECT_ROOT / "data" / "processed" / "limit_up_fill_scored.csv"
    if not path.exists():
        return False
    target_str = target_date.strftime("%Y%m%d")
    try:
        with path.open(encoding="utf-8") as f:
            header = f.readline().strip().split(",")
            if "trade_date" not in header:
                return False
            idx = header.index("trade_date")
            for line in f:
                parts = line.split(",")
                if len(parts) > idx:
                    td = parts[idx].strip().strip('"').replace("-", "")[:8]
                    if td == target_str:
                        return True
    except Exception:
        pass
    return False


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
        mkdir_p(POSITIONS_FILE.parent)
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

def _execute_orders_inprocess(
    planned_orders_path: Path | str,
    confirm: str,
    tag: str,
) -> None:
    """进程内单次 QMT 连接完成验证+下单，消除子进程启动和双次连接延迟。"""
    import pandas as pd
    from dataclasses import asdict
    from src.live_order_gateway import LiveOrderGateway
    from src.broker_adapter import OrderRequest

    log = logger()
    config_path = PROJECT_ROOT / "config" / "config.json"
    gateway = LiveOrderGateway(config_path)

    try:
        gateway.assert_real_order_allowed(confirm)
    except RuntimeError as e:
        log.error("❌ [%s] 下单条件不满足：%s", tag, e)
        return

    try:
        _, planned_orders = gateway.load_planned_orders(planned_orders_path)
    except Exception as e:
        log.error("❌ [%s] 读取计划单失败：%s", tag, e)
        return

    if planned_orders.empty:
        log.info("[%s] 计划单为空，跳过", tag)
        return

    broker_cfg = config_path and load_json_config(config_path).get("broker", {})
    with _qmt_lock:
      try:
        adapter = _qmt_get(broker_cfg)
        account = adapter.query_account()
        positions = adapter.query_positions()
        open_orders = adapter.query_orders()
        ts_codes = sorted(
            planned_orders.get("ts_code", pd.Series(dtype=str))
            .dropna().astype(str).unique().tolist()
        )
        quote_map = adapter.get_full_tick(ts_codes) if ts_codes else {}

        preview = gateway.validate_planned_orders(
            planned_orders,
            account.available_cash,
            open_orders,
            quote_map,
            positions=positions,
            account_total_asset=account.total_asset,
            current_market_value=account.market_value,
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
                log.warning("⚠️ [%s] %s %s 被拒绝：%s",
                            tag, r.get("side", ""), r.get("ts_code", ""), r.get("reject_reasons", ""))
            return

        now_str = now_beijing().strftime("%H:%M:%S")
        results = []
        for _, row in executable.iterrows():
            side = str(row["side"]).upper()
            qty = int(row["quantity"])
            ref_price = float(row.get("last_price", 0.0) or row.get("reference_price", 0.0))
            request = OrderRequest(
                ts_code=str(row["ts_code"]),
                broker_code=str(row["broker_code"]),
                side=side,
                quantity=qty,
                price_type=str(row["price_type"]),
                price=float(row.get("price", 0.0)),
                strategy_name=str(row.get("strategy_name", "A_SYSTEM_ABC")),
                remark=str(row.get("remark", "")),
            )
            result = adapter.place_order(request)
            results.append(asdict(result))
            if result.accepted:
                log.info("✅ [%s] %s %s %s %d股 参考价%.2f元 金额%.0f元",
                         tag, now_str, side, row["ts_code"], qty, ref_price, qty * ref_price)
            else:
                log.error("❌ [%s] %s %s %s %d股 失败：%s",
                          tag, now_str, side, row["ts_code"], qty, result.message)

        # 保存提交结果 CSV
        result_csv = PROJECT_ROOT / "reports" / "live_trade" / "qmt_live_order_submitted_orders.csv"
        pd.DataFrame(results).to_csv(result_csv, index=False, encoding="utf-8-sig")

      except Exception as e:
        _qmt_reset()
        log.error("❌ [%s] 下单异常（已重置连接）：%s", tag, e)


def _do_sell(pos: dict[str, Any], qmt_enabled: bool) -> None:
    """对单个持仓执行卖出动作，完全独立、单独 try/except。"""
    ts_code = pos["ts_code"]
    name = pos["name"]
    order_id = pos["order_id"]
    shares = int(pos.get("shares", 0))
    today_str = today_beijing().strftime("%Y%m%d")

    try:
        if qmt_enabled:
            logger().warning("⏳ [准备平仓] %s %s  %d股  计划平仓日 %s",
                             ts_code, name, shares, pos.get("planned_exit_date", ""))
            config = load_json_config(PROJECT_ROOT / "config" / "config.json")
            confirm = config.get("live_trade", {}).get(
                "real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED")
            _execute_orders_inprocess("latest", confirm, "平仓")
            mark_position_closed(order_id, today_str)
        else:
            logger().info("[平仓] 模拟盘：%s %s 标记已平仓", ts_code, name)
            mark_position_closed(order_id, today_str)
    except Exception as e:
        logger().error("❌ [平仓] 执行异常（%s %s）：%s —— 请立即手动检查！", ts_code, name, e)


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
    import platform as _plat
    import queue as _queue
    import threading as _threading
    cmd = [PYTHON, "-u", "-B", str(PROJECT_ROOT / "scripts" / name)] + list(args)
    logger().info("执行: %s", " ".join(cmd))
    kwargs: dict = {
        "cwd": PROJECT_ROOT,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "bufsize": 1,
    }
    env = dict(__import__("os").environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    kwargs["env"] = env
    if _plat.system() == "Windows":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW，禁止弹出新控制台
    try:
        proc = subprocess.Popen(cmd, **kwargs)
        output_lines: list[str] = []
        line_queue: _queue.Queue[str] = _queue.Queue()

        def _reader() -> None:
            if proc.stdout is None:
                return
            for raw_line in proc.stdout:
                if isinstance(raw_line, bytes):
                    try:
                        text = raw_line.decode("utf-8")
                    except UnicodeDecodeError:
                        text = raw_line.decode("gbk", errors="replace")
                else:
                    text = str(raw_line)
                line_queue.put(text.rstrip())

        reader = _threading.Thread(target=_reader, daemon=True)
        reader.start()
        started_at = time.time()

        while proc.poll() is None:
            try:
                line = line_queue.get(timeout=0.5)
            except _queue.Empty:
                line = ""
            if line:
                output_lines.append(line)
                if "| ERROR |" in line or "Traceback" in line or line.startswith("ERROR:"):
                    logger().error("  [%s] %s", name, line)
                else:
                    logger().info("  [%s] %s", name, line)
            if time.time() - started_at > timeout:
                proc.kill()
                logger().error("%s 超时（%ds），已强制终止", name, timeout)
                return False

        while True:
            try:
                line = line_queue.get_nowait()
            except _queue.Empty:
                break
            if line:
                output_lines.append(line)
                if "| ERROR |" in line or "Traceback" in line or line.startswith("ERROR:"):
                    logger().error("  [%s] %s", name, line)
                else:
                    logger().info("  [%s] %s", name, line)

        if proc.returncode != 0:
            logger().error("%s 退出码 %d", name, proc.returncode)
            return False
        return True
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
            _execute_orders_inprocess(planned_orders_path, confirm, "开仓")
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


def job_post_market(end_date: str | None = None) -> None:
    target_str = end_date or today_beijing().strftime("%Y%m%d")
    target_date = datetime.datetime.strptime(target_str, "%Y%m%d").date()
    logger().info("===== 收盘流水线（目标日期 %s）=====", target_str)

    # shift(2)=2天 + 最长连假/断档缓冲=8天 = 10个交易日
    # 历史数据已在 daily_merged.csv，只需追加缺失日期，避免扫描 2019 至今全量文件
    recent_start = prev_n_trade_days(today_beijing(), 10).strftime("%Y%m%d")

    steps = [
        ("collect_all_data.py",               "① 采集日线 + 涨停池",   TIMEOUT_DATA_STEP,  "约1分钟"),
        ("clean_collected_data.py",            "② 清洗合并数据",         TIMEOUT_DATA_STEP,  "约1分钟"),
        ("build_dynamic_features.py",          "③ 市场情绪 / 题材热度",  TIMEOUT_DATA_STEP,  "约1分钟"),
        ("score_limit_up_fill_probability.py", "④ 涨停成交概率打分",     TIMEOUT_DATA_STEP,  "约1分钟"),
        ("analyze_next_day_premium.py",        "⑤ 次日溢价因子",         TIMEOUT_DATA_STEP,  "约1分钟"),
        ("run_paper_ab_filtered_daily_ops.py", "⑥ A+B+C 信号生成",      TIMEOUT_SIGNAL_STEP,"约1分钟"),
    ]
    extra_args: dict[str, list[str]] = {
        "collect_all_data.py": ["--start-date", recent_start, "--end-date", target_str],
        "clean_collected_data.py": ["--start-date", recent_start, "--end-date", target_str],
        "run_paper_ab_filtered_daily_ops.py": ["--top-n", "10"],
    }
    critical_scripts = {
        "collect_all_data.py",
        "clean_collected_data.py",
        "score_limit_up_fill_probability.py",
    }

    for script, desc, timeout, eta in steps:
        try:
            logger().info("%s（%s）", desc, eta)
            args = extra_args.get(script, [])
            ok = run_script(script, *args, timeout=timeout)
            if not ok:
                if script in critical_scripts:
                    logger().warning("%s 第一次失败，等待10秒后自动重试一次", desc)
                    time.sleep(10)
                    ok = run_script(script, *args, timeout=timeout)
                if not ok and script in critical_scripts:
                    logger().error("❌ %s 仍然失败，本次收盘流水线停止；不生成计划单，避免使用旧信号", desc)
                    return False
                if not ok:
                    logger().error("%s 失败，继续后续步骤", desc)
        except Exception as e:
            if script in critical_scripts:
                logger().error("❌ %s 异常：%s，本次收盘流水线停止；不生成计划单，避免使用旧信号", desc, e)
                return False
            logger().error("%s 异常：%s，继续后续步骤", desc, e)

    # 关键检查：今日数据是否真的从 Tushare 入库了
    if not _date_in_scored(target_date):
        logger().warning(
            "⚠️ Tushare %s 数据尚未就绪（limit_up_fill_scored.csv 无该日记录），流水线步骤已完成但需等数据",
            target_str,
        )
        report_next_day_candidates()
        return False  # 通知调用方稍后重试

    if not _has_signal_for_date(target_date):
        logger().warning(
            "A/B/C 未生成 %s 计划单，自动使用当日涨停池生成安全模拟观察计划...",
            target_str,
        )
        fallback_ok = run_script(
            "generate_live_limit_pool_daily_ops.py",
            "--signal-date",
            target_str,
            "--top-n",
            "10",
            timeout=TIMEOUT_SIGNAL_STEP,
        )
        if fallback_ok and _has_signal_for_date(target_date):
            logger().info("✅ 当日涨停池模拟观察计划已生成：signal_date=%s，live_order_enabled=False", target_str)
        else:
            logger().error("❌ 当日涨停池模拟观察计划生成失败：signal_date=%s", target_str)

    logger().info("===== 收盘流水线完成 =====")
    report_next_day_candidates()
    mark_post_market_done(target_date)
    return True


def _run_post_market_with_retry(end_date: str | None = None) -> None:
    """运行收盘流水线，若当日 Tushare 数据未就绪则每小时重试，直到 20:00。"""
    cutoff_hour = 20
    date_str = end_date or today_beijing().strftime("%Y%m%d")
    while True:
        try:
            data_ready = job_post_market(end_date=end_date)
        except Exception as _e:
            logger().error("收盘流水线异常：%s", _e)
            data_ready = False
        if data_ready:
            break
        now = now_beijing()
        if now.hour >= cutoff_hour:
            logger().warning("已过 %d:00，停止等待 Tushare %s 数据，今日收盘流水线结束", cutoff_hour, date_str)
            break
        next_retry = now + datetime.timedelta(hours=1)
        logger().warning(
            "⚠️ Tushare %s 数据尚未就绪，1小时后（%s）自动重试",
            date_str,
            next_retry.strftime("%H:%M"),
        )
        time.sleep(3600)


def _segment_label(ts_code: str, market_segment: str = "") -> str:
    seg_map = {
        "sh_main": "沪市主板",
        "sz_main": "深市主板",
        "chi_next": "创业板",
        "star": "科创板",
        "bj": "北交所",
    }
    if market_segment in seg_map:
        return seg_map[market_segment]
    code = ts_code.split(".")[0] if "." in ts_code else ts_code
    suffix = ts_code.split(".")[-1].upper() if "." in ts_code else ""
    if suffix == "BJ":
        return "北交所"
    if suffix == "SH":
        return "科创板" if code.startswith("688") else "沪市主板"
    if suffix == "SZ":
        return "创业板" if code.startswith("3") else "深市主板"
    return "未知"


def _load_limit_for_codes(ts_codes: list[str]) -> tuple[str, dict[str, dict]]:
    """从 limit_up_merged.csv 读最新交易日的涨停状态和封单金额。
    返回 (最新交易日, {ts_code: {limit, open_times, fd_amount_wan, last_time}})。"""
    try:
        import pandas as pd
        path = PROJECT_ROOT / "data" / "processed" / "limit_up_merged.csv"
        if not path.exists():
            return "", {}
        need = ["ts_code", "trade_date", "limit", "open_times", "fd_amount", "last_time"]
        avail = list(pd.read_csv(path, nrows=0).columns)
        use_cols = [c for c in need if c in avail]
        if "ts_code" not in use_cols or "trade_date" not in use_cols:
            return "", {}
        df = pd.read_csv(path, usecols=use_cols, dtype={"trade_date": str}, low_memory=False)
        latest = str(df["trade_date"].max())
        sub = df[(df["trade_date"] == latest) & (df["ts_code"].isin(ts_codes))]
        result: dict[str, dict] = {}
        for _, r in sub.iterrows():
            t = r.get("last_time", 0)
            try:
                t_int = int(float(t))
                time_str = f"{t_int // 10000:02d}:{(t_int % 10000) // 100:02d}"
            except Exception:
                time_str = str(t)
            result[str(r["ts_code"])] = {
                "limit":          str(r.get("limit", "")),
                "open_times":     int(r.get("open_times", 0) or 0),
                "fd_amount_wan":  float(r.get("fd_amount", 0) or 0) / 10000,  # 元→万元
                "last_time":      time_str,
            }
        return latest, result
    except Exception as e:
        logger().debug("读取涨停数据失败：%s", e)
        return "", {}


def _load_daily_for_codes(ts_codes: list[str]) -> tuple[str, dict[str, dict]]:
    """从 daily_merged.csv 读最新交易日的 close/pct_chg/circ_mv。
    返回 (最新交易日字符串, {ts_code: {...}})。"""
    try:
        import pandas as pd
        path = PROJECT_ROOT / "data" / "processed" / "daily_merged.csv"
        if not path.exists():
            return "", {}
        need = ["ts_code", "trade_date", "close", "pct_chg", "circ_mv"]
        avail = list(pd.read_csv(path, nrows=0).columns)
        use_cols = [c for c in need if c in avail]
        if "ts_code" not in use_cols or "trade_date" not in use_cols:
            return "", {}
        daily = pd.read_csv(path, usecols=use_cols, dtype={"trade_date": str}, low_memory=False)
        latest = str(daily["trade_date"].max())
        sub = daily[(daily["trade_date"] == latest) & (daily["ts_code"].isin(ts_codes))]
        result: dict[str, dict] = {}
        for _, r in sub.iterrows():
            result[str(r["ts_code"])] = {
                "close":   float(r.get("close",   0) or 0),
                "pct_chg": float(r.get("pct_chg", 0) or 0),
                "circ_mv": float(r.get("circ_mv", 0) or 0),  # 万元
            }
        return latest, result
    except Exception as e:
        logger().debug("读取日线数据失败：%s", e)
        return "", {}


def report_next_day_candidates() -> None:
    """读取最新 planned_orders，播报下一交易日开仓候选，附当日行情。"""
    try:
        import pandas as pd
        import re as _re

        now_bj = now_beijing()
        today = today_beijing()
        today_str = today.strftime("%Y%m%d")

        # 收盘后（>=15:10 且是交易日）才要求信号必须是今天的；收盘前用最新缓存即可
        require_today = (
            is_trade_day(now_bj.date())
            and now_bj.time() >= datetime.time(15, 10)
        )

        # 交易日且未收盘：信号来自昨天，操作日=今天 → "今日候选"
        # 已收盘或非交易日：操作日=下一交易日 → "明日候选"
        if is_trade_day(now_bj.date()) and not require_today:
            header_label = "今日候选"
            action_date_str = today.strftime("%Y-%m-%d")
            no_candidate_msg = "今日暂不开仓"
        else:
            header_label = "明日候选"
            action_date_str = next_n_trade_days(today, 1).strftime("%Y-%m-%d")
            no_candidate_msg = "明日暂不开仓"

        pattern = str(PROJECT_ROOT / "reports/paper_trade/ab_filtered_daily_ops/*_planned_orders.csv")
        files = sorted(glob.glob(pattern))

        logger().info("=" * 60)

        if not files:
            logger().warning("【%s】%s  ⚠️  未找到 planned_orders 文件，收盘流水线可能从未成功运行",
                             header_label, action_date_str)
            logger().info("=" * 60)
            return

        latest_file = Path(files[-1])
        _m = _re.search(r"\d{8}", latest_file.stem)
        signal_date_str = _m.group() if _m else "未知"
        # 收盘后必须是今天的信号；收盘前最新缓存就算新鲜
        data_fresh = (signal_date_str == today_str) or (not require_today)

        try:
            orders = pd.read_csv(latest_file)
        except pd.errors.EmptyDataError:
            logger().info("【%s】%s  信号日期：%s", header_label, action_date_str, signal_date_str)
            if data_fresh:
                logger().info("  A/B/C 均无符合条件标的，%s", no_candidate_msg)
            else:
                logger().warning("  ⚠️  数据未更新！信号来自 %s，今日（%s）收盘流水线未成功运行", signal_date_str, today_str)
            logger().info("=" * 60)
            return
        except Exception as e:
            logger().error("  读取 planned_orders 失败（%s）：%s", latest_file.name, e)
            logger().info("=" * 60)
            return

        if "signal_date" in orders.columns and not orders["signal_date"].dropna().empty:
            signal_date_str = str(orders["signal_date"].dropna().iloc[0])

        buy_orders = (
            orders[orders["side"].astype(str).str.upper() == "BUY"].copy()
            if "side" in orders.columns else pd.DataFrame()
        )

        logger().info("【%s】%s  信号日期：%s", header_label, action_date_str, signal_date_str)
        if not data_fresh:
            logger().warning("  ⚠️  数据未更新！信号来自 %s，今日（%s）收盘流水线未成功运行，以下仅供参考", signal_date_str, today_str)

        if buy_orders.empty:
            logger().info("  A/B/C 均无符合条件标的，%s", no_candidate_msg)
        else:
            ts_codes = buy_orders["ts_code"].astype(str).tolist()
            daily_date, daily_map = _load_daily_for_codes(ts_codes)
            _, limit_map = _load_limit_for_codes(ts_codes)
            if daily_date:
                logger().info("  行情基准日：%s", daily_date)
            logger().info("  共 %d 只候选：", len(buy_orders))
            for i, (_, r) in enumerate(buy_orders.iterrows(), 1):
                code     = str(r.get("ts_code", ""))
                name     = str(r.get("name", ""))
                leg      = str(r.get("strategy_leg", ""))
                ref_px   = float(r.get("reference_price", 0.0))
                shares   = int(r.get("round_lot_shares", r.get("estimated_shares", 0)))
                amount   = ref_px * shares
                seg      = _segment_label(code, str(r.get("market_segment", "")))
                d        = daily_map.get(code, {})
                close    = d.get("close", 0.0)
                pct      = d.get("pct_chg", 0.0)
                circ_yi  = d.get("circ_mv", 0.0) / 10000  # 万元→亿元
                pct_sign = "+" if pct >= 0 else ""
                lm       = limit_map.get(code, {})
                is_limit = lm.get("limit", "") == "U"
                open_t   = lm.get("open_times", 0)
                fd_wan   = lm.get("fd_amount_wan", 0.0)
                last_t   = lm.get("last_time", "")

                logger().info("  %d. [%s] %s %s", i, seg, code, name)
                if close > 0:
                    logger().info(
                        "     行情：收盘 %.2f元  涨跌 %s%.2f%%  流通市值 %.1f亿",
                        close, pct_sign, pct, circ_yi,
                    )
                else:
                    logger().info("     行情：暂无（日线数据未采集到该日）")
                if is_limit:
                    open_desc = "一字板" if open_t == 0 else f"炸板{open_t}次"
                    logger().info(
                        "     涨停：✅ 封板中  %s  封单 %.0f万元  最后封板 %s",
                        open_desc, fd_wan, last_t,
                    )
                elif lm:
                    logger().info("     涨停：❌ 当日涨停已炸板（炸板 %d 次），收盘未封住", open_t)
                else:
                    logger().info("     涨停：— 当日未涨停")
                logger().info(
                    "     计划：策略 %s  参考价 %.2f元  %d股  预估 %.0f元",
                    leg, ref_px, shares, amount,
                )

        logger().info("=" * 60)
    except Exception as e:
        logger().error("播报候选异常：%s", e)


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
        if trade_day:
            threading.Thread(
                target=_run_post_market_with_retry,
                daemon=True,
                name="pipeline",
            ).start()
        else:
            logger().info("非交易日，跳过收盘流水线")


_qmt_reconnect_count: int = 0       # 累计重连次数，成功后归零
_qmt_adapter: Any = None             # 持久连接，程序生命周期内保持
_qmt_lock = threading.Lock()         # 保护 _qmt_adapter 并发访问


def _qmt_connect(broker_config: dict) -> Any:
    """建立新 QMT 连接（带20秒超时）。不持有 _qmt_lock，调用方按需加锁。"""
    from src.qmt_adapter import QMTBrokerAdapter
    adapter = QMTBrokerAdapter.from_config(broker_config)
    done = threading.Event()
    err: list = []

    def _do() -> None:
        try:
            adapter.connect()
        except Exception as e:
            err.append(e)
        finally:
            done.set()

    threading.Thread(target=_do, daemon=True).start()
    if not done.wait(20.0):
        raise TimeoutError("QMT 连接超时（20秒无响应）")
    if err:
        raise err[0]
    return adapter


def _qmt_get(broker_config: dict) -> Any:
    """返回持久连接，未连接时建立。调用方须持有 _qmt_lock。"""
    global _qmt_adapter
    if _qmt_adapter is None:
        _qmt_adapter = _qmt_connect(broker_config)
    return _qmt_adapter


def _qmt_reset() -> None:
    """断开并清除持久连接。调用方须持有 _qmt_lock。"""
    global _qmt_adapter
    if _qmt_adapter is not None:
        try:
            _qmt_adapter.disconnect()
        except Exception:
            pass
        _qmt_adapter = None


def _print_account_status(log: Any) -> None:
    """账户信息轮询（后台线程）：复用持久连接，查询无需重新握手。
    真正断线时立刻重连一次，失败则等下次轮询。"""
    global _qmt_reconnect_count
    now_str = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        qmt_on = (config.get("broker_adapter_enabled") and config.get("qmt_enabled")
                  and config.get("broker", {}).get("enabled"))
        if not qmt_on:
            log.info("✅ [账户] %s | 程序正常 | QMT未启用", now_str)
            return
        broker_cfg = config.get("broker", {})
    except Exception as e:
        log.error("❌ 读取配置失败：%s", e)
        return

    account = positions = None
    with _qmt_lock:
        try:
            adapter = _qmt_get(broker_cfg)
            account = adapter.query_account()
            positions = adapter.query_positions()
            if _qmt_reconnect_count > 0:
                log.info("✅ QMT连接已恢复（第%d次重连后恢复）", _qmt_reconnect_count)
                _qmt_reconnect_count = 0
        except Exception as first_err:
            _qmt_reset()
            _qmt_reconnect_count += 1
            log.warning("⚠️ QMT掉线（第%d次），立刻重连：%s", _qmt_reconnect_count, first_err)
            try:
                adapter = _qmt_get(broker_cfg)
                account = adapter.query_account()
                positions = adapter.query_positions()
                log.info("✅ QMT重连成功（第%d次恢复）", _qmt_reconnect_count)
                _qmt_reconnect_count = 0
            except Exception as retry_err:
                log.warning("⚠️ QMT重连失败（第%d次），等待下次重试：%s",
                            _qmt_reconnect_count, retry_err)
                return

    now_str = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    if positions:
        local_pos_map = {
            lp["ts_code"]: lp
            for lp in load_positions()
            if lp.get("status") == "open"
        }
        pos_parts = []
        for p in positions:
            current_price = p.market_value / p.volume if p.volume > 0 else 0.0
            lp = local_pos_map.get(p.ts_code, {})
            buy_price = float(lp.get("buy_price", 0))
            if buy_price > 0:
                pnl_pct = (current_price - buy_price) / buy_price * 100
                pnl_sign = "+" if pnl_pct >= 0 else ""
                pos_parts.append(
                    f"{p.ts_code}×{p.volume}股 "
                    f"现价{current_price:.2f} "
                    f"今日{pnl_sign}{pnl_pct:.2f}% "
                    f"市值{p.market_value:.0f}元"
                )
            else:
                pos_parts.append(
                    f"{p.ts_code}×{p.volume}股 市值{p.market_value:.0f}元"
                )
        log.info("✅ [账户] %s | 账户%s 可用%.0f元 | 持仓：%s",
                 now_str, account.account_id, account.available_cash,
                 "  ".join(pos_parts))
    else:
        log.info("✅ [账户] %s | 账户%s 可用%.0f元 | 无持仓",
                 now_str, account.account_id, account.available_cash)


def check_qmt_connection() -> None:
    log = logger()
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        if not (config.get("broker_adapter_enabled") and config.get("qmt_enabled") and
                config.get("broker", {}).get("enabled")):
            log.info("QMT 未启用，跳过连接检查")
            return
        from src.qmt_adapter import QMTBrokerAdapter

        last_error = ""
        for attempt in range(1, 6):
            adapter = QMTBrokerAdapter.from_config(config.get("broker", {}))
            try:
                adapter.connect()
                account = adapter.query_account()
                adapter.disconnect()
                time.sleep(2)
                log.info(
                    "✅ QMT连接成功：账户 %s，可用资金 %.0f 元（第 %d 次尝试）",
                    account.account_id,
                    account.available_cash,
                    attempt,
                )
                return
            except Exception as e:
                last_error = str(e)
                try:
                    adapter.disconnect()
                except Exception:
                    pass
                if attempt < 5:
                    log.warning("⚠️ QMT暂时未就绪，第 %d/5 次连接失败，15秒后自动重试：%s", attempt, last_error)
                    time.sleep(15)

        log.error("❌ QMT连接失败：连续 5 次失败。最后错误：%s", last_error)
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

    # ── 启动时先播报当前缓存候选，再按需后台补采 ─────────────────────────────
    try:
        expected = _expected_signal_date()
        expected_str = expected.strftime("%Y%m%d")
    except Exception as e:
        log.error("启动数据检查异常：%s", e)
        expected = today_beijing()
        expected_str = expected.strftime("%Y%m%d")

    # 无论如何先打印当前缓存（可能是旧数据，流水线跑完后会再次播报）
    try:
        report_next_day_candidates()
    except Exception as e:
        log.error("启动候选播报异常：%s", e)

    # 若缓存不是最新交易日数据，后台线程补采，不阻塞主循环 QMT 状态刷新
    if not _has_signal_for_date(expected):
        log.warning(
            "未找到 %s 收盘数据缓存，后台自动采集中（不影响主循环和 QMT 状态刷新）...",
            expected_str,
        )
        threading.Thread(
            target=_run_post_market_with_retry,
            args=(expected_str,),
            daemon=True,
            name="pipeline",
        ).start()
    else:
        log.info("已有 %s 收盘数据缓存，直接使用", expected_str)

    # ── 主循环 ────────────────────────────────────────────────────────────────
    while True:
        write_heartbeat("running")
        now = now_beijing()
        wake_dt, sched_time = next_event(now)
        sleep_secs = (wake_dt - now).total_seconds()
        log.info("下次任务：%s（%.0f 秒后）", wake_dt.strftime("%Y-%m-%d %H:%M"), sleep_secs)

        # 账户轮询：有持仓2秒/次，无持仓60秒/次；掉线时立刻重连，后续15秒间隔
        _ACCT_INTERVAL = 60       # 无持仓正常间隔
        _ACCT_WITH_POS = 2        # 有持仓高频间隔
        _RETRY_INTERVAL = 15      # 掉线重连间隔
        deadline = time.monotonic() + sleep_secs
        last_acct_ts = time.monotonic()
        last_pos_check_ts = 0.0   # 持仓状态每10秒刷新一次，避免频繁读文件
        has_open = False
        last_heartbeat_ts = time.monotonic()
        _acct_thread: threading.Thread | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(2, remaining))
            now_ts = time.monotonic()
            if now_ts - last_heartbeat_ts >= 10:
                write_heartbeat("sleeping")
                last_heartbeat_ts = now_ts
            if now_ts - last_pos_check_ts >= 10:
                has_open = any(p.get("status") == "open" for p in load_positions())
                last_pos_check_ts = now_ts
            if _qmt_reconnect_count == 0:
                interval = _ACCT_WITH_POS if has_open else _ACCT_INTERVAL
            elif _qmt_reconnect_count == 1:
                interval = 0
            else:
                interval = _RETRY_INTERVAL
            if now_ts - last_acct_ts >= interval:
                if _acct_thread is None or not _acct_thread.is_alive():
                    last_acct_ts = now_ts
                    _acct_thread = threading.Thread(
                        target=_print_account_status, args=(log,), daemon=True
                    )
                    _acct_thread.start()

        try:
            run_job(sched_time)
        except Exception as e:
            log.exception("任务执行异常（守护进程继续）：%s", e)

        time.sleep(60)


if __name__ == "__main__":
    main()
