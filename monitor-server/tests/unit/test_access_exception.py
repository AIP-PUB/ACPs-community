"""tests/unit/test_access_exception.py — Access 模块错误码与异常体系测试。

TDD B-8：先写测试（红）→ 实现 exception.py（绿）。
"""

from __future__ import annotations


class TestAccessErrorCode:
    """AccessErrorCode 枚举值测试。"""

    def test_trace_not_found_code(self) -> None:
        from app.access.exception import AccessErrorCode

        assert AccessErrorCode.TRACE_NOT_FOUND.value == "AMP_TRACE_NOT_FOUND"

    def test_topology_groupby_invalid_code(self) -> None:
        from app.access.exception import AccessErrorCode

        assert AccessErrorCode.TOPOLOGY_GROUPBY_INVALID.value == "AMP_TOPOLOGY_GROUPBY_INVALID"

    def test_attribution_groupby_invalid_code(self) -> None:
        from app.access.exception import AccessErrorCode

        assert AccessErrorCode.ATTRIBUTION_GROUPBY_INVALID.value == "AMP_ATTRIBUTION_GROUPBY_INVALID"

    def test_invalid_time_range_code(self) -> None:
        from app.access.exception import AccessErrorCode

        assert AccessErrorCode.INVALID_TIME_RANGE.value == "AMP_INVALID_TIME_RANGE"

    def test_invalid_filter_code(self) -> None:
        from app.access.exception import AccessErrorCode

        assert AccessErrorCode.INVALID_FILTER.value == "AMP_INVALID_FILTER"

    def test_unsupported_field_code(self) -> None:
        from app.access.exception import AccessErrorCode

        assert AccessErrorCode.UNSUPPORTED_FIELD.value == "AMP_UNSUPPORTED_FIELD"

    def test_unsupported_operator_code(self) -> None:
        from app.access.exception import AccessErrorCode

        assert AccessErrorCode.UNSUPPORTED_OPERATOR.value == "AMP_UNSUPPORTED_OPERATOR"

    def test_out_of_retention_code(self) -> None:
        from app.access.exception import AccessErrorCode

        assert AccessErrorCode.OUT_OF_RETENTION.value == "AMP_OUT_OF_RETENTION"

    def test_cursor_invalid_code(self) -> None:
        from app.access.exception import AccessErrorCode

        assert AccessErrorCode.CURSOR_INVALID.value == "AMP_CURSOR_INVALID"

    def test_read_model_lagging_code(self) -> None:
        from app.access.exception import AccessErrorCode

        assert AccessErrorCode.READ_MODEL_LAGGING.value == "AMP_READ_MODEL_LAGGING"


