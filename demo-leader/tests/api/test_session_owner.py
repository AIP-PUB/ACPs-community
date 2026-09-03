"""Leader session ownership authorization tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from acps_sdk.oidc import HumanPrincipal, build_principal_id, build_principal_key
from httpx import AsyncClient


def _principal(*, subject: str, roles: tuple[str, ...] = ("user",)) -> HumanPrincipal:
    issuer = "https://keycloak.example.com/realms/acps-leader"
    return HumanPrincipal(
        issuer=issuer,
        subject=subject,
        principal_key=build_principal_key(issuer=issuer, subject=subject),
        principal_id=build_principal_id(issuer=issuer, subject=subject),
        audiences=("leader-api",),
        username=subject,
        name=subject,
        email=f"{subject}@example.com",
        roles=roles,
        scopes=("leader:submit",),
        raw_claims={},
    )


@pytest.mark.asyncio
async def test_result_rejects_non_owner_when_oidc_enabled(
    app,
    client: AsyncClient,
    session_manager,
    scenario_loader,
    monkeypatch,
) -> None:
    import assistant.config as config_mod
    from assistant.api import routes
    from assistant.models import ExecutionMode

    owner = _principal(subject="owner-user")
    other = _principal(subject="other-user")
    session = session_manager.create_session(
        mode=ExecutionMode.DIRECT_RPC,
        base_scenario=scenario_loader.base_scenario,
        user_id=owner.principal_id,
        principal_issuer=owner.issuer,
        principal_subject=owner.subject,
    )

    monkeypatch.setitem(config_mod.settings["oidc"], "enabled", True)

    async def override_principal():
        return other

    app.dependency_overrides[routes.get_request_principal] = override_principal

    response = await client.get(f"/api/v1/result/{session.session_id}")

    assert response.status_code == 403
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_operator_can_inspect_and_cancel_other_session(
    app,
    client: AsyncClient,
    session_manager,
    scenario_loader,
    monkeypatch,
) -> None:
    import assistant.config as config_mod
    from assistant.api import routes
    from assistant.models import ExecutionMode

    from leader.assistant.api import routes as routes_module

    owner = _principal(subject="owner-user")
    operator = _principal(subject="operator-user", roles=("operator",))
    session = session_manager.create_session(
        mode=ExecutionMode.DIRECT_RPC,
        base_scenario=scenario_loader.base_scenario,
        user_id=owner.principal_id,
        principal_issuer=owner.issuer,
        principal_subject=owner.subject,
    )
    emit_mock = AsyncMock()
    monkeypatch.setattr(routes_module.LEADER_EMITTER, "emit", emit_mock)

    monkeypatch.setitem(config_mod.settings["oidc"], "enabled", True)

    async def override_principal():
        return operator

    app.dependency_overrides[routes.get_request_principal] = override_principal

    result_response = await client.get(f"/api/v1/result/{session.session_id}")
    cancel_response = await client.post(f"/api/v1/cancel/{session.session_id}", json={"deleteSession": False})

    assert result_response.status_code == 200
    assert cancel_response.status_code == 200
    emit_call = emit_mock.await_args
    assert emit_call is not None
    audit_body = emit_call.args[0]
    assert audit_body.actor.id == operator.principal_id

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_operator_stream_token_supports_foreign_session(
    app,
    client: AsyncClient,
    session_manager,
    scenario_loader,
    monkeypatch,
) -> None:
    import asyncio

    import assistant.config as config_mod
    import assistant.core.stream_event_bus as bus_mod
    from assistant.api import routes
    from assistant.core.stream_event_bus import StreamEventBus
    from assistant.models import ExecutionMode

    owner = _principal(subject="owner-user")
    operator = _principal(subject="operator-user", roles=("operator",))
    session = session_manager.create_session(
        mode=ExecutionMode.DIRECT_RPC,
        base_scenario=scenario_loader.base_scenario,
        user_id=owner.principal_id,
        principal_issuer=owner.issuer,
        principal_subject=owner.subject,
    )
    preloaded_queue: asyncio.Queue[object | None] = asyncio.Queue()
    await preloaded_queue.put(None)
    fake_bus = StreamEventBus()

    monkeypatch.setitem(config_mod.settings["oidc"], "enabled", True)
    monkeypatch.setattr(bus_mod, "get_stream_event_bus", lambda: fake_bus)
    monkeypatch.setattr(fake_bus, "subscribe", lambda _session_id: preloaded_queue)

    async def override_principal():
        return operator

    app.dependency_overrides[routes.get_request_principal] = override_principal

    try:
        token_response = await client.post(f"/api/v1/stream-token/{session.session_id}")
        assert token_response.status_code == 200

        stream_token = token_response.json()["result"]["streamToken"]
        stream_response = await client.get(
            f"/api/v1/stream/{session.session_id}",
            params={"streamToken": stream_token},
        )
    finally:
        app.dependency_overrides.clear()

    assert stream_response.status_code == 200
    assert '{"done":true}' in stream_response.text
