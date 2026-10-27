"""QMT 原生版的启停：用明确的 Windows 进程指令关闭 / 启动 QMT 客户端（XtItClient.exe）。

A_System 运行在 QMT 内置 Python 里；券商限制其读写文件和联网后，QMT 外的程序没有合规的通道给
策略发命令。因此 stop = 关闭 QMT（A_SYSTEM 和全部自动交易随之停止），start = 启动 QMT
（A_SYSTEM 已设为随 QMT 自动运行，登录后开始执行）。只用 tasklist / taskkill / 启动程序 /
按窗口标题判断是否已登录，不识别屏幕图像、不输入密码、不下单。
"""
from __future__ import annotations

import csv
import subprocess
import time
from pathlib import Path
from typing import Callable

DEFAULT_EXE = r"C:\QMT\GJQMT\bin.x64\XtItClient.exe"
MAIN_TITLE = "QMT交易端"   # 登录后的主窗口标题：“<资金账号> - 国金证券QMT交易端 <版本>”


def _run(command: list[str], timeout: float = 60) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None


class ProcessControl:
    def __init__(self, exe: str | Path = DEFAULT_EXE, sleep: Callable[[float], None] = time.sleep):
        self.exe = Path(exe)
        self.sleep = sleep

    def pids(self) -> list[int] | None:
        """XtItClient.exe 的进程号；查询失败时返回 None。"""
        done = _run(["tasklist", "/FI", "IMAGENAME eq XtItClient.exe", "/FO", "CSV", "/NH"])
        if done is None or done.returncode != 0:
            return None
        pids = []
        for row in csv.reader(done.stdout.splitlines()):
            if len(row) >= 2 and row[0].lower() == "xtitclient.exe" and row[1].isdigit():
                pids.append(int(row[1]))
        return pids

    def main_window(self) -> bool:
        """QMT 是否已登录：有标题含“QMT交易端”的可见主窗口（Win32 EnumWindows，按标题判断）。"""
        import ctypes
        from ctypes import wintypes

        pids = set(self.pids() or [])
        user32 = ctypes.windll.user32
        found = []
        pid = wintypes.DWORD()

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pids:
                    n = user32.GetWindowTextLengthW(hwnd)
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    if MAIN_TITLE in buf.value:
                        found.append(hwnd)
            return True
        user32.EnumWindows(callback, 0)
        return bool(found)

    def close(self, graceful_seconds: float = 30) -> tuple[bool, str]:
        """先 taskkill（发关闭消息，让 QMT 正常停止策略并退出），超时再 taskkill /F。"""
        pids = self.pids()
        if pids is None:
            return False, "无法查询 QMT 进程（tasklist 失败）"
        if not pids:
            return True, "QMT 本来就没有运行"
        for pid in pids:
            _run(["taskkill", "/PID", str(pid)])
        waited = 0.0
        while waited < graceful_seconds:
            self.sleep(2)
            waited += 2
            if not self.pids():
                return True, "QMT 已正常关闭"
        for pid in self.pids() or []:
            _run(["taskkill", "/F", "/PID", str(pid)])
        for _ in range(10):
            self.sleep(1)
            if not self.pids():
                return True, "QMT %d 秒内没有自行退出，已强制结束" % graceful_seconds
        return False, "无法结束 QMT 进程，请在任务管理器里结束 XtItClient"

    def launch(self) -> tuple[bool, str]:
        if not self.exe.is_file():
            return False, "找不到 QMT 程序：%s" % self.exe
        try:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen([str(self.exe)], cwd=str(self.exe.parent), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=flags)
        except OSError as exc:
            return False, "启动 QMT 失败：%s" % exc
        return True, "已启动 QMT：%s" % self.exe
