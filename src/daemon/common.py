"""守护进程基础：路径和常量（原文件“常量”一节）、日志、推送、交易日历、心跳和账户健康文件、
子进程运行、收盘流水线暂停/恢复、进程级恢复。

由 scripts/trading_daemon.py 按职责纯搬移拆出（2026-11-14，项目优化第4步）；分层规则见 src/daemon/__init__.py。
"""
from __future__ import annotations

import datetime
import csv
import json
import os
import subprocess
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
import sys as _sys
import platform as _platform

from src.utils.logger import get_logger
from src.utils.config import load_json_config, mkdir_p
from src.utils.time_utils import BEIJING_TZ, now_beijing, today_beijing
from src.execution_completion_tracker import ExecutionCompletionTracker


# 项目根目录。本文件在 src/daemon/ 下，所以是 parents[2]（原 scripts/trading_daemon.py 里是 parents[1]，同一个目录）。
# resolve() 在 Windows WebDAV 盘符（Z:\）上会展开为 UNC 路径导致文件 I/O 失败，用 absolute() 保留盘符路径。
PROJECT_ROOT = Path(__file__).absolute().parents[2]


_execution_completion_tracker = ExecutionCompletionTracker(PROJECT_ROOT)


def _track_execution(method: str, **values: Any) -> Any:
    """执行审计失败只能告警，绝不能改变下单、撤单或持仓回写结果。"""
    try:
        return getattr(_execution_completion_tracker, method)(**values)
    except Exception as exc:  # noqa: BLE001
        try:
            get_logger("a_share_quant").warning("成交完成率记录失败（%s，不影响交易）：%s", method, exc)
        except Exception:
            pass  # 可以忽略：记录失败的日志本身写不了时不能再抛，执行审计不能影响交易
        return None


def _notify(event: str, title: str, body: str = "", *, level: str = "active",
            call: bool = False) -> bool:
    """告警推送（失败安全，绝不影响交易主流程）。

    正文口径：账号仅显示后2位（***03）；金额用「万」单位2位小数；标的可含代码+名称。
    call=True 为重大错误（崩溃/下单失败/平仓失败/账户断连）：除警报式持续响铃外，
    额外再发一条普通通知。两条独立——即便用户在系统权限里关闭了「重要提醒」（警报不响），
    普通通知仍可见，确保不漏掉。
    """
    try:
        from src.notify import notify
        if call:
            # 普通通知（永远可见，不受警报权限影响）
            normal_sent = bool(notify(event, title, body, level="active", call=False))
            # 警报式（持续响铃，静音/勿扰也响，需「重要提醒」权限）
            critical_sent = bool(notify(event, title, body, level="critical", call=True))
            return normal_sent or critical_sent
        else:
            return bool(notify(event, title, body, level=level, call=False))
    except Exception as exc:  # noqa: BLE001
        try:
            get_logger("a_share_quant").warning("告警推送异常：%s", exc)
        except Exception:
            pass  # 可以忽略：推送异常的日志本身写不了时不能再抛
        return False


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


def _notify_with_retry_async(
    event: str,
    title: str,
    body: str = "",
    *,
    level: str = "active",
    call: bool = False,
    attempts: int = 3,
    retry_sec: float = 30.0,
) -> None:
    """后台确认通知送达；网络失败时按有界次数重试。"""

    def _worker() -> None:
        total = max(int(attempts), 1)
        for index in range(total):
            if _notify(event, title, body, level=level, call=call):
                return
            if index + 1 < total:
                time.sleep(max(float(retry_sec), 0.0))
        logger().warning(
            "通知连续%d次未确认送达：[%s] %s",
            total, event, title,
        )

    threading.Thread(
        target=_worker,
        daemon=True,
        name=f"notify-retry-{event}",
    ).start()


def _mask_account(account_id: str) -> str:
    """账号展示：统一前缀三星号，只保留后两位。"""
    from src.account_privacy import mask_account_id
    return mask_account_id(account_id)


def _fmt_wan(amount: float) -> str:
    """金额格式化为「万」单位、2位小数。"""
    try:
        return f"{float(amount) / 10000:.2f}万"
    except Exception:
        return "0.00万"


