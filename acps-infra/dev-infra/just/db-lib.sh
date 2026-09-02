#!/usr/bin/env bash
# 共享数据库/迁移 helper。依赖 doctor-lib.sh 已被 source。

set -euo pipefail

alembic_env_prefix_string() {
    local env_prefix="${1:-}"
    printf '%s' "${env_prefix}"
}

check_alembic_at_head() {
    local check_id="$1"
    local env_prefix="${2:-}"
    local current_output=""
    local heads_output=""
    local current_revs=""
    local head_revs=""
    local tmpfile=""
    local runner=()

    tmpfile="$(mktemp)"
    runner=(bash -lc)

    if ! command -v uv >/dev/null 2>&1; then
        emit_check_result "${check_id}" blocked error external "未找到 uv，无法检查 Alembic 状态。" "先安装 uv 并执行 just prep sync。"
        rm -f "${tmpfile}"
        return
    fi

    if ! current_output="$("${runner[@]}" "$(alembic_env_prefix_string "${env_prefix}") uv run --no-sync alembic current" 2>"${tmpfile}")"; then
        emit_check_result "${check_id}" missing error project "无法读取 Alembic current 状态。" "执行 just prep migrate。"
        rm -f "${tmpfile}"
        return
    fi

    if ! heads_output="$("${runner[@]}" "$(alembic_env_prefix_string "${env_prefix}") uv run --no-sync alembic heads" 2>"${tmpfile}")"; then
        emit_check_result "${check_id}" blocked error external "无法读取 Alembic heads 状态。" "检查 Alembic 配置和 Python 环境。"
        rm -f "${tmpfile}"
        return
    fi

    current_revs="$(printf '%s\n' "${current_output}" | awk 'NF {print $1}' | sort | tr '\n' ' ')"
    head_revs="$(printf '%s\n' "${heads_output}" | awk 'NF {print $1}' | sort | tr '\n' ' ')"

    if [[ -z "${head_revs// }" ]]; then
        emit_check_result "${check_id}" blocked error external "Alembic heads 为空，无法判断迁移状态。" "检查 migration 目录与 Alembic 配置。"
        rm -f "${tmpfile}"
        return
    fi

    if [[ "${current_revs}" == "${head_revs}" ]] && [[ -n "${current_revs// }" ]]; then
        emit_check_result "${check_id}" ready info project "Alembic 已位于 head。" ""
    else
        emit_check_result "${check_id}" stale error project "Alembic 未位于 head。" "执行 just prep migrate。"
    fi
    rm -f "${tmpfile}"
}

ensure_alembic_at_head() {
    local check_id="$1"
    local env_prefix="${2:-}"
    local fix_command="${3:-uv run alembic upgrade head}"
    local results_file=""
    local runner=()

    results_file="$(mktemp)"
    runner=(bash -lc)

    : > "${results_file}"
    _check_results_file="${results_file}"
    check_alembic_at_head "${check_id}" "${env_prefix}"
    if results_file_is_ready "${results_file}"; then
        render_results_file bootstrap "${results_file}"
        rm -f "${results_file}"
        return 0
    fi

    render_results_file doctor "${results_file}"
    if ! results_file_only_fixable_errors "${results_file}"; then
        rm -f "${results_file}"
        return 1
    fi

    "${runner[@]}" "$(alembic_env_prefix_string "${env_prefix}") ${fix_command}"

    : > "${results_file}"
    _check_results_file="${results_file}"
    check_alembic_at_head "${check_id}" "${env_prefix}"
    render_results_file bootstrap "${results_file}"
    results_file_is_ready "${results_file}"
    local exit_code=$?
    rm -f "${results_file}"
    return "${exit_code}"
}
