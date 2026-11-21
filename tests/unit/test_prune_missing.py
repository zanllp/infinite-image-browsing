"""Tests for pruning the index rows of files and folders that disappeared from disk.

The scanner only reports the rows of files that are gone; the background worker removes them,
and the rows below folders that are gone, once they have stayed missing for a grace period.
Everything is driven directly (no HTTP, no worker thread) and works inside its own temporary
directory tree.

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
    # DataBase.path is read when datamodel is first imported; another test module in the
    # same run may have imported it before this one, so point it at this module's database
    global _saved_db_path
    _close_conn()
    _saved_db_path = DataBase.path
    DataBase.path = _DB_PATH


def tearDownModule():
    _close_conn()
    # hand the path back; if this was the first module to import datamodel, the saved path is
    # ours, and the one a later-imported module put into the environment is the one to use
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

    # -- helpers ---------------------------------------------------------
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
                "SELECT name, count, (SELECT COUNT(*) FROM image_tag WHERE tag_id = tag.id) FROM tag "
                "WHERE count <> (SELECT COUNT(*) FROM image_tag WHERE tag_id = tag.id)"
            )
            return cur.fetchall()

    def like_count(self):
        with closing(self.conn.cursor()) as cur:
            cur.execute("SELECT count FROM tag WHERE name = 'like' AND type = 'custom'")
            return cur.fetchone()[0]

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
            DbImg(path, "", 1, "2000-01-01 00:00:00").save(self.conn)
            rows.append((self.row(path).id, path))
        self.conn.commit()
        return rows

    def assertIndexed(self, path):
        self.assertIsNotNone(self.row(path), f"{path} should still be indexed")

    def assertNotIndexed(self, path):
        self.assertIsNone(self.row(path), f"{path} should be removed from the index")


class ScanFolderReportTest(_PruneTestCase):
    """_scan_folder reports the rows of direct children that are gone and deletes nothing."""

    def report(self, folder):
        found = []
        scan_folder_incremental(self.conn, folder, missing_files=found)
        return found

    def test_deleted_file_is_reported_not_deleted(self):
        gone = self.png("gone.png")
        self.png("kept.png")
        self.report(self.root)
        gone_id = self.like(gone)
        os.remove(gone)
        self.bump(self.root)
        self.assertEqual(self.report(self.root), [(gone_id, gone)])
        self.assertIndexed(gone)
        self.assertEqual(self.related(gone_id), (1, 1))

    def test_nothing_is_reported_without_a_find(self):
        self.png("a.png")
        self.assertEqual(self.report(self.root), [])
        self.bump(self.root)
        self.assertEqual(self.report(self.root), [])

    def test_without_a_list_nothing_is_looked_for_or_deleted(self):
        gone = self.png("gone.png")
        self.png("kept.png")
        scan_folder_incremental(self.conn, self.root)
        os.remove(gone)
        self.bump(self.root)
        stats = scan_folder_incremental(self.conn, self.root)
        self.assertNotIn("files_pruned", stats)
        self.assertIndexed(gone)

    def test_full_update_deletes_nothing(self):
        gone = self.png("gone.png")
        self.png("kept.png")
        update_image_data([self.root])
        os.remove(gone)
        self.bump(self.root)
        update_image_data([self.root])
        self.assertIndexed(gone)

    def test_sidecar_stays_but_image_row_is_reported(self):
        img = self.png("a.png")
        sidecar = os.path.join(self.root, "a.txt")
        with open(sidecar, "w", encoding="utf-8") as f:
            f.write("prompt")
        self.png("b.png")
        self.report(self.root)
        os.remove(img)
        self.bump(self.root)
        self.assertEqual([p for _, p in self.report(self.root)], [img])
        self.assertTrue(os.path.exists(sidecar))

    def test_only_direct_children_are_reported(self):
        self.png("top.png")
        deep = self.png("sub", "deep.png")
        self.report(self.root)
        self.report(os.path.join(self.root, "sub"))
        os.remove(deep)
        self.bump(self.root)
        self.assertEqual(self.report(self.root), [])  # belongs to the scan of sub
        self.png("sub", "other.png")
        self.bump(os.path.join(self.root, "sub"))
        self.assertEqual([p for _, p in self.report(os.path.join(self.root, "sub"))], [deep])

    def test_folders_with_a_similar_prefix_are_not_affected(self):
        names = ["a", "a b", "a_x", "a%"]
        paths = {n: self.png(n, "f.png") for n in names}
        self.png("a", "g.png")
        for n in names:
            self.report(os.path.join(self.root, n))
        for n in names:
            os.remove(paths[n])
        self.bump(os.path.join(self.root, "a"))
        self.assertEqual([p for _, p in self.report(os.path.join(self.root, "a"))], [paths["a"]])

    def test_moved_file_is_reported_under_its_old_path(self):
        old = self.png("old.png")
        self.png("other.png")
        self.report(self.root)
        old_id = self.like(old)
        new = os.path.join(self.root, "new.png")
        os.rename(old, new)
        self.bump(self.root)
        self.assertEqual(self.report(self.root), [(old_id, old)])
        self.assertIndexed(new)  # the new path is a new row without the tags of the old one

    def test_empty_listing_reports_nothing(self):
        a = self.png("a.png")
        b = self.png("b.png")
        self.report(self.root)
        os.remove(a)
        os.remove(b)
        self.bump(self.root)
        self.assertEqual(self.report(self.root), [])

    def test_not_reported_when_gone_cannot_be_told(self):
        gone = self.png("gone.png")
        self.png("kept.png")
        self.report(self.root)
        os.remove(gone)
        self.bump(self.root)

        def lstat(path, *args, **kwargs):
            if path == gone:
                raise PermissionError(13, "denied", path)
            return _real_lstat(path, *args, **kwargs)

        with patch("os.lstat", side_effect=lstat):
            self.assertEqual(self.report(self.root), [])

    def test_not_reported_when_the_file_exists_under_another_spelling(self):
        upper = os.path.join(self.root, "A.png")
        self.png("a.png")
        self.png("b.png")
        self.report(self.root)
        DbImg(upper, "", 1, "2000-01-01 00:00:00").save(self.conn)
        self.conn.commit()
        self.bump(self.root)

        def lstat(path, *args, **kwargs):
            # a case-insensitive file system finds the file under either spelling
            if path == upper:
                path = os.path.join(self.root, "a.png")
            return _real_lstat(path, *args, **kwargs)

        with patch("os.lstat", side_effect=lstat):
            self.assertEqual(self.report(self.root), [])

    def test_not_reported_when_the_file_was_created_after_the_listing(self):
        self.png("a.png")
        self.png("late.png")
        self.report(self.root)
        self.bump(self.root)

        def listdir(path="."):
            if path == self.root:
                return ["a.png"]  # stale: late.png was created after this listing
            return _real_listdir(path)

        with patch("os.listdir", side_effect=listdir):
            self.assertEqual(self.report(self.root), [])


class RemoveUnchangedMissingTest(_PruneTestCase):
    """Image.remove_unchanged_missing deletes by id and the path that was seen missing."""

    def test_rows_with_cascade_and_tag_counts(self):
        gone = self.png("gone.png")
        kept = self.png("kept.png")
        update_image_data([self.root])
        gone_id, kept_id = self.like(gone), self.like(kept)
        self.assertEqual(self.tag_count_drift(), [])
        os.remove(gone)
        self.assertEqual(DbImg.remove_unchanged_missing(self.conn, [(gone_id, gone)]), 1)
        self.assertNotIndexed(gone)
        self.assertEqual(self.related(gone_id), (0, 0))
        self.assertEqual(self.related(kept_id), (1, 1))
        self.assertEqual(self.like_count(), 1)
        self.assertEqual(self.tag_count_drift(), [])

    def test_a_row_that_changed_its_path_stays(self):
        old = self.png("old.png")
        self.png("other.png")
        update_image_data([self.root])
        image_id = self.like(old)
        new = os.path.join(self.workdir, "moved.png")
        os.rename(old, new)
        # what /move_files and /db/rename do after moving the file: keep the id, change the path
        self.row(old).update_path(self.conn, new)
        self.conn.commit()
        self.assertEqual(DbImg.remove_unchanged_missing(self.conn, [(image_id, old)]), 0)
        self.assertIndexed(new)
        self.assertEqual(self.related(image_id), (1, 1))

    def test_a_file_that_is_back_stays(self):
        path = self.png("a.png")
        self.png("b.png")
        update_image_data([self.root])
        image_id = self.like(path)
        self.assertEqual(DbImg.remove_unchanged_missing(self.conn, [(image_id, path)]), 0)
        self.assertEqual(self.related(image_id), (1, 1))

    def test_more_than_one_batch(self):
        rows = self.add_rows(self.root, 1203)
        self.assertEqual(DbImg.remove_unchanged_missing(self.conn, rows), 1203)
        self.assertEqual(self.count("image"), 0)

    def test_a_database_error_removes_nothing_and_does_not_raise(self):
        gone = self.png("gone.png")
        self.png("kept.png")
        update_image_data([self.root])
        image_id = self.like(gone)
        os.remove(gone)
        with patch(
            "scripts.iib.db.datamodel.commit_with_retry", side_effect=sqlite3.OperationalError("locked")
        ):
            self.assertEqual(DbImg.remove_unchanged_missing(self.conn, [(image_id, gone)]), 0)
        self.assertIndexed(gone)
        self.assertEqual(self.related(image_id), (1, 1))
        self.assertEqual(self.like_count(), 1)


class ExpiredDirsTest(_PruneTestCase):
    def mark(self, folder):
        Folder.set_modified_date(self.conn, folder, "2000-01-01 00:00:00")
        self.conn.commit()

    def test_missing_folders_are_reported_only_when_asked(self):
        gone = os.path.join(self.root, "gone")
        here = os.path.join(self.root, "here")
        os.makedirs(here)
        self.mark(gone)
        self.mark(here)
        self.assertEqual(Folder.get_expired_dirs(self.conn), [here])
        missing = []
        self.assertEqual(Folder.get_expired_dirs(self.conn, missing), [here])
        self.assertEqual(missing, [gone])

    def test_not_a_directory_counts_as_missing(self):
        blocker = self.png("file.png")
        below = os.path.join(blocker, "sub")  # a path through a file: ENOTDIR
        self.mark(below)
        missing = []
        Folder.get_expired_dirs(self.conn, missing)
        self.assertEqual(missing, [below])

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
        """root/keep/k.png, root/top.png and root/gone/{g.png, deep/d.png}, indexed; all with tag and embedding."""
        self.keep = self.png("keep", "k.png")
        self.top = self.png("top.png")
        self.gone_dir = os.path.join(self.root, "gone")
        self.gone = [self.png("gone", "g.png"), self.png("gone", "deep", "d.png")]
        self.cycle()
        self.keep_id = self.like(self.keep)
        self.ids = [self.like(p) for p in self.gone]
        for p in self.gone + [self.keep, self.top]:
            self.assertIndexed(p)

    def assertPruned(self):
        for p, i in zip(self.gone, self.ids):
            self.assertNotIndexed(p)
            self.assertEqual(self.related(i), (0, 0))
        self.assertIndexed(self.keep)
        self.assertIndexed(self.top)
        self.assertEqual(self.related(self.keep_id), (1, 1))
        marks = self.folder_marks()
        self.assertNotIn(self.gone_dir, marks)
        self.assertNotIn(os.path.join(self.gone_dir, "deep"), marks)
        self.assertIn(os.path.join(self.root, "keep"), marks)
        self.assertEqual(self.tag_count_drift(), [])

    def assertAllStay(self, paths_and_ids):
        for p, i in paths_and_ids:
            self.assertIndexed(p)
            self.assertEqual(self.related(i), (1, 1))

    def assertNoPruneLog(self, logs):
        self.assertFalse([m for m in logs.output if "pruned" in m])

    # -- files ------------------------------------------------------------
    def test_deleted_file_goes_with_the_next_cycle(self):
        gone = self.png("gone.png")
        kept = self.png("kept.png")
        self.cycle()
        gone_id, kept_id = self.like(gone), self.like(kept)
        os.remove(gone)
        self.bump(self.root)
        self.cycle()  # sees it missing
        self.assertIndexed(gone)
        self.assertIn(gone_id, self.worker._pending_files)
        version = self.worker._version
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.cycle()
        self.assertNotIndexed(gone)
        self.assertEqual(self.related(gone_id), (0, 0))
        self.assertEqual(self.related(kept_id), (1, 1))
        self.assertEqual(self.tag_count_drift(), [])
        self.assertEqual(self.worker._pending_files, {})
        found = [m for m in logs.output if "missing files" in m]
        self.assertEqual(len(found), 1)
        self.assertIn("[maintenance] pruned 1 rows of missing files, 0 missing folders", found[0])
        self.assertGreater(self.worker._version, version)

    def test_nothing_is_removed_within_the_grace_period(self):
        self.worker._prune_grace = 60
        gone = self.png("gone.png")
        self.png("kept.png")
        self.cycle()
        gone_id = self.like(gone)
        os.remove(gone)
        self.bump(self.root)
        self.cycle()
        self.cycle()
        self.assertAllStay([(gone, gone_id)])
        self.age()
        self.cycle()
        self.assertNotIndexed(gone)

    def test_blocking_scan_removes_files_too(self):
        gone = self.png("gone.png")
        self.png("kept.png")
        self.cycle()
        self.like(gone)
        os.remove(gone)
        self.bump(self.root)
        self.worker.run_blocking()
        self.assertIndexed(gone)
        with self.assertLogs(LOGGER, "INFO") as logs:
            stats = self.worker.run_blocking()
        self.assertNotIndexed(gone)
        self.assertEqual(stats["files_pruned"], 1)
        self.assertTrue(any("blocking scan done" in m and "pruned=1" in m for m in logs.output))

    def test_the_path_of_the_row_changes_between_seeing_and_deleting(self):
        old = self.png("old.png")
        self.png("other.png")
        self.cycle()
        image_id = self.like(old)
        new = os.path.join(self.workdir, "elsewhere.png")
        os.rename(old, new)
        self.bump(self.root)
        self.cycle()  # sees old.png missing
        self.assertIn(image_id, self.worker._pending_files)
        self.row(old).update_path(self.conn, new)  # /move_files commits its update_path
        self.conn.commit()
        self.cycle()
        self.assertAllStay([(new, image_id)])

    def test_a_move_whose_row_is_not_updated_yet_is_left_alone_within_the_grace_period(self):
        self.worker._prune_grace = 60
        old = self.png("old.png")
        self.png("other.png")
        self.cycle()
        image_id = self.like(old)
        new = os.path.join(self.workdir, "elsewhere.png")
        os.rename(old, new)  # the file is moved, the row still has the old path
        self.bump(self.root)
        self.cycle()
        self.cycle()
        self.assertAllStay([(old, image_id)])
        self.row(old).update_path(self.conn, new)
        self.conn.commit()
        self.age()
        self.cycle()
        self.assertAllStay([(new, image_id)])
        self.assertEqual(self.tag_count_drift(), [])

    def test_database_error_does_not_stop_the_run(self):
        gone = self.png("gone.png")
        self.png("kept.png")
        self.cycle()
        image_id = self.like(gone)
        os.remove(gone)
        self.bump(self.root)
        self.cycle()
        with patch(
            "scripts.iib.db.datamodel.commit_with_retry", side_effect=sqlite3.OperationalError("locked")
        ):
            self.assertEqual(self.worker._prune_due(self.conn), (0, 0))
        self.assertAllStay([(gone, image_id)])

    def test_pause_in_the_middle_keeps_the_rest(self):
        rows = self.add_rows(self.root, 520)
        self.png("keep.png")
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
    def test_cycle_removes_rows_below_a_deleted_folder(self):
        self.tree()
        shutil.rmtree(self.gone_dir)
        version = self.worker._version
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.cycle()
        self.assertPruned()
        found = [m for m in logs.output if "missing files" in m]
        self.assertEqual(len(found), 1)
        self.assertIn("[maintenance] pruned 2 rows of missing files, 2 missing folders", found[0])
        self.assertGreater(self.worker._version, version)

    def test_blocking_scan_removes_them_too(self):
        self.tree()
        shutil.rmtree(self.gone_dir)
        with self.assertLogs(LOGGER, "INFO") as logs:
            stats = self.worker.run_blocking()
        self.assertPruned()
        self.assertEqual(stats["files_pruned"], 2)
        self.assertTrue(any("blocking scan done" in m and "pruned=2" in m for m in logs.output))

    def test_folders_wait_for_the_grace_period(self):
        self.tree()
        self.worker._prune_grace = 60
        shutil.rmtree(self.gone_dir)
        self.cycle()
        self.cycle()
        self.assertIndexed(self.gone[0])
        self.assertIn(self.gone_dir, self.folder_marks())
        self.age()
        self.cycle()
        self.assertPruned()

    def test_a_second_cycle_has_nothing_to_report(self):
        self.tree()
        shutil.rmtree(self.gone_dir)
        self.cycle()
        version = self.worker._version
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.cycle()
            logging.getLogger(LOGGER).info("marker")  # assertLogs needs one record
        self.assertNoPruneLog(logs)
        self.assertEqual(self.worker._version, version)

    def test_a_cycle_without_a_find_logs_nothing_at_info(self):
        self.tree()
        with self.assertNoLogs(LOGGER, "INFO"):
            self.cycle()

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
        Folder.set_modified_date(self.conn, a, "2000-01-01 00:00:00")
        Folder.set_modified_date(self.conn, b, "2000-01-01 00:00:00")
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
    def test_missing_scan_root_removes_nothing(self):
        self.tree()
        moved = self.root + "_moved"
        os.rename(self.root, moved)
        before = (self.count("image"), self.count("folders"))
        self.cycle()
        self.assertEqual((self.count("image"), self.count("folders")), before)
        os.rename(moved, self.root)

    def test_empty_scan_root_removes_nothing(self):
        self.tree()
        for name in os.listdir(self.root):
            path = os.path.join(self.root, name)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        self.bump(self.root)  # the empty root is dirty and gets scanned
        before = (self.count("image"), self.count("folders"))
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.cycle()
        self.assertNoPruneLog(logs)
        self.assertEqual((self.count("image"), self.count("folders")), before)

    def test_folder_outside_all_scan_roots_is_left_alone(self):
        other = os.path.join(self.workdir, "other")
        os.makedirs(other)
        outside = os.path.join(other, "gone", "o.png")
        os.makedirs(os.path.dirname(outside))
        with open(outside, "wb") as f:
            f.write(_png())
        self.png("keep", "k.png")
        self.roots = [self.root, other]
        self.cycle()
        self.assertIndexed(outside)
        # the folder is no longer under a scan path (as after removing the extra path)
        self.roots = [self.root]
        shutil.rmtree(os.path.join(other, "gone"))
        self.cycle()
        self.assertIndexed(outside)
        self.assertIn(os.path.join(other, "gone"), self.folder_marks())

    def test_guard(self):
        w = self.worker
        self.png("keep", "k.png")
        self.assertTrue(w._may_prune_missing_dir(os.path.join(self.root, "gone")))
        self.assertTrue(w._may_prune_missing_dir(os.path.join(self.root, "gone", "deep") + os.sep))
        self.assertFalse(w._may_prune_missing_dir(os.path.join(self.root, "keep")), "it still exists")
        self.assertFalse(w._may_prune_missing_dir(self.root))
        self.assertFalse(w._may_prune_missing_dir(self.root + os.sep))
        # a scan root below another one is still a scan root
        nested = os.path.join(self.root, "nested")
        self.roots = [self.root, nested]
        self.assertFalse(w._may_prune_missing_dir(nested))
        self.roots = [self.root]
        self.assertTrue(w._may_prune_missing_dir(nested))
        # a sibling whose name only starts like the root is not below it
        self.assertFalse(w._may_prune_missing_dir(self.root + "x" + os.sep + "gone"))
        self.assertFalse(w._may_prune_missing_dir(os.path.join(self.workdir, "elsewhere")))
        self.assertFalse(w._may_prune_missing_dir(os.path.join(self.root, "..", "elsewhere", "x")))

    def test_guard_needs_a_present_and_non_empty_root(self):
        w = self.worker
        folder = os.path.join(self.root, "gone")
        self.assertFalse(w._may_prune_missing_dir(folder), "empty root")
        self.png("keep", "k.png")
        self.assertTrue(w._may_prune_missing_dir(folder))
        shutil.rmtree(self.root)
        self.assertFalse(w._may_prune_missing_dir(folder), "missing root")

    def test_a_path_below_a_file_is_not_pruned(self):
        # gone (ENOTDIR), but the nearest existing ancestor is not a directory
        blocker = self.png("keep", "file.png")
        self.assertFalse(self.worker._may_prune_missing_dir(os.path.join(blocker, "sub")))

    # -- unmounted volumes (the review findings) ------------------------------
    def test_unmounted_mount_point_below_the_scan_root_keeps_its_rows(self):
        self.png("keep", "k.png")
        x = self.png("nas", "2024", "x.png")
        self.cycle()
        x_id = self.like(x)
        shutil.rmtree(os.path.join(self.root, "nas", "2024"))  # the volume is unmounted: an empty directory
        self.cycle()
        self.cycle()
        self.assertAllStay([(x, x_id)])
        self.assertIn(os.path.join(self.root, "nas", "2024"), self.folder_marks())
        # a file in the mount point (.DS_Store) makes it look like a real folder: known limit
        with open(os.path.join(self.root, "nas", ".DS_Store"), "wb") as f:
            f.write(b"x")
        self.cycle()
        self.assertNotIndexed(x)

    def nested_tree(self):
        self.png("keep", "k.png")
        self.ext = os.path.join(self.root, "ext")
        self.e = self.png("ext", "sub", "e.png")
        self.roots = [self.root, self.ext]
        self.cycle()
        self.e_id = self.like(self.e)

    def nested_mount_point(self, filtered):
        self.nested_tree()
        shutil.rmtree(self.ext)
        os.makedirs(self.ext)
        # production leaves a missing or unusable directory out of the scan paths
        self.roots = [self.root] if filtered else [self.root, self.ext]
        self.cycle()
        self.cycle()
        self.assertAllStay([(self.e, self.e_id)])

    def test_nested_scan_root_that_is_an_empty_mount_point(self):
        self.nested_mount_point(filtered=False)

    def test_nested_scan_root_that_is_an_empty_mount_point_and_left_out_of_the_scan_paths(self):
        self.nested_mount_point(filtered=True)

    def test_nested_scan_root_that_is_gone_entirely(self):
        self.nested_tree()
        shutil.rmtree(self.ext)
        self.roots = [self.root, self.ext]
        self.cycle()
        self.assertAllStay([(self.e, self.e_id)])  # the innermost scan root does not exist
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
        self.assertAllStay([(e, e_id)])
        self.assertIn(os.path.dirname(e), self.folder_marks())


if __name__ == "__main__":
    unittest.main()
