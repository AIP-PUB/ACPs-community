from __future__ import annotations

import secrets
import string
import uuid
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from fastapi import status
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, selectinload

from app.account.exception_account import AccountError, AccountErrorCode
from app.account.model import Role, RoleType, User
from app.agent.exception import AgentError, AgentErrorCode
from app.agent.model import Agent, EmailCode
from app.agent.smtp import send_password
from app.core.auth import get_password_hash, verify_password
from app.utils.aic import generate_aic_provider_code, normalize_aic_level_code
from app.utils.utils import get_beijing_time

if TYPE_CHECKING:
    from app.account.schema_account import UserCreate, UserUpdate

USER_ID_COLUMN = cast("Any", User.id)
USER_USERNAME_COLUMN = cast("Any", User.username)
USER_PHONE_COLUMN = cast("Any", User.phone)
USER_NAME_COLUMN = cast("Any", User.name)
USER_EMAIL_COLUMN = cast("Any", User.email)
USER_IS_ACTIVE_COLUMN = cast("Any", User.is_active)
USER_ROLES_RELATIONSHIP = cast("Any", User.roles)
ROLE_ID_COLUMN = cast("Any", Role.id)
ROLE_NAME_COLUMN = cast("Any", Role.name)
EMAIL_CODE_COLUMN = cast("Any", EmailCode.code)
EMAIL_EMAIL_COLUMN = cast("Any", EmailCode.email)
EMAIL_EXPIRES_AT_COLUMN = cast("Any", EmailCode.expires_at)
EMAIL_CODE_USED_AT_COLUMN = cast("Any", EmailCode.used_at)
EMAIL_CREATED_COLUMN = cast("Any", EmailCode.created_at)
AGENT_ID_COLUMN = cast("Any", Agent.id)
AGENT_CREATED_BY_ID_COLUMN = cast("Any", Agent.created_by_id)
AGENT_AIC_COLUMN = cast("Any", Agent.aic)
AGENT_IS_DELETED_COLUMN = cast("Any", Agent.is_deleted)
AIC_PROVIDER_CODE_ALLOC_RETRIES = 8


def _existing_aic_provider_code(user: User) -> str | None:
    raw = user.aic_provider_code
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    return normalize_aic_level_code(stripped)


def _aic_owner_not_found_error(user_id: uuid.UUID) -> AgentError:
    return AgentError(
        status_code=status.HTTP_409_CONFLICT,
        error_name=AgentErrorCode.AIC_OWNER_NOT_FOUND,
        error_msg="Agent owner not found while assigning AIC provider code",
        input_params={"user_id": str(user_id)},
    )


def _aic_provider_code_alloc_failed_error(user_id: uuid.UUID) -> AgentError:
    return AgentError(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error_name=AgentErrorCode.GENERATE_AIC_FAILED,
        error_msg="Failed to allocate a unique AIC provider code",
        input_params={"user_id": str(user_id)},
    )


def _user_aic_provider_lock_stmt(user_id: uuid.UUID) -> Any:
    return (
        select(User)
        .options(selectinload(USER_ROLES_RELATIONSHIP))
        .where(user_id == USER_ID_COLUMN)
        .limit(1)
        .with_for_update()
    )


