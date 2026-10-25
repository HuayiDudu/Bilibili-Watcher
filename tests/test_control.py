"""control 模块测试：多监控槽位的生命周期、报告结算与配置继承。

用替身客户端替代网络，用临时目录替代真实配置位置。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from bilimon.collector import CollectError
from bilimon.control import MonitorController
from bilimon.settings import Settings
from tests.support import BVID, FakeClient, make_metrics

SOURCE = f"https://www.bilibili.com/video/{BVID}"


class ControllerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name

        self.client = FakeClient()
        self.settings = Settings(self.base)
        self.controller = MonitorController(self.client, self.settings, self.reports_dir())
        self.addCleanup(self._stop_all)

    def _stop_all(self):
        for session in list(self.controller._monitors.values()):
            session.stop()

    def reports_dir(self) -> str:
        return os.path.join(self.base, "reports")


class CreateTest(ControllerTestBase):
    def test_first_create_becomes_active(self):
        payload = self.controller.create()

        self.assertEqual(payload["active_monitor"], "m1")
        self.assertEqual(len(payload["monitors"]), 1)
        summary = payload["monitors"][0]
        self.assertEqual(summary["id"], "m1")
        self.assertFalse(summary["configured"])
        self.assertEqual(summary["title"], "未配置监控")
        self.assertFalse(summary["running"])

    def test_ids_are_sequential(self):
        self.controller.create()
        payload = self.controller.create()

        self.assertEqual([item["id"] for item in payload["monitors"]], ["m1", "m2"])
        self.assertEqual(payload["active_monitor"], "m2")

    def test_limit_is_enforced(self):
        for _ in range(MonitorController.MAX_MONITORS):
            self.controller.create()

        with self.assertRaises(CollectError):
            self.controller.create()
        self.assertEqual(len(self.controller.state()["monitors"]), 5)


class StartTest(ControllerTestBase):
    def test_starts_without_prior_create(self):
        payload = self.controller.start(source=SOURCE)

        self.assertEqual(len(payload["monitors"]), 1)
        self.assertTrue(payload["running"])
        self.assertEqual(payload["sample_count"], 1)
        self.assertEqual(payload["video"]["bvid"], BVID)

    def test_unknown_monitor_id_is_rejected(self):
        self.controller.create()
        with self.assertRaises(CollectError):
            self.controller.start(monitor_id="m9", source=SOURCE)

    def test_failure_leaves_slot_untouched(self):
        self.controller.create()
        self.client.resolve_error = CollectError("链接无效", kind="input")

        with self.assertRaises(CollectError):
            self.controller.start(source="garbage")

        payload = self.controller.state()
        self.assertEqual(payload["state"], "idle")
        self.assertFalse(payload["running"])
        self.assertEqual(payload["sample_count"], 0)
        self.assertIsNone(payload["video"])

    def test_restart_settles_previous_report(self):
        self.controller.start(source=SOURCE)
        payload = self.controller.start(source=SOURCE)

        self.assertIsNotNone(payload["previous_report"])
        self.assertEqual(payload["previous_report"]["sample_count"], 1)
        # 新一段监控重新计数
        self.assertEqual(payload["sample_count"], 1)

    def test_interval_inherited_when_omitted(self):
        self.controller.start(source=SOURCE, interval=45)
        payload = self.controller.start(source=SOURCE)

        self.assertEqual(payload["interval"], 45)

    def test_sample_is_recorded_on_client(self):
        self.controller.start(source=SOURCE)
        self.assertEqual(self.client.resolve_calls, [SOURCE])
        self.assertEqual(len(self.client.fetch_calls), 1)

    def test_start_switches_active_monitor(self):
        self.controller.create()
        second = self.controller.create()
        self.assertEqual(second["active_monitor"], "m2")

        payload = self.controller.start(monitor_id="m1", source=SOURCE)
        self.assertEqual(payload["active_monitor"], "m1")
        self.assertTrue(payload["running"])


class StopTest(ControllerTestBase):
    def test_stop_builds_report(self):
        self.controller.start(source=SOURCE)
        payload = self.controller.stop()

        report = payload["report"]
        self.assertIsNotNone(report)
        self.assertIn("monitor_m1", report["name"])
        self.assertIn(BVID, report["name"])
        self.assertEqual(report["sample_count"], 1)
        # 自动保存默认开启且目录为空时落到仓库目录
        self.assertIsNotNone(report["saved_path"])
        self.assertTrue(report["saved_path"].endswith(".json"))
        self.assertFalse(payload["running"])

    def test_stop_without_samples_has_no_report(self):
        self.controller.create()
        payload = self.controller.stop()

        self.assertIsNone(payload["report"])

    def test_stop_non_active_monitor_reports_itself(self):
        self.controller.start(source=SOURCE)
        self.controller.create()

        payload = self.controller.stop(monitor_id="m1")

        self.assertEqual(payload["active_monitor"], "m2")
        self.assertIn("monitor_m1", payload["report"]["name"])

    def test_stop_is_safe_when_idle(self):
        payload = self.controller.stop()
        self.assertFalse(payload["running"])


class RemoveTest(ControllerTestBase):
    def test_removing_running_monitor_keeps_report(self):
        self.controller.start(source=SOURCE)
        payload = self.controller.remove()

        self.assertEqual(payload["monitors"], [])
        self.assertIsNone(payload["active_monitor"])
        # 无监控时回退到最近一份归档报告
        self.assertTrue(payload["report"]["archived"])
        self.assertIn("monitor_m1", payload["report"]["name"])

    def test_removing_blank_monitor_has_no_report(self):
        self.controller.create()
        payload = self.controller.remove()

        self.assertIsNone(payload["report"])
        self.assertEqual(payload["monitors"], [])

    def test_active_moves_to_remaining(self):
        self.controller.create()
        self.controller.create()

        payload = self.controller.remove(monitor_id="m2")

        self.assertEqual(payload["active_monitor"], "m1")
        self.assertEqual([item["id"] for item in payload["monitors"]], ["m1"])

    def test_removing_unknown_monitor_is_rejected(self):
        with self.assertRaises(CollectError):
            self.controller.remove(monitor_id="m9")

    def test_report_of_removed_monitor_is_downloadable(self):
        self.controller.start(source=SOURCE)
        self.controller.remove()

        document = self.controller.report_document("m1")
        self.assertIsNotNone(document)
        self.assertEqual(document["video"]["bvid"], BVID)

    def test_report_falls_back_to_archived_document(self):
        self.controller.start(source=SOURCE)
        self.controller.remove()

        document = self.controller.report_document()
        self.assertIsNotNone(document)
        self.assertEqual(document["video"]["bvid"], BVID)

    def test_no_document_before_any_run(self):
        self.controller.create()
        self.assertIsNone(self.controller.report_document())


class SelectTest(ControllerTestBase):
    def test_select_switches_active(self):
        self.controller.create()
        self.controller.create()

        payload = self.controller.select("m1")
        self.assertEqual(payload["active_monitor"], "m1")

    def test_select_defaults_to_first(self):
        self.controller.create()
        self.controller.create()

        self.assertEqual(self.controller.select()["active_monitor"], "m1")

    def test_select_unknown_is_rejected(self):
        self.controller.create()
        with self.assertRaises(CollectError):
            self.controller.select("m9")

    def test_switch_does_not_disturb_sampling(self):
        self.controller.start(source=SOURCE)
        payload = self.controller.create()

        summaries = payload["monitors"]
        self.assertTrue(summaries[0]["running"])
        self.assertEqual(payload["active_monitor"], "m2")
        self.assertEqual(payload["sample_count"], 0)


class IntervalTest(ControllerTestBase):
    def test_interval_remembered_without_monitor(self):
        self.assertEqual(self.controller.set_interval(120)["interval"], 120)

        payload = self.controller.create()
        self.assertEqual(payload["interval"], 120)

    def test_interval_applies_to_active_session(self):
        self.controller.start(source=SOURCE, interval=30)
        payload = self.controller.set_interval(90)

        self.assertEqual(payload["interval"], 90)

    def test_invalid_interval_is_rejected(self):
        with self.assertRaises(CollectError):
            self.controller.set_interval(1)


class SettingsTest(ControllerTestBase):
    def test_partial_update(self):
        result = self.controller.update_settings(auto_save=True)
        self.assertTrue(result["auto_save"])
        self.assertEqual(result["auto_save_dir"], "")

        result = self.controller.update_settings(auto_save_dir=self.base)
        self.assertTrue(result["auto_save"])
        self.assertEqual(result["auto_save_dir"], self.base)

    def test_settings_visible_in_state(self):
        self.controller.update_settings(align_clock=True)
        self.assertTrue(self.controller.state()["settings"]["align_clock"])

    def test_auto_save_writes_report(self):
        self.controller.update_settings(auto_save=True, auto_save_dir=self.reports_dir())
        self.controller.start(source=SOURCE)

        report = self.controller.stop()["report"]

        self.assertIsNotNone(report["saved_path"])
        self.assertTrue(os.path.isfile(report["saved_path"]))
        self.assertEqual(os.path.dirname(report["saved_path"]), os.path.abspath(self.reports_dir()))

    def test_auto_save_failure_is_reported_not_raised(self):
        # 用文件占住目录位置，落盘必然失败
        blocker = os.path.join(self.base, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        self.controller.update_settings(auto_save=True, auto_save_dir=blocker)

        self.controller.start(source=SOURCE)
        report = self.controller.stop()["report"]

        self.assertIsNone(report["saved_path"])
        self.assertIn("save_error", report)


class StateTest(ControllerTestBase):
    def test_empty_state_shape(self):
        payload = self.controller.state()

        self.assertEqual(payload["state"], "idle")
        self.assertFalse(payload["running"])
        self.assertEqual(payload["monitors"], [])
        self.assertIsNone(payload["report"])
        self.assertEqual(payload["cooldown_remaining"], 0)
        self.assertIn("settings", payload)

    def test_state_of_specific_monitor(self):
        self.controller.start(source=SOURCE)
        self.controller.create()

        payload = self.controller.state(monitor_id="m1")

        self.assertEqual(payload["active_monitor"], "m1")
        self.assertTrue(payload["running"])
        self.assertEqual(payload["sample_count"], 1)

    def test_cooldown_is_surfaced(self):
        self.client.set_cooldown(12.4)
        self.assertEqual(self.controller.state()["cooldown_remaining"], 12)

    def test_metrics_present_in_state(self):
        payload = self.controller.state()
        self.assertEqual(len(payload["metrics"]), 8)

    def test_summary_tracks_samples(self):
        self.controller.start(source=SOURCE)
        summary = self.controller.state()["monitors"][0]

        self.assertTrue(summary["configured"])
        self.assertTrue(summary["running"])
        self.assertEqual(summary["bvid"], BVID)
        self.assertEqual(summary["title"], "测试视频")
        self.assertEqual(summary["sample_count"], 1)

    def test_partial_metrics_do_not_break_summary(self):
        self.client.metrics = make_metrics(online=None, view=None)
        self.controller.start(source=SOURCE)

        payload = self.controller.state()
        self.assertEqual(payload["sample_count"], 1)
        self.assertIsNone(payload["samples"][0]["view"])


if __name__ == "__main__":
    unittest.main()


class ArchiveCleanupTest(ControllerTestBase):
    def test_removed_reports_are_released_beyond_archive_window(self):
        for _ in range(7):
            self.controller.create()
            self.controller.start(source=SOURCE)
            self.controller.stop()
            self.controller.remove()

        self.assertEqual(len(self.controller._monitors), 0)
        self.assertLessEqual(len(self.controller._orphans), 5)
        # 文档缓存不应超过归档窗口，否则反复增删监控会持续占用内存
        self.assertLessEqual(len(self.controller._documents), 5)


class StopAllTest(ControllerTestBase):
    def test_settles_every_running_monitor(self):
        self.controller.create()
        self.controller.start(source=SOURCE)
        self.controller.create()
        self.controller.start(source=SOURCE)
        self.assertEqual(len(self.controller._documents), 0)

        self.controller.stop_all()

        # 两个监控都应产出报告，而不是只结算当前选中的那个
        self.assertEqual(len(self.controller._documents), 2)
        for session in self.controller._monitors.values():
            self.assertFalse(session.is_running)

    def test_skips_idle_monitor(self):
        self.controller.create()
        self.controller.stop_all()

        self.assertEqual(len(self.controller._documents), 0)
