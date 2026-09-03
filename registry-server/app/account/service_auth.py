from __future__ import annotations

import re
import secrets
import string
import uuid
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, cast, overload

import jwt
from fastapi import status
from jwt import PyJWTError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.account.exception_account import AccountError, AccountErrorCode
from app.account.model import Role, RoleType, User, VerificationCode
from app.account.service_account import (
    get_role_by_name_async,
    get_user_async,
    get_user_by_phone_async,
    get_user_by_username_async,
)
from app.core.auth import create_access_token, get_password_hash, verify_password
from app.core.config import settings
from app.utils.utils import get_beijing_time

if TYPE_CHECKING:
    from app.account.schema_auth import RegisterRequest

ROLE_NAME_COLUMN = cast("Any", Role.name)
VERIFICATION_CODE_PHONE_COLUMN = cast("Any", VerificationCode.phone)
VERIFICATION_CODE_EXPIRES_AT_COLUMN = cast("Any", VerificationCode.expires_at)


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


ACCESS_TOKEN_TYPE = TokenType.ACCESS
REFRESH_TOKEN_TYPE = TokenType.REFRESH


def generate_verification_code() -> str:
    """生成随机 6 位验证码。"""
    return "".join(secrets.choice(string.digits) for _ in range(6))


async def _get_verification_code_by_phone(session: AsyncSession, phone: str) -> VerificationCode | None:
    stmt = select(VerificationCode).where(phone == VERIFICATION_CODE_PHONE_COLUMN).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def store_verification_code(session: AsyncSession, phone: str, code: str, expires_in: int = 300) -> None:
    """在数据库中存储或更新验证码。"""

    now = get_beijing_time()
    expires_at = now + timedelta(seconds=expires_in)

    await session.execute(delete(VerificationCode).where(now >= VERIFICATION_CODE_EXPIRES_AT_COLUMN))
    existing_code = await _get_verification_code_by_phone(session, phone)

    if existing_code is None:
        session.add(
            VerificationCode(
                phone=phone,
                code=code,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        existing_code.code = code
        existing_code.expires_at = expires_at
        existing_code.updated_at = now
        session.add(existing_code)

    await session.flush()


async def verify_code(session: AsyncSession, phone: str, code: str) -> bool:
    """校验输入验证码是否与手机号对应的存储验证码一致。"""

    if settings.verification_code_bypass and code == settings.verification_code_bypass:
        # 测试环境可通过配置显式开启固定验证码绕过。
        return True

    verification_code = await _get_verification_code_by_phone(session, phone)
    if verification_code is None:
        return False

    now = get_beijing_time()
    if now > verification_code.expires_at:
        await session.delete(verification_code)
        await session.flush()
        return False

    if verification_code.code != code:
        return False

    await session.delete(verification_code)
    await session.flush()
    return True


async def send_verification_code(session: AsyncSession, phone: str) -> str:
    """
    发送验证码。

    真实场景下应通过短信服务发送；当前为了便于开发和测试，直接返回验证码。
    """
    code = generate_verification_code()
    await store_verification_code(session, phone, code)
    # 真实场景下这里应调用短信服务发送验证码
    return code


def validate_password_complexity(password: str) -> None:
    """
    校验密码复杂度。

    规则如下：
    1. 长度为 8 到 20 个字符
    2. 必须包含大写字母
    3. 必须包含小写字母
    4. 必须包含数字
    5. 必须包含特殊字符（除字母、数字和空格之外的字符）
    """
    if not (8 <= len(password) <= 20):
        raise AccountError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AccountErrorCode.PASSWORD_COMPLEXITY_ERROR,
            error_msg="Password must be between 8 and 20 characters",
        )

    if not re.search(r"[A-Z]", password):
        raise AccountError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AccountErrorCode.PASSWORD_COMPLEXITY_ERROR,
            error_msg="Password must contain at least one uppercase letter",
        )

    if not re.search(r"[a-z]", password):
        raise AccountError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AccountErrorCode.PASSWORD_COMPLEXITY_ERROR,
            error_msg="Password must contain at least one lowercase letter",
        )

    if not re.search(r"\d", password):
        raise AccountError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AccountErrorCode.PASSWORD_COMPLEXITY_ERROR,
            error_msg="Password must contain at least one number",
        )

    if not re.search(r"[^A-Za-z0-9\s]", password):
        raise AccountError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AccountErrorCode.PASSWORD_COMPLEXITY_ERROR,
            error_msg="Password must contain at least one special character",
        )


