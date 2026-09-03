"""单元测试：Message 模块错误码与异常体系（A-1）。

按设计 §6.9 / exception.instructions 验证：
- MessageErrorCode 值域及与其他模块的一致性
- AppError 子类的 status_code / error_code
- 内部异常不继承 AppError
"""

from __future__ import annotations

from app.core.base_exception import AppError
from app.message.exception import (
    ClickHouseInsertError,
    CursorInvalidError,
    InvalidFilterError,
    InvalidMessageRecordError,
    InvalidTimeRangeError,
    LifecycleAmbiguousError,
    LifecycleKeyRequiredError,
    MessageCompactionError,
    MessageConfigError,
    MessageDestinationRequiredError,
    MessageErrorCode,
    MessageGroupByInvalidError,
    MessageNotFoundError,
    OutOfRetentionError,
    ReadModelLaggingError,
    ResultTooLargeError,
    StateSnapshotUnavailableError,
    UnsupportedFieldError,
    UnsupportedOperatorError,
)


class TestMessageErrorCodeValues:
    """MessageErrorCode 字符串值正确，且公共码与其他模块一致。"""

    def test_lifecycle_key_required(self) -> None:
        assert MessageErrorCode.LIFECYCLE_KEY_REQUIRED.value == "AMP_MESSAGE_LIFECYCLE_KEY_REQUIRED"

    def test_lifecycle_ambiguous(self) -> None:
        assert MessageErrorCode.LIFECYCLE_AMBIGUOUS.value == "AMP_MESSAGE_LIFECYCLE_AMBIGUOUS"

    def test_groupby_invalid(self) -> None:
        assert MessageErrorCode.GROUPBY_INVALID.value == "AMP_MESSAGE_GROUPBY_INVALID"

    def test_destination_required(self) -> None:
        assert MessageErrorCode.DESTINATION_REQUIRED.value == "AMP_MESSAGE_DESTINATION_REQUIRED"

    def test_state_snapshot_unavailable(self) -> None:
        assert MessageErrorCode.STATE_SNAPSHOT_UNAVAILABLE.value == "AMP_MESSAGE_STATE_SNAPSHOT_UNAVAILABLE"

    # 公共错误码与其他模块保持一致
    def test_not_found_public(self) -> None:
        assert MessageErrorCode.NOT_FOUND.value == "AMP_NOT_FOUND"

    def test_invalid_time_range_public(self) -> None:
        assert MessageErrorCode.INVALID_TIME_RANGE.value == "AMP_INVALID_TIME_RANGE"

    def test_invalid_filter_public(self) -> None:
        assert MessageErrorCode.INVALID_FILTER.value == "AMP_INVALID_FILTER"

    def test_unsupported_field_public(self) -> None:
        assert MessageErrorCode.UNSUPPORTED_FIELD.value == "AMP_UNSUPPORTED_FIELD"

    def test_unsupported_operator_public(self) -> None:
        assert MessageErrorCode.UNSUPPORTED_OPERATOR.value == "AMP_UNSUPPORTED_OPERATOR"

    def test_out_of_retention_public(self) -> None:
        assert MessageErrorCode.OUT_OF_RETENTION.value == "AMP_OUT_OF_RETENTION"

    def test_cursor_invalid_public(self) -> None:
        assert MessageErrorCode.CURSOR_INVALID.value == "AMP_CURSOR_INVALID"

    def test_result_too_large_public(self) -> None:
        assert MessageErrorCode.RESULT_TOO_LARGE.value == "AMP_RESULT_TOO_LARGE"

    def test_read_model_lagging_public(self) -> None:
        assert MessageErrorCode.READ_MODEL_LAGGING.value == "AMP_READ_MODEL_LAGGING"


