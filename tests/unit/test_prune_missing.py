"""Tests for pruning the index rows of files and folders that disappeared from disk.

The scanner only reports the rows of files that are gone; the background worker removes them,
and the rows below folders that are gone, once they have stayed missing for a grace period.
Everything is driven directly (no HTTP, no worker thread) inside a temporary directory tree.

Run from the repository root::

    python -m unittest tests/unit/test_prune_missing.py
"""
import logging
import os
import shutil
import sqlite3
import struct
import tempfile
import unittest
import zlib
from contextlib import closing
from unittest.mock import patch

# DataBase.path comes from IIB_DB_PATH when datamodel is imported; set it first.
_TMP_ROOT = tempfile.mkdtemp(prefix="iib_prune_missing_")
_DB_PATH = os.path.join(_TMP_ROOT, "iib.db")
os.environ["IIB_DB_PATH"] = _DB_PATH
os.environ["IIB_DB_FILE_BACKUP_DELAY"] = "9999"

from scripts.iib.db.datamodel import DataBase, Folder, Image as DbImg, ImageTag, Tag
from scripts.iib.db.update_image_data import scan_folder_incremental, update_image_data
from scripts.iib.index_worker import IndexWorker

LOGGER = "scripts.iib.logger"
_real_lstat = os.lstat
_real_listdir = os.listdir
_saved_db_path = DataBase.path
_OLD = "2000-01-01 00:00:00"


