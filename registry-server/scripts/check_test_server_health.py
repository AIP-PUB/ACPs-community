"""Check public /health and mTLS listener readiness for e2e temp server."""

from __future__ import annotations

import http.client
import socket
import sys
import urllib.parse


def _check_http_health(public_url: str) -> bool:
    parts = urllib.parse.urlsplit(public_url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported scheme for health check: {parts.scheme}")

    path = (parts.path.rstrip("/") or "") + "/health"
    if parts.query:
        path = f"{path}?{parts.query}"

    connection_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    hostname = parts.hostname or "127.0.0.1"
    connection = connection_cls(hostname, parts.port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        return response.status < 400
    finally:
        connection.close()


def main() -> int:
    public_url, mtls_url = sys.argv[1:3]
    try:
        if not _check_http_health(public_url):
            return 1

        mtls_parts = urllib.parse.urlsplit(mtls_url)
        with socket.create_connection(
            (mtls_parts.hostname or "127.0.0.1", mtls_parts.port),
            timeout=5,
        ):
            pass
        return 0
    except OSError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
