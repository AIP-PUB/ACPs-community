"""统一 CLI 命令树的配置桥接辅助函数。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import click

from acps_cli.shared.runtime import RootCliRuntime

DEFAULT_OIDC_SCOPES = ("openid", "profile", "email")


@dataclass(frozen=True)
class OidcAuthConfig:
    issuer: str
    client_id: str
    scopes: tuple[str, ...]
    require_https: bool


@dataclass(frozen=True)
class ServiceAuthConfig:
    service: str
    account_kind: str
    mode: str
    token_file: str
    oidc: OidcAuthConfig | None = None


def _section(runtime: RootCliRuntime, name: str) -> dict[str, Any]:
    value = runtime.toml_data.get(name, {})
    return value if isinstance(value, dict) else {}


def _normalize_url(label: str, value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise click.ClickException(f"Invalid {label}: {value}")
    return value.rstrip("/")


def _resolve_path(base_dir: Path, value: str) -> str:
    path = Path(value).expanduser()
    if path.is_absolute():
        return str(path)
    return str((base_dir / path).resolve())


def _normalize_bool(label: str, value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise click.ClickException(f"Invalid {label}: {value}")


def _normalize_scopes(label: str, value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [part for part in re.split(r"[\s,]+", value) if part]
    elif isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise click.ClickException(f"Invalid {label}: {value}")

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return tuple(result)


def _reject_legacy_toml_keys(section_name: str, section: dict[str, Any], replacements: dict[str, str]) -> None:
    for old_key, new_key in replacements.items():
        if old_key in section:
            raise click.ClickException(
                f"Config key [{section_name}].{old_key} is no longer supported. Use {new_key} instead."
            )


def _reject_legacy_env_keys(replacements: dict[str, str]) -> None:
    for old_key, new_key in replacements.items():
        if os.getenv(old_key) not in (None, ""):
            raise click.ClickException(f"Environment variable {old_key} is no longer supported. Use {new_key} instead.")


def build_registry_legacy_section(
    runtime: RootCliRuntime,
    *,
    cli_base_url: str | None,
    cli_mtls_url: str | None = None,
    admin: bool,
    require_mtls: bool = False,
) -> dict[str, Any]:
    registry_section = dict(_section(runtime, "registry"))
    auth_section = _section(runtime, "auth")
    base_dir = runtime.config_dir or Path.cwd()

    _reject_legacy_toml_keys(
        "registry",
        registry_section,
        {
            "server_base_url": "[registry].base_url",
            "atr_base_url": "internal derived ATR path from [registry].base_url",
            "token_file": "[auth].user_token_file / [auth].admin_token_file",  # nosec B105 - config path description
        },
    )
    _reject_legacy_env_keys(
        {
            "REGISTRY_SERVER_BASE_URL": "REGISTRY_BASE_URL",
            "REGISTRY_API_BASE_URL": "REGISTRY_BASE_URL",
            "REGISTRY_ATR_BASE_URL": "derived ATR path from REGISTRY_BASE_URL",
            "REGISTRY_TOKEN_FILE": "AUTH_USER_TOKEN_FILE or AUTH_ADMIN_TOKEN_FILE",  # nosec B105 - config key description
        }
    )

    base_url = _normalize_url(
        "REGISTRY_BASE_URL",
        str(
            cli_base_url
            or os.getenv("REGISTRY_BASE_URL")
            or registry_section.get("base_url")
            or "http://localhost:9001"
        ),
    )
    mtls_value = cli_mtls_url or os.getenv("REGISTRY_MTLS_BASE_URL") or registry_section.get("mtls_base_url")
    if require_mtls and not mtls_value:
        raise click.ClickException(
            "registry.mtls_base_url is required for entity derive. "
            "Configure [registry].mtls_base_url, REGISTRY_MTLS_BASE_URL, or pass --mtls-url."
        )

    token_key = "admin_token_file" if admin else "user_token_file"
    token_env_key = "AUTH_ADMIN_TOKEN_FILE" if admin else "AUTH_USER_TOKEN_FILE"
    token_default_name = "registry-admin.json" if admin else "registry-user.json"
    token_value = str(
        os.getenv(token_env_key)
        or auth_section.get(token_key)
        or (base_dir / ".acps-cli" / "tokens" / token_default_name)
    )

    legacy_section = dict(registry_section)
    legacy_section["server_base_url"] = f"{base_url}/api/v1"
    legacy_section["token_file"] = _resolve_path(base_dir, token_value)
    if mtls_value:
        legacy_section["mtls_base_url"] = _normalize_url("REGISTRY_MTLS_BASE_URL", str(mtls_value))
    return legacy_section


def build_ca_legacy_section(runtime: RootCliRuntime, *, cli_base_url: str | None) -> dict[str, Any]:
    ca_section = dict(_section(runtime, "ca"))

    _reject_legacy_toml_keys(
        "ca",
        ca_section,
        {
            "server_base_url": "[ca].base_url",
        },
    )
    _reject_legacy_env_keys(
        {
            "CA_SERVER_BASE_URL": "CA_BASE_URL",
            "CA_SERVER_ATR_BASE_URL": "derived ATR path from CA_BASE_URL",
        }
    )

    base_url = _normalize_url(
        "CA_BASE_URL",
        str(cli_base_url or os.getenv("CA_BASE_URL") or ca_section.get("base_url") or "http://localhost:9003"),
    )
    legacy_section = dict(ca_section)
    legacy_section["server_base_url"] = base_url
    return legacy_section


def build_discovery_runtime_context(runtime: RootCliRuntime, *, cli_base_url: str | None) -> dict[str, Any]:
    discovery_section = dict(_section(runtime, "discovery"))

    _reject_legacy_toml_keys(
        "discovery",
        discovery_section,
        {
            "server_base_url": "[discovery].base_url",
        },
    )
    _reject_legacy_env_keys(
        {
            "DISCOVERY_SERVER_BASE_URL": "DISCOVERY_BASE_URL",
        }
    )

    base_url = _normalize_url(
        "DISCOVERY_BASE_URL",
        str(
            cli_base_url
            or os.getenv("DISCOVERY_BASE_URL")
            or discovery_section.get("base_url")
            or "http://localhost:9005"
        ),
    )
    return {
        "server_base_url": base_url,
        "toml_data": runtime.toml_data,
        "config_dir": runtime.config_dir,
    }


def _normalize_api_prefix(value: str) -> str:
    prefix = value.strip()
    if not prefix.startswith("/"):
        raise click.ClickException(f"Invalid MONITOR_API_PREFIX: {value}")
    normalized = prefix.rstrip("/")
    if not normalized:
        raise click.ClickException(f"Invalid MONITOR_API_PREFIX: {value}")
    return normalized


def _normalize_positive_timeout(label: str, value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise click.ClickException(f"Invalid {label}: {value}") from exc
    if timeout <= 0:
        raise click.ClickException(f"Invalid {label}: {value}")
    return timeout


def _pick_optional_config_value(*values: object) -> object | None:
    for value in values:
        if value is None:
            continue
        return value
    return None


def _resolve_auth_token_file(
    runtime: RootCliRuntime,
    *,
    env_key: str,
    toml_key: str,
    default_file_name: str,
) -> str:
    auth_section = _section(runtime, "auth")
    base_dir = runtime.config_dir or Path.cwd()
    configured_value = os.getenv(env_key) or auth_section.get(toml_key)
    if configured_value is None:
        return str(base_dir / ".acps-cli" / "tokens" / default_file_name)
    return _resolve_path(base_dir, str(configured_value))


def _build_oidc_auth_config(
    *,
    section_name: str,
    section: dict[str, Any],
    issuer_env: str,
    client_id_env: str,
    scopes_env: str,
    require_https_env: str,
) -> OidcAuthConfig:
    issuer_value = os.getenv(issuer_env) or section.get("issuer")
    client_id_value = os.getenv(client_id_env) or section.get("client_id")
    scopes_value = _pick_optional_config_value(
        os.getenv(scopes_env),
        section.get("scopes"),
        DEFAULT_OIDC_SCOPES,
    )
    require_https_value = _pick_optional_config_value(
        os.getenv(require_https_env),
        section.get("require_https"),
        True,
    )

    if issuer_value in (None, ""):
        raise click.ClickException(f"{section_name}.issuer is required when mode=oidc")
    if client_id_value in (None, ""):
        raise click.ClickException(f"{section_name}.client_id is required when mode=oidc")

    issuer = _normalize_url(f"{section_name}.issuer", str(issuer_value))
    client_id = str(client_id_value).strip()
    if not client_id:
        raise click.ClickException(f"{section_name}.client_id is required when mode=oidc")

    scopes = _normalize_scopes(f"{section_name}.scopes", scopes_value)
    if "openid" not in scopes:
        raise click.ClickException(f"{section_name}.scopes must include openid")

    require_https = _normalize_bool(require_https_env, require_https_value)
    return OidcAuthConfig(
        issuer=issuer,
        client_id=client_id,
        scopes=scopes,
        require_https=require_https,
    )


def build_registry_auth_config(runtime: RootCliRuntime, *, admin: bool) -> ServiceAuthConfig:
    registry_section = _section(runtime, "registry")
    auth_section = registry_section.get("auth", {})
    auth_settings = auth_section if isinstance(auth_section, dict) else {}
    mode_value = _pick_optional_config_value(
        os.getenv("ACPS_CLI_REGISTRY_AUTH_MODE"),
        auth_settings.get("mode"),
        "local",
    )
    mode = str(mode_value).strip().lower()
    if mode not in {"local", "oidc"}:
        raise click.ClickException(f"registry.auth.mode must be one of: local, oidc (got {mode_value})")

    oidc = None
    if mode == "oidc":
        oidc = _build_oidc_auth_config(
            section_name="registry.auth",
            section=auth_settings,
            issuer_env="ACPS_CLI_REGISTRY_OIDC_ISSUER",
            client_id_env="ACPS_CLI_REGISTRY_OIDC_CLIENT_ID",
            scopes_env="ACPS_CLI_REGISTRY_OIDC_SCOPES",
            require_https_env="ACPS_CLI_REGISTRY_OIDC_REQUIRE_HTTPS",
        )

    return ServiceAuthConfig(
        service="registry",
        account_kind="admin" if admin else "user",
        mode=mode,
        token_file=_resolve_auth_token_file(
            runtime,
            env_key="AUTH_ADMIN_TOKEN_FILE" if admin else "AUTH_USER_TOKEN_FILE",
            toml_key="admin_token_file" if admin else "user_token_file",
            default_file_name="registry-admin.json" if admin else "registry-user.json",
        ),
        oidc=oidc,
    )


def build_monitor_auth_config(runtime: RootCliRuntime) -> ServiceAuthConfig:
    monitor_section = _section(runtime, "monitor")
    auth_section = monitor_section.get("auth", {})
    auth_settings = auth_section if isinstance(auth_section, dict) else {}
    mode_value = _pick_optional_config_value(
        os.getenv("ACPS_CLI_MONITOR_AUTH_MODE"),
        auth_settings.get("mode"),
        "none",
    )
    mode = str(mode_value).strip().lower()
    if mode not in {"none", "oidc"}:
        raise click.ClickException(f"monitor.auth.mode must be one of: none, oidc (got {mode_value})")

    oidc = None
    if mode == "oidc":
        oidc = _build_oidc_auth_config(
            section_name="monitor.auth",
            section=auth_settings,
            issuer_env="ACPS_CLI_MONITOR_OIDC_ISSUER",
            client_id_env="ACPS_CLI_MONITOR_OIDC_CLIENT_ID",
            scopes_env="ACPS_CLI_MONITOR_OIDC_SCOPES",
            require_https_env="ACPS_CLI_MONITOR_OIDC_REQUIRE_HTTPS",
        )

    return ServiceAuthConfig(
        service="monitor",
        account_kind="user",
        mode=mode,
        token_file=_resolve_auth_token_file(
            runtime,
            env_key="AUTH_MONITOR_TOKEN_FILE",
            toml_key="monitor_token_file",
            default_file_name="monitor-user.json",
        ),
        oidc=oidc,
    )


def build_monitor_runtime_context(
    runtime: RootCliRuntime,
    *,
    cli_base_url: str | None,
    cli_api_prefix: str | None,
) -> dict[str, Any]:
    monitor_section = dict(_section(runtime, "monitor"))
    base_url_value = _pick_optional_config_value(
        cli_base_url,
        os.getenv("MONITOR_BASE_URL"),
        monitor_section.get("base_url"),
        "http://localhost:9009",
    )
    api_prefix_value = _pick_optional_config_value(
        cli_api_prefix,
        os.getenv("MONITOR_API_PREFIX"),
        monitor_section.get("api_prefix"),
        "/acps-amp-v1",
    )
    timeout_value = _pick_optional_config_value(
        os.getenv("MONITOR_TIMEOUT_SECONDS"),
        monitor_section.get("timeout_seconds"),
        "15",
    )

    base_url = _normalize_url(
        "MONITOR_BASE_URL",
        str(base_url_value),
    )
    api_prefix = _normalize_api_prefix(str(api_prefix_value))
    timeout_seconds = _normalize_positive_timeout(
        "MONITOR_TIMEOUT_SECONDS",
        str(timeout_value),
    )
    return {
        "server_base_url": base_url,
        "api_prefix": api_prefix,
        "timeout_seconds": timeout_seconds,
        "toml_data": runtime.toml_data,
        "config_dir": runtime.config_dir,
    }
