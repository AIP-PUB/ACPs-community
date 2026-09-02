#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

PYTHON_BIN="${PYTHON_BIN:-${BASE_DIR}/.venv/bin/python}"
ENV_FILE="${ENV_FILE:-${BASE_DIR}/.env}"
LEADER_RUNTIME_ROOT="${LEADER_RUNTIME_ROOT:-${BASE_DIR}}"
LEADER_SCENARIO_ROOT="${LEADER_SCENARIO_ROOT:-${LEADER_RUNTIME_ROOT}/leader/scenario}"
CONFIG_FILE="${LEADER_CONFIG_FILE:-${LEADER_RUNTIME_ROOT}/leader/config.toml}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    err "未找到可执行 Python: ${PYTHON_BIN}"
    exit 1
fi

source_env_file "${ENV_FILE}"
require_file_exists "${CONFIG_FILE}" "leader/config.toml"

LEADER_API_HOST="${LEADER_API_HOST:-$(extract_toml_section_string_value "uvicorn" "host" "${CONFIG_FILE}")}"
LEADER_API_PORT="${LEADER_API_PORT:-$(extract_toml_section_integer_value "uvicorn" "port" "${CONFIG_FILE}")}"
UVICORN_RELOAD_VALUE="${UVICORN_RELOAD:-$(extract_toml_section_boolean_value "uvicorn" "reload" "${CONFIG_FILE}")}"

LEADER_API_HOST="${LEADER_API_HOST:-0.0.0.0}"
LEADER_API_PORT="${LEADER_API_PORT:-9031}"

leader_package_dir="$(${PYTHON_BIN} -c 'import pathlib; import leader; print(pathlib.Path(leader.__file__).resolve().parent)')"
if [[ -z "${leader_package_dir}" ]]; then
    err "无法解析已安装的 leader 包路径"
    exit 1
fi

export LEADER_RUNTIME_ROOT
export LEADER_SCENARIO_ROOT
export PYTHONPATH="${leader_package_dir}${PYTHONPATH:+:${PYTHONPATH}}"

uvicorn_args=(
    -m leader.main
)

if [[ "${UVICORN_RELOAD_VALUE:-false}" == "true" ]]; then
    export UVICORN_RELOAD="true"
fi

export LEADER_API_HOST
export LEADER_API_PORT
exec "${PYTHON_BIN}" "${uvicorn_args[@]}"
