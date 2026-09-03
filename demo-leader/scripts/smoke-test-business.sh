#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

PYTHON_BIN="${PYTHON_BIN:-${BASE_DIR}/.venv/bin/python}"
ENV_FILE="${ENV_FILE:-${BASE_DIR}/.env}"
CONFIG_FILE="${LEADER_CONFIG_FILE:-${BASE_DIR}/leader/config.toml}"

resolve_http_host() {
    local configured_host="$1"
    case "${configured_host}" in
        ""|"0.0.0.0"|"::"|"[::]")
            printf '%s\n' "localhost"
            ;;
        *)
            printf '%s\n' "${configured_host}"
            ;;
    esac
}

if [[ ! -x "${PYTHON_BIN}" ]]; then
    err "未找到可执行 Python: ${PYTHON_BIN}"
    exit 1
fi

source_env_file "${ENV_FILE}"
require_file_exists "${CONFIG_FILE}" "leader/config.toml"

leader_host_value="${LEADER_API_HOST:-$(extract_toml_section_string_value "uvicorn" "host" "${CONFIG_FILE}")}"
leader_port_value="${LEADER_API_PORT:-$(extract_toml_section_integer_value "uvicorn" "port" "${CONFIG_FILE}")}"
leader_host="$(resolve_http_host "${leader_host_value:-0.0.0.0}")"
export API_BASE_URL="${API_BASE_URL:-http://${leader_host}:${leader_port_value:-9011}/api/v1}"

log "api base: ${API_BASE_URL}"
exec "${PYTHON_BIN}" "${SCRIPT_DIR}/smoke/business.py"
