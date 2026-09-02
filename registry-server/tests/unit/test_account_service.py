"""针对 account/service_account.py 的单元测试。

覆盖：get_user_async（找到 / 不存在 + raise_exception）、
create_user（成功路径、用户名重复、手机号重复、角色不存在）、
update_user（成功、用户不存在）、update_user_password（成功、
用户不存在、密码错误）、delete_user（成功、用户不存在）、
batch_delete_users（全成功、部分失败）、
_build_user_filter_clauses（各过滤字段）。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import cast
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.account.service_account as svc
from app.account.exception_account import AccountError, AccountErrorCode
from app.account.model import Role, RoleType, User
from app.account.schema_account import UserCreate, UserUpdate
from app.agent.model import EmailCode
from app.utils.utils import get_beijing_time

pytestmark = pytest.mark.unit

TEST_STORED_HASH = "$argon2id$fake-hash"
TEST_LOGIN_VALUE = "Str0ngP@ss!"


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------


class DummyScalarsResult:
    def __init__(self, items: list[object]) -> None:
        self._items = items

    def all(self) -> list[object]:
        return self._items

    def scalar_one_or_none(self) -> object | None:
        return self._items[0] if self._items else None


class DummyExecuteResult:
    def __init__(self, value: object | None = None, *, items: list[object] | None = None) -> None:
        self._value = value
        if items is not None:
            self._items = items
        elif value is not None:
            self._items = [value]
        else:
            self._items = []

    def scalar_one_or_none(self) -> object | None:
        return self._value

    def scalar_one(self) -> object:
        return self._value

    def scalars(self) -> DummyScalarsResult:
        return DummyScalarsResult(self._items)


class DummyAsyncSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushed = False
        self.committed = False
        self._execute_queue: list[DummyExecuteResult] = []

    def queue_result(self, value: object | None = None, *, items: list[object] | None = None) -> None:
        self._execute_queue.append(DummyExecuteResult(value, items=items))

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        await asyncio.sleep(0)
        self.flushed = True

    async def commit(self) -> None:
        await asyncio.sleep(0)
        self.committed = True

    async def execute(self, statement: object) -> DummyExecuteResult:
        del statement
        await asyncio.sleep(0)
        if self._execute_queue:
            return self._execute_queue.pop(0)
        return DummyExecuteResult(None)


def _as_async_session(session: DummyAsyncSession) -> AsyncSession:
    return cast("AsyncSession", session)


def _user(
    uid: uuid.UUID | None = None,
    username: str = "alice",
    phone: str = "13800138000",
    email: str | None = None,
) -> User:
    u = User()
    u.id = uid or uuid.uuid4()
    u.username = username
    u.phone = phone
    u.email = email
    u.name = "Alice"
    u.is_active = True
    u.hashed_password = TEST_STORED_HASH
    u.roles = []
    return u


def _role(name: RoleType = RoleType.CLIENT) -> Role:
    r = Role()
    r.id = uuid.uuid4()
    r.name = name
    r.description = "test role"
    return r


def _email_code(email: str, code: str = "ABCD") -> EmailCode:
    return EmailCode(email=email, code=code, expires_at=get_beijing_time())


# ---------------------------------------------------------------------------
# 针对 get_user_async 的测试
# ---------------------------------------------------------------------------


class TestGetUserAsync:
    async def test_returns_user_when_found(self) -> None:
        session = DummyAsyncSession()
        user = _user()
        session.queue_result(user)

        result = await svc.get_user_async(_as_async_session(session), user.id)
        assert result is user

    async def test_returns_none_when_not_found(self) -> None:
        session = DummyAsyncSession()
        session.queue_result(None)

        result = await svc.get_user_async(_as_async_session(session), uuid.uuid4())
        assert result is None

    async def test_raises_when_not_found_and_raise_exception_true(self) -> None:
        session = DummyAsyncSession()
        session.queue_result(None)

        with pytest.raises(AccountError) as exc_info:
            await svc.get_user_async(_as_async_session(session), uuid.uuid4(), raise_exception=True)

        assert exc_info.value.error_name == AccountErrorCode.USER_NOT_FOUND

    async def test_does_not_raise_when_found_with_raise_exception_true(self) -> None:
        session = DummyAsyncSession()
        user = _user()
        session.queue_result(user)

        result = await svc.get_user_async(_as_async_session(session), user.id, raise_exception=True)
        assert result is user


# ---------------------------------------------------------------------------
# 针对 create_user 的测试
# ---------------------------------------------------------------------------


class TestCreateUser:
    async def test_creates_user_with_default_role(self) -> None:
        session = DummyAsyncSession()
        # get_user_by_username 查询返回 None（无重复用户名）
        session.queue_result(None)
        # get_user_by_phone 查询返回 None（无重复手机号）
        session.queue_result(None)
        # get_role_by_name 查询返回 CLIENT 角色
        role = _role()
        session.queue_result(role)

        with (
            patch("app.account.service_auth.validate_password_complexity"),
            patch("app.account.service_account.get_password_hash", return_value="hashed"),
        ):
            user = await svc.create_user(
                _as_async_session(session),
                UserCreate(username="bob", phone="13900139000", password=TEST_LOGIN_VALUE, roles=[]),
            )

        assert user.username == "bob"
        assert session.flushed

    async def test_raises_when_username_already_taken(self) -> None:
        session = DummyAsyncSession()
        existing = _user(username="alice")
        session.queue_result(existing)

        with pytest.raises(AccountError) as exc_info:
            await svc.create_user(_as_async_session(session), UserCreate(username="alice", roles=[]))

        assert exc_info.value.error_name == AccountErrorCode.USERNAME_ALREADY_TAKEN

    async def test_raises_when_phone_already_registered(self) -> None:
        session = DummyAsyncSession()
        # username 查询无重复
        session.queue_result(None)
        # phone 查询存在重复
        session.queue_result(_user())

        with pytest.raises(AccountError) as exc_info:
            await svc.create_user(
                _as_async_session(session), UserCreate(username="new_user", phone="13800138000", roles=[])
            )

        assert exc_info.value.error_name == AccountErrorCode.PHONE_ALREADY_REGISTERED

    async def test_creates_user_with_explicit_roles(self) -> None:
        session = DummyAsyncSession()
        # username 查询返回 None（无重复）；user_data 无 phone，跳过 phone 检查
        session.queue_result(None)
        # roles 查询返回 [admin_role]
        admin_role = _role(RoleType.ADMIN)
        session.queue_result(items=[admin_role])

        with (
            patch("app.account.service_auth.validate_password_complexity"),
            patch("app.account.service_account.get_password_hash", return_value="hashed"),
        ):
            user = await svc.create_user(
                _as_async_session(session),
                UserCreate(username="admin_user", password=TEST_LOGIN_VALUE, roles=["ADMIN"]),
            )

        assert user.roles == [admin_role]

    async def test_raises_when_roles_not_found(self) -> None:
        session = DummyAsyncSession()
        session.queue_result(None)
        session.queue_result(None)
        # 角色查询返回结果少于请求数量
        session.queue_result(items=[])

        with (
            patch("app.account.service_auth.validate_password_complexity"),
            patch("app.account.service_account.get_password_hash", return_value="hashed"),
            pytest.raises(AccountError) as exc_info,
        ):
            await svc.create_user(
                _as_async_session(session),
                UserCreate(username="new_user", password=TEST_LOGIN_VALUE, roles=["NONEXISTENT_ROLE"]),
            )

        assert exc_info.value.error_name == AccountErrorCode.ROLES_NOT_FOUND


# ---------------------------------------------------------------------------
# 针对 update_user 的测试
# ---------------------------------------------------------------------------


class TestUpdateUser:
    async def test_updates_user_fields(self) -> None:
        session = DummyAsyncSession()
        user = _user()
        session.queue_result(user)

        result = await svc.update_user(_as_async_session(session), user.id, UserUpdate(name="Bob Updated"))

        assert result.name == "Bob Updated"
        assert session.flushed

    async def test_raises_when_user_not_found(self) -> None:
        session = DummyAsyncSession()
        session.queue_result(None)

        with pytest.raises(AccountError) as exc_info:
            await svc.update_user(_as_async_session(session), uuid.uuid4(), UserUpdate(name="X"))

        assert exc_info.value.error_name == AccountErrorCode.USER_NOT_FOUND

    async def test_raises_when_email_changes_without_code(self) -> None:
        session = DummyAsyncSession()
        user = _user(email="old@example.com")
        session.queue_result(user)

        with pytest.raises(AccountError, match="验证码不能为空"):
            await svc.update_user(
                _as_async_session(session),
                user.id,
                UserUpdate(email="new@example.com"),
            )

    async def test_updates_email_when_verification_code_is_valid(self) -> None:
        session = DummyAsyncSession()
        user = _user(email="old@example.com")
        session.queue_result(user)
        session.queue_result(_email_code("new@example.com"))

        result = await svc.update_user(
            _as_async_session(session),
            user.id,
            UserUpdate(email="new@example.com", email_code="ABCD"),
        )

        assert result.email == "new@example.com"
        assert session.flushed is True


class TestUpdateUserPasswordByCode:
    async def test_updates_password_and_marks_code_used(self) -> None:
        session = DummyAsyncSession()
        user = _user(email="alice@example.com")
        latest_code = _email_code("alice@example.com")
        session.queue_result(latest_code)
        session.queue_result(items=[user])

        with (
            patch("app.account.service_auth.validate_password_complexity"),
            patch("app.account.service_account.get_password_hash", return_value="new_hash"),
        ):
            result = await svc.update_user_password_by_code(
                _as_async_session(session),
                "alice@example.com",
                "ABCD",
                "New$tr0ngP@ss!",
            )

        assert result is True
        assert user.hashed_password == "new_hash"
        assert latest_code.used_at is not None
        assert session.committed is True


# ---------------------------------------------------------------------------
# 针对 update_user_password 的测试
# ---------------------------------------------------------------------------


class TestUpdateUserPassword:
    async def test_updates_password_successfully(self) -> None:
        session = DummyAsyncSession()
        user = _user()
        session.queue_result(user)

        with (
            patch("app.account.service_account.verify_password", return_value=(True, False)),
            patch("app.account.service_auth.validate_password_complexity"),
            patch("app.account.service_account.get_password_hash", return_value="new_hash"),
        ):
            result = await svc.update_user_password(
                _as_async_session(session),
                user.id,
                "old_pass",
                "New$tr0ngP@ss!",
            )

        assert result is True
        assert user.hashed_password == "new_hash"

    async def test_raises_when_user_not_found(self) -> None:
        session = DummyAsyncSession()
        session.queue_result(None)

        with pytest.raises(AccountError) as exc_info:
            await svc.update_user_password(_as_async_session(session), uuid.uuid4(), "old", "new")

        assert exc_info.value.error_name == AccountErrorCode.USER_NOT_FOUND

    async def test_raises_when_old_password_wrong(self) -> None:
        session = DummyAsyncSession()
        user = _user()
        session.queue_result(user)

        with (
            patch("app.account.service_account.verify_password", return_value=(False, False)),
            pytest.raises(AccountError) as exc_info,
        ):
            await svc.update_user_password(
                _as_async_session(session),
                user.id,
                "wrong_pass",
                "New$tr0ngP@ss!",
            )

        assert exc_info.value.error_name == AccountErrorCode.INCORRECT_PASSWORD


# ---------------------------------------------------------------------------
# 针对 delete_user 的测试
# ---------------------------------------------------------------------------


class TestDeleteUser:
    async def test_soft_deletes_user(self) -> None:
        session = DummyAsyncSession()
        user = _user()
        user.is_active = True
        session.queue_result(user)

        result = await svc.delete_user(_as_async_session(session), user.id)

        assert result is True
        assert user.is_active is False
        assert session.flushed

    async def test_raises_when_user_not_found(self) -> None:
        session = DummyAsyncSession()
        session.queue_result(None)

        with pytest.raises(AccountError) as exc_info:
            await svc.delete_user(_as_async_session(session), uuid.uuid4())

        assert exc_info.value.error_name == AccountErrorCode.USER_NOT_FOUND


class TestResetPassword:
    async def test_generates_and_sends_password_without_commit(self) -> None:
        session = DummyAsyncSession()
        user = _user(email="alice@example.com")
        session.queue_result(user)

        with (
            patch("app.account.service_account.generate_password", return_value="NewPass1!"),
            patch("app.account.service_account.send_password") as send_password_mock,
            patch("app.account.service_account.get_password_hash", return_value="hashed-new-pass"),
        ):
            result = await svc.reset_password(_as_async_session(session), user.id)

        assert result is True
        assert user.hashed_password == "hashed-new-pass"
        send_password_mock.assert_called_once_with("alice@example.com", "NewPass1!")
        assert session.flushed is True
        assert session.committed is False


# ---------------------------------------------------------------------------
# 针对 batch_delete_users 的测试
# ---------------------------------------------------------------------------


class TestBatchDeleteUsers:
    async def test_all_succeed(self) -> None:
        session = DummyAsyncSession()
        uid1 = uuid.uuid4()
        uid2 = uuid.uuid4()
        session.queue_result(_user(uid1))
        session.queue_result(_user(uid2))

        result = await svc.batch_delete_users(_as_async_session(session), [uid1, uid2])

        assert len(result["success"]) == 2
        assert result["failed"] == []

    async def test_partial_failure_when_user_not_found(self) -> None:
        session = DummyAsyncSession()
        uid1 = uuid.uuid4()
        uid2 = uuid.uuid4()
        # uid1 能找到，uid2 找不到
        session.queue_result(_user(uid1))
        session.queue_result(None)

        result = await svc.batch_delete_users(_as_async_session(session), [uid1, uid2])

        assert len(result["success"]) == 1
        assert len(result["failed"]) == 1
        assert result["failed"][0]["id"] == str(uid2)

    async def test_unexpected_error_is_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = DummyAsyncSession()
        uid1 = uuid.uuid4()
        uid2 = uuid.uuid4()
        first_user = _user(uid1)

        async def fake_get_user(current_session: object, user_id: uuid.UUID) -> User | None:
            del current_session
            await asyncio.sleep(0)
            if user_id == uid1:
                return first_user
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(svc, "get_user", fake_get_user)

        with pytest.raises(RuntimeError, match="database unavailable"):
            await svc.batch_delete_users(_as_async_session(session), [uid1, uid2])


# ---------------------------------------------------------------------------
# _build_user_filter_clauses
# ---------------------------------------------------------------------------


class TestBuildUserFilterClauses:
    def test_no_filters_returns_empty_clauses(self) -> None:
        clauses, requires_join = svc._build_user_filter_clauses(
            username=None, phone=None, name=None, role=None, is_active=None
        )
        assert clauses == []
        assert requires_join is False

    def test_username_filter_adds_clause(self) -> None:
        clauses, _ = svc._build_user_filter_clauses(username="alice", phone=None, name=None, role=None, is_active=None)
        assert len(clauses) == 1

    def test_role_filter_sets_requires_join(self) -> None:
        _, requires_join = svc._build_user_filter_clauses(
            username=None, phone=None, name=None, role="ADMIN", is_active=None
        )
        assert requires_join is True

    def test_all_filters_combined(self) -> None:
        clauses, requires_join = svc._build_user_filter_clauses(
            username="user", phone="139", name="Test", role="CLIENT", is_active=True
        )
        assert len(clauses) == 5
        assert requires_join is True

    def test_is_active_false_adds_clause(self) -> None:
        clauses, _ = svc._build_user_filter_clauses(username=None, phone=None, name=None, role=None, is_active=False)
        assert len(clauses) == 1
