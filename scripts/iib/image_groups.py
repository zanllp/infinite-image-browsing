"""
Image groups: pixel duplicates and near duplicates by image hash.

A background job hashes the images that have no current hash yet (see ``image_hash.py``) and stores
the result in the table ``image_hash``. The groups are computed from that table on every request:

- pixel groups: images with the same ``pixel_sha`` (identical decoded pixels),
- near groups: connected components of "Hamming distance of the pHashes <= threshold" that contain at
  least two different ``pixel_sha`` (purely pixel-identical groups are pixel groups only). Flat images
  have no pHash and take no part in near groups.

Image variants come from the metadata (``image.exif`` in the A1111 form), not from the hash:

- seed variants: the same prompt (positive prompt without ``<lora:…>`` as raw text, negative prompt, LoRAs with
  their weights) with at least ``min_seeds`` different seeds,
- parameter variants: the same prompt, seed and size with more than one set of generation parameters (every field
  of the parameter line except Seed, Size and Source Identifier).

Images without a positive prompt take no part and are counted; images without a seed take no part. Inside a
group the images are ordered by the gray thumbnails of ``image_hash`` where they exist, otherwise by date.

The job decodes in a few threads of the server process (``IIB_IMAGE_HASH_THREADS``, default 3, 1 to 8).
Images it cannot read for a transient reason (missing file, permissions, I/O) are not stored; the process
remembers them, so they do not count as pending again until the server restarts or a new job tries them.

numpy is an optional dependency of IIB: it is imported when a route needs it, and the routes answer with
an error (500) where it is missing.

Routes (all below ``{db_api_base}``): ``image_groups_state``, ``image_groups_job_start``,
``image_groups_job_status``, ``image_groups_job_cancel``, ``image_groups`` and ``image_variants``.
"""
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from scripts.iib import image_hash
from scripts.iib.db.datamodel import DataBase, Image, ImageHash, commit_with_retry
from scripts.iib.image_hash import HASH_VERSION
from scripts.iib.logger import logger
from scripts.iib.tool import get_audio_type, get_video_type, re_param, unquote

DEFAULT_THREADS = 3
MAX_THREADS = 8
BATCH_SIZE = 8  # images per task: small, so little work runs on after a cancel
CANCEL_POLL = 0.25  # seconds between looks at the cancel flag while batches run
CANCEL_GRACE = 10.0  # seconds a cancelled job waits for the running batches before it lets them go
WRITE_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0, 4.0)  # seconds between writes while SQLite reports a locked database
SQL_BATCH = 900  # SQLite default max variable number is 999
MAX_THRESHOLD = 12
PREVIEW_IMAGES = 4  # file infos per group in the response; the paths of all members come with it
JOBS_MAX = 20
CACHE_MAX = 8  # group lists (small)
EDGE_CACHE_MAX = 2  # edge lists of the near graph (up to MAX_EDGES * 9 bytes each)
MAX_EDGES = 5_000_000
FAIL_LOG_MAX = 20  # images named in the log per job and kind (skipped, failed)
VARIANT_KINDS = ("seed", "params")
DEFAULT_MIN_SEEDS = 3
MAX_MIN_SEEDS = 100
PROMPT_PREVIEW = 200  # characters of the prompt in a variant group of the response
VARIANT_CHUNK = 2000  # rows per read of the exif column: bounded memory, the read lock is released in between
VARIANT_CACHE_MAX = 4  # grouped libraries (index lists, both kinds per entry)
# not part of the generation parameters; compared in lower case (NovelAI writes "seed", the A1111 form "Seed")
_PARAM_SKIP = frozenset({"seed", "size", "source identifier"})
_LORA_RE = re.compile(r"<lora:([^:>]+)(?::([^>]+))?>")
_EXTRA_JSON_RE = re.compile(r"\nextraJsonMetaInfo:\s*(\{[\s\S]*\})\s*$")  # as in tool.parse_generation_parameters

_JOBS: Dict[str, Dict] = {}  # in insertion order, the oldest are dropped
_JOBS_LOCK = threading.Lock()
_ACTIVE: Dict[str, Optional[str]] = {"job_id": None}
_TASKS = set()  # keeps the background tasks referenced until they are done

