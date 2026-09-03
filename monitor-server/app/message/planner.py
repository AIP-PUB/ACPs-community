"""app/message/planner.py — 查询规划与协议护栏（纯函数）。

实现设计 §3.4、§5.3、§6.6 / spec §6.5.4。
所有函数无副作用，仅转换参数为受控查询计划。
"""

from __future__ import annotations

import math
import re
from datetime import UTC
from typing import TYPE_CHECKING

from app.message.exception import (
    InvalidTimeRangeError,
    LifecycleKeyRequiredError,
    MessageDestinationRequiredError,
    MessageGroupByInvalidError,
    MessageStepInvalidError,
    OutOfRetentionError,
)

if TYPE_CHECKING:
    from app.core.amp_api_schema import AMPFilter, AMPPaginationRequest, AMPTimeRange

# ── 辅助 ─────────────────────────────────────────────────────────────────────

_VALID_GROUP_BY: frozenset[str] = frozenset(
    {"system", "destination.name", "destination.kind", "destination.virtualHost"}
)

_LIFECYCLE_HIGH_SELECTIVITY_FIELDS: frozenset[str] = frozenset(
    {"messageId", "lifecycleKey", "correlationId", "traceId"}
)

_ISO_DURATION_RE = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)


def _parse_iso_duration_to_seconds(duration: str) -> int:
    """把简单 ISO 8601 Duration 字符串解析为秒数（不支持 months/years/fractional）。"""
    m = _ISO_DURATION_RE.match(duration)
    if not m:
        raise MessageStepInvalidError(f"Invalid ISO 8601 duration: '{duration}'")
    years, months, weeks, days, hours, minutes, seconds_str = m.groups()
    if years or months:
        raise MessageStepInvalidError(f"Duration with years/months is not supported: '{duration}'")
    total = 0
    if weeks:
        total += int(weeks) * 7 * 86_400
    if days:
        total += int(days) * 86_400
    if hours:
        total += int(hours) * 3600
    if minutes:
        total += int(minutes) * 60
    if seconds_str:
        total += int(float(seconds_str))
    if total <= 0:
        raise MessageStepInvalidError(f"Duration must be positive: '{duration}'")
    return total


def _parse_iso_timestamp_to_ms(ts: str) -> int:
    """ISO 8601 时间戳 → UTC 毫秒整数。"""
    from datetime import datetime

    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


# ── 时间范围 ──────────────────────────────────────────────────────────────────


def require_time_range(tr: AMPTimeRange | None) -> tuple[int, int]:
    """除 lifecycles/{messageId} 外所有端点必填（spec §6.5.4）。

    返回 (from_ms, to_ms) 左闭右开，单位 ms。
    None / start >= end / 解析失败 → InvalidTimeRangeError(400)。
    """
    if tr is None:
        raise InvalidTimeRangeError("timeRange is required.")
    try:
        from_ms = _parse_iso_timestamp_to_ms(tr.start_at)
        to_ms = _parse_iso_timestamp_to_ms(tr.end_at)
    except Exception as exc:
        raise InvalidTimeRangeError(f"Could not parse timeRange: {exc}") from exc
    if from_ms >= to_ms:
        raise InvalidTimeRangeError("timeRange.startAt must be strictly before endAt.")
    return from_ms, to_ms


# ── 保留窗口校验 ──────────────────────────────────────────────────────────────


def assert_within_retention(from_ms: int, *, retention_days: int, now_ms: int) -> None:
    """C-MESSAGE-RETENTION-1：from_ms 超出保留期 → OutOfRetentionError(422)。

    调用方按端点传对应 retention_days：
      events → raw_retention_days；lifecycles/deadletters → lifecycle_retention_days；
      destinations → destination_state_retention_days；throughput → destination_stats_retention_days。
    """
    cutoff_ms = now_ms - retention_days * 86_400_000
    if from_ms < cutoff_ms:
        raise OutOfRetentionError(f"The requested time range extends beyond the {retention_days}-day retention window.")


# ── lifecycle 选择性校验 ───────────────────────────────────────────────────────