async def lock_user_for_aic_provider_code_async(session: AsyncSession, user_id: uuid.UUID) -> User:
    """锁定用户行以便分配或改写 AIC 第7级。"""
    result = await session.execute(_user_aic_provider_lock_stmt(user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise _aic_owner_not_found_error(user_id)
    return cast("User", user)


def lock_user_for_aic_provider_code(db: Session, user_id: uuid.UUID) -> User:
    """锁定用户行以便分配或改写 AIC 第7级（同步路径）。"""
    result = db.execute(_user_aic_provider_lock_stmt(user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise _aic_owner_not_found_error(user_id)
    return cast("User", user)


async def ensure_user_aic_provider_code_async(session: AsyncSession, user_id: uuid.UUID) -> str:
    """返回账户第7级序号；为空则在当前事务中随机分配并写入。"""
    user = await lock_user_for_aic_provider_code_async(session, user_id)
    existing = _existing_aic_provider_code(user)
    if existing is not None:
        return existing

    for _ in range(AIC_PROVIDER_CODE_ALLOC_RETRIES):
        code = generate_aic_provider_code()
        user.aic_provider_code = code
        try:
            async with session.begin_nested():
                session.add(user)
                await session.flush()
            return code
        except IntegrityError:
            user.aic_provider_code = None
            continue

    raise _aic_provider_code_alloc_failed_error(user_id)


def ensure_user_aic_provider_code(db: Session, user_id: uuid.UUID) -> str:
    """返回账户第7级序号；为空则在当前事务中随机分配并写入（同步路径）。"""
    user = lock_user_for_aic_provider_code(db, user_id)
    existing = _existing_aic_provider_code(user)
    if existing is not None:
        return existing

    for _ in range(AIC_PROVIDER_CODE_ALLOC_RETRIES):
        code = generate_aic_provider_code()
        user.aic_provider_code = code
        try:
            with db.begin_nested():
                db.add(user)
                db.flush()
            return code
        except IntegrityError:
            user.aic_provider_code = None
            continue

    raise _aic_provider_code_alloc_failed_error(user_id)


async def _user_has_in_use_aic_async(session: AsyncSession, user_id: uuid.UUID) -> bool:
    stmt = (
        select(AGENT_ID_COLUMN)
        .where(user_id == AGENT_CREATED_BY_ID_COLUMN)
        .where(AGENT_AIC_COLUMN.is_not(None))
        .where(AGENT_IS_DELETED_COLUMN.is_(False))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def set_user_aic_provider_code(session: AsyncSession, user_id: uuid.UUID, raw_code: str) -> User:
    """管理员预置或改写账户 AIC 第7级。有在用 AIC 时禁止改号。"""
    result = await session.execute(_user_aic_provider_lock_stmt(user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="User not found",
            input_params={"user_id": str(user_id)},
        )
    locked_user = cast("User", user)

    try:
        normalized = normalize_aic_level_code(raw_code)
    except ValueError as exc:
        raise AccountError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            error_name=AccountErrorCode.AIC_PROVIDER_CODE_INVALID,
            title="Invalid AIC Provider Code",
            detail="AIC provider code must be 1 to 6 Base36 characters and must not be all zeros",
            input_params={"user_id": str(user_id)},
        ) from exc

    existing = _existing_aic_provider_code(locked_user)
    if existing == normalized:
        return locked_user

    if await _user_has_in_use_aic_async(session, user_id):
        raise AccountError(
            status_code=status.HTTP_409_CONFLICT,
            error_name=AccountErrorCode.AIC_PROVIDER_CODE_IN_USE,
            title="AIC Provider Code In Use",
            detail="Delete all in-use agents before changing the AIC provider code",
            input_params={"user_id": str(user_id)},
        )

    locked_user.aic_provider_code = normalized
    session.add(locked_user)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise AccountError(
            status_code=status.HTTP_409_CONFLICT,
            error_name=AccountErrorCode.AIC_PROVIDER_CODE_CONFLICT,
            title="AIC Provider Code Conflict",
            detail="AIC provider code is already assigned to another account",
            input_params={"user_id": str(user_id)},
        ) from exc
    return locked_user


@overload
async def get_user_async(session: AsyncSession, user_id: uuid.UUID, raise_exception: Literal[True]) -> User: ...


@overload
async def get_user_async(session: AsyncSession, user_id: uuid.UUID, raise_exception: bool = False) -> User | None: ...


async def get_user_async(session: AsyncSession, user_id: uuid.UUID, raise_exception: bool = False) -> User | None:
    """根据 ID 获取用户，并为异步请求路径预加载角色关系。"""
    stmt = select(User).options(selectinload(USER_ROLES_RELATIONSHIP)).where(user_id == USER_ID_COLUMN).limit(1)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user and raise_exception:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="User not found",
            input_params={"user_id": str(user_id)},
        )

    return user


async def get_user_by_username_async(session: AsyncSession, username: str) -> User | None:
    """根据用户名获取用户，并为异步认证流程预加载角色关系。"""
    stmt = select(User).options(selectinload(USER_ROLES_RELATIONSHIP)).where(username == USER_USERNAME_COLUMN).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_by_phone_async(session: AsyncSession, phone: str) -> User | None:
    """根据手机号获取用户，并为异步认证流程预加载角色关系。"""
    stmt = select(User).options(selectinload(USER_ROLES_RELATIONSHIP)).where(phone == USER_PHONE_COLUMN).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_role_by_name_async(session: AsyncSession, name: str | RoleType) -> Role | None:
    """为异步认证流程根据角色名获取角色。"""
    stmt = select(Role).where(name == ROLE_NAME_COLUMN).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


@overload
async def get_user(session: AsyncSession, user_id: uuid.UUID, raise_exception: Literal[True]) -> User: ...


@overload
async def get_user(session: AsyncSession, user_id: uuid.UUID, raise_exception: bool = False) -> User | None: ...


async def get_user(session: AsyncSession, user_id: uuid.UUID, raise_exception: bool = False) -> User | None:
    """根据 ID 获取用户。"""
    return await get_user_async(session, user_id, raise_exception=raise_exception)


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    """根据用户名获取用户。"""
    return await get_user_by_username_async(session, username)


async def get_user_by_phone(session: AsyncSession, phone: str) -> User | None:
    """根据手机号获取用户。"""
    return await get_user_by_phone_async(session, phone)


def _build_user_filter_clauses(
    *,
    username: str | None,
    phone: str | None,
    name: str | None,
    role: str | None,
    is_active: bool | None,
) -> tuple[list[Any], bool]:
    """构建可复用的用户筛选条件，供列表和计数查询共用。"""
    clauses: list[Any] = []

    if username:
        clauses.append(USER_USERNAME_COLUMN.ilike(f"%{username}%"))
    if phone:
        clauses.append(USER_PHONE_COLUMN.ilike(f"%{phone}%"))
    if name:
        clauses.append(USER_NAME_COLUMN.ilike(f"%{name}%"))
    if role:
        clauses.append(role == ROLE_NAME_COLUMN)
    if is_active is not None:
        clauses.append(is_active == USER_IS_ACTIVE_COLUMN)

    return clauses, role is not None


async def get_users(
    session: AsyncSession,
    page: int,
    page_size: int,
    username: str | None = None,
    phone: str | None = None,
    name: str | None = None,
    role: str | None = None,
    is_active: bool | None = True,
) -> tuple[list[User], int]:
    """获取用户列表，并支持可选筛选条件。"""
    clauses, requires_role_join = _build_user_filter_clauses(
        username=username,
        phone=phone,
        name=name,
        role=role,
        is_active=is_active,
    )

    skip = (page - 1) * page_size

    users_stmt = select(User).options(selectinload(USER_ROLES_RELATIONSHIP))
    if requires_role_join:
        users_stmt = users_stmt.join(USER_ROLES_RELATIONSHIP).distinct(USER_ID_COLUMN)
    if clauses:
        users_stmt = users_stmt.where(*clauses)
    users_stmt = users_stmt.offset(skip).limit(page_size)

    count_stmt = select(func.count()).select_from(User)
    if requires_role_join:
        count_stmt = select(func.count(func.distinct(USER_ID_COLUMN))).select_from(User).join(USER_ROLES_RELATIONSHIP)
    if clauses:
        count_stmt = count_stmt.where(*clauses)

    users_result = await session.execute(users_stmt)
    total_result = await session.execute(count_stmt)

    users = list(users_result.scalars().all())
    total = int(total_result.scalar_one())

    return users, total


async def create_user(session: AsyncSession, user_input: UserCreate) -> User:
    """创建新用户。"""
    from app.account import service_auth as auth_service

    user_data = user_input.model_dump(exclude={"roles"})
    role_names = user_input.roles

    # 检查用户名是否已被占用
    if user_input.username:
        existing_user = await get_user_by_username(session, user_input.username)
        if existing_user:
            raise AccountError(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_name=AccountErrorCode.USERNAME_ALREADY_TAKEN,
                error_msg="Username already taken",
                input_params={"username": user_input.username},
            )

    # 检查手机号是否已注册
    if user_input.phone:
        existing_user = await get_user_by_phone(session, user_input.phone)
        if existing_user:
            raise AccountError(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_name=AccountErrorCode.PHONE_ALREADY_REGISTERED,
                error_msg="Phone number already registered",
                input_params={"phone": user_input.phone},
            )

    # 如提供密码，则先进行哈希处理
    if password := user_input.password:
        auth_service.validate_password_complexity(password)
        user_data["hashed_password"] = get_password_hash(password)

    # 在写入前移除明文密码
    user_data.pop("password", None)

    # 创建用户对象
    user = User(**user_data)

    # 根据传入角色名绑定角色；未提供时默认绑定 CLIENT
    if role_names:
        roles_result = await session.execute(select(Role).where(ROLE_NAME_COLUMN.in_(role_names)))
        roles = list(roles_result.scalars().all())
        if len(roles) != len(role_names):
            raise AccountError(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_name=AccountErrorCode.ROLES_NOT_FOUND,
                error_msg="One or more roles not found",
                input_params={"role_names": role_names},
            )
        user.roles = roles
    else:
        default_role = await get_role_by_name(session, RoleType.CLIENT)
        if not default_role:
            default_role = Role(name=RoleType.CLIENT, description="Regular user")
            session.add(default_role)
            await session.flush()
        user.roles = [default_role]

    session.add(user)
    await session.flush()

    return user


async def update_user(session: AsyncSession, user_id: uuid.UUID, user_data: UserUpdate) -> User:
    """更新用户信息。"""
    user = await get_user(session, user_id)

    if not user:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="User not found",
            input_params={"user_id": str(user_id)},
        )

    # 判断是需要验证验证码
    email = user_data.email
    code = user_data.email_code
    if email and user.email != email:
        current_time = get_beijing_time()
        if not code:
            raise AccountError(
                status_code=status.HTTP_409_CONFLICT,
                error_name="验证码不能为空",
                error_msg="验证码不能为空",
                input_params={"user_id": str(user_id)},
            )
        stmt = (
            select(EmailCode)
            .where(email == EMAIL_EMAIL_COLUMN)
            .where(current_time < EMAIL_EXPIRES_AT_COLUMN)
            .where(func.lower(EMAIL_CODE_COLUMN) == code.lower())
            .where(EMAIL_CODE_USED_AT_COLUMN.is_(None))
            .order_by(desc(EMAIL_CREATED_COLUMN))
            .limit(1)
        )
        result = await session.execute(stmt)
        latest_code = result.scalar_one_or_none()
        if not latest_code:
            raise AccountError(
                status_code=status.HTTP_404_NOT_FOUND,
                error_name="验证码不正确",
                error_msg="验证码不正确",
                input_params={"code": str(code)},
            )

    update_data = user_data.model_dump(exclude_unset=True)

    # 更新用户字段
    for key, value in update_data.items():
        if hasattr(user, key) and value is not None:
            setattr(user, key, value)

    # 使用北京时间更新更新时间戳
    user.updated_at = get_beijing_time()

    session.add(user)
    await session.flush()

    return user


async def update_user_password_by_code(
    session: AsyncSession,
    email: str,
    code: str,
    password: str,
) -> bool:
    """修改密码通过验证码"""
    # 验证邮箱是否正确
    # 1.通过邮箱和验证码获取最新的一条
    current_time = get_beijing_time()

    stmt = (
        select(EmailCode)
        .where(email == EMAIL_EMAIL_COLUMN)
        .where(current_time < EMAIL_EXPIRES_AT_COLUMN)
        # .where(func.lower(EmailCode.code) == code.lower())
        .where(EMAIL_CODE_COLUMN.ilike(code))
        .where(EMAIL_CODE_USED_AT_COLUMN.is_(None))
        .order_by(desc(EMAIL_CREATED_COLUMN))
        .limit(1)
    )
    result = await session.execute(stmt)
    latest_code = result.scalar_one_or_none()
    if not latest_code:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name="验证码不正确",
            error_msg="验证码不正确",
            input_params={"code": str(code)},
        )
    # 通过邮箱查询用户
    # users = db.query(User).filter(User.email == email).limit(2).all()
    user_stmt = select(User).where(email == USER_EMAIL_COLUMN).limit(2)
    result = await session.execute(user_stmt)
    users = result.scalars().all()
    if not users:
        raise AccountError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="用户不存在",
            input_params={"code": str(code)},
        )
    if len(users) > 1:
        raise AccountError(
            status_code=status.HTTP_409_CONFLICT,
            error_name="存在多个用户",
            error_msg="存在多个用户",
            input_params={"code": str(code)},
        )
    # 修改用户密码
    from app.account import service_auth as auth_service

    user = users[0]
    auth_service.validate_password_complexity(password)
    user.hashed_password = get_password_hash(password)
    user.updated_at = get_beijing_time()

    session.add(user)
    # 修改验证码
    latest_code.used_at = current_time
    session.add(latest_code)
    await session.commit()
    return True


async def update_user_password(session: AsyncSession, user_id: uuid.UUID, old_password: str, new_password: str) -> bool:
    """更新用户密码。"""
    from app.account import service_auth as auth_service

    user = await get_user(session, user_id)

    if not user:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="User not found",
            input_params={"user_id": str(user_id)},
        )

    # 校验旧密码
    hashed_password = user.hashed_password
    if hashed_password is None:
        raise AccountError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AccountErrorCode.INCORRECT_PASSWORD,
            error_msg="Incorrect password",
            input_params={"user_id": str(user_id)},
        )
    is_valid, _ = verify_password(old_password, hashed_password)
    if not is_valid:
        raise AccountError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AccountErrorCode.INCORRECT_PASSWORD,
            error_msg="Incorrect password",
            input_params={"user_id": str(user_id)},
        )

    # 更新密码
    auth_service.validate_password_complexity(new_password)
    user.hashed_password = get_password_hash(new_password)
    user.updated_at = get_beijing_time()

    session.add(user)
    await session.flush()

    return True