async def register_user(session: AsyncSession, payload: RegisterRequest) -> User:
    """在完成必要校验后注册新用户。"""
    phone = payload.phone
    code = payload.verify_code or ""
    username = payload.username
    password = payload.password

    if not (phone and code) and not (username and password):
        raise AccountError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AccountErrorCode.INVALID_REQUEST,
            error_msg="Either username/password or phone/verify_code must be provided",
            input_params={"username": username, "phone": phone},
        )

    # 用户名/密码注册路径（不经过手机号验证码校验）
    if not phone or not code:
        # 检查是否提供了用户名，以及用户名是否已被占用
        if not username:
            raise AccountError(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_name=AccountErrorCode.INVALID_REQUEST,
                error_msg="Username is required for registration without phone verification",
                input_params={"username": username},
            )

        existing_username = await get_user_by_username_async(session, username)
        if existing_username:
            raise AccountError(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_name=AccountErrorCode.USERNAME_ALREADY_TAKEN,
                error_msg="Username already taken",
                input_params={"username": username},
            )
    else:
        # 手机号验证码注册路径
        # 检查手机号是否已注册
        existing_user = await get_user_by_phone_async(session, phone)
        if existing_user:
            raise AccountError(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_name=AccountErrorCode.PHONE_ALREADY_REGISTERED,
                error_msg="Phone number already registered",
                input_params={"phone": phone},
            )

        # 仅在提供手机号时校验验证码
        if not await verify_code(session, phone, code):
            raise AccountError(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_name=AccountErrorCode.INVALID_VERIFICATION_CODE,
                error_msg="Invalid verification code",
                input_params={"phone": phone, "code": code},
            )

        # 若手机号注册同时提供了用户名，也需要校验用户名是否重复
        if username:
            existing_username = await get_user_by_username_async(session, username)
            if existing_username:
                raise AccountError(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    error_name=AccountErrorCode.USERNAME_ALREADY_TAKEN,
                    error_msg="Username already taken",
                    input_params={"username": username},
                )

    user_data = payload.model_dump(exclude={"verify_code", "password"}, exclude_none=True)

    # 如提供密码，则先进行哈希处理
    if password:
        user_data["hashed_password"] = get_password_hash(password)

    # 创建新用户
    user = User(**user_data)

    # 绑定默认 CLIENT 角色
    default_role = await get_role_by_name_async(session, RoleType.CLIENT)
    if not default_role:
        # 若默认角色不存在，则先创建
        default_role = Role(name=RoleType.CLIENT, description="Regular client")
        session.add(default_role)
        await session.flush()

    user.roles = [default_role]

    session.add(user)
    await session.flush()

    return user


@overload
async def authenticate_user(
    session: AsyncSession, username: str, password: str, raise_exception: Literal[True]
) -> User: ...


@overload
async def authenticate_user(
    session: AsyncSession, username: str, password: str, raise_exception: bool = False
) -> User | None: ...


async def authenticate_user(
    session: AsyncSession, username: str, password: str, raise_exception: bool = False
) -> User | None:
    """
    使用用户名和密码对用户进行认证。

    Args:
        session: 数据库会话
        username: 待认证的用户名
        password: 待校验的密码
        raise_exception: 认证失败时是否直接抛出异常

    Returns:
        认证成功时返回用户对象；若认证失败且 raise_exception 为 False，则返回 None

    Raises:
        AccountError: 当认证失败且 raise_exception 为 True 时抛出
    """
    user = await get_user_by_username_async(session, username)

    if not user:
        if raise_exception:
            raise AccountError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_name=AccountErrorCode.USER_NOT_FOUND,
                error_msg="User not found",
                input_params={"username": username},
            )
        return None

    if not user.is_active:
        if raise_exception:
            raise AccountError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_name=AccountErrorCode.INVALID_CREDENTIALS,
                error_msg="Incorrect username or password",
                input_params={"username": username},
            )
        return None

    if not user.hashed_password:
        if raise_exception:
            raise AccountError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_name=AccountErrorCode.INVALID_CREDENTIALS,
                error_msg="Incorrect username or password",
                input_params={"username": username},
            )
        return None

    # 渐进式迁移：bcrypt 哈希成功验证后自动更新为 argon2
    is_valid, needs_rehash = verify_password(password, user.hashed_password)
    if not is_valid:
        if raise_exception:
            raise AccountError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_name=AccountErrorCode.INVALID_CREDENTIALS,
                error_msg="Incorrect username or password",
                input_params={"username": username},
            )
        return None
    if needs_rehash:
        user.hashed_password = get_password_hash(password)
        session.add(user)

    return user


@overload
async def authenticate_by_phone(
    session: AsyncSession, phone: str, code: str, raise_exception: Literal[True]
) -> User: ...


@overload
async def authenticate_by_phone(
    session: AsyncSession, phone: str, code: str, raise_exception: bool = False
) -> User | None: ...


async def authenticate_by_phone(
    session: AsyncSession, phone: str, code: str, raise_exception: bool = False
) -> User | None:
    """
    使用手机号和验证码对用户进行认证。

    Args:
        session: 数据库会话
        phone: 待认证的手机号
        code: 待校验的验证码
        raise_exception: 认证失败时是否直接抛出异常

    Returns:
        认证成功时返回用户对象；若认证失败且 raise_exception 为 False，则返回 None

    Raises:
        AccountError: 当认证失败且 raise_exception 为 True 时抛出
    """
    user = await get_user_by_phone_async(session, phone)

    if not user or not user.is_active:
        if raise_exception:
            raise AccountError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_name=AccountErrorCode.INVALID_CREDENTIALS,
                error_msg="Invalid phone number or verification code",
                input_params={"phone": phone},
            )
        return None

    if not await verify_code(session, phone, code):
        if raise_exception:
            raise AccountError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_name=AccountErrorCode.INVALID_VERIFICATION_CODE,
                error_msg="Invalid verification code",
                input_params={"phone": phone, "code": code},
            )
        return None

    return user