# images a job could not read for a transient reason: id -> (size, date) of the image row at that time
_SKIPPED: Dict[int, Tuple[Optional[int], Optional[str]]] = {}
_SKIPPED_LOCK = threading.Lock()


def _ensure_deps() -> None:
    try:
        import numpy  # noqa: F401
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Image groups require numpy. Please install it. import_error={type(e).__name__}: {e}",
        )


def _thread_count() -> int:
    try:
        n = int(os.getenv("IIB_IMAGE_HASH_THREADS", "") or DEFAULT_THREADS)
    except ValueError:
        n = DEFAULT_THREADS
    return max(1, min(MAX_THREADS, n))


# ---------------------------------------------------------------- jobs (in memory)


def _job_update(job_id: str, **fields) -> None:
    with _JOBS_LOCK:
        _JOBS.setdefault(job_id, {"job_id": job_id}).update(fields)
        while len(_JOBS) > JOBS_MAX:
            del _JOBS[next(iter(_JOBS))]


def _job_get(job_id: Optional[str]) -> Optional[Dict]:
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        return dict(j) if j else None


def _active_job() -> Optional[Dict]:
    j = _job_get(_ACTIVE["job_id"])
    return j if j and j["status"] in ("queued", "running") else None


def _cancel_requested(job_id: str) -> bool:
    return bool((_job_get(job_id) or {}).get("cancel_requested"))


# ---------------------------------------------------------------- database access


Item = Tuple[int, str, Optional[int], Optional[str]]  # id, path, size, date


def _is_video_or_audio(path) -> bool:
    return isinstance(path, str) and (get_video_type(path) is not None or get_audio_type(path) is not None)


def _scan_hash_state(conn, path_ok: Optional[Callable[[str], bool]] = None):
    """(hashed, failed, pending items, skipped items) of the image table.

    Skipped items are the ones this process could not read for a transient reason; they are neither
    pending nor hashed. Videos, audio and (with access control) paths outside the allowed ones are left out.
    """
    rows = conn.execute(
        """SELECT i.id, i.path, i.size, i.date, h.status, h.version, h.size, h.date
           FROM image i LEFT JOIN image_hash h ON h.image_id = i.id"""
    ).fetchall()  # the read lock is released before the (slow) filtering below
    with _SKIPPED_LOCK:
        # forget skips of images that are gone or changed, so the memory does not grow with the library
        current = {r[0]: (r[2], r[3]) for r in rows}
        for image_id in [k for k, v in _SKIPPED.items() if current.get(k) != v]:
            del _SKIPPED[image_id]
        skipped_ids = set(_SKIPPED)
    hashed = failed = 0
    pending: List[Item] = []
    skipped: List[Item] = []
    for image_id, path, size, date, status, version, h_size, h_date in rows:
        if _is_video_or_audio(path) or (path_ok is not None and not path_ok(path)):
            continue
        if version == HASH_VERSION and h_size == size and h_date == date and status in ("ok", "fail"):
            if status == "ok":
                hashed += 1
            else:
                failed += 1
        else:
            (skipped if image_id in skipped_ids else pending).append((image_id, path, size, date))
    return hashed, failed, pending, skipped


def _state_counts(conn, path_ok=None) -> Dict:
    hashed, failed, pending, skipped = _scan_hash_state(conn, path_ok)
    total = hashed + failed + len(pending) + len(skipped)
    return {"total": total, "hashed": hashed, "failed": failed, "pending": len(pending), "skipped": len(skipped)}


def _retry_locked(fn):
    """Run fn, repeating it while SQLite reports a locked database (busy_timeout comes first)."""
    for delay in (*WRITE_RETRY_DELAYS, None):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            message = str(e).lower()
            if delay is None or ("locked" not in message and "busy" not in message):
                raise
            logger.warning("[image_groups] database is locked, retrying in %.1fs (%s)", delay, e)
            time.sleep(delay)


def _write_results(items: Dict[int, Item], results: List[tuple]) -> None:
    rows = [
        (image_id, sha, phash, gray, HASH_VERSION, *items[image_id][2:], status, error)
        for image_id, status, sha, phash, gray, error in results
    ]

    def write():
        conn = DataBase.get_conn()
        try:
            ImageHash.upsert_many(conn, rows)
            commit_with_retry(conn)
        except BaseException:
            conn.rollback()
            raise

    _retry_locked(write)


