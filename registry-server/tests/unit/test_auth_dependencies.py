from copy import deepcopy
from datetime import timedelta
from typing import Any, cast

import jwt
import pytest
from acps_sdk.oidc import HumanPrincipal, build_principal_id, build_principal_key
from sqlalchemy.ext.asyncio import AsyncSession

from app.account.exception_auth import AuthError, AuthErrorCode
from app.account.model import Role, RoleType, User
from app.agent import model as _agent_model
from app.core import auth as auth_module
from app.core.config import settings
from app.utils.utils import get_beijing_time

pytestmark = pytest.mark.unit


del _agent_model


class DummyAsyncResult:
    def __init__(self, user: User | None) -> None:
        self.user = user

    def scalar_one_or_none(self) -> User | None:
        return self.user


class DummyAsyncSession:
    def __init__(self, user: User | None) -> None:
        self.user = user
        self.executed_statement: object | None = None

    async def execute(self, statement: object) -> DummyAsyncResult:
        self.executed_statement = statement
        return DummyAsyncResult(self.user)


def _as_async_session(session: DummyAsyncSession) -> AsyncSession:
    return cast("AsyncSession", session)


def _statement_loads_roles(statement: object | None) -> bool:
    if statement is None:
        return False

    options = getattr(cast("Any", statement), "_with_options", ())
    return any("roles" in str(getattr(option, "path", "")) for option in options)


def _make_user(*, access_token: str | None = "token", role: RoleType = RoleType.CLIENT) -> User:  # noqa: S107
    user = User(username="demo-user", hashed_password="hashed-password", access_token=access_token, is_active=True)
    user.roles = [Role(name=role, description=f"{role} role")]
    user.token_expires_at = get_beijing_time() + timedelta(minutes=5)
    return user


def _make_principal() -> HumanPrincipal:
    issuer = "https://issuer.example/realms/acps-registry"
    subject = "user-123"
    return HumanPrincipal(
        issuer=issuer,
        subject=subject,
        principal_key=build_principal_key(issuer=issuer, subject=subject),
        principal_id=build_principal_id(issuer=issuer, subject=subject),
        audiences=("registry-api",),
        azp="registry-web",
        username="alice",
        roles=("CLIENT",),
    )


async def test_get_current_user_returns_user_with_roles_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _make_user()
    session = DummyAsyncSession(user)

    monkeypatch.setattr(jwt, "decode", lambda token, key, algorithms: {"sub": str(user.id)})

    current_user = await auth_module.get_current_user(token="token", session=_as_async_session(session))

    assert current_user is user
    assert _statement_loads_roles(session.executed_statement) is True


async def test_safe_get_current_user_returns_none_when_token_mismatches(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _make_user(access_token="stored-token")
    session = DummyAsyncSession(user)

    monkeypatch.setattr(jwt, "decode", lambda token, key, algorithms: {"sub": str(user.id)})

    current_user = await auth_module.safe_get_current_user(token="other-token", session=_as_async_session(session))

    assert current_user is None
    assert _statement_loads_roles(session.executed_statement) is True


async def test_check_user_role_rejects_missing_required_role() -> None:
    current_user = _make_user(role=RoleType.CLIENT)
    dependency = auth_module.check_user_role([RoleType.ADMIN])

    with pytest.raises(AuthError) as exc_info:
        await dependency(current_user=current_user)

    assert exc_info.value.error_name == AuthErrorCode.INSUFFICIENT_PERMISSIONS


async def test_get_current_user_uses_oidc_principal_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    original = deepcopy(settings._toml)
    settings._toml.setdefault("oidc", {})["enabled"] = True
    user = _make_user(access_token=None)
    session = DummyAsyncSession(None)
    principal = _make_principal()

    async def _validate_access_token(token: str) -> HumanPrincipal:
        assert token == "oidc-token"
        return principal

    async def _get_or_create_user_from_principal(_session: object, _principal: HumanPrincipal) -> User:
        return user

    async def _sync_user_email(_session: object, _user: User, _principal: HumanPrincipal) -> None:
        return None

    monkeypatch.setattr(auth_module, "validate_access_token", _validate_access_token)
    monkeypatch.setattr(auth_module, "get_or_create_user_from_principal", _get_or_create_user_from_principal)
    monkeypatch.setattr(auth_module, "sync_user_email", _sync_user_email)

    try:
        current_user = await auth_module.get_current_user(token="oidc-token", session=_as_async_session(session))
    finally:
        object.__setattr__(settings, "_toml", original)

    assert current_user is user


async def test_safe_get_current_user_returns_none_when_optional_oidc_principal_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = deepcopy(settings._toml)
    settings._toml.setdefault("oidc", {})["enabled"] = True
    session = DummyAsyncSession(None)

    async def _validate_optional_access_token(token: str | None) -> None:
        assert token == "bad-token"
        return

    monkeypatch.setattr(auth_module, "validate_optional_access_token", _validate_optional_access_token)

    try:
        current_user = await auth_module.safe_get_current_user(token="bad-token", session=_as_async_session(session))
    finally:
        object.__setattr__(settings, "_toml", original)

    assert current_user is None
