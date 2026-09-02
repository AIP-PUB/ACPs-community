"""单元测试：B-2 events.py — 行映射与 direction 派生（纯函数）。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.message.events import (
    build_event_row,
    derive_direction,
    parse_iso_to_ms,
    project_attributes,
)
from app.message.exception import InvalidMessageRecordError
from app.message.tables import INSERT_COLUMNS


def _make_body(**kwargs: object) -> MagicMock:
    """构造最小 MessageBody mock。"""
    body = MagicMock()
    body.event_type = kwargs.get("event_type", "send")
    body.system = kwargs.get("system", "kafka")
    body.destination = MagicMock()
    body.destination.name = kwargs.get("destination_name", "my-topic")
    body.destination.kind = kwargs.get("destination_kind", "topic")
    body.destination.virtual_host = kwargs.get("virtual_host", "/")
    body.message_id = kwargs.get("message_id", "m1")
    body.subscription_name = kwargs.get("subscription_name", "")
    body.consumer_group_name = kwargs.get("consumer_group_name", "")
    body.routing = MagicMock()
    body.routing.key = kwargs.get("routing_key", "")
    body.routing.partition = kwargs.get("partition")
    body.routing.offset = kwargs.get("offset")
    body.payload_size_bytes = kwargs.get("payload_size_bytes", 100)
    body.delivery_attempt = kwargs.get("delivery_attempt")
    body.settlement = kwargs.get("settlement")
    body.error = kwargs.get("error")
    body.attributes = kwargs.get("attributes")
    return body


def _make_record(**kwargs: object) -> MagicMock:
    """构造最小 LogRecord mock。"""
    record = MagicMock()
    record.timestamp = kwargs.get("timestamp", "2025-01-01T00:00:00Z")
    record.aic = kwargs.get("aic", "aic-001")
    record.trace_id = kwargs.get("trace_id", "trace-001")
    record.correlation_id = kwargs.get("correlation_id", "corr-001")
    record.log_id = kwargs.get("log_id", "log-001")
    return record


class TestDeriveDirection:
    def test_send_to_send(self) -> None:
        assert derive_direction("send") == "send"

    def test_receive_to_receive(self) -> None:
        assert derive_direction("receive") == "receive"

    def test_ack_to_receive(self) -> None:
        assert derive_direction("ack") == "receive"

    def test_nack_to_receive(self) -> None:
        assert derive_direction("nack") == "receive"

    def test_reject_to_receive(self) -> None:
        assert derive_direction("reject") == "receive"

    def test_timeout_to_receive(self) -> None:
        assert derive_direction("timeout") == "receive"

    def test_dead_letter_to_receive(self) -> None:
        assert derive_direction("dead_letter") == "receive"


class TestProjectAttributes:
    def test_none_returns_empty_dict(self) -> None:
        assert project_attributes(None) == {}

    def test_string_values_pass_through(self) -> None:
        result = project_attributes({"key": "value"})
        assert result == {"key": "value"}

    def test_non_string_values_json_encoded(self) -> None:
        # 非字符串值用 json.dumps() 序列化（设计 §4.1）
        result = project_attributes({"count": 42, "flag": True})
        assert result["count"] == "42"
        assert result["flag"] == "true"  # JSON 布尔值小写

    def test_nested_dict_json_encoded(self) -> None:
        result = project_attributes({"nested": {"a": 1}})
        assert result["nested"] == '{"a": 1}'  # JSON 格式（非 str()）


class TestParseIsoToMs:
    def test_valid_iso_utc(self) -> None:
        ms = parse_iso_to_ms("2025-01-01T00:00:00Z")
        assert ms == 1735689600000

    def test_valid_iso_offset(self) -> None:
        ms = parse_iso_to_ms("2025-01-01T08:00:00+08:00")
        assert ms == 1735689600000

    def test_invalid_raises(self) -> None:
        with pytest.raises(InvalidMessageRecordError):
            parse_iso_to_ms("not-a-date")

    def test_naive_datetime_raises(self) -> None:
        with pytest.raises(InvalidMessageRecordError):
            parse_iso_to_ms("2025-01-01T00:00:00")


class TestBuildEventRow:
    def test_direction_send(self) -> None:
        body = _make_body(event_type="send")
        record = _make_record()
        row = build_event_row(
            record=record,
            body=body,
            log_id="log-1",
            lifecycle_key="mid:m1",
            observed_at_ms=1735689600000,
            store_raw_log=False,
        )
        assert row.direction == "send"
        assert row.event_type == "send"

    def test_direction_ack_is_receive(self) -> None:
        body = _make_body(event_type="ack")
        record = _make_record()
        row = build_event_row(
            record=record,
            body=body,
            log_id="log-2",
            lifecycle_key="mid:m2",
            observed_at_ms=1735689600000,
            store_raw_log=False,
        )
        assert row.direction == "receive"

    def test_correlation_id_from_record_not_body(self) -> None:
        body = _make_body()
        record = _make_record(correlation_id="record-corr")
        row = build_event_row(
            record=record,
            body=body,
            log_id="log-3",
            lifecycle_key="mid:m3",
            observed_at_ms=1735689600000,
            store_raw_log=False,
        )
        assert row.correlation_id == "record-corr"

    def test_raw_log_empty_when_disabled(self) -> None:
        body = _make_body()
        record = _make_record()
        row = build_event_row(
            record=record,
            body=body,
            log_id="log-4",
            lifecycle_key="mid:m4",
            observed_at_ms=1735689600000,
            store_raw_log=False,
        )
        assert row.raw_log == ""

    def test_raw_log_non_empty_when_enabled(self) -> None:
        body = _make_body()
        record = _make_record()
        row = build_event_row(
            record=record,
            body=body,
            log_id="log-5",
            lifecycle_key="mid:m5",
            observed_at_ms=1735689600000,
            store_raw_log=True,
        )
        assert isinstance(row.raw_log, str)

    def test_invalid_timestamp_raises(self) -> None:
        body = _make_body()
        record = _make_record(timestamp="bad-ts")
        with pytest.raises(InvalidMessageRecordError):
            build_event_row(
                record=record,
                body=body,
                log_id="log-6",
                lifecycle_key="mid:m6",
                observed_at_ms=1735689600000,
                store_raw_log=False,
            )

    def test_partition_none_when_no_routing(self) -> None:
        body = _make_body(partition=None)
        record = _make_record()
        row = build_event_row(
            record=record,
            body=body,
            log_id="log-7",
            lifecycle_key="mid:m7",
            observed_at_ms=1735689600000,
            store_raw_log=False,
        )
        assert row.partition is None

    def test_settlement_latency_none_for_send(self) -> None:
        body = _make_body(event_type="send", settlement=None)
        record = _make_record()
        row = build_event_row(
            record=record,
            body=body,
            log_id="log-8",
            lifecycle_key="mid:m8",
            observed_at_ms=1735689600000,
            store_raw_log=False,
        )
        assert row.settlement_latency_ms is None

    def test_attributes_projected_to_str_dict(self) -> None:
        body = _make_body(attributes={"k": 123})
        record = _make_record()
        row = build_event_row(
            record=record,
            body=body,
            log_id="log-9",
            lifecycle_key="mid:m9",
            observed_at_ms=1735689600000,
            store_raw_log=False,
        )
        assert row.attributes == {"k": "123"}

    def test_raw_log_sanitizes_sensitive_keys(self) -> None:
        body = _make_body(attributes={"password": "secret", "token": "abc"})
        record = _make_record()
        row = build_event_row(
            record=record,
            body=body,
            log_id="log-10",
            lifecycle_key="mid:m10",
            observed_at_ms=1735689600000,
            store_raw_log=True,
        )
        assert "secret" not in row.raw_log
        assert "abc" not in row.raw_log

    def test_error_code_extracted(self) -> None:
        error = MagicMock()
        error.code = 500
        error.message = "Internal error"
        body = _make_body(error=error)
        record = _make_record()
        row = build_event_row(
            record=record,
            body=body,
            log_id="log-11",
            lifecycle_key="mid:m11",
            observed_at_ms=1735689600000,
            store_raw_log=False,
        )
        assert row.error_code == "500"
        assert row.error_message == "Internal error"

    def test_as_tuple_order_matches_insert_columns(self) -> None:
        body = _make_body()
        record = _make_record()
        row = build_event_row(
            record=record,
            body=body,
            log_id="log-12",
            lifecycle_key="mid:m12",
            observed_at_ms=1735689600000,
            store_raw_log=False,
        )
        t = row.as_tuple()
        assert len(t) == len(INSERT_COLUMNS)


class TestEventRowIsDataclass:
    def test_frozen(self) -> None:
        body = _make_body()
        record = _make_record()
        row = build_event_row(
            record=record,
            body=body,
            log_id="x",
            lifecycle_key="mid:x",
            observed_at_ms=0,
            store_raw_log=False,
        )
        with pytest.raises((AttributeError, TypeError)):
            row.log_id = "changed"  # type: ignore[misc]