def _execution_time_only(value: Any) -> str:
    """把券商时间统一为HH:MM:SS，避免ISO时区后缀被误当成成交时间。"""
    text = str(value or "").strip()
    if len(text) >= 19 and text[10] in {" ", "T"}:
        return text[11:19]
    if len(text) >= 8 and text[-8:-6].isdigit() and text[-5:-3].isdigit():
        return text[-8:]
    return text


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
    datetime.time(8, 50),   # 早盘推送：今日开仓计划（读昨晚缓存的轻活，不与09:00生成任务抢时间）
    datetime.time(9, 0),    # 盘前：提前生成/刷新组合状态机，避免09:20才开始算买入决策
    datetime.time(9, 20),   # 盘前：平仓检查 + 组合状态机复核 + 按涨停价预挂买入
    datetime.time(9, 23),   # 集合竞价：D接力仅提交盘口可安全承接的部分
    datetime.time(9, 26),   # 集合竞价成交后：同步实盘持仓，刷新今日买入决策
    datetime.time(9, 30),   # 开盘：若9:20预挂未成功则补充买入
    datetime.time(9, 35),   # 开仓核查：计划没买成必有推送提醒（不允许无声漏买）
    datetime.time(14, 40),  # 撤未成交买单（提前于平仓，绝不挡住14:55平仓通道）
    datetime.time(14, 55),  # 盘中收盘平仓（最高优先，独占QMT通道）
    datetime.time(15, 10),  # 收盘流水线
]
SCHED_MORNING_PLAN_PUSH = datetime.time(8, 50)
SCHED_PREOPEN_PLAN = datetime.time(9, 0)
SCHED_MORNING_REVIEW = datetime.time(9, 20)
SCHED_PREMARKET_SELL = datetime.time(9, 23)
SCHED_PREMARKET_SYNC = datetime.time(9, 26)
SCHED_OPENING_BUY = datetime.time(9, 30)
SCHED_OPEN_PLAN_FILL_CHECK = datetime.time(9, 35)
SCHED_AFTERNOON_CLOSE = datetime.time(14, 55)
# 撤未成交买单：2026-07-15 起提前到平仓之前、独立调度（14:40与14:55相距
# 15分钟）。目的：平仓时点独占QMT通道，绝不被撤单挡路；
# 且排队一天未成交的开仓买单（一字板排队）尾盘炸板成交=高位接盘，早撤无损失。
SCHED_CANCEL_BUY_ORDERS = datetime.time(14, 40)
# 平仓静默窗起点由 live_trade.exit_pov_handoff_time 统一驱动；
# 14:54:30~14:58账户心跳另行跳过，确保交权、14:55主平仓及
# 14:56:20撤单/竞价兜底时QMT通道零竞争。
SCHED_EXIT_LARGE_FORCE_START = datetime.time(14, 15)
# 连续竞价主平仓余单的最晚撤单起点。提前于14:57留出多轮状态复核；此后
# 14:57前禁止再新发连续竞价平仓单，避免旧价格笼子委托带入收盘集合竞价。
SCHED_CLOSE_AUCTION_HANDOFF = datetime.time(14, 56, 20)
SCHED_POST_MARKET = datetime.time(15, 10)
EXIT_CLOSE_SUBMIT_CLOCK_GUARD_DEFAULT_SEC = 2.0
if _platform.system() == "Windows":
    # Z: 盘上的 .venv/bin/python 是 Mac ARM64 二进制，Windows 无法执行
    PYTHON = _sys.executable
else:
    _venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    PYTHON = str(_venv_python) if _venv_python.exists() else _sys.executable
