"""Audit 模块错误码与 AppError 子类。

参照 AMP-API-Design-Audit.md §6 + ACPs-spec-AMP.md §6.1.5 / §6.6.4。
"""

from __future__ import annotations

from enum import StrEnum

from app.core.base_exception import AppError


class AuditErrorCode(StrEnum):
    # ── Audit 特有错误码 ──────────────────────────────────────────────────
    RECORD_NOT_FOUND = "AMP_AUDIT_RECORD_NOT_FOUND"
    EXPORT_TOO_LARGE = "AMP_AUDIT_EXPORT_TOO_LARGE"
    VERIFICATION_FAILED = "AMP_AUDIT_VERIFICATION_FAILED"
    KEY_UNAVAILABLE = "AMP_AUDIT_KEY_UNAVAILABLE"
    TASK_NOT_FOUND = "AMP_AUDIT_TASK_NOT_FOUND"

    # ── 公共错误码（跨组复用） ────────────────────────────────────────────
    INVALID_TIME_RANGE = "AMP_INVALID_TIME_RANGE"
    INVALID_FILTER = "AMP_INVALID_FILTER"
    UNSUPPORTED_FIELD = "AMP_UNSUPPORTED_FIELD"
    READ_MODEL_LAGGING = "AMP_READ_MODEL_LAGGING"
    CURSOR_INVALID = "AMP_CURSOR_INVALID"
    MISSING_TIME_RANGE = "AMP_MISSING_TIME_RANGE"


class AuditRecordNotFoundError(AppError):
    """指定 auditId 不存在。"""

    def __init__(self, audit_id: str) -> None:
        super().__init__(
            status_code=404,
            code=AuditErrorCode.RECORD_NOT_FOUND,
            detail=f"审计记录不存在: auditId={audit_id!r}",
        )


class AuditTaskNotFoundError(AppError):
    """指定 taskId 不存在。"""

    def __init__(self, task_id: str) -> None:
        super().__init__(
            status_code=404,
            code=AuditErrorCode.TASK_NOT_FOUND,
            detail=f"任务不存在: taskId={task_id!r}",
        )


class InvalidTimeRangeError(AppError):
    """请求缺少 timeRange 或 timeRange 非法。"""

    def __init__(self, detail: str = "请求必须提供有界 timeRange") -> None:
        super().__init__(
            status_code=400,
            code=AuditErrorCode.INVALID_TIME_RANGE,
            detail=detail,
        )


class UnsupportedFieldError(AppError):
    """过滤或排序字段不在白名单中。"""

    def __init__(self, field: str) -> None:
        super().__init__(
            status_code=422,
            code=AuditErrorCode.UNSUPPORTED_FIELD,
            detail=f"不支持的查询字段: {field!r}，请参阅 API 文档中的字段白名单",
        )


class ReadModelLaggingError(AppError):
    """读模型滞后超过告警阈值。"""

    def __init__(self, lag_ms: int) -> None:
        super().__init__(
            status_code=503,
            code=AuditErrorCode.READ_MODEL_LAGGING,
            detail=f"读模型滞后 {lag_ms}ms，超过阈值，暂时不可用",
        )


class ExportTooLargeError(AppError):
    """导出请求的预估记录数超过上限。"""

    def __init__(self, estimated: int, limit: int) -> None:
        super().__init__(
            status_code=400,
            code=AuditErrorCode.EXPORT_TOO_LARGE,
            detail=f"导出记录数 {estimated} 超过上限 {limit}，请缩小时间范围或添加过滤条件",
        )


class InvalidFilterError(AppError):
    """校验请求至少需要一个过滤条件（recordIds / timeRange / filter 全部缺失）。"""

    def __init__(self, detail: str = "请求必须提供 recordIds、timeRange 或 filter 中至少一个") -> None:
        super().__init__(
            status_code=400,
            code=AuditErrorCode.INVALID_FILTER,
            detail=detail,
        )


class CursorInvalidError(AppError):
    """分页游标无效或与查询参数不匹配。"""

    def __init__(self) -> None:
        super().__init__(
            status_code=400,
            code=AuditErrorCode.CURSOR_INVALID,
            detail="分页游标无效，请使用上一页响应中返回的 cursor 并保持查询参数不变",
        )
