#!/usr/bin/env bash

: "${leader_pid_file:=logs/leader.pid}"
: "${web_pid_file:=logs/static_web.pid}"
: "${leader_log_file:=logs/leader.log}"
: "${web_log_file:=logs/static_web.log}"

configure_project_package_runtime() {
    PACKAGE_RUNTIME_REQUIRED_PATHS=(
        .env.example
        README.md
        pyproject.toml
        leader/config.toml
        leader/atr
        leader/scenario
        web_app
        scripts/lib/common.sh
        scripts/smoke-test-business.sh
        scripts/smoke/business.py
        scripts/start-leader-api.sh
        scripts/start-web-ui.sh
    )

    PACKAGE_RUNTIME_SIBLING_REPOS=(
        acps-sdk
        acps-cli
    )

    PACKAGE_RUNTIME_BUNDLE_MAP=(
        ".env.example|.env.example|env_template"
        "README.md|README.md|doc"
        "leader/config.toml|leader/config.toml|config"
        "leader/atr|leader/atr|business_data"
        "leader/scenario|leader/scenario|business_data"
        "web_app|web_app|business_data"
        "scripts/lib/common.sh|scripts/lib/common.sh|other"
        "scripts/smoke-test-business.sh|scripts/smoke-test-business.sh|smoke_test"
        "scripts/smoke/business.py|scripts/smoke/business.py|other"
        "scripts/start-leader-api.sh|scripts/start-leader-api.sh|other"
        "scripts/start-web-ui.sh|scripts/start-web-ui.sh|other"
    )

    PACKAGE_RUNTIME_BUNDLE_EXCLUDE_MAP=(
        "${DEFAULT_BUNDLE_EXCLUDE_MAP[@]}"
        "._*"
        "*/._*"
    )

    PACKAGE_RUNTIME_CHMOD_PATHS=(
        "scripts/start-leader-api.sh"
        "scripts/start-web-ui.sh"
        "scripts/smoke-test-business.sh"
    )

    PACKAGE_RUNTIME_REMOVE_PATTERNS=()

    # demo-leader 对外是两个独立进程（Leader API + Web UI 静态前端），分别对应独立的
    # 启动脚本，因此声明两个 [[components]]（源设计 §4.3）。
    PACKAGE_RUNTIME_COMPONENTS=(
        "demo-leader-api|python-service|scripts/start-leader-api.sh|9031|http://127.0.0.1:9031/api/v1/health||leader/config.toml"
        "demo-leader-web|static-web|scripts/start-web-ui.sh|9030|||"
    )
    PACKAGE_RUNTIME_INTERNAL_WHEELS=(acps-sdk acps-cli)
    PACKAGE_RUNTIME_EXTERNAL_COMPONENTS=(rabbitmq)
}

run_project_package_filter_requirements() {
    local input_file="$1"
    local output_file="$2"

    grep -Ev '^-e \.\./acps-(cli|sdk)$' "${input_file}" > "${output_file}"
}

run_project_package_post_stage() {
    local staging_dir="$1"

    find "${staging_dir}/leader/atr" -type f \( -name '*.pem' -o -name '*.key' -o -name '*.csr' -o -name '*.srl' \) -delete
}

