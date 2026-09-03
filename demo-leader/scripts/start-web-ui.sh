#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

PYTHON_BIN="${PYTHON_BIN:-${BASE_DIR}/.venv/bin/python}"
ENV_FILE="${ENV_FILE:-${BASE_DIR}/.env}"
LEADER_RUNTIME_ROOT="${LEADER_RUNTIME_ROOT:-${BASE_DIR}}"
WEB_APP_ROOT="${WEB_APP_ROOT:-${LEADER_RUNTIME_ROOT}/web_app}"
CONFIG_FILE="${LEADER_CONFIG_FILE:-${LEADER_RUNTIME_ROOT}/leader/config.toml}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    err "未找到可执行 Python: ${PYTHON_BIN}"
    exit 1
fi

source_env_file "${ENV_FILE}"
require_file_exists "${CONFIG_FILE}" "leader/config.toml"

WEB_APP_HOST="${WEB_APP_HOST:-$(extract_toml_section_string_value "web" "host" "${CONFIG_FILE}")}"
WEB_APP_PORT="${WEB_APP_PORT:-$(extract_toml_section_integer_value "web" "port" "${CONFIG_FILE}")}"
LEADER_API_UPSTREAM="${LEADER_API_UPSTREAM:-}"

WEB_APP_HOST="${WEB_APP_HOST:-127.0.0.1}"
WEB_APP_PORT="${WEB_APP_PORT:-9030}"

export LEADER_RUNTIME_ROOT
export WEB_APP_ROOT

api_upstream_args=()
if [[ -n "${LEADER_API_UPSTREAM}" ]]; then
    api_upstream_args+=(--api-upstream "${LEADER_API_UPSTREAM}")
fi

exec "${PYTHON_BIN}" -m web_app.webserver --host "${WEB_APP_HOST}" --port "${WEB_APP_PORT}" --root "${WEB_APP_ROOT}" "${api_upstream_args[@]}"
