#!/usr/bin/env bash
# 共享证书准备 helper。依赖 doctor-lib.sh 已被 source。

set -euo pipefail

check_cert_group_files_ready() {
    local check_id="$1"
    local owner="${2:-project}"
    shift 2
    local existing=0
    local total=0
    local path=""

    if [[ "$#" -eq 0 ]]; then
        emit_check_result "${check_id}" invalid error "${owner}" "证书组未声明任何目标文件。" "在项目 hook 中传入至少一个证书路径。"
        return
    fi

    for path in "$@"; do
        total=$((total + 1))
        if [[ -f "${path}" ]]; then
            existing=$((existing + 1))
        fi
    done

    if [[ "${existing}" -eq "${total}" ]]; then
        emit_check_result "${check_id}" ready info "${owner}" "证书组已完整就绪（${existing}/${total}）。" ""
        return
    fi

    if [[ "${existing}" -eq 0 ]]; then
        emit_check_result "${check_id}" missing error "${owner}" "证书组缺失（0/${total}）。" "执行 just prep certs。"
        return
    fi

    emit_check_result "${check_id}" invalid error "${owner}" "证书组处于半生成状态（${existing}/${total}）。" "清理残留文件后重试，必要时执行显式 reset。"
}

ensure_cert_group_files_ready() {
    local check_id="$1"
    local build_callback="$2"
    shift 2
    local results_file=""

    results_file="$(mktemp)"
    : > "${results_file}"
    _check_results_file="${results_file}"
    check_cert_group_files_ready "${check_id}" project "$@"
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

    "${build_callback}"
    : > "${results_file}"
    _check_results_file="${results_file}"
    check_cert_group_files_ready "${check_id}" project "$@"
    render_results_file bootstrap "${results_file}"
    results_file_is_ready "${results_file}"
    local exit_code=$?
    rm -f "${results_file}"
    return "${exit_code}"
}
