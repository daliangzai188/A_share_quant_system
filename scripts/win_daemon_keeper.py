# -*- coding: utf-8 -*-
"""Windows 侧 daemon 守护器：进程级自愈 + 状态变化通知（2026-07-27 用户要求）。

事故背景：券商周末维护导致 QMT 登不上，daemon 门禁从周六卡到周一 10:28 共 13756 轮，
把套接字资源耗尽（WinError 10055），QMT 恢复后程序反而连不上，开仓窗口全部空过。
当天实测 stop→start（未重启虚拟机）即恢复，证明资源由进程持有、进程退出即释放。

本脚本解决三件事（用户明确要求）：
  1. 券商维护/连接中断 → 及时通知；
  2. 不间断重启：daemon 意外退出（含资源耗尽自退）→ 自动拉起，维护结束后无人值守恢复；
  3. 账户与程序恢复正常 → 再通知一次，明确"可以不用管了"。

用法（代替直接跑 start_windows.py）：
    py -3.11 scripts/win_daemon_keeper.py
Ctrl+C 停止 keeper（不会停 daemon）；要完全停止请先 Ctrl+C 再 py -3.11 stop_windows.py。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PID_FILE = PROJECT_ROOT / ".daemon_pid"
HEARTBEAT = PROJECT_ROOT / "logs" / "daemon_heartbeat.txt"
START_SCRIPT = PROJECT_ROOT / "start_windows.py"
BEIJING = timezone(timedelta(hours=8))

CHECK_INTERVAL = 30          # 检查间隔（秒）
STALE_LIMIT = 15 * 60        # 心跳陈旧阈值：超过视为假死，重启
BLOCKED_ALERT_AFTER = 10 * 60  # qmt_blocked 持续多久后告警（券商维护通知）
EXIT_CODE_SOCKET_EXHAUSTED = 87


def now() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"{now()} | [keeper] {msg}", flush=True)


def notify(title: str, body: str, level: str = "active") -> None:
    """走项目自带的 Bark 通道；失败不影响守护主逻辑。"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.notify import notify as _n
        _n("system_error", title, body, level=level)
        log(f"已推送：{title}")
    except Exception as exc:  # noqa: BLE001
        log(f"推送失败（不影响守护）：{exc}")


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


def heartbeat_state() -> tuple[str, float]:
    """返回 (状态标记, 心跳年龄秒)。读不到时返回 ('', 极大值)。"""
    try:
        text = HEARTBEAT.read_text(encoding="utf-8").strip()
        age = time.time() - HEARTBEAT.stat().st_mtime
        state = text.split()[-1] if text else ""
        return state, age
    except Exception:
        return "", float("inf")


def start_daemon() -> None:
    log("拉起 daemon ...")
    subprocess.run([sys.executable, str(START_SCRIPT), "--no-tail"],
                   cwd=str(PROJECT_ROOT), check=False)


def main() -> None:
    log("守护器启动：每%d秒检查一次；daemon 退出/假死自动拉起。" % CHECK_INTERVAL)
    blocked_since: float | None = None
    alerted_blocked = False
    was_down = False

    while True:
        try:
            pid = read_pid()
            alive = bool(pid and pid_alive(pid))
            state, age = heartbeat_state()

            # ── 1. 进程不在 → 拉起（含资源耗尽自退、崩溃、被误杀）──
            if not alive:
                log(f"daemon 不在运行（pid={pid}），准备拉起。")
                if not was_down:
                    was_down = True
                    notify("🔄 daemon 已退出，守护器正在拉起",
                           "检测到守护进程不在运行（可能是资源耗尽自愈退出或异常崩溃），"
                           "守护器正在启动全新进程。恢复后会再次通知。",
                           level="timeSensitive")
                start_daemon()
                time.sleep(CHECK_INTERVAL)
                continue

            # ── 2. 心跳陈旧 → 假死，强制重启 ──
            if age > STALE_LIMIT:
                log(f"心跳陈旧 {age/60:.1f} 分钟，判定假死，重启 daemon。")
                notify("⚠️ daemon 假死，守护器强制重启",
                       f"心跳已 {age/60:.0f} 分钟未更新，守护器将重启守护进程。",
                       level="timeSensitive")
                subprocess.run([sys.executable, str(PROJECT_ROOT / "stop_windows.py")],
                               cwd=str(PROJECT_ROOT), check=False)
                time.sleep(3)
                start_daemon()
                was_down = True
                time.sleep(CHECK_INTERVAL)
                continue

            # ── 3. QMT 长时间连不上 → 通知（券商维护/终端未登录）──
            if state == "qmt_blocked":
                if blocked_since is None:
                    blocked_since = time.time()
                elif (not alerted_blocked) and time.time() - blocked_since >= BLOCKED_ALERT_AFTER:
                    alerted_blocked = True
                    notify("⚠️ QMT 持续连不上，交易任务暂停",
                           f"daemon 已阻塞 {BLOCKED_ALERT_AFTER//60} 分钟以上（常见原因：券商周末维护、"
                           f"QMT 终端未登录）。程序会持续重试，恢复后自动接管并通知。"
                           f"若临近持仓到期仍未恢复，请用手机App手动平仓。",
                           level="timeSensitive")
            else:
                # ── 4. 从阻塞/宕机恢复 → 通知一次 ──
                if alerted_blocked or was_down:
                    notify("✅ 程序与账户已恢复正常",
                           f"daemon 运行中，QMT 连接与账户状态正常（心跳状态：{state or '正常'}）。"
                           f"交易任务已恢复调度，无需人工干预。",
                           level="timeSensitive")
                blocked_since = None
                alerted_blocked = False
                was_down = False

            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            log("收到中断，守护器退出（daemon 继续运行）。")
            return
        except Exception as exc:  # noqa: BLE001
            log(f"守护循环异常（继续守护）：{exc}")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
