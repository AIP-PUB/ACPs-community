"""单元测试：B-4 filters.py — 过滤器编译与字段白名单。"""

from __future__ import annotations

import pytest

from app.core.amp_api_schema import AMPFilter, AMPFilterCondition
from app.message.exception import UnsupportedFieldError, UnsupportedOperatorError
from app.message.filters import (
    DESTINATION_FILTER_FIELDS,
    EVENT_FILTER_FIELDS,
    LIFECYCLE_FILTER_FIELDS,
    FieldSpec,
    WhereClause,
    compile_filter,
    split_lifecycle_where,
    validate_sort,
)


def _filter(*conditions: dict) -> AMPFilter:
    conds = [AMPFilterCondition(**c) for c in conditions]
    return AMPFilter(conditions=conds, logic="and")


class TestFieldSpecStructure:
    def test_field_spec_frozen(self) -> None:
        spec = FieldSpec(column="system", ch_type="String", apis=frozenset({"events"}))
        with pytest.raises((AttributeError, TypeError)):
            spec.column = "x"  # type: ignore[misc]


class TestEventFilterFields:
    def test_system_in_events_fields(self) -> None:
        assert "system" in EVENT_FILTER_FIELDS

    def test_destination_name_in_events(self) -> None:
        assert "destination.name" in EVENT_FILTER_FIELDS

    def test_event_type_in_events(self) -> None:
        assert "eventType" in EVENT_FILTER_FIELDS

    def test_direction_in_events(self) -> None:
        assert "direction" in EVENT_FILTER_FIELDS

    def test_aic_in_events(self) -> None:
        assert "aic" in EVENT_FILTER_FIELDS

    def test_trace_id_in_events(self) -> None:
        assert "traceId" in EVENT_FILTER_FIELDS

    def test_correlation_id_in_events(self) -> None:
        assert "correlationId" in EVENT_FILTER_FIELDS

    def test_message_id_in_events(self) -> None:
        assert "messageId" in EVENT_FILTER_FIELDS

    def test_lifecycle_key_in_events(self) -> None:
        assert "lifecycleKey" in EVENT_FILTER_FIELDS

    def test_routing_partition_in_events(self) -> None:
        assert "routing.partition" in EVENT_FILTER_FIELDS

    def test_routing_offset_in_events(self) -> None:
        assert "routing.offset" in EVENT_FILTER_FIELDS


class TestLifecycleFilterFields:
    def test_system_in_lifecycle(self) -> None:
        assert "system" in LIFECYCLE_FILTER_FIELDS

    def test_terminal_state_in_lifecycle(self) -> None:
        assert "terminalState" in LIFECYCLE_FILTER_FIELDS

    def test_dead_lettered_in_lifecycle(self) -> None:
        assert "deadLettered" in LIFECYCLE_FILTER_FIELDS

    def test_compacted_at_not_in_lifecycle(self) -> None:
        assert "compacted_at" not in LIFECYCLE_FILTER_FIELDS
        assert "compactedAt" not in LIFECYCLE_FILTER_FIELDS


class TestDestinationFilterFields:
    def test_system_in_destinations(self) -> None:
        assert "system" in DESTINATION_FILTER_FIELDS

    def test_destination_name_in_destinations(self) -> None:
        assert "destination.name" in DESTINATION_FILTER_FIELDS


class TestCompileFilter:
    def test_simple_eq_filter(self) -> None:
        f = _filter({"field": "system", "op": "eq", "value": "kafka"})
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert isinstance(wc, WhereClause)
        assert "system" in wc.sql or "f0" in wc.sql
        assert len(wc.params) >= 1

    def test_none_filter_returns_empty(self) -> None:
        wc = compile_filter(None, api="events", fields=EVENT_FILTER_FIELDS)
        assert wc.sql == ""
        assert wc.params == {}

    def test_unsupported_field_raises(self) -> None:
        f = _filter({"field": "compacted_at", "op": "eq", "value": "x"})
        with pytest.raises(UnsupportedFieldError):
            compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)

    def test_unsupported_operator_raises(self) -> None:
        f = _filter({"field": "system", "op": "xor", "value": "x"})
        with pytest.raises(UnsupportedOperatorError):
            compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)

    def test_no_sql_injection_string_concatenation(self) -> None:
        injection = "'; DROP TABLE message_events; --"
        f = _filter({"field": "system", "op": "eq", "value": injection})
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        # SQL 模板不应拼接用户输入
        assert "DROP TABLE" not in wc.sql
        # 值通过参数绑定传递（保留原始值，由 ClickHouse driver 安全转义）
        assert any(v == injection for v in wc.params.values())

    def test_in_operator_events(self) -> None:
        f = _filter({"field": "system", "op": "in", "value": ["kafka", "rabbitmq"]})
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert "IN" in wc.sql.upper() or "(" in wc.sql

    def test_ne_operator(self) -> None:
        f = _filter({"field": "direction", "op": "ne", "value": "send"})
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert "!=" in wc.sql

    def test_multiple_conditions_chained(self) -> None:
        f = _filter(
            {"field": "system", "op": "eq", "value": "kafka"},
            {"field": "eventType", "op": "eq", "value": "send"},
        )
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert "AND" in wc.sql.upper() or wc.sql.count("{") >= 2

    def test_field_not_applicable_to_api(self) -> None:
        f = _filter({"field": "system", "op": "eq", "value": "kafka"})
        with pytest.raises(UnsupportedFieldError):
            compile_filter(f, api="destinations", fields=LIFECYCLE_FILTER_FIELDS)

    def test_contains_operator(self) -> None:
        f = _filter({"field": "system", "op": "contains", "value": "kafka"})
        wc = compile_filter(f, api="events", fields=EVENT_FILTER_FIELDS)
        assert wc.sql != ""


class TestSplitLifecycleWhere:
    def test_system_goes_to_inner(self) -> None:
        f = _filter({"field": "system", "op": "eq", "value": "kafka"})
        inner, _outer = split_lifecycle_where(f, api="lifecycles")
        assert "system" in inner.sql or "f0" in inner.sql

    def test_terminal_state_goes_to_outer(self) -> None:
        f = _filter({"field": "terminalState", "op": "eq", "value": "ack"})
        _inner, outer = split_lifecycle_where(f, api="lifecycles")
        assert outer.sql != "" or "terminal_state" in outer.sql or "f0" in outer.sql

    def test_none_returns_empty_clauses(self) -> None:
        inner, outer = split_lifecycle_where(None, api="lifecycles")
        assert inner.sql == ""
        assert outer.sql == ""

    def test_destination_name_to_inner(self) -> None:
        f = _filter({"field": "destination.name", "op": "eq", "value": "my-topic"})
        inner, outer = split_lifecycle_where(f, api="lifecycles")
        assert inner.sql != ""
        assert outer.sql == "" or "destination_name" not in outer.sql


class TestValidateSort:
    def test_default_events_sort(self) -> None:
        result = validate_sort(None, api="events")
        assert len(result) >= 1
        assert any("timestamp" in r.field or "timestamp" in r.column_or_alias for r in result)

    def test_unsupported_sort_field_raises(self) -> None:
        from app.core.amp_api_schema import AMPSortSpec

        with pytest.raises(UnsupportedFieldError):
            validate_sort([AMPSortSpec(field="bogusField", order="asc")], api="events")
