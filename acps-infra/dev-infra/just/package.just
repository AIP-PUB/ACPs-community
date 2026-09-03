# 共享 package 配方 — 由各 Python 项目 Justfile import。

package action='help' *args:
    #!/usr/bin/env bash
    set -eu -o pipefail
    source "../acps-infra/dev-infra/just/doctor-lib.sh"
    source "../acps-infra/dev-infra/just/package-lib.sh"
    action="{{action}}"
    args_string="{{args}}"

    export PACKAGE_SCOPE_ENABLED="{{package_scope_enabled}}"
    export PACKAGE_PROJECT_DIR="${PWD}"
    export PROJECT_JUST_HOOK_LIB="{{project_just_hook_lib}}"
    export PACKAGE_SCRIPT_PATH="{{package_script_path}}"
    export PACKAGE_PYTHON_VERSION="{{package_python_version}}"
    export PACKAGE_REQUIRED_PATHS="{{package_required_paths}}"
    export PACKAGE_SIBLING_REPOS="{{package_sibling_repos}}"
    export PACKAGE_OUTPUT_DIR="{{package_output_dir}}"
    export PACKAGE_RELEASE_NAME_PREFIX="{{package_release_name_prefix}}"
    export PACKAGE_WHEEL_DISTRIBUTION="{{package_wheel_distribution}}"
    export PACKAGE_BUILD_MODE="{{package_build_mode}}"
    export PACKAGE_REQUIRES_BUILD_ENV="{{package_requires_build_env}}"
    export PACKAGE_BUILD_ENV_DIR="${PACKAGE_BUILD_ENV_DIR:-.package-build-venv}"
    export PROJECT_PYTHON_VERSION="${PROJECT_PYTHON_VERSION:-{{project_python_version}}}"

    requested_args=()
    if [[ -n "${args_string}" ]]; then
        read -r -a requested_args <<< "${args_string}"
    fi

    print_help() {
        if [[ "${PACKAGE_SCOPE_ENABLED}" != "1" ]]; then
            echo "用法："
            echo "  just package [action] [args...]"
            echo "  不传 action 时等同于：just package help"
            echo ""
            echo "package：当前项目未启用 package scope"
            echo "  help"
            echo "  just package check"
            echo "      只读检查 package 入口是否可用，并提示 remediation"
            return
        fi

        echo "用法："
        echo "  just package [action] [args...]"
        echo "  不传 action 时等同于：just package help"
        echo ""
        echo "package 发布物构建（应用发布包方案）："
        echo "  help"
        echo "      显示 package 帮助"
        echo "  just package check [--python-version <version>]"
        echo "      只读检查运行包构建前置条件"
        echo "  just package bootstrap [--python-version <version>]"
        echo "      准备 package 前置条件（只修复可自动准备的本地条件）"
        echo "  just package wheel [--python-version <version>]"
        echo "      构建平台无关的应用薄包 {app}-wheel-{version}.tar.gz："
        echo "      自身 wheel + requirements-runtime.lock（带 hash） + 运行期资源 +"
        echo "      runtime-package.toml + checksums.txt，不包含 wheelhouse/。"
        echo "      由 acps-infra 采集后在目标平台装配为应用发布包，见"
        echo "      acps-infra/release/app-packaging/collect-app-release-kit.sh 与 app-packaging/assemble.sh。"
    }

    configure_package_args() {
        local command_args=()
        local index=0

        while [[ "${index}" -lt "${#requested_args[@]}" ]]; do
            case "${requested_args[${index}]}" in
                --python-version)
                    index=$((index + 1))
                    if [[ "${index}" -ge "${#requested_args[@]}" ]]; then
                        echo "[ERROR] --python-version 需要一个值。" >&2
                        exit 2
                    fi
                    export PACKAGE_PYTHON_VERSION="${requested_args[${index}]}"
                    command_args+=("--python-version" "${requested_args[${index}]}")
                    ;;
                *)
                    echo "[ERROR] 未知 package 参数: ${requested_args[${index}]}" >&2
                    exit 2
                    ;;
            esac
            index=$((index + 1))
        done

        PACKAGE_COMMAND_ARGS=()
        if [[ "${#command_args[@]}" -gt 0 ]]; then
            PACKAGE_COMMAND_ARGS=("${command_args[@]}")
        fi
    }

    configure_package_args

    package_checks="$(build_package_check_list)"
    package_bootstrap_checks="${package_checks}"

    case "${action}" in
        help|"")
            print_help
            ;;
        check)
            run_package_doctor "${package_checks}"
            ;;
        bootstrap)
            run_package_bootstrap "${package_bootstrap_checks}"
            ;;
        wheel)
            run_package_bootstrap "${package_bootstrap_checks}"
            if [[ "${PACKAGE_SCOPE_ENABLED}" != "1" ]]; then
                echo "[ERROR] 当前项目未启用 package scope。" >&2
                echo "         remediation: 补齐 package 脚本与声明，或移除 package 入口。" >&2
                exit 1
            fi

            command=(bash "${PACKAGE_SCRIPT_PATH}")
            if [[ "${#PACKAGE_COMMAND_ARGS[@]}" -gt 0 ]]; then
                command+=("${PACKAGE_COMMAND_ARGS[@]}")
            fi
            "${command[@]}"
            ;;
        *)
            echo "[ERROR] 未知 package action: ${action}" >&2
            exit 2
            ;;
    esac
