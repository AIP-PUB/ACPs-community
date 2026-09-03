"""Shared token/session store helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import click

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _normalize_distinct_strings(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def normalize_scope_value(value: str | list[str] | tuple[str, ...] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        scopes = _normalize_distinct_strings(value.replace(",", " ").split())
    else:
        scopes = _normalize_distinct_strings([str(item) for item in value])
    if not scopes:
        return None
    return " ".join(scopes)


def parse_scope_value(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    normalized = normalize_scope_value(value)
    if normalized is None:
        return ()
    return tuple(normalized.split())


def _normalize_token_type(value: object) -> str:
    token_type = BEARER_TOKEN_TYPE if value in (None, "") else str(value).strip()
    if token_type.lower() != "bearer":
        raise click.ClickException("Token type must be Bearer")
    return BEARER_TOKEN_TYPE


def _parse_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise click.ClickException(f"Invalid datetime value: {value}")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise click.ClickException(f"Invalid datetime value: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash_subject(value: str | None) -> str | None:
    if not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _normalize_string(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


BEARER_TOKEN_TYPE = "Bearer"  # noqa: S105  # nosec B105 - HTTP auth scheme label, not a secret


@dataclass(frozen=True)
class SessionPrincipalSnapshot:
    sub_hash: str | None = None
    preferred_username: str | None = None
    name: str | None = None
    email: str | None = None
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    tenant_id: str | None = None
    allowed_aics: tuple[str, ...] = ()

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any], *, scopes: tuple[str, ...]) -> SessionPrincipalSnapshot:
        roles: list[str] = []
        realm_access = claims.get("realm_access")
        if isinstance(realm_access, Mapping):
            realm_roles = realm_access.get("roles")
            if isinstance(realm_roles, list):
                roles.extend(str(item) for item in realm_roles)
        resource_access = claims.get("resource_access")
        if isinstance(resource_access, Mapping):
            for resource_claim in resource_access.values():
                if isinstance(resource_claim, Mapping):
                    resource_roles = resource_claim.get("roles")
                    if isinstance(resource_roles, list):
                        roles.extend(str(item) for item in resource_roles)

        allowed_aics_raw = claims.get("allowed_aics")
        allowed_aics: tuple[str, ...] = ()
        if isinstance(allowed_aics_raw, list):
            allowed_aics = _normalize_distinct_strings([str(item) for item in allowed_aics_raw])

        return cls(
            sub_hash=_hash_subject(_normalize_string(claims.get("sub"))),
            preferred_username=_normalize_string(claims.get("preferred_username") or claims.get("username")),
            name=_normalize_string(claims.get("name")),
            email=_normalize_string(claims.get("email")),
            roles=_normalize_distinct_strings(roles),
            scopes=scopes,
            tenant_id=_normalize_string(claims.get("tenant_id")),
            allowed_aics=allowed_aics,
        )

    @classmethod
    def from_mapping(cls, raw: object) -> SessionPrincipalSnapshot | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise click.ClickException("principal must be a JSON object")
        roles_raw = raw.get("roles")
        scopes_raw = raw.get("scopes")
        allowed_aics_raw = raw.get("allowed_aics")
        roles: tuple[str, ...] = ()
        scopes: tuple[str, ...] = ()
        allowed_aics: tuple[str, ...] = ()
        if isinstance(roles_raw, list):
            roles = _normalize_distinct_strings([str(item) for item in roles_raw])
        if isinstance(scopes_raw, list):
            scopes = _normalize_distinct_strings([str(item) for item in scopes_raw])
        if isinstance(allowed_aics_raw, list):
            allowed_aics = _normalize_distinct_strings([str(item) for item in allowed_aics_raw])
        return cls(
            sub_hash=_normalize_string(raw.get("sub_hash")),
            preferred_username=_normalize_string(raw.get("preferred_username")),
            name=_normalize_string(raw.get("name")),
            email=_normalize_string(raw.get("email")),
            roles=roles,
            scopes=scopes,
            tenant_id=_normalize_string(raw.get("tenant_id")),
            allowed_aics=allowed_aics,
        )

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in (
            ("sub_hash", self.sub_hash),
            ("preferred_username", self.preferred_username),
            ("name", self.name),
            ("email", self.email),
            ("tenant_id", self.tenant_id),
        ):
            if value is not None:
                payload[key] = value
        if self.roles:
            payload["roles"] = list(self.roles)
        if self.scopes:
            payload["scopes"] = list(self.scopes)
        if self.allowed_aics:
            payload["allowed_aics"] = list(self.allowed_aics)
        return payload


@dataclass(frozen=True)
class StoredSessionRecord:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    token_type: str = BEARER_TOKEN_TYPE
    schema_version: int | None = None
    service: str | None = None
    account_kind: str | None = None
    auth_mode: str | None = None
    issuer: str | None = None
    client_id: str | None = None
    scope: str | None = None
    expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    principal: SessionPrincipalSnapshot | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> StoredSessionRecord:
        access_token = raw.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise click.ClickException("Token file is missing access_token")
        refresh_token = raw.get("refresh_token")
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise click.ClickException("Token file refresh_token must be a string")
        schema_version_raw = raw.get("schema_version")
        schema_version = None
        if schema_version_raw is not None:
            if not isinstance(schema_version_raw, int):
                raise click.ClickException("schema_version must be an integer")
            schema_version = schema_version_raw

        return cls(
            schema_version=schema_version,
            service=_normalize_string(raw.get("service")),
            account_kind=_normalize_string(raw.get("account_kind")),
            auth_mode=_normalize_string(raw.get("auth_mode")),
            issuer=_normalize_string(raw.get("issuer")),
            client_id=_normalize_string(raw.get("client_id")),
            token_type=_normalize_token_type(raw.get("token_type")),
            access_token=access_token,
            refresh_token=refresh_token,
            scope=normalize_scope_value(raw.get("scope")),
            expires_at=_parse_datetime(raw.get("expires_at")),
            refresh_expires_at=_parse_datetime(raw.get("refresh_expires_at")),
            principal=SessionPrincipalSnapshot.from_mapping(raw.get("principal")),
            created_at=_parse_datetime(raw.get("created_at")),
            updated_at=_parse_datetime(raw.get("updated_at")),
        )

    @property
    def scopes(self) -> tuple[str, ...]:
        return parse_scope_value(self.scope)

    @property
    def has_refresh_token(self) -> bool:
        return bool(self.refresh_token)

    def is_access_token_expiring(self, *, now: datetime | None = None, leeway_seconds: int = 120) -> bool:
        if self.expires_at is None:
            return False
        current = now or _utc_now()
        return self.expires_at <= current + timedelta(seconds=leeway_seconds)

    def is_refresh_token_expired(self, *, now: datetime | None = None) -> bool:
        if self.refresh_expires_at is None:
            return False
        current = now or _utc_now()
        return self.refresh_expires_at <= current

    def with_updates(self, **changes: Any) -> StoredSessionRecord:
        return replace(self, **changes)

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "access_token": self.access_token,
            "token_type": self.token_type,
        }
        if self.schema_version is not None:
            payload["schema_version"] = self.schema_version
        for key, value in (
            ("service", self.service),
            ("account_kind", self.account_kind),
            ("auth_mode", self.auth_mode),
            ("issuer", self.issuer),
            ("client_id", self.client_id),
            ("scope", self.scope),
            ("refresh_token", self.refresh_token),
            ("expires_at", _format_datetime(self.expires_at)),
            ("refresh_expires_at", _format_datetime(self.refresh_expires_at)),
            ("created_at", _format_datetime(self.created_at)),
            ("updated_at", _format_datetime(self.updated_at)),
        ):
            if value is not None:
                payload[key] = value
        if self.principal is not None:
            payload["principal"] = self.principal.to_json_dict()
        return payload

    def summary(self) -> dict[str, Any]:
        principal_payload = self.principal.to_json_dict() if self.principal is not None else {}
        summary = {
            "service": self.service,
            "account_kind": self.account_kind,
            "auth_mode": self.auth_mode,
            "issuer": self.issuer,
            "client_id": self.client_id,
            "token_type": self.token_type,
            "scope": self.scope,
            "expires_at": _format_datetime(self.expires_at),
            "refresh_expires_at": _format_datetime(self.refresh_expires_at),
            "has_refresh_token": self.has_refresh_token,
            "principal": principal_payload or None,
        }
        return {key: value for key, value in summary.items() if value is not None}


class SessionStore:
    """Atomic JSON session store with restricted file permissions."""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._lock_path = file_path.with_name(f"{file_path.name}.lock")

    def load_raw(self) -> dict[str, Any] | None:
        if not self.file_path.exists():
            return None
        try:
            with self.file_path.open(encoding="utf-8") as file:
                payload = json.load(file)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def load(self) -> StoredSessionRecord | None:
        raw = self.load_raw()
        if raw is None:
            return None
        return StoredSessionRecord.from_mapping(raw)

    def save(self, record: StoredSessionRecord) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with self._exclusive_lock():
            fd, temp_name = tempfile.mkstemp(
                dir=str(self.file_path.parent),
                prefix=f".{self.file_path.name}.",
                suffix=".tmp",
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(record.to_json_dict(), file, ensure_ascii=True, indent=2)
                    file.write("\n")
                os.chmod(temp_path, 0o600)
                temp_path.replace(self.file_path)
                os.chmod(self.file_path, 0o600)
            finally:
                if temp_path.exists():
                    temp_path.unlink()

    def clear(self) -> None:
        with self._exclusive_lock():
            if self.file_path.exists():
                self.file_path.unlink()

    def _exclusive_lock(self) -> _FileLock:
        return _FileLock(self._lock_path)


class _FileLock:
    def __init__(self, lock_path: Path):
        self._lock_path = lock_path
        self._handle: Any | None = None

    def __enter__(self) -> _FileLock:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._lock_path.open("a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
