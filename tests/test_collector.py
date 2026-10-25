"""collector 模块测试：链接解析、底层请求的错误映射、限流冷却与快照映射。

网络一律打桩，不触达 B站。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

from bilimon import collector
from bilimon.collector import BiliClient, CollectError, VideoRef
from tests.support import AID, BVID

URLOPEN = "bilimon.collector.urllib.request.urlopen"


def _response(body: bytes):
    """构造 urllib.request.urlopen 返回的响应上下文管理器。"""
    response = mock.MagicMock()
    response.read.return_value = body
    response.geturl.return_value = f"https://www.bilibili.com/video/{BVID}"
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def _json_response(payload: dict):
    return _response(json.dumps(payload).encode("utf-8"))


def _view_payload(**overrides) -> dict:
    data = {
        "bvid": BVID,
        "aid": AID,
        "title": "标题",
        "pic": "https://i0.hdslb.com/cover.jpg",
        "duration": 300,
        "pubdate": 1_700_000_000,
        "owner": {"name": "UP主"},
        "stat": {
            "view": 1,
            "danmaku": 2,
            "reply": 3,
            "like": 4,
            "coin": 5,
            "favorite": 6,
            "share": 7,
        },
        "pages": [{"cid": 999}, {"cid": 1000}],
    }
    data.update(overrides)
    return {"code": 0, "data": data}


def make_client(**kwargs) -> BiliClient:
    """默认屏蔽指纹申请，使请求次数可精确断言。"""
    client = BiliClient(**kwargs)
    client._ensure_cookie = lambda: None
    return client


class ClientTestBase(unittest.TestCase):
    def setUp(self):
        gap = mock.patch.object(BiliClient, "MIN_REQUEST_GAP", 0)
        gap.start()
        self.addCleanup(gap.stop)
        self.client = make_client()


class ResolveTest(ClientTestBase):
    def test_extracts_bvid_from_url(self):
        ref = self.client.resolve(f"https://www.bilibili.com/video/{BVID}?p=1&t=3")
        self.assertEqual(ref.bvid, BVID)
        self.assertIsNone(ref.aid)

    def test_accepts_bare_bvid(self):
        self.assertEqual(self.client.resolve(f"  {BVID}  ").bvid, BVID)

    def test_extracts_aid(self):
        ref = self.client.resolve(f"https://www.bilibili.com/video/av{AID}")
        self.assertEqual(ref.aid, AID)

    def test_bvid_takes_precedence_over_aid(self):
        ref = self.client.resolve(f"https://www.bilibili.com/video/av{AID}?bvid={BVID}")
        self.assertEqual(ref.bvid, BVID)

    def test_av_needs_word_boundary(self):
        # 字母紧邻 av 时不视为编号，避免误匹配普通单词
        self.assertIsNone(BiliClient._match("xvav170001"))

    def test_empty_input_raises_input_error(self):
        with self.assertRaises(CollectError) as ctx:
            self.client.resolve("   ")
        self.assertEqual(ctx.exception.kind, "input")

    def test_unrecognised_text_raises_input_error(self):
        with self.assertRaises(CollectError) as ctx:
            self.client.resolve("这是一段没有链接的文字")
        self.assertEqual(ctx.exception.kind, "input")

    def test_short_link_is_followed(self):
        for host in ("b23.tv", "bili2233.cn"):
            with self.subTest(host=host):
                with mock.patch.object(
                    BiliClient,
                    "_follow_short_link",
                    return_value=f"https://www.bilibili.com/video/{BVID}",
                ) as follow:
                    ref = self.client.resolve(f"https://{host}/abcd")
                self.assertEqual(ref.bvid, BVID)
                follow.assert_called_once()

    def test_short_link_without_video_raises_input_error(self):
        with mock.patch.object(
            BiliClient, "_follow_short_link", return_value="https://www.bilibili.com/"
        ):
            with self.assertRaises(CollectError) as ctx:
                self.client.resolve("https://b23.tv/abcd")
        self.assertEqual(ctx.exception.kind, "input")

    def test_short_link_network_failure(self):
        with mock.patch(URLOPEN, side_effect=urllib.error.URLError("boom")):
            with self.assertRaises(CollectError) as ctx:
                self.client.resolve("https://b23.tv/abcd")
        self.assertEqual(ctx.exception.kind, "network")

    def test_short_link_without_scheme_is_upgraded(self):
        with mock.patch(URLOPEN, return_value=_response(b"")) as urlopen:
            self.client._follow_short_link("b23.tv/abcd")

        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://b23.tv/abcd")


class ToIntTest(unittest.TestCase):
    def test_accepts_numbers(self):
        self.assertEqual(collector._to_int(5), 5)
        self.assertEqual(collector._to_int(-7), -7)
        self.assertEqual(collector._to_int(3.9), 3)

    def test_accepts_numeric_strings(self):
        self.assertEqual(collector._to_int("42"), 42)
        self.assertEqual(collector._to_int(" 42 "), 42)
        self.assertEqual(collector._to_int("-12"), -12)

    def test_rejects_booleans(self):
        # bool 是 int 的子类，若不拦截会变成 1/0，污染指标
        self.assertIsNone(collector._to_int(True))
        self.assertIsNone(collector._to_int(False))

    def test_rejects_invalid(self):
        for value in (None, "", "  ", "4.2", "abc", [], {}):
            with self.subTest(value=value):
                self.assertIsNone(collector._to_int(value))


class GetTest(ClientTestBase):
    def test_returns_payload_and_encodes_query(self):
        with mock.patch(URLOPEN, return_value=_json_response(_view_payload())) as urlopen:
            payload = self.client._get("/x/web-interface/view", {"bvid": BVID, "cid": None})

        self.assertEqual(payload["code"], 0)
        request = urlopen.call_args[0][0]
        self.assertIn(f"bvid={BVID}", request.full_url)
        # None 参数不应出现在查询串里
        self.assertNotIn("cid", request.full_url)
        self.assertTrue(request.full_url.startswith("https://api.bilibili.com"))

    def test_http_412_enters_cooldown(self):
        error = urllib.error.HTTPError("https://api.bilibili.com", 412, "Precondition", None, None)
        with mock.patch(URLOPEN, side_effect=error):
            with self.assertRaises(CollectError) as ctx:
                self.client._get("/x/web-interface/view", {})

        self.assertEqual(ctx.exception.kind, "throttled")
        self.assertEqual(ctx.exception.retry_after, BiliClient.COOLDOWN_SECONDS)
        self.assertGreater(self.client.cooldown_remaining(), 0)

    def test_http_403_is_plain_error(self):
        error = urllib.error.HTTPError("https://api.bilibili.com", 403, "Forbidden", None, None)
        with mock.patch(URLOPEN, side_effect=error):
            with self.assertRaises(CollectError) as ctx:
                self.client._get("/x/web-interface/view", {})
        self.assertEqual(ctx.exception.kind, "error")

    def test_http_500_reports_status(self):
        error = urllib.error.HTTPError("https://api.bilibili.com", 500, "Boom", None, None)
        with mock.patch(URLOPEN, side_effect=error):
            with self.assertRaises(CollectError) as ctx:
                self.client._get("/x/web-interface/view", {})
        self.assertEqual(ctx.exception.kind, "error")
        self.assertIn("500", str(ctx.exception))

    def test_url_error_is_network(self):
        with mock.patch(URLOPEN, side_effect=urllib.error.URLError("unreachable")):
            with self.assertRaises(CollectError) as ctx:
                self.client._get("/x/web-interface/view", {})
        self.assertEqual(ctx.exception.kind, "network")
        self.assertIn("unreachable", str(ctx.exception))

    def test_timeout_is_network(self):
        with mock.patch(URLOPEN, side_effect=TimeoutError()):
            with self.assertRaises(CollectError) as ctx:
                self.client._get("/x/web-interface/view", {})
        self.assertEqual(ctx.exception.kind, "network")

    def test_invalid_json_is_error(self):
        with mock.patch(URLOPEN, return_value=_response(b"<html>nope</html>")):
            with self.assertRaises(CollectError) as ctx:
                self.client._get("/x/web-interface/view", {})
        self.assertEqual(ctx.exception.kind, "error")

    def test_risk_control_code_enters_cooldown(self):
        for code in sorted(collector.THROTTLE_CODES):
            with self.subTest(code=code):
                client = make_client()
                body = {"code": code, "message": "风控"}
                with mock.patch(URLOPEN, return_value=_json_response(body)):
                    with self.assertRaises(CollectError) as ctx:
                        client._get("/x/web-interface/view", {})
                self.assertEqual(ctx.exception.kind, "throttled")
                self.assertEqual(ctx.exception.retry_after, BiliClient.COOLDOWN_SECONDS)
                self.assertGreater(client.cooldown_remaining(), 0)

    def test_business_error_is_plain_error(self):
        body = {"code": -404, "message": "啥都木有"}
        with mock.patch(URLOPEN, return_value=_json_response(body)):
            with self.assertRaises(CollectError) as ctx:
                self.client._get("/x/web-interface/view", {})
        self.assertEqual(ctx.exception.kind, "error")
        self.assertIn("啥都木有", str(ctx.exception))

    def test_missing_code_is_error(self):
        with mock.patch(URLOPEN, return_value=_json_response({"data": {}})):
            with self.assertRaises(CollectError) as ctx:
                self.client._get("/x/web-interface/view", {})
        self.assertEqual(ctx.exception.kind, "error")

    def test_cooldown_blocks_network_call(self):
        self.client._cooldown_until = time.monotonic() + 30
        with mock.patch(URLOPEN) as urlopen:
            with self.assertRaises(CollectError) as ctx:
                self.client._get("/x/web-interface/view", {})
        urlopen.assert_not_called()
        self.assertEqual(ctx.exception.kind, "throttled")
        self.assertGreater(ctx.exception.retry_after, 29)

    def test_cooldown_expires(self):
        self.client._cooldown_until = time.monotonic() - 1
        with mock.patch(URLOPEN, return_value=_json_response(_view_payload())):
            payload = self.client._get("/x/web-interface/view", {})
        self.assertEqual(payload["code"], 0)

    def test_requests_are_spaced(self):
        with mock.patch.object(BiliClient, "MIN_REQUEST_GAP", 0.1), mock.patch(
            "bilimon.collector.time.sleep"
        ) as sleep, mock.patch(URLOPEN, return_value=_json_response({"code": 0, "data": {}})):
            self.client._get("/x/a", {})
            self.client._get("/x/b", {})

        self.assertEqual(sleep.call_count, 1)
        self.assertGreater(sleep.call_args[0][0], 0)
        self.assertLessEqual(sleep.call_args[0][0], 0.1)


class CookieTest(unittest.TestCase):
    def setUp(self):
        gap = mock.patch.object(BiliClient, "MIN_REQUEST_GAP", 0)
        gap.start()
        self.addCleanup(gap.stop)

    def test_cookie_requested_once(self):
        client = BiliClient()
        body = {"code": 0, "data": {"b_3": "AAA", "b_4": "BBB"}}
        with mock.patch(URLOPEN, return_value=_json_response(body)) as urlopen:
            client._ensure_cookie()
            client._ensure_cookie()

        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("buvid3=AAA", client._headers()["Cookie"])
        self.assertIn("buvid4=BBB", client._headers()["Cookie"])

    def test_failure_is_not_retried(self):
        client = BiliClient()
        with mock.patch(URLOPEN, side_effect=urllib.error.URLError("boom")) as urlopen:
            client._ensure_cookie()
            client._ensure_cookie()

        self.assertEqual(urlopen.call_count, 1)
        self.assertNotIn("Cookie", client._headers())

    def test_empty_fingerprint_leaves_cookie_unset(self):
        client = BiliClient()
        with mock.patch(URLOPEN, return_value=_json_response({"code": 0, "data": {}})):
            client._ensure_cookie()

        self.assertNotIn("Cookie", client._headers())


class CooldownFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "cooldown.json")

    def test_cooldown_survives_restart(self):
        client = make_client(cooldown_file=self.path)
        client._enter_cooldown()

        self.assertTrue(os.path.isfile(self.path))
        with open(self.path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertGreater(data["until"], time.time())

        restored = BiliClient(cooldown_file=self.path)
        self.assertGreater(restored.cooldown_remaining(), 0)

    def test_expired_file_is_ignored(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"until": time.time() - 10}, handle)

        client = BiliClient(cooldown_file=self.path)
        self.assertEqual(client.cooldown_remaining(), 0)

    def test_corrupt_file_is_ignored(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")

        client = BiliClient(cooldown_file=self.path)
        self.assertEqual(client.cooldown_remaining(), 0)

    def test_missing_file_is_ignored(self):
        client = BiliClient(cooldown_file=os.path.join(self._tmp.name, "absent.json"))
        self.assertEqual(client.cooldown_remaining(), 0)

    def test_unwritable_target_does_not_raise(self):
        # 用一个文件占住目录位置，写入必然失败
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")

        client = make_client(cooldown_file=os.path.join(blocker, "cooldown.json"))
        client._enter_cooldown()
        self.assertGreater(client.cooldown_remaining(), 0)

    def test_no_file_configured(self):
        client = BiliClient()
        client._enter_cooldown()
        self.assertGreater(client.cooldown_remaining(), 0)


class FetchSnapshotTest(ClientTestBase):
    _DEFAULT_ONLINE = {"code": 0, "data": {"total": 5}}

    def _patch_get(self, view=None, online=None, online_error=False):
        view = _view_payload() if view is None else view
        online = self._DEFAULT_ONLINE if online is None else online

        def fake_get(path, params, **kwargs):
            if path.endswith("/view"):
                return view
            if online_error:
                raise CollectError("在线人数不可用")
            return online

        patcher = mock.patch.object(self.client, "_get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_maps_video_and_metrics(self):
        self._patch_get(online={"code": 0, "data": {"total": 42}})
        info, metrics = self.client.fetch_snapshot(VideoRef(bvid=BVID))

        self.assertEqual(info.bvid, BVID)
        self.assertEqual(info.aid, AID)
        self.assertEqual(info.title, "标题")
        self.assertEqual(info.owner, "UP主")
        self.assertEqual(info.cid, 999)
        self.assertEqual(info.url, f"https://www.bilibili.com/video/{BVID}")
        self.assertEqual(
            metrics,
            {
                "view": 1,
                "danmaku": 2,
                "reply": 3,
                "online": 42,
                "like": 4,
                "coin": 5,
                "favorite": 6,
                "share": 7,
            },
        )

    def test_online_falls_back_to_count(self):
        self._patch_get(online={"code": 0, "data": {"count": 17}})
        _, metrics = self.client.fetch_snapshot(VideoRef(bvid=BVID))
        self.assertEqual(metrics["online"], 17)

    def test_online_respects_switch_off(self):
        self._patch_get(online={"code": 0, "data": {"show_switch": {"total": False}}})
        _, metrics = self.client.fetch_snapshot(VideoRef(bvid=BVID))
        self.assertIsNone(metrics["online"])

    def test_online_failure_keeps_other_metrics(self):
        self._patch_get(online_error=True)
        _, metrics = self.client.fetch_snapshot(VideoRef(bvid=BVID))

        self.assertIsNone(metrics["online"])
        self.assertEqual(metrics["view"], 1)
        self.assertEqual(metrics["share"], 7)

    def test_without_cid_online_is_skipped(self):
        patcher = mock.patch.object(self.client, "_get", return_value=_view_payload(pages=[]))
        patcher.start()
        self.addCleanup(patcher.stop)

        info, metrics = self.client.fetch_snapshot(VideoRef(bvid=BVID))
        self.assertIsNone(info.cid)
        self.assertIsNone(metrics["online"])
        self.assertEqual(self.client._get.call_count, 1)

    def test_missing_stat_yields_none_metrics(self):
        self._patch_get(view=_view_payload(stat=None))
        _, metrics = self.client.fetch_snapshot(VideoRef(bvid=BVID))

        for key in ("view", "danmaku", "reply", "like", "coin", "favorite", "share"):
            self.assertIsNone(metrics[key])
        # 计数缺失不影响同时在看的独立取值
        self.assertEqual(metrics["online"], 5)

    def test_empty_data_raises(self):
        patcher = mock.patch.object(self.client, "_get", return_value={"code": 0, "data": {}})
        patcher.start()
        self.addCleanup(patcher.stop)

        with self.assertRaises(CollectError):
            self.client.fetch_snapshot(VideoRef(bvid=BVID))

    def test_defaults_for_missing_fields(self):
        payload = _view_payload()
        for field in ("title", "pic", "owner", "duration", "pubdate", "pages"):
            payload["data"].pop(field)
        payload["data"]["bvid"] = BVID
        payload["data"]["stat"] = {}

        patcher = mock.patch.object(self.client, "_get", return_value=payload)
        patcher.start()
        self.addCleanup(patcher.stop)

        info, _ = self.client.fetch_snapshot(VideoRef(bvid=BVID))
        self.assertEqual(info.title, "未命名视频")
        self.assertEqual(info.owner, "未知UP主")
        self.assertEqual(info.cover, "")
        self.assertEqual(info.duration, 0)
        self.assertEqual(info.pubdate, 0)
        self.assertIsNone(info.cid)

    def test_bvid_falls_back_to_ref(self):
        payload = _view_payload()
        payload["data"].pop("bvid")

        patcher = mock.patch.object(self.client, "_get", return_value=payload)
        patcher.start()
        self.addCleanup(patcher.stop)

        info, _ = self.client.fetch_snapshot(VideoRef(bvid=BVID))
        self.assertEqual(info.bvid, BVID)


if __name__ == "__main__":
    unittest.main()
