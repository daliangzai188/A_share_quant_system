"""
A_System 量化策略常驻守护进程。

安全设计原则：
1. 平仓逻辑完全独立于数据流水线 —— 数据步骤出错不影响平仓。
2. 每个操作单独 try/except，不因局部错误崩溃。
3. subprocess 设超时上限，防止某步骤挂死导致平仓被跳过。
4. 进程本身崩溃由 start.sh 的外部 watchdog 自动重启。
5. 心跳文件每分钟更新，外部可监控守护进程存活状态。

调度时间表（A 股交易日）：
    09:20  盘前  —— 平仓检查（优先） + 组合状态机 + D监控
    09:30  开盘  —— 刷新组合状态机 + 执行 A/B/C/E2 买入
    14:50  盘中  —— 平仓检查（优先）
    15:10  收盘  —— 数据流水线 + 信号生成

持仓状态：data/processed/positions.json
心跳文件：logs/daemon_heartbeat.txt
"""
from __future__ import annotations

import datetime
import glob
import json
import os
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


def _notify(event: str, title: str, body: str = "", *, level: str = "active",
            call: bool = False) -> None:
    """告警推送（失败安全，绝不影响交易主流程）。

    正文口径：账号仅显示后2位（****03）；金额用「万」单位2位小数；标的可含代码+名称。
    call=True 为重大错误（崩溃/下单失败/平仓失败/账户断连）：除警报式持续响铃外，
    额外再发一条普通通知。两条独立——即便用户在系统权限里关闭了「重要提醒」（警报不响），
    普通通知仍可见，确保不漏掉。
    """
    try:
        from src.notify import notify
        if call:
            # 普通通知（永远可见，不受警报权限影响）
            notify(event, title, body, level="active", call=False)
            # 警报式（持续响铃，静音/勿扰也响，需「重要提醒」权限）
            notify(event, title, body, level="critical", call=True)
        else:
            notify(event, title, body, level=level, call=False)
    except Exception as exc:  # noqa: BLE001
        try:
            get_logger("a_share_quant").warning("告警推送异常：%s", exc)
        except Exception:
            pass


def _mask_account(account_id: str) -> str:
    """账号脱敏：前缀4星号 + 后2位。"""
    acct = str(account_id or "")
    return f"****{acct[-2:]}" if len(acct) >= 2 else f"****{acct}"


def _fmt_wan(amount: float) -> str:
    """金额格式化为「万」单位、2位小数。"""
    try:
        return f"{float(amount) / 10000:.2f}万"
    except Exception:
        return "0.00万"

