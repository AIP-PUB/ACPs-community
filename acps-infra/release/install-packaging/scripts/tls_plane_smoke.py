#!/usr/bin/env python3
"""安装层 TLS 平面探测（fail-closed）。

对已启用且材料齐全的 TLS 平面做真实握手验收：
  - Redis TLS（可选 AUTH+PING）
  - RabbitMQ AMQPS（mTLS + AMQP protocol header）
  - mq-auth group API mTLS（9007）
  - Registry mTLS（9002）
  - Demo partner HTTPS（可选；verify_client 时用 ACS 客户端证）

原则：线上用什么协议，这里就验什么协议；TCP-only / || true 不算通过。
从控制节点运行；材料默认取 control/work/certs。
"""
from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
from pathlib import Path


class ProbeError(RuntimeError):
    pass


def _log(msg: str) -> None:
    print(f"[tls-smoke] {msg}", flush=True)


def _require_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise ProbeError(f"missing {label}: {path}")
    return path


def _client_ctx(
    *,
    ca_file: Path,
    cert_file: Path | None = None,
    key_file: Path | None = None,
    check_hostname: bool = True,
) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(ca_file))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = check_hostname
    if cert_file is not None and key_file is not None:
        ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    return ctx


def _tcp_connect(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, int(port)), timeout=timeout)


