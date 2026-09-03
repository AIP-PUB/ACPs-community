#!/usr/bin/env python3
"""安装层无人值守 OIDC password-grant 登录 bootstrap。

交互式 device flow 仍是运维路径（`acps-cli auth login`）。
本辅助脚本用于 air-gap / CI 安装：对专用 public install client 使用 Keycloak Direct Access Grant，
写入 SessionStore 兼容 token 文件，使 `bootstrap_runtime.py` 无需本地 /auth/login 即可获得已认证 OIDC 会话。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _b64url_json(segment: str) -> dict[str, Any]:
    pad = "=" * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(segment + pad)
    return json.loads(raw.decode("utf-8"))


def _claims(access_token: str) -> dict[str, Any]:
    parts = access_token.split(".")
    if len(parts) < 2:
        raise SystemExit("access_token is not a JWT")
    return _b64url_json(parts[1])


def _post_form(url: str, data: dict[str, str], *, timeout: float = 30.0) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"token endpoint HTTP {exc.code}: {detail}") from exc


def _discover_token_endpoint(issuer: str, *, public_base: str = "") -> str:
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    with urllib.request.urlopen(url, timeout=30) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    endpoint = doc.get("token_endpoint")
    if not endpoint:
        raise SystemExit(f"OIDC discovery missing token_endpoint: {url}")
    endpoint = str(endpoint)
    if public_base:
        # Rewrite docker-DNS issuer endpoints to a host-reachable base (e.g. http://127.0.0.1:9080).
        from urllib.parse import urlparse, urlunparse

        pub = urlparse(public_base.rstrip("/"))
        ep = urlparse(endpoint)
        endpoint = urlunparse((pub.scheme, pub.netloc, ep.path, "", ep.query, ""))
    return endpoint


def _principal_from_claims(claims: dict[str, Any], scopes: tuple[str, ...]) -> dict[str, Any]:
    roles: set[str] = set()
    realm_roles = (claims.get("realm_access") or {}).get("roles") or []
    roles.update(realm_roles)
    for client_roles in (claims.get("resource_access") or {}).values():
        roles.update(client_roles.get("roles") or [])
    return {
        "subject": claims.get("sub"),
        "username": claims.get("preferred_username") or claims.get("email") or claims.get("sub"),
        "email": claims.get("email"),
        "roles": sorted(roles),
        "scopes": list(scopes),
    }


def write_session(
    *,
    path: Path,
    service: str,
    account_kind: str,
    issuer: str,
    client_id: str,
    token: dict[str, Any],
) -> None:
    access_token = token["access_token"]
    claims = _claims(access_token)
    now = _utc_now()
    scope = token.get("scope") or "openid profile email"
    scopes = tuple(s for s in str(scope).split() if s)
    expires_in = token.get("expires_in")
    if expires_in is None:
        exp = claims.get("exp")
        if not isinstance(exp, int):
            raise SystemExit("token missing expires_in and exp")
        expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
    else:
        expires_at = now + timedelta(seconds=int(expires_in))
    refresh_expires_at = None
    if token.get("refresh_expires_in") is not None:
        refresh_expires_at = now + timedelta(seconds=int(token["refresh_expires_in"]))

    payload: dict[str, Any] = {
        "schema_version": 2,
        "service": service,
        "account_kind": account_kind,
        "auth_mode": "oidc",
        "issuer": issuer.rstrip("/"),
        "client_id": client_id,
        "token_type": token.get("token_type") or "Bearer",
        "access_token": access_token,
        "scope": " ".join(scopes),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "principal": _principal_from_claims(claims, scopes),
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "updated_at": now.isoformat().replace("+00:00", "Z"),
    }
    if token.get("refresh_token"):
        payload["refresh_token"] = token["refresh_token"]
    if refresh_expires_at is not None:
        payload["refresh_expires_at"] = refresh_expires_at.isoformat().replace("+00:00", "Z")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issuer", required=True, help="Issuer URL used for OIDC discovery / token fetch")
    parser.add_argument(
        "--session-issuer",
        default="",
        help="Issuer stored in session file (defaults to JWT iss, else --issuer). "
        "Use docker DNS name when tokens are fetched via published host port.",
    )
    parser.add_argument(
        "--public-base",
        default="",
        help="Optional host-reachable base (scheme://host:port) used to rewrite discovered token_endpoint",
    )
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--service", default="registry")
    parser.add_argument("--account-kind", default="user", choices=("user", "admin"))
    parser.add_argument("--scope", default="openid profile email")
    parser.add_argument("--retries", type=int, default=15)
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()

    issuer = args.issuer.rstrip("/")
    token_endpoint = None
    last_err: Exception | None = None
    for attempt in range(1, args.retries + 1):
        try:
            token_endpoint = _discover_token_endpoint(issuer, public_base=args.public_base)
            break
        except Exception as exc:  # noqa: BLE001 — retry discovery while Keycloak warms
            last_err = exc
            time.sleep(args.delay)
    if token_endpoint is None:
        raise SystemExit(f"OIDC discovery failed after {args.retries} attempts: {last_err}")

    token = _post_form(
        token_endpoint,
        {
            "grant_type": "password",
            "client_id": args.client_id,
            "username": args.username,
            "password": args.password,
            "scope": args.scope,
        },
    )
    claims = _claims(token["access_token"])
    session_issuer = (args.session_issuer or claims.get("iss") or issuer).rstrip("/")
    write_session(
        path=Path(args.token_file).expanduser(),
        service=args.service,
        account_kind=args.account_kind,
        issuer=session_issuer,
        client_id=args.client_id,
        token=token,
    )
    print(f"wrote {args.token_file} ({args.service}/{args.account_kind}) iss={session_issuer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
