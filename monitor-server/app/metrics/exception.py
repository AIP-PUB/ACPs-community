"""app/metrics/exception.py — Metrics 模块错误码与异常体系。

遵循 exception.instructions：每个错误码一个 AppError 子类，HTTP status/error_code/detail 在构造器固定。
内部异常（UntimedMetricsError / RemoteWriteError / MetricsConfigError）不继承 AppError，不对外暴露。
"""

from __future__ import annotations

from enum import StrEnum

from app.core.base_exception import AppError

# ── Metrics 专有错误码（spec §6.3.4 + 公共 §6.1.5） ───────────────────────────


class MetricsErrorCode(StrEnum):
    """Metrics API 错误码。"""

    SLO_RULE_INVALID = "AMP_SLO_RULE_INVALID"
    METRIC_UNSUPPORTED = "AMP_METRIC_UNSUPPORTED"
    STEP_TOO_FINE = "AMP_STEP_TOO_FINE"
    INVALID_TIME_RANGE = "AMP_INVALID_TIME_RANGE"
    INVALID_FILTER = "AMP_INVALID_FILTER"
    UNSUPPORTED_FIELD = "AMP_UNSUPPORTED_FIELD"
    UNSUPPORTED_OPERATOR = "AMP_UNSUPPORTED_OPERATOR"
    OUT_OF_RETENTION = "AMP_OUT_OF_RETENTION"
    CURSOR_INVALID = "AMP_CURSOR_INVALID"
    READ_MODEL_LAGGING = "AMP_READ_MODEL_LAGGING"


# ── AppError 子类（API 层可见，每类 HTTP 状态码固定） ────────────────────────────


class SLORuleInvalidError(AppError):
    """SLO 规则非法（400）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=MetricsErrorCode.SLO_RULE_INVALID,
            detail=detail,
            status_code=400,
        )


class MetricUnsupportedError(AppError):
    """不支持的公共 metric 名（422）。"""

    def __init__(self, metric: str) -> None:
        super().__init__(
            code=MetricsErrorCode.METRIC_UNSUPPORTED,
            detail=f"Metric '{metric}' is not supported. Use a public metric name from the catalog.",
            status_code=422,
        )


class StepTooFineError(AppError):
    """请求步长过细或超出点数限制（422）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=MetricsErrorCode.STEP_TOO_FINE,
            detail=detail,
            status_code=422,
        )


class InvalidTimeRangeError(AppError):
    """无效时间范围（400）。"""

    def __init__(self, detail: str = "The specified time range is invalid (start must be before end).") -> None:
        super().__init__(
            code=MetricsErrorCode.INVALID_TIME_RANGE,
            detail=detail,
            status_code=400,
        )


class InvalidFilterError(AppError):
    """无效过滤器（400）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=MetricsErrorCode.INVALID_FILTER,
            detail=detail,
            status_code=400,
        )


class UnsupportedFieldError(AppError):
    """不支持的字段（422）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=MetricsErrorCode.UNSUPPORTED_FIELD,
            detail=detail,
            status_code=422,
        )


class UnsupportedOperatorError(AppError):
    """不支持的过滤器运算符（422）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=MetricsErrorCode.UNSUPPORTED_OPERATOR,
            detail=detail,
            status_code=422,
        )


class OutOfRetentionError(AppError):
    """查询时间范围超出保留窗口（422）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            code=MetricsErrorCode.OUT_OF_RETENTION,
            detail=detail,
            status_code=422,
        )


class CursorInvalidError(AppError):
    """游标无效或与当前查询参数不匹配（400）。"""

    def __init__(self, detail: str = "Cursor is invalid or does not match current query parameters.") -> None:
        super().__init__(
            code=MetricsErrorCode.CURSOR_INVALID,
            detail=detail,
            status_code=400,
        )


class ReadModelLaggingError(AppError):
    """读模型滞后，查询结果不可信（503）。"""

    def __init__(self, detail: str = "Metrics read model is lagging. Please retry later.") -> None:
        super().__init__(
            code=MetricsErrorCode.READ_MODEL_LAGGING,
            detail=detail,
            status_code=503,
        )


# ── 内部异常（不继承 AppError，不对外暴露为 HTTP 错误） ────────────────────────


class UntimedMetricsError(Exception):
    """Metrics 记录缺少可用时间戳（LogAppendTime 与 observed_timestamp 均不可用）。

    Writer 遇到此异常时不重试，直接路由 DLQ（§2.3 时间优先级规则）。
    """


class RemoteWriteError(Exception):
    """VictoriaMetrics Remote Write 失败（非 2xx 响应）。

    Writer 遇到此异常时不刷新 snapshot cache、不推进水位，让消息重投递（C-METRIC-WRITE-1）。
    """


class MetricsConfigError(Exception):
    """Metrics 配置校验失败。

    runtime.validate_metrics_config() 检出非法配置时抛此异常；进程启动拒绝继续（§6.18）。
    """


__all__ = [
    "CursorInvalidError",
    "InvalidFilterError",
    "InvalidTimeRangeError",
    "MetricUnsupportedError",
    "MetricsConfigError",
    "MetricsErrorCode",
    "OutOfRetentionError",
    "ReadModelLaggingError",
    "RemoteWriteError",
    "SLORuleInvalidError",
    "StepTooFineError",
    "UnsupportedFieldError",
    "UnsupportedOperatorError",
    "UntimedMetricsError",
]
