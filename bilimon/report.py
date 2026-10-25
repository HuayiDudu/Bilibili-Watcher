"""报告层：把一次监控会话的样本聚合成结构化 JSON 并写入磁盘。

本模块只做聚合与写文件，不持有会话状态、不依赖服务层。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .collector import METRIC_KEYS, METRIC_LABELS

SCHEMA_VERSION = "1.0"
REPORT_TYPE = "bilibili_video_monitor"
TIMEZONE = timezone(timedelta(hours=8))

# generated_at 形如 2026-10-25T21:45:53，取到秒即可
_ISO_SECONDS = 19
# 去掉分隔符后的时间戳长度，如 20261025_214553
_STAMP_LENGTH = 15
MS_PER_MINUTE = 60_000


def build(context: dict[str, Any]) -> dict[str, Any]:
    """根据会话上下文生成报告结构。"""
    samples: list[dict[str, Any]] = context.get("samples") or []
    started_at = context.get("started_at")
    stopped_at = context.get("stopped_at")

    if started_at is None and samples:
        started_at = samples[0]["t"]
    if stopped_at is None and samples:
        stopped_at = samples[-1]["t"]

    duration_seconds = (
        round((stopped_at - started_at) / 1000, 1)
        if started_at and stopped_at and stopped_at >= started_at
        else 0
    )

    video = context.get("video")
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": REPORT_TYPE,
        "generated_at": _iso(_now_ms()),
        "video": video,
        "monitor": {
            "id": context.get("monitor_id"),
            "started_at": _iso(started_at) if started_at else None,
            "stopped_at": _iso(stopped_at) if stopped_at else None,
            "duration_seconds": duration_seconds,
            "interval_seconds": context.get("interval"),
            "sample_count": len(samples),
            "success_count": context.get("success_count", 0),
            "failure_count": context.get("failure_count", 0),
            "last_error": context.get("last_error"),
        },
        "metrics": {key: _metric_stats(samples, key) for key in METRIC_KEYS},
        "samples": _serialize_samples(samples, started_at),
    }


def filename(document: dict[str, Any]) -> str:
    """报告文件名由监控 id、视频编号与生成时间决定，下载与落盘共用同一规则。"""
    monitor = document.get("monitor") or {}
    video = document.get("video") or {}
    monitor_id = monitor.get("id") or "m0"
    bvid = video.get("bvid") or "unknown"
    generated = str(document.get("generated_at") or "")[:_ISO_SECONDS]
    stamp = generated.replace("-", "").replace(":", "").replace("T", "_")
    if len(stamp) < _STAMP_LENGTH:
        stamp = datetime.now(TIMEZONE).strftime("%Y%m%d_%H%M%S")
    return f"monitor_{monitor_id}_{bvid}_{stamp}.json"


def write(report_dir: str, document: dict[str, Any]) -> str:
    """把报告写入目录，返回文件绝对路径。"""
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.abspath(os.path.join(report_dir, filename(document)))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
    return path


def _metric_stats(samples: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    points = [(s["t"], s[key]) for s in samples if isinstance(s.get(key), int)]
    if not points:
        return {"label": METRIC_LABELS[key], "sample_count": 0}

    values = [value for _, value in points]
    first, last = values[0], values[-1]
    span_minutes = (points[-1][0] - points[0][0]) / MS_PER_MINUTE

    peak_gain, peak_at = None, None
    for (prev_t, prev_v), (cur_t, cur_v) in zip(points, points[1:]):
        gain = cur_v - prev_v
        if peak_gain is None or gain > peak_gain:
            peak_gain, peak_at = gain, cur_t

    return {
        "label": METRIC_LABELS[key],
        "first": first,
        "last": last,
        "delta": last - first,
        "min": min(values),
        "max": max(values),
        "average": round(sum(values) / len(values), 2),
        "peak_gain": peak_gain,
        "peak_gain_at": _iso(peak_at) if peak_at else None,
        "average_per_minute": round((last - first) / span_minutes, 3)
        if span_minutes > 0
        else None,
        "sample_count": len(points),
    }


def _serialize_samples(
    samples: list[dict[str, Any]], started_at: int | None
) -> list[dict[str, Any]]:
    result = []
    for sample in samples:
        item: dict[str, Any] = {
            "time": _iso(sample["t"]),
            "elapsed_seconds": round((sample["t"] - started_at) / 1000, 1)
            if started_at
            else None,
        }
        for key in METRIC_KEYS:
            item[key] = sample.get(key)
        result.append(item)
    return result


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, TIMEZONE).isoformat(timespec="seconds")


def _now_ms() -> int:
    return int(time.time() * 1000)