POSITIONS_FILE = PROJECT_ROOT / "data" / "processed" / "positions.json"
PENDING_BUY_FILE = PROJECT_ROOT / "data" / "processed" / "pending_premarket_buy.json"
E_CAPACITY_FILE = PROJECT_ROOT / "data" / "processed" / "e_capacity_history.json"
LEGACY_E_CAPACITY_FILE = PROJECT_ROOT / "data" / "processed" / "e2_capacity_history.json"
# 卖出批次与PREPARED intent必须持久化：若进程恰在“券商已受理、QMT尚未
# 回显”窗口崩溃，重启后仅靠query_orders会误以为没有旧单并重复卖出。
EXIT_EXECUTION_STATE_FILE = (
    PROJECT_ROOT / "data" / "processed" / "exit_execution_state.json"
)
D_RELAY_PAIR_STATE_FILE = PROJECT_ROOT / "data" / "state" / "d_relay_pair_state.json"
D_RELAY_PAIR_EXEC_LOG = PROJECT_ROOT / "reports" / "d_relay_pair_execution_log.csv"
HEARTBEAT_FILE = PROJECT_ROOT / "logs" / "daemon_heartbeat.txt"
BROKER_HEALTH_FILE = PROJECT_ROOT / "logs" / "broker_health.json"
D_MONITOR_PID_FILE = PROJECT_ROOT / "logs" / "strategy_d_monitor.pid"
QMT_LAST_SUCCESS_FILE = PROJECT_ROOT / "logs" / "qmt_last_success.json"
CALENDAR_STALE_WARNED: set[str] = set()
_TRADE_CALENDAR_CACHE: dict[str, Any] = {
    "mtime": None,
    "open_dates": set(),
    "max_date": "",
}
_ACCOUNT_STATUS_DAILY_CACHE: dict[str, Any] = {
    "path": "",
    "mtime_ns": None,
    "price_column": "",
    "prices": {},
}
_pending_buy_lock = threading.RLock()
_broker_health_write_lock = threading.Lock()
# positions.json 会被POV、14:55主平仓、看门狗和账户心跳并发读改写。
# 原子replace只能防半文件，不能防“两个线程各自读旧值后相互覆盖”。
_positions_file_lock = threading.RLock()
_exit_execution_state_lock = threading.RLock()
# 卖出生命周期互斥：与_qmt_lock（只保护单次QMT调用）职责不同。该锁覆盖
# 下单→确认→撤单→终态回写，避免POV和14:55主平仓交叉冻结/重复卖出。
_exit_sell_lock = threading.RLock()
_d_relay_pair_state_lock = threading.RLock()
_d_relay_pair_thread_lock = threading.Lock()
_EXIT_POV_REMARK_PREFIX = "POV平滑卖"
_D_RELAY_SELL_REMARK_PREFIX = "D接力卖-"
_SYSTEM_EXIT_REMARK_PREFIXES = (
    _EXIT_POV_REMARK_PREFIX,
    _D_RELAY_SELL_REMARK_PREFIX,
    "ABC平仓-",
    "看门狗补挂-",
    "盘中止盈",
)
SCHED_EXIT_POV_HANDOFF = datetime.time(14, 53)

# subprocess 超时（秒）：防止某步骤挂死
TIMEOUT_DATA_STEP = 600      # 数据采集/清洗步骤：10 分钟
TIMEOUT_SIGNAL_STEP = 900    # 信号生成步骤：15 分钟；A/C 首次加载特征较慢，不能误杀导致旧信号
TIMEOUT_ORDER_STEP = 60      # 下单预览步骤：1 分钟
TIMEOUT_COMBINED_PLAN_STEP = 180  # 组合状态机生成：Windows/QMT环境下首次加载特征较慢
TIMEOUT_EQUITY_CURVE_STOP_STEP = 1800  # 方案甲停手门禁：2019起全量重建研究池+影子回放，Mac约4分钟


def logger():
    return get_logger("trading_daemon")


def _replace_runtime_file_with_retry(source: Path, destination: Path) -> None:
    """重试 Windows sharing violation，不把状态文件占用误判为进程/账户异常。"""
    for attempt in range(12):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            retryable = isinstance(exc, PermissionError) or getattr(
                exc, "winerror", None
            ) in {5, 32, 33}
            if not retryable or attempt == 11:
                raise
            time.sleep(min(0.05 * (attempt + 1), 0.25))


def write_heartbeat(status: str = "running") -> None:
    """原子更新daemon心跳，避免keeper读到write_text截断后的空文件。

    Windows侧keeper每30秒读取一次心跳。旧实现直接 ``write_text``，会先把
    目标文件截断再写入；keeper若恰好落在这个极短窗口，会解析出
    ``heartbeat_pid=None``，把仍在正常扫描的daemon误判为旧PID并强制重启。
    心跳和broker_health一样必须采用同目录临时文件 + ``os.replace``。
    """
    tmp = HEARTBEAT_FILE.with_name(
        f".{HEARTBEAT_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            f"{now_beijing().isoformat()} pid={os.getpid()} {status}\n",
            encoding="utf-8",
        )
        _replace_runtime_file_with_retry(tmp, HEARTBEAT_FILE)
    except Exception:
        pass  # 可以忽略：心跳写失败由外部看门狗按心跳过期告警，这里不每分钟刷屏
    finally:
        tmp.unlink(missing_ok=True)


