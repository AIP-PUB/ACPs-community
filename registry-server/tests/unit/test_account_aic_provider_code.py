"""账户 AIC 第7级字段的单元测试。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Literal

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.account.model import User
from app.account.schema_account import CurrentUserResponse, UserResponse, UserUpdate
from app.account.service_account import (
    _user_aic_provider_lock_stmt,
    ensure_user_aic_provider_code,
    ensure_user_aic_provider_code_async,
)
from app.agent.exception import AgentError, AgentErrorCode
from app.utils import aic

pytestmark = pytest.mark.unit


def test_new_user_defaults_aic_provider_code_to_none() -> None:
    user = User(username="vendor-one")
    assert user.aic_provider_code is None


def test_user_update_schema_excludes_aic_provider_code() -> None:
    assert "aic_provider_code" not in UserUpdate.model_fields
    payload = UserUpdate.model_validate({"name": "Ada", "aic_provider_code": "34C2"})
    assert payload.name == "Ada"
    assert not hasattr(payload, "aic_provider_code")


def test_user_response_exposes_aic_provider_code() -> None:
    assert "aic_provider_code" in UserResponse.model_fields
    assert "aic_provider_code" in CurrentUserResponse.model_fields


class _LockResult:
    def __init__(self, user: object | None) -> None:
        self._user = user

    def scalar_one_or_none(self) -> object | None:
        return self._user


class _Nested:
    def __enter__(self) -> _Nested:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        del exc_type, exc, tb
        return False

    async def __aenter__(self) -> _Nested:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        del exc_type, exc, tb
        return False


def _compiled_sql(stmt: object) -> str:
    compile_stmt = getattr(stmt, "compile", None)
    if compile_stmt is None:
        raise AssertionError("statement cannot be compiled")
    return str(compile_stmt(dialect=postgresql.dialect())).upper()


class _EnsureSession:
    def __init__(self, user: object | None, *, flush_errors: list[BaseException] | None = None) -> None:
        self.user = user
        self.added: list[object] = []
        self.flush_calls = 0
        self.statements: list[object] = []
        self._flush_errors = list(flush_errors or [])

    def execute(self, stmt: object) -> _LockResult:
        self.statements.append(stmt)
        return _LockResult(self.user)

    def begin_nested(self) -> _Nested:
        return _Nested()

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        self.flush_calls += 1
        if self._flush_errors:
            raise self._flush_errors.pop(0)


def test_user_aic_provider_lock_stmt_uses_for_update() -> None:
    sql = _compiled_sql(_user_aic_provider_lock_stmt(uuid.uuid4()))
    assert "FOR UPDATE" in sql
    assert "ACCOUNT_USER" in sql


def test_ensure_locks_user_row_for_update() -> None:
    user = SimpleNamespace(id=uuid.uuid4(), aic_provider_code="0001")
    session = _EnsureSession(user)

    ensure_user_aic_provider_code(session, user.id)  # type: ignore[arg-type]

    assert session.statements
    assert "FOR UPDATE" in _compiled_sql(session.statements[0])


def test_ensure_reuses_existing_provider_code() -> None:
    user = SimpleNamespace(id=uuid.uuid4(), aic_provider_code="0001")
    session = _EnsureSession(user)

    code = ensure_user_aic_provider_code(session, user.id)  # type: ignore[arg-type]

    assert code == "0001"
    assert session.flush_calls == 0


def test_ensure_assigns_random_code_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4(), aic_provider_code=None)
    session = _EnsureSession(user)
    monkeypatch.setattr("app.account.service_account.generate_aic_provider_code", lambda: "A1B2C3")

    code = ensure_user_aic_provider_code(session, user.id)  # type: ignore[arg-type]

    assert code == "A1B2C3"
    assert user.aic_provider_code == "A1B2C3"
    assert session.flush_calls == 1


def test_ensure_retries_after_unique_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4(), aic_provider_code=None)
    session = _EnsureSession(user, flush_errors=[IntegrityError("dup", {}, Exception())])
    codes = iter(["AAAAAA", "BBBBBB"])
    monkeypatch.setattr("app.account.service_account.generate_aic_provider_code", lambda: next(codes))

    code = ensure_user_aic_provider_code(session, user.id)  # type: ignore[arg-type]

    assert code == "BBBBBB"
    assert user.aic_provider_code == "BBBBBB"
    assert session.flush_calls == 2


def test_ensure_raises_when_owner_missing() -> None:
    session = _EnsureSession(None)
    with pytest.raises(AgentError) as exc_info:
        ensure_user_aic_provider_code(session, uuid.uuid4())  # type: ignore[arg-type]
    assert exc_info.value.error_name == AgentErrorCode.AIC_OWNER_NOT_FOUND


async def test_ensure_async_reuses_existing_provider_code() -> None:
    user = SimpleNamespace(id=uuid.uuid4(), aic_provider_code="34C2")
    session = _EnsureSession(user)

    async def _execute(stmt: object) -> _LockResult:
        return _LockResult(session.user)

    session.execute = _execute  # type: ignore[assignment, method-assign]

    code = await ensure_user_aic_provider_code_async(session, user.id)  # type: ignore[arg-type]
    assert code == "34C2"


def test_random_provider_code_is_valid_level_code() -> None:
    code = aic.generate_aic_provider_code()
    assert aic.normalize_aic_level_code(code) == code


class _SetSession:
    def __init__(
        self,
        user: object | None,
        *,
        in_use_id: uuid.UUID | None = None,
        flush_error: BaseException | None = None,
    ) -> None:
        self.user = user
        self.in_use_id = in_use_id
        self.flush_error = flush_error
        self.execute_calls = 0
        self.statements: list[object] = []
        self.flushed = False

    async def execute(self, stmt: object) -> _LockResult:
        self.statements.append(stmt)
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _LockResult(self.user)
        return _LockResult(self.in_use_id)

    def add(self, item: object) -> None:
        del item

    async def flush(self) -> None:
        if self.flush_error is not None:
            raise self.flush_error
        self.flushed = True


async def test_set_provider_code_normalizes_and_writes() -> None:
    from app.account.service_account import set_user_aic_provider_code

    user = SimpleNamespace(id=uuid.uuid4(), aic_provider_code=None, roles=[])
    session = _SetSession(user)

    result = await set_user_aic_provider_code(session, user.id, "34c2")  # type: ignore[arg-type]

    assert result is user  # type: ignore[comparison-overlap]
    assert user.aic_provider_code == "34C2"
    assert session.flushed is True


async def test_set_provider_code_is_idempotent_when_in_use() -> None:
    from app.account.service_account import set_user_aic_provider_code

    user = SimpleNamespace(id=uuid.uuid4(), aic_provider_code="0001", roles=[])
    session = _SetSession(user, in_use_id=uuid.uuid4())

    result = await set_user_aic_provider_code(session, user.id, "0001")  # type: ignore[arg-type]

    assert result is user  # type: ignore[comparison-overlap]
    assert session.execute_calls == 1
    assert session.flushed is False


async def test_set_provider_code_rejects_in_use_change() -> None:
    from app.account.exception_account import AccountError, AccountErrorCode
    from app.account.service_account import set_user_aic_provider_code

    user = SimpleNamespace(id=uuid.uuid4(), aic_provider_code="0001", roles=[])
    session = _SetSession(user, in_use_id=uuid.uuid4())

    with pytest.raises(AccountError) as exc_info:
        await set_user_aic_provider_code(session, user.id, "34C2")  # type: ignore[arg-type]

    assert exc_info.value.code == AccountErrorCode.AIC_PROVIDER_CODE_IN_USE
    assert exc_info.value.status_code == 409


async def test_set_provider_code_rejects_all_zero() -> None:
    from app.account.exception_account import AccountError, AccountErrorCode
    from app.account.service_account import set_user_aic_provider_code

    user = SimpleNamespace(id=uuid.uuid4(), aic_provider_code=None, roles=[])
    session = _SetSession(user)

    with pytest.raises(AccountError) as exc_info:
        await set_user_aic_provider_code(session, user.id, "0")  # type: ignore[arg-type]

    assert exc_info.value.code == AccountErrorCode.AIC_PROVIDER_CODE_INVALID
    assert exc_info.value.status_code == 422


async def test_set_provider_code_not_found() -> None:
    from app.account.exception_account import AccountError, AccountErrorCode
    from app.account.service_account import set_user_aic_provider_code

    session = _SetSession(None)
    with pytest.raises(AccountError) as exc_info:
        await set_user_aic_provider_code(session, uuid.uuid4(), "34C2")  # type: ignore[arg-type]
    assert exc_info.value.code == AccountErrorCode.USER_NOT_FOUND
    assert exc_info.value.status_code == 404


async def test_set_provider_code_locks_user_row_for_update() -> None:
    from app.account.service_account import set_user_aic_provider_code

    user = SimpleNamespace(id=uuid.uuid4(), aic_provider_code=None, roles=[])
    session = _SetSession(user)

    await set_user_aic_provider_code(session, user.id, "34C2")  # type: ignore[arg-type]

    assert session.statements
    assert "FOR UPDATE" in _compiled_sql(session.statements[0])
    assert all("FOR UPDATE" not in _compiled_sql(stmt) for stmt in session.statements[1:])


async def test_set_provider_code_maps_unique_conflict() -> None:
    from app.account.exception_account import AccountError, AccountErrorCode
    from app.account.service_account import set_user_aic_provider_code

    user = SimpleNamespace(id=uuid.uuid4(), aic_provider_code=None, roles=[])
    session = _SetSession(user, flush_error=IntegrityError("dup", {}, Exception()))

    with pytest.raises(AccountError) as exc_info:
        await set_user_aic_provider_code(session, user.id, "34C2")  # type: ignore[arg-type]

    assert exc_info.value.code == AccountErrorCode.AIC_PROVIDER_CODE_CONFLICT
    assert exc_info.value.status_code == 409
