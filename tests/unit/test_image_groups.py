"""Tests for the image groups: image hash, grouping, the hash job and its routes.

The hash procedure and the grouping run on synthetic images and hashes. The job and the routes are
driven through a small FastAPI app with ``TestClient`` on a temporary SQLite database.

Run from the repository root::

    python -m unittest tests/unit/test_image_groups.py
"""
import ast
import hashlib
import inspect
import io
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from collections import OrderedDict
from contextlib import ExitStack, closing, contextmanager
from unittest.mock import patch

import numpy as np
from PIL import Image, PngImagePlugin

# DataBase.path comes from IIB_DB_PATH when datamodel is imported; set it first.
_TMP_ROOT = tempfile.mkdtemp(prefix="iib_image_groups_")
_DB_PATH = os.path.join(_TMP_ROOT, "iib.db")
os.environ["IIB_DB_PATH"] = _DB_PATH
os.environ["IIB_DB_FILE_BACKUP_DELAY"] = "9999"

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from scripts.iib import image_groups, image_hash
from scripts.iib.db.datamodel import DataBase, Image as DbImg, ImageHash

DB_API = "/infinite_image_browsing/db"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_saved_db_path = DataBase.path


def _close_conn():
    conn = getattr(DataBase.local, "conn", None)
    if conn is not None:
        conn.close()
        del DataBase.local.conn


def setUpModule():
    global _saved_db_path
    _close_conn()
    _saved_db_path = DataBase.path
    DataBase.path = _DB_PATH


def tearDownModule():
    _close_conn()
    DataBase.path = _saved_db_path if _saved_db_path != _DB_PATH else os.environ["IIB_DB_PATH"]
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


# ---------------------------------------------------------------- synthetic images and hashes


