"""app/message/exception.py — Message 模块错误码与异常体系。

遵循 exception.instructions：每个错误码一个 AppError 子类，HTTP status/error_code/detail 在构造器固定。
内部异常（InvalidMessageRecordError / ClickHouseInsertError / MessageCompactionError / MessageConfigError）
不继承 AppError，不对外暴露。
"""

from __future__ import annotations

from enum import StrEnum

from app.core.base_exception import AppError


class MessageErrorCode(StrEnum):
    """Message API 错误码（spec §6.5.4 + 公共 §6.1.5）。"""

    # Message 专用
    LIFECYCLE_KEY_REQUIRED = "AMP_MESSAGE_LIFECYCLE_KEY_REQUIRED"
    LIFECYCLE_AMBIGUOUS = "AMP_MESSAGE_LIFECYCLE_AMBIGUOUS"
    GROUPBY_INVALID = "AMP_MESSAGE_GROUPBY_INVALID"
    DESTINATION_REQUIRED = "AMP_MESSAGE_DESTINATION_REQUIRED"
    STATE_SNAPSHOT_UNAVAILABLE = "AMP_MESSAGE_STATE_SNAPSHOT_UNAVAILABLE"
    STEP_INVALID = "AMP_MESSAGE_STEP_INVALID"
    # 公共（spec §6.1.5）
    NOT_FOUND = "AMP_NOT_FOUND"
    INVALID_TIME_RANGE = "AMP_INVALID_TIME_RANGE"
    INVALID_FILTER = "AMP_INVALID_FILTER"
    UNSUPPORTED_FIELD = "AMP_UNSUPPORTED_FIELD"
    UNSUPPORTED_OPERATOR = "AMP_UNSUPPORTED_OPERATOR"
    OUT_OF_RETENTION = "AMP_OUT_OF_RETENTION"
    CURSOR_INVALID = "AMP_CURSOR_INVALID"
    RESULT_TOO_LARGE = "AMP_RESULT_TOO_LARGE"
    READ_MODEL_LAGGING = "AMP_READ_MODEL_LAGGING"


# ── AppError 子类（API 层可见，每类 HTTP 状态码固定） ────────────────────────────


class LifecycleKeyRequiredError(AppError):
    """lifecycles/query 缺少选择性条件（422，C-MESSAGE-QUERY-1）。"""

    def __init__(
        self,
        detail: str = "A lifecycle key filter (messageId, lifecycleKey, correlationId, or traceId) is required.",
    ) -> None:
        super().__init__(
            code=MessageErrorCode.LIFECYCLE_KEY_REQUIRED,
            detail=detail,
            status_code=422,
        )


class LifecycleAmbiguousError(AppError):
    """lifecycles/{messageId} 命中多个目的地（422，C-MESSAGE-QUERY-7）。"""

    def __init__(
        self,
        detail: str = (
            "The message ID matches multiple destinations. Please add destination filters to narrow the result."
        ),
    ) -> None:
        super().__init__(
            code=MessageErrorCode.LIFECYCLE_AMBIGUOUS,
            detail=detail,
            status_code=422,
        )


class MessageGroupByInvalidError(AppError):
    """destinations/query groupBy 参数非法（422）。"""

    def __init__(self, value: str) -> None:
        super().__init__(
            code=MessageErrorCode.GROUPBY_INVALID,
            detail=(
                f"Invalid groupBy value: '{value}'. "
                "Allowed: system, destination.name, destination.kind, destination.virtualHost."
            ),
            status_code=422,
        )


class MessageStepInvalidError(AppError):
    """destinations/throughput step 参数非法（422）。"""

    def __init__(self, detail: str = "The 'step' parameter is not a valid ISO 8601 duration.") -> None:
        super().__init__(
            code=MessageErrorCode.STEP_INVALID,
            detail=detail,
            status_code=422,
        )


