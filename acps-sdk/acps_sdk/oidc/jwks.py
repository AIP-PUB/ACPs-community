"""JWKS 加载与 key 选择辅助工具。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from acps_sdk.oidc.errors import InvalidAccessTokenError, OidcProviderUnavailableError


@dataclass(slots=True)
class _CacheEntry:
    value: dict[str, Any]
    expires_at: float


class JwksCache:
    """按 JWKS URL 缓存文档的 TTL cache。"""

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

    async def get_jwk(self, *, jwks_uri: str, kid: str) -> dict[str, Any]:
        document = await self._get_document(jwks_uri=jwks_uri, force_refresh=False)
        jwk = self._find_jwk(document=document, kid=kid)
        if jwk is not None:
            return jwk

        refreshed_document = await self._get_document(jwks_uri=jwks_uri, force_refresh=True)
        jwk = self._find_jwk(document=refreshed_document, kid=kid)
        if jwk is not None:
            return jwk

        raise InvalidAccessTokenError(f"unknown JWK kid: {kid}")

    async def _get_document(self, *, jwks_uri: str, force_refresh: bool) -> dict[str, Any]:
        self._enforce_https(jwks_uri)
        now = time.monotonic()
        entry = self._entries.get(jwks_uri)
        if not force_refresh and entry is not None and entry.expires_at >= now:
            return entry.value

        async with self._lock:
            entry = self._entries.get(jwks_uri)
            if not force_refresh and entry is not None and entry.expires_at >= time.monotonic():
                return entry.value

            document = await self._fetch(jwks_uri)
            self._entries[jwks_uri] = _CacheEntry(
                value=document,
                expires_at=time.monotonic() + self._ttl_seconds,
            )
            return document

    async def _fetch(self, jwks_uri: str) -> dict[str, Any]:
        try:
            response = await self._http_client.get(jwks_uri)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OidcProviderUnavailableError(f"failed to load JWKS from {jwks_uri}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise OidcProviderUnavailableError(f"JWKS at {jwks_uri} is not valid JSON") from exc

        if not isinstance(data, dict):
            raise OidcProviderUnavailableError("JWKS response must be a JSON object")
        keys = data.get("keys")
        if not isinstance(keys, list):
            raise OidcProviderUnavailableError("JWKS response must contain a keys array")
        return data

    @staticmethod
    def _find_jwk(*, document: dict[str, Any], kid: str) -> dict[str, Any] | None:
        keys = document.get("keys", [])
        for key in keys:
            if isinstance(key, dict) and key.get("kid") == kid:
                return key
        return None

    def _enforce_https(self, jwks_uri: str) -> None:
        if self._require_https and not jwks_uri.startswith("https://"):
            raise OidcProviderUnavailableError("jwks_uri must use https when require_https=true")