# ---------------------------------------------------------------- the hash job


def _take_results(results: List[tuple], by_id: Dict[int, Item], problems: Dict[str, int]) -> List[tuple]:
    """Remember the skipped images, log the problems and return the results to store (all but the skipped)."""
    with _SKIPPED_LOCK:
        for image_id, status, *_ in results:
            if status == "skip":
                _SKIPPED[image_id] = by_id[image_id][2:]
            else:
                _SKIPPED.pop(image_id, None)
    for image_id, status, *_, error in results:
        if status in problems:
            problems[status] += 1
            if problems[status] <= FAIL_LOG_MAX:
                what = "skipped" if status == "skip" else "cannot decode"
                logger.warning("[image_groups] %s %s: %s", what, by_id[image_id][1], error)
    return [r for r in results if r[1] != "skip"]


async def _run_job(job_id: str, path_ok: Optional[Callable[[str], bool]]) -> None:
    executor: Optional[ThreadPoolExecutor] = None
    abandoned = False
    try:
        if _cancel_requested(job_id):
            _job_update(job_id, status="cancelled")
            return
        _job_update(job_id, status="running")

        def scan():
            _hashed, _failed, pending, skipped = _retry_locked(lambda: _scan_hash_state(DataBase.get_conn(), path_ok))
            return pending + skipped  # a new job tries the skipped images once more

        items = await asyncio.to_thread(scan)
        _job_update(job_id, total=len(items))
        by_id = {item[0]: item for item in items}
        todo = iter([[(i, p) for i, p, _s, _d in items[k : k + BATCH_SIZE]] for k in range(0, len(items), BATCH_SIZE)])
        workers = _thread_count()
        executor = ThreadPoolExecutor(workers, thread_name_prefix="iib-image-hash")  # threads start on demand
        loop = asyncio.get_running_loop()
        inflight = set()
        done = 0
        problems = {"skip": 0, "fail": 0}
        cancelled_at: Optional[float] = None
        while True:
            # After a cancel no new batch is started; the running ones finish and are written. A read that
            # hangs (a dead network drive) blocks its thread and cannot be interrupted: after CANCEL_GRACE the
            # job gives up on those batches (their results are dropped), so a new job can start.
            if cancelled_at is None and _cancel_requested(job_id):
                cancelled_at = time.monotonic()
            while cancelled_at is None and len(inflight) < workers:
                batch = next(todo, None)
                if batch is None:
                    break
                inflight.add(loop.run_in_executor(executor, image_hash.hash_batch, batch))
            if not inflight:
                break
            finished, inflight = await asyncio.wait(inflight, timeout=CANCEL_POLL, return_when=asyncio.FIRST_COMPLETED)
            if not finished and cancelled_at is not None and time.monotonic() - cancelled_at >= CANCEL_GRACE:
                abandoned = True
                break
            for fut in finished:
                results = fut.result()
                store = _take_results(results, by_id, problems)
                if store:
                    await asyncio.to_thread(_write_results, by_id, store)
                done += len(results)
                _job_update(job_id, done=done, skipped=problems["skip"])
        if max(problems.values()) > FAIL_LOG_MAX:
            logger.warning("[image_groups] %d images skipped, %d cannot be decoded", problems["skip"], problems["fail"])
        _job_update(job_id, status="done" if cancelled_at is None else "cancelled")
    except asyncio.CancelledError:
        _job_update(job_id, status="error", error="cancelled")
        raise
    except Exception as e:
        logger.error("[image_groups] job %s failed: %s", job_id, e, exc_info=True)
        _job_update(job_id, status="error", error=f"{type(e).__name__}: {e}")
    finally:
        if executor is not None:
            await asyncio.to_thread(executor.shutdown, not abandoned, cancel_futures=True)
        if _ACTIVE["job_id"] == job_id:
            _ACTIVE["job_id"] = None


# ---------------------------------------------------------------- grouping


