from __future__ import annotations

import socket
import ssl
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import uvicorn

from acps_sdk.aip.aip_identity import extract_common_name
from acps_sdk.aip.aip_peer_cert import AipPeerCertH11Protocol

REPO_ROOT = Path(__file__).resolve().parents[3]
PARTNER_TLS_DIR = REPO_ROOT / "demo-partner" / "partners" / "online" / "beijing_food"
LEADER_TLS_DIR = REPO_ROOT / "demo-leader" / "leader" / "atr"

PARTNER_SERVER_CERT = PARTNER_TLS_DIR / "server.pem"
PARTNER_SERVER_KEY = PARTNER_TLS_DIR / "server.key"
PARTNER_CLIENT_CERT = PARTNER_TLS_DIR / "client.pem"
PARTNER_CLIENT_KEY = PARTNER_TLS_DIR / "client.key"
PARTNER_TRUST_BUNDLE = PARTNER_TLS_DIR / "trust-bundle.pem"
LEADER_CLIENT_CERT = LEADER_TLS_DIR / "client.pem"
LEADER_CLIENT_KEY = LEADER_TLS_DIR / "client.key"
LEADER_TRUST_BUNDLE = LEADER_TLS_DIR / "trust-bundle.pem"


def cert_aic(cert_path: Path) -> str:
    cert = ssl._ssl._test_decode_cert(str(cert_path))  # type: ignore[attr-defined]
    common_name = extract_common_name(cert)
    assert common_name is not None
    return common_name


PARTNER_AIC = cert_aic(PARTNER_SERVER_CERT)
LEADER_AIC = cert_aic(LEADER_CLIENT_CERT)


def build_client_ssl_context(
    *,
    cert_file: Path,
    key_file: Path,
    ca_file: Path,
) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_file))
    context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def build_server_ssl_context(
    *,
    cert_file: Path,
    key_file: Path,
    ca_file: Path,
    require_client_cert: bool,
) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if require_client_cert:
        context.load_verify_locations(cafile=str(ca_file))
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        context.verify_mode = ssl.CERT_NONE
    return context


def reserve_free_port() -> tuple[int, socket.socket]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(2048)
    return int(sock.getsockname()[1]), sock


@contextmanager
def run_tls_app(
    app,
    *,
    ssl_context: ssl.SSLContext,
) -> Iterator[str]:
    port, sock = reserve_free_port()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        http=AipPeerCertH11Protocol,
        ws="none",
        log_level="warning",
        proxy_headers=False,
        lifespan="on",
    )
    config.load()
    config.ssl = ssl_context
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()

    deadline = time.time() + 10.0
    while time.time() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=2)
        sock.close()
        raise RuntimeError("Timed out waiting for TLS test server to start")

    try:
        yield f"https://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
