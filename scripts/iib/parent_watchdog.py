"""父进程看门狗。

桌面版由 tauri 壳以 `--parent_pid <pid>` 启动；父进程一旦消失（正常关闭、强杀、崩溃），
sidecar 就自己退出，避免变成孤儿进程继续占端口、占着数据库。

用的是父进程的**句柄**而不是 pid，所以不存在 pid 被复用导致的误判。
"""
import ctypes
import os
import sys
import threading
import time

from scripts.iib.timeline import tlog


def watch_parent(parent_pid: int) -> None:
    if not parent_pid or parent_pid <= 0:
        return
    threading.Thread(
        target=_watch,
        args=(parent_pid,),
        name="iib-parent-watchdog",
        daemon=True,
    ).start()


def _watch(parent_pid: int) -> None:
    if sys.platform == "win32":
        _wait_windows(parent_pid)
    else:
        _wait_posix(parent_pid)
    tlog("parent_gone", parent_pid=parent_pid)
    print(f"parent process {parent_pid} is gone, exiting", flush=True)
    os._exit(0)


def _wait_windows(pid: int) -> None:
    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        # 拿不到句柄（比如进程已经没了）——直接当作父进程已退出
        return
    try:
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject(handle, INFINITE)
    finally:
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(handle)


def _wait_posix(pid: int) -> None:
    while True:
        if os.getppid() != pid:
            return
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(1)