def _load_hashed(conn, path_ok: Optional[Callable[[str], bool]] = None):
    """The images with a current ok hash: (ids ascending, pixel_sha list, phash int64 array, has_phash bool array).

    Flat images have no phash (NULL); their array value is 0 and has_phash is False.
    """
    import numpy as np

    rows = conn.execute(
        """SELECT i.id, h.pixel_sha, h.phash, i.path FROM image i JOIN image_hash h ON h.image_id = i.id
           WHERE h.status = 'ok' AND h.version = ? AND h.size IS i.size AND h.date IS i.date
           ORDER BY i.id""",
        (HASH_VERSION,),
    ).fetchall()
    if path_ok is not None:
        rows = [r for r in rows if path_ok(r[3])]
    ids = [r[0] for r in rows]
    shas = [r[1] for r in rows]
    has_phash = np.array([r[2] is not None for r in rows], dtype=bool)
    phash = np.array([r[2] or 0 for r in rows], dtype=np.int64)
    return ids, shas, phash, has_phash


def _load_members(conn, image_ids: List[int]) -> Dict[int, Tuple[Image, Optional[bytes]]]:
    """{id: (image row, gray thumbnail)}. The rows come without their exif text: the file info does not
    use it, and it is by far the largest column."""
    out = {}
    for i in range(0, len(image_ids), SQL_BATCH):
        batch = image_ids[i : i + SQL_BATCH]
        rows = conn.execute(
            f"""SELECT i.id, i.path, i.size, i.date, h.gray FROM image i LEFT JOIN image_hash h ON h.image_id = i.id
                WHERE i.id IN ({','.join('?' * len(batch))})""",
            batch,
        ).fetchall()
        for image_id, path, size, date, gray in rows:
            out[image_id] = (Image(path, size=size, date=date, id=image_id), gray)
    return out


# Caches. The keys hold a digest of what the result is computed from, so a new hash, a deleted or changed
# image makes a new key and nothing has to be invalidated. Requests for the same key at the same time
# compute once: the others wait for the result (an error is passed on, not cached). Only one expensive
# computation runs at a time.
#   _GROUPS: the group lists per (kind, threshold, digest); small.
#   _EDGES:  the pairs of the near graph up to MAX_THRESHOLD per digest, so every threshold is a cheap filter
#            over the edges; None when there are more than MAX_EDGES (then each threshold is computed on its
#            own, and only its groups are cached).
_GROUPS: "OrderedDict[tuple, List[List[int]]]" = OrderedDict()
_EDGES: "OrderedDict[tuple, object]" = OrderedDict()
_INFLIGHT: Dict[tuple, Future] = {}
_CACHE_LOCK = threading.Lock()
_COMPUTE_LOCK = threading.Lock()


def _single_flight(cache: "OrderedDict", max_entries: int, key: tuple, compute: Callable[[], object]):
    with _CACHE_LOCK:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        waiting = _INFLIGHT.get(key)
        if waiting is None:
            mine = _INFLIGHT[key] = Future()
    if waiting is not None:
        return waiting.result()  # the result of the request that computes it, or its error
    try:
        value = compute()
    except BaseException as e:
        with _CACHE_LOCK:
            _INFLIGHT.pop(key, None)
        mine.set_exception(e)
        raise
    with _CACHE_LOCK:
        cache[key] = value
        while len(cache) > max_entries:
            cache.popitem(last=False)
        _INFLIGHT.pop(key, None)
    mine.set_result(value)
    return value


def _digest(*parts) -> str:
    import numpy as np

    h = hashlib.blake2b(digest_size=16)
    for part in parts:
        h.update(part if isinstance(part, bytes) else np.ascontiguousarray(part).tobytes())
    return h.hexdigest()


def _near_labels(phash_sub, threshold: int):
    """Component label per hash (smallest member index) of the graph "distance <= threshold"."""

    def compute_edges():
        with _COMPUTE_LOCK:
            return image_hash.near_edges(phash_sub, MAX_THRESHOLD, max_edges=MAX_EDGES)

    edges = _single_flight(_EDGES, EDGE_CACHE_MAX, ("edges", _digest(phash_sub)), compute_edges)
    if edges is not None:
        return image_hash.components_from_edges(len(phash_sub), edges, threshold)
    with _COMPUTE_LOCK:  # too dense to keep the edges
        return image_hash.near_components(phash_sub, threshold)