async def update_user_phone(session: AsyncSession, user_id: uuid.UUID, new_phone: str, code: str) -> bool:
    """更新用户手机号（需要验证码校验）。"""
    from app.account import service_auth as auth_service

    user = await get_user(session, user_id)

    if not user:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="User not found",
            input_params={"user_id": str(user_id)},
        )

    # 检查手机号是否已被其他用户占用
    existing_user = await get_user_by_phone(session, new_phone)
    if existing_user:
        raise AccountError(
            status_code=status.HTTP_409_CONFLICT,
            error_name=AccountErrorCode.PHONE_ALREADY_REGISTERED,
            error_msg="Phone number already registered",
            input_params={"phone": new_phone},
        )

    # 校验新手机号对应的验证码
    if not await auth_service.verify_code(session, new_phone, code):
        raise AccountError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AccountErrorCode.INVALID_VERIFICATION_CODE,
            error_msg="Invalid verification code",
            input_params={"phone": new_phone, "code": code},
        )

    # 更新手机号
    user.phone = new_phone
    user.updated_at = get_beijing_time()

    session.add(user)
    await session.flush()

    return True


async def reset_password(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """重置密码"""
    user = await get_user(session, user_id)
    if not user:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="User not found",
            input_params={"user_id": str(user_id)},
        )

    if not user.email:
        raise AccountError(
            status_code=status.HTTP_409_NOT_FOUND,
            error_name="未设置邮箱",
            error_msg="未设置邮箱",
            input_params={"user_id": str(user_id)},
        )
    # 验证
    password = generate_password()
    # 发送邮件
    send_password(user.email, password)
    user.hashed_password = get_password_hash(password)
    user.updated_at = get_beijing_time()
    session.add(user)
    await session.flush()
    return True


