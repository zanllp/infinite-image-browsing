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
from scripts.iib.db.datamodel import DataBase, ExtraPath, Image as DbImg

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