def _compute_groups(shas: List[str], phash, has_phash, kind: str, threshold: int) -> List[List[int]]:
    """All groups of two or more as lists of indices into the hashed images, largest first, then smallest id."""
    import numpy as np

    if kind == "pixel":
        by_sha: Dict[str, List[int]] = {}
        for idx, sha in enumerate(shas):
            by_sha.setdefault(sha, []).append(idx)
        groups = [m for m in by_sha.values() if len(m) >= 2]
    else:
        sub = np.flatnonzero(has_phash)  # flat images have no pHash and are not compared
        groups = []
        for members in image_hash.split_groups(_near_labels(phash[sub], threshold)).values():
            members = sub[members].tolist()
            if len({shas[i] for i in members}) >= 2:  # purely pixel-identical groups are pixel groups
                groups.append(members)
    # ids are ascending, so the smallest member index is the smallest image id
    groups.sort(key=lambda m: (-len(m), min(m)))
    return groups


def compute_groups(
    conn,
    kind: str,
    threshold: int,
    min_size: int,
    offset: int,
    limit: int,
    path_ok: Optional[Callable[[str], bool]] = None,
) -> Dict:
    import numpy as np

    ids, shas, phash, has_phash = _load_hashed(conn, path_ok)
    digest = _digest(np.asarray(ids), "".join(shas).encode(), phash, has_phash)
    key = ("groups", kind, threshold if kind == "near" else 0, digest)
    all_groups = _single_flight(
        _GROUPS, CACHE_MAX, key, lambda: _compute_groups(shas, phash, has_phash, kind, threshold)
    )
    # The counts and the paging position are those of this list. A page drops the members whose file is
    # gone (or whose row was deleted meanwhile) and the groups that fall below min_size, but the offset
    # moves on by the groups of the page as they are in the list, so paging visits every group exactly once.
    groups = [m for m in all_groups if len(m) >= min_size]
    page = groups[offset : offset + limit]
    loaded = _load_members(conn, [ids[i] for m in page for i in m])

    out = []
    for group in page:
        members = [i for i in group if ids[i] in loaded and os.path.exists(loaded[ids[i]][0].path)]
        if len(members) < min_size:
            continue
        rows = [loaded[ids[i]] for i in members]  # (image, gray)
        gray = np.stack([np.frombuffer(g or bytes(image_hash.GRAY_BYTES), np.uint8) for _img, g in rows])
        order = image_hash.order_group(gray, [img.path for img, _g in rows])
        ordered = [members[o] for o in order]
        images = [rows[o][0] for o in order]
        out.append(
            {
                # stable: from the whole group, not from the members that are left
                "id": f"pixel:{shas[group[0]][:16]}" if kind == "pixel" else f"near:{ids[min(group)]}",
                "kind": kind,
                "size": len(ordered),
                "max_distance": 0 if kind == "pixel" else image_hash.max_pairwise_distance(phash[ordered]),
                "images": [img.to_file_info() for img in images[:PREVIEW_IMAGES]],
                "paths": [img.path for img in images],
            }
        )
    counts = _state_counts(conn, path_ok)
    next_offset = offset + len(page)
    return {
        "kind": kind,
        "threshold": threshold,
        "group_count": len(groups),
        "image_count": sum(len(m) for m in groups),
        "offset": offset,
        "next_offset": next_offset,
        "has_more": next_offset < len(groups),
        "hashed": counts["hashed"],
        "pending": counts["pending"],
        "groups": out,
    }


# ---------------------------------------------------------------- image variants (from the metadata)


class VariantMeta(NamedTuple):
    prompt: tuple  # (positive prompt without <lora:...>, negative prompt, ((lora name, weight), ...) sorted)
    seed: str
    size: str
    params: tuple  # ((key, value), ...) sorted: the parameter line without Seed, Size and Source Identifier


class VariantGroup(NamedTuple):
    id: str  # "seed:<16 hex>" / "params:<16 hex>" of the group key: stable while members disappear
    members: List[int]  # indices into the scanned images, ascending (= image id order)
    variant: List[int]  # per member: number of its seed (seed) or parameter set (params) in the group, 0, 1, ...
    prompt: str  # positive prompt, whitespace collapsed, at most PROMPT_PREVIEW characters


def _lora_weight(w: Optional[str]) -> str:
    """'0.80' -> '0.8', missing -> '1'; several weights ('1:0.5') part by part, text as it is."""
    parts = []
    for p in (w or "1").split(":"):
        p = p.strip()
        try:
            p = format(float(p), "g")
        except ValueError:
            pass
        parts.append(p)
    return ":".join(parts)