def write_broker_health(
    status: str,
    *,
    account_id: str = "",
    error: Any = "",
    failure_count: int = 0,
) -> None:
    """原子写入账户连接健康状态，供外层 keeper 做真实恢复判定。

    daemon 心跳只证明 Python 进程还活着，不能证明 QMT 账户可查询。这里单独记录
    当前进程 PID、账户查询结果与更新时间，keeper 只有在 PID 匹配且 status=verified
    时才允许发送“程序与账户均恢复”通知，避免沿用上个进程的成功状态产生假恢复。
    """
    temp_path: Path | None = None
    try:
        with _broker_health_write_lock:
            mkdir_p(BROKER_HEALTH_FILE.parent)
            error_text = " ".join(str(error or "").split())[:500]
            payload = {
                "updated_at": now_beijing().isoformat(),
                "updated_ts": time.time(),
                "pid": os.getpid(),
                "status": str(status or "unknown"),
                "account": _mask_account(account_id) if account_id else "",
                "failure_count": max(0, int(failure_count or 0)),
                "error": error_text,
            }
            temp_path = BROKER_HEALTH_FILE.with_name(
                f".{BROKER_HEALTH_FILE.name}.{os.getpid()}."
                f"{threading.get_ident()}.{time.time_ns()}.tmp"
            )
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _replace_runtime_file_with_retry(temp_path, BROKER_HEALTH_FILE)
    except Exception as exc:  # noqa: BLE001
        try:
            logger().warning("写入QMT账户健康状态失败（不影响交易主流程）：%s", exc)
        except Exception:
            pass  # 可以忽略：写健康状态失败的日志本身写不了时不能再抛
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass  # 可以忽略：临时文件清理失败不影响健康状态写入结果


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


def market_is_open() -> bool:
    now = now_beijing()
    if not is_trade_day(now.date()):
        return False
    return (
        datetime.time(9, 30) <= now.time() <= datetime.time(11, 30)
        or datetime.time(13, 0) <= now.time() <= datetime.time(15, 0)
    )


def append_risk_flag(current: Any, flag: str) -> str:
    text = "" if current is None or str(current) == "nan" else str(current)
    return flag if not text else f"{text}|{flag}"


def _floor_buy_quantity_by_amount(amount: float, price: float, lot_size: int = 100) -> int:
    """按金额上限向下取整买入股数，保证“股数×委托价”绝不超过金额上限。"""
    if amount <= 0 or price <= 0 or lot_size <= 0:
        return 0
    quantity = int(Decimal(str(amount)) / Decimal(str(price)))
    quantity -= quantity % lot_size
    # Decimal负责精确折算；最后一层浮点复核防止券商侧按两位价格计算时越线。
    while quantity > 0 and quantity * price > amount + 1e-6:
        quantity -= lot_size
    return max(quantity, 0)


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


def _sleep_until_beijing(target: datetime.time, *, max_wait: float = 300.0) -> None:
    """阻塞到北京时间当天的 ``target`` 时刻；已过则立即返回。

    ``sleep`` 允许比目标时刻早几毫秒唤醒，且 Windows 墙钟与 monotonic 时钟
    可能有轻微校正。旧实现只睡一次，2026-08-12 就在 14:54:59.99x 进入
    14:55 任务。这里用短循环复核墙钟；``max_wait`` 只限制本次额外校时等待，
    不改变调度器原本等待数小时到目标任务的行为。
    """
    deadline = time.monotonic() + max(float(max_wait), 0.0)
    while True:
        now = now_beijing()
        target_dt = datetime.datetime.combine(now.date(), target, tzinfo=BEIJING_TZ)
        wait = (target_dt - now).total_seconds()
        if wait <= 0:
            return
        remaining_budget = deadline - time.monotonic()
        if remaining_budget <= 0:
            logger().warning(
                "调度墙钟对齐超过%.1f秒：目标%s，当前%s；按任务身份继续执行并保留告警/兜底链路。",
                max(float(max_wait), 0.0),
                target.strftime("%H:%M:%S"),
                now.strftime("%H:%M:%S.%f"),
            )
            return
        time.sleep(min(wait, remaining_budget, 0.2))


def _load_qmt_last_success() -> dict[str, Any]:
    try:
        if QMT_LAST_SUCCESS_FILE.exists():
            data = json.loads(QMT_LAST_SUCCESS_FILE.read_text(encoding="utf-8"))
            if data.get("qmt_path") and data.get("session_id"):
                return data
    except Exception:
        return {}
    return {}


