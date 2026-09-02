import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.account.exception_auth import LocalAuthDisabledError
from app.account.model import Role, RoleType, User
from app.account.schema_account import (
    AdminPasswordReset,
    AdminResetOtherUserPassword,
    AicProviderCodeUpdate,
    BatchDeleteUsersResponse,
    CurrentUserResponse,
    ExternalPrincipalSummary,
    MessageResponse,
    PasswordUpdate,
    PhoneUpdate,
    RoleCreate,
    RoleResponse,
    RoleUpdate,
    SuccessMessageResponse,
    UpdatePasswordRequest,
    UserCreate,
    UserListResponse,
    UserResponse,
    UserRoleUpdate,
    UserUpdate,
    UserUpdateCode,
)
from app.account.service_account import (
    admin_reset_password,
    batch_delete_users,
    create_role,
    create_user,
    delete_role,
    delete_user,
    get_roles,
    get_user,
    get_users,
    reset_password,
    set_user_aic_provider_code,
    update_role,
    update_user,
    update_user_password,
    update_user_password_by_code,
    update_user_phone,
    update_user_roles,
)
from app.core.auth import check_user_role, get_current_user, verify_password
from app.core.base_exception import PROBLEM_JSON_MEDIA_TYPE
from app.core.config import settings
from app.core.db_session import get_session
from app.utils.utils import parse_boolean_string

router = APIRouter(prefix="/account", tags=["account"])

type SessionDep = Annotated[AsyncSession, Depends(get_session)]
type CurrentUserDep = Annotated[User, Depends(get_current_user)]
type AdminUserDep = Annotated[User, Depends(check_user_role([RoleType.ADMIN]))]
type AdminOrStaffUserDep = Annotated[User, Depends(check_user_role([RoleType.ADMIN, RoleType.STAFF]))]
type UserIdsBody = Annotated[list[uuid.UUID], Body()]
PAGE_NUM_DEPRECATION_WARNING = '299 - "page_num query parameter is deprecated; use page"'


def _to_user_response(user: User) -> UserResponse:
    return UserResponse.model_validate(user)


def _to_current_user_response(user: User) -> CurrentUserResponse:
    payload = _to_user_response(user).model_dump()
    external_principal = None
    if user.auth_provider == "oidc" and user.external_issuer and user.external_principal_id:
        external_principal = ExternalPrincipalSummary(
            provider=user.auth_provider,
            issuer=user.external_issuer,
            principal_id=user.external_principal_id,
            username=user.external_username,
            email=user.email,
        )
    return CurrentUserResponse(**payload, external_principal=external_principal)


def _to_role_response(role: Role) -> RoleResponse:
    return RoleResponse.model_validate(role)


def _problem_response(description: str) -> dict[str, object]:
    return {"description": description, "content": {PROBLEM_JSON_MEDIA_TYPE: {}}}


def _resolve_page(page: int | None, page_num: int | None, response: Response) -> int:
    if page_num is not None:
        response.headers["Deprecation"] = "true"
        response.headers["Warning"] = PAGE_NUM_DEPRECATION_WARNING

    if page is not None:
        return page
    if page_num is not None:
        return page_num
    return 1


BAD_REQUEST_RESPONSE = _problem_response("Invalid request")
FORBIDDEN_RESPONSE = _problem_response("Insufficient permissions")
NOT_FOUND_RESPONSE = _problem_response("Resource not found")
UNAUTHORIZED_RESPONSE = _problem_response("Authentication required")
VALIDATION_RESPONSE = _problem_response("Request validation failed")
CONFLICT_RESPONSE = _problem_response("Resource conflict")
GONE_RESPONSE = _problem_response("Local authentication is disabled")


def _ensure_local_auth_enabled(detail: str) -> None:
    if settings.oidc_enabled:
        raise LocalAuthDisabledError(detail=detail)


# 当前用户相关端点
@router.get(
    "/me",
    response_model=CurrentUserResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_200_OK,
    summary="获取当前用户信息",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
    },
)
async def get_current_user_info(current_user: CurrentUserDep) -> CurrentUserResponse:
    """
    获取当前已认证用户的信息。
    """
    return _to_current_user_response(current_user)


@router.put(
    "/me",
    response_model=CurrentUserResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_200_OK,
    summary="更新当前用户信息",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
    },
)
async def update_current_user(
    user_update: UserUpdateCode,
    current_user: CurrentUserDep,
    db: SessionDep,
) -> CurrentUserResponse:
    """
    更新当前已认证用户的信息。
    """
    user = await update_user(db, current_user.id, user_update)
    return _to_current_user_response(user)