def parse_variant_meta(exif: Optional[str]) -> Optional[VariantMeta]:
    """The parts of the A1111 form that identify a generation; None without a parameter line or a positive prompt.

    The parameter line is the last line and starts with "Steps:". The prompt stays raw text (line breaks and
    spaces change the image); LoRAs are taken out of the positive prompt and kept as sorted (name, weight).
    Seed and size are looked up regardless of case; a missing one is "".
    """
    text = (exif or "").strip()
    extra = _EXTRA_JSON_RE.search(text)
    if extra:
        text = text[: extra.start()].rstrip()
    lines = text.split("\n")
    if not lines[-1].startswith("Steps:"):
        return None
    params = {}
    for k, v in re_param.findall(lines[-1]):
        params[k.strip()] = unquote(v.strip())
    if extra:
        params["extraJsonMetaInfo"] = extra.group(1)
    pos, _, neg = "\n".join(lines[:-1]).partition("\nNegative prompt:")
    if pos.startswith("Negative prompt:"):  # no positive prompt
        pos, neg = "", pos[len("Negative prompt:") :]
    loras = tuple(sorted((name.strip(), _lora_weight(w)) for name, w in _LORA_RE.findall(pos)))
    prompt = _LORA_RE.sub("", pos).strip()
    if not prompt:
        return None
    named = {k.lower(): v for k, v in params.items()}
    return VariantMeta(
        (prompt, neg.strip(), loras),
        named.get("seed", ""),
        named.get("size", ""),
        tuple(sorted((k, v) for k, v in params.items() if k.lower() not in _PARAM_SKIP)),
    )


def _short_prompt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= PROMPT_PREVIEW else text[: PROMPT_PREVIEW - 1] + "…"


def _group_id(kind: str, key) -> str:
    return f"{kind}:{hashlib.blake2b(json.dumps(key).encode(), digest_size=8).hexdigest()}"


def group_variants(metas: Sequence[Optional[VariantMeta]], kind: str) -> List[VariantGroup]:
    """All variant groups with two or more variants, largest first, then smallest index.

    seed: same prompt, at least two different seeds (min_seeds filters later); params: same prompt, seed and
    size, at least two different parameter sets. None (no prompt) takes no part, nor does an image without a
    seed: it is neither one of the seeds nor known to have the same seed (the built-in ComfyUI parser writes none).
    """
    buckets: Dict[tuple, List[int]] = {}
    for idx, m in enumerate(metas):
        if m is None or not m.seed:
            continue
        key = m.prompt if kind == "seed" else (m.prompt, m.seed, m.size)
        buckets.setdefault(key, []).append(idx)
    groups = []
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        numbers: Dict[object, int] = {}
        variant = [
            numbers.setdefault(metas[i].seed if kind == "seed" else metas[i].params, len(numbers)) for i in members
        ]
        if len(numbers) >= 2:
            groups.append(VariantGroup(_group_id(kind, key), members, variant, _short_prompt(metas[members[0]].prompt[0])))
    groups.sort(key=lambda g: (-len(g.members), g.members[0]))
    return groups


def order_variant_members(rows: Sequence[Tuple[Image, Optional[bytes]]]) -> List[int]:
    """Order of a variant group: by correlation of the gray thumbnails (medoid first, see image_hash.order_group)
    for the members that have one, then the others by date and path; with fewer than two thumbnails all by date."""
    import numpy as np

    with_gray = [i for i, (_img, g) in enumerate(rows) if g is not None and len(g) == image_hash.GRAY_BYTES]
    if len(with_gray) < 2:
        with_gray = []
    rest = sorted(set(range(len(rows))) - set(with_gray), key=lambda i: (rows[i][0].date or "", rows[i][0].path))
    if not with_gray:
        return rest
    gray = np.stack([np.frombuffer(rows[i][1], np.uint8) for i in with_gray])
    order = image_hash.order_group(gray, [rows[i][0].path for i in with_gray])
    return [with_gray[o] for o in order] + rest


def _norm_folders(folder_paths: Optional[List[str]]) -> List[str]:
    return sorted({os.path.normpath(p.strip()) for p in folder_paths or [] if p and p.strip()})


