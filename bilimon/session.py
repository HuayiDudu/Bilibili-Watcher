"""监控会话：在内存中保存一次监控的时序数据，并用后台线程按周期采样。

设计要点：
    1. 任意时刻只对应一个视频，数据只存在进程内存，不落库。
    2. 采样周期可热改，改完立即按新节奏排下一次采样，不额外触发采样。
    3. 单次采样失败不终止会话，只记录错误并继续下一轮。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from . import schedule
from .collector import (
    METRIC_KEYS,
    METRIC_LABELS,
    BiliClient,
    CollectError,
    VideoInfo,
    VideoRef,
)

MIN_INTERVAL = 5
MAX_INTERVAL = 3600
MAX_SAMPLES = 20000
DEFAULT_INTERVAL = 30

_TICK = 0.25
# 首次采样延迟超过这个秒数才算"延后开始"，此时探测样本不计入
_DEFER_THRESHOLD = 0.5
# 停止时等待采样线程退出的上限
_STOP_TIMEOUT = 5


class MonitorSession:
    """一次监控会话的完整状态。"""

    def __init__(self, client: BiliClient | None = None) -> None:
        self._client = client or BiliClient()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_due = 0.0
        # 每启动一次自增；旧线程据此确认自己是否已被新会话取代
        self._generation = 0

        self._state = "idle"
        self._ref: VideoRef | None = None
        self._video: VideoInfo | None = None
        self._samples: list[dict[str, Any]] = []
        self._interval = DEFAULT_INTERVAL
        self._started_at: int | None = None
        self._stopped_at: int | None = None
        self._last_error: str | None = None
        self._success_count = 0
        self._failure_count = 0
        self._align_clock = False
        # 由控制器在创建监控槽位时赋值；会话本身不依赖该值
        self.monitor_id: str | None = None

    # ---------------------------------------------------------------- 状态

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._state == "running"

    @property
    def interval(self) -> int:
        with self._lock:
            return self._interval

    @property
    def bvid(self) -> str | None:
        """当前监控的视频 bvid，未绑定目标时为 None。"""
        with self._lock:
            return self._video.bvid if self._video else None

    def snapshot(self, since: int | None = None) -> dict[str, Any]:
        """对外状态快照。since 为毫秒时间戳，只返回其后的样本。"""
        with self._lock:
            samples = (
                self._samples
                if since is None
                else [s for s in self._samples if s["t"] > since]
            )
            return {
                "state": self._state,
                "running": self._state == "running",
                "video": self._video.as_dict() if self._video else None,
                "interval": self._interval,
                "sample_count": len(self._samples),
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "last_error": self._last_error,
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "align_clock": self._align_clock,
                "next_sample_at": self._next_sample_ms(),
                "metrics": [
                    {"key": key, "label": METRIC_LABELS[key]} for key in METRIC_KEYS
                ],
                "samples": samples,
                "server_time": _now_ms(),
            }

    def samples(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._samples)

    def cooldown_remaining(self) -> float:
        """B站 限流冷却剩余秒数，0 表示可正常请求。"""
        return self._client.cooldown_remaining()

    def context(self) -> dict[str, Any]:
        """生成报告所需的会话上下文。"""
        with self._lock:
            return {
                "monitor_id": self.monitor_id,
                "video": self._video.as_dict() if self._video else None,
                "interval": self._interval,
                "started_at": self._started_at,
                "stopped_at": self._stopped_at,
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "last_error": self._last_error,
                "samples": list(self._samples),
            }

    # ---------------------------------------------------------------- 控制

    def start(
        self, source: str, interval: int | None = None, align_clock: bool = False
    ) -> dict[str, Any]:
        """开始监控。首次采样同步完成，链接无效时直接抛出异常。"""
        return self.begin(self.prepare(source, interval), align_clock)

    def _next_sample_ms(self) -> int | None:
        """下一次采样时刻（epoch 毫秒），仅在运行中有效。"""
        if self._state != "running" or not self._next_due:
            return None
        return _now_ms() + int((self._next_due - time.monotonic()) * 1000)

    def prepare(self, source: str, interval: int | None = None) -> dict[str, Any]:
        return prepare_snapshot(self._client, source, interval)

    def begin(
        self, prepared: dict[str, Any], align_clock: bool = False
    ) -> dict[str, Any]:
        """用 prepare 的结果启动会话。

        align_clock 为真时，首次采样对齐到整点：prepare 阶段的探测采样
        只用于校验链接，不计入样本，实际数据从对齐时刻开始。
        """
        with self._lock:
            if self._state == "running":
                raise CollectError("监控正在运行，请先停止当前监控")

            now = prepared["timestamp"]
            seconds = prepared["interval"]
            start_at = (
                schedule.first_sample_at(now, seconds) if align_clock else now
            )
            delay = max(0.0, (start_at - now) / 1000)
            deferred = delay > _DEFER_THRESHOLD

            # 每轮都给一个新事件：上一轮线程持有的旧事件保持"已置位"，
            # 即使它还没退出，也不会被 clear 唤醒后继续采样。
            self._generation += 1
            generation = self._generation
            self._stop_event = threading.Event()
            self._ref = prepared["ref"]
            self._video = prepared["info"]
            self._interval = seconds
            self._align_clock = align_clock
            self._samples = (
                [] if deferred else [_make_sample(now, prepared["metrics"])]
            )
            self._started_at = now
            self._stopped_at = None
            self._last_error = None
            self._success_count = 0 if deferred else 1
            self._failure_count = 0
            self._state = "running"
            self._next_due = time.monotonic() + (
                delay if deferred else float(seconds)
            )
            self._thread = threading.Thread(
                target=self._loop,
                args=(generation, self._stop_event),
                name="bili-monitor-sampler",
                daemon=True,
            )
            self._thread.start()
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        """停止采样并冻结会话，返回最终快照。"""
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=_STOP_TIMEOUT)

        with self._lock:
            self._thread = None
            self._next_due = 0.0
            if self._state == "running":
                self._state = "stopped"
                self._stopped_at = _now_ms()
        return self.snapshot()

    def set_interval(self, interval: int) -> dict[str, Any]:
        """热更新采样周期，立即按新节奏重排下一次采样。"""
        seconds = clamp_interval(interval)
        with self._lock:
            self._interval = seconds
            if self._state == "running" and self._samples:
                elapsed = time.time() - self._samples[-1]["t"] / 1000
                remaining = max(_TICK, seconds - elapsed)
                self._next_due = time.monotonic() + remaining
        return self.snapshot()

    # ---------------------------------------------------------------- 采样

    def _loop(self, generation: int, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            with self._lock:
                if self._generation != generation:
                    return
                due = self._next_due
            remaining = due - time.monotonic()
            if remaining > 0:
                if stop_event.wait(min(remaining, _TICK)):
                    return
                continue

            # 用计划时刻而非请求完成时刻打时间戳，否则每个点都会带上请求耗时
            scheduled_ms = _now_ms() - int((time.monotonic() - due) * 1000)
            self._sample_once(scheduled_ms, generation)
            with self._lock:
                if self._generation != generation or self._state != "running":
                    return
                # 按绝对时间轴递推，避免每轮把采样耗时累加进周期
                self._next_due += self._interval
                now_mono = time.monotonic()
                if self._next_due < now_mono:
                    # 落后超过一个周期（如网络阻塞），跳过错过的时隙，不补采
                    missed = int((now_mono - self._next_due) // self._interval) + 1
                    self._next_due += missed * self._interval

    def _sample_once(
        self, scheduled_ms: int | None = None, generation: int | None = None
    ) -> None:
        """采样一次。generation 用于让旧线程在会话已换代时放弃写入。"""
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            ref = self._ref
            if self._state != "running" or ref is None:
                return

        # 处于限流冷却期时不发请求，也不算一次失败，只提示恢复时间
        remaining = self._client.cooldown_remaining()
        if remaining > 0:
            with self._lock:
                if self._state == "running":
                    self._last_error = (
                        f"B站 限制访问中，约 {int(remaining) + 1} 秒后恢复采集"
                    )
            return

        try:
            info, metrics = self._client.fetch_snapshot(ref)
        except CollectError as exc:
            self._record_failure(str(exc))
            return
        except Exception as exc:  # 防御性兜底，避免线程意外退出
            self._record_failure(f"采样异常：{exc}")
            return

        now = _now_ms() if scheduled_ms is None else scheduled_ms
        with self._lock:
            # 请求期间的 stop/重启会让状态与代次都变，结果一律丢弃
            if self._state != "running":
                return
            if generation is not None and generation != self._generation:
                return
            self._video = info
            self._samples.append(_make_sample(now, metrics))
            if len(self._samples) > MAX_SAMPLES:
                del self._samples[: len(self._samples) - MAX_SAMPLES]
            self._success_count += 1
            self._last_error = None

    def _record_failure(self, message: str) -> None:
        with self._lock:
            if self._state != "running":
                return
            self._failure_count += 1
            self._last_error = message


def prepare_snapshot(
    client: BiliClient, source: str, interval: int | None = None
) -> dict[str, Any]:
    """校验目标并完成一次探测采样，但不绑定任何会话。

    与 begin 分离的原因：切换视频时必须先确认新目标可用，
    否则一次错误输入就会把正在运行的监控停掉。
    """
    seconds = DEFAULT_INTERVAL if interval is None else clamp_interval(interval)
    ref = client.resolve(source)
    info, metrics = client.fetch_snapshot(ref)
    return {
        "ref": ref,
        "info": info,
        "metrics": metrics,
        "interval": seconds,
        "timestamp": _now_ms(),
    }


def clamp_interval(value: Any) -> int:
    try:
        seconds = int(round(float(value)))
    # float('inf') 会抛 OverflowError，同样属于输入不合法
    except (TypeError, ValueError, OverflowError) as exc:
        raise CollectError("采样周期必须是数字（单位：秒）") from exc
    if seconds < MIN_INTERVAL:
        raise CollectError(f"采样周期不能小于 {MIN_INTERVAL} 秒，避免触发 B站限流")
    if seconds > MAX_INTERVAL:
        raise CollectError(f"采样周期不能大于 {MAX_INTERVAL} 秒")
    return seconds


def empty_snapshot(interval: int = DEFAULT_INTERVAL) -> dict[str, Any]:
    """没有绑定会话时的快照，字段与 MonitorSession.snapshot 保持一致。"""
    return {
        "state": "idle",
        "running": False,
        "video": None,
        "interval": interval,
        "sample_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "last_error": None,
        "started_at": None,
        "stopped_at": None,
        "align_clock": False,
        "next_sample_at": None,
        "metrics": [
            {"key": key, "label": METRIC_LABELS[key]} for key in METRIC_KEYS
        ],
        "samples": [],
        "server_time": _now_ms(),
    }


def _make_sample(timestamp: int, metrics: dict[str, int | None]) -> dict[str, Any]:
    sample: dict[str, Any] = {"t": timestamp}
    for key in METRIC_KEYS:
        sample[key] = metrics.get(key)
    return sample


def _now_ms() -> int:
    return int(time.time() * 1000)
