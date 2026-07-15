"""
A_System 量化策略常驻守护进程。

安全设计原则：
1. 平仓逻辑完全独立于数据流水线 —— 数据步骤出错不影响平仓。
2. 每个操作单独 try/except，不因局部错误崩溃。
3. subprocess 设超时上限，防止某步骤挂死导致平仓被跳过。
4. 进程本身崩溃由 start.sh 的外部 watchdog 自动重启。
5. 心跳文件每分钟更新，外部可监控守护进程存活状态。

调度时间表（A 股交易日）：
    09:00  盘前计划 —— 生成/刷新组合状态机买入计划
    09:15  集合竞价 —— 已有计划立刻按涨停价预挂买入
    09:20  盘前复核 —— 平仓检查（优先） + 组合状态机复核 + D监控
    09:30  开盘确认 —— 确认09:15预挂成交，未成交再补买
    14:55  收盘平仓 —— 平仓检查（最高优先，绝不被任何步骤阻塞；14:55挂跌停价，留足收盘竞价前的补救窗口）
    14:56  撤买单 —— 撤销所有未成交买单（不动卖单；深市14:57起收盘竞价不可撤单，必须在此之前）
    15:10  收盘  —— 数据流水线 + 信号生成

持仓状态：data/processed/positions.json
心跳文件：logs/daemon_heartbeat.txt
"""
from __future__ import annotations

import datetime
import csv
import glob
import json
import os
import signal
import subprocess
import sys
import threading
import time
from decimal import Decimal, ROUND_HALF_UP
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


def _notify_async(event: str, title: str, body: str = "", *, level: str = "active",
                  call: bool = False) -> None:
    """后台推送通知，避免 Bark/网络响应拖慢 QMT 门禁和交易主流程。"""
    threading.Thread(
        target=_notify,
        args=(event, title, body),
        kwargs={"level": level, "call": call},
        daemon=True,
        name=f"notify-{event}",
    ).start()


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


