from __future__ import annotations

import os
from pathlib import Path

RUNTIME_ROOT_ENV = "LEADER_RUNTIME_ROOT"
SCENARIO_ROOT_ENV = "LEADER_SCENARIO_ROOT"
WEB_APP_ROOT_ENV = "WEB_APP_ROOT"

PACKAGE_LEADER_DIR = Path(__file__).resolve().parent
PACKAGE_RUNTIME_ROOT = PACKAGE_LEADER_DIR.parent
PACKAGE_WEB_APP_DIR = PACKAGE_RUNTIME_ROOT / "web_app"


def _resolve_env_dir(env_name: str) -> Path | None:
    raw_value = os.getenv(env_name, "").strip()
    if not raw_value:
        return None
    return Path(raw_value).expanduser().resolve()


def resolve_runtime_root() -> Path:
    return _resolve_env_dir(RUNTIME_ROOT_ENV) or PACKAGE_RUNTIME_ROOT


def resolve_leader_dir() -> Path:
    runtime_root = resolve_runtime_root()
    candidate = runtime_root / "leader"
    if candidate.is_dir():
        return candidate
    return PACKAGE_LEADER_DIR


def resolve_project_env_file() -> Path:
    return resolve_runtime_root() / ".env"


def resolve_config_path() -> Path:
    return resolve_leader_dir() / "config.toml"


def resolve_acs_path(relative_path: str) -> Path:
    return resolve_leader_dir() / relative_path


def resolve_scenario_root() -> Path:
    explicit_root = _resolve_env_dir(SCENARIO_ROOT_ENV)
    if explicit_root is not None:
        return explicit_root

    candidate = resolve_leader_dir() / "scenario"
    if candidate.is_dir():
        return candidate
    return PACKAGE_LEADER_DIR / "scenario"


def resolve_web_app_root() -> Path:
    explicit_root = _resolve_env_dir(WEB_APP_ROOT_ENV)
    if explicit_root is not None:
        return explicit_root

    candidate = resolve_runtime_root() / "web_app"
    if candidate.is_dir():
        return candidate
    return PACKAGE_WEB_APP_DIR
