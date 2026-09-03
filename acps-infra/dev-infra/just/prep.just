# 共享 prep 配方 — 统一提供公开 prep 入口，并将项目差异委托给 hook。

prep action='help' *args:
    #!/usr/bin/env bash
    set -eu -o pipefail

    action="{{action}}"
    args_string="{{args}}"
    project_hook_lib="{{project_just_hook_lib}}"
    export PROJECT_PYTHON_VERSION="{{project_python_version}}"

    source "../acps-infra/dev-infra/just/prep-lib.sh"

    export UV_CACHE_DIR="${UV_CACHE_DIR:-$(resolve_mutating_uv_cache_dir)}"
    export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$(resolve_mutating_uv_python_install_dir)}"

    requested_args=()
    if [[ -n "${args_string}" ]]; then
        read -r -a requested_args <<< "${args_string}"
    fi

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

    try_project_prep_action() {
        local exit_code=0

        if ! declare -F run_project_prep_action >/dev/null 2>&1; then
            return 127
        fi

        set +e
        run_project_prep_action "$@"
        exit_code="$?"
        set -e

        return "${exit_code}"
    }

    run_project_prep_action_checked() {
        local exit_code=0

        set +e
        try_project_prep_action "$@"
        exit_code="$?"
        set -e

        return "${exit_code}"
    }

    case "${action}" in
        help|"")
            just _shared_project_help prep
            ;;
        env)
            load_project_hook_lib
            ensure_dotenv_ready
            ;;
        sync)
            load_project_hook_lib
            ensure_uv_synced
            ;;
        hooks)
            load_project_hook_lib
            ensure_git_hooks_ready
            ;;
        package)
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                just package bootstrap "${requested_args[@]}"
            else
                just package bootstrap
            fi
            ;;
        *)
            load_project_hook_lib
            exit_code=0
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                run_project_prep_action_checked "${action}" "${requested_args[@]}" || exit_code="$?"
            else
                run_project_prep_action_checked "${action}" || exit_code="$?"
            fi
            if [[ "${exit_code}" -eq 0 ]]; then
                exit 0
            fi
            if [[ "${exit_code}" -eq 127 ]]; then
                echo "[ERROR] 未知 prep action: ${action}" >&2
                exit 2
            fi
            exit "${exit_code}"
            ;;
    esac