def _close_conn():
    # the sqlite connection holds the database file open, so close it before removing the tree
    conn = getattr(DataBase.local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        try:
            del DataBase.local.conn
        except AttributeError:
            pass


def setUpModule():
    # another test module in the same run may have imported datamodel first
    global _saved_db_path
    _close_conn()
    _saved_db_path = DataBase.path
    DataBase.path = _DB_PATH


def tearDownModule():
    _close_conn()
    # if this module imported datamodel first, the path a later module set is the one to restore
    DataBase.path = _saved_db_path if _saved_db_path != _DB_PATH else os.environ["IIB_DB_PATH"]
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


def _png(shade: int = 0) -> bytes:
    """A 1x1 grey PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes([0, shade % 256])))
        + chunk(b"IEND", b"")
    )


class _PruneTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = DataBase.get_conn()
        with closing(self.conn.cursor()) as cur:
            for table in ("image_embedding", "image_embedding_fail", "image_tag", "tag", "image", "folders"):
                cur.execute(f"DELETE FROM {table}")
        self.conn.commit()
        self.workdir = tempfile.mkdtemp(dir=_TMP_ROOT)
        self.root = os.path.join(self.workdir, "scan")
        os.makedirs(self.root)
        self._clock = 0

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def png(self, *parts):
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(_png(len(parts)))
        return path

    def bump(self, folder):
        """Give a folder a newer mtime, as a real change would (the scan skips folders whose mtime it knows)."""
        self._clock += 10
        t = os.stat(folder).st_mtime + self._clock
        os.utime(folder, (t, t))

    def row(self, path):
        return DbImg.get(self.conn, path)

    def like(self, path):
        """Give an indexed file a custom tag and an embedding, so the cascade has something to remove."""
        img = self.row(path)
        self.assertIsNotNone(img, f"{path} is not indexed")
        tag = Tag.get_or_create(self.conn, "like", "custom")
        ImageTag(img.id, tag.id).save_or_ignore(self.conn)
        Tag.add_count(self.conn, tag.id, 1)
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO image_embedding (image_id, model, dim, text_hash, vec, updated_at) "
                "VALUES (?, 'm', 1, 'h', x'00', '2000-01-01')",
                (img.id,),
            )
        self.conn.commit()
        return img.id

    def related(self, image_id):
        """(custom image_tag rows, image_embedding rows) of an image id."""
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                "SELECT COUNT(*) FROM image_tag JOIN tag ON tag.id = image_tag.tag_id "
                "WHERE image_id = ? AND tag.type = 'custom'",
                (image_id,),
            )
            tags = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM image_embedding WHERE image_id = ?", (image_id,))
            return tags, cur.fetchone()[0]

    def tag_count_drift(self):
        """Tags whose count differs from the number of image_tag rows that use them."""
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                "SELECT name, count FROM tag "
                "WHERE count <> (SELECT COUNT(*) FROM image_tag WHERE tag_id = tag.id)"
            )
            return cur.fetchall()

    def count(self, table):
        with closing(self.conn.cursor()) as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]

    def folder_marks(self):
        with closing(self.conn.cursor()) as cur:
            cur.execute("SELECT path FROM folders")
            return {r[0] for r in cur.fetchall()}

    def add_rows(self, folder, n):
        """n rows for files that do not exist, as [(id, path)]."""
        rows = []
        for i in range(n):
            path = os.path.join(folder, f"f{i:04d}.png")
            DbImg(path, "", 1, _OLD).save(self.conn)
            rows.append((self.row(path).id, path))
        self.conn.commit()
        return rows

    def assertIndexed(self, path):
        self.assertIsNotNone(self.row(path), f"{path} should still be indexed")

    def assertNotIndexed(self, path):
        self.assertIsNone(self.row(path), f"{path} should be removed from the index")

    def assertAllStay(self, *paths_and_ids):
        for p, i in paths_and_ids:
            self.assertIndexed(p)
            self.assertEqual(self.related(i), (1, 1))


class ScanFolderReportTest(_PruneTestCase):
    """_scan_folder reports the rows of direct children that are gone and deletes nothing."""

    def report(self, folder):
        found = []
        scan_folder_incremental(self.conn, folder, missing_files=found)
        return found

    def scanned_then_removed(self):
        """root/gone.png and root/kept.png, scanned; then gone.png is deleted."""
        gone = self.png("gone.png")
        self.png("kept.png")
        self.assertEqual(self.report(self.root), [])
        os.remove(gone)
        self.bump(self.root)
        return gone

    def test_deleted_file_is_reported_not_deleted(self):
        gone = self.scanned_then_removed()
        gone_id = self.row(gone).id
        self.assertEqual(self.report(self.root), [(gone_id, gone)])
        self.assertIndexed(gone)

    def test_other_callers_delete_nothing(self):
        gone = self.png("gone.png")
        self.png("kept.png")
        update_image_data([self.root])
        os.remove(gone)
        self.bump(self.root)
        self.assertNotIn("files_pruned", scan_folder_incremental(self.conn, self.root))
        self.bump(self.root)
        update_image_data([self.root])
        self.assertIndexed(gone)

    def test_only_direct_children_are_reported(self):
        sub = os.path.join(self.root, "sub")
        self.png("top.png")
        deep = self.png("sub", "deep.png")
        self.png("sub", "other.png")
        self.report(self.root)
        self.report(sub)
        os.remove(deep)
        self.bump(self.root)
        self.assertEqual(self.report(self.root), [])  # belongs to the scan of sub
        self.bump(sub)
        self.assertEqual([p for _, p in self.report(sub)], [deep])

    def test_folders_with_a_similar_prefix_are_not_affected(self):
        names = ["a", "a b", "a_x", "a%"]
        paths = {n: self.png(n, "f.png") for n in names}
        self.png("a", "g.png")
        for n in names:
            self.report(os.path.join(self.root, n))
            os.remove(paths[n])
        self.bump(os.path.join(self.root, "a"))
        self.assertEqual([p for _, p in self.report(os.path.join(self.root, "a"))], [paths["a"]])

    def test_empty_listing_reports_nothing(self):
        gone = self.scanned_then_removed()
        os.remove(os.path.join(self.root, "kept.png"))
        self.assertEqual(self.report(self.root), [])
        self.assertIndexed(gone)

    def test_not_reported_unless_lstat_says_not_found(self):
        gone = self.scanned_then_removed()
        kept = os.path.join(self.root, "kept.png")

        def denied():
            raise PermissionError(13, "denied", gone)

        cases = {
            "permission error": denied,
            # a case-insensitive file system finds the file under another spelling
            "found under another spelling": lambda: _real_lstat(kept),
        }
        for name, on_gone in cases.items():
            with self.subTest(name), patch(
                "os.lstat", side_effect=lambda p, *a, **k: on_gone() if p == gone else _real_lstat(p, *a, **k)
            ):
                self.assertEqual(self.report(self.root), [])
                self.bump(self.root)

    def test_not_reported_when_the_file_was_created_after_the_listing(self):
        self.png("a.png")
        self.png("late.png")
        self.report(self.root)
        self.bump(self.root)
        stale = lambda p=".": ["a.png"] if p == self.root else _real_listdir(p)
        with patch("os.listdir", side_effect=stale):
            self.assertEqual(self.report(self.root), [])


class RemoveUnchangedMissingTest(_PruneTestCase):
    """Image.remove_unchanged_missing deletes by id and the path that was seen missing."""

    def setUp(self):
        super().setUp()
        self.gone = self.png("gone.png")
        self.kept = self.png("kept.png")
        update_image_data([self.root])
        self.gone_id, self.kept_id = self.like(self.gone), self.like(self.kept)
        os.remove(self.gone)

    def test_rows_with_cascade_and_tag_counts(self):
        self.assertEqual(DbImg.remove_unchanged_missing(self.conn, [(self.gone_id, self.gone)]), 1)
        self.assertNotIndexed(self.gone)
        self.assertEqual(self.related(self.gone_id), (0, 0))
        self.assertAllStay((self.kept, self.kept_id))
        self.assertEqual(self.tag_count_drift(), [])

    def test_a_file_that_is_there_stays(self):
        self.assertEqual(DbImg.remove_unchanged_missing(self.conn, [(self.kept_id, self.kept)]), 0)
        self.assertAllStay((self.kept, self.kept_id))

    def test_a_database_error_removes_nothing_and_does_not_raise(self):
        with patch("scripts.iib.db.datamodel.commit_with_retry", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(DbImg.remove_unchanged_missing(self.conn, [(self.gone_id, self.gone)]), 0)
        self.assertAllStay((self.gone, self.gone_id))
        self.assertEqual(self.tag_count_drift(), [])

    def test_more_than_one_batch(self):
        rows = self.add_rows(self.root, 1203)
        self.assertEqual(DbImg.remove_unchanged_missing(self.conn, rows), 1203)
        self.assertEqual(self.count("image"), 2)


class ExpiredDirsTest(_PruneTestCase):
    def mark(self, folder):
        Folder.set_modified_date(self.conn, folder, _OLD)
        self.conn.commit()

    def test_missing_folders_are_reported_only_when_asked(self):
        gone = os.path.join(self.root, "gone")
        here = os.path.join(self.root, "here")
        below_a_file = os.path.join(self.png("file.png"), "sub")  # ENOTDIR counts as gone
        os.makedirs(here)
        for folder in (gone, here, below_a_file):
            self.mark(folder)
        self.assertEqual(Folder.get_expired_dirs(self.conn), [here])
        missing = []
        self.assertEqual(Folder.get_expired_dirs(self.conn, missing), [here])
        self.assertEqual(sorted(missing), sorted([gone, below_a_file]))

    def test_other_stat_errors_are_not_missing(self):
        locked = os.path.join(self.root, "locked")
        os.makedirs(locked)
        self.mark(locked)
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if path == locked:
                raise PermissionError(13, "denied", path)
            return real_stat(path, *args, **kwargs)

        missing = []
        with patch("os.stat", side_effect=stat):
            self.assertEqual(Folder.get_expired_dirs(self.conn, missing), [])
        self.assertEqual(missing, [])


class WorkerPruneTest(_PruneTestCase):
    """The worker removes what stayed missing for the grace period."""

    def setUp(self):
        super().setUp()
        self.roots = [self.root]
        self.worker = IndexWorker(lambda: list(self.roots))
        self.worker._governor.tick = lambda: None
        self.worker._prune_grace = 0
        self.settings = dict(self.worker._settings_snapshot(), thumbnail=False)

    def cycle(self):
        self.worker._run_cycle(self.conn, self.settings)

    def age(self):
        """Let everything that was seen missing count as missing for long enough."""
        w = self.worker
        w._pending_files = {k: (p, t - 1000) for k, (p, t) in w._pending_files.items()}
        w._missing_since = {p: t - 1000 for p, t in w._missing_since.items()}

    def tree(self):
        """root/keep/k.png, root/top.png and root/gone/{g.png, deep/d.png}, indexed; all but top liked."""
        self.keep = self.png("keep", "k.png")
        self.top = self.png("top.png")
        self.gone_dir = os.path.join(self.root, "gone")
        self.gone = [self.png("gone", "g.png"), self.png("gone", "deep", "d.png")]
        self.cycle()
        self.keep_id = self.like(self.keep)
        self.ids = [self.like(p) for p in self.gone]

    def assertPruned(self):
        for p, i in zip(self.gone, self.ids):
            self.assertNotIndexed(p)
            self.assertEqual(self.related(i), (0, 0))
        self.assertAllStay((self.keep, self.keep_id))
        self.assertIndexed(self.top)
        marks = self.folder_marks()
        self.assertFalse({self.gone_dir, os.path.join(self.gone_dir, "deep")} & marks)
        self.assertIn(os.path.join(self.root, "keep"), marks)
        self.assertEqual(self.tag_count_drift(), [])

    def assertPruneLog(self, logs, text):
        found = [m for m in logs.output if "pruned" in m]
        self.assertEqual(len(found), 1, found)
        self.assertIn(text, found[0])

    def assertNothingRemoved(self):
        before = (self.count("image"), self.count("folders"))
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.cycle()
            self.cycle()
            logging.getLogger(LOGGER).info("marker")  # assertLogs needs one record
        self.assertFalse([m for m in logs.output if "pruned" in m])
        self.assertEqual((self.count("image"), self.count("folders")), before)

    # -- files ------------------------------------------------------------
    def test_deleted_file_goes_after_the_grace_period(self):
        self.worker._prune_grace = 60
        gone = self.png("gone.png")
        kept = self.png("kept.png")
        self.cycle()
        gone_id, kept_id = self.like(gone), self.like(kept)
        os.remove(gone)
        self.bump(self.root)
        self.cycle()  # sees it missing
        self.cycle()
        self.assertIn(gone_id, self.worker._pending_files)
        self.assertAllStay((gone, gone_id))
        self.age()
        version = self.worker._version
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.cycle()
        self.assertNotIndexed(gone)
        self.assertEqual(self.related(gone_id), (0, 0))
        self.assertAllStay((kept, kept_id))
        self.assertEqual(self.tag_count_drift(), [])
        self.assertEqual(self.worker._pending_files, {})
        self.assertPruneLog(logs, "[maintenance] pruned 1 rows of missing files, 0 missing folders")
        self.assertGreater(self.worker._version, version)

    def test_a_move_racing_the_worker_keeps_the_row(self):
        # /move_files and /db/rename move the file first and update the row's path a moment later
        self.worker._prune_grace = 60
        old = self.png("old.png")
        self.png("other.png")
        self.cycle()
        image_id = self.like(old)
        new = os.path.join(self.workdir, "elsewhere.png")
        os.rename(old, new)
        self.bump(self.root)
        self.cycle()  # sees old.png missing, its row is not updated yet
        self.cycle()
        self.assertAllStay((old, image_id))
        self.row(old).update_path(self.conn, new)
        self.conn.commit()
        self.age()
        self.cycle()  # due, but the row has another path now
        self.assertAllStay((new, image_id))
        self.assertEqual(self.tag_count_drift(), [])

    def test_pause_in_the_middle_keeps_the_rest(self):
        rows = self.add_rows(self.root, 520)
        self.worker._pending_files = {i: (p, 0.0) for i, p in rows}
        calls = []

        def pause():
            calls.append(1)
            return len(calls) > 2  # start, first batch, then pause before the second

        with patch.object(self.worker, "_should_pause", side_effect=pause):
            self.assertEqual(self.worker._prune_pending_files(self.conn), 500)
        self.assertEqual(self.count("image"), 20)
        self.assertEqual(len(self.worker._pending_files), 20)
        self.assertEqual(self.worker._prune_pending_files(self.conn), 20)
        self.assertEqual(self.count("image"), 0)

    # -- folders ----------------------------------------------------------
    def test_deleted_folder_goes_after_the_grace_period(self):
        self.tree()
        self.worker._prune_grace = 60
        shutil.rmtree(self.gone_dir)
        self.cycle()
        self.cycle()
        self.assertAllStay(*zip(self.gone, self.ids))
        self.assertIn(self.gone_dir, self.folder_marks())
        self.age()
        version = self.worker._version
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.cycle()
        self.assertPruned()
        self.assertPruneLog(logs, "[maintenance] pruned 2 rows of missing files, 2 missing folders")
        self.assertGreater(self.worker._version, version)
        version = self.worker._version
        with self.assertNoLogs(LOGGER, "INFO"):
            self.cycle()  # nothing left to report
        self.assertEqual(self.worker._version, version)

    def test_blocking_scan_prunes_too(self):
        extra = self.png("extra.png")
        self.tree()
        os.remove(extra)
        shutil.rmtree(self.gone_dir)
        self.bump(self.root)
        self.assertEqual(self.worker.run_blocking()["files_pruned"], 2)  # the folder; sees extra.png missing
        self.assertPruned()
        self.assertIndexed(extra)
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.assertEqual(self.worker.run_blocking()["files_pruned"], 1)
        self.assertNotIndexed(extra)
        self.assertTrue(any("blocking scan done" in m and "pruned=1" in m for m in logs.output))

    def test_scan_dirs_are_determined_once_per_run(self):
        self.tree()
        extra = [os.path.join(self.root, f"gone{i}") for i in range(3)]
        self.worker._missing_since = {p: 0.0 for p in [self.gone_dir] + extra}
        shutil.rmtree(self.gone_dir)
        calls = []
        self.worker._get_scan_dirs = lambda: calls.append(1) or [self.root]
        self.worker._prune_missing_folders(self.conn)
        self.assertEqual(len(calls), 1)

    def test_pause_in_the_middle_keeps_the_other_folders(self):
        self.png("keep", "k.png")
        a, b = os.path.join(self.root, "a"), os.path.join(self.root, "b")
        rows = self.add_rows(a, 1) + self.add_rows(b, 1)
        for folder in (a, b):
            Folder.set_modified_date(self.conn, folder, _OLD)
        self.conn.commit()
        self.worker._missing_since = {a: 0.0, b: 0.0}
        calls = []

        def pause():
            calls.append(1)
            return len(calls) > 3  # start, folder a, its batch, then pause before folder b

        with patch.object(self.worker, "_should_pause", side_effect=pause):
            self.assertEqual(self.worker._prune_missing_folders(self.conn), (1, 1))
        self.assertNotIndexed(rows[0][1])
        self.assertIndexed(rows[1][1])
        self.assertEqual(list(self.worker._missing_since), [b])
        self.assertEqual(self.worker._prune_missing_folders(self.conn), (1, 1))
        self.assertNotIndexed(rows[1][1])

    # -- the guard ----------------------------------------------------------
    def test_guard(self):
        may = self.worker._may_prune_missing_dir
        blocker = self.png("keep", "file.png")
        cases = [
            (os.path.join(self.root, "gone"), True),
            (os.path.join(self.root, "gone", "deep") + os.sep, True),
            (os.path.join(self.root, "keep"), False),  # still exists
            (self.root, False),
            (self.root + os.sep, False),
            (self.root + "x" + os.sep + "gone", False),  # a sibling whose name starts like the root
            (os.path.join(self.workdir, "elsewhere"), False),
            (os.path.join(self.root, "..", "elsewhere", "x"), False),
            (os.path.join(blocker, "sub"), False),  # gone, but the nearest ancestor is a file
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(may(path), expected)
        nested = os.path.join(self.root, "nested")
        self.roots = [self.root, nested]
        self.assertFalse(may(nested), "a scan root below another one is still a scan root")

    def test_guard_needs_a_present_and_non_empty_root(self):
        may = self.worker._may_prune_missing_dir
        folder = os.path.join(self.root, "gone")
        self.assertFalse(may(folder), "empty root")
        self.png("keep", "k.png")
        self.assertTrue(may(folder))
        shutil.rmtree(self.root)
        self.assertFalse(may(folder), "missing root")

    def test_empty_scan_root_removes_nothing(self):
        self.tree()
        for name in os.listdir(self.root):
            path = os.path.join(self.root, name)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        self.bump(self.root)  # the empty root is dirty and gets scanned
        self.assertNothingRemoved()

    # -- unmounted volumes ----------------------------------------------------
    def test_unmounted_mount_point_below_the_scan_root_keeps_its_rows(self):
        self.png("keep", "k.png")
        x = self.png("nas", "2024", "x.png")
        self.cycle()
        x_id = self.like(x)
        shutil.rmtree(os.path.join(self.root, "nas", "2024"))  # the volume is unmounted: an empty directory
        self.assertNothingRemoved()
        self.assertAllStay((x, x_id))
        # known limit: a file in the mount point (.DS_Store) makes it look like a real folder
        with open(os.path.join(self.root, "nas", ".DS_Store"), "wb") as f:
            f.write(b"x")
        self.cycle()
        self.assertNotIndexed(x)

    def nested_tree(self):
        """A second scan root root/ext with ext/sub/e.png, liked."""
        self.png("keep", "k.png")
        self.ext = os.path.join(self.root, "ext")
        self.e = self.png("ext", "sub", "e.png")
        self.roots = [self.root, self.ext]
        self.cycle()
        self.e_id = self.like(self.e)

    def test_nested_scan_root_that_is_an_empty_mount_point(self):
        self.nested_tree()
        shutil.rmtree(self.ext)
        os.makedirs(self.ext)
        self.assertNothingRemoved()
        # production leaves a missing or unusable directory out of the scan paths
        self.roots = [self.root]
        self.assertNothingRemoved()
        self.assertAllStay((self.e, self.e_id))

    def test_nested_scan_root_that_is_gone_entirely(self):
        self.nested_tree()
        shutil.rmtree(self.ext)
        self.cycle()
        self.assertAllStay((self.e, self.e_id))  # the innermost scan root does not exist
        # known limit: once it is not a scan root any more, the non-empty parent looks like the
        # place where the folder was deleted
        self.roots = [self.root]
        self.cycle()
        self.assertNotIndexed(self.e)

    def test_symlink_to_a_volume_that_goes_away(self):
        self.png("keep", "k.png")
        volume = os.path.join(self.workdir, "volume")
        os.makedirs(os.path.join(volume, "sub"))
        with open(os.path.join(volume, "sub", "e.png"), "wb") as f:
            f.write(_png())
        os.symlink(volume, os.path.join(self.root, "ext"))
        e = os.path.join(self.root, "ext", "sub", "e.png")
        self.cycle()
        scan_folder_incremental(self.conn, os.path.dirname(e))
        e_id = self.like(e)
        shutil.rmtree(volume)
        self.cycle()
        self.cycle()
        self.assertAllStay((e, e_id))
        self.assertIn(os.path.dirname(e), self.folder_marks())


if __name__ == "__main__":
    unittest.main()
