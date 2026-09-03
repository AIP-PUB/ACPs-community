"""安装层控制节点 OIDC discovery 重写（无需 sudo /etc/hosts）。

JWT iss 为 ``http://keycloak:8080/realms/...``（集群内 Keycloak 主机名）。
Password-grant 登录已使用 ``--public-base``；CLI token *刷新* 经 session issuer 重新发现，
控制主机无法解析 ``keycloak`` 或访问 8080 时会失败（发布主机端口通常为 9080）。

本模块作为 ``sitecustomize.py`` 安装到 control CLI venv 以自动加载。重写：
  fetch URL:  http://keycloak:8080 → <public-base>
  discovery 文档中的 token 等端点为同一 public base，
同时仍按 JWT iss 字符串做 issuer 校验。
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse


def _public_base() -> str:
    env = (os.environ.get("ACPS_OIDC_PUBLIC_BASE") or "").strip().rstrip("/")
    if env:
        return env
    for candidate in (
        os.environ.get("ACPS_OIDC_PUBLIC_BASE_FILE", ""),
        str(Path.home() / ".local/share/acps/control/work/oidc_public_base"),
    ):
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path.read_text(encoding="utf-8").strip().rstrip("/")
    return ""


def _rewrite_origin(url: str, public_base: str) -> str:
    if not url or not public_base:
        return url
    pub = urlparse(public_base)
    ep = urlparse(url)
    return urlunparse((pub.scheme, pub.netloc, ep.path, "", ep.query, ""))


def _install_patch() -> None:
    public_base = _public_base()
    if not public_base:
        return
    try:
        import httpx
        from pydantic import ValidationError

        from acps_sdk.oidc.client import OidcDeviceClient
        from acps_sdk.oidc.discovery import OidcDiscoveryDocument
        from acps_sdk.oidc.errors import OidcProviderUnavailableError
    except Exception:
        return

    def _fetch_discovery_document(self):  # type: ignore[no-untyped-def]
        issuer = self._config.issuer.rstrip("/")
        fetch_issuer = issuer
        if "://keycloak:8080" in issuer:
            fetch_issuer = issuer.replace("http://keycloak:8080", public_base).replace(
                "https://keycloak:8080", public_base
            )
        self._enforce_https(url=issuer, kind="issuer")
        well_known_url = f"{fetch_issuer}/.well-known/openid-configuration"
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
        for key in (
            "authorization_endpoint",
            "device_authorization_endpoint",
            "token_endpoint",
            "revocation_endpoint",
            "end_session_endpoint",
            "jwks_uri",
        ):
            if key in payload and isinstance(payload[key], str):
                payload[key] = _rewrite_origin(payload[key], public_base)
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
        if self._config.require_https:
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

    OidcDeviceClient._fetch_discovery_document = _fetch_discovery_document  # type: ignore[method-assign]


_install_patch()