run_project_prep_action() {
    local action="$1"
    shift || true

    extract_aic_from_acs() {
        local acs_path="$1"
        UV_MANAGED_PYTHON=1 run_uv_with_mutating_cache uv run --python 3.14 --managed-python --no-project python -c 'import json, sys; from pathlib import Path; data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); aic = data.get("aic") or data.get("agentAic"); sys.exit(1) if not isinstance(aic, str) or not aic.strip() else None; print(aic.strip())' "${acs_path}"
    }

    case "${action}" in
        certs)
            local leader_aic=""

            if ! command -v openssl >/dev/null 2>&1; then
                echo "[ERROR] 未找到 openssl，just prep certs 依赖共享开发 PKI 脚本使用 openssl。" >&2
                exit 1
            fi

            if [[ ! -x "../acps-infra/dev-infra/dev-cert.sh" ]]; then
                echo "[ERROR] 未找到共享开发 PKI 脚本：../acps-infra/dev-infra/dev-cert.sh" >&2
                exit 1
            fi

            if ! leader_aic="$(extract_aic_from_acs leader/atr/acs.json)"; then
                echo "[ERROR] 无法从 leader/atr/acs.json 提取 Leader AIC。" >&2
                exit 1
            fi

            bash ../acps-infra/dev-infra/dev-cert.sh issue-leaf \
                --ca agent \
                --common-name "${leader_aic}" \
                --usage clientAuth \
                --cert-out "leader/atr/client.pem" \
                --key-out "leader/atr/client.key" \
                --bundle-out "leader/atr/trust-bundle.pem" \
                --relative-to "$PWD"

            for required_file in leader/atr/client.pem leader/atr/client.key leader/atr/trust-bundle.pem; do
                if [[ ! -f "${required_file}" ]]; then
                    echo "[ERROR] 共享开发 PKI 签发完成后仍缺少证书文件：${required_file}" >&2
                    exit 1
                fi
            done

            echo "[INFO]  共享开发 PKI 已按 leader/atr/acs.json 签发 Leader 本地 mTLS 开发证书。"
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_help_section() {
    local section="$1"

    case "${section}" in
        test-actions)
            echo "  api              运行进程内 API 测试；支持透传 pytest 参数"
            ;;
        prep)
            echo "  certs    基于 leader/atr/acs.json 声明并签发本地 mTLS 开发证书"
            ;;
        examples)
            echo "  just prep certs"
            ;;
    esac
}

load_demo_leader_common_helpers() {
    # shellcheck source=/dev/null
    source "scripts/lib/common.sh"
}

load_demo_leader_cert_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/cert-lib.sh"
}

load_demo_leader_pytest_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/pytest-lib.sh"
}

load_demo_leader_app_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/app-lib.sh"
}

demo_leader_extract_env_file_value() {
    local key="$1"
    local env_file="${2:-.env}"

    if [[ ! -f "${env_file}" ]]; then
        return 0
    fi

    awk -F= -v key="${key}" '
        $0 ~ /^[[:space:]]*#/ { next }
        $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
            sub(/^[^=]*=/, "", $0)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)
            print $0
            exit
        }
    ' "${env_file}"
}

demo_leader_normalize_bool() {
    local raw_value="${1:-}"
    local normalized=""

    normalized="$(printf '%s' "${raw_value}" | tr '[:upper:]' '[:lower:]')"
    case "${normalized}" in
        1|true|yes|on)
            printf 'true\n'
            ;;
        0|false|no|off|"")
            printf 'false\n'
            ;;
        *)
            printf 'false\n'
            ;;
    esac
}

demo_leader_resolve_oidc_enabled() {
    local raw_value=""

    load_demo_leader_common_helpers

    if [[ -n "${LEADER_OIDC_ENABLED+x}" ]]; then
        raw_value="${LEADER_OIDC_ENABLED}"
    else
        raw_value="$(demo_leader_extract_env_file_value LEADER_OIDC_ENABLED .env)"
    fi

    if [[ -z "${raw_value}" ]]; then
        raw_value="$(extract_toml_section_boolean_value "oidc" "enabled" "leader/config.toml")"
    fi

    demo_leader_normalize_bool "${raw_value}"
}

demo_leader_collect_cert_files() {
    printf '%s\n' \
        "leader/atr/client.pem" \
        "leader/atr/client.key" \
        "leader/atr/trust-bundle.pem"
}

demo_leader_load_cert_files() {
    demo_leader_cert_files=()

    while IFS= read -r cert_path; do
        if [[ -n "${cert_path}" ]]; then
            demo_leader_cert_files+=("${cert_path}")
        fi
    done < <(demo_leader_collect_cert_files)
}

