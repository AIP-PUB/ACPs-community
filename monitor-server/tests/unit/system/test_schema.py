"""tests/unit/system/test_schema.py — schema.py 单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.system.schema import SystemEventQueryRequest, SystemEventView


class TestSystemEventView:
    """SystemEventView 视图模型测试。"""

    def _make_view(self, **kwargs: object) -> dict[str, object]:
        defaults = {
            "log_id": "log-001",
            "timestamp": "2024-06-14T12:00:00Z",
            "aic": "aic-001",
            "severity_number": 0,
            "message": "test message",
        }
        defaults.update(kwargs)
        return defaults

    def test_camel_case_alias_serialization(self) -> None:
        """logId/severityNumber 等 camelCase alias 在序列化时出现。"""
        view = SystemEventView(**self._make_view())
        data = view.model_dump(by_alias=True, exclude_none=True)
        assert "logId" in data
        assert "severityNumber" in data
        assert "timestamp" in data
        assert "aic" in data
        assert "message" in data

    def test_camel_case_alias_deserialization(self) -> None:
        """可从 camelCase 键反序列化。"""
        view = SystemEventView.model_validate(
            {
                "logId": "log-001",
                "timestamp": "2024-06-14T12:00:00Z",
                "aic": "aic-001",
                "severityNumber": 5,
                "message": "hello",
            }
        )
        assert view.log_id == "log-001"
        assert view.severity_number == 5

    def test_snake_case_also_works(self) -> None:
        """populate_by_name=True：snake_case 键也可用于反序列化。"""
        view = SystemEventView.model_validate(
            {
                "log_id": "log-001",
                "timestamp": "2024-06-14T12:00:00Z",
                "aic": "aic-001",
                "severity_number": 3,
                "message": "hello",
            }
        )
        assert view.log_id == "log-001"

    def test_message_is_required(self) -> None:
        """message 为必填非 Optional（C-SYSTEM-WRITE-7）。"""
        with pytest.raises(ValidationError):
            SystemEventView.model_validate(
                {
                    "log_id": "x",
                    "timestamp": "2024-01-01T00:00:00Z",
                    "aic": "a",
                    "severity_number": 0,
                }
            )

    def test_severity_number_is_required(self) -> None:
        """severity_number 为必填非 Optional（C-SYSTEM-WRITE-2）。"""
        with pytest.raises(ValidationError):
            SystemEventView.model_validate(
                {
                    "log_id": "x",
                    "timestamp": "2024-01-01T00:00:00Z",
                    "aic": "a",
                    "message": "hello",
                }
            )

    def test_raw_body_default_none_excluded(self) -> None:
        """raw_body 默认 None，exclude_none 序列化时不出现（§5.3 第 6 条）。"""
        view = SystemEventView(**self._make_view())
        data = view.model_dump(by_alias=True, exclude_none=True)
        assert "rawBody" not in data

    def test_raw_body_included_when_set(self) -> None:
        """raw_body 非 None 时，exclude_none 序列化时出现。"""
        view = SystemEventView(**self._make_view(raw_body={"k": "v"}))
        data = view.model_dump(by_alias=True, exclude_none=True)
        assert "rawBody" in data
        assert data["rawBody"] == {"k": "v"}

    def test_optional_fields_default_none(self) -> None:
        """可选字段默认 None。"""
        view = SystemEventView(**self._make_view())
        assert view.severity_text is None
        assert view.trace_id is None
        assert view.correlation_id is None
        assert view.category is None
        assert view.component is None
        assert view.module is None
        assert view.tags is None

    def test_trace_id_alias(self) -> None:
        view = SystemEventView.model_validate(
            {
                "logId": "x",
                "timestamp": "2024-01-01T00:00:00Z",
                "aic": "a",
                "severityNumber": 0,
                "message": "m",
                "traceId": "trace-123",
                "correlationId": "corr-456",
            }
        )
        assert view.trace_id == "trace-123"
        assert view.correlation_id == "corr-456"
        data = view.model_dump(by_alias=True, exclude_none=True)
        assert "traceId" in data
        assert "correlationId" in data


class TestSystemEventQueryRequest:
    """SystemEventQueryRequest 请求模型测试。"""

    def test_default_fields(self) -> None:
        req = SystemEventQueryRequest()
        assert req.time_range is None
        assert req.filter is None
        assert req.keyword is None
        assert req.sort is None
        assert req.include_raw_log is False
        assert req.page.limit == 50

    def test_include_raw_log_alias(self) -> None:
        req = SystemEventQueryRequest.model_validate({"includeRawLog": True})
        assert req.include_raw_log is True

    def test_snake_case_include_raw_log(self) -> None:
        req = SystemEventQueryRequest(include_raw_log=True)
        assert req.include_raw_log is True

    def test_reuses_amp_filter_not_redefined(self) -> None:
        """schema.py 从 app.core.amp_api_schema 导入 AMPFilter，不重复定义。"""
        from app.core.amp_api_schema import AMPFilter

        req = SystemEventQueryRequest()
        assert req.filter is None or isinstance(req.filter, AMPFilter)
