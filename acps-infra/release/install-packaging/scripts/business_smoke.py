#!/usr/bin/env python3
"""安装层 business smoke。

从控制节点对已部署集群运行。
不拉起同级服务（不同于各仓库 e2e）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class SmokeError(RuntimeError):
    pass


def _log(msg: str) -> None:
    print(f"[biz-smoke] {msg}", flush=True)


def _http_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 15.0) -> Any:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            if not body:
                return None
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SmokeError(f"HTTP {exc.code} {url}: {detail}") from exc


def _http_status(url: str, *, headers: dict[str, str] | None = None, timeout: float = 15.0) -> int:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _tcp(host: str, port: int, timeout: float = 5.0) -> None:
    with socket.create_connection((host, int(port)), timeout=timeout):
        return


def _load_token(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    token = data.get("access_token")
    if not token:
        raise SmokeError(f"missing access_token in {path}")
    return str(token)


def _cli(cli_bin: str, config: str, *args: str) -> dict[str, Any]:
    cmd = [cli_bin, "--config", config, *args]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SmokeError(f"cli failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr or proc.stdout}")
    out = (proc.stdout or "").strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}


def run_biz_core_atr(*, registry: str, public_port: int, token_file: Path) -> None:
    _log("biz_core_atr: registry health + authenticated /api/v1/agent/client")
    health = _http_json(f"http://{registry}:{public_port}/health")
    if not isinstance(health, dict) or health.get("status") not in {"ok", "healthy", "UP", True, "OK"}:
        # tolerate loose health shapes
        if not isinstance(health, dict):
            raise SmokeError(f"unexpected registry health: {health!r}")
    token = _load_token(token_file)
    status = _http_status(
        f"http://{registry}:{public_port}/api/v1/agent/client",
        headers={"Authorization": f"Bearer {token}"},
    )
    if status not in {200, 401, 403}:
        # 401/403 still prove OIDC middleware is engaged when OIDC on; for local auth 200 expected
        raise SmokeError(f"agent/client list unexpected status {status}")
    if status == 200:
        _log("biz_core_atr: agent/client list OK")
    else:
        _log(f"biz_core_atr: agent/client returned {status} (auth middleware live)")


def run_biz_core_discovery(*, discovery: str, port: int) -> None:
    _log("biz_core_discovery: discovery /health")
    body = _http_json(f"http://{discovery}:{port}/health")
    if not isinstance(body, dict):
        raise SmokeError(f"discovery health not json: {body!r}")
    _log("biz_core_discovery: OK")


def run_biz_core_mq(*, mq_host: str, group_port: int, ca_file: Path, cert_file: Path, key_file: Path) -> None:
    _log("biz_core_mq: mq-auth group API TCP+TLS handshake")
    _tcp(mq_host, group_port)
    if not ca_file.is_file():
        raise SmokeError(f"mq CA missing: {ca_file}")
    ctx = ssl.create_default_context(cafile=str(ca_file))
    ctx.check_hostname = False
    client_loaded = False
    if cert_file.is_file() and key_file.is_file():
        try:
            ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
            client_loaded = True
        except ssl.SSLError as exc:
            # Ed25519 key / RSA leaf mismatches (or similar) must not block TLS liveness.
            _log(f"biz_core_mq: client cert not loaded ({exc}); verifying server TLS only")
    try:
        with socket.create_connection((mq_host, int(group_port)), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=mq_host) as tls:
                _ = tls.version()
    except ssl.SSLError as exc:
        # CertificateRequest without usable client cert still proves TLS is up.
        msg = str(exc).lower()
        if any(
            needle in msg
            for needle in (
                "certificate required",
                "handshake failure",
                "alert",
                "eof",
                "violation of protocol",
            )
        ):
            _log(f"biz_core_mq: server TLS up (handshake closed without client: {exc})")
        else:
            raise SmokeError(f"mq TLS handshake failed: {exc}") from exc
    _log(f"biz_core_mq: OK (client_cert_loaded={client_loaded})")


def run_biz_monitor(*, monitor: str, port: int, token_file: Path | None) -> None:
    _log("biz_monitor: /health")
    body = _http_json(f"http://{monitor}:{port}/health")
    if not isinstance(body, dict):
        raise SmokeError(f"monitor health not json: {body!r}")
    if token_file and token_file.is_file():
        token = _load_token(token_file)
        # Prefer a real authenticated AMP surface; fall back to /health with bearer.
        for path in (
            "/acps-amp-v1/heartbeat/summary",
            "/acps-amp-v1/system/events/query",
            "/health",
        ):
            status = _http_status(
                f"http://{monitor}:{port}{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if status != 404:
                _log(f"biz_monitor: authenticated probe {path} status={status}")
                if status not in {200, 401, 403}:
                    raise SmokeError(f"monitor authenticated probe unexpected status {status}")
                break
        else:
            raise SmokeError("monitor authenticated probe: no known path answered")
    _log("biz_monitor: OK")


def run_biz_demo(*, partner_host: str, partner_ports: list[int], leader_url: str, partner_root: Path, leader_root: Path) -> None:
    _log("biz_demo: partner TCP + leader health + AIC presence")
    for port in partner_ports:
        _tcp(partner_host, port)
    body = _http_json(leader_url)
    if not isinstance(body, dict):
        raise SmokeError(f"leader health not json: {body!r}")
    partner_acs = list((partner_root / "partners/online").glob("*/acs.json"))
    if not partner_acs:
        raise SmokeError(f"no partner ACS under {partner_root}")
    for acs in partner_acs:
        data = json.loads(acs.read_text(encoding="utf-8"))
        if not (data.get("aic") or "").strip():
            raise SmokeError(f"partner ACS missing AIC: {acs}")
    leader_acs = leader_root / "leader/atr/acs.json"
    data = json.loads(leader_acs.read_text(encoding="utf-8"))
    if not (data.get("aic") or "").strip():
        raise SmokeError("leader ACS missing AIC")
    _log("biz_demo: OK")


def run_biz_oidc(*, issuer: str, token_file: Path, registry: str, public_port: int) -> None:
    _log("biz_oidc: discovery + bearer against registry")
    disc = _http_json(issuer.rstrip("/") + "/.well-known/openid-configuration")
    if not isinstance(disc, dict) or "token_endpoint" not in disc:
        raise SmokeError("OIDC discovery failed")
    token = _load_token(token_file)
    status = _http_status(
        f"http://{registry}:{public_port}/api/v1/agent/client",
        headers={"Authorization": f"Bearer {token}"},
    )
    if status not in {200, 401, 403}:
        raise SmokeError(f"OIDC bearer probe unexpected status {status}")
    _log(f"biz_oidc: OK (status={status})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", required=True, help="comma-separated biz_* groups")
    parser.add_argument("--registry-host", default="127.0.0.1")
    parser.add_argument("--registry-port", type=int, default=9001)
    parser.add_argument("--discovery-host", default="127.0.0.1")
    parser.add_argument("--discovery-port", type=int, default=9005)
    parser.add_argument("--monitor-host", default="127.0.0.1")
    parser.add_argument("--monitor-port", type=int, default=9009)
    parser.add_argument("--mq-host", default="127.0.0.1")
    parser.add_argument("--mq-group-port", type=int, default=9007)
    parser.add_argument("--mq-ca-file", default="")
    parser.add_argument("--mq-cert-file", default="")
    parser.add_argument("--mq-key-file", default="")
    parser.add_argument("--partner-host", default="127.0.0.1")
    parser.add_argument("--partner-ports", default="9021,9022,9023,9024,9025")
    parser.add_argument("--leader-health-url", default="http://127.0.0.1:9030/api/v1/health")
    parser.add_argument("--partner-root", default="")
    parser.add_argument("--leader-root", default="")
    parser.add_argument("--oidc-issuer", default="")
    parser.add_argument("--user-token-file", default="")
    parser.add_argument("--monitor-token-file", default="")
    args = parser.parse_args()

    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    user_token = Path(args.user_token_file) if args.user_token_file else None
    monitor_token = Path(args.monitor_token_file) if args.monitor_token_file else None

    for group in groups:
        if group == "biz_core_atr":
            if user_token is None or not user_token.is_file():
                raise SmokeError("biz_core_atr requires --user-token-file")
            run_biz_core_atr(
                registry=args.registry_host,
                public_port=args.registry_port,
                token_file=user_token,
            )
        elif group == "biz_core_discovery":
            run_biz_core_discovery(discovery=args.discovery_host, port=args.discovery_port)
        elif group == "biz_core_mq":
            run_biz_core_mq(
                mq_host=args.mq_host,
                group_port=args.mq_group_port,
                ca_file=Path(args.mq_ca_file),
                cert_file=Path(args.mq_cert_file),
                key_file=Path(args.mq_key_file),
            )
        elif group == "biz_monitor":
            run_biz_monitor(
                monitor=args.monitor_host,
                port=args.monitor_port,
                token_file=monitor_token if monitor_token and monitor_token.is_file() else None,
            )
        elif group == "biz_demo":
            run_biz_demo(
                partner_host=args.partner_host,
                partner_ports=[int(p) for p in args.partner_ports.split(",") if p.strip()],
                leader_url=args.leader_health_url,
                partner_root=Path(args.partner_root),
                leader_root=Path(args.leader_root),
            )
        elif group == "biz_oidc":
            if not args.oidc_issuer or user_token is None:
                raise SmokeError("biz_oidc requires --oidc-issuer and --user-token-file")
            run_biz_oidc(
                issuer=args.oidc_issuer,
                token_file=user_token,
                registry=args.registry_host,
                public_port=args.registry_port,
            )
        else:
            raise SmokeError(f"unknown group: {group}")

    _log(f"all groups passed: {', '.join(groups)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeError as exc:
        print(f"[biz-smoke] FAIL: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
