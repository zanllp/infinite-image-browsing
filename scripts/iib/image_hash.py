"""
Image hashing and grouping for the image groups (pixel duplicates and near duplicates).

Pillow + numpy, no database. numpy is optional in IIB, so it is imported inside the functions that
need it. The routes and the job live in ``scripts/iib/image_groups.py``.

Procedure (change HASH_VERSION whenever a step changes: stored hashes of an older version are recomputed)
- Open with Pillow, first frame of animations. The EXIF orientation is NOT applied: a rotated copy of
  an image is a different image.
- Normalise to 8 bit RGB: alpha is composited on white, palette / L / LA / CMYK are converted, 16 bit
  modes are scaled to 8 bit.
- pixel_sha: SHA-256 over ``b"{width}x{height}:"`` + the RGB bytes (decoded pixels, not the file).
- gray: the RGB image as L, resized with LANCZOS to 32x32 (1024 bytes).
- phash: DCT-II of the gray values, the upper left 8x8 coefficients, bit = coefficient > median of
  them without the DC coefficient, 64 bit (stored as signed int64). Images whose gray thumbnail has a
  standard deviation below FLAT_STD get no phash: their bits would be noise, so they only take part
  in pixel groups.

The pHash of very smooth gradients can change by a few bits between encodings of the same picture
(the DCT coefficients sit close to their median), so such copies may need a larger threshold.
"""
from __future__ import annotations

import hashlib
import threading
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image

HASH_VERSION = 1
GRAY_SIDE = 32
GRAY_BYTES = GRAY_SIDE * GRAY_SIDE
FLAT_STD = 2.0
_STRIP_BYTES = 4 << 20

# The job decodes in threads of the server process. Images over MAX_PIXELS are refused (stored as
# unreadable) and images over BIG_PIXELS are decoded one at a time: an out-of-memory situation is not
# an exception on Linux (the OOM killer ends the process), so these limits keep a huge file from taking
# the server with it.
MAX_PIXELS = 64_000_000
BIG_PIXELS = 12_000_000
_BIG_LOCK = threading.Lock()

# Pillow's warnings (big images, slightly broken files) are left alone: warnings.catch_warnings is not
# thread-safe and a process-wide filter would hide the warnings of other code.


# ---------------------------------------------------------------- hashing of one image


@lru_cache(maxsize=None)
def _dct():
    import numpy as np

    k = np.arange(GRAY_SIDE).reshape(-1, 1)
    i = np.arange(GRAY_SIDE).reshape(1, -1)
    return np.cos(np.pi * (2 * i + 1) * k / (2 * GRAY_SIDE))


def _phash(gray) -> int:
    """64 bit pHash (as signed int64) of 32x32 grayscale values."""
    import numpy as np

    d = _dct()
    g = np.asarray(gray, dtype=np.float64).reshape(GRAY_SIDE, GRAY_SIDE)
    # rounding keeps the noise of flat images (all AC coefficients ~1e-13) from flipping bits
    low = np.round((d @ g @ d.T)[:8, :8].reshape(-1), 6)
    value = sum(1 << int(i) for i in np.flatnonzero(low > np.median(low[1:])))
    return value - (1 << 64) if value >= (1 << 63) else value


def normalize_rgb(im: Image.Image) -> Image.Image:
    """8 bit RGB version of a loaded image (alpha on white, palette and 16 bit converted)."""
    import numpy as np

    mode = im.mode
    if mode.startswith("I;16") or mode in ("I", "F"):
        arr = np.asarray(im)
        if mode == "F":
            v = np.rint(np.clip(arr, 0, 255))
        else:
            v = (np.clip(arr, 0, 65535).astype(np.uint32) + 128) // 257
        return Image.fromarray(v.astype(np.uint8), "L").convert("RGB")
    if mode in ("RGBA", "LA", "PA", "RGBa", "La") or (mode == "P" and "transparency" in im.info):
        rgba = im if mode == "RGBA" else im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return im if mode == "RGB" else im.convert("RGB")


