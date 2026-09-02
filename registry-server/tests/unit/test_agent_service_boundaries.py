import asyncio
import builtins
import json
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.account.model import RoleType
from app.agent import api as agent_api
from app.agent import api_atr
from app.agent import service as agent_service
from app.agent.exception import AgentError, AgentErrorCode
from app.agent.model import ApprovalStatus
from app.core.base_exception import PROBLEM_JSON_MEDIA_TYPE
from app.utils import acs as acs_utils
from app.utils import aic as aic_module

if TYPE_CHECKING:
    from app.agent.schema import AgentCreate, AgentUpdate

pytestmark = pytest.mark.unit


def test_agent_api_routes_define_status_summary_and_problem_responses() -> None:
    for router in (agent_api.router_public, agent_api.router_client, agent_api.router_staff, api_atr.router):
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue

            assert route.summary
            assert route.status_code is not None
            assert route.responses

            for response in route.responses.values():
                assert response["content"][PROBLEM_JSON_MEDIA_TYPE] == {}


def test_notify_ca_server_revoke_cert_swallows_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = cast("Any", SimpleNamespace(aic="agt-001"))

    monkeypatch.setattr(agent_service, "settings", SimpleNamespace(ca_server_mock=False, CA_SERVER_BASE_URL=None))

    agent_service.notify_ca_server_revoke_cert(agent)


def test_notify_ca_server_revoke_cert_swallows_unexpected_response_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = cast("Any", SimpleNamespace(aic="agt-001"))

    monkeypatch.setattr(
        agent_service,
        "settings",
        SimpleNamespace(ca_server_mock=False, CA_SERVER_BASE_URL="http://ca.example"),
    )
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: None)

    agent_service.notify_ca_server_revoke_cert(agent)


def test_notify_ca_server_revoke_cert_sends_internal_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = cast("Any", SimpleNamespace(aic="agt-001"))
    captured_headers: dict[str, str] = {}

    monkeypatch.setattr(
        agent_service,
        "settings",
        SimpleNamespace(
            ca_server_mock=False,
            ca_server_atr_base_url="http://ca.example/acps-atr-v2",
            registry_server_internal_api_token="service-token",
        ),
    )

    def fake_post(*args: object, **kwargs: object) -> object:
        del args
        captured_headers.update(cast("dict[str, str]", kwargs["headers"]))
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(httpx, "post", fake_post)

    agent_service.notify_ca_server_revoke_cert(agent)

    assert captured_headers["Authorization"] == "Bearer service-token"


class DummyQuery:
    def __init__(self, result: object) -> None:
        self.result = result

    def filter(self, *args: object, **kwargs: object) -> DummyQuery:
        del args, kwargs
        return self

    def first(self) -> object:
        return self.result


class DummyNestedTransaction:
    def __enter__(self) -> DummyNestedTransaction:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        del exc_type, exc, tb
        return False


class DummyDb:
    def __init__(self, processor: object | None = None) -> None:
        self.added: list[object] = []
        self.flushed = False
        self.committed = False
        self.processor = processor
        self.begin_nested_calls = 0

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        self.flushed = True

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.committed = False

    def query(self, model: object) -> DummyQuery:
        del model
        return DummyQuery(self.processor)

    def get(self, model: object, ident: object) -> object | None:
        del model, ident
        return self.processor

    def begin_nested(self) -> DummyNestedTransaction:
        self.begin_nested_calls += 1
        return DummyNestedTransaction()


class RecordingDb(DummyDb):
    def __init__(self, processor: object | None = None) -> None:
        super().__init__(processor=processor)
        self.events: list[str] = []

    def commit(self) -> None:
        super().commit()
        self.events.append("commit")


class AsyncRecordingDb:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.committed = False

    async def commit(self) -> None:
        await asyncio.sleep(0)
        self.committed = True
        self.events.append("commit")

    async def rollback(self) -> None:
        await asyncio.sleep(0)
        self.committed = False

    async def run_sync(self, fn: Callable[[Any], Any]) -> Any:
        await asyncio.sleep(0)
        return fn(self)


