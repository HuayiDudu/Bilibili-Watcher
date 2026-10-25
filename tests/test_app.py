"""app 模块测试：端口顺延与退出清理。

网络、浏览器与配置文件全部打桩，只验证装配与退出路径。
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

import app


def _quiet_main(argv):
    """吞掉启动横幅，保持测试输出干净。"""
    with contextlib.redirect_stdout(io.StringIO()):
        return app.main(argv)


class CreateServerTest(unittest.TestCase):
    def setUp(self):
        self.controller = mock.MagicMock()

    def test_uses_requested_port_when_free(self):
        server = mock.MagicMock()
        with mock.patch("app.MonitorServer", return_value=server) as factory:
            result = app.create_server(8765, self.controller)

        self.assertIs(result, server)
        self.assertEqual(factory.call_args[0][0], ("127.0.0.1", 8765))

    def test_skips_busy_ports(self):
        attempts = []

        def factory(address, controller, web_dir):
            attempts.append(address[1])
            if len(attempts) < 3:
                raise OSError("端口占用")
            return mock.MagicMock()

        with mock.patch("app.MonitorServer", side_effect=factory):
            app.create_server(8765, self.controller)

        self.assertEqual(attempts, [8765, 8766, 8767])

    def test_raises_when_all_ports_busy(self):
        with mock.patch("app.MonitorServer", side_effect=OSError("端口占用")):
            with self.assertRaises(SystemExit):
                app.create_server(8765, self.controller)

    def test_attempt_range_is_twenty_ports(self):
        attempts = []

        def factory(address, controller, web_dir):
            attempts.append(address[1])
            raise OSError("端口占用")

        with mock.patch("app.MonitorServer", side_effect=factory):
            with self.assertRaises(SystemExit):
                app.create_server(8765, self.controller)

        self.assertEqual(len(attempts), 20)
        self.assertEqual(attempts[-1], 8765 + 19)


class MainTest(unittest.TestCase):
    def _prepare(self, **kwargs):
        server = mock.MagicMock()
        server.server_address = ("127.0.0.1", 8765)
        server.serve_forever.side_effect = kwargs.get("serve_side_effect", KeyboardInterrupt)
        controller = mock.MagicMock()
        if "stop_side_effect" in kwargs:
            controller.stop_all.side_effect = kwargs["stop_side_effect"]
        return server, controller

    def test_returns_zero_on_interrupt(self):
        server, controller = self._prepare()

        with mock.patch("app.Settings"), mock.patch("app.BiliClient"), mock.patch(
            "app.MonitorController", return_value=controller
        ), mock.patch("app.create_server", return_value=server):
            code = _quiet_main(["--no-browser"])

        self.assertEqual(code, 0)
        controller.stop_all.assert_called_once_with()
        server.server_close.assert_called_once_with()

    def test_closes_server_even_if_stop_fails(self):
        server, controller = self._prepare(stop_side_effect=RuntimeError("结算失败"))

        with mock.patch("app.Settings"), mock.patch("app.BiliClient"), mock.patch(
            "app.MonitorController", return_value=controller
        ), mock.patch("app.create_server", return_value=server):
            code = _quiet_main(["--no-browser"])

        self.assertEqual(code, 0)
        server.server_close.assert_called_once_with()

    def test_no_browser_flag_avoids_opening(self):
        server, controller = self._prepare()

        with mock.patch("app.Settings"), mock.patch("app.BiliClient"), mock.patch(
            "app.MonitorController", return_value=controller
        ), mock.patch("app.create_server", return_value=server), mock.patch(
            "app.threading.Timer"
        ) as timer:
            _quiet_main(["--no-browser"])

        timer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