def _fmt_position_time(value: Any, *, default_time: str = "", trim_zero_seconds: bool = False) -> str:
    """把持仓账本里的日期/时间字段转成日志可读时间。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) == 8 and text.isdigit():
        suffix = f" {default_time}" if default_time else ""
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}{suffix}"
    if trim_zero_seconds and len(text) == 19 and text.endswith(":00"):
        return text[:16]
    return text


def _normalize_ts_code(value: Any) -> str:
    """统一股票代码格式，兼容 QMT 返回 002687 / 002687.SZ 两种口径。"""
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        return text
    if text.startswith(("6", "9")):
        return f"{text}.SH"
    if text.startswith(("0", "2", "3")):
        return f"{text}.SZ"
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return text


def _ts_code_aliases(value: Any) -> set[str]:
    normalized = _normalize_ts_code(value)
    raw = str(value or "").strip().upper()
    aliases = {code for code in {normalized, raw} if code}
    if normalized and "." in normalized:
        aliases.add(normalized.split(".")[0])
    return aliases

# ── 常量 ───────────────────────────────────────────────────────────────────────
SCHEDULE = [
    datetime.time(9, 0),    # 盘前：提前生成/刷新组合状态机，避免09:20才开始算买入决策
    datetime.time(9, 15),   # 集合竞价开始：按涨停价预挂买入
    datetime.time(9, 20),   # 盘前：平仓检查 + 组合状态机
    datetime.time(9, 23),   # 集合竞价：按跌停价挂单平仓
    datetime.time(9, 26),   # 集合竞价成交后：同步实盘持仓，刷新今日买入决策
    datetime.time(9, 30),   # 开盘：若9:15未成功则补充买入
    datetime.time(14, 40),  # 撤未成交买单（提前于平仓，绝不挡住14:55平仓通道）
    datetime.time(14, 55),  # 盘中收盘平仓（最高优先，独占QMT通道）
    datetime.time(15, 10),  # 收盘流水线
]
SCHED_PREOPEN_PLAN = datetime.time(9, 0)
SCHED_PREMARKET_BUY = datetime.time(9, 15)
SCHED_MORNING_REVIEW = datetime.time(9, 20)
SCHED_PREMARKET_SELL = datetime.time(9, 23)
SCHED_PREMARKET_SYNC = datetime.time(9, 26)
SCHED_OPENING_BUY = datetime.time(9, 30)
SCHED_AFTERNOON_CLOSE = datetime.time(14, 55)
# 撤未成交买单：2026-07-15 起提前到平仓之前、独立调度（14:40与14:55相距
# 15分钟，调度器30秒护栏无碍）。目的：平仓时点独占QMT通道，绝不被撤单挡路；
# 且排队一天未成交的开仓买单（一字板排队）尾盘炸板成交=高位接盘，早撤无损失。
SCHED_CANCEL_BUY_ORDERS = datetime.time(14, 40)
# 平仓静默窗：14:53起止盈线程休眠、14:54:30~14:58账户心跳跳过，
# 确保14:55平仓发单时QMT通道零竞争（独占通道的实现，不只是口号）。
SCHED_TAKEPROFIT_QUIET = datetime.time(14, 53)
SCHED_POST_MARKET = datetime.time(15, 10)
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
E2_CAPACITY_FILE = PROJECT_ROOT / "data" / "processed" / "e2_capacity_history.json"
HEARTBEAT_FILE = PROJECT_ROOT / "logs" / "daemon_heartbeat.txt"
D_MONITOR_PID_FILE = PROJECT_ROOT / "logs" / "strategy_d_monitor.pid"
QMT_LAST_SUCCESS_FILE = PROJECT_ROOT / "logs" / "qmt_last_success.json"
CALENDAR_STALE_WARNED: set[str] = set()
_TRADE_CALENDAR_CACHE: dict[str, Any] = {
    "mtime": None,
    "open_dates": set(),
    "max_date": "",
}
_pending_buy_lock = threading.Lock()
_premarket_buy_monitor_thread: threading.Thread | None = None

# subprocess 超时（秒）：防止某步骤挂死
TIMEOUT_DATA_STEP = 600      # 数据采集/清洗步骤：10 分钟
TIMEOUT_SIGNAL_STEP = 900    # 信号生成步骤：15 分钟；A/B/C 首次加载特征较慢，不能误杀导致旧信号
TIMEOUT_ORDER_STEP = 60      # 下单预览步骤：1 分钟
TIMEOUT_COMBINED_PLAN_STEP = 180  # 组合状态机生成：Windows/QMT环境下首次加载特征较慢


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


def active_strategy_mode() -> int:
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        return int(config.get("active_strategy_profile", {}).get("mode", 1))
    except Exception:
        return 1


def is_strategy_l_mode() -> bool:
    return active_strategy_mode() == 2


def is_strategy_model3_mode() -> bool:
    return active_strategy_mode() == 3


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
    """读取交易日历。

    启动路径会频繁判断交易日，不能每次都 import pandas 再读 CSV。
    Windows/Z盘环境下 pandas 首次导入和网络盘 CSV 读取会显著拖慢启动，
    这里改用标准库 csv，并按文件 mtime 做进程内缓存。
    """
    try:
        cal_path = PROJECT_ROOT / "data" / "raw" / "trade_calendar.csv"
        if not cal_path.exists():
            return set(), ""
        mtime = cal_path.stat().st_mtime
        if _TRADE_CALENDAR_CACHE.get("mtime") == mtime:
            return (
                set(_TRADE_CALENDAR_CACHE.get("open_dates") or set()),
                str(_TRADE_CALENDAR_CACHE.get("max_date") or ""),
            )

        open_dates: set[str] = set()
        max_date = ""
        with cal_path.open("r", encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                cal_date = str(row.get("cal_date", "")).strip()
                if not cal_date:
                    continue
                if cal_date > max_date:
                    max_date = cal_date
                if str(row.get("is_open", "")).strip() in {"1", "1.0", "True", "true"}:
                    open_dates.add(cal_date)

        _TRADE_CALENDAR_CACHE["mtime"] = mtime
        _TRADE_CALENDAR_CACHE["open_dates"] = set(open_dates)
        _TRADE_CALENDAR_CACHE["max_date"] = max_date
        return open_dates, max_date
    except Exception:
        return set(), ""


def ensure_trade_calendar_fresh() -> None:
    """启动时自动刷新交易日历，避免日期判断长期依赖周历兜底。"""
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        data_cfg = config.get("data", {})
        start_date = str(data_cfg.get("start_date", "20190101"))
        end_date = str(data_cfg.get("end_date", "20261231"))
        today_str = today_beijing().strftime("%Y%m%d")
        # 滚动前瞻：日历须覆盖今天+30天，否则刷新；拉取到今天+370天，
        # 避免配置end_date写死导致跨年后永远刷不出新日历、降级周历误判节假日。
        target_date = max(today_str, (today_beijing() + datetime.timedelta(days=30)).strftime("%Y%m%d"))
        fetch_end = max(end_date, (today_beijing() + datetime.timedelta(days=370)).strftime("%Y%m%d"))

        _, max_date = _load_calendar()
        if max_date and max_date >= target_date:
            logger().info("交易日历已覆盖到 %s，无需刷新。", max_date)
            return

        logger().warning(
            "交易日历未覆盖目标日期（当前最大=%s，目标=%s），启动时自动刷新。",
            max_date or "无",
            target_date,
        )
        from src.data_source import TushareDataSource
        from src.trading_calendar import TradingCalendar

        source = TushareDataSource(PROJECT_ROOT / "config" / "config.json")
        calendar = TradingCalendar(source, PROJECT_ROOT / "config" / "config.json").fetch_and_save(
            start_date,
            fetch_end,
            overwrite=True,
        )
        max_after = str(calendar["cal_date"].astype(str).max()) if "cal_date" in calendar.columns and not calendar.empty else ""
        logger().info("✅ 交易日历自动刷新完成：%s-%s，行数=%d。", start_date, max_after, len(calendar))
    except Exception as exc:
        logger().error("交易日历自动刷新失败：%s；将暂时使用本地缓存/周历兜底。", exc)
        _notify(
            "system_error",
            "⚠️ 交易日历自动刷新失败",
            "交易日历未能自动更新，程序会暂用本地缓存或周历兜底，请稍后检查数据源。",
            level="timeSensitive",
        )


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
    return (
        datetime.time(9, 30) <= now.time() <= datetime.time(11, 30)
        or datetime.time(13, 0) <= now.time() <= datetime.time(15, 0)
    )


def qmt_is_critical_window() -> bool:
    """判断 QMT 心跳是否处于必须严格告警的窗口。

    非交易日/夜间 QMT 账户查询偶发卡住很常见，不能按一次超时就推送“账户断连”。
    但交易日前后、持仓中、盘前买单待确认时，QMT 可用性会直接影响买卖执行，
    必须保持严格检测和快速重连。
    """
    now = now_beijing()
    if has_open_local_position() or load_pending_buys():
        return True
    if not is_trade_day(now.date()):
        return False
    return datetime.time(9, 0) <= now.time() <= datetime.time(15, 10)


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


_broker_empty_streak = 0  # 连续"券商空但本地有持仓"的确认计数，见下方两次确认机制


def _note_broker_has_positions() -> None:
    """券商查询确认有实盘持仓时调用，重置幽灵清理确认计数。"""
    global _broker_empty_streak
    _broker_empty_streak = 0


def clear_local_positions_when_broker_empty(source: str) -> int:
    """券商接口已明确返回无持仓时，清理本地 open/sell_pending 幽灵持仓。

    只能在 query_positions() 成功且已确认实盘 volume>0 持仓为空后调用。
    接口失败、QMT未启用、未拿到明确结果时严禁调用，避免误删真实持仓记录。

    两次确认机制：QMT 偶发会在调用成功时返回空持仓（客户端数据未同步/
    session 切换瞬间），单次空结果就清理曾导致 20260701 乔治白真实持仓
    被误清、20260701 到期日平仓流程失明漏卖（T+2 被动变 T+3）。
    因此连续第 2 次（不同轮查询）确认为空才执行清理，第 1 次只告警等复核；
    任何一次查到券商有持仓即由 _note_broker_has_positions() 归零计数。
    """
    global _broker_empty_streak
    positions = load_positions()
    open_like = [p for p in positions if str(p.get("status", "")).lower() in {"open", "sell_pending"}]
    if not open_like:
        _broker_empty_streak = 0
        return 0
    _broker_empty_streak += 1
    if _broker_empty_streak < 2:
        logger().warning(
            "⚠️ [幽灵持仓疑似] QMT返回无持仓，但本地有%d条open/sell_pending记录（来源=%s，第1次发现）。"
            "暂不清理，等待下一轮查询复核（防QMT数据未同步误清真实持仓）。",
            len(open_like), source,
        )
        return 0
    changed = 0
    now_str = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    for pos in positions:
        status = str(pos.get("status", "")).lower()
        if status not in {"open", "sell_pending"}:
            continue
        pos["status"] = "closed"
        pos["sell_date"] = pos.get("sell_date") or today_beijing().strftime("%Y%m%d")
        pos["sell_price"] = pos.get("sell_price") or 0.0
        pos["ghost_cleared_at"] = now_str
        pos["ghost_clear_source"] = source
        pos["ghost_clear_reason"] = "QMT接口查询成功且返回无实盘持仓"
        changed += 1

    if changed:
        save_positions(positions)
        logger().warning(
            "🧹 [幽灵持仓清理] QMT连续2轮确认实盘无持仓，已将本地%d条open/sell_pending持仓标记为closed。来源=%s",
            changed,
            source,
        )
    _broker_empty_streak = 0
    return changed


def record_buy(order_id: str, ts_code: str, name: str, signal_date: str,
               buy_date: str, shares: int, buy_price: float, strategy_leg: str,
               exit_n_days: int = 2, traded_at: str = "") -> None:
    positions = load_positions()
    if any(p["order_id"] == order_id for p in positions):
        return
    exit_date = next_n_trade_days(
        datetime.datetime.strptime(buy_date, "%Y%m%d").date(), n=exit_n_days
    )
    # 券商成交回报时间常是Unix秒（国金QMT实测），转成可读北京时间便于审计成交时点；
    # 其他格式（HHMMSS整数、已是字符串）原样保留，不做破坏性解析。
    if traded_at and str(traded_at).isdigit() and int(traded_at) > 1_000_000_000:
        try:
            traded_at = datetime.datetime.fromtimestamp(
                int(traded_at), tz=BEIJING_TZ
            ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    buy_time = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    planned_exit_time = datetime.datetime.combine(exit_date, SCHED_AFTERNOON_CLOSE).strftime("%Y-%m-%d %H:%M")
    positions.append({
        "order_id": order_id,
        # buy_time=确认回写时刻；traded_at=券商成交回报的真实成交时间（审计成交时点用）
        "traded_at": traded_at,
        "ts_code": ts_code,
        "name": name,
        "signal_date": signal_date,
        "buy_date": buy_date,
        "buy_time": buy_time,
        "planned_exit_date": exit_date.strftime("%Y%m%d"),
        "planned_exit_time": planned_exit_time,
        "shares": shares,
        "buy_price": buy_price,
        "strategy_leg": strategy_leg,
        "status": "open",
        "sell_date": None,
        "sell_price": None,
    })
    save_positions(positions)
    logger().info("持仓记录：策略=%s %s %s 买入日 %s 计划平仓日 %s",
                  strategy_leg, ts_code, name, buy_date, exit_date.strftime("%Y%m%d"))


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
    target_aliases = _ts_code_aliases(ts_code)
    for p in load_positions():
        if str(p.get("status", "")).lower() not in {"open", "sell_pending"}:
            continue
        if not (_ts_code_aliases(p.get("ts_code", "")) & target_aliases):
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


def _confirm_fill(
    broker_cfg: dict,
    order_id: str,
    expected_qty: int,
    tag: str,
    *,
    timeout_sec: float | None = None,
    poll_sec: float | None = None,
) -> "OrderFill":
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

    timeout = float(timeout_sec if timeout_sec is not None else lt.get("fill_confirm_timeout_sec", 60))
    poll = max(1.0, float(poll_sec if poll_sec is not None else lt.get("fill_confirm_poll_sec", 3)))
    # 集合竞价时段（09:15~09:25）挂的单要到09:25集中撮合才有成交回报，
    # 固定60秒超时必然误报"未成交"（如逾期持仓09:20清理挂单）。
    # 确认窗口自动延长到09:25:20之后，避免虚假critical告警。
    now_t = now_beijing()
    if datetime.time(9, 15) <= now_t.time() < datetime.time(9, 25):
        auction_done = now_t.replace(hour=9, minute=25, second=20, microsecond=0)
        wait_to_auction = (auction_done - now_t).total_seconds()
        if wait_to_auction + 10 > timeout:
            timeout = wait_to_auction + 10
            log.info("[%s] 集合竞价时段挂单，成交确认窗口延长至09:25:30（%.0f秒）", tag, timeout)
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


def _format_order_fill_detail(fill: Any) -> str:
    """把券商委托终态尽量转成可读说明，尤其用于废单排查。"""
    raw = fill.raw if isinstance(getattr(fill, "raw", None), dict) else {}
    detail_parts = [
        f"状态码={getattr(fill, 'status_code', '')}",
        f"状态={getattr(fill, 'status_text', '')}",
        f"已成={getattr(fill, 'filled_qty', 0)}股",
    ]
    interesting_keywords = (
        "error", "err", "fail", "失败", "废", "拒",
        "msg", "message", "remark", "memo", "note", "status", "reason",
        "m_str", "order", "entrust",
    )
    extra: list[str] = []
    for key, value in raw.items():
        if value in {None, ""}:
            continue
        key_text = str(key)
        if any(word.lower() in key_text.lower() for word in interesting_keywords):
            extra.append(f"{key_text}={value}")
    if extra:
        detail_parts.append("原始字段：" + "；".join(extra[:12]))
    return "；".join(detail_parts)


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
                | action_text.eq("PLAN_SELL_D_T2_CLOSE")
                | action_text.eq("PLAN_SELL_L_T2_CLOSE")
                | risk_text.str.contains("E2_SELL_T2_CLOSE", na=False)
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
                        traded_at=getattr(fill, "traded_at", ""),
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
                    mark_position_closed(local_oid, today_str, fill_price)
                    log.info("✅ [%s] %s 卖出全部成交 %d股 @%.2f，已平仓。",
                             tag, s["ts_code"], fill.filled_qty, fill_price)
                    _notify("sell_success", "✅ 平仓成交",
                            f"{s['ts_code']} {s['name']} 卖出{fill.filled_qty}股 "
                            f"@{fill_price:.2f} 金额{_fmt_wan(fill.filled_qty * fill_price)}")
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
    if "risk_flags" not in adjusted.columns:
        adjusted["risk_flags"] = ""
    else:
        # planned_orders 来自 CSV 时，空 risk_flags 会被 pandas 读成 float64 NaN。
        # 后续需要追加 LIVE_SIZE_ADJUSTED 等字符串标记，先转 object，避免 09:15 实盘缩单时报 dtype 错误。
        adjusted["risk_flags"] = adjusted["risk_flags"].astype("object")

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
            if side == "BUY":
                parts.append(f"不在允许交易时间内；BUY 允许 09:15-14:56，09:15前或14:56后会被拒")
            else:
                parts.append(f"不在允许交易时间内；SELL 允许 09:15-11:30、13:00-15:00")
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
    """平仓挂单取价：跌停价限价（市价单效果），确保成交。

    2026-07-08 皇氏集团事故：14:56挂买5价3.26，尾盘tick滞后/瞬间下砸导致
    已报0成交，持仓被动过夜。买N档取价的致命弱点：挂单瞬间价格跌破挂价
    就变成排队单，且深市14:57后进收盘集合竞价不可撤单，无补救窗口。
    改挂跌停价（D策略09:23集合竞价同款"挂跌停不砸盘"原理）：
    - 连续竞价中＝吃买盘直到成交，成交价≈买一起的真实对手价，不会真砸到跌停；
    - 进收盘集合竞价＝低于任何可能收盘价，必以收盘价成交——与回测口径
      （T+2收盘价卖出）完全一致；
    - 仅当股价已真实跌停（无买盘）才可能不成交，那是市场极端，任何挂法都无解。
    gateway 的 reject_limit_down_sell 拒的是"股价已跌停时的卖出"，
    不拒"挂单价=跌停价"，两者不冲突。
    跌停价缺失时退回买10/买5/买1/最新价（原逻辑兜底）。
    """
    lower_limit = float(getattr(quote, "lower_limit", 0.0) or 0.0) if quote else 0.0
    if lower_limit > 0:
        return round(lower_limit, 2), "跌停价(市价效果)"
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
        except Exception:
            pass
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                cur = adapter.query_orders()
            except Exception:
                continue
            st = -2
            for o in cur or []:
                od = object_to_dict(o)
                if str(first_present(od, ["order_id", "m_nOrderID"], "")) == oid:
                    st = to_int(first_present(od, ["order_status", "m_nOrderStatus", "status"], -1), -1)
                    break
            if st in (53, 54, 56, -2):
                log.info("[平仓前置] %s 止盈单已了结（状态%s），继续平仓。", code, st)
                break
        else:
            log.error("[平仓前置] %s 止盈单撤单超时未确认，平仓可能因股份冻结被拒！", code)


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
        _notify("sell_fail", "❌ ABC平仓条件不满足",
                f"{ts_code} {name} 平仓未提交：{e}。请立即回终端核对。",
                level="critical", call=True)
        return False

    with _qmt_lock:
        adapter = _qmt_get(broker_cfg)
        _cancel_own_takeprofit_orders(adapter, {ts_code})
        quote_map = adapter.get_full_tick([ts_code])

    quote = quote_map.get(ts_code)
    price, price_label = _pick_sell_limit_price(quote)
    if price <= 0:
        log.warning("ABC平仓：%s 无法获取价格，跳过本次。", ts_code)
        _notify("sell_fail", "❌ ABC平仓无报价",
                f"{ts_code} {name} 无法获取有效卖出价格，平仓未提交，请立即回终端核对。",
                level="critical", call=True)
        return False

    if shares <= 0:
        log.error("ABC平仓：%s 持仓股数为0，跳过。", ts_code)
        _notify("sell_fail", "❌ ABC平仓股数异常",
                f"{ts_code} {name} 本地持仓股数为0，平仓未提交，请核对positions.json和QMT持仓。",
                level="critical", call=True)
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
        _notify("sell_fail", "❌ ABC平仓提交失败",
                f"{ts_code} {name} 平仓委托提交失败：{result.message}。请立即回终端处理。",
                level="critical", call=True)
        return False

    log.info("✅ [ABC平仓] %s %s %d股 @%.2f 委托已受理（待成交确认）", ts_code, name, shares, price)
    order_id_broker = str(result.order_id or f"abc-sell-{today_str}-{ts_code}")
    fill = _confirm_fill(broker_cfg, order_id_broker, shares, "ABC平仓")
    fill_price = fill.avg_price if fill.avg_price > 0 else price
    if fill.filled_qty >= shares:
        mark_position_closed(order_id, today_str, fill_price)
        log.info("✅ [ABC平仓] %s %s 全部成交 %d股 @%.2f，已平仓。", ts_code, name, fill.filled_qty, fill_price)
        _notify("sell_success", "✅ 平仓成交",
                f"{ts_code} {name} 卖出{fill.filled_qty}股 "
                f"@{fill_price:.2f} 金额{_fmt_wan(fill.filled_qty * fill_price)}")
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
                # 成交确认与持仓回写由 _execute_orders_inprocess 内部完成。
                # 平仓不允许单点依赖计划文件：09:00组合状态机若失败/文件缺失/
                # 无本标的SELL行，原逻辑会静默跳过导致被动过夜。兜底改走
                # ABC/L 同款直接卖出（买10/买5挂限价+成交确认+失败告警），
                # 保证 T+2 收盘必卖，与回测口径一致。
                combined_path = (
                    PROJECT_ROOT / "reports" / "live_trade" / "combined"
                    / f"combined_planned_orders_{today_str}.csv"
                )
                has_sell_row = False
                if combined_path.exists():
                    try:
                        import pandas as pd

                        _po = pd.read_csv(combined_path, low_memory=False)
                        if not _po.empty and {"side", "ts_code"}.issubset(_po.columns):
                            has_sell_row = bool((
                                (_po["side"].astype(str).str.upper() == "SELL")
                                & (_po["ts_code"].astype(str) == str(ts_code))
                            ).any())
                    except Exception as read_err:
                        logger().warning("E2平仓：读取组合计划单失败（%s），走直接卖出兜底。", read_err)
                if has_sell_row:
                    _execute_orders_inprocess(
                        combined_path,
                        confirm,
                        "E2平仓",
                        allowed_sides={"SELL"},
                        allow_t2_close_sell_now=True,
                    )
                else:
                    logger().warning(
                        "E2平仓兜底：组合计划单缺失或无 %s 的SELL行（%s），直接按买10/买5挂限价卖出。",
                        ts_code, combined_path,
                    )
                    _abc_place_sell_order_direct(ts_code, name, shares, order_id, confirm, config, broker_cfg)
            elif strategy_leg in {"A", "B", "C", "L"}:
                # A/B/C/L 均是 T+N 收盘卖出口径：
                # planned_orders 文件通常只负责买入计划，平仓时直接按买10/买5挂限价卖出。
                # L 接入后复用同一套“确保尽量成交、成交后回写持仓”的平仓链路。
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


def _close_position_watchdog() -> None:
    """收盘平仓看门狗（独立常驻线程）：14:52/14:55 两次核查当日到期持仓是否已有人管。

    2026-07-10 事故：主调度线程被锁竞争拖慢，14:55 平仓迟到 6 分钟且被拒后，
    没有独立哨兵发现"到点未平仓"，只能靠用户自己盯盘手动平仓。
    本线程只告警不下单（避免与主流程双发卖单）：
    - 判据="该标的当日存在活跃(48~52,55)或已成(56)委托"——主平仓已发单、
      止盈单已成交都算有人管；已撤/废单不算；
    - 拿不到 _qmt_lock（超时2秒）说明主流程正在使用 QMT（大概率正在平仓），
      本次静默跳过，下一检查点再核。
    """
    log = logger()
    fired: set[str] = set()
    checkpoints = (datetime.time(14, 57), datetime.time(14, 59))
    while True:
        try:
            now = now_beijing(); t = now.time()
            today_str = today_beijing().strftime("%Y%m%d")
            if is_trade_day(now.date()) and t < datetime.time(15, 0):
                for chk in checkpoints:
                    key = f"{today_str}-{chk}"
                    if key in fired or t < chk:
                        continue
                    fired.add(key)
                    due = [p for p in load_positions()
                           if str(p.get("status", "")).lower() in {"open", "sell_pending"}
                           and str(p.get("planned_exit_date", "99991231")) <= today_str]
                    if not due:
                        continue
                    if not _qmt_lock.acquire(timeout=2):
                        log.info("[平仓看门狗] %s QMT忙（主流程可能正在平仓），本次跳过。", chk)
                        continue
                    try:
                        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
                        adapter = _qmt_get(config.get("broker", {}))
                        orders = adapter.query_orders()
                    except Exception as e:
                        log.error("[平仓看门狗] 查询委托失败：%s", e)
                        continue
                    finally:
                        _qmt_lock.release()
                    from src.qmt_adapter import object_to_dict, first_present, to_int
                    covered: set[str] = set()
                    for o in orders or []:
                        od = object_to_dict(o)
                        st = to_int(first_present(od, ["order_status", "m_nOrderStatus", "status"], -1), -1)
                        if st in (48, 49, 50, 51, 52, 55, 56):
                            code = str(first_present(od, ["stock_code", "m_strInstrumentID", "ts_code"], "")).upper().split(".")[0]
                            covered.add(code)
                    missing = [p for p in due if str(p.get("ts_code", "")).split(".")[0] not in covered]
                    if missing:
                        codes = "、".join(f"{p.get('ts_code','')} {p.get('name','')}" for p in missing)
                        log.error("🛑 [平仓看门狗] %s 核查：%s 今日到期但未发现有效卖出委托！", chk, codes)
                        _notify("sell_fail", "🛑 收盘平仓疑似未执行",
                                f"{chk.strftime('%H:%M')}核查：{codes} 今日到期，QMT中未发现活跃/已成委托，"
                                f"平仓流程可能被卡住或被拒。请立即人工核对并手动平仓（15:00收盘前）！",
                                level="critical", call=True)
                    else:
                        log.info("[平仓看门狗] %s 核查通过：%d笔到期持仓均已有委托在场。", chk, len(due))
        except Exception as e:
            logger().error("平仓看门狗异常：%s", e)
        # 距下一个14:52超过10分钟就长睡，窗口附近10秒粒度
        now2 = now_beijing()
        nxt = now2.replace(hour=14, minute=52, second=0, microsecond=0)
        if now2 >= now2.replace(hour=15, minute=0, second=0, microsecond=0):
            nxt += datetime.timedelta(days=1)
        gap = (nxt - now2).total_seconds()
        time.sleep(10 if 0 <= gap <= 600 or now2.time() >= datetime.time(14, 52) and now2.time() < datetime.time(15, 0) else min(max(gap - 600, 60), 3600))


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
        except Exception as e:
            logger().error("晨检线程异常：%s", e)
        # 睡到下一个08:30（当天已过则次日），上限1小时防时钟跳变
        now = now_beijing()
        nxt = now.replace(hour=8, minute=30, second=0, microsecond=0)
        if now >= nxt:
            nxt += datetime.timedelta(days=1)
        time.sleep(min(max((nxt - now).total_seconds(), 60), 3600))


def _intraday_takeprofit_monitor() -> None:
    """当日到期持仓的涨停预挂止盈（常驻线程，2026-07-10 用户定稿规则）。

    规则（零轮询压力版）：
    - 今日有平仓计划(planned_exit_date==今日)的 open 持仓，09:20 无条件
      挂（涨停价-0.01）限价卖单（参与集合竞价）——不看涨幅、不做盘中判断；
    - 冲板/秒板/炸板前触及 → 自动成交在涨停-0.01（锁定强势卖出）；
    - 14:45 仍未成交 → 撤单（礼让式重试3次），14:55 收盘平仓主流程接管；
    - 非当日到期持仓不做任何操作。
    回测口径不变（成交集=触及涨停-0.01，E2 62笔 8.8x→9.1x）。

    负载设计：一天只有两个动作点（09:30挂、14:45撤）+ 每5分钟一次
    礼让式成交检查（成交须及时回写，否则账户心跳会与本地账目失联）。
    无状态：每轮从 query_orders 按 remark 前缀认领自家单，重启自动接管
    不重复挂；所有QMT调用礼让式（拿不到锁/超时即放弃本轮），任何情况
    不堵塞主交易流程。
    """
    from src.broker_adapter import OrderRequest

    log = logger()
    REMARK_PREFIX = "盘中止盈"
    place_tries: dict[str, int] = {}   # 当日每标的发单次数（熔断用）
    tries_day = ""
    MAX_PLACE_TRIES = 5
    qmt_stall = {"n": 0, "alerted": False}   # 当日QMT调用超时计数（通道卡滞监测）
    while True:
        try:
            now = now_beijing(); t = now.time()
            # 09:20起挂单：让止盈卖单参与集合竞价——开盘即涨停的极端日
            # 在09:25直接按开盘价(=涨停价)成交，消灭9:25~9:30静默期的
            # 炸板空窗；9:20后竞价不可撤单对本单无碍（本就挂到14:45）。
            if not is_trade_day(now.date()) or t < datetime.time(9, 20) or t >= SCHED_TAKEPROFIT_QUIET:
                time.sleep(60 if datetime.time(9, 0) <= t < datetime.time(9, 20) else 120); continue
            config = load_json_config(PROJECT_ROOT / "config" / "config.json")
            lt = config.get("live_trade", {})
            if not lt.get("intraday_takeprofit_enabled", True):
                time.sleep(300); continue
            offset = float(lt.get("intraday_takeprofit_offset", 0.01))
            qmt_on = (config.get("broker_adapter_enabled") and config.get("qmt_enabled")
                      and config.get("broker", {}).get("enabled"))
            if not qmt_on:
                time.sleep(300); continue
            broker_cfg = config.get("broker", {})
            today_str = today_beijing().strftime("%Y%m%d")
            if tries_day != today_str:
                place_tries.clear(); tries_day = today_str
                qmt_stall.update(n=0, alerted=False)
            due = [p for p in load_positions()
                   if str(p.get("status", "")).lower() == "open"
                   and str(p.get("planned_exit_date", "")) == today_str]
            if t < datetime.time(9, 30):
                # D 让路仓走09:23竞价挂跌停卖（口径=开盘价），9:20抢先挂止盈
                # 单会冻结股份让09:23挂单被拒，把D劣化成收盘卖。D一律等9:30
                # 后再挂（让路仓届时已closed，剩下的是T+2收盘卖仓，安全）。
                due = [p for p in due if str(p.get("strategy_leg", "")).upper() != "D"]
            if not due:
                time.sleep(300); continue   # 无当日到期持仓：不碰QMT，5分钟后再看

            def _polite_qmt(fn_name: str, args: tuple = (), lock_timeout: float = 3.0, call_timeout: float = 8.0):
                """礼让式QMT调用：拿不到锁或调用超时都返回None放弃，绝不堵塞他人。"""
                if not _qmt_lock.acquire(timeout=lock_timeout):
                    return None
                try:
                    result: list[Any] = []; err: list[BaseException] = []
                    def _run() -> None:
                        try:
                            adapter = _qmt_get(broker_cfg)
                            result.append(getattr(adapter, fn_name)(*args))
                        except BaseException as e:
                            err.append(e)
                    th = threading.Thread(target=_run, daemon=True)
                    th.start(); th.join(call_timeout)
                    if th.is_alive():
                        # 调用线程超时未归（泄漏）：QMT/共享盘IO变慢的早期信号
                        qmt_stall["n"] += 1
                        if qmt_stall["n"] >= 8 and not qmt_stall["alerted"]:
                            qmt_stall["alerted"] = True
                            _notify("system_error", "⚠️ QMT通道疑似卡滞",
                                    f"盘中止盈线程今日已{qmt_stall['n']}次QMT调用超时，"
                                    f"通道/共享盘IO疑似恶化，请关注14:55平仓链路是否正常。",
                                    level="critical")
                        return None
                    if err or not result:
                        return None
                    return result[0]
                finally:
                    _qmt_lock.release()

            # 认领自家止盈单（无状态，防重启丢单/重复挂；重启后查不到
            # 自家活单会自动走下方补挂分支——交易时间内重启即接管）
            active: dict[str, dict] = {}
            cancelled_codes: set[str] = set()
            orders_raw = _polite_qmt("query_orders")
            if orders_raw is None:
                time.sleep(60); continue
            from src.qmt_adapter import object_to_dict, first_present, to_int
            for o in orders_raw:
                od = object_to_dict(o)
                remark = str(first_present(od, ["order_remark", "m_strRemark", "remark"], ""))
                if not remark.startswith(REMARK_PREFIX):
                    continue
                status = to_int(first_present(od, ["order_status", "m_nOrderStatus", "status"], -1), -1)
                code = str(first_present(od, ["stock_code", "m_strInstrumentID", "ts_code"], "")).upper()
                oid = str(first_present(od, ["order_id", "m_nOrderID"], ""))
                short_c = code.split(".")[0]
                if status in (53, 54):   # 部撤/已撤：死单不占坑，单独记录（区分人工撤单）
                    cancelled_codes.add(short_c); continue
                if status == 57:         # 废单：不占坑 → 下面走补挂重试
                    continue
                active[short_c] = {"order_id": oid, "status": status}

            def _order_status(oid: str):
                """查单笔委托当前状态；None=查询失败，-2=已不在委托列表。"""
                orders2 = _polite_qmt("query_orders")
                if orders2 is None:
                    return None
                for o2 in orders2:
                    od2 = object_to_dict(o2)
                    if str(first_present(od2, ["order_id", "m_nOrderID"], "")) == str(oid):
                        return to_int(first_present(od2, ["order_status", "m_nOrderStatus", "status"], -1), -1)
                return -2

            cancel_window = t >= datetime.time(14, 45)
            # 可发新委托的时段：9:20~9:25（竞价申报）、9:30~11:30、13:00~14:45。
            # 午休、9:25~9:30静默期等非交易时间不发委托（无论是否刚重启），
            # 已挂单的成交监控/撤单照常。
            can_place = ((datetime.time(9, 20) <= t < datetime.time(9, 25))
                         or (datetime.time(9, 30) <= t < datetime.time(11, 30))
                         or (datetime.time(13, 0) <= t < datetime.time(14, 45)))
            for pos in due:
                ts_code = str(pos.get("ts_code", "")); short = ts_code.split(".")[0]
                shares = int(pos.get("shares", 0)); name_s = str(pos.get("name", ""))
                rec = active.get(short)
                if rec:
                    status = rec["status"]
                    if status == 56:  # 已成 → 及时回写（否则账户心跳与本地账目失联）
                        fill = _confirm_fill(broker_cfg, rec["order_id"], shares, "盘中止盈确认", timeout_sec=10)
                        price = fill.avg_price if fill.avg_price > 0 else 0.0
                        mark_position_closed(pos.get("order_id", ""), today_str, price)
                        log.info("✅ [盘中止盈] %s %s 全部成交 @%.2f，已平仓。", ts_code, name_s, price)
                        _notify("sell_success", "✅ 盘中止盈成交",
                                f"{ts_code} {name_s} 涨停附近止盈卖出成交 @{price:.2f}。")
                    elif cancel_window and status in (48, 49, 50, 51, 52, 55):  # 14:45 撤单
                        # 撤单成败以订单最终状态为准，绝不信 cancel_order 的返回值
                        # 或调用超时（2026-07-10 事故：QMT慢→调用超时→撤单实际已
                        # 成功却连报三次失败；且超时的请求可能已送达，盲目重发无益）。
                        final_st = None
                        for _ in range(3):
                            _polite_qmt("cancel_order", (rec["order_id"],))  # 发出请求即可
                            time.sleep(2)
                            st = _order_status(rec["order_id"])
                            if st in (53, 54, 56, -2):
                                final_st = st; break
                            time.sleep(1)
                        if final_st == 56:
                            fill = _confirm_fill(broker_cfg, rec["order_id"], shares, "盘中止盈确认", timeout_sec=10)
                            price = fill.avg_price if fill.avg_price > 0 else 0.0
                            mark_position_closed(pos.get("order_id", ""), today_str, price)
                            log.info("✅ [盘中止盈] %s %s 撤单前已全部成交 @%.2f，已平仓。", ts_code, name_s, price)
                            _notify("sell_success", "✅ 盘中止盈成交",
                                    f"{ts_code} {name_s} 止盈卖单在撤单前已全部成交 @{price:.2f}。")
                        elif final_st in (53, 54, -2):
                            log.info("[盘中止盈] %s 14:45未成交已撤单（订单状态%s确认），交回14:55收盘平仓。", ts_code, final_st)
                            fill = _confirm_fill(broker_cfg, rec["order_id"], shares, "止盈撤单后确认", timeout_sec=8)
                            if 0 < fill.filled_qty < shares:
                                reduce_position_shares(pos.get("order_id", ""), shares - fill.filled_qty)
                                log.warning("[盘中止盈] %s 部成%d/%d股后撤单，剩余%d股由14:55平仓。",
                                            ts_code, fill.filled_qty, shares, shares - fill.filled_qty)
                        else:
                            _notify("sell_fail", "⚠️ 止盈单撤单失败",
                                    f"{ts_code} 盘中止盈单14:45撤单后订单状态仍未确认为已撤（最后状态={final_st}），"
                                    f"请立即手动处理，避免与14:55平仓冲突。",
                                    level="critical", call=True)
                    continue
                if cancel_window or not can_place:
                    continue   # 14:45后/非交易时段（午休、竞价静默期）不发新委托
                if short in cancelled_codes:
                    # 14:45前出现已撤单只可能是人工撤的 → 尊重人工干预不补挂；
                    # 若撤前有部分成交，本地持仓不会自动扣减，请人工核对。
                    log.info("[盘中止盈] %s %s 止盈单已被撤销（疑人工干预），不再补挂；14:55收盘平仓仍生效。", ts_code, name_s)
                    continue
                if place_tries.get(short, 0) >= MAX_PLACE_TRIES:
                    continue   # 当日发单熔断：反复被拒/废单说明环境异常，放弃当日预挂
                # 未挂单 → 预挂（涨停价-0.01，参与集合竞价）；重启后由此补挂
                tick_map = _polite_qmt("get_full_tick", ([ts_code],), call_timeout=5.0)
                quote = tick_map.get(ts_code) if tick_map else None
                pre = float(getattr(quote, "pre_close", 0.0) or 0.0) if quote else 0.0
                upper = float(getattr(quote, "upper_limit", 0.0) or 0.0) if quote else 0.0
                if upper <= 0:
                    if pre <= 0:
                        log.warning("[盘中止盈] %s 无法取得涨停价（行情不可得），下轮重试。", ts_code)
                        continue
                    pcap = 0.30 if ts_code.endswith(".BJ") else (0.20 if short.startswith(("300", "301", "688", "689")) else 0.10)
                    upper = round(pre * (1 + pcap), 2)
                sell_price = round(upper - offset, 2)
                request = OrderRequest(
                    ts_code=ts_code, broker_code=ts_code, side="SELL",
                    quantity=shares, price_type="FIXED_PRICE", price=sell_price,
                    strategy_name="A_SYSTEM_ABC", remark=f"{REMARK_PREFIX}-{today_str}",
                )
                place_tries[short] = place_tries.get(short, 0) + 1
                if place_tries[short] >= MAX_PLACE_TRIES:
                    _notify("sell_fail", "⚠️ 盘中止盈熔断",
                            f"{ts_code} {name_s} 止盈委托当日已尝试{MAX_PLACE_TRIES}次仍无活单"
                            f"（反复被拒或废单，疑节假日/通道异常），今日放弃预挂，14:55收盘平仓照常兜底。",
                            level="critical")
                result = _polite_qmt("place_order", (request,))
                if result is None:
                    log.warning("[盘中止盈] %s 挂单未执行（QMT忙/超时），下轮重试。", ts_code)
                    continue
                if result.accepted:
                    log.warning("⏳ [盘中止盈] %s %s 当日到期，09:20预挂涨停-%.2f止盈卖单 %d股@%.2f（14:45未成交自动撤）",
                                ts_code, name_s, offset, shares, sell_price)
                    _notify("sell_success", "📈 止盈卖单已预挂",
                            f"{ts_code} {name_s} 今日到期，已挂{sell_price:.2f}（涨停-{offset:.2f}）止盈卖单，"
                            f"冲板即成交锁定强势；14:45未成交自动撤单，14:55照常收盘平仓。",
                            level="timeSensitive")
                else:
                    log.error("❌ [盘中止盈] %s 挂单失败：%s", ts_code, result.message)
        except Exception as e:
            log.error("盘中止盈监控异常：%s", e)
        # 5分钟一轮；若下一轮将跨过14:45撤单点，则对齐到14:45:10醒来，保证撤单及时
        _now = now_beijing()
        _cancel_at = _now.replace(hour=14, minute=45, second=10, microsecond=0)
        _gap = (_cancel_at - _now).total_seconds()
        time.sleep(_gap if 0 < _gap < 300 else 300)


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

            t2_close_leg = strategy_leg in {"A", "B", "C", "D", "E2", "L"}
            due_today = planned_exit == today_str
            before_close_sell_window = now_beijing().time() < SCHED_AFTERNOON_CLOSE
            if t2_close_leg and due_today and before_close_sell_window:
                logger().warning(
                    "T2收盘卖门禁：%s %s 策略=%s 今日到期，但当前未到14:55收盘平仓窗口，保持持仓不提前平仓。",
                    ts_code,
                    name,
                    strategy_leg,
                )
                continue

            # 逾期持仓（计划平仓日已过=事故残留，如2026-07-08皇氏平仓失败过夜）
            # 必须第一时间清理：09:15后即可挂跌停价卖单（参与集合竞价/连续竞价，
            # 必成交），不等14:55、也不依赖上一轮先标记sell_pending。
            overdue_past = planned_exit < today_str
            sellable_now = now_beijing().time() >= datetime.time(9, 15) and now_beijing().time() <= datetime.time(15, 0)
            if market_is_open() or pending or (overdue_past and sellable_now):
                if overdue_past:
                    logger().warning("⚠️ 逾期持仓第一时间清理：%s %s 计划平仓日%s已过，立即挂单卖出。", ts_code, name, planned_exit)
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

_pipeline_thread: threading.Thread | None = None
_pipeline_thread_lock = threading.Lock()
_pipeline_resume_event = threading.Event()
_pipeline_resume_event.set()
_pipeline_pause_reason = ""


def _pipeline_paused() -> bool:
    return not _pipeline_resume_event.is_set()


def _pause_pipeline_for_trade(reason: str) -> None:
    """交易执行优先：平仓/撤单窗口临时暂停数据流水线后续步骤。"""
    global _pipeline_pause_reason
    if not _pipeline_paused():
        logger().warning("交易优先暂停：%s；收盘/采集流水线将在安全点暂停，交易处理完成后继续。", reason)
    _pipeline_pause_reason = reason
    _pipeline_resume_event.clear()


def _resume_pipeline_after_trade(reason: str) -> None:
    global _pipeline_pause_reason
    if _pipeline_paused():
        logger().info("交易优先恢复：%s；收盘/采集流水线继续执行。", reason)
    _pipeline_pause_reason = ""
    _pipeline_resume_event.set()


def _wait_if_pipeline_paused(context: str) -> None:
    last_log = 0.0
    while _pipeline_paused():
        # 收盘后自动释放：15:05后交易窗口已结束，卖单要么已成交要么已成废单，
        # 继续暂停毫无意义，却会把晚间数据采集/明日信号生成全部堵死。
        # 2026-07-08 皇氏平仓失败后"保持暂停等人工"卡死流水线1小时+，即此场景。
        if now_beijing().time() >= datetime.time(15, 5):
            _resume_pipeline_after_trade(
                f"收盘后自动释放（原因={_pipeline_pause_reason or '未知'}）：15:05后不再阻塞数据流水线"
            )
            break
        now_ts = time.time()
        if now_ts - last_log >= 15:
            logger().warning(
                "%s 暂停中：当前交易优先任务=%s；等待平仓/撤单处理完成后继续。",
                context,
                _pipeline_pause_reason or "未知",
            )
            last_log = now_ts
        time.sleep(1)
    if last_log > 0:
        logger().info("%s 暂停结束：交易优先任务已释放，继续执行。", context)


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


def _has_due_close_plan_now() -> bool:
    today_str = today_beijing().strftime("%Y%m%d")
    now_time = now_beijing().time()
    for pos in load_positions():
        status = str(pos.get("status", "")).lower()
        if status == "closed":
            continue
        planned_exit = str(pos.get("planned_exit_date", "99991231"))
        if status == "sell_pending":
            return True
        if planned_exit < today_str:
            return True
        if planned_exit == today_str and now_time >= SCHED_AFTERNOON_CLOSE:
            return True
    return False


def _maybe_resume_pipeline_after_trade() -> None:
    if _pipeline_paused() and not _has_due_close_plan_now() and not _has_premarket_close_plan():
        _resume_pipeline_after_trade("未检测到仍需执行的平仓计划")


def _start_post_market_pipeline(end_date: str | None = None, *, reason: str = "") -> None:
    """后台启动收盘/补采流水线。主调度继续运行；流水线遇到交易暂停门禁会等待。"""
    global _pipeline_thread
    with _pipeline_thread_lock:
        if _pipeline_thread is not None and _pipeline_thread.is_alive():
            logger().warning("收盘/采集流水线已在运行，本次不重复启动：%s", reason or "未指定原因")
            return

        def _target() -> None:
            try:
                _run_post_market_with_retry(end_date)
            finally:
                logger().info("收盘/采集流水线线程结束：%s", reason or "正常结束")

        _pipeline_thread = threading.Thread(target=_target, daemon=True, name="post-market-pipeline")
        _pipeline_thread.start()
        logger().info("收盘/采集流水线已后台启动：%s", reason or "未指定原因")


def run_script(name: str, *args: str, timeout: int = TIMEOUT_DATA_STEP) -> bool:
    import platform as _plat
    import queue as _queue
    import threading as _threading
    _wait_if_pipeline_paused(f"{name} 启动前")
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
        last_progress_at = started_at
        last_output_at = started_at

        while proc.poll() is None:
            try:
                line = line_queue.get(timeout=0.5)
            except _queue.Empty:
                line = ""
            if line:
                last_output_at = time.time()
                output_lines.append(line)
                if "| ERROR |" in line or "Traceback" in line or line.startswith("ERROR:"):
                    logger().error("  [%s] %s", name, line)
                else:
                    logger().info("  [%s] %s", name, line)
            now_ts = time.time()
            if now_ts - last_progress_at >= 15:
                logger().info(
                    "  [%s] 进度：已运行%d秒 / 超时上限%d秒，最近输出%d秒前；仍在执行，请等待...",
                    name,
                    int(now_ts - started_at),
                    timeout,
                    int(now_ts - last_output_at),
                )
                last_progress_at = now_ts
            if now_ts - started_at > timeout:
                proc.kill()
                logger().error(
                    "%s 超时（%ds），已强制终止；最近输出%d秒前",
                    name,
                    timeout,
                    int(now_ts - last_output_at),
                )
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
        logger().info("%s 完成，用时%d秒", name, int(time.time() - started_at))
        return True
    except Exception as e:
        logger().error("%s 执行异常：%s", name, e)
        return False


# ── 定时任务 ───────────────────────────────────────────────────────────────────

def job_premarket_sell() -> None:
    """09:23 集合竞价：仅处理 D 接力让路或历史 sell_pending 的平仓。

    D 默认平仓口径是 T+2 收盘卖，不在 09:23 提前卖。
    只有组合状态机给出 PLAN_SELL_D_FIRST（次日有 A/B/C/E2 接力，需要 D 让路）时，
    才按 T+1 开盘口径在集合竞价卖 D。
    E2/ABC/D默认T+2收盘卖由 14:55 job_afternoon/check_and_close_positions 执行。
    """
    logger().info("===== 集合竞价平仓挂单（09:23）=====")
    # 平仓检查冗余点②：09:20被挤掉时由此兜底（见09:15处注释）
    try:
        check_and_close_positions()
    except Exception as e:
        logger().error("09:23 平仓检查异常：%s", e)
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
                    "09:23 %s %s 策略=%s，回测用收盘价平仓，跳过集合竞价，等待14:55收盘平仓。",
                    ts_code, name, strategy_leg or "未知",
                )
                continue

            force_relay_sell = ts_code in force_d_sell_codes

            # 只处理历史 sell_pending，或因A/B/C/E2接力需要T+1开盘先卖的D持仓。
            # D 默认 T+2 到期日也必须等 14:55 收盘平仓，不在09:23提前卖。
            if pos.get("status") != "sell_pending" and not force_relay_sell:
                if planned_exit <= today_str:
                    logger().info(
                        "09:23 D默认T+2平仓：%s %s 今日到期(%s)，等待14:55收盘平仓，不集合竞价卖出。",
                        ts_code, name, planned_exit,
                    )
                else:
                    logger().info("09:23 持仓 %s %s 计划平仓日 %s，今日无需平仓，跳过。", ts_code, name, planned_exit)
                continue
            if force_relay_sell and planned_exit > today_str:
                logger().warning(
                    "09:23 D接力平仓：%s %s 默认计划平仓日%s，但今日有A/B/C/E2接力买入计划，按回测口径T+1开盘先卖D。",
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
    """09:26 集合竞价成交确认：确认09:15买单 + 同步09:23平仓成交。

    9:25集合竞价撮合完成后，券商委托/持仓会更新：
    1. 先确认09:15预挂买单，成交则立刻记录本地持仓；未成交但仍排队则不撤单。
    2. 再确认09:23平仓卖单，若实盘已无某标的则同步标记平仓。
    3. 若平仓状态发生变化，再刷新组合状态机，给09:30兜底任务使用。
    """
    logger().info("===== 盘前持仓同步（09:26）=====")

    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))
    today_str = today_beijing().strftime("%Y%m%d")

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
    if not live_codes:
        cleared = clear_local_positions_when_broker_empty("盘前持仓同步09:26")
        synced_any = synced_any or cleared > 0
        local_positions = load_positions()
    else:
        _note_broker_has_positions()

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
            _notify("sell_success", "✅ D集合竞价平仓确认",
                    f"{ts_code} {pos.get('name', '')} 实盘持仓已清空，09:26同步标记已平仓。")
            synced_any = True

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


def _combined_paths_for_today() -> tuple[Path, Path]:
    today_str = today_beijing().strftime("%Y%m%d")
    base = PROJECT_ROOT / "reports" / "live_trade" / "combined"
    return (
        base / f"combined_decisions_{today_str}.csv",
        base / f"combined_planned_orders_{today_str}.csv",
    )


def read_cached_combined_decisions():
    """只读取今天已经生成的组合状态机文件，不重新运行脚本。

    09:15 集合竞价挂单必须快：计划在09:00已生成，09:15只读缓存并下单，
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


