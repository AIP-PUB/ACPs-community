"""tests/unit/system/test_normalizer.py — normalizer.py 单元测试（核心 TDD 靶点）。"""

from __future__ import annotations

from typing import Any

import pytest

from app.system.exception import InvalidSystemRecordError
from app.system.normalizer import (
    SEVERITY_UNSPECIFIED,
    build_document,
    build_search_text,
    derive_message,
    extract_structured_fields,
    normalize_tags,
    resolve_severity_number,
)


class TestDeriveMessage:
    """C-SYSTEM-WRITE-7: message 确定性规则，任何 body 形态都不缺失。"""

    def test_str_body_direct_take(self) -> None:
        assert derive_message("hello world", max_length=1000) == "hello world"

    def test_str_body_truncated(self) -> None:
        msg = derive_message("x" * 200, max_length=100)
        assert len(msg) == 100

    def test_dict_with_message_key(self) -> None:
        body = {"message": "dict message"}
        assert derive_message(body, max_length=1000) == "dict message"

    def test_dict_with_msg_key(self) -> None:
        """msg 作为备用键（message 不存在时）。"""
        body = {"msg": "msg value"}
        assert derive_message(body, max_length=1000) == "msg value"

    def test_dict_with_message_takes_priority_over_msg(self) -> None:
        body = {"message": "primary", "msg": "secondary"}
        assert derive_message(body, max_length=1000) == "primary"

    def test_dict_without_readable_message_gives_json_summary(self) -> None:
        """dict 无 message/msg → 确定性 JSON 摘要（sort_keys）。"""
        body = {"b": 2, "a": 1}
        result = derive_message(body, max_length=1000)
        # 结果是非空字符串（JSON 摘要）
        assert isinstance(result, str)
        assert len(result) > 0

    def test_dict_json_summary_uses_sort_keys(self) -> None:
        """同 dict 不同键顺序 → 相同 JSON 摘要（稳定性，设计 §3.1 步骤 3）。"""
        body1 = {"b": 2, "a": 1}
        body2 = {"a": 1, "b": 2}
        assert derive_message(body1, max_length=1000) == derive_message(body2, max_length=1000)

    def test_dict_json_summary_truncated(self) -> None:
        body = {f"key_{i}": f"value_{i}" for i in range(100)}
        result = derive_message(body, max_length=50)
        assert len(result) <= 50

    def test_int_body(self) -> None:
        assert derive_message(42, max_length=1000) == "42"

    def test_float_body(self) -> None:
        assert derive_message(3.14, max_length=1000) == "3.14"

    def test_bool_body(self) -> None:
        assert derive_message(True, max_length=1000) == "True"

    def test_list_body_returns_empty_string(self) -> None:
        """list body → 返回空字符串（事件仍落库，设计 §3.1 步骤 3）。"""
        result = derive_message([1, 2, 3], max_length=1000)
        assert result == ""

    def test_none_body_returns_empty_string(self) -> None:
        result = derive_message(None, max_length=1000)
        assert result == ""

    def test_message_never_missing_for_any_body(self) -> None:
        """C-SYSTEM-WRITE-7：任何形态 body 都不返回 None。"""
        bodies = ["str", {"key": "val"}, 42, 3.14, True, [1, 2], None, {}, ""]
        for body in bodies:
            result = derive_message(body, max_length=1000)
            assert result is not None, f"message is None for body: {body!r}"
            assert isinstance(result, str), f"message is not str for body: {body!r}"