def create_user_token(user: User) -> dict[str, str]:
    """为已认证用户创建 token，并更新用户模型中的 token 信息。

    Args:
        user: 已认证的用户模型。

    Returns:
        dict[str, str]: token 响应载荷。
    """
    access_expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    refresh_expires_delta = timedelta(minutes=settings.refresh_token_expire_minutes)
    # 使用北京时间计算过期时间
    expires_at = get_beijing_time() + access_expires_delta

    # 使用用户 ID 作为 subject
    token_data: dict[str, str | list[str]] = {"sub": str(user.id)}

    # 将角色信息写入 token
    user_roles = [str(role.name) for role in user.roles]
    token_data["roles"] = user_roles

    access_token = create_access_token(
        data={**token_data, "type": ACCESS_TOKEN_TYPE, "jti": str(uuid.uuid4())},
        expires_delta=access_expires_delta,
    )
    refresh_token = create_access_token(
        data={"sub": str(user.id), "type": REFRESH_TOKEN_TYPE, "jti": str(uuid.uuid4())},
        expires_delta=refresh_expires_delta,
    )

    # 将 token 信息回写到用户模型
    user.access_token = access_token
    user.refresh_token = refresh_token
    user.token_expires_at = expires_at
    user.updated_at = get_beijing_time()

    return {
        "access_token": access_token,
        "token_type": "bearer",  # nosec B105 - OAuth token type, not a credential
        "refresh_token": refresh_token,
        "expires_at": expires_at.isoformat(),  # Include expiration time in ISO format with timezone
    }


async def reset_password(session: AsyncSession, phone: str, code: str, new_password: str) -> bool:
    """在验证码校验通过后重置用户密码。"""
    user = await get_user_by_phone_async(session, phone)

    if not user:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="User not found",
            input_params={"phone": phone},
        )

    if not await verify_code(session, phone, code):
        raise AccountError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AccountErrorCode.INVALID_VERIFICATION_CODE,
            error_msg="Invalid verification code",
            input_params={"phone": phone, "code": code},
        )

    user.hashed_password = get_password_hash(new_password)
    user.updated_at = get_beijing_time()

    session.add(user)
    await session.flush()

    return True


@overload
async def refresh_access_token(
    session: AsyncSession, refresh_token: str, raise_exception: Literal[True]
) -> dict[str, str]: ...


@overload
async def refresh_access_token(
    session: AsyncSession, refresh_token: str, raise_exception: bool = False
) -> dict[str, str] | None: ...


async def refresh_access_token(
    session: AsyncSession, refresh_token: str, raise_exception: bool = False
) -> dict[str, str] | None:
    """
    使用 refresh token 刷新访问令牌。

    Args:
        session: 数据库会话
        refresh_token: 用于刷新的 refresh token
        raise_exception: 刷新失败时是否直接抛出异常

    Returns:
        刷新成功时返回新的 token 字典；若失败且 raise_exception 为 False，则返回 None

    Raises:
        AccountError: 当刷新失败且 raise_exception 为 True 时抛出
    """
    try:
        # 校验 refresh token
        payload = jwt.decode(refresh_token, settings.secret_key, algorithms=[settings.algorithm])

        user_id = payload.get("sub")
        token_type = payload.get("type")
        if not isinstance(user_id, str) or token_type != REFRESH_TOKEN_TYPE:
            if raise_exception:
                raise AccountError(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    error_name=AccountErrorCode.INVALID_REFRESH_TOKEN,
                    error_msg="Invalid refresh token",
                    input_params={"refresh_token": "***"},  # nosec B105 - redacted diagnostic placeholder
                )
            return None

        parsed_user_id = uuid.UUID(user_id)
    except PyJWTError, ValueError:
        if raise_exception:
            raise AccountError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_name=AccountErrorCode.INVALID_REFRESH_TOKEN,
                error_msg="Invalid refresh token",
                input_params={"refresh_token": "***"},  # nosec B105 - redacted diagnostic placeholder
            ) from None
        return None

    # 获取用户
    user = await get_user_async(session, parsed_user_id)
    if not user or not user.is_active:
        if raise_exception:
            raise AccountError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_name=AccountErrorCode.USER_NOT_FOUND,
                error_msg="User not found or inactive",
                input_params={"user_id": user_id},
            )
        return None

    # 校验传入的 refresh token 与数据库中保存的值一致
    if not user.refresh_token or user.refresh_token != refresh_token:
        if raise_exception:
            raise AccountError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_name=AccountErrorCode.INVALID_REFRESH_TOKEN,
                error_msg="Invalid refresh token",
                input_params={"refresh_token": "***"},  # nosec B105 - redacted diagnostic placeholder
            )
        return None

    # 生成新的 token
    return create_user_token(user)
