#!/usr/bin/env python3
"""运行部署态业务烟测。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import ssl
import subprocess  # nosec B404 - commands are assembled from fixed local deployment inputs
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error as url_error
from urllib import request as url_request
from urllib.parse import urlsplit
from urllib.request import urlopen

from acps_cli.shared.config import load_toml_config
from acps_cli.shared.runtime import RootCliRuntime
from acps_cli.shared.unified_config import build_registry_auth_config

TEXT_PLAIN = "text/plain"
VALID_MEMBER_AIC = "1.2.156.3088.1.1.89AB.123456.7LMNOP.1ABC"
VALID_VHOST = "acps"
PROBE_CERT_FILE_NAME = "client.pem"
PROBE_KEY_FILE_NAME = "client.key"
TRUST_BUNDLE_FILE_NAME = "trust-bundle.pem"
MQ_CA_BUNDLE_FILE_NAME = "acps-root-ca.pem"
SMOKE_SUMMARY_FILE_NAME = "smoke-test-summary.json"
DISCOVERY_QUERY_POLL_INTERVAL = float(os.getenv("DISCOVERY_QUERY_POLL_INTERVAL", "3"))
DISCOVERY_QUERY_POLL_TIMEOUT = int(os.getenv("DISCOVERY_QUERY_POLL_TIMEOUT", "90"))
DISCOVERY_SYNC_REQUEST_TIMEOUT = int(os.getenv("DISCOVERY_SYNC_REQUEST_TIMEOUT", "120"))
DISCOVERY_SYNC_WAIT_TIMEOUT = int(os.getenv("DISCOVERY_SYNC_WAIT_TIMEOUT", "180"))
DISCOVERY_SYNC_WAIT_INTERVAL = float(os.getenv("DISCOVERY_SYNC_WAIT_INTERVAL", "5"))


@dataclass(frozen=True)
class BootstrapArtifacts:
    registry_dir: Path
    mq_dir: Path
    registry_probe_cert: Path
    registry_probe_key: Path
    registry_trust_bundle: Path
    mq_probe_cert: Path
    mq_probe_key: Path
    mq_ca_bundle: Path


@dataclass(frozen=True)
class SmokeSession:
    user_username: str
    agent_id: str
    aic: str
    issued_cert: Path
    issued_key: Path
    cleanup_acs_paths: tuple[Path, ...]


class SmokeError(RuntimeError):
    """烟测失败。"""


def build_runtime(original_data: dict[str, Any], original_path: Path) -> RootCliRuntime:
    return RootCliRuntime(
        config_path=str(original_path),
        verbose=False,
        toml_data=original_data,
        resolved_config_path=original_path,
        config_dir=original_path.parent,
    )


def registry_auth_mode(original_data: dict[str, Any], original_path: Path) -> str:
    return build_registry_auth_config(build_runtime(original_data, original_path), admin=False).mode


def log(message: str) -> None:
    print(f"[smoketest] {message} - smoke_test_runtime.py:85")


def pass_step(name: str, detail: str = "") -> None:
    suffix = f" ({detail})" if detail else ""
    log(f"PASS: {name}{suffix}")


def resolve_cli_bin(cli_bin: str | None, runtime_dir: Path) -> str:
    candidates = [
        cli_bin,
        str(runtime_dir / ".venv/bin/acps-cli"),
        shutil.which("acps-cli"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise SmokeError("未找到 acps-cli 可执行文件；请先安装 wheel 运行包")


def ensure_non_empty(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise SmokeError(f"{label} 不能为空")
    return cleaned


def resolve_text_option(
    explicit: str | None,
    env_keys: tuple[str, ...],
    prompt: str,
    *,
    secret: bool = False,
) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    for env_key in env_keys:
        env_value = os.getenv(env_key, "").strip()
        if env_value:
            return env_value
    if not sys.stdin.isatty():
        raise SmokeError(f"缺少 {prompt}；请通过参数或环境变量提供")
    value = getpass.getpass(f"{prompt}: ") if secret else input(f"{prompt}: ")
    return ensure_non_empty(value, prompt)


def current_timestamp() -> str:
    beijing = timezone(timedelta(hours=8))
    return datetime.now(tz=beijing).replace(microsecond=0).isoformat()


def run_command(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(  # noqa: S603  # nosec B603 - command is passed as an argv list without a shell
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise SmokeError(f"命令失败: {' '.join(cmd)}\n{output}")
    return output


def run_cli_json(cli_bin: str, config_path: Path, *args: str) -> dict[str, Any]:
    output = run_command([cli_bin, "--config", str(config_path), *args])
    return load_json_output(output)


def load_json_output(output: str) -> dict[str, Any]:
    text = output.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data

    first_brace = text.find("{")
    if first_brace >= 0:
        try:
            data = json.loads(text[first_brace:])
        except json.JSONDecodeError as exc:
            raise SmokeError(f"无法解析 JSON 输出:\n{output}") from exc
        if isinstance(data, dict):
            return data
    raise SmokeError(f"无法解析 JSON 输出:\n{output}")


def quote_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def quote_int(value: int) -> str:
    return str(value)


def load_config(config_path: str | None) -> tuple[dict[str, Any], Path]:
    toml_data, resolved_path = load_toml_config(config_path)
    if resolved_path is None:
        raise SmokeError("未找到 acps-cli.toml；请通过 --config 指定配置文件")
    return toml_data, resolved_path


def get_section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name, {})
    if isinstance(section, dict):
        return section
    return {}


def get_string_setting(
    data: dict[str, Any],
    section: str,
    key: str,
    env_key: str,
    default: str,
) -> str:
    env_value = os.getenv(env_key, "").strip()
    if env_value:
        return env_value
    section_data = get_section(data, section)
    value = section_data.get(key)
    if value is None:
        return default
    return str(value).strip() or default


def get_int_setting(
    data: dict[str, Any],
    section: str,
    key: str,
    env_key: str,
    default: int,
) -> int:
    env_value = os.getenv(env_key, "").strip()
    if env_value:
        try:
            return int(env_value)
        except ValueError as exc:
            raise SmokeError(f"{env_key} 不是有效整数") from exc
    section_data = get_section(data, section)
    value = section_data.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SmokeError(f"[{section}].{key} 不是有效整数") from exc


def write_runtime_config(
    original_data: dict[str, Any],
    original_path: Path,
    work_root: Path,
    *,
    mq_ca_file: Path,
    registry_mtls_server_ca_file: Path,
) -> Path:
    runtime = build_runtime(original_data, original_path)
    registry_user_auth = build_registry_auth_config(runtime, admin=False)
    registry_admin_auth = build_registry_auth_config(runtime, admin=True)
    registry_base_url = get_string_setting(
        original_data,
        "registry",
        "base_url",
        "REGISTRY_URL",
        "http://localhost:9001",
    )
    registry_mtls_base_url = get_string_setting(
        original_data,
        "registry",
        "mtls_base_url",
        "REGISTRY_MTLS_BASE_URL",
        registry_base_url.replace(":9001", ":9002"),
    )
    registry_timeout = get_int_setting(original_data, "registry", "timeout_seconds", "REGISTRY_TIMEOUT_SECONDS", 15)
    ca_base_url = get_string_setting(original_data, "ca", "base_url", "CA_URL", "http://localhost:9003")
    discovery_base_url = get_string_setting(
        original_data,
        "discovery",
        "base_url",
        "DISCO_URL",
        "http://localhost:9005",
    )
    mq_group_url = get_string_setting(
        original_data,
        "mq",
        "group_api_url",
        "MQ_GROUP_API_URL",
        "https://localhost:9007",
    )
    mq_auth_url = get_string_setting(
        original_data,
        "mq",
        "auth_api_url",
        "MQ_AUTH_API_URL",
        "https://localhost:9008",
    )
    mq_timeout = get_int_setting(original_data, "mq", "timeout_seconds", "MQ_TIMEOUT_SECONDS", 10)

    token_dir = work_root / ".acps-cli" / "tokens"
    keyfiles_dir = work_root / "keyfiles"
    token_dir.mkdir(parents=True, exist_ok=True)
    for subdir in ("accounts", "private", "certs", "csr"):
        (keyfiles_dir / subdir).mkdir(parents=True, exist_ok=True)

    user_token_file = (
        registry_user_auth.token_file if registry_user_auth.mode == "oidc" else str(token_dir / "registry-user.json")
    )
    admin_token_file = (
        registry_admin_auth.token_file if registry_admin_auth.mode == "oidc" else str(token_dir / "registry-admin.json")
    )

    lines = [
        "[registry]",
        f"base_url = {quote_string(registry_base_url)}",
        f"mtls_base_url = {quote_string(registry_mtls_base_url)}",
        f"mtls_server_ca_file = {quote_string(str(registry_mtls_server_ca_file))}",
        f"timeout_seconds = {quote_int(registry_timeout)}",
        "",
        "[auth]",
        f"user_token_file = {quote_string(user_token_file)}",
        f"admin_token_file = {quote_string(admin_token_file)}",
        "",
    ]
    if registry_user_auth.mode == "oidc" and registry_user_auth.oidc is not None:
        lines.extend(
            [
                "[registry.auth]",
                'mode = "oidc"',
                f"issuer = {quote_string(registry_user_auth.oidc.issuer)}",
                f"client_id = {quote_string(registry_user_auth.oidc.client_id)}",
                "scopes = [" + ", ".join(quote_string(scope) for scope in registry_user_auth.oidc.scopes) + "]",
                f"require_https = {'true' if registry_user_auth.oidc.require_https else 'false'}",
                "",
            ]
        )
    lines.extend(
        [
            "[ca]",
            f"base_url = {quote_string(ca_base_url)}",
            f"account_keys_dir = {quote_string(str(keyfiles_dir / 'accounts'))}",
            f"private_keys_dir = {quote_string(str(keyfiles_dir / 'private'))}",
            f"certs_dir = {quote_string(str(keyfiles_dir / 'certs'))}",
            f"csr_dir = {quote_string(str(keyfiles_dir / 'csr'))}",
            f"trust_bundle_path = {quote_string(str(keyfiles_dir / TRUST_BUNDLE_FILE_NAME))}",
            "",
            "[discovery]",
            f"base_url = {quote_string(discovery_base_url)}",
            "",
            "[mq]",
            f"group_api_url = {quote_string(mq_group_url)}",
            f"auth_api_url = {quote_string(mq_auth_url)}",
            f"ca_cert_file = {quote_string(str(mq_ca_file))}",
            f"timeout_seconds = {quote_int(mq_timeout)}",
        ]
    )
    config_path = work_root / original_path.name
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


def make_acs_file(work_dir: Path) -> Path:
    agent_name = f"smoke-agent-{uuid.uuid4().hex[:6]}"
    timestamp = datetime.now(timezone(timedelta(hours=8))).isoformat()
    payload = {
        "aic": "",
        "active": False,
        "lastModifiedTime": timestamp,
        "protocolVersion": "02.02",
        "name": agent_name,
        "version": "1.0.0",
        "description": "Deployment smoke test agent",
        "provider": {
            "organization": "Smoke Test",
            "url": "https://smoke.example.org",
            "license": "INTERNAL",
        },
        "securitySchemes": {
            "mtls": {
                "type": "mutualTLS",
                "description": "Smoke test mutual TLS",
            }
        },
        "endPoints": [
            {
                "url": "https://localhost:9000/rpc",
                "transport": "JSONRPC",
                "security": [{"mtls": []}],
            }
        ],
        "capabilities": {"streaming": False, "notification": False, "messageQueue": []},
        "defaultInputModes": [TEXT_PLAIN],
        "defaultOutputModes": [TEXT_PLAIN],
        "skills": [
            {
                "id": f"{agent_name}.skill",
                "name": "Smoke Test Skill",
                "description": "Deployment smoke test skill",
                "version": "1.0.0",
                "tags": ["smoke"],
                "examples": ["deployment smoke"],
                "inputModes": [TEXT_PLAIN],
                "outputModes": [TEXT_PLAIN],
            }
        ],
    }
    path = work_dir / "smoke-agent-acs.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def make_discovery_acs_file(work_dir: Path) -> tuple[Path, str]:
    suffix = uuid.uuid4().hex[:6]
    query_marker = f"smoke-discovery-{suffix}"
    agent_name = f"smoke-discovery-agent-{suffix}"
    timestamp = datetime.now(timezone(timedelta(hours=8))).isoformat()
    payload = {
        "aic": "",
        "active": False,
        "lastModifiedTime": timestamp,
        "protocolVersion": "02.02",
        "name": agent_name,
        "version": "1.0.0",
        "description": f"Discovery smoke test agent {query_marker}",
        "provider": {
            "organization": "Smoke Test",
            "url": "https://smoke.example.org",
            "license": "INTERNAL",
        },
        "securitySchemes": {
            "mtls": {
                "type": "mutualTLS",
                "description": "Smoke test mutual TLS",
            }
        },
        "endPoints": [
            {
                "url": "https://localhost:9000/rpc",
                "transport": "JSONRPC",
                "security": [{"mtls": []}],
            }
        ],
        "capabilities": {
            "streaming": False,
            "notification": False,
            "messageQueue": [],
        },
        "defaultInputModes": [TEXT_PLAIN],
        "defaultOutputModes": [TEXT_PLAIN],
        "skills": [
            {
                "id": f"{agent_name}.skill",
                "name": "Smoke Discovery Skill",
                "description": f"discovery smoke skill {query_marker}",
                "version": "1.0.0",
                "tags": ["smoke", "discovery", query_marker],
                "examples": [query_marker],
                "inputModes": [TEXT_PLAIN],
                "outputModes": [TEXT_PLAIN],
            }
        ],
    }
    path = work_dir / "smoke-discovery-agent-acs.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, agent_name


def request_json(
    method: str,
    url: str,
    *,
    timeout: int = 30,
) -> tuple[int, dict[str, Any] | None, str]:
    if urlsplit(url).scheme.lower() not in {"http", "https"}:
        raise SmokeError(f"不支持的 URL scheme: {url}")

    request = url_request.Request(  # noqa: S310
        url,
        headers={"Accept": "application/json"},
        method=method,
    )
    try:
        with url_request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310 - URL scheme is validated above
            raw = response.read().decode("utf-8")
            payload = parse_response_json(raw)
            return response.status, payload, raw
    except url_error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        payload = parse_response_json_safe(raw)
        return exc.code, payload, raw
    except url_error.URLError as exc:
        raise SmokeError(f"请求失败: {method} {url}: {exc}") from exc


def parse_response_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise SmokeError(f"响应不是 JSON object: {raw}")
    return decoded


def parse_response_json_safe(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        return parse_response_json(raw)
    except json.JSONDecodeError:
        return None


def verify_certificate_subject_contains_aic(aic: str, cert_path: Path) -> None:
    openssl_bin = shutil.which("openssl")
    if openssl_bin is None:
        log("未找到 openssl，跳过证书内容校验")
        return

    output = run_command([openssl_bin, "x509", "-in", str(cert_path), "-noout", "-subject"])
    if aic not in output:
        raise SmokeError(f"证书 subject 未包含 AIC: {aic}; output={output}")


def run_discovery_sync(discovery_base_url: str) -> None:
    status, _payload, raw = request_json(
        "POST",
        f"{discovery_base_url}/admin/dsp/hard-reset",
        timeout=30,
    )
    if status != 200:
        raise SmokeError(f"Discovery hard-reset 失败: {status} {raw}")

    status, _payload, raw = request_json(
        "POST",
        f"{discovery_base_url}/admin/dsp/sync",
        timeout=DISCOVERY_SYNC_REQUEST_TIMEOUT,
    )
    if status not in {200, 504}:
        raise SmokeError(f"Discovery run-sync 失败: {status} {raw}")

    deadline = time.monotonic() + DISCOVERY_SYNC_WAIT_TIMEOUT
    last_raw = raw
    while time.monotonic() < deadline:
        status, payload, raw = request_json(
            "GET",
            f"{discovery_base_url}/admin/dsp/status",
            timeout=30,
        )
        if status != 200 or payload is None:
            raise SmokeError(f"Discovery status 查询失败: {status} {raw}")
        counts = payload.get("object_count_by_type")
        if not isinstance(counts, dict):
            raise SmokeError(f"Discovery status object_count_by_type 非法: {payload}")
        last_raw = raw
        if (
            payload.get("needs_snapshot") is False
            and payload.get("last_sync_time")
            and int(counts.get("acs") or 0) >= 1
        ):
            return
        time.sleep(DISCOVERY_SYNC_WAIT_INTERVAL)

    raise SmokeError(f"Discovery run-sync 轮询超时: timeout={DISCOVERY_SYNC_WAIT_TIMEOUT}s, last_status={last_raw}")


def query_discovery_until_hit(
    *,
    cli_bin: str,
    config_path: Path,
    query_text: str,
    expected_aic: str,
) -> None:
    deadline = time.monotonic() + DISCOVERY_QUERY_POLL_TIMEOUT
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        payload = run_cli_json(
            cli_bin,
            config_path,
            "discover",
            "query",
            query_text,
            "--limit",
            "5",
        )
        last_payload = payload
        result = payload.get("result")
        if isinstance(result, dict):
            acs_map = result.get("acsMap")
            if isinstance(acs_map, dict) and expected_aic in acs_map:
                return
        time.sleep(DISCOVERY_QUERY_POLL_INTERVAL)

    raise SmokeError(f"Discovery query 未命中目标 AIC: query={query_text}, aic={expected_aic}, payload={last_payload}")


def run_discovery_smoke_flow(
    *,
    cli_bin: str,
    config_path: Path,
    work_root: Path,
    discovery_base_url: str,
) -> tuple[Path, str]:
    discovery_acs_path, query_text = make_discovery_acs_file(work_root)
    discovery_agent_id = save_agent_from_acs(
        cli_bin=cli_bin,
        config_path=config_path,
        acs_path=discovery_acs_path,
        step_name="Discovery ACS 保存",
    )
    discovery_aic = submit_and_approve_agent(
        cli_bin=cli_bin,
        config_path=config_path,
        agent_id=discovery_agent_id,
        submit_step_name="Discovery ACS 提交审核",
        approve_step_name="Discovery ACS 审核通过",
    )
    run_cli_json(
        cli_bin,
        config_path,
        "agent",
        "sync",
        "--acs-file",
        str(discovery_acs_path),
        "--json",
    )
    pass_step("Discovery ACS 同步")
    run_discovery_sync(discovery_base_url)
    pass_step("Discovery DSP run-sync")
    query_discovery_until_hit(
        cli_bin=cli_bin,
        config_path=config_path,
        query_text=query_text,
        expected_aic=discovery_aic,
    )
    pass_step("Discovery query 命中", query_text)
    return discovery_acs_path, discovery_aic


def save_agent_from_acs(
    *,
    cli_bin: str,
    config_path: Path,
    acs_path: Path,
    step_name: str,
) -> str:
    save_output = run_cli_json(
        cli_bin,
        config_path,
        "agent",
        "save",
        "--acs-file",
        str(acs_path),
        "--json",
    )
    agent_id = str(save_output.get("agent_id") or "")
    if not agent_id:
        raise SmokeError(f"agent save 未返回 agent_id: {save_output}")
    pass_step(step_name, agent_id)
    return agent_id


def submit_and_approve_agent(
    *,
    cli_bin: str,
    config_path: Path,
    agent_id: str,
    submit_step_name: str,
    approve_step_name: str,
) -> str:
    submit_output = run_cli_json(
        cli_bin,
        config_path,
        "agent",
        "submit",
        "--agent-id",
        agent_id,
        "--json",
    )
    if str(submit_output.get("approval_status") or "").upper() != "PENDING":
        raise SmokeError(f"agent submit 未进入 PENDING: {submit_output}")
    pass_step(submit_step_name)

    approve_output = run_cli_json(
        cli_bin,
        config_path,
        "admin",
        "registry",
        "review",
        "approve",
        "--agent-id",
        agent_id,
        "--json",
    )
    aic = str(approve_output.get("aic") or "")
    if not aic:
        raise SmokeError(f"review approve 未返回 AIC: {approve_output}")
    pass_step(approve_step_name, aic)
    return aic


def https_health_check(url: str, *, cert_file: Path, key_file: Path, ca_file: Path) -> None:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    with urlopen(url, context=context, timeout=10) as response:  # noqa: S310  # nosec B310 - caller provides the mTLS health endpoint
        if response.status != 200:
            raise SmokeError(f"{url} 返回 HTTP {response.status}")


def resolve_admin_credentials(args: argparse.Namespace, *, auth_mode: str) -> tuple[str | None, str | None]:
    if auth_mode == "oidc":
        return None, None
    username = resolve_text_option(
        args.admin_username,
        ("SMOKE_ADMIN_USERNAME", "REGISTRY_ADMIN_USERNAME"),
        "Registry 管理员用户名",
    )
    password = resolve_text_option(
        args.admin_password,
        ("SMOKE_ADMIN_PASSWORD", "REGISTRY_ADMIN_PASSWORD"),
        "Registry 管理员密码",
        secret=True,
    )
    return username, password


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, help="acps-cli.toml 路径；默认按 CLI 规则搜索")
    parser.add_argument(
        "--bootstrap-dir",
        default=None,
        help="bootstrap.sh 产物目录；默认 <config_dir>/bootstrap-artifacts",
    )
    parser.add_argument("--cli-bin", default=None, help="显式指定 acps-cli 可执行文件")
    parser.add_argument("--admin-username", default=None, help="Registry 管理员用户名")
    parser.add_argument("--admin-password", default=None, help="Registry 管理员密码")
    parser.add_argument("--member-aic", default=VALID_MEMBER_AIC, help="MQ 群组流程中使用的成员 AIC")
    parser.add_argument("--group-id", default=None, help="显式指定 MQ smoke group_id")
    return parser


def resolve_bootstrap_artifacts(bootstrap_dir: Path, config_path: Path) -> BootstrapArtifacts:
    registry_dir = bootstrap_dir / "registry-server-9002"
    mq_dir = bootstrap_dir / "mq-auth-server"
    required_files = {
        "registry probe cert": registry_dir / PROBE_CERT_FILE_NAME,
        "registry probe key": registry_dir / PROBE_KEY_FILE_NAME,
        "registry trust bundle": registry_dir / TRUST_BUNDLE_FILE_NAME,
        "mq probe cert": mq_dir / PROBE_CERT_FILE_NAME,
        "mq probe key": mq_dir / PROBE_KEY_FILE_NAME,
        "mq CA bundle": mq_dir / MQ_CA_BUNDLE_FILE_NAME,
    }
    missing = [label for label, path in required_files.items() if not path.is_file()]
    if missing:
        raise SmokeError(
            "缺少 bootstrap 产物："
            + ", ".join(missing)
            + f"。请先执行 bash scripts/bootstrap.sh all --config {config_path}"
        )
    return BootstrapArtifacts(
        registry_dir=registry_dir,
        mq_dir=mq_dir,
        registry_probe_cert=registry_dir / PROBE_CERT_FILE_NAME,
        registry_probe_key=registry_dir / PROBE_KEY_FILE_NAME,
        registry_trust_bundle=registry_dir / TRUST_BUNDLE_FILE_NAME,
        mq_probe_cert=mq_dir / PROBE_CERT_FILE_NAME,
        mq_probe_key=mq_dir / PROBE_KEY_FILE_NAME,
        mq_ca_bundle=mq_dir / MQ_CA_BUNDLE_FILE_NAME,
    )


def prepare_smoke_runtime(
    original_data: dict[str, Any],
    original_config_path: Path,
    bootstrap_dir: Path,
    artifacts: BootstrapArtifacts,
) -> tuple[Path, Path]:
    work_root = bootstrap_dir / ".smoke-runtime"
    work_root.mkdir(parents=True, exist_ok=True)
    config_path = write_runtime_config(
        original_data,
        original_config_path,
        work_root,
        mq_ca_file=artifacts.mq_ca_bundle,
        registry_mtls_server_ca_file=artifacts.registry_trust_bundle,
    )
    return work_root, config_path


def generate_smoke_password() -> str:
    return f"Smk!{uuid.uuid4().hex}7Z"


def run_registry_ca_discovery_smoke(
    *,
    cli_bin: str,
    config_path: Path,
    work_root: Path,
    admin_username: str | None,
    admin_password: str | None,
    registry_auth_mode_value: str,
) -> SmokeSession:
    discovery_base_url = get_string_setting(
        load_toml_config(str(config_path))[0],
        "discovery",
        "base_url",
        "DISCO_URL",
        "http://localhost:9005",
    ).rstrip("/")

    run_command([cli_bin, "--config", str(config_path), "--help"])
    pass_step("CLI entrypoint")

    if registry_auth_mode_value == "oidc":
        user_status = load_json_output(run_command([cli_bin, "--config", str(config_path), "auth", "status", "--json"]))
        admin_status = load_json_output(
            run_command([cli_bin, "--config", str(config_path), "admin", "auth", "status", "--json"])
        )
        if not user_status.get("authenticated"):
            raise SmokeError("registry.auth.mode=oidc 时请先执行 acps-cli auth login")
        if not admin_status.get("authenticated"):
            raise SmokeError("registry.auth.mode=oidc 时请先执行 acps-cli admin auth login")
        user_whoami = load_json_output(run_command([cli_bin, "--config", str(config_path), "auth", "whoami", "--json"]))
        user_username = str(user_whoami.get("username") or user_whoami.get("preferred_username") or "oidc-user")
        pass_step("Registry 用户登录", user_username)
    else:
        user_username = f"smoke_{uuid.uuid4().hex[:8]}"
        user_password = generate_smoke_password()
        login_output = load_json_output(
            run_command(
                [
                    cli_bin,
                    "--config",
                    str(config_path),
                    "auth",
                    "login",
                    "--username",
                    user_username,
                    "--password",
                    user_password,
                    "--json",
                ]
            )
        )
        login_status = str(login_output.get("status") or "")
        if login_status not in {"registered", "logged-in"}:
            raise SmokeError(f"普通用户登录/注册返回异常: {login_output}")
        pass_step("Registry 用户登录", login_status)

    acs_path = make_acs_file(work_root)
    agent_id = save_agent_from_acs(
        cli_bin=cli_bin,
        config_path=config_path,
        acs_path=acs_path,
        step_name="Registry 保存 ACS",
    )

    if registry_auth_mode_value == "oidc":
        pass_step("Registry 管理员登录", str(admin_status.get("preferred_username") or "oidc-admin"))
    else:
        if not admin_username or not admin_password:
            raise SmokeError("local 模式缺少管理员用户名或密码")
        run_command(
            [
                cli_bin,
                "--config",
                str(config_path),
                "admin",
                "auth",
                "login",
                "--username",
                admin_username,
                "--password",
                admin_password,
                "--json",
            ]
        )
        pass_step("Registry 管理员登录", admin_username)

    aic = submit_and_approve_agent(
        cli_bin=cli_bin,
        config_path=config_path,
        agent_id=agent_id,
        submit_step_name="Registry 提交审核",
        approve_step_name="Registry 审核通过",
    )

    eab_path = work_root / "smoke-eab.json"
    run_command(
        [
            cli_bin,
            "--config",
            str(config_path),
            "cert",
            "eab",
            "fetch",
            "--aic",
            aic,
            "--output",
            str(eab_path),
            "--json",
        ]
    )
    if not eab_path.is_file():
        raise SmokeError("cert eab fetch 未生成 eab.json")
    pass_step("CA 获取 EAB")

    run_command(
        [
            cli_bin,
            "--config",
            str(config_path),
            "cert",
            "issue",
            "--aic",
            aic,
            "--eab-file",
            str(eab_path),
            "--usage",
            "clientAuth",
        ]
    )
    issued_cert = work_root / "keyfiles" / "certs" / f"{aic}.pem"
    issued_key = work_root / "keyfiles" / "private" / f"{aic}.key"
    if not issued_cert.is_file() or not issued_key.is_file():
        raise SmokeError(f"cert issue 未生成期望的证书文件: {issued_cert} / {issued_key}")
    pass_step("CA 申请 clientAuth 证书")

    verify_certificate_subject_contains_aic(aic, issued_cert)
    pass_step("CA 证书主题校验", aic)

    discovery_output = run_command([cli_bin, "--config", str(config_path), "discover", "status"])
    if "Status: 200" not in discovery_output:
        raise SmokeError(f"Discovery status 输出异常: {discovery_output}")
    pass_step("Discovery status")

    discovery_acs_path, _discovery_aic = run_discovery_smoke_flow(
        cli_bin=cli_bin,
        config_path=config_path,
        work_root=work_root,
        discovery_base_url=discovery_base_url,
    )

    return SmokeSession(
        user_username=user_username,
        agent_id=agent_id,
        aic=aic,
        issued_cert=issued_cert,
        issued_key=issued_key,
        cleanup_acs_paths=(acs_path, discovery_acs_path),
    )


def cleanup_smoke_agents(*, cli_bin: str, config_path: Path, session: SmokeSession) -> None:
    for acs_path in session.cleanup_acs_paths:
        run_cli_json(
            cli_bin,
            config_path,
            "agent",
            "delete",
            "--acs-file",
            str(acs_path),
            "--json",
        )
    pass_step("Registry 清理 smoke agents", str(len(session.cleanup_acs_paths)))


def run_registry_probe_smoke(original_data: dict[str, Any], artifacts: BootstrapArtifacts) -> None:
    registry_mtls_url = get_string_setting(
        original_data,
        "registry",
        "mtls_base_url",
        "REGISTRY_MTLS_BASE_URL",
        "https://localhost:9002",
    )
    registry_parts = urlsplit(registry_mtls_url)
    registry_health_url = f"https://{registry_parts.netloc}/health"
    https_health_check(
        registry_health_url,
        cert_file=artifacts.registry_probe_cert,
        key_file=artifacts.registry_probe_key,
        ca_file=artifacts.registry_trust_bundle,
    )
    pass_step("Registry 9002 mTLS /health")


def run_mq_smoke(
    *,
    cli_bin: str,
    config_path: Path,
    artifacts: BootstrapArtifacts,
    session: SmokeSession,
    member_aic: str,
    group_id: str,
) -> None:
    mq_health_output = load_json_output(
        run_command(
            [
                cli_bin,
                "--config",
                str(config_path),
                "admin",
                "mq",
                "health",
                "--cert-file",
                str(artifacts.mq_probe_cert),
                "--key-file",
                str(artifacts.mq_probe_key),
                "--json",
            ]
        )
    )
    if (
        mq_health_output.get("group_api", {}).get("status") != "ok"
        or mq_health_output.get("auth_api", {}).get("status") != "ok"
    ):
        raise SmokeError(f"MQ health 输出异常: {mq_health_output}")
    pass_step("MQ health")

    add_member_output = load_json_output(
        run_command(
            [
                cli_bin,
                "--config",
                str(config_path),
                "admin",
                "mq",
                "group",
                "add-member",
                "--leader-aic",
                session.aic,
                "--group-id",
                group_id,
                "--member-aic",
                member_aic,
                "--cert-file",
                str(session.issued_cert),
                "--key-file",
                str(session.issued_key),
                "--json",
            ]
        )
    )
    if add_member_output.get("status") != "ok":
        raise SmokeError(f"MQ add-member 失败: {add_member_output}")
    pass_step("MQ add-member")

    queue_name = f"group_{session.aic}_{group_id}_{member_aic}"
    allow_output = load_json_output(
        run_command(
            [
                cli_bin,
                "--config",
                str(config_path),
                "admin",
                "mq",
                "auth-probe",
                "resource",
                "--username",
                member_aic,
                "--vhost",
                VALID_VHOST,
                "--resource",
                "queue",
                "--name",
                queue_name,
                "--permission",
                "read",
                "--cert-file",
                str(session.issued_cert),
                "--key-file",
                str(session.issued_key),
                "--json",
            ]
        )
    )
    if allow_output.get("result") != "allow":
        raise SmokeError(f"MQ auth-probe allow 校验失败: {allow_output}")
    pass_step("MQ auth-probe allow")

    delete_output = load_json_output(
        run_command(
            [
                cli_bin,
                "--config",
                str(config_path),
                "admin",
                "mq",
                "group",
                "delete",
                "--leader-aic",
                session.aic,
                "--group-id",
                group_id,
                "--yes",
                "--cert-file",
                str(session.issued_cert),
                "--key-file",
                str(session.issued_key),
                "--json",
            ]
        )
    )
    if delete_output.get("status") != "ok":
        raise SmokeError(f"MQ delete group 失败: {delete_output}")
    pass_step("MQ delete group")

    deny_output = load_json_output(
        run_command(
            [
                cli_bin,
                "--config",
                str(config_path),
                "admin",
                "mq",
                "auth-probe",
                "resource",
                "--username",
                member_aic,
                "--vhost",
                VALID_VHOST,
                "--resource",
                "queue",
                "--name",
                queue_name,
                "--permission",
                "read",
                "--cert-file",
                str(session.issued_cert),
                "--key-file",
                str(session.issued_key),
                "--json",
            ]
        )
    )
    if deny_output.get("result") != "deny":
        raise SmokeError(f"MQ auth-probe deny 校验失败: {deny_output}")
    pass_step("MQ auth-probe deny")


def main() -> int:
    args = build_parser().parse_args()
    original_data, original_config_path = load_config(args.config)
    runtime_dir = original_config_path.parent
    bootstrap_dir = (
        Path(args.bootstrap_dir).expanduser().resolve() if args.bootstrap_dir else runtime_dir / "bootstrap-artifacts"
    )
    cli_bin = resolve_cli_bin(args.cli_bin, runtime_dir)
    registry_auth_mode_value = registry_auth_mode(original_data, original_config_path)
    admin_username, admin_password = resolve_admin_credentials(args, auth_mode=registry_auth_mode_value)
    artifacts = resolve_bootstrap_artifacts(bootstrap_dir, original_config_path)
    work_root, config_path = prepare_smoke_runtime(original_data, original_config_path, bootstrap_dir, artifacts)
    group_id = args.group_id or f"smoke-group-{uuid.uuid4().hex[:8]}"
    session: SmokeSession | None = None
    try:
        session = run_registry_ca_discovery_smoke(
            cli_bin=cli_bin,
            config_path=config_path,
            work_root=work_root,
            admin_username=admin_username,
            admin_password=admin_password,
            registry_auth_mode_value=registry_auth_mode_value,
        )
        run_registry_probe_smoke(original_data, artifacts)
        run_mq_smoke(
            cli_bin=cli_bin,
            config_path=config_path,
            artifacts=artifacts,
            session=session,
            member_aic=args.member_aic,
            group_id=group_id,
        )
        cleanup_smoke_agents(cli_bin=cli_bin, config_path=config_path, session=session)
    except Exception:
        if session is not None:
            try:
                cleanup_smoke_agents(cli_bin=cli_bin, config_path=config_path, session=session)
            except SmokeError as cleanup_exc:
                log(f"WARN: 清理 smoke agents 失败: {cleanup_exc}")
        raise

    summary = {
        "created_at": current_timestamp(),
        "config_file": str(original_config_path),
        "bootstrap_dir": str(bootstrap_dir),
        "smoke_user": session.user_username,
        "agent_id": session.agent_id,
        "aic": session.aic,
        "group_id": group_id,
    }
    summary_path = bootstrap_dir / SMOKE_SUMMARY_FILE_NAME
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"PASS: 业务烟测完成；汇总文件：{summary_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeError as exc:
        print(f"[smoketest] ERROR: {exc} - smoke_test_runtime.py:1209", file=sys.stderr)
        raise SystemExit(1) from exc
