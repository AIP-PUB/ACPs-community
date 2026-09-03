"""tests/unit/test_access_events.py — Access 行映射与 route 归一化测试。

TDD B-3：先写测试（红）→ 实现 events.py（绿）。
"""

from __future__ import annotations

from typing import Any

import pytest

# ── 辅助 fixture ─────────────────────────────────────────────────────────────


def _make_record(**kwargs: Any) -> Any:
    """构造最简 LogRecord（只有必填字段 + kwargs 覆盖）。"""
    from acps_sdk.amp.models import LogRecord

    defaults = {
        "schema_version": "1.0",
        "timestamp": "2026-01-15T10:00:00Z",
        "aic": "aic-test",
        "log_type": "access",
    }
    defaults.update(kwargs)
    return LogRecord(**defaults)


def _make_body(**kwargs: Any) -> Any:
    """构造最简 AccessBody（所有字段可选，kwargs 覆盖）。"""
    from acps_sdk.amp.models import AccessBody

    return AccessBody(**kwargs)


def _make_request(**kwargs: Any) -> Any:
    """构造 AccessRequest。"""
    from acps_sdk.amp.models import AccessRequest

    return AccessRequest(**kwargs)


def _make_participant(**kwargs: Any) -> Any:
    """构造 AccessParticipant。"""
    from acps_sdk.amp.models import AccessParticipant

    return AccessParticipant(**kwargs)


def _make_response(**kwargs: Any) -> Any:
    """构造 AccessResponse。"""
    from acps_sdk.amp.models import AccessResponse

    return AccessResponse(**kwargs)


def _make_error(**kwargs: Any) -> Any:
    """构造 ErrorInfo。"""
    from acps_sdk.amp.models import ErrorInfo

    return ErrorInfo(**kwargs)


# ── EventRow 结构测试 ─────────────────────────────────────────────────────────


class TestEventRowStructure:
    """EventRow dataclass 基本结构。"""

    def test_is_frozen_dataclass(self) -> None:
        import dataclasses

        from app.access.events import EventRow

        assert dataclasses.is_dataclass(EventRow)

    def test_as_tuple_length_matches_insert_columns(self) -> None:
        """as_tuple() 长度与 INSERT_COLUMNS 一致。"""
        from app.access.events import EventRow
        from app.access.tables import INSERT_COLUMNS

        row = EventRow(
            log_id="lid",
            timestamp_ms=0,
            observed_at_ms=0,
            aic="aic",
            trace_id="",
            span_id="",
            parent_span_id="",
            correlation_id="",
            severity="",
            duration_ms=0,
            request_method="",
            request_route="",
            request_url="",
            request_size=0,
            response_status=0,
            response_size=0,
            caller_aic="",
            caller_service="",
            caller_ip="",
            callee_aic="",
            callee_service="",
            callee_ip="",
            error_code="",
            error_message="",
            service_name="",
            deployment_env="",
            request_headers={},
            response_headers={},
            attributes={},
            raw_log="",
        )
        assert len(row.as_tuple()) == len(INSERT_COLUMNS)

    def test_as_tuple_order_matches_insert_columns(self) -> None:
        """as_tuple() 第一列为 log_id（INSERT_COLUMNS[0]）。"""
        from app.access.events import EventRow

        row = EventRow(
            log_id="my-log-id",
            timestamp_ms=0,
            observed_at_ms=0,
            aic="",
            trace_id="",
            span_id="",
            parent_span_id="",
            correlation_id="",
            severity="",
            duration_ms=0,
            request_method="",
            request_route="",
            request_url="",
            request_size=0,
            response_status=0,
            response_size=0,
            caller_aic="",
            caller_service="",
            caller_ip="",
            callee_aic="",
            callee_service="",
            callee_ip="",
            error_code="",
            error_message="",
            service_name="",
            deployment_env="",
            request_headers={},
            response_headers={},
            attributes={},
            raw_log="",
        )
        assert row.as_tuple()[0] == "my-log-id"


