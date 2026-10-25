"""server 模块测试：路由、静态资源防护与错误到状态码的映射。

服务监听回环地址的随机端口，请求经真实 HTTP 往返，不触达 B站。
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest

from bilimon.collector import CollectError
from bilimon.control import MonitorController
from bilimon.server import MonitorServer
from bilimon.settings import Settings
from tests.support import BVID, FakeClient

WEB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"
)
SOURCE = f"https://www.bilibili.com/video/{BVID}"


class StubController:
    """按需抛错或返回固定快照，用于验证异常映射。"""

    def __init__(self, error: Exception | None = None, payload: dict | None = None) -> None:
        self.error = error
        self.payload = payload or {}
        self.calls: list[tuple] = []

    def state(self, since=None, monitor_id=None):
        self.calls.append((since, monitor_id))
        if self.error is not None:
            raise self.error
        return self.payload

    def settings(self):
        return {"auto_save": False, "auto_save_dir": "", "align_clock": False}


class ServerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.client = FakeClient()
        self.controller = MonitorController(
            self.client, Settings(self._tmp.name), os.path.join(self._tmp.name, "reports")
        )
        self.server = self._start_server(self.controller)
        self.addCleanup(self._stop_sessions)

    def _stop_sessions(self):
        for session in list(self.controller._monitors.values()):
            session.stop()

    def _start_server(self, controller) -> MonitorServer:
        server = MonitorServer(("127.0.0.1", 0), controller, WEB_DIR)
        # 缩短轮询间隔，让 shutdown 不必等默认的 0.5 秒
        thread = threading.Thread(
            target=server.serve_forever, args=(0.02,), daemon=True
        )
        thread.start()

        def shutdown():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(shutdown)
        return server

    def request(
        self,
        path: str,
        method: str = "GET",
        body=None,
        headers: dict | None = None,
    ) -> tuple[int, dict, bytes]:
        host, port = self.server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        payload = None
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        merged = dict(headers or {})
        if payload is not None:
            merged.setdefault("Content-Type", "application/json")
            merged["Content-Length"] = str(len(payload))
        try:
            connection.request(method, path, body=payload, headers=merged)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def get_json(self, path: str, **kwargs):
        status, headers, body = self.request(path, **kwargs)
        return status, headers, json.loads(body.decode("utf-8"))


class StaticTest(ServerTestBase):
    def test_root_serves_index(self):
        status, headers, body = self.request("/")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"<", body)

    def test_known_mime_overrides(self):
        expected = {
            "/style.css": "text/css; charset=utf-8",
            "/app.js": "text/javascript; charset=utf-8",
            "/chart.js": "text/javascript; charset=utf-8",
        }
        for path, content_type in expected.items():
            with self.subTest(path=path):
                status, headers, _ = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], content_type)

    def test_missing_asset_returns_json_404(self):
        status, _, body = self.request("/nope.js")

        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_path_traversal_is_forbidden(self):
        status, _, body = self.request("/../bilimon/collector.py")

        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"], "禁止访问")

    def test_nested_traversal_is_forbidden(self):
        status, _, _ = self.request("/assets/../../app.py")
        self.assertEqual(status, 403)

    def test_encoded_traversal_is_not_a_file(self):
        # 服务端不做 URL 解码，%2e 只是普通文件名，落到 404 而非 403
        status, _, _ = self.request("/%2e%2e/bilimon/collector.py")
        self.assertEqual(status, 404)


class StateApiTest(ServerTestBase):
    def test_state_shape(self):
        status, _, payload = self.get_json("/api/state")

        self.assertEqual(status, 200)
        for key in ("active_monitor", "monitors", "report", "settings", "cooldown_remaining"):
            self.assertIn(key, payload)

    def test_since_and_monitor_id_are_parsed(self):
        stub = StubController(payload={"ok": True})
        server = self._start_server(stub)
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request("GET", "/api/state?since=1700&monitor_id=m2")
            connection.getresponse().read()
        finally:
            connection.close()

        self.assertEqual(stub.calls, [(1700, "m2")])

    def test_non_numeric_since_is_ignored(self):
        stub = StubController(payload={"ok": True})
        server = self._start_server(stub)
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request("GET", "/api/state?since=abc")
            connection.getresponse().read()
        finally:
            connection.close()

        self.assertEqual(stub.calls, [(None, None)])

    def test_settings_endpoint(self):
        status, _, payload = self.get_json("/api/settings")

        self.assertEqual(status, 200)
        self.assertIn("auto_save", payload)

    def test_unknown_api_returns_404(self):
        status, _, payload = self.get_json("/api/nope")

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "接口不存在")


class ReportApiTest(ServerTestBase):
    def test_no_report_returns_404(self):
        status, _, payload = self.get_json("/api/report")

        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_report_download_headers(self):
        self.controller.start(source=SOURCE)
        self.controller.stop()

        status, headers, body = self.request("/api/report")

        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn(f"monitor_m1_{BVID}", headers["Content-Disposition"])
        self.assertEqual(json.loads(body.decode("utf-8"))["video"]["bvid"], BVID)

    def test_removed_monitor_report_still_downloadable(self):
        self.controller.start(source=SOURCE)
        self.controller.remove()

        status, _, body = self.request("/api/report?monitor_id=m1")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["video"]["bvid"], BVID)


class MonitorApiTest(ServerTestBase):
    def test_create(self):
        status, _, payload = self.get_json("/api/monitor/create", method="POST", body={})

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["monitors"]), 1)
        self.assertEqual(payload["active_monitor"], "m1")

    def test_start_and_stop(self):
        status, _, payload = self.get_json(
            "/api/monitor/start", method="POST", body={"source": SOURCE, "interval": 30}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["running"])

        status, _, payload = self.get_json("/api/monitor/stop", method="POST", body={})
        self.assertEqual(status, 200)
        self.assertFalse(payload["running"])
        self.assertIsNotNone(payload["report"])

    def test_select_and_interval(self):
        self.controller.create()
        self.controller.create()

        status, _, payload = self.get_json(
            "/api/monitor/select", method="POST", body={"monitor_id": "m1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["active_monitor"], "m1")

        status, _, payload = self.get_json(
            "/api/monitor/interval", method="POST", body={"interval": 120}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["interval"], 120)

    def test_remove(self):
        self.controller.create()
        status, _, payload = self.get_json(
            "/api/monitor/remove", method="POST", body={"monitor_id": "m1"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["monitors"], [])

    def test_invalid_interval_maps_to_400(self):
        status, _, payload = self.get_json(
            "/api/monitor/interval", method="POST", body={"interval": 1}
        )

        self.assertEqual(status, 400)
        self.assertIn("采样周期", payload["error"])

    def test_settings_update(self):
        status, _, payload = self.get_json(
            "/api/settings", method="POST", body={"align_clock": True}
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["align_clock"])

    def test_empty_body_is_accepted(self):
        status, _, payload = self.get_json("/api/settings", method="POST", body={})
        self.assertEqual(status, 200)

    def test_malformed_json_is_treated_as_empty(self):
        status, _, payload = self.get_json(
            "/api/monitor/interval",
            method="POST",
            body=b"{ not json",
            headers={"Content-Type": "application/json"},
        )

        # 非法负载退化为空对象，缺少 interval 时按参数错误处理
        self.assertEqual(status, 400)
        self.assertIn("采样周期", payload["error"])

    def test_unknown_endpoint(self):
        status, _, payload = self.get_json("/api/monitor/nope", method="POST", body={})

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "接口不存在")


class ErrorMappingTest(unittest.TestCase):
    def _server_with(self, error: Exception) -> MonitorServer:
        server = MonitorServer(("127.0.0.1", 0), StubController(error=error), WEB_DIR)
        thread = threading.Thread(
            target=server.serve_forever, args=(0.02,), daemon=True
        )
        thread.start()

        def shutdown():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(shutdown)
        return server

    def _get(self, error: Exception, path: str = "/api/state"):
        server = self._server_with(error)
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def test_throttled_maps_to_429_with_retry_after(self):
        status, payload = self._get(
            CollectError("限流中", kind="throttled", retry_after=30)
        )

        self.assertEqual(status, 429)
        self.assertEqual(payload["retry_after"], 30)

    def test_throttled_without_retry_after_omits_field(self):
        status, payload = self._get(CollectError("限流中", kind="throttled"))

        self.assertEqual(status, 429)
        self.assertNotIn("retry_after", payload)

    def test_network_maps_to_502(self):
        status, _ = self._get(CollectError("不可达", kind="network"))
        self.assertEqual(status, 502)

    def test_input_maps_to_400(self):
        status, _ = self._get(CollectError("链接无效", kind="input"))
        self.assertEqual(status, 400)

    def test_generic_error_maps_to_400(self):
        status, _ = self._get(CollectError("业务失败"))
        self.assertEqual(status, 400)

    def test_value_error_maps_to_400(self):
        status, payload = self._get(ValueError("bad"))

        self.assertEqual(status, 400)
        self.assertIn("参数不合法", payload["error"])

    def test_unexpected_exception_maps_to_500(self):
        status, payload = self._get(RuntimeError("boom"))

        self.assertEqual(status, 500)
        self.assertIn("服务端异常", payload["error"])
        # 内部异常文本只留在启动窗口的堆栈里，不回给浏览器
        self.assertNotIn("boom", payload["error"])


if __name__ == "__main__":
    unittest.main()


class TrustTest(ServerTestBase):
    """来源校验：挡住跨站请求与 DNS 重绑定。"""

    def test_rejects_foreign_host(self):
        status, _, body = self.request("/api/state", headers={"Host": "evil.example:8765"})

        self.assertEqual(status, 403)
        self.assertIn("信任", json.loads(body.decode("utf-8"))["error"])

    def test_rejects_foreign_origin(self):
        status, _, _ = self.request(
            "/api/settings", "POST", {"auto_save": False}, {"Origin": "http://evil.example"}
        )

        self.assertEqual(status, 403)

    def test_accepts_same_origin(self):
        host, port = self.server.server_address[:2]

        status, _, _ = self.request(
            "/api/settings", "POST", {"auto_save": False}, {"Origin": f"http://127.0.0.1:{port}"}
        )

        self.assertEqual(status, 200)

    def test_accepts_request_without_origin(self):
        status, _, _ = self.request("/api/state")

        self.assertEqual(status, 200)

    def test_rejects_untrusted_before_touching_controller(self):
        status, _, _ = self.request(
            "/api/monitor/create", "POST", {}, {"Origin": "https://evil.example"}
        )

        self.assertEqual(status, 403)
        self.assertEqual(self.controller._order, [])


class BodyLimitTest(ServerTestBase):
    def test_oversized_body_is_rejected(self):
        status, _, body = self.request("/api/settings", "POST", b"{" + b"a" * (1 << 20))

        self.assertEqual(status, 400)
        self.assertIn("过大", json.loads(body.decode("utf-8"))["error"])
