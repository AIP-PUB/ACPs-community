import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, cast

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from jwt import PyJWTError
from passlib.context import CryptContext
from passlib.exc import PasswordSizeError, UnknownHashError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.account.exception_auth import (
    AuthUserNotFoundError,
    ExpiredTokenError,
    InactiveUserError,
    InsufficientPermissionsError,
    TokenValidationError,
)
from app.account.model import RoleType, User
from app.account.service_oidc import get_or_create_user_from_principal, sync_user_email
from app.core.config import settings
from app.core.db_session import get_session
from app.core.oidc import validate_access_token, validate_optional_access_token
from app.utils.utils import get_beijing_time

# argon2 hasher（新用户默认）
_ph = PasswordHasher()

# bcrypt context（仅用于验证旧哈希；新哈希始终使用 argon2）
_bcrypt_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_v1_str}/auth/login")
USER_ID_COLUMN = cast("Any", User.id)
USER_ROLES_RELATIONSHIP = cast("Any", User.roles)


def verify_password(plain_password: str, hashed_password: str) -> tuple[bool, bool]:
    """验证密码，并指示是否需要重新哈希（渐进式迁移）。

    先尝试 argon2 验证；若哈希格式不兼容（旧 bcrypt 哈希），则回退到 bcrypt 验证。
    bcrypt 验证成功后返回 needs_rehash=True，调用方应将密码更新为 argon2 哈希。

    Returns:
        tuple[bool, bool]: (is_valid, needs_rehash)
            - is_valid: 密码是否正确
            - needs_rehash: 是否需要将哈希更新为 argon2 格式
    """
    try:
        _ph.verify(hashed_password, plain_password)
        # argon2 验证成功；检查是否需要更新参数
        needs_rehash = _ph.check_needs_rehash(hashed_password)
        return True, needs_rehash
    except VerifyMismatchError:
        # argon2 格式正确但密码错误
        return False, False
    except InvalidHashError:
        pass  # 哈希格式不是 argon2，尝试 bcrypt 兜底

    # bcrypt 兜底（旧哈希）
    try:
        if _bcrypt_ctx.verify(plain_password, hashed_password):
            return True, True  # 密码正确，但需要迁移到 argon2
        return False, False
    except PasswordSizeError, UnknownHashError, ValueError, TypeError:
        return False, False


def get_password_hash(password: str) -> str:
    """使用 argon2 生成密码哈希，不再生成新的 bcrypt 哈希。"""
    return _ph.hash(password)


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """创建新的 JWT token。"""
    to_encode = data.copy()
    # 使用北京时间作为基准计算过期时间
    expire = get_beijing_time() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return str(jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm))


async def _get_user_with_roles(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """加载用户及其角色关系，避免认证流程中触发异步 lazy-load。"""
    stmt = select(User).options(selectinload(USER_ROLES_RELATIONSHIP)).where(user_id == USER_ID_COLUMN)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_current_local_user(token: str, session: AsyncSession) -> User:
    """从本地 JWT token 解析当前用户，并校验数据库中的已保存 token 状态。"""
    credentials_exception = TokenValidationError()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        token_type = payload.get("type")
        if token_type not in (None, "access"):
            raise credentials_exception
        subject = payload.get("sub")
        if not isinstance(subject, str):
            raise credentials_exception
        user_id = uuid.UUID(subject)
    except PyJWTError, ValueError:
        raise credentials_exception from None

    # 获取用户
    user = await _get_user_with_roles(session, user_id)
    if user is None:
        raise AuthUserNotFoundError(user_id=str(user_id))

    # 检查用户是否处于激活状态
    if not user.is_active:
        raise InactiveUserError(user_id=str(user_id))

    # 校验数据库中保存的 token 与当前 token 一致
    if not user.access_token or user.access_token != token:
        raise credentials_exception

    # 根据数据库中保存的过期时间校验 token 是否过期
    if user.token_expires_at and get_beijing_time() > user.token_expires_at:
        raise ExpiredTokenError()

    return user


async def get_current_user(token: str = Depends(oauth2_scheme), session: AsyncSession = Depends(get_session)) -> User:
    """根据当前认证模式解析当前用户。"""
    if not settings.oidc_enabled:
        return await get_current_local_user(token, session)

    principal = await validate_access_token(token)
    user = await get_or_create_user_from_principal(session, principal)
    await sync_user_email(session, user, principal)
    return user


def get_optional_token(request: Request) -> str | None:
    """
    获取 Authorization header，如果没有则返回 None。
    """
    auth = request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


async def safe_get_current_user(
    token: str | None = Depends(get_optional_token), session: AsyncSession = Depends(get_session)
) -> User | None:
    """
    与 get_current_user 类似，但在未登录或 token 无效/过期时返回 None。
    """
    if not token:
        return None
    if settings.oidc_enabled:
        principal = await validate_optional_access_token(token)
        if principal is None:
            return None
        user = await get_or_create_user_from_principal(session, principal)
        await sync_user_email(session, user, principal)
        return user
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        token_type = payload.get("type")
        if token_type not in (None, "access"):
            return None
        subject = payload.get("sub")
        if not isinstance(subject, str):
            return None
        user_id = uuid.UUID(subject)
    except PyJWTError, ValueError:
        return None
    local_user = await _get_user_with_roles(session, user_id)
    if local_user is None or not local_user.is_active:
        return None
    if not local_user.access_token or local_user.access_token != token:
        return None
    if local_user.token_expires_at and get_beijing_time() > local_user.token_expires_at:
        return None
    return local_user


def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    """检查当前用户是否处于激活状态。"""
    if not current_user.is_active:
        raise InactiveUserError(user_id=str(current_user.id))
    return current_user


def check_user_role(required_roles: list[str | RoleType]) -> Callable[..., Awaitable[User]]:
    """检查当前用户是否具备所需角色。"""

    async def _check_user_role(current_user: User = Depends(get_current_user)) -> User:
        # 检查用户角色中是否至少有一个命中所需角色
        user_roles = [role.name for role in current_user.roles]
        if not any(role in required_roles for role in user_roles):
            raise InsufficientPermissionsError(
                user_id=str(current_user.id),
                user_roles=user_roles,
                required_roles=required_roles,
            )
        return current_user

    return _check_user_role
