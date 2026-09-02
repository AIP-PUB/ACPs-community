"""tests/unit/test_access_filters.py — 过滤器编译与白名单测试。

TDD B-4：先写测试（红）→ 实现 filters.py（绿）。
"""

from __future__ import annotations

from typing import Any

import pytest


def _make_filter(conditions: list[Any] | None = None, groups: list[Any] | None = None, logic: str = "and") -> Any:
    from app.core.amp_api_schema import AMPFilter

    return AMPFilter(conditions=conditions or [], groups=groups or [], logic=logic)


def _make_condition(field: str, op: str, value: Any) -> Any:
    from app.core.amp_api_schema import AMPFilterCondition

    return AMPFilterCondition(field=field, op=op, value=value)


def _make_sort(field: str, order: str = "desc") -> Any:
    from app.core.amp_api_schema import AMPSortSpec

    return AMPSortSpec(field=field, order=order)


# ── WhereClause & FieldSpec 结构测试 ─────────────────────────────────────────


class TestWhereClauseStructure:
    def test_where_clause_has_sql_and_params(self) -> None:
        from app.access.filters import WhereClause

        wc = WhereClause(sql="AND aic = {f0:String}", params={"f0": "x"})
        assert wc.sql == "AND aic = {f0:String}"
        assert wc.params == {"f0": "x"}

    def test_field_spec_has_column_type_apis(self) -> None:
        from app.access.filters import FieldSpec

        spec = FieldSpec(column="aic", ch_type="String", apis=frozenset({"events", "operations"}))
        assert spec.column == "aic"
        assert spec.ch_type == "String"
        assert "events" in spec.apis


# ── EVENT_FILTER_FIELDS 白名单完整性测试 ──────────────────────────────────────