_fix_demo_leader_certs() {
    run_project_prep_action certs
}

_fix_demo_leader_oidc_keycloak() {
    just infra up keycloak
    just infra wait keycloak
}

_fix_demo_leader_demo_partner_runtime() {
    (
        cd ../demo-partner
        just prep sync
    )
}

describe_project_check_step() {
    local check_id="$1"

    case "${check_id}" in
        demo-leader-dev-ready)
            echo "检查 .env、openssl 与本地兄弟仓 acps-sdk / acps-cli 路径等开发前置。"
            ;;
        demo-leader-certs)
            echo "检查 leader/atr 下本地 mTLS 客户端证书材料已按 acs.json 签发；需要修复时等价于 just prep certs。"
            ;;
        demo-leader-oidc-keycloak)
            echo "当 OIDC 启用时检查共享 Keycloak 已启动且健康；需要修复时等价于 just infra up keycloak && just infra wait keycloak。"
            ;;
        demo-leader-test-ready)
            echo "测试轻量前置检查；当前测试模式会跳过 .env / openssl / local path / shared infra 的重检查。"
            ;;
        demo-leader-demo-partner-runtime)
            echo "检查兄弟项目 demo-partner 的受管测试运行时与 online Partner 目录已就绪；需要修复时会执行 cd ../demo-partner && just prep sync。"
            ;;
        *)
            return 1
            ;;
    esac
}