def generate_password() -> str:
    """
    生成8-18位随机密码，要求：
    - 恰好一个大写字母
    - 恰好一个特殊符号
    - 其余为数字或小写字母
    """
    # 定义字符集
    digits = string.digits  # 数字: 0-9
    lowercase = string.ascii_lowercase  # 小写字母: a-z
    uppercase = string.ascii_uppercase  # 大写字母: A-Z（只取1个）
    symbols = "!@#$%^&*()_+-=[]{}|;:,.<>?"  # 特殊符号（只取1个）

    # 普通字符集（用于填充）
    normal_chars = digits + lowercase

    # 随机生成长度（8-18位）
    length = 8 + secrets.randbelow(11)

    password_chars = [secrets.choice(normal_chars) for _ in range(length)]
    uppercase_index = secrets.randbelow(length)
    symbol_index = secrets.randbelow(length - 1)
    if symbol_index >= uppercase_index:
        symbol_index += 1

    password_chars[uppercase_index] = secrets.choice(uppercase)
    password_chars[symbol_index] = secrets.choice(symbols)

    return "".join(password_chars)


async def admin_reset_password(session: AsyncSession, user_id: uuid.UUID, new_password: str) -> bool:
    """重置用户密码（管理员功能）。"""
    from app.account import service_auth as auth_service

    user = await get_user(session, user_id)

    if not user:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="User not found",
            input_params={"user_id": str(user_id)},
        )

    # 更新密码
    auth_service.validate_password_complexity(new_password)
    user.hashed_password = get_password_hash(new_password)
    user.access_token = None
    user.refresh_token = None
    user.token_expires_at = None
    user.updated_at = get_beijing_time()

    session.add(user)
    await session.flush()

    return True