@router.post(
    "/update_password",
    status_code=status.HTTP_200_OK,
    summary="通过验证码修改密码",
    responses={
        status.HTTP_410_GONE: GONE_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def update_password_by_code(
    params: UpdatePasswordRequest,
    db: SessionDep,
) -> SuccessMessageResponse:
    """
    通过验证码修改密码
    """
    _ensure_local_auth_enabled("Password reset by verification code is disabled when OIDC is enabled")
    success = await update_user_password_by_code(db, params.email, params.code, params.password)
    return SuccessMessageResponse(success=success, message="密码修改成功")


@router.put(
    "/me/password",
    status_code=status.HTTP_200_OK,
    summary="更新当前用户密码",
    responses={
        status.HTTP_400_BAD_REQUEST: BAD_REQUEST_RESPONSE,
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def update_current_user_password(
    password_update: PasswordUpdate,
    current_user: CurrentUserDep,
    db: SessionDep,
) -> SuccessMessageResponse:
    """
    更新当前已认证用户的密码。
    """
    _ensure_local_auth_enabled("Password change is disabled when OIDC is enabled")
    success = await update_user_password(
        db, current_user.id, password_update.old_password, password_update.new_password
    )
    return SuccessMessageResponse(success=success, message="Password updated successfully")


@router.put(
    "/me/phone",
    status_code=status.HTTP_200_OK,
    summary="更新当前用户手机号",
    responses={
        status.HTTP_400_BAD_REQUEST: BAD_REQUEST_RESPONSE,
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
    },
)
async def update_current_user_phone(
    phone_update: PhoneUpdate,
    current_user: CurrentUserDep,
    db: SessionDep,
) -> SuccessMessageResponse:
    """
    更新当前已认证用户的手机号（需要验证码校验）。
    """
    success = await update_user_phone(db, current_user.id, phone_update.new_phone, phone_update.verify_code)
    return SuccessMessageResponse(success=success, message="Phone number updated successfully")


# 管理员用户管理端点
@router.get(
    "/user",
    status_code=status.HTTP_200_OK,
    summary="分页获取用户列表",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def read_users(
    response: Response,
    db: SessionDep,
    current_user: AdminOrStaffUserDep,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_num: Annotated[int | None, Query(ge=1, deprecated=True)] = None,
    page_size: Annotated[int, Query(ge=1)] = 10,
    username: str | None = None,
    phone: str | None = None,
    name: str | None = None,
    role: str | None = None,
    is_active: Annotated[str | None, Query()] = None,
) -> UserListResponse:
    """
    获取用户列表并支持可选筛选条件（管理员与工作人员）。
    """
    # 使用工具函数将 is_active 字符串转换为布尔值或 None
    is_active_bool = parse_boolean_string(is_active)
    resolved_page = _resolve_page(page, page_num, response)

    del current_user
    users, total = await get_users(db, resolved_page, page_size, username, phone, name, role, is_active_bool)
    items = [_to_user_response(user) for user in users]
    return UserListResponse(items=items, total=total, page=resolved_page, page_num=resolved_page, page_size=page_size)


@router.post(
    "/user",
    status_code=status.HTTP_200_OK,
    summary="创建用户",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def create_new_user(
    user_create: UserCreate,
    db: SessionDep,
    current_user: AdminUserDep,
) -> UserResponse:
    """
    创建新用户（仅管理员）。
    """
    del current_user
    _ensure_local_auth_enabled("Local user creation is disabled when OIDC is enabled")
    user = await create_user(db, user_create)
    return _to_user_response(user)


@router.get(
    "/user/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="按 ID 获取用户",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def read_user(
    user_id: uuid.UUID,
    db: SessionDep,
    current_user: AdminOrStaffUserDep,
) -> UserResponse:
    """
    根据 ID 获取指定用户（仅管理员或员工）。
    """
    del current_user
    return _to_user_response(await get_user(db, user_id, raise_exception=True))


@router.put(
    "/user/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="更新指定用户信息",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def update_user_info(
    user_id: uuid.UUID,
    user_update: UserUpdate,
    db: SessionDep,
    current_user: AdminUserDep,
) -> UserResponse:
    """
    更新用户信息（仅管理员）。
    """
    del current_user
    user = await update_user(db, user_id, user_update)
    return _to_user_response(user)


@router.put(
    "/user/{user_id}/aic-provider-code",
    status_code=status.HTTP_200_OK,
    summary="设置指定用户的 AIC 供应商序号",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def update_user_aic_provider_code(
    user_id: uuid.UUID,
    payload: AicProviderCodeUpdate,
    db: SessionDep,
    current_user: AdminOrStaffUserDep,
) -> UserResponse:
    """预置或改写账户 AIC 第7级（仅管理员或员工）。"""
    del current_user
    user = await set_user_aic_provider_code(db, user_id, payload.aic_provider_code)
    return _to_user_response(user)


@router.put(
    "/user/{user_id}/reset_password",
    response_model=SuccessMessageResponse,
    status_code=status.HTTP_200_OK,
    summary="通过管理员当前密码触发目标用户重置密码邮件",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_410_GONE: GONE_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def reset_user_pwd(
    user_id: uuid.UUID,
    params: AdminResetOtherUserPassword,
    db: SessionDep,
    current_user: AdminOrStaffUserDep,
) -> SuccessMessageResponse:
    """重置用户密码，密码是随机生成的，通过邮箱发送"""
    _ensure_local_auth_enabled("Local password reset is disabled when OIDC is enabled")
    hashed_password = current_user.hashed_password
    if hashed_password:
        is_valid, _ = verify_password(params.current_password, hashed_password)
        if is_valid:
            await reset_password(db, user_id)
            return SuccessMessageResponse(success=True, message="重置成功")
    return SuccessMessageResponse(success=False, message="密码错误")


@router.put(
    "/user/{user_id}/password",
    status_code=status.HTTP_200_OK,
    summary="重置指定用户密码",
    responses={
        status.HTTP_400_BAD_REQUEST: BAD_REQUEST_RESPONSE,
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def reset_user_password(
    user_id: uuid.UUID,
    password_reset: AdminPasswordReset,
    db: SessionDep,
    current_user: AdminUserDep,
) -> MessageResponse:
    """
    重置用户密码（仅管理员）。
    """
    del current_user
    _ensure_local_auth_enabled("Local password reset is disabled when OIDC is enabled")
    await admin_reset_password(db, user_id, password_reset.new_password)
    return MessageResponse(message="Password reset successfully")


@router.put(
    "/user/{user_id}/roles",
    status_code=status.HTTP_200_OK,
    summary="更新指定用户角色",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def update_user_role_assignments(
    user_id: uuid.UUID,
    role_update: UserRoleUpdate,
    db: SessionDep,
    current_user: AdminUserDep,
) -> UserResponse:
    """
    使用角色名更新用户角色（仅管理员）。
    """
    del current_user
    user = await update_user_roles(db, user_id, role_update.role_names)
    return _to_user_response(user)


@router.delete(
    "/user/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="删除指定用户",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def delete_user_account(
    user_id: uuid.UUID,
    db: SessionDep,
    current_user: AdminUserDep,
) -> SuccessMessageResponse:
    """
    删除用户（仅管理员）。
    """
    del current_user
    success = await delete_user(db, user_id)
    return SuccessMessageResponse(success=success, message="User deleted successfully")


@router.delete(
    "/user",
    status_code=status.HTTP_200_OK,
    summary="批量删除用户",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def batch_delete_user_accounts(
    user_ids: UserIdsBody,
    db: SessionDep,
    current_user: AdminUserDep,
) -> BatchDeleteUsersResponse:
    """
    批量删除多个用户（仅管理员）。
    """
    del current_user
    result = await batch_delete_users(db, user_ids)
    return BatchDeleteUsersResponse.model_validate(result)


# 角色管理端点
@router.get(
    "/role",
    status_code=status.HTTP_200_OK,
    summary="获取角色列表",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def read_roles(
    db: SessionDep,
    current_user: AdminUserDep,
    skip: int = 0,
    limit: int = 100,
) -> list[RoleResponse]:
    """
    获取所有角色（仅管理员）。
    """
    del current_user
    return [_to_role_response(role) for role in await get_roles(db, skip, limit)]


@router.post(
    "/role",
    status_code=status.HTTP_200_OK,
    summary="创建角色",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def create_new_role(
    role_create: RoleCreate,
    db: SessionDep,
    current_user: AdminUserDep,
) -> RoleResponse:
    """
    创建新角色（仅管理员）。
    """
    del current_user
    role = await create_role(db, role_create.name, role_create.description)
    return _to_role_response(role)


@router.put(
    "/role/{role_id}",
    status_code=status.HTTP_200_OK,
    summary="更新角色信息",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_409_CONFLICT: CONFLICT_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def update_role_info(
    role_id: uuid.UUID,
    role_update: RoleUpdate,
    db: SessionDep,
    current_user: AdminUserDep,
) -> RoleResponse:
    """
    更新角色信息（仅管理员）。
    """
    del current_user
    role = await update_role(db, role_id, role_update.name, role_update.description)
    return _to_role_response(role)


@router.delete(
    "/role/{role_id}",
    status_code=status.HTTP_200_OK,
    summary="删除角色",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED_RESPONSE,
        status.HTTP_403_FORBIDDEN: FORBIDDEN_RESPONSE,
        status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE,
        status.HTTP_422_UNPROCESSABLE_CONTENT: VALIDATION_RESPONSE,
    },
)
async def delete_role_item(
    role_id: uuid.UUID,
    db: SessionDep,
    current_user: AdminUserDep,
) -> MessageResponse:
    """
    Delete a role (admin only)
    """
    del current_user
    await delete_role(db, role_id)
    return MessageResponse(message="Role deleted successfully")
