"""Tests for the endpoints that change files on disk.

The endpoints are driven through the real FastAPI app with ``TestClient``, so
routing, dependency injection and each endpoint's own ordering (check every
path first, then act) are covered rather than re-implemented here.

Access control is switched per test by patching
``scripts.iib.api.enable_access_control``. Every test works inside its own
temporary directory tree; nothing outside it is read or written.

Run from the repository root::

    python -m pytest tests/unit/test_api_file_write.py
"""
import os
import contextlib
import shutil
import tempfile
import unittest
from unittest.mock import patch

# scripts.iib.api reads its configuration while being imported, so these have
# to be in place before the import below.
#
# The directory is fixed and wiped on import rather than random: the app under
# test opens the database from a background thread, so the file stays locked
# and the teardown below can only remove what it still can. Wiping on import
# gives every run a clean database and keeps at most one directory around.
_TMP_ROOT = os.path.join(tempfile.gettempdir(), "iib_api_file_write")
shutil.rmtree(_TMP_ROOT, ignore_errors=True)
os.makedirs(_TMP_ROOT, exist_ok=True)
os.environ["IIB_DB_PATH"] = os.path.join(_TMP_ROOT, "iib.db")
os.environ["IIB_DB_FILE_BACKUP_DELAY"] = "9999"
os.environ.pop("IIB_SECRET_KEY", None)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.iib import api as iib_api
from scripts.iib import organize_files
from scripts.iib.db.datamodel import DataBase, ExtraPath, Folder, Image as DbImg, ImageTag, Tag

API = "/infinite_image_browsing"
DB_API = API + "/db"

_app = FastAPI()
iib_api.infinite_image_browsing_api(_app)
_client = TestClient(_app)


def tearDownModule():
    # the sqlite connection holds the database file open, so close it before
    # removing the tree, otherwise the cleanup silently leaves it behind
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
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


class _FileWriteTestCase(unittest.TestCase):
    """One scanned root and one outside folder per test."""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(dir=_TMP_ROOT)
        self.scan = os.path.join(self.workdir, "scan")
        self.outside = os.path.join(self.workdir, "outside")
        os.makedirs(os.path.join(self.scan, "sub"))
        os.makedirs(self.outside)
        self._saved_paths = iib_api.mem["all_scanned_paths"]
        iib_api.mem["all_scanned_paths"] = [self.scan]
        self.client = _client
        # every test starts with access control on, the case that guards writes
        self.access_control(True)

    def tearDown(self):
        iib_api.mem["all_scanned_paths"] = self._saved_paths
        shutil.rmtree(self.workdir, ignore_errors=True)

    def access_control(self, enabled):
        patcher = patch.object(iib_api, "enable_access_control", enabled)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers ---------------------------------------------------------
    def inside(self, name):
        path = os.path.join(self.scan, name)
        self.touch(path)
        return path

    def outside_path(self, name):
        path = os.path.join(self.outside, name)
        self.touch(path)
        return path

    @staticmethod
    def touch(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")

    def db_row(self, path):
        return DbImg.get(DataBase.get_conn(), path)

    def assertStatus(self, response, expected):
        self.assertEqual(
            response.status_code,
            expected,
            f"expected {expected}, got {response.status_code}: {response.text}",
        )


class RenameTest(_FileWriteTestCase):
    """/db/rename must only ever change the name of a file."""

    def test_rejects_names_that_are_not_a_plain_file_name(self):
        cases = [
            "../escape.jpg",
            "../../escape.jpg",
            "sub/x.jpg",
            "sub\\x.jpg",
            "C:\\evil.jpg",
            ".",
            "..",
            "",
        ]
        for name in cases:
            with self.subTest(name=name):
                src = self.inside("rename_src.jpg")
                self.assertStatus(
                    self.client.post(f"{DB_API}/rename", json={"path": src, "name": name}), 400
                )
                self.assertTrue(os.path.exists(src), "the file must stay where it was")
                self.assertFalse(
                    os.path.exists(os.path.join(self.workdir, "escape.jpg")),
                    "the file escaped the scanned folder",
                )
                os.remove(src)

    def test_renames_a_file_inside_the_scanned_path(self):
        src = self.inside("before.jpg")
        r = self.client.post(f"{DB_API}/rename", json={"path": src, "name": "after.jpg"})
        self.assertStatus(r, 200)
        dest = os.path.join(self.scan, "after.jpg")
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.exists(dest))
        self.assertEqual(os.path.normpath(r.json()["new_path"]), os.path.normpath(dest))

    def test_missing_source_is_404(self):
        missing = os.path.join(self.scan, "nope.jpg")
        r = self.client.post(f"{DB_API}/rename", json={"path": missing, "name": "x.jpg"})
        self.assertStatus(r, 404)

    def test_existing_target_is_rejected(self):
        src = self.inside("one.jpg")
        taken = self.inside("two.jpg")
        r = self.client.post(f"{DB_API}/rename", json={"path": src, "name": "two.jpg"})
        self.assertStatus(r, 400)
        self.assertTrue(os.path.exists(src))
        self.assertTrue(os.path.exists(taken))

    def test_outside_source_is_refused(self):
        src = self.outside_path("outside.jpg")
        r = self.client.post(f"{DB_API}/rename", json={"path": src, "name": "renamed.jpg"})
        self.assertStatus(r, 403)
        self.assertTrue(os.path.exists(src))

    def test_database_row_follows_the_file(self):
        src = self.inside("tracked.jpg")
        self.assertStatus(
            self.client.post(f"{API}/update_exif", json={"path": src, "exif": "{}"}), 200
        )
        self.assertIsNotNone(self.db_row(src), "precondition: the row exists")

        self.assertStatus(
            self.client.post(f"{DB_API}/rename", json={"path": src, "name": "tracked2.jpg"}), 200
        )
        dest = os.path.join(self.scan, "tracked2.jpg")
        self.assertIsNone(self.db_row(src), "the old path must not keep a row")
        self.assertIsNotNone(self.db_row(dest), "the row must follow the file")