async def update_user_roles(session: AsyncSession, user_id: uuid.UUID, role_names: list[str]) -> User:
    """使用角色名更新用户角色。"""
    user = await get_user(session, user_id)

    if not user:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="User not found",
            input_params={"user_id": str(user_id)},
        )

    # 根据角色名获取角色对象
    roles_result = await session.execute(select(Role).where(ROLE_NAME_COLUMN.in_(role_names)))
    roles = list(roles_result.scalars().all())

    # 校验所有角色都存在
    if len(roles) != len(role_names):
        raise AccountError(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_name=AccountErrorCode.ROLES_NOT_FOUND,
            error_msg="One or more roles not found",
            input_params={"role_names": role_names},
        )

    # 更新用户角色
    user.roles = roles
    user.updated_at = get_beijing_time()

    session.add(user)
    await session.flush()

    return user


async def delete_user(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """删除用户。"""
    user = await get_user(session, user_id)

    if not user:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AccountErrorCode.USER_NOT_FOUND,
            error_msg="User not found",
            input_params={"user_id": str(user_id)},
        )

    # 不做硬删除，改为将用户标记为 inactive（软删除）
    user.is_active = False
    user.updated_at = get_beijing_time()

    session.add(user)
    await session.flush()

    return True


async def batch_delete_users(session: AsyncSession, user_ids: list[uuid.UUID]) -> dict[str, Any]:
    """批量删除多个用户。"""
    success_ids: list[str] = []
    failed_items: list[dict[str, str]] = []

    for user_id in user_ids:
        user = await get_user(session, user_id)
        if not user:
            failed_items.append({"id": str(user_id), "reason": "User not found"})
            continue

        # 不做硬删除，改为将用户标记为 inactive（软删除）
        user.is_active = False
        user.updated_at = get_beijing_time()

        session.add(user)
        success_ids.append(str(user_id))

    await session.flush()
    return {"success": success_ids, "failed": failed_items}