# ── 常量 ───────────────────────────────────────────────────────────────────────
SCHEDULE = [
    datetime.time(9, 20),   # 盘前：平仓检查 + 组合状态机
    datetime.time(9, 23),   # 集合竞价：按跌停价挂单平仓
    datetime.time(9, 26),   # 集合竞价成交后：同步实盘持仓，刷新今日买入决策
    datetime.time(9, 28),   # 盘前：按卖5价挂单买入
    datetime.time(9, 30),   # 开盘：若9:28未成功则补充买入
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
PENDING_BUY_FILE = PROJECT_ROOT / "data" / "processed" / "pending_premarket_buy.json"
HEARTBEAT_FILE = PROJECT_ROOT / "logs" / "daemon_heartbeat.txt"
D_MONITOR_PID_FILE = PROJECT_ROOT / "logs" / "strategy_d_monitor.pid"
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


def _checklist_data_quality_blocked(target_date: datetime.date) -> bool:
    """检查目标日期 A/B/C checklist 是否因数据质量不足被阻断。"""
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
               buy_date: str, shares: int, buy_price: float, strategy_leg: str,
               exit_n_days: int = 2) -> None:
    positions = load_positions()
    if any(p["order_id"] == order_id for p in positions):
        return
    exit_date = next_n_trade_days(
        datetime.datetime.strptime(buy_date, "%Y%m%d").date(), n=exit_n_days
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


def reduce_position_shares(order_id: str, remaining_shares: int) -> None:
    """部分成交后保留持仓，仅把剩余未卖股数写回，status 维持 open 以便下次继续卖出。"""
    positions = load_positions()
    for p in positions:
        if p["order_id"] == order_id and str(p.get("status", "")).lower() in {"open", "sell_pending"}:
            p["shares"] = int(remaining_shares)
            p["status"] = "open"
    save_positions(positions)


def find_open_position_by_code(ts_code: str, strategy_leg: str | None = None) -> dict[str, Any] | None:
    """按 ts_code（可选 strategy_leg）查找一条未平持仓，用于卖出后回写。"""
    leg = (strategy_leg or "").upper()
    for p in load_positions():
        if str(p.get("status", "")).lower() not in {"open", "sell_pending"} or str(p.get("ts_code", "")) != str(ts_code):
            continue
        if leg and str(p.get("strategy_leg", "")).upper() != leg:
            continue
        return p
    return None


def reconcile_positions_with_broker() -> None:
    """收盘全量对账：比对本地 positions.json(open) 与券商实际持仓，差异告警。

    只读不改账（避免误判自动改账带来风险），发现差异打日志 + Bark 告警，由人工核对。
    覆盖逐单成交确认漏掉的边角：成交确认超时后才成交、手动交易、券商端撤废修正等。
    """
    log = logger()
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        qmt_on = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled")) \
            and bool(config.get("broker", {}).get("enabled"))
    except Exception as e:
        log.error("[收盘对账] 读取配置失败：%s", e)
        return

    if not qmt_on:
        log.info("[收盘对账] QMT 未启用，跳过。")
        return

    log.info("===== 收盘持仓对账 =====")
    broker_cfg = config.get("broker", {})

    # 券商实际持仓（volume>0）
    try:
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            broker_positions = adapter.query_positions()
    except Exception as e:
        log.error("[收盘对账] 查询券商持仓失败：%s", e)
        _notify("reconcile", "⚠️ 收盘对账失败", "查询券商持仓失败，无法核对账实一致，请回终端检查。",
                level="timeSensitive")
        return

    broker_map: dict[str, dict[str, Any]] = {}
    for bp in broker_positions:
        vol = int(getattr(bp, "volume", 0) or 0)
        if vol <= 0:
            continue
        code = str(getattr(bp, "ts_code", ""))
        broker_map[code] = {"volume": vol, "name": str(getattr(bp, "name", ""))}

    # 本地 open 持仓按 ts_code 汇总股数
    local_map: dict[str, dict[str, Any]] = {}
    for p in load_positions():
        if str(p.get("status", "")).lower() not in {"open", "sell_pending"}:
            continue
        code = str(p.get("ts_code", ""))
        entry = local_map.setdefault(code, {"shares": 0, "name": str(p.get("name", ""))})
        entry["shares"] += int(p.get("shares", 0) or 0)

    diffs: list[str] = []
    for code in sorted(set(local_map) | set(broker_map)):
        local_qty = local_map.get(code, {}).get("shares", 0)
        broker_qty = broker_map.get(code, {}).get("volume", 0)
        name = local_map.get(code, {}).get("name") or broker_map.get(code, {}).get("name", "")
        if local_qty == broker_qty:
            continue
        if local_qty > 0 and broker_qty == 0:
            diffs.append(f"{code} {name} 本地{local_qty}股/券商无（已卖未标记或幽灵持仓）")
        elif broker_qty > 0 and local_qty == 0:
            diffs.append(f"{code} {name} 券商{broker_qty}股/本地无（实际持有未记账）")
        else:
            diffs.append(f"{code} {name} 数量不符 本地{local_qty}/券商{broker_qty}")

    if not diffs:
        log.info("✅ [收盘对账] 账实一致：本地与券商持仓完全匹配（共%d只）。", len(broker_map))
        return

    for d in diffs:
        log.warning("⚠️ [收盘对账] 差异：%s", d)
    summary = "；".join(diffs[:5])
    if len(diffs) > 5:
        summary += f"；…共{len(diffs)}处"
    _notify("reconcile", "⚠️ 收盘对账发现差异",
            f"本地与券商持仓不一致：{summary}。请回终端核对并手动修正。",
            level="timeSensitive")


def _confirm_fill(broker_cfg: dict, order_id: str, expected_qty: int, tag: str) -> "OrderFill":
    """轮询确认委托成交情况。受理(accepted)不等于成交(filled)，此处确认真实成交股数与均价。

    - fill_confirm_enabled=False 时返回"乐观全成"（avg_price=0，调用方回退到参考价）。
    - 每次轮询单独加锁、轮询间隔释放锁，不会长时间阻塞账户轮询线程。
    """
    from src.broker_adapter import OrderFill

    log = logger()
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    lt = config.get("live_trade", {})

    if not lt.get("fill_confirm_enabled", True):
        return OrderFill(order_id=str(order_id), filled_qty=int(expected_qty),
                         is_terminal=True, is_filled=True, status_text="确认已禁用")

    timeout = float(lt.get("fill_confirm_timeout_sec", 60))
    poll = max(1.0, float(lt.get("fill_confirm_poll_sec", 3)))
    deadline = time.monotonic() + timeout
    last_fill = OrderFill(order_id=str(order_id))

    while True:
        try:
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                last_fill = adapter.get_order_fill(order_id)
        except Exception as e:
            log.warning("[%s] 成交确认查询异常 order_id=%s：%s", tag, order_id, e)

        if expected_qty > 0 and last_fill.filled_qty >= expected_qty:
            return last_fill
        if last_fill.is_terminal:
            return last_fill
        if time.monotonic() >= deadline:
            log.warning("⚠️ [%s] 成交确认超时（%.0f秒）order_id=%s 已成%d/%d股 状态=%s",
                        tag, timeout, order_id, last_fill.filled_qty, expected_qty,
                        last_fill.status_text)
            return last_fill
        time.sleep(poll)


# ── 平仓检查（最高优先级，独立运行，绝不因其他错误跳过）────────────────────

def _execute_orders_inprocess(
    planned_orders_path: Path | str,
    confirm: str,
    tag: str,
    *,
    allowed_sides: set[str] | None = None,
    allow_t2_close_sell_now: bool = False,
) -> bool:
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
                | risk_text.str.contains("E2_SELL_T2_CLOSE", na=False)
            )
        )
        if t2_close_sell.any():
            skipped = planned_orders[t2_close_sell]
            for _, row in skipped.iterrows():
                log.warning(
                    "⏸️ [%s] 跳过T2收盘卖计划：%s %s planned_action=%s。该类订单只允许14:50平仓流程执行。",
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
        positions = adapter.query_positions()
        open_orders = adapter.query_orders()
        ts_codes = sorted(
            planned_orders.get("ts_code", pd.Series(dtype=str))
            .dropna().astype(str).unique().tolist()
        )
        quote_map = adapter.get_full_tick(ts_codes) if ts_codes else {}
        planned_orders = resize_buy_orders_for_live_account(
            planned_orders=planned_orders,
            account=account,
            quote_map=quote_map,
            current_market_value=account.market_value,
        )

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
                log.warning(
                    "⚠️ [%s] %s %s 被拒绝：%s",
                    tag,
                    r.get("side", ""),
                    r.get("ts_code", ""),
                    explain_reject_reasons(r),
                )
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
            # 卖出（E2平仓）改用买10/买5挂限价，确保成交、避免被动过夜
            if side == "SELL":
                sell_price, sell_label = _pick_sell_limit_price(quote_map.get(str(row["ts_code"])))
                if sell_price > 0:
                    order_price_type = "FIXED_PRICE"
                    order_price = sell_price
                    ref_price = sell_price
                    order_remark = f"{order_remark}|平仓{sell_label}".strip("|")
                    log.info("[%s] %s 平仓挂单取价 %s=%.2f", tag, row["ts_code"], sell_label, sell_price)
            request = OrderRequest(
                ts_code=str(row["ts_code"]),
                broker_code=str(row["broker_code"]),
                side=side,
                quantity=qty,
                price_type=order_price_type,
                price=order_price,
                strategy_name=str(row.get("strategy_name", "A_SYSTEM_ABC")),
                remark=order_remark,
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
                    record_buy(
                        order_id=s["order_id"],
                        ts_code=s["ts_code"],
                        name=s["name"],
                        signal_date=s["signal_date"],
                        buy_date=today_str,
                        shares=fill.filled_qty,
                        buy_price=fill_price,
                        strategy_leg=s["strategy_leg"],
                        exit_n_days=s["exit_n"],
                    )
                    amount = fill.filled_qty * fill_price
                    if fill.filled_qty < s["qty"]:
                        log.warning("⚠️ [%s] %s 买入部分成交 %d/%d股 @%.2f，按实际成交记录持仓。",
                                    tag, s["ts_code"], fill.filled_qty, s["qty"], fill_price)
                        _notify("buy_result", "⚠️ 开仓部分成交",
                                f"{s['ts_code']} {s['name']} 成交{fill.filled_qty}/{s['qty']}股 "
                                f"@{fill_price:.2f} 金额{_fmt_wan(amount)}")
                    else:
                        log.info("✅ [%s] %s 买入全部成交 %d股 @%.2f，已记录持仓。",
                                 tag, s["ts_code"], fill.filled_qty, fill_price)
                        _notify("buy_result", "✅ 开仓成交",
                                f"{s['ts_code']} {s['name']} 买入{fill.filled_qty}股 "
                                f"@{fill_price:.2f} 金额{_fmt_wan(amount)}")
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
                elif fill.filled_qty >= held:
                    mark_position_closed(local_oid, today_str, fill_price)
                    log.info("✅ [%s] %s 卖出全部成交 %d股 @%.2f，已平仓。",
                             tag, s["ts_code"], fill.filled_qty, fill_price)
                elif fill.filled_qty > 0:
                    reduce_position_shares(local_oid, held - fill.filled_qty)
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
) -> "pd.DataFrame":
    """按实盘账户资金和风控上限缩放买入计划，避免用回测初始资金生成超额订单。"""
    if planned_orders.empty or "side" not in planned_orders.columns:
        return planned_orders

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    live_cfg = config.get("live_trade", {})
    max_single_order_amount = float(live_cfg.get("max_single_order_amount", 100000))
    max_position_pct = float(live_cfg.get("max_position_pct", 0.8))
    max_total_position_pct = float(live_cfg.get("max_total_position_pct", 0.8))
    round_lot_size = int(live_cfg.get("round_lot_size", 100))
    cash_buffer = float(live_cfg.get("cash_buffer_amount", 1000))

    total_asset = float(getattr(account, "total_asset", 0.0) or getattr(account, "available_cash", 0.0) or 0.0)
    available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
    market_value = float(current_market_value or 0.0)
    adjusted = planned_orders.copy()

    for idx, row in adjusted.iterrows():
        side = str(row.get("side", "")).upper()
        if side != "BUY":
            continue

        ts_code = str(row.get("ts_code", "")).upper()
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
        total_position_cap = max(0.0, total_asset * max_total_position_pct - market_value)
        allowed_amount = min(cash_cap, single_position_cap, total_position_cap, max_single_order_amount)

        old_qty = to_int(row.get("round_lot_shares", row.get("estimated_shares", 0)))
        max_qty = int((allowed_amount - 0.01) / price) if allowed_amount > 0 else 0
        if round_lot_size > 0:
            max_qty -= max_qty % round_lot_size
        new_qty = min(old_qty, max_qty)
        if round_lot_size > 0:
            new_qty -= new_qty % round_lot_size
        new_qty = max(0, new_qty)
        new_amount = new_qty * price

        adjusted.at[idx, "estimated_shares"] = new_qty
        adjusted.at[idx, "round_lot_shares"] = new_qty
        adjusted.at[idx, "planned_amount_by_equity"] = new_amount
        adjusted.at[idx, "planned_equity"] = total_asset
        if total_asset > 0:
            adjusted.at[idx, "planned_position_pct"] = new_amount / total_asset

        if new_qty < old_qty:
            adjusted.at[idx, "risk_flags"] = append_risk_flag(
                row.get("risk_flags", ""),
                f"LIVE_SIZE_ADJUSTED:{old_qty}->{new_qty}",
            )
            logger().warning(
                "实盘缩单：%s 原计划%d股，按账户资金/仓位/单笔上限调整为%d股；"
                "可用资金%.0f元，总资产%.0f元，当前市值%.0f元，单笔上限需小于%.0f元，参考成交价%.2f元",
                ts_code,
                old_qty,
                new_qty,
                available_cash,
                total_asset,
                market_value,
                max_single_order_amount,
                price,
            )

    return adjusted


def append_risk_flag(current: Any, flag: str) -> str:
    text = "" if current is None or str(current) == "nan" else str(current)
    return flag if not text else f"{text}|{flag}"


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


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
    max_position_pct = float(max_cfg.get("max_position_pct", 0.8))
    max_total_position_pct = float(max_cfg.get("max_total_position_pct", 0.8))
    max_single_order_amount = float(max_cfg.get("max_single_order_amount", 100000))

    parts = []
    for code in [item for item in raw.split("|") if item]:
        if code == "OUTSIDE_TRADING_TIME":
            parts.append(f"不在允许交易时间内；{side} 只允许 09:30-11:30、13:00-14:55/15:00，当前任务若在09:20会被拒")
        elif code == "EXCEED_POSITION_PCT":
            cap = total_asset * max_position_pct
            parts.append(f"超过单票仓位上限：订单约{estimated_amount:.0f}元，上限={total_asset:.0f}×{max_position_pct:.0%}={cap:.0f}元")
        elif code == "EXCEED_TOTAL_POSITION_PCT":
            cap = total_asset * max_total_position_pct - current_market_value
            parts.append(f"超过总仓位上限：订单约{estimated_amount:.0f}元，剩余额度约{cap:.0f}元")
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


def _pick_sell_limit_price(quote: Any) -> tuple[float, str]:
    """平仓挂单取价：优先买10（有10档盘口时），否则买5；都没有再退买1/最新价。

    挂得越深越能吃穿买盘、越确保成交（代价是成交价略差）。返回 (价格, 档位标签)。
    """
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


def _abc_place_sell_order_direct(
    ts_code: str, name: str, shares: int, order_id: str,
    confirm: str, config: dict, broker_cfg: dict
) -> bool:
    """A/B/C 持仓直接挂 FIXED_PRICE 卖单（优先买10/买5确保成交），不走 CSV 流水线。

    下单受理后确认真实成交：全成→平仓回写；部成→保留剩余股数；未成→保留持仓。
    返回 True 仅表示全部成交。
    """
    from src.broker_adapter import OrderRequest
    from src.live_order_gateway import LiveOrderGateway

    today_str = today_beijing().strftime("%Y%m%d")
    log = logger()

    gateway = LiveOrderGateway(PROJECT_ROOT / "config" / "config.json")
    try:
        gateway.assert_real_order_allowed(confirm)
    except RuntimeError as e:
        log.error("❌ [ABC平仓] 下单条件不满足：%s", e)
        return False

    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        quote_map = adapter.get_full_tick([ts_code])

    quote = quote_map.get(ts_code)
    price, price_label = _pick_sell_limit_price(quote)
    if price <= 0:
        log.warning("ABC平仓：%s 无法获取价格，跳过本次。", ts_code)
        return False

    if shares <= 0:
        log.error("ABC平仓：%s 持仓股数为0，跳过。", ts_code)
        return False

    log.warning("⏳ [ABC平仓] %s %s  %d股  %s=%.2f元", ts_code, name, shares, price_label, price)

    request = OrderRequest(
        ts_code=ts_code,
        broker_code=ts_code,
        side="SELL",
        quantity=shares,
        price_type="FIXED_PRICE",
        price=price,
        strategy_name="A_SYSTEM_ABC",
        remark=f"ABC平仓-{price_label}-{today_str}",
    )
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        result = adapter.place_order(request)

    if not result.accepted:
        log.error("❌ [ABC平仓] %s %s 提交失败：%s", ts_code, name, result.message)
        return False

    log.info("✅ [ABC平仓] %s %s %d股 @%.2f 委托已受理（待成交确认）", ts_code, name, shares, price)
    order_id_broker = str(result.order_id or f"abc-sell-{today_str}-{ts_code}")
    fill = _confirm_fill(broker_cfg, order_id_broker, shares, "ABC平仓")
    fill_price = fill.avg_price if fill.avg_price > 0 else price
    if fill.filled_qty >= shares:
        mark_position_closed(order_id, today_str, fill_price)
        log.info("✅ [ABC平仓] %s %s 全部成交 %d股 @%.2f，已平仓。", ts_code, name, fill.filled_qty, fill_price)
        return True
    elif fill.filled_qty > 0:
        reduce_position_shares(order_id, shares - fill.filled_qty)
        log.warning("⚠️ [ABC平仓] %s %s 部分成交 %d/%d股 @%.2f，剩余%d股保留待下次卖出。",
                    ts_code, name, fill.filled_qty, shares, fill_price, shares - fill.filled_qty)
        _notify("sell_fail", "⚠️ 平仓部分成交",
                f"{ts_code} {name} 卖出{fill.filled_qty}/{shares}股 @{fill_price:.2f}，"
                f"剩余{shares - fill.filled_qty}股仍持有，请回终端查看。", level="timeSensitive")
        return False
    else:
        log.error("❌ [ABC平仓] %s %s 未成交（状态=%s），持仓保留，等待下次重试或手动处理。",
                  ts_code, name, fill.status_text)
        _notify("sell_fail", "❌ 平仓未成交",
                f"{ts_code} {name} 平仓委托未成交，可能被动过夜，请立即回终端处理。",
                level="critical", call=True)
        return False


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
            broker_cfg = config.get("broker", {})
            strategy_leg = str(pos.get("strategy_leg", "")).upper()
            if strategy_leg == "E2":
                # E2 卖出计划单由 combined_planned_orders 生成（包含 PLAN_SELL_T2_CLOSE 行）
                # 成交确认与持仓回写由 _execute_orders_inprocess 内部完成
                combined_path = (
                    PROJECT_ROOT / "reports" / "live_trade" / "combined"
                    / f"combined_planned_orders_{today_str}.csv"
                )
                _execute_orders_inprocess(
                    combined_path,
                    confirm,
                    "E2平仓",
                    allowed_sides={"SELL"},
                    allow_t2_close_sell_now=True,
                )
            elif strategy_leg in {"A", "B", "C"}:
                # ABC planned_orders 文件只有BUY行，必须直接下单
                # 成交确认与持仓回写由 _abc_place_sell_order_direct 内部完成
                _abc_place_sell_order_direct(ts_code, name, shares, order_id, confirm, config, broker_cfg)
            else:
                # D 策略已在 9:23 集合竞价卖出，不应再进入此分支
                logger().warning(
                    "[平仓] 策略=%s 不匹配已知分支（%s %s），跳过，请手动确认。",
                    strategy_leg, ts_code, name,
                )
        else:
            logger().info("[平仓] 模拟盘：%s %s 标记已平仓", ts_code, name)
            mark_position_closed(order_id, today_str)
    except Exception as e:
        logger().error("❌ [平仓] 执行异常（%s %s）：%s —— 请立即手动检查！", ts_code, name, e)
        _notify("sell_fail", "❌ 平仓执行异常",
                f"{ts_code} {name} 平仓过程出现异常，持仓可能未正确卖出，请立即回终端检查。",
                level="critical", call=True)


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
            strategy_leg = str(pos.get("strategy_leg", "")).upper()
            logger().warning("需要平仓：%s %s  计划平仓日 %s  状态 %s  市场开盘 %s",
                             ts_code, name, planned_exit, status, market_is_open())

            t2_close_leg = strategy_leg in {"A", "B", "C", "E2"}
            due_today = planned_exit == today_str
            before_close_sell_window = now_beijing().time() < datetime.time(14, 50)
            if t2_close_leg and due_today and before_close_sell_window:
                logger().warning(
                    "T2收盘卖门禁：%s %s 策略=%s 今日到期，但当前未到14:50，保持持仓不提前平仓。",
                    ts_code,
                    name,
                    strategy_leg,
                )
                continue

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

def job_premarket_sell() -> None:
    """09:23 集合竞价：仅对 D 策略持仓按跌停价挂单平仓。

    D 策略回测平仓价为 next_open（T+1 开盘价），集合竞价清算价≈开盘价，与回测一致。
    E2/ABC 回测平仓价为 T+2 收盘价，不在此处处理，由 14:55 job_afternoon 执行。
    """
    logger().info("===== 集合竞价平仓挂单（09:23）=====")
    positions = load_positions()
    if not positions:
        logger().info("09:23 无持仓，跳过集合竞价平仓。")
        return

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))
    today_str = today_beijing().strftime("%Y%m%d")
    force_d_sell_codes: set[str] = set()
    combined = load_combined_decisions()
    decisions = combined[0] if combined is not None else None
    if decisions is not None and not decisions.empty and {"action", "strategy_leg"}.issubset(decisions.columns):
        rows = decisions[
            (decisions["action"].astype(str) == "PLAN_SELL_D_FIRST")
            & (decisions["strategy_leg"].astype(str).str.upper() == "D")
        ]
        force_d_sell_codes = set(rows.get("ts_code", []).dropna().astype(str).tolist())

    for pos in positions:
        try:
            if pos.get("status") == "closed":
                continue
            ts_code = pos.get("ts_code", "")
            name    = pos.get("name", "")
            shares  = int(pos.get("shares", 0))
            planned_exit = pos.get("planned_exit_date", "99991231")
            strategy_leg = str(pos.get("strategy_leg", "")).upper()

            # 只处理 D 策略：E2/ABC 回测用收盘价，不在集合竞价提前卖出
            if strategy_leg != "D":
                logger().info(
                    "09:23 %s %s 策略=%s，回测用收盘价平仓，跳过集合竞价，等待14:55。",
                    ts_code, name, strategy_leg or "未知",
                )
                continue

            force_relay_sell = ts_code in force_d_sell_codes

            # 只处理今天到期、已标记 sell_pending，或因A/B/C接力需要T+1开盘先卖的D持仓
            if planned_exit > today_str and pos.get("status") != "sell_pending" and not force_relay_sell:
                logger().info("09:23 持仓 %s %s 计划平仓日 %s，今日无需平仓，跳过。", ts_code, name, planned_exit)
                continue
            if force_relay_sell and planned_exit > today_str:
                logger().warning(
                    "09:23 D接力平仓：%s %s 默认计划平仓日%s，但今日有A/B/C接力买入计划，按回测口径T+1开盘先卖D。",
                    ts_code, name, planned_exit,
                )

            if not qmt_enabled:
                logger().info("[集合竞价平仓] 模拟盘：%s %s 将在开盘时平仓", ts_code, name)
                continue

            broker_cfg = config.get("broker", {})
            confirm    = config.get("live_trade", {}).get("real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED")

            with _qmt_lock:
                adapter   = _qmt_get(broker_cfg)
                quote_map = adapter.get_full_tick([ts_code])

            quote       = quote_map.get(ts_code)
            lower_limit = float(getattr(quote, "lower_limit", 0.0) or 0.0) if quote else 0.0

            if lower_limit <= 0:
                logger().warning("09:22 %s %s 无法获取跌停价，跳过集合竞价平仓，等待09:30。", ts_code, name)
                continue

            logger().warning("⏳ [集合竞价平仓] %s %s  %d股  跌停价%.2f元", ts_code, name, shares, lower_limit)

            from src.broker_adapter import OrderRequest
            request = OrderRequest(
                ts_code=ts_code,
                broker_code=ts_code,
                side="SELL",
                quantity=shares,
                price_type="FIXED_PRICE",
                price=lower_limit,
                strategy_name="A_SYSTEM_PREMARKET_SELL",
                remark=f"集合竞价平仓-{today_str}",
            )
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                result  = adapter.place_order(request)

            if result.accepted:
                # 集合竞价9:25才撮合，此处不立即标记平仓，由09:26持仓同步按实盘实际成交确认
                logger().info(
                    "✅ [集合竞价平仓] %s %s 委托已受理（order_id=%s），等待09:25撮合，09:26按实盘确认。",
                    ts_code, name, result.order_id,
                )
            else:
                logger().error("❌ [集合竞价平仓] %s %s 提交失败：%s，等待后续平仓任务处理。", ts_code, name, result.message)

        except Exception as e:
            logger().error("集合竞价平仓异常（%s）：%s", pos.get("ts_code"), e)

    logger().info("===== 集合竞价平仓挂单完成 =====")


