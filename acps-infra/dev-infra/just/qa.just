# 共享 qa 配方 — 统一提供公开 qa 入口，并将项目差异委托给 hook。

qa action='help':
    #!/usr/bin/env bash
    set -eu -o pipefail

    action="{{action}}"
    project_hook_lib="{{project_just_hook_lib}}"

    source "../acps-infra/dev-infra/just/doctor-lib.sh"

    export UV_CACHE_DIR="${UV_CACHE_DIR:-$(resolve_mutating_uv_cache_dir)}"
    export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$(resolve_mutating_uv_python_install_dir)}"

    load_project_hook_lib() {
        if [[ -z "${project_hook_lib}" ]]; then
            return
        fi

        if [[ ! -f "${project_hook_lib}" ]]; then
            echo "[ERROR] 项目 hook 文件不存在：${project_hook_lib}" >&2
            exit 1
        fi

        # shellcheck source=/dev/null
        source "${project_hook_lib}"
    }

    try_project_qa_action() {
        local exit_code=0

        if ! declare -F run_project_qa_action >/dev/null 2>&1; then
            return 127
        fi

        set +e
        run_project_qa_action "$@"
        exit_code="$?"
        set -e

        return "${exit_code}"
    }

    run_project_qa_action_checked() {
        local exit_code=0

        set +e
        try_project_qa_action "$@"
        exit_code="$?"
        set -e

        return "${exit_code}"
    }

    run_qa_pip_audit() {
        local tmpfile=""
        local audit_runner=""

        tmpfile="$(mktemp)"
        trap 'rm -f "${tmpfile:-}"' RETURN

        run_uv_with_mutating_cache uv export --locked --format requirements.txt --all-groups --no-hashes --no-header --no-emit-project --prune pip-audit --prune pip-api --prune pip --output-file "${tmpfile}"

        if run_uv_with_mutating_cache uv run pip-audit --version >/dev/null 2>&1; then
            audit_runner="uv-run"
        else
            audit_runner="uvx"
        fi

        case "${audit_runner}" in
            uv-run)
                run_uv_with_mutating_cache uv run pip-audit -r "${tmpfile}" --progress-spinner off --no-deps --disable-pip
                ;;
            uvx)
                run_uv_with_mutating_cache uvx pip-audit -r "${tmpfile}" --progress-spinner off --no-deps --disable-pip
                ;;
        esac
    }

    load_project_hook_lib

    case "${action}" in
        help|"")
            just _shared_project_help qa
            ;;
        precommit)
            run_uv_with_mutating_cache uv run pre-commit run --all-files
            ;;
        pip-audit)
            exit_code=0
            run_project_qa_action_checked "${action}" || exit_code="$?"
            if [[ "${exit_code}" -eq 0 ]]; then
                exit 0
            fi
            if [[ "${exit_code}" -ne 127 ]]; then
                exit "${exit_code}"
            fi
            run_qa_pip_audit
            ;;
        *)
            echo "[ERROR] 未知 qa action: ${action}" >&2
            exit 2
            ;;
    esac
