"""E2E System 测试共享请求体构造。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


def system_time_range_body(
    *,
    lookback_hours: int = 1,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    log_id: str | None = None,
    aic: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    sort: list[dict[str, str]] | None = None,
    filter_conditions: list[dict[str, Any]] | None = None,
    include_raw_log: bool | None = None,
) -> dict[str, Any]:
    """构造 POST /system/events/query 请求体。"""
    now = datetime.now(UTC)
    body: dict[str, Any] = {
        "timeRange": {
            "startAt": (start_at or (now - timedelta(hours=lookback_hours))).isoformat(),
            "endAt": (end_at or now).isoformat(),
        },
        "page": {"limit": limit},
    }
    conditions: list[dict[str, Any]] = list(filter_conditions or [])
    if log_id:
        conditions.append({"field": "logId", "op": "eq", "value": log_id})
    if aic:
        conditions.append({"field": "aic", "op": "eq", "value": aic})
    if conditions:
        body["filter"] = {"conditions": conditions, "logic": "and"}
    if keyword:
        body["keyword"] = keyword
    if cursor:
        body["page"]["cursor"] = cursor
    if sort:
        body["sort"] = sort
    if include_raw_log is not None:
        body["includeRawLog"] = include_raw_log
    return body
