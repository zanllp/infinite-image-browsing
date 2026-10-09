"""启动时间线日志，用于分析冷启动各阶段耗时。

输出走 stdout，桌面版由 tauri 壳加上带毫秒的时间戳写进 iib_api_server.log，
独立运行时直接看控制台即可。设置 IIB_TIMELINE_LOG=0 可关闭。

注意：Nuitka / PyInstaller 的 onefile 自解压发生在 Python 启动之前，
所以本模块导入时刻可以近似视为「解压完成、Python 开始跑」。
"""
import os
import time

_T0 = time.time()


def elapsed() -> float:
    return time.time() - _T0


def tlog(event: str, **kwargs) -> None:
    if os.environ.get("IIB_TIMELINE_LOG", "1") == "0":
        return
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
    line = f"[TIMELINE] +{elapsed():.3f}s pid={os.getpid()} event={event}"
    if extra:
        line += f" {extra}"
    print(line, flush=True)
