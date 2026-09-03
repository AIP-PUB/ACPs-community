"""项目级异常基座与全局异常处理器。

AMP 错误响应遵循 RFC 9457 Problem Details，扩展了 AMP spec 定义的 error_code 字段。
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"
AMP_ERROR_TYPE_PREFIX = "urn:acps:amp:error:"


class CoreErrorCode(StrEnum):
    """核心模块错误码。"""

    VALIDATION_FAILED = "VALIDATION_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def _default_error_title(code: str) -> str:
    """根据错误码生成默认标题。"""
    return code.replace("_", " ").title()


def build_problem_details(
    *,
    status: int,
    title: str,
    detail: str,
    type_: str,
    extensions: dict[str, object] | None = None,
) -> dict[str, object]:
    """构造符合 RFC 9457 的响应载荷。

    Args:
        status: HTTP 状态码。
        title: 问题简要标题。
        detail: 面向人的详细说明。
        type_: 稳定的问题类型标识符（URN）。
        extensions: 可选的项目扩展字段（如 error_code）。

    Returns:
        dict[str, object]: Problem Details 响应载荷。
    """
    payload: dict[str, object] = {
        "type": type_,
        "status": status,
        "title": title,
        "detail": detail,
    }
    if extensions:
        payload.update(extensions)
    return payload


def _to_json_safe(value: Any) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _to_json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_to_json_safe(item) for item in value]
    return str(value)


def _normalize_validation_errors(errors: Sequence[Any]) -> list[dict[str, object]]:
    normalized_errors: list[dict[str, object]] = []
    for error in errors:
        if isinstance(error, dict):
            normalized_errors.append({str(key): _to_json_safe(value) for key, value in error.items()})
            continue
        normalized_errors.append({"detail": _to_json_safe(error)})
    return normalized_errors


class AppError(Exception):
    """项目级业务异常基类。

    遵循 AMP spec 的 AMPErrorResponse 格式，对外响应为 RFC 9457 Problem Details。

    Args:
        code: 业务错误码（AMP error_code）。
        title: 错误标题。
        detail: 错误详情（英文，对外暴露）。
        status_code: HTTP 状态码。
        type_: RFC 9457 problem type URN。
        extensions: 可选扩展字段。
    """

    def __init__(
        self,
        *,
        code: str,
        title: str | None = None,
        detail: str,
        status_code: int = 400,
        type_: str | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.title = title or _default_error_title(code)
        self.detail = detail
        self.status_code = status_code
        self.type = type_ or f"{AMP_ERROR_TYPE_PREFIX}{code.lower().replace('_', '-')}"
        self.extensions = extensions or {}
        super().__init__(detail)

    def to_problem_details(self) -> dict[str, object]:
        """将异常转换为符合 RFC 9457 的响应载荷。

        Returns:
            dict[str, object]: AMP AMPErrorResponse 载荷。
        """
        return build_problem_details(
            status=self.status_code,
            title=self.title,
            detail=self.detail,
            type_=self.type,
            extensions={"error_code": self.code, **self.extensions},
        )


def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    """将项目级异常转换为 Problem Details 响应。

    Args:
        _request: 当前传入的 HTTP 请求。
        exc: 抛出的应用异常。

    Returns:
        JSONResponse: 序列化后的 Problem Details 响应。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_problem_details(),
        media_type=PROBLEM_JSON_MEDIA_TYPE,
    )


def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """将请求校验异常转换为 RFC 9457 Problem Details 格式。

    Args:
        _request: 当前传入的 HTTP 请求。
        exc: 抛出的校验异常。

    Returns:
        JSONResponse: 符合 RFC 9457 的错误响应。
    """
    return JSONResponse(
        status_code=422,
        content=build_problem_details(
            status=422,
            title="Request validation failed",
            detail="Request body, query, or path parameters failed validation.",
            type_=f"{AMP_ERROR_TYPE_PREFIX}validation:request-validation-failed",
            extensions={
                "error_code": CoreErrorCode.VALIDATION_FAILED,
                "errors": _normalize_validation_errors(exc.errors()),
            },
        ),
        media_type=PROBLEM_JSON_MEDIA_TYPE,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """注册项目级全局异常处理器。

    Args:
        app: FastAPI 应用实例。
    """
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