def _in_folders(path: str, folders: List[str]) -> bool:
    if not folders:
        return True
    p = os.path.normpath(path)
    return any(p == f or p.startswith(f.rstrip(os.sep) + os.sep) for f in folders)


def _variant_rows(conn, keep: Callable[[str], bool]):
    """(id, path, exif) of the images keep() accepts, in id order, read in chunks (the exif column is large)."""
    last = -1
    while True:
        rows = conn.execute(
            "SELECT id, path, exif FROM image WHERE id > ? ORDER BY id LIMIT ?", (last, VARIANT_CHUNK)
        ).fetchall()
        if not rows:
            return
        last = rows[-1][0]
        for row in rows:
            if keep(row[1]):
                yield row


def _scan_variants(conn, keep: Callable[[str], bool]):
    """(ids, paths, digest of ids and exif) of the images to group."""
    ids: List[int] = []
    paths: List[str] = []
    h = hashlib.blake2b(digest_size=16)
    for image_id, path, exif in _variant_rows(conn, keep):
        ids.append(image_id)
        paths.append(path)
        h.update(str(image_id).encode())
        h.update(b"\0")
        h.update((exif or "").encode("utf-8", "surrogatepass"))
        h.update(b"\1")
    return ids, paths, h.hexdigest()


def _parse_variants(conn, keep: Callable[[str], bool], ids: List[int]) -> List[Optional[VariantMeta]]:
    """The metadata of the scanned images, aligned with ids (a row deleted meanwhile counts as without prompt)."""
    index = {image_id: k for k, image_id in enumerate(ids)}
    metas: List[Optional[VariantMeta]] = [None] * len(ids)
    for image_id, _path, exif in _variant_rows(conn, keep):
        k = index.get(image_id)
        if k is not None:
            metas[k] = parse_variant_meta(exif)
    return metas


# the variant groups of both kinds and the count without prompt per digest of the scanned images (see the caches above)
_VARIANTS: "OrderedDict[tuple, Tuple[int, Dict[str, List[VariantGroup]]]]" = OrderedDict()


def compute_variants(
    conn,
    kind: str,
    min_seeds: int,
    folders: List[str],
    multi_folder: bool,
    offset: int,
    limit: int,
    path_ok: Optional[Callable[[str], bool]] = None,
) -> Dict:
    def keep(path) -> bool:
        return not _is_video_or_audio(path) and (path_ok is None or path_ok(path)) and _in_folders(path, folders)

    ids, paths, digest = _scan_variants(conn, keep)

    def compute():
        with _COMPUTE_LOCK:
            metas = _parse_variants(conn, keep, ids)
            return sum(m is None for m in metas), {k: group_variants(metas, k) for k in VARIANT_KINDS}

    no_prompt, by_kind = _single_flight(_VARIANTS, VARIANT_CACHE_MAX, ("variants", digest), compute)
    need = min_seeds if kind == "seed" else 2
    groups = [
        g
        for g in by_kind[kind]
        if max(g.variant) + 1 >= need
        and (not multi_folder or len({os.path.dirname(paths[i]) for i in g.members}) > 1)
    ]
    # counts and paging as in compute_groups: a page leaves out members whose file is gone and groups that fall
    # below two images or below the needed variants; the offset moves on by the groups of the list
    page = groups[offset : offset + limit]
    loaded = _load_members(conn, [ids[i] for g in page for i in g.members])
    out = []
    for g in page:
        kept = [
            (i, v)
            for i, v in zip(g.members, g.variant)
            if ids[i] in loaded and os.path.exists(loaded[ids[i]][0].path)
        ]
        variant_count = len({v for _i, v in kept})
        if len(kept) < 2 or variant_count < need:
            continue
        rows = [loaded[ids[i]] for i, _v in kept]
        images = [rows[o][0] for o in order_variant_members(rows)]
        out.append(
            {
                "id": g.id,
                "kind": kind,
                "size": len(images),
                "variant_count": variant_count,
                "prompt": g.prompt,
                "images": [img.to_file_info() for img in images[:PREVIEW_IMAGES]],
                "paths": [img.path for img in images],
            }
        )
    next_offset = offset + len(page)
    return {
        "kind": kind,
        "min_seeds": min_seeds,
        "group_count": len(groups),
        "image_count": sum(len(g.members) for g in groups),
        "offset": offset,
        "next_offset": next_offset,
        "has_more": next_offset < len(groups),
        "total": len(ids),
        "no_prompt": no_prompt,
        "groups": out,
    }


