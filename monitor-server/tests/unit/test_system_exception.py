"""tests/unit/test_system_exception.py — System 模块异常体系测试。"""

from __future__ import annotations

import pytest

from app.core.base_exception import AppError
from app.system.exception import (
    CursorInvalidError,
    InvalidFilterError,
    InvalidSystemRecordError,
    InvalidTimeRangeError,
    OpenSearchBulkError,
    OpenSearchQueryError,
    OutOfRetentionError,
    ReadModelLaggingError,
    ResultTooLargeError,
    SystemConfigError,
    SystemErrorCode,
    SystemKeywordTooBroadError,
    UnsupportedFieldError,
    UnsupportedOperatorError,
)


class TestSystemErrorCode:
    """SystemErrorCode 字符串值精确匹配 spec §6.7.4。"""

    def test_keyword_too_broad(self) -> None:
        assert SystemErrorCode.KEYWORD_TOO_BROAD.value == "AMP_SYSTEM_KEYWORD_TOO_BROAD"

    def test_invalid_time_range(self) -> None:
        assert SystemErrorCode.INVALID_TIME_RANGE.value == "AMP_INVALID_TIME_RANGE"

    def test_invalid_filter(self) -> None:
        assert SystemErrorCode.INVALID_FILTER.value == "AMP_INVALID_FILTER"

    def test_unsupported_field(self) -> None:
        assert SystemErrorCode.UNSUPPORTED_FIELD.value == "AMP_UNSUPPORTED_FIELD"

    def test_unsupported_operator(self) -> None:
        assert SystemErrorCode.UNSUPPORTED_OPERATOR.value == "AMP_UNSUPPORTED_OPERATOR"

    def test_out_of_retention(self) -> None:
        assert SystemErrorCode.OUT_OF_RETENTION.value == "AMP_OUT_OF_RETENTION"

    def test_cursor_invalid(self) -> None:
        assert SystemErrorCode.CURSOR_INVALID.value == "AMP_CURSOR_INVALID"

    def test_result_too_large(self) -> None:
        assert SystemErrorCode.RESULT_TOO_LARGE.value == "AMP_RESULT_TOO_LARGE"

    def test_read_model_lagging(self) -> None:
        assert SystemErrorCode.READ_MODEL_LAGGING.value == "AMP_READ_MODEL_LAGGING"


class TestAppErrorSubclasses:
    """每个 AppError 子类的 HTTP 状态码和 error_code 固定。"""

    def test_keyword_too_broad_is_422(self) -> None:
        exc = SystemKeywordTooBroadError()
        assert exc.status_code == 422
        assert exc.code == "AMP_SYSTEM_KEYWORD_TOO_BROAD"
        assert isinstance(exc, AppError)

    def test_invalid_time_range_is_400(self) -> None:
        exc = InvalidTimeRangeError()
        assert exc.status_code == 400
        assert exc.code == "AMP_INVALID_TIME_RANGE"

    def test_invalid_filter_is_400(self) -> None:
        exc = InvalidFilterError("bad filter")
        assert exc.status_code == 400
        assert exc.code == "AMP_INVALID_FILTER"

    def test_unsupported_field_is_422(self) -> None:
        exc = UnsupportedFieldError("rawBody.x")
        assert exc.status_code == 422
        assert exc.code == "AMP_UNSUPPORTED_FIELD"
        assert "rawBody.x" in exc.detail

    def test_unsupported_operator_is_422(self) -> None:
        exc = UnsupportedOperatorError("contains", "message")
        assert exc.status_code == 422
        assert exc.code == "AMP_UNSUPPORTED_OPERATOR"

    def test_out_of_retention_is_422(self) -> None:
        exc = OutOfRetentionError()
        assert exc.status_code == 422
        assert exc.code == "AMP_OUT_OF_RETENTION"

    def test_cursor_invalid_is_400(self) -> None:
        exc = CursorInvalidError()
        assert exc.status_code == 400
        assert exc.code == "AMP_CURSOR_INVALID"

    def test_result_too_large_is_413(self) -> None:
        exc = ResultTooLargeError()
        assert exc.status_code == 413
        assert exc.code == "AMP_RESULT_TOO_LARGE"

    def test_read_model_lagging_is_503(self) -> None:
        exc = ReadModelLaggingError()
        assert exc.status_code == 503
        assert exc.code == "AMP_READ_MODEL_LAGGING"

    def test_keyword_too_broad_maps_422_both_triggers(self) -> None:
        """KEYWORD_TOO_BROAD 的两种触发路径（短关键词 + 过宽窗口）均映射 422 同码（设计 §8）。"""
        exc1 = SystemKeywordTooBroadError("keyword 'ab' is too short (min length: 3)")
        exc2 = SystemKeywordTooBroadError("keyword-only query window too wide")
        assert exc1.status_code == exc2.status_code == 422
        assert exc1.code == exc2.code == "AMP_SYSTEM_KEYWORD_TOO_BROAD"


class TestInternalExceptions:
    """内部异常不继承 AppError，不对外暴露为 HTTP 错误。"""

    def test_invalid_system_record_error_not_app_error(self) -> None:
        exc = InvalidSystemRecordError("bad timestamp")
        assert not isinstance(exc, AppError)
        assert isinstance(exc, Exception)

    def test_opensearch_bulk_error_not_app_error(self) -> None:
        exc = OpenSearchBulkError("bulk failed")
        assert not isinstance(exc, AppError)

    def test_opensearch_query_error_not_app_error(self) -> None:
        exc = OpenSearchQueryError("query failed")
        assert not isinstance(exc, AppError)

    def test_system_config_error_not_app_error(self) -> None:
        exc = SystemConfigError(["warm_days < hot_days", "keyword_min_length <= 0"])
        assert not isinstance(exc, AppError)
        assert exc.errors == ["warm_days < hot_days", "keyword_min_length <= 0"]
        assert "warm_days < hot_days" in str(exc)

    def test_system_config_error_stores_all_errors(self) -> None:
        errors = ["error1", "error2", "error3"]
        exc = SystemConfigError(errors)
        assert exc.errors == errors


@pytest.mark.parametrize(
    "exc_class",
    [
        SystemKeywordTooBroadError,
        InvalidTimeRangeError,
        OutOfRetentionError,
        CursorInvalidError,
        ResultTooLargeError,
        ReadModelLaggingError,
    ],
)
def test_app_error_has_problem_details(exc_class: type) -> None:
    exc = exc_class()
    pd = exc.to_problem_details()
    assert "error_code" in pd
    assert "status" in pd
    assert "detail" in pd