class DeleteFilesTest(_FileWriteTestCase):
    def test_deletes_files_inside_the_scanned_path(self):
        first = self.inside("a.jpg")
        second = self.inside(os.path.join("sub", "b.jpg"))
        r = self.client.post(f"{API}/delete_files", json={"file_paths": [first, second]})
        self.assertStatus(r, 200)
        self.assertFalse(os.path.exists(first))
        self.assertFalse(os.path.exists(second))

    def test_already_missing_file_counts_as_deleted(self):
        path = self.inside("gone.jpg")
        self.assertStatus(
            self.client.post(f"{API}/update_exif", json={"path": path, "exif": "{}"}), 200
        )
        os.remove(path)  # deleted outside iib, e.g. in an explorer window
        self.assertIsNotNone(self.db_row(path), "precondition: the row exists")

        self.assertStatus(self.client.post(f"{API}/delete_files", json={"file_paths": [path]}), 200)
        self.assertIsNone(self.db_row(path), "the stale row must be cleaned up")

    def test_one_path_outside_stops_the_whole_request(self):
        keep = self.inside("keep.jpg")
        intruder = self.outside_path("intruder.jpg")
        r = self.client.post(f"{API}/delete_files", json={"file_paths": [keep, intruder]})
        self.assertStatus(r, 403)
        self.assertTrue(os.path.exists(keep), "nothing may be deleted when a path is refused")
        self.assertTrue(os.path.exists(intruder))

    def test_parent_of_the_scanned_root_is_refused(self):
        r = self.client.post(f"{API}/delete_files", json={"file_paths": [self.workdir]})
        self.assertStatus(r, 403)


