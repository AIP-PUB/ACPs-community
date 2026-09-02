#!/usr/bin/env python3
"""通过 acps-cli 为部署环境自举 mTLS 证书。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from acps_cli.shared.config import load_toml_config
from acps_cli.shared.runtime import RootCliRuntime
from acps_cli.shared.unified_config import build_registry_auth_config

SCRIPT_DIR = Path(__file__).resolve().parent
STATIC_ACS_DIR = SCRIPT_DIR / "acs"
REGISTRY_SERVICE_ACS_FILE_NAME = "registry-server-9002-service-acs.json"
REGISTRY_PROBE_ACS_FILE_NAME = "registry-server-9002-probe-acs.json"
MQ_SERVICE_ACS_FILE_NAME = "mq-auth-server-acs.json"
MQ_PROBE_ACS_FILE_NAME = "healthcheck-client-acs.json"
RABBITMQ_ACS_FILE_NAME = "rabbitmq-acs.json"
REDIS_ACS_FILE_NAME = "redis-acs.json"
CHINA_HOTEL_ACS_FILE_NAME = "china_hotel.json"
CHINA_TRANSPORT_ACS_FILE_NAME = "china_transport.json"
SERVER_CERT_FILE_NAME = "server.pem"
SERVER_KEY_FILE_NAME = "server.key"
TRUST_BUNDLE_FILE_NAME = "trust-bundle.pem"
PROBE_CERT_FILE_NAME = "client.pem"
PROBE_KEY_FILE_NAME = "client.key"
CA_BUNDLE_FILE_NAME = "acps-root-ca.pem"
RABBITMQ_SERVER_CERT_FILE_NAME = "rabbitmq-server.pem"
RABBITMQ_SERVER_KEY_FILE_NAME = "rabbitmq-server.key"
RABBITMQ_CLIENT_CERT_FILE_NAME = "rabbitmq-client.pem"
RABBITMQ_CLIENT_KEY_FILE_NAME = "rabbitmq-client.key"
REDIS_SERVER_CERT_FILE_NAME = "redis-server.pem"
REDIS_SERVER_KEY_FILE_NAME = "redis-server.key"
SUMMARY_FILE_NAME = "summary.json"
WORK_DIR_NAME = ".work"


class BootstrapError(RuntimeError):
    """证书自举失败。"""


@dataclass(frozen=True)
class RegistrationSpec:
    name: str
    acs_path: Path
    cleanup_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class CertificateSpec:
    name: str
    acs_path: Path
    usage: str
    cert_path: Path
    key_path: Path


@dataclass(frozen=True)
class BootstrapCredentials:
    user_username: str
    user_password: str
    admin_username: str
    admin_password: str


@dataclass(frozen=True)
class DemoPartnerAgentSpec:
    directory: Path
    acs_path: Path
    name: str


@dataclass(frozen=True)
class DemoLeaderRuntimeSpec:
    install_dir: Path
    leader_dir: Path
    scenario_dir: Path
    atr_dir: Path
    acs_path: Path
    name: str


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


def resolve_optional_install_dir(path_value: str | None, label: str) -> Path | None:
    if not path_value:
        return None

    install_dir = Path(path_value).expanduser().resolve()
    if not install_dir.is_dir():
        raise BootstrapError(
            f"{label} 不存在或不可访问: {install_dir}；仅在服务安装目录位于本机可访问文件系统时才能使用该参数"
        )
    return install_dir


def deploy_profile_materials(
    profile_name: str,
    profile_dir: Path,
    install_dir: Path,
    file_names: tuple[str, ...],
) -> list[str]:
    certs_dir = install_dir / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)

    deployed_paths: list[str] = []
    for file_name in file_names:
        source_path = profile_dir / file_name
        destination_path = certs_dir / file_name
        shutil.copy2(source_path, destination_path)
        deployed_paths.append(str(destination_path))

    log(f"{profile_name} 证书已写入安装目录: {certs_dir}")
    return deployed_paths


def log(message: str) -> None:
    print(f"[bootstrap] {message}")


def ensure_non_empty(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise BootstrapError(f"{label} 不能为空")
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
        raise BootstrapError(f"缺少 {prompt}；请通过参数或环境变量提供")

    value = getpass.getpass(f"{prompt}: ") if secret else input(f"{prompt}: ")
    return ensure_non_empty(value, prompt)


def resolve_credentials(args: argparse.Namespace) -> BootstrapCredentials:
    return BootstrapCredentials(
        user_username=resolve_text_option(
            args.user_username,
            ("BOOTSTRAP_REGISTRY_USERNAME", "REGISTRY_USER_USERNAME"),
            "Registry 用户名",
        ),
        user_password=resolve_text_option(
            args.user_password,
            ("BOOTSTRAP_REGISTRY_PASSWORD", "REGISTRY_USER_PASSWORD"),
            "Registry 用户密码",
            secret=True,
        ),
        admin_username=resolve_text_option(
            args.admin_username,
            ("BOOTSTRAP_ADMIN_USERNAME", "REGISTRY_ADMIN_USERNAME"),
            "Registry 管理员用户名",
        ),
        admin_password=resolve_text_option(
            args.admin_password,
            ("BOOTSTRAP_ADMIN_PASSWORD", "REGISTRY_ADMIN_PASSWORD"),
            "Registry 管理员密码",
            secret=True,
        ),
    )


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
        if shutil.which(candidate):
            return str(shutil.which(candidate))
    raise BootstrapError("未找到 acps-cli 可执行文件；请先安装 wheel 运行包")


def current_timestamp() -> str:
    beijing = timezone(timedelta(hours=8))
    return datetime.now(tz=beijing).replace(microsecond=0).isoformat()


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BootstrapError(f"读取 JSON 文件失败: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"JSON 文件格式错误: {path}") from exc

    if not isinstance(payload, dict):
        raise BootstrapError(f"JSON 文件必须是对象: {path}")
    return payload


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_static_acs(template_name: str) -> tuple[Path, dict[str, Any]]:
    template_path = STATIC_ACS_DIR / template_name
    if not template_path.is_file():
        raise BootstrapError(f"未找到静态 ACS 文件: {template_path}")

    payload = load_json_file(template_path)
    if str(payload.get("aic") or "").strip():
        raise BootstrapError(f"静态 ACS 文件中的 aic 必须保持为空，请先手工清理: {template_path}")
    return template_path, payload


def copy_static_acs(template_name: str, destination_path: Path) -> dict[str, Any]:
    source_path, payload = load_static_acs(template_name)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
    return payload


def sync_acs_file(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file():
        raise BootstrapError(f"缺少源 ACS 文件: {source_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)


def sync_acs_aic(acs_path: Path, aic: str) -> None:
    payload = load_json_file(acs_path)
    if str(payload.get("aic") or "").strip() == aic:
        return
    payload["aic"] = aic
    write_json_file(acs_path, payload)


def extract_agent_name(acs_payload: dict[str, Any], template_name: str) -> str:
    return ensure_non_empty(str(acs_payload.get("name") or ""), f"{template_name} 中的 name")


def quote_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def quote_int(value: int) -> str:
    return str(value)


def load_config(config_path: str | None) -> tuple[dict[str, Any], Path]:
    toml_data, resolved_path = load_toml_config(config_path)
    if resolved_path is None:
        raise BootstrapError("未找到 acps-cli.toml；请通过 --config 指定配置文件")
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
            raise BootstrapError(f"{env_key} 不是有效整数") from exc
    section_data = get_section(data, section)
    value = section_data.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BootstrapError(f"[{section}].{key} 不是有效整数") from exc


def clear_generated_state(cleanup_paths: tuple[Path, ...]) -> None:
    for path in cleanup_paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.is_file():
                path.unlink()
        except OSError:
            pass


def run_command(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(  # noqa: S603  # nosec B603
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise BootstrapError(f"命令失败: {' '.join(cmd)}\n{output}")
    return output


def run_cli(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> tuple[bool, str]:
    result = subprocess.run(  # noqa: S603  # nosec B603
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


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
            raise BootstrapError(f"无法解析 JSON 输出:\n{output}") from exc
        if isinstance(data, dict):
            return data
    raise BootstrapError(f"无法解析 JSON 输出:\n{output}")


def run_json_command(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    return load_json_output(run_command(cmd, cwd=cwd, env=env))


def is_approved_update_conflict(output: str) -> bool:
    upper_output = output.upper()
    return "APPROVED" in upper_output and "CANNOT BE UPDATED" in upper_output


def save_registration_metadata(spec: RegistrationSpec, *, cli_bin: str, config_path: Path) -> dict[str, Any]:
    command = [
        cli_bin,
        "--config",
        str(config_path),
        "agent",
        "save",
        "--acs-file",
        str(spec.acs_path),
        "--json",
    ]
    ok, output = run_cli(command)
    if not ok and is_approved_update_conflict(output):
        log(f"{spec.name} 已审批且 ACS 有变更，自动删除后重建")
        delete_command = [
            cli_bin,
            "--config",
            str(config_path),
            "agent",
            "delete",
            "--acs-file",
            str(spec.acs_path),
            "--json",
        ]
        delete_ok, delete_output = run_cli(delete_command)
        if not delete_ok:
            raise BootstrapError(f"{spec.name} 删除旧 Agent 失败: {delete_output}")
        clear_generated_state(spec.cleanup_paths)
        ok, output = run_cli(command)
    if not ok:
        raise BootstrapError(f"{spec.name} 保存 ACS metadata 失败: {output}")
    return load_json_output(output)


def parse_bool_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def read_registration_status(
    spec: RegistrationSpec,
    *,
    cli_bin: str,
    config_path: Path,
    payload: dict[str, Any],
) -> tuple[str, str, bool, str]:
    current_status = str(payload.get("approval_status") or "").lower()
    agent_id = str(payload.get("agent_id") or "")
    is_disabled = parse_bool_flag(payload.get("is_disabled"))
    aic = str(payload.get("aic") or "").strip()

    check_output = run_json_command(
        [
            cli_bin,
            "--config",
            str(config_path),
            "agent",
            "check",
            "--acs-file",
            str(spec.acs_path),
            "--json",
        ]
    )
    current_status = str(check_output.get("status") or current_status).lower()
    agent_id = str(check_output.get("agent_id") or agent_id)
    is_disabled = parse_bool_flag(check_output.get("is_disabled", is_disabled))
    aic = str(check_output.get("aic") or aic).strip()
    return current_status, agent_id, is_disabled, aic


def ensure_registration(
    spec: RegistrationSpec,
    *,
    cli_bin: str,
    config_path: Path,
    approval_comments: str,
) -> str:
    payload = save_registration_metadata(spec, cli_bin=cli_bin, config_path=config_path)
    current_status, agent_id, is_disabled, aic = read_registration_status(
        spec,
        cli_bin=cli_bin,
        config_path=config_path,
        payload=payload,
    )
    if not agent_id:
        raise BootstrapError(f"{spec.name} 未返回 agent_id")
    if current_status not in {"draft", "pending", "approved"}:
        raise BootstrapError(f"{spec.name} 遇到不支持的状态: {current_status}")

    if is_disabled:
        run_json_command(
            [
                cli_bin,
                "--config",
                str(config_path),
                "admin",
                "registry",
                "agent",
                "enable",
                "--agent-id",
                agent_id,
                "--json",
            ]
        )

    if current_status == "draft":
        submit_output = run_json_command(
            [
                cli_bin,
                "--config",
                str(config_path),
                "agent",
                "submit",
                "--agent-id",
                agent_id,
                "--json",
            ]
        )
        current_status = str(submit_output.get("approval_status") or "").lower()

    if current_status == "pending":
        approve_output = run_json_command(
            [
                cli_bin,
                "--config",
                str(config_path),
                "admin",
                "registry",
                "review",
                "approve",
                "--agent-id",
                agent_id,
                "--comments",
                approval_comments,
                "--json",
            ]
        )
        current_status = str(approve_output.get("approval_status") or "").lower()
        aic = str(approve_output.get("aic") or aic).strip()

    current_status, agent_id, _is_disabled, aic = read_registration_status(
        spec,
        cli_bin=cli_bin,
        config_path=config_path,
        payload={
            "approval_status": current_status,
            "agent_id": agent_id,
            "is_disabled": is_disabled,
            "aic": aic,
        },
    )
    if current_status != "approved":
        raise BootstrapError(f"{spec.name} 未进入 APPROVED 状态: {current_status}")
    if not aic:
        raise BootstrapError(f"{spec.name} 审批完成后仍未返回 AIC")
    return aic


def issue_certificate(
    spec: CertificateSpec,
    *,
    aic: str,
    cli_bin: str,
    config_path: Path,
    trust_bundle_path: Path,
    work_root: Path,
) -> None:
    work_root.mkdir(parents=True, exist_ok=True)
    spec.cert_path.parent.mkdir(parents=True, exist_ok=True)
    spec.key_path.parent.mkdir(parents=True, exist_ok=True)
    eab_path = work_root / f"{spec.name}-eab.json"

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
            spec.usage,
            "--cert-path",
            str(spec.cert_path),
            "--key-path",
            str(spec.key_path),
            "--trust-bundle-path",
            str(trust_bundle_path),
        ]
    )


def write_runtime_config(
    original_data: dict[str, Any],
    original_path: Path,
    work_root: Path,
    *,
    mq_ca_file: Path | None = None,
    registry_mtls_server_ca_file: Path | None = None,
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
    registry_timeout = get_int_setting(
        original_data,
        "registry",
        "timeout_seconds",
        "REGISTRY_TIMEOUT_SECONDS",
        15,
    )
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
    ontology_dir = work_root / ".registry-client" / "ontology-mtls"
    token_dir.mkdir(parents=True, exist_ok=True)
    ontology_dir.mkdir(parents=True, exist_ok=True)
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
        f"ontology_mtls_materials_dir = {quote_string(str(ontology_dir))}",
        f"timeout_seconds = {quote_int(registry_timeout)}",
    ]
    if registry_mtls_server_ca_file is not None:
        lines.append(f"mtls_server_ca_file = {quote_string(str(registry_mtls_server_ca_file))}")

    lines.extend(
        [
            "",
            "[auth]",
            f"user_token_file = {quote_string(user_token_file)}",
            f"admin_token_file = {quote_string(admin_token_file)}",
            "",
        ]
    )
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
            f"timeout_seconds = {quote_int(mq_timeout)}",
        ]
    )
    if mq_ca_file is not None:
        lines.append(f"ca_cert_file = {quote_string(str(mq_ca_file))}")

    config_path = work_root / original_path.name
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


def ensure_registry_logins(
    cli_bin: str,
    config_path: Path,
    *,
    auth_mode: str,
    credentials: BootstrapCredentials | None,
) -> None:
    if auth_mode == "oidc":
        user_status = load_json_output(run_command([cli_bin, "--config", str(config_path), "auth", "status", "--json"]))
        admin_status = load_json_output(
            run_command([cli_bin, "--config", str(config_path), "admin", "auth", "status", "--json"])
        )
        if not user_status.get("authenticated"):
            raise BootstrapError("registry.auth.mode=oidc 时请先执行 acps-cli auth login 完成用户登录")
        if not admin_status.get("authenticated"):
            raise BootstrapError("registry.auth.mode=oidc 时请先执行 acps-cli admin auth login 完成管理员登录")
        log("检测到现有 registry OIDC user/admin session，跳过用户名密码登录")
        return

    if credentials is None:
        raise BootstrapError("local 模式缺少 Registry 用户/管理员凭据")
    run_command(
        [
            cli_bin,
            "--config",
            str(config_path),
            "auth",
            "login",
            "--username",
            credentials.user_username,
            "--password",
            credentials.user_password,
            "--json",
        ]
    )
    run_command(
        [
            cli_bin,
            "--config",
            str(config_path),
            "admin",
            "auth",
            "login",
            "--username",
            credentials.admin_username,
            "--password",
            credentials.admin_password,
            "--json",
        ]
    )


def build_registry_result(
    profile_dir: Path,
    service_aic: str,
    probe_aic: str,
    install_dir: Path | None = None,
    deployed_files: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile": "registry-server-9002",
        "output_dir": str(profile_dir),
        "service_aic": service_aic,
        "probe_aic": probe_aic,
        "files": {
            "server_cert": str(profile_dir / SERVER_CERT_FILE_NAME),
            "server_key": str(profile_dir / SERVER_KEY_FILE_NAME),
            "trust_bundle": str(profile_dir / TRUST_BUNDLE_FILE_NAME),
            "probe_cert": str(profile_dir / PROBE_CERT_FILE_NAME),
            "probe_key": str(profile_dir / PROBE_KEY_FILE_NAME),
        },
        "copy_to": [],
    }

    if install_dir is None:
        result["copy_to"] = [
            {
                "description": "复制到 registry-server 9002 listener 的服务端证书目录",
                "files": [
                    f"{SERVER_CERT_FILE_NAME} -> <registry-runtime>/certs/{SERVER_CERT_FILE_NAME}",
                    f"{SERVER_KEY_FILE_NAME} -> <registry-runtime>/certs/{SERVER_KEY_FILE_NAME}",
                    f"{TRUST_BUNDLE_FILE_NAME} -> <registry-runtime>/certs/{TRUST_BUNDLE_FILE_NAME}",
                ],
            },
            {
                "description": "保留在 acps-cli / 运维机上，用于 9002 健康检查与烟测",
                "files": [
                    PROBE_CERT_FILE_NAME,
                    PROBE_KEY_FILE_NAME,
                    TRUST_BUNDLE_FILE_NAME,
                ],
            },
        ]
    else:
        result["install_dir"] = str(install_dir)
        result["deployed_files"] = deployed_files or []
        result["copy_to"] = [
            {
                "description": "已自动写入 registry-server 安装目录 certs/",
                "files": deployed_files or [],
            },
            {
                "description": "保留在 acps-cli / 运维机上，用于 9002 健康检查与烟测",
                "files": [
                    PROBE_CERT_FILE_NAME,
                    PROBE_KEY_FILE_NAME,
                    TRUST_BUNDLE_FILE_NAME,
                ],
            },
        ]

    return result


def build_mq_result(
    profile_dir: Path,
    service_aic: str,
    probe_aic: str,
    install_dir: Path | None = None,
    deployed_files: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile": "mq-auth-server",
        "output_dir": str(profile_dir),
        "service_aic": service_aic,
        "probe_aic": probe_aic,
        "files": {
            "server_cert": str(profile_dir / SERVER_CERT_FILE_NAME),
            "server_key": str(profile_dir / SERVER_KEY_FILE_NAME),
            "probe_cert": str(profile_dir / PROBE_CERT_FILE_NAME),
            "probe_key": str(profile_dir / PROBE_KEY_FILE_NAME),
            "ca_bundle": str(profile_dir / CA_BUNDLE_FILE_NAME),
        },
        "copy_to": [],
    }

    if install_dir is None:
        result["copy_to"] = [
            {
                "description": "复制到 mq-auth-server 运行目录的 certs/",
                "files": [
                    f"{SERVER_CERT_FILE_NAME} -> <mq-runtime>/certs/{SERVER_CERT_FILE_NAME}",
                    f"{SERVER_KEY_FILE_NAME} -> <mq-runtime>/certs/{SERVER_KEY_FILE_NAME}",
                    f"{PROBE_CERT_FILE_NAME} -> <mq-runtime>/certs/{PROBE_CERT_FILE_NAME}",
                    f"{PROBE_KEY_FILE_NAME} -> <mq-runtime>/certs/{PROBE_KEY_FILE_NAME}",
                    f"{CA_BUNDLE_FILE_NAME} -> <mq-runtime>/certs/{CA_BUNDLE_FILE_NAME}",
                ],
            }
        ]
    else:
        result["install_dir"] = str(install_dir)
        result["deployed_files"] = deployed_files or []
        result["copy_to"] = [
            {
                "description": "已自动写入 mq-auth-server 安装目录 certs/",
                "files": deployed_files or [],
            }
        ]

    return result


def build_rabbitmq_result(
    profile_dir: Path,
    aic: str,
    install_dir: Path | None = None,
    deployed_files: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile": "rabbitmq",
        "output_dir": str(profile_dir),
        "aic": aic,
        "files": {
            "server_cert": str(profile_dir / RABBITMQ_SERVER_CERT_FILE_NAME),
            "server_key": str(profile_dir / RABBITMQ_SERVER_KEY_FILE_NAME),
            "client_cert": str(profile_dir / RABBITMQ_CLIENT_CERT_FILE_NAME),
            "client_key": str(profile_dir / RABBITMQ_CLIENT_KEY_FILE_NAME),
            "ca_bundle": str(profile_dir / CA_BUNDLE_FILE_NAME),
        },
        "copy_to": [],
    }

    if install_dir is None:
        result["copy_to"] = [
            {
                "description": "复制到 RabbitMQ 安装目录的 certs/",
                "files": [
                    f"{RABBITMQ_SERVER_CERT_FILE_NAME} -> <rabbitmq-runtime>/certs/{RABBITMQ_SERVER_CERT_FILE_NAME}",
                    f"{RABBITMQ_SERVER_KEY_FILE_NAME} -> <rabbitmq-runtime>/certs/{RABBITMQ_SERVER_KEY_FILE_NAME}",
                    f"{RABBITMQ_CLIENT_CERT_FILE_NAME} -> <rabbitmq-runtime>/certs/{RABBITMQ_CLIENT_CERT_FILE_NAME}",
                    f"{RABBITMQ_CLIENT_KEY_FILE_NAME} -> <rabbitmq-runtime>/certs/{RABBITMQ_CLIENT_KEY_FILE_NAME}",
                    f"{CA_BUNDLE_FILE_NAME} -> <rabbitmq-runtime>/certs/{CA_BUNDLE_FILE_NAME}",
                ],
            }
        ]
    else:
        result["install_dir"] = str(install_dir)
        result["deployed_files"] = deployed_files or []
        result["copy_to"] = [
            {
                "description": "已自动写入 RabbitMQ 安装目录 certs/",
                "files": deployed_files or [],
            }
        ]

    return result


def build_redis_result(
    profile_dir: Path,
    aic: str,
    install_dir: Path | None = None,
    deployed_files: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile": "redis",
        "output_dir": str(profile_dir),
        "aic": aic,
        "files": {
            "server_cert": str(profile_dir / REDIS_SERVER_CERT_FILE_NAME),
            "server_key": str(profile_dir / REDIS_SERVER_KEY_FILE_NAME),
            "ca_bundle": str(profile_dir / CA_BUNDLE_FILE_NAME),
        },
        "copy_to": [],
    }

    if install_dir is None:
        result["copy_to"] = [
            {
                "description": "复制到 Redis 安装目录的 certs/",
                "files": [
                    f"{REDIS_SERVER_CERT_FILE_NAME} -> <redis-runtime>/certs/{REDIS_SERVER_CERT_FILE_NAME}",
                    f"{REDIS_SERVER_KEY_FILE_NAME} -> <redis-runtime>/certs/{REDIS_SERVER_KEY_FILE_NAME}",
                    f"{CA_BUNDLE_FILE_NAME} -> <redis-runtime>/certs/{CA_BUNDLE_FILE_NAME}",
                ],
            }
        ]
    else:
        result["install_dir"] = str(install_dir)
        result["deployed_files"] = deployed_files or []
        result["copy_to"] = [
            {
                "description": "已自动写入 Redis 安装目录 certs/",
                "files": deployed_files or [],
            }
        ]

    return result


def discover_demo_partner_agents(install_dir: Path) -> list[DemoPartnerAgentSpec]:
    online_dir = install_dir / "partners" / "online"
    if not online_dir.is_dir():
        raise BootstrapError(f"未找到 demo-partner 在线目录: {online_dir}")

    agents: list[DemoPartnerAgentSpec] = []
    for agent_dir in sorted(online_dir.iterdir()):
        if not agent_dir.is_dir():
            continue

        acs_path = agent_dir / "acs.json"
        if not acs_path.is_file():
            raise BootstrapError(f"缺少 Partner ACS 文件: {acs_path}")

        acs_payload = load_json_file(acs_path)
        agents.append(
            DemoPartnerAgentSpec(
                directory=agent_dir,
                acs_path=acs_path,
                name=extract_agent_name(acs_payload, f"{agent_dir.name}/acs.json"),
            )
        )

    if not agents:
        raise BootstrapError(f"{online_dir} 下未发现任何 Partner 目录")
    return agents


def discover_demo_leader_runtime(install_dir: Path) -> DemoLeaderRuntimeSpec:
    leader_dir = install_dir / "leader"
    if not leader_dir.is_dir():
        raise BootstrapError(f"未找到 demo-leader leader 目录: {leader_dir}")

    scenario_dir = leader_dir / "scenario" / "expert" / "tour"
    if not scenario_dir.is_dir():
        raise BootstrapError(f"未找到 demo-leader 静态场景目录: {scenario_dir}")

    atr_dir = leader_dir / "atr"
    if not atr_dir.is_dir():
        raise BootstrapError(f"未找到 demo-leader atr 目录: {atr_dir}")

    acs_path = atr_dir / "acs.json"
    if not acs_path.is_file():
        raise BootstrapError(f"缺少 demo-leader ACS 文件: {acs_path}")

    acs_payload = load_json_file(acs_path)
    return DemoLeaderRuntimeSpec(
        install_dir=install_dir,
        leader_dir=leader_dir,
        scenario_dir=scenario_dir,
        atr_dir=atr_dir,
        acs_path=acs_path,
        name=extract_agent_name(acs_payload, "leader/atr/acs.json"),
    )


def build_demo_partner_result(
    profile_dir: Path,
    install_dir: Path,
    agent_results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "profile": "demo-partner",
        "output_dir": str(profile_dir),
        "install_dir": str(install_dir),
        "agents": agent_results,
        "copy_to": [
            {
                "description": "证书已直接写入 demo-partner 安装目录各 Partner 子目录",
                "files": [
                    "<install-dir>/partners/online/<partner>/acs.json",
                    f"<install-dir>/partners/online/<partner>/{SERVER_CERT_FILE_NAME}",
                    f"<install-dir>/partners/online/<partner>/{SERVER_KEY_FILE_NAME}",
                    f"<install-dir>/partners/online/<partner>/{TRUST_BUNDLE_FILE_NAME}",
                    f"<install-dir>/partners/online/<partner>/{PROBE_CERT_FILE_NAME}",
                    f"<install-dir>/partners/online/<partner>/{PROBE_KEY_FILE_NAME}",
                ],
            }
        ],
    }


def discover_demo_partner_install_dir(
    explicit_install_dir: Path | None,
    leader_install_dir: Path,
) -> Path:
    if explicit_install_dir is not None:
        return explicit_install_dir

    for ancestor in leader_install_dir.parents:
        candidate_root = ancestor / "demo-partner"
        if not candidate_root.is_dir():
            continue

        hotel_candidates = {path.parents[3] for path in candidate_root.glob("**/partners/online/china_hotel/acs.json")}
        transport_candidates = {
            path.parents[3] for path in candidate_root.glob("**/partners/online/china_transport/acs.json")
        }
        shared_candidates = hotel_candidates & transport_candidates
        if len(shared_candidates) == 1:
            return shared_candidates.pop()

    raise BootstrapError("无法自动定位 demo-partner 安装目录；请通过 --partner-install-dir 显式指定")


def sync_demo_leader_scenario_acs_from_demo_partner(
    partner_install_dir: Path,
    scenario_dir: Path,
) -> None:
    source_dest_pairs = [
        (
            partner_install_dir / "partners/online/china_hotel/acs.json",
            scenario_dir / CHINA_HOTEL_ACS_FILE_NAME,
        ),
        (
            partner_install_dir / "partners/online/china_transport/acs.json",
            scenario_dir / CHINA_TRANSPORT_ACS_FILE_NAME,
        ),
    ]
    for source_path, destination_path in source_dest_pairs:
        sync_acs_file(source_path, destination_path)
    log(f"demo-leader 静态 ACS 已从 demo-partner 安装目录同步: {scenario_dir}")


def build_demo_leader_result(
    profile_dir: Path,
    runtime_spec: DemoLeaderRuntimeSpec,
    aic: str,
) -> dict[str, Any]:
    return {
        "profile": "demo-leader",
        "output_dir": str(profile_dir),
        "install_dir": str(runtime_spec.install_dir),
        "leader_dir": str(runtime_spec.leader_dir),
        "aic": aic,
        "files": {
            "acs": str(runtime_spec.acs_path),
            "client_cert": str(runtime_spec.atr_dir / PROBE_CERT_FILE_NAME),
            "client_key": str(runtime_spec.atr_dir / PROBE_KEY_FILE_NAME),
            "trust_bundle": str(runtime_spec.atr_dir / TRUST_BUNDLE_FILE_NAME),
        },
        "copy_to": [
            {
                "description": "证书已直接写入 demo-leader 安装目录 leader/atr/",
                "files": [
                    "<install-dir>/leader/atr/acs.json",
                    f"<install-dir>/leader/atr/{PROBE_CERT_FILE_NAME}",
                    f"<install-dir>/leader/atr/{PROBE_KEY_FILE_NAME}",
                    f"<install-dir>/leader/atr/{TRUST_BUNDLE_FILE_NAME}",
                ],
            }
        ],
    }


def bootstrap_registry_profile(
    *,
    cli_bin: str,
    original_data: dict[str, Any],
    original_config_path: Path,
    output_root: Path,
    install_dir: Path | None,
    credentials: BootstrapCredentials | None,
    approval_comments: str,
) -> dict[str, Any]:
    profile_dir = output_root / "registry-server-9002"
    work_root = profile_dir / WORK_DIR_NAME
    profile_dir.mkdir(parents=True, exist_ok=True)
    config_path = write_runtime_config(original_data, original_config_path, work_root)
    ensure_registry_logins(
        cli_bin,
        config_path,
        auth_mode=registry_auth_mode(original_data, original_config_path),
        credentials=credentials,
    )

    service_acs_path = profile_dir / REGISTRY_SERVICE_ACS_FILE_NAME
    probe_acs_path = profile_dir / REGISTRY_PROBE_ACS_FILE_NAME
    service_acs = copy_static_acs(REGISTRY_SERVICE_ACS_FILE_NAME, service_acs_path)
    probe_acs = copy_static_acs(REGISTRY_PROBE_ACS_FILE_NAME, probe_acs_path)
    service_agent_name = extract_agent_name(service_acs, REGISTRY_SERVICE_ACS_FILE_NAME)
    probe_agent_name = extract_agent_name(probe_acs, REGISTRY_PROBE_ACS_FILE_NAME)

    registrations = [
        RegistrationSpec(
            name=service_agent_name,
            acs_path=service_acs_path,
            cleanup_paths=(
                work_root / "registry-server-9002-service",
                profile_dir / SERVER_CERT_FILE_NAME,
                profile_dir / SERVER_KEY_FILE_NAME,
            ),
        ),
        RegistrationSpec(
            name=probe_agent_name,
            acs_path=probe_acs_path,
            cleanup_paths=(
                work_root / "registry-server-9002-probe",
                profile_dir / PROBE_CERT_FILE_NAME,
                profile_dir / PROBE_KEY_FILE_NAME,
            ),
        ),
    ]
    service_aic = ensure_registration(
        registrations[0],
        cli_bin=cli_bin,
        config_path=config_path,
        approval_comments=approval_comments,
    )
    probe_aic = ensure_registration(
        registrations[1],
        cli_bin=cli_bin,
        config_path=config_path,
        approval_comments=approval_comments,
    )

    trust_bundle_path = profile_dir / TRUST_BUNDLE_FILE_NAME
    certificates = [
        (
            CertificateSpec(
                name=service_agent_name,
                acs_path=service_acs_path,
                usage="serverAuth",
                cert_path=profile_dir / SERVER_CERT_FILE_NAME,
                key_path=profile_dir / SERVER_KEY_FILE_NAME,
            ),
            service_aic,
        ),
        (
            CertificateSpec(
                name=probe_agent_name,
                acs_path=probe_acs_path,
                usage="clientAuth",
                cert_path=profile_dir / PROBE_CERT_FILE_NAME,
                key_path=profile_dir / PROBE_KEY_FILE_NAME,
            ),
            probe_aic,
        ),
    ]
    for spec, aic in certificates:
        issue_certificate(
            spec,
            aic=aic,
            cli_bin=cli_bin,
            config_path=config_path,
            trust_bundle_path=trust_bundle_path,
            work_root=work_root,
        )

    deployed_files: list[str] = []
    if install_dir is not None:
        deployed_files = deploy_profile_materials(
            "registry-server-9002",
            profile_dir,
            install_dir,
            (
                SERVER_CERT_FILE_NAME,
                SERVER_KEY_FILE_NAME,
                TRUST_BUNDLE_FILE_NAME,
            ),
        )

    result = build_registry_result(
        profile_dir,
        service_aic,
        probe_aic,
        install_dir=install_dir,
        deployed_files=deployed_files,
    )
    write_json_file(profile_dir / SUMMARY_FILE_NAME, result)
    log(f"registry-server 9002 证书已生成：{profile_dir}")
    return result


def bootstrap_mq_profile(
    *,
    cli_bin: str,
    original_data: dict[str, Any],
    original_config_path: Path,
    output_root: Path,
    install_dir: Path | None,
    credentials: BootstrapCredentials | None,
    approval_comments: str,
) -> dict[str, Any]:
    profile_dir = output_root / "mq-auth-server"
    work_root = profile_dir / WORK_DIR_NAME
    profile_dir.mkdir(parents=True, exist_ok=True)
    config_path = write_runtime_config(original_data, original_config_path, work_root)
    ensure_registry_logins(
        cli_bin,
        config_path,
        auth_mode=registry_auth_mode(original_data, original_config_path),
        credentials=credentials,
    )

    service_acs_path = profile_dir / MQ_SERVICE_ACS_FILE_NAME
    probe_acs_path = profile_dir / MQ_PROBE_ACS_FILE_NAME
    service_acs = copy_static_acs(MQ_SERVICE_ACS_FILE_NAME, service_acs_path)
    probe_acs = copy_static_acs(MQ_PROBE_ACS_FILE_NAME, probe_acs_path)
    service_agent_name = extract_agent_name(service_acs, MQ_SERVICE_ACS_FILE_NAME)
    probe_agent_name = extract_agent_name(probe_acs, MQ_PROBE_ACS_FILE_NAME)

    registrations = [
        RegistrationSpec(
            name=service_agent_name,
            acs_path=service_acs_path,
            cleanup_paths=(
                work_root / "mq-auth-server-service",
                profile_dir / SERVER_CERT_FILE_NAME,
                profile_dir / SERVER_KEY_FILE_NAME,
            ),
        ),
        RegistrationSpec(
            name=probe_agent_name,
            acs_path=probe_acs_path,
            cleanup_paths=(
                work_root / "mq-auth-server-probe",
                profile_dir / PROBE_CERT_FILE_NAME,
                profile_dir / PROBE_KEY_FILE_NAME,
            ),
        ),
    ]
    service_aic = ensure_registration(
        registrations[0],
        cli_bin=cli_bin,
        config_path=config_path,
        approval_comments=approval_comments,
    )
    probe_aic = ensure_registration(
        registrations[1],
        cli_bin=cli_bin,
        config_path=config_path,
        approval_comments=approval_comments,
    )

    trust_bundle_path = profile_dir / CA_BUNDLE_FILE_NAME
    certificates = [
        (
            CertificateSpec(
                name=service_agent_name,
                acs_path=service_acs_path,
                usage="serverAuth",
                cert_path=profile_dir / SERVER_CERT_FILE_NAME,
                key_path=profile_dir / SERVER_KEY_FILE_NAME,
            ),
            service_aic,
        ),
        (
            CertificateSpec(
                name=probe_agent_name,
                acs_path=probe_acs_path,
                usage="clientAuth",
                cert_path=profile_dir / PROBE_CERT_FILE_NAME,
                key_path=profile_dir / PROBE_KEY_FILE_NAME,
            ),
            probe_aic,
        ),
    ]
    for spec, aic in certificates:
        issue_certificate(
            spec,
            aic=aic,
            cli_bin=cli_bin,
            config_path=config_path,
            trust_bundle_path=trust_bundle_path,
            work_root=work_root,
        )

    deployed_files: list[str] = []
    if install_dir is not None:
        deployed_files = deploy_profile_materials(
            "mq-auth-server",
            profile_dir,
            install_dir,
            (
                SERVER_CERT_FILE_NAME,
                SERVER_KEY_FILE_NAME,
                PROBE_CERT_FILE_NAME,
                PROBE_KEY_FILE_NAME,
                CA_BUNDLE_FILE_NAME,
            ),
        )

    result = build_mq_result(
        profile_dir,
        service_aic,
        probe_aic,
        install_dir=install_dir,
        deployed_files=deployed_files,
    )
    write_json_file(profile_dir / SUMMARY_FILE_NAME, result)
    log(f"mq-auth-server 证书已生成：{profile_dir}")
    return result


def bootstrap_rabbitmq_profile(
    *,
    cli_bin: str,
    original_data: dict[str, Any],
    original_config_path: Path,
    output_root: Path,
    install_dir: Path | None,
    credentials: BootstrapCredentials | None,
    approval_comments: str,
) -> dict[str, Any]:
    profile_dir = output_root / "rabbitmq"
    work_root = profile_dir / WORK_DIR_NAME
    profile_dir.mkdir(parents=True, exist_ok=True)
    config_path = write_runtime_config(original_data, original_config_path, work_root)
    ensure_registry_logins(
        cli_bin,
        config_path,
        auth_mode=registry_auth_mode(original_data, original_config_path),
        credentials=credentials,
    )

    acs_path = profile_dir / RABBITMQ_ACS_FILE_NAME
    acs_payload = copy_static_acs(RABBITMQ_ACS_FILE_NAME, acs_path)
    agent_name = extract_agent_name(acs_payload, RABBITMQ_ACS_FILE_NAME)

    registration = RegistrationSpec(
        name=agent_name,
        acs_path=acs_path,
        cleanup_paths=(
            work_root / "rabbitmq-server",
            work_root / "rabbitmq-client",
            profile_dir / RABBITMQ_SERVER_CERT_FILE_NAME,
            profile_dir / RABBITMQ_SERVER_KEY_FILE_NAME,
            profile_dir / RABBITMQ_CLIENT_CERT_FILE_NAME,
            profile_dir / RABBITMQ_CLIENT_KEY_FILE_NAME,
        ),
    )
    aic = ensure_registration(
        registration,
        cli_bin=cli_bin,
        config_path=config_path,
        approval_comments=approval_comments,
    )

    trust_bundle_path = profile_dir / CA_BUNDLE_FILE_NAME
    certificates = [
        CertificateSpec(
            name="rabbitmq-server",
            acs_path=acs_path,
            usage="serverAuth",
            cert_path=profile_dir / RABBITMQ_SERVER_CERT_FILE_NAME,
            key_path=profile_dir / RABBITMQ_SERVER_KEY_FILE_NAME,
        ),
        CertificateSpec(
            name="rabbitmq-client",
            acs_path=acs_path,
            usage="clientAuth",
            cert_path=profile_dir / RABBITMQ_CLIENT_CERT_FILE_NAME,
            key_path=profile_dir / RABBITMQ_CLIENT_KEY_FILE_NAME,
        ),
    ]
    for spec in certificates:
        issue_certificate(
            spec,
            aic=aic,
            cli_bin=cli_bin,
            config_path=config_path,
            trust_bundle_path=trust_bundle_path,
            work_root=work_root,
        )

    deployed_files: list[str] = []
    if install_dir is not None:
        deployed_files = deploy_profile_materials(
            "rabbitmq",
            profile_dir,
            install_dir,
            (
                RABBITMQ_SERVER_CERT_FILE_NAME,
                RABBITMQ_SERVER_KEY_FILE_NAME,
                RABBITMQ_CLIENT_CERT_FILE_NAME,
                RABBITMQ_CLIENT_KEY_FILE_NAME,
                CA_BUNDLE_FILE_NAME,
            ),
        )

    result = build_rabbitmq_result(
        profile_dir,
        aic,
        install_dir=install_dir,
        deployed_files=deployed_files,
    )
    write_json_file(profile_dir / SUMMARY_FILE_NAME, result)
    log(f"RabbitMQ 证书已生成：{profile_dir}")
    return result


def bootstrap_redis_profile(
    *,
    cli_bin: str,
    original_data: dict[str, Any],
    original_config_path: Path,
    output_root: Path,
    install_dir: Path | None,
    credentials: BootstrapCredentials | None,
    approval_comments: str,
) -> dict[str, Any]:
    profile_dir = output_root / "redis"
    work_root = profile_dir / WORK_DIR_NAME
    profile_dir.mkdir(parents=True, exist_ok=True)
    config_path = write_runtime_config(original_data, original_config_path, work_root)
    ensure_registry_logins(
        cli_bin,
        config_path,
        auth_mode=registry_auth_mode(original_data, original_config_path),
        credentials=credentials,
    )

    acs_path = profile_dir / REDIS_ACS_FILE_NAME
    acs_payload = copy_static_acs(REDIS_ACS_FILE_NAME, acs_path)
    agent_name = extract_agent_name(acs_payload, REDIS_ACS_FILE_NAME)

    registration = RegistrationSpec(
        name=agent_name,
        acs_path=acs_path,
        cleanup_paths=(
            work_root / "redis-server",
            profile_dir / REDIS_SERVER_CERT_FILE_NAME,
            profile_dir / REDIS_SERVER_KEY_FILE_NAME,
        ),
    )
    aic = ensure_registration(
        registration,
        cli_bin=cli_bin,
        config_path=config_path,
        approval_comments=approval_comments,
    )

    trust_bundle_path = profile_dir / CA_BUNDLE_FILE_NAME
    issue_certificate(
        CertificateSpec(
            name="redis-server",
            acs_path=acs_path,
            usage="serverAuth",
            cert_path=profile_dir / REDIS_SERVER_CERT_FILE_NAME,
            key_path=profile_dir / REDIS_SERVER_KEY_FILE_NAME,
        ),
        aic=aic,
        cli_bin=cli_bin,
        config_path=config_path,
        trust_bundle_path=trust_bundle_path,
        work_root=work_root,
    )

    deployed_files: list[str] = []
    if install_dir is not None:
        deployed_files = deploy_profile_materials(
            "redis",
            profile_dir,
            install_dir,
            (
                REDIS_SERVER_CERT_FILE_NAME,
                REDIS_SERVER_KEY_FILE_NAME,
                CA_BUNDLE_FILE_NAME,
            ),
        )

    result = build_redis_result(
        profile_dir,
        aic,
        install_dir=install_dir,
        deployed_files=deployed_files,
    )
    write_json_file(profile_dir / SUMMARY_FILE_NAME, result)
    log(f"Redis 证书已生成：{profile_dir}")
    return result


def bootstrap_demo_partner_profile(
    *,
    cli_bin: str,
    original_data: dict[str, Any],
    original_config_path: Path,
    output_root: Path,
    install_dir: Path,
    credentials: BootstrapCredentials | None,
    approval_comments: str,
) -> dict[str, Any]:
    install_dir = install_dir.expanduser().resolve()
    profile_dir = output_root / "demo-partner"
    work_root = profile_dir / WORK_DIR_NAME
    profile_dir.mkdir(parents=True, exist_ok=True)
    config_path = write_runtime_config(original_data, original_config_path, work_root)
    ensure_registry_logins(
        cli_bin,
        config_path,
        auth_mode=registry_auth_mode(original_data, original_config_path),
        credentials=credentials,
    )

    agent_results: list[dict[str, Any]] = []
    for agent in discover_demo_partner_agents(install_dir):
        log(f"处理 demo-partner Agent: {agent.name}")
        agent_work_root = work_root / agent.directory.name
        registration = RegistrationSpec(
            name=agent.name,
            acs_path=agent.acs_path,
            cleanup_paths=(
                agent_work_root,
                agent.directory / SERVER_CERT_FILE_NAME,
                agent.directory / SERVER_KEY_FILE_NAME,
                agent.directory / TRUST_BUNDLE_FILE_NAME,
                agent.directory / PROBE_CERT_FILE_NAME,
                agent.directory / PROBE_KEY_FILE_NAME,
            ),
        )
        aic = ensure_registration(
            registration,
            cli_bin=cli_bin,
            config_path=config_path,
            approval_comments=approval_comments,
        )
        sync_acs_aic(agent.acs_path, aic)

        trust_bundle_path = agent.directory / TRUST_BUNDLE_FILE_NAME
        certificates = [
            CertificateSpec(
                name=agent.name,
                acs_path=agent.acs_path,
                usage="serverAuth",
                cert_path=agent.directory / SERVER_CERT_FILE_NAME,
                key_path=agent.directory / SERVER_KEY_FILE_NAME,
            ),
            CertificateSpec(
                name=agent.name,
                acs_path=agent.acs_path,
                usage="clientAuth",
                cert_path=agent.directory / PROBE_CERT_FILE_NAME,
                key_path=agent.directory / PROBE_KEY_FILE_NAME,
            ),
        ]
        for certificate in certificates:
            issue_certificate(
                certificate,
                aic=aic,
                cli_bin=cli_bin,
                config_path=config_path,
                trust_bundle_path=trust_bundle_path,
                work_root=agent_work_root,
            )

        agent_results.append(
            {
                "agent_name": agent.name,
                "agent_dir": str(agent.directory),
                "aic": aic,
                "files": {
                    "acs": str(agent.acs_path),
                    "server_cert": str(agent.directory / SERVER_CERT_FILE_NAME),
                    "server_key": str(agent.directory / SERVER_KEY_FILE_NAME),
                    "trust_bundle": str(trust_bundle_path),
                    "client_cert": str(agent.directory / PROBE_CERT_FILE_NAME),
                    "client_key": str(agent.directory / PROBE_KEY_FILE_NAME),
                },
            }
        )

    result = build_demo_partner_result(profile_dir, install_dir, agent_results)
    write_json_file(profile_dir / SUMMARY_FILE_NAME, result)
    log(f"demo-partner 证书已生成：{profile_dir}")
    return result


def bootstrap_demo_leader_profile(
    *,
    cli_bin: str,
    original_data: dict[str, Any],
    original_config_path: Path,
    output_root: Path,
    install_dir: Path,
    demo_partner_install_dir: Path | None,
    credentials: BootstrapCredentials | None,
    approval_comments: str,
) -> dict[str, Any]:
    install_dir = install_dir.expanduser().resolve()
    runtime_spec = discover_demo_leader_runtime(install_dir)
    profile_dir = output_root / "demo-leader"
    work_root = profile_dir / WORK_DIR_NAME
    profile_dir.mkdir(parents=True, exist_ok=True)
    config_path = write_runtime_config(original_data, original_config_path, work_root)
    ensure_registry_logins(
        cli_bin,
        config_path,
        auth_mode=registry_auth_mode(original_data, original_config_path),
        credentials=credentials,
    )

    # Agent ACME/EAB workdir only — never wipe work_root itself (holds runtime acps-cli.toml + tokens).
    agent_work_root = work_root / "demo-leader"
    log(f"处理 demo-leader Agent: {runtime_spec.name}")
    registration = RegistrationSpec(
        name=runtime_spec.name,
        acs_path=runtime_spec.acs_path,
        cleanup_paths=(
            agent_work_root,
            runtime_spec.atr_dir / PROBE_CERT_FILE_NAME,
            runtime_spec.atr_dir / PROBE_KEY_FILE_NAME,
            runtime_spec.atr_dir / TRUST_BUNDLE_FILE_NAME,
        ),
    )
    aic = ensure_registration(
        registration,
        cli_bin=cli_bin,
        config_path=config_path,
        approval_comments=approval_comments,
    )
    sync_acs_aic(runtime_spec.acs_path, aic)
    partner_install_dir = discover_demo_partner_install_dir(
        demo_partner_install_dir,
        install_dir,
    )
    sync_demo_leader_scenario_acs_from_demo_partner(
        partner_install_dir,
        runtime_spec.scenario_dir,
    )

    issue_certificate(
        CertificateSpec(
            name=runtime_spec.name,
            acs_path=runtime_spec.acs_path,
            usage="clientAuth",
            cert_path=runtime_spec.atr_dir / PROBE_CERT_FILE_NAME,
            key_path=runtime_spec.atr_dir / PROBE_KEY_FILE_NAME,
        ),
        aic=aic,
        cli_bin=cli_bin,
        config_path=config_path,
        trust_bundle_path=runtime_spec.atr_dir / TRUST_BUNDLE_FILE_NAME,
        work_root=agent_work_root,
    )

    result = build_demo_leader_result(profile_dir, runtime_spec, aic)
    write_json_file(profile_dir / SUMMARY_FILE_NAME, result)
    log(f"demo-leader 证书已生成：{profile_dir}")
    return result


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="acps-cli.toml 路径；默认按 CLI 规则搜索")
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "证书输出目录；默认 <config_dir>/bootstrap-artifacts，"
            "demo-partner / demo-leader 模式默认 <install-dir>/bootstrap-artifacts"
        ),
    )
    parser.add_argument("--cli-bin", default=None, help="显式指定 acps-cli 可执行文件")
    parser.add_argument("--user-username", default=None, help="执行 agent save/submit 的普通用户用户名")
    parser.add_argument("--user-password", default=None, help="普通用户密码")
    parser.add_argument("--admin-username", default=None, help="审核用管理员用户名")
    parser.add_argument("--admin-password", default=None, help="审核用管理员密码")
    parser.add_argument(
        "--approval-comments",
        default="acps-cli bootstrap",
        help="自动审批时写入 registry review approve 的备注",
    )


def add_app_service_install_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--registry-install-dir",
        default=None,
        help=(
            "registry-server 安装目录（可选，本机可访问时使用）；"
            "若设置，将把 registry-9002 服务端证书自动写入 <install-dir>/certs/"
        ),
    )
    parser.add_argument(
        "--mq-auth-install-dir",
        default=None,
        help=("mq-auth-server 安装目录（可选，本机可访问时使用）；若设置，将把 mq 证书自动写入 <install-dir>/certs/"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    all_parser = subparsers.add_parser("all", help="一次性生成 registry-server 9002 与 mq-auth-server 所需证书")
    add_common_args(all_parser)
    add_app_service_install_args(all_parser)
    registry_parser = subparsers.add_parser("registry-9002", help="只生成 registry-server 9002 所需证书")
    add_common_args(registry_parser)
    add_app_service_install_args(registry_parser)

    mq_parser = subparsers.add_parser("mq-auth-server", help="只生成 mq-auth-server 所需证书")
    add_common_args(mq_parser)
    add_app_service_install_args(mq_parser)

    rabbitmq_parser = subparsers.add_parser("rabbitmq", help="为 RabbitMQ 基础设施生成部署证书")
    add_common_args(rabbitmq_parser)
    rabbitmq_parser.add_argument(
        "--install-dir",
        default=None,
        help="RabbitMQ 安装目录（可选，本机可访问时使用）；若设置，将把证书自动写入 <install-dir>/certs/",
    )

    redis_parser = subparsers.add_parser("redis", help="为 Redis 基础设施生成部署证书")
    add_common_args(redis_parser)
    redis_parser.add_argument(
        "--install-dir",
        default=None,
        help="Redis 安装目录（可选，本机可访问时使用）；若设置，将把证书自动写入 <install-dir>/certs/",
    )

    demo_partner_parser = subparsers.add_parser(
        "demo-partner",
        help="为 demo-partner 安装目录注册 Partner ACS 并签发证书",
    )
    add_common_args(demo_partner_parser)
    demo_partner_parser.add_argument(
        "--install-dir",
        required=True,
        help="demo-partner 安装目录；应包含 partners/online/*/acs.json",
    )

    demo_leader_parser = subparsers.add_parser(
        "demo-leader",
        help="为 demo-leader 安装目录注册 leader/atr/acs.json 并签发证书",
    )
    add_common_args(demo_leader_parser)
    demo_leader_parser.add_argument(
        "--install-dir",
        required=True,
        help="demo-leader 安装目录；应包含 leader/atr/acs.json",
    )
    demo_leader_parser.add_argument(
        "--partner-install-dir",
        default=None,
        help=(
            "demo-partner 安装目录；用于把 partner 的静态 ACS 整文件同步到 leader/scenario/"
            "；未提供时会尝试自动搜索同级 demo-partner"
        ),
    )
    return parser


def resolve_runtime_output_root(
    args: argparse.Namespace,
    runtime_dir: Path,
) -> tuple[Path | None, Path]:
    if args.command in {"demo-partner", "demo-leader"}:
        install_dir = Path(args.install_dir).expanduser().resolve()
        output_root = (
            Path(args.output_dir).expanduser().resolve() if args.output_dir else install_dir / "bootstrap-artifacts"
        )
        return install_dir, output_root

    output_root = (
        Path(args.output_dir).expanduser().resolve() if args.output_dir else runtime_dir / "bootstrap-artifacts"
    )
    return None, output_root


def resolve_server_install_dirs(
    args: argparse.Namespace,
) -> tuple[Path | None, Path | None]:
    registry_install_dir = resolve_optional_install_dir(
        getattr(args, "registry_install_dir", None),
        "registry-server 安装目录",
    )
    mq_auth_install_dir = resolve_optional_install_dir(
        getattr(args, "mq_auth_install_dir", None),
        "mq-auth-server 安装目录",
    )

    if args.command == "registry-9002" and mq_auth_install_dir is not None:
        raise BootstrapError("registry-9002 模式不支持 --mq-auth-install-dir")
    if args.command == "mq-auth-server" and registry_install_dir is not None:
        raise BootstrapError("mq-auth-server 模式不支持 --registry-install-dir")
    if args.command in {"demo-partner", "demo-leader"} and (
        registry_install_dir is not None or mq_auth_install_dir is not None
    ):
        raise BootstrapError("demo-partner/demo-leader 模式不支持 --registry-install-dir 或 --mq-auth-install-dir")

    return registry_install_dir, mq_auth_install_dir


def resolve_infra_install_dirs(
    args: argparse.Namespace,
) -> tuple[Path | None, Path | None]:
    rabbitmq_install_dir = None
    if args.command == "rabbitmq":
        rabbitmq_install_dir = resolve_optional_install_dir(getattr(args, "install_dir", None), "RabbitMQ 安装目录")

    redis_install_dir = None
    if args.command == "redis":
        redis_install_dir = resolve_optional_install_dir(getattr(args, "install_dir", None), "Redis 安装目录")

    return rabbitmq_install_dir, redis_install_dir


def run_selected_profiles(
    args: argparse.Namespace,
    *,
    cli_bin: str,
    original_data: dict[str, Any],
    original_config_path: Path,
    output_root: Path,
    install_dir: Path | None,
    credentials: BootstrapCredentials | None,
    registry_install_dir: Path | None,
    mq_auth_install_dir: Path | None,
    rabbitmq_install_dir: Path | None,
    redis_install_dir: Path | None,
    demo_partner_install_dir: Path | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    if args.command in {"all", "registry-9002"}:
        results.append(
            bootstrap_registry_profile(
                cli_bin=cli_bin,
                original_data=original_data,
                original_config_path=original_config_path,
                output_root=output_root,
                install_dir=registry_install_dir,
                credentials=credentials,
                approval_comments=args.approval_comments,
            )
        )

    if args.command in {"all", "mq-auth-server"}:
        results.append(
            bootstrap_mq_profile(
                cli_bin=cli_bin,
                original_data=original_data,
                original_config_path=original_config_path,
                output_root=output_root,
                install_dir=mq_auth_install_dir,
                credentials=credentials,
                approval_comments=args.approval_comments,
            )
        )

    if args.command == "rabbitmq":
        results.append(
            bootstrap_rabbitmq_profile(
                cli_bin=cli_bin,
                original_data=original_data,
                original_config_path=original_config_path,
                output_root=output_root,
                install_dir=rabbitmq_install_dir,
                credentials=credentials,
                approval_comments=args.approval_comments,
            )
        )

    if args.command == "redis":
        results.append(
            bootstrap_redis_profile(
                cli_bin=cli_bin,
                original_data=original_data,
                original_config_path=original_config_path,
                output_root=output_root,
                install_dir=redis_install_dir,
                credentials=credentials,
                approval_comments=args.approval_comments,
            )
        )

    if args.command == "demo-partner":
        if install_dir is None:
            raise BootstrapError("demo-partner 模式缺少 --install-dir")
        results.append(
            bootstrap_demo_partner_profile(
                cli_bin=cli_bin,
                original_data=original_data,
                original_config_path=original_config_path,
                output_root=output_root,
                install_dir=install_dir,
                credentials=credentials,
                approval_comments=args.approval_comments,
            )
        )

    if args.command == "demo-leader":
        if install_dir is None:
            raise BootstrapError("demo-leader 模式缺少 --install-dir")
        results.append(
            bootstrap_demo_leader_profile(
                cli_bin=cli_bin,
                original_data=original_data,
                original_config_path=original_config_path,
                output_root=output_root,
                install_dir=install_dir,
                demo_partner_install_dir=demo_partner_install_dir,
                credentials=credentials,
                approval_comments=args.approval_comments,
            )
        )

    return results


def main() -> int:
    args = build_parser().parse_args()
    original_data, original_config_path = load_config(args.config)
    runtime_dir = original_config_path.parent
    install_dir, output_root = resolve_runtime_output_root(args, runtime_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    cli_bin = resolve_cli_bin(args.cli_bin, runtime_dir)
    credentials = (
        resolve_credentials(args) if registry_auth_mode(original_data, original_config_path) == "local" else None
    )
    registry_install_dir, mq_auth_install_dir = resolve_server_install_dirs(args)
    rabbitmq_install_dir, redis_install_dir = resolve_infra_install_dirs(args)
    demo_partner_install_dir = resolve_optional_install_dir(
        getattr(args, "partner_install_dir", None),
        "demo-partner 安装目录",
    )
    results = run_selected_profiles(
        args,
        cli_bin=cli_bin,
        original_data=original_data,
        original_config_path=original_config_path,
        output_root=output_root,
        install_dir=install_dir,
        credentials=credentials,
        registry_install_dir=registry_install_dir,
        mq_auth_install_dir=mq_auth_install_dir,
        rabbitmq_install_dir=rabbitmq_install_dir,
        redis_install_dir=redis_install_dir,
        demo_partner_install_dir=demo_partner_install_dir,
    )

    summary = {
        "created_at": current_timestamp(),
        "config_file": str(original_config_path),
        "output_dir": str(output_root),
        "profiles": results,
    }
    write_json_file(output_root / SUMMARY_FILE_NAME, summary)
    log(f"证书自举完成，汇总文件：{output_root / SUMMARY_FILE_NAME}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapError as exc:
        print(f"[bootstrap] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
