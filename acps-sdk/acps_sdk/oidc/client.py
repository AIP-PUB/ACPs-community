"""OIDC Device Authorization Grant 协议 helper。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator

from acps_sdk.oidc.discovery import OidcDiscoveryDocument
from acps_sdk.oidc.errors import (
    OidcClientError,
    OidcDeviceAuthorizationDeniedError,
    OidcDeviceAuthorizationExpiredError,
    OidcDeviceAuthorizationNotSupportedError,
    OidcProviderUnavailableError,
)


class OidcDeviceClientConfig(BaseModel):
    """OIDC Device Grant 客户端配置。"""

    model_config = ConfigDict(extra="forbid")

    issuer: str
    client_id: str
    scopes: tuple[str, ...] = ()
    http_timeout_seconds: float = 5.0
    require_https: bool = True

    @field_validator("issuer")
    @classmethod
    def _validate_issuer(cls, value: str) -> str:
        issuer = value.strip().rstrip("/")
        if not issuer:
            raise ValueError("issuer must not be empty")
        return issuer

    @field_validator("client_id")
    @classmethod
    def _validate_client_id(cls, value: str) -> str:
        client_id = value.strip()
        if not client_id:
            raise ValueError("client_id must not be empty")
        return client_id

    @field_validator("scopes", mode="before")
    @classmethod
    def _normalize_scopes(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            values = value.split()
        elif isinstance(value, (bytes, bytearray)):
            raise ValueError("scopes must be a string or iterable of strings")
        elif isinstance(value, Iterable):
            values = [str(item) for item in value]
        else:
            raise ValueError("scopes must be a string or iterable of strings")

        seen: set[str] = set()
        result: list[str] = []
        for raw_value in values:
            scope = raw_value.strip()
            if not scope or scope in seen:
                continue
            seen.add(scope)
            result.append(scope)
        return tuple(result)

    @field_validator("http_timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("http_timeout_seconds must be > 0")
        return value


class OidcDeviceAuthorizationResponse(BaseModel):
    """Device Authorization endpoint 响应。"""

    model_config = ConfigDict(extra="allow")

    device_code: SecretStr
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = None
    expires_in: int
    interval: int = 5
    expires_at: datetime

    @field_validator("expires_in", "interval")
    @classmethod
    def _validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("value must be > 0")
        return value


class OidcTokenResponse(BaseModel):
    """Token endpoint 成功响应。"""

    model_config = ConfigDict(extra="allow")

    access_token: SecretStr
    token_type: str = "Bearer"
    expires_in: int | None = None
    refresh_token: SecretStr | None = None
    refresh_expires_in: int | None = None
    scope: str | None = None
    id_token: SecretStr | None = None

    @field_validator("token_type", mode="before")
    @classmethod
    def _normalize_token_type(cls, value: object) -> str:
        raw_value = "Bearer" if value in (None, "") else str(value).strip()
        if raw_value.lower() != "bearer":
            raise ValueError("token_type must be Bearer")
        return "Bearer"

    @field_validator("expires_in", "refresh_expires_in")
    @classmethod
    def _validate_non_negative_int(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 0:
            raise ValueError("value must be >= 0")
        return value

    @property
    def access_token_value(self) -> str:
        return self.access_token.get_secret_value()

    @property
    def refresh_token_value(self) -> str | None:
        if self.refresh_token is None:
            return None
        return self.refresh_token.get_secret_value()

    @property
    def id_token_value(self) -> str | None:
        if self.id_token is None:
            return None
        return self.id_token.get_secret_value()


class OidcDeviceTokenPollingStatus(str, Enum):
    """Device token polling 的非终止状态。"""

    SUCCESS = "success"
    AUTHORIZATION_PENDING = "authorization_pending"
    SLOW_DOWN = "slow_down"


class OidcDeviceTokenPollingResult(BaseModel):
    """单次轮询 token endpoint 的结果。"""

    model_config = ConfigDict(extra="forbid")

    status: OidcDeviceTokenPollingStatus
    interval: int
    token_response: OidcTokenResponse | None = None

    @field_validator("interval")
    @classmethod
    def _validate_interval(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("interval must be > 0")
        return value


class OidcTokenRevocationResult(BaseModel):
    """Token revocation 的结构化结果。"""

    model_config = ConfigDict(extra="forbid")

    attempted: bool
    revoked: bool
    reason: str | None = None


class OidcDeviceClient:
    """同步 OIDC Device Authorization Grant 客户端。"""

    def __init__(
        self,
        config: OidcDeviceClientConfig,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._http_client = http_client or httpx.Client(
            timeout=config.http_timeout_seconds
        )
        self._owns_http_client = http_client is None
        self._discovery_document: OidcDiscoveryDocument | None = None

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def __enter__(self) -> OidcDeviceClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @property
    def config(self) -> OidcDeviceClientConfig:
        return self._config

    def get_discovery_document(self) -> OidcDiscoveryDocument:
        if self._discovery_document is None:
            self._discovery_document = self._fetch_discovery_document()
        return self._discovery_document

    def start_device_authorization(self) -> OidcDeviceAuthorizationResponse:
        document = self.get_discovery_document()
        endpoint = self._required_endpoint(
            document.device_authorization_endpoint,
            "device_authorization_endpoint",
            unsupported_message="OIDC provider does not expose device_authorization_endpoint",
            error_cls=OidcDeviceAuthorizationNotSupportedError,
        )
        payload: dict[str, str] = {"client_id": self._config.client_id}
        scope = " ".join(self._config.scopes)
        if scope:
            payload["scope"] = scope
        response_payload = self._post_form(
            endpoint, payload, endpoint_name="device authorization endpoint"
        )
        expires_in = self._required_positive_int(
            response_payload, field_name="expires_in"
        )
        interval = self._optional_positive_int(
            response_payload, field_name="interval", default=5
        )
        try:
            return OidcDeviceAuthorizationResponse.model_validate(
                {
                    **response_payload,
                    "expires_in": expires_in,
                    "interval": interval,
                    "expires_at": datetime.now(tz=timezone.utc)
                    + timedelta(seconds=expires_in),
                }
            )
        except ValidationError as exc:
            raise OidcClientError(
                "OIDC device authorization response is invalid"
            ) from exc

    def poll_device_token(
        self,
        device_code: str,
        *,
        interval: int = 5,
    ) -> OidcDeviceTokenPollingResult:
        document = self.get_discovery_document()
        endpoint = self._required_endpoint(
            document.token_endpoint,
            "token_endpoint",
            unsupported_message="OIDC provider does not expose token_endpoint",
            error_cls=OidcClientError,
        )
        payload = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": self._config.client_id,
            "device_code": device_code,
        }
        response = self._request_form("POST", endpoint, payload)
        if 200 <= response.status_code < 300:
            token_response = self._parse_token_response(response)
            return OidcDeviceTokenPollingResult(
                status=OidcDeviceTokenPollingStatus.SUCCESS,
                interval=interval,
                token_response=token_response,
            )

        oauth_error = self._parse_oauth_error(response)
        error_code = oauth_error.get("error")
        error_description = oauth_error.get("error_description")
        if error_code == OidcDeviceTokenPollingStatus.AUTHORIZATION_PENDING.value:
            return OidcDeviceTokenPollingResult(
                status=OidcDeviceTokenPollingStatus.AUTHORIZATION_PENDING,
                interval=interval,
            )
        if error_code == OidcDeviceTokenPollingStatus.SLOW_DOWN.value:
            return OidcDeviceTokenPollingResult(
                status=OidcDeviceTokenPollingStatus.SLOW_DOWN,
                interval=interval + 5,
            )
        if error_code == "access_denied":
            raise OidcDeviceAuthorizationDeniedError(
                "OIDC device authorization was denied",
                error=error_code,
                error_description=error_description,
                status_code=response.status_code,
            )
        if error_code == "expired_token":
            raise OidcDeviceAuthorizationExpiredError(
                "OIDC device authorization expired",
                error=error_code,
                error_description=error_description,
                status_code=response.status_code,
            )
        raise self._build_oauth_error(
            "OIDC device token polling failed",
            response=response,
            error=error_code,
            error_description=error_description,
        )

    def refresh_token(self, refresh_token: str) -> OidcTokenResponse:
        document = self.get_discovery_document()
        endpoint = self._required_endpoint(
            document.token_endpoint,
            "token_endpoint",
            unsupported_message="OIDC provider does not expose token_endpoint",
            error_cls=OidcClientError,
        )
        response = self._request_form(
            "POST",
            endpoint,
            {
                "grant_type": "refresh_token",
                "client_id": self._config.client_id,
                "refresh_token": refresh_token,
            },
        )
        if 200 <= response.status_code < 300:
            return self._parse_token_response(response)

        oauth_error = self._parse_oauth_error(response)
        raise self._build_oauth_error(
            "OIDC refresh token request failed",
            response=response,
            error=oauth_error.get("error"),
            error_description=oauth_error.get("error_description"),
        )

    def revoke_token(
        self,
        token: str,
        *,
        token_type_hint: str | None = None,
    ) -> OidcTokenRevocationResult:
        document = self.get_discovery_document()
        endpoint = document.revocation_endpoint
        if not endpoint:
            return OidcTokenRevocationResult(
                attempted=False,
                revoked=False,
                reason="revocation_endpoint_unavailable",
            )
        endpoint = self._required_endpoint(
            endpoint,
            "revocation_endpoint",
            unsupported_message="OIDC provider does not expose revocation_endpoint",
            error_cls=OidcClientError,
        )
        payload = {
            "client_id": self._config.client_id,
            "token": token,
        }
        if token_type_hint:
            payload["token_type_hint"] = token_type_hint
        response = self._request_form("POST", endpoint, payload)
        if 200 <= response.status_code < 300:
            return OidcTokenRevocationResult(attempted=True, revoked=True)

        oauth_error = self._parse_oauth_error(response)
        raise self._build_oauth_error(
            "OIDC token revocation failed",
            response=response,
            error=oauth_error.get("error"),
            error_description=oauth_error.get("error_description"),
        )

    def _fetch_discovery_document(self) -> OidcDiscoveryDocument:
        self._enforce_https(url=self._config.issuer, kind="issuer")
        well_known_url = f"{self._config.issuer}/.well-known/openid-configuration"
        try:
            response = self._http_client.get(well_known_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OidcProviderUnavailableError(
                f"failed to load OIDC discovery document from {well_known_url}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise OidcProviderUnavailableError(
                f"OIDC discovery document at {well_known_url} is not valid JSON"
            ) from exc

        try:
            document = OidcDiscoveryDocument.model_validate(payload)
        except ValidationError as exc:
            raise OidcProviderUnavailableError(
                f"OIDC discovery document at {well_known_url} is invalid"
            ) from exc

        if document.issuer.rstrip("/") != self._config.issuer:
            raise OidcProviderUnavailableError(
                f"OIDC discovery issuer mismatch: expected {self._config.issuer}, got {document.issuer}"
            )
        self._enforce_https(url=document.jwks_uri, kind="jwks_uri")
        for endpoint_name in (
            "authorization_endpoint",
            "device_authorization_endpoint",
            "token_endpoint",
            "revocation_endpoint",
            "end_session_endpoint",
        ):
            endpoint = getattr(document, endpoint_name)
            if endpoint:
                self._enforce_https(url=endpoint, kind=endpoint_name)
        return document

    def _required_endpoint(
        self,
        endpoint: str | None,
        kind: str,
        *,
        unsupported_message: str,
        error_cls: type[OidcClientError],
    ) -> str:
        if not endpoint:
            raise error_cls(unsupported_message)
        self._enforce_https(url=endpoint, kind=kind)
        return endpoint

    def _request_form(
        self, method: str, url: str, payload: dict[str, str]
    ) -> httpx.Response:
        try:
            return self._http_client.request(
                method,
                url,
                data=payload,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OidcClientError(f"OIDC request to {url} failed") from exc

    def _post_form(
        self, url: str, payload: dict[str, str], *, endpoint_name: str
    ) -> dict[str, Any]:
        response = self._request_form("POST", url, payload)
        if 200 <= response.status_code < 300:
            return self._parse_json_object(
                response, f"OIDC {endpoint_name} response is invalid"
            )

        oauth_error = self._parse_oauth_error(response)
        raise self._build_oauth_error(
            f"OIDC {endpoint_name} request failed",
            response=response,
            error=oauth_error.get("error"),
            error_description=oauth_error.get("error_description"),
        )

    def _parse_token_response(self, response: httpx.Response) -> OidcTokenResponse:
        payload = self._parse_json_object(response, "OIDC token response is invalid")
        try:
            return OidcTokenResponse.model_validate(payload)
        except ValidationError as exc:
            raise OidcClientError("OIDC token response is invalid") from exc

    def _parse_oauth_error(self, response: httpx.Response) -> dict[str, str]:
        payload = self._parse_json_object(response, default_message=None)
        if payload is None:
            return {}
        error = payload.get("error")
        error_description = payload.get("error_description")
        result: dict[str, str] = {}
        if isinstance(error, str) and error.strip():
            result["error"] = error.strip()
        if isinstance(error_description, str) and error_description.strip():
            result["error_description"] = error_description.strip()
        return result

    def _parse_json_object(
        self, response: httpx.Response, default_message: str | None
    ) -> dict[str, Any] | None:
        try:
            payload = response.json()
        except ValueError:
            if default_message is None:
                return None
            raise OidcClientError(
                default_message, status_code=response.status_code
            ) from None
        if not isinstance(payload, dict):
            if default_message is None:
                return None
            raise OidcClientError(default_message, status_code=response.status_code)
        return payload

    def _build_oauth_error(
        self,
        message: str,
        *,
        response: httpx.Response,
        error: str | None,
        error_description: str | None,
    ) -> OidcClientError:
        return OidcClientError(
            message,
            error=error,
            error_description=error_description,
            status_code=response.status_code,
        )

    def _required_positive_int(
        self, payload: dict[str, Any], *, field_name: str
    ) -> int:
        value = payload.get(field_name)
        if not isinstance(value, int) or value <= 0:
            raise OidcClientError(
                f"OIDC response field {field_name} must be a positive integer"
            )
        return value

    def _optional_positive_int(
        self, payload: dict[str, Any], *, field_name: str, default: int
    ) -> int:
        value = payload.get(field_name, default)
        if not isinstance(value, int) or value <= 0:
            raise OidcClientError(
                f"OIDC response field {field_name} must be a positive integer"
            )
        return value

    def _enforce_https(self, *, url: str, kind: str) -> None:
        if self._config.require_https and not url.startswith("https://"):
            raise OidcProviderUnavailableError(
                f"{kind} must use https when require_https=true"
            )
