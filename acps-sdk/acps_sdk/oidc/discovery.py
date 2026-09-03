"""带 TTL cache 的 OIDC discovery document 加载器。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from acps_sdk.oidc.errors import OidcProviderUnavailableError


class OidcDiscoveryDocument(BaseModel):
    """OIDC discovery document 中当前实现会用到的字段子集。"""

    model_config = ConfigDict(extra="allow")

    issuer: str
    jwks_uri: str = Field(alias="jwks_uri")
    authorization_endpoint: str | None = Field(default=None, alias="authorization_endpoint")
    device_authorization_endpoint: str | None = Field(default=None, alias="device_authorization_endpoint")
    token_endpoint: str | None = Field(default=None, alias="token_endpoint")
    revocation_endpoint: str | None = Field(default=None, alias="revocation_endpoint")
    end_session_endpoint: str | None = Field(default=None, alias="end_session_endpoint")


@dataclass(slots=True)
class _CacheEntry:
    value: OidcDiscoveryDocument
    expires_at: float


class OidcDiscoveryCache:
    """按 issuer 缓存 discovery document 的 TTL cache。"""

    def __init__(
        self,
        *,
        ttl_seconds: int,
        require_https: bool,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._require_https = require_https
        self._http_client = http_client
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, issuer: str) -> OidcDiscoveryDocument:
        issuer_key = issuer.rstrip("/")
        now = time.monotonic()
        entry = self._entries.get(issuer_key)
        if entry is not None and entry.expires_at >= now:
            return entry.value

        async with self._lock:
            entry = self._entries.get(issuer_key)
            if entry is not None and entry.expires_at >= time.monotonic():
                return entry.value

            document = await self._fetch(issuer_key)
            self._entries[issuer_key] = _CacheEntry(
                value=document,
                expires_at=time.monotonic() + self._ttl_seconds,
            )
            return document

    async def _fetch(self, issuer: str) -> OidcDiscoveryDocument:
        self._enforce_https(url=issuer, kind="issuer")
        well_known_url = f"{issuer}/.well-known/openid-configuration"
        try:
            response = await self._http_client.get(well_known_url)
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

        if document.issuer.rstrip("/") != issuer:
            raise OidcProviderUnavailableError(
                f"OIDC discovery issuer mismatch: expected {issuer}, got {document.issuer}"
            )
        self._enforce_https(url=document.jwks_uri, kind="jwks_uri")
        return document

    def _enforce_https(self, *, url: str, kind: str) -> None:
        if self._require_https and not url.startswith("https://"):
            raise OidcProviderUnavailableError(f"{kind} must use https when require_https=true")
