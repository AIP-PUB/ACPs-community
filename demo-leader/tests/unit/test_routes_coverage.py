from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from acps_sdk.oidc import HumanPrincipal, build_principal_id, build_principal_key
from assistant.api import routes
from assistant.api.schemas import CancelRequest, SubmitRequest, SubmitResponse, SubmitResult
from assistant.models import (
    ActiveTaskStatus,
    EventLogEntry,
    EventLogType,
    ExecutionMode,
    ScenarioRuntime,
    UserResult,
    UserResultType,
)
from assistant.models.exceptions import (
    ActiveTaskMismatchError,
    DuplicateRequestError,
    LeaderAgentError,
    LLMCallError,
    LLMParseError,
    ModeMismatchError,
    SessionClosedError,
    SessionExpiredError,
    SessionNotFoundError,
)
from fastapi import HTTPException

NOW = "2026-06-30T00:00:00+00:00"


@pytest.fixture(autouse=True)
def reset_route_globals():
    old = (routes._orchestrator, routes._session_manager, routes._task_execution_manager)
    routes._orchestrator = None
    routes._session_manager = None
    routes._task_execution_manager = None
    yield
    routes._orchestrator, routes._session_manager, routes._task_execution_manager = old


def _principal(principal_id: str = "user-1", *, roles: tuple[str, ...] = ()) -> HumanPrincipal:
    issuer = "https://issuer.example"
    subject = principal_id
    return HumanPrincipal(
        issuer=issuer,
        subject=subject,
        principal_key=build_principal_key(issuer=issuer, subject=subject),
        principal_id=build_principal_id(issuer=issuer, subject=subject),
        audiences=("leader",),
        roles=roles,
    )


def _submit_request() -> SubmitRequest:
    return SubmitRequest(clientRequestId="req-1", query="hello")


def _submit_response() -> SubmitResponse:
    return SubmitResponse(
        result=SubmitResult(
            sessionId="sess-1",
            mode=ExecutionMode.DIRECT_RPC,
            activeTaskId="task-1",
            acceptedAt=NOW,
            externalStatus=ActiveTaskStatus.RUNNING,
        )
    )


def _group_runtime() -> dict:
    return {
        "session_id": "sess-1",
        "group_id": "group-sess-1",
        "leader_aic": "leader-aic",
        "state": "active",
        "total_members": 1,
        "connected_members": 1,
        "pending_invitations": [],
        "members": [
            {
                "partner_aic": "partner-a",
                "invitation_route": "rpc",
                "connected": True,
                "muted": False,
                "connection_name": "conn",
                "vhost": "acps",
                "node_name": "rabbit@node",
                "queue_name": "queue",
                "joined_at": NOW,
            }
        ],
    }


@pytest.fixture
def sample_session():
    return SimpleNamespace(
        session_id="test-session-001",
        mode=ExecutionMode.DIRECT_RPC,
        user_id=None,
        created_at=NOW,
        updated_at=NOW,
        touched_at=NOW,
        ttl_seconds=3600,
        expires_at="2026-06-30T01:00:00+00:00",
        closed=False,
        closed_at=None,
        closed_reason=None,
        group_id=None,
        base_scenario=ScenarioRuntime(id="base", kind="base", version="1.0.0", loadedAt=NOW),
        expert_scenario=None,
        scenario_briefs=[],
        active_task=None,
        partners={},
        user_context={},
        dialog_context=None,
        event_log=[],
        user_result=UserResult(type=UserResultType.PENDING, dataItems=[], updatedAt=NOW),
    )


def test_route_getters_require_initialization() -> None:
    with pytest.raises(RuntimeError, match="Routes not initialized"):
        routes._get_orchestrator()
    with pytest.raises(RuntimeError, match="Routes not initialized"):
        routes._get_session_manager()

    manager = routes._get_task_execution_manager()
    assert manager is routes._task_execution_manager