class AsyncNestedTransaction:
    async def __aenter__(self) -> AsyncNestedTransaction:
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        del exc_type, exc, tb
        await asyncio.sleep(0)
        return False


class AsyncBatchRecordingDb(AsyncRecordingDb):
    def begin_nested(self) -> AsyncNestedTransaction:
        self.events.append("begin_nested")
        return AsyncNestedTransaction()


class DummyFetchoneResult:
    def __init__(self, result: object | None) -> None:
        self.result = result

    def fetchone(self) -> object | None:
        return self.result


class PayloadStub:
    def __init__(self, payload: builtins.dict[str, object], acs: object | None = None) -> None:
        self.payload = payload
        self.acs = acs

    def dict(self, *, exclude_unset: bool = False) -> builtins.dict[str, object]:
        del exclude_unset
        return self.payload

    def model_dump(self, *, exclude_unset: bool = False) -> builtins.dict[str, object]:
        return self.dict(exclude_unset=exclude_unset)


def _as_session(db: object) -> Session:
    return cast("Session", db)


def _as_async_session(db: object) -> AsyncSession:
    return cast("AsyncSession", db)


def _as_agent_create(payload: PayloadStub) -> AgentCreate:
    return cast("AgentCreate", payload)


def _as_agent_update(payload: PayloadStub) -> AgentUpdate:
    return cast("AgentUpdate", payload)


def test_submit_agent_for_approval_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent = cast(
        "Any",
        SimpleNamespace(
            id=agent_id,
            is_active=True,
            created_by_id=user_id,
            approval_status=ApprovalStatus.DRAFT,
            submitted_at=None,
            processed_by_id=uuid.uuid4(),
            processed_at=object(),
            process_comments="old review",
            updated_at=None,
        ),
    )

    monkeypatch.setattr(agent_service, "get_agent", lambda *args, **kwargs: agent)

    result = agent_service.submit_agent_for_approval(_as_session(db), agent_id, user_id)

    assert result is agent
    assert agent.approval_status == ApprovalStatus.PENDING
    assert agent.submitted_at is not None
    assert agent.processed_by_id is None
    assert agent.processed_at is None
    assert agent.process_comments is None
    assert db.flushed is True
    assert db.committed is False


def test_cancel_agent_submission_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent = cast(
        "Any",
        SimpleNamespace(
            id=agent_id,
            is_active=True,
            created_by_id=user_id,
            approval_status=ApprovalStatus.PENDING,
            submitted_at=object(),
            processed_by_id=uuid.uuid4(),
            processed_at=object(),
            process_comments="stale review",
            updated_at=None,
        ),
    )

    monkeypatch.setattr(agent_service, "get_agent", lambda *args, **kwargs: agent)

    result = agent_service.cancel_agent_submission(_as_session(db), agent_id, user_id)

    assert result is agent
    assert agent.approval_status == ApprovalStatus.DRAFT
    assert agent.submitted_at is None
    assert agent.processed_by_id is None
    assert agent.processed_at is None
    assert agent.process_comments is None
    assert db.flushed is True
    assert db.committed is False


def test_process_agent_approval_reject_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    processor_id = uuid.uuid4()
    processor = cast("Any", SimpleNamespace(id=processor_id, roles=[SimpleNamespace(name=RoleType.STAFF)]))
    db = DummyDb(processor=processor)
    agent = cast(
        "Any",
        SimpleNamespace(
            id=agent_id,
            is_active=True,
            approval_status=ApprovalStatus.PENDING,
            processed_by_id=None,
            processed_at=None,
            process_comments=None,
            updated_at=None,
            aic=None,
        ),
    )

    monkeypatch.setattr(agent_service, "get_agent", lambda *args, **kwargs: agent)

    result = agent_service.process_agent_approval(
        _as_session(db),
        agent_id,
        processor_id,
        approve=False,
        comments="<b>reject</b>",
    )

    assert result is agent
    assert agent.approval_status == ApprovalStatus.REJECTED
    assert agent.processed_by_id == processor_id
    assert agent.process_comments == "reject"
    assert db.flushed is True
    assert db.committed is False


