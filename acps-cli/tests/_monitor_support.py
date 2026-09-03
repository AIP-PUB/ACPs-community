"""monitor 联机测试辅助。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

CLI_REPO = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = CLI_REPO.parent
MONITOR_REPO = WORKSPACE_ROOT / "monitor-server"
MONITOR_DRIVER = CLI_REPO / "tests" / "_monitor_fixture_driver.py"
MONITOR_RUNTIME = MONITOR_REPO / ".venv" / "bin" / "python"
DEFAULT_MONITOR_TEST_DATABASE_URL = os.getenv(
    "ACPS_CLI_MANAGED_MONITOR_DATABASE_URL",
    "postgresql+asyncpg://monitor:monitor@localhost:5432/agent_monitor_test",
)
DEFAULT_MONITOR_TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/3")


def _merge_pythonpath(prefix: str, current: str | None) -> str:
    if not current:
        return prefix
    return f"{prefix}{os.pathsep}{current}"


def monitor_runtime_env() -> dict[str, str]:
    """构造 monitor-server 测试辅助脚本运行环境。"""

    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "development",
            "MONITOR_BASE_URL": os.getenv("MONITOR_URL", os.getenv("MONITOR_BASE_URL", "http://localhost:9009")),
            "DATABASE_URL": DEFAULT_MONITOR_TEST_DATABASE_URL,
            "TEST_DATABASE_URL": DEFAULT_MONITOR_TEST_DATABASE_URL,
            "REDIS_URL": DEFAULT_MONITOR_TEST_REDIS_URL,
            "CLICKHOUSE_DATABASE": "amp_test",
            "KAFKA_BOOTSTRAP_SERVERS": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092"),
            "CLICKHOUSE_HOST": os.getenv("CLICKHOUSE_HOST", "localhost"),
            "CLICKHOUSE_PORT": os.getenv("CLICKHOUSE_PORT", "8123"),
            "VM_QUERY_URL": os.getenv("VM_QUERY_URL", "http://localhost:8428"),
            "VM_REMOTE_WRITE_URL": os.getenv("VM_REMOTE_WRITE_URL", "http://localhost:8428"),
            "MINIO_ENDPOINT": os.getenv("MINIO_ENDPOINT", "http://localhost:19000"),
            "MINIO_ACCESS_KEY": os.getenv("MINIO_ACCESS_KEY", "admin"),
            "MINIO_SECRET_KEY": os.getenv("MINIO_SECRET_KEY", "devpass"),
            "OPENSEARCH_HOSTS": os.getenv("OPENSEARCH_HOSTS", "http://localhost:9200"),
            "OPENSEARCH_VERIFY_CERTS": "false",
            "PYTHONPATH": _merge_pythonpath(str(MONITOR_REPO), env.get("PYTHONPATH")),
        }
    )
    return env


def run_monitor_fixture_action(action: str) -> dict[str, Any]:
    """执行 monitor-server 测试辅助动作，并返回 JSON 结果。"""

    if not MONITOR_RUNTIME.is_file():
        raise RuntimeError(
            "monitor-server Python 运行时缺失，请先执行 "
            "`cd ../monitor-server && just test bootstrap` 或 `just prep sync`。"
        )
    if not MONITOR_DRIVER.is_file():
        raise RuntimeError(f"monitor 测试辅助脚本不存在：{MONITOR_DRIVER}")

    result = subprocess.run(  # noqa: S603
        [str(MONITOR_RUNTIME), str(MONITOR_DRIVER), action],
        cwd=MONITOR_REPO,
        env=monitor_runtime_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"monitor 测试辅助执行失败：action={action}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    payload: Any | None = None
    for line in reversed([item.strip() for item in result.stdout.splitlines() if item.strip()]):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if payload is None:
        raise RuntimeError(
            f"monitor 测试辅助未返回合法 JSON：action={action}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    if not isinstance(payload, dict):
        raise RuntimeError(f"monitor 测试辅助返回值必须为 JSON object，当前为：{type(payload).__name__}")
    return payload