class TestResolveSeverityNumber:
    """C-SYSTEM-WRITE-2: severity 顶层优先，缺省 UNSPECIFIED(0)。"""

    def _make_record(
        self,
        severity_number: int | None = None,
        severity_text: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        from acps_sdk.amp.models import LogRecord

        return LogRecord.model_validate(
            {
                "schema_version": "1.0",
                "log_type": "system",
                "log_id": "test-id",
                "timestamp": "2024-01-01T00:00:00Z",
                "aic": "test-aic",
                "severity_number": severity_number,
                "severity_text": severity_text,
                "body": body or {},
            }
        )

    def test_top_level_severity_number_taken_directly(self) -> None:
        """顶层 severity_number 非空直取。"""
        record = self._make_record(severity_number=9)
        assert resolve_severity_number(record) == 9

    def test_zero_severity_number_is_taken(self) -> None:
        """severity_number=0 也是合法值（UNSPECIFIED），直取。"""
        record = self._make_record(severity_number=0)
        assert resolve_severity_number(record) == 0

    def test_none_severity_number_defaults_to_unspecified(self) -> None:
        """None → SEVERITY_UNSPECIFIED(0)。"""
        record = self._make_record(severity_number=None)
        assert resolve_severity_number(record) == SEVERITY_UNSPECIFIED
        assert resolve_severity_number(record) == 0

    def test_body_custom_severity_field_not_used(self) -> None:
        """绝不从 body 自定义字段推导 severity（C-SYSTEM-WRITE-2）。"""
        record = self._make_record(severity_number=None, body={"level": "ERROR", "severity": 17})
        assert resolve_severity_number(record) == SEVERITY_UNSPECIFIED


class TestExtractStructuredFields:
    def test_dict_body_extracts_category(self) -> None:
        result = extract_structured_fields({"category": "auth", "message": "test"})
        assert result["category"] == "auth"

    def test_dict_body_extracts_component(self) -> None:
        result = extract_structured_fields({"component": "api-gateway"})
        assert result["component"] == "api-gateway"

    def test_dict_body_extracts_module(self) -> None:
        result = extract_structured_fields({"module": "user-service"})
        assert result["module"] == "user-service"

    def test_dict_body_missing_fields_return_none(self) -> None:
        result = extract_structured_fields({})
        assert result["category"] is None
        assert result["component"] is None
        assert result["module"] is None

    def test_non_dict_body_returns_all_none(self) -> None:
        """非 dict body（str/list/None）→ 全 None（设计 §3.1 步骤 4）。"""
        for body in ["string", [1, 2], None, 42]:
            result = extract_structured_fields(body)
            assert result["category"] is None, f"category not None for body={body!r}"
            assert result["component"] is None
            assert result["module"] is None

    def test_non_scalar_category_ignored(self) -> None:
        """category 为 dict/list 等非标量 → None（读可读标量键）。"""
        result = extract_structured_fields({"category": {"nested": "val"}})
        assert result["category"] is None


class TestNormalizeTags:
    """设计 §3.1 步骤 4 / §4.1：tags 规范化，超长截断但不丢事件。"""

    def test_dict_tags_extracted(self) -> None:
        body = {"tags": {"env": "prod", "version": "1.0"}}
        result = normalize_tags(body)
        assert result["env"] == "prod"
        assert result["version"] == "1.0"

    def test_values_coerced_to_string(self) -> None:
        body = {"tags": {"count": 42, "flag": True}}
        result = normalize_tags(body)
        assert result["count"] == "42"
        assert result["flag"] == "True"

    def test_value_truncated_to_max_term_bytes(self) -> None:
        """超 32KB 截断（Lucene term 上限），但事件不丢。"""
        long_val = "x" * 40000  # 超 32768
        body = {"tags": {"key": long_val}}
        result = normalize_tags(body, max_term_bytes=32768)
        assert "key" in result
        assert len(result["key"].encode()) <= 32768

    def test_no_tags_key_returns_empty_dict(self) -> None:
        result = normalize_tags({"other": "val"})
        assert result == {}

    def test_non_dict_body_returns_empty_dict(self) -> None:
        for body in ["string", None, 42, [1, 2]]:
            result = normalize_tags(body)
            assert result == {}, f"expected empty dict for body={body!r}"

    def test_tags_not_dict_returns_empty_dict(self) -> None:
        body = {"tags": "not-a-dict"}
        result = normalize_tags(body)
        assert result == {}

    def test_event_not_dropped_due_to_oversized_tag(self) -> None:
        """超长 tag 只截断，不丢整个事件（设计 §3.1）。"""
        body = {"tags": {"ok_key": "short", "long_key": "y" * 40000}}
        result = normalize_tags(body, max_term_bytes=32768)
        assert "ok_key" in result
        assert "long_key" in result


class TestBuildSearchText:
    """C-SYSTEM-WRITE-5：search_text 含 message + body 可读标量 + resource 标量。"""

    def test_includes_message(self) -> None:
        text = build_search_text(
            message="error occurred",
            body={},
            resource=None,
            max_length=10000,
        )
        assert "error occurred" in text

    def test_includes_resource_service_name(self) -> None:
        text = build_search_text(
            message="test",
            body={},
            resource={"service.name": "api-gateway"},
            max_length=10000,
        )
        assert "api-gateway" in text

    def test_includes_resource_host_name(self) -> None:
        text = build_search_text(
            message="test",
            body={},
            resource={"host.name": "prod-host-01"},
            max_length=10000,
        )
        assert "prod-host-01" in text

    def test_includes_body_scalar_values(self) -> None:
        text = build_search_text(
            message="test",
            body={"custom_field": "custom_value"},
            resource=None,
            max_length=10000,
        )
        assert "custom_value" in text

    def test_truncated_to_max_length(self) -> None:
        long_msg = "x " * 10000
        text = build_search_text(
            message=long_msg,
            body={},
            resource=None,
            max_length=100,
        )
        assert len(text) <= 100

    def test_none_resource_handled(self) -> None:
        text = build_search_text(
            message="hello",
            body={},
            resource=None,
            max_length=10000,
        )
        assert isinstance(text, str)

    def test_control_chars_cleaned(self) -> None:
        text = build_search_text(
            message="hello\x00world\x01",
            body={},
            resource=None,
            max_length=10000,
        )
        assert "\x00" not in text
        assert "\x01" not in text


_MISSING = object()


class TestBuildDocument:
    """build_document 编排测试。"""

    def _make_record(
        self,
        body: Any = _MISSING,
        severity_number: int | None = None,
        timestamp: str = "2024-06-14T12:00:00Z",
    ) -> Any:
        from acps_sdk.amp.models import LogRecord

        return LogRecord.model_validate(
            {
                "schema_version": "1.0",
                "log_type": "system",
                "log_id": "test-log-001",
                "timestamp": timestamp,
                "aic": "aic-001",
                "severity_number": severity_number,
                "body": body if body is not _MISSING else {"message": "test message"},
            }
        )

    def test_log_id_equals_record_log_id(self) -> None:
        record = self._make_record()
        doc = build_document(record, log_id="test-log-001", search_text_max_length=1000)
        assert doc.log_id == "test-log-001"

    def test_raw_body_preserved_verbatim(self) -> None:
        """C-SYSTEM-WRITE-3：raw_body 原样保留，无论 body 结构。"""
        body = {"key": "value", "nested": {"x": 1}}
        record = self._make_record(body=body)
        doc = build_document(record, log_id="test-log-001", search_text_max_length=1000)
        assert doc.source["raw_body"] == body

    def test_raw_body_preserved_for_none_body(self) -> None:
        """body=None 时 raw_body=None（C-SYSTEM-WRITE-3：原样保留）。"""
        record = self._make_record(body=None)
        doc = build_document(record, log_id="test-log-001", search_text_max_length=1000)
        assert doc.source["raw_body"] is None

    def test_message_always_present(self) -> None:
        """C-SYSTEM-WRITE-7：dict/None body 形态 message 恒存在。"""
        for body in [None, {"key": "val"}, {}, {"message": "hello"}]:
            record = self._make_record(body=body)
            doc = build_document(record, log_id="x", search_text_max_length=1000)
            assert "message" in doc.source
            assert doc.source["message"] is not None

    def test_indexed_at_not_in_source(self) -> None:
        """indexed_at 由 as_bulk_action 注入，source 中不含（设计 §2.4）。"""
        record = self._make_record()
        doc = build_document(record, log_id="x", search_text_max_length=1000)
        assert "indexed_at" not in doc.source

    def test_invalid_timestamp_raises(self) -> None:
        """时间无法解析 → InvalidSystemRecordError（writer 据此投 DLQ）。"""
        record = self._make_record(timestamp="not-a-timestamp")
        with pytest.raises(InvalidSystemRecordError):
            build_document(record, log_id="x", search_text_max_length=1000)

    def test_index_derived_from_timestamp(self) -> None:
        record = self._make_record(timestamp="2024-06-14T12:00:00Z")
        doc = build_document(record, log_id="x", search_text_max_length=1000)
        assert doc.index == "amp-system-events-20240614"

    def test_search_text_in_source(self) -> None:
        """search_text 写时生成，存于 source（C-SYSTEM-WRITE-5）。"""
        record = self._make_record(body={"message": "hello search"})
        doc = build_document(record, log_id="x", search_text_max_length=1000)
        assert "search_text" in doc.source
        assert isinstance(doc.source["search_text"], str)

    def test_as_bulk_action_injects_indexed_at(self) -> None:
        """as_bulk_action 注入 indexed_at，且 action meta 含 _id=log_id。"""
        record = self._make_record()
        doc = build_document(record, log_id="test-log-001", search_text_max_length=1000)
        meta, source = doc.as_bulk_action(indexed_at_iso="2024-06-14T12:00:00Z")
        assert meta["index"]["_id"] == "test-log-001"
        assert source["indexed_at"] == "2024-06-14T12:00:00Z"

    def test_original_source_not_mutated_by_bulk_action(self) -> None:
        """as_bulk_action 不修改原始 source dict（pure function 属性）。"""
        record = self._make_record()
        doc = build_document(record, log_id="x", search_text_max_length=1000)
        _, source1 = doc.as_bulk_action(indexed_at_iso="2024-06-14T00:00:00Z")
        _, source2 = doc.as_bulk_action(indexed_at_iso="2024-06-14T01:00:00Z")
        assert source1["indexed_at"] != source2["indexed_at"]
        assert "indexed_at" not in doc.source

    def test_aic_in_source(self) -> None:
        record = self._make_record()
        doc = build_document(record, log_id="x", search_text_max_length=1000)
        assert doc.source.get("aic") == "aic-001"

    def test_timestamp_ms_correct(self) -> None:
        record = self._make_record(timestamp="2024-06-14T12:00:00Z")
        doc = build_document(record, log_id="x", search_text_max_length=1000)
        assert doc.timestamp_ms > 0
