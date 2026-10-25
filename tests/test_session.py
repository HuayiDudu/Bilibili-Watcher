"""session 模块测试：会话状态机、单次采样行为与采样线程的时序规则。

采样线程的周期最短 5 秒，测试不等待真实周期，而是直接驱动到期时刻。
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from bilimon import session as session_module
from bilimon.collector import METRIC_KEYS, CollectError
from bilimon.session import MonitorSession, clamp_interval, prepare_snapshot
from tests.support import BVID, FakeClient, make_metrics

T0 = 1_700_000_000_000
SECOND = 1_000


def make_session(**client_kwargs):
    client = FakeClient(**client_kwargs)
    session = MonitorSession(client)
    session.monitor_id = "m1"
    return session, client


def make_prepared(client, interval: int = 30, timestamp: int | None = None) -> dict:
    return {
        "ref": client.ref,
        "info": client.info,
        "metrics": dict(client.metrics),
        "interval": interval,
        "timestamp": int(time.time() * 1000) if timestamp is None else timestamp,
    }


class ClampIntervalTest(unittest.TestCase):
    def test_accepts_bounds(self):
        self.assertEqual(clamp_interval(5), 5)
        self.assertEqual(clamp_interval(3600), 3600)

    def test_accepts_numeric_strings_and_floats(self):
        self.assertEqual(clamp_interval("30"), 30)
        self.assertEqual(clamp_interval(30.4), 30)
        self.assertEqual(clamp_interval(31.6), 32)

    def test_rejects_values_below_minimum(self):
        for value in (4, 0, -30):
            with self.subTest(value=value):
                with self.assertRaises(CollectError):
                    clamp_interval(value)

    def test_rejects_values_above_maximum(self):
        with self.assertRaises(CollectError):
            clamp_interval(3601)

    def test_rejects_non_numeric(self):
        for value in (None, "abc", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(CollectError):
                    clamp_interval(value)


class InitialStateTest(unittest.TestCase):
    def test_idle_by_default(self):
        session, _ = make_session()
        snapshot = session.snapshot()

        self.assertEqual(snapshot["state"], "idle")
        self.assertFalse(snapshot["running"])
        self.assertIsNone(snapshot["video"])
        self.assertEqual(snapshot["sample_count"], 0)
        self.assertEqual(snapshot["interval"], session_module.DEFAULT_INTERVAL)
        self.assertIsNone(snapshot["next_sample_at"])
        self.assertIsNone(session.bvid)

    def test_metric_catalogue_is_exposed(self):
        session, _ = make_session()
        metrics = session.snapshot()["metrics"]

        self.assertEqual([item["key"] for item in metrics], list(METRIC_KEYS))
        self.assertTrue(all(item["label"] for item in metrics))


class BeginTest(unittest.TestCase):
    def setUp(self):
        self.session, self.client = make_session()
        self.addCleanup(self.session.stop)

    def test_immediate_start_counts_probe_sample(self):
        prepared = make_prepared(self.client, interval=30)
        snapshot = self.session.begin(prepared)

        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["sample_count"], 1)
        self.assertEqual(snapshot["success_count"], 1)
        self.assertEqual(snapshot["samples"][0]["t"], prepared["timestamp"])
        self.assertEqual(snapshot["video"]["bvid"], BVID)
        self.assertIsNotNone(snapshot["next_sample_at"])

    def test_sample_carries_all_metric_keys(self):
        self.session.begin(make_prepared(self.client))
        sample = self.session.samples()[0]

        self.assertEqual(set(sample), {"t", *METRIC_KEYS})

    def test_aligned_start_defers_first_sample(self):
        prepared = make_prepared(self.client, interval=30)
        target = prepared["timestamp"] + 60 * SECOND
        with mock.patch.object(
            session_module.schedule, "first_sample_at", return_value=target
        ):
            snapshot = self.session.begin(prepared, align_clock=True)

        # 对齐模式下探测只用于校验链接，不计入样本
        self.assertEqual(snapshot["sample_count"], 0)
        self.assertEqual(snapshot["success_count"], 0)
        self.assertTrue(snapshot["align_clock"])
        self.assertGreater(snapshot["next_sample_at"], snapshot["server_time"] + 55 * SECOND)

    def test_aligned_start_without_wait_keeps_probe(self):
        prepared = make_prepared(self.client, interval=30)
        with mock.patch.object(
            session_module.schedule,
            "first_sample_at",
            return_value=prepared["timestamp"],
        ):
            snapshot = self.session.begin(prepared, align_clock=True)

        self.assertEqual(snapshot["sample_count"], 1)
        self.assertEqual(snapshot["success_count"], 1)

    def test_second_begin_while_running_is_rejected(self):
        self.session.begin(make_prepared(self.client))
        with self.assertRaises(CollectError):
            self.session.begin(make_prepared(self.client))

        self.assertTrue(self.session.is_running)


class StopTest(unittest.TestCase):
    def test_stop_freezes_session(self):
        session, client = make_session()
        self.addCleanup(session.stop)
        session.begin(make_prepared(client))
        snapshot = session.stop()

        self.assertEqual(snapshot["state"], "stopped")
        self.assertFalse(snapshot["running"])
        self.assertIsNotNone(snapshot["stopped_at"])
        self.assertIsNone(snapshot["next_sample_at"])
        self.assertEqual(snapshot["sample_count"], 1)

    def test_stop_is_idempotent(self):
        session, client = make_session()
        self.addCleanup(session.stop)
        session.begin(make_prepared(client))
        first = session.stop()
        second = session.stop()

        self.assertEqual(first["stopped_at"], second["stopped_at"])

    def test_restart_clears_samples(self):
        session, client = make_session()
        self.addCleanup(session.stop)
        session.begin(make_prepared(client))
        session.stop()

        snapshot = session.begin(make_prepared(client, timestamp=T0 + 60 * SECOND))
        self.assertEqual(snapshot["sample_count"], 1)
        self.assertEqual(snapshot["started_at"], T0 + 60 * SECOND)


class SampleOnceTest(unittest.TestCase):
    def setUp(self):
        self.session, self.client = make_session()
        self.addCleanup(self.session.stop)
        self.session.begin(make_prepared(self.client, interval=30))

    def test_successful_sample_appends(self):
        self.session._sample_once(T0)
        snapshot = self.session.snapshot()

        self.assertEqual(snapshot["sample_count"], 2)
        self.assertEqual(snapshot["success_count"], 2)
        self.assertEqual(snapshot["failure_count"], 0)
        self.assertIsNone(snapshot["last_error"])
        self.assertEqual(self.session.samples()[-1]["t"], T0)

    def test_failure_keeps_session_running(self):
        self.client.fetch_error = CollectError("boom")
        self.session._sample_once(T0)
        snapshot = self.session.snapshot()

        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["failure_count"], 1)
        self.assertEqual(snapshot["last_error"], "boom")
        self.assertEqual(snapshot["sample_count"], 1)

    def test_unexpected_exception_is_contained(self):
        self.client.fetch_error = RuntimeError("kaboom")
        self.session._sample_once(T0)
        snapshot = self.session.snapshot()

        self.assertTrue(snapshot["running"])
        self.assertEqual(snapshot["failure_count"], 1)
        self.assertIn("kaboom", snapshot["last_error"])

    def test_success_clears_previous_error(self):
        self.client.fetch_error = CollectError("boom")
        self.session._sample_once(T0)
        self.client.fetch_error = None
        self.session._sample_once(T0 + SECOND)

        self.assertIsNone(self.session.snapshot()["last_error"])

    def test_cooldown_skips_request_without_counting_failure(self):
        self.client.set_cooldown(5)
        before = len(self.client.fetch_calls)
        self.session._sample_once(T0)
        snapshot = self.session.snapshot()

        self.assertEqual(len(self.client.fetch_calls), before)
        self.assertEqual(snapshot["failure_count"], 0)
        self.assertEqual(snapshot["sample_count"], 1)
        self.assertIn("限制访问", snapshot["last_error"])

    def test_sample_ignored_when_not_running(self):
        self.session.stop()
        self.session._sample_once(T0)

        self.assertEqual(self.session.snapshot()["sample_count"], 1)

    def test_sample_buffer_is_capped(self):
        with mock.patch.object(session_module, "MAX_SAMPLES", 3):
            for index in range(4):
                self.session._sample_once(T0 + index * SECOND)
            samples = self.session.samples()

        self.assertEqual(len(samples), 3)
        # 超限时丢弃最旧的样本
        self.assertEqual(samples[-1]["t"], T0 + 3 * SECOND)
        self.assertEqual(samples[0]["t"], T0 + SECOND)


class SnapshotSinceTest(unittest.TestCase):
    def test_since_filters_samples_but_not_counts(self):
        session, client = make_session()
        self.addCleanup(session.stop)
        session.begin(make_prepared(client, interval=30, timestamp=T0))
        session._sample_once(T0 + SECOND)
        session._sample_once(T0 + 2 * SECOND)

        snapshot = session.snapshot(since=T0)

        self.assertEqual(
            [item["t"] for item in snapshot["samples"]],
            [T0 + SECOND, T0 + 2 * SECOND],
        )
        self.assertEqual(snapshot["sample_count"], 3)

    def test_since_strictly_greater(self):
        session, client = make_session()
        self.addCleanup(session.stop)
        session.begin(make_prepared(client, interval=30, timestamp=T0))

        snapshot = session.snapshot(since=T0)
        self.assertEqual(snapshot["samples"], [])


class IntervalTest(unittest.TestCase):
    def test_set_interval_updates_snapshot(self):
        session, _ = make_session()

        self.assertEqual(session.set_interval(120)["interval"], 120)
        self.assertEqual(session.interval, 120)

    def test_set_interval_validates(self):
        session, _ = make_session()
        with self.assertRaises(CollectError):
            session.set_interval(1)

    def test_set_interval_while_running_reschedules(self):
        session, client = make_session()
        self.addCleanup(session.stop)
        session.begin(make_prepared(client, interval=30))

        snapshot = session.set_interval(60)

        self.assertEqual(snapshot["interval"], 60)
        self.assertIsNotNone(snapshot["next_sample_at"])


class LoopTest(unittest.TestCase):
    def test_missed_slots_are_skipped_not_backfilled(self):
        session, client = make_session()
        self.addCleanup(session.stop)
        session.begin(make_prepared(client, interval=5))
        before = len(client.fetch_calls)

        # 把下一次到期时刻推到 100 秒前，模拟线程被阻塞
        with session._lock:
            session._next_due = time.monotonic() - 100

        deadline = time.monotonic() + 5
        while len(client.fetch_calls) == before and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertGreater(len(client.fetch_calls), before)
        # 落后 20 个时隙只补一次，其余跳过错过的时隙
        self.assertEqual(len(client.fetch_calls) - before, 1)

        time.sleep(1.0)
        self.assertEqual(len(client.fetch_calls) - before, 1)
        self.assertGreater(session._next_due, time.monotonic())

    def test_stop_terminates_thread(self):
        session, client = make_session()
        session.begin(make_prepared(client, interval=5))
        thread = session._thread
        self.assertIsNotNone(thread)
        self.assertTrue(thread.is_alive())

        session.stop()

        self.assertFalse(thread.is_alive())


class PrepareSnapshotTest(unittest.TestCase):
    def test_bundles_probe_result(self):
        client = FakeClient()
        prepared = prepare_snapshot(client, "https://www.bilibili.com/video/" + BVID, 45)

        self.assertEqual(
            set(prepared), {"ref", "info", "metrics", "interval", "timestamp"}
        )
        self.assertEqual(prepared["interval"], 45)
        self.assertEqual(prepared["ref"], client.ref)
        self.assertEqual(client.resolve_calls, ["https://www.bilibili.com/video/" + BVID])

    def test_uses_default_interval_when_omitted(self):
        prepared = prepare_snapshot(FakeClient(), "BV")
        self.assertEqual(prepared["interval"], session_module.DEFAULT_INTERVAL)

    def test_propagates_fetch_failure(self):
        client = FakeClient(fetch_error=CollectError("网络不可达", kind="network"))
        with self.assertRaises(CollectError) as ctx:
            prepare_snapshot(client, "BV")
        self.assertEqual(ctx.exception.kind, "network")

    def test_propagates_invalid_interval(self):
        with self.assertRaises(CollectError):
            prepare_snapshot(FakeClient(), "BV", 1)


class ContextTest(unittest.TestCase):
    def test_context_feeds_report(self):
        session, client = make_session()
        self.addCleanup(session.stop)
        session.begin(make_prepared(client))

        context = session.context()
        self.assertEqual(context["monitor_id"], "m1")
        self.assertEqual(context["video"]["bvid"], BVID)
        self.assertEqual(len(context["samples"]), 1)
        self.assertEqual(session.bvid, BVID)

    def test_metrics_default_to_none(self):
        session, client = make_session(metrics=make_metrics(online=None))
        self.addCleanup(session.stop)
        session.begin(make_prepared(client))

        self.assertIsNone(session.samples()[0]["online"])


if __name__ == "__main__":
    unittest.main()


class GenerationTest(unittest.TestCase):
    """停止后立刻重启时，上一轮线程不得再写入新会话。"""

    def test_stale_generation_sample_is_dropped(self):
        session, client = make_session()
        prepared = make_prepared(client)
        session.begin(prepared)
        stale = session._generation

        session.stop()
        session.begin(prepared)
        before = len(session.samples())

        session._sample_once(T0, stale)

        self.assertEqual(len(session.samples()), before)

    def test_restart_replaces_stop_event(self):
        session, client = make_session()
        prepared = make_prepared(client)
        session.begin(prepared)
        first = session._stop_event

        session.stop()
        session.begin(prepared)

        # 旧事件保持置位，旧线程不会被"复活"
        self.assertIsNot(first, session._stop_event)
        self.assertTrue(first.is_set())
        self.assertFalse(session._stop_event.is_set())

    def test_current_generation_still_records(self):
        session, client = make_session()
        session.begin(make_prepared(client))
        before = len(session.samples())

        session._sample_once(T0, session._generation)

        self.assertEqual(len(session.samples()), before + 1)


class InfinityIntervalTest(unittest.TestCase):
    def test_infinity_is_rejected_as_invalid_input(self):
        for value in ("inf", "-inf", "1e400"):
            with self.subTest(value=value):
                with self.assertRaises(CollectError):
                    clamp_interval(value)
