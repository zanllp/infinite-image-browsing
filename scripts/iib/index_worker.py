"""后台静默维护 worker：单 daemon 线程增量更新索引 + 预热缩略图。

设计要点（见 .dev-contract.md 第 4 节）：

* 只有一个后台线程，负责"找出过期的目录 → 逐个增量扫描 → 消费缩略图预热队列"；
* CPU 用 :class:`Governor` 限速：按 ``time.thread_time()`` 计量工作单元耗时，
  按整机目标百分比（``settings.cpuLimit``）归还 CPU，并用 ``time.process_time()``
  EWMA 校正；前台有请求时额外让路；
* 所有状态都在内存里，:meth:`IndexWorker.status` 不查 DB，供前端 3s 轮询；
* 写库都是短事务、幂等操作，线程为 daemon，进程退出不会被它拖住。

对外入口：``init_worker`` / ``get_worker`` / ``IndexWorker``。
"""

from __future__ import annotations

import math
import os
import stat
import threading
import time
from contextlib import closing
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from scripts.iib.db.datamodel import DataBase, Folder, Image as DbImg, tree_bounds
from scripts.iib.logger import logger
from scripts.iib.maintenance_settings import DEFAULTS, load_settings
from scripts.iib.thumb_cache import (
    cache_path_for,
    ensure_thumbnail,
    is_cache_candidate,
    thumb_cache_root,
)
from scripts.iib.tool import get_cache_dir, get_formatted_date

# 缩略图预热 pending 表：worker 自建、幂等。
_THUMB_PENDING_DDL = """
CREATE TABLE IF NOT EXISTS thumb_warm_pending (
  path TEXT PRIMARY KEY,
  added_at TEXT,
  tries INTEGER DEFAULT 0
)
"""

_THUMB_BATCH_SIZE = 20
_THUMB_MAX_TRIES = 3
_THUMB_CACHE_MAX_MB_DEFAULT = 2048
_THUMB_CAP_RECHECK_SEC = 900.0  # 已达到软上限后，每 15 分钟重算一次缓存体积（容忍外部清理）
_EXPIRED_CACHE_TTL = 15.0  # basic_info / expired_dirs 的过期目录缓存
_INDEX_POLL_DEFAULT = 90.0
_INDEX_DELAY_DEFAULT = 20.0
_PROGRESS_LOG_INTERVAL = 5.0
_BATCH_COMMIT_SIZE = 50
# A missing file or folder is only removed from the index when it is still missing this long after it was
# first seen: moving a file in IIB moves it on disk first and updates its row a moment later.
_PRUNE_GRACE_SEC = 60.0
_PRUNE_BATCH_SIZE = 500

_TRACKER_UNSET = object()
_request_tracker_cache = _TRACKER_UNSET


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _commit(conn) -> None:
    """提交事务；优先使用 data 层带锁重试的 commit_with_retry。"""
    try:
        from scripts.iib.db.datamodel import commit_with_retry
    except ImportError:
        conn.commit()
        return
    commit_with_retry(conn)


def _get_request_tracker():
    """惰性拿 api.py 里的请求追踪器（避免 api <-> worker 循环 import）。"""
    global _request_tracker_cache
    if _request_tracker_cache is not _TRACKER_UNSET:
        return _request_tracker_cache
    try:
        from scripts.iib import api as api_module

        _request_tracker_cache = getattr(api_module, "request_tracker", None)
    except Exception:
        _request_tracker_cache = None
    return _request_tracker_cache


def _dir_size(root: str) -> int:
    total = 0
    if not root:
        return total
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, filename))
                except OSError:
                    continue
    except OSError:
        pass
    return total


class Governor:
    """把单线程 worker 的 CPU 占用限制在整机 ``target_percent`` 附近。

    duty = clamp(target/100 * cpu_count, 0.05, 1.0)：单线程的 CPU time 只占一个核，
    睡眠 ``cpu * (1/duty - 1)`` 后整机口径即回到目标值。每 2s 用进程级 CPU 时间
    做一次 EWMA 校正（系数 clamp 0.5..4），补偿解码/IO 等实测偏差。
    """

    def __init__(self, target_percent=None, stop_event=None, wake_event=None):
        self._cpu_count = os.cpu_count() or 1
        self._target: Optional[float] = None
        self._coeff = 1.0
        self._ewma = 0.0
        self._unit_start = time.thread_time()
        self._sample_wall = time.monotonic()
        self._sample_proc = time.process_time()
        self._stop_event = stop_event
        self._wake_event = wake_event
        self.set_target(target_percent)

    def set_target(self, target_percent) -> None:
        try:
            target = float(target_percent or 0)
        except (TypeError, ValueError):
            target = 0.0
        self._target = target if target > 0 else None

    @property
    def cpu_percent(self) -> float:
        """最近一次 EWMA 校正得到的整机 CPU 估算（%）。"""
        return self._ewma

    def _duty(self) -> float:
        if not self._target:
            return 1.0
        return min(1.0, max(0.05, (self._target / 100.0) * self._cpu_count))

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        # 可被 stop / wake 打断，避免退出或立即触发时卡在长睡眠里。
        if self._wake_event is not None:
            self._wake_event.wait(seconds)
            self._wake_event.clear()
        elif self._stop_event is not None:
            self._stop_event.wait(seconds)
        else:
            time.sleep(seconds)

    def _calibrate(self) -> None:
        now_wall = time.monotonic()
        elapsed = now_wall - self._sample_wall
        if elapsed < 2.0:
            return
        now_proc = time.process_time()
        usage = (now_proc - self._sample_proc) / (elapsed * self._cpu_count) * 100.0
        self._sample_wall = now_wall
        self._sample_proc = now_proc
        # EWMA：新样本占三成，避免单次抖动把限速系数打飘。
        self._ewma = usage if self._ewma <= 0 else (self._ewma * 0.7 + usage * 0.3)
        if not self._target:
            self._coeff = 1.0
            return
        if self._ewma > self._target:
            self._coeff = min(4.0, self._coeff * 1.25)
        else:
            self._coeff = max(0.5, self._coeff * 0.85)

    def tick(self) -> None:
        """一个工作单元结束时调用：归还这段时间"欠"的 CPU，并检查前台请求。"""
        now_thread = time.thread_time()
        cpu = max(0.0, now_thread - self._unit_start)
        self._unit_start = now_thread
        self._calibrate()
        duty = self._duty()
        if duty < 1.0 and cpu > 0:
            self._sleep(cpu * (1.0 / duty - 1.0) * self._coeff)
        tracker = _get_request_tracker()
        if tracker is not None:
            try:
                if tracker.recently_active(1.0):
                    self._sleep(0.2)
            except Exception:
                pass