def require_lifecycle_selectivity(
    *,
    filter_: AMPFilter | None,
    time_range: AMPTimeRange | None,
) -> None:
    """C-MESSAGE-QUERY-1 / spec §6.5.4：lifecycles/query 须满足至少一种选择性条件。

    可接受：
    1. filter 含 messageId / lifecycleKey / correlationId / traceId 之一；
    2. filter 含 system + destination.name 且 time_range 非空。
    """
    if filter_ is None or not filter_.conditions:
        raise LifecycleKeyRequiredError()

    fields_in_filter = {cond.field for cond in filter_.conditions}

    if _LIFECYCLE_HIGH_SELECTIVITY_FIELDS & fields_in_filter:
        return

    if "system" in fields_in_filter and "destination.name" in fields_in_filter and time_range is not None:
        return

    raise LifecycleKeyRequiredError()


# ── destinations/query groupBy 白名单 ─────────────────────────────────────────


def validate_destination_group_by(group_by: list[str] | None) -> list[str]:
    """子集 ⊆ {system, destination.name, destination.kind, destination.virtualHost}。

    非法 → MessageGroupByInvalidError(422)；None / [] → []（逐目的地不聚合）。
    """
    if not group_by:
        return []
    for field in group_by:
        if field not in _VALID_GROUP_BY:
            raise MessageGroupByInvalidError(field)
    return list(group_by)


# ── throughput 必要参数校验 ───────────────────────────────────────────────────


def require_throughput_destination(
    *,
    system: str | None,
    destination_name: str | None,
) -> tuple[str, str]:
    """destinations/throughput 必须带 system + destinationName。

    缺失 → MessageDestinationRequiredError(422)。
    """
    if not system or not destination_name:
        raise MessageDestinationRequiredError()
    return system, destination_name


# ── step 解析与对齐 ────────────────────────────────────────────────────────────

_BUCKET_SECONDS = 300  # 5 分钟基础桶


def parse_throughput_step(
    step: str | None,
    *,
    from_ms: int,
    to_ms: int,
) -> int:
    """ISO 8601 Duration → 秒；缺省按时间跨度自动选择；下限对齐 300s 整数倍。

    自动选择规则（§6.6）：
      > 1d → 1h (3600)；> 6h → 15m (900)；否则 5m (300)。
    最小 300s；非 300 整数倍 → 向上取整到 300 倍数。
    """
    if step is None:
        span_ms = to_ms - from_ms
        if span_ms > 86_400_000:
            raw = 3600
        elif span_ms > 6 * 3600_000:
            raw = 900
        else:
            raw = 300
    else:
        raw = _parse_iso_duration_to_seconds(step)

    if raw < _BUCKET_SECONDS:
        return _BUCKET_SECONDS
    if raw % _BUCKET_SECONDS != 0:
        return math.ceil(raw / _BUCKET_SECONDS) * _BUCKET_SECONDS
    return raw


# ── compactor rebuild_from ────────────────────────────────────────────────────


def compute_rebuild_from(*, last_watermark_ms: int | None, overlap_seconds: int) -> int:
    """设计 §2.4 / C-MESSAGE-MODEL-7：rebuild_from = watermark − overlap*1000（ms）。

    首轮 last_watermark_ms=None → 0（全量回算）。结果下限为 0。
    """
    if last_watermark_ms is None:
        return 0
    result = last_watermark_ms - overlap_seconds * 1000
    return max(0, result)


# ── 页大小与 deadletter 上限 ──────────────────────────────────────────────────


def resolve_page_limit(page: AMPPaginationRequest | None) -> int:
    """limit = page.limit or 50（AMPPaginationRequest Field 约束 1..500）。"""
    if page is None or page.limit is None:
        return 50
    return page.limit


def clamp_deadletter_n(requested_limit: int, *, hard_max: int) -> int:
    """min(requested_limit, hard_max)（C-MESSAGE-QUERY-3 deadletter_query_max_n 门控）。"""
    return min(requested_limit, hard_max)


# ── 桶对齐 ─────────────────────────────────────────────────────────────────────


def align_to_bucket(ts_ms: int, *, bucket_seconds: int) -> int:
    """时间戳向下对齐到桶边界（5 分钟桶：300s）。单位保持 ms。"""
    bucket_ms = bucket_seconds * 1000
    return (ts_ms // bucket_ms) * bucket_ms
