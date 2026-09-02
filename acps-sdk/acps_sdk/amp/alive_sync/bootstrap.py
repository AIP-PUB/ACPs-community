"""AMP alive-sync 自举起始 offset 时间锚点计算（§7.5 第 8 条）。

SDK 只做纯时间计算；`offsetsForTimes` 的实际调用由 discovery-server 侧的 aiokafka 完成。
"""
from __future__ import annotations

from datetime import datetime, timezone


def seek_timestamp_ms(generated_at: str, lookback_seconds: int) -> int:
    """计算 Kafka `offsetsForTimes` 的入参时间戳（epoch ms）。

    将 snapshot generatedAt 解析为 epoch ms，减去 lookback_seconds * 1000，
    得到「snapshot 生成时刻前若干秒」的毫秒时间戳。结果不低于 0（避免传负值）。

    Args:
        generated_at: ISO 8601 UTC 时间戳字符串（如 "2026-06-13T01:20:00Z"）。
        lookback_seconds: 回看裕量，单位秒（§7.5 第 8 条，典型值 300）。

    Returns:
        epoch 毫秒整数，供 aiokafka offsetsForTimes 调用。

    Raises:
        ValueError: generated_at 格式无法解析为 ISO 8601 时间戳。
    """
    if generated_at.endswith("Z"):
        generated_at = generated_at[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(generated_at)
    except ValueError as exc:
        raise ValueError(f"generated_at 格式无效: {generated_at!r}") from exc

    dt_utc = dt.astimezone(timezone.utc)
    epoch_ms = int(dt_utc.timestamp() * 1000)
    result = epoch_ms - lookback_seconds * 1000
    return max(result, 0)


def next_lookback_seconds(
    current: int,
    *,
    factor: int = 2,
    max_seconds: int,
) -> int:
    """自举免误报：缺口疑似 offset 过晚时倍增回看窗口。

    每次调用将 current 乘以 factor，直至达到或超过 max_seconds 则钳制到 max_seconds。
    当返回值已等于 max_seconds 时，调用方应退而使用 Kafka earliest offset。

    Args:
        current: 当前回看秒数。
        factor: 倍增系数（默认 2）。
        max_seconds: 上限（单位秒，§7.5 第 8 条）。

    Returns:
        新的回看秒数（≤ max_seconds）。
    """
    return min(current * factor, max_seconds)