def job_preopen_plan() -> None:
    """09:00 提前生成组合状态机计划。

    A/B/C/E2/L/model3 的盘前开仓信息都来自上个交易日收盘后已有数据，D策略除外。
    因此开仓计划不需要等到09:20才计算；09:00先生成，09:15可以直接挂单。
    """
    logger().info("===== 盘前计划生成（09:00）=====")
    combined = load_combined_decisions()
    if combined is None:
        # 2026-07-09 事故链第一环：09:00生成失败（共享盘IO慢180秒超时）后
        # 干等到09:15才现场重算，又耗180秒，把09:15预挂拖到09:21、挤掉09:20
        # 平仓检查。失败后立即重试一次，把恢复动作留在09:00~09:06的富余时段。
        logger().warning("09:00 组合状态机生成失败，立即重试一次（避免拖累09:15预挂窗口）。")
        combined = load_combined_decisions()
    if combined is None:
        logger().error("09:00 组合状态机决策生成两次失败，09:15将现场重算（可能影响集合竞价排队）。")
        return
    logger().info("09:00 组合状态机计划已生成，09:15如有开仓计划将直接按涨停价预挂。")
    logger().info("===== 盘前计划生成完成 =====")


def _record_e2_capacity_and_alert(records: list[dict[str, Any]], live_cfg: dict) -> None:
    """E2 容量档案与关停预警。必须在 09:24 关键窗口结束后调用（含共享盘IO与推送）。

    E2 仓位 = min(缩单额, 竞价额×参与率)：账户资金增长后若频繁被竞价盘钳制，
    E2 的绝对收益不再随资金增长，而隔夜持仓风险与运维成本不变。
    钳制比 = 实际可用仓位/计划仓位；竞价额兜底(fallback)的记录不计入容量证据。
    最近 window 笔竞价记录中钳制 ≥ min_hits 次 → 强提醒建议关闭 E2。
    """
    log = logger()
    alert_ratio = float(live_cfg.get("e2_capacity_alert_clamp_ratio", 0.5))
    window = int(live_cfg.get("e2_capacity_alert_window", 5))
    min_hits = int(live_cfg.get("e2_capacity_alert_min_hits", 3))
    try:
        payload = json.loads(E2_CAPACITY_FILE.read_text(encoding="utf-8")) if E2_CAPACITY_FILE.exists() else {}
    except Exception:
        payload = {}
    history = payload.get("records", [])
    for rec in records:
        planned = float(rec.get("planned_amt", 0) or 0)
        rec["clamp_ratio"] = round(float(rec.get("cap", 0) or 0) / planned, 4) if planned > 0 else 1.0
        history.append(rec)
        if rec.get("source") == "auction" and rec["clamp_ratio"] < alert_ratio:
            _notify_async(
                "buy_result", "📉 E2容量提示：仓位被竞价盘钳制",
                f"{rec.get('ts_code','')} {rec.get('name','')} 计划{planned / 1e4:.2f}万，"
                f"竞价容量只允许{float(rec.get('cap', 0)) / 1e4:.2f}万（{rec['clamp_ratio']:.0%}）。"
                f"资金规模已接近E2标的的竞价容量上限。",
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
            "buy_result", "⚠️ E2容量预警：建议评估关闭E2",
            f"最近{len(auction_recent)}笔E2竞价开仓中有{len(hits)}笔被竞价盘钳制"
            f"（被钳制笔平均只能用计划资金的{avg_clamp:.0%}）。明细：{detail}。"
            f"若竞价额普遍几十万级则是资金超容量（E2绝对收益不再随资金增长，隔夜风险不变），"
            f"如决定停用：config的 live_trade.e2_enabled 改为 false（信号照常生成，只停止下单）；"
            f"若竞价额只是近期偶发偏小，可以先观察。",
            level="timeSensitive",
        )
        log.warning("E2容量预警已推送：最近%d笔中%d笔钳制（阈值%.0f%%）。", len(auction_recent), len(hits), alert_ratio * 100)
    payload["records"] = history
    try:
        mkdir_p(E2_CAPACITY_FILE.parent)
        tmp = E2_CAPACITY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(E2_CAPACITY_FILE)
    except Exception as e:
        log.error("E2容量档案写入失败：%s", e)


def _e2_auction_buy_worker(e2_rows: list[Any], broker_cfg: dict, today_str: str) -> None:
    """E2 竞价动态买入：09:24 读实时竞价撮合量，按参与率上限定仓后挂涨停价。

    仓位 = min(资金缩单后金额, 实时竞价成交额 × e2_auction_participation_ratio)。
    竞价额读不到（QMT竞价时段字段可能无效）时用 e2_auction_fallback_amount 兜底。
    E2 标的从不一字开盘，09:24 挂单无排队损失；挂单后并入 pending_buys，
    由既有的 09:30 确认链路和盘前监控线程统一收尾。
    """
    from src.broker_adapter import OrderRequest

    log = logger()
    try:
        # 所有共享盘IO(读配置)在09:15线程启动时就完成,09:24关键窗口内
        # 只剩两次短锁QMT调用(读tick+下单),把慢IO挡在时间窗之外。
        cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
        lt = cfg.get("live_trade", {})
        if not bool(lt.get("e2_enabled", True)):
            log.warning("E2已被 live_trade.e2_enabled=false 关闭，今日%d只E2候选不挂单。", len(e2_rows))
            return
        ratio = float(lt.get("e2_auction_participation_ratio", 0.10))
        fallback_amt = float(lt.get("e2_auction_fallback_amount", 50000))
        capacity_records: list[dict[str, Any]] = []
        target = now_beijing().replace(hour=9, minute=24, second=0, microsecond=0)
        wait = (target - now_beijing()).total_seconds()
        if wait > 0:
            time.sleep(min(wait, 540))
        # 窗口预算监控:9:25撮合前必须完成挂单;超时由09:30补买路径兜底,
        # 这里只负责让日志能看出迟到。
        if now_beijing().time() >= datetime.time(9, 25, 0):
            log.warning("E2竞价买入线程晚于09:25启动执行（可能被IO/锁延误），仍尝试挂单；若错过竞价将由09:30补买兜底。")
        new_pending: list[dict[str, Any]] = []
        for row in e2_rows:
            try:
                ts_code = str(row["ts_code"]); name_s = str(row.get("name", ""))
                planned_amt = float(row.get("round_lot_shares", 0) or 0) * float(row.get("reference_price", 0.0) or 0.0)
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
                price, price_label = _premarket_buy_price(quote, ts_code, name_s, str(row.get("signal_date", "")))
                if price <= 0:
                    log.warning("E2竞价买入：%s 无法取得涨停价，跳过。", ts_code)
                    continue
                qty = int(cap // price // 100) * 100
                if qty <= 0:
                    log.warning("E2竞价买入：%s 动态仓位不足一手（%s，计划%.0f元），放弃本笔。", ts_code, src, planned_amt)
                    _notify("buy_result", "⚠️ E2流动性不足放弃开仓",
                            f"{ts_code} {name_s} 竞价盘过小（{src}），动态仓位不足一手，今日放弃。", level="timeSensitive")
                    continue
                log.warning("⏳ [E2竞价买入] %s %s %d股 %s=%.2f元（动态仓位：%s → %.1f万）",
                            ts_code, name_s, qty, price_label, price, src, qty * price / 1e4)
                request = OrderRequest(
                    ts_code=ts_code, broker_code=str(row.get("broker_code", ts_code)),
                    side="BUY", quantity=qty, price_type="FIXED_PRICE", price=price,
                    strategy_name=str(row.get("strategy_name", "A_SYSTEM_ABC")),
                    remark=f"E2竞价动态-{today_str}",
                )
                with _qmt_lock:
                    adapter = _qmt_get(broker_cfg)
                    result = adapter.place_order(request)
                if result.accepted:
                    raw_exit_n = row.get("exit_n_days", None)
                    exit_n = int(float(raw_exit_n)) if raw_exit_n is not None and str(raw_exit_n) not in {"", "nan"} else 1
                    new_pending.append({
                        "order_id": str(result.order_id or f"e2auction-{today_str}-{ts_code}"),
                        "ts_code": ts_code, "name": name_s,
                        "signal_date": str(row.get("signal_date", "")),
                        "strategy_leg": "E2", "qty": qty, "ref_price": price, "exit_n": exit_n,
                    })
                    log.info("✅ [E2竞价买入] %s %s %d股 @%.2f 已受理（待09:30确认）", ts_code, name_s, qty, price)
                    _notify("buy_result", "📋 E2竞价动态开仓已挂单",
                            f"{ts_code} {name_s} {qty}股@{price:.2f}（{src}），待09:30确认成交。", level="timeSensitive")
                else:
                    log.error("❌ [E2竞价买入] %s 提交失败：%s", ts_code, result.message)
            except Exception as e:
                log.error("E2竞价买入异常（%s）：%s", row.get("ts_code"), e)
        if new_pending:
            existing = load_pending_buys()
            save_pending_buys(existing + new_pending)
            _start_premarket_buy_monitor()
        if capacity_records:
            try:
                _record_e2_capacity_and_alert(capacity_records, lt)
            except Exception as e:
                log.error("E2容量预警统计异常（不影响交易）：%s", e)
    except Exception as e:
        log.error("E2竞价动态买入线程异常：%s", e)


def job_premarket_buy() -> None:
    """09:15 集合竞价预挂：对计划开仓且当前无持仓的标的，优先按涨停价挂限价买单。

    总策略模式说明：
      mode=1：只执行现有 A/B/C/E2 买入计划。
      mode=2：只执行 L 独立龙头策略买入计划。
      mode=3：执行 model=3 组合计划，可能是 mode=1 买入，也可能是 L 补位/替换。
    各模式互斥，避免同一资金被两套策略同时占用。
    """
    logger().info("===== 集合竞价买入预挂（09:15）=====")

    # 平仓最高优先：逾期持仓（如昨日平仓失败残留）先于一切买入动作清理。
    # 2026-07-09 事故：共享盘IO慢→09:15任务拖到09:21→09:20平仓检查被调度器
    # 跳过→逾期仓整个上午无人处理。平仓检查冗余挂载到09:15/09:23/09:30，
    # 任一任务存活即可完成清理；无逾期仓时本调用毫秒级返回。
    try:
        check_and_close_positions()
    except Exception as e:
        logger().error("09:15 平仓检查异常：%s", e)

    if has_position_bought_today():
        logger().info("09:15 今日已有买入成交，跳过集合竞价买入预挂。")
        return

    combined = read_cached_combined_decisions()
    if combined is None:
        logger().warning("09:15 未找到今日组合状态机缓存，临时生成一次；若耗时较长可能影响集合竞价排队。")
        combined = load_combined_decisions()
    decisions          = combined[0] if combined is not None else None
    combined_orders_path = combined[1] if combined is not None else None
    if decisions is None:
        logger().error("09:15 组合状态机决策获取失败，跳过集合竞价买入预挂。")
        return

    if is_strategy_l_mode():
        # L 模式只认 ALLOW_L_BUY；即使旧 ABC/E2 文件还在，也不会在模式2里被执行。
        has_buy_plan = has_combined_action(decisions, "ALLOW_L_BUY")
    elif is_strategy_model3_mode():
        has_buy_plan = (
            has_combined_action(decisions, "ALLOW_MODEL3_L_SUPPLEMENT") or
            has_combined_action(decisions, "ALLOW_MODEL3_L_REPLACE") or
            has_combined_action(decisions, "ALLOW_ABC_BUY_PREVIEW") or
            has_combined_action(decisions, "ALLOW_E2_BUY")
        )
    else:
        has_buy_plan = (
            has_combined_action(decisions, "ALLOW_ABC_BUY_PREVIEW") or
            has_combined_action(decisions, "ALLOW_E2_BUY")
        )
    if not has_buy_plan:
        logger().info("09:15 今日无开仓计划，跳过集合竞价买入预挂。")
        return

    if has_combined_action(decisions, "PLAN_SELL_D_FIRST"):
        logger().info("09:15 组合状态机要求先卖D，跳过集合竞价买入预挂。")
        return

    config     = load_json_config(PROJECT_ROOT / "config" / "config.json")
    qmt_enabled = bool(config.get("broker_adapter_enabled")) and bool(config.get("qmt_enabled"))
    if not qmt_enabled:
        logger().info("[盘前买入] 模拟盘，跳过实盘挂单。")
        return

    import pandas as pd
    if combined_orders_path is None or not combined_orders_path.exists():
        logger().error("09:15 找不到计划单文件，跳过集合竞价买入预挂。")
        return
    try:
        orders = pd.read_csv(combined_orders_path)
    except Exception as e:
        logger().error("09:15 读取计划单失败：%s", e)
        return

    buy_orders = orders[orders.get("side", pd.Series()).astype(str).str.upper() == "BUY"]
    if buy_orders.empty:
        logger().info("09:15 计划单中无买入行，跳过。")
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
    e2_rows: list[Any] = []
    for _, row in buy_orders.iterrows():
        try:
            # E2 专属通道：不在09:15挂单，推迟到09:24读实时竞价撮合量后
            # 按 min(缩单金额, 竞价额×参与率) 动态定仓再挂涨停价参与竞价。
            # 原因：E2标的为冷门小市值，竞价盘常仅几十~几百万（2026-07-09
            # 皇氏实测68万），50万级固定仓位会吃掉大半竞价盘直接顶高开盘价。
            # E2标的从不一字开盘，9:24挂单无排队损失；ABC/L保持9:15排队优势。
            if str(row.get("strategy_leg", "")).upper() == "E2":
                if not bool(config.get("live_trade", {}).get("e2_enabled", True)):
                    logger().warning("E2已被 live_trade.e2_enabled=false 关闭，跳过E2买入计划：%s", row.get("ts_code"))
                    continue
                e2_rows.append(row.copy())
                continue
            ts_code  = str(row["ts_code"])
            name_s   = str(row.get("name", ""))
            qty      = int(row.get("round_lot_shares", 0))
            if qty <= 0:
                continue

            signal_date_s = str(row.get("signal_date", ""))
            quote = quote_map.get(ts_code)
            price, price_label = _premarket_buy_price(quote, ts_code, name_s, signal_date_s)
            if price <= 0:
                logger().warning("09:15 %s %s 无法获取涨停价/估算涨停价/卖档价格，跳过。", ts_code, name_s)
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
                # 09:15集合竞价预挂单09:25开始撮合，此处不立即记录持仓，落盘待确认，09:30按实盘成交确认
                raw_exit_n = row.get("exit_n_days", None)
                exit_n = int(float(raw_exit_n)) if raw_exit_n is not None and str(raw_exit_n) not in {"", "nan"} else 2
                pending_buys.append({
                    "order_id": str(result.order_id or f"premarket-{today_str}-{ts_code}"),
                    "ts_code": ts_code,
                    "name": name_s,
                    "signal_date": signal_date_s,
                    "strategy_leg": str(row.get("strategy_leg", "")),
                    "qty": qty,
                    "ref_price": price,
                    "exit_n": exit_n,
                })
                logger().info("✅ [盘前买入] %s %s %d股 @%.2f 委托已受理（09:15预挂，待09:30确认成交）",
                              ts_code, name_s, qty, price)
            else:
                logger().error("❌ [盘前买入] %s %s 提交失败：%s", ts_code, name_s, result.message)

        except Exception as e:
            logger().error("盘前买入异常（%s）：%s", row.get("ts_code"), e)

    if pending_buys:
        save_pending_buys(pending_buys)
        _start_premarket_buy_monitor()
        # 开仓计划通知：09:15预挂成功即推送，让用户开盘前就知道今日买什么，
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
                "；".join(plan_parts) + "。09:15集合竞价预挂完成，实际按开盘价成交（通常低于预挂价），"
                "09:30确认成交后再推送持仓通知。",
                level="timeSensitive")
    if e2_rows:
        threading.Thread(
            target=_e2_auction_buy_worker,
            args=(e2_rows, broker_cfg, today_str),
            daemon=True,
            name="e2-auction-buy",
        ).start()
        logger().info("E2竞价动态买入线程已启动：09:24读实时竞价量后定仓挂单（%d只候选）。", len(e2_rows))
    logger().info("===== 盘前买入挂单完成（受理%d笔，待开盘确认；E2延迟至09:24=%d笔）=====", len(pending_buys), len(e2_rows))


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

    if is_strategy_l_mode():
        # L 独立模式下，09:20 只做状态播报，不启动 ABC/E2/D。
        # 如果 L 买入开关关闭，组合状态机会给出 BLOCK_L_LIVE_ORDER；如果打开且有昨日信号，会给出 ALLOW_L_BUY。
        if has_combined_action(decisions, "ALLOW_L_BUY"):
            logger().info("当前总策略模式=2（独立L龙头策略），组合状态机允许L开仓；将于09:15/09:30按L计划执行。")
        elif has_combined_action(decisions, "PLAN_SELL_L"):
            logger().info("当前总策略模式=2（独立L龙头策略），存在L到期平仓计划；等待14:55收盘平仓窗口。")
        else:
            logger().info("当前总策略模式=2（独立L龙头策略），本轮无L实盘开仓计划；ABCDE2/D已阻断。")
        logger().info("===== 盘前任务完成 =====")
        return

    # ② D 待卖持仓最高优先级。09:20只播报，等待09:23集合竞价平仓入口执行。
    if has_combined_action(decisions, "PLAN_SELL_D_FIRST"):
        logger().info("组合状态机要求先卖D；等待09:23集合竞价平仓，不在09:20非交易时段提交委托。")
        return

    # ③ A/B/C 买入信号 —— 09:20 只播报，不提交，避免触发 OUTSIDE_TRADING_TIME
    if has_combined_action(decisions, "ALLOW_ABC_BUY_PREVIEW"):
        logger().info("组合状态机允许A/B/C买入；09:15集合竞价预挂，09:30确认成交/必要时补单。")
    else:
        logger().info("组合状态机未允许A/B/C买入，跳过。")

    # ④ E2 T+1 开仓 —— 09:20 只播报，不提交，避免触发 OUTSIDE_TRADING_TIME
    if has_combined_action(decisions, "ALLOW_E2_BUY"):
        logger().info("组合状态机允许E2开仓；09:15集合竞价预挂，09:30确认成交/必要时补单。")
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

    # 平仓检查冗余点③：确保逾期清理不因09:20/09:23被挤而丢失（见09:15处注释）
    try:
        check_and_close_positions()
    except Exception as e:
        logger().error("09:30 平仓检查异常：%s", e)

    # 先确认09:15盘前买单是否在开盘成交，再决定是否需要补单
    try:
        confirm_pending_premarket_buys()
    except Exception as e:
        logger().error("盘前买单成交确认异常：%s —— 请手动核对！", e)

    if has_position_bought_today():
        logger().info("09:30 检测到今日已有买入成交（09:15盘前买入已成交），跳过重复买入。")
        logger().info("===== 开盘买入任务完成 =====")
        return

    if load_pending_buys():
        logger().info("09:30 仍有09:15盘前买单在排队/待补挂，跳过新的开盘补买，避免重复委托。")
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

    if is_strategy_l_mode():
        # L 模式只执行 ALLOW_L_BUY。默认配置 live_order_enabled=false 时不会出现该动作，
        # 因此模式1默认运行、模式2未开启实盘时，都不会误触 L 下单。
        if has_combined_action(decisions, "ALLOW_L_BUY"):
            accepted = handle_combined_order_preview(
                combined_orders_path,
                reason="L 09:30开仓",
                allowed_sides={"BUY"},
            )
            if not accepted and not has_position_bought_today():
                logger().warning("09:30 L开仓未提交成功/未成交，今日不切回ABCDE2/D，避免策略模式混跑。")
        else:
            logger().info("09:30 L模式无 ALLOW_L_BUY，跳过开盘买入。")
        logger().info("===== 开盘买入任务完成 =====")
        return

    # 走到这里=今日有买入窗口但09:15预挂链路没有产生持仓、也没有待确认单
    # （预挂失败/未执行/被拒）。属于执行降级：失去集合竞价排队优势，
    # 补买按最新价成交。当天必须推送告警，不能等用户几天后从成交价反推。
    # （2026-07-03 德冠新材即此场景：预挂未生效，09:30补买20.33，事后才发现。）
    if has_combined_action(decisions, "ALLOW_ABC_BUY_PREVIEW") or has_combined_action(decisions, "ALLOW_E2_BUY"):
        logger().warning("⚠️ 开仓执行降级：09:15盘前预挂未生效（09:30无持仓且无待确认单），转09:30按最新价补买。")
        _notify("buy_result", "⚠️ 开仓执行降级",
                "09:15盘前预挂未生效，已转09:30按最新价补买。请留意成交价与开盘价的差异。",
                level="timeSensitive")

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
    elif not accepted_buy and not has_position_bought_today():
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


