"""tests/support/factory.py — 测试数据工厂（设计 §9.1）。

提供构造合法 amp.metrics LogRecord 的工厂函数，供 Writer 单元测试和
集成测试使用。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def make_metrics_log_record(
    *,
    aic: str = "test-aic-001",
    log_id: str | None = "test-log-id-001",
    observed_timestamp: str | None = None,
    load: dict[str, Any] | None = None,
    windows: list[dict[str, Any]] | None = None,
    uptime_seconds: float = 0.0,
    resource: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造合法 amp.metrics LogRecord raw dict（Writer 解析入口）。

    Args:
        aic: Agent Identity Code。
        log_id: 可选 log_id（None 则由 Writer 计算 fallback hash）。
        observed_timestamp: ISO 8601 时间字符串（None 则不包含此字段，测试
            LogAppendTime 路径）。
        load: LoadMetrics dict（None 则使用默认值）。
        windows: WindowMetrics list（None 则使用空列表）。
        uptime_seconds: 正常运行时间（秒）。
        resource: OTEL resource dict（如 {"service.name": "demo-leader"}）。

    Returns:
        符合 LogRecord schema 的 raw dict（可直接 JSON 序列化投递 Kafka）。
    """
    default_load: dict[str, Any] = {
        "activeTasks": 1,
        "queuedTasks": 0,
        "maxActiveTasks": 10,
        "maxQueuedTasks": 100,
        "cpuUsage": 0.5,
        "memoryUsage": 0.3,
    }

    body: dict[str, Any] = {
        "uptimeSeconds": uptime_seconds,
        "loadMetrics": load if load is not None else default_load,
        "windowMetrics": windows if windows is not None else [],
    }

    # LogRecord 使用 snake_case 字段名（无 camelCase alias）
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "timestamp": datetime.now(UTC).isoformat(),
        "log_type": "metrics",
        "aic": aic,
        "body": body,
    }

    if log_id is not None:
        record["log_id"] = log_id

    if observed_timestamp is not None:
        record["observed_timestamp"] = observed_timestamp

    if resource is not None:
        record["resource"] = resource

    return record


def make_metrics_log_record_bytes(
    *,
    aic: str = "test-aic-001",
    **kwargs: Any,
) -> bytes:
    """构造合法 amp.metrics LogRecord JSON 字节串（Kafka 消息 value）。"""
    return json.dumps(make_metrics_log_record(aic=aic, **kwargs)).encode()


def make_window_metrics(
    *,
    window: str = "PT5M",
    success_rate: float = 99.5,
    request_per_second: float = 10.0,
    p50_latency_ms: float = 50.0,
    p95_latency_ms: float = 100.0,
    p99_latency_ms: float = 200.0,
    request_total: int = 3000,
) -> dict[str, Any]:
    """构造单个 WindowMetrics dict。"""
    return {
        "window": window,
        "successRate": success_rate,
        "requestPerSecond": request_per_second,
        "p50LatencyMs": p50_latency_ms,
        "p95LatencyMs": p95_latency_ms,
        "p99LatencyMs": p99_latency_ms,
        "requestTotal": request_total,
    }


def now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(UTC).isoformat()


METRICS_API_PREFIX = "/acps-amp-v1/metrics"


