"""reports 模块测试：结构校验、导入重命名、导出与删除。

全部基于临时目录，不触碰真实 reports/。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from bilimon import reports


def make_report(**overrides):
    document = {
        "report_type": reports.REPORT_TYPE,
        "generated_at": "2026-10-20T09:30:00",
        "video": {"bvid": "BV1TEST00001", "title": "测试报告", "cover": ""},
        "monitor": {
            "id": "m1",
            "sample_count": 2,
            "duration_seconds": 20,
            "interval_seconds": 10,
        },
        "samples": [
            {"time": "2026-10-20T09:30:00", "view": 100},
            {"time": "2026-10-20T09:30:20", "view": 120},
        ],
    }
    document.update(overrides)
    return document


class ReportsTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name
        self.report_dir = os.path.join(self.base, "reports")
        self.source_dir = os.path.join(self.base, "source")
        os.makedirs(self.source_dir)

    def write_source(self, name: str, payload) -> str:
        path = os.path.join(self.source_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return path


class ValidateTest(unittest.TestCase):
    def test_accepts_complete_report(self):
        self.assertIsNone(reports.validate(make_report()))

    def test_rejects_foreign_json(self):
        self.assertIsNotNone(reports.validate({"foo": "bar"}))
        self.assertIsNotNone(reports.validate([]))

    def test_rejects_missing_sections(self):
        self.assertIsNotNone(reports.validate({"report_type": reports.REPORT_TYPE}))
        broken = make_report()
        broken.pop("samples")
        self.assertIsNotNone(reports.validate(broken))


class ImportTest(ReportsTestBase):
    def test_renames_to_canonical_and_writes(self):
        path = self.write_source("随便起的名字.json", make_report())
        saved, skipped = reports.import_paths(self.report_dir, [path])

        self.assertEqual(len(saved), 1)
        self.assertEqual(skipped, [])
        self.assertEqual(saved[0]["name"], "monitor_m1_BV1TEST00001_20261020_093000.json")
        self.assertTrue(os.path.isfile(os.path.join(self.report_dir, saved[0]["name"])))

    def test_directory_import_skips_non_json_and_invalid(self):
        self.write_source("a.json", make_report())
        self.write_source("b.json", {"foo": "bar"})
        with open(os.path.join(self.source_dir, "note.txt"), "w", encoding="utf-8") as handle:
            handle.write("not json")

        saved, skipped = reports.import_paths(self.report_dir, [self.source_dir])

        self.assertEqual(len(saved), 1)
        self.assertEqual([item["name"] for item in skipped], ["b.json"])

    def test_duplicate_import_is_skipped(self):
        path = self.write_source("x.json", make_report())
        reports.import_paths(self.report_dir, [path])
        saved, skipped = reports.import_paths(self.report_dir, [path])

        self.assertEqual(saved, [])
        self.assertEqual([item["reason"] for item in skipped], ["仓库中已有相同报告"])

    def test_import_from_reports_dir_is_skipped(self):
        path = self.write_source("a.json", make_report())
        saved, _ = reports.import_paths(self.report_dir, [path])
        inside = os.path.join(self.report_dir, saved[0]["name"])

        again, skipped = reports.import_paths(self.report_dir, [inside])

        self.assertEqual(again, [])
        self.assertEqual([item["reason"] for item in skipped], ["已在 reports 目录中"])
        self.assertEqual(len(reports.list_saved(self.report_dir)), 1)

    def test_report_without_monitor_id_falls_back(self):
        document = make_report()
        document["monitor"].pop("id")
        path = self.write_source("no_id.json", document)
        saved, _ = reports.import_paths(self.report_dir, [path])

        self.assertTrue(saved[0]["name"].startswith("monitor_m0_"))


class ListAndLoadTest(ReportsTestBase):
    def test_list_sorted_by_generated_at_desc(self):
        older = make_report(generated_at="2026-10-01T08:00:00")
        newer = make_report(generated_at="2026-10-02T08:00:00")
        reports.import_paths(self.report_dir, [
            self.write_source("old.json", older),
            self.write_source("new.json", newer),
        ])

        items = reports.list_saved(self.report_dir)
        self.assertEqual(len(items), 2)
        self.assertGreater(items[0]["generated_at"], items[1]["generated_at"])

    def test_load_saved_returns_document(self):
        path = self.write_source("a.json", make_report())
        saved, _ = reports.import_paths(self.report_dir, [path])
        document = reports.load_saved(self.report_dir, saved[0]["name"])

        self.assertEqual(document["video"]["bvid"], "BV1TEST00001")

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            reports.load_saved(self.report_dir, "../secret.json")

    def test_missing_directory_lists_empty(self):
        self.assertEqual(reports.list_saved(os.path.join(self.base, "nope")), [])


class ExportAndRemoveTest(ReportsTestBase):
    def test_export_copies_and_dedupes(self):
        path = self.write_source("a.json", make_report())
        saved, _ = reports.import_paths(self.report_dir, [path])
        name = saved[0]["name"]
        target = os.path.join(self.base, "out")

        first = reports.export(self.report_dir, [name], target)
        second = reports.export(self.report_dir, [name], target)

        self.assertTrue(os.path.isfile(first[0]))
        self.assertTrue(os.path.basename(second[0]).endswith("-2.json"))

    def test_write_document_for_live_report(self):
        target = os.path.join(self.base, "out")
        written = reports.write_document(target, make_report())

        self.assertTrue(os.path.isfile(written))
        self.assertEqual(os.path.basename(written), "monitor_m1_BV1TEST00001_20261020_093000.json")

    def test_remove_deletes_file(self):
        path = self.write_source("a.json", make_report())
        saved, _ = reports.import_paths(self.report_dir, [path])
        reports.remove(self.report_dir, saved[0]["name"])

        self.assertEqual(reports.list_saved(self.report_dir), [])

    def test_remove_missing_raises(self):
        with self.assertRaises(ValueError):
            reports.remove(self.report_dir, "missing.json")


if __name__ == "__main__":
    unittest.main()
