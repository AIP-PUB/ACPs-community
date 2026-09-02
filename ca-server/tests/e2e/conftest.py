import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncGenerator, Generator, MutableMapping
from contextlib import suppress
from pathlib import Path

import pytest
from httpx import AsyncClient, Client, HTTPError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_SERVER_HOST = "127.0.0.1"
TEST_SERVER_STARTUP_TIMEOUT_SECONDS = 30.0


def _set_env_default_if_blank(env: MutableMapping[str, str], key: str, value: str) -> None:
    """为空字符串或缺失时写入测试默认值。"""

    if not env.get(key, "").strip():
        env[key] = value


def _find_free_port() -> int:
    """查找一个可用的本地 TCP 端口。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((TEST_SERVER_HOST, 0))
        return int(sock.getsockname()[1])


def _build_startup_error(message: str, log_path: Path) -> RuntimeError:
    """拼接临时测试实例启动失败时的错误信息。"""

    log_output = log_path.read_text(encoding="utf-8", errors="replace").strip()
    if log_output:
        return RuntimeError(f"{message}\n\n临时实例日志：\n{log_output}")
    return RuntimeError(message)


def _wait_for_test_server(base_url: str, process: subprocess.Popen[str], log_path: Path) -> None:
    """等待临时测试实例完成启动并通过健康检查。"""

    deadline = time.monotonic() + TEST_SERVER_STARTUP_TIMEOUT_SECONDS
    health_url = f"{base_url}/health"

    with Client(timeout=1.0) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise _build_startup_error("E2E 临时测试实例启动失败。", log_path)

            try:
                response = client.get(health_url)
                response.raise_for_status()
                return
            except HTTPError:
                time.sleep(1)
            except OSError:
                time.sleep(1)

    if process.poll() is not None:
        raise _build_startup_error("E2E 临时测试实例在健康检查前退出。", log_path)

    raise _build_startup_error(f"等待 E2E 临时测试实例就绪超时：{base_url}", log_path)


def _managed_e2e_base_url() -> Generator[str]:
    """在未显式注入 TEST_E2E_BASE_URL 时，自管理一个临时测试实例。"""

    port = _find_free_port()
    base_url = f"http://{TEST_SERVER_HOST}:{port}"
    file_descriptor, log_path_str = tempfile.mkstemp(prefix="ca-server-e2e-", suffix=".log")
    os.close(file_descriptor)
    log_path = Path(log_path_str)

    env = os.environ.copy()
    env["APP_ENV"] = "testing"
    env["DATABASE_URL"] = env["TEST_DATABASE_URL"]
    env["TEST_DATABASE_URL"] = env["TEST_DATABASE_URL"]
    env["REGISTRY_SERVER_MOCK"] = "true"
    _set_env_default_if_blank(env, "CA_SERVER_ADMIN_API_TOKEN", "local-ca-admin-token")
    _set_env_default_if_blank(env, "CA_SERVER_INTERNAL_API_TOKEN", "test-ca-internal-token")

    with log_path.open("w+", encoding="utf-8") as log_file:
        process = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", TEST_SERVER_HOST, "--port", str(port)],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

        try:
            _wait_for_test_server(base_url, process, log_path)
            yield base_url
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

    with suppress(FileNotFoundError):
        log_path.unlink()


@pytest.fixture(scope="session")
def e2e_base_url() -> Generator[str]:
    """返回 E2E 目标服务地址；缺省时自动拉起临时测试实例。"""

    base_url = os.getenv("TEST_E2E_BASE_URL", "").strip()
    if base_url:
        yield base_url.rstrip("/")
        return

    yield from _managed_e2e_base_url()


@pytest.fixture
async def client(e2e_base_url: str) -> AsyncGenerator[AsyncClient]:
    """黑盒 E2E HTTP 客户端。"""

    async with AsyncClient(
        base_url=e2e_base_url,
        headers={
            "Authorization": f"Bearer {os.getenv('CA_SERVER_ADMIN_API_TOKEN', '').strip() or 'local-ca-admin-token'}"
        },
    ) as e2e_client:
        yield e2e_client