def snapshot_query_body(
    *,
    aics: list[str] | None = None,
    service_name: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """构造 snapshots/query 请求体（E2E / 集成测试共用）。"""
    conditions: list[dict[str, Any]] = []
    if aics:
        if len(aics) == 1:
            conditions.append({"field": "aic", "op": "eq", "value": aics[0]})
        else:
            conditions.append({"field": "aic", "op": "in", "value": aics})
    if service_name:
        conditions.append({"field": "service_name", "op": "eq", "value": service_name})

    body: dict[str, Any] = {"page": {"limit": limit}}
    if conditions:
        body["filter"] = {"conditions": conditions}
    return body


async def poll_snapshot(
    client: Any,
    *,
    aics: list[str] | None = None,
    service_name: str | None = None,
    uptime_seconds: float | None = None,
    timeout_s: float = 20.0,
    poll_interval_s: float = 0.5,
) -> dict[str, Any] | None:
    """轮询 snapshots/query 直到命中条件或超时（容忍 503 水位未就绪）。

    Returns:
        匹配的快照 dict；超时返回 None。
    """
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout_s
    body = snapshot_query_body(aics=aics, service_name=service_name, limit=20)
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.post(f"{METRICS_API_PREFIX}/snapshots/query", json=body)
        if resp.status_code != 200:
            await asyncio.sleep(poll_interval_s)
            continue
        for item in resp.json().get("items", []):
            if aics and item.get("aic") not in aics:
                continue
            if uptime_seconds is not None and item.get("uptimeSeconds") != uptime_seconds:
                continue
            return item
        await asyncio.sleep(poll_interval_s)
    return None


__all__ = [
    "ACCESS_API_PREFIX",
    "METRICS_API_PREFIX",
    "make_access_log_record",
    "make_access_log_record_bytes",
    "make_metrics_log_record",
    "make_metrics_log_record_bytes",
    "make_window_metrics",
    "now_iso",
    "poll_snapshot",
    "snapshot_query_body",
]


ACCESS_API_PREFIX = "/acps-amp-v1/access"


def make_access_log_record(
    *,
    aic: str = "aic-test-001",
    log_id: str | None = None,
    timestamp: str | None = None,
    observed_timestamp: str | None = None,
    trace_id: str = "",
    span_id: str = "",
    parent_span_id: str = "",
    correlation_id: str = "",
    method: str = "GET",
    route: str = "/health",
    url: str = "/health",
    response_status: int = 200,
    duration_ms: int = 50,
    error_code: str = "",
    caller_aic: str = "",
    callee_aic: str = "",
    caller_service: str = "",
    callee_service: str = "",
    service_name: str = "demo-svc",
) -> dict[str, Any]:
    """构造合法 amp.access LogRecord raw dict（供 Writer 集成测试和 E2E 使用）。"""
    import uuid

    cs_name = caller_service or service_name
    ce_name = callee_service or service_name

    record: dict[str, Any] = {
        "schema_version": "1.0",
        "log_id": log_id or str(uuid.uuid4()),
        "log_type": "access",
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "aic": aic,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "correlation_id": correlation_id,
        "body": {
            "durationMs": duration_ms,
            "request": {
                "method": method,
                "route": route,
                "url": url,
                "headers": {},
                "bodySizeBytes": 0,
            },
            "response": {
                "statusCode": response_status,
                "headers": {},
                "bodySizeBytes": 0,
            },
            "error": {"code": error_code, "message": ""} if error_code else None,
            "caller": {"aic": caller_aic, "serviceName": cs_name} if caller_aic else None,
            "callee": {"aic": callee_aic, "serviceName": ce_name} if callee_aic else None,
        },
        "resource": {
            "service.name": service_name,
            "deployment.environment": "testing",
        },
    }
    if observed_timestamp is not None:
        record["observed_timestamp"] = observed_timestamp
    return record


def make_access_log_record_bytes(**kwargs: Any) -> bytes:
    """返回 JSON 序列化的 access LogRecord（用于 Kafka 投递）。"""
    return json.dumps(make_access_log_record(**kwargs)).encode()


def make_message_log_record(
    *,
    aic: str = "svc-sender-001",
    log_id: str | None = None,
    timestamp: str | None = None,
    observed_timestamp: str | None = None,
    correlation_id: str = "",
    trace_id: str = "",
    span_id: str = "",
    parent_span_id: str = "",
    message_id: str | None = None,
    event_type: str = "send",
    system: str = "kafka",
    destination_name: str = "my-topic",
    destination_kind: str = "topic",
    virtual_host: str | None = None,
    delivery_attempt: int = 1,
    settlement_reason: str | None = None,
    settlement_latency_ms: int | None = None,
    attributes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """构造合法 amp.message LogRecord raw dict（供 Writer 集成测试和 E2E 使用）。"""
    import uuid

    destination: dict[str, Any] = {
        "name": destination_name,
        "kind": destination_kind,
    }
    if virtual_host is not None:
        destination["virtualHost"] = virtual_host

    body: dict[str, Any] = {
        "eventType": event_type,
        "system": system,
        "destination": destination,
        "messageId": message_id or str(uuid.uuid4()),
        "deliveryAttempt": delivery_attempt,
    }
    if settlement_reason is not None:
        settlement: dict[str, Any] = {"reason": settlement_reason}
        if settlement_latency_ms is not None:
            settlement["latencyMs"] = settlement_latency_ms
        body["settlement"] = settlement
    if attributes is not None:
        body["attributes"] = attributes

    record: dict[str, Any] = {
        "schema_version": "1.0",
        "log_id": log_id or str(uuid.uuid4()),
        "log_type": "message",
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "aic": aic,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "correlation_id": correlation_id,
        "body": body,
        "resource": {
            "service.name": "demo-svc",
            "deployment.environment": "testing",
        },
    }
    if observed_timestamp is not None:
        record["observed_timestamp"] = observed_timestamp
    return record


def make_message_log_record_bytes(**kwargs: Any) -> bytes:
    """返回 JSON 序列化的 message LogRecord（用于 Kafka 投递）。"""
    return json.dumps(make_message_log_record(**kwargs)).encode()


def make_system_log_record(
    *,
    aic: str = "aic-system-001",
    log_id: str | None = None,
    timestamp: str | None = None,
    observed_timestamp: str | None = None,
    severity_number: int = 9,
    severity_text: str | None = "INFO",
    correlation_id: str | None = None,
    message: str = "test system event",
    category: str | None = None,
    component: str | None = None,
    module_name: str | None = None,
    tags: dict[str, str] | None = None,
    resource: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造合法 amp.system LogRecord raw dict（供 Writer 集成测试和 E2E 使用）。"""
    import uuid

    body: dict[str, Any] = {"message": message}
    if category is not None:
        body["category"] = category
    if component is not None:
        body["component"] = component
    if module_name is not None:
        body["module"] = module_name
    if tags is not None:
        body["tags"] = tags

    record: dict[str, Any] = {
        "schema_version": "1.0",
        "log_id": log_id or str(uuid.uuid4()),
        "log_type": "system",
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "aic": aic,
        "severity_number": severity_number,
        "body": body,
        "resource": resource
        or {
            "service.name": "e2e-system-svc",
            "service.namespace": "acps-demo",
            "deployment.environment.name": "testing",
        },
    }
    if severity_text is not None:
        record["severity_text"] = severity_text
    if correlation_id is not None:
        record["correlation_id"] = correlation_id
    if observed_timestamp is not None:
        record["observed_timestamp"] = observed_timestamp
    return record


def make_system_log_record_bytes(**kwargs: Any) -> bytes:
    """返回 JSON 序列化的 system LogRecord（用于 Kafka 投递）。"""
    return json.dumps(make_system_log_record(**kwargs)).encode()
