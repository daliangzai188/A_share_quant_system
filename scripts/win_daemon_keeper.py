# -*- coding: utf-8 -*-
"""Windows侧daemon进程守护器。

责任边界只有三项：确认PID存活、确认原子心跳属于当前PID、进程退出/假死时
拉起新daemon。keeper不读取QMT账户、委托、成交、持仓或交易恢复状态，也不参与
任何交易判断。QMT连接、交易门禁和恢复通知均由trading_daemon自己负责。

用法：无需手动运行。start_windows.py 会自动拉起本守护器，
    stop_windows.py 会连带停止。用户的启动/停止命令保持不变。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PID_FILE = PROJECT_ROOT / ".daemon_pid"
# keeper 自己的 pid 文件：stop_windows.py 会先停 keeper 再停 daemon，
# 否则用户按老习惯只停 daemon，keeper 会在 30 秒内把它拉回来（2026-07-27）。
KEEPER_PID_FILE = PROJECT_ROOT / ".keeper_pid"
HEARTBEAT = PROJECT_ROOT / "logs" / "daemon_heartbeat.txt"
KEEPER_LOG = PROJECT_ROOT / "logs" / "win_daemon_keeper.log"
START_SCRIPT = PROJECT_ROOT / "start_windows.py"
BEIJING = timezone(timedelta(hours=8))


def _load_process_keeper_settings() -> dict:
    try:
        config_path = PROJECT_ROOT / "config" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        settings = config.get("process_keeper", {})
        return settings if isinstance(settings, dict) else {}
    except Exception:
        return {}


def _positive_int(settings: dict, key: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(settings.get(key, default)))
    except (TypeError, ValueError):
        return default


_PROCESS_KEEPER_SETTINGS = _load_process_keeper_settings()
CHECK_INTERVAL = _positive_int(_PROCESS_KEEPER_SETTINGS, "check_interval_sec", 30, 5)
STALE_LIMIT = _positive_int(_PROCESS_KEEPER_SETTINGS, "heartbeat_stale_sec", 15 * 60, 60)

# 防重启风暴：daemon 若因配置错/依赖坏/磁盘满等原因“一启动就崩”，不能每30秒
# 无限拉起。前 N 次快速拉起，之后降为低频永久重试并告警；不会永久放弃自愈。
MAX_CONSECUTIVE_RESTARTS = _positive_int(
    _PROCESS_KEEPER_SETTINGS, "max_fast_restarts", 5, 1
)
MIN_ALIVE_SEC = _positive_int(_PROCESS_KEEPER_SETTINGS, "stable_alive_sec", 120, 30)
CRASH_LOOP_RETRY_SEC = _positive_int(
    _PROCESS_KEEPER_SETTINGS, "crash_loop_retry_sec", 10 * 60, 60
)
STARTUP_HEARTBEAT_GRACE_SEC = _positive_int(
    _PROCESS_KEEPER_SETTINGS, "startup_heartbeat_grace_sec", 5 * 60, 60
)
HEARTBEAT_PID_MISMATCH_CONFIRMATIONS = _positive_int(
    _PROCESS_KEEPER_SETTINGS, "heartbeat_pid_mismatch_confirmations", 3, 2
)


def now() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


_file_logger: logging.Logger | None = None


def _get_file_logger() -> logging.Logger:
    global _file_logger
    if _file_logger is not None:
        return _file_logger
    KEEPER_LOG.parent.mkdir(parents=True, exist_ok=True)
    file_logger = logging.getLogger("win_daemon_keeper")
    file_logger.setLevel(logging.INFO)
    file_logger.propagate = False
    if not file_logger.handlers:
        handler = RotatingFileHandler(
            KEEPER_LOG,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        file_logger.addHandler(handler)
    _file_logger = file_logger
    return file_logger


def _print_console_safely(line: str) -> None:
    """兼容 Windows GBK 控制台；终端打印失败绝不能影响守护状态。"""
    try:
        print(line, flush=True)
        return
    except UnicodeEncodeError:
        # Windows PowerShell 可能仍使用 GBK，无法直接打印“✅”等字符。
        # 这里仅替换控制台无法表示的字符，UTF-8 日志文件仍保留完整原文。
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_line = line.encode(encoding, errors="replace").decode(
            encoding,
            errors="replace",
        )
        try:
            print(safe_line, flush=True)
        except Exception:
            pass
    except Exception:
        # 控制台被关闭或重定向异常时仍继续文件日志和守护循环。
        pass


def log(msg: str) -> None:
    line = f"{now()} | [keeper] {msg}"
    _print_console_safely(line)
    try:
        _get_file_logger().info(line)
    except Exception:
        pass


def _log_without_affecting_result(msg: str) -> None:
    """尽力记录日志，但不允许日志异常改写通知通道的真实返回值。"""
    try:
        log(msg)
    except Exception:
        pass


def notify(
    title: str,
    body: str,
    level: str = "active",
    *,
    event: str = "system_error",
) -> bool:
    """走项目自带的 Bark 通道；返回值只取决于通知通道是否发送成功。"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.notify import notify as _n
        sent = bool(_n(event, title, body, level=level))
    except Exception as exc:  # noqa: BLE001
        _log_without_affecting_result(f"推送失败（不影响守护）：{exc}")
        return False

    # 通知已经成功后，即使 Windows 控制台无法打印标题里的 emoji，也必须返回 True。
    # 否则恢复标记不会清除，守护器会把一条成功通知误当失败而反复发送。
    if sent:
        _log_without_affecting_result(f"已推送：{title}")
    else:
        _log_without_affecting_result(
            f"推送未成功，后续状态轮询会按需重试：{title}"
        )
    return sent


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def pid_alive(pid: int) -> bool:
    """Windows: 用 OpenProcess 判断存活，避免依赖 tasklist 的慢与编码问题。"""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == 259   # STILL_ACTIVE
        return False
    finally:
        k32.CloseHandle(h)


