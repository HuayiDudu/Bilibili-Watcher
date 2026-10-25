"""B站视频监控工具 —— 启动入口。

只负责装配各层并启动 HTTP 服务，不含任何业务逻辑。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

from bilimon.collector import BiliClient
from bilimon.control import MonitorController
from bilimon.server import MonitorServer
from bilimon.settings import Settings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
COOLDOWN_FILE = os.path.join(BASE_DIR, "cooldown.json")
REPORT_DIR = os.path.join(BASE_DIR, "reports")
DEFAULT_PORT = 8765
# 端口被占用时向后探测的个数
PORT_SCAN_LIMIT = 20
# 服务起来后延迟打开浏览器，确保端口已进入监听
BROWSER_OPEN_DELAY = 0.8


def create_server(port: int, controller: MonitorController) -> MonitorServer:
    """端口被占用时向后顺延，避免启动失败。"""
    last_error: OSError | None = None
    for candidate in range(port, port + PORT_SCAN_LIMIT):
        try:
            return MonitorServer(("127.0.0.1", candidate), controller, WEB_DIR)
        except OSError as exc:
            last_error = exc
    last = port + PORT_SCAN_LIMIT - 1
    raise SystemExit(f"端口 {port}-{last} 均被占用：{last_error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="B站视频监控工具")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    # 不强制改编码：Windows 控制台默认是本地代码页（GBK），强制 UTF-8 会输出乱码。
    # 只兜底不可编码字符，保证任何控制台下都不会因打印异常退出。
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

    settings = Settings(BASE_DIR)
    client = BiliClient(cooldown_file=COOLDOWN_FILE)
    controller = MonitorController(client, settings, REPORT_DIR)
    server = create_server(args.port, controller)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"

    print("=" * 46)
    print("  B站视频监控工具")
    print(f"  访问地址：{url}")
    print("  停止服务：在此窗口按 Ctrl+C")
    print("=" * 46)

    if not args.no_browser:
        threading.Timer(BROWSER_OPEN_DELAY, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        # 关闭窗口等同于停止所有监控，每个有样本的会话都要落一份报告。
        # 清理失败不应阻止服务关闭，否则退出会被异常打断。
        try:
            controller.stop_all()
        except Exception:
            pass
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