class TestAppErrorSubclasses:
    """AppError 子类的 status_code 与 error_code 映射。"""

    def test_trace_not_found_status_404(self) -> None:
        from app.access.exception import TraceNotFoundError

        err = TraceNotFoundError("trace-abc")
        assert err.status_code == 404
        assert err.code == "AMP_TRACE_NOT_FOUND"

    def test_topology_groupby_invalid_status_422(self) -> None:
        from app.access.exception import TopologyGroupByInvalidError

        err = TopologyGroupByInvalidError("bad")
        assert err.status_code == 422
        assert err.code == "AMP_TOPOLOGY_GROUPBY_INVALID"

    def test_attribution_groupby_invalid_status_422(self) -> None:
        from app.access.exception import AttributionGroupByInvalidError

        err = AttributionGroupByInvalidError("bad")
        assert err.status_code == 422
        assert err.code == "AMP_ATTRIBUTION_GROUPBY_INVALID"

    def test_invalid_time_range_status_400(self) -> None:
        from app.access.exception import InvalidTimeRangeError

        err = InvalidTimeRangeError()
        assert err.status_code == 400
        assert err.code == "AMP_INVALID_TIME_RANGE"

    def test_invalid_filter_status_400(self) -> None:
        from app.access.exception import InvalidFilterError

        err = InvalidFilterError("bad filter")
        assert err.status_code == 400
        assert err.code == "AMP_INVALID_FILTER"

    def test_unsupported_field_status_422(self) -> None:
        from app.access.exception import UnsupportedFieldError

        err = UnsupportedFieldError("field", "events")
        assert err.status_code == 422
        assert err.code == "AMP_UNSUPPORTED_FIELD"

    def test_unsupported_operator_status_422(self) -> None:
        from app.access.exception import UnsupportedOperatorError

        err = UnsupportedOperatorError("op")
        assert err.status_code == 422
        assert err.code == "AMP_UNSUPPORTED_OPERATOR"

    def test_out_of_retention_status_422(self) -> None:
        from app.access.exception import OutOfRetentionError

        err = OutOfRetentionError("30 days")
        assert err.status_code == 422
        assert err.code == "AMP_OUT_OF_RETENTION"

    def test_cursor_invalid_status_400(self) -> None:
        from app.access.exception import CursorInvalidError

        err = CursorInvalidError()
        assert err.status_code == 400
        assert err.code == "AMP_CURSOR_INVALID"

    def test_read_model_lagging_status_503(self) -> None:
        from app.access.exception import ReadModelLaggingError

        err = ReadModelLaggingError()
        assert err.status_code == 503
        assert err.code == "AMP_READ_MODEL_LAGGING"

    def test_all_app_errors_have_problem_details(self) -> None:
        from app.access.exception import TraceNotFoundError
        from app.core.base_exception import AppError

        err = TraceNotFoundError("trace-xyz")
        assert isinstance(err, AppError)
        pd = err.to_problem_details()
        assert pd["status"] == 404
        assert pd["error_code"] == "AMP_TRACE_NOT_FOUND"


class TestInternalExceptions:
    """内部异常不继承 AppError，不对外泄漏 HTTP 错误。"""

    def test_invalid_access_record_error_not_app_error(self) -> None:
        from app.access.exception import InvalidAccessRecordError
        from app.core.base_exception import AppError

        err = InvalidAccessRecordError("bad timestamp")
        assert not isinstance(err, AppError)
        assert isinstance(err, Exception)

    def test_clickhouse_insert_error_not_app_error(self) -> None:
        from app.access.exception import ClickHouseInsertError
        from app.core.base_exception import AppError

        err = ClickHouseInsertError("ch fail")
        assert not isinstance(err, AppError)
        assert isinstance(err, Exception)

    def test_access_config_error_not_app_error(self) -> None:
        from app.access.exception import AccessConfigError
        from app.core.base_exception import AppError

        err = AccessConfigError(["archive_retention_days must be >= raw_retention_days"])
        assert not isinstance(err, AppError)
        assert isinstance(err, Exception)

    def test_access_config_error_stores_errors(self) -> None:
        from app.access.exception import AccessConfigError

        msgs = ["error1", "error2"]
        err = AccessConfigError(msgs)
        assert err.errors == msgs

    def test_public_error_codes_consistent_with_metrics(self) -> None:
        """公共错误码字符串在 access 与 metrics 中保持一致。"""
        from app.access.exception import AccessErrorCode
        from app.metrics.exception import MetricsErrorCode

        assert AccessErrorCode.INVALID_TIME_RANGE.value == MetricsErrorCode.INVALID_TIME_RANGE.value
        assert AccessErrorCode.INVALID_FILTER.value == MetricsErrorCode.INVALID_FILTER.value
        assert AccessErrorCode.UNSUPPORTED_FIELD.value == MetricsErrorCode.UNSUPPORTED_FIELD.value
        assert AccessErrorCode.UNSUPPORTED_OPERATOR.value == MetricsErrorCode.UNSUPPORTED_OPERATOR.value
        assert AccessErrorCode.OUT_OF_RETENTION.value == MetricsErrorCode.OUT_OF_RETENTION.value
        assert AccessErrorCode.CURSOR_INVALID.value == MetricsErrorCode.CURSOR_INVALID.value
        assert AccessErrorCode.READ_MODEL_LAGGING.value == MetricsErrorCode.READ_MODEL_LAGGING.value
