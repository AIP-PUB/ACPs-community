"""app/access/planner.py — 查询规划（纯函数）。

实现设计 §3.3、§5.3，spec §6.4.4。把协议级约束转成受控执行计划：
时间范围校验、保留窗口判定、桶对齐、trace 分区外扩、limit 钳制、groupBy 校验。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from app.access.exception import (
    AttributionGroupByInvalidError,
    InvalidFilterError,
    InvalidTimeRangeError,
    OutOfRetentionError,
    TopologyGroupByInvalidError,
)

if TYPE_CHECKING:
    from app.core.amp_api_schema import AMPPaginationRequest, AMPTimeRange

_5MIN_MS: Final[int] = 5 * 60 * 1000
_VALID_BUCKET_SIZES: Final[dict[str, str]] = {
    "5m": "toStartOfFiveMinutes",
    "15m": "toStartOfFifteenMinutes",
    "1h": "toStartOfHour",
    "1d": "toStartOfDay",
}
_VALID_TOPOLOGY_GROUP_BY: Final[frozenset[str]] = frozenset({"aic", "service"})
_VALID_OPERATIONS_GROUP_BY: Final[frozenset[str]] = frozenset({"aic", "service", "endpoint"})
_VALID_ATTRIBUTION_GROUP_BY: Final[frozenset[str]] = frozenset({"errorCode", "statusCode", "endpoint"})


def require_time_range(tr: AMPTimeRange | None) -> tuple[int, int]:
    """时间范围必填校验（除 traces/{traceId} 外所有端点，C-ACCESS-QUERY-1）。

    None → raise InvalidTimeRangeError(400)；start >= end → 400。
    返回 (from_ms, to_ms)，左闭右开。
    """
    if tr is None:
        raise InvalidTimeRangeError("timeRange is required for this endpoint")
    from_ms = _parse_iso_ms(tr.start_at)
    to_ms = _parse_iso_ms(tr.end_at)
    if from_ms >= to_ms:
        raise InvalidTimeRangeError("timeRange.startAt must be before endAt")
    return from_ms, to_ms


def assert_within_retention(from_ms: int, *, retention_days: int, now_ms: int) -> None:
    """保留窗口断言（C-ACCESS-RETENTION-1）。

    from_ms 早于 (now - retention_days) → raise OutOfRetentionError(422)。
    不静默返回空，主动拒绝超出 TTL 的查询。
    """
    oldest_ms = now_ms - retention_days * 86_400_000
    if from_ms < oldest_ms:
        raise OutOfRetentionError(f"Query start is before retention window (retention={retention_days}d)")


def align_topology_buckets(from_ms: int, to_ms: int) -> tuple[int, int]:
    """5 分钟桶边界外扩取整（spec §6.4.4 / 设计 §6.5）。

    起点 floor 到 5min、终点 ceil 到 5min。
    """
    from_bucket = (from_ms // _5MIN_MS) * _5MIN_MS
    remainder = to_ms % _5MIN_MS
    to_bucket = to_ms if remainder == 0 else to_ms + (_5MIN_MS - remainder)
    return from_bucket, to_bucket


def trace_partition_expand_hours(*, configured: int) -> int:
    """traces/query 外层分区裁剪双向外扩量（设计 §6.3）。

    返回 max(1, configured)。
    """
    return max(1, configured)


def clamp_top_n(requested: int | None, *, default: int, hard_max: int) -> int:
    """TopN 钳制（slow_top_max_n / error_attribution_max_n）。

    min(requested or default, hard_max)；requested <= 0 时返回 default。
    """
    value = requested if requested and requested > 0 else default
    return min(value, hard_max)


def resolve_page_limit(page: AMPPaginationRequest | None) -> int:
    """解析分页 limit（spec §6.1.2 默认 50、上限 500 由 AMPPaginationRequest.Field 约束）。"""
    if page is None:
        return 50
    return page.limit


def validate_topology_group_by(group_by: str | None) -> str:
    """topology/query groupBy 校验（C-ACCESS-QUERY）。

    取值 ∈ {"aic","service"}；非法 → raise TopologyGroupByInvalidError(422)；缺省 "aic"。
    """
    if group_by is None:
        return "aic"
    if group_by not in _VALID_TOPOLOGY_GROUP_BY:
        raise TopologyGroupByInvalidError(
            f"topology groupBy must be one of {sorted(_VALID_TOPOLOGY_GROUP_BY)}, got {group_by!r}"
        )
    return group_by


def validate_operations_group_by(group_by: list[str] | None) -> list[str]:
    """operations/query groupBy 校验。

    子集 ⊆ {"aic","service","endpoint"}；非法 → raise InvalidFilterError(422)；缺省 []（整体聚合）。
    """
    if not group_by:
        return []
    invalid = [g for g in group_by if g not in _VALID_OPERATIONS_GROUP_BY]
    if invalid:
        raise InvalidFilterError(
            f"operations groupBy contains unsupported dimensions: {invalid}. "
            f"Allowed: {sorted(_VALID_OPERATIONS_GROUP_BY)}"
        )
    return list(group_by)


def validate_attribution_group_by(group_by: list[str] | None) -> list[str]:
    """errors/attribution groupBy 校验。

    子集 ⊆ {"errorCode","statusCode","endpoint"}；
    非法 → raise AttributionGroupByInvalidError(422 AMP_ATTRIBUTION_GROUPBY_INVALID)。
    """
    if not group_by:
        return ["errorCode"]
    invalid = [g for g in group_by if g not in _VALID_ATTRIBUTION_GROUP_BY]
    if invalid:
        raise AttributionGroupByInvalidError(
            f"errors/attribution groupBy contains unsupported dimensions: {invalid}. "
            f"Allowed: {sorted(_VALID_ATTRIBUTION_GROUP_BY)}"
        )
    return list(group_by)


def parse_bucket_size(bucket_size: str | None, *, collapse: bool | None) -> str | None:
    """operations bucketSize 解析（spec §6.4.4）。

    collapseBuckets=True 或 bucket_size=None → None（不分时间桶）。
    有效值 {"5m","15m","1h","1d"} → ClickHouse toStartOf* 函数名。
    非法值 → raise InvalidFilterError(400)。
    """
    if collapse:
        return None
    if bucket_size is None:
        return None
    func = _VALID_BUCKET_SIZES.get(bucket_size)
    if func is None:
        raise InvalidFilterError(f"bucketSize {bucket_size!r} is not valid. Allowed: {list(_VALID_BUCKET_SIZES)}")
    return func


# ── 私有辅助 ──────────────────────────────────────────────────────────────────


def _parse_iso_ms(ts: str) -> int:
    """解析 ISO 8601 时间戳为 epoch 毫秒（UTC aware）。"""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError) as exc:
        raise InvalidTimeRangeError(f"Invalid time value: {ts!r}") from exc
