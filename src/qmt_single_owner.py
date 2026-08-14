"""QMT交易连接单一所有权门禁。

正式运行时只有 trading_daemon 可以持有 QMT 交易连接。任何独立诊断、预览或
策略监控入口在 daemon 存活时都必须拒绝建立第二条交易连接，避免不同 session
并发查询/下单/撤单导致账户状态抖动。
"""
from __future__ import annotations

import os
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    if int(pid or 0) <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError):
            return False

    import ctypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        int(pid),
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
            exit_code.value == 259
        )
    finally:
        kernel32.CloseHandle(handle)


def running_daemon_pid(project_root: str | Path) -> int | None:
    """返回仍存活的daemon PID；陈旧pid文件不构成阻断。"""

    pid_file = Path(project_root).absolute() / ".daemon_pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, TypeError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def assert_standalone_qmt_allowed(
    project_root: str | Path,
    *,
    caller: str,
    current_pid: int | None = None,
) -> None:
    """daemon存活时拒绝独立入口建立QMT交易连接。

    ``current_pid`` 仅供测试或daemon内部复用；同一daemon进程自身不会被阻断。
    """

    owner_pid = running_daemon_pid(project_root)
    this_pid = int(current_pid if current_pid is not None else os.getpid())
    if owner_pid is None or owner_pid == this_pid:
        return
    raise RuntimeError(
        f"QMT交易连接已由trading_daemon(PID {owner_pid})持有；"
        f"{caller}不得并发建立第二条连接。需要独立诊断时请先运行stop_windows.py。"
    )