def has_open_local_position() -> bool:
    return any(str(p.get("status", "")).lower() in {"open", "sell_pending"} for p in load_positions())


def has_position_bought_today() -> bool:
    """今日已有买入成交的持仓——09:15/09:30 防重复买入的正确判据。

    不能用 has_open_local_position()：衔接日（旧仓当日14:55到期平仓）早上
    买新仓是回测口径的一部分，旧仓(buy_date<今日)的存在不能阻止新仓买入。
    2026-07-06 贤丰控股漏买事故根因：旧仓德冠新材让09:15/09:30全部跳过。"""
    today = today_beijing().strftime("%Y%m%d")
    return any(
        str(p.get("status", "")).lower() in {"open", "sell_pending"}
        and str(p.get("buy_date", "")) == today
        for p in load_positions()
    )


# ── 盘前买单待确认（09:15挂单→09:30开盘成交确认）────────────────────────────

def save_pending_buys(orders: list[dict[str, Any]]) -> None:
    """记录09:15盘前已受理买单，等09:30开盘后确认成交。"""
    try:
        with _pending_buy_lock:
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
        with _pending_buy_lock:
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
        with _pending_buy_lock:
            if PENDING_BUY_FILE.exists():
                PENDING_BUY_FILE.unlink()
    except Exception as e:
        logger().error("清除盘前待确认买单失败：%s", e)


def _replace_pending_buy_order(old_order_id: str, new_order: dict[str, Any] | None) -> None:
    """替换或移除某笔09:15待确认买单，供集合竞价监控线程使用。"""
    try:
        with _pending_buy_lock:
            if not PENDING_BUY_FILE.exists():
                return
            payload = json.loads(PENDING_BUY_FILE.read_text(encoding="utf-8"))
            if payload.get("date") != today_beijing().strftime("%Y%m%d"):
                return
            updated: list[dict[str, Any]] = []
            replaced = False
            for order in payload.get("orders", []):
                if str(order.get("order_id", "")) == str(old_order_id):
                    replaced = True
                    if new_order is not None:
                        updated.append(new_order)
                else:
                    updated.append(order)
            if not replaced:
                return
            if updated:
                tmp = PENDING_BUY_FILE.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps({"date": payload.get("date"), "orders": updated}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp.replace(PENDING_BUY_FILE)
            else:
                PENDING_BUY_FILE.unlink(missing_ok=True)
    except Exception as e:
        logger().error("更新盘前待确认买单失败：%s", e)


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
    record_buy(
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
    )
    amount = int(fill.filled_qty) * float(fill_price)
    logger().info("✅ [盘前买入监控] 持仓信息：策略=%s %s %s 持仓%d股 成本%.2f 市值%s",
                  s.get("strategy_leg", ""), s.get("ts_code", ""), s.get("name", ""),
                  int(fill.filled_qty), fill_price, _fmt_wan(amount))
    _notify("buy_result", "✅ 盘前持仓信息",
            f"策略={s.get('strategy_leg', '')} {s.get('ts_code', '')} {s.get('name', '')} "
            f"持仓{int(fill.filled_qty)}股 成本{fill_price:.2f} 市值{_fmt_wan(amount)}")


def _resubmit_premarket_buy(s: dict[str, Any], broker_cfg: dict, config: dict) -> dict[str, Any] | None:
    """9:15-9:30内发现预挂单被撤/废单后，重新按涨停价/卖档价补挂。"""
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
        quote = adapter.get_full_tick([ts_code]).get(ts_code)

    price, price_label = _premarket_buy_price(quote, ts_code, name_s, str(s.get("signal_date", "")))
    if price <= 0:
        logger().warning("盘前买入监控：%s %s 无法获取补挂价格，等待下一轮。", ts_code, name_s)
        return None

    available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
    total_asset = float(getattr(account, "total_asset", 0.0) or available_cash)
    cash_buffer = float(live_cfg.get("cash_buffer_amount", 1000))
    max_single = float(live_cfg.get("max_single_order_amount", 50000))
    max_pct = float(live_cfg.get("max_position_pct", 0.8))
    usable = min(available_cash - cash_buffer, total_asset * max_pct, max_single)
    max_qty_by_cash = int(usable / price) if usable > 0 and price > 0 else 0
    max_qty_by_cash -= max_qty_by_cash % 100
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
    )
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
    """监控09:15预挂买单；若券商撤单/废单，按涨停价补挂，直到撤单窗口前。"""
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    broker_cfg = config.get("broker", {})
    cutoff = datetime.time(14, 55)
    poll_seconds = 15
    logger().info("盘前买入监控已启动：09:15-14:56 每%d秒检查一次成交/撤单/废单。", poll_seconds)
    while now_beijing().time() < cutoff:
        if has_position_bought_today():
            logger().info("盘前买入监控：今日买入已成交，退出。")
            return
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
                    return
                if getattr(fill, "is_terminal", False):
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
    """确认09:15盘前买单成交。

    已成交按实际成交记录持仓；仍是“已报/部成未终态”的排队单不主动撤，
    继续交给监控线程跟踪，避免把09:15排队优势撤掉。
    """
    pending = load_pending_buys()
    if not pending:
        return

    logger().info("===== 确认盘前买单成交（%s）共%d笔 =====", confirm_source, len(pending))
    config = load_json_config(PROJECT_ROOT / "config" / "config.json")
    broker_cfg = config.get("broker", {})
    today_str = today_beijing().strftime("%Y%m%d")
    still_pending: list[dict[str, Any]] = []

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
                    traded_at=getattr(fill, "traded_at", ""),
                )
                name_s = str(s.get("name", ""))
                amount = fill.filled_qty * fill_price
                if fill.filled_qty < qty:
                    planned_price = float(s.get("ref_price", fill_price) or fill_price)
                    planned_amount = qty * planned_price
                    unfilled_amount = max(planned_amount - amount, 0.0)
                    logger().warning("⚠️ [盘前买入确认] %s 部分成交 %d/%d股 @%.2f，撤残单。",
                                     ts_code, fill.filled_qty, qty, fill_price)
                    _try_cancel_order(broker_cfg, order_id, ts_code)
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
                    still_pending.append(s)
                    logger().warning(
                        "⚠️ [盘前买入确认] %s %s暂未成交（状态=%s），原委托仍在排队，不撤单，继续监控。",
                        ts_code, confirm_source, fill.status_text,
                    )
                else:
                    still_pending.append(s)
                    logger().warning(
                        "⚠️ [盘前买入确认] %s %s未成交且已终态（状态=%s），不跑组合补单，交给监控线程按涨停价补挂。",
                        ts_code, confirm_source, fill.status_text,
                    )
        except Exception as e:
            logger().error("❌ [盘前买入确认] %s 异常：%s —— 请手动核对！", s.get("ts_code"), e)
            _notify("buy_result", "❌ 盘前开仓确认异常",
                    f"{s.get('ts_code','')} 盘前买单成交确认出现异常，请回终端核对持仓。",
                    level="critical", call=True)

    if still_pending and not has_position_bought_today():
        save_pending_buys(still_pending)
        _start_premarket_buy_monitor()
    else:
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
    """09:20 盘前任务后立即启动策略D监控（后台线程，不阻塞 daemon）。

    架构原则：QMT 连接只能由主守护进程持有。D监控在线程内运行，
    通过 SharedQMTBrokerProxy 复用主连接，禁止再启动独立 QMT 子进程。
    监控脚本内部等到09:30开始扫描，10:00起发WATCH提醒，14:00起发BUY信号，14:56自动撤单。
    """
    global _d_monitor_thread
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

        import importlib.util

        d_module_path = PROJECT_ROOT / "scripts" / "monitor_strategy_d_intraday.py"
        d_spec = importlib.util.spec_from_file_location("a_system_strategy_d_monitor", d_module_path)
        if d_spec is None or d_spec.loader is None:
            raise RuntimeError(f"无法加载D监控模块：{d_module_path}")
        d_module = importlib.util.module_from_spec(d_spec)
        sys.modules[d_spec.name] = d_module
        d_spec.loader.exec_module(d_module)
        StrategyDMonitor = d_module.StrategyDMonitor
        configured_allowed_segments = d_module.configured_allowed_segments
        configured_position_pct = d_module.configured_position_pct

        today_str = today_beijing().strftime("%Y%m%d")
        signal_dir = PROJECT_ROOT / "reports" / "strategy_d"
        mkdir_p(signal_dir)
        signal_csv = signal_dir / f"intraday_signals_{today_str}.csv"
        monitor = StrategyDMonitor(
            broker=SharedQMTBrokerProxy(broker_config),
            live_order=live_order,
            logger=logger(),
            signal_csv=signal_csv,
            allowed_segments=configured_allowed_segments(config),
            position_pct=configured_position_pct(config),
            config=config,
        )

        def _run_monitor() -> None:
            try:
                monitor.run()
            except Exception as exc:
                logger().exception("D监控线程异常退出：%s", exc)

        _d_monitor_thread = threading.Thread(
            target=_run_monitor,
            daemon=True,
            name="strategy-d-monitor",
        )
        _d_monitor_thread.start()
        if D_MONITOR_PID_FILE.exists():
            D_MONITOR_PID_FILE.unlink(missing_ok=True)
        logger().info("策略D监控已在线程内启动（live_order=%s，QMT连接复用主守护进程）", live_order)
    except Exception as e:
        logger().error("策略D监控启动失败：%s", e)
    logger().info("===== 策略D监控已在线程内运行，不阻塞主程序 =====")


def stop_strategy_d_monitor(reason: str = "") -> None:
    """停止 D 盘中监控。

    新架构下 D 监控是 daemon 线程，跟随主守护进程退出；这里主要兼容清理旧版子进程PID。
    """
    try:
        if _d_monitor_thread is not None and _d_monitor_thread.is_alive():
            logger().info("D策略监控为主进程内线程，将随主守护进程退出。%s", f"原因：{reason}" if reason else "")
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


def _csv_readiness(path: Path, signal_date: str, required_columns: list[str]) -> dict[str, Any]:
    """轻量检查某个因子文件是否包含信号日数据和必需字段。"""
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "rows": 0,
        "missing_columns": [],
        "ok": False,
        "data_available_false": None,
    }
    if not path.exists():
        result["missing_columns"] = required_columns
        return result
    try:
        import pandas as pd

        header = pd.read_csv(path, nrows=0).columns.tolist()
        missing = [c for c in required_columns if c not in header]
        result["missing_columns"] = missing
        if "trade_date" not in header:
            return result
        usecols = sorted(set(["trade_date", "data_available", *required_columns]) & set(header))
        data = pd.read_csv(path, usecols=usecols, dtype={"trade_date": str}, low_memory=False)
        day = data[data["trade_date"].astype(str).eq(signal_date)].copy()
        result["rows"] = int(len(day))
        if "data_available" in day.columns and len(day) > 0:
            available = day["data_available"].astype(str).str.lower().isin({"true", "1", "1.0"})
            result["data_available_false"] = int((~available).sum())
        result["ok"] = len(day) > 0 and not missing
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        return result


def _json_signal_has_date(path: Path, signal_date: str) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return any(str(item.get("signal_date", "")) == signal_date for item in data.get("signals", []))
    except Exception:
        return False


def report_next_trade_factor_readiness(signal_date: str) -> bool:
    """收盘后确认下个交易日开盘计算所需因子是否准备齐全。

    口径说明：
    - T日收盘能准备 T日涨停/日线/情绪/题材/资金/龙虎榜，用于 T+1 09:00 计划。
    - T+1集合竞价和开盘5分钟是真实未来盘中数据，不可能在T日晚提前获取；这里只检查审计文件。
    """
    try:
        signal_dt = datetime.datetime.strptime(signal_date, "%Y%m%d").date()
    except Exception:
        logger().warning("因子就绪检查跳过：signal_date非法=%s", signal_date)
        return False
    next_date = next_n_trade_days(signal_dt, 1).strftime("%Y%m%d")
    processed = PROJECT_ROOT / "data" / "processed"
    raw = PROJECT_ROOT / "data" / "raw"
    checks = [
        ("raw日线", raw / "daily" / f"{signal_date}.csv", ["trade_date", "ts_code", "open", "close", "amount"], True, False),
        ("raw每日基本面", raw / "daily_basic" / f"{signal_date}.csv", ["trade_date", "ts_code", "turnover_rate", "volume_ratio", "circ_mv"], True, False),
        ("raw涨停池", raw / "limit_list" / f"{signal_date}.csv", ["trade_date", "ts_code", "first_time", "last_time", "open_times", "limit_times"], True, False),
        ("实盘涨停合并", processed / "live_limit_up_merged.csv", ["trade_date", "ts_code", "fd_amount", "fd_amount_to_circ_mv", "market_segment"], True, False),
        ("成交概率打分", processed / "live_limit_up_fill_scored.csv", ["trade_date", "ts_code", "fill_probability", "allow_buy_reliable", "is_fill_score_reliable"], True, False),
        ("市场情绪", processed / "live_market_emotion_features.csv", ["trade_date", "market_segment", "market_chain_count", "segment_emotion_state"], True, False),
        ("题材热度", processed / "live_theme_heat_features.csv", ["trade_date", "ts_code", "theme_name", "theme_heat_rank", "theme_limit_count"], True, False),
        # 资金流和龙虎榜是T日收盘后应尽量补齐的增强因子；如果整日数据全不可用，继续走5分钟重试。
        ("资金流增强", processed / "sector_moneyflow_features.csv", ["trade_date", "ts_code", "sector_moneyflow_score"], False, True),
        ("龙虎榜增强", processed / "top_list_features.csv", ["trade_date", "ts_code", "top_list_net_buy_score"], False, True),
        # 集合竞价/开盘5分钟属于T+1盘中数据，T日晚只能生成审计占位，不能用未来数据伪造分数。
        ("集合竞价审计", processed / "auction_features.csv", ["trade_date", "ts_code", "auction_strength_score"], False, False),
        ("开盘5分钟审计", processed / "open_5m_features.csv", ["trade_date", "ts_code", "open_5m_strength_score"], False, False),
    ]

    critical_missing: list[str] = []
    retry_missing: list[str] = []
    enhanced_missing: list[str] = []
    logger().info("----- 明日开盘因子就绪检查：signal_date=%s next_trade_date=%s -----", signal_date, next_date)
    for name, path, cols, critical, require_available in checks:
        status = _csv_readiness(path, signal_date, cols)
        detail = f"{name}: rows={status['rows']} file={path.name}"
        if status.get("data_available_false") not in (None, 0):
            detail += f" data_available_false={status['data_available_false']}"
        all_unavailable = (
            require_available
            and int(status.get("rows") or 0) > 0
            and status.get("data_available_false") == status.get("rows")
        )
        if status["ok"]:
            if all_unavailable:
                logger().warning("  ⚠️ %s 当日数据全不可用，将等待接口补齐", detail)
                retry_missing.append(name)
            else:
                logger().info("  ✅ %s", detail)
        else:
            missing_text = ",".join(status.get("missing_columns") or [])
            err = status.get("error", "")
            logger().warning("  ⚠️ %s 缺失字段=%s 错误=%s", detail, missing_text or "无", err or "无")
            if critical:
                critical_missing.append(name)
            elif require_available:
                retry_missing.append(name)
            else:
                enhanced_missing.append(name)

    abc_ready = _has_signal_for_date(signal_dt)
    e2_ready = _json_signal_has_date(PROJECT_ROOT / "reports" / "strategy_e2" / "e2_signals_recent.json", signal_date)
    l_ready = _json_signal_has_date(PROJECT_ROOT / "reports" / "strategy_l" / "l_signals_recent.json", signal_date)
    logger().info("  %s A/B/C计划或空计划文件已生成", "✅" if abc_ready else "⚠️")
    logger().info("  %s E2信号文件含signal_date=%s", "✅" if e2_ready else "⚠️", signal_date)
    logger().info("  %s L信号文件含signal_date=%s", "✅" if l_ready else "⚠️", signal_date)
    if not abc_ready:
        critical_missing.append("A/B/C planned_orders")
    if not e2_ready:
        enhanced_missing.append("E2 signal（可能是当日不触发）")
    if not l_ready:
        enhanced_missing.append("L signal（可能是当日不触发）")

    if critical_missing:
        body = f"signal_date={signal_date} next={next_date} 缺关键因子/计划：{', '.join(critical_missing)}。明日09:00计划可能不可用。"
        logger().error("❌ 明日开盘关键因子未齐：%s", body)
        _notify("system_error", "❌ 明日开盘关键因子未齐", body, level="critical", call=True)
        logger().info("----- 明日开盘因子就绪检查结束 -----")
        return False
    if retry_missing:
        body = f"signal_date={signal_date} next={next_date} 待补齐增强因子：{', '.join(retry_missing)}。收盘流水线将5分钟后重试。"
        logger().warning("⚠️ 明日开盘增强因子未齐：%s", body)
        logger().info("----- 明日开盘因子就绪检查结束 -----")
        return False
    elif enhanced_missing:
        logger().warning("⚠️ 明日开盘关键因子已齐，增强/备用项缺失：%s", ", ".join(enhanced_missing))
    else:
        logger().info("✅ 明日开盘因子已齐：ABCE2/L/D 所需关键文件均已准备")
    logger().info("----- 明日开盘因子就绪检查结束 -----")
    return True


def _strategy_d_monitor_running() -> bool:
    try:
        if _d_monitor_thread is not None and _d_monitor_thread.is_alive():
            return True
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


def _processed_data_ready_for_date(target_date: datetime.date) -> bool:
    """启动自检：实盘审计依赖的 processed 表必须包含目标信号日。"""
    required_paths = [
        _prefer_live_processed_path("live_market_sentiment.csv", "market_sentiment.csv"),
        _prefer_live_processed_path("live_market_emotion_features.csv", "market_emotion_features.csv"),
        _prefer_live_processed_path("live_limit_up_fill_scored.csv", "limit_up_fill_scored.csv"),
    ]
    return all(_date_in_csv(path, target_date, "trade_date") for path in required_paths)


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
    """按ask5/ask1/last_price挂FIXED_PRICE限价买单，不走CSV→validate流水线。

    返回 True 仅表示已真实成交并记账；False 表示本次未成交/未提交。
    终态废单会在调用方通过 _last_e2_terminal_reject 判断后停止重试。
    """
    from src.broker_adapter import OrderRequest
    from src.live_order_gateway import LiveOrderGateway

    today_str = today_beijing().strftime("%Y%m%d")
    log = logger()
    _e2_place_order_direct.last_terminal_reject = False  # type: ignore[attr-defined]

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
            traded_at=getattr(fill, "traded_at", ""),
        )
        amount = fill.filled_qty * fill_price
        if fill.filled_qty < qty:
            planned_amount = qty * price
            unfilled_amount = max(planned_amount - amount, 0.0)
            log.warning("⚠️ [E2延迟开仓] %s 部分成交 %d/%d股 @%.2f，按实际成交记录持仓。",
                        ts_code, fill.filled_qty, qty, fill_price)
            _notify("buy_result", "⚠️ E2开仓部分成交",
                    f"策略={strategy_leg} {ts_code} {name} 成交{fill.filled_qty}/{qty}股 "
                    f"@{fill_price:.2f}。总委托金额{_fmt_wan(planned_amount)}，"
                    f"已成交金额{_fmt_wan(amount)}，未成交金额{_fmt_wan(unfilled_amount)}")
        else:
            log.info("✅ [E2延迟开仓] 持仓信息：策略=%s %s %s 持仓%d股 成本%.2f 市值%s",
                     strategy_leg, ts_code, name, fill.filled_qty, fill_price, _fmt_wan(amount))
            _notify("buy_result", "✅ E2持仓信息",
                    f"策略={strategy_leg} {ts_code} {name} "
                    f"持仓{fill.filled_qty}股 成本{fill_price:.2f} 市值{_fmt_wan(amount)}")
        return True
    else:
        detail = _format_order_fill_detail(fill)
        log.error("❌ [E2延迟开仓] %s %s 未成交（%s），不记录持仓，避免幽灵持仓。",
                  ts_code, name, detail)
        if fill.is_terminal:
            _e2_place_order_direct.last_terminal_reject = True  # type: ignore[attr-defined]
        return False


def _e2_delayed_buy_loop(combined_orders_path, decisions) -> None:
    """后台线程：9:31-13:30 内每60秒检查价格条件，满足则用ask5/last_price挂FIXED_PRICE单。

    价格条件：当前价（或最新价）≤ 今日开盘价 × 1.02。
    午间11:30-13:00不提交委托，避免券商返回废单；13:00后继续检查。
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

        if has_position_bought_today():
            log.info("E2延迟开仓：今日买入已成交，退出重试线程。")
            return

        if now.time() >= CUTOFF:
            log.warning("E2延迟开仓：已过13:30截止时间，放弃开仓，补启动D监控。")
            if not _strategy_d_monitor_running():
                job_strategy_d()
            return

        if not market_is_open():
            log.info("E2延迟开仓：当前不在连续竞价时段，等待下一轮检查，不提交委托。")
            continue

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
        terminal_reject_any = False
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
            if bool(getattr(_e2_place_order_direct, "last_terminal_reject", False)):
                terminal_reject_any = True

        if placed_any:
            log.info("E2延迟开仓：提交成功，退出重试线程。")
            return
        if terminal_reject_any:
            log.warning(
                "E2延迟开仓：券商已返回终态废单/终态未成交，停止重复提交同一计划，释放资金占用并补启动D监控。"
            )
            if not _strategy_d_monitor_running():
                job_strategy_d()
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

    09:15-09:30 属于集合竞价预挂窗口，若守护进程刚启动或09:00任务错过，
    这里会立即补挂计划买单；09:30之后不在这里执行 A/B/C 买入预览，避免盘中重启重复触发开仓动作；
    只读取组合状态机，如果它明确允许 D 才补启动监控。
    对于 E2 开仓：若 9:30 后市场仍开盘（14:00 前），允许延迟重试（涨幅≤2%%）。
    """
    now = now_beijing()
    if not is_trade_day(now.date()):
        return
    if datetime.time(9, 0) <= now.time() < datetime.time(9, 15):
        logger().info("启动补检：当前已过09:00未到09:15，先补生成今日组合计划。")
        job_preopen_plan()
        return
    if datetime.time(9, 15) <= now.time() < datetime.time(9, 30):
        logger().warning("启动补检：当前已过09:15未到09:30，立即补执行集合竞价买入预挂。")
        job_premarket_buy()
        return
    if not (datetime.time(9, 20) <= now.time() < datetime.time(14, 55)):
        return
    if _strategy_d_monitor_running():
        logger().info("启动补检：D策略监控已在运行。")
        return
    logger().info("启动补检：当前处于D盘中监控时段，优先读取今日组合状态机缓存，检查是否需要补启动D。")
    # 启动补检不是正式计划生成点，不能在非关键时段再重跑一次组合状态机。
    # 09:00 会生成当天计划；09:15 会直接读缓存挂单。这里若重新运行
    # run_combined_live_plan.py，容易和启动候选播报/信号审计同时抢 CPU 与文件，
    # 造成 180 秒超时，并拖慢 QMT 心跳。缓存不存在时保守跳过，等下一轮定时任务处理。
    combined = read_cached_combined_decisions()
    decisions = combined[0] if combined is not None else None
    combined_orders_path = combined[1] if combined is not None else None
    if decisions is None:
        logger().warning("启动补检：今日组合状态机缓存不存在或读取失败，不重算、不补启动D。")
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


