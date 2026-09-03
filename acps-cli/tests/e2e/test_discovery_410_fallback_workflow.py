"""端到端测试：410 fallback 专用 retention fixture。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from acps_cli.main import main as cli_main
from tests.e2e.test_discovery_incremental_sync_workflow import (
    _assert_last_seq_advanced,
    _create_approved_agent,
    _login_admin,
    _login_user,
    _prepare_agent_for_filtered_query,
    _run_incremental_sync,
    _run_snapshot_sync_until_agent_available,
    _wait_for_filtered_query_state,
)

pytestmark = pytest.mark.e2e

disco_main = cli_main
admin_main = cli_main

_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
_REGISTRY_REPO = _WORKSPACE_ROOT / "registry-server"
_DISCOVERY_REPO = _WORKSPACE_ROOT / "discovery-server"
_REGISTRY_FIXTURE_PORT = int(os.getenv("DISCOVERY_410_REGISTRY_PORT", "9012"))
_DISCOVERY_FIXTURE_PORT = int(os.getenv("DISCOVERY_410_DISCOVERY_PORT", "9016"))
_REGISTRY_FIXTURE_URL = f"http://localhost:{_REGISTRY_FIXTURE_PORT}"
_DISCOVERY_FIXTURE_URL = f"http://localhost:{_DISCOVERY_FIXTURE_PORT}"
_REGISTRY_LOG_FILE = _REGISTRY_REPO / "logs/discovery-410-registry.log"
_DISCOVERY_LOG_FILE = _DISCOVERY_REPO / "logs/discovery-410-discovery.log"
_TOKEN_DIR_NAME = ".acps-cli"


@dataclass(frozen=True)
class RetentionFallbackHarness:
    """410 fallback 场景所需的独立 registry/discovery 实例配置。"""

    registry_env: dict[str, str]
    discovery_env: dict[str, str]
    registry_database_url: str
    discovery_database_url: str


def _load_env_file(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _tail_text(path: Path, max_lines: int = 80) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def _wait_for_http(url: str, *, expected_status: int = 200, timeout_seconds: int = 120) -> None:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=5)
            if response.status_code == expected_status:
                return
            last_error = f"status={response.status_code}, body={response.text}"
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            last_error = str(exc)
        time.sleep(1)

    pytest.fail(f"服务未在预期时间内就绪: url={url}, last_error={last_error}")


def _resolve_harness() -> RetentionFallbackHarness:
    registry_env = _load_env_file(_REGISTRY_REPO / ".env")
    discovery_env = _load_env_file(_DISCOVERY_REPO / ".env")

    registry_database_url = os.getenv("DISCOVERY_410_REGISTRY_DATABASE_URL") or registry_env.get("TEST_DATABASE_URL")
    if not registry_database_url:
        pytest.skip(
            "缺少 DISCOVERY_410_REGISTRY_DATABASE_URL，且 registry-server/.env 未提供 TEST_DATABASE_URL，"
            "无法启动短 retention registry fixture"
        )

    discovery_database_url = os.getenv("DISCOVERY_410_DISCOVERY_DATABASE_URL") or discovery_env.get("TEST_DATABASE_URL")
    if not discovery_database_url:
        pytest.skip(
            "缺少 DISCOVERY_410_DISCOVERY_DATABASE_URL，且 discovery-server/.env 未提供 TEST_DATABASE_URL，"
            "无法启动 410 fallback discovery fixture"
        )

    return RetentionFallbackHarness(
        registry_env=registry_env,
        discovery_env=discovery_env,
        registry_database_url=registry_database_url,
        discovery_database_url=discovery_database_url,
    )


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@contextmanager
def _run_registry_retention_fixture(
    harness: RetentionFallbackHarness,
) -> Iterator[None]:
    env = os.environ.copy()
    env.update(harness.registry_env)
    env.update(
        {
            "APP_ENV": "development",
            "DATABASE_URL": harness.registry_database_url,
            "CA_SERVER_MOCK": "false",
            "CA_SERVER_BASE_URL": os.getenv("CA_URL", "http://localhost:9003"),
            "REGISTRY_SERVER_DSP_RETENTION_WINDOW_HOURS": "1",
            "REGISTRY_SERVER_DSP_RETENTION_MAX_RECORDS": "1",
        }
    )
    _REGISTRY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_file = _REGISTRY_LOG_FILE.open("w", encoding="utf-8")
    process = subprocess.Popen(  # noqa: S603
        [
            str(_REGISTRY_REPO / ".venv/bin/python"),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(_REGISTRY_FIXTURE_PORT),
        ],
        cwd=_REGISTRY_REPO,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_http(f"{_REGISTRY_FIXTURE_URL}/health")
        yield
    finally:
        _stop_process(process)
        log_file.close()


@contextmanager
def _run_discovery_retention_fixture(
    harness: RetentionFallbackHarness,
) -> Iterator[None]:
    env = os.environ.copy()
    env.update(harness.discovery_env)
    env.update(
        {
            "APP_ENV": "development",
            "UVICORN_PORT": str(_DISCOVERY_FIXTURE_PORT),
            "DATABASE_URL": harness.discovery_database_url,
            "TEST_DATABASE_URL": harness.discovery_database_url,
            "DSP_BASE_URL": f"{_REGISTRY_FIXTURE_URL}/acps-dsp-v2",
            "DSP_WEBHOOK_RECEIVE_URL": f"{_DISCOVERY_FIXTURE_URL}/admin/dsp/webhooks/receive",
            "DSP_AUTO_START": "false",
            "POLLING_SERVER_URL": "",
            "FORWARDER_SERVER_ENABLED": "false",
            "FORWARDER_SERVER_URL": "",
        }
    )
    _DISCOVERY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_file = _DISCOVERY_LOG_FILE.open("w", encoding="utf-8")
    process = subprocess.Popen(  # noqa: S603
        [
            str(_DISCOVERY_REPO / ".venv/bin/python"),
            "-m",
            "app.main",
        ],
        cwd=_DISCOVERY_REPO,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_http(f"{_DISCOVERY_FIXTURE_URL}/health")
        yield
    finally:
        _stop_process(process)
        log_file.close()


def _write_fixture_cli_config(work_dir: Path) -> Path:
    conf = work_dir / "acps-cli.toml"
    conf.write_text(
        "[registry]\n"
        f'base_url = "{_REGISTRY_FIXTURE_URL}"\n'
        "\n"
        "[auth]\n"
        f'user_token_file = "{work_dir / _TOKEN_DIR_NAME / "tokens" / "registry-user.json"}"\n'
        f'admin_token_file = "{work_dir / _TOKEN_DIR_NAME / "tokens" / "registry-admin.json"}"\n'
        "\n"
        "[discovery]\n"
        f'base_url = "{_DISCOVERY_FIXTURE_URL}"\n',
        encoding="utf-8",
    )
    return conf


def _run_dsp_hard_reset(runner: CliRunner, disco_conf: Path) -> dict[str, object]:
    result = runner.invoke(
        disco_main,
        ["--config", str(disco_conf), "admin", "discovery", "dsp", "hard-reset"],
    )
    assert result.exit_code == 0, f"dsp hard-reset 失败: {result.output}"
    payload = json.loads(result.output)
    assert isinstance(payload, dict)
    return payload


def _load_access_token(token_file: Path) -> str:
    payload = json.loads(token_file.read_text(encoding="utf-8"))
    token = payload.get("access_token")
    assert isinstance(token, str) and token, f"token 文件缺少 access_token: {token_file}"
    return token


def _cleanup_registry_changelogs(access_token: str) -> dict[str, object]:
    response = httpx.post(
        f"{_REGISTRY_FIXTURE_URL}/acps-dsp-v2/admin/changelogs/cleanup",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    assert response.status_code == 200, (
        f"cleanup changelogs 失败: status={response.status_code}, body={response.text}, "
        f"registry_log_tail={_tail_text(_REGISTRY_LOG_FILE)}"
    )
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def test_incremental_sync_recovers_from_410_with_dedicated_retention_fixture(
    work_dir: Path,
    user_credentials: tuple[str, str],
    admin_credentials: tuple[str, str],
) -> None:
    harness = _resolve_harness()

    with (
        _run_registry_retention_fixture(harness),
        _run_discovery_retention_fixture(harness),
    ):
        runner = CliRunner()
        conf = _write_fixture_cli_config(work_dir)

        username, password = user_credentials
        admin_username, admin_password = admin_credentials
        _login_user(runner, conf, username, password)
        try:
            _login_admin(runner, conf, admin_username, admin_password)
        except AssertionError as exc:
            if "User not found (status=401)" in str(exc):
                pytest.skip(
                    "registry-server TEST_DATABASE_URL 缺少 bootstrap admin 用户，"
                    "请先对该测试库执行 just test bootstrap"
                )
            raise

        _run_dsp_hard_reset(runner, conf)

        _acs_path, agent_id, aic = _create_approved_agent(
            runner,
            work_dir=work_dir,
            reg_conf=conf,
            admin_conf=conf,
        )

        before_status = _run_snapshot_sync_until_agent_available(
            runner,
            conf,
            harness.discovery_database_url,
            aic,
        )
        _prepare_agent_for_filtered_query(
            harness.discovery_database_url,
            aic,
            f"410 fallback seeded skill for {aic}",
        )
        _wait_for_filtered_query_state(
            runner,
            disco_conf=conf,
            aic=aic,
            expected_visible=True,
        )

        for command, reason in (
            ("disable", "discovery 410 retention fallback pre-gap"),
            ("enable", None),
            ("disable", "discovery 410 retention fallback"),
        ):
            args = [
                "--config",
                str(conf),
                "admin",
                "registry",
                "agent",
                command,
                "--agent-id",
                agent_id,
            ]
            if reason is not None:
                args.extend(["--reason", reason])
            args.append("--json")
            result = runner.invoke(admin_main, args)
            assert result.exit_code == 0, f"{command} 失败: {result.output}"

        admin_token = _load_access_token(work_dir / _TOKEN_DIR_NAME / "tokens" / "registry-admin.json")
        cleanup_payload = _cleanup_registry_changelogs(admin_token)
        assert cleanup_payload.get("retention_config") == {
            "window_hours": 1,
            "max_records": 1,
        }, cleanup_payload
        assert int(cleanup_payload.get("cleaned_count") or 0) >= 1, cleanup_payload

        after_status = _run_incremental_sync(runner, conf)
        _assert_last_seq_advanced(before_status, after_status)
        _wait_for_filtered_query_state(
            runner,
            disco_conf=conf,
            aic=aic,
            expected_visible=False,
        )