class DeleteFolderTest(_FileWriteTestCase):
    """A folder that is not empty is deleted with its content, only when asked to."""

    def indexed(self, name, tag=None):
        """A file with a row in the database, optionally carrying a tag of the user."""
        path = self.inside(name)
        self.assertStatus(
            self.client.post(f"{API}/update_exif", json={"path": path, "exif": "{}"}), 200
        )
        conn = DataBase.get_conn()
        row = DbImg.get(conn, path)
        if tag:
            ImageTag(row.id, Tag.get_or_create(conn, name=tag, type="custom").id).save(conn)
        conn.execute(
            "INSERT INTO image_embedding (image_id, model, dim, text_hash, vec, updated_at)"
            " VALUES (?, 'm', 1, 'h', x'00', '')",
            (row.id,),
        )
        conn.commit()
        return path

    def count_rows(self, table, where="1", params=()):
        return DataBase.get_conn().execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", params
        ).fetchone()[0]

    def rows_under(self, folder):
        """All rows left for images below the folder, in the tables that hang on an image."""
        prefix = folder + os.sep
        under = "substr(path, 1, ?) = ?"
        ids = f"(SELECT id FROM image WHERE {under})"
        args = (len(prefix), prefix)
        return (
            self.count_rows("image", under, args),
            self.count_rows("image_tag", f"image_id IN {ids}", args),
            self.count_rows("image_embedding", f"image_id IN {ids}", args),
        )

    def post_delete(self, paths, recursive=None):
        body = {"file_paths": paths}
        if recursive is not None:
            body["recursive"] = recursive
        return self.client.post(f"{API}/delete_files", json=body)

    def test_empty_folder_is_deleted_without_recursive(self):
        folder = os.path.join(self.scan, "empty")
        os.makedirs(folder)
        self.assertStatus(self.post_delete([folder]), 200)
        self.assertFalse(os.path.exists(folder))

    def test_folder_with_content_is_refused_without_recursive(self):
        keep = self.indexed(os.path.join("full", "a.jpg"))
        folder = os.path.dirname(keep)
        for recursive in (None, False):
            self.assertStatus(self.post_delete([folder], recursive), 400)
        self.assertTrue(os.path.exists(keep))
        self.assertIsNotNone(self.db_row(keep))

    def test_deletes_files_subfolders_sidecars_and_all_rows(self):
        folder = os.path.join(self.scan, "full")
        liked = self.indexed(os.path.join("full", "a.jpg"), tag="like")
        deep = self.indexed(os.path.join("full", "x", "y", "b.jpg"), tag="mine")
        self.touch(os.path.join(folder, "a.txt"))
        os.makedirs(os.path.join(folder, "empty-sub"))
        sibling = self.indexed(os.path.join("full2", "c.jpg"), tag="like")
        for p in (liked, deep):
            self.assertIsNotNone(self.db_row(p), "precondition: the row exists")
        # the scan mark of the folders, as the indexer writes it
        Folder.update_modified_date_or_create(DataBase.get_conn(), folder)
        Folder.update_modified_date_or_create(DataBase.get_conn(), os.path.dirname(deep))
        Folder.update_modified_date_or_create(DataBase.get_conn(), os.path.dirname(sibling))
        DataBase.get_conn().commit()

        self.assertStatus(self.post_delete([folder], True), 200)

        self.assertFalse(os.path.exists(folder))
        self.assertEqual(self.rows_under(folder), (0, 0, 0))
        self.assertEqual(
            self.count_rows("folders", "path = ? OR substr(path, 1, ?) = ?", (folder, len(folder) + 1, folder + os.sep)),
            0,
        )
        # neighbours with a similar name keep their rows
        self.assertTrue(os.path.exists(sibling))
        self.assertIsNotNone(self.db_row(sibling))
        self.assertEqual(self.rows_under(os.path.dirname(sibling)), (1, 1, 1))
        self.assertEqual(self.count_rows("folders", "path = ?", (os.path.dirname(sibling),)), 1)

    def test_wildcards_in_the_folder_name_do_not_match_other_folders(self):
        victim = self.indexed(os.path.join("a_b", "x.jpg"))
        neighbour = self.indexed(os.path.join("aXb", "x.jpg"))
        percent = self.indexed(os.path.join("100%", "x.jpg"))
        neighbour2 = self.indexed(os.path.join("1000", "x.jpg"))
        self.assertStatus(self.post_delete([os.path.dirname(victim), os.path.dirname(percent)], True), 200)
        self.assertIsNone(self.db_row(victim))
        self.assertIsNone(self.db_row(percent))
        self.assertIsNotNone(self.db_row(neighbour))
        self.assertIsNotNone(self.db_row(neighbour2))

    def test_files_and_folders_in_one_request(self):
        single = self.indexed("single.jpg")
        inner = self.indexed(os.path.join("f", "a.jpg"))
        self.assertStatus(self.post_delete([single, os.path.dirname(inner)], True), 200)
        self.assertFalse(os.path.exists(single))
        self.assertFalse(os.path.exists(os.path.dirname(inner)))
        self.assertIsNone(self.db_row(single))
        self.assertIsNone(self.db_row(inner))

    def test_already_missing_folder_counts_as_deleted(self):
        folder = os.path.join(self.scan, "gone")
        self.assertStatus(self.post_delete([folder], True), 200)

    def test_outside_folder_is_refused_and_nothing_is_deleted(self):
        keep = self.indexed("keep.jpg")
        intruder = self.outside_path("x.jpg")
        r = self.post_delete([keep, os.path.dirname(intruder)], True)
        self.assertStatus(r, 403)
        self.assertTrue(os.path.exists(keep))
        self.assertTrue(os.path.exists(intruder))

    def test_scanned_path_and_its_parent_are_refused(self):
        keep = self.indexed("keep.jpg")
        for folder in (self.scan, self.workdir):
            self.assertStatus(self.post_delete([folder], True), 403)
        self.assertTrue(os.path.exists(keep))
        self.assertIsNotNone(self.db_row(keep))

    def test_folder_that_holds_a_scanned_path_is_refused(self):
        nested = os.path.join(self.scan, "sub", "nested")
        self.touch(os.path.join(nested, "a.jpg"))
        iib_api.mem["all_scanned_paths"] = [self.scan, nested]
        self.assertStatus(self.post_delete([os.path.join(self.scan, "sub")], True), 403)
        self.assertStatus(self.post_delete([nested], True), 403)
        self.assertTrue(os.path.exists(os.path.join(nested, "a.jpg")))

    def test_empty_scanned_path_is_still_deleted_as_before(self):
        folder = os.path.join(self.scan, "sub")  # empty, set up in setUp
        iib_api.mem["all_scanned_paths"] = [self.scan, folder]
        self.assertStatus(self.post_delete([folder], True), 200)

    def test_without_access_control_the_library_boundary_still_holds(self):
        self.access_control(False)
        outside = os.path.dirname(self.outside_path("x.jpg"))
        self.assertStatus(self.post_delete([outside], True), 403)
        self.assertTrue(os.path.exists(os.path.join(outside, "x.jpg")))
        inside = os.path.dirname(self.indexed(os.path.join("in", "a.jpg")))
        self.assertStatus(self.post_delete([inside], True), 200)
        self.assertFalse(os.path.exists(inside))

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_links_are_removed_not_followed(self):
        target = self.outside_path("target.jpg")
        folder = os.path.join(self.scan, "linked")
        os.makedirs(folder)
        try:
            os.symlink(os.path.dirname(target), os.path.join(folder, "dirlink"))
            os.symlink(target, os.path.join(folder, "filelink.jpg"))
        except OSError:
            self.skipTest("cannot create symlinks")
        r = self.client.post(f"{API}/folder_content", json={"folder_paths": [folder]})
        self.assertEqual((r.json()["folders"][0]["files"], r.json()["folders"][0]["folders"]), (2, 0))
        self.assertStatus(self.post_delete([folder], True), 200)
        self.assertFalse(os.path.exists(folder))
        self.assertTrue(os.path.exists(target), "what a link points to must survive")

    def test_failure_halfway_keeps_the_rows_of_the_files_left(self):
        gone = self.indexed(os.path.join("part", "a.jpg"), tag="like")
        left = self.indexed(os.path.join("part", "b.jpg"), tag="like")
        folder = os.path.dirname(gone)
        os.remove(gone)
        Folder.remove_missing_under(DataBase.get_conn(), folder)
        DbImg.remove_missing_under(DataBase.get_conn(), folder)
        self.assertIsNone(self.db_row(gone))
        self.assertIsNotNone(self.db_row(left))
        self.assertEqual(self.rows_under(folder), (1, 1, 1))

    def test_failing_rmtree_removes_only_the_rows_of_what_is_gone(self):
        gone = self.indexed(os.path.join("part", "a.jpg"))
        left = self.indexed(os.path.join("part", "b.jpg"))
        folder = os.path.dirname(gone)

        def rmtree_that_stops(path):
            os.remove(gone)
            raise PermissionError("denied")

        with patch.object(iib_api.shutil, "rmtree", rmtree_that_stops):
            self.assertStatus(self.post_delete([folder], True), 400)
        self.assertTrue(os.path.exists(left))
        self.assertIsNone(self.db_row(gone))
        self.assertIsNotNone(self.db_row(left))

    def link_to(self, target, name):
        link = os.path.join(self.scan, name)
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks")
        return link

    def test_dotdot_after_a_link_does_not_touch_rows_of_other_folders(self):
        # on disk scan/link/../b is scan/x/b, as text it is scan/b
        target = os.path.join(self.scan, "x", "y")
        os.makedirs(target)
        os.makedirs(os.path.join(self.scan, "x", "b"))
        link = self.link_to(target, "link")
        keep = self.indexed(os.path.join("b", "keep.jpg"), tag="like")
        path = os.path.join(link, "..", "b")
        for recursive in (None, True):
            self.assertStatus(self.post_delete([path], recursive), 400)
        r = self.client.post(f"{API}/folder_content", json={"folder_paths": [path]})
        self.assertStatus(r, 400)
        self.assertTrue(os.path.isdir(os.path.join(self.scan, "x", "b")))
        self.assertTrue(os.path.exists(keep))
        self.assertEqual(self.rows_under(os.path.join(self.scan, "b")), (1, 1, 1))

    def test_rows_below_the_path_the_file_system_resolved_are_removed(self):
        target = os.path.join(self.scan, "x", "y")
        os.makedirs(target)
        link = self.link_to(target, "lk")
        victim = self.indexed(os.path.join("x", "y", "b", "f.jpg"), tag="like")
        self.assertStatus(self.post_delete([os.path.join(link, "b")], True), 200)
        self.assertFalse(os.path.exists(victim))
        self.assertIsNone(self.db_row(victim))
        self.assertEqual(self.rows_under(os.path.join(target, "b")), (0, 0, 0))

    def test_link_to_a_folder_is_refused_with_or_without_a_slash(self):
        kept = self.indexed(os.path.join("target", "k.jpg"), tag="like")
        link = self.link_to(os.path.dirname(kept), "ln")
        for path in (link, link + os.sep):
            for recursive in (None, True):
                r = self.post_delete([path], recursive)
                self.assertStatus(r, 400)
                self.assertIn("link", r.json()["detail"])
        self.assertTrue(os.path.exists(kept), "what the link points to must survive")
        self.assertIsNotNone(self.db_row(kept))

    def test_folder_name_with_other_case_is_refused(self):
        # a file system that ignores case reaches the folder with this name, the rows are found by the exact text
        self.indexed(os.path.join("sub2", "k.jpg"))
        wrong = os.path.join(self.scan, "SUB2")
        real_isdir = os.path.isdir
        with patch.object(os.path, "isdir", lambda p: real_isdir(p) or p == wrong), patch.object(
            iib_api.shutil, "rmtree"
        ) as rmtree:
            self.assertStatus(self.post_delete([wrong], True), 400)
        rmtree.assert_not_called()

    def test_scanned_paths_are_compared_by_the_file_system(self):
        sub = os.path.join(self.scan, "sub")
        nested = os.path.join(sub, "nested")
        os.makedirs(nested)
        alias = self.link_to(sub, "alias")
        # the text of alias is not a parent of nested, the folder is the same
        self.assertTrue(iib_api.is_same_or_parent(alias, nested))
        self.assertTrue(iib_api.is_same_or_parent(sub, sub))
        self.assertFalse(iib_api.is_same_or_parent(nested, sub))
        self.assertFalse(iib_api.is_same_or_parent(os.path.join(self.scan, "missing"), nested))
        # where the file system cannot tell (inodes of FUSE, SMB, NFS), the text of the paths still does
        missing = os.path.join(self.scan, "missing")
        self.assertTrue(iib_api.is_same_or_parent(missing, os.path.join(missing, "x")))

    def test_folder_that_got_a_file_after_the_check_is_not_deleted_as_empty(self):
        folder = os.path.join(self.scan, "sub")  # empty, and a scanned path
        iib_api.mem["all_scanned_paths"] = [self.scan, folder]
        late = os.path.join(folder, "late.jpg")
        state = {"filled": False}

        def fill_after_first_look(real, wrap):
            def look(path, *args, **kwargs):
                if not isinstance(path, str) or path != folder or state["filled"]:
                    return real(path, *args, **kwargs)
                state["filled"] = True
                seen = wrap(real(path, *args, **kwargs))  # empty
                self.touch(late)
                return seen

            return look

        real_scandir, real_listdir = os.scandir, os.listdir
        with patch.object(
            os, "scandir", fill_after_first_look(real_scandir, lambda it: contextlib.nullcontext(iter(list(it))))
        ), patch.object(os, "listdir", fill_after_first_look(real_listdir, lambda names: names)):
            r = self.post_delete([folder], True)
        self.assertIn(r.status_code, (400, 403), r.text)
        self.assertTrue(os.path.exists(late), "the file that came in is not deleted")

    def unreadable(self, folder):
        os.chmod(folder, 0)

        def restore():
            # tearDown has already removed the folder when the test gave the access back
            with contextlib.suppress(FileNotFoundError):
                os.chmod(folder, 0o755)

        self.addCleanup(restore)

    skip_unless_held_back = unittest.skipIf(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        "needs a user that is held back by permissions",
    )

    @skip_unless_held_back
    def test_unreadable_folder_is_a_400_and_keeps_its_rows(self):
        kept = self.indexed(os.path.join("closed", "k.jpg"), tag="like")
        folder = os.path.dirname(kept)
        self.unreadable(folder)
        for recursive in (None, True):
            self.assertStatus(self.post_delete([folder], recursive), 400)
        r = self.client.post(f"{API}/folder_content", json={"folder_paths": [folder]})
        self.assertStatus(r, 400)
        os.chmod(folder, 0o755)
        self.assertTrue(os.path.exists(kept))
        self.assertEqual(self.rows_under(folder), (1, 1, 1), "the row, the like and the embedding of a file that is there")

    @skip_unless_held_back
    def test_unreadable_subfolder_keeps_the_rows_of_the_files_in_it(self):
        locked = self.indexed(os.path.join("outer", "locked", "b.jpg"), tag="like")
        folder = os.path.join(self.scan, "outer")
        self.unreadable(os.path.dirname(locked))
        self.assertStatus(self.post_delete([folder], True), 400)
        os.chmod(os.path.dirname(locked), 0o755)
        self.assertTrue(os.path.exists(locked), "rmtree could not get into it")
        self.assertEqual(self.rows_under(os.path.dirname(locked)), (1, 1, 1))

    def test_folder_content_checks_before_it_counts(self):
        self.access_control(False)
        outside = os.path.dirname(self.outside_path("x.jpg"))
        with patch.object(os, "walk", side_effect=AssertionError("counted before the check")):
            r = self.client.post(f"{API}/folder_content", json={"folder_paths": [outside]})
        self.assertStatus(r, 403)

    def test_folder_content_counts_files_and_subfolders(self):
        self.indexed(os.path.join("c", "a.jpg"), tag="like")
        self.indexed(os.path.join("c", "s", "b.jpg"))
        self.indexed(os.path.join("c", "s", "t", "m.jpg"))
        self.touch(os.path.join(self.scan, "c", "a.txt"))
        os.makedirs(os.path.join(self.scan, "c", "empty"))
        self.indexed(os.path.join("other", "z.jpg"))
        folder = os.path.join(self.scan, "c")

        r = self.client.post(f"{API}/folder_content", json={"folder_paths": [folder]})
        self.assertStatus(r, 200)
        self.assertEqual(r.json()["folders"], [{"path": folder, "files": 4, "folders": 3}])
        self.assertTrue(os.path.exists(folder), "counting deletes nothing")

    def test_folder_content_of_an_empty_or_missing_folder_is_zero(self):
        empty = os.path.join(self.scan, "sub")
        missing = os.path.join(self.scan, "missing")
        r = self.client.post(f"{API}/folder_content", json={"folder_paths": [empty, missing]})
        self.assertStatus(r, 200)
        for entry in r.json()["folders"]:
            self.assertEqual((entry["files"], entry["folders"]), (0, 0))

    def test_folder_content_is_refused_where_the_delete_would_be(self):
        self.indexed("keep.jpg")
        for folder in (os.path.dirname(self.outside_path("x.jpg")), self.workdir, self.scan):
            r = self.client.post(f"{API}/folder_content", json={"folder_paths": [folder]})
            self.assertStatus(r, 403)