def _sleep_until_beijing(target: datetime.time, *, max_wait: float = 300.0) -> None:
    """阻塞到北京时间当天的 target 时刻；已过则立即返回。

    max_wait 兜底防止时钟异常导致超长阻塞（正常场景 14:55→14:56 只等约120秒）。
    """
    now = now_beijing()
    target_dt = datetime.datetime.combine(now.date(), target, tzinfo=BEIJING_TZ)
    wait = (target_dt - now).total_seconds()
    if wait <= 0:
        return
    time.sleep(min(wait, max_wait))


def job_afternoon() -> None:
    logger().info("===== 盘中任务（14:55 收盘平仓 → 14:40 撤未成交买单）=====")
    close_plan_exists = _has_due_close_plan_now()
    if close_plan_exists:
        _pause_pipeline_for_trade("14:55收盘平仓计划")
    else:
        logger().info("14:55 未检测到到期平仓计划，流水线无需暂停。")

    # ① 平仓最高优先：任何情况下先执行，绝不被组合状态机刷新/取数/超时阻塞。
    #    E2 SELL 依赖的 combined_planned_orders 今日文件已在 09:00/09:20/09:26 生成
    #    （SELL 行仅按 planned_exit_date<=today 产生，与盘中这次刷新无关），
    #    且平仓价在执行时实时取买10/买5，不从文件读死，因此先平仓完全安全。
    try:
        check_and_close_positions()
    except Exception as e:
        logger().error("平仓检查异常：%s —— 请立即手动检查持仓！", e)
    finally:
        if close_plan_exists:
            if _has_due_close_plan_now():
                logger().warning("14:55平仓后仍检测到待平仓计划，流水线保持暂停；等待后续成交确认/人工处理。")
            else:
                _resume_pipeline_after_trade("14:55收盘平仓处理完成")

    # ②（2026-07-15 起）撤未成交买单已前移至 14:40 独立调度执行，
    #    平仓时点独占 QMT 通道，此处不再有任何撤单动作。

    # ③ 平仓完成后刷新组合状态机（后台线程，绝不阻塞调度/关键动作）。
    #    此步即便超时被强杀，也不影响上面已执行完的平仓与撤单。
    def _refresh_combined_plan() -> None:
        try:
            run_script("run_combined_live_plan.py", timeout=TIMEOUT_COMBINED_PLAN_STEP)
        except Exception as e:
            logger().error("刷新组合状态机失败：%s", e)

    threading.Thread(target=_refresh_combined_plan, daemon=True, name="combined-refresh-after-close").start()

    logger().info("===== 盘中任务完成 =====")


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
    OWN_BUY_PREFIXES = ("A_SYSTEM", "E2竞价动态")
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
                "14:56撤未成交买单" if failed == 0 else "⚠️ 14:40撤买单部分失败",
                f"撤买单={cancelled}笔 失败={failed}笔 保留卖单={kept_sell}笔 终态跳过={skipped}笔",
                level="timeSensitive" if failed == 0 else "critical",
            )
        except Exception:
            pass

    log.info("===== 14:40 撤未成交买单完成 =====")


def _log_collection_brief(signal_date: str) -> None:
    try:
        import pandas as pd

        daily_path = PROJECT_ROOT / "data" / "raw" / "daily" / f"{signal_date}.csv"
        basic_path = PROJECT_ROOT / "data" / "raw" / "daily_basic" / f"{signal_date}.csv"
        limit_path = PROJECT_ROOT / "data" / "raw" / "limit_list" / f"{signal_date}.csv"
        daily_rows = len(pd.read_csv(daily_path, low_memory=False)) if daily_path.exists() else 0
        basic_rows = len(pd.read_csv(basic_path, low_memory=False)) if basic_path.exists() else 0
        if not limit_path.exists():
            logger().warning("涨停数据状态：❌ %s 涨停池文件不存在：%s", signal_date, limit_path)
            return
        limit_df = pd.read_csv(limit_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False)
        open_times = pd.to_numeric(limit_df.get("open_times"), errors="coerce").fillna(0)
        one_word = int((open_times == 0).sum()) if "open_times" in limit_df.columns else 0
        opened = int((open_times > 0).sum()) if "open_times" in limit_df.columns else 0
        logger().info(
            "采集结果：✅ %s raw日线=%s行，每日基本面=%s行，涨停池=%s行",
            signal_date,
            daily_rows,
            basic_rows,
            len(limit_df),
        )
        logger().info(
            "涨停数据状态：✅ %s 涨停池已获取，数量=%d，一字板=%d，开板/炸板=%d，source={%s} quality={%s} compatible={%s}",
            signal_date,
            len(limit_df),
            one_word,
            opened,
            _value_counts_text(limit_df, "limit_data_source"),
            _value_counts_text(limit_df, "limit_data_quality"),
            _value_counts_text(limit_df, "strategy_compatible"),
        )
    except Exception as exc:
        logger().warning("采集结果摘要失败：%s", exc)


def _log_cleaning_brief(signal_date: str) -> None:
    try:
        import pandas as pd

        daily_path = PROJECT_ROOT / "data" / "processed" / "daily_merged_by_date" / f"{signal_date}.csv"
        limit_path = _prefer_live_processed_path("live_limit_up_merged.csv", "limit_up_merged.csv")
        daily_rows = len(pd.read_csv(daily_path, low_memory=False)) if daily_path.exists() else 0
        limit_df = pd.read_csv(limit_path, dtype={"trade_date": str, "ts_code": str}, low_memory=False) if limit_path.exists() else pd.DataFrame()
        if not limit_df.empty and "trade_date" in limit_df.columns:
            limit_df = limit_df[limit_df["trade_date"].astype(str).eq(str(signal_date))].copy()
        logger().info(
            "清洗结果：%s 日线合并 rows=%s；涨停合并 rows=%s source={%s} quality={%s}",
            signal_date,
            daily_rows,
            len(limit_df),
            _value_counts_text(limit_df, "limit_data_source"),
            _value_counts_text(limit_df, "limit_data_quality"),
        )
    except Exception as exc:
        logger().warning("清洗结果摘要失败：%s", exc)


def _log_dynamic_feature_brief(signal_date: str) -> None:
    try:
        import pandas as pd

        sentiment_path = _prefer_live_processed_path("live_market_sentiment.csv", "market_sentiment.csv")
        emotion_path = _prefer_live_processed_path("live_market_emotion_features.csv", "market_emotion_features.csv")
        theme_path = _prefer_live_processed_path("live_theme_heat_features.csv", "theme_heat_features.csv")
        sentiment = pd.read_csv(sentiment_path, dtype={"trade_date": str}, low_memory=False) if sentiment_path.exists() else pd.DataFrame()
        emotion = pd.read_csv(emotion_path, dtype={"trade_date": str}, low_memory=False) if emotion_path.exists() else pd.DataFrame()
        theme = pd.read_csv(theme_path, dtype={"trade_date": str}, low_memory=False) if theme_path.exists() else pd.DataFrame()
        srow = sentiment[sentiment["trade_date"].astype(str).eq(str(signal_date))].iloc[0] if not sentiment.empty and "trade_date" in sentiment.columns and not sentiment[sentiment["trade_date"].astype(str).eq(str(signal_date))].empty else pd.Series(dtype=object)
        erow = emotion[emotion["trade_date"].astype(str).eq(str(signal_date))].iloc[0] if not emotion.empty and "trade_date" in emotion.columns and not emotion[emotion["trade_date"].astype(str).eq(str(signal_date))].empty else pd.Series(dtype=object)
        theme_daily = theme[theme["trade_date"].astype(str).eq(str(signal_date))].copy() if not theme.empty and "trade_date" in theme.columns else pd.DataFrame()
        lead_count = int(pd.to_numeric(theme_daily.get("theme_limit_count"), errors="coerce").fillna(0).ge(2).sum()) if not theme_daily.empty else 0
        top_theme = str(theme_daily.iloc[0].get("theme_name", theme_daily.iloc[0].get("theme", ""))) if not theme_daily.empty else "NA"
        logger().info(
            "动态特征：✅ 市场情绪已计算，全市场涨停=%s，跌停=%s，连板数=%s，最高板=%s",
            srow.get("limit_up_count", "NA"),
            erow.get("market_limit_down_count", "NA"),
            erow.get("market_chain_count", srow.get("limit_up_max_height", "NA")),
            srow.get("limit_up_max_height", "NA"),
        )
        logger().info("动态特征：✅ 题材热度已计算，rows=%d，主线样本=%d，首位题材=%s", len(theme_daily), lead_count, top_theme)
    except Exception as exc:
        logger().warning("动态特征摘要失败：%s", exc)


def _log_fill_score_brief(signal_date: str) -> None:
    try:
        import pandas as pd

        path = _prefer_live_processed_path("live_limit_up_fill_scored.csv", "limit_up_fill_scored.csv")
        data = pd.read_csv(path, dtype={"trade_date": str}, low_memory=False) if path.exists() else pd.DataFrame()
        daily = data[data["trade_date"].astype(str).eq(str(signal_date))].copy() if not data.empty and "trade_date" in data.columns else pd.DataFrame()
        prob_source = daily["fill_probability"] if "fill_probability" in daily.columns else pd.Series(dtype=float)
        prob = pd.to_numeric(prob_source, errors="coerce")
        if prob.empty or prob.isna().all():
            fallback_source = daily["estimated_fill_probability"] if "estimated_fill_probability" in daily.columns else pd.Series(dtype=float)
            prob = pd.to_numeric(fallback_source, errors="coerce")
        avg_text = "NA" if prob.isna().all() else f"{prob.mean() * 100:.1f}%"
        logger().info(
            "成交概率：✅ %s 打分完成，rows=%d，平均成交概率=%s，allow_buy_reliable={%s}，score_reliable={%s}",
            signal_date,
            len(daily),
            avg_text,
            _value_counts_text(daily, "allow_buy_reliable"),
            _value_counts_text(daily, "is_fill_score_reliable"),
        )
    except Exception as exc:
        logger().warning("成交概率摘要失败：%s", exc)


def _log_enhanced_feature_brief(signal_date: str) -> None:
    try:
        import pandas as pd

        items = [
            ("资金流", PROJECT_ROOT / "data" / "processed" / "sector_moneyflow_features.csv"),
            ("龙虎榜", PROJECT_ROOT / "data" / "processed" / "top_list_features.csv"),
            ("集合竞价", PROJECT_ROOT / "data" / "processed" / "auction_features.csv"),
            ("开盘5分钟", PROJECT_ROOT / "data" / "processed" / "open_5m_features.csv"),
        ]
        parts = []
        for label, path in items:
            data = pd.read_csv(path, dtype={"trade_date": str}, low_memory=False) if path.exists() else pd.DataFrame()
            daily = data[data["trade_date"].astype(str).eq(str(signal_date))].copy() if not data.empty and "trade_date" in data.columns else pd.DataFrame()
            available = len(daily)
            if "data_available" in daily.columns:
                available = int(daily["data_available"].fillna(False).astype(bool).sum())
            parts.append(f"{label}=rows{len(daily)}/available{available}")
        logger().info("增强因子：✅ %s %s", signal_date, "；".join(parts))
    except Exception as exc:
        logger().warning("增强因子摘要失败：%s", exc)


def _log_abc_strategy_brief(signal_date: str) -> None:
    try:
        import pandas as pd

        checklist = _load_ab_checklist(signal_date)
        if checklist.empty:
            logger().warning("A/B/C策略状态：⚠️ 未找到 %s checklist，A/B/C 本步可能未完成", signal_date)
            return
        row = checklist.iloc[0]
        planned_count = int(float(row.get("planned_order_count", 0) or 0))
        if planned_count > 0:
            logger().info(
                "A/B/C策略状态：✅ 符合开仓条件，计划单=%d，选中=%s %s，operation=%s，selection=%s",
                planned_count,
                row.get("top_ts_code", ""),
                row.get("top_name", ""),
                row.get("operation_status", ""),
                row.get("selection_status", ""),
            )
        else:
            logger().info(
                "A/B/C策略状态：ℹ️ 今日暂无可执行计划单；原因：selection=%s（%s） operation=%s（%s）",
                row.get("selection_status", ""),
                row.get("selection_status_desc", ""),
                row.get("operation_status", ""),
                row.get("operation_status_desc", ""),
            )
            logger().info(
                "A/B/C候选分布：A=%s，B=%s，B过滤=%s，C=%s，C过滤=%s，最终选中=%s",
                row.get("a_candidate_count", 0),
                row.get("b_candidate_count", 0),
                row.get("b_rejected_by_filter_count", 0),
                row.get("c_candidate_count", 0),
                row.get("c_rejected_by_filter_count", 0),
                row.get("selected_count", 0),
            )
    except Exception as exc:
        logger().warning("A/B/C策略摘要失败：%s", exc)


def _log_post_market_step_brief(script: str, signal_date: str) -> None:
    if script == "collect_all_data.py":
        _log_collection_brief(signal_date)
    elif script == "clean_collected_data.py":
        _log_cleaning_brief(signal_date)
    elif script == "build_dynamic_features.py":
        _log_dynamic_feature_brief(signal_date)
    elif script == "score_limit_up_fill_probability.py":
        _log_fill_score_brief(signal_date)
    elif script == "build_live_enhanced_features.py":
        _log_enhanced_feature_brief(signal_date)
    elif script == "run_paper_ab_filtered_daily_ops.py":
        _log_abc_strategy_brief(signal_date)
    elif script == "run_strategy_e2_signal.py":
        _log_e2_signal_status(signal_date)
    elif script == "run_strategy_l_signal.py":
        _log_l_model3_signal_status(signal_date)


