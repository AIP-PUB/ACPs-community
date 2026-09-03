"""针对 agent/service.py 的核心异步业务逻辑单元测试。

覆盖：create_agent_async、update_agent_async（权限/状态校验分支）、
submit_agent_for_approval_async、cancel_agent_submission_async、
process_agent_approval_async、delete_agent_async、
disable_agent_async、enable_agent_async 的错误路径与正常路径。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.account.model import Role, RoleType, User
from app.agent import service as agent_service
from app.agent.exception import AgentError, AgentErrorCode
from app.agent.model import Agent, ApprovalStatus
from app.utils import acs as acs_utils
from app.utils import aic as aic_module

pytestmark = pytest.mark.unit


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
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        return self._value

    def scalars(self) -> DummyScalarsResult:
        return DummyScalarsResult([self._value] if self._value is not None else [])


class DummyAsyncSession:
    """最小化的 AsyncSession 桩对象。"""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushed = False
        self._execute_queue: list[object | None] = []  # 顺序返回的结果
        self._default_result: object | None = None

    def queue_result(self, value: object | None) -> None:
        """入队一个 execute 调用的返回值。"""
        self._execute_queue.append(value)

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        await asyncio.sleep(0)
        self.flushed = True

    async def execute(self, statement: object) -> DummyExecuteResult:
        del statement
        await asyncio.sleep(0)
        if self._execute_queue:
            return DummyExecuteResult(self._execute_queue.pop(0))
        return DummyExecuteResult(self._default_result)


def _as_async_session(session: DummyAsyncSession) -> AsyncSession:
    return cast("AsyncSession", session)


def _build_agent(
    *,
    user_id: uuid.UUID | None = None,
    status: ApprovalStatus = ApprovalStatus.DRAFT,
    is_active: bool = True,
    is_deleted: bool = False,
    is_disabled: bool = False,
    is_ontology: bool = False,
    aic: str | None = None,
    acs_hash: str | None = None,
) -> Agent:
    owner_id = user_id or uuid.uuid4()
    return Agent(
        id=uuid.uuid4(),
        name=f"agent-{uuid.uuid4().hex[:6]}",
        version="1.0.0",
        created_by_id=owner_id,
        approval_status=status,
        is_active=is_active,
        is_deleted=is_deleted,
        is_disabled=is_disabled,
        is_ontology=is_ontology,
        aic=aic,
        acs_hash=acs_hash,
    )


def _build_staff_user() -> User:
    user = User(id=uuid.uuid4(), username="staff-user", is_active=True)
    user.roles = [Role(name=RoleType.STAFF, description="staff")]
    return user


def _build_client_user() -> User:
    user = User(id=uuid.uuid4(), username="client-user", is_active=True)
    user.roles = [Role(name=RoleType.CLIENT, description="client")]
    return user


# ---------------------------------------------------------------------------
# 针对 create_agent_async 的测试
# ---------------------------------------------------------------------------


class TestCreateAgentAsync:
    async def test_name_already_claimed_by_other_user_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        other_agent = _build_agent()
        session = DummyAsyncSession()
        # 第一次 execute -> 模拟找到其他用户拥有该名称
        session.queue_result(other_agent)

        with pytest.raises(AgentError) as exc_info:
            await agent_service.create_agent_async(
                _as_async_session(session), user_id, {"name": "MyAgent", "version": "1.0"}
            )
        assert exc_info.value.error_name == AgentErrorCode.AGENT_NAME_ALREADY_CLAIMED

    async def test_name_version_already_exists_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        existing = _build_agent()
        session = DummyAsyncSession()
        session.queue_result(None)  # 第一次查询：无其他用户拥有
        session.queue_result(existing)  # 第二次查询：name+version 已存在

        with pytest.raises(AgentError) as exc_info:
            await agent_service.create_agent_async(
                _as_async_session(session), user_id, {"name": "MyAgent", "version": "1.0"}
            )
        assert exc_info.value.error_name == AgentErrorCode.AGENT_NAME_VERSION_EXISTS

    async def test_successful_creation_adds_agent_and_flushes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        session = DummyAsyncSession()
        session.queue_result(None)  # 无 name_owner
        session.queue_result(None)  # 无 existing_agent

        agent = await agent_service.create_agent_async(
            _as_async_session(session),
            user_id,
            {"name": "NewAgent", "version": "2.0", "description": "<b>async-agent</b>"},
        )

        assert agent.name == "NewAgent"
        assert agent.version == "2.0"
        assert agent.description == "async-agent"
        assert agent.created_by_id == user_id
        assert agent.approval_status == ApprovalStatus.DRAFT
        assert session.flushed is True
        assert agent in session.added

    async def test_invalid_acs_json_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        session = DummyAsyncSession()
        session.queue_result(None)
        session.queue_result(None)

        monkeypatch.setattr(acs_utils, "validate", lambda value: None)

        with pytest.raises(AgentError) as exc_info:
            await agent_service.create_agent_async(
                _as_async_session(session),
                user_id,
                {"name": "NewAgent", "version": "2.0", "acs": "not-json"},
            )

        assert exc_info.value.error_name == AgentErrorCode.AGENT_CREATE_FAILED


# ---------------------------------------------------------------------------
# 针对 submit_agent_for_approval_async 的测试
# ---------------------------------------------------------------------------


class TestSubmitAgentForApprovalAsync:
    async def test_submit_draft_agent_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id, status=ApprovalStatus.DRAFT)
        session = DummyAsyncSession()

        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        result = await agent_service.submit_agent_for_approval_async(_as_async_session(session), agent.id, user_id)

        assert result.approval_status == ApprovalStatus.PENDING
        assert result.submitted_at is not None
        assert session.flushed is True

    async def test_submit_rejected_agent_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id, status=ApprovalStatus.REJECTED)
        agent.processed_by_id = uuid.uuid4()
        agent.processed_at = datetime.now(UTC)
        agent.process_comments = "old reject"
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        result = await agent_service.submit_agent_for_approval_async(_as_async_session(session), agent.id, user_id)
        assert result.approval_status == ApprovalStatus.PENDING
        assert result.processed_by_id is None
        assert result.processed_at is None
        assert result.process_comments is None

    async def test_inactive_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id, is_active=False)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.submit_agent_for_approval_async(_as_async_session(session), agent.id, user_id)
        assert exc_info.value.error_name == AgentErrorCode.AGENT_INACTIVE

    async def test_wrong_owner_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        owner_id = uuid.uuid4()
        other_id = uuid.uuid4()
        agent = _build_agent(user_id=owner_id, status=ApprovalStatus.DRAFT)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.submit_agent_for_approval_async(_as_async_session(session), agent.id, other_id)
        assert exc_info.value.error_name == AgentErrorCode.UNAUTHORIZED_ACCESS

    async def test_already_pending_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id, status=ApprovalStatus.PENDING)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.submit_agent_for_approval_async(_as_async_session(session), agent.id, user_id)
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION

    async def test_submit_deleted_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id, status=ApprovalStatus.DRAFT, is_active=False, is_deleted=True)
        agent.deleted_at = datetime.now(UTC)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.submit_agent_for_approval_async(_as_async_session(session), agent.id, user_id)
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION

    async def test_submit_disabled_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id, status=ApprovalStatus.DRAFT, is_active=False, is_disabled=True)
        agent.disabled_at = datetime.now(UTC)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.submit_agent_for_approval_async(_as_async_session(session), agent.id, user_id)
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION


# ---------------------------------------------------------------------------
# 针对 cancel_agent_submission_async 的测试
# ---------------------------------------------------------------------------


class TestCancelAgentSubmissionAsync:
    async def test_cancel_pending_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id, status=ApprovalStatus.PENDING)
        agent.processed_by_id = uuid.uuid4()
        agent.processed_at = datetime.now(UTC)
        agent.process_comments = "stale review"
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        result = await agent_service.cancel_agent_submission_async(_as_async_session(session), agent.id, user_id)
        assert result.approval_status == ApprovalStatus.DRAFT
        assert result.submitted_at is None
        assert result.processed_by_id is None
        assert result.processed_at is None
        assert result.process_comments is None
        assert session.flushed is True

    async def test_cancel_non_pending_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id, status=ApprovalStatus.DRAFT)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.cancel_agent_submission_async(_as_async_session(session), agent.id, user_id)
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION

    async def test_cancel_other_user_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        owner_id = uuid.uuid4()
        other_id = uuid.uuid4()
        agent = _build_agent(user_id=owner_id, status=ApprovalStatus.PENDING)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.cancel_agent_submission_async(_as_async_session(session), agent.id, other_id)
        assert exc_info.value.error_name == AgentErrorCode.UNAUTHORIZED_ACCESS

    async def test_cancel_deleted_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id, status=ApprovalStatus.PENDING, is_active=False, is_deleted=True)
        agent.deleted_at = datetime.now(UTC)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.cancel_agent_submission_async(_as_async_session(session), agent.id, user_id)
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION


# ---------------------------------------------------------------------------
# 针对 process_agent_approval_async 的测试
# ---------------------------------------------------------------------------


class TestProcessAgentApprovalAsync:
    async def test_approve_pending_agent_applies_provider_before_mark_processed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staff = _build_staff_user()
        agent = _build_agent(status=ApprovalStatus.PENDING, aic="already-has-aic")
        session = DummyAsyncSession()
        events: list[str] = []

        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))

        async def fake_apply_verified_provider_snapshot_async(*args: object, **kwargs: object) -> bool:
            del args, kwargs
            events.append("provider")
            return False

        def fake_mark_agent_processed(*args: object, **kwargs: object) -> None:
            del args, kwargs
            events.append("mark")
            agent.approval_status = ApprovalStatus.APPROVED
            agent.processed_by_id = staff.id

        monkeypatch.setattr(
            agent_service,
            "apply_verified_provider_snapshot_async",
            fake_apply_verified_provider_snapshot_async,
        )
        monkeypatch.setattr(agent_service, "_mark_agent_processed", fake_mark_agent_processed)

        await agent_service.process_agent_approval_async(_as_async_session(session), agent.id, staff.id, approve=True)

        assert events == ["provider", "mark"]

    async def test_provider_snapshot_failure_keeps_agent_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff = _build_staff_user()
        agent = _build_agent(status=ApprovalStatus.PENDING)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))

        async def fake_apply_verified_provider_snapshot_async(*args: object, **kwargs: object) -> bool:
            del args, kwargs
            raise AgentError(
                status_code=409,
                error_name=AgentErrorCode.INVALID_ACS,
                error_msg="provider snapshot failed",
            )

        monkeypatch.setattr(
            agent_service,
            "apply_verified_provider_snapshot_async",
            fake_apply_verified_provider_snapshot_async,
        )

        with pytest.raises(AgentError) as exc_info:
            await agent_service.process_agent_approval_async(
                _as_async_session(session), agent.id, staff.id, approve=True
            )

        assert exc_info.value.error_name == AgentErrorCode.INVALID_ACS
        assert agent.approval_status == ApprovalStatus.PENDING

    async def test_approve_existing_aic_updates_changelog_when_provider_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staff = _build_staff_user()
        agent = _build_agent(status=ApprovalStatus.PENDING, aic="already-has-aic")
        agent.acs = {"provider": {"name": "trusted"}}
        session = DummyAsyncSession()
        update_mock = AsyncMock()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))
        monkeypatch.setattr(agent_service, "apply_verified_provider_snapshot_async", AsyncMock(return_value=True))
        monkeypatch.setattr(agent_service, "update_agent_with_changelog_async", update_mock)

        result = await agent_service.process_agent_approval_async(
            _as_async_session(session), agent.id, staff.id, approve=True
        )

        assert result.approval_status == ApprovalStatus.APPROVED
        update_mock.assert_awaited_once_with(_as_async_session(session), agent, {"acs": agent.acs})
        assert session.flushed is True

    async def test_approve_pending_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff = _build_staff_user()
        agent = _build_agent(status=ApprovalStatus.PENDING, aic="already-has-aic")
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))

        result = await agent_service.process_agent_approval_async(
            _as_async_session(session), agent.id, staff.id, approve=True
        )
        assert result.approval_status == ApprovalStatus.APPROVED
        assert result.processed_by_id == staff.id

    async def test_reject_pending_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff = _build_staff_user()
        agent = _build_agent(status=ApprovalStatus.PENDING)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))

        result = await agent_service.process_agent_approval_async(
            _as_async_session(session),
            agent.id,
            staff.id,
            approve=False,
            comments="<b>不符合要求</b>",
        )
        assert result.approval_status == ApprovalStatus.REJECTED
        assert result.process_comments == "不符合要求"
        assert session.flushed is True

    async def test_processor_not_found_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _build_agent(status=ApprovalStatus.PENDING)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=None))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.process_agent_approval_async(
                _as_async_session(session), agent.id, uuid.uuid4(), approve=True
            )
        assert exc_info.value.error_name == AgentErrorCode.PROCESSOR_NOT_FOUND

    async def test_non_staff_processor_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client_user()
        agent = _build_agent(status=ApprovalStatus.PENDING)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=client))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.process_agent_approval_async(
                _as_async_session(session), agent.id, client.id, approve=True
            )
        assert exc_info.value.error_name == AgentErrorCode.PROCESSOR_NOT_STAFF

    async def test_inactive_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff = _build_staff_user()
        agent = _build_agent(status=ApprovalStatus.PENDING, is_active=False)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.process_agent_approval_async(
                _as_async_session(session), agent.id, staff.id, approve=True
            )
        assert exc_info.value.error_name == AgentErrorCode.AGENT_INACTIVE

    async def test_non_pending_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff = _build_staff_user()
        agent = _build_agent(status=ApprovalStatus.APPROVED, aic="already-has-aic")
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.process_agent_approval_async(
                _as_async_session(session), agent.id, staff.id, approve=True
            )
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION

    async def test_process_deleted_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff = _build_staff_user()
        agent = _build_agent(status=ApprovalStatus.PENDING, is_active=False, is_deleted=True)
        agent.deleted_at = datetime.now(UTC)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.process_agent_approval_async(
                _as_async_session(session), agent.id, staff.id, approve=True
            )
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION


# ---------------------------------------------------------------------------
# 针对 delete_agent_async 的测试
# ---------------------------------------------------------------------------


class TestDeleteAgentAsync:
    async def test_owner_can_delete_own_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "update_agent_acs_data_async", AsyncMock())
        monkeypatch.setattr(agent_service, "notify_ca_server_revoke_cert", lambda *a, **kw: None)

        result = await agent_service.delete_agent_async(_as_async_session(session), agent.id, user_id)
        assert result is True
        assert agent.is_deleted is True
        assert agent.is_active is False
        assert session.flushed is True

    async def test_non_owner_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        owner_id = uuid.uuid4()
        other_id = uuid.uuid4()
        agent = _build_agent(user_id=owner_id)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.delete_agent_async(_as_async_session(session), agent.id, other_id)
        assert exc_info.value.error_name == AgentErrorCode.UNAUTHORIZED_ACCESS

    async def test_delete_deleted_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id, is_deleted=True)
        agent.deleted_at = datetime.now(UTC)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.delete_agent_async(_as_async_session(session), agent.id, user_id)
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION


# ---------------------------------------------------------------------------
# 针对 disable_agent_async / enable_agent_async 的测试
# ---------------------------------------------------------------------------


class TestDisableEnableAgentAsync:
    async def test_disable_by_staff_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff = _build_staff_user()
        agent = _build_agent(is_active=True, is_disabled=False)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))
        monkeypatch.setattr(agent_service, "update_agent_acs_data_async", AsyncMock())
        monkeypatch.setattr(agent_service, "notify_ca_server_revoke_cert", lambda *a, **kw: None)

        result = await agent_service.disable_agent_async(_as_async_session(session), agent.id, staff.id)
        assert result.is_disabled is True
        assert result.is_active is False
        assert session.flushed is True

    async def test_disable_by_non_staff_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client_user()
        agent = _build_agent()
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=client))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.disable_agent_async(_as_async_session(session), agent.id, client.id)
        assert exc_info.value.error_name == AgentErrorCode.PROCESSOR_NOT_STAFF

    async def test_disable_staff_not_found_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _build_agent()
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=None))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.disable_agent_async(_as_async_session(session), agent.id, uuid.uuid4())
        assert exc_info.value.error_name == AgentErrorCode.PROCESSOR_NOT_FOUND

    async def test_disable_deleted_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff = _build_staff_user()
        agent = _build_agent(is_active=False, is_deleted=True)
        agent.deleted_at = datetime.now(UTC)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.disable_agent_async(_as_async_session(session), agent.id, staff.id)
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION

    async def test_disable_already_disabled_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff = _build_staff_user()
        agent = _build_agent(is_active=False, is_disabled=True)
        agent.disabled_at = datetime.now(UTC)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.disable_agent_async(_as_async_session(session), agent.id, staff.id)
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION

    async def test_enable_by_staff_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff = _build_staff_user()
        agent = _build_agent(is_active=False, is_disabled=True, is_deleted=False)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))
        monkeypatch.setattr(agent_service, "update_agent_acs_data_async", AsyncMock())

        result = await agent_service.enable_agent_async(_as_async_session(session), agent.id, staff.id)
        assert result.is_disabled is False
        assert result.is_active is True
        assert session.flushed is True

    async def test_enable_by_non_staff_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client_user()
        agent = _build_agent(is_disabled=True)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=client))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.enable_agent_async(_as_async_session(session), agent.id, client.id)
        assert exc_info.value.error_name == AgentErrorCode.PROCESSOR_NOT_STAFF

    async def test_enable_non_disabled_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff = _build_staff_user()
        agent = _build_agent(is_active=True, is_disabled=False)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.enable_agent_async(_as_async_session(session), agent.id, staff.id)
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION

    async def test_enable_deleted_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff = _build_staff_user()
        agent = _build_agent(is_active=False, is_deleted=True, is_disabled=True)
        agent.deleted_at = datetime.now(UTC)
        agent.disabled_at = datetime.now(UTC)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))
        monkeypatch.setattr(agent_service, "get_user_async", AsyncMock(return_value=staff))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.enable_agent_async(_as_async_session(session), agent.id, staff.id)
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION


# ---------------------------------------------------------------------------
# 针对 update_agent_async 的权限与状态校验测试
# ---------------------------------------------------------------------------


class TestUpdateAgentAsyncValidation:
    async def test_update_inactive_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id, is_active=False)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.update_agent_async(_as_async_session(session), agent.id, user_id, {"name": "new-name"})
        assert exc_info.value.error_name == AgentErrorCode.AGENT_INACTIVE

    async def test_update_other_user_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        owner_id = uuid.uuid4()
        other_id = uuid.uuid4()
        agent = _build_agent(user_id=owner_id, status=ApprovalStatus.DRAFT)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.update_agent_async(_as_async_session(session), agent.id, other_id, {"name": "hacked"})
        assert exc_info.value.error_name == AgentErrorCode.UNAUTHORIZED_ACCESS

    @pytest.mark.parametrize(
        "approval_status",
        [ApprovalStatus.APPROVED, ApprovalStatus.PENDING],
    )
    async def test_update_approved_or_pending_raises(
        self, monkeypatch: pytest.MonkeyPatch, approval_status: ApprovalStatus
    ) -> None:
        user_id = uuid.uuid4()
        agent = _build_agent(user_id=user_id, status=approval_status)
        session = DummyAsyncSession()
        monkeypatch.setattr(agent_service, "get_agent_async", AsyncMock(return_value=agent))

        with pytest.raises(AgentError) as exc_info:
            await agent_service.update_agent_async(_as_async_session(session), agent.id, user_id, {"name": "new-name"})
        assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION


class TestUpdateAgentAcsDataAsync:
    async def test_replaces_aic_placeholder_and_triggers_changelog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fixed_time = datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC)
        agent = Agent(
            id=uuid.uuid4(),
            name="demo-agent",
            version="1.0.0",
            created_by_id=uuid.uuid4(),
            approval_status=ApprovalStatus.APPROVED,
            is_active=True,
            aic="test-aic-123",
            acs={
                "name": "Test Agent",
                "version": "1.0.0",
                "active": True,
                "endPoints": [
                    {"url": "https://agent.example.com/rpc", "transport": "JSONRPC"},
                    {"url": "amqps://mq.example.com:5671/acps?inbox=inbox_{AIC}", "transport": "AMQP"},
                ],
            },
        )
        session = DummyAsyncSession()
        update_changelog = AsyncMock()

        monkeypatch.setattr(agent_service, "get_beijing_time", lambda: fixed_time)
        monkeypatch.setattr(agent_service, "update_agent_with_changelog_async", update_changelog)

        await agent_service.update_agent_acs_data_async(agent, _as_async_session(session))

        assert agent.acs is not None
        assert agent.acs["endPoints"][1]["url"] == "amqps://mq.example.com:5671/acps?inbox=inbox_test-aic-123"
        assert agent.acs["lastModifiedTime"] == fixed_time.isoformat()
        update_changelog.assert_awaited_once()


class TestRegisterEntityAsync:
    async def test_successful_registration_adds_agent_and_flushes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ontology_aic = aic_module.generate_ontology_aic(manager_code="1", provider_code="00001")
        entity_aic = aic_module.generate_entity_aic_from_ontology(ontology_aic)
        assert entity_aic is not None

        ontology_agent = _build_agent(
            user_id=uuid.uuid4(),
            status=ApprovalStatus.APPROVED,
            is_active=True,
            is_deleted=False,
            is_disabled=False,
            is_ontology=True,
            aic=ontology_aic,
        )
        ontology_agent.acs = {"name": "Ontology", "version": "1.0.0"}
        ontology_agent.description = "demo"
        ontology_agent.logo_url = None

        session = DummyAsyncSession()
        session.queue_result(ontology_agent)
        session.queue_result(None)
        create_change_log = AsyncMock(return_value=SimpleNamespace(seq=123))

        monkeypatch.setattr(
            aic_module,
            "generate_entity_aic_from_ontology",
            lambda ontology: entity_aic,
        )
        monkeypatch.setattr(agent_service, "create_change_log_async", create_change_log)

        result = await agent_service.register_entity_async(_as_async_session(session), ontology_aic=ontology_aic)

        registered_agent = session.added[0]
        assert isinstance(registered_agent, Agent)
        assert result["ontologyAic"] == ontology_aic
        assert result["entityAic"] == entity_aic
        assert session.flushed is True
        assert registered_agent.aic == entity_aic
        assert registered_agent.acs_last_seq == 123