class CopyFilesTest(_FileWriteTestCase):
    def test_refused_source_does_not_create_the_destination(self):
        src = self.outside_path("src.jpg")
        dest = os.path.join(self.scan, "created")
        r = self.client.post(
            f"{API}/copy_files",
            json={"file_paths": [src], "dest": dest, "create_dest_folder": True},
        )
        self.assertStatus(r, 403)
        self.assertFalse(os.path.isdir(dest), "the destination must not be created")

    def test_refused_destination_copies_nothing(self):
        src = self.inside("src.jpg")
        dest = os.path.join(self.outside, "nowhere")
        r = self.client.post(
            f"{API}/copy_files",
            json={"file_paths": [src], "dest": dest, "create_dest_folder": True},
        )
        self.assertStatus(r, 403)
        self.assertFalse(os.path.isdir(dest))
        self.assertTrue(os.path.exists(src))

    def test_copies_inside_the_scanned_path(self):
        src = self.inside("src.jpg")
        dest = os.path.join(self.scan, "dest")
        r = self.client.post(
            f"{API}/copy_files",
            json={"file_paths": [src], "dest": dest, "create_dest_folder": True},
        )
        self.assertStatus(r, 200)
        self.assertTrue(os.path.exists(os.path.join(dest, "src.jpg")))
        self.assertTrue(os.path.exists(src), "a copy leaves the source alone")


