"""把文件送入 Windows 系统回收站（不引入任何第三方依赖）。

实现方式：ctypes 调用 ``shell32!SHFileOperationW``，参数为
``FO_DELETE + FOF_ALLOWUNDO``，文件进入当前用户的真实回收站
（``C:\\$RECYCLE.BIN\\...``），用户可以在系统回收站里还原。

设计约束：

- 只支持 Windows；非 Windows 平台 :func:`is_supported` 返回 False，
  调用方（``api.delete_files``）负责降级为永久删除，保持原有行为不变。
- ``SHFileOperationW`` 不支持超过 ``MAX_PATH``(260) 的路径，这里提前检查并抛出
  明确的 ``OSError``，避免系统返回含义不清的错误码。
- 回收站被禁用、目标在网络共享/可移动磁盘上等特殊场景下，Windows 可能直接
  永久删除或操作失败。本模块不会吞掉错误：失败一律抛 ``OSError``，由调用方
  决定提示用户（宁可不删，也不静默永久删除）。
- 使用 ``FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT``，删除过程不弹系统
  确认框：确认交互由 IIB 前端负责。
"""

from __future__ import annotations

import os
import subprocess
from typing import Iterable, List

IS_WINDOWS = os.name == "nt"

# shell32 SHFileOperation 的常量（Win32 API）
_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400

MAX_PATH = 260

if IS_WINDOWS:  # pragma: no cover - 仅在 Windows 上编译
    import ctypes

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        """Win32 ``SHFILEOPSTRUCTW`` 结构体（x64 下按 void* 对齐）。"""

        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    _shell32 = ctypes.windll.shell32
    _shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    _shell32.SHFileOperationW.restype = ctypes.c_int


def is_supported() -> bool:
    """当前平台是否支持系统回收站。"""
    return IS_WINDOWS


def _describe_error(code: int) -> str:
    """把 SHFileOperationW 的返回码翻译成可读文本。"""
    message = ""
    try:
        import ctypes

        message = ctypes.FormatError(code).strip()
    except Exception:
        message = ""
    if message:
        return f"SHFileOperation failed with code {code} ({message})"
    return f"SHFileOperation failed with code {code}"


def send_to_recycle_bin(paths: Iterable[str]) -> None:
    """把一批路径送入系统回收站。

    Args:
        paths: 待删除的文件或空目录绝对/相对路径。不存在的路径视为错误。

    Raises:
        OSError: 平台不支持、路径不存在/过长、系统调用返回非 0 或操作被中止。
            注意：抛出异常时可能有部分路径已被移入回收站（与原 ``os.remove``
            的逐条删除语义一致，调用方按文件粒度处理错误即可）。
    """
    if not IS_WINDOWS:
        raise OSError("System recycle bin is only supported on Windows")

    targets: List[str] = []
    for raw in paths:
        if not raw:
            continue
        path = os.path.abspath(os.fspath(raw))
        if not os.path.exists(path):
            raise OSError(f"Path does not exist: {path}")
        if len(path) >= MAX_PATH:
            raise OSError(
                f"Path is too long for the system recycle bin "
                f"(>= {MAX_PATH} chars): {path}"
            )
        targets.append(path)

    if not targets:
        return

    # pFrom 需要 "path1\0path2\0\0" 的双 '\0' 结尾格式；buffer 必须活到调用结束。
    buffer = ctypes.create_unicode_buffer("\0".join(targets) + "\0\0")
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = _FO_DELETE
    op.pFrom = ctypes.cast(buffer, ctypes.c_wchar_p)
    op.pTo = None
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_NOERRORUI | _FOF_SILENT

    result = _shell32.SHFileOperationW(ctypes.byref(op))
    if op.fAnyOperationsAborted:
        raise OSError("Recycle bin operation was aborted")
    if result != 0:
        raise OSError(_describe_error(result))


def open_recycle_bin() -> None:
    """打开系统回收站窗口（本地桌面场景）。"""
    if not IS_WINDOWS:
        raise OSError("System recycle bin is only supported on Windows")
    try:
        subprocess.Popen(["explorer.exe", "shell:RecycleBinFolder"])
    except OSError as e:  # pragma: no cover - 极少数环境无 explorer
        raise OSError(f"Failed to open system recycle bin: {e}") from e
