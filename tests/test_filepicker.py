"""filepicker 模块测试：目录列举、模式与扩展名过滤。"""

from __future__ import annotations

import os
import tempfile
import unittest

from bilimon import filepicker
from bilimon.collector import CollectError


class FilepickerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name

        os.makedirs(os.path.join(self.base, "sub"))
        for name in ("a.json", "b.json", "note.txt"):
            with open(os.path.join(self.base, name), "w", encoding="utf-8") as handle:
                handle.write("{}")

    def list(self, **kwargs):
        return filepicker.list_directory(self.base, **kwargs)


class ListingTest(FilepickerTestBase):
    def test_lists_dirs_and_matching_files(self):
        data = self.list(exts=["json"])

        self.assertEqual([item["name"] for item in data["dirs"]], ["sub"])
        self.assertEqual([item["name"] for item in data["files"]], ["a.json", "b.json"])
        self.assertEqual(data["path"], os.path.abspath(self.base))

    def test_reports_parent(self):
        data = self.list()
        self.assertEqual(data["parent"], os.path.dirname(os.path.abspath(self.base)))

    def test_missing_directory_raises(self):
        with self.assertRaises(CollectError):
            filepicker.list_directory(os.path.join(self.base, "nope"))

    def test_empty_path_returns_computer_root(self):
        data = filepicker.list_directory("")

        self.assertTrue(data["isRoot"])
        self.assertEqual(data["path"], "")
        self.assertEqual(data["files"], [])

    def test_root_view_lists_drives_as_dirs(self):
        data = filepicker.list_directory("")

        self.assertTrue(all(item.get("drive") for item in data["dirs"]))


class ModeTest(FilepickerTestBase):
    def test_dirs_mode_hides_files(self):
        data = self.list(mode="dirs")
        self.assertEqual(data["files"], [])
        self.assertEqual(len(data["dirs"]), 1)

    def test_files_mode_keeps_dirs_for_navigation(self):
        data = self.list(mode="files", exts=["json"])
        self.assertEqual(len(data["dirs"]), 1)
        self.assertEqual([item["name"] for item in data["files"]], ["a.json", "b.json"])

    def test_custom_extension_filter(self):
        with open(os.path.join(self.base, "c.md"), "w", encoding="utf-8") as handle:
            handle.write("#")

        data = self.list(exts=["md"])
        self.assertEqual([item["name"] for item in data["files"]], ["c.md"])

    def test_empty_exts_allows_every_file(self):
        data = self.list(exts=[])
        self.assertEqual(len(data["files"]), 3)

    def test_unknown_mode_falls_back_to_any(self):
        data = self.list(mode="whatever", exts=["json"])
        self.assertEqual(data["mode"], "any")
        self.assertEqual(len(data["files"]), 2)


if __name__ == "__main__":
    unittest.main()
