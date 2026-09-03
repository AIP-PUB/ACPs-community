from __future__ import annotations

import asyncio
import ssl
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from acps_sdk.aip.aip_base_model import (
    TaskCommand,
    TaskCommandType,
    TaskResult,
    TaskState,
    TaskStatus,
)
from acps_sdk.aip.aip_group_model import (
    ACSObject,
    AMQPConfig,
    GroupInfo,
    GroupMgmtCommand,
    GroupMgmtCommandType,
    RabbitMQRequest,
    RabbitMQRequestParams,
    RabbitMQResponse,
    RabbitMQResponseError,
    RabbitMQResponseResult,
    RabbitMQServerConfig,
)
from aiormq.exceptions import AMQPError

from partners.group_handler import GroupHandler


class _DummyRunner:
    def __init__(self) -> None:
        self.acs = {"aic": "partner-aic"}
        self.tasks: dict[str, object] = {}
        self.on_start = AsyncMock()
        self.on_continue = AsyncMock()
        self.on_complete = AsyncMock()
        self.on_cancel = AsyncMock()
        self.on_get = AsyncMock()
        self._state_change_callback: Any = None
        self._removed_state_change_listener: Any = None
        self._message_emitter = Mock()

    def set_state_change_callback(self, callback: Any) -> None:
        self._state_change_callback = callback

    def remove_state_change_listener(self, callback: Any) -> None:
        self._removed_state_change_listener = callback


async def _yield_control() -> None:
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)
    await future


def _build_task_result(task_id: str) -> TaskResult:
    now = datetime.now(UTC).isoformat()
    return TaskResult(
        id="result-1",
        sentAt=now,
        senderRole="partner",
        senderId="partner-aic",
        taskId=task_id,
        groupId="group-new",
        sessionId="sess-new",
        status=TaskStatus(
            state=TaskState.Accepted,
            stateChangedAt=now,
        ),
    )


def _build_group_rpc(group_id: str = "group-1") -> RabbitMQRequest:
    return RabbitMQRequest(
        id="rpc-1",
        params=RabbitMQRequestParams(
            protocol="rabbitmq:4.0",
            group=GroupInfo(
                groupId=group_id,
                leader=ACSObject(aic="leader-aic"),
                partners=[],
            ),
            server=RabbitMQServerConfig(host="mq.local", port=5671, vhost="acps"),
            amqp=AMQPConfig(exchange=group_id, exchangeType="fanout", routingKey=""),
        ),
    )


def _build_command(
    command_type: TaskCommandType,
    *,
    task_id: str = "task-1",
    group_id: str | None = "group-1",
    sender_id: str = "leader-aic",
) -> TaskCommand:
    return TaskCommand(
        id="cmd-1",
        sentAt=datetime.now(UTC).isoformat(),
        senderRole="leader",
        senderId=sender_id,
        command=command_type,
        taskId=task_id,
        groupId=group_id,
        sessionId="sess-1",
    )


@pytest.mark.asyncio
async def test_task_command_prefers_command_group_id_over_sender_match() -> None:
    runner = _DummyRunner()
    runner.on_start.return_value = _build_task_result("task-1")

    handler = GroupHandler("test-agent", cast("Any", runner))
    handler._group_clients = {
        "group-old": cast("Any", SimpleNamespace(is_joined=True)),
        "group-new": cast("Any", SimpleNamespace(is_joined=True)),
    }
    handler._find_group_for_sender = Mock(return_value="group-old")  # type: ignore[method-assign]
    handler._broadcast_task_update = AsyncMock()  # type: ignore[method-assign]

    command = TaskCommand(
        id="cmd-1",
        sentAt=datetime.now(UTC).isoformat(),
        senderRole="leader",
        senderId="leader-aic",
        command=TaskCommandType.Start,
        taskId="task-1",
        groupId="group-new",
        sessionId="sess-new",
    )

    await handler._on_task_command(command, is_mentioned=True)

    assert handler._task_group_map["task-1"] == "group-new"
    handler._find_group_for_sender.assert_not_called()
    handler._broadcast_task_update.assert_awaited_once()
    await_args = handler._broadcast_task_update.await_args
    assert await_args is not None
    assert await_args.args[1] == "group-new"


