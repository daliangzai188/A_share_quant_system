"""Prevent Windows idle sleep while allowing display-off and screen locking."""
import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import threading

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
EXECUTION_STATE = ES_CONTINUOUS | ES_SYSTEM_REQUIRED


def main():
    if os.name != 'nt':
        raise RuntimeError('Windows only')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_bool
    handle = kernel.CreateMutexW(None, False, 'Local\\A_System_SessionAwakeGuard')
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return
    kernel.SetThreadExecutionState.argtypes = [ctypes.c_uint]
    kernel.SetThreadExecutionState.restype = ctypes.c_uint
    output = Path(__file__).resolve().with_name('session_awake_status.json')
    wait = threading.Event()
    try:
        while True:
            value = kernel.SetThreadExecutionState(EXECUTION_STATE)
            result = {
                'status': 'AWAKE' if value else 'FAILED',
                'time': datetime.now().astimezone().isoformat(),
                'pid': os.getpid(),
                'api_return_nonzero': bool(value),
                'execution_state': '0x80000001',
                'system_sleep_prevented': bool(value),
                'display_sleep_allowed': True,
                'screen_lock_allowed_by_guard': True,
                'authentication_or_update_policy_changed': False,
            }
            temp = output.with_suffix('.tmp')
            temp.write_text(json.dumps(result, indent=2), encoding='utf-8')
            temp.replace(output)
            if not value:
                raise RuntimeError('SetThreadExecutionState failed')
            wait.wait(30)
    finally:
        kernel.SetThreadExecutionState(ES_CONTINUOUS)
        kernel.CloseHandle(handle)


if __name__ == '__main__':
    main()
