"""One writer per spreadsheet on a Windows computer (including both EXEs)."""
from __future__ import annotations

import ctypes
import hashlib
import os
import threading
from contextlib import contextmanager

_locks = {}
_guard = threading.Lock()


@contextmanager
def sheet_mutex(spreadsheet_id, timeout=30):
    """Hold through read/plan/write/readback. Cross-computer writes need rereads."""
    key = hashlib.sha256(str(spreadsheet_id).encode()).hexdigest()
    if os.name != "nt":
        with _guard:
            lock = _locks.setdefault(key, threading.RLock())
        if not lock.acquire(timeout=timeout):
            raise TimeoutError("另一個程序正在更新此刀表，稍後重試。")
        try:
            yield
        finally:
            lock.release()
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateMutexW(None, False, "Local\\VGHKS-Sheet-" + key)
    if not handle:
        raise OSError(ctypes.get_last_error(), "無法建立刀表更新鎖。")
    acquired = False
    try:
        status = kernel.WaitForSingleObject(handle, min(max(int(timeout * 1000), 0), 0xFFFFFFFE))
        acquired = status in {0, 0x80}  # WAIT_ABANDONED is acquired after a crashed writer.
        if not acquired:
            if status == 0x102:
                raise TimeoutError("另一個程序正在更新此刀表，稍後重試。")
            raise OSError(ctypes.get_last_error(), "無法取得刀表更新鎖。")
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)
