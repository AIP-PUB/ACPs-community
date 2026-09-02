"""Leader submit authorization tests."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from acps_sdk.oidc import HumanPrincipal, build_principal_id, build_principal_key
from httpx import AsyncClient

from .conftest import build_submit_request, extract_session_id


def _principal(*, subject: str = "leader-user-001", roles: tuple[str, ...] = ("user",)) -> HumanPrincipal:
    issuer = "https://keycloak.example.com/realms/acps-leader"
    return HumanPrincipal(
        issuer=issuer,
        subject=subject,
        principal_key=build_principal_key(issuer=issuer, subject=subject),
        principal_id=build_principal_id(issuer=issuer, subject=subject),
        audiences=("leader-api",),
        username="leader-user",
        name="Leader User",
        email="leader@example.com",
        roles=roles,
        scopes=("leader:submit",),
        raw_claims={},
    )


@pytest.mark.asyncio
async def test_submit_requires_bearer_token_when_oidc_enabled(app, client: AsyncClient, monkeypatch) -> None:
    import assistant.auth as auth_mod
    import assistant.config as config_mod

    monkeypatch.setitem(config_mod.settings["oidc"], "enabled", True)
    monkeypatch.setattr(auth_mod, "_validator", MagicMock())

    response = await client.post(
        "/api/v1/submit",
        json=build_submit_request(query="你好，帮我规划北京三日游"),
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_submit_ignores_body_user_id_and_binds_principal(
    app,
    client: AsyncClient,
    session_manager,
    scenario_loader,
    monkeypatch,
) -> None:
    import assistant.config as config_mod
    from assistant.api import routes
    from assistant.api.schemas import SubmitResponse, SubmitResult
    from assistant.models import ActiveTaskStatus

    principal = _principal()

    monkeypatch.setitem(config_mod.settings["oidc"], "enabled", True)

    async def override_principal():
        return principal

    async def fake_handle_submit(submit_request, *, principal):
        assert principal is not None
        assert submit_request.user_id == principal.principal_id
        assert submit_request.user_id != "spoofed-user-id"

        session = session_manager.create_session(
            mode=submit_request.mode,
            base_scenario=scenario_loader.base_scenario,
            user_id=submit_request.user_id,
            principal_issuer=principal.issuer,
            principal_subject=principal.subject,
            principal_username=principal.username,
            principal_email=principal.email,
        )
        return SubmitResponse(
            result=SubmitResult(
                sessionId=session.session_id,
                mode=session.mode,
                activeTaskId="task-auth-test",
                acceptedAt=datetime.now(UTC).isoformat(),
                externalStatus=ActiveTaskStatus.PENDING,
            )
        )

    app.dependency_overrides[routes.require_leader_user] = override_principal
    app.state.orchestrator.handle_submit = AsyncMock(side_effect=fake_handle_submit)

    try:
        response = await client.post(
            "/api/v1/submit",
            json=build_submit_request(
                query="给我一份上海周末行程",
                user_id="spoofed-user-id",
            ),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    session_id = extract_session_id(response.json())
    session = session_manager.get_session(session_id)
    assert session is not None
    assert session.user_id == principal.principal_id
    assert session.user_id != "spoofed-user-id"
    assert session.principal_subject == principal.subject