def job_premarket_position_sync() -> None:
    """09:26 集合竞价成交确认：对比实盘持仓与本地持仓，若实盘已无某标的则同步标记平仓，并刷新今日组合决策。

    9:25集合竞价撮合完成后，券商持仓会更新。若9:23挂单的卖出已成交，本地标记为closed，
    再重新运行组合状态机，让9:28买入任务能看到正确的空仓+有买入计划的决策。
    """
    logger().info("===== 盘前持仓同步（09:26）=====")

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))
    today_str = today_beijing().strftime("%Y%m%d")

    if not qmt_enabled:
        logger().info("[盘前持仓同步] 模拟盘，跳过实盘查询。")
        logger().info("===== 盘前持仓同步完成 =====")
        return

    broker_cfg = config.get("broker", {})
    try:
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            live_positions = adapter.query_positions()
        live_codes = {
            str(p.ts_code)
            for p in live_positions
            if getattr(p, "volume", 0) and int(getattr(p, "volume", 0)) > 0
        }
    except Exception as e:
        logger().error("[盘前持仓同步] 查询实盘持仓失败：%s", e)
        logger().info("===== 盘前持仓同步完成 =====")
        return

    local_positions = load_positions()
    synced_any = False
    for pos in local_positions:
        if pos.get("status") != "open":
            continue
        # 只同步 D 策略持仓（D策略在9:23卖出，E2/ABC在14:55卖出不在此处）
        if str(pos.get("strategy_leg", "")).upper() != "D":
            continue
        ts_code = str(pos.get("ts_code", ""))
        if ts_code and ts_code not in live_codes:
            logger().info(
                "✅ [盘前持仓同步] %s %s (D策略) 实盘持仓已消失（集合竞价成交），本地标记已平仓。",
                ts_code, pos.get("name", ""),
            )
            mark_position_closed(pos.get("order_id", ""), today_str)
            synced_any = True

    if not synced_any:
        logger().info("[盘前持仓同步] 无需同步（本地与实盘持仓一致）。")
        logger().info("===== 盘前持仓同步完成 =====")
        return

    # 持仓已更新，重新运行组合状态机刷新今日决策
    logger().info("[盘前持仓同步] 持仓已更新，重新生成今日组合决策...")
    try:
        from src.combined_live_engine import CombinedLiveEngine
        engine = CombinedLiveEngine(PROJECT_ROOT)
        engine.run()
        logger().info("✅ [盘前持仓同步] 组合决策已刷新，9:28买入任务将使用最新决策。")
    except Exception as e:
        logger().error("[盘前持仓同步] 刷新组合决策失败：%s，9:28将沿用旧决策。", e)

    logger().info("===== 盘前持仓同步完成 =====")