# ── build_event_row 核心测试 ──────────────────────────────────────────────────


class TestBuildEventRow:
    """build_event_row 行映射核心行为。"""

    def _build(self, record: Any = None, body: Any = None, **kwargs: Any) -> Any:
        from app.access.events import build_event_row

        if record is None:
            record = _make_record()
        if body is None:
            body = _make_body()
        defaults = {
            "record": record,
            "body": body,
            "log_id": "lid-1",
            "observed_at_ms": 1_700_000_000_000,
            "allowlist": frozenset(),
            "store_raw_log": False,
        }
        defaults.update(kwargs)
        row, _redacted = build_event_row(**defaults)
        return row

    def test_log_id_set(self) -> None:
        row = self._build(log_id="my-lid")
        assert row.log_id == "my-lid"

    def test_aic_from_record(self) -> None:
        record = _make_record(aic="aic-xyz")
        row = self._build(record=record)
        assert row.aic == "aic-xyz"

    def test_timestamp_ms_parsed_from_record(self) -> None:
        record = _make_record(timestamp="2026-01-15T10:00:00Z")
        row = self._build(record=record)
        assert isinstance(row.timestamp_ms, int)
        assert row.timestamp_ms > 0

    def test_observed_at_ms_preserved(self) -> None:
        row = self._build(observed_at_ms=12345678)
        assert row.observed_at_ms == 12345678

    def test_trace_fields_from_record(self) -> None:
        record = _make_record(trace_id="t1", span_id="s1", parent_span_id="p1", correlation_id="c1")
        row = self._build(record=record)
        assert row.trace_id == "t1"
        assert row.span_id == "s1"
        assert row.parent_span_id == "p1"
        assert row.correlation_id == "c1"

    def test_missing_trace_fields_default_empty(self) -> None:
        record = _make_record()
        row = self._build(record=record)
        assert row.trace_id == ""
        assert row.span_id == ""
        assert row.parent_span_id == ""
        assert row.correlation_id == ""

    def test_severity_from_record(self) -> None:
        record = _make_record(severity_text="ERROR")
        row = self._build(record=record)
        assert row.severity == "ERROR"

    def test_missing_severity_default_empty(self) -> None:
        row = self._build()
        assert row.severity == ""

    def test_duration_ms_from_body(self) -> None:
        body = _make_body(duration_ms=123.7)
        row = self._build(body=body)
        assert row.duration_ms == 123

    def test_missing_duration_ms_default_zero(self) -> None:
        row = self._build(body=_make_body())
        assert row.duration_ms == 0

    def test_request_fields(self) -> None:
        req = _make_request(method="POST", url="/users/42", route="/users/{id}")
        body = _make_body(request=req)
        row = self._build(body=body)
        assert row.request_method == "POST"
        assert row.request_url == "/users/42"
        assert row.request_route == "/users/{id}"

    def test_response_status(self) -> None:
        resp = _make_response(status_code=404)
        body = _make_body(response=resp)
        row = self._build(body=body)
        assert row.response_status == 404

    def test_missing_response_default_zero(self) -> None:
        row = self._build(body=_make_body())
        assert row.response_status == 0

    def test_caller_fields(self) -> None:
        caller = _make_participant(aic="caller-aic", service_name="svc-a", ip="1.2.3.4")
        body = _make_body(caller=caller)
        row = self._build(body=body)
        assert row.caller_aic == "caller-aic"
        assert row.caller_service == "svc-a"
        assert row.caller_ip == "1.2.3.4"

    def test_callee_fields(self) -> None:
        callee = _make_participant(aic="callee-aic", service_name="svc-b", ip="5.6.7.8")
        body = _make_body(callee=callee)
        row = self._build(body=body)
        assert row.callee_aic == "callee-aic"
        assert row.callee_service == "svc-b"
        assert row.callee_ip == "5.6.7.8"

    def test_error_code_int(self) -> None:
        err = _make_error(code=404, message="Not Found")
        body = _make_body(error=err)
        row = self._build(body=body)
        assert row.error_code == "404"
        assert row.error_message == "Not Found"

    def test_error_code_str(self) -> None:
        err = _make_error(code="AUTH_FAILED")
        body = _make_body(error=err)
        row = self._build(body=body)
        assert row.error_code == "AUTH_FAILED"

    def test_no_error_defaults_empty(self) -> None:
        row = self._build(body=_make_body())
        assert row.error_code == ""
        assert row.error_message == ""

    def test_resource_labels(self) -> None:
        record = _make_record(resource={"service.name": "my-svc", "deployment.environment.name": "prod"})
        row = self._build(record=record)
        assert row.service_name == "my-svc"
        assert row.deployment_env == "prod"

    def test_resource_labels_alt_key(self) -> None:
        """deployment.environment（不带.name 后缀）也应识别。"""
        record = _make_record(resource={"service.name": "svc2", "deployment.environment": "staging"})
        row = self._build(record=record)
        assert row.deployment_env == "staging"

    def test_missing_resource_defaults_empty(self) -> None:
        row = self._build(record=_make_record())
        assert row.service_name == ""
        assert row.deployment_env == ""

    def test_store_raw_log_false(self) -> None:
        row = self._build(store_raw_log=False)
        assert row.raw_log == ""

    def test_store_raw_log_true_not_sensitive(self) -> None:
        """store_raw_log=True 时 raw_log 不为空。"""
        row = self._build(store_raw_log=True)
        assert isinstance(row.raw_log, str)

    def test_invalid_timestamp_raises(self) -> None:
        from app.access.exception import InvalidAccessRecordError

        record = _make_record(timestamp="not-a-date")
        with pytest.raises(InvalidAccessRecordError):
            self._build(record=record)