# 角色管理函数


async def get_role(session: AsyncSession, role_id: uuid.UUID) -> Role | None:
    """根据 ID 获取角色。"""
    stmt = select(Role).where(role_id == ROLE_ID_COLUMN).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_role_by_name(session: AsyncSession, name: str | RoleType) -> Role | None:
    """根据角色名获取角色。"""
    return await get_role_by_name_async(session, name)


async def get_roles(session: AsyncSession, skip: int = 0, limit: int = 100) -> list[Role]:
    """获取所有角色。"""
    stmt = select(Role).offset(skip).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_role(session: AsyncSession, name: str, description: str | None = None) -> Role:
    """创建新角色。"""
    role = Role(name=cast("RoleType", name), description=description)
    session.add(role)
    await session.flush()
    return role


async def update_role(
    session: AsyncSession,
    role_id: uuid.UUID,
    name: str | None = None,
    description: str | None = None,
) -> Role:
    """更新角色信息。"""
    role = await get_role(session, role_id)

    if not role:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AccountErrorCode.ROLE_NOT_FOUND,
            error_msg="Role not found",
            input_params={"role_id": str(role_id)},
        )

    if name is not None:
        role.name = cast("RoleType", name)

    if description is not None:
        role.description = description

    session.add(role)
    await session.flush()

    return role


async def delete_role(session: AsyncSession, role_id: uuid.UUID) -> bool:
    """删除角色。"""
    role = await get_role(session, role_id)

    if not role:
        raise AccountError(
            status_code=status.HTTP_404_NOT_FOUND,
            error_name=AccountErrorCode.ROLE_NOT_FOUND,
            error_msg="Role not found",
            input_params={"role_id": str(role_id)},
        )

    await session.delete(role)
    await session.flush()

    return True
