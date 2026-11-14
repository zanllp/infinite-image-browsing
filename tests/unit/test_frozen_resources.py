import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.iib.tool import get_data_file_path


class FrozenResourcesTest(unittest.TestCase):
    def test_pyinstaller_resources_are_relative_to_extraction_root(self):
        with tempfile.TemporaryDirectory(prefix="iib resources ") as root:
            with patch.object(sys, "_MEIPASS", root, create=True):
                for filename in ("vue/dist/index.html", "vue/dist"):
                    self.assertEqual(
                        get_data_file_path(filename), os.path.normpath(os.path.join(root, filename))
                    )

    def test_source_resources_are_relative_to_repository(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        self.assertEqual(
            get_data_file_path("vue/dist/index.html"),
            os.path.normpath(os.path.join(root, "vue/dist/index.html")),
        )


if __name__ == "__main__":
    unittest.main()