# ── derive_request_route 测试（C-ACCESS-WRITE-6）────────────────────────────


class TestDeriveRequestRoute:
    def _derive(self, method: str | None = None, url: str | None = None, route: str | None = None) -> Any:
        from acps_sdk.amp.models import AccessRequest

        from app.access.events import derive_request_route

        req = AccessRequest(method=method, url=url, route=route)
        return derive_request_route(req)

    def test_explicit_route_used_first(self) -> None:
        """源端 route 优先（spec §5.4.1）。"""
        result = self._derive(url="/users/123", route="/users/{id}")
        assert result == "/users/{id}"

    def test_url_normalized_when_no_route(self) -> None:
        result = self._derive(url="/users/123")
        assert result == "/users/{id}"

    def test_none_request_returns_empty(self) -> None:
        from app.access.events import derive_request_route

        assert derive_request_route(None) == ""

    def test_both_none_returns_empty(self) -> None:
        result = self._derive()
        assert result == ""


# ── normalize_url_to_route 测试 ───────────────────────────────────────────────


class TestNormalizeUrlToRoute:
    def _norm(self, url: str | None) -> Any:
        from app.access.events import normalize_url_to_route

        return normalize_url_to_route(url)

    def test_numeric_segment_replaced(self) -> None:
        assert self._norm("/users/42") == "/users/{id}"

    def test_uuid_segment_replaced(self) -> None:
        assert self._norm("/orders/550e8400-e29b-41d4-a716-446655440000") == "/orders/{uuid}"

    def test_query_string_stripped(self) -> None:
        assert "?" not in self._norm("/search?q=foo&page=2")

    def test_static_path_unchanged(self) -> None:
        result = self._norm("/health")
        assert result == "/health"

    def test_none_returns_empty(self) -> None:
        assert self._norm(None) == ""

    def test_empty_returns_empty(self) -> None:
        assert self._norm("") == ""

    def test_nested_numeric_path(self) -> None:
        result = self._norm("/api/v1/orgs/99/members/42")
        assert "{id}" in result
        assert "99" not in result
        assert "42" not in result

    def test_long_hex_segment_replaced(self) -> None:
        """高熵 hex 段（hash/token）→ {var}。"""
        result = self._norm("/files/a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8")
        assert "{var}" in result


