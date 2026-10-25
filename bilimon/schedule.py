"""采样时刻计算。

单独成模块的原因：整点对齐是一段纯算术，既不属于会话状态机也不属于采集，
独立出来才能脱离网络与线程单独验证。
"""

from __future__ import annotations

import datetime

MINUTE_MS = 60_000
HALF_HOUR_MS = 30 * 60 * 1000
# 5 分钟及以上对齐半小时整点，以下对齐整分钟
HALF_HOUR_THRESHOLD = 300


def anchor_interval(interval_seconds: int) -> int:
    """返回该周期对应的锚点间隔（毫秒）。"""
    return HALF_HOUR_MS if interval_seconds >= HALF_HOUR_THRESHOLD else MINUTE_MS


def _offset_ms(epoch_seconds: float) -> int:
    """指定时刻本地时区相对 UTC 的偏移，用于把锚点对齐到本地整点。"""
    local = datetime.datetime.fromtimestamp(epoch_seconds).astimezone()
    return int(local.utcoffset().total_seconds() * 1000)


def first_sample_at(now_ms: int, interval_seconds: int) -> int:
    """开启整点采样时的首次采样时刻（epoch 毫秒）。

    取不小于当前时刻的最近锚点，从锚点往回按周期倒数，
    取不早于当前时刻的点；周期大于距锚点的间隔时倒数结果为 0，
    即直接等待到该锚点。
    """
    period = max(1, interval_seconds) * 1000
    anchor = anchor_interval(interval_seconds)
    offset = _offset_ms(now_ms / 1000)

    local_now = now_ms + offset
    remainder = local_now % anchor
    next_anchor = local_now if remainder == 0 else local_now + (anchor - remainder)

    steps = (next_anchor - local_now) // period
    return next_anchor - steps * period - offset