def job_post_market(end_date: str | None = None) -> None:
    target_str = end_date or today_beijing().strftime("%Y%m%d")
    target_date = datetime.datetime.strptime(target_str, "%Y%m%d").date()
    logger().info("===== 收盘流水线（目标日期 %s）=====", target_str)

    cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
    live_window_days = max(1, int(cfg.get("cleaning", {}).get("live_signal_window_trade_days", 3)))
    # 实盘只维护策略判断所需的最近N个交易日缓存。
    # 1天策略从窗口里读目标日；L/市场状态需要前两日上下文，所以默认N=3。
    # 已有 raw 缓存由 collect_all_data.py 自动跳过，缺哪天才补哪天；回测研究大表不在这里更新。
    recent_start_date = prev_n_trade_days(target_date, live_window_days - 1) if live_window_days > 1 else target_date
    recent_start = recent_start_date.strftime("%Y%m%d")
    live_limit_up_path = "data/processed/live_limit_up_merged.csv"
    live_market_sentiment_path = "data/processed/live_market_sentiment.csv"
    live_market_emotion_path = "data/processed/live_market_emotion_features.csv"
    live_theme_heat_path = "data/processed/live_theme_heat_features.csv"
    live_fill_scored_path = "data/processed/live_limit_up_fill_scored.csv"

    steps = [
        ("collect_all_data.py",               "① 采集日线 + 涨停池",             TIMEOUT_DATA_STEP,  "约1分钟"),
        ("clean_collected_data.py",            "② 清洗合并数据",                   TIMEOUT_DATA_STEP,  "约1分钟"),
        ("build_dynamic_features.py",          "③ 市场情绪 / 题材热度",            TIMEOUT_DATA_STEP,  "约1分钟"),
        ("score_limit_up_fill_probability.py", "④ 涨停成交概率打分",               TIMEOUT_DATA_STEP,  "约1分钟"),
        ("build_live_enhanced_features.py",    "⑤ 增强因子生成（资金流/龙虎榜/竞价审计）", TIMEOUT_DATA_STEP, "约1分钟"),
        ("run_paper_ab_filtered_daily_ops.py", "⑥ A+B+C 信号生成",                TIMEOUT_SIGNAL_STEP,"约1分钟"),
        ("run_strategy_e2_signal.py",          "⑦ E2 信号生成（板块中性小市值）", TIMEOUT_SIGNAL_STEP,"约30秒"),
        ("run_strategy_l_signal.py",           "⑧ L 龙头信号生成（独立模式备用）", TIMEOUT_SIGNAL_STEP,"约30秒"),
    ]
    extra_args: dict[str, list[str]] = {
        "collect_all_data.py": ["--start-date", recent_start, "--end-date", target_str, "--require-end-date-limit"],
        "clean_collected_data.py": ["--start-date", recent_start, "--end-date", target_str, "--incremental-replace"],
        "build_dynamic_features.py": [
            "--start-date", target_str,
            "--end-date", target_str,
            "--limit-up-path", live_limit_up_path,
            "--market-emotion-output", live_market_emotion_path,
            "--theme-heat-output", live_theme_heat_path,
        ],
        "score_limit_up_fill_probability.py": [
            "--input-path", live_limit_up_path,
            "--output-path", live_fill_scored_path,
            "--market-sentiment-path", live_market_sentiment_path,
        ],
        "build_live_enhanced_features.py": [
            "--trade-date", target_str,
            "--input-path", live_fill_scored_path,
            "--max-trade-days", "10",
        ],
        "run_paper_ab_filtered_daily_ops.py": [
            "--signal-date", target_str,
            "--top-n", "10",
            "--input-trades-path", live_fill_scored_path,
            "--fill-scored-path", live_fill_scored_path,
            "--market-emotion-features-path", live_market_emotion_path,
            "--theme-heat-features-path", live_theme_heat_path,
        ],
        "run_strategy_e2_signal.py": ["--signal-date", target_str],
        # L 信号默认只落文件，不会接入实盘；必须 mode=2 且 strategy_l.live_order_enabled=true
        # 时，组合状态机才会把昨日 L 信号转换为次日实盘买入计划。
        "run_strategy_l_signal.py": ["--signal-date", target_str],
    }
    critical_scripts = {
        "collect_all_data.py",
        "clean_collected_data.py",
        "score_limit_up_fill_probability.py",
    }
    # 信号生成步骤：失败时本趟内立即用已缓存数据就地重试一次（不阻断后续、不停止流水线）。
    # A/B/C 在慢 IO 夜晚易被单步超时误杀；此时 raw/清洗数据已缓存，重试仅剩加载+选股，命中率高。
    signal_retry_scripts = {
        "run_paper_ab_filtered_daily_ops.py",
        "run_strategy_e2_signal.py",
        "run_strategy_l_signal.py",
    }

    total_steps = len(steps)
    for step_index, (script, desc, timeout, eta) in enumerate(steps, 1):
        try:
            logger().info(
                "收盘流水线进度：%d/%d %s（预计%s，单步超时上限%d秒）",
                step_index,
                total_steps,
                desc,
                eta,
                timeout,
            )
            args = extra_args.get(script, [])
            ok = run_script(script, *args, timeout=timeout)
            if not ok:
                if script in critical_scripts:
                    logger().warning("收盘流水线进度：%d/%d %s 第一次失败，等待10秒后自动重试一次", step_index, total_steps, desc)
                    time.sleep(10)
                    ok = run_script(script, *args, timeout=timeout)
                elif script in signal_retry_scripts:
                    logger().warning("收盘流水线进度：%d/%d %s 第一次失败（不阻断后续步骤），等待10秒后就地重试一次", step_index, total_steps, desc)
                    time.sleep(10)
                    ok = run_script(script, *args, timeout=timeout)
                if not ok and script in critical_scripts:
                    logger().error("❌ %s 仍然失败，本次收盘流水线停止；不生成计划单，避免使用旧信号", desc)
                    return False
                if not ok:
                    logger().error("%s 失败，继续后续步骤", desc)
                    _log_post_market_step_brief(script, target_str)
                    continue
            logger().info("收盘流水线进度：%d/%d %s 完成", step_index, total_steps, desc)
            _log_post_market_step_brief(script, target_str)
        except Exception as e:
            if script in critical_scripts:
                logger().error("❌ %s 异常：%s，本次收盘流水线停止；不生成计划单，避免使用旧信号", desc, e)
                return False
            logger().error("%s 异常：%s，继续后续步骤", desc, e)

    # 关键检查：今日数据是否真的从 Tushare 入库了
    if not _date_in_scored(target_date):
        logger().warning(
            "⚠️ Tushare %s 数据尚未就绪（live_limit_up_fill_scored.csv 无该日记录），流水线步骤已完成但需等数据",
            target_str,
        )
        report_next_day_candidates()
        return False  # 通知调用方稍后重试

    if not _has_signal_for_date(target_date):
        logger().warning(
            "A/B/C 未生成 %s 计划单，自动使用当日涨停池生成安全模拟观察计划...",
            target_str,
        )
        def _run_limit_pool_fallback() -> bool:
            return run_script(
                "generate_live_limit_pool_daily_ops.py",
                "--signal-date",
                target_str,
                "--top-n",
                "10",
                timeout=TIMEOUT_SIGNAL_STEP,
            )

        fallback_ok = _run_limit_pool_fallback()
        if not (fallback_ok and _has_signal_for_date(target_date)):
            logger().warning("兜底涨停池观察计划第一次失败，等待10秒后就地重试一次：signal_date=%s", target_str)
            time.sleep(10)
            fallback_ok = _run_limit_pool_fallback()
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

    report_next_day_candidates()
    report_signal_readiness_summary(target_str)
    if not report_next_trade_factor_readiness(target_str):
        logger().warning("⚠️ 明日开盘因子尚未完全就绪，本次不标记收盘完成；5分钟后自动重试")
        return False
    logger().info("===== 收盘流水线完成 =====")
    mark_post_market_done(target_date)
    # 数据与因子全部就绪、流水线正式完成后，再讲决策逻辑和结果；
    # 之后由 _decision_chain_broadcast_loop 每30分钟重播一次。
    _log_decision_chain_summary(target_str)
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
    """运行收盘流水线，若收盘数据或明日开盘因子未就绪则每5分钟重试，直到23:00。"""
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
            "⚠️ %s 收盘数据/因子尚未就绪，%d分钟后（%s）自动重试",
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
    """从实盘涨停表读最新交易日的涨停状态和封单金额。
    返回 (最新交易日, {ts_code: {limit, open_times, fd_amount_wan, last_time}})。"""
    try:
        import pandas as pd
        path = _prefer_live_processed_path("live_limit_up_merged.csv", "limit_up_merged.csv")
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
    """从日线分片读最新交易日的 close/pct_chg/circ_mv。

    实盘播报只需要最新信号日当天候选股行情，不应读取 250 万行级别
    daily_merged.csv。优先使用 data/processed/daily_merged_by_date/YYYYMMDD.csv；
    旧 daily_merged.csv 仅作为离线研究遗留文件，不再进入实盘启动路径。
    返回 (最新交易日字符串, {ts_code: {...}})。"""
    try:
        import pandas as pd
        part_dir = PROJECT_ROOT / "data" / "processed" / "daily_merged_by_date"
        part_files = sorted(part_dir.glob("*.csv"))
        if not part_files:
            return "", {}
        path = part_files[-1]
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
            logger().warning("【%s】%s  ⚠️  未找到 A/B/C planned_orders 文件", header_label, action_date_str)
            if require_today:
                signal_date_str = today_str
                logger().info("  A/B/C：今日未生成计划单，继续检查 E2/L 是否已有 %s 信号", signal_date_str)
                _log_e2_signal_status(signal_date_str)
                _log_l_model3_signal_status(signal_date_str, action_date_str.replace("-", ""))
                _log_final_decision_summary(signal_date_str, action_date_str.replace("-", ""), pd.DataFrame())
            logger().info("=" * 60)
            return

        today_files = [Path(file) for file in files if today_str in Path(file).stem]
        latest_file = today_files[-1] if (require_today and today_files) else (Path(files[-1]) if not require_today else None)
        signal_date_str = today_str if require_today and latest_file is None else "未知"
        if latest_file is not None:
            _m = _re.search(r"\d{8}", latest_file.stem)
            signal_date_str = _m.group() if _m else "未知"
        # 收盘后必须是今天的信号；如果 A/B/C 今天无文件，也按今天检查 E2/L，不能回退到旧 planned_orders。
        data_fresh = (signal_date_str == today_str) or (not require_today)

        try:
            orders = pd.read_csv(latest_file) if latest_file is not None else pd.DataFrame()
        except pd.errors.EmptyDataError:
            logger().info("【%s】%s  信号日期：%s", header_label, action_date_str, signal_date_str)
            if data_fresh:
                logger().info("  A/B/C 均无符合条件标的，%s", no_candidate_msg)
            else:
                logger().warning("  ⚠️  数据未更新！信号来自 %s，今日（%s）收盘流水线未成功运行", signal_date_str, today_str)
            _log_e2_signal_status(signal_date_str)
            _log_l_model3_signal_status(signal_date_str, action_date_str.replace("-", ""))
            _log_final_decision_summary(signal_date_str, action_date_str.replace("-", ""), None)
            logger().info("=" * 60)
            return
        except Exception as e:
            file_name = latest_file.name if latest_file is not None else "无今日A/B/C planned_orders"
            logger().error("  读取 planned_orders 失败（%s）：%s", file_name, e)
            logger().info("=" * 60)
            return

        if not orders.empty and "signal_date" in orders.columns and not orders["signal_date"].dropna().empty:
            signal_date_str = str(orders["signal_date"].dropna().iloc[0])

        buy_orders = (
            orders[orders["side"].astype(str).str.upper() == "BUY"].copy()
            if not orders.empty and "side" in orders.columns else pd.DataFrame()
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
            _log_e2_signal_status(signal_date_str)
            _log_l_model3_signal_status(signal_date_str, action_date_str.replace("-", ""))
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
            _log_e2_signal_status(signal_date_str)
            _log_l_model3_signal_status(signal_date_str, action_date_str.replace("-", ""))

        _log_final_decision_summary(signal_date_str, action_date_str.replace("-", ""), buy_orders)
        logger().info("=" * 60)
    except Exception as e:
        logger().error("播报候选异常：%s", e)


def _load_e2_signal_for_signal_date(signal_date: str) -> dict[str, Any] | None:
    """读取 e2_signals_recent.json 中对应 signal_date 的入选信号。"""
    try:
        import json

        path = PROJECT_ROOT / "reports" / "strategy_e2" / "e2_signals_recent.json"
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
        logger().debug("读取E2信号失败：%s", exc)
    return None


def _load_e2_candidate_count(signal_date: str) -> int | None:
    """读取 E2 候选池规模（通过筛选的可买候选数，按 circ_mv 升序取首位为选中）。"""
    try:
        import pandas as pd

        path = PROJECT_ROOT / "reports" / "strategy_e2" / f"e2_signal_{signal_date}_candidates.csv"
        if not path.exists():
            return None
        return int(len(pd.read_csv(path, low_memory=False)))
    except Exception as exc:
        logger().debug("读取E2候选数失败：%s", exc)
        return None


def _log_e2_signal_status(signal_date: str) -> None:
    """播报 E2 候选/选中标的/allow_buy_reliable/计划买卖日。

    E2 信号存于 reports/strategy_e2/e2_signals_recent.json，与 A/B/C 的
    planned_orders.csv、L 的 l_signals_recent.json 都不同。启动播报若不单独
    读取，会出现"有 E2 信号却完全看不到"的盲区，故在此独立播报。
    """
    try:
        candidate_count = _load_e2_candidate_count(signal_date)
        count_text = "未知" if candidate_count is None else str(candidate_count)
        signal = _load_e2_signal_for_signal_date(signal_date)
        if signal is None:
            logger().info(
                "  E2策略：信号日期 %s 无E2入选信号，候选池=%s",
                signal_date,
                count_text,
            )
            return
        logger().info(
            "  E2策略：信号日期 %s 候选池=%s，选中 %s %s，板块=%s，"
            "allow_buy_reliable=%s，仓位=%s，计划买入=%s(%s)，计划卖出=%s(%s)，状态=%s",
            signal_date,
            count_text,
            signal.get("ts_code", ""),
            signal.get("name", ""),
            signal.get("market_segment", ""),
            bool(signal.get("allow_buy_reliable", False)),
            signal.get("position_pct", ""),
            signal.get("planned_buy_date", ""),
            signal.get("planned_buy_price", ""),
            signal.get("planned_exit_date", ""),
            signal.get("planned_exit_rule", ""),
            signal.get("status", ""),
        )
    except Exception as exc:
        logger().error("播报E2状态异常：%s", exc)


def _planned_shares_by_equity(position_pct: Any, price: float) -> int:
    """按初始资金模型估算计划买入股数（floor 到 100 股）。

    实盘 09:30 下单前会由 resize_buy_orders_for_live_account 按真实可用资金再缩放，
    这里只用于收盘/启动播报展示一个"计划数量"参考值。
    """
    try:
        cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
        initial_cash = float(cfg.get("position", {}).get("initial_cash", 500_000.0))
        amount = initial_cash * float(position_pct or 0.0)
        if str(cfg.get("trade_mode", "")).lower() == "live":
            cap = float(cfg.get("live_trade", {}).get("max_single_order_amount", amount) or amount)
            amount = min(amount, cap)
        if price and price > 0:
            shares = int(amount // price)
            shares -= shares % 100
            return max(shares, 0)
    except Exception as exc:
        logger().debug("估算计划股数失败：%s", exc)
    return 0


def _exit_method_desc(strategy: str, exit_rule: str) -> str:
    """按对应策略的平仓逻辑给出平仓时点/方式描述。

    - D：09:23 集合竞价挂跌停（成交≈开盘价）；或被A/B/C/E2接力时T+1开盘让路。
    - T+1开盘卖（含 *_open）：09:30 开盘平仓，买10/买5挂限价。
    - T+2收盘卖（默认 ABC/E2/L *_close）：14:55 收盘平仓，跌停价挂限价（市价效果）。
    口径与 check_and_close_positions / job_premarket_sell 一致。
    """
    s = str(strategy).upper()
    rule = str(exit_rule).lower()
    if s == "D":
        return "09:23集合竞价挂跌停平仓（成交≈开盘价）"
    if "open" in rule:
        return "09:30开盘平仓（买10/买5挂限价）"
    return "14:55收盘平仓（跌停价挂限价确保成交）"


def _log_final_decision_summary(signal_date: str, action_date_compact: str, buy_orders: Any) -> None:
    """打印【最终结果】：按当前总策略模式判定下一交易日实际开仓计划。

    - 模式1(ABCDE2)：A/B/C 计划单优先，无则 E2。
    - 模式2(L)：仅当 strategy_l.live_order_enabled 且 L 信号满足时开仓。
    - 模式3(MODEL3)：先取 mode1 计划；mode1 有买入则仅在 L 通过替换保护时由 L 替换，
      mode1 无买入则 L 通过基础条件时补位。
    与 combined_live_engine.build_model3_plan 的判定口径保持一致。
    """
    try:
        cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
        mode = int(cfg.get("active_strategy_profile", {}).get("mode", 1))
        mode_name = {1: "ABCDE2", 2: "L龙头", 3: "MODEL3"}.get(mode, str(mode))
        readable = f"{action_date_compact[:4]}-{action_date_compact[4:6]}-{action_date_compact[6:]}" \
            if len(action_date_compact) == 8 else action_date_compact

        # ── mode1 候选：A/B/C 计划单优先，否则 E2 ──
        abc_rows: list[dict[str, Any]] = []
        if buy_orders is not None and not buy_orders.empty:
            for _, r in buy_orders.iterrows():
                shares = int(r.get("round_lot_shares", r.get("estimated_shares", 0)) or 0)
                price = float(r.get("reference_price", 0.0) or 0.0)
                if shares <= 0 or price <= 0:
                    # PLAN_ONLY 占位计划（如C策略0股@0.00仅模拟观察）不算实盘买入。
                    # 必须与 combined_live_engine.build_abc_buy_decisions 的
                    # qty>0 & ref_price>0 过滤口径一致，否则【最终结果】会把
                    # 模拟观察计划当成mode1买入展示，与实际执行（E2顶上）不符。
                    continue
                exit_n = int(r.get("exit_n_days", 2) or 2)
                try:
                    bd = datetime.datetime.strptime(action_date_compact, "%Y%m%d").date()
                    exit_date = next_n_trade_days(bd, exit_n).strftime("%Y%m%d")
                except Exception:
                    exit_date = ""
                abc_rows.append({
                    "strategy": str(r.get("strategy_leg", "")) or "ABC",
                    "ts_code": str(r.get("ts_code", "")),
                    "name": str(r.get("name", "")),
                    "shares": shares,
                    "price": price,
                    "exit_date": exit_date,
                    "exit_rule": "T+2收盘",
                })
        e2_sig = _load_e2_signal_for_signal_date(signal_date)
        e2_buy: dict[str, Any] | None = None
        if (not abc_rows and e2_sig
                and str(e2_sig.get("planned_buy_date", "")) == action_date_compact
                and bool(e2_sig.get("allow_buy_reliable", False))):
            price = float(e2_sig.get("limit_close", 0.0) or 0.0)
            e2_buy = {
                "strategy": "E2",
                "ts_code": str(e2_sig.get("ts_code", "")),
                "name": str(e2_sig.get("name", "")),
                "shares": _planned_shares_by_equity(e2_sig.get("position_pct", 0.8), price),
                "price": price,
                "exit_date": str(e2_sig.get("planned_exit_date", "")),
                "exit_rule": str(e2_sig.get("planned_exit_rule", "T+2_close")),
            }
        mode1_buys = abc_rows if abc_rows else ([e2_buy] if e2_buy else [])

        # ── L 候选 ──
        l_sig = _load_l_signal_for_signal_date(signal_date)
        l_buy: dict[str, Any] | None = None
        l_base_ok = l_guard_ok = False
        if l_sig and str(l_sig.get("planned_buy_date", "")) == action_date_compact:
            l_base_ok, _ = _model3_l_base_rule_pass_for_log(l_sig)
            l_guard_ok, _ = _model3_l_replace_guard_pass_for_log(l_sig)
            l_price = float(l_sig.get("limit_close", 0.0) or 0.0)
            l_shares = _planned_shares_by_equity(l_sig.get("position_pct", 0.8), l_price)
            if l_shares > 0:
                l_buy = {
                    "strategy": "L龙头",
                    "ts_code": str(l_sig.get("ts_code", "")),
                    "name": str(l_sig.get("name", "")),
                    "shares": l_shares,
                    "price": l_price,
                    "exit_date": str(l_sig.get("planned_exit_date", "")),
                    "exit_rule": str(l_sig.get("planned_exit_rule", "T+2_close")),
                }

        # ── 按模式决策 ──
        final_buys: list[dict[str, Any]] = []
        note = ""
        if mode == 1:
            final_buys = mode1_buys
            note = "模式1：执行ABCDE2组合（A/B/C优先，无则E2）"
        elif mode == 2:
            l_live = bool(cfg.get("strategy_l", {}).get("live_order_enabled", False))
            if l_buy and l_live:
                final_buys = [l_buy]
                note = "模式2：独立L龙头策略开仓"
            else:
                note = "模式2：L龙头未满足实盘开仓（live_order_enabled=false 或信号不满足）"
        else:  # mode == 3
            m3 = cfg.get("strategy_model3", {})
            if not (bool(m3.get("enabled")) and bool(m3.get("live_order_enabled"))):
                final_buys = mode1_buys
                note = "模式3：model3未完全开启，沿用mode1计划"
            elif mode1_buys:
                if l_buy and l_guard_ok:
                    final_buys = [l_buy]
                    note = "模式3：mode1有买入，L通过替换保护 → 由L替换"
                else:
                    final_buys = mode1_buys
                    note = "模式3：mode1有买入，L替换保护不通过 → 保留mode1买入"
            else:
                if l_buy and l_base_ok:
                    final_buys = [l_buy]
                    note = "模式3：mode1无买入，L通过基础条件 → L补位"
                else:
                    note = "模式3：mode1无买入，L也不满足 → 不开仓"

        # ── 打印 ──
        logger().info("=" * 60)
        logger().info("======================== 最终结果 ========================")
        logger().info("明日(%s)  总策略模式：%s (%s)", readable, mode, mode_name)
        logger().info("判定：%s", note)
        if not final_buys:
            logger().info("开仓计划：❌ 无 —— ABCDE2 与龙头均无开仓计划")
        else:
            logger().info("开仓计划：✅ 共 %d 笔", len(final_buys))
            for b in final_buys:
                amount = b["shares"] * b["price"]
                logger().info(
                    "  • 策略 %s | %s %s | 计划买入 %d 股 @%.2f元（约%.0f元，实盘按可用资金缩放）",
                    b["strategy"], b["ts_code"], b["name"], b["shares"], b["price"], amount,
                )
            logger().info("准备下单时间：%s 09:00生成计划 → 09:15集合竞价预挂 → 09:30确认/补单", readable)
            seen_exits: set[tuple[str, str]] = set()
            for b in final_buys:
                ed = str(b.get("exit_date", ""))
                ed_readable = f"{ed[:4]}-{ed[4:6]}-{ed[6:]}" if len(ed) == 8 else (ed or "未知")
                method = _exit_method_desc(b.get("strategy", ""), b.get("exit_rule", ""))
                key = (ed_readable, method)
                if key in seen_exits:
                    continue
                seen_exits.add(key)
                logger().info("准备平仓时间：%s %s", ed_readable, method)
        logger().info("==========================================================")
    except Exception as exc:
        logger().error("最终结果汇总异常：%s", exc)


def _log_decision_chain_summary(signal_date: str) -> None:
    """收盘流水线完成后，用一段决策链日志讲清楚明日开仓逻辑。

    每行统一以 ┃ 开头：start_windows.py 终端着色按该前缀显示紫色，
    与其他日志区分；日志文件本身仍是纯文本，不含 ANSI 码。
    内容：策略优先级顺序 → 每个策略成立/不成立及原因 → 明日开仓计划
    （基于 signal_date 收盘数据统计的下一交易日计划），已持仓时标注。
    只做展示，任何异常不影响流水线。
    """
    try:
        import pandas as pd
        P = "┃"

        cfg = load_json_config(PROJECT_ROOT / "config" / "config.json")
        mode = int(cfg.get("active_strategy_profile", {}).get("mode", 1))
        try:
            sd = datetime.datetime.strptime(signal_date, "%Y%m%d").date()
            action_date = next_n_trade_days(sd, 1).strftime("%Y%m%d")
        except Exception:
            action_date = ""
        readable = f"{action_date[:4]}-{action_date[4:6]}-{action_date[6:]}" if len(action_date) == 8 else action_date
        day_label = "今日" if (action_date and action_date == today_beijing().strftime("%Y%m%d")) else "明日"

        # ── ABC：checklist + 计划单 ──
        checklist = _load_ab_checklist(signal_date)
        ss, top_code, top_name = "", "", ""
        a_cnt = b_cnt = c_cnt = 0
        if not checklist.empty:
            row = checklist.iloc[0]
            ss = str(row.get("selection_status", ""))
            top_code = str(row.get("top_ts_code", "") or "")
            top_name = str(row.get("top_name", "") or "")
            a_cnt = int(row.get("a_candidate_count", 0) or 0)
            b_cnt = int(row.get("b_candidate_count", 0) or 0)
            c_cnt = int(row.get("c_candidate_count", 0) or 0)
        hit_cond = ss.split(":", 1)[1] if ":" in ss else ""

        abc_buy: dict[str, Any] | None = None
        try:
            po_files = sorted(glob.glob(str(PROJECT_ROOT / f"reports/paper_trade/ab_filtered_daily_ops/*_{signal_date}_planned_orders.csv")))
            if po_files:
                po = pd.read_csv(po_files[-1], low_memory=False)
                buys = po[po.get("side", pd.Series(dtype=str)).astype(str).str.upper() == "BUY"] if not po.empty else po
                for _, r in (buys.iterrows() if not buys.empty else []):
                    qty = int(float(r.get("round_lot_shares", 0) or 0))
                    price = float(r.get("reference_price", 0.0) or 0.0)
                    if qty > 0 and price > 0:
                        abc_buy = {"strategy": str(r.get("strategy_leg", "") or "ABC"), "ts_code": str(r.get("ts_code", "")),
                                   "name": str(r.get("name", "")), "shares": qty, "price": price}
                        break
        except Exception:
            pass

        # A/B/C 三行文案：selection_status → 成立/不成立及原因
        if ss.startswith("A_SELECTED"):
            a_line = f"成立｜候选{a_cnt}，选中 {top_code} {top_name}"
            b_line = "未启用｜A已选中，B不再评估"
            c_line = "未启用｜A已选中，C不再评估"
        elif ss.startswith("A_NO_SELECTED_B_SELECTED"):
            a_line = f"不成立｜候选{a_cnt}，未形成可成交标的"
            b_line = f"成立｜候选{b_cnt}，选中 {top_code} {top_name}" + (f"（命中：{hit_cond}）" if hit_cond else "")
            c_line = "未启用｜B已选中，C不再评估"
        elif ss.startswith("A_NO_SELECTED_B_RISK_FILTERED"):
            a_line = f"不成立｜候选{a_cnt}，未形成可成交标的"
            b_line = f"不成立｜B首选命中风险过滤规则被剔除（候选{b_cnt}）"
            c_line = f"候选{c_cnt}" if c_cnt else "不成立｜无候选"
        elif ss.startswith("A_B_NO_FILLED_C_SELECTED"):
            a_line = f"不成立｜候选{a_cnt}，未形成可成交标的"
            b_line = f"不成立｜候选{b_cnt}，未形成可成交标的"
            c_line = f"成立｜候选{c_cnt}，补位选中 {top_code} {top_name}" + (f"（命中：{hit_cond}）" if hit_cond else "")
        elif ss.startswith("A_B_NO_FILLED_C_NO_SELECTED") or ss.startswith("A_NO_SELECTED_B_NO_SELECTED"):
            a_line = f"不成立｜候选{a_cnt}，未形成可成交标的"
            b_line = f"不成立｜候选{b_cnt}，未形成可成交标的"
            c_line = f"不成立｜候选{c_cnt}，无补位标的"
        else:
            a_line = b_line = c_line = f"状态={ss or '无checklist'}"

        # ABC名义上选中但计划单无有效股数/价格（如影子计划或参考价缺失）时，
        # 执行层按"无ABC计划"处理，文案必须与实际执行一致，避免自相矛盾。
        if abc_buy is None and ("SELECTED" in ss and not ss.endswith("NO_SELECTED")):
            note_shadow = "（但计划单无有效股数/价格，实盘按无ABC计划处理）"
            if ss.startswith("A_SELECTED"):
                a_line += note_shadow
            elif "B_SELECTED" in ss:
                b_line += note_shadow
            elif "C_SELECTED" in ss:
                c_line += note_shadow

        # ── E2 ──
        e2_sig = _load_e2_signal_for_signal_date(signal_date)
        e2_buy: dict[str, Any] | None = None
        if (e2_sig and str(e2_sig.get("planned_buy_date", "")) == action_date
                and bool(e2_sig.get("allow_buy_reliable", False))):
            price = float(e2_sig.get("limit_close", 0.0) or 0.0)
            e2_buy = {"strategy": "E2", "ts_code": str(e2_sig.get("ts_code", "")),
                      "name": str(e2_sig.get("name", "")),
                      "shares": _planned_shares_by_equity(e2_sig.get("position_pct", 0.8), price), "price": price}
        if abc_buy:
            e2_line = "让位｜ABC已有买入计划，同一资金不重复占用" + (
                f"（E2备选={e2_buy['ts_code']} {e2_buy['name']}）" if e2_buy else "")
        elif e2_buy:
            e2_line = f"成立｜兜底顶上，选中 {e2_buy['ts_code']} {e2_buy['name']}"
        elif e2_sig:
            e2_line = f"不成立｜信号存在但不满足执行条件（计划买入日={e2_sig.get('planned_buy_date','')}≠{action_date} 或不可靠）"
        else:
            e2_line = "不成立｜无E2信号"

        # ── D ──
        d_line = (f"阻断｜{day_label}已有A/B/C/E2开仓计划占用同一资金" if (abc_buy or e2_buy)
                  else "允许｜无开仓计划时启动盘中监控（仍需实时行情+风控校验）")

        # ── L / model3 ──
        mode1_buy = abc_buy or e2_buy
        l_sig = _load_l_signal_for_signal_date(signal_date)
        l_buy: dict[str, Any] | None = None
        l_base_ok = l_guard_ok = False
        base_reason = guard_reason = ""
        if l_sig:
            l_base_ok, base_reason = _model3_l_base_rule_pass_for_log(l_sig)
            l_guard_ok, guard_reason = _model3_l_replace_guard_pass_for_log(l_sig)
            if l_base_ok:
                l_price = float(l_sig.get("limit_close", 0.0) or 0.0)
                l_buy = {"strategy": "L", "ts_code": str(l_sig.get("ts_code", "")),
                         "name": str(l_sig.get("name", "")),
                         "shares": _planned_shares_by_equity(l_sig.get("position_pct", 0.8), l_price), "price": l_price}
        if not l_sig:
            l_line = "不参与｜无L信号"
        elif not l_base_ok:
            l_line = f"不参与｜基础规则不通过（{base_reason}）"
        elif mode1_buy and l_guard_ok:
            l_line = f"替换｜{l_sig.get('ts_code','')} {l_sig.get('name','')} 通过替换保护，顶掉mode1买入"
        elif mode1_buy:
            l_line = f"不替换｜选中{l_sig.get('ts_code','')} {l_sig.get('name','')}但替换保护不通过（{guard_reason}）"
        else:
            l_line = f"补位｜mode1无买入，L补位 {l_sig.get('ts_code','')} {l_sig.get('name','')}（补位不限板块）"

        # ── 最终计划（与【最终结果】/组合状态机同口径） ──
        final_buy: dict[str, Any] | None = None
        if mode == 2:
            final_buy = l_buy
        elif mode == 3 and mode1_buy and l_buy and l_guard_ok:
            final_buy = l_buy
        elif mode == 3 and not mode1_buy and l_buy and l_base_ok:
            final_buy = l_buy
        else:
            final_buy = mode1_buy

        # ── 持仓标注 ──
        # ★行只保留"（已持仓）"；非D持仓的占用/阻断说明单独成行，
        # 无论有无开仓候选都要让读者知道"目前谁在持仓、明日开不开仓"。
        open_pos = [p for p in load_positions() if str(p.get("status", "")).lower() in {"open", "sell_pending"}]
        non_d_pos = [p for p in open_pos if str(p.get("strategy_leg", "")).upper() != "D"]
        pos_note = ""
        if final_buy and any(str(p.get("ts_code", "")) == final_buy["ts_code"] for p in open_pos):
            pos_note = "（已持仓）"
        hold_line = ""
        if non_d_pos:
            desc = "、".join(
                f"{str(p.get('strategy_leg','') or '?').upper()}策略 {p.get('ts_code','')} {p.get('name','')}"
                f"（计划{p.get('planned_exit_date','')}平仓）"
                for p in non_d_pos
            )
            blocking = [p for p in non_d_pos if str(p.get("planned_exit_date", "99991231")) > action_date]
            overdue = [p for p in non_d_pos if str(p.get("planned_exit_date", "99991231")) < action_date]
            if blocking:
                hold_line = f"目前已持仓：{desc}；非D策略持仓未到期，{day_label}暂不开新仓"
            elif overdue:
                hold_line = (f"目前已持仓：{desc}；⚠该持仓已逾期（平仓失败残留），"
                             f"{day_label}09:20开盘窗口将第一时间挂跌停价清理；不阻断开仓计划")
            else:
                hold_line = (f"目前已持仓：{desc}；{day_label}到期将于14:55收盘平仓，"
                             "不阻断开仓计划（09:15照常下单，按可用资金校验/缩放）")

        # 整个决策链拼成一条多行日志、单次原子写入：daemon 是多线程
        # （账户心跳/候选播报/周期播报并发打日志），逐行输出必然被其他
        # 线程的日志穿插进框内。单条消息由 logging 内部锁保证原子性，
        # 两个框永远完整闭合，框内不会掺杂其他打印。
        date_banner = f"{P}━━━ {day_label}{readable} · 基于{signal_date}收盘数据 ━━━"
        bottom = f"{P}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        # 框0：决策优先级总图（静态规则+当日实际路径标记），与决策链同一条
        # 原子消息：决策链打印它就打印，不打印就都不打印。
        m1_has = mode1_buy is not None
        t2y = m1_has and (l_buy is not None) and l_guard_ok
        t3y = (not m1_has) and (l_buy is not None) and l_base_ok
        TAG = " ◄―今日路径"
        lines = [
            f"{P}━━━━━━━━━━━━ 决策优先级树状图（mode=3） ━━━━━━━━━━━━",
            f"{P} │ 有未到期持仓？",
            f"{P} │",
            f"{P} ├─ 是 ──▶ 等到期平仓（14:55收盘）──▶ 结束",
            f"{P} │",
            f"{P} └─ 否 ──▶ mode1 选票",
            f"{P}           │ A > B > C > E2，先中先得",
            f"{P}           │",
            f"{P}           ├─ mode1 有选票 ──▶ L 替换窄门{TAG if m1_has else ''}",
            f"{P}           │           │ 创业板 + 题材涨停≥2 + 非尾盘首板",
            f"{P}           │           │",
            f"{P}           │           ├─ 是 ──▶ 买 L 的票（顶掉mode1）──▶ 结束{TAG if t2y else ''}",
            f"{P}           │           │",
            f"{P}           │           └─ 否 ──▶ 买 mode1 的票（①~④谁中买谁）──▶ 结束{TAG if (m1_has and not t2y) else ''}",
            f"{P}           │",
            f"{P}           └─ mode1 无选票 ──▶ L 补位判断{TAG if not m1_has else ''}",
            f"{P}                       │ 非科创板 + 板块情绪OK + 全市场连板家数≥8（非个股8连板）",
            f"{P}                       │",
            f"{P}                       ├─ 是 ──▶ 买 L 的票（补位）──▶ 结束{TAG if t3y else ''}",
            f"{P}                       │",
            f"{P}                       └─ 否 ──▶ D 盘中扫描（开盘后实时找机会，需过行情/成交概率/风控，不保证开仓）──▶ 结束{TAG if (not m1_has and not t3y) else ''}",
            bottom,
            P,
            f"{P}━━━━━━━━━━━━ 决策优先级总图（mode=3） ━━━━━━━━━━━━",
            f"{P} 【0】有未到期持仓? ─是→ 不开新仓，等到期日14:55收盘平仓",
            f"{P}   ↓否",
            f"{P} 【1】mode1选票（串行先中先得）: ①A主 → ②B备 → ③C补位 → ④E2兜底",
            f"{P}   ├─有票 → 进【2】L替换审查{TAG if m1_has else ''}",
            f"{P}   └─无票 → 跳【3】L补位审查{TAG if not m1_has else ''}",
            f"{P} 【2】L替换窄门: 基础规则 ∧ 创业板 ∧ 题材涨停≥2 ∧ 非尾盘首板",
            f"{P}   ├─过　 → ★改买L的票（顶掉mode1）■{TAG if t2y else ''}",
            f"{P}   └─不过 → ★买mode1的票（①~④命中者）■{TAG if (m1_has and not t2y) else ''}",
            f"{P} 【3】L补位: 只需基础规则（非科创板∧情绪OK∧全市场连板家数≥8，不限板块）",
            f"{P}   ├─过　 → ★买L的票（补位）■{TAG if t3y else ''}",
            f"{P}   └─不过 → 进【4】{TAG if (not m1_has and not t3y) else ''}",
            f"{P} 【4】D盘中扫描（兜底）: 开盘后实时监控，仍需成交概率+风控校验",
            bottom,
            P,
            # 框1：开仓决策链（策略顺序 + 各策略成立/不成立及原因）
            f"{P}━━━━━━━━━━━━━━ 开仓决策链 ━━━━━━━━━━━━━━",
            date_banner,
            f"{P} 策略顺序：mode1内 A主 > B备 > C补位 > E2兜底 > D盘中；L按model3规则补位/替换（当前mode={mode}）",
            f"{P} ① A主策略：{a_line}",
            f"{P} ② B备用策略：{b_line}",
            f"{P} ③ C补位策略：{c_line}",
            f"{P} ④ E2兜底：{e2_line}",
            f"{P} ⑤ D盘中：{d_line}",
            f"{P} ⑥ L/model3：{l_line}",
            bottom,
            P,
            # 框2：最终开仓计划（开仓计划/无计划 + 持仓状态）
            f"{P}━━━━━━━━━━━━━━ 最终开仓计划 ━━━━━━━━━━━━━━",
            date_banner,
        ]
        if final_buy:
            amount = final_buy["shares"] * final_buy["price"]
            lines.append(
                f"{P} ★ 开仓计划：策略{final_buy['strategy']} {final_buy['ts_code']} {final_buy['name']} "
                f"{final_buy['shares']}股@参考{final_buy['price']:.2f} ≈{amount / 10000:.2f}万"
                f"（09:15集合竞价预挂→09:30确认，实际按账户资金/单笔限额缩放）{pos_note}"
            )
        else:
            lines.append(f"{P} ★ {day_label}所有策略均无开仓计划")
        if hold_line:
            lines.append(f"{P} ⚠ {hold_line}")
        lines.append(bottom)
        logger().info("\n".join(lines))
    except Exception as exc:
        logger().warning("开仓决策链播报失败（不影响流水线）：%s", exc)


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


def _decision_chain_broadcast_loop() -> None:
    """每30分钟重播一次开仓决策链（后台线程），方便随时查看明日计划。

    只在决策结果已产出（存在 planned_orders 信号文件）且执行日未过期时重播：
    执行日==今天盘中也播（标签自动切换为"今日"），执行日已过则静默等新信号。
    启动后立即播第一次（覆盖"缓存命中不跑流水线"的启动路径），再进入30分钟循环。
    纯展示线程，任何异常不影响交易主流程。
    """
    while True:
        try:
            sd = _latest_planned_orders_signal_date()
            if sd:
                sd_date = datetime.datetime.strptime(sd, "%Y%m%d").date()
                action_date = next_n_trade_days(sd_date, 1).strftime("%Y%m%d")
                if action_date >= today_beijing().strftime("%Y%m%d"):
                    _log_decision_chain_summary(sd)
        except Exception as exc:
            logger().debug("决策链周期播报异常：%s", exc)
        time.sleep(1800)


def _load_l_signal_for_signal_date(signal_date: str) -> dict[str, Any] | None:
    try:
        import json

        path = PROJECT_ROOT / "reports" / "strategy_l" / "l_signals_recent.json"
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
        logger().debug("读取L信号失败：%s", exc)
    return None


def _load_l_candidate_count(signal_date: str) -> int | None:
    try:
        import pandas as pd

        path = PROJECT_ROOT / "reports" / "strategy_l" / f"l_signal_{signal_date}_candidates.csv"
        if not path.exists():
            return None
        return int(len(pd.read_csv(path, low_memory=False)))
    except Exception as exc:
        logger().debug("读取L候选数失败：%s", exc)
        return None


def _model3_l_base_rule_pass_for_log(signal: dict[str, Any]) -> tuple[bool, str]:
    segment = str(signal.get("market_segment", ""))
    retreat = str(signal.get("segment_retreat_state_bucket", ""))
    chain = str(signal.get("market_chain_count_bucket", ""))
    reasons = []
    if segment == "star":
        reasons.append("market_segment=star被排除")
    if retreat not in {"neutral", "warming_2day"}:
        reasons.append(f"segment_retreat_state_bucket={retreat}不在neutral/warming_2day")
    if chain not in {"8_15", "15_30", "gte_30"}:
        reasons.append(f"market_chain_count_bucket={chain}不在8_15/15_30/gte_30")
    return not reasons, "；".join(reasons) if reasons else "L通过model=3基础稳健条件"


def _model3_l_replace_guard_pass_for_log(signal: dict[str, Any]) -> tuple[bool, str]:
    """model=3 替换保护：判定 L 是否有资格顶掉 mode=1（ABC/E2）已有的买入计划。

    为什么替换要设更高门槛——机会成本逻辑：
    mode=1 本身是认证过的正期望策略，L 替换它等于放弃一笔大概率赚钱的交易，
    所以 L 的期望收益必须"显著更高"才划算，而不是"还行"就行。
    注意：本保护只管"抢同一笔资金"的场景；mode=1 空闲日 L 补位只需基础规则，
    不受这里的板块限制（主板/北交所补位单都允许）。

    三个条件均来自 9 种候选规则的穷举赛马
    （reports/strategy_model3/occupancy_guards/model3_occupancy_guard_summary.csv）：
    选中组合 8302x/胜率69.9%/回撤-18.3% 为最优；只要创业板不加条件 6861x；
    不限板块的宽松规则掉到 4778~6172x，连用未来信息的理论上限(6548x)都不如选中组合。
    - 创业板(chi_next)：L 赚的是龙头次日溢价，20cm 弹性是主板(10cm)两倍；
      主板龙头期望溢价平均不够补偿放弃 mode=1 的机会成本（回测中此类替换为负贡献）。
    - theme_limit_count>=2：题材有梯队（非孤板），接力胜率更高。
    - 排除 after_1430：尾盘偷袭板封板质量弱，次日溢价差。
    配置出处：config.json strategy_model3.replace_guard（规则名 replace_theme_ge_2_not_after_1430）。
    """
    segment = str(signal.get("market_segment", ""))
    first_time_bucket = str(signal.get("first_time_detail_bucket", ""))
    try:
        theme_limit_count = float(signal.get("theme_limit_count", 0) or 0)
    except (TypeError, ValueError):
        theme_limit_count = 0.0
    reasons = []
    if segment != "chi_next":
        reasons.append(
            f"替换要求创业板（20cm龙头溢价弹性是主板2倍，主板L替换在回测中为负贡献），当前market_segment={segment}"
        )
    if theme_limit_count < 2:
        reasons.append(f"替换要求theme_limit_count>=2（题材有梯队接力胜率更高），当前={theme_limit_count:g}")
    if first_time_bucket == "after_1430":
        reasons.append("替换排除first_time_detail_bucket=after_1430（尾盘偷袭板封板质量弱、次日溢价差）")
    return not reasons, "；".join(reasons) if reasons else "L通过model=3替换保护条件"


def _log_l_model3_signal_status(signal_date: str, action_date: str | None = None) -> None:
    """播报 L 龙头信号和 mode=3 切换状态。

    周末/非交易日启动时，组合状态机不会用“今天”直接生成周一买单，
    但这里仍应把最新收盘信号对应的 L 候选、计划买入日和 model=3
    基础规则打印出来，避免只看到 ABC/E2 而看不到 L。
    """
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        active_mode = int(config.get("active_strategy_profile", {}).get("mode", 1))
        model3_config = config.get("strategy_model3", {})
        candidate_count = _load_l_candidate_count(signal_date)
        signal = _load_l_signal_for_signal_date(signal_date)
        count_text = "未知" if candidate_count is None else str(candidate_count)

        logger().info(
            "  L/model3状态：active_mode=%s strategy_model3.enabled=%s live_order_enabled=%s",
            active_mode,
            bool(model3_config.get("enabled", False)),
            bool(model3_config.get("live_order_enabled", False)),
        )
        if signal is None:
            logger().info(
                "  L龙头策略：信号日期 %s 无L入选信号，候选数=%s；model=3本轮不会使用L。",
                signal_date,
                count_text,
            )
            return

        base_ok, base_reason = _model3_l_base_rule_pass_for_log(signal)
        guard_ok, guard_reason = _model3_l_replace_guard_pass_for_log(signal)
        planned_buy_date = str(signal.get("planned_buy_date", ""))
        action_note = ""
        if action_date:
            if planned_buy_date == str(action_date):
                action_note = "；计划买入日与下个交易日一致"
            else:
                action_note = f"；计划买入日与当前播报操作日不一致（操作日={action_date}）"

        logger().info(
            "  L龙头策略：信号日期 %s 候选数=%s，选中 %s %s，题材=%s，计划买入=%s，计划卖出=%s%s",
            signal_date,
            count_text,
            signal.get("ts_code", ""),
            signal.get("name", ""),
            signal.get("theme_name", ""),
            planned_buy_date,
            signal.get("planned_exit_date", ""),
            action_note,
        )
        logger().info(
            "  L龙头条件：基础规则=%s（%s）；替换保护=%s（%s）",
            "通过" if base_ok else "不通过",
            base_reason,
            "通过" if guard_ok else "不通过",
            guard_reason,
        )
        if active_mode != 3:
            logger().info("  L/model3结论：当前不是mode=3，L只展示不参与当前组合切换。")
        elif not bool(model3_config.get("enabled", False)) or not bool(model3_config.get("live_order_enabled", False)):
            logger().info("  L/model3结论：model3开关未同时开启，沿用mode=1。")
        elif not base_ok:
            logger().info("  L/model3结论：L未通过基础规则，沿用mode=1。")
        elif guard_ok:
            logger().info(
                "  L/model3结论：若%s无mode=1买入则L可补位；若有mode=1买入，L也具备替换资格"
                "（创业板+题材梯队+非尾盘板三条件全部满足）。",
                planned_buy_date,
            )
        else:
            logger().info(
                "  L/model3结论：若%s无mode=1买入则L可补位（补位只需基础规则，不限板块，主板也能买）；"
                "若已有mode=1买入则不替换，原因=%s。",
                planned_buy_date,
                guard_reason,
            )
            logger().info(
                "  L替换保护依据：mode1是认证正期望策略，L顶掉它有机会成本，期望收益须显著更高才划算；"
                "穷举9种替换规则中「创业板+题材涨停≥2+非尾盘首板」组合认证最优（8302x/胜率69.9%），"
                "放宽板块限制复利降至4778~6172x。该限制只作用于替换场景，与账户交易权限无关。"
            )
    except Exception as exc:
        logger().warning("  L/model3状态播报失败：%s", exc)


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
        fill_path = _prefer_live_processed_path(
            "live_limit_up_fill_scored.csv",
            cfg.get("fill_model", {}).get("output_limit_up_fill_scored_path", "data/processed/limit_up_fill_scored.csv"),
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
            logger().warning("  数据口径：❌ %s 没有 %s 记录", fill_path.name, signal_date)
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
        _log_l_model3_signal_status(signal_date)

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

        sentiment_path = _prefer_live_processed_path("live_market_sentiment.csv", "market_sentiment.csv")
        emotion_path = _prefer_live_processed_path("live_market_emotion_features.csv", "market_emotion_features.csv")
        if not sentiment_path.exists():
            logger().info("  市场环境：未找到 %s", sentiment_path.name)
            return
        sentiment = pd.read_csv(sentiment_path, dtype={"trade_date": str}, low_memory=False)
        row_df = sentiment[sentiment["trade_date"].astype(str) == str(signal_date)]
        if row_df.empty:
            logger().info("  市场环境：%s 无 %s 记录", sentiment_path.name, signal_date)
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
        live_fill_scored_path = _prefer_live_processed_path("live_limit_up_fill_scored.csv", "limit_up_fill_scored.csv")
        live_market_emotion_path = _prefer_live_processed_path("live_market_emotion_features.csv", "market_emotion_features.csv")
        live_theme_heat_path = _prefer_live_processed_path("live_theme_heat_features.csv", "theme_heat_features.csv")
        generator_kwargs = {
            "input_trades_path": live_fill_scored_path,
            "market_emotion_features_path": live_market_emotion_path,
            "theme_heat_features_path": live_theme_heat_path,
        }
        base_generator = PaperCandidateGenerator(strategy_path, **generator_kwargs)
        all_candidates = base_generator.load_all_candidates()

        traces = [filter_trace("A主策略", base_generator, all_candidates, signal_date)]

        b_conditions = configured_b_conditions(strategy_cfg)
        b_config = backup_config(strategy_cfg, b_conditions)
        b_generator = PaperCandidateGenerator(strategy_path, **generator_kwargs)
        b_generator.config = b_config
        b_generator.paper_config = b_config.get("paper_candidate", {})
        b_generator.risk_thresholds = b_generator.paper_config.get("risk_thresholds", {})
        traces.append(filter_trace(f"B备用策略（{condition_text(b_conditions)}）", b_generator, all_candidates, signal_date))

        c_conditions = configured_c_conditions(strategy_cfg)
        if c_conditions:
            c_config = backup_config(strategy_cfg, c_conditions)
            c_generator = PaperCandidateGenerator(strategy_path, **generator_kwargs)
            c_generator.config = c_config
            c_generator.paper_config = c_config.get("paper_candidate", {})
            c_generator.risk_thresholds = c_generator.paper_config.get("risk_thresholds", {})
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
        d_running = _strategy_d_monitor_running()
        if d_running:
            config = load_json_config(PROJECT_ROOT / "config" / "config.json")
            allowed_segments = config.get("strategy_d", {}).get("allowed_market_segments", [])
            allowed_text = ",".join(str(x) for x in allowed_segments) if isinstance(allowed_segments, list) else "未配置"
            reason = (
                "D盘中监控进程正在运行；若同时存在A/B/C计划，通常表示开仓窗口已过、"
                "A/B/C/E2未实际成交且账户空仓，系统释放资金占用后补启动D。"
            )
            logger().info("  D策略状态：RUNNING（%s）", reason)
            logger().info("  D实盘扫描范围：%s；日志中的扫描数量为D当前配置股票池数量。", allowed_text)
        elif planned_count > 0:
            logger().info("  D策略停止点：组合状态机。原因：今日已有 A/B/C 买入计划，阻断 D 盘中监控，避免同一资金重复占用。")
        elif not in_d_start_window:
            logger().info(
                "  D策略停止点：交易时段。原因：当前不是 D 盘中监控时段。D 只在交易日 09:20 组合状态机允许后启动，09:30后扫描，10:00起WATCH，14:00起BUY，14:56停止/撤单。"
            )
            logger().info(
                "  D策略后续过滤链：组合状态机允许 -> 实时行情扫描 -> 首板且昨日未涨停 -> 当前封涨停 -> 曾炸板至少1次 -> 今日曾涨停数量达到强情绪阈值 -> 14:00后按实时封单金额/流通市值(fd_amount_to_circ_mv)排序 -> LiveOrderGateway二次风控。"
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
    if scheduled_time == SCHED_PREOPEN_PLAN:   # 09:00
        job_preopen_plan() if trade_day else logger().info("非交易日，跳过盘前计划生成")
    elif scheduled_time == SCHED_PREMARKET_BUY:     # 09:15
        job_premarket_buy() if trade_day else logger().info("非交易日，跳过盘前买入")
    elif scheduled_time == SCHED_MORNING_REVIEW:   # 09:20
        job_morning() if trade_day else logger().info("非交易日，跳过盘前任务")
    elif scheduled_time == SCHED_PREMARKET_SELL:   # 09:23
        if trade_day:
            if _has_premarket_close_plan():
                _pause_pipeline_for_trade("09:23集合竞价平仓计划")
                try:
                    job_premarket_sell()
                finally:
                    if _has_due_close_plan_now() or _has_premarket_close_plan():
                        logger().warning("09:23平仓后仍检测到待平仓计划，流水线保持暂停；等待后续平仓确认或持仓清理。")
                    else:
                        _resume_pipeline_after_trade("09:23集合竞价平仓处理完成")
            else:
                logger().info("09:23 未检测到集合竞价平仓计划，流水线无需暂停。")
                job_premarket_sell()
        else:
            logger().info("非交易日，跳过集合竞价平仓")
    elif scheduled_time == SCHED_PREMARKET_SYNC:   # 09:26
        job_premarket_position_sync() if trade_day else logger().info("非交易日，跳过盘前持仓同步")
    elif scheduled_time == SCHED_OPENING_BUY:   # 09:30
        job_opening_buy() if trade_day else logger().info("非交易日，跳过开盘买入任务")
    elif scheduled_time == SCHED_CANCEL_BUY_ORDERS:   # 14:40 独立撤未成交买单
        job_cancel_unfilled_buy_orders() if trade_day else logger().info("非交易日，跳过撤买单")
    elif scheduled_time == SCHED_AFTERNOON_CLOSE:   # 14:55 收盘平仓
        job_afternoon() if trade_day else logger().info("非交易日，跳过盘中任务")
    elif scheduled_time == SCHED_POST_MARKET:   # 15:10
        if trade_day:
            # 收盘全量对账（只读+告警，独立于数据流水线，先跑）
            try:
                reconcile_positions_with_broker()
            except Exception as e:
                logger().error("收盘对账异常：%s", e)
            _start_post_market_pipeline(reason="15:10收盘流水线")
        else:
            logger().info("非交易日，跳过收盘流水线")


_qmt_reconnect_count: int = 0       # 累计重连次数，成功后归零
_qmt_adapter: Any = None             # 持久连接，程序生命周期内保持
_qmt_last_verified_at: str = ""      # 最近一次 query_account/query_positions 成功时间
_last_account_has_position: bool = False  # 最近一次券商账户心跳是否确认有持仓
_qmt_lock = threading.RLock()        # 保护 _qmt_adapter 并发访问（可重入：平仓路径内嵌撤止盈单）
_d_monitor_thread: threading.Thread | None = None  # D监控线程；D不再独立连接QMT


def _broker_config_with_qmt_override(
    broker_config: dict,
    *,
    qmt_path: str = "",
    session_id: str = "",
) -> dict:
    """按上次成功会话覆盖 QMT path/session，避免重复连接已知失败的默认 session。"""
    if not qmt_path and not session_id:
        return broker_config
    cfg = dict(broker_config)
    if qmt_path:
        os.environ[str(cfg.get("qmt_path_env", "QMT_PATH"))] = qmt_path
    if session_id:
        os.environ[str(cfg.get("session_id_env", "QMT_SESSION_ID"))] = str(session_id)
    return cfg


def _load_qmt_last_success() -> dict[str, Any]:
    try:
        if QMT_LAST_SUCCESS_FILE.exists():
            data = json.loads(QMT_LAST_SUCCESS_FILE.read_text(encoding="utf-8"))
            if data.get("qmt_path") and data.get("session_id"):
                return data
    except Exception:
        return {}
    return {}


def _save_qmt_last_success(*, qmt_path: str, session_id: str, account_id: str = "") -> None:
    try:
        if not qmt_path or not session_id:
            return
        mkdir_p(QMT_LAST_SUCCESS_FILE.parent)
        QMT_LAST_SUCCESS_FILE.write_text(
            json.dumps(
                {
                    "qmt_path": str(qmt_path),
                    "session_id": str(session_id),
                    "account_id": str(account_id),
                    "verified_at": now_beijing().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        logger().warning("QMT成功会话缓存写入失败：%s", exc)


def _clear_qmt_last_success(reason: str) -> None:
    """清除失效的 QMT 成功会话缓存，避免下次启动继续先试坏 session。"""
    try:
        if QMT_LAST_SUCCESS_FILE.exists():
            QMT_LAST_SUCCESS_FILE.unlink()
            logger().warning("QMT成功会话缓存已清除：%s", reason)
    except Exception as exc:
        logger().warning("QMT成功会话缓存清除失败：%s", exc)


def _should_clear_qmt_cache_for_error(error_text: str) -> bool:
    """判断是否真的要清除 QMT 成功会话缓存。

    QMT 的 connect=-1 在实盘环境里可能是短暂忙，不一定代表 session 永久失效；
    日志里已经出现过同一 session 先 connect=-1、随后完整扫描又成功的情况。
    因此首选 session 单次失败不清缓存，完整扫描成功后会自然刷新缓存。
    只有缓存路径本身明显不可用时才清除。
    """
    text = str(error_text)
    hard_fail_markers = ["No such file", "不存在", "路径不存在", "not found"]
    return any(marker in text for marker in hard_fail_markers)


def _qmt_connect_once(
    broker_config: dict,
    *,
    preferred_only: bool,
    timeout_sec: float,
    qmt_path: str = "",
    session_id: str = "",
) -> Any:
    """单次建立 QMT 连接，给创建适配器、加载 xtquant 和 connect 全流程独立超时。"""
    done = threading.Event()
    timed_out = threading.Event()
    result: list[Any] = []
    err: list = []

    def _do() -> None:
        try:
            from src.qmt_adapter import QMTBrokerAdapter

            adapter = QMTBrokerAdapter.from_config(
                _broker_config_with_qmt_override(
                    broker_config,
                    qmt_path=qmt_path,
                    session_id=session_id,
                )
            )
            adapter.connect(preferred_only=preferred_only)
            if timed_out.is_set():
                try:
                    adapter.disconnect()
                except Exception:
                    pass
                return
            result.append(adapter)
        except Exception as e:
            err.append(e)
        finally:
            done.set()

    threading.Thread(target=_do, daemon=True).start()
    if not done.wait(timeout_sec):
        timed_out.set()
        mode = "首选path/session" if preferred_only else "完整备用path/session"
        raise TimeoutError(f"QMT {mode}连接超时（{int(timeout_sec)}秒无响应）")
    if err:
        raise err[0]
    if not result:
        raise RuntimeError("QMT连接未返回适配器")
    return result[0]


def _qmt_connect(broker_config: dict, *, allow_full_scan: bool | None = None) -> Any:
    """建立新 QMT 连接。不持有 _qmt_lock，调用方按需加锁。

    连接原则：
    1. 启动门禁/关键重连使用 allow_full_scan=True，只做一次完整扫描。
       不先试缓存再叠加完整扫描，避免首选 session 超时线程未退出时又抢 QMT。
    2. 轻量心跳使用缓存优先，失败后交给下一轮关键重连处理。
    """
    errors: list[str] = []
    cached = _load_qmt_last_success()
    attempts: list[dict[str, Any]] = []

    if allow_full_scan is True:
        if cached:
            attempts.append({
                "label": "上次成功path/session",
                "preferred_only": True,
                "timeout_sec": 18.0,
                "qmt_path": str(cached.get("qmt_path", "")),
                "session_id": str(cached.get("session_id", "")),
            })
        attempts.append({
            "label": "完整备用path/session",
            "preferred_only": False,
            "timeout_sec": 55.0,
            "qmt_path": "",
            "session_id": "",
        })
    elif cached:
        attempts.append({
            "label": "上次成功path/session",
            "preferred_only": True,
            "timeout_sec": 12.0,
            "qmt_path": str(cached.get("qmt_path", "")),
            "session_id": str(cached.get("session_id", "")),
        })
    else:
        attempts.append({
            "label": "完整备用path/session",
            "preferred_only": False,
            "timeout_sec": 25.0,
            "qmt_path": "",
            "session_id": "",
        })

    for attempt in attempts:
        try:
            adapter = _qmt_connect_once(
                broker_config,
                preferred_only=bool(attempt["preferred_only"]),
                timeout_sec=float(attempt["timeout_sec"]),
                qmt_path=str(attempt.get("qmt_path", "")),
                session_id=str(attempt.get("session_id", "")),
            )
            _save_qmt_last_success(
                qmt_path=str(getattr(adapter, "_active_qmt_path", "")),
                session_id=str(getattr(adapter, "_active_session_id", "")),
                account_id=str(getattr(getattr(adapter, "config", None), "account_id", "")),
            )
            return adapter
        except Exception as exc:
            errors.append(f"{attempt['label']}: {exc}")
            if attempt["label"] == "上次成功path/session" and _should_clear_qmt_cache_for_error(str(exc)):
                _clear_qmt_last_success(str(exc))
    raise RuntimeError("QMT连接失败: " + " | ".join(errors))


def _qmt_get(broker_config: dict, *, allow_full_scan: bool | None = None) -> Any:
    """返回持久连接，未连接时建立。调用方须持有 _qmt_lock。"""
    global _qmt_adapter
    if _qmt_adapter is None:
        _qmt_adapter = _qmt_connect(broker_config, allow_full_scan=allow_full_scan)
    return _qmt_adapter


class SharedQMTBrokerProxy:
    """D监控使用的共享QMT代理。

    架构约束：一个进程生命周期内只允许主守护进程持有 QMT 连接。
    D监控不能再作为独立子进程连接 QMT，否则会和账户心跳/下单互抢 session。
    该代理把 D 监控需要的 broker 方法统一转发到 `_qmt_get` 的唯一连接上。
    """

    def __init__(self, broker_config: dict[str, Any]) -> None:
        self.broker_config = broker_config

    def query_account(self) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).query_account()

    def query_positions(self) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).query_positions()

    def query_orders(self) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).query_orders()

    def query_trades(self) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).query_trades()

    def get_full_tick(self, ts_codes: list[str]) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).get_full_tick(ts_codes)

    def place_order(self, request: Any) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).place_order(request)

    def cancel_order(self, order_id: str) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).cancel_order(order_id)

    def get_order_fill(self, order_id: str) -> Any:
        with _qmt_lock:
            return _qmt_get(self.broker_config).get_order_fill(order_id)

    def disconnect(self) -> None:
        """共享连接由主守护进程管理，D监控退出时不能断开。"""
        return None


