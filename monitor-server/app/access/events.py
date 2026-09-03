"""app/access/events.py — AccessBody/LogRecord → access_events 行映射（纯函数，C-ACCESS-WRITE-6）。

实现设计 §2.4、§3.1 第 2~5 步。
所有字段缺省落空串/0，保证与 ClickHouse 非空列类型兼容。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.access.exception import InvalidAccessRecordError
from app.access.redaction import redact_headers

if TYPE_CHECKING:
    from acps_sdk.amp.models import AccessBody, AccessRequest, LogRecord

# ── UUID / 数字 / 高熵段识别正则 ─────────────────────────────────────────────

_RE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_RE_NUMERIC = re.compile(r"^\d+$")
# 高熵段：纯十六进制 >= 16 字符（hash/token/fingerprint）
_RE_HIGH_ENTROPY_HEX = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)


# ── EventRow 数据类（写入行；与 INSERT_COLUMNS 一一对应）─────────────────────


@dataclass(frozen=True)
class EventRow:
    """access_events 写入行。store.insert_events 调用 .as_tuple() 落库。"""

    log_id: str
    timestamp_ms: int
    observed_at_ms: int
    aic: str
    trace_id: str
    span_id: str
    parent_span_id: str
    correlation_id: str
    severity: str
    duration_ms: int
    request_method: str
    request_route: str
    request_url: str
    request_size: int
    response_status: int
    response_size: int
    caller_aic: str
    caller_service: str
    caller_ip: str
    callee_aic: str
    callee_service: str
    callee_ip: str
    error_code: str
    error_message: str
    service_name: str
    deployment_env: str
    request_headers: dict[str, str]
    response_headers: dict[str, str]
    attributes: dict[str, str]
    raw_log: str

    def as_tuple(self) -> tuple:  # type: ignore[type-arg]
        """按 INSERT_COLUMNS 顺序返回元组，供 ClickHouse insert 使用。"""
        return (
            self.log_id,
            self.timestamp_ms,
            self.aic,
            self.trace_id,
            self.span_id,
            self.parent_span_id,
            self.correlation_id,
            self.severity,
            self.duration_ms,
            self.request_method,
            self.request_route,
            self.request_url,
            self.request_size,
            self.response_status,
            self.response_size,
            self.caller_aic,
            self.caller_service,
            self.caller_ip,
            self.callee_aic,
            self.callee_service,
            self.callee_ip,
            self.error_code,
            self.error_message,
            self.service_name,
            self.deployment_env,
            self.request_headers,
            self.response_headers,
            self.attributes,
            self.observed_at_ms,
            self.raw_log,
        )


# ── 公开函数 ──────────────────────────────────────────────────────────────────


def build_event_row(
    *,
    record: LogRecord,
    body: AccessBody,
    log_id: str,
    observed_at_ms: int,
    allowlist: frozenset[str],
    store_raw_log: bool,
) -> tuple[EventRow, int]:
    """将 LogRecord + AccessBody 映射为 access_events 写入行（设计 §3.1 第 2~5 步）。

    返回 (EventRow, redacted_count)，redacted_count 为被脱敏剔除的 header 总数，
    供 Writer 累计到 amp_access_writer_redacted_headers_total 指标。

    1. timestamp_ms = parse_iso_to_ms(record.timestamp)（失败 → InvalidAccessRecordError）
    2. 展开 request/response/caller/callee/error；缺省字段落空串/0
    3. request_route = derive_request_route(body.request)
    4. request/response headers 经 redaction.redact_headers 分列写入
    5. service_name / deployment_env = derive_resource_labels(record.resource)
    6. error_code = str(body.error.code) if body.error and code 非 None else ''
    7. severity = record.severity_text or ''
    8. raw_log = _safe_raw_log(record) if store_raw_log else ''
    """
    timestamp_ms = parse_iso_to_ms(record.timestamp)

    req = body.request
    resp = body.response
    caller = body.caller
    callee = body.callee
    error = body.error

    req_hdrs_raw = req.headers if req and req.headers else None
    resp_hdrs_raw = resp.headers if resp and resp.headers else None
    req_headers, req_dropped = redact_headers(req_hdrs_raw, allowlist)
    resp_headers, resp_dropped = redact_headers(resp_hdrs_raw, allowlist)
    total_redacted = req_dropped + resp_dropped

    svc_name, deploy_env = derive_resource_labels(record.resource)

    return (
        EventRow(
            log_id=log_id,
            timestamp_ms=timestamp_ms,
            observed_at_ms=observed_at_ms,
            aic=record.aic,
            trace_id=record.trace_id or "",
            span_id=record.span_id or "",
            parent_span_id=record.parent_span_id or "",
            correlation_id=record.correlation_id or "",
            severity=record.severity_text or "",
            duration_ms=int(body.duration_ms) if body.duration_ms is not None else 0,
            request_method=(req.method or "") if req else "",
            request_route=derive_request_route(req),
            request_url=(req.url or "") if req else "",
            request_size=(req.body_size_bytes or 0) if req else 0,
            response_status=(resp.status_code or 0) if resp else 0,
            response_size=(resp.body_size_bytes or 0) if resp else 0,
            caller_aic=(caller.aic or "") if caller else "",
            caller_service=(caller.service_name or "") if caller else "",
            caller_ip=(caller.ip or "") if caller else "",
            callee_aic=(callee.aic or "") if callee else "",
            callee_service=(callee.service_name or "") if callee else "",
            callee_ip=(callee.ip or "") if callee else "",
            error_code=str(error.code) if error and error.code is not None else "",
            error_message=(error.message or "") if error else "",
            service_name=svc_name,
            deployment_env=deploy_env,
            request_headers=req_headers,
            response_headers=resp_headers,
            attributes={k: str(v) for k, v in (record.attributes or {}).items()},
            raw_log=_safe_raw_log(record, req_headers=req_headers, resp_headers=resp_headers) if store_raw_log else "",
        ),
        total_redacted,
    )


def derive_request_route(request: AccessRequest | None) -> str:
    """从 AccessRequest 中提取稳定路由模板（C-ACCESS-WRITE-6）。

    优先使用 request.route（源端模板，spec §5.4.1）；
    缺省时用 normalize_url_to_route(request.url)；
    都无则返回 ''。
    """
    if request is None:
        return ""
    if request.route:
        return request.route
    return normalize_url_to_route(request.url)


def normalize_url_to_route(url: str | None) -> str:
    """将高基数 URL 归一化为有限 endpoint 维度（C-ACCESS-WRITE-6）。

    规则（优先级从高到低）：
      - 去 query string（截断 '?' 之后的部分）
      - UUID 段 → {uuid}
      - 纯数字段 → {id}
      - 高熵十六进制段（>= 16 chars）→ {var}

    目的：把高基数 url 收敛为聚合维度；框架路由表不可用时退而求其次。
    """
    if not url:
        return ""
    path = url.split("?")[0]
    segments = path.split("/")
    normalized: list[str] = []
    for seg in segments:
        if not seg:
            normalized.append(seg)
        elif _RE_UUID.match(seg):
            normalized.append("{uuid}")
        elif _RE_NUMERIC.match(seg):
            normalized.append("{id}")
        elif _RE_HIGH_ENTROPY_HEX.match(seg):
            normalized.append("{var}")
        else:
            normalized.append(seg)
    return "/".join(normalized)


def derive_resource_labels(resource: dict | None) -> tuple[str, str]:  # type: ignore[type-arg]
    """从 LogRecord.resource 提取低基数标签（设计 §3.1 第 3 步）。

    返回 (service_name, deployment_env)；缺省为空串。
    高基数字段（host/容器/实例 id）不提取，与 LowCardinality 列约束一致。
    """
    if not resource:
        return "", ""
    svc = str(resource.get("service.name") or "")
    env = str(resource.get("deployment.environment.name") or resource.get("deployment.environment") or "")
    return svc, env


def parse_iso_to_ms(ts: str) -> int:
    """解析 ISO 8601 时间戳为 epoch 毫秒（aware UTC）。

    失败时抛出 InvalidAccessRecordError（→ DLQ，重试不会改变格式错误）。
    """
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError) as exc:
        raise InvalidAccessRecordError(f"Invalid timestamp: {ts!r}") from exc


# ── 私有辅助 ──────────────────────────────────────────────────────────────────


def _safe_raw_log(
    record: LogRecord,
    *,
    req_headers: dict[str, str],
    resp_headers: dict[str, str],
) -> str:
    """将 record 序列化为 raw_log，使用已脱敏的 headers 替换原始敏感 headers（C-ACCESS-WRITE-2）。

    直接序列化原始 record 会暴露 authorization/cookie 等敏感请求头；
    此函数将 request.headers 和 response.headers 替换为经 redact_headers 处理的版本，
    再行序列化，确保 ClickHouse raw_log 列不含敏感字段。
    """
    import json

    try:
        data = record.model_dump()
        body = data.get("body")
        if isinstance(body, dict):
            req_data = body.get("request")
            if isinstance(req_data, dict):
                req_data["headers"] = req_headers
            resp_data = body.get("response")
            if isinstance(resp_data, dict):
                resp_data["headers"] = resp_headers
        return json.dumps(data, default=str)
    except Exception:
        return ""
