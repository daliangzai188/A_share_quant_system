# -*- coding: utf-8 -*-
"""只读查看 QMT 客户端进程现场：进程启动时间和可见窗口标题。

内置桥接心跳过期时，用它区分四种情况，并告诉用户该做什么：
1. 客户端进程不存在；
2. 客户端停在登录窗口（自动登录遇到验证码等），必须人工登录；
3. 客户端在最后一次账户验证成功之后重新启动过，内置模型尚未恢复；
4. 客户端一直在运行，但内置模型没有心跳。

只调用 Win32 查询接口（CreateToolhelp32Snapshot / GetProcessTimes / EnumWindows），
毫秒级完成；不启动 PowerShell、不连接券商、不点击或输入，也不改变任何进程和窗口。
非 Windows 返回 None。
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
import re
from typing import Any

BEIJING_TZ = _dt.timezone(_dt.timedelta(hours=8))
CLIENT_EXE_NAMES = ("xtitclient.exe",)
LOGIN_TITLE_KEYWORDS = ("登录", "登陆", "验证码", "login")
_EPOCH_AS_FILETIME = 116444736000000000
_LONG_DIGITS = re.compile(r"\d{6,}")


def mask_title(title: str) -> str:
    """窗口标题可能带资金账号：连续6位以上数字只保留末两位。"""
    return _LONG_DIGITS.sub(lambda m: "***" + m.group(0)[-2:], str(title or ""))


def _filetime_to_datetime(value: int) -> _dt.datetime | None:
    if value <= _EPOCH_AS_FILETIME:
        return None
    seconds = (value - _EPOCH_AS_FILETIME) / 10_000_000
    return _dt.datetime.fromtimestamp(seconds, tz=BEIJING_TZ)


def list_qmt_client_processes(exe_names: tuple[str, ...] = CLIENT_EXE_NAMES) -> list[dict[str, Any]] | None:
    """返回客户端进程列表：pid、启动时间（北京时间）、可见窗口标题（已脱敏）。"""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    wanted = {name.lower() for name in exe_names}
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    processes: dict[int, dict[str, Any]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in wanted:
                processes[int(entry.th32ProcessID)] = {
                    "pid": int(entry.th32ProcessID),
                    "exe": entry.szExeFile,
                    "started_at": None,
                    "window_titles": [],
                }
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    for pid, info in processes.items():
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                info["started_at"] = _filetime_to_datetime(created)
        finally:
            kernel32.CloseHandle(handle)

    if processes:
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = (enum_proc, wintypes.LPARAM)
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)

        def _collect(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            info = processes.get(int(owner.value))
            if info is not None and user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buffer, length + 1)
                    if buffer.value:
                        info["window_titles"].append(mask_title(buffer.value))
            return True

        user32.EnumWindows(enum_proc(_collect), 0)
    return sorted(processes.values(), key=lambda item: item["pid"])


def classify_qmt_client_state(
    processes: list[dict[str, Any]] | None,
    *,
    last_verified_at: _dt.datetime | None,
) -> dict[str, str]:
    """把进程现场归类成用户能直接执行的结论。纯函数，便于离线测试。"""
    if processes is None:
        return {
            "state": "unknown",
            "summary": "当前系统无法查看QMT客户端进程",
            "action": "请核对客户端登录和A_SYSTEM_QMT_INNER运行状态。",
        }
    if not processes:
        return {
            "state": "client_missing",
            "summary": "QMT客户端进程不存在（已退出）",
            "action": "请重新打开QMT客户端并登录；登录后内置模型会自动运行。",
        }
    floor = _dt.datetime.min.replace(tzinfo=BEIJING_TZ)
    newest = max(processes, key=lambda item: item.get("started_at") or floor)
    started = newest.get("started_at")
    started_text = started.strftime("%m-%d %H:%M:%S") if started else "未知时间"
    titles = [title for item in processes for title in item.get("window_titles", [])]
    if any(keyword in title.lower() for title in titles for keyword in LOGIN_TITLE_KEYWORDS):
        return {
            "state": "client_login_pending",
            "summary": f"QMT客户端（启动于{started_text}）停在登录窗口",
            "action": "需要你手动登录QMT（可能要输入验证码）；登录后内置模型自动运行，系统自动恢复。",
        }
    if started is not None and last_verified_at is not None and started > last_verified_at:
        return {
            "state": "client_restarted",
            "summary": f"QMT客户端在{started_text}重新启动过，内置模型尚未恢复心跳",
            "action": "正常情况1分钟内自动登录并运行模型；若持续不恢复，请看客户端是否停在登录页。",
        }
    if last_verified_at is None:
        return {
            "state": "client_running_unverified",
            "summary": f"QMT客户端在运行（启动于{started_text}），内置模型没有心跳",
            "action": "请看客户端是否已登录，并在“模型交易”里确认A_SYSTEM_QMT_INNER在运行。",
        }
    return {
        "state": "model_not_running",
        "summary": f"QMT客户端一直在运行（启动于{started_text}），但内置模型没有心跳",
        "action": "请在QMT“模型交易”里确认A_SYSTEM_QMT_INNER在运行，必要时重新运行。",
    }


def describe_processes(processes: list[dict[str, Any]] | None) -> str:
    """日志用的一行现场：pid、启动时间、窗口标题。"""
    if processes is None:
        return "非Windows，未查询"
    if not processes:
        return "无客户端进程"
    parts = []
    for item in processes:
        started = item.get("started_at")
        started_text = started.strftime("%Y-%m-%d %H:%M:%S") if started else "未知"
        titles = "、".join(item.get("window_titles") or []) or "无可见窗口"
        parts.append(f"pid={item.get('pid')} 启动={started_text} 窗口=[{titles}]")
    return "；".join(parts)


# ---------------------------------------------------------------- 客户端定时重启设置

# 属性值里允许出现“>”（XML合法）；按引号跳过值，避免把元素截断丢掉后面的属性。
_TRADE_SETTING = re.compile(r'<TradeSetting\b(?:[^>"]|"[^"]*")*>', re.S)
_ATTR = re.compile(r'\b(restart|restarttimelist)="([^"]*)"')


def parse_scheduled_restart(text: str) -> dict[str, Any]:
    """从一份Config.xml文本里读出全部TradeSetting的定时重启设置。"""

    elements = [dict(_ATTR.findall(match.group(0))) for match in _TRADE_SETTING.finditer(text)]
    values = [attrs["restart"] for attrs in elements if "restart" in attrs]
    if "1" in values:
        restart = "1"
    elif "0" in values:
        restart = "0"
    else:
        restart = ""
    times = next((attrs["restarttimelist"] for attrs in elements if attrs.get("restarttimelist")), "")
    return {
        "restart": restart,
        "restarttimelist": times,
        "trade_setting_count": len(elements),
        "with_restart_count": len(values),
    }


def read_scheduled_restart_settings(qmt_root: Path, account_id: str = "") -> list[dict[str, Any]]:
    """读取资金账号Config.xml里的客户端定时重启设置（只读）。

    2026-10-03查明：QMT每天按TradeSetting的restarttimelist整进程重启并自动重新登录；
    自动登录失败（节假日券商服务器不可用、需要验证码）时停在登录框，内置模型不再运行，
    桥接一直断到人工登录。restart="0"才是关闭；没写这一项时按模板默认（开启）处理。
    给出account_id时只核对实盘正在使用的账号目录。
    """
    users = Path(qmt_root) / "userdata" / "users"
    paths = [users / str(account_id) / "Config.xml"] if account_id else sorted(users.glob("*/Config.xml"))
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            rows.append({"account": mask_title(path.parent.name), "restart": "", "restarttimelist": "",
                         "trade_setting_count": 0, "with_restart_count": 0, "missing_file": True})
            continue
        parsed = parse_scheduled_restart(path.read_text(encoding="utf-8", errors="replace"))
        rows.append({"account": mask_title(path.parent.name), **parsed})
    return rows


def scheduled_restart_problem(rows: list[dict[str, Any]]) -> str:
    """返回需要报警的说明；全部明确关闭时返回空串。纯函数，便于离线测试。"""

    if not rows:
        return "没有找到QMT资金账号配置（userdata/users/*/Config.xml），无法确认定时重启已关闭"
    problems = []
    for row in rows:
        if row.get("missing_file"):
            problems.append(f"账号{row['account']}的Config.xml不存在，无法确认定时重启已关闭")
            continue
        if row["restart"] == "0":
            continue
        times = "、".join(
            f"{value[:2]}:{value[2:4]}:{value[4:6]}" for value in row["restarttimelist"].split("|") if len(value) >= 6
        ) or "默认时间"
        if row["restart"] == "1":
            problems.append(f"账号{row['account']}定时重启已开启，每天{times}整进程重启")
        else:
            problems.append(
                f"账号{row['account']}定时重启未明确关闭（按默认开启），每天{times}整进程重启"
                f"（找到{row.get('trade_setting_count', 0)}个TradeSetting，"
                f"其中{row.get('with_restart_count', 0)}个写了restart）"
            )
    return "；".join(problems)
