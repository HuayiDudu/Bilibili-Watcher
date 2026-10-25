"""测试夹具：替身客户端与数据构造器。

替身只实现被测层真正调用的方法，不复制被测逻辑，避免测试与实现同错。
"""

from __future__ import annotations

from bilimon.collector import METRIC_KEYS, VideoInfo, VideoRef

BVID = "BV1xx411c7mD"
AID = 170001
VIDEO_URL = f"https://www.bilibili.com/video/{BVID}"


def make_info(**overrides) -> VideoInfo:
    fields = {
        "bvid": BVID,
        "aid": AID,
        "title": "测试视频",
        "owner": "测试UP主",
        "cover": "https://i0.hdslb.com/cover.jpg",
        "duration": 120,
        "pubdate": 1_700_000_000,
        "cid": 111,
        "url": VIDEO_URL,
    }
    fields.update(overrides)
    return VideoInfo(**fields)


def make_metrics(**overrides) -> dict[str, int | None]:
    metrics: dict[str, int | None] = {key: 0 for key in METRIC_KEYS}
    metrics.update(
        view=1000,
        danmaku=10,
        reply=5,
        online=3,
        like=50,
        coin=20,
        favorite=8,
        share=2,
    )
    metrics.update(overrides)
    return metrics


class FakeClient:
    """BiliClient 的替身，记录调用次数并可按需失败。"""

    def __init__(
        self,
        metrics: dict[str, int | None] | None = None,
        info: VideoInfo | None = None,
        resolve_error: Exception | None = None,
        fetch_error: Exception | None = None,
        cooldown: float = 0.0,
    ) -> None:
        self.ref = VideoRef(bvid=BVID)
        self.info = info or make_info()
        self.metrics = metrics if metrics is not None else make_metrics()
        self.resolve_error = resolve_error
        self.fetch_error = fetch_error
        self._cooldown = cooldown

        self.resolve_calls: list[str] = []
        self.fetch_calls: list[VideoRef] = []

    def resolve(self, source: str) -> VideoRef:
        self.resolve_calls.append(source)
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.ref

    def fetch_snapshot(self, ref: VideoRef) -> tuple[VideoInfo, dict[str, int | None]]:
        self.fetch_calls.append(ref)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.info, dict(self.metrics)

    def cooldown_remaining(self) -> float:
        return self._cooldown

    def set_cooldown(self, seconds: float) -> None:
        self._cooldown = seconds
