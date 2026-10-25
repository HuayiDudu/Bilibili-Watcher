"""settings 模块测试：默认值、部分更新与损坏配置的容错。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from bilimon.settings import CONFIG_NAME, Settings


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name
        self.path = os.path.join(self.base, CONFIG_NAME)

    def _write(self, raw) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            if isinstance(raw, str):
                handle.write(raw)
            else:
                json.dump(raw, handle)

    def test_defaults(self):
        settings = Settings(self.base)

        self.assertEqual(
            settings.as_dict(),
            {"auto_save": True, "auto_save_dir": "", "align_clock": False, "import_dirs": []},
        )
        self.assertTrue(settings.auto_save_enabled())
        self.assertIsNone(settings.auto_save_dir())

    def test_update_is_partial(self):
        settings = Settings(self.base)
        settings.update(auto_save=True, auto_save_dir=self.base)

        result = settings.update(align_clock=True)

        self.assertEqual(result["auto_save"], True)
        self.assertEqual(result["auto_save_dir"], self.base)
        self.assertEqual(result["align_clock"], True)

    def test_update_writes_through(self):
        Settings(self.base).update(auto_save=True, auto_save_dir=self.base, align_clock=True)

        reloaded = Settings(self.base)
        self.assertTrue(reloaded.auto_save_enabled())
        self.assertEqual(reloaded.auto_save_dir(), self.base)
        self.assertTrue(reloaded.as_dict()["align_clock"])

    def test_blank_directory_normalises_to_none(self):
        settings = Settings(self.base)
        settings.update(auto_save_dir="   ")

        self.assertIsNone(settings.auto_save_dir())
        self.assertEqual(settings.as_dict()["auto_save_dir"], "")

    def test_as_dict_returns_copy(self):
        settings = Settings(self.base)
        snapshot = settings.as_dict()
        snapshot["auto_save_dir"] = "D:/somewhere"
        snapshot["import_dirs"].append("D:/extra")

        self.assertEqual(settings.as_dict()["auto_save_dir"], "")
        self.assertEqual(settings.as_dict()["import_dirs"], [])

    def test_corrupt_json_falls_back_to_defaults(self):
        self._write("{ this is not json")

        settings = Settings(self.base)
        self.assertEqual(settings.as_dict()["auto_save"], True)

    def test_non_dict_json_falls_back_to_defaults(self):
        self._write([1, 2, 3])

        settings = Settings(self.base)
        self.assertEqual(settings.as_dict()["auto_save_dir"], "")

    def test_unexpected_field_types_are_coerced(self):
        self._write({"auto_save": 1, "auto_save_dir": 123, "align_clock": "yes"})

        settings = Settings(self.base)
        self.assertTrue(settings.auto_save_enabled())
        self.assertIsNone(settings.auto_save_dir())
        self.assertTrue(settings.as_dict()["align_clock"])

    def test_saved_directory_is_stripped(self):
        self._write({"auto_save_dir": "  D:/reports  "})

        self.assertEqual(Settings(self.base).auto_save_dir(), "D:/reports")

    def test_unwritable_path_keeps_values_in_memory(self):
        # 用文件占住目录位置，写盘必然失败；配置仍应在内存中生效
        blocker = os.path.join(self.base, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")

        settings = Settings(blocker)
        settings.update(auto_save=True)

        self.assertTrue(settings.auto_save_enabled())


if __name__ == "__main__":
    unittest.main()