def motif(seed: int, side: int = 96) -> Image.Image:
    """A smooth, structured RGB image (a few random blobs); different seeds give different motifs."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:side, 0:side].astype(np.float64) / side
    img = np.zeros((side, side, 3))
    for _ in range(6):
        cx, cy = rng.random(2)
        r = 0.08 + 0.25 * rng.random()
        img += np.exp(-(((x - cx) ** 2 + (y - cy) ** 2) / (2 * r * r)))[..., None] * rng.random(3) * 255
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")


def noisy(im: Image.Image, sigma: float = 3.0, seed: int = 99) -> Image.Image:
    arr = np.asarray(im).astype(np.float64)
    arr += np.random.default_rng(seed).normal(0, sigma, arr.shape)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def hash_of(im: Image.Image):
    return image_hash.hash_pixels(image_hash.normalize_rgb(im))


def reload(im: Image.Image, fmt: str, **kw) -> Image.Image:
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    buf.seek(0)
    out = Image.open(buf)
    out.load()
    return out


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")


def clustered_hashes(seed: int, centers: int = 12, max_flips: int = 7):
    """Signed 64 bit hashes in clusters of 1 to 8 around random centers, shuffled."""
    rng = np.random.default_rng(seed)
    hashes = []
    for c in rng.integers(0, 1 << 63, centers, dtype=np.int64):
        for _ in range(int(rng.integers(1, 9))):
            v = int(c)
            for b in rng.choice(64, int(rng.integers(0, max_flips)), replace=False):
                v ^= 1 << int(b)
            hashes.append(v - (1 << 64) if v >= 1 << 63 else v)
    return [hashes[i] for i in rng.permutation(len(hashes))]


def gen(prompt="a red fox", seed=1, size="512x512", steps=20, neg="", extra=""):
    """An exif text in the A1111 form."""
    lines = [prompt] + ([f"Negative prompt: {neg}"] if neg else [])
    lines.append(
        f"Steps: {steps}, Sampler: Euler, CFG scale: 7, Seed: {seed}, Size: {size}, Model: m1{extra}, "
        "Source Identifier: ComfyUI"
    )
    return "\n".join(lines)


def components(label):
    comps = {}
    for i, l in enumerate(list(label)):
        comps.setdefault(l, []).append(i)
    return sorted(comps.values())


def brute_components(hashes, threshold):
    parent = list(range(len(hashes)))

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            if hamming(hashes[i], hashes[j]) <= threshold:
                parent[find(j)] = find(i)
    return components([find(i) for i in range(len(hashes))])


# ---------------------------------------------------------------- hash procedure


class HashProcedureTest(unittest.TestCase):
    def path(self, name):
        return os.path.join(_TMP_ROOT, name)

    def test_pixel_sha_depends_on_the_decoded_pixels_only(self):
        im = motif(1)
        sha = hash_of(im)[0]
        for fmt, kw in (("PNG", {}), ("WEBP", {"lossless": True}), ("BMP", {}), ("TIFF", {})):
            self.assertEqual(hash_of(reload(im, fmt, **kw))[0], sha, fmt)
        meta = PngImagePlugin.PngInfo()
        meta.add_text("parameters", "a different prompt")
        im.save(self.path("a.png"))
        im.save(self.path("other name.png"), pnginfo=meta)
        self.assertNotEqual(os.path.getsize(self.path("a.png")), os.path.getsize(self.path("other name.png")))
        self.assertEqual(image_hash.hash_image(self.path("a.png")), image_hash.hash_image(self.path("other name.png")))
        changed = im.copy()
        changed.putpixel((3, 3), (1, 2, 3))
        self.assertNotEqual(hash_of(changed)[0], sha)
        # the same bytes in another shape
        self.assertNotEqual(hash_of(Image.new("RGB", (4, 6), (9, 9, 9)))[0], hash_of(Image.new("RGB", (6, 4), (9, 9, 9)))[0])
        # hashed in strips of any size: the digest of the header and all pixels
        part = motif(15, 50).crop((0, 0, 37, 29))
        expected = hashlib.sha256(b"37x29:" + part.tobytes()).hexdigest()
        for strip in (image_hash._STRIP_BYTES, 100, 1):
            with patch.object(image_hash, "_STRIP_BYTES", strip):
                self.assertEqual(hash_of(part)[0], expected, strip)

    def test_modes_are_normalised_to_8_bit_rgb(self):
        im = motif(4)
        self.assertEqual(hash_of(im.convert("RGBA"))[0], hash_of(im)[0])
        # alpha is composited on white
        clear = Image.new("RGBA", (16, 16), (255, 0, 0, 0))
        self.assertEqual(hash_of(clear)[0], hash_of(Image.new("RGB", (16, 16), (255, 255, 255)))[0])
        half = Image.new("RGBA", (16, 16), (0, 0, 0, 128))
        self.assertIn(image_hash.normalize_rgb(half).getpixel((0, 0))[0], (127, 128))
        pal = Image.new("P", (8, 8), 0)
        pal.putpalette([255, 0, 0] + [0, 0, 0] * 255)
        pal.info["transparency"] = 0
        self.assertEqual(image_hash.normalize_rgb(pal).getpixel((0, 0)), (255, 255, 255))
        for mode in ("P", "L", "LA", "CMYK", "1"):
            sha, gray, ph = hash_of(im.convert(mode))
            self.assertEqual((len(sha), len(gray)), (64, image_hash.GRAY_BYTES), mode)
            self.assertTrue(-(1 << 63) <= ph < (1 << 63), mode)
        self.assertEqual(hash_of(im.convert("L"))[0], hash_of(im.convert("L").convert("RGB"))[0])
        # 16 and 32 bit modes are scaled to 8 bit
        values = np.array([[0, 257, 128 * 257, 65535]], dtype=np.uint16)
        for wide in (Image.fromarray(values), Image.fromarray(values.astype(np.int32), "I")):
            self.assertEqual([image_hash.normalize_rgb(wide).getpixel((x, 0))[0] for x in range(4)], [0, 1, 128, 255])
        floats = Image.fromarray(np.array([[0.0, 300.0, 100.4]], dtype=np.float32), "F")
        self.assertEqual([image_hash.normalize_rgb(floats).getpixel((x, 0))[0] for x in range(3)], [0, 255, 100])
        path = self.path("sixteen.png")
        Image.fromarray(np.random.default_rng(5).integers(0, 65536, (40, 40), dtype=np.uint16)).save(path)
        self.assertEqual(len(image_hash.hash_image(path)[0]), 64)

    def test_first_frame_of_an_animation_and_no_exif_orientation(self):
        # palette frames, so that no Pillow version dithers them on saving
        first, second = motif(6, 32).quantize(64), motif(7, 32).quantize(64)
        first.save(self.path("anim.gif"), save_all=True, append_images=[second], duration=100, loop=0)
        self.assertEqual(image_hash.hash_image(self.path("anim.gif"))[0], hash_of(first)[0])
        im = motif(8, 64)
        exif = Image.Exif()
        exif[0x0112] = 6  # rotated by 90 degrees
        im.save(self.path("orient.jpg"), "JPEG", quality=95, exif=exif)
        im.save(self.path("plain.jpg"), "JPEG", quality=95)
        self.assertEqual(image_hash.hash_image(self.path("orient.jpg"))[0], image_hash.hash_image(self.path("plain.jpg"))[0])

    def test_phash_is_close_for_variants_far_for_other_motifs_and_none_for_flat_images(self):
        im = motif(10, 160)
        base = hash_of(im)[2]
        for variant in (noisy(im), im.resize((120, 120), Image.BICUBIC), reload(im, "JPEG", quality=60)):
            self.assertLessEqual(hamming(base, hash_of(variant)[2]), 6)
        self.assertGreaterEqual(min(hamming(base, hash_of(motif(s, 160))[2]) for s in range(11, 21)), 12)
        for shade in (0, 255, 90):
            sha, gray, ph = hash_of(Image.new("RGB", (40, 40), (shade,) * 3))
            self.assertEqual((len(sha), len(gray), ph), (64, image_hash.GRAY_BYTES, None))
        # nearly flat (std below FLAT_STD) as well
        faint = Image.fromarray((128 + np.random.default_rng(3).integers(0, 2, (64, 64, 3))).astype(np.uint8), "RGB")
        self.assertIsNone(hash_of(faint)[2])

    def test_hash_batch_tells_failures_of_the_file_from_transient_errors(self):
        good, bad, trunc, big = (self.path(n) for n in ("good.png", "bad.png", "trunc.png", "big.png"))
        motif(12).save(good)
        with open(bad, "wb") as f:
            f.write(b"not an image")
        motif(14, 128).save(trunc)
        with open(trunc, "rb") as f:
            data = f.read()
        with open(trunc, "wb") as f:
            f.write(data[: len(data) // 2])
        res = image_hash.hash_batch([(1, good), (2, bad), (3, self.path("missing.png")), (4, trunc)])
        self.assertEqual([r[1] for r in res], ["ok", "fail", "skip", "fail"])
        self.assertEqual(res[0][2], image_hash.hash_image(good)[0])
        self.assertIn("UnidentifiedImageError", res[1][5])
        self.assertIn("FileNotFoundError", res[2][5])
        Image.new("L", (400, 300)).save(big)
        with patch.object(image_hash, "MAX_PIXELS", 1000):
            self.assertEqual(image_hash.hash_batch([(1, big)])[0][1], "fail")
        # whatever else a decoder raises is a failure with the error text
        with patch.object(image_hash, "_hash_file", side_effect=KeyError("palette")):
            res = image_hash.hash_batch([(1, good)])
        self.assertEqual((res[0][1], res[0][5]), ("fail", "KeyError: 'palette'"))

    def test_which_errors_are_transient(self):
        transient = [
            FileNotFoundError(2, "x"),
            PermissionError(13, "x"),
            OSError(5, "I/O"),
            TimeoutError(),
            MemoryError(),
            ConnectionResetError(),
        ]
        decode_errors = [
            Image.UnidentifiedImageError("x"),
            Image.DecompressionBombError("x"),
            OSError("image file is truncated (3 bytes not processed)"),
            SyntaxError("broken PNG file"),
            EOFError(),
            struct.error("x"),
            ValueError("x"),
            KeyError("x"),
            RuntimeError("x"),
        ]
        self.assertTrue(all(image_hash.is_transient_error(e) for e in transient))
        self.assertFalse(any(image_hash.is_transient_error(e) for e in decode_errors))

    def test_a_big_image_is_opened_only_after_it_holds_the_lock(self):
        path = self.path("mid.png")
        motif(19, 64).save(path)
        real_open = Image.open
        open_files, seen = [0], []

        @contextmanager
        def tracked_open(p, *a, **k):
            open_files[0] += 1
            try:
                with real_open(p, *a, **k) as im:
                    yield im
            finally:
                open_files[0] -= 1

        class WatchedLock:
            def __enter__(self):
                seen.append(open_files[0])

            def __exit__(self, *exc):
                return False

        with patch.object(image_hash, "_BIG_LOCK", WatchedLock()), patch.object(Image, "open", tracked_open):
            image_hash.hash_image(path)  # small: no lock
            with patch.object(image_hash, "BIG_PIXELS", 10):
                image_hash.hash_image(path)
        self.assertEqual((seen, open_files[0]), ([0], 0))

    def test_no_warning_filter_is_touched(self):
        # neither a process-wide filter nor catch_warnings (not thread-safe) in the module
        calls = {
            getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            for n in ast.walk(ast.parse(inspect.getsource(image_hash)))
            if isinstance(n, ast.Call)
        }
        self.assertFalse(calls & {"filterwarnings", "simplefilter", "resetwarnings", "catch_warnings"})
        motif(200, 32).save(self.path("warn.png"))
        before = list(warnings.filters)
        image_hash.hash_image(self.path("warn.png"))
        self.assertEqual(list(warnings.filters), before)


# ---------------------------------------------------------------- grouping


class GroupingTest(unittest.TestCase):
    def test_components_and_edges_match_brute_force_for_any_block_size(self):
        hashes = clustered_hashes(1)
        arr = np.array(hashes, dtype=np.int64)
        expected = {t: brute_components(hashes, t) for t in range(13)}
        for block_elems in (1, 7, 1_000_000):
            edges = image_hash.near_edges(arr, 12, block_elems=block_elems)
            for t in range(13):
                self.assertEqual(components(image_hash.near_components(arr, t, block_elems)), expected[t], (t, block_elems))
                self.assertEqual(components(image_hash.components_from_edges(len(arr), edges, t)), expected[t], t)
        self.assertIsNone(image_hash.near_edges(arr, 12, max_edges=0))

    def test_degenerate_inputs_negative_hashes_and_chains(self):
        self.assertEqual(image_hash.near_components(np.array([], dtype=np.int64), 8).tolist(), [])
        self.assertEqual(image_hash.near_components(np.array([5], dtype=np.int64), 8).tolist(), [0])
        self.assertEqual([(len(x), x.dtype) for x in image_hash.near_edges(np.array([5], dtype=np.int64), 12)],
                         [(0, np.int32), (0, np.int32), (0, np.uint8)])
        self.assertEqual(components(image_hash.near_components(np.array([-1, -1 ^ 1, 0], dtype=np.int64), 1)), [[0, 1], [2]])
        # a chain a - b - c links a and c (8 apart) through b (4 from each)
        chain = np.array([0, 0b11111111, 0b1111], dtype=np.int64)
        self.assertEqual(components(image_hash.near_components(chain, 4)), [[0, 1, 2]])
        self.assertEqual(components(image_hash.near_components(chain, 3)), [[0], [1], [2]])
        self.assertEqual((image_hash.max_pairwise_distance(chain), image_hash.max_pairwise_distance(chain[:1])), (8, 0))
        self.assertEqual(sorted(image_hash.split_groups(np.array([0, 1, 0, 3, 1, 0, 6])).values()), [[0, 2, 5], [1, 4]])
        self.assertEqual(image_hash.split_groups(np.array([], dtype=np.int64)), {})

    def test_popcount_with_and_without_bitwise_count(self):
        x = np.random.default_rng(2).integers(0, 1 << 63, (7, 5), dtype=np.int64).view(np.uint64) | np.uint64(1 << 63)
        x[0, 0], x[0, 1], x[0, 2] = 0, np.uint64(0xFFFFFFFFFFFFFFFF), 1
        expected = np.array([[bin(int(v)).count("1") for v in row] for row in x], dtype=np.uint8)
        swar = image_hash.popcount64_swar(x)
        self.assertEqual(swar.dtype, np.uint8)
        self.assertTrue(np.array_equal(swar, expected))
        self.assertTrue(np.array_equal(image_hash.popcount64(x), expected))
        self.assertEqual(image_hash.popcount64_swar(np.zeros((0,), dtype=np.uint64)).shape, (0,))
        # numpy before 2.0 (no bitwise_count): the same components and distances
        arr = np.array(clustered_hashes(6), dtype=np.int64)
        labels = image_hash.near_components(arr, 4)
        with patch.object(np, "bitwise_count", None, create=True):
            self.assertTrue(np.array_equal(image_hash.near_components(arr, 4), labels))
            sub = [int(v) for v in arr[:40]]
            self.assertEqual(image_hash.max_pairwise_distance(arr[:40]), max(hamming(a, b) for a in sub for b in sub))

    def test_order_medoid_first_then_by_correlation_ties_by_path(self):
        base = np.frombuffer(hash_of(motif(30))[1], dtype=np.uint8).astype(np.float64)
        rng = np.random.default_rng(3)

        def variant(sigma):
            return np.clip(base + rng.normal(0, sigma, base.shape), 0, 255).astype(np.uint8)

        far = np.frombuffer(hash_of(motif(31))[1], dtype=np.uint8)
        order = image_hash.order_group(np.stack([variant(40), far, base, variant(3), variant(15)]), list("edcba"))
        self.assertIn(order[0], (2, 3))  # the medoid is near the center of the similar ones
        self.assertEqual(order[-1], 1)  # the unrelated one is last
        self.assertEqual(sorted(order), [0, 1, 2, 3, 4])
        same = np.tile(np.arange(1024, dtype=np.uint8) % 251, (3, 1))
        self.assertEqual(image_hash.order_group(same, ["b", "c", "a"]), [2, 0, 1])
        flat = np.full((2, 1024), 7, dtype=np.uint8)
        self.assertEqual(image_hash.order_group(flat, ["y", "x"]), [1, 0])
        self.assertEqual(image_hash.order_group(flat[:1], ["x"]), [0])

    def test_order_of_a_large_group_uses_a_sample_for_the_medoid(self):
        base = np.frombuffer(hash_of(motif(32))[1], dtype=np.uint8).astype(np.float64)
        k = image_hash.ORDER_SAMPLE * 2 + 50
        gray = np.clip(base + np.random.default_rng(4).normal(0, 8, (k, 1024)), 0, 255).astype(np.uint8)
        order = image_hash.order_group(gray, [f"{i:05d}" for i in range(k)])
        self.assertEqual(sorted(order), list(range(k)))
        # after the medoid the members fall in correlation to it
        z = image_hash._normalized(gray)
        corr = z @ z[order[0]]
        self.assertTrue(np.all(np.diff(np.round(corr[order[1:]], 5)) <= 3e-5))


class SingleFlightTest(unittest.TestCase):
    def test_equal_requests_at_the_same_time_compute_once_and_share_an_error(self):
        for outcome in ("value", "error"):
            cache = OrderedDict()
            gate = threading.Event()
            waiting = threading.Semaphore(0)
            calls, results = [], []

            class Inflight(dict):
                def get(self, key, default=None):
                    found = super().get(key, default)
                    if found is not None:
                        waiting.release()  # this request waits for the one that computes
                    return found

            def compute():
                calls.append(1)
                gate.wait(10)
                if outcome == "error":
                    raise RuntimeError("boom")
                return ["groups"]

            def ask():
                try:
                    results.append(image_groups._single_flight(cache, 1, ("key",), compute))
                except RuntimeError as e:
                    results.append(str(e))

            with patch.object(image_groups, "_INFLIGHT", Inflight()):
                threads = [threading.Thread(target=ask) for _ in range(3)]
                for t in threads:
                    t.start()
                for _ in range(2):
                    self.assertTrue(waiting.acquire(timeout=10))
                gate.set()
                for t in threads:
                    t.join()
                self.assertEqual(image_groups._INFLIGHT, {})
            self.assertEqual(len(calls), 1, outcome)
            self.assertEqual(results, [["groups"]] * 3 if outcome == "value" else ["boom"] * 3)
            self.assertEqual(list(cache), [("key",)] if outcome == "value" else [])  # an error is not cached
        # the next request computes again; the cache keeps max_entries
        self.assertEqual(image_groups._single_flight(cache, 1, ("key",), lambda: 1), 1)
        self.assertEqual(image_groups._single_flight(cache, 1, ("other",), lambda: 2), 2)
        self.assertEqual(list(cache), [("other",)])


# ---------------------------------------------------------------- job, routes and database


class VariantParseTest(unittest.TestCase):
    def test_the_a1111_form(self):
        exif = (
            'a cat, <lora:fur:0.80> sitting\n<lora:Light:1>\nNegative prompt: ugly\nSteps: 20, Sampler: Euler a, '
            'Schedule type: Karras, CFG scale: 7, Seed: 42, Size: 512x768, Model hash: abc, Model: m1, '
            'Lora hashes: "fur: 1a2b, Light: 3c4d", Source Identifier: Stable Diffusion web UI'
        )
        self.assertEqual(
            image_groups.parse_variant_meta(exif),
            (
                ("a cat,  sitting", "ugly", (("Light", "1"), ("fur", "0.8"))),
                "42",
                "512x768",
                (("CFG scale", "7"), ("Lora hashes", "fur: 1a2b, Light: 3c4d"), ("Model", "m1"),
                 ("Model hash", "abc"), ("Sampler", "Euler a"), ("Schedule type", "Karras"), ("Steps", "20")),
            ),
        )

    def test_lora_weights_are_normalised(self):
        weights = [None, "", "1.0", "0.50", "1:0.5", "abc", " 0.7 ", "-1"]
        self.assertEqual([image_groups._lora_weight(w) for w in weights], ["1", "1", "1", "0.5", "1:0.5", "abc", "0.7", "-1"])
        a = image_groups.parse_variant_meta(gen("p <lora:a:0.80> <lora:b>"))
        b = image_groups.parse_variant_meta(gen("p <lora:b:1.0> <lora:a:0.8>"))
        self.assertEqual((a.prompt, b.prompt), (("p", "", (("a", "0.8"), ("b", "1"))),) * 2)

    def test_the_prompt_stays_raw_text(self):
        self.assertEqual(image_groups.parse_variant_meta(gen("a cat\nsitting")).prompt[0], "a cat\nsitting")
        self.assertEqual(image_groups.parse_variant_meta("a cat  \nSteps: 1").prompt[0], "a cat")
        self.assertNotEqual(
            image_groups.parse_variant_meta(gen("a cat\nsitting")).prompt,
            image_groups.parse_variant_meta(gen("a cat sitting")).prompt,
        )

    def test_without_parameter_line_or_positive_prompt_there_is_none(self):
        for exif in ("Negative prompt: bad\nSteps: 20, Seed: 1", "<lora:x:1>\nSteps: 20, Seed: 1", "just text",
                     "", None, "p\nNegative prompt: n"):
            self.assertIsNone(image_groups.parse_variant_meta(exif), repr(exif))

    def test_extra_json_and_quoted_values(self):
        m = image_groups.parse_variant_meta('p\nSteps: 20, Seed: 1, Size: 1x1\nextraJsonMetaInfo: {"a": 1}')
        self.assertEqual(m, (("p", "", ()), "1", "1x1", (("Steps", "20"), ("extraJsonMetaInfo", '{"a": 1}'))))
        m = image_groups.parse_variant_meta(gen(extra=', Hires prompt: "x, y"'))
        self.assertIn(("Hires prompt", "x, y"), m.params)

    def test_seed_and_size_in_any_case(self):
        # NovelAI writes its own fields in lower case after "Steps"
        m = image_groups.parse_variant_meta("p\nSteps: 28, Source Identifier: NovelAI, steps: 28, seed: 7, size: 8x8")
        self.assertEqual((m.seed, m.size, m.params), ("7", "8x8", (("Steps", "28"), ("steps", "28"))))


class VariantGroupingTest(unittest.TestCase):
    @staticmethod
    def groups(exifs, kind):
        return image_groups.group_variants([image_groups.parse_variant_meta(e) for e in exifs], kind)

    def test_seed_groups_count_seeds_and_skip_images_without_prompt(self):
        exifs = [gen("a red fox", 1), gen("a red fox", 2), gen("a red fox", 3), gen("a red fox", 3),
                 gen("a blue bird", 1), gen("a blue bird", 2), None, "Negative prompt: x\nSteps: 20, Seed: 9"]
        g = self.groups(exifs, "seed")
        self.assertEqual([(x.members, x.variant, x.prompt) for x in g],
                         [([0, 1, 2, 3], [0, 1, 2, 2], "a red fox"), ([4, 5], [0, 1], "a blue bird")])
        # the id is a hash of the prompt key: stable and known
        self.assertEqual(g[0].id, "seed:792e9a3313aa1a70")
        self.assertEqual(image_groups._group_id("seed", ("a red fox", "", ())), g[0].id)
        self.assertRegex(g[1].id, r"^seed:[0-9a-f]{16}$")

    def test_parameter_groups_need_the_same_prompt_seed_and_size(self):
        exifs = [gen(seed=1, size="512x512", steps=20), gen(seed=1, size="512x512", steps=30),
                 gen(seed=1, size="768x768", steps=30), gen(seed=1, size="512x512", steps=20),
                 gen(seed=2, size="512x512", steps=20)]
        g = self.groups(exifs, "params")
        self.assertEqual([(x.members, x.variant) for x in g], [([0, 1, 3], [0, 1, 0])])
        self.assertRegex(g[0].id, r"^params:[0-9a-f]{16}$")
        # the same images as seed variants: one group with two seeds
        self.assertEqual([(x.members, x.variant) for x in self.groups(exifs, "seed")], [([0, 1, 2, 3, 4], [0, 0, 0, 0, 1])])

    def test_images_without_a_seed_take_no_part(self):
        # the built-in ComfyUI parser writes no seed: nothing says the seeds are the same, or different
        comfy = ["p\nSteps: 20, Sampler: euler, Model: m1, Source Identifier: ComfyUI",
                 "p\nSteps: 30, Sampler: euler, Model: m1, Source Identifier: ComfyUI"]
        self.assertEqual(self.groups(comfy, "params"), [])
        g = self.groups(comfy + [gen("p", 1), gen("p", 2)], "seed")
        self.assertEqual([(x.members, x.variant) for x in g], [([2, 3], [0, 1])])

    def test_what_separates_and_what_does_not(self):
        self.assertEqual(self.groups([gen(seed=1, neg="x"), gen(seed=2, neg="y")], "seed"), [])  # negative prompt
        self.assertEqual(self.groups([gen("p <lora:a:0.8>", 1), gen("p", 2)], "seed"), [])  # LoRAs
        g = self.groups([gen(seed=1), gen(seed=1, extra=", Clip skip: 2")], "params")  # any extra field
        self.assertEqual([(x.members, x.variant) for x in g], [([0, 1], [0, 1])])
        g = self.groups([gen("p <lora:a:0.80> <lora:b>", 1), gen("p <lora:b:1.0> <lora:a:0.8>", 2)], "seed")
        self.assertEqual([x.members for x in g], [[0, 1]])

    def test_sorting_and_prompt_preview(self):
        exifs = [gen("q", 1), gen("q", 2), gen("p", 1), gen("p", 2), gen("p", 3), gen("r", 1), gen("r", 2)]
        self.assertEqual([x.members[0] for x in self.groups(exifs, "seed")], [2, 0, 5])
        g = self.groups([gen("word " * 80, 1), gen("word " * 80, 2)], "seed")[0]
        self.assertEqual((len(g.prompt), g.prompt[-1]), (200, "…"))
        self.assertEqual(image_groups._short_prompt("a\n  b\tc"), "a b c")

    def test_order_by_gray_thumbnails_where_they_exist_else_by_date(self):
        def row(name, second, gray=None):
            return DbImg(f"/x/{name}.png", size=1, date=f"2000-01-01 00:00:0{second}"), gray

        order = image_groups.order_variant_members
        self.assertEqual(order([row("a", 3), row("b", 1), row("c", 2)]), [1, 2, 0])
        self.assertEqual(order([row("c", 1), row("a", 1)]), [1, 0])  # same date: by path
        g0 = (np.arange(1024) % 256).astype(np.uint8)
        self.assertEqual(order([row("a", 3, g0.tobytes()), row("b", 1)]), [1, 0])  # one thumbnail: all by date
        g1 = g0.copy()
        g1[:16] = 0
        g2 = 255 - g0
        rows = [row("A", 3, g2.tobytes()), row("B", 0), row("C", 2, g0.tobytes()), row("D", 1, g1.tobytes())]
        expected = [[0, 2, 3][o] for o in image_hash.order_group(np.stack([g2, g0, g1]), ["/x/A.png", "/x/C.png", "/x/D.png"])]
        result = order(rows)
        self.assertEqual(result, expected + [1])
        self.assertEqual(result[2:], [0, 1])  # the anti-correlated one last of the thumbnails, then the one without


class _ApiCase(unittest.TestCase):
    @staticmethod
    def make_app(write_permission_required=None, is_path_trusted=None):
        async def allow():
            return None

        app = FastAPI()
        image_groups.mount_image_groups_routes(app, DB_API, allow, write_permission_required or allow, is_path_trusted)
        return app

    def setUp(self):
        self.conn = DataBase.get_conn()
        with closing(self.conn.cursor()) as cur:
            for table in ("image_hash", "image_tag", "image"):
                cur.execute(f"DELETE FROM {table}")
        self.conn.commit()
        image_groups._ACTIVE["job_id"] = None
        for state in (image_groups._JOBS, image_groups._GROUPS, image_groups._EDGES, image_groups._SKIPPED, image_groups._VARIANTS):
            state.clear()
        self.dir = tempfile.mkdtemp(dir=_TMP_ROOT)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._clock = 0
        self.client = TestClient(self.make_app())
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def add(self, name: str, im: Image.Image = None, raw: bytes = None) -> int:
        if raw is not None:
            with open(self.path(name), "wb") as f:
                f.write(raw)
        else:
            im.save(self.path(name))
        return self.index(self.path(name))

    def index(self, path: str) -> int:
        self._clock += 1
        img = DbImg(path, size=os.stat(path).st_size, date=f"2000-01-01 00:00:{self._clock:02d}")
        img.save(self.conn)
        self.conn.commit()
        return img.id

    def touch(self, image_id: int, column: str = "size", value=None):
        """Change the image row (size + 1 by default), as a rescan of a changed file does."""
        sql = f"UPDATE image SET {column} = {'size + 1' if value is None else '?'} WHERE id = ?"
        self.conn.execute(sql, (image_id,) if value is None else (value, image_id))
        self.conn.commit()

    def start(self, client=None) -> dict:
        return (client or self.client).post(DB_API + "/image_groups_job_start", json={}).json()

    def wait(self, job_id, client=None, timeout=30) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = (client or self.client).get(DB_API + "/image_groups_job_status", params={"job_id": job_id}).json()
            if st["status"] in ("done", "error", "cancelled"):
                return st
            time.sleep(0.05)
        self.fail("job did not finish")

    def run_job(self, client=None) -> dict:
        return self.wait(self.start(client)["job_id"], client)

    def state(self, client=None) -> dict:
        return (client or self.client).get(DB_API + "/image_groups_state").json()

    def groups(self, kind="near", client=None, **kw) -> dict:
        return (client or self.client).post(DB_API + "/image_groups", json={"kind": kind, **kw}).json()

    def hash_rows(self):
        return self.conn.execute("SELECT image_id, status, version, size, date FROM image_hash ORDER BY image_id").fetchall()

    def job_settings(self, batch_size: int, threads: int) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch.object(image_groups, "BATCH_SIZE", batch_size))
        stack.enter_context(patch.dict(os.environ, {"IIB_IMAGE_HASH_THREADS": str(threads)}))
        return stack

    def gated_hash_batch(self):
        """hash_batch that waits for self.gate; self.started is set when the first batch begins."""
        self.gate, self.started = threading.Event(), threading.Event()
        self.addCleanup(self.gate.set)
        real = image_hash.hash_batch

        def slow(items):
            self.started.set()
            self.gate.wait(20)
            return real(items)

        return patch.object(image_hash, "hash_batch", slow)


class RoutesTest(_ApiCase):
    def test_state_job_and_groups_of_a_small_library(self):
        base = motif(40, 128)
        ids = {
            "a": self.add("a.png", base),
            "a_bmp": self.add("a_copy.bmp", base),
            "a_noisy": self.add("a_noisy.png", noisy(base)),
            "b": self.add("b.png", motif(41, 128)),
            "c": self.add("c.png", motif(42, 128)),
            "broken": self.add("broken.png", raw=b"garbage"),
        }
        self.add("clip.mp4", raw=b"\x00\x00\x00\x18ftypmp42" + bytes(40))
        self.add("song.mp3", raw=b"ID3" + bytes(30))
        st = self.state()
        # videos and audio are not counted
        self.assertEqual(
            st, {"total": 6, "hashed": 0, "failed": 0, "pending": 6, "skipped": 0, "version": image_groups.HASH_VERSION, "job": None}
        )
        res = self.run_job()
        self.assertEqual(res, {"job_id": res["job_id"], "status": "done", "done": 6, "total": 6, "error": None, "skipped": 0})
        st = self.state()
        # the broken image is a known failure: neither hashed nor pending
        self.assertEqual((st["total"], st["hashed"], st["failed"], st["pending"], st["job"]), (6, 5, 1, 0, None))
        self.assertEqual(dict((r[0], r[1]) for r in self.hash_rows()), {**{i: "ok" for i in ids.values()}, ids["broken"]: "fail"})
        self.assertEqual(self.run_job()["total"], 0)  # nothing left to do

        pix = self.groups("pixel")
        self.assertEqual(
            {k: v for k, v in pix.items() if k != "groups"},
            {"kind": "pixel", "threshold": 8, "group_count": 1, "image_count": 2, "offset": 0, "next_offset": 1,
             "has_more": False, "hashed": 5, "pending": 0},
        )
        g = pix["groups"][0]
        self.assertEqual((g["kind"], g["size"], g["max_distance"]), ("pixel", 2, 0))
        self.assertRegex(g["id"], r"^pixel:[0-9a-f]{16}$")
        file_info = {"type", "id", "date", "created_date", "size", "is_under_scanned_path", "bytes", "name", "fullpath"}
        self.assertEqual(set(g["images"][0]), file_info)
        # ties of identical images are ordered by path
        self.assertEqual([(i["id"], i["name"]) for i in g["images"]], [(ids["a"], "a.png"), (ids["a_bmp"], "a_copy.bmp")])

        near = self.groups("near", threshold=8)
        self.assertEqual((near["threshold"], near["group_count"], near["image_count"]), (8, 1, 3))
        g = near["groups"][0]
        self.assertEqual((g["id"], g["size"]), (f"near:{ids['a']}", 3))
        self.assertEqual({i["id"] for i in g["images"]}, {ids["a"], ids["a_bmp"], ids["a_noisy"]})
        self.assertLessEqual(g["max_distance"], 8)
        # the id of a near group stays when the member with the smallest id is gone
        os.remove(self.path("a.png"))
        g = self.groups("near", threshold=8)["groups"][0]
        self.assertEqual((g["id"], g["size"]), (f"near:{ids['a']}", 2))

    def test_pixel_only_and_flat_images_are_no_near_groups(self):
        x = motif(50)
        self.add("x1.png", x)
        self.add("x2.png", x)
        black = Image.new("RGB", (32, 32), (0, 0, 0))
        b1, b2 = self.add("black1.png", black), self.add("black2.png", black)
        self.add("white.png", Image.new("RGB", (32, 32), (255, 255, 255)))
        self.add("grey.png", Image.new("RGB", (32, 32), (9, 9, 9)))
        base = motif(230)
        m1, m2 = self.add("m1.png", base), self.add("m2.png", noisy(base))
        self.run_job()
        flat = self.conn.execute("SELECT COUNT(*) FROM image_hash WHERE status = 'ok' AND phash IS NULL").fetchone()[0]
        self.assertEqual(flat, 4)
        pix = self.groups("pixel")
        self.assertEqual((pix["group_count"], pix["image_count"]), (2, 4))
        self.assertIn({b1, b2}, [{i["id"] for i in g["images"]} for g in pix["groups"]])
        self.assertEqual(self.groups("pixel", threshold=3)["groups"], pix["groups"])  # pixel groups have no threshold
        near = self.groups("near", threshold=8)
        self.assertEqual([{i["id"] for i in g["images"]} for g in near["groups"]], [{m1, m2}])

    def test_sorting_paging_preview_min_size_and_clamps(self):
        a, b, c = motif(60), motif(61), motif(62)
        first = [self.add(f"1{s}.png", a) for s in "abcdef"]
        second = [self.add(f"2{s}.png", b) for s in "ab"]
        third = [self.add(f"3{s}.png", c) for s in "ab"]
        self.run_job()
        full = self.groups("pixel")
        # largest first, equal size: smaller image id first
        sizes_and_first_ids = [(g["size"], min(i["id"] for i in g["images"])) for g in full["groups"]]
        self.assertEqual(sizes_and_first_ids, [(6, first[0]), (2, second[0]), (2, third[0])])
        self.assertEqual((full["group_count"], full["image_count"]), (3, 10))
        # a preview of four file infos, the paths of all members in the same order
        g = full["groups"][0]
        self.assertEqual((len(g["images"]), [i["fullpath"] for i in g["images"]]), (4, g["paths"][:4]))
        self.assertEqual(sorted(g["paths"]), [self.path(f"1{s}.png") for s in "abcdef"])
        page = self.groups("pixel", offset=1, limit=1)
        position = (page["group_count"], page["image_count"], page["offset"], page["next_offset"], page["has_more"])
        self.assertEqual(position, (3, 10, 1, 2, True))
        self.assertEqual([g["id"] for g in page["groups"]], [full["groups"][1]["id"]])
        self.assertEqual(self.groups("pixel", offset=5)["groups"], [])
        self.assertEqual(self.groups("pixel", min_size=3)["group_count"], 1)
        self.assertEqual(self.groups("pixel", min_size=0)["group_count"], 3)
        self.assertEqual(self.groups("near", threshold=99)["threshold"], 12)
        self.assertEqual(self.groups("near", threshold=-4)["threshold"], 0)
        for kind in ("x", 3):
            self.assertIn(self.client.post(DB_API + "/image_groups", json={"kind": kind}).status_code, (400, 422))

    def test_change_detection_by_size_date_and_version(self):
        a_img = motif(80)
        a = self.add("a.png", a_img)
        self.add("a2.png", a_img)
        bad = self.add("bad.png", raw=b"garbage")
        self.add("broken.png", raw=b"more garbage")
        r = self.groups("pixel")
        self.assertEqual((r["group_count"], r["hashed"], r["pending"]), (0, 0, 4))  # pending images are not grouped
        self.run_job()
        self.add("a3.png", a_img)
        r = self.groups("pixel")
        self.assertEqual((r["group_count"], r["image_count"], r["hashed"], r["pending"]), (1, 2, 2, 1))
        self.run_job()
        self.assertEqual(self.run_job()["total"], 0)  # unchanged: nothing to do, known failures are not retried
        # a changed date or size makes the image pending, and its stale hash leaves the groups
        self.touch(a, "date", "2001-01-01")
        self.assertEqual((self.state()["pending"], self.groups("pixel")["image_count"]), (1, 2))
        self.assertEqual(self.run_job()["total"], 1)
        self.touch(a)
        self.assertEqual(self.state()["pending"], 1)
        self.run_job()
        # a failed image is retried when its row changes, and now succeeds
        motif(81).save(self.path("bad.png"))
        self.touch(bad, "size", os.path.getsize(self.path("bad.png")))
        self.assertEqual(self.state()["pending"], 1)
        self.run_job()
        self.assertEqual(dict((r[0], r[1]) for r in self.hash_rows())[bad], "ok")
        # a new version of the procedure recomputes everything, failures included
        newer = image_groups.HASH_VERSION + 1
        with patch.object(image_groups, "HASH_VERSION", newer):
            st = self.state()
            self.assertEqual((st["version"], st["hashed"], st["failed"], st["pending"]), (newer, 0, 0, 5))
            self.assertEqual(self.run_job()["total"], 5)
            self.assertEqual({r[2] for r in self.hash_rows()}, {newer})
        self.assertEqual(self.state()["pending"], 5)

    def test_job_hashes_in_batches_on_the_configured_threads(self):
        for i in range(12):
            self.add(f"t{i:02d}.png", motif(100 + i, 24))
        names = set()
        real = image_hash.hash_batch

        def tracked(items):
            names.add(threading.current_thread().name)
            time.sleep(0.02)  # long enough for the second thread to get work
            return real(items)

        with self.job_settings(batch_size=5, threads=2), patch.object(image_hash, "hash_batch", tracked):
            res = self.run_job()
        self.assertEqual((res["status"], res["done"], res["total"], self.state()["hashed"]), ("done", 12, 12, 12))
        self.assertEqual(len(names), 2)
        self.assertTrue(all(n.startswith("iib-image-hash") for n in names), names)

    def test_thread_count_and_job_list(self):
        for value, expected in {None: 3, "": 3, "1": 1, "5": 5, "0": 1, "-3": 1, "8": 8, "20": 8, "abc": 3}.items():
            with patch.dict(os.environ):
                os.environ.pop("IIB_IMAGE_HASH_THREADS", None)
                if value is not None:
                    os.environ["IIB_IMAGE_HASH_THREADS"] = value
                self.assertEqual(image_groups._thread_count(), expected, value)
        with patch.object(image_groups, "JOBS_MAX", 2):
            for job_id in "abc":
                image_groups._job_update(job_id, status="done")
        self.assertEqual(list(image_groups._JOBS), ["b", "c"])  # only the newest are kept

    def test_job_error_is_reported(self):
        self.add("a.png", motif(96))
        with patch.object(image_hash, "hash_batch", side_effect=RuntimeError("kaputt")):
            res = self.run_job()
        self.assertEqual(res["status"], "error")
        self.assertIn("kaputt", res["error"])
        self.assertIsNone(self.state()["job"])
        self.assertFalse(self.start()["already_running"])

    def test_locked_database_is_retried_by_the_job(self):
        self.add("a.png", motif(130))
        self.add("b.png", motif(131))
        real = ImageHash.upsert_many
        attempts = []

        def failing(error, times):
            def upsert(conn, rows):
                attempts.append(1)
                if len(attempts) <= times:
                    raise sqlite3.OperationalError(error)
                return real(conn, rows)

            return patch.object(ImageHash, "upsert_many", upsert)

        with patch.object(image_groups, "WRITE_RETRY_DELAYS", (0,) * 5):
            with failing("database is locked", 3):
                res = self.run_job()
            self.assertEqual((res["status"], res["done"], len(attempts), len(self.hash_rows())), ("done", 2, 4, 2))
            # a lock that does not go away ends the job after all attempts; other errors are not retried
            self.add("c.png", motif(132))
            for error, expected in (("database is locked", 6), ("no such table", 1)):
                attempts.clear()
                with failing(error, 99):
                    res = self.run_job()
                self.assertEqual((res["status"], len(attempts)), ("error", expected))
                self.assertIn(error, res["error"])

    def test_transient_errors_are_remembered_in_memory_only(self):
        a = self.add("ok.png", motif(110))
        gone, vanished, keep = (self.add(f"{n}.png", motif(111 + k)) for k, n in enumerate(("gone", "vanished", "keep")))
        for name in ("gone", "vanished", "keep"):
            os.remove(self.path(f"{name}.png"))
        calls = []
        real = image_hash.hash_batch

        def tracked(items):
            calls.extend(i for i, _ in items)
            return real(items)

        with patch.object(image_hash, "hash_batch", tracked):
            res = self.run_job()
            self.assertEqual((res["status"], res["done"], res["total"], res["skipped"]), ("done", 4, 4, 3))
            # nothing is stored for a missing file, and pending does not count it (else the view starts job after job)
            self.assertEqual([r[0] for r in self.hash_rows()], [a])
            st = self.state()
            self.assertEqual((st["total"], st["hashed"], st["pending"], st["skipped"], st["job"]), (4, 1, 0, 3, None))
            self.assertEqual(self.groups("pixel")["pending"], 0)
            # a new job tries them once more, and only those
            calls.clear()
            self.assertEqual(self.run_job()["skipped"], 3)
            self.assertEqual(sorted(calls), [gone, vanished, keep])
        # the skip is forgotten when its image row is deleted or changed (the changed one is pending again)
        DbImg.remove(self.conn, vanished)
        self.touch(gone)
        st = self.state()
        self.assertEqual((st["total"], st["pending"], st["skipped"]), (3, 1, 1))
        motif(111).save(self.path("gone.png"))
        res = self.run_job()
        self.assertEqual((res["total"], res["skipped"]), (2, 1))
        self.assertEqual((self.state()["hashed"], self.state()["skipped"]), (2, 1))
        # a new server process starts without the memory
        image_groups._SKIPPED.clear()
        st = self.state()
        self.assertEqual((st["pending"], st["skipped"]), (1, 0))

    def test_failures_are_stored_with_their_error_and_logged(self):
        self.add("ok.png", motif(114))
        bad = self.add("bad1.png", raw=b"garbage")
        self.add("bad2.png", raw=b"more garbage")
        self.add("gone.png", motif(115))
        os.remove(self.path("gone.png"))
        with patch.object(image_groups, "FAIL_LOG_MAX", 1), self.assertLogs("scripts.iib.logger", level="WARNING") as logs:
            self.assertEqual(self.run_job()["status"], "done")
        status, error, sha = self.conn.execute("SELECT status, error, pixel_sha FROM image_hash WHERE image_id = ?", (bad,)).fetchone()
        self.assertEqual((status, sha), ("fail", None))
        self.assertIn("UnidentifiedImageError", error)
        # FAIL_LOG_MAX images per kind are named in the log, then the totals
        self.assertEqual(len([line for line in logs.output if "cannot decode" in line and "UnidentifiedImageError" in line]), 1)
        self.assertEqual(len([line for line in logs.output if "skipped" in line and "gone.png" in line]), 1)
        self.assertTrue(any("1 images skipped, 2 cannot be decoded" in line for line in logs.output), logs.output)
        # the state adds up; the groups report hashed and pending only
        self.add("new.png", motif(116))
        st = self.state()
        self.assertEqual((st["total"], st["hashed"], st["failed"], st["skipped"], st["pending"]), (5, 1, 2, 1, 1))
        r = self.groups("pixel")
        self.assertEqual((r["hashed"], r["pending"], "failed" in r), (1, 1, False))

    def test_cancel_keeps_what_was_written_and_a_second_start_joins_the_job(self):
        for i in range(12):
            self.add(f"c{i:02d}.png", motif(300 + i, 16))
        with self.job_settings(batch_size=2, threads=1), self.gated_hash_batch():
            first = self.start()
            self.assertTrue(self.started.wait(10))
            self.assertFalse(first["already_running"])
            self.assertEqual(self.start(), {"job_id": first["job_id"], "already_running": True})
            self.assertEqual(self.state()["job"], {"job_id": first["job_id"], "status": "running", "done": 0, "total": 12})
            # without a job id: the active job
            self.assertEqual(self.client.post(DB_API + "/image_groups_job_cancel").json(), {"cancelled": True})
            self.gate.set()
            res = self.wait(first["job_id"])
        # the batch that ran is written, no new one was started
        self.assertEqual((res["status"], res["total"], res["done"], len(self.hash_rows())), ("cancelled", 12, 2, 2))
        self.assertEqual((self.state()["pending"], self.state()["job"]), (10, None))
        for body in ({}, {"job_id": first["job_id"]}, {"job_id": "nope"}):
            self.assertEqual(self.client.post(DB_API + "/image_groups_job_cancel", json=body).json(), {"cancelled": False})
        # the next job is a new one and finishes the rest
        res = self.run_job()
        self.assertNotEqual(res["job_id"], first["job_id"])
        self.assertEqual((res["status"], res["total"], self.state()["pending"]), ("done", 10, 0))

    def test_a_cancelled_job_with_a_hanging_read_does_not_block_a_new_one(self):
        for i in range(6):
            self.add(f"h{i}.png", motif(500 + i, 16))
        grace = patch.object(image_groups, "CANCEL_GRACE", 0.3)
        with self.job_settings(batch_size=2, threads=1), self.gated_hash_batch(), grace:
            job = self.start()["job_id"]
            self.assertTrue(self.started.wait(10))  # the read hangs (a dead network drive)
            self.client.post(DB_API + "/image_groups_job_cancel", json={"job_id": job})
            self.assertEqual(self.wait(job, timeout=10)["status"], "cancelled")
            # what the hanging batch would have produced is dropped
            self.assertEqual((self.hash_rows(), self.state()["job"]), ([], None))
        self.gate.set()
        res = self.run_job()
        self.assertEqual((res["status"], self.state()["pending"]), ("done", 0))

    def test_edges_are_computed_once_per_data_for_every_threshold(self):
        base = motif(140)
        self.add("c1.png", base)
        self.add("c2.png", noisy(base))
        self.run_job()
        calls = []
        real = image_hash.near_edges

        def counting(*a, **kw):
            calls.append(image_groups._COMPUTE_LOCK.locked())
            return real(*a, **kw)

        with patch.object(image_hash, "near_edges", counting):
            first = self.groups("near", threshold=8)
            self.assertEqual(self.groups("near", threshold=8), first)
            self.assertEqual(self.groups("near", threshold=8, offset=1, limit=5)["groups"], [])
            counts = [self.groups("near", threshold=t)["image_count"] for t in (0, 3, 6, 12)]
            # once, under the lock that lets only one expensive computation run at a time
            self.assertEqual(calls, [True])
            self.assertEqual(counts, sorted(counts))  # a larger threshold never gives fewer images
            # a new hash changes the data: new edges
            c3 = self.add("c3.png", noisy(base, 2.0, seed=5))
            self.run_job()
            self.assertEqual(self.groups("near", threshold=8)["image_count"], first["image_count"] + 1)
            self.assertEqual(len(calls), 2)
            # deleting it gives the data of before, whose edges are still cached
            DbImg.remove(self.conn, c3)
            self.assertEqual(self.groups("near", threshold=8), first)
            self.assertEqual(len(calls), 2)

    def test_a_dense_graph_is_not_kept_and_each_threshold_is_computed_on_its_own(self):
        base = motif(142)
        for i in range(4):
            self.add(f"x{i}.png", noisy(base, 2.0 + i, seed=i))
        self.run_job()
        calls = []
        real = image_hash.near_components

        def counting(ph, threshold, *a, **kw):
            calls.append((threshold, image_groups._COMPUTE_LOCK.locked()))
            return real(ph, threshold, *a, **kw)

        with patch.object(image_groups, "MAX_EDGES", 0), patch.object(image_hash, "near_components", counting):
            r8 = self.groups("near", threshold=8)
            self.groups("near", threshold=8)  # the groups are cached
            self.groups("near", threshold=4)
        self.assertEqual(calls, [(8, True), (4, True)])
        self.assertEqual(list(image_groups._EDGES.values()), [None])
        image_groups._GROUPS.clear()
        image_groups._EDGES.clear()
        self.assertEqual(self.groups("near", threshold=8), r8)  # from the edges: the same groups

    def test_paging_with_vanishing_groups_visits_every_group_once(self):
        for k in range(11):
            im = motif(400 + k, 24)
            self.add(f"p{k:02d}a.png", im)
            self.add(f"p{k:02d}b.png", im)
        self.run_job()
        full = self.groups("pixel", limit=1000)
        self.assertEqual((full["group_count"], full["image_count"], full["next_offset"], full["has_more"]), (11, 22, 11, False))
        # only the members on the page are checked against the disk
        checked = []
        real_exists = os.path.exists
        with patch.object(image_groups.os.path, "exists", lambda p: checked.append(p) or real_exists(p)):
            self.groups("pixel", limit=1)
        self.assertEqual(len(checked), 2)
        # files of three groups disappear: those groups drop out of the pages, the raw offset does not shift
        for k in (0, 4, 5):
            os.remove(self.path(f"p{k:02d}a.png"))
        seen, offset = [], 0
        for _ in range(10):
            r = self.groups("pixel", offset=offset, limit=3)
            self.assertEqual((r["group_count"], r["image_count"]), (11, 22))  # not reduced by missing files
            self.assertEqual((r["next_offset"], r["has_more"]), (min(11, offset + 3), offset + 3 < 11))
            seen += [g["id"] for g in r["groups"]]
            offset = r["next_offset"]
            if not r["has_more"]:
                break
        self.assertEqual(seen, [g["id"] for k, g in enumerate(full["groups"]) if k not in (0, 4, 5)])
        # a page that is empty after filtering still moves on (here the groups 4 and 5)
        r = self.groups("pixel", offset=4, limit=2)
        self.assertEqual((r["groups"], r["next_offset"], r["has_more"]), ([], 6, True))

    def test_members_that_are_gone_leave_the_page_but_not_the_counts(self):
        a, other = motif(150), motif(151)
        one, two, three = (self.add(f"d{i}.png", a) for i in (1, 2, 3))
        e1, e2 = self.add("e1.png", other), self.add("e2.png", other)
        self.run_job()
        # a file gone from disk: its group shrinks, the counts are those of the stored groups
        os.remove(self.path("d3.png"))
        r = self.groups("pixel")
        self.assertEqual((r["group_count"], r["image_count"], [g["size"] for g in r["groups"]]), (2, 5, [2, 2]))
        self.assertNotIn(self.path("d3.png"), [p for g in r["groups"] for p in g["paths"]])
        real = image_groups._load_members

        def deleting(*victims):
            def load(conn, image_ids):
                for victim in victims:
                    DbImg.remove(conn, victim)
                return real(conn, image_ids)

            return patch.object(image_groups, "_load_members", load)

        # a row deleted while the request runs: the group that falls below min_size is left out, the position stays
        with deleting(e2):
            r = self.groups("pixel")
        self.assertEqual((r["group_count"], r["image_count"], r["next_offset"], r["has_more"]), (2, 5, 2, False))
        self.assertEqual([sorted(i["id"] for i in g["images"]) for g in r["groups"]], [[one, two]])
        # every member of the page gone: an empty page, no error
        with deleting(one, two, e1):
            r = self.groups("pixel")
        self.assertEqual((r["group_count"], r["image_count"], r["groups"], r["next_offset"]), (1, 3, [], 1))

    def test_access_control_hides_images_from_job_state_and_groups(self):
        inside = os.path.join(self.dir, "inside")
        os.makedirs(inside)
        os.makedirs(os.path.join(self.dir, "outside"))
        for name in ("inside/1.png", "inside/2.png", "outside/3.png"):
            motif(160).save(self.path(name))
            self.index(self.path(name))
        with TestClient(self.make_app(is_path_trusted=lambda p: p.startswith(inside + os.sep))) as client:
            self.assertEqual(self.state(client)["total"], 2)
            self.run_job(client)
            self.assertEqual(len(self.hash_rows()), 2)  # the outside image is not even read
            r = self.groups("pixel", client)
            self.assertEqual((r["group_count"], r["image_count"], r["hashed"], r["pending"]), (1, 2, 2, 0))
            self.assertTrue(all(i["fullpath"].startswith(inside) for i in r["groups"][0]["images"]))

    def test_read_only_access_may_read_but_not_start_or_cancel(self):
        async def read_only():
            raise HTTPException(status_code=403, detail="read only")

        with TestClient(self.make_app(write_permission_required=read_only)) as client:
            self.assertEqual(client.post(DB_API + "/image_groups_job_start", json={}).status_code, 403)
            self.assertEqual(client.post(DB_API + "/image_groups_job_cancel", json={}).status_code, 403)
            self.assertEqual(client.get(DB_API + "/image_groups_state").status_code, 200)
            self.assertEqual(client.post(DB_API + "/image_groups", json={"kind": "pixel"}).status_code, 200)
            self.assertEqual(client.get(DB_API + "/image_groups_job_status", params={"job_id": "nope"}).status_code, 404)

    def test_without_numpy_the_api_imports_and_the_routes_report_it(self):
        code = "\n".join(
            [
                "import json, sys",
                "sys.modules['numpy'] = None",
                "from fastapi import FastAPI",
                "from fastapi.testclient import TestClient",
                "from scripts.iib import api",
                "app = FastAPI()",
                "api.infinite_image_browsing_api(app)",
                "c, b = TestClient(app), '/infinite_image_browsing/db'",
                "r = [c.get(b + '/image_groups_state'), c.post(b + '/image_groups_job_start', json={}),",
                "     c.post(b + '/image_groups', json={'kind': 'pixel'}),",
                "     c.post(b + '/image_variants', json={'kind': 'seed'})]",
                "print('RESULT' + json.dumps([[x.status_code, x.text] for x in r]))",
            ]
        )
        env = dict(os.environ, IIB_DB_PATH=os.path.join(_TMP_ROOT, "nonumpy.db"))
        env.pop("IIB_SECRET_KEY", None)
        proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT")]
        self.assertTrue(lines, proc.stdout + proc.stderr)
        for status, text in json.loads(lines[0][len("RESULT") :]):
            self.assertEqual(status, 500, text)
            self.assertIn("numpy", text)


class VariantRoutesTest(_ApiCase):
    def gen_file(self, name: str, exif=None, im: Image.Image = None, raw: bytes = None) -> int:
        path = self.path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if raw is not None:
            with open(path, "wb") as f:
                f.write(raw)
        else:
            (im or Image.new("RGB", (8, 8), (9, 9, 9))).save(path)
        self._clock += 1
        img = DbImg(path, exif, size=os.stat(path).st_size, date=f"2000-01-01 00:00:{self._clock:02d}")
        img.save(self.conn)
        self.conn.commit()
        return img.id

    def variants(self, kind="seed", client=None, **kw) -> dict:
        return (client or self.client).post(DB_API + "/image_variants", json={"kind": kind, **kw}).json()

    @staticmethod
    def head(r: dict) -> dict:
        return {k: v for k, v in r.items() if k != "groups"}

    def test_seed_and_parameter_variants_of_a_small_library(self):
        s = [self.gen_file(f"s{k}.png", gen("a red fox", seed)) for k, seed in enumerate((1, 2, 3), 1)]
        s.append(self.gen_file("s4.png", gen("a red fox", 3, steps=30)))
        self.gen_file("q1.png", gen("a blue bird", 1))
        self.gen_file("q2.png", gen("a blue bird", 2))
        self.gen_file("n1.png", "Negative prompt: x\nSteps: 20, Seed: 9")
        self.gen_file("n2.png", None)
        self.gen_file("clip.mp4", gen("a red fox", 4), raw=b"\x00\x00\x00\x18ftypmp42" + bytes(40))
        r = self.variants("seed")
        self.assertEqual(self.head(r), {"kind": "seed", "min_seeds": 3, "group_count": 1, "image_count": 4, "offset": 0,
                                        "next_offset": 1, "has_more": False, "total": 8, "no_prompt": 2})
        g = r["groups"][0]
        self.assertEqual((g["id"], g["kind"], g["size"], g["variant_count"], g["prompt"]),
                         ("seed:792e9a3313aa1a70", "seed", 4, 3, "a red fox"))
        file_info = {"type", "id", "date", "created_date", "size", "is_under_scanned_path", "bytes", "name", "fullpath"}
        self.assertEqual(set(g["images"][0]), file_info)
        self.assertEqual(g["paths"], [self.path(f"s{k}.png") for k in (1, 2, 3, 4)])  # no hashes: by date
        r = self.variants("seed", min_seeds=2)
        self.assertEqual((r["group_count"], r["image_count"], [x["size"] for x in r["groups"]]), (2, 6, [4, 2]))
        r = self.variants("params")
        self.assertEqual((r["group_count"], r["image_count"], r["total"], r["no_prompt"]), (1, 2, 8, 2))
        g = r["groups"][0]
        self.assertRegex(g["id"], r"^params:[0-9a-f]{16}$")
        self.assertEqual((g["kind"], g["size"], g["variant_count"], g["paths"]),
                         ("params", 2, 2, [self.path("s3.png"), self.path("s4.png")]))
        for kind in ("x", 3):
            self.assertIn(self.client.post(DB_API + "/image_variants", json={"kind": kind}).status_code, (400, 422))

    def test_filters_min_seeds_folders_and_several_folders(self):
        for k in (1, 2, 3):
            self.gen_file(f"a/x{k}.png", gen("a red fox", k))
        self.gen_file("b/y1.png", gen("a red fox", 4))
        for k in (1, 2, 3):
            self.gen_file(f"a/z{k}.png", gen("a green frog", k))
        r = self.variants()
        self.assertEqual((r["group_count"], r["image_count"], r["total"], [x["size"] for x in r["groups"]]), (2, 7, 7, [4, 3]))
        self.assertEqual(self.variants(min_seeds=4)["group_count"], 1)
        self.assertEqual([self.variants(min_seeds=1)[k] for k in ("min_seeds", "group_count")], [2, 2])
        self.assertEqual([self.variants(min_seeds=999)[k] for k in ("min_seeds", "group_count")], [100, 0])
        a, b = self.path("a"), self.path("b")
        r = self.variants(folder_paths=[a])
        self.assertEqual((r["group_count"], r["total"], [x["size"] for x in r["groups"]]), (2, 6, [3, 3]))
        self.assertEqual(self.head(self.variants(folder_paths=[a + os.sep, " "])), self.head(r))
        self.assertEqual((self.variants(folder_paths=[b])["group_count"], self.variants(folder_paths=[b])["total"]), (0, 1))
        self.assertEqual(self.variants(folder_paths=[a, b])["total"], 7)
        r = self.variants(multi_folder=True)
        self.assertEqual((r["group_count"], r["groups"][0]["size"]), (1, 4))
        self.assertEqual(self.variants("params", multi_folder=True)["group_count"], 0)

    def test_groups_are_computed_once_per_data_for_both_kinds(self):
        ids = [self.gen_file(f"c{k}.png", gen("a red fox", k)) for k in (1, 2, 3)]
        self.gen_file("other.png", gen("a blue bird", 1))
        calls = []
        real = image_groups.parse_variant_meta

        def counting(exif):
            calls.append(image_groups._COMPUTE_LOCK.locked())
            return real(exif)

        with patch.object(image_groups, "parse_variant_meta", counting):
            first = self.variants("seed")
            self.variants("params")
            self.assertEqual(self.variants("seed"), first)
            self.assertEqual(calls, [True] * 4)  # once per image for both kinds, under the compute lock
            # an edited exif (same size and date) is new data
            self.conn.execute("UPDATE image SET exif = ? WHERE id = ?", (gen("a red fox", 9), ids[0]))
            self.conn.commit()
            self.assertEqual(self.variants("seed")["groups"][0]["variant_count"], 3)
            self.assertEqual(len(calls), 8)
            # back to the old data: from the cache
            self.conn.execute("UPDATE image SET exif = ? WHERE id = ?", (gen("a red fox", 1), ids[0]))
            self.conn.commit()
            self.assertEqual(self.variants("seed"), first)
            self.assertEqual(len(calls), 8)

    def test_paging_and_members_that_are_gone(self):
        for name, n in (("a", 5), ("b", 4), ("c", 3)):
            for k in range(1, n + 1):
                self.gen_file(f"{name}{k}.png", gen(f"prompt {name}", k))
        full = self.variants()
        self.assertEqual((full["group_count"], full["image_count"], [x["size"] for x in full["groups"]]), (3, 12, [5, 4, 3]))
        page = self.variants(offset=1, limit=1)
        self.assertEqual((page["next_offset"], page["has_more"], [x["id"] for x in page["groups"]]),
                         (2, True, [full["groups"][1]["id"]]))
        os.remove(self.path("c3.png"))
        r = self.variants()
        # c has two seeds left: below min_seeds it leaves the page, the counts stay those of the stored groups
        self.assertEqual((r["group_count"], r["image_count"], r["next_offset"], r["has_more"]), (3, 12, 3, False))
        self.assertEqual([x["id"] for x in r["groups"]], [x["id"] for x in full["groups"][:2]])
        g = self.variants(min_seeds=2)["groups"][2]
        self.assertEqual((g["id"], g["size"], g["variant_count"]), (full["groups"][2]["id"], 2, 2))

    def test_order_follows_the_image_hashes_once_computed(self):
        base = motif(300)
        self.gen_file("o.png", gen("a red fox", 1), im=motif(301))  # oldest, but another motif
        self.gen_file("b.png", gen("a red fox", 2), im=base)
        self.gen_file("n.png", gen("a red fox", 3), im=noisy(base))
        self.assertEqual(self.variants()["groups"][0]["paths"], [self.path(x) for x in ("o.png", "b.png", "n.png")])
        self.run_job()
        paths = self.variants()["groups"][0]["paths"]
        self.assertEqual((set(paths[:2]), paths[2]), ({self.path("b.png"), self.path("n.png")}, self.path("o.png")))

    def test_access_control_and_read_only(self):
        for k in (1, 2, 3):
            self.gen_file(f"inside/{k}.png", gen("a red fox", k))
        self.gen_file("outside/4.png", gen("a red fox", 4))
        inside = self.path("inside")
        with TestClient(self.make_app(is_path_trusted=lambda p: p.startswith(inside + os.sep))) as client:
            r = self.variants(client=client)
            self.assertEqual((r["total"], r["group_count"], r["groups"][0]["size"]), (3, 1, 3))
            self.assertTrue(all(p.startswith(inside) for p in r["groups"][0]["paths"]))

        async def read_only():
            raise HTTPException(status_code=403, detail="read only")

        with TestClient(self.make_app(write_permission_required=read_only)) as client:
            self.assertEqual(client.post(DB_API + "/image_variants", json={"kind": "seed"}).status_code, 200)


class DatabaseTest(_ApiCase):
    def hashed(self, name: str) -> int:
        image_id = self.add(name, motif(1, 8))
        ImageHash.upsert_many(self.conn, [(image_id, "sha", 1, bytes(1024), 1, 1, "d", "ok", None)])
        self.conn.commit()
        return image_id

    def has_hash(self, image_id: int) -> bool:
        return self.conn.execute("SELECT COUNT(*) FROM image_hash WHERE image_id = ?", (image_id,)).fetchone()[0] == 1

    def test_every_delete_path_removes_the_image_hash(self):
        a, b, c, d, keep, moved, target = (self.hashed(f"{n}.png") for n in ("a", "b", "c", "d", "keep", "moved", "target"))
        DbImg.remove(self.conn, a)
        DbImg.safe_batch_remove(self.conn, [b, c])
        os.remove(self.path("d.png"))
        self.assertEqual(DbImg.remove_unchanged_missing(self.conn, [(d, self.path("d.png"))]), 1)
        # a path update that overwrites another row takes that row's hash with it
        DbImg.get(self.conn, moved).update_path(self.conn, self.path("target.png"), force=True)
        self.conn.commit()
        self.assertEqual([self.has_hash(i) for i in (a, b, c, d, keep, moved, target)], [False] * 4 + [True, True, False])

    def test_upsert_replaces_the_row_skips_deleted_images_and_keeps_signed_hashes(self):
        a = self.hashed("a.png")
        ph = -(1 << 63) + 5
        ImageHash.upsert_many(
            self.conn,
            [(a, None, ph, None, 1, 5, "d2", "fail", "ValueError: x"), (424242, "sha", 1, bytes(1024), 1, 1, "d", "ok", None)],
        )
        self.conn.commit()
        rows = self.conn.execute("SELECT image_id, phash, status, error, size, date FROM image_hash").fetchall()
        self.assertEqual(rows, [(a, ph, "fail", "ValueError: x", 5, "d2")])


if __name__ == "__main__":
    unittest.main()