def job_premarket_buy() -> None:
    """09:28 盘前挂单：对计划开仓且当前无持仓的标的，按卖5价挂限价买单。"""
    logger().info("===== 盘前买入挂单（09:28）=====")

    if has_open_local_position():
        logger().info("09:28 已有本地持仓，跳过盘前买入。")
        return

    combined = load_combined_decisions()
    decisions          = combined[0] if combined is not None else None
    combined_orders_path = combined[1] if combined is not None else None
    if decisions is None:
        logger().error("09:28 组合状态机决策获取失败，跳过盘前买入。")
        return

    has_buy_plan = (
        has_combined_action(decisions, "ALLOW_ABC_BUY_PREVIEW") or
        has_combined_action(decisions, "ALLOW_E2_BUY")
    )
    if not has_buy_plan:
        logger().info("09:28 今日无开仓计划，跳过盘前买入。")
        return

    if has_combined_action(decisions, "PLAN_SELL_D_FIRST"):
        logger().info("09:28 组合状态机要求先卖D，跳过盘前买入。")
        return

    config     = load_json_config(PROJECT_ROOT / "config" / "config.json")
    qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))
    if not qmt_enabled:
        logger().info("[盘前买入] 模拟盘，跳过实盘挂单。")
        return

    import pandas as pd
    if combined_orders_path is None or not combined_orders_path.exists():
        logger().error("09:28 找不到计划单文件，跳过盘前买入。")
        return
    try:
        orders = pd.read_csv(combined_orders_path)
    except Exception as e:
        logger().error("09:28 读取计划单失败：%s", e)
        return

    buy_orders = orders[orders.get("side", pd.Series()).astype(str).str.upper() == "BUY"]
    if buy_orders.empty:
        logger().info("09:28 计划单中无买入行，跳过。")
        return

    broker_cfg = config.get("broker", {})
    confirm    = config.get("live_trade", {}).get("real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED")
    today_str  = today_beijing().strftime("%Y%m%d")

    from src.broker_adapter import OrderRequest
    from src.live_order_gateway import LiveOrderGateway
    gateway = LiveOrderGateway(PROJECT_ROOT / "config" / "config.json")
    try:
        gateway.assert_real_order_allowed(confirm)
    except RuntimeError as e:
        logger().error("❌ [盘前买入] 下单条件不满足：%s", e)
        return

    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        account = adapter.query_account()
        positions_live = adapter.query_positions()

    ts_codes = buy_orders["ts_code"].dropna().astype(str).tolist()
    with _qmt_lock:
        adapter   = _qmt_get(broker_cfg)
        quote_map = adapter.get_full_tick(ts_codes)

    # 按账户资金缩放订单
    buy_orders = resize_buy_orders_for_live_account(
        planned_orders=buy_orders,
        account=account,
        quote_map=quote_map,
        current_market_value=account.market_value,
    )

    pending_buys: list[dict[str, Any]] = []
    for _, row in buy_orders.iterrows():
        try:
            ts_code  = str(row["ts_code"])
            name_s   = str(row.get("name", ""))
            qty      = int(row.get("round_lot_shares", 0))
            if qty <= 0:
                continue

            quote    = quote_map.get(ts_code)
            ask_prices = getattr(quote, "ask_prices", None) if quote else None

            if ask_prices and len(ask_prices) >= 5 and ask_prices[4] > 0:
                price = round(ask_prices[4], 2)
                price_label = "卖5"
            elif ask_prices and len(ask_prices) >= 1 and ask_prices[0] > 0:
                price = round(ask_prices[0], 2)
                price_label = "卖1（卖5不可用）"
            elif quote and getattr(quote, "last_price", 0) > 0:
                price = round(float(quote.last_price), 2)
                price_label = "最新价（五档不可用）"
            else:
                logger().warning("09:28 %s %s 无法获取卖档价格，跳过。", ts_code, name_s)
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
            )
            with _qmt_lock:
                adapter = _qmt_get(broker_cfg)
                result  = adapter.place_order(request)

            if result.accepted:
                # 盘前挂单09:30开盘才撮合，此处不立即记录持仓，落盘待确认，09:30按实盘成交确认
                raw_exit_n = row.get("exit_n_days", None)
                exit_n = int(float(raw_exit_n)) if raw_exit_n is not None and str(raw_exit_n) not in {"", "nan"} else 2
                pending_buys.append({
                    "order_id": str(result.order_id or f"premarket-{today_str}-{ts_code}"),
                    "ts_code": ts_code,
                    "name": name_s,
                    "signal_date": str(row.get("signal_date", "")),
                    "strategy_leg": str(row.get("strategy_leg", "")),
                    "qty": qty,
                    "ref_price": price,
                    "exit_n": exit_n,
                })
                logger().info("✅ [盘前买入] %s %s %d股 @%.2f 委托已受理（待09:30开盘确认成交）",
                              ts_code, name_s, qty, price)
            else:
                logger().error("❌ [盘前买入] %s %s 提交失败：%s", ts_code, name_s, result.message)

        except Exception as e:
            logger().error("盘前买入异常（%s）：%s", row.get("ts_code"), e)

    if pending_buys:
        save_pending_buys(pending_buys)
    logger().info("===== 盘前买入挂单完成（受理%d笔，待开盘确认）=====", len(pending_buys))


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

    # ② D 待卖持仓最高优先级。09:20只播报，等待09:23集合竞价平仓入口执行。
    if has_combined_action(decisions, "PLAN_SELL_D_FIRST"):
        logger().info("组合状态机要求先卖D；等待09:23集合竞价平仓，不在09:20非交易时段提交委托。")
        return

    # ③ A/B/C 买入信号 —— 09:20 只播报，不提交，避免触发 OUTSIDE_TRADING_TIME
    if has_combined_action(decisions, "ALLOW_ABC_BUY_PREVIEW"):
        logger().info("组合状态机允许A/B/C买入；将于09:30交易时段内执行开仓预览/下单。")
    else:
        logger().info("组合状态机未允许A/B/C买入，跳过。")

    # ④ E2 T+1 开仓 —— 09:20 只播报，不提交，避免触发 OUTSIDE_TRADING_TIME
    if has_combined_action(decisions, "ALLOW_E2_BUY"):
        logger().info("组合状态机允许E2开仓；将于09:30交易时段内执行开仓预览/下单。")
    else:
        logger().info("组合状态机未允许E2开仓，跳过。")

    # ⑤ 策略D监控 —— 只有无持仓且无A/B/C/E2买入计划时才启动
    if has_combined_action(decisions, "ALLOW_D_INTRADAY_MONITOR"):
        job_strategy_d()
    else:
        logger().info("组合状态机未允许D盘中监控，跳过。")

    logger().info("===== 盘前任务完成 =====")


def job_opening_buy() -> None:
    logger().info("===== 开盘买入任务（09:30）=====")

    # 先确认09:28盘前买单是否在开盘成交，再决定是否需要补单
    try:
        confirm_pending_premarket_buys()
    except Exception as e:
        logger().error("盘前买单成交确认异常：%s —— 请手动核对！", e)

    if has_open_local_position():
        logger().info("09:30 检测到已有本地持仓（09:28盘前买入已成交），跳过重复买入。")
        logger().info("===== 开盘买入任务完成 =====")
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

    attempted_buy = False
    accepted_buy = False
    if has_combined_action(decisions, "ALLOW_ABC_BUY_PREVIEW"):
        attempted_buy = True
        accepted_buy = handle_combined_order_preview(
            combined_orders_path,
            reason="A/B/C 09:30开仓",
            allowed_sides={"BUY"},
        ) or accepted_buy
    else:
        logger().info("组合状态机未允许A/B/C买入，跳过。")

    if has_combined_action(decisions, "ALLOW_E2_BUY"):
        attempted_buy = True
        accepted_buy = handle_combined_order_preview(
            combined_orders_path,
            reason="E2 09:30开仓",
            allowed_sides={"BUY"},
        ) or accepted_buy
    else:
        logger().info("组合状态机未允许E2开仓，跳过。")

    if not attempted_buy:
        logger().info("09:30无A/B/C/E2买入计划。")
    elif not accepted_buy and not has_open_local_position():
        if has_combined_action(decisions, "ALLOW_E2_BUY"):
            logger().warning(
                "09:30 E2开仓未提交成功，启动延迟重试（9:31-13:30，相对开盘涨幅≤2%%）。"
            )
            _start_e2_retry_thread(combined_orders_path, decisions)
        else:
            logger().warning(
                "09:30开仓计划未成交/未提交成功，且账户本地无持仓；释放资金占用，补启动D盘中监控。"
            )
            job_strategy_d()

    logger().info("===== 开盘买入任务完成 =====")


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


def has_open_local_position() -> bool:
    return any(str(p.get("status", "")).lower() in {"open", "sell_pending"} for p in load_positions())


# ── 盘前买单待确认（09:28挂单→09:30开盘成交确认）────────────────────────────

def save_pending_buys(orders: list[dict[str, Any]]) -> None:
    """记录09:28盘前已受理买单，等09:30开盘后确认成交。"""
    try:
        mkdir_p(PENDING_BUY_FILE.parent)
        payload = {"date": today_beijing().strftime("%Y%m%d"), "orders": orders}
        tmp = PENDING_BUY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(PENDING_BUY_FILE)
    except Exception as e:
        logger().error("保存盘前待确认买单失败：%s", e)


def load_pending_buys() -> list[dict[str, Any]]:
    """读取当日盘前待确认买单（非当日的视为过期，忽略）。"""
    try:
        if not PENDING_BUY_FILE.exists():
            return []
        payload = json.loads(PENDING_BUY_FILE.read_text(encoding="utf-8"))
        if payload.get("date") != today_beijing().strftime("%Y%m%d"):
            return []
        return payload.get("orders", [])
    except Exception as e:
        logger().error("读取盘前待确认买单失败：%s", e)
        return []


def clear_pending_buys() -> None:
    try:
        if PENDING_BUY_FILE.exists():
            PENDING_BUY_FILE.unlink()
    except Exception as e:
        logger().error("清除盘前待确认买单失败：%s", e)


def confirm_pending_premarket_buys() -> None:
    """09:30开盘后确认09:28盘前买单成交：全成/部成→按实际记录持仓；未成→撤掉残单，避免与开盘补单重复成交。"""
    pending = load_pending_buys()
    if not pending:
        return

    logger().info("===== 确认盘前买单成交（09:30）共%d笔 =====", len(pending))
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    broker_cfg = config.get("broker", {})
    today_str = today_beijing().strftime("%Y%m%d")

    for s in pending:
        try:
            order_id = str(s.get("order_id", ""))
            ts_code = str(s.get("ts_code", ""))
            qty = int(s.get("qty", 0))
            ref_price = float(s.get("ref_price", 0.0))
            fill = _confirm_fill(broker_cfg, order_id, qty, "盘前买入确认")
            fill_price = fill.avg_price if fill.avg_price > 0 else ref_price
            if fill.filled_qty > 0:
                record_buy(
                    order_id=order_id,
                    ts_code=ts_code,
                    name=str(s.get("name", "")),
                    signal_date=str(s.get("signal_date", "")),
                    buy_date=today_str,
                    shares=fill.filled_qty,
                    buy_price=fill_price,
                    strategy_leg=str(s.get("strategy_leg", "")),
                    exit_n_days=int(s.get("exit_n", 2)),
                )
                name_s = str(s.get("name", ""))
                amount = fill.filled_qty * fill_price
                if fill.filled_qty < qty:
                    logger().warning("⚠️ [盘前买入确认] %s 部分成交 %d/%d股 @%.2f，撤残单。",
                                     ts_code, fill.filled_qty, qty, fill_price)
                    _try_cancel_order(broker_cfg, order_id, ts_code)
                    _notify("buy_result", "⚠️ 盘前开仓部分成交",
                            f"{ts_code} {name_s} 成交{fill.filled_qty}/{qty}股 "
                            f"@{fill_price:.2f} 金额{_fmt_wan(amount)}")
                else:
                    logger().info("✅ [盘前买入确认] %s 全部成交 %d股 @%.2f，已记录持仓。",
                                  ts_code, fill.filled_qty, fill_price)
                    _notify("buy_result", "✅ 盘前开仓成交",
                            f"{ts_code} {name_s} 买入{fill.filled_qty}股 "
                            f"@{fill_price:.2f} 金额{_fmt_wan(amount)}")
            else:
                logger().warning("⚠️ [盘前买入确认] %s 开盘未成交（状态=%s），撤单后转09:30开盘补买。",
                                 ts_code, fill.status_text)
                _try_cancel_order(broker_cfg, order_id, ts_code)
        except Exception as e:
            logger().error("❌ [盘前买入确认] %s 异常：%s —— 请手动核对！", s.get("ts_code"), e)
            _notify("buy_result", "❌ 盘前开仓确认异常",
                    f"{s.get('ts_code','')} 盘前买单成交确认出现异常，请回终端核对持仓。",
                    level="critical", call=True)

    clear_pending_buys()
    logger().info("===== 盘前买单成交确认完成 =====")


def _try_cancel_order(broker_cfg: dict, order_id: str, ts_code: str) -> None:
    try:
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            ok = adapter.cancel_order(order_id)
        logger().info("撤单 %s（%s）请求%s", ts_code, order_id, "已提交" if ok else "失败")
    except Exception as e:
        logger().warning("撤单 %s（%s）异常：%s", ts_code, order_id, e)