class MessageDestinationRequiredError(AppError):
    """destinations/throughput 缺少 system / destinationName（422）。"""

    def __init__(
        self,
        detail: str = "Both 'system' and 'destinationName' are required for the throughput endpoint.",
    ) -> None:
        super().__init__(
            code=MessageErrorCode.DESTINATION_REQUIRED,
            detail=detail,
            status_code=422,
        )


class StateSnapshotUnavailableError(AppError):
    """destinations/query 窗口内无快照或采样未启用（503，C-MESSAGE-QUERY-4）。"""

    def __init__(
        self,
        detail: str = "No destination state snapshot is available for the requested time window.",
    ) -> None:
        super().__init__(
            code=MessageErrorCode.STATE_SNAPSHOT_UNAVAILABLE,
            detail=detail,
            status_code=503,
        )


class MessageNotFoundError(AppError):
    """lifecycles/{messageId} 未命中（404）。"""

    def __init__(self, message_id: str) -> None:
        super().__init__(
            code=MessageErrorCode.NOT_FOUND,
            detail=f"No lifecycle found for message ID '{message_id}'.",
            status_code=404,
        )


class InvalidTimeRangeError(AppError):
    """无效时间范围（400）。"""

    def __init__(self, detail: str = "The specified time range is invalid (start must be before end).") -> None:
        super().__init__(
            code=MessageErrorCode.INVALID_TIME_RANGE,
            detail=detail,
            status_code=400,
        )


class InvalidFilterError(AppError):
    """无效过滤器（400）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=MessageErrorCode.INVALID_FILTER,
            detail=detail,
            status_code=400,
        )


class UnsupportedFieldError(AppError):
    """不支持的过滤/排序字段（422）。"""

    def __init__(self, field: str, api: str) -> None:
        super().__init__(
            code=MessageErrorCode.UNSUPPORTED_FIELD,
            detail=f"Field '{field}' is not supported for the '{api}' endpoint.",
            status_code=422,
        )


class UnsupportedOperatorError(AppError):
    """不支持的过滤运算符（422）。"""

    def __init__(self, op: str) -> None:
        super().__init__(
            code=MessageErrorCode.UNSUPPORTED_OPERATOR,
            detail=f"Operator '{op}' is not supported.",
            status_code=422,
        )


class OutOfRetentionError(AppError):
    """查询窗口超出保留期（422，C-MESSAGE-RETENTION-1）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=MessageErrorCode.OUT_OF_RETENTION,
            detail=detail,
            status_code=422,
        )


class CursorInvalidError(AppError):
    """分页游标无效或指纹不匹配（400）。"""

    def __init__(self, detail: str = "The pagination cursor is invalid or does not match this query.") -> None:
        super().__init__(
            code=MessageErrorCode.CURSOR_INVALID,
            detail=detail,
            status_code=400,
        )


class ResultTooLargeError(AppError):
    """结果集超出上限（413）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=MessageErrorCode.RESULT_TOO_LARGE,
            detail=detail,
            status_code=413,
        )


class ReadModelLaggingError(AppError):
    """读模型滞后，查询不可用（503）。"""

    def __init__(self, detail: str = "The read model is lagging. Please retry later.") -> None:
        super().__init__(
            code=MessageErrorCode.READ_MODEL_LAGGING,
            detail=detail,
            status_code=503,
        )


# ── 内部异常（不继承 AppError，不对外暴露为 HTTP 错误） ──────────────────────────


class InvalidMessageRecordError(Exception):
    """Message 记录格式非法（缺时间戳/解析失败）。

    Writer 收到此异常时不重试，直接投递 DLQ（格式错误重试无意义）。
    """


class ClickHouseInsertError(Exception):
    """message_events 批量 insert 失败。

    Writer 收到此异常时不 commit offset、不推摄取水位、不写去重标记（C-MESSAGE-WRITE-2）。
    """


class MessageCompactionError(Exception):
    """compactor 重算失败 → 不推进 compactor 水位（保留旧派生行，下轮重试）。"""


class MessageConfigError(Exception):
    """配置校验失败，进程拒绝启动（§6.23）。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))
