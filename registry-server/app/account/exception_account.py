from enum import StrEnum
from typing import Any

from app.core.base_exception import AppError


class AccountErrorCode(StrEnum):
    """账户模块错误码。"""

    PHONE_ALREADY_REGISTERED = "PHONE_ALREADY_REGISTERED"
    USERNAME_ALREADY_TAKEN = "USERNAME_ALREADY_TAKEN"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    INCORRECT_PASSWORD = "INCORRECT_PASSWORD"  # nosec B105 - public error code, not a credential
    INVALID_VERIFICATION_CODE = "INVALID_VERIFICATION_CODE"
    ROLE_NOT_FOUND = "ROLE_NOT_FOUND"
    ROLES_NOT_FOUND = "ROLES_NOT_FOUND"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    INVALID_REFRESH_TOKEN = "INVALID_REFRESH_TOKEN"  # nosec B105 - public error code, not a credential
    EXPIRED_TOKEN = "EXPIRED_TOKEN"  # nosec B105 - public error code, not a credential
    INVALID_REQUEST = "INVALID_REQUEST"
    PASSWORD_COMPLEXITY_ERROR = "PASSWORD_COMPLEXITY_ERROR"  # nosec B105 - public error code, not a credential
    AIC_PROVIDER_CODE_INVALID = "AIC_PROVIDER_CODE_INVALID"
    AIC_PROVIDER_CODE_CONFLICT = "AIC_PROVIDER_CODE_CONFLICT"
    AIC_PROVIDER_CODE_IN_USE = "AIC_PROVIDER_CODE_IN_USE"


class AccountError(AppError):
    """账户相关异常的基类。"""

    def __init__(
        self,
        *,
        status_code: int = 400,
        code: str | AccountErrorCode | None = None,
        title: str | None = None,
        detail: str | None = None,
        input_params: dict[str, Any] | None = None,
        error_name: str | AccountErrorCode | None = None,
        error_msg: str | None = None,
    ) -> None:
        resolved_code = str(code or error_name or AccountErrorCode.INVALID_REQUEST)
        resolved_detail = detail or error_msg or "An error occurred with account operation"
        super().__init__(
            status_code=status_code,
            code=resolved_code,
            title=title,
            detail=resolved_detail,
            type_=f"urn:acps:error:account:{resolved_code.lower()}",
            extensions={
                "error_group": "account",
                "input_params": input_params or {},
            },
        )