def blocks_d_for_opening_plan(decisions) -> bool:
    """识别 D 是否只是被当日 A/B/C/E2 开仓计划占用资金挡住。

    盘中补启动只用于开仓窗口已经过去、且本地无持仓的场景；如果 D 是因为待卖、
    行情时段、风控等原因被挡住，不在这里强行放行。
    """
    if decisions is None or decisions.empty or "action" not in decisions.columns:
        return False
    actions = decisions["action"].astype(str)
    if actions.isin({"ALLOW_ABC_BUY_PREVIEW", "ALLOW_E2_BUY"}).any():
        return True
    if not actions.eq("BLOCK_D_INTRADAY_MONITOR").any():
        return False
    reason_text = ""
    if "reason" in decisions.columns:
        reason_text = " ".join(decisions["reason"].fillna("").astype(str).tolist())
    return any(keyword in reason_text for keyword in ("开仓", "同一资金", "A/B/C", "E2"))


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
                    )
                    recorded_any = True
                except Exception as e:
                    logger().error("记录持仓异常：%s", e)
            return recorded_any

    except Exception as e:
        logger().error("买入信号处理异常：%s", e)
        return False


def job_strategy_d() -> None:
    """09:20 盘前任务后立即启动策略D监控（后台子进程，不阻塞 daemon）。
    监控脚本内部等到09:30开始扫描，10:00起发WATCH提醒，14:00起发BUY信号，14:55自动撤单。
    """
    logger().info("===== 策略D监控启动（盘中后台）=====")
    try:
        if _strategy_d_monitor_running():
            logger().info("策略D监控已在运行，跳过重复启动。")
            return
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
        cmd = [PYTHON, "-u", "-B", str(PROJECT_ROOT / "scripts" / "monitor_strategy_d_intraday.py")]
        if live_order:
            cmd.append("--live-order")
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        # Popen 非阻塞：D 监控脚本自管循环 + 14:55 自动撤单，daemon 继续正常运行
        popen_kwargs: dict[str, Any] = {
            "cwd": PROJECT_ROOT,
            "env": child_env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if sys.platform.startswith("win"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            popen_kwargs["startupinfo"] = startupinfo
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(cmd, **popen_kwargs)
        _start_d_monitor_log_forwarder(proc)
        _start_d_monitor_health_probe(proc)
        mkdir_p(D_MONITOR_PID_FILE.parent)
        D_MONITOR_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
        logger().info(
            "策略D监控已启动（PID %d，live_order=%s，输出已接入主终端日志）",
            proc.pid,
            live_order,
        )
    except Exception as e:
        logger().error("策略D监控启动失败：%s", e)
    logger().info("===== 策略D监控已独立运行，不阻塞主程序 =====")


def stop_strategy_d_monitor(reason: str = "") -> None:
    """停止 D 盘中监控进程，保证它跟随主守护进程生命周期。"""
    try:
        if not D_MONITOR_PID_FILE.exists():
            return
        pid_text = D_MONITOR_PID_FILE.read_text(encoding="utf-8").strip()
        D_MONITOR_PID_FILE.unlink(missing_ok=True)
        if not pid_text:
            return
        pid = int(pid_text)
        if not _pid_is_running(pid):
            logger().info("D策略监控已不在运行，清理PID记录。")
            return
        if sys.platform.startswith("win"):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:
            os.kill(pid, signal.SIGTERM)
        logger().info("D策略监控已关闭（PID %s）%s", pid, f"原因：{reason}" if reason else "")
    except Exception as exc:
        logger().warning("关闭D策略监控失败：%s", exc)


def _start_d_monitor_log_forwarder(proc: subprocess.Popen) -> None:
    """异步转发 D 扫描进程输出到主日志，避免子进程输出阻塞。"""
    def _forward() -> None:
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                text = line.rstrip()
                if text:
                    logger().info("[D扫描] %s", text)
            code = proc.wait(timeout=5)
            logger().info("[D扫描] 进程退出，PID=%s exit_code=%s", proc.pid, code)
        except Exception as exc:
            logger().warning("[D扫描] 输出转发异常：%s", exc)

    threading.Thread(
        target=_forward,
        daemon=True,
        name=f"d-monitor-log-forwarder-{proc.pid}",
    ).start()


def _start_d_monitor_health_probe(proc: subprocess.Popen) -> None:
    """启动后短延迟检查，确认 D 子进程没有静默退出。"""
    def _probe() -> None:
        time.sleep(5)
        code = proc.poll()
        if code is None:
            logger().info("[D扫描] 启动健康检查：进程仍在运行，PID=%s", proc.pid)
        else:
            logger().warning("[D扫描] 启动健康检查：进程已退出，PID=%s exit_code=%s", proc.pid, code)

    threading.Thread(
        target=_probe,
        daemon=True,
        name=f"d-monitor-health-probe-{proc.pid}",
    ).start()


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return str(pid) in output and "INFO:" not in output.upper()
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _strategy_d_monitor_running() -> bool:
    try:
        if not D_MONITOR_PID_FILE.exists():
            return False
        pid_text = D_MONITOR_PID_FILE.read_text(encoding="utf-8").strip()
        pid = int(pid_text)
        if _pid_is_running(pid):
            return True
        D_MONITOR_PID_FILE.unlink(missing_ok=True)
    except Exception:
        return False
    return False


_e2_retry_thread: threading.Thread | None = None


def _e2_retry_running() -> bool:
    global _e2_retry_thread
    return _e2_retry_thread is not None and _e2_retry_thread.is_alive()


def _get_e2_buy_ts_codes(decisions) -> list[str]:
    """从组合决策中提取 ALLOW_E2_BUY 行对应的标的代码。"""
    if decisions is None or decisions.empty or "action" not in decisions.columns:
        return []
    rows = decisions[decisions["action"].astype(str) == "ALLOW_E2_BUY"]
    codes = rows["ts_code"].dropna().astype(str).str.strip().tolist()
    return [c for c in codes if c and c.lower() != "nan"]


def _e2_open_price_ok(ts_code: str, tolerance: float = 0.02) -> bool:
    """当前价相比今日开盘价的涨幅是否在 tolerance 以内。"""
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        broker_cfg = config.get("broker", {})
        with _qmt_lock:
            adapter = _qmt_get(broker_cfg)
            quote_map = adapter.get_full_tick([ts_code])
        quote = quote_map.get(ts_code)
        if quote is None:
            logger().warning("E2延迟开仓：无法获取 %s 行情数据，跳过价格检查", ts_code)
            return False
        open_price = float(getattr(quote, "open_price", 0.0) or 0.0)
        last_price = float(getattr(quote, "last_price", 0.0) or 0.0)
        if open_price <= 0:
            logger().warning("E2延迟开仓：%s 开盘价异常（%.2f），跳过", ts_code, open_price)
            return False
        pct = (last_price - open_price) / open_price
        ok = pct <= tolerance
        logger().info(
            "E2延迟开仓价格检查：%s 开盘%.2f 当前%.2f 涨幅%+.2f%% —— %s",
            ts_code, open_price, last_price, pct * 100,
            f"允许（≤{tolerance * 100:.0f}%%）" if ok else f"拒绝（>{tolerance * 100:.0f}%%）",
        )
        return ok
    except Exception as e:
        logger().error("E2延迟开仓价格检查异常：%s", e)
        return False


def _e2_place_order_direct(ts_code: str, name: str, planned_qty: int, signal_date: str,
                            strategy_leg: str, exit_n_days: int,
                            config: dict, broker_cfg: dict, confirm: str) -> bool:
    """按ask5/ask1/last_price挂FIXED_PRICE限价买单，不走CSV→validate流水线。"""
    from src.broker_adapter import OrderRequest
    from src.live_order_gateway import LiveOrderGateway

    today_str = today_beijing().strftime("%Y%m%d")
    log = logger()

    gateway = LiveOrderGateway(PROJECT_ROOT / "config" / "config.json")
    try:
        gateway.assert_real_order_allowed(confirm)
    except RuntimeError as e:
        log.error("❌ [E2延迟开仓] 下单条件不满足：%s", e)
        return False

    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        account = adapter.query_account()
        quote_map = adapter.get_full_tick([ts_code])

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
        log.warning("E2延迟开仓：%s 无法获取价格，跳过本次。", ts_code)
        return False

    # 按账户可用资金计算可买股数，上限为计划股数
    available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
    total_asset = float(getattr(account, "total_asset", 0.0) or available_cash)
    cash_buffer = float(config.get("live_trade", {}).get("cash_buffer_amount", 1000))
    max_single = float(config.get("live_trade", {}).get("max_single_order_amount", 50000))
    max_pct = float(config.get("live_trade", {}).get("max_position_pct", 0.8))
    usable = min(available_cash - cash_buffer, total_asset * max_pct, max_single)
    max_qty_by_cash = int(usable / price) if usable > 0 and price > 0 else 0
    max_qty_by_cash = max_qty_by_cash - (max_qty_by_cash % 100)
    qty = max(0, min(planned_qty, max_qty_by_cash))

    if qty <= 0:
        log.warning("E2延迟开仓：%s 可用资金%.0f元不足以购买（价格%.2f），跳过。",
                    ts_code, available_cash, price)
        return False

    log.warning("⏳ [E2延迟开仓] %s %s  %d股  %s=%.2f元  可用%.0f元",
                ts_code, name, qty, price_label, price, available_cash)

    request = OrderRequest(
        ts_code=ts_code,
        broker_code=ts_code,
        side="BUY",
        quantity=qty,
        price_type="FIXED_PRICE",
        price=price,
        strategy_name="A_SYSTEM_ABC",
        remark=f"E2延迟开仓-{price_label}-{today_str}",
    )
    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        result = adapter.place_order(request)

    if not result.accepted:
        log.error("❌ [E2延迟开仓] %s %s 提交失败：%s", ts_code, name, result.message)
        return False

    log.info("✅ [E2延迟开仓] %s %s %d股 @%.2f 委托已受理（待成交确认）", ts_code, name, qty, price)
    order_id_broker = str(result.order_id or f"e2retry-{today_str}-{ts_code}")
    fill = _confirm_fill(broker_cfg, order_id_broker, qty, "E2延迟开仓")
    fill_price = fill.avg_price if fill.avg_price > 0 else price
    if fill.filled_qty > 0:
        record_buy(
            order_id=order_id_broker,
            ts_code=ts_code,
            name=name,
            signal_date=signal_date,
            buy_date=today_str,
            shares=fill.filled_qty,
            buy_price=fill_price,
            strategy_leg=strategy_leg,
            exit_n_days=exit_n_days,
        )
        amount = fill.filled_qty * fill_price
        if fill.filled_qty < qty:
            log.warning("⚠️ [E2延迟开仓] %s 部分成交 %d/%d股 @%.2f，按实际成交记录持仓。",
                        ts_code, fill.filled_qty, qty, fill_price)
            _notify("buy_result", "⚠️ E2开仓部分成交",
                    f"{ts_code} {name} 成交{fill.filled_qty}/{qty}股 "
                    f"@{fill_price:.2f} 金额{_fmt_wan(amount)}")
        else:
            log.info("✅ [E2延迟开仓] %s %s 全部成交 %d股 @%.2f，已记录持仓。",
                     ts_code, name, fill.filled_qty, fill_price)
            _notify("buy_result", "✅ E2开仓成交",
                    f"{ts_code} {name} 买入{fill.filled_qty}股 "
                    f"@{fill_price:.2f} 金额{_fmt_wan(amount)}")
        return True
    else:
        log.error("❌ [E2延迟开仓] %s %s 未成交（状态=%s），不记录持仓，避免幽灵持仓。",
                  ts_code, name, fill.status_text)
        return False


def _e2_delayed_buy_loop(combined_orders_path, decisions) -> None:
    """后台线程：9:31-13:30 内每60秒检查价格条件，满足则用ask5/last_price挂FIXED_PRICE单。

    价格条件：当前价（或最新价）≤ 今日开盘价 × 1.02。
    午间11:30-13:00使用最新价（ask5为0），QMT接受挂单并于13:00生效。
    超出条件或超过截止时间时放弃并补启动D监控。
    """
    log = logger()
    ts_codes = _get_e2_buy_ts_codes(decisions)
    if not ts_codes:
        log.warning("E2延迟开仓：无法获取标的代码，直接补启动D监控。")
        if not has_open_local_position() and not _strategy_d_monitor_running():
            job_strategy_d()
        return

    # 从decisions中提取买入元信息（stock details）
    import pandas as pd
    rows = decisions[decisions["action"].astype(str) == "ALLOW_E2_BUY"] if not decisions.empty else pd.DataFrame()

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    broker_cfg = config.get("broker", {})
    confirm = config.get("live_trade", {}).get("real_order_confirm_text", "A_SYSTEM_REAL_ORDER_CONFIRMED")
    qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))

    log.info("E2延迟开仓线程启动：标的 %s，每60秒检查一次，截止13:30。", ts_codes)
    RETRY_INTERVAL_S = 60
    CUTOFF = datetime.time(13, 30)
    TOLERANCE = 0.02

    while True:
        time.sleep(RETRY_INTERVAL_S)
        now = now_beijing()

        if has_open_local_position():
            log.info("E2延迟开仓：已有本地持仓，退出重试线程。")
            return

        if now.time() >= CUTOFF:
            log.warning("E2延迟开仓：已过13:30截止时间，放弃开仓，补启动D监控。")
            if not _strategy_d_monitor_running():
                job_strategy_d()
            return

        if not all(_e2_open_price_ok(code, TOLERANCE) for code in ts_codes):
            log.warning("E2延迟开仓：%s 涨幅已超开盘价2%%，放弃开仓，补启动D监控。", ts_codes)
            if not _strategy_d_monitor_running():
                job_strategy_d()
            return

        log.info("E2延迟开仓 %s：价格满足条件，尝试提交...", now.strftime("%H:%M:%S"))

        if not qmt_enabled:
            log.info("[E2延迟开仓] 模拟盘，跳过实盘下单。")
            return

        placed_any = False
        for _, row in rows.iterrows():
            code = str(row.get("ts_code", ""))
            if code not in ts_codes:
                continue
            ok = _e2_place_order_direct(
                ts_code=code,
                name=str(row.get("name", "")),
                planned_qty=int(row.get("quantity", 0)),
                signal_date=str(row.get("signal_date", today_beijing().strftime("%Y%m%d"))),
                strategy_leg=str(row.get("strategy_leg", "E2")),
                exit_n_days=1,
                config=config,
                broker_cfg=broker_cfg,
                confirm=confirm,
            )
            if ok:
                placed_any = True

        if placed_any:
            log.info("E2延迟开仓：提交成功，退出重试线程。")
            return
        log.warning("E2延迟开仓：本次提交未成功，%d秒后重试。", RETRY_INTERVAL_S)


