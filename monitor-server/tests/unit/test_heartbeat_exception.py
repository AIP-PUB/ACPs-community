"""tests/unit/test_heartbeat_exception.py — Heartbeat 异常定义单元测试。"""

from __future__ import annotations

from app.heartbeat.exception import (
    CursorInvalidError,
    DeltaLogUnhealthyError,
    HeartbeatAicUnknownError,
    HeartbeatConfigError,
    HeartbeatErrorCode,
    InvalidFilterError,
    QueryRequiresSelectiveFilterError,
    ReadModelLaggingError,
    SilenceRangeInvalidError,
    SnapshotUnavailableError,
    SyncDisabledError,
    UnsupportedFieldError,
    UnsupportedOperatorError,
    UntimedHeartbeatError,
)


class TestHeartbeatErrorCode:
    def test_aic_unknown(self) -> None:
        assert HeartbeatErrorCode.AIC_UNKNOWN.value == "AMP_HEARTBEAT_AIC_UNKNOWN"

    def test_silence_range_invalid(self) -> None:
        assert HeartbeatErrorCode.SILENCE_RANGE_INVALID.value == "AMP_HEARTBEAT_SILENCE_RANGE_INVALID"

    def test_read_model_lagging(self) -> None:
        assert HeartbeatErrorCode.READ_MODEL_LAGGING.value == "AMP_READ_MODEL_LAGGING"

    def test_all_codes_are_strings(self) -> None:
        for code in HeartbeatErrorCode:
            assert isinstance(code, str)


class TestHttpExceptions:
    def test_aic_unknown_404(self) -> None:
        exc = HeartbeatAicUnknownError("agent-001")
        assert exc.status_code == 404
        assert exc.code == HeartbeatErrorCode.AIC_UNKNOWN
        assert "agent-001" in exc.detail

    def test_silence_range_invalid_400(self) -> None:
        exc = SilenceRangeInvalidError("min > max")
        assert exc.status_code == 400
        assert exc.code == HeartbeatErrorCode.SILENCE_RANGE_INVALID

    def test_query_requires_selective_filter_400(self) -> None:
        exc = QueryRequiresSelectiveFilterError()
        assert exc.status_code == 400
        assert exc.code == HeartbeatErrorCode.QUERY_REQUIRES_SELECTIVE_FILTER

    def test_invalid_filter_400(self) -> None:
        exc = InvalidFilterError("in list too long")
        assert exc.status_code == 400
        assert exc.code == HeartbeatErrorCode.INVALID_FILTER

    def test_unsupported_field_422(self) -> None:
        exc = UnsupportedFieldError("timeRange")
        assert exc.status_code == 422
        assert exc.code == HeartbeatErrorCode.UNSUPPORTED_FIELD
        assert "timeRange" in exc.detail

    def test_unsupported_operator_422(self) -> None:
        exc = UnsupportedOperatorError("aic", "startsWith")
        assert exc.status_code == 422
        assert exc.code == HeartbeatErrorCode.UNSUPPORTED_OPERATOR
        assert "startsWith" in exc.detail
        assert "aic" in exc.detail

    def test_cursor_invalid_400(self) -> None:
        exc = CursorInvalidError()
        assert exc.status_code == 400
        assert exc.code == HeartbeatErrorCode.CURSOR_INVALID

    def test_read_model_lagging_503_with_lag(self) -> None:
        exc = ReadModelLaggingError(lag_ms=5000)
        assert exc.status_code == 503
        assert exc.code == HeartbeatErrorCode.READ_MODEL_LAGGING
        assert "5000" in exc.detail

    def test_read_model_lagging_503_with_detail(self) -> None:
        exc = ReadModelLaggingError(detail="Redis unavailable")
        assert exc.status_code == 503
        assert "Redis unavailable" in exc.detail

    def test_read_model_lagging_503_no_args(self) -> None:
        exc = ReadModelLaggingError()
        assert exc.status_code == 503

    def test_sync_disabled_404(self) -> None:
        exc = SyncDisabledError()
        assert exc.status_code == 404
        assert exc.code == HeartbeatErrorCode.SYNC_DISABLED

    def test_snapshot_unavailable_503(self) -> None:
        exc = SnapshotUnavailableError()
        assert exc.status_code == 503
        assert exc.code == HeartbeatErrorCode.SNAPSHOT_UNAVAILABLE

    def test_delta_log_unhealthy_503(self) -> None:
        exc = DeltaLogUnhealthyError()
        assert exc.status_code == 503
        assert exc.code == HeartbeatErrorCode.DELTA_LOG_UNHEALTHY

    def test_problem_details_contains_error_code(self) -> None:
        exc = HeartbeatAicUnknownError("agent-001")
        details = exc.to_problem_details()
        assert details["error_code"] == HeartbeatErrorCode.AIC_UNKNOWN
        assert details["status"] == 404


class TestInternalExceptions:
    def test_untimed_heartbeat_error_is_exception(self) -> None:
        exc = UntimedHeartbeatError("no timestamp")
        assert isinstance(exc, Exception)

    def test_heartbeat_config_error_is_runtime_error(self) -> None:
        exc = HeartbeatConfigError("bad config")
        assert isinstance(exc, RuntimeError)
