# 共享 dev 配方 — 统一提供公开 dev 入口，并按 profile 分发到项目实现。

dev action='help' *args:
    #!/usr/bin/env bash
    set -eu -o pipefail

    action="{{action}}"
    args_string="{{args}}"
    project_kind="{{project_kind}}"
    project_hook_lib="{{project_just_hook_lib}}"
    dev_runtime_profile="{{dev_runtime_profile}}"
    dev_runtime_actions="{{dev_runtime_actions}}"
    dev_checks="{{dev_checks}}"
    dev_bootstrap_checks="{{dev_bootstrap_checks}}"

    export project_name="{{project_name}}"

    source "../acps-infra/dev-infra/just/doctor-lib.sh"
    source "../acps-infra/dev-infra/just/bootstrap-lib.sh"

    requested_args=()
    if [[ -n "${args_string}" ]]; then
        read -r -a requested_args <<< "${args_string}"
    fi

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

    has_runtime_action() {
        local target="$1"
        [[ " ${dev_runtime_actions} " == *" ${target} "* ]]
    }

    justfile_declares_recipe() {
        local recipe_name="$1"

        if [[ ! -f Justfile ]]; then
            return 1
        fi

        grep -Eq "^${recipe_name}(\\s|:|$)" Justfile
    }

    run_recipe_with_optional_args() {
        local recipe_name="$1"
        shift

        if [[ $# -gt 0 ]]; then
            just "${recipe_name}" "$@"
        else
            just "${recipe_name}"
        fi
    }

    dispatch_legacy_dev_check() {
        if justfile_declares_recipe "_project_dev_doctor"; then
            just _project_dev_doctor
            return
        fi

        if justfile_declares_recipe "_dev_doctor"; then
            just _dev_doctor
            return
        fi

        echo "[ERROR] 当前项目未声明共享 dev check 所需实现；请补充 dev_checks 或隐藏 check recipe。" >&2
        exit 2
    }

    dispatch_legacy_dev_bootstrap() {
        if justfile_declares_recipe "_project_dev_bootstrap"; then
            just _project_dev_bootstrap
            return
        fi

        if justfile_declares_recipe "_project_dev_runtime"; then
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                just _project_dev_runtime bootstrap "${requested_args[@]}"
            else
                just _project_dev_runtime bootstrap
            fi
            return
        fi

        if justfile_declares_recipe "_dev_runtime"; then
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                just _dev_runtime bootstrap "${requested_args[@]}"
            else
                just _dev_runtime bootstrap
            fi
            return
        fi

        if justfile_declares_recipe "_bootstrap_common"; then
            just _bootstrap_common
            return
        fi

        echo "[ERROR] 当前项目未声明共享 dev bootstrap 所需实现；请补充 dev_bootstrap_checks 或隐藏 bootstrap recipe。" >&2
        exit 2
    }

    dispatch_runtime_action() {
        local runtime_action="$1"
        shift

        case "${dev_runtime_profile}" in
            none)
                echo "[ERROR] ${project_kind} 项目不提供 dev ${runtime_action}；请使用 just dev check / just dev bootstrap 完成联调准备。" >&2
                exit 2
                ;;
            project)
                load_project_hook_lib
                if ! declare -F run_project_dev_runtime >/dev/null 2>&1; then
                    echo "[ERROR] dev_runtime_profile=project 但未实现 run_project_dev_runtime。" >&2
                    exit 2
                fi
                run_project_dev_runtime "${runtime_action}" "$@"
                ;;
            project-recipe)
                if justfile_declares_recipe "_project_dev_runtime"; then
                    run_recipe_with_optional_args "_project_dev_runtime" "${runtime_action}" "$@"
                    return
                fi

                if justfile_declares_recipe "_dev_runtime"; then
                    run_recipe_with_optional_args "_dev_runtime" "${runtime_action}" "$@"
                    return
                fi

                echo "[ERROR] dev_runtime_profile=project-recipe 但未找到 _project_dev_runtime / _dev_runtime。" >&2
                exit 2
                ;;
            uvicorn)
                if justfile_declares_recipe "_dev_runtime"; then
                    run_recipe_with_optional_args "_dev_runtime" "${runtime_action}" "$@"
                    return
                fi

                run_recipe_with_optional_args "_shared_dev_runtime_uvicorn" "${runtime_action}" "$@"
                return
                ;;
            *)
                echo "[ERROR] 未知 dev_runtime_profile：${dev_runtime_profile}" >&2
                exit 2
                ;;
        esac
    }

    run_dev_check() {
        if [[ -n "${dev_checks}" ]]; then
            load_project_hook_lib
            run_scope_doctor dev "${dev_checks}"
            return
        fi

        dispatch_legacy_dev_check
    }

    run_dev_bootstrap() {
        if [[ -n "${dev_bootstrap_checks}" ]]; then
            load_project_hook_lib
            bootstrap_scope dev "${dev_bootstrap_checks}"
            return
        fi

        if [[ "${dev_runtime_profile}" == "uvicorn" ]]; then
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                just _shared_dev_runtime_uvicorn bootstrap "${requested_args[@]}"
            else
                just _shared_dev_runtime_uvicorn bootstrap
            fi
            return
        fi

        dispatch_legacy_dev_bootstrap
    }

    case "${action}" in
        help|"")
            just _shared_project_help dev
            ;;
        check)
            run_dev_check
            ;;
        bootstrap)
            run_dev_bootstrap
            ;;
        start|stop|status|logs|restart|smoke)
            if ! has_runtime_action "${action}"; then
                echo "[ERROR] 当前项目未声明 dev ${action} 能力。" >&2
                exit 2
            fi
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                dispatch_runtime_action "${action}" "${requested_args[@]}"
            else
                dispatch_runtime_action "${action}"
            fi
            ;;
        *)
            echo "[ERROR] 未知 dev action: ${action}" >&2
            exit 2
            ;;
    esac
