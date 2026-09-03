# 共享 test 配方 — 统一提供公开 test 入口，并负责 e2e profile 编排。

test action='all' *args:
    #!/usr/bin/env bash
    set -eu -o pipefail

    action="{{ action }}"
    args_string="{{ args }}"
    project_hook_lib="{{ project_just_hook_lib }}"
    test_checks="{{ test_checks }}"
    test_bootstrap_checks="{{ test_bootstrap_checks }}"
    test_profile="{{ test_profile }}"
    test_e2e_profiles="{{ test_e2e_profiles }}"

    export project_name="{{project_name}}"

    source "../acps-infra/dev-infra/just/doctor-lib.sh"
    source "../acps-infra/dev-infra/just/bootstrap-lib.sh"

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

    dispatch_project_recipe_test() {
        local test_action="$1"
        shift

        if justfile_declares_recipe "_project_test"; then
            run_recipe_with_optional_args "_project_test" "${test_action}" "$@"
            return
        fi

        echo "[ERROR] test_profile=project-recipe 但未找到 _project_test。" >&2
        exit 2
    }

    dispatch_project_hook_test() {
        local test_action="$1"
        shift

        load_project_hook_lib
        if ! declare -F run_project_test_action >/dev/null 2>&1; then
            echo "[ERROR] test_profile=project 但未实现 run_project_test_action。" >&2
            exit 2
        fi

        run_project_test_action "${test_action}" "$@"
    }

    dispatch_test_action() {
        local test_action="$1"
        shift

        case "${test_profile}" in
            project-recipe)
                dispatch_project_recipe_test "${test_action}" "$@"
                ;;
            project)
                dispatch_project_hook_test "${test_action}" "$@"
                ;;
            *)
                echo "[ERROR] 当前共享 test.just 仅支持 project / project-recipe，收到：${test_profile}" >&2
                exit 2
                ;;
        esac
    }

    dispatch_test_action_checked() {
        local exit_code=0

        set +e
        dispatch_test_action "$@"
        exit_code="$?"
        set -e

        return "${exit_code}"
    }

    project_supports_test_action() {
        local test_action="$1"

        case "${test_action}" in
            help|check|bootstrap|e2e|unit|integration|coverage|all)
                return 0
                ;;
        esac

        load_project_hook_lib
        if declare -F supports_project_test_action >/dev/null 2>&1; then
            if supports_project_test_action "${test_action}"; then
                return 0
            fi
        fi

        return 1
    }

    run_test_check() {
        if [[ -n "${test_checks}" ]]; then
            load_project_hook_lib
            run_scope_doctor test "${test_checks}"
            return
        fi

        if [[ "${#requested_args[@]}" -gt 0 ]]; then
            dispatch_test_action check "${requested_args[@]}"
        else
            dispatch_test_action check
        fi
    }

    run_test_bootstrap() {
        if [[ -n "${test_bootstrap_checks}" ]]; then
            load_project_hook_lib
            bootstrap_scope test "${test_bootstrap_checks}"
            return
        fi

        if [[ "${#requested_args[@]}" -gt 0 ]]; then
            dispatch_test_action bootstrap "${requested_args[@]}"
        else
            dispatch_test_action bootstrap
        fi
    }

    run_test_e2e_profile() {
        local profile="$1"
        shift
        local legacy_action=""

        load_project_hook_lib
        if declare -F run_project_test_e2e_profile >/dev/null 2>&1; then
            run_project_test_e2e_profile "${profile}" "$@"
            return
        fi

        legacy_action="e2e"
        if [[ "${profile}" != "local" ]]; then
            legacy_action="${profile}-e2e"
        fi

        dispatch_test_action "${legacy_action}" "$@"
    }

    run_test_e2e_profiles() {
        local profile=""

        if [[ -z "${test_e2e_profiles}" ]] || [[ "${test_e2e_profiles}" == "local" ]]; then
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                run_test_e2e_profile local "${requested_args[@]}"
            else
                run_test_e2e_profile local
            fi
            return
        fi

        for profile in ${test_e2e_profiles}; do
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                run_test_e2e_profile "${profile}" "${requested_args[@]}"
            else
                run_test_e2e_profile "${profile}"
            fi
        done
    }

    is_reserved_legacy_e2e_action() {
        local test_action="$1"
        local profile=""

        if [[ -z "${test_e2e_profiles}" ]] || [[ "${test_e2e_profiles}" == "local" ]]; then
            return 1
        fi

        for profile in ${test_e2e_profiles}; do
            if [[ "${profile}" == "local" ]]; then
                continue
            fi
            if [[ "${test_action}" == "${profile}-e2e" ]]; then
                return 0
            fi
        done

        return 1
    }

    case "${action}" in
        help|"")
            just _shared_project_help test
            ;;
        check)
            run_test_check
            ;;
        bootstrap)
            run_test_bootstrap
            ;;
        e2e)
            run_test_e2e_profiles
            ;;
        unit|integration|coverage|all)
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                dispatch_test_action "${action}" "${requested_args[@]}"
            else
                dispatch_test_action "${action}"
            fi
            ;;
        coverage-report)
            if ! project_supports_test_action "${action}"; then
                echo "[ERROR] 未知 test action: ${action}" >&2
                exit 2
            fi

            exit_code=0
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                dispatch_test_action_checked "${action}" "${requested_args[@]}" || exit_code="$?"
            else
                dispatch_test_action_checked "${action}" || exit_code="$?"
            fi
            if [[ "${exit_code}" -eq 127 ]]; then
                echo "[ERROR] 当前项目声明了 test ${action}，但未实现对应动作。" >&2
                exit 2
            fi
            exit "${exit_code}"
            ;;
        *)
            if is_reserved_legacy_e2e_action "${action}"; then
                echo "[ERROR] 未知 test action: ${action}" >&2
                exit 2
            fi

            if ! project_supports_test_action "${action}"; then
                echo "[ERROR] 未知 test action: ${action}" >&2
                exit 2
            fi

            exit_code=0
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                dispatch_test_action_checked "${action}" "${requested_args[@]}" || exit_code="$?"
            else
                dispatch_test_action_checked "${action}" || exit_code="$?"
            fi
            if [[ "${exit_code}" -eq 127 ]]; then
                echo "[ERROR] 当前项目声明了 test ${action}，但未实现对应动作。" >&2
                exit 2
            fi
            exit "${exit_code}"
            ;;
    esac