class MoveFilesTest(_FileWriteTestCase):
    def test_refused_source_moves_nothing(self):
        src = self.outside_path("src.jpg")
        dest = os.path.join(self.scan, "created")
        r = self.client.post(
            f"{API}/move_files",
            json={"file_paths": [src], "dest": dest, "create_dest_folder": True},
        )
        self.assertStatus(r, 403)
        self.assertTrue(os.path.exists(src))
        self.assertFalse(os.path.isdir(dest), "the destination must not be created")

    def test_moves_inside_the_scanned_path(self):
        src = self.inside("src.jpg")
        dest = os.path.join(self.scan, "dest")
        r = self.client.post(
            f"{API}/move_files",
            json={"file_paths": [src], "dest": dest, "create_dest_folder": True},
        )
        self.assertStatus(r, 200)
        self.assertTrue(os.path.exists(os.path.join(dest, "src.jpg")))
        self.assertFalse(os.path.exists(src))


class UpdateExifTest(_FileWriteTestCase):
    def test_outside_path_is_refused_and_writes_no_row(self):
        path = self.outside_path("exif.jpg")
        r = self.client.post(f"{API}/update_exif", json={"path": path, "exif": "{}"})
        self.assertStatus(r, 403)
        self.assertIsNone(self.db_row(path), "no row may be created for an outside path")

    def test_inside_path_is_allowed(self):
        path = self.inside("exif.jpg")
        r = self.client.post(f"{API}/update_exif", json={"path": path, "exif": "{}"})
        self.assertStatus(r, 200)
        self.assertIsNotNone(self.db_row(path))


