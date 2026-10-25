"""report 模块测试：聚合口径、时间序列化与落盘。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from bilimon import report
from tests.support import BVID, make_info, make_metrics

T0 = 1_700_000_000_000
MINUTE = 60_000


def _sample(offset_ms: int, **overrides) -> dict:
    metrics = make_metrics(**overrides)
    sample = {"t": T0 + offset_ms}
    sample.update(metrics)
    return sample


def _context(samples, **overrides) -> dict:
    context = {
        "monitor_id": "m3",
        "video": make_info().as_dict(),
        "interval": 60,
        "started_at": samples[0]["t"] if samples else None,
        "stopped_at": samples[-1]["t"] if samples else None,
        "success_count": len(samples),
        "failure_count": 0,
        "last_error": None,
        "samples": samples,
    }
    context.update(overrides)
    return context


class BuildTest(unittest.TestCase):
    def test_empty_samples_yield_zeroed_document(self):
        document = report.build(_context([], started_at=None, stopped_at=None))

        self.assertEqual(document["schema_version"], report.SCHEMA_VERSION)
        self.assertEqual(document["report_type"], report.REPORT_TYPE)
        self.assertEqual(document["monitor"]["sample_count"], 0)
        self.assertEqual(document["monitor"]["duration_seconds"], 0)
        self.assertEqual(document["samples"], [])
        for key in ("view", "online"):
            self.assertEqual(document["metrics"][key]["sample_count"], 0)
            # 无有效点时不应伪造 first/last 等统计字段
            self.assertNotIn("first", document["metrics"][key])

    def test_single_sample_has_no_slope(self):
        document = report.build(_context([_sample(0, view=500)]))
        stats = document["metrics"]["view"]

        self.assertEqual(stats["first"], 500)
        self.assertEqual(stats["last"], 500)
        self.assertEqual(stats["delta"], 0)
        self.assertIsNone(stats["peak_gain"])
        self.assertIsNone(stats["peak_gain_at"])
        self.assertIsNone(stats["average_per_minute"])
        self.assertEqual(stats["sample_count"], 1)

    def test_three_samples_aggregate(self):
        samples = [
            _sample(0, view=100),
            _sample(MINUTE, view=160),
            _sample(2 * MINUTE, view=220),
        ]
        stats = report.build(_context(samples))["metrics"]["view"]

        self.assertEqual(stats["first"], 100)
        self.assertEqual(stats["last"], 220)
        self.assertEqual(stats["delta"], 120)
        self.assertEqual(stats["min"], 100)
        self.assertEqual(stats["max"], 220)
        self.assertEqual(stats["average"], 160.0)
        self.assertEqual(stats["peak_gain"], 60)
        # 两段增量相同，取先出现的一段
        self.assertEqual(stats["average_per_minute"], 60.0)

    def test_peak_gain_tracks_largest_step(self):
        samples = [
            _sample(0, view=100),
            _sample(MINUTE, view=110),
            _sample(2 * MINUTE, view=310),
        ]
        stats = report.build(_context(samples))["metrics"]["view"]

        self.assertEqual(stats["peak_gain"], 200)
        self.assertTrue(stats["peak_gain_at"].startswith("2023-11-15T06:15"))

    def test_average_per_minute_uses_observed_span(self):
        samples = [_sample(0, view=0), _sample(30_000, view=300)]
        stats = report.build(_context(samples))["metrics"]["view"]

        # 30 秒涨 300，折算每分钟 600
        self.assertEqual(stats["average_per_minute"], 600.0)

    def test_none_values_are_skipped(self):
        samples = [
            _sample(0, online=None),
            _sample(MINUTE, online=7),
            _sample(2 * MINUTE, online=None),
        ]
        stats = report.build(_context(samples))["metrics"]["online"]

        self.assertEqual(stats["sample_count"], 1)
        self.assertEqual(stats["first"], 7)
        self.assertEqual(stats["delta"], 0)

    def test_missing_bounds_fall_back_to_samples(self):
        samples = [_sample(0), _sample(4 * MINUTE)]
        document = report.build(_context(samples, started_at=None, stopped_at=None))

        self.assertEqual(document["monitor"]["duration_seconds"], 240.0)
        self.assertEqual(document["samples"][0]["elapsed_seconds"], 0.0)
        self.assertEqual(document["samples"][1]["elapsed_seconds"], 240.0)

    def test_timestamps_use_utc_plus_eight(self):
        document = report.build(_context([_sample(0)]))

        self.assertEqual(document["monitor"]["started_at"], "2023-11-15T06:13:20+08:00")

    def test_negative_duration_is_clamped(self):
        samples = [_sample(0)]
        document = report.build(
            _context(samples, started_at=samples[0]["t"] + 5_000, stopped_at=samples[0]["t"])
        )

        self.assertEqual(document["monitor"]["duration_seconds"], 0)

    def test_all_metrics_present(self):
        document = report.build(_context([_sample(0)]))

        self.assertEqual(
            set(document["metrics"]),
            {"view", "danmaku", "reply", "online", "like", "coin", "favorite", "share"},
        )


class FilenameTest(unittest.TestCase):
    def test_name_encodes_monitor_bvid_and_stamp(self):
        document = report.build(_context([_sample(0)]))
        document["generated_at"] = "2023-11-15T06:13:20+08:00"

        self.assertEqual(
            report.filename(document),
            f"monitor_m3_{BVID}_20231115_061320.json",
        )

    def test_missing_fields_use_placeholders(self):
        name = report.filename({"generated_at": None, "monitor": None, "video": None})

        self.assertTrue(name.startswith("monitor_m0_unknown_"))
        self.assertTrue(name.endswith(".json"))

    def test_truncated_stamp_falls_back_to_now(self):
        document = report.build(_context([_sample(0)]))
        document["generated_at"] = "2023-11-15"

        name = report.filename(document)
        self.assertRegex(
            name,
            rf"^monitor_m3_{BVID}_\d{{8}}_\d{{6}}\.json$",
        )


class WriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = os.path.join(self._tmp.name, "reports", "nested")

    def test_creates_directory_and_returns_absolute_path(self):
        document = report.build(_context([_sample(0)]))
        path = report.write(self.directory, document)

        self.assertTrue(os.path.isabs(path))
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.isdir(self.directory))

    def test_written_content_round_trips(self):
        document = report.build(_context([_sample(0), _sample(MINUTE, view=1100)]))
        path = report.write(self.directory, document)

        with open(path, "r", encoding="utf-8") as handle:
            reloaded = json.load(handle)
        self.assertEqual(reloaded, document)
        # 中文不转义，便于人工查看
        with open(path, "r", encoding="utf-8") as handle:
            self.assertIn("测试视频", handle.read())


if __name__ == "__main__":
    unittest.main()
