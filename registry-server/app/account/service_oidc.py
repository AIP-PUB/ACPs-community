from __future__ import annotations

from typing import Any, cast

from acps_sdk.oidc import HumanPrincipal
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.account.exception_auth import InactiveUserError
from app.account.model import Role, RoleType, User
from app.utils.utils import get_beijing_time

USER_ID_COLUMN = cast("Any", User.id)
USER_USERNAME_COLUMN = cast("Any", User.username)
USER_EMAIL_COLUMN = cast("Any", User.email)
USER_EXTERNAL_PRINCIPAL_ID_COLUMN = cast("Any", User.external_principal_id)
USER_EXTERNAL_ISSUER_COLUMN = cast("Any", User.external_issuer)
USER_EXTERNAL_SUBJECT_COLUMN = cast("Any", User.external_subject)
USER_ROLES_RELATIONSHIP = cast("Any", User.roles)
ROLE_NAME_COLUMN = cast("Any", Role.name)


async def get_or_create_user_from_principal(session: AsyncSession, principal: HumanPrincipal) -> User:
    user = await _get_user_by_external_principal_id(session, principal.principal_id)
    if user is None:
        user = await _get_user_by_external_identity(session, principal.issuer, principal.subject)

    if user is None:
        user = User(
            username=await _choose_username(session, principal),
            auth_provider="oidc",
            external_issuer=principal.issuer,
            external_subject=principal.subject,
            external_principal_id=principal.principal_id,
        )
    elif not user.is_active:
        raise InactiveUserError(user_id=str(user.id))

    _sync_user_snapshot(user=user, principal=principal)
    user.roles = await _resolve_roles(session, principal.roles)

    session.add(user)
    await session.flush()
    await session.refresh(user, attribute_names=["roles"])
    return user


async def sync_user_email(session: AsyncSession, user: User, principal: HumanPrincipal) -> None:
    email = (principal.email or "").strip()
    if not email:
        return
    if await _email_available(session, email=email, current_user_id=user.id):
        user.email = email


async def _get_user_by_external_principal_id(session: AsyncSession, principal_id: str) -> User | None:
    stmt = (
        select(User)
        .options(selectinload(USER_ROLES_RELATIONSHIP))
        .where(principal_id == USER_EXTERNAL_PRINCIPAL_ID_COLUMN)
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_user_by_external_identity(session: AsyncSession, issuer: str, subject: str) -> User | None:
    stmt = (
        select(User)
        .options(selectinload(USER_ROLES_RELATIONSHIP))
        .where(
            and_(
                issuer == USER_EXTERNAL_ISSUER_COLUMN,
                subject == USER_EXTERNAL_SUBJECT_COLUMN,
            )
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _choose_username(session: AsyncSession, principal: HumanPrincipal) -> str:
    preferred = (principal.username or "").strip()
    if preferred and not await _username_exists(session, preferred):
        return preferred

    fallback = f"oidc:{principal.principal_id[:16]}"
    if not await _username_exists(session, fallback):
        return fallback

    index = 1
    while True:
        candidate = f"{fallback}-{index}"
        if not await _username_exists(session, candidate):
            return candidate
        index += 1


async def _username_exists(session: AsyncSession, username: str) -> bool:
    stmt = select(USER_ID_COLUMN).where(username == USER_USERNAME_COLUMN).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _email_available(session: AsyncSession, *, email: str, current_user_id: object) -> bool:
    stmt = select(USER_ID_COLUMN).where(email == USER_EMAIL_COLUMN).limit(2)
    result = await session.execute(stmt)
    ids = result.scalars().all()
    return all(existing_id == current_user_id for existing_id in ids)


def _sync_user_snapshot(*, user: User, principal: HumanPrincipal) -> None:
    now = get_beijing_time()
    user.auth_provider = "oidc"
    user.external_issuer = principal.issuer
    user.external_subject = principal.subject
    user.external_principal_id = principal.principal_id
    user.external_username = principal.username
    if principal.name:
        user.name = principal.name
    elif principal.username and not user.name:
        user.name = principal.username

    picture = principal.raw_claims.get("picture")
    if isinstance(picture, str) and picture.strip():
        user.avatar = picture.strip()

    user.last_login_at = now
    user.updated_at = now


async def _resolve_roles(session: AsyncSession, principal_roles: tuple[str, ...]) -> list[Role]:
    normalized: list[RoleType] = []
    for principal_role in principal_roles:
        try:
            role_type = RoleType(principal_role.upper())
        except ValueError:
            continue
        if role_type not in normalized:
            normalized.append(role_type)

    if not normalized:
        return []

    stmt = select(Role).where(ROLE_NAME_COLUMN.in_(normalized))
    result = await session.execute(stmt)
    role_map = {existing_role.name: existing_role for existing_role in result.scalars().all()}

    resolved_roles: list[Role] = []
    for role_type in normalized:
        resolved_role = role_map.get(role_type)
        if resolved_role is None:
            resolved_role = Role(name=role_type, description=f"{role_type.value} role")
            session.add(resolved_role)
            await session.flush()
        resolved_roles.append(resolved_role)
    return resolved_roles