class FlattenFolderTest(_FileWriteTestCase):
    def test_the_scanned_root_itself_is_allowed(self):
        self.inside("flat.jpg")
        r = self.client.post(
            f"{API}/flatten_folder", json={"folder_path": self.scan, "dry_run": True}
        )
        self.assertStatus(r, 200)

    def test_a_child_folder_is_allowed(self):
        r = self.client.post(
            f"{API}/flatten_folder",
            json={"folder_path": os.path.join(self.scan, "sub"), "dry_run": True},
        )
        self.assertStatus(r, 200)

    def test_outside_folder_is_refused(self):
        r = self.client.post(
            f"{API}/flatten_folder", json={"folder_path": self.outside, "dry_run": True}
        )
        self.assertStatus(r, 403)


class ExtraPathsTest(_FileWriteTestCase):
    def test_outside_path_is_refused_and_not_stored(self):
        target = os.path.join(self.outside, "extra")
        os.makedirs(target)
        r = self.client.post(f"{DB_API}/extra_paths", json={"path": target, "types": ["walk"]})
        self.assertStatus(r, 403)
        self.assertIsNone(ExtraPath.get_target_path(DataBase.get_conn(), target))

    def test_inside_path_is_stored(self):
        target = os.path.join(self.scan, "extra")
        os.makedirs(target)
        r = self.client.post(f"{DB_API}/extra_paths", json={"path": target, "types": ["walk"]})
        self.assertStatus(r, 200)
        self.assertIsNotNone(ExtraPath.get_target_path(DataBase.get_conn(), target))