@pytest.mark.asyncio
async def test_submit_success_and_error_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = MagicMock()
    orchestrator.handle_submit = AsyncMock(return_value=_submit_response())
    session_manager = MagicMock()
    session_manager.get_session.return_value = None
    routes.init_routes(orchestrator, session_manager)
    monkeypatch.setattr(routes, "oidc_enabled", lambda: False)

    response = await routes.submit(_submit_request(), principal=None)
    assert response.result is not None
    assert response.result.session_id == "sess-1"

    cases = [
        (SessionNotFoundError("missing"), 404),
        (SessionExpiredError("expired"), 404),
        (SessionClosedError("closed"), 403),
        (ModeMismatchError("sess", "direct_rpc", "group"), 409),
        (ActiveTaskMismatchError("sess", "old", "new"), 409),
        (DuplicateRequestError("sess", "req"), 409),
        (LLMCallError("llm down"), 500),
        (LLMParseError("bad json"), 500),
        (LeaderAgentError(500123, "leader failed"), 500),
        (RuntimeError("boom"), 500),
    ]
    for exc, status_code in cases:
        orchestrator.handle_submit = AsyncMock(side_effect=exc)
        with pytest.raises(HTTPException) as raised:
            await routes.submit(_submit_request(), principal=None)
        assert raised.value.status_code == status_code


@pytest.mark.asyncio
async def test_submit_binds_oidc_user_for_existing_session(sample_session, monkeypatch: pytest.MonkeyPatch) -> None:
    orchestrator = MagicMock()
    orchestrator.handle_submit = AsyncMock(return_value=_submit_response())
    session_manager = MagicMock()
    session_manager.get_session.return_value = sample_session
    routes.init_routes(orchestrator, session_manager)
    monkeypatch.setattr(routes, "oidc_enabled", lambda: True)
    monkeypatch.setattr(routes, "_ensure_session_access", lambda *args, **kwargs: None)
    bind_mock = MagicMock()
    monkeypatch.setattr(routes, "bind_session_principal", bind_mock)
    principal = _principal("user-oidc")

    request = _submit_request()
    request.session_id = sample_session.session_id
    await routes.submit(request, principal=principal)

    assert request.user_id == principal.principal_id
    bind_mock.assert_called_once_with(sample_session, principal)


def test_session_group_id_resolution_uses_existing_manager_and_fallback(sample_session) -> None:
    sample_session.mode = ExecutionMode.GROUP
    sample_session.group_id = "existing"
    assert routes._resolve_session_group_id(sample_session) == "existing"

    sample_session.group_id = None
    group_manager = MagicMock()
    group_manager.get_group_id.return_value = "group-from-manager"
    routes._session_manager = MagicMock(get_group_manager=MagicMock(return_value=group_manager))
    assert routes._resolve_session_group_id(sample_session) == "group-from-manager"

    sample_session.group_id = None
    group_manager.get_group_id.return_value = None
    assert routes._resolve_session_group_id(sample_session) is not None

    sample_session.mode = ExecutionMode.DIRECT_RPC
    sample_session.group_id = None
    assert routes._derive_session_group_id(sample_session) is None


@pytest.mark.asyncio
async def test_result_log_group_and_member_routes(sample_session, monkeypatch: pytest.MonkeyPatch) -> None:
    group_manager = MagicMock()
    group_manager.get_group_id.return_value = None
    group_manager.get_group_runtime.return_value = _group_runtime()
    group_manager.request_partner_leave = AsyncMock()
    group_manager.force_remove_partner = AsyncMock(
        return_value={
            "session_id": "sess-1",
            "group_id": "group-sess-1",
            "partner_aic": "partner-a",
            "queue_deleted": True,
        }
    )
    session_manager = MagicMock()
    session_manager.get_session.return_value = sample_session
    session_manager.get_group_manager.return_value = group_manager
    routes._session_manager = session_manager
    monkeypatch.setattr(routes, "oidc_enabled", lambda: False)

    result = await routes.get_result(sample_session.session_id)
    assert result.result is not None
    assert result.result.session_id == sample_session.session_id

    sample_session.event_log = [
        EventLogEntry(
            id=f"ev-{idx}",
            createdAt=NOW,
            type=EventLogType.USER_SUBMIT,
            sessionId=sample_session.session_id,
            payload={"idx": idx},
        )
        for idx in range(3)
    ]
    log_response = await routes.get_log(sample_session.session_id, limit=2)
    assert log_response.result is not None
    assert len(log_response.result.items) == 2
    assert log_response.result.has_more is True

    runtime_response = await routes.get_group_runtime(sample_session.session_id)
    assert runtime_response.result is not None
    assert runtime_response.result.group_id == "group-sess-1"

    leave_response = await routes.request_group_member_leave(sample_session.session_id, "partner-a")
    assert leave_response.result is not None
    assert leave_response.result.action == "request-leave"
    group_manager.request_partner_leave.assert_awaited_once_with(sample_session.session_id, "partner-a")

    remove_response = await routes.force_remove_group_member(sample_session.session_id, "partner-a")
    assert remove_response.result is not None
    assert remove_response.result.queue_deleted is True


