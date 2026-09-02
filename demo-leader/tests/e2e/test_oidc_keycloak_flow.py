"""黑盒 E2E：demo-leader 与 Keycloak 的真实 OIDC 联调。"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.oidc]

BASE_URL = "https://localhost:9011"
API_PREFIX = "/api/v1"
E2E_REQUEST_TIMEOUT = float(os.getenv("LEADER_E2E_REQUEST_TIMEOUT", "120"))


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"未设置 {name}，跳过 OIDC 黑盒联调测试")
    return value


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _issue_password_grant_token(
    *,
    issuer: str,
    client_id: str,
    username: str,
    password: str,
    client_secret: str | None = None,
) -> str:
    token_endpoint = f"{issuer.rstrip('/')}/protocol/openid-connect/token"
    form_data = {
        "grant_type": "password",
        "client_id": client_id,
        "username": username,
        "password": password,
        "scope": "openid profile email",
    }
    if client_secret:
        form_data["client_secret"] = client_secret

    with httpx.Client(trust_env=False, timeout=30.0) as auth_client:
        response = auth_client.post(token_endpoint, data=form_data)

    assert response.status_code == 200, response.text
    payload = response.json()
    access_token = payload.get("access_token")
    assert isinstance(access_token, str) and access_token
    return access_token


def _api_url(path: str) -> str:
    return f"{BASE_URL}{API_PREFIX}{path}"


def _build_submit_payload(*, query: str, user_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": query,
        "mode": "direct_rpc",
        "clientRequestId": f"oidc_{uuid.uuid4().hex[:12]}",
    }
    if user_id is not None:
        payload["userId"] = user_id
    return payload


def _submit_or_skip_for_llm_unavailable(
    client: httpx.Client,
    *,
    access_token: str,
    query: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    response = client.post(
        _api_url("/submit"),
        headers=_auth_headers(access_token),
        json=_build_submit_payload(query=query, user_id=user_id),
    )

    if response.status_code == 500:
        try:
            detail = response.json().get("detail", {})
        except ValueError:
            detail = {}
        if isinstance(detail, dict) and detail.get("code") in {"LLM_CALL_ERROR", "LLM_SERVICE_UNAVAILABLE"}:
            pytest.skip(f"LLM 服务不可用，跳过需要创建真实 session 的 OIDC 黑盒联调：{detail}")

    assert response.status_code == 200, response.text
    payload = response.json()
    result = payload.get("result")
    assert isinstance(result, dict), payload
    return result


@pytest.fixture(scope="session")
def oidc_issuer() -> str:
    return _require_env("TEST_OIDC_ISSUER")


@pytest.fixture(scope="session")
def oidc_e2e_client_id() -> str:
    return _require_env("TEST_OIDC_E2E_CLIENT_ID")


@pytest.fixture(scope="session")
def oidc_e2e_client_secret() -> str | None:
    client_secret = os.getenv("TEST_OIDC_E2E_CLIENT_SECRET", "").strip()
    return client_secret or None


@pytest.fixture(scope="session")
def oidc_user_username() -> str:
    return _require_env("TEST_OIDC_USER_USERNAME")


@pytest.fixture(scope="session")
def oidc_user_password() -> str:
    return _require_env("TEST_OIDC_USER_PASSWORD")


@pytest.fixture(scope="session")
def oidc_operator_username() -> str:
    return _require_env("TEST_OIDC_OPERATOR_USERNAME")


@pytest.fixture(scope="session")
def oidc_operator_password() -> str:
    return _require_env("TEST_OIDC_OPERATOR_PASSWORD")


@pytest.fixture(scope="session")
def foreign_oidc_issuer() -> str:
    return _require_env("TEST_OIDC_FOREIGN_ISSUER")


@pytest.fixture(scope="session")
def foreign_oidc_client_id() -> str:
    return _require_env("TEST_OIDC_FOREIGN_CLIENT_ID")


@pytest.fixture(scope="session")
def foreign_oidc_client_secret() -> str | None:
    client_secret = os.getenv("TEST_OIDC_FOREIGN_CLIENT_SECRET", "").strip()
    return client_secret or None


@pytest.fixture(scope="session")
def foreign_oidc_username() -> str:
    return _require_env("TEST_OIDC_FOREIGN_USERNAME")


@pytest.fixture(scope="session")
def foreign_oidc_password() -> str:
    return _require_env("TEST_OIDC_FOREIGN_PASSWORD")


@pytest.fixture
def issue_leader_token(
    oidc_issuer: str,
    oidc_e2e_client_id: str,
    oidc_e2e_client_secret: str | None,
) -> Callable[[str, str], str]:
    def _issue(username: str, password: str) -> str:
        return _issue_password_grant_token(
            issuer=oidc_issuer,
            client_id=oidc_e2e_client_id,
            client_secret=oidc_e2e_client_secret,
            username=username,
            password=password,
        )

    return _issue


@pytest.fixture
def issue_foreign_token(
    foreign_oidc_issuer: str,
    foreign_oidc_client_id: str,
    foreign_oidc_client_secret: str | None,
) -> Callable[[str, str], str]:
    def _issue(username: str, password: str) -> str:
        return _issue_password_grant_token(
            issuer=foreign_oidc_issuer,
            client_id=foreign_oidc_client_id,
            client_secret=foreign_oidc_client_secret,
            username=username,
            password=password,
        )

    return _issue


def test_cross_realm_token_is_rejected_by_leader_api(
    issue_foreign_token: Callable[[str, str], str],
    foreign_oidc_username: str,
    foreign_oidc_password: str,
) -> None:
    foreign_access_token = issue_foreign_token(foreign_oidc_username, foreign_oidc_password)

    with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
        response = client.post(
            _api_url("/submit"),
            headers=_auth_headers(foreign_access_token),
            json=_build_submit_payload(query="我想规划北京两日游"),
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_oidc_submit_binds_session_to_authenticated_principal(
    issue_leader_token: Callable[[str, str], str],
    oidc_user_username: str,
    oidc_user_password: str,
) -> None:
    user_access_token = issue_leader_token(oidc_user_username, oidc_user_password)

    with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
        submit_result = _submit_or_skip_for_llm_unavailable(
            client,
            access_token=user_access_token,
            query="你好，请帮我规划北京三日游",
            user_id="spoofed-user-id",
        )
        session_id = submit_result["sessionId"]

        result_response = client.get(
            _api_url(f"/result/{session_id}"),
            headers=_auth_headers(user_access_token),
        )

    assert result_response.status_code == 200, result_response.text
    payload = result_response.json()
    result = payload["result"]
    assert result["sessionId"] == session_id
    assert result["userId"]
    assert result["userId"] != "spoofed-user-id"
    principal = result["userContext"]["principal"]
    assert principal["username"] == oidc_user_username
    assert "user" in principal["roles"]


def test_oidc_operator_can_read_and_cancel_other_users_session(
    issue_leader_token: Callable[[str, str], str],
    oidc_user_username: str,
    oidc_user_password: str,
    oidc_operator_username: str,
    oidc_operator_password: str,
) -> None:
    user_access_token = issue_leader_token(oidc_user_username, oidc_user_password)
    operator_access_token = issue_leader_token(oidc_operator_username, oidc_operator_password)

    with httpx.Client(timeout=E2E_REQUEST_TIMEOUT) as client:
        submit_result = _submit_or_skip_for_llm_unavailable(
            client,
            access_token=user_access_token,
            query="请给我做一个北京周末路线建议",
        )
        session_id = submit_result["sessionId"]

        operator_result_response = client.get(
            _api_url(f"/result/{session_id}"),
            headers=_auth_headers(operator_access_token),
        )
        assert operator_result_response.status_code == 200, operator_result_response.text

        stream_token_response = client.post(
            _api_url(f"/stream-token/{session_id}"),
            headers=_auth_headers(operator_access_token),
        )
        assert stream_token_response.status_code == 200, stream_token_response.text
        stream_payload = stream_token_response.json()
        assert stream_payload["result"]["sessionId"] == session_id
        assert stream_payload["result"]["streamToken"]

        cancel_response = client.post(
            _api_url(f"/cancel/{session_id}"),
            headers=_auth_headers(operator_access_token),
        )
        assert cancel_response.status_code == 200, cancel_response.text
        cancel_payload = cancel_response.json()
        assert cancel_payload["result"]["sessionId"] == session_id
        assert cancel_payload["result"]["success"] is True

        owner_result_response = client.get(
            _api_url(f"/result/{session_id}"),
            headers=_auth_headers(user_access_token),
        )

    assert owner_result_response.status_code == 200, owner_result_response.text
    owner_payload = owner_result_response.json()
    owner_result = owner_payload["result"]
    assert owner_result["closed"] is True
    assert owner_result["closedReason"] == "user_cancel"
