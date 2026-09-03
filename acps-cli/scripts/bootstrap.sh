#!/usr/bin/env bash

set -eu -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${ACPS_CLI_BOOTSTRAP_PYTHON:-}"

if [[ -z "${PYTHON_BIN}" ]]; then
    if [[ -x "${RUNTIME_DIR}/.venv/bin/python" ]]; then
        PYTHON_BIN="${RUNTIME_DIR}/.venv/bin/python"
    else
        PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
    fi
fi

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[bootstrap] ERROR: 未找到可用 Python；请先创建 .venv 或安装 python3" >&2
    exit 1
fi

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/bootstrap_runtime.py" "$@"