def _start_e2_retry_thread(combined_orders_path, decisions) -> None:
    global _e2_retry_thread
    if _e2_retry_running():
        logger().info("E2延迟开仓线程已在运行，跳过重复启动。")
        return
    _e2_retry_thread = threading.Thread(
        target=_e2_delayed_buy_loop,
        args=(combined_orders_path, decisions),
        daemon=True,
        name="e2-retry",
    )
    _e2_retry_thread.start()
    logger().info("E2延迟开仓线程已启动（后台daemon线程，每60秒检查，最晚13:30放弃）。")


def startup_catchup_strategy_d() -> None:
    """盘中重启守护进程时，补启动错过 09:20 的 D 监控。

    不在这里执行 A/B/C 买入预览，避免盘中重启重复触发开仓动作；
    只读取组合状态机，如果它明确允许 D 才补启动监控。
    对于 E2 开仓：若 9:30 后市场仍开盘（14:00 前），允许延迟重试（涨幅≤2%%）。
    """
    now = now_beijing()
    if not is_trade_day(now.date()):
        return
    if not (datetime.time(9, 20) <= now.time() < datetime.time(14, 55)):
        return
    if _strategy_d_monitor_running():
        logger().info("启动补检：D策略监控已在运行。")
        return
    logger().info("启动补检：当前处于D盘中监控时段，检查是否需要补启动D。")
    combined = load_combined_decisions()
    decisions = combined[0] if combined is not None else None
    combined_orders_path = combined[1] if combined is not None else None
    if decisions is None:
        logger().warning("启动补检：组合状态机决策生成失败，不补启动D。")
        return
    if has_combined_action(decisions, "PLAN_SELL_D_FIRST"):
        logger().info("启动补检：存在D待卖优先动作，不补启动新的D监控。")
        return
    if has_combined_action(decisions, "ALLOW_D_INTRADAY_MONITOR"):
        logger().info("启动补检：组合状态机允许D盘中监控，补启动D。")
        job_strategy_d()
    elif now.time() >= datetime.time(9, 30) and blocks_d_for_opening_plan(decisions) and not has_open_local_position():
        if has_combined_action(decisions, "ALLOW_E2_BUY") and now.time() < datetime.time(13, 30) and not _e2_retry_running():
            logger().warning(
                "启动补检：E2未开仓且当前时间在13:30前，启动延迟开仓重试（涨幅≤2%%）。"
            )
            _start_e2_retry_thread(combined_orders_path, decisions)
        else:
            logger().warning(
                "启动补检：开仓窗口已过，A/B/C/E2计划仍占用D但本地无持仓；释放资金占用，补启动D。"
            )
            job_strategy_d()
    else:
        logger().info("启动补检：组合状态机未允许D盘中监控，跳过补启动。")