class TestAppErrorSubclasses:
    """每个 AppError 子类：status_code / error_code 正确。"""

    def test_lifecycle_key_required_is_app_error(self) -> None:
        err = LifecycleKeyRequiredError()
        assert isinstance(err, AppError)
        assert err.status_code == 422
        assert err.code == MessageErrorCode.LIFECYCLE_KEY_REQUIRED

    def test_lifecycle_ambiguous_is_app_error(self) -> None:
        err = LifecycleAmbiguousError()
        assert isinstance(err, AppError)
        assert err.status_code == 422
        assert err.code == MessageErrorCode.LIFECYCLE_AMBIGUOUS

    def test_message_groupby_invalid_is_app_error(self) -> None:
        err = MessageGroupByInvalidError("bad")
        assert isinstance(err, AppError)
        assert err.status_code == 422
        assert err.code == MessageErrorCode.GROUPBY_INVALID

    def test_message_destination_required_is_app_error(self) -> None:
        err = MessageDestinationRequiredError()
        assert isinstance(err, AppError)
        assert err.status_code == 422
        assert err.code == MessageErrorCode.DESTINATION_REQUIRED

    def test_state_snapshot_unavailable_is_app_error(self) -> None:
        err = StateSnapshotUnavailableError()
        assert isinstance(err, AppError)
        assert err.status_code == 503
        assert err.code == MessageErrorCode.STATE_SNAPSHOT_UNAVAILABLE

    def test_message_not_found_is_app_error(self) -> None:
        err = MessageNotFoundError("m1")
        assert isinstance(err, AppError)
        assert err.status_code == 404
        assert err.code == MessageErrorCode.NOT_FOUND

    def test_invalid_time_range_is_app_error(self) -> None:
        err = InvalidTimeRangeError()
        assert isinstance(err, AppError)
        assert err.status_code == 400
        assert err.code == MessageErrorCode.INVALID_TIME_RANGE

    def test_invalid_filter_is_app_error(self) -> None:
        err = InvalidFilterError("bad filter")
        assert isinstance(err, AppError)
        assert err.status_code == 400
        assert err.code == MessageErrorCode.INVALID_FILTER

    def test_unsupported_field_is_app_error(self) -> None:
        err = UnsupportedFieldError("foo", "events")
        assert isinstance(err, AppError)
        assert err.status_code == 422
        assert err.code == MessageErrorCode.UNSUPPORTED_FIELD

    def test_unsupported_operator_is_app_error(self) -> None:
        err = UnsupportedOperatorError("xor")
        assert isinstance(err, AppError)
        assert err.status_code == 422
        assert err.code == MessageErrorCode.UNSUPPORTED_OPERATOR

    def test_out_of_retention_is_app_error(self) -> None:
        err = OutOfRetentionError("too old")
        assert isinstance(err, AppError)
        assert err.status_code == 422
        assert err.code == MessageErrorCode.OUT_OF_RETENTION

    def test_cursor_invalid_is_app_error(self) -> None:
        err = CursorInvalidError()
        assert isinstance(err, AppError)
        assert err.status_code == 400
        assert err.code == MessageErrorCode.CURSOR_INVALID

    def test_result_too_large_is_app_error(self) -> None:
        err = ResultTooLargeError("too large")
        assert isinstance(err, AppError)
        assert err.status_code == 413
        assert err.code == MessageErrorCode.RESULT_TOO_LARGE

    def test_read_model_lagging_is_app_error(self) -> None:
        err = ReadModelLaggingError()
        assert isinstance(err, AppError)
        assert err.status_code == 503
        assert err.code == MessageErrorCode.READ_MODEL_LAGGING


class TestInternalExceptions:
    """内部异常不继承 AppError，不会暴露为 HTTP 错误。"""

    def test_invalid_message_record_not_app_error(self) -> None:
        err = InvalidMessageRecordError("bad")
        assert not isinstance(err, AppError)
        assert isinstance(err, Exception)

    def test_clickhouse_insert_not_app_error(self) -> None:
        err = ClickHouseInsertError("ch fail")
        assert not isinstance(err, AppError)
        assert isinstance(err, Exception)

    def test_message_compaction_not_app_error(self) -> None:
        err = MessageCompactionError("comp fail")
        assert not isinstance(err, AppError)
        assert isinstance(err, Exception)

    def test_message_config_not_app_error(self) -> None:
        err = MessageConfigError(["err1", "err2"])
        assert not isinstance(err, AppError)
        assert isinstance(err, Exception)
        assert err.errors == ["err1", "err2"]

    def test_message_config_error_message(self) -> None:
        err = MessageConfigError(["e1", "e2"])
        assert "e1" in str(err)
        assert "e2" in str(err)