def heartbeat_state() -> tuple[str, float, int | None]:
    """返回 (状态标记, 心跳年龄秒, 写入心跳的daemon PID)。"""
    age = float("inf")
    try:
        if HEARTBEAT.exists():
            age = max(0.0, time.time() - HEARTBEAT.stat().st_mtime)
        text = HEARTBEAT.read_text(encoding="utf-8").strip()
        parts = text.split()
        state = parts[-1] if parts else ""
        heartbeat_pid = None
        for part in parts:
            if part.startswith("pid=") and part[4:].isdigit():
                heartbeat_pid = int(part[4:])
                break
        return state, age, heartbeat_pid
    except Exception:
        # 文件存在且很新时，读取失败/内容为空更可能是瞬时替换或杀毒软件占用，
        # 不能伪装成“陈旧15分钟”而立即误杀仍在运行的daemon。
        return "", age, None


def heartbeat_restart_reason(
    *,
    age: float,
    heartbeat_same_process: bool,
    mismatch_count: int,
) -> str:
    """返回需要重启的心跳原因；新鲜PID解析失败必须连续确认。"""

    if age > STALE_LIMIT:
        return f"心跳陈旧 {age/60:.1f} 分钟"
    if (
        not heartbeat_same_process
        and int(mismatch_count) >= HEARTBEAT_PID_MISMATCH_CONFIRMATIONS
    ):
        return (
            "心跳连续%d次不属于当前daemon PID"
            % int(mismatch_count)
        )
    return ""


def process_heartbeat_ready(
    *, heartbeat_age: float, heartbeat_same_process: bool
) -> bool:
    """keeper的唯一恢复标准：当前PID存活且它自己的心跳新鲜。"""

    return heartbeat_age <= STALE_LIMIT and heartbeat_same_process


def restart_delay_seconds(attempt_no: int) -> int:
    """快速重启额度用尽后降频，但任意次数都仍返回下一次重试间隔。"""
    if int(attempt_no) > MAX_CONSECUTIVE_RESTARTS:
        return CRASH_LOOP_RETRY_SEC
    return CHECK_INTERVAL


def start_daemon() -> bool:
    log("拉起 daemon ...")
    try:
        result = subprocess.run(
            [sys.executable, str(START_SCRIPT), "--no-tail"],
            cwd=str(PROJECT_ROOT),
            check=False,
            timeout=120,
        )
        if result.returncode == 0:
            return True
        log(f"start_windows.py 返回 {result.returncode}，本轮拉起未确认成功。")
    except subprocess.TimeoutExpired:
        log("start_windows.py 运行超过120秒，放弃本轮并等待下次守护重试。")
    except Exception as exc:  # noqa: BLE001
        log(f"拉起 daemon 异常（会继续重试）：{exc}")
    return False