def _save_qmt_last_success(*, qmt_path: str, session_id: str) -> None:
    try:
        if not qmt_path or not session_id:
            return
        mkdir_p(QMT_LAST_SUCCESS_FILE.parent)
        QMT_LAST_SUCCESS_FILE.write_text(
            json.dumps(
                {
                    "qmt_path": str(qmt_path),
                    "session_id": str(session_id),
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


EXIT_CODE_EXECUTION_STATE_RECOVERY = 90
_ONCE_PER_STATE: dict[str, float] = {}
_ONCE_PER_ATTEMPT: dict[str, float] = {}
_PROCESS_RECOVERY_EVENT = threading.Event()
# 兼容现有运维测试/诊断名；所有致命交易状态统一由同一个进程恢复门控制，
# 避免QMT、D、盘前订单各自同时发起退出和重复通知。
_QMT_FATAL_RESTART_EVENT = _PROCESS_RECOVERY_EVENT

# Windows 套接字/句柄资源耗尽类错误码。出现这些说明系统资源已经见底，
# 再怎么重试都不会成功，只会让泄漏更严重——必须停手、告警、等人工重启。
_SOCKET_EXHAUSTED_MARKERS = (
    "10055",   # WSAENOBUFS 系统缓冲区空间不足或队列已满
    "10024",   # WSAEMFILE 打开的套接字太多
    "缓冲区空间不足",
    "队列已满",
    "打开的套接字太多",
    "WaitingFreeWriter instances exceed maximum limit",
)


def _is_socket_resource_exhausted(err_text: str) -> bool:
    text = str(err_text or "")
    return any(marker in text for marker in _SOCKET_EXHAUSTED_MARKERS)


def _request_process_recovery(
    *,
    reason: str,
    exit_code: int,
    heartbeat_status: str,
    title: str,
    body: str,
    clear_qmt_cache: bool = False,
) -> bool:
    """统一请求整进程恢复；同一进程只允许一个恢复发起者。

    券商已受理后的本地状态失败、D成交回写失败和QMT底层永久卡死都不能
    在线程内继续。退出后keeper只负责重新拉起；新进程必须先通过交易恢复
    门禁，再启动开仓/平仓线程。
    """

    if _PROCESS_RECOVERY_EVENT.is_set():
        return False
    _PROCESS_RECOVERY_EVENT.set()
    logger().critical("🛑 %s。程序将退出，由keeper启动全新进程并按券商真实状态恢复。", reason)
    write_broker_health("restarting", error=reason)
    write_heartbeat(heartbeat_status)
    if clear_qmt_cache:
        try:
            _clear_qmt_last_success(reason)
        except Exception as exc:
            logger().warning("清除QMT上次成功缓存失败（不影响进程恢复）：%s", exc)
    _notify_async(
        "system_error",
        title,
        body,
        level="critical",
        call=True,
    )

    def _terminate_for_recovery() -> None:
        time.sleep(3.0)
        os._exit(exit_code)

    threading.Thread(
        target=_terminate_for_recovery,
        daemon=True,
        name=f"trade-recovery-process-exit-{exit_code}",
    ).start()
    return True


def _critical_execution_state_failure(operation: str, error: BaseException) -> None:
    """关键交易状态不可持久化/读取时停止本进程继续交易。"""

    reason = f"关键交易状态失败:{operation}:{error}"
    _request_process_recovery(
        reason=reason,
        exit_code=EXIT_CODE_EXECUTION_STATE_RECOVERY,
        heartbeat_status="execution_state_recovery_restarting",
        title="🔄 交易状态异常，程序自动恢复",
        body=(
            f"{operation}失败。当前进程已停止继续开平仓；keeper拉起新进程后，"
            "会先按QMT真实持仓、委托、成交和事务账本恢复，避免重复交易。"
        ),
    )
    # SystemExit不属于Exception，能穿透各交易任务的“单笔异常继续”保护；
    # 主线程会直接以专用退出码结束，工作线程则先结束自身，再由上面的
    # 统一退出线程终止整个进程。不能在这类错误后继续提交下一笔委托。
    raise SystemExit(EXIT_CODE_EXECUTION_STATE_RECOVERY) from error


def _notify_once_per(key: str, interval_sec: float, title: str, body: str, **kwargs: Any) -> bool:
    """同一key只对“成功送达”节流；失败后带短退避重试。"""
    now_ts = time.time()
    last = _ONCE_PER_STATE.get(key, 0.0)
    if now_ts - last < interval_sec:
        return False
    retry_backoff = min(max(float(interval_sec), 1.0), 60.0)
    last_attempt = _ONCE_PER_ATTEMPT.get(key, 0.0)
    if now_ts - last_attempt < retry_backoff:
        return False
    _ONCE_PER_ATTEMPT[key] = now_ts
    try:
        sent = bool(_notify("system_error", title, body, **kwargs))
        if sent:
            _ONCE_PER_STATE[key] = time.time()
        return sent
    except Exception as exc:  # noqa: BLE001
        logger().warning("资源告警推送失败：%s", exc)
        return False