class OrganizeFilesTest(_FileWriteTestCase):
    def _job_count(self):
        return len(organize_files._ORGANIZE_JOBS)

    def test_outside_folder_is_refused_and_starts_no_job(self):
        before = self._job_count()
        r = self.client.post(
            f"{DB_API}/organize_files_start", json={"folder_paths": [self.outside]}
        )
        self.assertStatus(r, 403)
        self.assertEqual(self._job_count(), before, "no job may be queued")

    def test_outside_destination_is_refused_and_starts_no_job(self):
        before = self._job_count()
        r = self.client.post(
            f"{DB_API}/organize_files_start",
            json={
                "folder_paths": [os.path.join(self.scan, "sub")],
                "dest_folder": self.outside,
            },
        )
        self.assertStatus(r, 403)
        self.assertEqual(self._job_count(), before, "no job may be queued")


class ImportUserDataTest(_FileWriteTestCase):
    @staticmethod
    def _document(extra_paths):
        return {
            "format": "iib-user-data",
            "format_version": 1,
            "extra_paths": [
                {"path": p, "type": "walk", "alias": ""} for p in extra_paths
            ],
        }

    def _import(self, extra_paths):
        r = self.client.post(
            f"{DB_API}/import_user_data",
            json={"data": self._document(extra_paths), "mode": "merge", "dry_run": True},
        )
        self.assertStatus(r, 200)
        return r.json()["extra_paths"]

    def test_outside_extra_path_counts_as_invalid(self):
        stats = self._import([os.path.join(self.outside, "imported")])
        self.assertEqual(stats["invalid"], 1)
        self.assertEqual(stats["added"], 0)

    def test_inside_and_outside_are_split(self):
        stats = self._import(
            [
                os.path.join(self.scan, "inside_import"),
                os.path.join(self.outside, "outside_import"),
            ]
        )
        self.assertEqual(stats["added"], 1)
        self.assertEqual(stats["invalid"], 1)


