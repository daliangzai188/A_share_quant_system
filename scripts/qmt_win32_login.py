# -*- coding: utf-8 -*-
"""国金 QMT Qt 登录后端。密码输入前先用非秘密字符检查掩码。

不保存截图，不输入验证码，不按脱机按钮；不在不确定布局下尝试输入。
"""
from __future__ import annotations

import base64
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import time


def password_is_masked(pixels, width, height):
    """非秘密字符应显示为实心圆点；空框、明文 0 和文字拒绝。"""
    points = []
    for y in range(height):
        for x in range(width):
            b, g, r, _ = pixels[(y * width + x) * 4:(y * width + x + 1) * 4]
            if max(b, g, r) < 190 and max(b, g, r) - min(b, g, r) < 25:
                points.append((x, y))
    if not points:
        return False
    x0, x1 = min(x for x, y in points), max(x for x, y in points)
    y0, y1 = min(y for x, y in points), max(y for x, y in points)
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    center = ((x0 + x1) // 2, (y0 + y1) // 2)
    return bool(bw >= 3 and bh >= 3 and .65 <= bw / bh <= 1.5
                and len(points) / (bw * bh) >= .62 and center in points)


def same_field(before, after):
    """容忍插入光标闪烁；输入到错误字段的文字变化仍会拒绝。"""
    if not before or len(before) != len(after):
        return False
    changed = sum(max(abs(before[i + j] - after[i + j]) for j in range(3)) > 40
                  for i in range(0, len(before), 4))
    return changed / (len(before) // 4) < .01


def decrypt_credentials(path):
    class Blob(C.Structure):
        _fields_ = [("size", W.DWORD), ("data", C.POINTER(C.c_ubyte))]
    crypt = C.WinDLL("crypt32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    crypt.CryptUnprotectData.argtypes = (C.POINTER(Blob), C.c_void_p, C.c_void_p,
                                        C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(Blob))
    kernel.LocalFree.argtypes = (C.c_void_p,)
    raw = base64.b64decode(json.loads(Path(path).read_text(encoding="utf-8"))["credentials_dpapi_b64"], validate=True)
    storage = (C.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, destination = Blob(len(raw), storage), Blob()
    if not crypt.CryptUnprotectData(C.byref(source), None, None, None, None, 1, C.byref(destination)):
        raise RuntimeError("CREDENTIAL_DECRYPT_FAILED")
    try:
        result = json.loads(C.string_at(destination.data, destination.size).decode("utf-8"))
        if not result.get("account", "").isdigit() or not result.get("password"):
            raise ValueError("CREDENTIAL_FIELDS_MISSING")
        return result
    finally:
        C.memset(destination.data, 0, destination.size)
        kernel.LocalFree(destination.data)


class NativeLogin:
    def __init__(self, hwnd, pid, exe):
        self.hwnd, self.pid, self.exe = int(hwnd), int(pid), str(exe)
        self.u = C.WinDLL("user32", use_last_error=True)
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.g = C.WinDLL("gdi32", use_last_error=True)
        self.u.GetForegroundWindow.restype = W.HWND
        self.u.GetWindowThreadProcessId.argtypes = (W.HWND, C.POINTER(W.DWORD))
        self.u.GetWindowLongW.argtypes = (W.HWND, C.c_int)
        self.u.IsWindowVisible.argtypes = (W.HWND,)
        self.u.GetWindowRect.argtypes = (W.HWND, C.POINTER(W.RECT))
        self.u.SetForegroundWindow.argtypes = (W.HWND,)
        self.u.ShowWindow.argtypes = (W.HWND, C.c_int)
        self.u.GetAncestor.argtypes = (W.HWND, W.UINT)
        self.u.GetAncestor.restype = W.HWND
        self.u.WindowFromPoint.argtypes = (W.POINT,)
        self.u.WindowFromPoint.restype = W.HWND
        self.k.OpenProcess.argtypes = (W.DWORD, W.BOOL, W.DWORD)
        self.k.OpenProcess.restype = W.HANDLE
        self.k.QueryFullProcessImageNameW.argtypes = (W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD))
        self.k.CloseHandle.argtypes = (W.HANDLE,)
        self.u.OpenInputDesktop.argtypes = (W.DWORD, W.BOOL, W.DWORD)
        self.u.OpenInputDesktop.restype = W.HANDLE
        self.u.GetUserObjectInformationW.argtypes = (W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD))
        self.u.CloseDesktop.argtypes = (W.HANDLE,)
        self.u.GetDC.argtypes = (W.HWND,)
        self.u.GetDC.restype = W.HDC
        self.u.ReleaseDC.argtypes = (W.HWND, W.HDC)
        self.g.CreateCompatibleDC.argtypes = (W.HDC,)
        self.g.CreateCompatibleDC.restype = W.HDC
        self.g.CreateCompatibleBitmap.argtypes = (W.HDC, C.c_int, C.c_int)
        self.g.CreateCompatibleBitmap.restype = W.HBITMAP
        self.g.SelectObject.argtypes = (W.HDC, W.HANDLE)
        self.g.SelectObject.restype = W.HANDLE
        self.g.BitBlt.argtypes = (W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, W.HDC, C.c_int, C.c_int, W.DWORD)
        self.g.GetDIBits.argtypes = (W.HDC, W.HBITMAP, W.UINT, W.UINT, C.c_void_p, C.c_void_p, W.UINT)
        self.g.DeleteObject.argtypes = (W.HANDLE,)
        self.g.DeleteDC.argtypes = (W.HDC,)
        self.u.SetProcessDPIAware()

    def verify(self, foreground=True):
        owner = W.DWORD()
        self.u.GetWindowThreadProcessId(self.hwnd, C.byref(owner))
        if owner.value != self.pid or not self.u.IsWindowVisible(self.hwnd):
            raise RuntimeError("WINDOW_IDENTITY_CHANGED")
        process = self.k.OpenProcess(0x1000, False, self.pid)
        if not process:
            raise RuntimeError("PROCESS_IDENTITY_UNREADABLE")
        try:
            name, size = C.create_unicode_buffer(32768), W.DWORD(32768)
            if not self.k.QueryFullProcessImageNameW(process, 0, name, C.byref(size)):
                raise RuntimeError("PROCESS_PATH_UNREADABLE")
            if name.value.casefold() != self.exe.casefold():
                raise RuntimeError("PROCESS_PATH_CHANGED")
        finally:
            self.k.CloseHandle(process)
        if self.u.GetWindowLongW(self.hwnd, -16) & 0x00cf0000:
            raise RuntimeError("NOT_A_LOGIN_WINDOW")
        if foreground and self.u.GetForegroundWindow() != self.hwnd:
            raise RuntimeError("LOGIN_WINDOW_NOT_FOREGROUND")
        desktop = self.u.OpenInputDesktop(0, False, 1)
        if not desktop:
            raise RuntimeError("DESKTOP_LOCKED")
        try:
            name, needed = C.create_unicode_buffer(256), W.DWORD()
            if not self.u.GetUserObjectInformationW(desktop, 2, name, C.sizeof(name), C.byref(needed)) or name.value != "Default":
                raise RuntimeError("DESKTOP_LOCKED")
        finally:
            self.u.CloseDesktop(desktop)

    def capture(self, x, y, width, height):
        self.verify()
        class Header(C.Structure):
            _fields_ = [("size", W.DWORD), ("width", W.LONG), ("height", W.LONG),
                        ("planes", W.WORD), ("bits", W.WORD), ("compression", W.DWORD),
                        ("image_size", W.DWORD), ("xppm", W.LONG), ("yppm", W.LONG),
                        ("used", W.DWORD), ("important", W.DWORD)]
        screen = self.u.GetDC(None)
        dc = self.g.CreateCompatibleDC(screen)
        bitmap = self.g.CreateCompatibleBitmap(screen, width, height)
        previous = self.g.SelectObject(dc, bitmap)
        try:
            if not self.g.BitBlt(dc, 0, 0, width, height, screen, x, y, 0x00CC0020):
                raise RuntimeError("FIELD_CAPTURE_FAILED")
            self.g.SelectObject(dc, previous)
            header = Header(C.sizeof(Header), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
            pixels = C.create_string_buffer(width * height * 4)
            if self.g.GetDIBits(dc, bitmap, 0, height, pixels, C.byref(header), 0) != height:
                raise RuntimeError("FIELD_PIXELS_FAILED")
            return pixels.raw
        finally:
            self.g.SelectObject(dc, previous)
            self.g.DeleteObject(bitmap)
            self.g.DeleteDC(dc)
            self.u.ReleaseDC(None, screen)

    def key(self, vk):
        self.verify()
        self.u.keybd_event(vk, 0, 0, 0)
        self.u.keybd_event(vk, 0, 2, 0)

    def clear(self):
        self.verify()
        self.u.keybd_event(0x11, 0, 0, 0)
        try:
            self.key(0x41)
        finally:
            self.u.keybd_event(0x11, 0, 2, 0)
        self.key(0x2E)
        time.sleep(.15)

    def type_digits(self, value):
        if not str(value).isdigit():
            raise ValueError("ONLY_NUMERIC_CREDENTIALS_SUPPORTED")
        for char in str(value):
            self.key(ord(char))
            time.sleep(.04)
        time.sleep(.25)

    def click(self, x, y):
        self.verify()
        if self.u.GetAncestor(self.u.WindowFromPoint(W.POINT(x, y)), 2) != self.hwnd:
            raise RuntimeError("FIELD_OCCLUDED")
        self.u.SetCursorPos(x, y)
        self.u.mouse_event(2, 0, 0, 0, 0)
        self.u.mouse_event(4, 0, 0, 0, 0)
        time.sleep(.2)

    def login(self, credentials):
        if not str(credentials["account"]).isdigit() or not str(credentials["password"]).isdigit():
            raise ValueError("ONLY_NUMERIC_CREDENTIALS_SUPPORTED")
        self.verify(foreground=False)
        # 使用者正在按修饰键或近期操作电脑时，不抢占其输入。
        for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C):
            if self.u.GetAsyncKeyState(vk) & 0x8000:
                raise RuntimeError("USER_INPUT_ACTIVE")
        self.u.ShowWindow(self.hwnd, 9)
        self.u.SetForegroundWindow(self.hwnd)
        time.sleep(.4)
        self.verify()
        rect = W.RECT()
        self.u.GetWindowRect(self.hwnd, C.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if not .68 <= h / w <= .73:
            raise RuntimeError("CAPTCHA_OR_UNSUPPORTED_LAYOUT")
        # 找实际输入框的蓝色上下边框，不依赖焦点或盲按 TAB。
        x, cw = rect.left + round(.30 * w), round(.40 * w)
        top, ch = rect.top + round(.46 * h), round(.28 * h)
        image = self.capture(x, top, cw, ch)
        borders = []
        for row in range(ch):
            count = 0
            for col in range(5, cw - 5):
                b, g, r, _ = image[(row * cw + col) * 4:(row * cw + col + 1) * 4]
                if b > 180 and 70 <= g <= 185 and r < 80:
                    count += 1
            if count > .92 * (cw - 10) and (not borders or row - borders[-1] > 3):
                borders.append(row)
        if len(borders) != 4:
            raise RuntimeError("INPUT_BORDERS_UNVERIFIED")
        account_y = top + (borders[0] + borders[1]) // 2
        password_y = top + (borders[2] + borders[3]) // 2
        if not .025 * w <= borders[1] - borders[0] <= .065 * w or not .025 * w <= borders[3] - borders[2] <= .065 * w:
            raise RuntimeError("INPUT_GEOMETRY_UNVERIFIED")
        # 使用“行情+交易”；仅行情登录不能恢复内置模型。
        tab_x, tab_y = x + round(.065 * w), account_y - round(.056 * w)
        self.click(tab_x, tab_y)
        tab_pixels = self.capture(tab_x - 3, tab_y - 3, 6, 6)
        if sum(1 for i in range(0, len(tab_pixels), 4)
               if tab_pixels[i] > 180 and 70 <= tab_pixels[i + 1] <= 185 and tab_pixels[i + 2] < 80) < 30:
            raise RuntimeError("TRADING_LOGIN_TAB_UNVERIFIED")
        fx, cropw, croph = x + round(.008 * w), round(.024 * w), round(.027 * w)
        password_crop = (fx, password_y - croph // 2, cropw, croph)
        account_crop = (x + 5, account_y - croph // 2, cw - 10, croph)
        self.click(x + cw // 2, account_y)
        self.clear()
        blank = self.capture(*account_crop)
        self.type_digits(credentials["account"])
        account_pixels = self.capture(*account_crop)
        if same_field(blank, account_pixels):
            raise RuntimeError("ACCOUNT_ENTRY_FAILED")
        self.click(x + cw // 2, password_y)
        self.clear()
        # 先输入一个公开的 0，只有实心掩码且账号区没变化才接触真正密码。
        self.type_digits("0")
        masked = password_is_masked(self.capture(*password_crop), cropw, croph)
        if not masked or not same_field(self.capture(*account_crop), account_pixels):
            self.clear()
            raise RuntimeError("PASSWORD_MASK_NOT_VERIFIED")
        self.clear()
        self.type_digits(credentials["password"])
        if not same_field(self.capture(*account_crop), account_pixels):
            self.clear()
            raise RuntimeError("PASSWORD_FOCUS_CHANGED")
        self.verify()
        self.key(0x0D)
        return {"result": "login_submitted_unverified"}


def login_native(observed, exe, credential_path):
    return NativeLogin(observed["hwnd"], observed["pid"], exe).login(decrypt_credentials(credential_path))
