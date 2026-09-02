"""Heartbeat 模块错误码与异常定义（spec §6.2.4 + exception.instructions.md）。

业务异常继承 AppError；内部异常（不出 HTTP 边界）直接继承 Exception / RuntimeError。
"""

from __future__ import annotations

from enum import StrEnum

from app.core.base_exception import AppError


class HeartbeatErrorCode(StrEnum):
    """Heartbeat 模块错误码（spec §6.2.4）。"""

    # ── Heartbeat 专有 ──
    AIC_UNKNOWN = "AMP_HEARTBEAT_AIC_UNKNOWN"
    SILENCE_RANGE_INVALID = "AMP_HEARTBEAT_SILENCE_RANGE_INVALID"
    QUERY_REQUIRES_SELECTIVE_FILTER = "AMP_QUERY_REQUIRES_SELECTIVE_FILTER"
    SYNC_DISABLED = "AMP_HEARTBEAT_SYNC_DISABLED"
    SNAPSHOT_UNAVAILABLE = "AMP_HEARTBEAT_SNAPSHOT_UNAVAILABLE"
    DELTA_LOG_UNHEALTHY = "AMP_HEARTBEAT_DELTA_LOG_UNHEALTHY"
    # AMP_HEARTBEAT_SYNC_VIEW_UNSUPPORTED：设计 §9 判定当前端点集合下无触发点，
    # 保留错误码不纳入实现

    # ── 公共（spec §6.1.5，按 exception.instructions 在本模块直接定义使用） ──
    INVALID_FILTER = "AMP_INVALID_FILTER"
    UNSUPPORTED_FIELD = "AMP_UNSUPPORTED_FIELD"
    UNSUPPORTED_OPERATOR = "AMP_UNSUPPORTED_OPERATOR"
    CURSOR_INVALID = "AMP_CURSOR_INVALID"
    READ_MODEL_LAGGING = "AMP_READ_MODEL_LAGGING"


# ── HTTP 业务异常（出 HTTP 边界） ─────────────────────────────────────────────


class HeartbeatAicUnknownError(AppError):
    """AIC 不存在（404）。"""

    def __init__(self, aic: str) -> None:
        super().__init__(
            code=HeartbeatErrorCode.AIC_UNKNOWN,
            detail=f"AIC not found: {aic}",
            status_code=404,
        )


class SilenceRangeInvalidError(AppError):
    """silence 时间范围参数非法（400）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=HeartbeatErrorCode.SILENCE_RANGE_INVALID,
            detail=detail,
            status_code=400,
        )


class QueryRequiresSelectiveFilterError(AppError):
    """查询缺少选择性过滤条件（400，C-QUERY-1）。"""

    def __init__(self) -> None:
        super().__init__(
            code=HeartbeatErrorCode.QUERY_REQUIRES_SELECTIVE_FILTER,
            detail="Query requires a selective filter (aic eq/in, or silence range with cursor).",
            status_code=400,
        )


class InvalidFilterError(AppError):
    """过滤条件非法（400）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=HeartbeatErrorCode.INVALID_FILTER,
            detail=detail,
            status_code=400,
        )


class UnsupportedFieldError(AppError):
    """字段不支持（422）。"""

    def __init__(self, field: str) -> None:
        super().__init__(
            code=HeartbeatErrorCode.UNSUPPORTED_FIELD,
            detail=f"Field '{field}' is not supported.",
            status_code=422,
        )


class UnsupportedOperatorError(AppError):
    """运算符不支持（422）。"""

    def __init__(self, field: str, op: str) -> None:
        super().__init__(
            code=HeartbeatErrorCode.UNSUPPORTED_OPERATOR,
            detail=f"Operator '{op}' is not supported for field '{field}'.",
            status_code=422,
        )


class CursorInvalidError(AppError):
    """游标无效（400）。"""

    def __init__(self) -> None:
        super().__init__(
            code=HeartbeatErrorCode.CURSOR_INVALID,
            detail="Cursor is invalid or does not match the current query parameters.",
            status_code=400,
        )


class ReadModelLaggingError(AppError):
    """读模型滞后（503）。"""

    def __init__(self, lag_ms: int | None = None, detail: str | None = None) -> None:
        if detail:
            msg = detail
        elif lag_ms is not None:
            msg = f"Read model is lagging behind by {lag_ms}ms."
        else:
            msg = "Read model is lagging behind."
        super().__init__(
            code=HeartbeatErrorCode.READ_MODEL_LAGGING,
            detail=msg,
            status_code=503,
        )


class SyncDisabledError(AppError):
    """Sync Profile 未启用（404）。"""

    def __init__(self) -> None:
        super().__init__(
            code=HeartbeatErrorCode.SYNC_DISABLED,
            detail="Sync Profile is disabled on this deployment.",
            status_code=404,
        )


class SnapshotUnavailableError(AppError):
    """快照不可用（503）。"""

    def __init__(self, detail: str = "Snapshot is currently unavailable.") -> None:
        super().__init__(
            code=HeartbeatErrorCode.SNAPSHOT_UNAVAILABLE,
            detail=detail,
            status_code=503,
        )


class DeltaLogUnhealthyError(AppError):
    """Delta log 不健康（503）。"""

    def __init__(self, detail: str = "Delta log is unhealthy; publish lag exceeds threshold.") -> None:
        super().__init__(
            code=HeartbeatErrorCode.DELTA_LOG_UNHEALTHY,
            detail=detail,
            status_code=503,
        )


# ── 内部异常（不出 HTTP 边界） ─────────────────────────────────────────────────


class UntimedHeartbeatError(Exception):
    """心跳消息缺少可靠 observedAt 时间戳，应直接进 DLQ（writer 内部）。"""


class HeartbeatConfigError(RuntimeError):
    """Heartbeat 配置校验失败，必须拒绝服务启动（C-CONF-1）。"""
