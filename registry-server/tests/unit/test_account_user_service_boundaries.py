import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.account import service_account
from app.account.model import Role, RoleType, User
from app.account.schema_account import UserCreate, UserUpdate
from app.agent import model as _agent_model

pytestmark = pytest.mark.unit


del _agent_model


class DummyQuery:
    def __init__(self, first_result: object | None = None, all_result: Sequence[object] | None = None) -> None:
        self.first_result = first_result
        self.all_result = list(all_result or [])

    def filter(self, *args: object, **kwargs: object) -> DummyQuery:
        del args, kwargs
        return self

    def scalars(self) -> DummyQuery:
        return self

    def first(self) -> object | None:
        return self.first_result

    def all(self) -> list[object]:
        return self.all_result


class DummyDb:
    def __init__(
        self,
        *,
        role_first_result: Role | None = None,
        role_all_result: list[Role] | None = None,
    ) -> None:
        self.added: list[object] = []
        self.flushed = False
        self.flush_calls = 0
        self.committed = False
        self.role_first_result = role_first_result
        self.role_all_result = role_all_result or []

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        self.flushed = True
        self.flush_calls += 1

    async def execute(self, statement: object) -> DummyQuery:
        del statement
        return DummyQuery(first_result=self.role_first_result, all_result=self.role_all_result)


def _as_async_session(db: DummyDb) -> AsyncSession:
    return cast("AsyncSession", db)


def _async_return(value: object | None) -> Callable[..., Awaitable[object | None]]:
    async def _wrapper(*args: object, **kwargs: object) -> object | None:
        del args, kwargs
        return value

    return _wrapper


def _async_return_from_map(mapping: dict[uuid.UUID, User]) -> Callable[[object, uuid.UUID], Awaitable[User | None]]:
    async def _wrapper(_db: object, user_id: uuid.UUID) -> User | None:
        return mapping.get(user_id)

    return _wrapper


async def test_create_user_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb(role_first_result=None)

    monkeypatch.setattr(service_account, "get_user_by_username", _async_return(None))
    monkeypatch.setattr(service_account, "get_user_by_phone", _async_return(None))
    monkeypatch.setattr(service_account, "get_role_by_name", _async_return(None))
    monkeypatch.setattr(service_account, "get_password_hash", lambda password: f"hashed:{password}")

    from app.account import service_auth

    monkeypatch.setattr(service_auth, "validate_password_complexity", lambda password: None)

    user = await service_account.create_user(
        _as_async_session(db),
        UserCreate(
            username="user-1",
            password="new-pass",
            phone="13800000000",
            name="Test User",
            roles=[],
        ),
    )

    assert user.username == "user-1"
    assert user.hashed_password == "hashed:new-pass"
    assert len(user.roles) == 1
    assert user.roles[0].name == RoleType.CLIENT
    assert db.flush_calls == 2
    assert db.committed is False


async def test_update_user_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    user = User(username="user-1", name="before")

    monkeypatch.setattr(service_account, "get_user", _async_return(user))

    updated_user = await service_account.update_user(
        _as_async_session(db), user.id, UserUpdate(name="after", avatar="avatar.png")
    )

    assert updated_user.name == "after"
    assert updated_user.avatar == "avatar.png"
    assert db.flushed is True
    assert db.committed is False


async def test_update_user_password_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    user = User(username="user-1", hashed_password="old-hash")

    monkeypatch.setattr(service_account, "get_user", _async_return(user))
    monkeypatch.setattr(
        service_account,
        "verify_password",
        lambda plain, hashed: (plain == "old-pass", False),
    )
    monkeypatch.setattr(service_account, "get_password_hash", lambda password: f"hashed:{password}")

    from app.account import service_auth

    monkeypatch.setattr(service_auth, "validate_password_complexity", lambda password: None)

    result = await service_account.update_user_password(_as_async_session(db), user.id, "old-pass", "new-pass")

    assert result is True
    assert user.hashed_password == "hashed:new-pass"
    assert db.flushed is True
    assert db.committed is False


async def test_update_user_phone_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    user = User(username="user-1", phone="13800000000")

    monkeypatch.setattr(service_account, "get_user", _async_return(user))
    monkeypatch.setattr(service_account, "get_user_by_phone", _async_return(None))

    from app.account import service_auth

    monkeypatch.setattr(service_auth, "verify_code", _async_return(True))

    result = await service_account.update_user_phone(_as_async_session(db), user.id, "13900000000", "123456")

    assert result is True
    assert user.phone == "13900000000"
    assert db.flushed is True
    assert db.committed is False


async def test_admin_reset_password_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    user = User(username="user-1", hashed_password="old-hash")

    monkeypatch.setattr(service_account, "get_user", _async_return(user))
    monkeypatch.setattr(service_account, "get_password_hash", lambda password: f"hashed:{password}")

    from app.account import service_auth

    monkeypatch.setattr(service_auth, "validate_password_complexity", lambda password: None)

    result = await service_account.admin_reset_password(_as_async_session(db), user.id, "reset-pass")

    assert result is True
    assert user.hashed_password == "hashed:reset-pass"
    assert db.flushed is True
    assert db.committed is False


async def test_delete_user_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    user = User(username="user-1", is_active=True)

    monkeypatch.setattr(service_account, "get_user", _async_return(user))

    result = await service_account.delete_user(_as_async_session(db), user.id)

    assert result is True
    assert user.is_active is False
    assert db.flushed is True
    assert db.committed is False


async def test_update_user_roles_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    role = Role(name=RoleType.ADMIN, description="admin")
    db = DummyDb(role_all_result=[role])
    user = User(username="user-1")

    monkeypatch.setattr(service_account, "get_user", _async_return(user))

    async def _execute(statement: object) -> DummyQuery:
        del statement
        return DummyQuery(all_result=[role])

    monkeypatch.setattr(db, "execute", _execute)

    updated_user = await service_account.update_user_roles(_as_async_session(db), user.id, [RoleType.ADMIN])

    assert updated_user.roles == [role]
    assert db.flushed is True
    assert db.committed is False


async def test_batch_delete_users_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    user_id_1 = uuid.uuid4()
    user_id_2 = uuid.uuid4()
    users = {
        user_id_1: User(username="user-1", is_active=True),
        user_id_2: User(username="user-2", is_active=True),
    }

    monkeypatch.setattr(service_account, "get_user", _async_return_from_map(users))

    result = await service_account.batch_delete_users(_as_async_session(db), [user_id_1, user_id_2])

    assert result == {"success": [str(user_id_1), str(user_id_2)], "failed": []}
    assert users[user_id_1].is_active is False
    assert users[user_id_2].is_active is False
    assert db.flushed is True
    assert db.committed is False