run_project_check() {
    local check_id="$1"
    local oidc_enabled=""

    case "${check_id}" in
        demo-leader-dev-ready)
            if [[ "${SKIP_DOTENV_DOCTOR:-0}" == "1" ]]; then
                emit_check_result "${check_id}" ready info project "已跳过 .env 检查（SKIP_DOTENV_DOCTOR=1）。" ""
            elif [[ -f .env ]]; then
                emit_check_result "${check_id}" ready info project ".env 已就绪。" ""
            else
                emit_check_result "${check_id}" missing error project ".env 缺失。" "执行 just prep env。"
            fi

            if [[ "${SKIP_OPENSSL_DOCTOR:-0}" == "1" ]]; then
                emit_check_result "${check_id}" ready info external "已跳过 openssl 检查（SKIP_OPENSSL_DOCTOR=1）。" ""
            elif command -v openssl >/dev/null 2>&1; then
                emit_check_result "${check_id}" ready info external "openssl 已安装。" ""
            else
                emit_check_result "${check_id}" blocked error external "未找到 openssl。" "安装 openssl 后重试。"
            fi

            if [[ "${SKIP_LOCAL_PATH_DOCTOR:-0}" == "1" ]]; then
                emit_check_result "${check_id}" ready info sibling "已跳过兄弟项目路径检查（SKIP_LOCAL_PATH_DOCTOR=1）。" ""
            else
                if [[ -d ../acps-sdk ]]; then
                    emit_check_result "${check_id}" ready info sibling "兄弟项目 ../acps-sdk 已就绪。" ""
                else
                    emit_check_result "${check_id}" blocked error sibling "未找到兄弟项目 ../acps-sdk。" "补齐本地兄弟项目后重试。"
                fi

                if [[ -d ../acps-cli ]]; then
                    emit_check_result "${check_id}" ready info sibling "兄弟项目 ../acps-cli 已就绪。" ""
                else
                    emit_check_result "${check_id}" blocked error sibling "未找到兄弟项目 ../acps-cli。" "补齐本地兄弟项目后重试。"
                fi
            fi
            ;;
        demo-leader-certs)
            demo_leader_load_cert_files
            load_demo_leader_cert_helpers
            check_cert_group_files_ready "${check_id}" project "${demo_leader_cert_files[@]}"
            ;;
        demo-leader-oidc-keycloak)
            oidc_enabled="$(demo_leader_resolve_oidc_enabled)"
            if [[ "${oidc_enabled}" != "true" ]]; then
                emit_check_result "${check_id}" ready info project "OIDC 未启用，跳过 Keycloak 检查。" ""
                return 0
            fi
            check_infra_service_ready keycloak "${check_id}"
            ;;
        demo-leader-test-ready)
            emit_check_result "${check_id}" ready info project "test check 使用轻量模式，跳过 .env / openssl / local path / shared infra 检查。" ""
            ;;
        demo-leader-demo-partner-runtime)
            if [[ ! -d ../demo-partner ]]; then
                emit_check_result "${check_id}" blocked error sibling "未找到兄弟项目 ../demo-partner。" "补齐 demo-partner 仓库后重试。"
                return 0
            fi
            if [[ ! -d ../demo-partner/partners/online ]]; then
                emit_check_result "${check_id}" invalid error sibling "缺少 demo-partner online 目录。" "检查 ../demo-partner/partners/online。"
                return 0
            fi
            if [[ ! -x ../demo-partner/.venv/bin/python ]]; then
                emit_check_result "${check_id}" missing error sibling "demo-partner Python 运行时缺失。" "执行 cd ../demo-partner && just prep sync。"
                return 0
            fi
            emit_check_result "${check_id}" ready info sibling "demo-partner 受管运行时已就绪。" ""
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_ensure() {
    local check_id="$1"

    case "${check_id}" in
        demo-leader-certs)
            demo_leader_load_cert_files
            load_demo_leader_cert_helpers
            ensure_cert_group_files_ready "${check_id}" _fix_demo_leader_certs "${demo_leader_cert_files[@]}"
            ;;
        demo-leader-oidc-keycloak)
            ensure_check_with_fix "${check_id}" _fix_demo_leader_oidc_keycloak
            ;;
        demo-leader-demo-partner-runtime)
            ensure_check_with_fix "${check_id}" _fix_demo_leader_demo_partner_runtime
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_dev_runtime() {
    local action="$1"
    shift || true

    load_demo_leader_app_helpers

    ensure_background_process() {
        local name="$1"
        local target_pid_file="$2"
        local target_log_file="$3"
        shift 3
        local pid=""

        ensure_log_dir_for_file "${target_log_file}"

        if pid="$(read_live_pid "${target_pid_file}")"; then
            echo "[INFO]  ${name} 已在后台运行（pid=${pid}）。"
            return 1
        fi

        pid="$(launch_detached "${target_log_file}" "$@")"
        echo "${pid}" > "${target_pid_file}"

        sleep 3
        if ! kill -0 "${pid}" 2>/dev/null; then
            rm -f "${target_pid_file}"
            echo "[ERROR] ${name} 启动失败，请检查 ${target_log_file}。" >&2
            exit 1
        fi

        echo "[INFO]  ${name} 已在后台启动（pid=${pid}）。"
        return 0
    }

    stop_background_process() {
        local name="$1"
        local target_pid_file="$2"
        local pid=""

        if ! pid="$(read_live_pid "${target_pid_file}")"; then
            echo "[INFO]  ${name} 当前未在后台运行。"
            return 0
        fi

        kill_wait "${pid}"
        rm -f "${target_pid_file}"
        echo "[INFO]  ${name} 已停止（pid=${pid}）。"
    }

    show_status() {
        local name="$1"
        local target_pid_file="$2"
        local pid=""

        if pid="$(read_live_pid "${target_pid_file}")"; then
            echo "[INFO]  ${name} 正在运行（pid=${pid}）。"
        else
            echo "[INFO]  ${name} 当前未运行。"
        fi
    }

    collect_log_files() {
        local -a log_files=()

        [[ -f "${leader_log_file}" ]] && log_files+=("${leader_log_file}")
        [[ -f "${web_log_file}" ]] && log_files+=("${web_log_file}")

        if [[ "${#log_files[@]}" -eq 0 ]]; then
            echo "[ERROR] 未找到后台日志文件，请先执行 just dev start。" >&2
            exit 1
        fi

        printf '%s\n' "${log_files[@]}"
    }

    run_leader_foreground() {
        UVICORN_RELOAD=true bash scripts/start-leader-api.sh
    }

    start_leader_background() {
        ensure_background_process \
            "leader" \
            "${leader_pid_file}" \
            "${leader_log_file}" \
            env UVICORN_RELOAD=true \
            bash scripts/start-leader-api.sh
    }

    start_web_background() {
        ensure_background_process \
            "static_web" \
            "${web_pid_file}" \
            "${web_log_file}" \
            bash scripts/start-web-ui.sh
    }

    case "${action}" in
        start)
            local leader_running=0
            local web_running=0

            if [[ "$#" -gt 1 ]]; then
                echo "[ERROR] dev start 只接受一个可选参数：bg / fg。" >&2
                exit 2
            fi

            if [[ "$#" -gt 0 ]] && [[ "$1" == "fg" ]]; then
                if pid="$(read_live_pid "${leader_pid_file}")"; then
                    echo "[ERROR] leader 已有后台实例在运行（pid=${pid}）。请先执行 just dev stop，再执行 just dev start fg。" >&2
                    exit 2
                fi
                if pid="$(read_live_pid "${web_pid_file}")"; then
                    echo "[ERROR] static_web 已有后台实例在运行（pid=${pid}）。请先执行 just dev stop，再执行 just dev start fg。" >&2
                    exit 2
                fi
                just dev bootstrap
                ensure_log_dir_for_file "${leader_log_file}"
                ensure_log_dir_for_file "${web_log_file}"
                web_started=0
                echo "[INFO]  启动 ${project_name}（前台 Leader，后台 Web）..."
                if start_web_background; then
                    web_started=1
                fi
                cleanup() {
                    if [[ "${web_started}" -eq 1 ]]; then
                        stop_background_process "static_web" "${web_pid_file}" >/dev/null 2>&1 || true
                    fi
                }
                trap cleanup EXIT INT TERM
                run_leader_foreground
                return 0
            fi

            if [[ "$#" -gt 0 ]] && [[ "$1" != "bg" ]]; then
                echo "[ERROR] 未知 dev start 参数：$*。支持：bg / fg" >&2
                exit 2
            fi

            if pid="$(read_live_pid "${leader_pid_file}")"; then
                leader_running=1
            fi
            if pid="$(read_live_pid "${web_pid_file}")"; then
                web_running=1
            fi
            if [[ "${leader_running}" -eq 1 ]]; then
                echo "[INFO]  leader 已在后台运行。"
            fi
            if [[ "${web_running}" -eq 1 ]]; then
                echo "[INFO]  static_web 已在后台运行。"
            fi
            if [[ "${leader_running}" -eq 1 && "${web_running}" -eq 1 ]]; then
                return 0
            fi

            just dev bootstrap
            ensure_log_dir_for_file "${leader_log_file}"
            ensure_log_dir_for_file "${web_log_file}"
            start_leader_background || true
            start_web_background || true
            echo "[INFO]  ${project_name} 已在后台启动。"
            echo "[INFO]  执行 just dev status 查看进程状态。"
            ;;
        stop)
            stop_background_process "leader" "${leader_pid_file}"
            stop_background_process "static_web" "${web_pid_file}"
            kill_port 9031
            kill_port 9030
            ;;
        status)
            show_status "leader" "${leader_pid_file}"
            show_status "static_web" "${web_pid_file}"
            ;;
        logs)
            log_files=()
            while IFS= read -r log_file; do
                log_files+=("${log_file}")
            done < <(collect_log_files)

            if [[ "${1:-}" == "follow" ]]; then
                exec tail -f "${log_files[@]}"
            fi

            tail -n 200 "${log_files[@]}"
            ;;
        restart)
            if [[ -n "${1:-}" ]] && [[ "$1" != "bg" ]]; then
                echo "[ERROR] dev restart 只管理后台实例；如需前台运行，请直接执行 just dev start fg。" >&2
                exit 2
            fi
            run_project_dev_runtime stop
            run_project_dev_runtime start
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_test_action() {
    local action="$1"
    shift || true
    local default_loadfile_workers="${JUST_TEST_LOADFILE_WORKERS:-2}"

    load_demo_leader_pytest_helpers

    has_explicit_xdist_args() {
        local expect_plugin_name=0
        local arg=""

        for arg in "$@"; do
            if [[ "${expect_plugin_name}" -eq 1 ]]; then
                if [[ "${arg}" == "no:xdist" ]]; then
                    return 0
                fi
                expect_plugin_name=0
                continue
            fi

            case "${arg}" in
                -n|--numprocesses|--dist|-d)
                    return 0
                    ;;
                -n=*|--numprocesses=*|--dist=*|-pno:xdist)
                    return 0
                    ;;
                -p)
                    expect_plugin_name=1
                    ;;
            esac
        done

        return 1
    }

    run_scoped_demo_leader_pytest() {
        local default_path="$1"
        local parallel_mode="$2"
        shift 2
        local selection_count="$#"
        local -a selection=()
        local -a parallel_args=()
        local -a pytest_cmd=(run_uv_with_mutating_cache uv run pytest)

        if [[ "${selection_count}" -gt 0 ]]; then
            selection=("$@")
        fi

        if [[ "${parallel_mode}" == "loadfile" ]]; then
            if [[ "${selection_count}" -eq 0 ]] || ! has_explicit_xdist_args "${selection[@]}"; then
                parallel_args=(-n "${default_loadfile_workers}" --dist=loadfile)
            fi
        fi

        if [[ "${selection_count}" -eq 0 ]]; then
            pytest_cmd+=("${default_path}")
            if [[ "${#parallel_args[@]}" -gt 0 ]]; then
                pytest_cmd+=("${parallel_args[@]}")
            fi
            APP_ENV=testing "${pytest_cmd[@]}"
            return
        fi

        if [[ "${selection[0]}" == -* ]]; then
            pytest_cmd+=("${default_path}")
            if [[ "${#parallel_args[@]}" -gt 0 ]]; then
                pytest_cmd+=("${parallel_args[@]}")
            fi
            pytest_cmd+=("${selection[@]}")
            APP_ENV=testing "${pytest_cmd[@]}"
            return
        fi

        if [[ "${#parallel_args[@]}" -gt 0 ]]; then
            pytest_cmd+=("${parallel_args[@]}")
        fi
        pytest_cmd+=("${selection[@]}")
        APP_ENV=testing "${pytest_cmd[@]}"
    }

    run_requested_demo_leader_tests() {
        local default_path="$1"
        local parallel_mode="${2:-serial}"

        if [[ "$#" -gt 2 ]]; then
            shift 2
            run_scoped_demo_leader_pytest "${default_path}" "${parallel_mode}" "$@"
            return
        fi

        run_scoped_demo_leader_pytest "${default_path}" "${parallel_mode}"
    }

    case "${action}" in
        unit)
            run_requested_demo_leader_tests tests/unit/ serial "$@"
            ;;
        api)
            run_requested_demo_leader_tests tests/api/ serial "$@"
            ;;
        coverage)
            if [[ "$#" -gt 0 ]]; then
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/unit/ "$@" --cov=leader --cov-report=term-missing --cov-fail-under=70
            else
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/unit/ --cov=leader --cov-report=term-missing --cov-fail-under=70
            fi
            ;;
        integration)
            just test bootstrap
            run_requested_demo_leader_tests tests/integration/ loadfile "$@"
            ;;
        all)
            if [[ "$#" -gt 0 ]]; then
                just test unit "$@"
                just test api "$@"
                just test integration "$@"
                just test e2e "$@"
                just test coverage "$@"
            else
                just test unit
                just test api
                just test integration
                just test e2e
                just test coverage
            fi
            ;;
        *)
            return 127
            ;;
    esac
}

