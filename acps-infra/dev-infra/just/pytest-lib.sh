#!/usr/bin/env bash
# 共享 pytest 辅助函数 — 由各项目 Justfile test 配方 source。
#
#   source "../acps-infra/dev-infra/just/pytest-lib.sh"
#
# 函数：
#   find_free_port
#   validate_test_e2e_base_url <url>   # 默认允许 http/https；mq-auth 等可设 TEST_E2E_ALLOWED_SCHEMES=https
#   run_scoped_pytest <default_path> [pytest args...]
#   run_requested_tests <default_path> <test_server_base_url> [requested_args...]

set -euo pipefail

_dev_infra_pytest_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! declare -F run_uv_with_mutating_cache >/dev/null 2>&1; then
    # shellcheck disable=SC1091
    source "${_dev_infra_pytest_lib_dir}/doctor-lib.sh"
fi

find_free_port() {
    python3 -c 'import socket; sock = socket.socket(); sock.bind(("127.0.0.1", 0)); print(sock.getsockname()[1]); sock.close()'
}

validate_test_e2e_base_url() {
    local base_url="$1"
    local allowed_schemes="${TEST_E2E_ALLOWED_SCHEMES:-http,https}"
    local example_url="${TEST_E2E_BASE_URL_EXAMPLE:-http://127.0.0.1:19123}"

    if ! TEST_E2E_ALLOWED_SCHEMES="${allowed_schemes}" python3 -c '
import os, sys
from urllib.parse import urlsplit

base_url = sys.argv[1]
allowed = {s.strip() for s in os.environ["TEST_E2E_ALLOWED_SCHEMES"].split(",") if s.strip()}
parts = urlsplit(base_url)
host = parts.hostname or ""
ok = (
    parts.scheme in allowed
    and host in {"127.0.0.1", "localhost"}
    and parts.port is not None
    and parts.path in {"", "/"}
)
raise SystemExit(0 if ok else 1)
' "${base_url}"; then
        echo "[ERROR] TEST_E2E_BASE_URL 必须是指向本地临时测试实例的完整 URL，例如：${example_url}。" >&2
        exit 1
    fi
}

run_scoped_pytest() {
    local default_path="$1"
    shift
    local selection=("$@")

    while [[ "${#selection[@]}" -gt 0 ]] && [[ "${selection[0]}" == "--" ]]; do
        selection=("${selection[@]:1}")
    done

    if [[ "${#selection[@]}" -eq 0 ]]; then
        APP_ENV=testing run_uv_with_mutating_cache uv run pytest "${default_path}"
        return
    fi

    if [[ "${selection[0]}" == -* ]]; then
        APP_ENV=testing run_uv_with_mutating_cache uv run pytest "${default_path}" "${selection[@]}"
        return
    fi

    APP_ENV=testing run_uv_with_mutating_cache uv run pytest "${selection[@]}"
}

run_requested_tests() {
    local default_path="$1"
    local test_server_base_url="${2:-}"
    if (( $# >= 2 )); then
        shift 2
    else
        shift
    fi
    local requested_args=("$@")

    if [[ "${#requested_args[@]}" -gt 0 ]]; then
        TEST_E2E_BASE_URL="${test_server_base_url:-${TEST_E2E_BASE_URL:-}}" run_scoped_pytest "${default_path}" "${requested_args[@]}"
        return
    fi

    TEST_E2E_BASE_URL="${test_server_base_url:-${TEST_E2E_BASE_URL:-}}" run_scoped_pytest "${default_path}"
}