@pytest.mark.asyncio
async def test_group_routes_translate_value_errors(sample_session, monkeypatch: pytest.MonkeyPatch) -> None:
    group_manager = MagicMock()
    group_manager.get_group_runtime.side_effect = ValueError("no group")
    group_manager.request_partner_leave = AsyncMock(side_effect=ValueError("no member"))
    group_manager.force_remove_partner = AsyncMock(side_effect=ValueError("no member"))
    session_manager = MagicMock()
    session_manager.get_session.return_value = sample_session
    session_manager.get_group_manager.return_value = group_manager
    routes._session_manager = session_manager
    monkeypatch.setattr(routes, "oidc_enabled", lambda: False)

    with pytest.raises(HTTPException) as runtime_err:
        await routes.get_group_runtime(sample_session.session_id)
    assert runtime_err.value.status_code == 404

    group_manager.get_group_runtime.side_effect = None
    group_manager.get_group_runtime.return_value = _group_runtime()
    with pytest.raises(HTTPException) as leave_err:
        await routes.request_group_member_leave(sample_session.session_id, "partner-a")
    assert leave_err.value.status_code == 404

    with pytest.raises(HTTPException) as remove_err:
        await routes.force_remove_group_member(sample_session.session_id, "partner-a")
    assert remove_err.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_task_closes_session_and_notifies_nonterminal_partners(
    sample_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partner_task = SimpleNamespace(state="Working", aip_task_id="aip-task-1")
    sample_session.active_task = SimpleNamespace(
        active_task_id="task-1",
        partner_tasks={"partner-a": partner_task},
    )
    sample_session.partners = {
        "partner-a": SimpleNamespace(resolved_endpoint=SimpleNamespace(url="http://partner/rpc")),
    }
    session_manager = MagicMock()
    session_manager.get_session.return_value = sample_session
    session_manager.update_session = MagicMock()
    session_manager.delete_session = AsyncMock(return_value=True)
    routes._session_manager = session_manager
    executor = SimpleNamespace(cancel_partner=AsyncMock(side_effect=RuntimeError("cancel failed")))
    routes._orchestrator = SimpleNamespace(_executor=executor)
    monkeypatch.setattr(routes, "oidc_enabled", lambda: False)
    monkeypatch.setattr(routes.LEADER_EMITTER, "emit", AsyncMock(side_effect=RuntimeError("audit down")))

    response = await routes.cancel_task(
        sample_session.session_id,
        SimpleNamespace(delete_session=False, user_id="user-1"),
    )

    assert response.result is not None
    assert response.result.cancelled_tasks == ["task-1"]
    assert response.result.session_deleted is False
    assert sample_session.closed is True
    assert sample_session.active_task is None
    executor.cancel_partner.assert_awaited_once()
    session_manager.update_session.assert_called_once()

    sample_session.active_task = SimpleNamespace(active_task_id="task-2", partner_tasks={})
    response_deleted = await routes.cancel_task(
        sample_session.session_id,
        CancelRequest(deleteSession=True),
    )
    assert response_deleted.result is not None
    assert response_deleted.result.session_deleted is True
    session_manager.delete_session.assert_awaited_once_with(sample_session.session_id)


@pytest.mark.asyncio
async def test_stream_token_and_health_paths(sample_session, monkeypatch: pytest.MonkeyPatch) -> None:
    session_manager = MagicMock()
    session_manager.get_session.return_value = sample_session
    routes._session_manager = session_manager
    routes._orchestrator = MagicMock()
    principal = _principal("stream-user")
    monkeypatch.setattr(routes, "oidc_enabled", lambda: False)

    with pytest.raises(HTTPException) as missing_principal:
        await routes.create_stream_token(sample_session.session_id, principal=None)
    assert missing_principal.value.status_code == 401

    monkeypatch.setattr(routes, "issue_stream_token", lambda **kwargs: ("token-1", 1780000000.0))
    monkeypatch.setattr(routes, "can_manage_group", lambda _principal: False)
    response = await routes.create_stream_token(sample_session.session_id, principal=principal)
    assert response.result is not None
    assert response.result.stream_token == "token-1"

    health = await routes.health_check()
    assert health["status"] == "healthy"
    assert health["components"]["orchestrator"] is True
    assert health["components"]["session_manager"] is True
