"""采集层：把用户输入解析成视频标识，并抓取一次完整的指标快照。

本模块只做网络与解析，不涉及存储、调度和展示。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

API_BASE = "https://api.bilibili.com"
WEB_HOME = "https://www.bilibili.com/"
DEFAULT_TIMEOUT = 10

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

METRIC_KEYS = ("view", "danmaku", "reply", "online", "like", "coin", "favorite", "share")

METRIC_LABELS = {
    "view": "播放量",
    "danmaku": "弹幕数",
    "reply": "评论数",
    "online": "在看人数",
    "like": "点赞数",
    "coin": "投币数",
    "favorite": "收藏数",
    "share": "分享数",
}

# stat 字段名与内部指标名的对应关系，online 单独取值
_STAT_FIELDS = {
    "view": "view",
    "danmaku": "danmaku",
    "reply": "reply",
    "like": "like",
    "coin": "coin",
    "favorite": "favorite",
    "share": "share",
}

_BV_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")
_AV_PATTERN = re.compile(r"(?:^|[^0-9A-Za-z])av(\d+)", re.IGNORECASE)
_SHORT_HOSTS = ("b23.tv", "bili2233.cn")

# B站 的风控类返回码，命中后进入冷却
THROTTLE_CODES = {-352, -412, -509, -799}


class CollectError(Exception):
    """采集失败。message 直接面向用户展示。

    kind 用于让服务层给出正确的 HTTP 状态码，不再把所有失败都当成 400：
        throttled  B站 限流，可附带 retry_after 秒数
        input      用户输入无法解析
        network    网络不可达或超时
        error      其余情况
    """

    def __init__(
        self, message: str, kind: str = "error", retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


@dataclass
class VideoRef:
    """视频标识，bvid 与 aid 至少有一个。"""

    bvid: str | None = None
    aid: int | None = None


@dataclass
class VideoInfo:
    bvid: str
    aid: int
    title: str
    owner: str
    cover: str
    duration: int
    pubdate: int
    cid: int | None
    url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bvid": self.bvid,
            "aid": self.aid,
            "title": self.title,
            "owner": self.owner,
            "cover": self.cover,
            "duration": self.duration,
            "pubdate": self.pubdate,
            "cid": self.cid,
            "url": self.url,
        }


class BiliClient:
    """B站接口客户端。

    请求纪律：每个采样点对每个接口只请求一次，不做重试。
    收到 412 后进入冷却期，期间连网络请求都不发出，避免反复点击把限制时间越拖越长。
    """

    # 相邻两次请求的最小间隔，防止 view 与 online 背靠背触发限流
    MIN_REQUEST_GAP = 0.15
    # 412 后的静默冷却时长
    COOLDOWN_SECONDS = 90

    def __init__(
        self, timeout: int = DEFAULT_TIMEOUT, cooldown_file: str | None = None
    ) -> None:
        self._timeout = timeout
        self._cookie = ""
        self._cookie_attempted = False
        self._cooldown_until = 0.0
        self._last_request_at = 0.0
        self._cooldown_file = cooldown_file
        self._restore_cooldown()

    # ---------------------------------------------------------------- 解析

    def resolve(self, source: str) -> VideoRef:
        text = (source or "").strip()
        if not text:
            raise CollectError("请输入视频链接或 BV 号", kind="input")

        ref = self._match(text)
        if ref:
            return ref

        if any(host in text for host in _SHORT_HOSTS):
            ref = self._match(self._follow_short_link(text))
            if ref:
                return ref

        raise CollectError("未能识别视频，请粘贴 bilibili.com 视频页链接或 BV 号", kind="input")

    @staticmethod
    def _match(text: str) -> VideoRef | None:
        found = _BV_PATTERN.search(text)
        if found:
            return VideoRef(bvid=found.group(0))
        found = _AV_PATTERN.search(text)
        if found:
            return VideoRef(aid=int(found.group(1)))
        return None

    def _follow_short_link(self, text: str) -> str:
        url = text if text.startswith("http") else f"https://{text}"
        request = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.geturl()
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise CollectError(f"短链解析失败：{reason}", kind="network") from exc

    # ---------------------------------------------------------------- 抓取

    def fetch_snapshot(self, ref: VideoRef) -> tuple[VideoInfo, dict[str, int | None]]:
        """返回视频元信息与一次指标快照。"""
        params: dict[str, Any] = {"bvid": ref.bvid} if ref.bvid else {"aid": ref.aid}
        payload = self._get("/x/web-interface/view", params)
        data = payload.get("data") or {}
        if not data:
            raise CollectError("B站未返回视频信息，可能已被删除或不可见", kind="error")

        stat = data.get("stat") or {}
        pages = data.get("pages") or []
        cid = pages[0].get("cid") if pages else None
        bvid = str(data.get("bvid") or ref.bvid or "")

        info = VideoInfo(
            bvid=bvid,
            aid=int(data.get("aid") or ref.aid or 0),
            title=str(data.get("title") or "未命名视频"),
            owner=str((data.get("owner") or {}).get("name") or "未知UP主"),
            cover=str(data.get("pic") or ""),
            duration=int(data.get("duration") or 0),
            pubdate=int(data.get("pubdate") or 0),
            cid=cid,
            url=f"https://www.bilibili.com/video/{bvid}" if bvid else (ref.bvid or ""),
        )

        metrics: dict[str, int | None] = {}
        for key, field in _STAT_FIELDS.items():
            metrics[key] = _to_int(stat.get(field))
        metrics["online"] = self._fetch_online(bvid, cid)
        return info, metrics

    def _fetch_online(self, bvid: str, cid: int | None) -> int | None:
        """同时在看人数。每个采样点只请求一次，失败即留空，不重试。

        total 取不到时回退同一响应里的 count；仍未开放则返回 None，
        不影响同一次采样的其余指标。
        """
        if not bvid or not cid:
            return None

        try:
            payload = self._get("/x/player/online/total", {"bvid": bvid, "cid": cid})
        except CollectError:
            return None

        data = payload.get("data") or {}
        total = _to_int(data.get("total"))
        if total is not None:
            return total
        if (data.get("show_switch") or {}).get("total") is False:
            return None  # 该视频未开放在线人数
        return _to_int(data.get("count"))

    # ---------------------------------------------------------------- 底层

    def cooldown_remaining(self) -> float:
        """距离解除限制还有多少秒，0 表示可用。"""
        return max(0.0, self._cooldown_until - time.monotonic())

    def _enter_cooldown(self) -> None:
        self._cooldown_until = time.monotonic() + self.COOLDOWN_SECONDS
        self._persist_cooldown()

    def _restore_cooldown(self) -> None:
        """从磁盘恢复冷却，避免重启后立刻再撞一次限制。"""
        if not self._cooldown_file:
            return
        try:
            with open(self._cooldown_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            until = float(data.get("until") or 0)
        except (OSError, ValueError, TypeError):
            return
        remaining = until - time.time()
        if remaining > 0:
            self._cooldown_until = time.monotonic() + remaining

    def _persist_cooldown(self) -> None:
        if not self._cooldown_file:
            return
        try:
            with open(self._cooldown_file, "w", encoding="utf-8") as handle:
                json.dump(
                    {"until": time.time() + self.cooldown_remaining()}, handle
                )
        except OSError:
            pass

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": WEB_HOME,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    def _ensure_cookie(self) -> None:
        """只申请一次指纹。失败就不再尝试，避免每次请求都多打一个接口。"""
        if self._cookie or self._cookie_attempted:
            return
        self._cookie_attempted = True
        try:
            payload = self._get("/x/frontend/finger/spi", {}, use_cookie=False)
        except CollectError:
            return
        data = payload.get("data") or {}
        b3 = data.get("b_3")
        if b3:
            self._cookie = f"buvid3={b3}; buvid4={data.get('b_4') or ''}"

    def _get(self, path: str, params: dict[str, Any], *, use_cookie: bool = True) -> dict[str, Any]:
        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            wait = int(remaining) + 1
            raise CollectError(
                f"B站 正在限制访问，请等待 {wait} 秒后再试",
                kind="throttled",
                retry_after=wait,
            )

        if use_cookie:
            self._ensure_cookie()

        gap = time.monotonic() - self._last_request_at
        if gap < self.MIN_REQUEST_GAP:
            time.sleep(self.MIN_REQUEST_GAP - gap)
        self._last_request_at = time.monotonic()

        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{API_BASE}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(url, headers=self._headers())

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 412:
                self._enter_cooldown()
                raise CollectError(
                    f"B站 临时限制了访问（HTTP 412）。这通常由短时间内请求过多引起，"
                    f"与采样周期无关。已暂停请求 {self.COOLDOWN_SECONDS} 秒，请稍后重试",
                    kind="throttled",
                    retry_after=self.COOLDOWN_SECONDS,
                ) from exc
            if exc.code == 403:
                raise CollectError("B站 拒绝访问（HTTP 403），该内容可能需要登录或已下架", kind="error") from exc
            raise CollectError(f"请求 B站接口失败：HTTP {exc.code}", kind="error") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise CollectError(f"网络不可达：{reason}", kind="network") from exc
        except TimeoutError as exc:
            raise CollectError("请求 B站接口超时", kind="network") from exc

        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise CollectError("B站返回了无法解析的响应", kind="error") from exc

        code = payload.get("code")
        if code != 0:
            message = payload.get("message") or "未知错误"
            if code in THROTTLE_CODES:
                self._enter_cooldown()
                raise CollectError(
                    f"B站 风控拦截 [{code}]：{message}。已暂停请求 {self.COOLDOWN_SECONDS} 秒",
                    kind="throttled",
                    retry_after=self.COOLDOWN_SECONDS,
                )
            raise CollectError(f"B站接口错误 [{code}]：{message}", kind="error")
        return payload


def _to_int(value: Any) -> int | None:
    """把接口返回的数字或数字字符串转成 int，无法转换时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or not text.lstrip("-").isdigit():
            return None
        return int(text)
    return None