@pytest.mark.asyncio
async def test_start_retries_shared_inbox_until_rabbitmq_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))

    attempts = {"count": 0}
    real_sleep = asyncio.sleep

    class _FakeClient:
        def __init__(self, **_: object) -> None:
            self.connection = object()
            self.closed = False
            created_clients.append(self)

        async def connect(self) -> None:
            await _yield_control()
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise AMQPError("broker not ready")

        async def start_inbox_consuming(self, _handler: object) -> None:
            await _yield_control()

        async def close(self) -> None:
            await _yield_control()
            self.closed = True

    created_clients: list[_FakeClient] = []

    async def _fast_sleep(_: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("partners.group_handler.GroupPartnerMqClient", _FakeClient)
    monkeypatch.setattr("partners.group_handler.asyncio.sleep", _fast_sleep)

    await handler.start()

    retry_task = handler._shared_mq_retry_task
    assert retry_task is not None
    await asyncio.wait_for(retry_task, timeout=1)

    assert attempts["count"] == 2
    assert created_clients[0].closed is True
    assert cast("Any", handler._shared_mq_client) is created_clients[1]
    assert handler._shared_mq_retry_task is None

    await handler.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_shared_inbox_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))

    class _AlwaysFailClient:
        def __init__(self, **_: object) -> None:
            self.connection = object()

        async def connect(self) -> None:
            await _yield_control()
            raise OSError("still unavailable")

        async def start_inbox_consuming(self, _handler: object) -> None:
            await _yield_control()

        async def close(self) -> None:
            await _yield_control()

    gate = asyncio.Event()

    async def _blocking_sleep(_: float) -> None:
        await gate.wait()

    monkeypatch.setattr("partners.group_handler.GroupPartnerMqClient", _AlwaysFailClient)
    monkeypatch.setattr("partners.group_handler.asyncio.sleep", _blocking_sleep)

    await handler.start()

    retry_task = handler._shared_mq_retry_task
    assert retry_task is not None

    await handler.shutdown()

    assert retry_task.cancelled() is True
    assert handler._shared_mq_client is None
    assert handler._shared_mq_retry_task is None


@pytest.mark.asyncio
async def test_start_does_not_retry_shared_inbox_on_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))

    class _AuthFailClient:
        def __init__(self, **_: object) -> None:
            self.closed = False

        async def connect(self) -> None:
            await _yield_control()
            raise OSError("ACCESS_REFUSED - Login was refused using authentication mechanism EXTERNAL")

        async def start_inbox_consuming(self, _handler: object) -> None:
            await _yield_control()

        async def close(self) -> None:
            await _yield_control()
            self.closed = True

    monkeypatch.setattr("partners.group_handler.GroupPartnerMqClient", _AuthFailClient)

    await handler.start()

    assert handler._shared_mq_client is None
    assert handler._shared_mq_retry_task is None


@pytest.mark.asyncio
async def test_inbox_invitation_replaces_stale_client_with_dedicated_connection() -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))

    stale_client = cast("Any", SimpleNamespace(is_joined=False, close=AsyncMock()))
    new_client = cast(
        "Any",
        SimpleNamespace(
            set_command_handler=Mock(),
            set_task_result_handler=Mock(),
            set_mgmt_command_handler=Mock(),
            set_disconnect_handler=Mock(),
            join_group_from_invitation=AsyncMock(return_value=True),
        ),
    )
    create_group_client = Mock(return_value=new_client)

    handler._group_clients["group-1"] = stale_client
    handler._create_group_client = create_group_client  # type: ignore[method-assign]

    invitation = cast("Any", SimpleNamespace(group=SimpleNamespace(groupId="group-1")))

    await handler._handle_inbox_invitation(invitation)

    stale_client.close.assert_awaited_once()
    create_group_client.assert_called_once_with(use_shared_connection=False)
    new_client.set_disconnect_handler.assert_called_once_with(handler._on_group_client_disconnected)
    new_client.join_group_from_invitation.assert_awaited_once_with(invitation)
    assert handler._group_clients["group-1"] is new_client


