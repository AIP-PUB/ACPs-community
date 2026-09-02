#!/usr/bin/env bash
# 共享 prep helper。依赖 doctor-lib.sh 已被 source。

set -euo pipefail

_dev_infra_prep_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! declare -F ensure_dotenv >/dev/null 2>&1 || ! declare -F ensure_uv_sync >/dev/null 2>&1 || ! declare -F ensure_hooks >/dev/null 2>&1; then
    # shellcheck disable=SC1091
    source "${_dev_infra_prep_lib_dir}/doctor-lib.sh"
fi

run_default_dotenv_generator() {
    cp .env.example .env
}

run_default_hooks_installer() {
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        echo "[INFO]  当前目录不是 Git 仓库，跳过 Git hooks 安装。"
        return
    fi

    run_uv_with_mutating_cache uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
}

ensure_dotenv_ready() {
    if declare -F generate_project_dotenv >/dev/null 2>&1; then
        ensure_dotenv
        return
    fi

    generate_project_dotenv() {
        run_default_dotenv_generator
    }
    ensure_dotenv
    unset -f generate_project_dotenv
}

ensure_uv_synced() {
    ensure_uv_sync
}

ensure_git_hooks_ready() {
    ensure_hooks
}
