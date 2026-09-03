from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from acps_cli.monitor.client import MonitorClient, MonitorClientError


def _response(*, status_code: int, body: str) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.text = body
    return response


class StubAuthSession:
    def __init__(self) -> None:
        self.access_token = "access-token-1"
        self.get_access_token_calls = 0
        self.handle_unauthorized_calls = 0

    def get_access_token(self) -> str:
        self.get_access_token_calls += 1
        return self.access_token

    def handle_unauthorized(self) -> str:
        self.handle_unauthorized_calls += 1
        self.access_token = "access-token-2"
        return self.access_token


def test_get_health_uses_health_endpoint_without_api_prefix() -> None:
    client = MonitorClient("http://localhost:9009", "/acps-amp-v1", 15.0)

    with patch("httpx.request", return_value=_response(status_code=200, body='{"status":"ok"}')) as mock_request:
        payload = client.get_health()

    assert payload == {"status": "ok"}
    assert mock_request.call_args.args[0] == "GET"
    assert mock_request.call_args.args[1] == "http://localhost:9009/health"


def test_get_api_includes_api_prefix_and_params() -> None:
    client = MonitorClient("http://localhost:9009", "/acps-amp-v1", 15.0)

    with patch("httpx.request", return_value=_response(status_code=200, body='{"items":[]}')) as mock_request:
        payload = client.get_api("/heartbeat/summary", params={"aic": "demo"})

    assert payload == {"items": []}
    assert mock_request.call_args.args[1] == "http://localhost:9009/acps-amp-v1/heartbeat/summary"
    assert mock_request.call_args.kwargs["params"] == {"aic": "demo"}


def test_get_api_includes_authorization_header_in_oidc_mode() -> None:
    auth_session = StubAuthSession()
    client = MonitorClient("http://localhost:9009", "/acps-amp-v1", 15.0, auth_mode="oidc", auth_session=auth_session)

    with patch("httpx.request", return_value=_response(status_code=200, body='{"items":[]}')) as mock_request:
        client.get_api("/heartbeat/summary")

    assert auth_session.get_access_token_calls == 1
    assert mock_request.call_args.kwargs["headers"]["Authorization"] == "Bearer access-token-1"


def test_get_health_skips_authorization_header_even_in_oidc_mode() -> None:
    auth_session = StubAuthSession()
    client = MonitorClient("http://localhost:9009", "/acps-amp-v1", 15.0, auth_mode="oidc", auth_session=auth_session)

    with patch("httpx.request", return_value=_response(status_code=200, body='{"status":"ok"}')) as mock_request:
        client.get_health()

    assert auth_session.get_access_token_calls == 0
    assert "Authorization" not in mock_request.call_args.kwargs["headers"]


def test_post_api_sends_json_payload() -> None:
    client = MonitorClient("http://localhost:9009", "/acps-amp-v1", 15.0)

    with patch("httpx.request", return_value=_response(status_code=200, body='{"ok":true}')) as mock_request:
        payload = client.post_api("/metrics/series/query", {"metric": "cpu"})

    assert payload == {"ok": True}
    assert mock_request.call_args.args[0] == "POST"
    assert mock_request.call_args.kwargs["json"] == {"metric": "cpu"}


def test_request_json_accepts_json_array_for_success() -> None:
    client = MonitorClient("http://localhost:9009", "/acps-amp-v1", 15.0)

    with patch("httpx.request", return_value=_response(status_code=200, body='[{"id":1}]')):
        payload = client.get_api("/array")

    assert payload == [{"id": 1}]


def test_request_json_raises_for_non_json_success_response() -> None:
    client = MonitorClient("http://localhost:9009", "/acps-amp-v1", 15.0)

    with (
        patch("httpx.request", return_value=_response(status_code=200, body="plain text")),
        pytest.raises(MonitorClientError, match="Expected JSON response body"),
    ):
        client.get_api("/heartbeat/summary")


def test_request_json_raises_with_json_body_for_non_success_response() -> None:
    client = MonitorClient("http://localhost:9009", "/acps-amp-v1", 15.0)

    with (
        patch("httpx.request", return_value=_response(status_code=404, body='{"error":"missing"}')),
        pytest.raises(MonitorClientError) as exc_info,
    ):
        client.get_api("/metrics/rankings/query")

    exc = exc_info.value
    assert exc.status_code == 404
    assert exc.json_body == {"error": "missing"}
    assert exc.raw_body is None


def test_request_json_retries_once_after_401_in_oidc_mode() -> None:
    auth_session = StubAuthSession()
    client = MonitorClient("http://localhost:9009", "/acps-amp-v1", 15.0, auth_mode="oidc", auth_session=auth_session)

    with patch(
        "httpx.request",
        side_effect=[
            _response(status_code=401, body='{"error":"expired"}'),
            _response(status_code=200, body='{"items":[1]}'),
        ],
    ) as mock_request:
        payload = client.get_api("/metrics/snapshots/query")

    assert payload == {"items": [1]}
    assert auth_session.handle_unauthorized_calls == 1
    first_headers = mock_request.call_args_list[0].kwargs["headers"]
    second_headers = mock_request.call_args_list[1].kwargs["headers"]
    assert first_headers["Authorization"] == "Bearer access-token-1"
    assert second_headers["Authorization"] == "Bearer access-token-2"


def test_request_json_raises_with_raw_body_for_non_success_non_json_response() -> None:
    client = MonitorClient("http://localhost:9009", "/acps-amp-v1", 15.0)

    with (
        patch("httpx.request", return_value=_response(status_code=503, body="unavailable")),
        pytest.raises(MonitorClientError) as exc_info,
    ):
        client.get_api("/system/events/query")

    exc = exc_info.value
    assert exc.status_code == 503
    assert exc.json_body is None
    assert exc.raw_body == "unavailable"


def test_none_mode_401_includes_oidc_hint() -> None:
    client = MonitorClient("http://localhost:9009", "/acps-amp-v1", 15.0, auth_mode="none")

    with (
        patch("httpx.request", return_value=_response(status_code=401, body='{"error":"missing"}')),
        pytest.raises(MonitorClientError) as exc_info,
    ):
        client.get_api("/system/events/query")

    assert "monitor auth login" in str(exc_info.value)


def test_oidc_mode_403_includes_permission_hint() -> None:
    client = MonitorClient(
        "http://localhost:9009",
        "/acps-amp-v1",
        15.0,
        auth_mode="oidc",
        auth_session=StubAuthSession(),
    )

    with (
        patch("httpx.request", return_value=_response(status_code=403, body='{"error":"forbidden"}')),
        pytest.raises(MonitorClientError) as exc_info,
    ):
        client.get_api("/system/events/query")

    assert "roles, scopes, and allowed AIC scope" in str(exc_info.value)


def test_request_json_raises_for_network_error() -> None:
    client = MonitorClient("http://localhost:9009", "/acps-amp-v1", 15.0)

    with (
        patch("httpx.request", side_effect=httpx.ConnectError("boom")),
        pytest.raises(MonitorClientError, match="boom"),
    ):
        client.get_api("/heartbeat/summary")