# ---------------------------------------------------------------- routes


class ImageGroupsReq(BaseModel):
    kind: str = "pixel"
    threshold: int = 8
    min_size: int = 2
    offset: int = 0
    limit: int = 200


class ImageGroupsCancelReq(BaseModel):
    job_id: Optional[str] = None


class ImageVariantsReq(BaseModel):
    kind: str = "seed"
    min_seeds: int = DEFAULT_MIN_SEEDS
    folder_paths: Optional[List[str]] = None
    multi_folder: bool = False
    offset: int = 0
    limit: int = 200


def mount_image_groups_routes(
    app: FastAPI,
    db_api_base: str,
    verify_secret,
    write_permission_required,
    is_path_trusted: Optional[Callable[[str], bool]] = None,
):
    """is_path_trusted: with access control, the check of api.py; images outside the allowed paths are
    neither hashed, counted, grouped nor returned. None (no access control) lets everything pass."""

    @app.get(f"{db_api_base}/image_groups_state", dependencies=[Depends(verify_secret)])
    def image_groups_state():
        _ensure_deps()
        counts = _state_counts(DataBase.get_conn(), is_path_trusted)
        j = _active_job()
        job = {k: j[k] for k in ("job_id", "status", "done", "total")} if j else None
        return {**counts, "version": HASH_VERSION, "job": job}

    @app.post(
        f"{db_api_base}/image_groups_job_start",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def image_groups_job_start():
        _ensure_deps()
        j = _active_job()
        if j:
            return {"job_id": j["job_id"], "already_running": True}
        job_id = uuid.uuid4().hex
        _job_update(job_id, status="queued", done=0, total=0, skipped=0, error=None)
        _ACTIVE["job_id"] = job_id
        task = asyncio.create_task(_run_job(job_id, is_path_trusted))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return {"job_id": job_id, "already_running": False}

    @app.post(
        f"{db_api_base}/image_groups_job_cancel",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def image_groups_job_cancel(req: Optional[ImageGroupsCancelReq] = None):
        """Stop starting new batches; the running ones finish and what is written stays."""
        job_id = req.job_id if req and req.job_id else _ACTIVE["job_id"]
        j = _job_get(job_id)
        if not j or j["status"] not in ("queued", "running"):
            return {"cancelled": False}
        _job_update(job_id, cancel_requested=True)
        return {"cancelled": True}

    @app.get(f"{db_api_base}/image_groups_job_status", dependencies=[Depends(verify_secret)])
    def image_groups_job_status(job_id: str):
        j = _job_get(job_id)
        if not j:
            raise HTTPException(status_code=404, detail="job not found")
        return {k: j[k] for k in ("job_id", "status", "done", "total", "error", "skipped")}

    @app.post(f"{db_api_base}/image_groups", dependencies=[Depends(verify_secret)])
    def image_groups(req: ImageGroupsReq):
        _ensure_deps()
        if req.kind not in ("pixel", "near"):
            raise HTTPException(status_code=422, detail="kind must be 'pixel' or 'near'")
        return compute_groups(
            DataBase.get_conn(),
            req.kind,
            max(0, min(MAX_THRESHOLD, req.threshold)),
            max(2, req.min_size),
            max(0, req.offset),
            max(1, min(1000, req.limit)),
            is_path_trusted,
        )

    @app.post(f"{db_api_base}/image_variants", dependencies=[Depends(verify_secret)])
    def image_variants(req: ImageVariantsReq):
        _ensure_deps()  # numpy orders the members, as in the other routes
        if req.kind not in VARIANT_KINDS:
            raise HTTPException(status_code=422, detail="kind must be 'seed' or 'params'")
        return compute_variants(
            DataBase.get_conn(),
            req.kind,
            max(2, min(MAX_MIN_SEEDS, req.min_seeds)),
            _norm_folders(req.folder_paths),
            req.multi_folder,
            max(0, req.offset),
            max(1, min(1000, req.limit)),
            is_path_trusted,
        )