demo_leader_export_local_oidc_test_env() {
    export LEADER_OIDC_ENABLED="${LEADER_OIDC_ENABLED:-true}"
    export LEADER_OIDC_ISSUER="${LEADER_OIDC_ISSUER:-http://localhost:9080/realms/acps-leader}"
    export LEADER_OIDC_AUDIENCE="${LEADER_OIDC_AUDIENCE:-leader-api}"
    export LEADER_OIDC_ALLOWED_AZP="${LEADER_OIDC_ALLOWED_AZP:-leader-e2e}"
    export LEADER_OIDC_CLIENT_ID="${LEADER_OIDC_CLIENT_ID:-leader-api}"
    export LEADER_OIDC_ALGORITHMS="${LEADER_OIDC_ALGORITHMS:-EdDSA}"
    export LEADER_OIDC_REQUIRE_HTTPS="${LEADER_OIDC_REQUIRE_HTTPS:-false}"
    export LEADER_OIDC_ROLE_SOURCE_CLIENT_ID="${LEADER_OIDC_ROLE_SOURCE_CLIENT_ID:-leader-api}"
    export TEST_OIDC_ISSUER="${TEST_OIDC_ISSUER:-${LEADER_OIDC_ISSUER}}"
    export TEST_OIDC_E2E_CLIENT_ID="${TEST_OIDC_E2E_CLIENT_ID:-leader-e2e}"
    export TEST_OIDC_USER_USERNAME="${TEST_OIDC_USER_USERNAME:-leader-user}"
    export TEST_OIDC_USER_PASSWORD="${TEST_OIDC_USER_PASSWORD:-demo123}"
    export TEST_OIDC_OPERATOR_USERNAME="${TEST_OIDC_OPERATOR_USERNAME:-leader-operator}"
    export TEST_OIDC_OPERATOR_PASSWORD="${TEST_OIDC_OPERATOR_PASSWORD:-demo123}"
    export TEST_OIDC_ADMIN_USERNAME="${TEST_OIDC_ADMIN_USERNAME:-leader-admin}"
    export TEST_OIDC_ADMIN_PASSWORD="${TEST_OIDC_ADMIN_PASSWORD:-demo123}"
    export TEST_OIDC_FOREIGN_ISSUER="${TEST_OIDC_FOREIGN_ISSUER:-http://localhost:9080/realms/acps-registry}"
    export TEST_OIDC_FOREIGN_CLIENT_ID="${TEST_OIDC_FOREIGN_CLIENT_ID:-registry-e2e}"
    export TEST_OIDC_FOREIGN_USERNAME="${TEST_OIDC_FOREIGN_USERNAME:-registry-client}"
    export TEST_OIDC_FOREIGN_PASSWORD="${TEST_OIDC_FOREIGN_PASSWORD:-demo123}"
}

run_project_test_e2e_profile() {
    local profile="$1"
    shift || true

    case "${profile}" in
        local)
            just test bootstrap
            if [[ "$#" -eq 0 ]]; then
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/e2e/
            elif [[ "$1" == -* ]]; then
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/e2e/ "$@"
            else
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest "$@"
            fi
            ;;
        oidc)
            demo_leader_export_local_oidc_test_env
            just infra up keycloak
            just infra wait keycloak
            just test bootstrap
            if [[ "$#" -eq 0 ]]; then
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/e2e/test_oidc_keycloak_flow.py
            elif [[ "$1" == -* ]]; then
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/e2e/test_oidc_keycloak_flow.py "$@"
            else
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest "$@"
            fi
            ;;
        *)
            return 127
            ;;
    esac
}

supports_project_test_action() {
    local action="$1"

    case "${action}" in
        api)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}
