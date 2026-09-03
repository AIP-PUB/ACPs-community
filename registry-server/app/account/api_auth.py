from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.account.exception_auth import LocalAuthDisabledError
from app.account.schema_auth import (
    MessageResponse,
    OidcLogoutResponse,
    PhoneLoginRequest,
    RefreshTokenRequest,
    RegisterRequest,
    ResetPasswordRequest,
    Token,
    VerifyCodeRequest,
    VerifyCodeResponse,
)
from app.account.service_auth import (
    authenticate_by_phone,
    authenticate_user,
    create_user_token,
    refresh_access_token,
    register_user,
    reset_password,
    send_verification_code,
)
from app.core.auth import safe_get_current_user
from app.core.base_exception import PROBLEM_JSON_MEDIA_TYPE
from app.core.config import settings
from app.core.db_session import get_session
from app.core.oidc import get_keycloak_end_session_endpoint
from app.core.security import limiter
from app.utils.utils import get_beijing_time

if TYPE_CHECKING:
    from app.account.model import User

router = APIRouter(prefix="/auth", tags=["authentication"])

type SessionDep = Annotated[AsyncSession, Depends(get_session)]
type OAuthFormDep = Annotated[OAuth2PasswordRequestForm, Depends()]


def _to_token_response(token_data: dict[str, str]) -> Token:
    return Token.model_validate(token_data)


def _problem_response(description: str) -> dict[str, object]:
    return {"description": description, "content": {PROBLEM_JSON_MEDIA_TYPE: {}}}


BAD_REQUEST_RESPONSE = _problem_response("Invalid request")
CONFLICT_RESPONSE = _problem_response("Resource conflict")
NOT_FOUND_RESPONSE = _problem_response("Resource not found")
UNAUTHORIZED_RESPONSE = _problem_response("Authentication failed")
VALIDATION_RESPONSE = _problem_response("Request validation failed")
RATE_LIMIT_RESPONSE = _problem_response("Too many requests")
GONE_RESPONSE = _problem_response("Local authentication is disabled")


def _ensure_local_auth_enabled() -> None:
    if settings.oidc_enabled:
        raise LocalAuthDisabledError()


@router.post(
    "/verify-code",
    status_code=status.HTTP_200_OK,
    summary="发送短信验证码",
    responses={
        status.HTTP_410_GONE: GONE_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
        status.HTTP_429_TOO_MANY_REQUESTS: RATE_LIMIT_RESPONSE,
    },
)
@limiter.limit(settings.rate_limit_auth)
async def request_verification_code(request: Request, payload: VerifyCodeRequest, db: SessionDep) -> VerifyCodeResponse:
    """
    请求向指定手机号发送验证码。

    在生产环境中应发送短信；当前实现为了便于开发和测试，直接返回验证码。
    """
    del request
    _ensure_local_auth_enabled()
    code = await send_verification_code(db, payload.phone)
    return VerifyCodeResponse(message="Verification code sent", code=code)


@router.post(
    "/register",
    status_code=status.HTTP_200_OK,
    summary="注册新用户并签发令牌",
    responses={
        status.HTTP_400_BAD_REQUEST: BAD_REQUEST_RESPONSE,
        status.HTTP_410_GONE: GONE_RESPONSE,
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
        status.HTTP_429_TOO_MANY_REQUESTS: RATE_LIMIT_RESPONSE,
    },
)
@limiter.limit(settings.rate_limit_auth)
async def register_new_user(request: Request, payload: RegisterRequest, db: SessionDep) -> Token:
    """
    使用凭据注册新用户。

    同时支持用户名/密码注册和手机号验证码注册。
    """
    del request
    _ensure_local_auth_enabled()

    # 校验必须提供用户名/密码或手机号/验证码其中一组
    if not (payload.phone and payload.verify_code) and not (payload.username and payload.password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either username/password or phone/verify_code must be provided",
        )

    # 执行注册逻辑；所需异常由 service 层直接抛出
    user = await register_user(db, payload)

    # 创建并返回 token
    token = create_user_token(user)
    return _to_token_response(token)


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    summary="使用用户名和密码登录",
    responses={
        status.HTTP_410_GONE: GONE_RESPONSE,
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
        status.HTTP_429_TOO_MANY_REQUESTS: RATE_LIMIT_RESPONSE,
    },
)
@limiter.limit(settings.rate_limit_auth)
async def login_with_username_password(
    request: Request,
    form_data: OAuthFormDep,
    db: SessionDep,
) -> Token:
    """
    通过 OAuth2 兼容表单登录，并返回后续请求使用的访问令牌。
    """
    del request
    _ensure_local_auth_enabled()
    # 通过 raise_exception=True 让 service 层统一处理认证失败异常
    user = await authenticate_user(db, form_data.username, form_data.password, raise_exception=True)
    token = create_user_token(user)
    return _to_token_response(token)


