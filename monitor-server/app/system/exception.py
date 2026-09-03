"""app/system/exception.py — System 模块错误码与异常体系。

遵循 exception.instructions：每个错误码一个 AppError 子类，HTTP status/error_code/detail 在构造器固定。
内部异常（InvalidSystemRecordError / OpenSearchBulkError / OpenSearchQueryError / SystemConfigError）
不继承 AppError，不对外暴露。
"""

from __future__ import annotations

from enum import StrEnum

from app.core.base_exception import AppError


class SystemErrorCode(StrEnum):
    """System API 错误码（spec §6.7.4 + 公共 §6.1.5）。"""

    # System 专用
    KEYWORD_TOO_BROAD = "AMP_SYSTEM_KEYWORD_TOO_BROAD"
    # 公共（spec §6.1.5）
    INVALID_TIME_RANGE = "AMP_INVALID_TIME_RANGE"
    INVALID_FILTER = "AMP_INVALID_FILTER"
    UNSUPPORTED_FIELD = "AMP_UNSUPPORTED_FIELD"
    UNSUPPORTED_OPERATOR = "AMP_UNSUPPORTED_OPERATOR"
    OUT_OF_RETENTION = "AMP_OUT_OF_RETENTION"
    CURSOR_INVALID = "AMP_CURSOR_INVALID"
    RESULT_TOO_LARGE = "AMP_RESULT_TOO_LARGE"
    READ_MODEL_LAGGING = "AMP_READ_MODEL_LAGGING"


# ── AppError 子类（API 层可见，每类 HTTP 状态码固定） ────────────────────────────


class SystemKeywordTooBroadError(AppError):
    """keyword 过宽（短关键词或 keyword-only 召回过宽，422，C-SYSTEM-QUERY-4）。"""

    def __init__(
        self,
        detail: str = (
            "The keyword is too short or the time window is too wide for a keyword-only query. "
            "Please add more specific filters or narrow the time range."
        ),
    ) -> None:
        super().__init__(
            code=SystemErrorCode.KEYWORD_TOO_BROAD,
            detail=detail,
            status_code=422,
        )


class InvalidTimeRangeError(AppError):
    """无效时间范围（400）。"""

    def __init__(self, detail: str = "The specified time range is invalid (start must be before end).") -> None:
        super().__init__(
            code=SystemErrorCode.INVALID_TIME_RANGE,
            detail=detail,
            status_code=400,
        )


class InvalidFilterError(AppError):
    """无效过滤器（400，含 in/nin >256）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=SystemErrorCode.INVALID_FILTER,
            detail=detail,
            status_code=400,
        )


class UnsupportedFieldError(AppError):
    """不支持的过滤/排序字段（422，含 rawBody 深层、非白名单 sort 字段）。"""

    def __init__(self, field: str) -> None:
        super().__init__(
            code=SystemErrorCode.UNSUPPORTED_FIELD,
            detail=f"Field '{field}' is not supported for the system events/query endpoint.",
            status_code=422,
        )


class UnsupportedOperatorError(AppError):
    """不支持的过滤运算符（422，含 tags.* 不支持算子、message 子串算子）。"""

    def __init__(self, op: str, field: str = "") -> None:
        detail = (
            f"Operator '{op}' is not supported for field '{field}'." if field else f"Operator '{op}' is not supported."
        )
        super().__init__(
            code=SystemErrorCode.UNSUPPORTED_OPERATOR,
            detail=detail,
            status_code=422,
        )


class OutOfRetentionError(AppError):
    """查询窗口超出有效保留期（422，C-SYSTEM-RETENTION-1）。"""

    def __init__(self, detail: str = "The requested time range is outside the available retention window.") -> None:
        super().__init__(
            code=SystemErrorCode.OUT_OF_RETENTION,
            detail=detail,
            status_code=422,
        )


class CursorInvalidError(AppError):
    """分页游标无效、损坏或指纹不匹配（400，含 PIT 过期）。"""

    def __init__(
        self, detail: str = "The pagination cursor is invalid, expired, or does not match this query."
    ) -> None:
        super().__init__(
            code=SystemErrorCode.CURSOR_INVALID,
            detail=detail,
            status_code=400,
        )


class ResultTooLargeError(AppError):
    """结果集超出上限（413）。"""

    def __init__(self, detail: str = "The result set is too large. Please narrow your query.") -> None:
        super().__init__(
            code=SystemErrorCode.RESULT_TOO_LARGE,
            detail=detail,
            status_code=413,
        )


class ReadModelLaggingError(AppError):
    """读模型滞后，查询不可用（503）。"""

    def __init__(self, detail: str = "The read model is lagging. Please retry later.") -> None:
        super().__init__(
            code=SystemErrorCode.READ_MODEL_LAGGING,
            detail=detail,
            status_code=503,
        )


# ── 内部异常（不继承 AppError，不对外暴露为 HTTP 错误） ──────────────────────────


class InvalidSystemRecordError(Exception):
    """System 记录格式非法（缺时间戳/解析失败）。

    Writer 收到此异常时不重试，直接投递 DLQ（格式错误重试无意义）。
    """


class OpenSearchBulkError(Exception):
    """Bulk Index 失败（transient 错误）。

    Writer 收到此异常时不 commit offset、不推摄取水位（C-SYSTEM-WRITE）。
    """


class OpenSearchQueryError(Exception):
    """搜索失败（PIT 失效 / 超时 / OpenSearch 不可用）。

    service 层据错误消息区分 PIT 失效（→ CursorInvalidError）与其他故障（→ ReadModelLaggingError）。
    """


class SystemConfigError(Exception):
    """配置校验失败，进程拒绝启动（§6.15）。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))
