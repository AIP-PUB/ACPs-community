"""Authentication session helpers shared by registry and monitor domains."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from acps_sdk.oidc import (
    OidcClientError,
    OidcDeviceAuthorizationDeniedError,
    OidcDeviceAuthorizationExpiredError,
    OidcDeviceClient,
    OidcDeviceClientConfig,
    OidcDeviceTokenPollingStatus,
    OidcTokenResponse,
)

from acps_cli.shared.session_store import (
    SessionPrincipalSnapshot,
    SessionStore,
    StoredSessionRecord,
    normalize_scope_value,
)
from acps_cli.shared.unified_config import ServiceAuthConfig


class AuthSessionError(RuntimeError):
    """Base error for CLI auth/session handling."""


class LoginRequiredError(AuthSessionError):
    """Raised when no valid local session is available."""


@dataclass(frozen=True)
class DeviceAuthorizationPrompt:
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    interval: int
    expires_at: datetime


REFRESH_TOKEN_TYPE_HINT = "refresh_token"  # noqa: S105  # nosec B105 - OAuth token type hint, not a credential


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _decode_unverified_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise AuthSessionError("Access token is not a JWT")
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{payload}{padding}".encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AuthSessionError("Access token claims are not valid JSON") from exc
    if not isinstance(claims, dict):
        raise AuthSessionError("Access token claims are not a JSON object")
    return claims


def _claims_expiry(token: str) -> datetime | None:
    claims = _decode_unverified_claims(token)
    exp = claims.get("exp")
    if exp is None:
        return None
    if not isinstance(exp, (int, float)):
        raise AuthSessionError("Access token exp claim is invalid")
    return datetime.fromtimestamp(exp, tz=timezone.utc)


def _session_summary(record: StoredSessionRecord) -> dict[str, Any]:
    principal = record.principal
    summary = {
        "service": record.service,
        "account_kind": record.account_kind,
        "auth_mode": record.auth_mode,
        "issuer": record.issuer,
        "client_id": record.client_id,
        "preferred_username": principal.preferred_username if principal else None,
        "name": principal.name if principal else None,
        "email": principal.email if principal else None,
        "roles": list(principal.roles) if principal and principal.roles else None,
        "scopes": list(principal.scopes) if principal and principal.scopes else list(record.scopes) or None,
        "tenant_id": principal.tenant_id if principal else None,
        "allowed_aics": list(principal.allowed_aics) if principal and principal.allowed_aics else None,
        "expires_at": record.expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if record.expires_at is not None
        else None,
        "refresh_expires_at": record.refresh_expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if record.refresh_expires_at is not None
        else None,
        "has_refresh_token": record.has_refresh_token,
    }
    return {key: value for key, value in summary.items() if value is not None}


class OidcAuthSessionManager:
    """Service-scoped OIDC session manager."""

    def __init__(
        self,
        auth_config: ServiceAuthConfig,
        *,
        session_store: SessionStore | None = None,
        refresh_leeway_seconds: int = 120,
        http_timeout_seconds: float = 10.0,
    ) -> None:
        if auth_config.mode != "oidc" or auth_config.oidc is None:
            raise AuthSessionError("OIDC auth session requires mode=oidc")
        self._auth_config = auth_config
        self._oidc = auth_config.oidc
        self._store = session_store or SessionStore(Path(auth_config.token_file))
        self._refresh_leeway_seconds = refresh_leeway_seconds
        self._http_timeout_seconds = http_timeout_seconds

    @property
    def store(self) -> SessionStore:
        return self._store

    def start_login(self) -> DeviceAuthorizationPrompt:
        response = self._with_device_client(lambda client: client.start_device_authorization())
        return DeviceAuthorizationPrompt(
            device_code=response.device_code.get_secret_value(),
            user_code=response.user_code,
            verification_uri=response.verification_uri,
            verification_uri_complete=response.verification_uri_complete,
            interval=response.interval,
            expires_at=response.expires_at.astimezone(timezone.utc),
        )

    def finish_login(
        self,
        prompt: DeviceAuthorizationPrompt,
        *,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> StoredSessionRecord:
        interval = prompt.interval
        while True:
            now = _utc_now()
            if now >= prompt.expires_at:
                raise AuthSessionError(
                    "Device authorization expired before login completed. Run the login command again."
                )
            sleep_func(interval)
            try:
                result = self._poll_device_token(prompt.device_code, interval=interval)
            except OidcDeviceAuthorizationDeniedError as exc:
                raise AuthSessionError("Authorization was denied in the browser. Run the login command again.") from exc
            except OidcDeviceAuthorizationExpiredError as exc:
                raise AuthSessionError("Device authorization expired. Run the login command again.") from exc
            except OidcClientError as exc:
                raise AuthSessionError(f"Device authorization failed: {exc}") from exc

            if result.status == OidcDeviceTokenPollingStatus.AUTHORIZATION_PENDING:
                continue
            if result.status == OidcDeviceTokenPollingStatus.SLOW_DOWN:
                interval = result.interval
                continue
            if result.token_response is None:
                raise AuthSessionError("OIDC device login completed without a token response")
            return self._persist_token_response(result.token_response)

    def login(
        self,
        *,
        on_prompt: Callable[[DeviceAuthorizationPrompt], None],
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> StoredSessionRecord:
        prompt = self.start_login()
        on_prompt(prompt)
        return self.finish_login(prompt, sleep_func=sleep_func)

    def load_session(self, *, require_valid_metadata: bool = True) -> StoredSessionRecord | None:
        record = self._store.load()
        if record is None:
            return None
        if require_valid_metadata:
            self._ensure_record_matches_config(record)
        return record

    def require_session(self) -> StoredSessionRecord:
        record = self.load_session()
        if record is None:
            raise LoginRequiredError("No local session found. Run the login command first.")
        return record

    def status(self) -> dict[str, Any]:
        record = self.load_session(require_valid_metadata=False)
        if record is None:
            return {
                "service": self._auth_config.service,
                "account_kind": self._auth_config.account_kind,
                "auth_mode": self._auth_config.mode,
                "authenticated": False,
            }
        self._ensure_record_matches_config(record)
        summary = _session_summary(record)
        summary["authenticated"] = True
        return summary

    def whoami(self) -> dict[str, Any]:
        record = self.require_session()
        return _session_summary(record)

    def refresh(self) -> StoredSessionRecord:
        record = self.require_session()
        return self._refresh_record(record)

    def get_access_token(self, *, allow_refresh: bool = True) -> str:
        record = self.require_session()
        if allow_refresh and record.is_access_token_expiring(leeway_seconds=self._refresh_leeway_seconds):
            record = self._refresh_record(record)
        return record.access_token

    def handle_unauthorized(self) -> str:
        record = self.require_session()
        refreshed = self._refresh_record(record)
        return refreshed.access_token

    def logout(self) -> dict[str, Any]:
        record = self.load_session(require_valid_metadata=False)
        if record is None:
            return {
                "service": self._auth_config.service,
                "account_kind": self._auth_config.account_kind,
                "auth_mode": self._auth_config.mode,
                "local_session_cleared": False,
                "revocation_attempted": False,
                "revoked": False,
            }

        revocation_attempted = False
        revoked = False
        revocation_error: str | None = None
        revocation_skipped_reason: str | None = None
        if record.refresh_token:
            try:
                self._ensure_record_matches_config(record)
            except AuthSessionError:
                revocation_skipped_reason = (
                    "Stored session metadata does not match current OIDC configuration; skipped token revocation."
                )
            else:
                revocation_attempted = True
                try:
                    revoke_result = self._with_device_client(
                        lambda client: client.revoke_token(
                            record.refresh_token or "",
                            token_type_hint=REFRESH_TOKEN_TYPE_HINT,
                        )
                    )
                    revoked = revoke_result.revoked
                except OidcClientError as exc:
                    revocation_error = str(exc)

        self._store.clear()
        result = {
            "service": self._auth_config.service,
            "account_kind": self._auth_config.account_kind,
            "auth_mode": self._auth_config.mode,
            "local_session_cleared": True,
            "revocation_attempted": revocation_attempted,
            "revoked": revoked,
        }
        if revocation_error:
            result["revocation_error"] = revocation_error
        if revocation_skipped_reason:
            result["revocation_skipped_reason"] = revocation_skipped_reason
        return result

    def _persist_token_response(
        self,
        token_response: OidcTokenResponse,
        *,
        previous: StoredSessionRecord | None = None,
    ) -> StoredSessionRecord:
        now = _utc_now()
        access_token = token_response.access_token_value
        scope = normalize_scope_value(token_response.scope) or normalize_scope_value(self._oidc.scopes)
        expires_at = None
        if token_response.expires_in is not None:
            expires_at = now + timedelta(seconds=token_response.expires_in)
        else:
            expires_at = _claims_expiry(access_token)
        if expires_at is None:
            raise AuthSessionError("OIDC token response is missing expires_in and access_token exp")

        refresh_expires_at = None
        if token_response.refresh_expires_in is not None:
            refresh_expires_at = now + timedelta(seconds=token_response.refresh_expires_in)
        claims = _decode_unverified_claims(access_token)
        principal = SessionPrincipalSnapshot.from_claims(claims, scopes=tuple(scope.split()) if scope else ())
        created_at = previous.created_at if previous and previous.created_at is not None else now
        record = StoredSessionRecord(
            schema_version=2,
            service=self._auth_config.service,
            account_kind=self._auth_config.account_kind,
            auth_mode="oidc",
            issuer=self._oidc.issuer,
            client_id=self._oidc.client_id,
            token_type=token_response.token_type,
            access_token=access_token,
            refresh_token=token_response.refresh_token_value,
            scope=scope,
            expires_at=expires_at,
            refresh_expires_at=refresh_expires_at,
            principal=principal,
            created_at=created_at,
            updated_at=now,
        )
        self._store.save(record)
        return record

    def _refresh_record(self, record: StoredSessionRecord) -> StoredSessionRecord:
        if not record.refresh_token:
            raise AuthSessionError("Current session does not include a refresh token. Run the login command again.")
        if record.is_refresh_token_expired():
            raise AuthSessionError("Current session has expired. Run the login command again.")
        try:
            token_response = self._with_device_client(lambda client: client.refresh_token(record.refresh_token or ""))
        except OidcClientError as exc:
            raise AuthSessionError(f"Session refresh failed: {exc}. Run the login command again.") from exc
        return self._persist_token_response(token_response, previous=record)

    def _build_client_config(self) -> OidcDeviceClientConfig:
        return OidcDeviceClientConfig(
            issuer=self._oidc.issuer,
            client_id=self._oidc.client_id,
            scopes=self._oidc.scopes,
            require_https=self._oidc.require_https,
            http_timeout_seconds=self._http_timeout_seconds,
        )

    def _poll_device_token(self, device_code: str, *, interval: int) -> Any:
        def _poll(client: OidcDeviceClient) -> Any:
            return client.poll_device_token(device_code, interval=interval)

        return self._with_device_client(_poll)

    def _with_device_client(self, callback: Callable[[OidcDeviceClient], Any]) -> Any:
        with (
            httpx.Client(timeout=self._http_timeout_seconds) as http_client,
            OidcDeviceClient(
                self._build_client_config(),
                http_client=http_client,
            ) as client,
        ):
            return callback(client)

    def _ensure_record_matches_config(self, record: StoredSessionRecord) -> None:
        if record.auth_mode != "oidc":
            raise AuthSessionError(
                "Stored session auth_mode does not match current OIDC configuration. Run login again."
            )
        if record.service not in (None, self._auth_config.service):
            raise AuthSessionError("Stored session service does not match current command domain. Run login again.")
        if record.account_kind not in (None, self._auth_config.account_kind):
            raise AuthSessionError(
                "Stored session account kind does not match current command domain. Run login again."
            )
        if record.issuer != self._oidc.issuer or record.client_id != self._oidc.client_id:
            raise AuthSessionError(
                "Stored session issuer/client_id does not match current configuration. Run login again."
            )
