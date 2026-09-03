"""最简静态文件服务器 (web_app 静态资源 + 可选 /api 同源反代)

使用标准库 http.server, 避免对 FastAPI / Uvicorn 等依赖；可直接使用全局 python 运行。

用法:
    python web_app/webserver.py            # 默认 127.0.0.1:9030
    python web_app/webserver.py --port 4000 # 指定端口
    python web_app/webserver.py --api-upstream http://127.0.0.1:9031

说明:
    - 根路径 http://127.0.0.1:9030/ 自动返回 index.html (SimpleHTTPRequestHandler 默认行为)
    - 当提供 --api-upstream 时，/api/* 请求反代到 Leader API（同源 Web 入口，替代 nginx）
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import os
import socket
import socketserver
import sys
import urllib.error
import urllib.request
from pathlib import Path

from leader.runtime_paths import resolve_web_app_root

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9030


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "ACPsDemoLeaderWeb/1.0"

    def __init__(self, *args, api_upstream: str = "", **kwargs):
        self._api_upstream = (api_upstream or "").rstrip("/")
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args):
        sys.stderr.write("[static] " + (format % args) + "\n")

    def end_headers(self):
        """Tell the browser not to cache responses so edits show immediately."""
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        if self._api_upstream and self.path.startswith("/api/"):
            self._proxy_api()
            return
        super().do_GET()

    def do_POST(self):
        if self._api_upstream and self.path.startswith("/api/"):
            self._proxy_api()
            return
        self.send_error(405, "Method Not Allowed")

    def do_PUT(self):
        if self._api_upstream and self.path.startswith("/api/"):
            self._proxy_api()
            return
        self.send_error(405, "Method Not Allowed")

    def do_PATCH(self):
        if self._api_upstream and self.path.startswith("/api/"):
            self._proxy_api()
            return
        self.send_error(405, "Method Not Allowed")

    def do_DELETE(self):
        if self._api_upstream and self.path.startswith("/api/"):
            self._proxy_api()
            return
        self.send_error(405, "Method Not Allowed")

    def do_OPTIONS(self):
        if self._api_upstream and self.path.startswith("/api/"):
            self._proxy_api()
            return
        self.send_error(405, "Method Not Allowed")

    def _proxy_api(self):
        upstream_url = f"{self._api_upstream}{self.path}"
        if self.path.startswith("/api/"):
            body = None
            if self.command in {"POST", "PUT", "PATCH", "DELETE"}:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else None
            headers = {}
            for key, value in self.headers.items():
                lk = key.lower()
                if lk in {"host", "connection", "content-length"}:
                    continue
                headers[key] = value
            req = urllib.request.Request(  # noqa: S310
                upstream_url,
                data=body,
                headers=headers,
                method=self.command,
            )
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310  # nosec B310
                    payload = resp.read()
                    self.send_response(resp.status)
                    for key, value in resp.headers.items():
                        lk = key.lower()
                        if lk in {"transfer-encoding", "connection"}:
                            continue
                        self.send_header(key, value)
                    self.end_headers()
                    if payload:
                        self.wfile.write(payload)
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                self.send_response(exc.code)
                for key, value in exc.headers.items():
                    lk = key.lower()
                    if lk in {"transfer-encoding", "connection"}:
                        continue
                    self.send_header(key, value)
                self.end_headers()
                if payload:
                    self.wfile.write(payload)
            except Exception as exc:  # noqa: BLE001 — surface proxy failures to browser
                self.send_error(502, f"Bad Gateway: {exc}")
            return
        self.send_error(404, "Not Found")


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def find_free_port(host: str, port: int) -> int:
    if port != 0:
        return port
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description="Simple static file server (web_app) with optional /api proxy")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (default 127.0.0.1)")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Bind port (default 9030; 0 = auto)",
    )
    parser.add_argument(
        "--root",
        default="",
        help="Static file root directory (default runtime web_app/)",
    )
    parser.add_argument(
        "--api-upstream",
        default=os.environ.get("LEADER_API_UPSTREAM", ""),
        help="Leader API base URL for /api/* reverse proxy (e.g. http://127.0.0.1:9031)",
    )
    args = parser.parse_args()

    base_dir = Path(args.root).expanduser().resolve() if args.root else resolve_web_app_root()
    if not base_dir.is_dir():
        parser.error(f"静态资源目录不存在: {base_dir}")

    os.chdir(base_dir)
    port = find_free_port(args.host, args.port)
    api_upstream = (args.api_upstream or "").strip()

    def handler(*handler_args, **handler_kwargs):
        return QuietHandler(*handler_args, api_upstream=api_upstream, **handler_kwargs)

    with ReusableTCPServer((args.host, port), handler) as httpd:
        print(f"[static-server] Serving {base_dir} at http://{args.host}:{port}/ (Ctrl+C to quit)")
        if api_upstream:
            print(f"[static-server] /api/* -> {api_upstream}")
        if (base_dir / "index.html").exists():
            print("[static-server] Found index.html -> will serve as root page")
        else:
            print("[static-server] WARNING: index.html not found in web_app directory")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[static-server] Stopped.")


if __name__ == "__main__":
    main()