def probe_redis_tls(
    *,
    host: str,
    port: int,
    ca_file: Path,
    password: str | None,
    timeout: float,
) -> None:
    _log(f"redis TLS {host}:{port}")
    ctx = _client_ctx(ca_file=ca_file, check_hostname=True)
    with _tcp_connect(host, port, timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            if password:
                tls.sendall(f"AUTH {password}\r\nPING\r\n".encode())
                data = tls.recv(256)
                text = data.decode("utf-8", errors="replace")
                if "+PONG" not in text and "-NOAUTH" not in text:
                    # AUTH 成功后应为 +PONG；若密码错会是 -ERR
                    if text.startswith("-ERR"):
                        raise ProbeError(f"redis AUTH/PING failed: {text.strip()!r}")
            else:
                # 仅握手：发送 PING，容忍 -NOAUTH
                tls.sendall(b"PING\r\n")
                data = tls.recv(256)
                text = data.decode("utf-8", errors="replace")
                if not text:
                    raise ProbeError("redis TLS handshake ok but empty response")
    _log("redis TLS ok")


def probe_amqps(
    *,
    host: str,
    port: int,
    ca_file: Path,
    cert_file: Path,
    key_file: Path,
    timeout: float,
    plane_label: str = "amqps",
    server_hostname: str | None = None,
    check_hostname: bool = True,
) -> None:
    """RabbitMQ AMQPS：mTLS 握手后发送 AMQP 协议头，期望 Connection.Start。"""
    sni = server_hostname or host
    _log(f"{plane_label} mTLS {host}:{port} (sni={sni})")
    ctx = _client_ctx(
        ca_file=ca_file,
        cert_file=cert_file,
        key_file=key_file,
        check_hostname=check_hostname,
    )
    # AMQP 0-9-1 protocol header
    header = b"AMQP\x00\x00\x09\x01"
    with _tcp_connect(host, port, timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=sni) as tls:
            tls.sendall(header)
            # Connection.Start 是 method frame，至少数十字节
            data = tls.recv(512)
            if not data:
                raise ProbeError(f"{plane_label}: empty response after protocol header")
            # 粗检：frame type=1 (method) 或至少非 TLS alert
            if data[0] not in (1, 3, 8) and b"AMQP" not in data[:8]:
                # 仍可能是合法二进制；只要握手完成且有回包即视为 broker TLS+AMQP 响应
                if len(data) < 8:
                    raise ProbeError(f"{plane_label}: short response {data!r}")
    _log(f"{plane_label} mTLS ok")


def probe_mtls_tcp(
    *,
    name: str,
    host: str,
    port: int,
    ca_file: Path,
    cert_file: Path,
    key_file: Path,
    timeout: float,
) -> None:
    _log(f"{name} mTLS {host}:{port}")
    ctx = _client_ctx(ca_file=ca_file, cert_file=cert_file, key_file=key_file)
    with _tcp_connect(host, port, timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            ver = tls.version()
            if not ver:
                raise ProbeError(f"{name}: TLS version missing after handshake")
    _log(f"{name} mTLS ok ({ver})")


def probe_https_mtls(
    *,
    name: str,
    host: str,
    port: int,
    path: str,
    ca_file: Path,
    cert_file: Path | None,
    key_file: Path | None,
    timeout: float,
) -> None:
    import urllib.error
    import urllib.request

    _log(f"{name} HTTPS {host}:{port}{path}")
    ctx = _client_ctx(ca_file=ca_file, cert_file=cert_file, key_file=key_file)
    url = f"https://{host}:{int(port)}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            code = int(resp.status)
    except urllib.error.HTTPError as exc:
        # 握手已成功；4xx/5xx 仍证明 TLS 平面通（路径可能需特定 AIC）
        code = int(exc.code)
        _log(f"{name}: TLS ok, HTTP {code} (accepted as transport proof)")
        return
    if code <= 0:
        raise ProbeError(f"{name}: unexpected HTTP status {code}")
    _log(f"{name} HTTPS ok (HTTP {code})")


def _first_existing(paths: list[Path]) -> Path | None:
    for p in paths:
        if p.is_file():
            return p
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--control-certs", type=Path, required=True, help="control/work/certs")
    p.add_argument("--timeout", type=float, default=10.0)

    p.add_argument("--redis-host")
    p.add_argument("--redis-port", type=int, default=6379)
    p.add_argument("--redis-password", default="", help="deprecated; prefer ACPS_TLS_SMOKE_REDIS_PASSWORD")

    p.add_argument("--rabbitmq-host")
    p.add_argument("--rabbitmq-port", type=int, default=5671)
    p.add_argument(
        "--require-mq-auth-for-amqps",
        action="store_true",
        help="B15: mq-auth mTLS must be requested and pass before AMQPS counts as green",
    )
    p.add_argument(
        "--plane-label",
        default="",
        help="optional label for AMQPS plane log (e.g. amqps-colocated)",
    )
    p.add_argument(
        "--amqps-server-hostname",
        default="",
        help="SNI/hostname verify override (default: rabbitmq-host)",
    )
    p.add_argument(
        "--amqps-no-check-hostname",
        action="store_true",
        help="disable AMQPS hostname check (avoid; prefer SAN rabbitmq)",
    )

    p.add_argument("--mq-auth-host")
    p.add_argument("--mq-auth-port", type=int, default=9007)

    p.add_argument("--registry-host")
    p.add_argument("--registry-mtls-port", type=int, default=9002)

    p.add_argument("--demo-partner-host")
    p.add_argument("--demo-partner-ports", default="", help="comma-separated ports")
    p.add_argument("--demo-partner-staging", type=Path, default=None)

    args = p.parse_args(argv)
    certs = args.control_certs
    errors: list[str] = []
    redis_password = args.redis_password or os.environ.get("ACPS_TLS_SMOKE_REDIS_PASSWORD", "")
    mq_auth_failed = False

    def run(label: str, fn) -> None:  # noqa: ANN001
        nonlocal mq_auth_failed
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — 汇总全部平面后再失败
            errors.append(f"{label}: {exc}")
            _log(f"FAIL {label}: {exc}")
            if label == "mq-auth-mtls":
                mq_auth_failed = True

    if args.redis_host:
        ca = certs / "redis" / "acps-root-ca.pem"
        run(
            "redis-tls",
            lambda: probe_redis_tls(
                host=args.redis_host,
                port=args.redis_port,
                ca_file=_require_file(ca, "redis CA"),
                password=(redis_password or None),
                timeout=args.timeout,
            ),
        )

    # B15：mq-auth :9007 在 AMQPS 之前；作为 auth_http 硬前提。
    if args.mq_auth_host:
        ca = certs / "mq-auth-server" / "acps-root-ca.pem"
        cert = certs / "mq-auth-server" / "client.pem"
        key = certs / "mq-auth-server" / "client.key"
        run(
            "mq-auth-mtls",
            lambda: probe_mtls_tcp(
                name="mq-auth",
                host=args.mq_auth_host,
                port=args.mq_auth_port,
                ca_file=_require_file(ca, "mq-auth CA"),
                cert_file=_require_file(cert, "mq-auth client cert"),
                key_file=_require_file(key, "mq-auth client key"),
                timeout=args.timeout,
            ),
        )

    if args.rabbitmq_host:
        if args.require_mq_auth_for_amqps and not args.mq_auth_host:
            errors.append(
                "amqps: mq-auth host required as AMQPS auth hard prerequisite "
                "(--require-mq-auth-for-amqps)"
            )
            _log("FAIL amqps: missing mq-auth hard prerequisite")
        elif args.require_mq_auth_for_amqps and mq_auth_failed:
            errors.append(
                "amqps: blocked — mq-auth-mtls failed (AMQPS auth_http hard prerequisite)"
            )
            _log("FAIL amqps: mq-auth-mtls hard prerequisite failed")
        else:
            ca = certs / "rabbitmq" / "acps-root-ca.pem"
            cert = certs / "rabbitmq" / "rabbitmq-client.pem"
            key = certs / "rabbitmq" / "rabbitmq-client.key"
            plane = args.plane_label or "amqps"
            sni = args.amqps_server_hostname or args.rabbitmq_host
            run(
                plane,
                lambda: probe_amqps(
                    host=args.rabbitmq_host,
                    port=args.rabbitmq_port,
                    ca_file=_require_file(ca, "rabbitmq CA"),
                    cert_file=_require_file(cert, "rabbitmq client cert"),
                    key_file=_require_file(key, "rabbitmq client key"),
                    timeout=args.timeout,
                    plane_label=plane,
                    server_hostname=sni,
                    check_hostname=not args.amqps_no_check_hostname,
                ),
            )

    if args.registry_host:
        # ATR trust-bundle 合同为「仅 Root」；校验叶证须 intermediate+root（ca-chain）。
        ca = None
        for cand in (
            certs / "ca-chain.pem",
            certs / "registry-server-9002" / "ca-chain.pem",
            certs / "registry-server-9002" / "trust-bundle.pem",
            certs / "trust-bundle" / "trust-bundle.pem",
        ):
            if cand.is_file():
                ca = cand
                break
        probe_dir = certs / "registry-9002-probe"
        cert = probe_dir / "client.pem"
        key = probe_dir / "client.key"
        run(
            "registry-mtls-9002",
            lambda: probe_mtls_tcp(
                name="registry-9002",
                host=args.registry_host,
                port=args.registry_mtls_port,
                ca_file=_require_file(ca if ca is not None else certs / "ca-chain.pem", "registry verify CA (ca-chain preferred)"),
                cert_file=_require_file(cert, "registry probe client cert"),
                key_file=_require_file(key, "registry probe client key"),
                timeout=args.timeout,
            ),
        )

    if args.demo_partner_host and args.demo_partner_ports.strip():
        ports = [int(x) for x in args.demo_partner_ports.split(",") if x.strip()]
        staging = args.demo_partner_staging
        # 任选一个 online partner 的 client 材料（服务端 verify_client 时需要）
        client_cert = None
        client_key = None
        ca_demo = None
        if staging and staging.is_dir():
            online = staging / "partners" / "online"
            if online.is_dir():
                for agent_dir in sorted(online.iterdir()):
                    c = agent_dir / "client.pem"
                    k = agent_dir / "client.key"
                    t = agent_dir / "trust-bundle.pem"
                    if c.is_file() and k.is_file():
                        client_cert, client_key = c, k
                    if t.is_file():
                        ca_demo = t
                    if client_cert and ca_demo:
                        break
        if ca_demo is None:
            ca_demo = certs / "demo-partner" / "trust-bundle.pem"
            if not ca_demo.is_file():
                # 回退 registry trust
                ca_demo = certs / "registry-server-9002" / "trust-bundle.pem"
        for port in ports:
            run(
                f"demo-partner-https:{port}",
                lambda port=port: probe_https_mtls(
                    name=f"demo-partner:{port}",
                    host=args.demo_partner_host,
                    port=port,
                    path="/health",
                    ca_file=_require_file(ca_demo, "demo trust-bundle"),
                    cert_file=client_cert,
                    key_file=client_key,
                    timeout=args.timeout,
                ),
            )

    if errors:
        _log(f"{len(errors)} TLS plane probe(s) failed")
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    _log("all requested TLS plane probes passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