def test_process_agent_approval_applies_provider_before_mark_processed(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    processor_id = uuid.uuid4()
    processor = cast("Any", SimpleNamespace(id=processor_id, roles=[SimpleNamespace(name=RoleType.STAFF)]))
    db = DummyDb(processor=processor)
    agent = cast(
        "Any",
        SimpleNamespace(
            id=agent_id,
            is_active=True,
            approval_status=ApprovalStatus.PENDING,
            processed_by_id=None,
            processed_at=None,
            process_comments=None,
            updated_at=None,
            aic="existing-aic",
            acs={"provider": {"name": "draft"}},
        ),
    )
    events: list[str] = []

    monkeypatch.setattr(agent_service, "get_agent", lambda *args, **kwargs: agent)

    def fake_apply_verified_provider_snapshot(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        events.append("provider")
        return False

    def fake_mark_agent_processed(*args: object, **kwargs: object) -> None:
        del args, kwargs
        events.append("mark")
        agent.approval_status = ApprovalStatus.APPROVED
        agent.processed_by_id = processor_id

    monkeypatch.setattr(agent_service, "apply_verified_provider_snapshot", fake_apply_verified_provider_snapshot)
    monkeypatch.setattr(agent_service, "_mark_agent_processed", fake_mark_agent_processed)

    agent_service.process_agent_approval(_as_session(db), agent_id, processor_id, approve=True, comments="ok")

    assert events == ["provider", "mark"]


def test_process_agent_approval_existing_aic_updates_changelog_when_provider_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    processor_id = uuid.uuid4()
    processor = cast("Any", SimpleNamespace(id=processor_id, roles=[SimpleNamespace(name=RoleType.STAFF)]))
    db = DummyDb(processor=processor)
    agent = cast(
        "Any",
        SimpleNamespace(
            id=agent_id,
            is_active=True,
            approval_status=ApprovalStatus.PENDING,
            processed_by_id=None,
            processed_at=None,
            process_comments=None,
            updated_at=None,
            aic="existing-aic",
            acs={"provider": {"name": "trusted"}},
        ),
    )
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(agent_service, "get_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr(agent_service, "apply_verified_provider_snapshot", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        agent_service,
        "update_agent_with_changelog",
        lambda current_db, current_agent, payload: calls.append(payload),
    )

    result = agent_service.process_agent_approval(_as_session(db), agent_id, processor_id, approve=True, comments="ok")

    assert result.approval_status == ApprovalStatus.APPROVED
    assert calls == [{"acs": agent.acs}]
    assert db.flushed is True


def test_process_agent_approval_non_pending_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    processor_id = uuid.uuid4()
    processor = cast("Any", SimpleNamespace(id=processor_id, roles=[SimpleNamespace(name=RoleType.STAFF)]))
    db = DummyDb(processor=processor)
    agent = cast(
        "Any",
        SimpleNamespace(
            id=agent_id,
            is_active=True,
            approval_status=ApprovalStatus.APPROVED,
            processed_by_id=None,
            processed_at=None,
            process_comments=None,
            updated_at=None,
            aic="existing-aic",
        ),
    )

    monkeypatch.setattr(agent_service, "get_agent", lambda *args, **kwargs: agent)

    with pytest.raises(AgentError) as exc_info:
        agent_service.process_agent_approval(_as_session(db), agent_id, processor_id, approve=False, comments="reject")

    assert exc_info.value.error_name == AgentErrorCode.INVALID_STATUS_TRANSITION
    assert db.flushed is False
    assert db.committed is False


def test_generate_aic_for_agent_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    agent = cast(
        "Any",
        SimpleNamespace(
            approval_status=ApprovalStatus.APPROVED,
            aic=None,
            is_ontology=False,
            created_by_id=uuid.uuid4(),
            updated_at=None,
            acs=None,
        ),
    )

    monkeypatch.setattr(aic_module, "generate_aic", lambda **_kwargs: "aic-generated")
    monkeypatch.setattr(
        "app.agent.service_acs.ensure_user_aic_provider_code",
        lambda db, user_id: "ABC123",
    )

    result = agent_service.generate_aic_for_agent(_as_session(db), agent)

    assert result is agent
    assert agent.aic == "aic-generated"
    assert db.begin_nested_calls == 1
    assert db.flushed is True
    assert db.committed is False


def test_create_agent_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    class FakeAgent:
        name = ""
        version = ""
        is_active = True
        created_by_id = None

        def __init__(self, **kwargs: object) -> None:
            self.id = agent_id
            for key, value in kwargs.items():
                setattr(self, key, value)

    monkeypatch.setattr(
        agent_service,
        "Agent",
        FakeAgent,
    )

    result = agent_service.create_agent(
        _as_session(db),
        user_id,
        {
            "name": "demo-agent",
            "version": "1.0.0",
            "description": "<b>demo-agent</b>",
        },
    )

    assert result.id == agent_id
    assert result.created_by_id == user_id
    assert result.approval_status == ApprovalStatus.DRAFT
    assert result.description == "demo-agent"
    assert db.flushed is True
    assert db.committed is False


def test_create_agent_invalid_acs_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    user_id = uuid.uuid4()

    monkeypatch.setattr(acs_utils, "validate", lambda value: None)

    with pytest.raises(AgentError) as exc_info:
        agent_service.create_agent(
            _as_session(db),
            user_id,
            {
                "name": "demo-agent",
                "version": "1.0.0",
                "acs": "not-json",
            },
        )

    assert exc_info.value.error_name == AgentErrorCode.AGENT_CREATE_FAILED


def test_update_agent_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent = cast(
        "Any",
        SimpleNamespace(
            id=agent_id,
            is_active=True,
            created_by_id=user_id,
            approval_status=ApprovalStatus.DRAFT,
            name="demo-agent",
            version="1.0.0",
            description=None,
            acs_hash=None,
            updated_at=None,
            acs={"name": "demo"},
        ),
    )
    sync_calls: list[str] = []

    monkeypatch.setattr(agent_service, "get_agent", lambda *args, **kwargs: agent)

    def _fake_update_agent_acs_data(*args: object, **kwargs: object) -> None:
        del args, kwargs
        sync_calls.append("acs-update")

    monkeypatch.setattr(
        agent_service,
        "update_agent_acs_data",
        _fake_update_agent_acs_data,
    )

    result = agent_service.update_agent(
        _as_session(db), agent_id, user_id, {"name": "demo-agent", "description": "<i>safe</i>"}
    )

    assert result is agent
    assert agent.description == "safe"
    assert sync_calls == ["acs-update"]
    assert db.flushed is True
    assert db.committed is False


def test_register_entity_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    ontology_aic = aic_module.generate_ontology_aic(manager_code="1", provider_code="00001")
    entity_aic = aic_module.generate_entity_aic_from_ontology(ontology_aic)
    ontology_agent = cast(
        "Any",
        SimpleNamespace(
            aic=ontology_aic,
            is_active=True,
            is_disabled=False,
            is_deleted=False,
            is_ontology=True,
            approval_status=ApprovalStatus.APPROVED,
            acs={"name": "Ontology", "version": "1.0.0"},
            name="Ontology",
            version="1.0.0",
            description="demo",
            logo_url=None,
            created_by_id=uuid.uuid4(),
        ),
    )
    query_results = [ontology_agent, None]
    db = cast("Any", DummyDb())
    changelog_calls: list[dict[str, object]] = []

    def _fake_query(model: object) -> DummyQuery:
        del model
        return DummyQuery(query_results.pop(0) if query_results else None)

    def _fake_execute(*args: object, **kwargs: object) -> DummyFetchoneResult:
        del args, kwargs
        return DummyFetchoneResult(None)

    def _fake_create_change_log(**kwargs: object) -> SimpleNamespace:
        changelog_calls.append(dict(kwargs))
        return SimpleNamespace(seq=123)

    db.query = _fake_query
    db.execute = _fake_execute
    monkeypatch.setattr(
        agent_service,
        "create_change_log",
        _fake_create_change_log,
    )
    monkeypatch.setattr(
        aic_module,
        "generate_entity_aic_from_ontology",
        lambda ontology: entity_aic,
    )

    result = agent_service.register_entity(db=db, ontology_aic=ontology_aic)

    assert result["ontologyAic"] == ontology_aic
    assert result["entityAic"] == entity_aic
    assert changelog_calls[0]["data_type"] == "acs"
    assert changelog_calls[0]["object_id"] == entity_aic
    assert changelog_calls[0]["version"] == 1
    assert db.flushed is True
    assert db.committed is False


def test_delete_agent_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = DummyDb()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent = cast(
        "Any",
        SimpleNamespace(
            id=agent_id,
            created_by_id=user_id,
            is_ontology=False,
            aic=None,
            is_active=True,
            is_deleted=False,
            deleted_at=None,
            deleted_reason=None,
            updated_at=None,
            acs=None,
        ),
    )

    monkeypatch.setattr(agent_service, "get_agent", lambda *args, **kwargs: agent)

    result = agent_service.delete_agent(_as_session(db), agent_id, user_id, reason="cleanup")

    assert result is True
    assert agent.is_active is False
    assert agent.is_deleted is True
    assert agent.deleted_reason == "cleanup"
    assert db.flushed is True
    assert db.committed is False


def test_batch_delete_agents_uses_savepoints_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    first_agent_id = uuid.uuid4()
    second_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db = RecordingDb()

    def _fake_delete_agent(
        session: object,
        agent_id: uuid.UUID,
        current_user_id: uuid.UUID,
        reason: str,
    ) -> None:
        del session, current_user_id, reason
        db.events.append(f"delete:{agent_id}")
        if agent_id == second_agent_id:
            raise AgentError(
                status_code=403,
                error_name=AgentErrorCode.UNAUTHORIZED_ACCESS,
                error_msg="delete failed",
            )

    monkeypatch.setattr(agent_service, "delete_agent", _fake_delete_agent)

    result = agent_service.batch_delete_agents(_as_session(db), [first_agent_id, second_agent_id], user_id)

    assert result == {
        "success": [str(first_agent_id)],
        "failed": [{"id": str(second_agent_id), "reason": "delete failed"}],
    }
    assert "commit" not in db.events


def test_batch_delete_agents_propagates_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    first_agent_id = uuid.uuid4()
    second_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db = RecordingDb()

    def _fake_delete_agent(
        session: object,
        agent_id: uuid.UUID,
        current_user_id: uuid.UUID,
        reason: str,
    ) -> None:
        del session, current_user_id, reason
        db.events.append(f"delete:{agent_id}")
        if agent_id == second_agent_id:
            raise RuntimeError("unexpected delete failure")

    monkeypatch.setattr(agent_service, "delete_agent", _fake_delete_agent)

    with pytest.raises(RuntimeError, match="unexpected delete failure"):
        agent_service.batch_delete_agents(_as_session(db), [first_agent_id, second_agent_id], user_id)

    assert "commit" not in db.events


def test_disable_agent_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    staff_user_id = uuid.uuid4()
    staff_user = cast("Any", SimpleNamespace(id=staff_user_id, roles=[SimpleNamespace(name=RoleType.STAFF)]))
    db = DummyDb(processor=staff_user)
    agent = cast(
        "Any",
        SimpleNamespace(
            id=agent_id,
            is_ontology=False,
            aic=None,
            is_active=True,
            is_disabled=False,
            disabled_at=None,
            disabled_reason=None,
            updated_at=None,
            acs=None,
        ),
    )

    monkeypatch.setattr(agent_service, "get_agent", lambda *args, **kwargs: agent)

    result = agent_service.disable_agent(_as_session(db), agent_id, staff_user_id, reason="staff")

    assert result is agent
    assert agent.is_active is False
    assert agent.is_disabled is True
    assert agent.disabled_reason == "staff"
    assert db.flushed is True
    assert db.committed is False


def test_admin_can_disable_agent_through_sync_service(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    admin_user_id = uuid.uuid4()
    admin_user = cast("Any", SimpleNamespace(id=admin_user_id, roles=[SimpleNamespace(name=RoleType.ADMIN)]))
    db = DummyDb(processor=admin_user)
    agent = cast(
        "Any",
        SimpleNamespace(
            id=agent_id,
            is_ontology=False,
            aic=None,
            is_active=True,
            is_disabled=False,
            disabled_at=None,
            disabled_reason=None,
            updated_at=None,
            acs=None,
        ),
    )

    monkeypatch.setattr(agent_service, "get_agent", lambda *args, **kwargs: agent)

    result = agent_service.disable_agent(_as_session(db), agent_id, admin_user_id, reason="admin")

    assert result is agent
    assert agent.is_active is False
    assert agent.is_disabled is True
    assert agent.disabled_reason == "admin"
    assert db.flushed is True
    assert db.committed is False


def test_enable_agent_flushes_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    staff_user_id = uuid.uuid4()
    staff_user = cast("Any", SimpleNamespace(id=staff_user_id, roles=[SimpleNamespace(name=RoleType.STAFF)]))
    db = DummyDb(processor=staff_user)
    agent = cast(
        "Any",
        SimpleNamespace(
            id=agent_id,
            is_ontology=False,
            aic=None,
            is_active=False,
            is_deleted=False,
            is_disabled=True,
            disabled_at=object(),
            disabled_reason="old",
            updated_at=None,
            acs=None,
        ),
    )

    monkeypatch.setattr(agent_service, "get_agent", lambda *args, **kwargs: agent)

    result = agent_service.enable_agent(_as_session(db), agent_id, staff_user_id)

    assert result is agent
    assert agent.is_active is True
    assert agent.is_disabled is False
    assert agent.disabled_at is None
    assert agent.disabled_reason is None
    assert db.flushed is True
    assert db.committed is False


def test_admin_can_enable_agent_through_sync_service(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    admin_user_id = uuid.uuid4()
    admin_user = cast("Any", SimpleNamespace(id=admin_user_id, roles=[SimpleNamespace(name=RoleType.ADMIN)]))
    db = DummyDb(processor=admin_user)
    agent = cast(
        "Any",
        SimpleNamespace(
            id=agent_id,
            is_ontology=False,
            aic=None,
            is_active=False,
            is_deleted=False,
            is_disabled=True,
            disabled_at=object(),
            disabled_reason="old",
            updated_at=None,
            acs=None,
        ),
    )

    monkeypatch.setattr(agent_service, "get_agent", lambda *args, **kwargs: agent)

    result = agent_service.enable_agent(_as_session(db), agent_id, admin_user_id)

    assert result is agent
    assert agent.is_active is True
    assert agent.is_disabled is False
    assert agent.disabled_at is None
    assert agent.disabled_reason is None
    assert db.flushed is True
    assert db.committed is False


async def test_client_delete_agent_route_commits_before_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db = AsyncRecordingDb()
    current_user = cast("Any", SimpleNamespace(id=user_id))

    async def _fake_delete_agent(*args: object, **kwargs: object) -> None:
        del args, kwargs
        await asyncio.sleep(0)
        db.events.append("delete")

    def _fake_trigger_data_change_webhook(*args: object, **kwargs: object) -> None:
        del args, kwargs
        db.events.append("trigger")

    monkeypatch.setattr(
        agent_api,
        "delete_agent_async",
        _fake_delete_agent,
    )
    monkeypatch.setattr(
        agent_api,
        "trigger_data_change_webhook",
        _fake_trigger_data_change_webhook,
    )

    result = await agent_api.client_delete_agent_record(
        agent_id=agent_id,
        reason="cleanup",
        db=_as_async_session(db),
        current_user=current_user,
    )

    assert result.message == "Agent deleted successfully"
    assert db.events == ["delete", "commit", "trigger"]


async def test_batch_delete_agents_async_uses_savepoints_without_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    first_agent_id = uuid.uuid4()
    second_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db = AsyncBatchRecordingDb()

    async def _fake_delete_agent(
        session: object,
        agent_id: uuid.UUID,
        current_user_id: uuid.UUID,
        reason: str,
    ) -> None:
        del session, current_user_id, reason
        await asyncio.sleep(0)
        db.events.append(f"delete:{agent_id}")
        if agent_id == second_agent_id:
            raise AgentError(
                status_code=403,
                error_name=AgentErrorCode.UNAUTHORIZED_ACCESS,
                error_msg="delete failed",
            )

    monkeypatch.setattr(agent_service, "delete_agent_async", _fake_delete_agent)

    result = await agent_service.batch_delete_agents_async(
        _as_async_session(db), [first_agent_id, second_agent_id], user_id
    )

    assert result == {
        "success": [str(first_agent_id)],
        "failed": [{"id": str(second_agent_id), "reason": "delete failed"}],
    }
    assert "commit" not in db.events


async def test_batch_delete_agents_async_propagates_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    first_agent_id = uuid.uuid4()
    second_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db = AsyncBatchRecordingDb()

    async def _fake_delete_agent(
        session: object,
        agent_id: uuid.UUID,
        current_user_id: uuid.UUID,
        reason: str,
    ) -> None:
        del session, current_user_id, reason
        await asyncio.sleep(0)
        db.events.append(f"delete:{agent_id}")
        if agent_id == second_agent_id:
            raise RuntimeError("unexpected delete failure")

    monkeypatch.setattr(agent_service, "delete_agent_async", _fake_delete_agent)

    with pytest.raises(RuntimeError, match="unexpected delete failure"):
        await agent_service.batch_delete_agents_async(_as_async_session(db), [first_agent_id, second_agent_id], user_id)

    assert "commit" not in db.events


async def test_client_delete_multiple_agents_route_commits_before_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db = AsyncRecordingDb()
    current_user = cast("Any", SimpleNamespace(id=user_id))

    async def _fake_batch_delete_agents(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        await asyncio.sleep(0)
        db.events.append("batch-delete")
        return {"success": [str(agent_id)], "failed": []}

    def _fake_trigger_data_change_webhook(*args: object, **kwargs: object) -> None:
        del args, kwargs
        db.events.append("trigger")

    monkeypatch.setattr(agent_api, "batch_delete_agents_async", _fake_batch_delete_agents)
    monkeypatch.setattr(agent_api, "trigger_data_change_webhook", _fake_trigger_data_change_webhook)

    result = await agent_api.client_delete_multiple_agents(
        agent_ids=[agent_id],
        db=_as_async_session(db),
        current_user=current_user,
    )

    assert result.success == [str(agent_id)]
    assert result.failed == []
    assert db.events == ["batch-delete", "commit", "trigger"]


async def test_client_create_agent_route_uses_async_service_without_explicit_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db = AsyncRecordingDb()
    current_user = cast("Any", SimpleNamespace(id=user_id))
    created_agent = cast("Any", SimpleNamespace(id=agent_id))
    payload = PayloadStub({"name": "demo-agent", "version": "1.0.0"})

    async def _fake_create_agent(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        await asyncio.sleep(0)
        db.events.append("create")
        return created_agent

    monkeypatch.setattr(
        agent_api,
        "create_agent_async",
        _fake_create_agent,
    )
    monkeypatch.setattr(agent_api, "create_agent_response", lambda agent: agent)

    result = await agent_api.client_create_new_agent(
        agent_create=_as_agent_create(payload),
        current_user=current_user,
        db=_as_async_session(db),
    )

    assert result is created_agent
    assert db.events == ["create"]


async def test_client_update_agent_route_uses_async_service_without_explicit_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db = AsyncRecordingDb()
    current_user = cast("Any", SimpleNamespace(id=user_id))
    updated_agent = cast("Any", SimpleNamespace(id=agent_id))
    payload = PayloadStub({"name": "demo-agent"}, acs=None)

    async def _fake_update_agent(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        await asyncio.sleep(0)
        db.events.append("update")
        return updated_agent

    monkeypatch.setattr(
        agent_api,
        "update_agent_async",
        _fake_update_agent,
    )
    monkeypatch.setattr(agent_api, "create_agent_response", lambda agent: agent)

    result = await agent_api.client_update_agent_info(
        agent_id=agent_id,
        agent_update=_as_agent_update(payload),
        db=_as_async_session(db),
        current_user=current_user,
    )

    assert result is updated_agent
    assert db.events == ["update"]


async def test_register_entity_route_uses_async_service_without_explicit_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_aic = aic_module.generate_ontology_aic(manager_code="1", provider_code="00001")
    db = AsyncRecordingDb()
    request = cast(
        "Any",
        SimpleNamespace(
            ontologyAic=ontology_aic.lower(),
            endPoints=None,
            entityMeta=None,
            entityUserId=None,
            certificate=None,
        ),
    )
    current_user = cast("Any", SimpleNamespace(id=uuid.uuid4(), roles=[]))
    http_request = cast(
        "Any",
        SimpleNamespace(
            state=SimpleNamespace(peer_common_name=ontology_aic),
            headers={},
            app=SimpleNamespace(state=SimpleNamespace(app_env="testing")),
        ),
    )

    async def _fake_register_entity(**kwargs: object) -> dict[str, str]:
        del kwargs
        await asyncio.sleep(0)
        db.events.append("register")
        return {"ontologyAic": ontology_aic, "entityAic": "ENTITY"}

    async def _fake_get_agent_by_aic(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        await asyncio.sleep(0)
        return SimpleNamespace(
            created_by_id=current_user.id,
            is_ontology=True,
            is_active=True,
            is_disabled=False,
            is_deleted=False,
            approval_status=ApprovalStatus.APPROVED,
        )

    monkeypatch.setattr(api_atr, "validate_aic", lambda aic: True)
    monkeypatch.setattr(api_atr, "is_ontology_aic", lambda aic: True)
    monkeypatch.setattr(api_atr, "get_agent_by_aic_async", _fake_get_agent_by_aic)
    monkeypatch.setattr(
        api_atr,
        "register_entity_async",
        _fake_register_entity,
    )

    response = await api_atr.register_entity_endpoint(
        request=request,
        http_request=http_request,
        db=_as_async_session(db),
        current_user=current_user,
    )

    assert response.status_code == 201
    assert json.loads(bytes(response.body)) == {
        "status": "ok",
        "result": {"ontologyAic": ontology_aic, "entityAic": "ENTITY"},
    }
    assert db.events == ["register"]


async def test_staff_disable_agent_route_commits_before_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    staff_user_id = uuid.uuid4()
    db = AsyncRecordingDb()
    current_user = cast("Any", SimpleNamespace(id=staff_user_id))
    response_agent = cast("Any", SimpleNamespace(id=agent_id))

    async def _fake_disable_agent(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        await asyncio.sleep(0)
        db.events.append("disable")
        return response_agent

    def _fake_trigger_data_change_webhook(*args: object, **kwargs: object) -> None:
        del args, kwargs
        db.events.append("trigger")

    monkeypatch.setattr(
        agent_api,
        "disable_agent_async",
        _fake_disable_agent,
    )
    monkeypatch.setattr(
        agent_api,
        "trigger_data_change_webhook",
        _fake_trigger_data_change_webhook,
    )
    monkeypatch.setattr(agent_api, "create_agent_response", lambda agent: agent)

    result = await agent_api.staff_disable_agent(
        agent_id=agent_id,
        reason="staff",
        db=_as_async_session(db),
        current_user=current_user,
    )

    assert result is response_agent
    assert db.events == ["disable", "commit", "trigger"]


async def test_staff_enable_agent_route_commits_before_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = uuid.uuid4()
    staff_user_id = uuid.uuid4()
    db = AsyncRecordingDb()
    current_user = cast("Any", SimpleNamespace(id=staff_user_id))
    response_agent = cast("Any", SimpleNamespace(id=agent_id))

    async def _fake_enable_agent(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        await asyncio.sleep(0)
        db.events.append("enable")
        return response_agent

    def _fake_trigger_data_change_webhook(*args: object, **kwargs: object) -> None:
        del args, kwargs
        db.events.append("trigger")

    monkeypatch.setattr(
        agent_api,
        "enable_agent_async",
        _fake_enable_agent,
    )
    monkeypatch.setattr(
        agent_api,
        "trigger_data_change_webhook",
        _fake_trigger_data_change_webhook,
    )
    monkeypatch.setattr(agent_api, "create_agent_response", lambda agent: agent)

    result = await agent_api.staff_enable_agent(
        agent_id=agent_id,
        db=_as_async_session(db),
        current_user=current_user,
    )

    assert result is response_agent
    assert db.events == ["enable", "commit", "trigger"]
