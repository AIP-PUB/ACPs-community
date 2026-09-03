"""app/system/planner.py — 查询规划与协议护栏（纯函数）。设计 §5.3/§8 / spec §6.7.4。

护栏函数全部纯函数、零 I/O，在 service.query_events 中先于 OpenSearch 搜索执行。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from app.core.authz import principal_scope_filter
from app.system.exception import (
    InvalidTimeRangeError,
    OutOfRetentionError,
    SystemKeywordTooBroadError,
    UnsupportedFieldError,
)

if TYPE_CHECKING:
    from app.core.amp_api_schema import AMPPaginationRequest, AMPSortSpec, AMPTimeRange


@dataclass(frozen=True)
class ResolvedSort:
    """解析后的排序规范（API 字段 → 文档字段 + 方向）。"""

    field: str  # API 字段名（如 "timestamp"）
    doc_field: str  # 文档字段名（如 "timestamp"）
    order: str  # "asc" | "desc"


# 排序白名单（C-SYSTEM-QUERY-5 / 设计 §6.5）
_SORT_WHITELIST: Final[dict[str, str]] = {
    "timestamp": "timestamp",
    "severityNumber": "severity_number",
}


def require_time_range(tr: AMPTimeRange | None) -> tuple[int, int]:
    """spec §6.7.4：events/query 必带 timeRange；解析并返回 (from_ms, to_ms)。

    None / start >= end / 解析失败 → InvalidTimeRangeError(400)。
    """
    if tr is None:
        raise InvalidTimeRangeError("timeRange is required for system events/query.")
    try:
        from datetime import datetime

        start = datetime.fromisoformat(tr.start_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(tr.end_at.replace("Z", "+00:00"))
        from_ms = int(start.timestamp() * 1000)
        to_ms = int(end.timestamp() * 1000)
    except (ValueError, AttributeError) as exc:
        raise InvalidTimeRangeError(f"Cannot parse timeRange: {exc}") from exc
    if from_ms >= to_ms:
        raise InvalidTimeRangeError("timeRange.startAt must be before timeRange.endAt.")
    return from_ms, to_ms


def assert_within_retention(from_ms: int, *, archive_retention_days: int, now_ms: int | None = None) -> None:
    """C-SYSTEM-RETENTION-1：from_ms 早于有效保留窗口 → OutOfRetentionError(422)；不静默返回空。"""
    _now = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff_ms = _now - archive_retention_days * 86400 * 1000
    if from_ms < cutoff_ms:
        raise OutOfRetentionError(
            f"The requested time range starts before the {archive_retention_days}-day retention window."
        )


def validate_keyword(
    keyword: str | None,
    *,
    min_length: int,
    has_other_filter: bool,
    window_seconds: int,
    keyword_only_max_window_seconds: int,
) -> None:
    """C-SYSTEM-QUERY-4 / 设计 §5.3 第 2 条 / §8：过宽全文检索护栏。

    两种触发均映射 SystemKeywordTooBroadError（422，AMP_SYSTEM_KEYWORD_TOO_BROAD）：
    1. keyword 非空且 strip 长度 < min_length。
    2. keyword 非空 且 not has_other_filter 且 window_seconds > keyword_only_max_window_seconds。
    """
    if keyword is None:
        return
    stripped = keyword.strip()
    if len(stripped) < min_length:
        raise SystemKeywordTooBroadError(
            f"Keyword '{stripped}' is too short. Minimum length is {min_length} characters."
        )
    if not has_other_filter and window_seconds > keyword_only_max_window_seconds:
        raise SystemKeywordTooBroadError(
            f"Keyword-only query time window ({window_seconds}s) exceeds the maximum "
            f"allowed window ({keyword_only_max_window_seconds}s). Please add more filters."
        )


def validate_sort(sort: list[AMPSortSpec] | None) -> list[ResolvedSort]:
    """解析排序规范；None → 默认 timestamp desc（spec §6.7.4）。

    字段 ∉ _SORT_WHITELIST → UnsupportedFieldError(422)（C-SYSTEM-QUERY-5）。
    不在此追加 log_id tiebreaker——由 dsl.build_sort 负责（职责分离）。
    """
    if sort is None or len(sort) == 0:
        return [ResolvedSort("timestamp", "timestamp", "desc")]
    resolved: list[ResolvedSort] = []
    for spec in sort:
        doc_field = _SORT_WHITELIST.get(spec.field)
        if doc_field is None:
            raise UnsupportedFieldError(spec.field)
        resolved.append(ResolvedSort(spec.field, doc_field, spec.order))
    return resolved


def resolve_page_limit(page: AMPPaginationRequest | None) -> int:
    """返回请求的分页大小；None → 50（AMPPaginationRequest Field 已约束 1..500）。"""
    if page is None:
        return 50
    return page.limit


def inject_scope_filter(*, principal: Any = None) -> list[dict[str, Any]]:
    """把调用方租户/AIC 权限边界翻译为 OpenSearch filter 子句。"""
    scope = principal_scope_filter(principal)
    if scope.is_admin:
        return []

    clauses: list[dict[str, Any]] = []
    if scope.tenant_id:
        clauses.append({"term": {"tenant_id": scope.tenant_id}})
    if scope.allowed_aics:
        if len(scope.allowed_aics) == 1:
            clauses.append({"term": {"aic": scope.allowed_aics[0]}})
        else:
            clauses.append({"terms": {"aic": list(scope.allowed_aics)}})
    return clauses