class IndexWorker:
    """后台索引/缩略图维护 worker。"""

    def __init__(self, get_scan_dirs: Callable[[], List[str]]):
        self._get_scan_dirs = get_scan_dirs
        self._lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._expired_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._settings: Dict = dict(DEFAULTS)
        self._paused = False
        self._pause_reason = ""

        self._phase = "idle"
        self._current_dir = ""
        self._dirs_total = 0
        self._dirs_done = 0
        self._files_indexed = 0
        self._files_failed = 0
        self._last_run_at: Optional[str] = None
        self._last_finished_at: Optional[str] = None
        self._error: Optional[str] = None
        self._next_run_wall: Optional[float] = None
        self._trigger_requested = False
        self._version = 0

        self._thumb_pending = 0
        self._thumb_done = 0
        self._thumb_failed = 0
        self._thumb_running = False
        self._thumb_capped = False
        self._thumb_capped_check_at = 0.0
        self._thumb_last_finished_at: Optional[str] = None
        self._thumb_cache_bytes = 0
        self._pending_table_ready = False

        self._last_progress_log = 0.0
        self._last_scan_dirs_error_log = 0.0

        self._expired_cache: Optional[tuple] = None  # (monotonic_ts, [dirs])
        # Rows and folders that were seen missing, waiting for the grace period; touched under _scan_lock
        # (files) or replaced as a whole (folders), kept in memory only.
        self._prune_grace = _PRUNE_GRACE_SEC
        self._pending_files: Dict[int, Tuple[str, float]] = {}  # image id -> (path, first seen)
        self._missing_since: Dict[str, float] = {}  # indexed folder gone from disk -> first seen

        self._governor = Governor(
            target_percent=DEFAULTS["cpuLimit"],
            stop_event=self._stop_event,
            wake_event=self._wake_event,
        )
        self._poll_interval = _env_float("IIB_BG_INDEX_POLL", _INDEX_POLL_DEFAULT)
        self._start_delay = _env_float("IIB_BG_INDEX_DELAY", _INDEX_DELAY_DEFAULT)
        max_mb = _env_int("IIB_BG_THUMB_CACHE_MAX_MB", _THUMB_CACHE_MAX_MB_DEFAULT)
        self._thumb_cache_max_bytes = max_mb * 1024 * 1024

    # ------------------------------------------------------------------ lifecycle

    def start(self, delay: Optional[float] = None) -> None:
        """启动后台线程；已有线程存活时只唤醒它。"""
        with self._lock:
            thread = self._thread
            alive = thread is not None and thread.is_alive()
            stopping = alive and self._stop_event.is_set()
        if stopping:
            # 刚被 stop() 的线程可能还在退出（例如正在解析一张图）；等它收尾再起新线程，
            # 避免"关闭后立刻重开"时残留的停止信号把新线程也带走。
            thread.join(timeout=3.0)
            with self._lock:
                if self._thread is thread and not thread.is_alive():
                    self._thread = None
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._wake_event.set()
                logger.info(
                    "[maintenance] worker start requested (already running, enabled=%s)",
                    bool(self._settings.get("index")),
                )
                return
            start_delay = self._start_delay if delay is None else max(0.0, float(delay))
            self._stop_event.clear()
            self._next_run_wall = time.monotonic() + start_delay
            thread = threading.Thread(
                target=self._thread_main,
                args=(start_delay,),
                name="iib-bg-index-worker",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            self._wake_event.set()
        logger.info(
            "[maintenance] worker start requested (enabled=%s, delay=%.1fs)",
            bool(self._settings.get("index")),
            self._start_delay if delay is None else delay,
        )

    def stop(self) -> None:
        """请求后台线程退出，并等待其收尾（最多 2s，避免阻塞请求线程太久）。"""
        with self._lock:
            thread = self._thread
        self._stop_event.set()
        self._wake_event.set()
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
        if thread is not None and not thread.is_alive():
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        logger.info("[maintenance] worker stop requested")

    def reload_settings(self) -> dict:
        """从 DB 重读设置并应用；按 index 开关启停后台线程。"""
        try:
            settings = load_settings(DataBase.get_conn())
        except Exception as exc:
            logger.error("[maintenance] reload settings failed: %s", exc)
            settings = dict(DEFAULTS)
        self._apply_settings(settings)
        return settings

    def _apply_settings(self, settings: dict) -> None:
        settings = settings if isinstance(settings, dict) else dict(DEFAULTS)
        with self._lock:
            self._settings = settings
            self._governor.set_target(settings.get("cpuLimit"))
            self._version += 1
            if not settings.get("index"):
                self._trigger_requested = False
                self._next_run_wall = None
        # 设置变化后过期目录缓存立即失效（例如新增 extra path / 打开功能）。
        self._expired_cache = None
        if settings.get("index"):
            self.start(delay=0.0)
        else:
            self.stop()

    # ------------------------------------------------------------------ controls

    def wake(self, reason: str = "") -> None:
        """唤醒后台线程立即检查一次（不改变扫描节奏）。"""
        self._wake_event.set()

    def trigger_now(self) -> bool:
        """请求立即扫描一轮；功能未开启返回 False。"""
        with self._lock:
            if not self._settings.get("index"):
                return False
            self._trigger_requested = True
            self._next_run_wall = None
            thread_alive = self._thread is not None and self._thread.is_alive()
        self._wake_event.set()
        if not thread_alive and not self._paused:
            self.start(delay=0.0)
        return True

    def pause(self, reason: str = "") -> None:
        with self._lock:
            self._paused = True
            self._pause_reason = reason or ""
        self._wake_event.set()
        logger.info("[maintenance] paused (%s)", reason)

    def resume(self, reason: str = "") -> None:
        with self._lock:
            self._paused = False
            self._pause_reason = ""
            self._next_run_wall = 0.0
        self._wake_event.set()
        logger.info("[maintenance] resumed (%s)", reason)

    def invalidate_expired_cache(self) -> None:
        self._expired_cache = None

    # ------------------------------------------------------------------ queries

    def _settings_snapshot(self) -> dict:
        with self._lock:
            return dict(self._settings)

    def _should_pause(self) -> bool:
        return self._stop_event.is_set() or self._paused

    def status(self) -> dict:
        """纯内存状态快照（字段名是前端契约，不要改名）。"""
        with self._lock:
            settings = dict(self._settings)
            index_enabled = bool(settings.get("index"))
            thumb_enabled = index_enabled and bool(settings.get("thumbnail"))
            phase = self._phase
            if not index_enabled:
                phase = "idle"
            elif phase == "idle":
                phase = "waiting"
            next_in = 0
            if index_enabled and self._next_run_wall is not None:
                next_in = max(0, int(math.ceil(self._next_run_wall - time.monotonic())))
            return {
                "settings": {
                    "index": index_enabled,
                    "thumbnail": thumb_enabled,
                    "thumbnailSize": int(settings.get("thumbnailSize") or 512),
                    "cpuLimit": int(settings.get("cpuLimit") or 20),
                },
                "index": {
                    "enabled": index_enabled,
                    "running": self._phase == "scanning",
                    "phase": phase,
                    "currentDir": self._current_dir if self._phase == "scanning" else "",
                    "dirsTotal": int(self._dirs_total),
                    "dirsDone": int(self._dirs_done),
                    "filesIndexed": int(self._files_indexed),
                    "filesFailed": int(self._files_failed),
                    "cpuPercent": int(round(self._governor.cpu_percent)),
                    "lastRunAt": self._last_run_at,
                    "lastFinishedAt": self._last_finished_at,
                    "nextRunInSec": next_in,
                    "error": self._error,
                },
                "thumb": {
                    "enabled": thumb_enabled,
                    "running": bool(thumb_enabled and self._thumb_running),
                    "pending": int(self._thumb_pending),
                    "done": int(self._thumb_done),
                    "failed": int(self._thumb_failed),
                    "capped": bool(self._thumb_capped),
                    "lastFinishedAt": self._thumb_last_finished_at,
                },
                "version": int(self._version),
            }

    def cached_expired_dirs(self) -> List[str]:
        """过期目录（+ 尚无 folders 记录的新扫描根），TTL 15s 缓存。

        供 /db/basic_info、/db/expired_dirs 使用，避免页面 mount 时每次都
        对全部已知目录（可能上万个）做 os.stat。
        """
        try:
            conn = DataBase.get_conn()
        except Exception as exc:
            logger.warning("[maintenance] cached_expired_dirs no conn: %s", exc)
            return []
        return self._dirty_dirs(conn, force=False)

    # ------------------------------------------------------------------ scanning

    def run_blocking(self, dirs: Optional[List[str]] = None) -> dict:
        """同步跑完一轮扫描，返回 stats（供旧 /db/update_image_data 兼容）。

        与后台线程用 ``scan_lock`` 互斥；``dirs=None`` 时自行计算过期目录。
        """
        with self._scan_lock:
            conn = DataBase.get_conn()
            settings = self._settings_snapshot()
            if dirs is None:
                seed_dirs = self._dirty_dirs(conn, force=True)
            else:
                seed_dirs = [
                    os.path.normpath(d)
                    for d in dirs
                    if isinstance(d, str) and d
                ]
            if settings.get("thumbnail"):
                self._ensure_pending_table(conn)
            pruned, pruned_folders = self._prune_due(conn)
            stats = self._scan_queue(
                conn,
                seed_dirs,
                settings,
                update_status=False,
                pace=False,
            )
        stats["files_pruned"] = pruned
        self._expired_cache = None
        if (
            stats.get("folders")
            or stats.get("files_indexed")
            or pruned
            or pruned_folders
        ):
            with self._lock:
                self._version += 1
        logger.info(
            "[maintenance] blocking scan done: dirs=%s indexed=%s failed=%s pruned=%s",
            stats.get("folders"),
            stats.get("files_indexed"),
            stats.get("files_failed"),
            pruned,
        )
        return stats

    def _scan_dirs(self) -> List[str]:
        try:
            dirs = self._get_scan_dirs() or []
        except Exception as exc:
            now = time.monotonic()
            if now - self._last_scan_dirs_error_log >= 60.0:
                self._last_scan_dirs_error_log = now
                logger.warning("[maintenance] get_scan_dirs failed: %s", exc)
            return []
        result = []
        for path in dirs:
            if isinstance(path, str) and path:
                result.append(os.path.normpath(path))
        return result

    def _known_folder_paths(self, conn) -> set:
        """已扫描过的目录集合（normcase 归一，Windows 大小写不敏感）。"""
        try:
            with closing(conn.cursor()) as cur:
                cur.execute("SELECT path FROM folders")
                return {
                    os.path.normcase(os.path.normpath(row[0]))
                    for row in cur.fetchall()
                    if row and row[0]
                }
        except Exception as exc:
            logger.warning("[maintenance] load folder records failed: %s", exc)
            return set()

    def _compute_dirty_dirs(self, conn) -> List[str]:
        try:
            missing: List[str] = []
            dirs = list(Folder.get_expired_dirs(conn, missing))
            now = time.monotonic()
            self._missing_since = {
                p: self._missing_since.get(p, now) for p in map(os.path.normpath, missing)
            }
        except Exception as exc:
            logger.warning("[maintenance] get_expired_dirs failed: %s", exc)
            dirs = []
        # 还没有 folders 记录的扫描根（首次开启 / 新加 extra path）也要扫。
        known = self._known_folder_paths(conn)
        for root in self._scan_dirs():
            if os.path.normcase(root) in known:
                continue
            try:
                if os.path.isdir(root):
                    dirs.append(root)
            except OSError:
                continue
        # 去重（Windows 大小写不敏感）
        seen = set()
        result = []
        for path in dirs:
            if not isinstance(path, str) or not path:
                continue
            norm = os.path.normpath(path)
            key = os.path.normcase(norm)
            if key in seen:
                continue
            seen.add(key)
            result.append(norm)
        return result

    def _prune_due(self, conn) -> Tuple[int, int]:
        """Remove what has been missing for the grace period; returns (image rows, folders rows) removed.

        Call with ``_scan_lock`` held. Only the worker removes rows of missing files and folders.
        """
        files = self._prune_pending_files(conn)
        images, folders = self._prune_missing_folders(conn)
        return files + images, folders

    def _prune_pending_files(self, conn) -> int:
        now = time.monotonic()
        due = [
            (image_id, path)
            for image_id, (path, seen) in self._pending_files.items()
            if now - seen >= self._prune_grace
        ]
        removed = 0
        if not due or self._should_pause():
            return removed
        for start in range(0, len(due), _PRUNE_BATCH_SIZE):
            if self._should_pause():
                break
            batch = due[start : start + _PRUNE_BATCH_SIZE]
            removed += DbImg.remove_unchanged_missing(conn, batch)
            # whoever is not missing any more, or has another path now, is done as well
            for image_id, _path in batch:
                self._pending_files.pop(image_id, None)
        return removed

    @staticmethod
    def _has_entries(path: str) -> bool:
        try:
            with os.scandir(path) as entries:
                return next(entries, None) is not None
        except OSError:
            return False

    def _may_prune_missing_dir(self, path: str, scan_dirs: Optional[List[str]] = None) -> bool:
        """May the rows below a vanished indexed folder be removed?

        Only if the folder really is gone (not found), lies below a scan root that exists and is not
        empty, and the nearest ancestor that still exists is a real, non-empty directory. An unmounted
        volume leaves an empty mount point (or a dangling symlink) behind and must not look like a
        deleted folder.
        """

        def key(p: str) -> str:
            return os.path.normcase(os.path.normpath(p))

        try:
            os.lstat(path)
            return False
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError:
            return False
        if scan_dirs is None:
            scan_dirs = self._scan_dirs()
        folder = key(path)
        if any(folder == key(root) for root in scan_dirs):
            return False
        containing = [
            root
            for root in scan_dirs
            if folder.startswith(key(root).rstrip(os.sep) + os.sep)
        ]
        if not containing:
            return False
        root = max(containing, key=lambda r: len(key(r)))
        if not os.path.isdir(root) or not self._has_entries(root):
            return False
        ancestor = os.path.dirname(os.path.normpath(path))
        while True:
            if key(ancestor) == key(root):
                return True  # checked above; a scan root may be a symlink
            try:
                st = os.lstat(ancestor)
            except (FileNotFoundError, NotADirectoryError):
                parent = os.path.dirname(ancestor)
                if parent == ancestor:
                    return False
                ancestor = parent
                continue
            except OSError:
                return False
            return stat.S_ISDIR(st.st_mode) and self._has_entries(ancestor)

    @staticmethod
    def _count_folder_marks(conn, folder: str) -> int:
        low, high = tree_bounds(folder)
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT COUNT(*) FROM folders WHERE path = ? OR (path >= ? AND path < ?)",
                (os.path.normpath(folder), low, high),
            )
            return cur.fetchone()[0]

    def _prune_missing_folders(self, conn) -> Tuple[int, int]:
        """Remove the rows of indexed folders that vanished from disk; returns (image rows, folders rows) removed."""
        now = time.monotonic()
        due = [p for p, seen in self._missing_since.items() if now - seen >= self._prune_grace]
        if not due or self._should_pause():
            return 0, 0
        scan_dirs = self._scan_dirs()
        images = folders = 0
        for path in due:
            if self._should_pause():
                break
            if not self._may_prune_missing_dir(path, scan_dirs):
                continue
            try:
                low, high = tree_bounds(path)
                with closing(conn.cursor()) as cur:
                    cur.execute("SELECT id, path FROM image WHERE path >= ? AND path < ?", (low, high))
                    rows = cur.fetchall()
                removed = 0
                paused = False
                for start in range(0, len(rows), _PRUNE_BATCH_SIZE):
                    if self._should_pause():
                        paused = True
                        break
                    removed += DbImg.remove_unchanged_missing(conn, rows[start : start + _PRUNE_BATCH_SIZE])
                if paused:
                    images += removed
                    break
                marks = self._count_folder_marks(conn, path)
                Folder.remove_missing_under(conn, path)
                marks -= self._count_folder_marks(conn, path)
            except Exception as exc:
                logger.warning("[maintenance] prune missing folder failed %s: %s", path, exc)
                continue
            self._missing_since.pop(path, None)
            images += removed
            folders += marks
            logger.debug(
                "[maintenance] pruned missing folder %s: %s rows, %s folder marks", path, removed, marks
            )
        if images or folders:
            self._expired_cache = None
        return images, folders

    def _dirty_dirs(self, conn, force: bool = False) -> List[str]:
        now = time.monotonic()
        cached = self._expired_cache
        if not force and cached is not None and (now - cached[0]) < _EXPIRED_CACHE_TTL:
            return list(cached[1])
        with self._expired_lock:
            cached = self._expired_cache
            now = time.monotonic()
            if not force and cached is not None and (now - cached[0]) < _EXPIRED_CACHE_TTL:
                return list(cached[1])
            dirs = self._compute_dirty_dirs(conn)
            self._expired_cache = (time.monotonic(), dirs)
            return list(dirs)

    def _mark_dir_discovered(self, conn, path: str) -> bool:
        """给"刚发现、尚未扫描"的子目录写持久化脏标记。

        做法是把 folders.modified_date 置为空串（scanner 扫描完会用真实水位覆盖），
        这样即使后台扫描被 pause / 关闭开关 / 进程硬杀打断，内存里的队列丢了，
        下一轮 ``Folder.get_expired_dirs`` 也会把这个目录当过期目录带回来。
        """
        norm = os.path.normpath(path)
        try:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    "INSERT INTO folders (path, modified_date) "
                    "SELECT ?, '' WHERE NOT EXISTS "
                    "(SELECT 1 FROM folders WHERE path = ?)",
                    (norm, norm),
                )
                return cur.rowcount > 0
        except Exception as exc:
            logger.debug("[maintenance] mark discovered dir failed %s: %s", norm, exc)
            return False

    def _scan_fn(self):
        from scripts.iib.db.update_image_data import scan_folder_incremental

        return scan_folder_incremental

    def _scan_queue(
        self,
        conn,
        seed_dirs: List[str],
        settings: dict,
        *,
        update_status: bool,
        pace: bool,
    ) -> dict:
        total = {
            "folders": 0,
            "files_indexed": 0,
            "files_skipped": 0,
            "files_failed": 0,
            "paused": False,
        }
        try:
            scan_fn = self._scan_fn()
        except Exception as exc:
            raise RuntimeError(f"scan_folder_incremental unavailable: {exc}") from exc

        known = self._known_folder_paths(conn)
        queue: List[str] = []
        queued_keys = set()

        def enqueue(path: str) -> bool:
            norm = os.path.normpath(path)
            key = os.path.normcase(norm)
            if key in queued_keys:
                return False
            queued_keys.add(key)
            queue.append(norm)
            return True

        for path in seed_dirs:
            enqueue(path)
        if update_status:
            with self._lock:
                self._dirs_total = len(queue)
                self._dirs_done = 0

        index = 0
        while index < len(queue):
            if self._should_pause():
                total["paused"] = True
                break
            dir_path = queue[index]
            index += 1
            try:
                if not os.path.isdir(dir_path):
                    continue
            except OSError:
                continue

            base_indexed = self._files_indexed
            base_failed = self._files_failed
            if update_status:
                with self._lock:
                    self._current_dir = dir_path

            def on_progress(
                stats: dict, _base_indexed=base_indexed, _base_failed=base_failed
            ) -> None:
                if not update_status:
                    return
                if not isinstance(stats, dict):
                    return
                with self._lock:
                    self._files_indexed = _base_indexed + int(
                        stats.get("files_indexed") or 0
                    )
                    self._files_failed = _base_failed + int(stats.get("files_failed") or 0)
                self._maybe_log_progress()
                if pace:
                    self._governor.tick()

            missing_files: List[tuple] = []
            try:
                stats = scan_fn(
                    conn,
                    dir_path,
                    on_new_image=self._on_new_image,
                    on_progress=on_progress if update_status else None,
                    should_pause=self._should_pause,
                    batch_commit_size=_BATCH_COMMIT_SIZE,
                    missing_files=missing_files,
                )
            except Exception as exc:
                logger.error("[maintenance] scan folder failed: %s: %s", dir_path, exc)
                stats = {"folders": 0, "files_failed": 1, "paused": False}

            # rows of files that vanished from this folder wait for the grace period (see _prune_due)
            seen = time.monotonic()
            for image_id, path in missing_files:
                if self._pending_files.get(image_id, (None,))[0] != path:
                    self._pending_files[image_id] = (path, seen)

            if isinstance(stats, dict):
                for key in ("folders", "files_indexed", "files_skipped", "files_failed"):
                    total[key] += int(stats.get(key) or 0)
                if stats.get("paused"):
                    total["paused"] = True
                    if update_status:
                        with self._lock:
                            # 当前目录只处理了一部分（未写水位，下轮重做），进度按已完成计。
                            self._dirs_done = max(0, index - 1)
                    break

            known.add(os.path.normcase(os.path.normpath(dir_path)))

            # 增量扫描器只处理直接子项：父目录过期时它下面新出现的子目录需要
            # 由 worker 发现并排队（不递归扫描未知目录会永久漏图）。
            # 排队的子目录同时写一个持久化脏标记（folders.modified_date = ''），
            # 否则 pause / 关闭开关 / 进程被硬杀时内存队列丢失，父目录水位又已写好，
            # 这批新子目录就再也发现不了了（下轮 get_expired_dirs 会把标记目录带回来）。
            marked = 0
            try:
                with os.scandir(dir_path) as entries:
                    for entry in entries:
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        child = os.path.normpath(entry.path)
                        child_key = os.path.normcase(child)
                        if child_key in known:
                            continue
                        if not enqueue(child):
                            continue
                        known.add(child_key)
                        if self._mark_dir_discovered(conn, child):
                            marked += 1
                        if update_status:
                            with self._lock:
                                self._dirs_total = len(queue)
            except OSError:
                pass
            if marked:
                try:
                    _commit(conn)
                except Exception as exc:
                    logger.debug("[maintenance] commit dir markers failed: %s", exc)

            if update_status:
                with self._lock:
                    self._dirs_done = index
                self._maybe_log_progress()
            if pace:
                self._governor.tick()

        if update_status:
            with self._lock:
                self._current_dir = ""
        return total

    def _maybe_log_progress(self) -> None:
        now = time.monotonic()
        if now - self._last_progress_log < _PROGRESS_LOG_INTERVAL:
            return
        self._last_progress_log = now
        with self._lock:
            logger.info(
                "[maintenance] indexing %s/%s dirs, indexed=%s failed=%s current=%s",
                self._dirs_done,
                self._dirs_total,
                self._files_indexed,
                self._files_failed,
                self._current_dir,
            )

    def _set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def _set_error(self, error: str) -> None:
        with self._lock:
            self._error = error
        self._set_phase("error")

    # ------------------------------------------------------------------ main loop

    def _thread_main(self, start_delay: float) -> None:
        logger.info(
            "[maintenance] background worker thread started (delay=%.1fs, poll=%.1fs, cpuLimit=%s%%)",
            start_delay,
            self._poll_interval,
            self._settings.get("cpuLimit"),
        )
        try:
            conn = DataBase.get_conn()
        except Exception as exc:
            logger.exception("[maintenance] worker cannot open database: %s", exc)
            self._set_error(str(exc))
            with self._lock:
                self._thread = None
            return

        self._ensure_pending_table(conn)
        self._refresh_pending_count(conn)

        try:
            while not self._stop_event.is_set():
                self._wake_event.clear()
                settings = self._settings_snapshot()
                if not settings.get("index"):
                    self._set_phase("idle")
                    with self._lock:
                        self._next_run_wall = None
                    self._wait(5.0)
                    continue
                if self._paused:
                    self._set_phase("paused")
                    self._wait(1.0)
                    continue

                now = time.monotonic()
                with self._lock:
                    trigger = self._trigger_requested
                    next_wall = self._next_run_wall
                if not trigger and next_wall is not None and now < next_wall:
                    if self._phase != "error":
                        self._set_phase("waiting")
                    self._wait(min(next_wall - now, 1.0))
                    continue

                with self._lock:
                    self._trigger_requested = False
                    self._next_run_wall = None
                try:
                    self._run_cycle(conn, settings)
                except Exception as exc:
                    logger.exception("[maintenance] maintenance cycle failed: %s", exc)
                    self._set_error(str(exc))
                with self._lock:
                    self._next_run_wall = time.monotonic() + self._poll_interval
                self._wait(0.5)
        finally:
            self._set_phase("idle")
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
            logger.info("[maintenance] background worker thread stopped")

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._wake_event.wait(seconds)
        self._wake_event.clear()

    def _run_cycle(self, conn, settings: dict) -> None:
        started = time.monotonic()
        with self._lock:
            self._last_run_at = _now_iso()
            self._error = None
            self._current_dir = ""
            self._dirs_total = 0
            self._dirs_done = 0
            self._files_indexed = 0
            self._files_failed = 0

        dirty_dirs = self._dirty_dirs(conn, force=True)
        with self._lock:
            self._dirs_total = len(dirty_dirs)

        pruned_files = pruned_folders = 0
        if self._missing_since or self._pending_files:
            with self._scan_lock:
                pruned_files, pruned_folders = self._prune_due(conn)

        if dirty_dirs:
            with self._lock:
                self._phase = "scanning"
            # 与旧 /db/update_image_data 的 run_blocking() 互斥，避免两个线程同时扫同一批目录。
            with self._scan_lock:
                self._scan_queue(
                    conn,
                    dirty_dirs,
                    settings,
                    update_status=True,
                    pace=True,
                )
        # 扫描结束后在后台线程重算一次过期目录并缓存：
        # 1) 让页面上的"索引可能还没更新完"提示在索引追平后马上消失；
        # 2) 避免 /db/basic_info 在 API 线程里做上万目录的全量 stat。
        try:
            self._dirty_dirs(conn, force=True)
        except Exception as exc:
            logger.warning("[maintenance] refresh expired dirs after scan failed: %s", exc)

        with self._lock:
            self._last_finished_at = _now_iso()
            self._current_dir = ""
            # 目录扫描结束后就离开 scanning 态：缩略图预热可能持续数分钟，
            # 期间前端应显示「缩略图预热」而不是「后台更新索引」。
            if self._phase == "scanning":
                self._phase = "waiting"
            # version 语义（前端契约）：每完成一轮真正有扫描工作的后台维护 +1，
            # 前端据此刷新标签列表 / 过期提示；缩略图预热不阻塞这次通知。
            if dirty_dirs or pruned_files or pruned_folders:
                self._version += 1

        if pruned_files or pruned_folders:
            logger.info(
                "[maintenance] pruned %s rows of missing files, %s missing folders",
                pruned_files,
                pruned_folders,
            )

        # 缩略图预热不占用 scan_lock：它不扫目录，且生成单张耗时较长，
        # 不应该让旧端点里的同步扫描等它。
        thumb_done_before = self._thumb_done
        if settings.get("thumbnail"):
            self._consume_pending_thumbs(conn, settings)

        with self._lock:
            # 本轮结束（即使上一轮 error / 本轮扫描被 pause 打断，下一轮会重新设置）。
            self._phase = "waiting"
            indexed = self._files_indexed
            failed = self._files_failed
            dirs_done = self._dirs_done
            dirs_total = self._dirs_total
        # 空转轮不写 info 日志（每 90s 一次会长期刷屏），需要时用 debug 看。
        log = logger.info if (dirty_dirs or thumb_done_before != self._thumb_done) else logger.debug
        log(
            "[maintenance] cycle done in %.1fs: dirs=%s/%s indexed=%s failed=%s thumbs_done=%s pending=%s",
            time.monotonic() - started,
            dirs_done,
            dirs_total,
            indexed,
            failed,
            self._thumb_done,
            self._thumb_pending,
        )

    # ------------------------------------------------------------------ thumbnail warm-up

    def _ensure_pending_table(self, conn) -> None:
        if self._pending_table_ready:
            return
        try:
            with closing(conn.cursor()) as cur:
                cur.execute(_THUMB_PENDING_DDL)
            _commit(conn)
            self._pending_table_ready = True
        except Exception as exc:
            logger.warning("[maintenance] create thumb_warm_pending failed: %s", exc)

    def _refresh_pending_count(self, conn) -> None:
        try:
            with closing(conn.cursor()) as cur:
                cur.execute("SELECT COUNT(*) FROM thumb_warm_pending")
                row = cur.fetchone()
                pending = int(row[0]) if row else 0
        except Exception:
            return
        with self._lock:
            self._thumb_pending = pending

    def _on_new_image(self, path: str) -> None:
        """扫描器回调：开启缩略图预热时，把新加入索引的图入队。"""
        if not path:
            return
        settings = self._settings_snapshot()
        if not settings.get("thumbnail"):
            return
        try:
            conn = DataBase.get_conn()
            self._enqueue_thumb(conn, path)
        except Exception as exc:
            logger.debug("[maintenance] enqueue thumbnail failed %s: %s", path, exc)

    def _enqueue_thumb(self, conn, path: str) -> None:
        path = os.path.normpath(path)
        if not is_cache_candidate(path):
            return
        settings = self._settings_snapshot()
        size = int(settings.get("thumbnailSize") or 512)
        try:
            date_str = get_formatted_date(os.path.getmtime(path))
        except OSError:
            return
        cache_path = cache_path_for(path, date_str, size)
        if not cache_path or os.path.exists(cache_path):
            return
        self._ensure_pending_table(conn)
        try:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    "INSERT OR IGNORE INTO thumb_warm_pending (path, added_at, tries) VALUES (?, ?, 0)",
                    (path, _now_iso()),
                )
                inserted = cur.rowcount > 0
        except Exception as exc:
            logger.debug("[maintenance] pending insert failed %s: %s", path, exc)
            return
        if inserted:
            # 不单独 commit：扫描器会按批提交，pending 行跟着同一事务落库。
            with self._lock:
                self._thumb_pending += 1

    def _consume_pending_thumbs(self, conn, settings: dict) -> None:
        cache_base = get_cache_dir()
        if not cache_base:
            return
        size = int(settings.get("thumbnailSize") or 512)
        self._ensure_pending_table(conn)

        # 清理 image 表中已不存在的 pending 行（删除图片后残留），避免 COUNT 虚高。
        try:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    "DELETE FROM thumb_warm_pending WHERE NOT EXISTS "
                    "(SELECT 1 FROM image WHERE image.path = thumb_warm_pending.path)"
                )
            _commit(conn)
        except Exception:
            pass

        self._refresh_pending_count(conn)
        with self._lock:
            pending = self._thumb_pending
        if pending <= 0:
            with self._lock:
                self._thumb_last_finished_at = _now_iso()
            return

        if not self._thumb_capped:
            # 缩略图阶段开始时一次性统计缓存体积（只在开启且有 pending 时做）。
            self._thumb_cache_bytes = _dir_size(thumb_cache_root())
            if self._thumb_cache_bytes >= self._thumb_cache_max_bytes:
                self._mark_thumb_capped()
        elif time.monotonic() - self._thumb_capped_check_at >= _THUMB_CAP_RECHECK_SEC:
            self._thumb_cache_bytes = _dir_size(thumb_cache_root())
            self._thumb_capped_check_at = time.monotonic()
            if self._thumb_cache_bytes < self._thumb_cache_max_bytes:
                with self._lock:
                    self._thumb_capped = False
                logger.info("[maintenance] thumbnail cache under limit again, resume warming")
        if self._thumb_capped:
            with self._lock:
                self._thumb_last_finished_at = _now_iso()
            return

        with self._lock:
            self._thumb_running = True
        last_batch_key = None
        try:
            while not self._should_pause():
                rows = self._select_thumb_batch(conn)
                if not rows:
                    break
                batch_key = tuple(row[0] for row in rows)
                if batch_key == last_batch_key:
                    # 删不掉行（例如持续写锁失败）时避免在同一批上死循环；
                    # 下一轮/下次开启会重试。
                    logger.warning(
                        "[maintenance] thumbnail batch made no progress, stop this round"
                    )
                    break
                last_batch_key = batch_key
                for path, date_str in rows:
                    if self._should_pause():
                        break
                    self._warm_single_thumb(conn, path, date_str, size)
                    if self._thumb_capped:
                        break
                    self._governor.tick()
                if self._thumb_capped:
                    break
                self._refresh_pending_count(conn)
        finally:
            with self._lock:
                self._thumb_running = False
                self._thumb_last_finished_at = _now_iso()
                done = self._thumb_done
                failed = self._thumb_failed
            self._refresh_pending_count(conn)
            with self._lock:
                pending = self._thumb_pending
                capped = self._thumb_capped
            logger.info(
                "[maintenance] thumbnail warm done=%s failed=%s pending=%s capped=%s",
                done,
                failed,
                pending,
                capped,
            )

    def _select_thumb_batch(self, conn) -> list:
        try:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    "SELECT image.path, image.date FROM image "
                    "JOIN thumb_warm_pending ON image.path = thumb_warm_pending.path "
                    "LIMIT ?",
                    (_THUMB_BATCH_SIZE,),
                )
                return cur.fetchall()
        except Exception as exc:
            logger.warning("[maintenance] select thumb batch failed: %s", exc)
            return []

    def _warm_single_thumb(self, conn, path: str, date_str: str, size: int) -> None:
        cache_path = cache_path_for(path, date_str or "", size)
        if not cache_path:
            self._delete_pending(conn, path)
            return
        if os.path.exists(cache_path):
            self._delete_pending(conn, path)
            with self._lock:
                self._thumb_done += 1
            return
        if not is_cache_candidate(path):
            # 源文件已删除、变小或不是图片：没有预热价值，直接丢弃。
            self._delete_pending(conn, path)
            return
        try:
            ensure_thumbnail(path, cache_path, size, size)
        except Exception:
            self._handle_thumb_failure(conn, path)
            return
        try:
            self._thumb_cache_bytes += os.path.getsize(cache_path)
        except OSError:
            pass
        self._delete_pending(conn, path)
        with self._lock:
            self._thumb_done += 1
        if self._thumb_cache_bytes >= self._thumb_cache_max_bytes:
            self._mark_thumb_capped()

    def _delete_pending(self, conn, path: str) -> None:
        try:
            with closing(conn.cursor()) as cur:
                cur.execute("DELETE FROM thumb_warm_pending WHERE path = ?", (path,))
            _commit(conn)
        except Exception as exc:
            logger.debug("[maintenance] delete pending failed %s: %s", path, exc)

    def _handle_thumb_failure(self, conn, path: str) -> None:
        drop = False
        try:
            with closing(conn.cursor()) as cur:
                cur.execute(
                    "SELECT tries FROM thumb_warm_pending WHERE path = ?", (path,)
                )
                row = cur.fetchone()
                tries = (int(row[0]) if row and row[0] is not None else 0) + 1
                if tries >= _THUMB_MAX_TRIES:
                    cur.execute(
                        "DELETE FROM thumb_warm_pending WHERE path = ?", (path,)
                    )
                    drop = True
                else:
                    cur.execute(
                        "UPDATE thumb_warm_pending SET tries = ? WHERE path = ?",
                        (tries, path),
                    )
            _commit(conn)
        except Exception as exc:
            logger.debug("[maintenance] thumb failure bookkeeping failed %s: %s", path, exc)
            return
        if drop:
            with self._lock:
                self._thumb_failed += 1

    def _mark_thumb_capped(self) -> None:
        with self._lock:
            first = not self._thumb_capped
            self._thumb_capped = True
            self._thumb_capped_check_at = time.monotonic()
        if first:
            logger.warning(
                "[maintenance] thumbnail warm stopped: iib_cache %.0fMB >= limit %.0fMB",
                self._thumb_cache_bytes / (1024 * 1024),
                self._thumb_cache_max_bytes / (1024 * 1024),
            )