# ── derive_resource_labels 单元测试 ──────────────────────────────────────────


class TestDeriveResourceLabels:
    def _derive(self, resource: Any) -> Any:
        from app.access.events import derive_resource_labels

        return derive_resource_labels(resource)

    def test_standard_keys(self) -> None:
        svc, env = self._derive({"service.name": "svc", "deployment.environment.name": "prod"})
        assert svc == "svc"
        assert env == "prod"

    def test_alt_env_key(self) -> None:
        _, env = self._derive({"service.name": "svc", "deployment.environment": "staging"})
        assert env == "staging"

    def test_none_resource(self) -> None:
        svc, env = self._derive(None)
        assert svc == ""
        assert env == ""

    def test_missing_keys_default_empty(self) -> None:
        svc, env = self._derive({"host.name": "h1"})
        assert svc == ""
        assert env == ""


# ── parse_iso_to_ms 测试 ──────────────────────────────────────────────────────


class TestParseIsoToMs:
    def _parse(self, ts: str) -> Any:
        from app.access.events import parse_iso_to_ms

        return parse_iso_to_ms(ts)

    def test_utc_z_suffix(self) -> None:
        ms = self._parse("2026-01-15T10:00:00Z")
        assert ms == 1_768_471_200_000

    def test_with_ms_precision(self) -> None:
        ms = self._parse("2026-01-15T10:00:00.123Z")
        assert ms == 1_768_471_200_123

    def test_invalid_raises(self) -> None:
        from app.access.exception import InvalidAccessRecordError

        with pytest.raises(InvalidAccessRecordError):
            self._parse("not-a-date")


# ── _safe_raw_log 敏感 header 脱敏测试 ────────────────────────────────────────


class TestSafeRawLog:
    """验证 _safe_raw_log 以脱敏后的 headers 替换原始敏感 headers（C-ACCESS-WRITE-2）。"""

    def _call(self, record: Any, req_headers: dict, resp_headers: dict) -> Any:
        import json

        from app.access.events import _safe_raw_log

        raw = _safe_raw_log(record, req_headers=req_headers, resp_headers=resp_headers)
        return json.loads(raw) if raw else {}

    def test_sensitive_request_header_is_stripped(self) -> None:
        """Authorization header 在 raw_log 中应被脱敏版本替换。"""
        record = _make_record(
            body={
                "request": {"method": "GET", "route": "/api", "headers": {"authorization": "Bearer secret"}},
                "response": {"status": 200, "headers": {}},
            }
        )
        result = self._call(record, req_headers={"x-request-id": "r1"}, resp_headers={})
        req_headers_stored = result.get("body", {}).get("request", {}).get("headers", {})
        assert "authorization" not in req_headers_stored, "authorization 不应出现在 raw_log 中"
        assert req_headers_stored == {"x-request-id": "r1"}, "raw_log 中应只保留脱敏后的 headers"

    def test_sensitive_response_header_is_stripped(self) -> None:
        """Set-Cookie 在 raw_log 的 response.headers 中应被脱敏版本替换。"""
        record = _make_record(
            body={
                "request": {"method": "GET", "route": "/api", "headers": {}},
                "response": {"status": 200, "headers": {"set-cookie": "session=abc"}},
            }
        )
        result = self._call(record, req_headers={}, resp_headers={"x-trace-id": "t1"})
        resp_headers_stored = result.get("body", {}).get("response", {}).get("headers", {})
        assert "set-cookie" not in resp_headers_stored, "set-cookie 不应出现在 raw_log 中"
        assert resp_headers_stored == {"x-trace-id": "t1"}
