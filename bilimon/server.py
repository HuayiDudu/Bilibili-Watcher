"""服务层：托管前端静态资源，对外提供监控控制与数据查询接口。

本模块只做 HTTP 协议处理，业务逻辑全部委托给编排层。
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import filepicker
from .collector import CollectError
from .control import MonitorController
from .report import filename as report_filename

# 只接受来自本机的请求。浏览器在跨站请求里一定会带 Origin，
# 而 DNS 重绑定会让攻击者的域名解析到 127.0.0.1，因此 Host 也要校验。
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 接口体都很小，超出这个体积直接拒绝，避免被大包拖住
MAX_BODY_BYTES = 1 << 20
# 拒绝前先读完丢弃的上限：读掉才能正常回 413/400，否则连接会被客户端重置
DRAIN_LIMIT = 8 << 20
# 丢弃请求体时的分块大小
DRAIN_CHUNK_BYTES = 64 * 1024

MIME_OVERRIDES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 的 SO_REUSEADDR 允许两个进程同时绑定同一端口，
    # 打开它会让"端口被占用就顺延"永远不触发，两个实例静默抢同一端口。
    allow_reuse_address = sys.platform != "win32"

    def __init__(
        self, address: tuple[str, int], controller: MonitorController, web_dir: str
    ) -> None:
        super().__init__(address, RequestHandler)
        self.controller = controller
        self.web_dir = os.path.abspath(web_dir)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "BiliMonitor/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- 路由

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._guard(self._dispatch_get, parsed)
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        self._guard(self._dispatch_post, parsed)

    def _dispatch_get(self, parsed) -> None:
        path = parsed.path
        if path == "/api/state":
            query = parse_qs(parsed.query)
            since_param = (query.get("since") or [""])[0]
            since = int(since_param) if since_param.isdigit() else None
            monitor_id = (query.get("monitor_id") or [None])[0]
            self._send_json(self.server.controller.state(since, monitor_id))
            return
        if path == "/api/report":
            query = parse_qs(parsed.query)
            monitor_id = (query.get("monitor_id") or [None])[0]
            name = (query.get("name") or [None])[0]
            self._serve_report(monitor_id, name)
            return
        if path == "/api/fs":
            query = parse_qs(parsed.query)
            target = (query.get("path") or [""])[0]
            mode = (query.get("mode") or ["any"])[0]
            ext_param = (query.get("ext") or [""])[0]
            exts = [item for item in ext_param.split(",") if item.strip()]
            self._send_json(filepicker.list_directory(target, mode, exts))
            return
        if path == "/api/reports":
            self._send_json(self.server.controller.reports_page())
            return
        if path == "/api/reports/detail":
            query = parse_qs(parsed.query)
            key = (query.get("key") or [""])[0]
            self._send_json(self.server.controller.report_detail(key))
            return
        if path == "/api/settings":
            self._send_json(self.server.controller.settings())
            return
        self._send_json({"error": "接口不存在"}, 404)

    def _dispatch_post(self, parsed) -> None:
        path = parsed.path
        payload = self._read_json_body()

        if path == "/api/monitor/create":
            result = self.server.controller.create()
        elif path == "/api/monitor/start":
            result = self.server.controller.start(
                payload.get("monitor_id"),
                payload.get("source"),
                payload.get("interval"),
                payload.get("align_clock"),
            )
        elif path == "/api/monitor/stop":
            result = self.server.controller.stop(payload.get("monitor_id"))
        elif path == "/api/monitor/remove":
            result = self.server.controller.remove(payload.get("monitor_id"))
        elif path == "/api/monitor/select":
            result = self.server.controller.select(payload.get("monitor_id"))
        elif path == "/api/monitor/interval":
            result = self.server.controller.set_interval(payload.get("interval"))
        elif path == "/api/reports/import":
            result = self.server.controller.import_reports(payload.get("paths"))
        elif path == "/api/reports/export":
            result = self.server.controller.export_reports(
                payload.get("names"),
                payload.get("live_ids"),
                payload.get("target_dir"),
            )
        elif path == "/api/reports/delete":
            result = self.server.controller.delete_report(payload.get("name"))
        elif path == "/api/reports/save-live":
            result = self.server.controller.save_live_reports()
        elif path == "/api/settings":
            result = self.server.controller.update_settings(
                payload.get("auto_save"),
                payload.get("auto_save_dir"),
                payload.get("align_clock"),
                payload.get("import_dirs"),
            )
        else:
            self._send_json({"error": "接口不存在"}, 404)
            return
        self._send_json(result)

    def _trusted(self) -> bool:
        """校验请求来源是否为本机页面。

        Host 挡住 DNS 重绑定（攻击者域名解析到 127.0.0.1 后，Host 仍是外部域名）；
        Origin 挡住跨站写操作（text/plain 等简单请求不触发预检，能直接打到接口）。
        命令行与同源 GET 不一定带 Origin，缺失时放行。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.split("]")[0].lstrip("[") if host.startswith("[") else host.split(":")[0]
        if name not in LOCAL_HOSTS:
            return False

        origin = (self.headers.get("Origin") or "").strip().lower()
        if not origin:
            return True

        port = self.server.server_address[1]
        allowed = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }
        return origin in allowed

    def _guard(self, handler, parsed) -> None:
        """统一异常出口：按错误性质给出对应状态码。"""
        try:
            if not self._trusted():
                self._send_json({"error": "请求来源不受信任"}, 403)
                return
            handler(parsed)
        except CollectError as exc:
            self._send_collect_error(exc)
        except ValueError as exc:
            self._send_json({"error": f"参数不合法：{exc}"}, 400)
        except BrokenPipeError:
            return
        except Exception:  # 防御性兜底，避免线程崩溃无响应
            # 堆栈留给启动窗口，回给浏览器的是通用文案，避免暴露内部细节
            traceback.print_exc()
            self._send_json({"error": "服务端异常，详情见启动窗口"}, 500)

    def _send_collect_error(self, exc: CollectError) -> None:
        payload: dict = {"error": str(exc)}
        if exc.kind == "throttled":
            if exc.retry_after:
                payload["retry_after"] = exc.retry_after
            self._send_json(payload, 429)
        elif exc.kind == "network":
            self._send_json(payload, 502)
        else:
            self._send_json(payload, 400)

    # ---------------------------------------------------------------- 静态

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = os.path.normpath(os.path.join(self.server.web_dir, relative))
        root = self.server.web_dir

        if target != root and not target.startswith(root + os.sep):
            self._send_json({"error": "禁止访问"}, 403)
            return
        if not os.path.isfile(target):
            self._send_json({"error": "文件不存在"}, 404)
            return

        content_type = MIME_OVERRIDES.get(os.path.splitext(target)[1].lower())
        if content_type is None:
            content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"

        with open(target, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_report(
        self, monitor_id: str | None = None, name: str | None = None
    ) -> None:
        document = (
            self.server.controller.report_file(name)
            if name
            else self.server.controller.report_document(monitor_id)
        )
        if not document:
            self._send_json({"error": "暂无报告，请先停止一次监控"}, 404)
            return

        body = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        filename = report_filename(document)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{filename}"'
        )
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------------- 工具

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("Content-Length 不合法") from exc
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            # 小超标的读取丢弃，让客户端能收到正常响应；极端大的直接断开
            if length <= DRAIN_LIMIT:
                self._discard(length)
            self.close_connection = True
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _discard(self, length: int) -> None:
        """读掉并丢弃请求体，避免未读完就响应导致连接被重置。"""
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, DRAIN_CHUNK_BYTES))
            if not chunk:
                return
            remaining -= len(chunk)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        """只保留异常请求的日志，避免轮询刷屏。"""
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            print(f"[http] {self.address_string()} {format % args}")