_worker: Optional[IndexWorker] = None
_worker_init_lock = threading.Lock()


def init_worker(get_scan_dirs: Callable[[], List[str]]) -> IndexWorker:
    """创建（或复用）全局 worker；启动时按 DB 设置决定是否拉起后台线程。"""
    global _worker
    with _worker_init_lock:
        if _worker is not None:
            _worker._get_scan_dirs = get_scan_dirs
            _worker.reload_settings()
            return _worker
        worker = IndexWorker(get_scan_dirs)
        try:
            settings = load_settings(DataBase.get_conn())
        except Exception as exc:
            logger.error("[maintenance] load settings on init failed: %s", exc)
            settings = dict(DEFAULTS)
        with worker._lock:
            worker._settings = settings
            worker._governor.set_target(settings.get("cpuLimit"))
            worker._version += 1
        _worker = worker
        if settings.get("index"):
            worker.start(delay=worker._start_delay)
        else:
            logger.info("[maintenance] worker initialized but index disabled")
        return worker


def get_worker() -> Optional[IndexWorker]:
    return _worker


def default_status() -> dict:
    """worker 未初始化时的兜底状态（形状与 IndexWorker.status 一致）。"""
    settings = dict(DEFAULTS)
    return {
        "settings": {
            "index": settings["index"],
            "thumbnail": settings["thumbnail"],
            "thumbnailSize": settings["thumbnailSize"],
            "cpuLimit": settings["cpuLimit"],
        },
        "index": {
            "enabled": False,
            "running": False,
            "phase": "idle",
            "currentDir": "",
            "dirsTotal": 0,
            "dirsDone": 0,
            "filesIndexed": 0,
            "filesFailed": 0,
            "cpuPercent": 0,
            "lastRunAt": None,
            "lastFinishedAt": None,
            "nextRunInSec": 0,
            "error": None,
        },
        "thumb": {
            "enabled": False,
            "running": False,
            "pending": 0,
            "done": 0,
            "failed": 0,
            "capped": False,
            "lastFinishedAt": None,
        },
        "version": 0,
    }
