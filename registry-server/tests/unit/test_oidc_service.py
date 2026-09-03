from __future__ import annotations

import uuid
from typing import cast

import pytest
from acps_sdk.oidc import HumanPrincipal, build_principal_id, build_principal_key
from sqlalchemy.ext.asyncio import AsyncSession

from app.account import service_oidc
from app.account.exception_auth import InactiveUserError
from app.account.model import Role, RoleType, User

pytestmark = pytest.mark.unit


class DummySession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushed = 0
        self.refreshed: list[tuple[object, list[str] | None]] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    async def refresh(self, obj: object, attribute_names: list[str] | None = None) -> None:
        self.refreshed.append((obj, attribute_names))


def _as_async_session(session: DummySession) -> AsyncSession:
    return cast("AsyncSession", session)


def _principal(
    *,
    subject: str,
    username: str = "alice",
    email: str | None = "alice@example.com",
    roles: tuple[str, ...] = ("CLIENT",),
) -> HumanPrincipal:
    issuer = "https://issuer.example/realms/acps-registry"
    return HumanPrincipal(
        issuer=issuer,
        subject=subject,
        principal_key=build_principal_key(issuer=issuer, subject=subject),
        principal_id=build_principal_id(issuer=issuer, subject=subject),
        audiences=("registry-api",),
        azp="registry-web",
        username=username,
        name="Alice",
        email=email,
        roles=roles,
        scopes=("agent:read",),
        groups=("/registry",),
        raw_claims={"picture": "https://example.com/avatar.png"},
    )


async def test_get_or_create_user_from_principal_creates_shadow_user(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    principal = _principal(subject="user-1", roles=("CLIENT", "STAFF"))
    expected_roles = [Role(name=RoleType.CLIENT), Role(name=RoleType.STAFF)]

    async def _none(*args: object, **kwargs: object) -> None:
        return None

    async def _choose_username(*args: object, **kwargs: object) -> str:
        return "alice"

    async def _resolve_roles(*args: object, **kwargs: object) -> list[Role]:
        return expected_roles

    monkeypatch.setattr(service_oidc, "_get_user_by_external_principal_id", _none)
    monkeypatch.setattr(service_oidc, "_get_user_by_external_identity", _none)
    monkeypatch.setattr(service_oidc, "_choose_username", _choose_username)
    monkeypatch.setattr(service_oidc, "_resolve_roles", _resolve_roles)

    user = await service_oidc.get_or_create_user_from_principal(_as_async_session(session), principal)

    assert user.auth_provider == "oidc"
    assert user.external_issuer == principal.issuer
    assert user.external_subject == principal.subject
    assert user.external_principal_id == principal.principal_id
    assert user.external_username == "alice"
    assert user.avatar == "https://example.com/avatar.png"
    assert [role.name for role in user.roles] == [RoleType.CLIENT, RoleType.STAFF]
    assert session.flushed == 1
    assert session.refreshed


async def test_get_or_create_user_from_principal_reuses_existing_shadow_user(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    principal = _principal(subject="user-2")
    existing = User(username="existing")
    existing.id = uuid.uuid4()
    existing.roles = [Role(name=RoleType.CLIENT)]

    async def _get_existing(*args: object, **kwargs: object) -> User:
        return existing

    async def _resolve_roles(*args: object, **kwargs: object) -> list[Role]:
        return [Role(name=RoleType.CLIENT)]

    monkeypatch.setattr(service_oidc, "_get_user_by_external_principal_id", _get_existing)
    monkeypatch.setattr(service_oidc, "_resolve_roles", _resolve_roles)

    user = await service_oidc.get_or_create_user_from_principal(_as_async_session(session), principal)

    assert user is existing
    assert user.external_principal_id == principal.principal_id


async def test_choose_username_falls_back_on_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    principal = _principal(subject="user-3", username="alice")
    seen: list[str] = []

    async def _username_exists(_session: object, username: str) -> bool:
        seen.append(username)
        return username == "alice"

    monkeypatch.setattr(service_oidc, "_username_exists", _username_exists)

    username = await service_oidc._choose_username(_as_async_session(session), principal)

    assert username.startswith("oidc:")
    assert seen[0] == "alice"


async def test_inactive_shadow_user_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    principal = _principal(subject="user-4")
    existing = User(username="inactive-user", is_active=False)
    existing.id = uuid.uuid4()

    async def _get_existing(*args: object, **kwargs: object) -> User:
        return existing

    monkeypatch.setattr(service_oidc, "_get_user_by_external_principal_id", _get_existing)

    with pytest.raises(InactiveUserError):
        await service_oidc.get_or_create_user_from_principal(_as_async_session(session), principal)


async def test_sync_user_email_skips_conflicting_email(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    principal = _principal(subject="user-5", email="conflict@example.com")
    user = User(username="shadow-user")
    user.id = uuid.uuid4()
    user.email = None

    async def _email_unavailable(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(service_oidc, "_email_available", _email_unavailable)

    await service_oidc.sync_user_email(_as_async_session(session), user, principal)

    assert user.email is None