def _qmt_query_account_positions(adapter: Any, *, timeout_sec: float = 25.0) -> tuple[Any, Any]:
    """账户连接验证：资产和持仓都能成功返回，才算 QMT 对程序可用。"""
    done = threading.Event()
    result: list[tuple[Any, Any]] = []
    err: list[BaseException] = []

    def _do() -> None:
        try:
            account = adapter.query_account()
            positions = adapter.query_positions()
            result.append((account, positions))
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)
        finally:
            done.set()

    threading.Thread(target=_do, daemon=True).start()
    if not done.wait(timeout_sec):
        raise TimeoutError(f"QMT账户查询超时（{int(timeout_sec)}秒无响应）")
    if err:
        raise err[0]
    if not result:
        raise RuntimeError("QMT账户查询未返回结果")
    return result[0]


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
    只有 query_account/query_positions 成功返回，才算账户连接已验证可用。
    关键交易窗口账户不可用时立刻重连和告警；非交易时段只做低频恢复尝试。"""
    global _qmt_reconnect_count, _qmt_last_verified_at, _last_account_has_position
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

    # 平仓窗口(14:54:30~14:58)心跳静默：不与14:55平仓抢一秒钟的锁
    _t = now_beijing().time()
    if datetime.time(14, 54, 30) <= _t < datetime.time(14, 58):
        return
    account = positions = None
    quote_map: dict = {}
    with _qmt_lock:
        try:
            adapter = _qmt_get(broker_cfg)
            account, positions = _qmt_query_account_positions(adapter)
            _qmt_last_verified_at = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
            live_positions = [p for p in (positions or []) if int(getattr(p, "volume", 0) or 0) > 0]
            if live_positions:
                codes = [p.ts_code for p in live_positions]
                if codes:
                    quote_map = adapter.get_full_tick(codes)
            if _qmt_reconnect_count > 0:
                log.info("✅ QMT连接已恢复（第%d次心跳失败后恢复）", _qmt_reconnect_count)
                if qmt_is_critical_window():
                    _notify("connection", "✅ 账户重连成功", "QMT连接已恢复正常。")
                _qmt_reconnect_count = 0
        except Exception as first_err:
            _qmt_reset()
            _qmt_reconnect_count += 1
            critical_window = qmt_is_critical_window()
            last_ok = _qmt_last_verified_at or "本轮启动后尚未验证成功"
            if not critical_window and _qmt_reconnect_count < 3:
                log.info(
                    "QMT账户连接状态=未验证/不可用（非交易时段第%d/3次，最后验证成功=%s；暂不推送断连，稍后低频重试）：%s",
                    _qmt_reconnect_count,
                    last_ok,
                    first_err,
                )
                return

            if critical_window:
                log.warning(
                    "⚠️ QMT账户连接状态=不可用（第%d次，最后验证成功=%s），立刻重连：%s",
                    _qmt_reconnect_count,
                    last_ok,
                    first_err,
                )
            else:
                log.info(
                    "QMT账户连接状态=未验证/不可用（非交易时段连续%d次，最后验证成功=%s），开始后台静默重连：%s",
                    _qmt_reconnect_count,
                    last_ok,
                    first_err,
                )

            # 关键窗口刚断连时告警；非关键时段只写日志，避免周末/夜间刷通知。
            if critical_window and _qmt_reconnect_count == 1:
                _notify("connection", "🔌 账户断连", "QMT连接断开，正在自动重连，请关注。",
                        level="critical", call=False)
            try:
                if critical_window:
                    log.info("QMT自动重连开始（第%d次）", _qmt_reconnect_count)
                    allow_full_scan = True
                else:
                    allow_full_scan = _qmt_reconnect_count >= 3
                    scan_desc = "完整扫描备用session/path" if allow_full_scan else "首选session"
                    log.info(
                        "QMT非交易时段后台静默重连开始（第%d次，%s）",
                        _qmt_reconnect_count,
                        scan_desc,
                    )
                adapter = _qmt_get(broker_cfg, allow_full_scan=allow_full_scan)
                account, positions = _qmt_query_account_positions(adapter)
                _qmt_last_verified_at = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
                live_positions = [p for p in (positions or []) if int(getattr(p, "volume", 0) or 0) > 0]
                if live_positions:
                    codes = [p.ts_code for p in live_positions]
                    if codes:
                        quote_map = adapter.get_full_tick(codes)
                log.info("✅ QMT重连成功（第%d次恢复）", _qmt_reconnect_count)
                if critical_window:
                    _notify("connection", "✅ 账户重连成功", "QMT连接已恢复正常。")
                _qmt_reconnect_count = 0
            except Exception as retry_err:
                if critical_window:
                    log.warning("⚠️ QMT重连失败（第%d次），等待下次重试：%s",
                                _qmt_reconnect_count, retry_err)
                else:
                    log.info("QMT非交易时段静默重连未恢复（第%d次），下次低频重试：%s",
                             _qmt_reconnect_count, retry_err)
                # 仅关键窗口连续多次失败时告警；非交易时段不升级为持续响铃。
                if critical_window and _qmt_reconnect_count >= 3:
                    _notify("system_error", "❌ QMT持续掉线",
                            "QMT连接已连续多次重连失败，实盘下单/平仓可能受影响，请立即检查。",
                            level="critical", call=True)
                return

    now_str = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    acct_id = str(account.account_id or "")
    masked_acct = f"****{acct_id[-2:]}" if len(acct_id) >= 2 else f"****{acct_id}"
    total_asset = float(getattr(account, "total_asset", 0.0) or 0.0)
    live_positions = [p for p in (positions or []) if int(getattr(p, "volume", 0) or 0) > 0]
    _last_account_has_position = bool(live_positions)
    if live_positions:
        _note_broker_has_positions()
        local_positions = load_positions()
        local_pos_map: dict[str, dict[str, Any]] = {}
        for lp in local_positions:
            if str(lp.get("status", "")).lower() not in {"open", "sell_pending"}:
                continue
            for alias in _ts_code_aliases(lp.get("ts_code", "")):
                local_pos_map[alias] = lp
        notify_cfg = config.get("notify", {}) if isinstance(config, dict) else {}
        loss_thresholds = notify_cfg.get("position_loss_alert_thresholds_pct", [-5, -10, -15, -20, -30])
        try:
            loss_thresholds = sorted({float(x) for x in loss_thresholds if float(x) < 0}, reverse=True)
        except Exception:
            loss_thresholds = [-5.0, -10.0, -15.0, -20.0, -30.0]
        positions_dirty = False
        pos_parts = []
        for p in live_positions:
            current_price = p.market_value / p.volume
            lp = {}
            for alias in _ts_code_aliases(p.ts_code):
                if alias in local_pos_map:
                    lp = local_pos_map[alias]
                    break
            strategy_leg = str(lp.get("strategy_leg", "未知") or "未知").upper()
            name_s = str(lp.get("name") or getattr(p, "name", "") or "")
            buy_price = float(lp.get("buy_price", 0) or 0)
            if buy_price <= 0:
                buy_price = float(getattr(p, "cost_price", 0.0) or 0.0)
            buy_time_text = _fmt_position_time(lp.get("buy_time") or lp.get("buy_date"))
            exit_time_text = _fmt_position_time(
                lp.get("planned_exit_time") or lp.get("planned_exit_date"),
                default_time="14:56",
                trim_zero_seconds=True,
            )
            time_text = ""
            if buy_time_text:
                time_text += f"开{buy_time_text} "
            if exit_time_text:
                time_text += f"～{exit_time_text}平 "

            # 今日涨跌幅（相对昨收）
            quote = quote_map.get(p.ts_code)
            pre_close = float(getattr(quote, "pre_close", 0.0) or 0.0) if quote else 0.0
            if pre_close > 0:
                chg_pct = (current_price - pre_close) / pre_close * 100
                chg_sign = "+" if chg_pct >= 0 else ""
                chg_str = f"今日{chg_sign}{chg_pct:.2f}% "
            else:
                chg_str = ""

            if buy_price > 0:
                pnl_pct = (current_price - buy_price) / buy_price * 100
                pnl_sign = "+" if pnl_pct >= 0 else ""
                notified_thresholds = {
                    str(x)
                    for x in (lp.get("notified_loss_thresholds") or [])
                }
                for threshold in loss_thresholds:
                    threshold_key = str(int(threshold)) if float(threshold).is_integer() else str(threshold)
                    if pnl_pct <= threshold and threshold_key not in notified_thresholds:
                        level = "critical" if threshold <= -15 else "timeSensitive"
                        call = threshold <= -15
                        _notify(
                            "position_risk",
                            f"⚠️ 持仓浮亏达到{threshold_key}%",
                            (
                                f"策略={strategy_leg} {p.ts_code} {lp.get('name', '')} 当前收益{pnl_pct:.2f}% "
                                f"买入{buy_price:.2f} 现价{current_price:.2f} "
                                f"持仓{int(p.volume)}股 市值{p.market_value / 10000:.2f}万"
                            ),
                            level=level,
                            call=call,
                        )
                        notified_thresholds.add(threshold_key)
                        lp["notified_loss_thresholds"] = sorted(
                            notified_thresholds,
                            key=lambda x: float(x),
                            reverse=True,
                        )
                        positions_dirty = True
                pos_parts.append(
                    f"策略={strategy_leg} {p.ts_code} {name_s} ×{int(p.volume)}股 "
                    f"{time_text}"
                    f"现价{current_price:.2f} "
                    f"{chg_str}"
                    f"收益{pnl_sign}{pnl_pct:.2f}% "
                    f"市值{p.market_value / 10000:.2f}万"
                )
            else:
                pos_parts.append(
                    f"策略={strategy_leg} {p.ts_code} {name_s} ×{int(p.volume)}股 "
                    f"{time_text}"
                    f"现价{current_price:.2f} "
                    f"{chg_str}"
                    f"市值{p.market_value / 10000:.2f}万"
                )
        if positions_dirty:
            save_positions(local_positions)
        log.info("✅ [账户] %s 总资产%.2f万 | 持仓：%s",
                 masked_acct, total_asset / 10000,
                 "  ".join(pos_parts))
    else:
        clear_local_positions_when_broker_empty("账户心跳")
        log.info("✅ [账户] %s | 账户%s 总资产%.2f万 | 无持仓",
                 now_str, masked_acct, total_asset / 10000)


def check_qmt_connection(*, allow_full_scan: bool | None = None) -> bool:
    global _qmt_last_verified_at, _last_account_has_position, _qmt_reconnect_count

    log = logger()
    try:
        config = load_json_config(PROJECT_ROOT / "config" / "config.json")
        if not (config.get("broker_adapter_enabled") and config.get("qmt_enabled") and
                config.get("broker", {}).get("enabled")):
            log.info("QMT 未启用，跳过连接检查")
            return True

        # 启动门禁必须建立主进程自己的持久连接，而不是只用子进程探测。
        # 否则会出现“启动验证成功，但D监控/账户心跳第一次使用QMT又重新连接并超时”的双连接口径。
        broker_config = config.get("broker", {})
        scan_desc = "完整扫描备用path/session" if allow_full_scan else "上次成功path/session优先"
        log.info("QMT启动门禁：建立主进程持久连接并验证账户/持仓（%s；不并发抢QMT）。", scan_desc)
        with _qmt_lock:
            adapter = _qmt_get(broker_config, allow_full_scan=allow_full_scan)
            account, positions = _qmt_query_account_positions(adapter)

        account_id = str(getattr(account, "account_id", "") or getattr(getattr(adapter, "config", None), "account_id", ""))
        available_cash = float(getattr(account, "available_cash", 0.0) or 0.0)
        _qmt_last_verified_at = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
        _last_account_has_position = any(float(getattr(p, "volume", 0) or 0) > 0 for p in positions)
        _qmt_reconnect_count = 0
        log.info(
            "✅ QMT连接成功且账户已验证：账户 %s，可用资金 %.0f 元（主进程持久连接，path=%s session=%s）",
            account_id,
            available_cash,
            getattr(adapter, "_active_qmt_path", ""),
            getattr(adapter, "_active_session_id", ""),
        )
        _notify_async(
            "connection",
            "✅ 账户连接成功",
            f"守护进程启动就绪，QMT主连接已建立，账户{_mask_account(account_id)}。",
        )
        return True
    except Exception as e:
        log.error("❌ QMT主连接验证失败：%s", e)
        try:
            with _qmt_lock:
                _qmt_reset()
        except Exception:
            pass
        _notify_async("system_error", "❌ QMT启动连接异常",
                      "守护进程启动连接QMT时发生异常，实盘功能不可用，请立即检查。",
                      level="critical", call=True)
        return False


def wait_for_qmt_startup_gate() -> None:
    """QMT 启动门禁：账户未验证成功前，不进入启动检查和主循环。

    这里验证的是主守护进程自己的持久 QMT 连接。
    启动成功后，账户心跳、盘前下单和D线程都复用同一个连接，避免多进程互抢QMT session。
    """
    log = logger()
    round_no = 0
    while True:
        round_no += 1
        log.info("QMT启动门禁：第%d轮验证账户连接，验证成功前不执行启动检查/下次任务。", round_no)
        # 第1轮就允许全量扫描：缓存 session 在刚 stop 旧 daemon 后常被占用（connect=-1），
        # 若第1轮只试缓存必然失败，白白多花“一整轮 + sleep 10s + 一条误告警”。
        # 全量扫描本身仍是“缓存 preferred 优先，失败才 fallback 扫描”：
        # 缓存可用时 preferred 秒连、速度不变；缓存坏时同一轮内直接扫到可用 session。
        allow_full_scan = True
        if check_qmt_connection(allow_full_scan=allow_full_scan):
            log.info("QMT启动门禁：账户连接已验证，继续启动流程。")
            return
        write_heartbeat("qmt_blocked")
        log.error("QMT启动门禁：账户连接未验证成功，10秒后继续重试；不会进入启动检查和任务调度。")
        time.sleep(10)


def _should_run_startup_signal_audit(now: datetime.datetime) -> bool:
    """启动时是否自动跑重量信号审计。

    信号审计会读取成交打标、市场情绪、A/B/C逐层漏斗等文件，纯属人工复盘展示；
    交易决策由09:00/09:15/09:20定时任务和组合状态机负责，不依赖启动审计输出。
    为避免启动后抢 CPU/磁盘、拖慢 QMT 心跳，只有盘前关键窗口自动跑。
    """
    if not is_trade_day(now.date()):
        return False
    return datetime.time(8, 50) <= now.time() <= datetime.time(9, 35)


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

    # 启动硬门禁：QMT账户没有验证成功前，不做任何交易/数据/候选/调度动作。
    wait_for_qmt_startup_gate()

    ensure_trade_calendar_fresh()

    # 交易日历晨检（每自然日08:30，确保9点前明确今天是否交易日）
    threading.Thread(
        target=_daily_calendar_sentinel,
        daemon=True,
        name="calendar-sentinel",
    ).start()

    # 收盘平仓看门狗（14:52/14:55核查到期持仓是否有人管，只告警不下单）
    threading.Thread(
        target=_close_position_watchdog,
        daemon=True,
        name="close-watchdog",
    ).start()

    # 开仓决策链周期播报（每30分钟，纯展示）：已有决策结果且执行日未过期时重播。
    threading.Thread(
        target=_decision_chain_broadcast_loop,
        daemon=True,
        name="decision-chain-broadcast",
    ).start()

    # 盘中涨停止盈监控（当日到期持仓涨幅≥7%挂涨停-0.01卖单，14:55未成交撤单）
    threading.Thread(
        target=_intraday_takeprofit_monitor,
        daemon=True,
        name="intraday-takeprofit",
    ).start()

    # ── 启动时立刻执行平仓检查 ────────────────────────────────────────────────
    log.info("启动检查：扫描逾期/待平仓持仓...")
    try:
        check_and_close_positions()
    except Exception as e:
        log.error("启动平仓检查异常：%s —— 请立即手动检查持仓！", e)
    startup_has_position = has_open_local_position()
    if startup_has_position:
        log.info("启动检查：检测到已有持仓，仍会检查/补跑收盘数据与候选信号；仅跳过D/E2盘中补开仓逻辑。")
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

    # 候选播报放后台线程：纯展示，不影响开仓关键路径。
    # 重量信号审计只在盘前关键窗口自动跑；盘中/夜间启动只播报候选和L/model3状态。
    # 如果收盘数据缺失，先让收盘流水线补齐，流水线完成后会自行播报，避免启动时先打印旧审计。
    def _startup_report() -> None:
        try:
            report_next_day_candidates()
            if _should_run_startup_signal_audit(now_beijing()):
                report_signal_readiness_summary(expected_str)
            else:
                log.info(
                    "启动信号审计：当前不在盘前关键窗口，已跳过重量A/B/C逐层审计；"
                    "候选与L/model3状态已播报，完整审计会在盘前/收盘流水线按需执行。"
                )
        except Exception as exc:
            log.error("启动候选播报异常：%s", exc)

    # 若缓存或 processed 审计数据不是最新交易日数据，后台线程补采。
    # raw 缓存存在但 processed 缺日时也必须补跑，否则盘前审计会误报旧口径。
    if not _has_signal_for_date(expected):
        log.warning("未找到 %s 收盘数据缓存，后台启动收盘/采集流水线补齐；若遇到平仓窗口会自动暂停让路。", expected_str)
        _start_post_market_pipeline(expected_str, reason=f"启动补齐缺失收盘信号 {expected_str}")
    elif not _processed_data_ready_for_date(expected):
        log.warning("已有 %s 收盘信号缓存，但 processed 审计数据缺失该日期，后台启动流水线补齐；raw缓存命中会直接跳过采集。", expected_str)
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
        log.info("启动补检：已有持仓，跳过D监控/E2延迟开仓补启动。")

    # ── 主循环 ────────────────────────────────────────────────────────────────
    while True:
        write_heartbeat("running")
        now = now_beijing()
        wake_dt, sched_time = next_event(now)
        sleep_secs = (wake_dt - now).total_seconds()
        log.info("下次任务：%s（%.0f 秒后）", wake_dt.strftime("%Y-%m-%d %H:%M"), sleep_secs)

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
        if has_open_local_position() or _last_account_has_position:
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
            if _qmt_reconnect_count == 0:
                has_position_for_poll = has_open_local_position() or _last_account_has_position
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
