"""tests/unit/test_audit_exception.py — Audit 错误码与 AppError 子类单元测试。"""

from enum import StrEnum

from app.audit.exception import (
    AuditErrorCode,
    AuditRecordNotFoundError,
    AuditTaskNotFoundError,
    CursorInvalidError,
    ExportTooLargeError,
    InvalidFilterError,
    InvalidTimeRangeError,
    ReadModelLaggingError,
    UnsupportedFieldError,
)
from app.core.base_exception import AppError


class TestAuditErrorCode:
    def test_is_str_enum(self) -> None:
        assert issubclass(AuditErrorCode, StrEnum)

    def test_all_required_codes_defined(self) -> None:
        required = {
            "RECORD_NOT_FOUND",
            "EXPORT_TOO_LARGE",
            "VERIFICATION_FAILED",
            "KEY_UNAVAILABLE",
            "TASK_NOT_FOUND",
            "INVALID_TIME_RANGE",
            "INVALID_FILTER",
            "UNSUPPORTED_FIELD",
            "READ_MODEL_LAGGING",
            "CURSOR_INVALID",
            "MISSING_TIME_RANGE",
        }
        defined = set(AuditErrorCode.__members__)
        assert required.issubset(defined), f"缺少错误码: {required - defined}"


class TestAppErrorSubclasses:
    def test_audit_record_not_found_is_404(self) -> None:
        err = AuditRecordNotFoundError("aud-001")
        assert err.status_code == 404
        assert isinstance(err, AppError)

    def test_invalid_time_range_is_400(self) -> None:
        err = InvalidTimeRangeError()
        assert err.status_code == 400

    def test_unsupported_field_is_422(self) -> None:
        err = UnsupportedFieldError("unknownField")
        assert err.status_code == 422
        assert "unknownField" in err.detail

    def test_export_too_large_is_400(self) -> None:
        err = ExportTooLargeError(estimated=2000, limit=1000)
        assert err.status_code == 400
        assert "2000" in err.detail

    def test_read_model_lagging_is_503(self) -> None:
        err = ReadModelLaggingError(lag_ms=90000)
        assert err.status_code == 503
        assert err.code == AuditErrorCode.READ_MODEL_LAGGING

    def test_invalid_filter_is_400(self) -> None:
        err = InvalidFilterError()
        assert err.status_code == 400
        assert err.code == AuditErrorCode.INVALID_FILTER


class TestProblemDetails:
    def test_rfc9457_structure(self) -> None:
        err = AuditRecordNotFoundError("aud-xyz")
        payload = err.to_problem_details()
        assert "type" in payload
        assert "status" in payload
        assert "title" in payload
        assert "detail" in payload
        assert payload["status"] == 404

    def test_error_code_in_extensions(self) -> None:
        err = AuditTaskNotFoundError("task-abc")
        payload = err.to_problem_details()
        assert payload.get("error_code") == AuditErrorCode.TASK_NOT_FOUND

    def test_cursor_invalid_code(self) -> None:
        err = CursorInvalidError()
        assert err.code == AuditErrorCode.CURSOR_INVALID