def job_afternoon() -> None:
    logger().info("===== 盘中任务（14:50）=====")

    # ① 刷新组合状态机 + combined_planned_orders（含 E2 SELL 行）
    try:
        run_script("run_combined_live_plan.py", timeout=TIMEOUT_ORDER_STEP)
    except Exception as e:
        logger().error("刷新组合状态机失败：%s —— E2平仓可能依赖旧计划单", e)

    # ② 平仓检查（依赖 combined_planned_orders 已更新，E2 SELL 才能正确执行）
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
        ("collect_all_data.py",               "① 采集日线 + 涨停池",             TIMEOUT_DATA_STEP,  "约1分钟"),
        ("clean_collected_data.py",            "② 清洗合并数据",                   TIMEOUT_DATA_STEP,  "约1分钟"),
        ("build_dynamic_features.py",          "③ 市场情绪 / 题材热度",            TIMEOUT_DATA_STEP,  "约1分钟"),
        ("score_limit_up_fill_probability.py", "④ 涨停成交概率打分",               TIMEOUT_DATA_STEP,  "约1分钟"),
        ("analyze_next_day_premium.py",        "⑤ 次日溢价因子",                   TIMEOUT_DATA_STEP,  "约1分钟"),
        ("run_paper_ab_filtered_daily_ops.py", "⑥ A+B+C 信号生成",                TIMEOUT_SIGNAL_STEP,"约1分钟"),
        ("run_strategy_e2_signal.py",          "⑦ E2 信号生成（板块中性小市值）", TIMEOUT_SIGNAL_STEP,"约30秒"),
    ]
    extra_args: dict[str, list[str]] = {
        "collect_all_data.py": ["--start-date", recent_start, "--end-date", target_str, "--require-end-date-limit"],
        "clean_collected_data.py": ["--start-date", recent_start, "--end-date", target_str],
        "run_paper_ab_filtered_daily_ops.py": ["--signal-date", target_str, "--top-n", "10"],
        "run_strategy_e2_signal.py": ["--signal-date", target_str],
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

    if _checklist_data_quality_blocked(target_date):
        logger().warning(
            "⚠️ %s 只有基础涨停口径，A/B/C 已阻断开仓；继续等待完整 limit_list_d，稍后自动重试",
            target_str,
        )
        report_next_day_candidates()
        report_signal_readiness_summary(target_str)
        return False

    logger().info("===== 收盘流水线完成 =====")
    report_next_day_candidates()
    report_signal_readiness_summary(target_str)
    mark_post_market_done(target_date)
    try:
        open_cnt = sum(1 for p in load_positions() if p.get("status") == "open")
        acct_part = ""
        try:
            cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
            if cfg.get("broker_adapter_enabled") and cfg.get("qmt_enabled"):
                with _qmt_lock:
                    adapter = _qmt_get(cfg.get("broker", {}))
                    account = adapter.query_account()
                acct_part = (f"账户{_mask_account(account.account_id)} "
                             f"总资产{_fmt_wan(getattr(account, 'total_asset', 0.0))} ")
        except Exception as acct_exc:
            logger().warning("收盘汇总查询账户失败：%s", acct_exc)
        _notify("daily_summary", "📊 今日收盘汇总",
                f"{acct_part}持仓{open_cnt}笔，收盘流水线已完成，明日计划已生成。")
    except Exception as exc:
        logger().warning("收盘汇总推送异常：%s", exc)
    return True


def _run_post_market_with_retry(end_date: str | None = None) -> None:
    """运行收盘流水线，若完整涨停池未就绪则短间隔重试，直到 23:00。"""
    cutoff_hour = 23
    retry_seconds = 300
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
        next_retry = now + datetime.timedelta(seconds=retry_seconds)
        logger().warning(
            "⚠️ Tushare %s 完整涨停池尚未就绪，%d分钟后（%s）自动重试",
            date_str,
            retry_seconds // 60,
            next_retry.strftime("%H:%M"),
        )
        time.sleep(retry_seconds)


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
            checklist = _load_ab_checklist(signal_date_str)
            if not checklist.empty:
                row = checklist.iloc[0]
                logger().info("  A/B/C 未生成计划单，%s", no_candidate_msg)
                logger().info(
                    "  状态：%s（%s）",
                    row.get("operation_status", ""),
                    row.get("operation_status_desc", "无中文解释，请重跑 run_paper_ab_filtered_daily_ops.py"),
                )
                logger().info(
                    "  筛选：%s（%s）",
                    row.get("selection_status", ""),
                    row.get("selection_status_desc", "无中文解释，请查看 checklist"),
                )
                logger().info(
                    "  漏斗：A候选%s只，B候选%s只，B过滤%s只，C候选%s只，C过滤%s只，最终选中%s只，计划单%s条",
                    row.get("a_candidate_count", 0),
                    row.get("b_candidate_count", 0),
                    row.get("b_rejected_by_filter_count", 0),
                    row.get("c_candidate_count", 0),
                    row.get("c_rejected_by_filter_count", 0),
                    row.get("selected_count", 0),
                    row.get("planned_order_count", 0),
                )
                if str(row.get("next_action", "")):
                    logger().info("  下一步：%s", row.get("next_action", ""))
            else:
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


def _load_ab_checklist(signal_date: str):
    try:
        import pandas as pd

        pattern = str(PROJECT_ROOT / f"reports/paper_trade/ab_filtered_daily_ops/*_{signal_date}_checklist.csv")
        files = sorted(glob.glob(pattern))
        if not files:
            return pd.DataFrame()
        return pd.read_csv(files[-1], low_memory=False)
    except Exception:
        return pd.DataFrame()


def report_signal_readiness_summary(signal_date: str) -> None:
    """启动/收盘后播报数据口径、字段完整性和 A/B/C 筛选结果。"""
    try:
        import pandas as pd

        cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
        strategy_cfg = load_json_config(PROJECT_ROOT / "config" / "strategy_config.json")
        fill_path = PROJECT_ROOT / cfg.get("fill_model", {}).get(
            "output_limit_up_fill_scored_path",
            "data/processed/limit_up_fill_scored.csv",
        )
        requirements = (
            strategy_cfg.get("paper_ab_filtered_strategy", {})
            .get("data_quality_requirements", {})
        )
        required_columns = [str(column) for column in requirements.get("required_columns", [])]

        logger().info("----- 信号就绪审计：%s -----", signal_date)
        _log_market_environment(signal_date)
        if not fill_path.exists():
            logger().warning("  数据口径：❌ 成交概率打标文件不存在：%s", fill_path)
            return

        scored = pd.read_csv(fill_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        daily = scored[scored["trade_date"].astype(str) == str(signal_date)].copy()
        if daily.empty:
            logger().warning("  数据口径：❌ limit_up_fill_scored.csv 没有 %s 记录", signal_date)
            return

        source_counts = _value_counts_text(daily, "limit_data_source")
        quality_counts = _value_counts_text(daily, "limit_data_quality")
        compatible_counts = _value_counts_text(daily, "strategy_compatible")
        missing_columns = [column for column in required_columns if column not in daily.columns]
        empty_columns = [
            column
            for column in required_columns
            if column in daily.columns and daily[column].isna().all()
        ]
        is_full = (
            "limit_data_quality" in daily.columns
            and daily["limit_data_quality"].fillna("").astype(str).eq("full").all()
        )
        is_limit_list_d = (
            "limit_data_source" in daily.columns
            and daily["limit_data_source"].fillna("").astype(str).eq("limit_list_d").all()
        )
        is_compatible = (
            "strategy_compatible" in daily.columns
            and daily["strategy_compatible"].fillna("").astype(str).str.lower().isin({"true", "1"}).all()
        )
        fields_ok = not missing_columns and not empty_columns

        logger().info(
            "  数据口径：%s  rows=%d  source={%s} quality={%s} compatible={%s}",
            "✅ 通过，和历史回测 limit_list_d/full 口径一致" if (is_full and is_limit_list_d and is_compatible and fields_ok) else "❌ 不通过",
            len(daily),
            source_counts,
            quality_counts,
            compatible_counts,
        )
        logger().info(
            "  必需字段：%s  缺失=%s  全空=%s",
            "✅ 齐全" if fields_ok else "❌ 不齐",
            missing_columns or "无",
            empty_columns or "无",
        )
        logger().info(
            "  成交概率：allow_buy_reliable={%s}  is_fill_score_reliable={%s}",
            _value_counts_text(daily, "allow_buy_reliable"),
            _value_counts_text(daily, "is_fill_score_reliable"),
        )
        _log_abc_filter_funnel(signal_date)

        checklist = _load_ab_checklist(signal_date)
        if checklist.empty:
            logger().warning("  A/B/C：⚠️ 未找到 %s checklist，可能尚未生成每日操作台", signal_date)
            return
        row = checklist.iloc[0]
        logger().info(
            "  A/B/C状态：%s（%s）",
            row.get("operation_status", ""),
            row.get("operation_status_desc", "无中文解释，请重跑每日操作台"),
        )
        logger().info(
            "  筛选状态：%s（%s）",
            row.get("selection_status", ""),
            row.get("selection_status_desc", "无中文解释，请重跑每日操作台"),
        )
        logger().info(
            "  漏斗统计：A候选%s B候选%s B过滤%s C候选%s C过滤%s 最终选中%s 计划单%s",
            row.get("a_candidate_count", 0),
            row.get("b_candidate_count", 0),
            row.get("b_rejected_by_filter_count", 0),
            row.get("c_candidate_count", 0),
            row.get("c_rejected_by_filter_count", 0),
            row.get("selected_count", 0),
            row.get("planned_order_count", 0),
        )
        for label, suffix in [("B风险过滤", "b_rejected_by_filter"), ("C风险过滤", "c_rejected_by_filter")]:
            detail = _load_reject_detail(signal_date, suffix)
            if detail:
                logger().info("  %s：%s", label, detail)
        _log_d_status_for_signal(signal_date)
        logger().info("----- 信号就绪审计结束 -----")
    except Exception as exc:
        logger().warning("信号就绪审计失败：%s", exc)


def _log_market_environment(signal_date: str) -> None:
    try:
        import pandas as pd

        sentiment_path = PROJECT_ROOT / "data" / "processed" / "market_sentiment.csv"
        emotion_path = PROJECT_ROOT / "data" / "processed" / "market_emotion_features.csv"
        if not sentiment_path.exists():
            logger().info("  市场环境：未找到 market_sentiment.csv")
            return
        sentiment = pd.read_csv(sentiment_path, dtype={"trade_date": str}, low_memory=False)
        row_df = sentiment[sentiment["trade_date"].astype(str) == str(signal_date)]
        if row_df.empty:
            logger().info("  市场环境：market_sentiment.csv 无 %s 记录", signal_date)
            return
        row = row_df.iloc[0]
        emotion = pd.DataFrame()
        if emotion_path.exists():
            raw_emotion = pd.read_csv(emotion_path, dtype={"trade_date": str}, low_memory=False)
            emotion = raw_emotion[raw_emotion["trade_date"].astype(str) == str(signal_date)]
        market_row = emotion.iloc[0] if not emotion.empty else pd.Series(dtype=object)
        logger().info(
            "  市场环境：market_sentiment=%s  全市场涨停=%s  跌停=%s  连板数=%s  最高板=%s  一字板=%s  炸板/开板=%s",
            row.get("market_sentiment_level", "unknown"),
            row.get("limit_up_count", "NA"),
            market_row.get("market_limit_down_count", "NA"),
            market_row.get("market_chain_count", row.get("limit_up_max_height", "NA")),
            row.get("limit_up_max_height", "NA"),
            row.get("one_word_limit_count", "NA"),
            row.get("opened_limit_count", "NA"),
        )
        logger().info(
            "  分段热度：沪主板%s只/%s，深主板%s只/%s，创业板%s只/%s，科创板%s只/%s，北交所%s只/%s",
            row.get("sh_main_limit_up_count", "NA"),
            row.get("sh_main_market_sentiment_level", "NA"),
            row.get("sz_main_limit_up_count", "NA"),
            row.get("sz_main_market_sentiment_level", "NA"),
            row.get("chi_next_limit_up_count", "NA"),
            row.get("chi_next_market_sentiment_level", "NA"),
            row.get("star_limit_up_count", "NA"),
            row.get("star_market_sentiment_level", "NA"),
            row.get("bj_limit_up_count", "NA"),
            row.get("bj_market_sentiment_level", "NA"),
        )
        logger().info(
            "  环境结论：市场不是数据阻断项；是否开仓由 A/B/C 条件和风险过滤继续决定。D 盘中策略还会在交易时段单独看实时首板/炸板/情绪。"
        )
    except Exception as exc:
        logger().info("  市场环境读取失败：%s", exc)


def _log_abc_filter_funnel(signal_date: str) -> None:
    try:
        import pandas as pd

        from scripts.audit_signal_readiness import filter_trace, stop_point_text
        from scripts.run_paper_ab_filtered_daily_ops import configured_c_conditions
        from scripts.run_paper_ab_filtered_observation_window import configured_b_conditions, condition_text
        from scripts.search_paper_backup_strategy_b import backup_config
        from src.paper_candidate_generator import PaperCandidateGenerator

        strategy_path = PROJECT_ROOT / "config" / "strategy_config.json"
        strategy_cfg = load_json_config(strategy_path)
        base_generator = PaperCandidateGenerator(strategy_path)
        all_candidates = base_generator.load_all_candidates()

        traces = [filter_trace("A主策略", base_generator, all_candidates, signal_date)]

        b_conditions = configured_b_conditions(strategy_cfg)
        b_config = backup_config(strategy_cfg, b_conditions)
        b_generator = PaperCandidateGenerator(strategy_path)
        b_generator.config = b_config
        b_generator.paper_config = b_config.get("paper_candidate", {})
        traces.append(filter_trace(f"B备用策略（{condition_text(b_conditions)}）", b_generator, all_candidates, signal_date))

        c_conditions = configured_c_conditions(strategy_cfg)
        if c_conditions:
            c_config = backup_config(strategy_cfg, c_conditions)
            c_generator = PaperCandidateGenerator(strategy_path)
            c_generator.config = c_config
            c_generator.paper_config = c_config.get("paper_candidate", {})
            traces.append(filter_trace(f"C补位策略（{condition_text(c_conditions)}）", c_generator, all_candidates, signal_date))

        trace = pd.concat(traces, ignore_index=True)
        logger().info("  A/B/C逐层筛选漏斗：")
        for _, row in trace.iterrows():
            logger().info(
                "    %s | %s | 当日 %s -> %s，剔除 %s | %s",
                row.get("strategy_layer", ""),
                row.get("step", ""),
                row.get("signal_date_before", 0),
                row.get("signal_date_after", 0),
                row.get("removed_on_signal_date", 0),
                row.get("description", ""),
            )
            if str(row.get("reason_detail", "")):
                logger().info("      未进入/停留原因：%s", row.get("reason_detail", ""))
        logger().info("  A/B/C停止点：")
        for layer in trace["strategy_layer"].dropna().astype(str).drop_duplicates().tolist():
            logger().info("    %s", stop_point_text(trace, layer))
    except Exception as exc:
        logger().info("  A/B/C逐层筛选漏斗生成失败：%s", exc)


def _log_d_status_for_signal(signal_date: str) -> None:
    try:
        now = now_beijing()
        checklist = _load_ab_checklist(signal_date)
        planned_count = 0
        if not checklist.empty and "planned_order_count" in checklist.columns:
            planned_count = int(float(checklist["planned_order_count"].iloc[0] or 0))
        in_d_start_window = datetime.time(9, 20) <= now.time() <= datetime.time(14, 55)
        if planned_count > 0:
            logger().info("  D策略停止点：组合状态机。原因：今日已有 A/B/C 买入计划，阻断 D 盘中监控，避免同一资金重复占用。")
        elif not in_d_start_window:
            logger().info(
                "  D策略停止点：交易时段。原因：当前不是 D 盘中监控时段。D 只在交易日 09:20 组合状态机允许后启动，09:30后扫描，10:00起WATCH，14:00起BUY，14:55停止/撤单。"
            )
            logger().info(
                "  D策略后续过滤链：组合状态机允许 -> 实时行情扫描 -> 首板且昨日未涨停 -> 当前封涨停 -> 曾炸板至少1次 -> 炸板次数<=3 -> 今日曾涨停数量达到强情绪阈值 -> 14:00后打分选最高分 -> LiveOrderGateway二次风控。"
            )
            logger().info("  D策略明日判断：若 09:20 无持仓且仍无 A/B/C 买入计划，则允许启动 D 盘中监控；能否下单取决于盘中实时过滤。")
        else:
            logger().info("  D策略停止点：组合状态机或实时扫描。当前处于 D 可启动/监控时段，实际是否启动以组合状态机决策明细为准；若已启动，还要看盘中实时基础过滤。")
    except Exception as exc:
        logger().info("  D策略状态读取失败：%s", exc)


def _value_counts_text(data, column: str) -> str:
    if data.empty or column not in data.columns:
        return "无"
    counts = data[column].fillna("").astype(str).value_counts().head(8).to_dict()
    return ", ".join(f"{key or '空'}={value}" for key, value in counts.items())


def _load_reject_detail(signal_date: str, suffix: str) -> str:
    try:
        import pandas as pd

        pattern = str(PROJECT_ROOT / f"reports/paper_trade/ab_filtered_daily_ops/*_{signal_date}_{suffix}.csv")
        files = sorted(glob.glob(pattern))
        if not files:
            return ""
        data = pd.read_csv(files[-1], dtype=str, low_memory=False)
        if data.empty:
            return ""
        row = data.iloc[0]
        code = str(row.get("ts_code", ""))
        name = str(row.get("name", ""))
        reason = str(row.get("reject_reason_desc", row.get("reject_reason", "")))
        detail = str(row.get("risk_reject_detail", ""))
        return f"{code} {name}；{reason}；{detail}"
    except Exception:
        return ""


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
    elif scheduled_time == SCHEDULE[1]:   # 09:23
        job_premarket_sell() if trade_day else logger().info("非交易日，跳过集合竞价平仓")
    elif scheduled_time == SCHEDULE[2]:   # 09:26
        job_premarket_position_sync() if trade_day else logger().info("非交易日，跳过盘前持仓同步")
    elif scheduled_time == SCHEDULE[3]:   # 09:28
        job_premarket_buy() if trade_day else logger().info("非交易日，跳过盘前买入")
    elif scheduled_time == SCHEDULE[4]:   # 09:30
        job_opening_buy() if trade_day else logger().info("非交易日，跳过开盘买入任务")
    elif scheduled_time == SCHEDULE[5]:   # 14:50
        job_afternoon() if trade_day else logger().info("非交易日，跳过盘中任务")
    elif scheduled_time == SCHEDULE[6]:   # 15:10
        if trade_day:
            # 收盘全量对账（只读+告警，独立于数据流水线，先跑）
            try:
                reconcile_positions_with_broker()
            except Exception as e:
                logger().error("收盘对账异常：%s", e)
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
    quote_map: dict = {}
    with _qmt_lock:
        try:
            adapter = _qmt_get(broker_cfg)
            account = adapter.query_account()
            positions = adapter.query_positions()
            live_positions = [p for p in (positions or []) if int(getattr(p, "volume", 0) or 0) > 0]
            if live_positions:
                codes = [p.ts_code for p in live_positions]
                if codes:
                    quote_map = adapter.get_full_tick(codes)
            if _qmt_reconnect_count > 0:
                log.info("✅ QMT连接已恢复（第%d次重连后恢复）", _qmt_reconnect_count)
                _notify("connection", "✅ 账户重连成功", "QMT连接已恢复正常。")
                _qmt_reconnect_count = 0
        except Exception as first_err:
            _qmt_reset()
            _qmt_reconnect_count += 1
            log.warning("⚠️ QMT掉线（第%d次），立刻重连：%s", _qmt_reconnect_count, first_err)
            # 仅在刚断连那一刻告警（叠加节流），避免每轮轮询刷屏
            if _qmt_reconnect_count == 1:
                _notify("connection", "🔌 账户断连", "QMT连接断开，正在自动重连，请关注。",
                        level="critical", call=True)
            try:
                adapter = _qmt_get(broker_cfg)
                account = adapter.query_account()
                positions = adapter.query_positions()
                live_positions = [p for p in (positions or []) if int(getattr(p, "volume", 0) or 0) > 0]
                if live_positions:
                    codes = [p.ts_code for p in live_positions]
                    if codes:
                        quote_map = adapter.get_full_tick(codes)
                log.info("✅ QMT重连成功（第%d次恢复）", _qmt_reconnect_count)
                _notify("connection", "✅ 账户重连成功", "QMT连接已恢复正常。")
                _qmt_reconnect_count = 0
            except Exception as retry_err:
                log.warning("⚠️ QMT重连失败（第%d次），等待下次重试：%s",
                            _qmt_reconnect_count, retry_err)
                # 仅在连续多次失败时告警，避免偶发抖动刷屏（叠加节流）
                if _qmt_reconnect_count >= 3:
                    _notify("system_error", "❌ QMT持续掉线",
                            "QMT连接已连续多次重连失败，实盘下单/平仓可能受影响，请立即检查。",
                            level="critical", call=True)
                return

    now_str = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    acct_id = str(account.account_id or "")
    masked_acct = f"****{acct_id[-2:]}" if len(acct_id) >= 2 else f"****{acct_id}"
    total_asset = float(getattr(account, "total_asset", 0.0) or 0.0)
    live_positions = [p for p in (positions or []) if int(getattr(p, "volume", 0) or 0) > 0]
    if live_positions:
        local_pos_map = {
            lp["ts_code"]: lp
            for lp in load_positions()
            if lp.get("status") == "open"
        }
        pos_parts = []
        for p in live_positions:
            current_price = p.market_value / p.volume
            lp = local_pos_map.get(p.ts_code, {})
            buy_price = float(lp.get("buy_price", 0))

            # 今日涨跌幅（相对昨收）
            quote = quote_map.get(p.ts_code)
            pre_close = float(getattr(quote, "pre_close", 0.0) or 0.0) if quote else 0.0
            if pre_close > 0:
                chg_pct = (current_price - pre_close) / pre_close * 100
                chg_sign = "+" if chg_pct >= 0 else ""
                chg_str = f"涨跌{chg_sign}{chg_pct:.2f}% "
            else:
                chg_str = ""

            if buy_price > 0:
                pnl_pct = (current_price - buy_price) / buy_price * 100
                pnl_sign = "+" if pnl_pct >= 0 else ""
                pos_parts.append(
                    f"{p.ts_code}×{p.volume}股 "
                    f"现价{current_price:.2f} "
                    f"{chg_str}"
                    f"收益{pnl_sign}{pnl_pct:.2f}% "
                    f"市值{p.market_value / 10000:.2f}万"
                )
            else:
                pos_parts.append(
                    f"{p.ts_code}×{p.volume}股 "
                    f"现价{current_price:.2f} "
                    f"{chg_str}"
                    f"市值{p.market_value / 10000:.2f}万"
                )
        log.info("✅ [账户] %s | 账户%s 总资产%.2f万 | 持仓：%s",
                 now_str, masked_acct, total_asset / 10000,
                 "  ".join(pos_parts))
    else:
        log.info("✅ [账户] %s | 账户%s 总资产%.2f万 | 无持仓",
                 now_str, masked_acct, total_asset / 10000)


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
                _notify("connection", "✅ 账户连接成功",
                        f"守护进程启动就绪，QMT已连接，账户{_mask_account(account.account_id)}。")
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
        _notify("system_error", "❌ QMT启动连接失败",
                "守护进程启动时QMT连续5次连接失败，实盘功能不可用，请立即检查。",
                level="critical", call=True)
    except Exception as e:
        log.error("❌ QMT连接失败：%s", e)
        _notify("system_error", "❌ QMT启动连接异常",
                "守护进程启动连接QMT时发生异常，实盘功能不可用，请立即检查。",
                level="critical", call=True)


def main() -> None:
    setup()
    log = logger()
    log.info("A_System 守护进程启动（PID %d）", os.getpid() if (os := __import__("os")) else 0)

    def _exit(signum, _frame):
        log.info("收到信号 %d，退出", signum)
        _notify("system_error", "🔌 守护进程已停止",
                "守护进程被主动停止，实盘自动交易已暂停。如需恢复请重新启动。")
        stop_strategy_d_monitor("主守护进程退出")
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

    # 候选播报和信号审计放后台线程：纯展示，不影响开仓关键路径
    # 后台跑完会自动打印日志，不阻塞 startup_catchup 和 E2/D 补启动
    def _startup_report() -> None:
        try:
            report_next_day_candidates()
            report_signal_readiness_summary(expected_str)
        except Exception as exc:
            log.error("启动候选播报异常：%s", exc)
    threading.Thread(target=_startup_report, daemon=True, name="startup-report").start()

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

    try:
        startup_catchup_strategy_d()
    except Exception as e:
        log.error("启动补检D策略异常：%s", e)

    # ── 主循环 ────────────────────────────────────────────────────────────────
    while True:
        write_heartbeat("running")
        now = now_beijing()
        wake_dt, sched_time = next_event(now)
        sleep_secs = (wake_dt - now).total_seconds()
        log.info("下次任务：%s（%.0f 秒后）", wake_dt.strftime("%Y-%m-%d %H:%M"), sleep_secs)

        # 账户轮询：交易时段10秒/次，非交易时段60秒/次；掉线时立刻重连，后续15秒间隔
        _ACCT_INTERVAL = 60       # 非交易时段间隔
        _ACCT_TRADING = 10        # 交易时段间隔
        _RETRY_INTERVAL = 15      # 掉线重连间隔
        deadline = time.monotonic() + sleep_secs
        last_acct_ts = time.monotonic()
        last_trade_check_ts = 0.0  # 交易时段状态每5秒刷新一次，避免频繁计算
        is_trading = False
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
            if now_ts - last_trade_check_ts >= 5:
                is_trading = market_is_open()
                last_trade_check_ts = now_ts
            if _qmt_reconnect_count == 0:
                interval = _ACCT_TRADING if is_trading else _ACCT_INTERVAL
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
            _notify("system_error", "❌ 定时任务异常",
                    "守护进程某个定时任务执行异常（进程未退出），请回终端查看日志。",
                    level="timeSensitive")

        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise  # 信号处理器(_exit)已发"已停止"通知，这里直接退出
    except KeyboardInterrupt:
        # Windows 下 Ctrl+C 常直接抛 KeyboardInterrupt 而绕过信号处理器，这里补发停止通知
        _notify("system_error", "🔌 守护进程已停止",
                "守护进程被手动中断(Ctrl+C)，实盘自动交易已暂停。如需恢复请重新启动。")
        raise
    except Exception as _fatal:
        try:
            get_logger("a_share_quant").exception("守护进程致命错误，即将退出：%s", _fatal)
        except Exception:
            pass
        _notify("system_error", "🛑 守护进程异常退出",
                "守护进程发生致命错误即将退出，实盘自动交易已停止，请立即检查并重启。",
                level="critical", call=True)
        raise