@router.post(
    "/login-phone",
    status_code=status.HTTP_200_OK,
    summary="使用手机号验证码登录",
    responses={
        status.HTTP_410_GONE: GONE_RESPONSE,
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
        status.HTTP_429_TOO_MANY_REQUESTS: RATE_LIMIT_RESPONSE,
    },
)
@limiter.limit(settings.rate_limit_auth)
async def login_with_phone(request: Request, payload: PhoneLoginRequest, db: SessionDep) -> Token:
    """
    使用手机号和验证码登录。
    """
    del request
    _ensure_local_auth_enabled()
    # 通过 raise_exception=True 让 service 层统一处理认证失败异常
    user = await authenticate_by_phone(db, payload.phone, payload.verify_code, raise_exception=True)
    token = create_user_token(user)
    return _to_token_response(token)


@router.post(
    "/reset-password",
    status_code=status.HTTP_200_OK,
    summary="使用验证码重置密码",
    responses={
        status.HTTP_400_BAD_REQUEST: BAD_REQUEST_RESPONSE,
        status.HTTP_410_GONE: GONE_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
        status.HTTP_429_TOO_MANY_REQUESTS: RATE_LIMIT_RESPONSE,
    },
)
@limiter.limit(settings.rate_limit_auth)
async def reset_user_password(
    request: Request,
    payload: ResetPasswordRequest,
    db: SessionDep,
) -> MessageResponse:
    """
    使用手机号验证码重置用户密码。
    """
    del request
    _ensure_local_auth_enabled()
    # 所需异常由 service 层直接抛出
    await reset_password(db, payload.phone, payload.verify_code, payload.new_password)

    return MessageResponse(message="Password reset successfully")


@router.post(
    "/logout",
    response_model=OidcLogoutResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_200_OK,
    summary="注销当前登录态",
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def logout(
    current_user: Annotated[User | None, Depends(safe_get_current_user)],
    db: SessionDep,
) -> OidcLogoutResponse:
    """
    通过清除已保存的 token 状态来登出当前用户。

    即使当前未登录，也允许调用登出接口。
    """
    if settings.oidc_enabled:
        del current_user, db
        return OidcLogoutResponse(
            success=True,
            message="Use the Keycloak end-session endpoint to complete logout",
            end_session_endpoint=get_keycloak_end_session_endpoint(),
        )

    if current_user:
        # 清空用户模型中的 token 字段
        current_user.access_token = None
        current_user.refresh_token = None
        current_user.token_expires_at = None
        current_user.updated_at = get_beijing_time()

        # 保存变更到数据库
        db.add(current_user)

    # 即使未登录也统一返回成功
    return OidcLogoutResponse(success=True, message="Successfully logged out")


@router.post(
    "/refresh-token",
    status_code=status.HTTP_200_OK,
    summary="使用刷新令牌换取新令牌",
    responses={
        status.HTTP_410_GONE: GONE_RESPONSE,
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
        status.HTTP_429_TOO_MANY_REQUESTS: RATE_LIMIT_RESPONSE,
    },
)
@limiter.limit(settings.rate_limit_auth)
async def refresh_token(request: Request, payload: RefreshTokenRequest, db: SessionDep) -> Token:
    """
    使用 refresh token 刷新访问令牌。
    """
    del request
    _ensure_local_auth_enabled()
    # 通过 raise_exception=True 让 service 层统一处理刷新失败异常
    token = await refresh_access_token(db, payload.refresh_token, raise_exception=True)
    return _to_token_response(token)