def main() -> None:
    KEEPER_PID_FILE.write_text(str(os.getpid()))
    log("守护器启动（pid=%d）：每%d秒检查一次；daemon 退出/假死自动拉起。"
        % (os.getpid(), CHECK_INTERVAL))
    log("由 start_windows.py 自动拉起；stop_windows.py 会连带停止。用户命令保持不变。")
    was_down = False
    consecutive_restarts = 0     # 连续"拉起后很快又死"的次数
    last_start_ts = 0.0
    next_restart_not_before = 0.0
    crash_loop_alerted = False
    observed_pid: int | None = None
    observed_pid_since = 0.0
    heartbeat_pid_mismatch_count = 0

    while True:
        try:
            pid = read_pid()
            alive = bool(pid and pid_alive(pid))
            _state, age, heartbeat_pid = heartbeat_state()

            # ── 1. 进程不在 → 拉起（含资源耗尽自退、崩溃、被误杀）──
            if not alive:
                observed_pid = None
                observed_pid_since = 0.0
                heartbeat_pid_mismatch_count = 0
                now_ts = time.time()
                if now_ts < next_restart_not_before:
                    time.sleep(CHECK_INTERVAL)
                    continue

                consecutive_restarts += 1

                if consecutive_restarts > MAX_CONSECUTIVE_RESTARTS:
                    if not crash_loop_alerted:
                        crash_loop_alerted = notify(
                            "🛑 daemon 持续崩溃，已转为低频永久重试",
                            f"连续 {consecutive_restarts - 1} 次快速启动均未稳定运行 "
                            f"{MIN_ALIVE_SEC} 秒。常见原因：配置错误、依赖损坏、磁盘满或运行时崩溃。"
                            f"为避免重启风暴，现改为每 {CRASH_LOOP_RETRY_SEC // 60} 分钟拉起一次，"
                            f"不会永久停止；请人工查看 logs/trading_daemon.log。"
                            f"keeper不判断账户、委托、持仓或交易动作。",
                            level="timeSensitive",
                        )

                log(f"daemon 不在运行（pid={pid}），准备拉起（第{consecutive_restarts}次）。")
                if not was_down:
                    was_down = True
                    notify("🔄 daemon 已退出，keeper正在拉起",
                           "检测到daemon进程不在运行，keeper正在启动全新进程。"
                           "keeper只确认进程恢复；QMT和交易恢复结果由daemon另行通知。",
                           level="timeSensitive")
                started = start_daemon()
                if started:
                    last_start_ts = time.time()
                retry_delay = restart_delay_seconds(consecutive_restarts)
                next_restart_not_before = time.time() + retry_delay
                time.sleep(CHECK_INTERVAL)
                continue

            if pid != observed_pid:
                observed_pid = pid
                observed_pid_since = time.time()
                heartbeat_pid_mismatch_count = 0
            heartbeat_same_process = bool(pid and heartbeat_pid == pid)
            if heartbeat_same_process:
                heartbeat_pid_mismatch_count = 0
            elif age <= STALE_LIMIT:
                heartbeat_pid_mismatch_count += 1

            # ── 2. 心跳陈旧 → 假死，强制重启 ──
            restart_reason = heartbeat_restart_reason(
                age=age,
                heartbeat_same_process=heartbeat_same_process,
                mismatch_count=heartbeat_pid_mismatch_count,
            )
            if restart_reason:
                if time.time() - observed_pid_since < STARTUP_HEARTBEAT_GRACE_SEC:
                    log(
                        f"新 daemon PID {pid} 尚无本进程心跳，处于"
                        f"{STARTUP_HEARTBEAT_GRACE_SEC // 60}分钟启动宽限内，暂不误杀。"
                    )
                    time.sleep(CHECK_INTERVAL)
                    continue
                stale_desc = restart_reason
                log(f"{stale_desc}，判定假死，重启 daemon。")
                notify("⚠️ daemon 假死，守护器强制重启",
                       f"{stale_desc}，守护器将重启守护进程。",
                       level="timeSensitive")
                # 不能调用 stop_windows.py：它会先停止 keeper 自己，导致执行不到后续拉起。
                # start_windows.py --no-tail 会只清理旧 daemon/D 子进程，保留当前 keeper。
                was_down = True
                consecutive_restarts += 1
                if start_daemon():
                    last_start_ts = time.time()
                next_restart_not_before = time.time() + CHECK_INTERVAL
                time.sleep(CHECK_INTERVAL)
                continue
            if not heartbeat_same_process:
                log(
                    "心跳PID本轮未能确认（heartbeat_pid=%s，当前pid=%s，"
                    "连续%d/%d次）；先观察，不重启。"
                    % (
                        heartbeat_pid,
                        pid,
                        heartbeat_pid_mismatch_count,
                        HEARTBEAT_PID_MISMATCH_CONFIRMATIONS,
                    )
                )
                time.sleep(CHECK_INTERVAL)
                continue

            # daemon进程已稳定存活，清空进程级崩溃计数。
            if last_start_ts and (time.time() - last_start_ts) >= MIN_ALIVE_SEC:
                consecutive_restarts = 0
                next_restart_not_before = 0.0
                crash_loop_alerted = False
                last_start_ts = 0.0

            # ── 3. 进程心跳恢复 → 只通知进程层，不猜测QMT/交易状态 ──
            if was_down and process_heartbeat_ready(
                heartbeat_age=age,
                heartbeat_same_process=heartbeat_same_process,
            ):
                recovery_sent = notify(
                    "✅ daemon进程心跳已恢复",
                    "keeper已确认新daemon PID存活且原子心跳正常。"
                    "这不代表QMT连接或交易恢复已通过；请以daemon自己的"
                    "“程序与账户已恢复正常”通知为准。",
                    level="timeSensitive",
                    event="system_error",
                )
                if not recovery_sent:
                    time.sleep(CHECK_INTERVAL)
                    continue
                was_down = False

            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            log("收到中断，守护器退出（daemon 继续运行）。")
            KEEPER_PID_FILE.unlink(missing_ok=True)
            return
        except Exception as exc:  # noqa: BLE001
            log(f"守护循环异常（继续守护）：{exc}")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
