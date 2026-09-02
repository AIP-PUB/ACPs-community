"""monitor-server Query API 的 HTTP client 辅助逻辑。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from acps_cli.shared.auth_session import AuthSessionError, OidcAuthSessionManager

_MAX_RAW_BODY_CHARS = 500


def _truncate_raw_body(value: str) -> str:
    if len(value) <= _MAX_RAW_BODY_CHARS:
        return value
    return f"{value[:_MAX_RAW_BODY_CHARS]}..."


@dataclass(frozen=True)
class MonitorClientError(Exception):
    """带诊断上下文的 Monitor client 请求/响应异常。"""

    method: str
    url: str
    message: str
    status_code: int | None = None
    json_body: Any | None = None
    raw_body: str | None = None

    def __str__(self) -> str:
        prefix = f"{self.method} {self.url}"
        if self.status_code is not None:
            return f"{prefix} returned HTTP {self.status_code}: {self.message}"
        return f"{prefix} failed: {self.message}"


class MonitorClient:
    def __init__(
        self,
        base_url: str,
        api_prefix: str,
        timeout: float,
        *,
        auth_mode: str = "none",
        auth_session: OidcAuthSessionManager | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_prefix = api_prefix.rstrip("/")
        self._timeout = timeout
        self._auth_mode = auth_mode
        self._auth_session = auth_session

    def get_health(self) -> dict[str, Any]:
        payload = self._request_json("GET", f"{self._base_url}/health", use_auth=False)
        if not isinstance(payload, dict):
            raise MonitorClientError(
                method="GET",
                url=f"{self._base_url}/health",
                status_code=200,
                message="Expected JSON object response",
                json_body=payload,
            )
        return payload

    def get_api(self, path: str, params: dict[str, Any] | None = None) -> Any:
        use_auth = self._auth_session is not None
        return self._request_json(
            "GET",
            self._build_api_url(path),
            params=params,
            use_auth=use_auth,
        )

    def post_api(self, path: str, payload: dict[str, Any]) -> Any:
        use_auth = self._auth_session is not None
        return self._request_json(
            "POST",
            self._build_api_url(path),
            payload=payload,
            use_auth=use_auth,
        )

    def _build_api_url(self, path: str) -> str:
        normalized_path = path if path.startswith("/") else f"/{path}"
        return f"{self._base_url}{self._api_prefix}{normalized_path}"

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        use_auth: bool,
        retry_on_unauthorized: bool = True,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if use_auth:
            headers.update(self._build_auth_headers())
        try:
            response = httpx.request(
                method,
                url,
                headers=headers,
                json=payload,
                params=params,
                timeout=self._timeout,
            )
        except httpx.RequestError as exc:
            raise MonitorClientError(method=method, url=url, message=str(exc)) from exc

        raw_body = response.text
        parsed_body: Any | None = None
        if raw_body:
            try:
                parsed_body = json.loads(raw_body)
            except json.JSONDecodeError:
                parsed_body = None

        if response.status_code == 401 and use_auth and retry_on_unauthorized and self._auth_session is not None:
            headers = {"Accept": "application/json", "Authorization": f"Bearer {self._refresh_after_unauthorized()}"}
            try:
                response = httpx.request(
                    method,
                    url,
                    headers=headers,
                    json=payload,
                    params=params,
                    timeout=self._timeout,
                )
            except httpx.RequestError as exc:
                raise MonitorClientError(method=method, url=url, message=str(exc)) from exc
            raw_body = response.text
            parsed_body = None
            if raw_body:
                try:
                    parsed_body = json.loads(raw_body)
                except json.JSONDecodeError:
                    parsed_body = None

        if 200 <= response.status_code < 300:
            if parsed_body is None:
                raise MonitorClientError(
                    method=method,
                    url=url,
                    status_code=response.status_code,
                    message="Expected JSON response body",
                    raw_body=_truncate_raw_body(raw_body),
                )
            return parsed_body

        message = self._failure_message(status_code=response.status_code, use_auth=use_auth)
        raise MonitorClientError(
            method=method,
            url=url,
            status_code=response.status_code,
            message=message,
            json_body=parsed_body,
            raw_body=None if parsed_body is not None else _truncate_raw_body(raw_body),
        )

    def _build_auth_headers(self) -> dict[str, str]:
        if self._auth_session is None:
            return {}
        try:
            access_token = self._auth_session.get_access_token()
        except AuthSessionError as exc:
            raise MonitorClientError(
                method="AUTH",
                url=self._base_url,
                message=str(exc),
            ) from exc
        return {"Authorization": f"Bearer {access_token}"}

    def _refresh_after_unauthorized(self) -> str:
        if self._auth_session is None:
            raise MonitorClientError(method="AUTH", url=self._base_url, message="Authentication is not configured")
        try:
            return self._auth_session.handle_unauthorized()
        except AuthSessionError as exc:
            raise MonitorClientError(
                method="AUTH",
                url=self._base_url,
                message=f"{exc} Verify monitor OIDC configuration and log in again.",
            ) from exc

    def _failure_message(self, *, status_code: int, use_auth: bool) -> str:
        if status_code == 401 and not use_auth and self._auth_mode == "none":
            return 'Authentication required. Set [monitor.auth].mode = "oidc" and run acps-cli monitor auth login.'
        if status_code == 401 and use_auth:
            return "Authentication failed after retry. Verify monitor OIDC configuration and log in again."
        if status_code == 403 and use_auth:
            return "Permission denied. Verify monitor roles, scopes, and allowed AIC scope."
        return "Non-success response"
