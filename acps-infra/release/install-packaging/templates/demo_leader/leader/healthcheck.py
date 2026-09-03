from __future__ import annotations

import http.client
import os
import ssl
import sys

from assistant.config import settings
from assistant.core.orchestrator import _build_client_ssl_context

HEALTH_PATH = "/api/v1/health"


def _resolve_host() -> str:
    host = os.getenv("LEADER_API_HOST", settings.get("uvicorn", {}).get("host", "127.0.0.1"))
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _resolve_port() -> int:
    raw_port = os.getenv("LEADER_API_PORT")
    if raw_port:
        return int(raw_port)
    return int(settings.get("uvicorn", {}).get("port", 9031))


def _use_mtls_healthcheck() -> bool:
    app_cfg = settings.get("app", {})
    return bool(app_cfg.get("identity_binding_enabled", True) and app_cfg.get("callback_base_url"))


def _build_https_context() -> ssl.SSLContext:
    ctx = _build_client_ssl_context(settings)
    if ctx is None:
        raise RuntimeError("mTLS callback healthcheck requires [mtls] client cert configuration")
    ctx.check_hostname = False
    return ctx


def main() -> int:
    host = _resolve_host()
    port = _resolve_port()
    conn: http.client.HTTPConnection | None = None
    try:
        if _use_mtls_healthcheck():
            conn = http.client.HTTPSConnection(
                host,
                port,
                context=_build_https_context(),
                timeout=5,
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=5)

        conn.request("GET", HEALTH_PATH)
        resp = conn.getresponse()
        return 0 if 200 <= resp.status < 400 else 1
    except Exception:
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
