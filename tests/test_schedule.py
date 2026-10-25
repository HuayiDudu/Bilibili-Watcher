"""schedule 模块测试：整点对齐是纯算术，可完全脱离网络与时区环境验证。"""

from __future__ import annotations

import datetime
import unittest
from unittest import mock

from bilimon import schedule

MINUTE = 60_000
HALF_HOUR = 1_800_000
# 取整分钟时刻作基准，余数只由 interval 决定，断言可读
BASE = 1_700_000_040_000


def _next_anchor(now_ms: int, anchor_ms: int) -> int:
    """不早于 now 的最近锚点，按需求定义独立推导，不复用被测实现。"""
    remainder = now_ms % anchor_ms
    return now_ms if remainder == 0 else now_ms + (anchor_ms - remainder)


class AnchorIntervalTest(unittest.TestCase):
    def test_short_interval_anchors_to_minute(self):
        for seconds in (5, 30, 299):
            with self.subTest(seconds=seconds):
                self.assertEqual(schedule.anchor_interval(seconds), MINUTE)

    def test_long_interval_anchors_to_half_hour(self):
        for seconds in (300, 600, 1800, 3600):
            with self.subTest(seconds=seconds):
                self.assertEqual(schedule.anchor_interval(seconds), HALF_HOUR)


class FirstSampleAtTest(unittest.TestCase):
    """把时区偏移固定为 0，先验证算术本身。"""

    def setUp(self):
        patcher = mock.patch.object(schedule, "_offset_ms", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_exactly_on_anchor_returns_now(self):
        self.assertEqual(schedule.first_sample_at(BASE, 30), BASE)

    def test_aligns_to_next_period_boundary(self):
        now = BASE + 9_000
        # 距整分钟还有 51 秒，30 秒周期只能容纳一个点：51 - 30 = 21 秒后
        self.assertEqual(schedule.first_sample_at(now, 30), now + 21_000)

    def test_interval_longer_than_gap_waits_for_anchor(self):
        now = BASE + 9_000
        self.assertEqual(schedule.first_sample_at(now, 60), BASE + MINUTE)

    def test_interval_not_dividing_anchor_lands_inside_window(self):
        now = BASE + 9_000
        result = schedule.first_sample_at(now, 45)
        self.assertEqual(result, now + 6_000)
        self.assertEqual((result - (BASE + MINUTE)) % 45_000, 0)

    def test_half_hour_anchor(self):
        now = BASE + 6_000
        anchor = _next_anchor(now, HALF_HOUR)
        self.assertEqual(schedule.first_sample_at(now, 600), anchor - 600_000)
        self.assertEqual(schedule.first_sample_at(now, 450), anchor - 900_000)
        # 周期超过距锚点距离，只能等到锚点本身
        self.assertEqual(schedule.first_sample_at(now, 1800), anchor)

    def test_result_never_precedes_now(self):
        moments = [BASE, BASE + 1, BASE + 9_000, BASE + 29_999, BASE + 840_000]
        for interval in (5, 30, 45, 60, 75, 300, 450, 600, 900, 1800, 3600):
            for now in moments:
                with self.subTest(interval=interval, now=now):
                    result = schedule.first_sample_at(now, interval)
                    self.assertGreaterEqual(result, now)

    def test_result_sits_on_period_grid_before_anchor(self):
        now = BASE + 17_000
        for interval in (5, 30, 45, 60, 75, 300, 450, 600):
            with self.subTest(interval=interval):
                period = interval * 1000
                anchor_ms = schedule.anchor_interval(interval)
                anchor = _next_anchor(now, anchor_ms)
                result = schedule.first_sample_at(now, interval)
                self.assertLessEqual(result, anchor)
                self.assertEqual((anchor - result) % period, 0)

    def test_anchor_hit_when_gap_divides_period(self):
        now = BASE + 9_000
        # 51 秒之后是整分钟；6 秒、12 秒等能整除 51 余数的周期会直接落在当下
        self.assertEqual(schedule.first_sample_at(now, 51), now)
        self.assertEqual(schedule.first_sample_at(now, 17), now)


class TimezoneOffsetTest(unittest.TestCase):
    def test_offset_is_whole_quarter_hour(self):
        offset = schedule._offset_ms(1_700_000_000)
        self.assertEqual(offset % (15 * MINUTE), 0)

    def test_offset_matches_local_utcoffset(self):
        expected = int(
            datetime.datetime.fromtimestamp(1_700_000_000)
            .astimezone()
            .utcoffset()
            .total_seconds()
            * 1000
        )
        self.assertEqual(schedule._offset_ms(1_700_000_000), expected)

    def test_whole_minute_offsets_cancel_out(self):
        now = BASE + 9_000
        results = []
        for offset in (0, 8 * 3_600_000, -5 * 3_600_000, 5 * 3_600_000 + 30 * MINUTE):
            with mock.patch.object(schedule, "_offset_ms", return_value=offset):
                results.append(schedule.first_sample_at(now, 30))
        # 锚点按本地整点算，而时区偏移都是整分钟，抵回 UTC 后结果一致
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(results[0], now + 21_000)


if __name__ == "__main__":
    unittest.main()
