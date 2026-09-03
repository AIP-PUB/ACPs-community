"""app/access/exception.py — Access 模块错误码与异常体系。

遵循 exception.instructions：每个错误码一个 AppError 子类，HTTP status/error_code/detail 在构造器固定。
内部异常（InvalidAccessRecordError / ClickHouseInsertError / AccessConfigError）不继承 AppError，不对外暴露。
"""

from __future__ import annotations

from enum import StrEnum

from app.core.base_exception import AppError


class AccessErrorCode(StrEnum):
    """Access API 错误码（spec §6.4.4 + 公共 §6.1.5）。"""

    # Access 专用
    TRACE_NOT_FOUND = "AMP_TRACE_NOT_FOUND"
    TOPOLOGY_GROUPBY_INVALID = "AMP_TOPOLOGY_GROUPBY_INVALID"
    ATTRIBUTION_GROUPBY_INVALID = "AMP_ATTRIBUTION_GROUPBY_INVALID"
    # 公共（与其他模块同字符串值，spec §6.1.5）
    INVALID_TIME_RANGE = "AMP_INVALID_TIME_RANGE"
    INVALID_FILTER = "AMP_INVALID_FILTER"
    UNSUPPORTED_FIELD = "AMP_UNSUPPORTED_FIELD"
    UNSUPPORTED_OPERATOR = "AMP_UNSUPPORTED_OPERATOR"
    OUT_OF_RETENTION = "AMP_OUT_OF_RETENTION"
    CURSOR_INVALID = "AMP_CURSOR_INVALID"
    RESULT_TOO_LARGE = "AMP_RESULT_TOO_LARGE"
    READ_MODEL_LAGGING = "AMP_READ_MODEL_LAGGING"


# ── AppError 子类（API 层可见，每类 HTTP 状态码固定） ────────────────────────────


class TraceNotFoundError(AppError):
    """traces/{traceId} 未命中（404）。以 ClickHouse 结果为准，C-ACCESS-MODEL-5。"""

    def __init__(self, trace_id: str) -> None:
        super().__init__(
            code=AccessErrorCode.TRACE_NOT_FOUND,
            detail=f"Trace '{trace_id}' was not found.",
            status_code=404,
        )


class TopologyGroupByInvalidError(AppError):
    """topology/query groupBy 参数非法（422）。"""

    def __init__(self, value: str) -> None:
        super().__init__(
            code=AccessErrorCode.TOPOLOGY_GROUPBY_INVALID,
            detail=f"Invalid topology groupBy value: '{value}'. Allowed values: 'aic', 'service'.",
            status_code=422,
        )


class AttributionGroupByInvalidError(AppError):
    """errors/attribution groupBy 参数非法（422）。"""

    def __init__(self, value: str) -> None:
        super().__init__(
            code=AccessErrorCode.ATTRIBUTION_GROUPBY_INVALID,
            detail=(
                f"Invalid attribution groupBy value: '{value}'. Allowed values: 'errorCode', 'statusCode', 'endpoint'."
            ),
            status_code=422,
        )


class InvalidTimeRangeError(AppError):
    """无效时间范围（400）。"""

    def __init__(self, detail: str = "The specified time range is invalid (start must be before end).") -> None:
        super().__init__(
            code=AccessErrorCode.INVALID_TIME_RANGE,
            detail=detail,
            status_code=400,
        )


class InvalidFilterError(AppError):
    """无效过滤器（400）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=AccessErrorCode.INVALID_FILTER,
            detail=detail,
            status_code=400,
        )


class UnsupportedFieldError(AppError):
    """不支持的过滤/排序字段（422，C-ACCESS-QUERY-3/14）。"""

    def __init__(self, field: str, api: str) -> None:
        super().__init__(
            code=AccessErrorCode.UNSUPPORTED_FIELD,
            detail=f"Field '{field}' is not supported for the '{api}' endpoint.",
            status_code=422,
        )


class UnsupportedOperatorError(AppError):
    """不支持的过滤运算符（422）。"""

    def __init__(self, op: str) -> None:
        super().__init__(
            code=AccessErrorCode.UNSUPPORTED_OPERATOR,
            detail=f"Operator '{op}' is not supported.",
            status_code=422,
        )


class OutOfRetentionError(AppError):
    """查询窗口超出保留期（422，C-ACCESS-RETENTION-1）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=AccessErrorCode.OUT_OF_RETENTION,
            detail=detail,
            status_code=422,
        )


class CursorInvalidError(AppError):
    """分页游标无效或指纹不匹配（400）。"""

    def __init__(self, detail: str = "The pagination cursor is invalid or does not match this query.") -> None:
        super().__init__(
            code=AccessErrorCode.CURSOR_INVALID,
            detail=detail,
            status_code=400,
        )


class ResultTooLargeError(AppError):
    """结果集超出上限（413）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=AccessErrorCode.RESULT_TOO_LARGE,
            detail=detail,
            status_code=413,
        )


class ReadModelLaggingError(AppError):
    """读模型滞后，查询不可用（503）。"""

    def __init__(self, detail: str = "The read model is lagging. Please retry later.") -> None:
        super().__init__(
            code=AccessErrorCode.READ_MODEL_LAGGING,
            detail=detail,
            status_code=503,
        )


# ── 内部异常（不继承 AppError，不对外暴露为 HTTP 错误） ──────────────────────────


class InvalidAccessRecordError(Exception):
    """Access 记录格式非法（缺时间戳/解析失败）。

    Writer 收到此异常时不重试，直接投递 DLQ（格式错误重试无意义）。
    """


class ClickHouseInsertError(Exception):
    """access_events 批量 insert 失败。

    Writer 收到此异常时不 commit offset、不推摄取水位、不写去重标记（C-ACCESS-WRITE-7）。
    """


class AccessConfigError(Exception):
    """配置校验失败，进程拒绝启动（§6.20）。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))