class TestEventFilterFields:
    def test_aic_in_all_apis(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS

        spec = EVENT_FILTER_FIELDS["aic"]
        assert "events" in spec.apis
        assert "operations" in spec.apis
        assert "traces" in spec.apis

    def test_trace_id_in_multiple_apis(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS

        spec = EVENT_FILTER_FIELDS["traceId"]
        assert "events" in spec.apis

    def test_span_id_only_events(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS

        spec = EVENT_FILTER_FIELDS["spanId"]
        assert "events" in spec.apis
        assert "operations" not in spec.apis

    def test_request_route_in_events(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS

        assert "request.route" in EVENT_FILTER_FIELDS

    def test_response_status_is_uint16(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS

        spec = EVENT_FILTER_FIELDS["response.statusCode"]
        assert spec.ch_type == "UInt16"
        assert spec.column == "response_status"

    def test_duration_ms_is_uint32(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS

        spec = EVENT_FILTER_FIELDS["durationMs"]
        assert spec.ch_type == "UInt32"


class TestTraceSpanFilterFields:
    def test_trace_id_present(self) -> None:
        from app.access.filters import TRACE_SPAN_FILTER_FIELDS

        assert "traceId" in TRACE_SPAN_FILTER_FIELDS

    def test_error_message_absent(self) -> None:
        """error.message 未在 access_trace_span 物化，traces API 不支持。"""
        from app.access.filters import TRACE_SPAN_FILTER_FIELDS

        assert "error.message" not in TRACE_SPAN_FILTER_FIELDS

    def test_caller_ip_absent(self) -> None:
        from app.access.filters import TRACE_SPAN_FILTER_FIELDS

        assert "caller.ip" not in TRACE_SPAN_FILTER_FIELDS


class TestTopologyFilterFields:
    def test_caller_aic_present(self) -> None:
        from app.access.filters import TOPOLOGY_FILTER_FIELDS

        assert "caller.aic" in TOPOLOGY_FILTER_FIELDS

    def test_callee_service_present(self) -> None:
        from app.access.filters import TOPOLOGY_FILTER_FIELDS

        assert "callee.serviceName" in TOPOLOGY_FILTER_FIELDS

    def test_trace_id_absent(self) -> None:
        """traceId 未在 access_topology_edge_5m，topology API 不支持。"""
        from app.access.filters import TOPOLOGY_FILTER_FIELDS

        assert "traceId" not in TOPOLOGY_FILTER_FIELDS


# ── compile_filter 基础行为 ───────────────────────────────────────────────────


class TestCompileFilterBasic:
    def test_none_filter_returns_empty_where(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS, compile_filter

        wc = compile_filter(None, api="events", fields=EVENT_FILTER_FIELDS)
        assert wc.sql == ""
        assert wc.params == {}

    def test_empty_filter_returns_empty_where(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS, compile_filter

        f = _make_filter()
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert wc.sql == ""
        assert wc.params == {}

    def test_single_eq_condition(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS, compile_filter

        f = _make_filter(conditions=[_make_condition("aic", "eq", "aic-1")])
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert "aic" in wc.sql
        assert "=" in wc.sql
        assert "aic-1" in wc.params.values()

    def test_params_use_positional_keys(self) -> None:
        """参数名为 fN（防注入：不拼用户 field 名）。"""
        from app.access.filters import EVENT_FILTER_FIELDS, compile_filter

        f = _make_filter(conditions=[_make_condition("aic", "eq", "x")])
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert any(k.startswith("f") for k in wc.params)

    def test_type_annotation_in_sql(self) -> None:
        """SQL 中包含 ClickHouse 类型注解 {:String}。"""
        from app.access.filters import EVENT_FILTER_FIELDS, compile_filter

        f = _make_filter(conditions=[_make_condition("aic", "eq", "x")])
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert ":String" in wc.sql

    def test_uint16_type_annotation(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS, compile_filter

        f = _make_filter(conditions=[_make_condition("response.statusCode", "gte", 200)])
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert ":UInt16" in wc.sql

    def test_multiple_conditions_all_in_sql(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS, compile_filter

        f = _make_filter(
            conditions=[
                _make_condition("aic", "eq", "a1"),
                _make_condition("response.statusCode", "gte", 500),
            ]
        )
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert "aic" in wc.sql
        assert "response_status" in wc.sql
        assert len(wc.params) == 2

    def test_ne_operator(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS, compile_filter

        f = _make_filter(conditions=[_make_condition("error.code", "ne", "")])
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert "!=" in wc.sql

    def test_contains_operator(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS, compile_filter

        f = _make_filter(conditions=[_make_condition("request.route", "contains", "/users")])
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert "position" in wc.sql.lower() or "like" in wc.sql.lower() or "/users" in str(wc.params)

    def test_in_operator_list_value(self) -> None:
        from app.access.filters import EVENT_FILTER_FIELDS, compile_filter

        f = _make_filter(conditions=[_make_condition("severity", "in", ["INFO", "WARN"])])
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert "IN" in wc.sql.upper()


# ── compile_filter 错误路径 ───────────────────────────────────────────────────


class TestCompileFilterErrors:
    def test_unsupported_field_raises(self) -> None:
        from app.access.exception import UnsupportedFieldError
        from app.access.filters import EVENT_FILTER_FIELDS, compile_filter

        f = _make_filter(conditions=[_make_condition("unknown_field", "eq", "x")])
        with pytest.raises(UnsupportedFieldError):
            compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)

    def test_field_not_in_api_raises(self) -> None:
        """spanId 仅适用 events，在 operations API 应报错。"""
        from app.access.exception import UnsupportedFieldError
        from app.access.filters import EVENT_FILTER_FIELDS, compile_filter

        f = _make_filter(conditions=[_make_condition("spanId", "eq", "s1")])
        with pytest.raises(UnsupportedFieldError):
            compile_filter(f, api="operations", fields=EVENT_FILTER_FIELDS)

    def test_trace_span_unsupported_field_raises(self) -> None:
        """error.message 在 traces API 应报错（未物化）。"""
        from app.access.exception import UnsupportedFieldError
        from app.access.filters import TRACE_SPAN_FILTER_FIELDS, compile_filter

        f = _make_filter(conditions=[_make_condition("error.message", "eq", "x")])
        with pytest.raises(UnsupportedFieldError):
            compile_filter(f, api="traces", fields=TRACE_SPAN_FILTER_FIELDS)


# ── validate_sort 测试 ────────────────────────────────────────────────────────


class TestValidateSort:
    def test_none_sort_returns_default(self) -> None:
        from app.access.filters import validate_sort

        result = validate_sort(None, api="events")
        assert isinstance(result, list)
        assert len(result) > 0

    def test_valid_sort_events(self) -> None:
        from app.access.filters import validate_sort

        result = validate_sort([_make_sort("timestamp", "desc")], api="events")
        assert result[0].field == "timestamp"
        assert result[0].order == "desc"

    def test_valid_sort_operations(self) -> None:
        from app.access.filters import validate_sort

        result = validate_sort([_make_sort("avgDurationMs", "desc")], api="operations")
        assert result[0].field == "avgDurationMs"

    def test_invalid_sort_field_raises(self) -> None:
        from app.access.exception import UnsupportedFieldError
        from app.access.filters import validate_sort

        with pytest.raises(UnsupportedFieldError):
            validate_sort([_make_sort("unknownField")], api="events")

    def test_sort_for_wrong_api_raises(self) -> None:
        """requestCount 是 operations 的排序字段，events API 不支持。"""
        from app.access.exception import UnsupportedFieldError
        from app.access.filters import validate_sort

        with pytest.raises(UnsupportedFieldError):
            validate_sort([_make_sort("requestCount")], api="events")

    def test_resolved_sort_has_column_or_alias(self) -> None:
        from app.access.filters import validate_sort

        result = validate_sort([_make_sort("durationMs", "asc")], api="events")
        assert result[0].column_or_alias


class TestResolvedSortStructure:
    def test_resolved_sort_is_frozen_dataclass(self) -> None:
        import dataclasses

        from app.access.filters import ResolvedSort

        assert dataclasses.is_dataclass(ResolvedSort)