def test_disconnect_callback_removes_group_state() -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))

    client = cast("Any", SimpleNamespace())
    handler._group_clients = {
        "group-1": client,
        "group-2": cast("Any", SimpleNamespace()),
    }
    handler._task_group_map = {
        "task-1": "group-1",
        "task-2": "group-2",
    }

    handler._on_group_client_disconnected(client, "group-1")

    assert "group-1" not in handler._group_clients
    assert handler._task_group_map == {"task-2": "group-2"}


def test_create_group_client_disables_robust_reconnect_for_group_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner), identity_binding_enabled=False)
    created_kwargs: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            created_kwargs.update(kwargs)
            self.connection = kwargs.get("connection")

    monkeypatch.setattr("partners.group_handler.GroupPartnerMqClient", _FakeClient)

    handler._create_group_client(use_shared_connection=False)

    assert created_kwargs["robust_connection"] is False
    assert created_kwargs["identity_binding_enabled"] is False


def test_resolve_rabbitmq_config_uses_env_for_non_tls_port(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _DummyRunner()
    handler = GroupHandler(
        "test-agent",
        cast("Any", runner),
        rabbitmq_config={"host": "cfg-host", "port": 5672, "vhost": "cfg-vhost", "user": "cfg-user"},
    )

    monkeypatch.setenv("RABBITMQ_HOST", "env-host")
    monkeypatch.setenv("RABBITMQ_PORT", "not-an-int")
    monkeypatch.setenv("RABBITMQ_VHOST", "env-vhost")
    monkeypatch.setenv("RABBITMQ_USER", "env-user")
    monkeypatch.setenv("RABBITMQ_PASSWORD", "env-pass")

    assert handler._resolve_rabbitmq_config() == {
        "host": "env-host",
        "port": 5671,
        "vhost": "env-vhost",
        "user": "env-user",
        "password": "env-pass",
    }


def test_resolve_rabbitmq_config_keeps_cert_auth_credentials_when_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLS 默认配置不应受调用环境中 RabbitMQ 覆盖项影响。"""
    for name in ("RABBITMQ_HOST", "RABBITMQ_PORT", "RABBITMQ_USER", "RABBITMQ_PASSWORD", "RABBITMQ_VHOST"):
        monkeypatch.delenv(name, raising=False)
    runner = _DummyRunner()
    handler = GroupHandler(
        "test-agent",
        cast("Any", runner),
        rabbitmq_config={"user": "cert-user", "password": "cert-pass"},
        ssl_context=cast("ssl.SSLContext", object()),
    )

    config = handler._resolve_rabbitmq_config()

    assert config["port"] == 5671
    assert config["user"] == "cert-user"
    assert config["password"] == "cert-pass"


@pytest.mark.asyncio
async def test_join_group_returns_existing_connection_info() -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))
    existing_client = cast(
        "Any",
        SimpleNamespace(
            is_joined=True,
            _connection_name="conn-existing",
            _vhost="acps",
            _node_name="rabbit@node",
            queue_name="queue-existing",
        ),
    )
    handler._group_clients["group-1"] = existing_client

    response = await handler._handle_join_group(_build_group_rpc())

    assert response.error is None
    assert response.result is not None
    assert response.result.connectionName == "conn-existing"
    assert response.result.queueName == "queue-existing"


@pytest.mark.asyncio
async def test_join_group_success_stores_client() -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))
    response = RabbitMQResponse(
        id="rpc-1",
        result=RabbitMQResponseResult(
            connectionName="conn-1",
            vhost="acps",
            nodeName="rabbit@node",
            queueName="queue-1",
            processId="pid-1",
        ),
    )
    set_command_handler = Mock()
    set_task_result_handler = Mock()
    set_mgmt_command_handler = Mock()
    set_disconnect_handler = Mock()
    join_group = AsyncMock(return_value=response)
    client = cast(
        "Any",
        SimpleNamespace(
            is_joined=True,
            queue_name="queue-1",
            set_command_handler=set_command_handler,
            set_task_result_handler=set_task_result_handler,
            set_mgmt_command_handler=set_mgmt_command_handler,
            set_disconnect_handler=set_disconnect_handler,
            join_group=join_group,
        ),
    )
    handler._create_group_client = Mock(return_value=client)  # type: ignore[method-assign]

    result = await handler._handle_join_group(_build_group_rpc())

    assert result.error is None
    assert result.result is not None
    assert result.result.processId == "pid-1"
    assert handler._group_clients["group-1"] is client
    set_command_handler.assert_called_once_with(handler._on_task_command)
    join_group.assert_awaited_once()


@pytest.mark.asyncio
async def test_join_group_returns_client_error_without_storing_client() -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))
    response = RabbitMQResponse(
        id="rpc-1",
        error=RabbitMQResponseError(code=403, message="denied"),
    )
    client = cast(
        "Any",
        SimpleNamespace(
            set_command_handler=Mock(),
            set_task_result_handler=Mock(),
            set_mgmt_command_handler=Mock(),
            set_disconnect_handler=Mock(),
            join_group=AsyncMock(return_value=response),
        ),
    )
    handler._create_group_client = Mock(return_value=client)  # type: ignore[method-assign]

    result = await handler._handle_join_group(_build_group_rpc())

    assert result.error is not None
    assert result.error.code == 403
    assert "group-1" not in handler._group_clients


@pytest.mark.asyncio
async def test_join_group_missing_result_is_reported_as_internal_error() -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))
    client = cast(
        "Any",
        SimpleNamespace(
            is_joined=True,
            queue_name="queue-1",
            set_command_handler=Mock(),
            set_task_result_handler=Mock(),
            set_mgmt_command_handler=Mock(),
            set_disconnect_handler=Mock(),
            join_group=AsyncMock(return_value=RabbitMQResponse(id="rpc-1")),
        ),
    )
    handler._create_group_client = Mock(return_value=client)  # type: ignore[method-assign]

    result = await handler._handle_join_group(_build_group_rpc())

    assert result.error is not None
    assert result.error.code == -32603
    assert "no result" in result.error.message


@pytest.mark.asyncio
async def test_task_command_continue_complete_cancel_get_paths() -> None:
    runner = _DummyRunner()
    task_ctx = SimpleNamespace(task=SimpleNamespace(id="task-1"))
    runner.tasks["task-1"] = task_ctx
    for method_name in ("on_continue", "on_complete", "on_cancel", "on_get"):
        getattr(runner, method_name).return_value = _build_task_result("task-1")

    handler = GroupHandler("test-agent", cast("Any", runner))
    handler._group_clients["group-1"] = cast("Any", SimpleNamespace(is_joined=True))
    handler._broadcast_task_update = AsyncMock()  # type: ignore[method-assign]

    for command_type in (
        TaskCommandType.Continue,
        TaskCommandType.Complete,
        TaskCommandType.Cancel,
        TaskCommandType.Get,
    ):
        await handler._on_task_command(_build_command(command_type), is_mentioned=True)

    runner.on_continue.assert_awaited_once_with(ANY, task_ctx.task)
    runner.on_complete.assert_awaited_once()
    runner.on_cancel.assert_awaited_once()
    runner.on_get.assert_awaited_once()
    assert handler._broadcast_task_update.await_count == 4


@pytest.mark.asyncio
async def test_task_command_ignores_unmentioned_and_missing_task_paths() -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))
    handler._broadcast_task_update = AsyncMock()  # type: ignore[method-assign]

    await handler._on_task_command(_build_command(TaskCommandType.Start), is_mentioned=False)
    await handler._on_task_command(_build_command(TaskCommandType.Continue), is_mentioned=True)
    await handler._on_task_command(_build_command(TaskCommandType.Complete), is_mentioned=True)
    await handler._on_task_command(_build_command(TaskCommandType.Cancel), is_mentioned=True)
    await handler._on_task_command(_build_command(TaskCommandType.Get), is_mentioned=True)

    runner.on_start.assert_not_awaited()
    runner.on_continue.assert_not_awaited()
    runner.on_complete.assert_not_awaited()
    runner.on_cancel.assert_not_awaited()
    runner.on_get.assert_not_awaited()
    handler._broadcast_task_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcast_task_update_skip_and_success_paths() -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))
    task_result = _build_task_result("task-1")

    await handler._broadcast_task_update(task_result)

    disconnected = cast("Any", SimpleNamespace(is_joined=False, send_task_result=AsyncMock()))
    handler._group_clients["group-1"] = disconnected
    handler._task_group_map["task-1"] = "group-1"
    await handler._broadcast_task_update(task_result)

    joined = cast("Any", SimpleNamespace(is_joined=True, send_task_result=AsyncMock()))
    handler._group_clients["group-1"] = joined
    await handler._broadcast_task_update(task_result)

    disconnected.send_task_result.assert_not_awaited()
    joined.send_task_result.assert_awaited_once()
    assert joined.send_task_result.await_args.kwargs["session_id"] == "sess-new"


@pytest.mark.asyncio
async def test_broadcast_task_update_validates_required_result_fields() -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))
    client = cast("Any", SimpleNamespace(is_joined=True, send_task_result=AsyncMock()))
    handler._group_clients["group-1"] = client

    no_session = _build_task_result("task-1")
    no_session.sessionId = None
    await handler._broadcast_task_update(no_session, "group-1")

    no_status = _build_task_result("task-2")
    no_status_without_status = no_status.model_copy(update={"status": None})
    await handler._broadcast_task_update(no_status_without_status, "group-1")

    client.send_task_result.assert_not_awaited()


def test_find_group_for_sender_and_active_groups() -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))
    joined_client = cast(
        "Any",
        SimpleNamespace(
            is_joined=True,
            _group_info=SimpleNamespace(leader=SimpleNamespace(aic="leader-aic")),
        ),
    )
    other_client = cast("Any", SimpleNamespace(is_joined=False, _group_info=None))
    handler._group_clients = {"group-1": joined_client, "group-2": other_client}

    assert handler._find_group_for_sender("leader-aic") == "group-1"
    assert handler._find_group_for_sender("missing") is None
    assert handler.active_groups == {"group-1": joined_client}


@pytest.mark.asyncio
async def test_leave_group_success_failure_and_leave_all() -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))

    assert await handler.leave_group("missing") is False

    failing_client = cast("Any", SimpleNamespace(leave_group=AsyncMock(side_effect=RuntimeError("boom"))))
    handler._group_clients["group-fail"] = failing_client
    assert await handler.leave_group("group-fail") is False

    ok_client = cast("Any", SimpleNamespace(leave_group=AsyncMock()))
    handler._group_clients["group-ok"] = ok_client
    handler._task_group_map = {"task-ok": "group-ok", "task-other": "other"}
    assert await handler.leave_group("group-ok") is True
    assert "group-ok" not in handler._group_clients
    assert handler._task_group_map == {"task-other": "other"}

    handler._group_clients["group-left"] = cast("Any", SimpleNamespace(leave_group=AsyncMock()))
    await handler.leave_all_groups()
    assert set(handler._group_clients) == {"group-fail"}


@pytest.mark.asyncio
async def test_misc_callbacks_cover_observer_paths() -> None:
    runner = _DummyRunner()
    handler = GroupHandler("test-agent", cast("Any", runner))

    await handler._on_runner_state_change(_build_task_result(""))
    await handler._on_task_result(_build_task_result("task-1"))
    await handler._on_mgmt_command(
        GroupMgmtCommand(
            id="mgmt-1",
            sentAt=datetime.now(UTC).isoformat(),
            senderRole="leader",
            senderId="leader-aic",
            groupId="group-1",
            command=GroupMgmtCommandType.LEAVE_GROUP,
        )
    )

    handler._broadcast_task_update = AsyncMock()  # type: ignore[method-assign]
    handler._task_group_map["task-1"] = "group-1"
    await handler._on_runner_state_change(_build_task_result("task-1"))
    handler._broadcast_task_update.assert_awaited_once()
