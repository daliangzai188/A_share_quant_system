# -*- coding: utf-8 -*-
"""只启动缺失的 Windows 实盘运行组件，不重启健康进程。

供 Windows 计划任务在“用户登录”和每天 08:15 运行。与 start_windows.py
不同，本脚本绝不会主动停止一个健康 daemon；因此定时检查不会中断 QMT 会话。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.runtime_stop_state import load_manual_stop


DAEMON_PID_FILE = PROJECT_ROOT / ".daemon_pid"
KEEPER_PID_FILE = PROJECT_ROOT / ".keeper_pid"
START_SCRIPT = PROJECT_ROOT / "start_windows.py"
KEEPER_SCRIPT = PROJECT_ROOT / "scripts" / "win_daemon_keeper.py"

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def _read_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
        return value if value > 0 else None
    except (OSError, TypeError, ValueError):
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return int(exit_code.value) == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def runtime_status() -> tuple[bool, bool, int | None, int | None]:
    daemon_pid = _read_pid(DAEMON_PID_FILE)
    keeper_pid = _read_pid(KEEPER_PID_FILE)
    return (
        _pid_alive(daemon_pid),
        _pid_alive(keeper_pid),
        daemon_pid,
        keeper_pid,
    )


def _detached_flags() -> int:
    return int(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def ensure_runtime() -> int:
    if sys.platform != "win32":
        print("本脚本只在 Windows 虚拟机内运行。")
        return 2

    manual_stop = load_manual_stop(PROJECT_ROOT)
    if manual_stop is not None:
        print(
            "A_System处于人工停机状态；登录/每日08:15运行兜底不启动daemon或keeper。"
            "如需恢复，请人工运行 start_windows.py。"
        )
        return 0

    daemon_alive, keeper_alive, daemon_pid, keeper_pid = runtime_status()
    if daemon_alive and keeper_alive:
        print(f"A_System运行正常：daemon={daemon_pid} keeper={keeper_pid}，无需重启。")
        return 0

    if daemon_alive and not keeper_alive:
        # daemon健康时绝不调用start_windows.py（它会停止旧daemon）；只补keeper。
        proc = subprocess.Popen(
            [sys.executable, str(KEEPER_SCRIPT)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_detached_flags(),
        )
        KEEPER_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
        time.sleep(1.0)
        if _pid_alive(proc.pid):
            print(f"daemon={daemon_pid}健康；已补启动keeper={proc.pid}。")
            return 0
        print("keeper补启动失败，请查看 logs\\win_daemon_keeper.log。")
        return 1

    # daemon已不在。start_windows.py会清理失效pid文件、启动daemon，并确保keeper。
    print(f"daemon未运行（记录PID={daemon_pid}），开始无人值守恢复。")
    result = subprocess.run(
        [sys.executable, str(START_SCRIPT), "--no-tail", "--automatic-recovery"],
        cwd=str(PROJECT_ROOT),
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        print(f"start_windows.py返回{result.returncode}，恢复失败。")
        return int(result.returncode or 1)
    time.sleep(1.0)
    daemon_alive, keeper_alive, daemon_pid, keeper_pid = runtime_status()
    if daemon_alive and keeper_alive:
        print(f"A_System已恢复：daemon={daemon_pid} keeper={keeper_pid}。")
        return 0
    print(
        "启动命令已返回，但运行状态尚未双就绪："
        f"daemon_alive={daemon_alive} keeper_alive={keeper_alive}。"
    )
    return 1


def main() -> int:
    try:
        return ensure_runtime()
    except subprocess.TimeoutExpired:
        print("无人值守恢复超过120秒，请检查QMT/Windows状态。")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"无人值守恢复异常：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