class WithoutAccessControlTest(_FileWriteTestCase):
    """Without access control the checks are a no-op: nothing changes."""

    def setUp(self):
        super().setUp()
        self.access_control(False)

    def test_writes_outside_the_scanned_path_still_allowed(self):
        src = self.outside_path("free.jpg")
        self.assertStatus(self.client.post(f"{API}/delete_files", json={"file_paths": [src]}), 200)
        self.assertFalse(os.path.exists(src))

        exif = self.outside_path("exif.jpg")
        r = self.client.post(f"{API}/update_exif", json={"path": exif, "exif": "{}"})
        self.assertStatus(r, 200)

        r = self.client.post(
            f"{API}/flatten_folder", json={"folder_path": self.outside, "dry_run": True}
        )
        self.assertStatus(r, 200)

    def test_copy_and_move_outside_still_allowed(self):
        src = self.outside_path("copy_src.jpg")
        dest = os.path.join(self.scan, "dest")
        self.assertStatus(
            self.client.post(
                f"{API}/copy_files",
                json={"file_paths": [src], "dest": dest, "create_dest_folder": True},
            ),
            200,
        )
        self.assertTrue(os.path.exists(os.path.join(dest, "copy_src.jpg")))

        src2 = self.outside_path("move_src.jpg")
        dest2 = os.path.join(self.scan, "dest2")
        self.assertStatus(
            self.client.post(
                f"{API}/move_files",
                json={"file_paths": [src2], "dest": dest2, "create_dest_folder": True},
            ),
            200,
        )
        self.assertTrue(os.path.exists(os.path.join(dest2, "move_src.jpg")))

    def test_import_keeps_outside_extra_paths(self):
        doc = ImportUserDataTest._document([os.path.join(self.outside, "free_import")])
        r = self.client.post(
            f"{DB_API}/import_user_data", json={"data": doc, "mode": "merge", "dry_run": True}
        )
        self.assertStatus(r, 200)
        stats = r.json()["extra_paths"]
        self.assertEqual(stats["invalid"], 0)
        self.assertEqual(stats["added"], 1)


if __name__ == "__main__":
    unittest.main()