def hash_pixels(rgb: Image.Image) -> Tuple[str, bytes, Optional[int]]:
    """(pixel_sha, gray bytes, signed phash or None for a flat image) of a normalised RGB image."""
    import numpy as np

    w, h = rgb.size
    # hashing the rows in strips gives the digest of header + rgb.tobytes() without a second copy of the image
    digest = hashlib.sha256(f"{w}x{h}:".encode("ascii"))
    strip = max(1, _STRIP_BYTES // max(1, w * 3))
    for y in range(0, h, strip):
        digest.update(rgb.crop((0, y, w, min(h, y + strip))).tobytes())
    gray = rgb.convert("L").resize((GRAY_SIDE, GRAY_SIDE), Image.LANCZOS).tobytes()
    arr = np.frombuffer(gray, dtype=np.uint8)
    return digest.hexdigest(), gray, None if float(arr.std()) < FLAT_STD else _phash(arr)


def _hash_file(path: str) -> Tuple[str, bytes, Optional[int]]:
    with Image.open(path) as im:
        im.seek(0)
        im.load()
        return hash_pixels(normalize_rgb(im))


def hash_image(path: str) -> Tuple[str, bytes, Optional[int]]:
    """Hash the image at path. Raises on files Pillow cannot decode."""
    with Image.open(path) as im:  # the header only, nothing is decoded yet
        pixels = im.width * im.height
    if pixels > MAX_PIXELS:
        raise Image.DecompressionBombError(f"image too large: {pixels} pixels")
    if pixels <= BIG_PIXELS:
        return _hash_file(path)
    # a big image is opened only after the lock is held, so no file stays open while it waits
    with _BIG_LOCK:
        return _hash_file(path)


def is_transient_error(e: BaseException) -> bool:
    """A problem of the moment (not stored, tried again by a later job) rather than of the file?

    I/O errors with an errno (missing file, no permission), timeouts and out of memory are transient;
    everything else comes from the decoder and is stored as a failure of the image.
    """
    if isinstance(e, (MemoryError, TimeoutError, ConnectionError)):
        return True
    return isinstance(e, OSError) and e.errno is not None  # Pillow raises plain OSError("image file is truncated")


def hash_batch(items: Sequence[Tuple[int, str]]) -> List[tuple]:
    """[(image_id, path)] -> [(image_id, status, sha, phash, gray, error)].

    status is 'ok', 'fail' (the file does not decode, error says why) or 'skip' (a transient error).
    """
    out = []
    for image_id, path in items:
        try:
            sha, gray, ph = hash_image(path)
            out.append((image_id, "ok", sha, ph, gray, None))
        except Exception as e:
            status = "skip" if is_transient_error(e) else "fail"
            out.append((image_id, status, None, None, None, f"{type(e).__name__}: {e}"[:300]))
    return out


# ---------------------------------------------------------------- near duplicates


def popcount64_swar(x):
    """Number of set bits per uint64 element without np.bitwise_count (numpy before 2.0)."""
    import numpy as np

    u = np.uint64
    x = np.ascontiguousarray(x, dtype=np.uint64)
    t = x >> u(1)
    t &= u(0x5555555555555555)
    x = x - t
    t = x >> u(2)
    t &= u(0x3333333333333333)
    x &= u(0x3333333333333333)
    x += t
    t = x >> u(4)
    x += t
    x &= u(0x0F0F0F0F0F0F0F0F)
    x *= u(0x0101010101010101)  # wraps around by design
    x >>= u(56)
    return x.astype(np.uint8)


def popcount64(x):
    """Number of set bits per uint64 element."""
    import numpy as np

    count = getattr(np, "bitwise_count", None) or popcount64_swar  # bitwise_count: numpy >= 2.0
    return count(x)


def _as_uint64(phash):
    import numpy as np

    return np.ascontiguousarray(phash).astype(np.int64).view(np.uint64)


def _close_pairs(phash, max_distance: int, block_elems: int):
    """Yield (i, j, distance) arrays of the pairs i < j with Hamming distance <= max_distance.

    The pairs are compared in blocks of at most block_elems elements, so memory stays bounded.
    """
    import numpy as np

    h = _as_uint64(phash)
    n = len(h)
    block = max(1, min(n, block_elems // max(1, n)))
    for i0 in range(0, n, block):
        dist = popcount64(h[i0 : i0 + block, None] ^ h[None, i0:])
        ia, jb = np.nonzero(dist <= max_distance)
        keep = jb > ia  # column jb is node i0 + jb, row ia is node i0 + ia
        ia, jb = ia[keep], jb[keep]
        yield ia + i0, jb + i0, dist[ia, jb]


def _union_edges(label, a, b) -> None:
    """Merge the components of the edges (a, b). label[i] is the smallest node of i's component."""
    import numpy as np

    while True:
        la, lb = label[a], label[b]
        diff = la != lb
        if not diff.any():
            return
        a, b, la, lb = a[diff], b[diff], la[diff], lb[diff]
        np.minimum.at(label, np.maximum(la, lb), np.minimum(la, lb))
        while True:  # pointer jumping until every label is a root
            nl = label[label]
            if np.array_equal(nl, label):
                break
            label[:] = nl


def near_components(phash, threshold: int, block_elems: int = 1_000_000):
    """Connected components of the graph "Hamming distance <= threshold" of an int64 array.

    Returns the smallest member index of each node's component.
    """
    import numpy as np

    label = np.arange(len(phash), dtype=np.int64)
    for ia, jb, _d in _close_pairs(phash, threshold, block_elems):
        _union_edges(label, ia, jb)
    return label


def near_edges(phash, max_distance: int, block_elems: int = 1_000_000, max_edges: int = 5_000_000):
    """All pairs (i < j) with Hamming distance <= max_distance: (i, j, distance) as int32, int32, uint8.

    None when there are more than max_edges pairs (the graph is too dense to keep).
    """
    import numpy as np

    parts = [(np.empty(0, np.int32), np.empty(0, np.int32), np.empty(0, np.uint8))]
    total = 0
    for ia, jb, d in _close_pairs(phash, max_distance, block_elems):
        total += len(ia)
        if total > max_edges:
            return None
        parts.append((ia.astype(np.int32), jb.astype(np.int32), d))
    return tuple(np.concatenate(p) for p in zip(*parts))


def components_from_edges(n: int, edges, threshold: int):
    """near_components for the edges of near_edges with distance <= threshold."""
    import numpy as np

    ia, jb, d = edges
    label = np.arange(n, dtype=np.int64)
    keep = d <= threshold
    _union_edges(label, ia[keep], jb[keep])
    return label


def max_pairwise_distance(phash, limit: int = 3000) -> int:
    """Largest Hamming distance between two of the hashes (over the first `limit` ones)."""
    h = _as_uint64(phash[:limit])
    block = max(1, 1_000_000 // max(1, len(h)))
    blocks = (popcount64(h[i0 : i0 + block, None] ^ h[None, :]) for i0 in range(0, len(h), block))
    return max((int(d.max()) for d in blocks), default=0)


# ---------------------------------------------------------------- order within a group


ORDER_SAMPLE = 1000
_ORDER_BLOCK = 512


def _normalized(gray):
    """Rows mean-free and of unit length (float32); rows without variance become zero."""
    import numpy as np

    z = gray.astype(np.float32)
    z -= z.mean(axis=1, keepdims=True)
    norm = np.linalg.norm(z, axis=1)
    ok = norm > 0
    z[ok] /= norm[ok, None]
    z[~ok] = 0.0
    return z


def order_group(gray, keys: Sequence[str]) -> List[int]:
    """Order of the members of a group: medoid first, then by falling correlation to the medoid.

    gray: (k, 1024) uint8 thumbnails. The medoid is the member with the highest mean Pearson
    correlation to the others, picked from an evenly spaced sample of ORDER_SAMPLE members in larger
    groups. Ties (to 5 decimals) are broken by keys (the paths).
    """
    import numpy as np

    k = len(gray)
    if k <= 1:
        return list(range(k))
    sample = np.arange(k) if k <= ORDER_SAMPLE else np.unique(np.linspace(0, k - 1, ORDER_SAMPLE).astype(np.int64))
    zs = _normalized(gray[sample])
    m = len(sample)
    mean_corr = (zs @ zs.sum(axis=0, dtype=np.float64).astype(np.float32) - np.einsum("ij,ij->i", zs, zs)) / (m - 1)
    best = min(range(m), key=lambda i: (-round(float(mean_corr[i]), 5), keys[int(sample[i])]))
    medoid = int(sample[best])
    corr = np.empty(k, dtype=np.float32)
    for i0 in range(0, k, _ORDER_BLOCK):  # in blocks, so the memory stays bounded
        corr[i0 : i0 + _ORDER_BLOCK] = _normalized(gray[i0 : i0 + _ORDER_BLOCK]) @ zs[best]
    rest = sorted((i for i in range(k) if i != medoid), key=lambda i: (-round(float(corr[i]), 5), keys[i]))
    return [medoid] + rest


def split_groups(label) -> Dict[int, List[int]]:
    """{component label: [member indices]} for every component of size >= 2."""
    import numpy as np

    if len(label) == 0:
        return {}
    order = np.argsort(label, kind="stable")
    bounds = np.flatnonzero(np.diff(label[order])) + 1
    return {int(label[chunk[0]]): chunk.tolist() for chunk in np.split(order, bounds) if len(chunk) >= 2}
