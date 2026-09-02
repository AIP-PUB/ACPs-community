#!/usr/bin/env bash

: "${app_pid_file:=logs/partners_base.pid}"
: "${app_log_file:=logs/partners_base.log}"

configure_project_package_runtime() {
    PACKAGE_RUNTIME_REQUIRED_PATHS=(
        .env.example
        README.md
        pyproject.toml
        partners/online
    )

    PACKAGE_RUNTIME_SIBLING_REPOS=(
        acps-sdk
        acps-cli
    )

    PACKAGE_RUNTIME_BUNDLE_MAP=(
        ".env.example|.env.example|env_template"
        "README.md|README.md|doc"
        "partners/online|partners/online|business_data"
    )

    PACKAGE_RUNTIME_BUNDLE_EXCLUDE_MAP=(
        "${DEFAULT_BUNDLE_EXCLUDE_MAP[@]}"
        "._*"
        "*/._*"
    )

    PACKAGE_RUNTIME_CHMOD_PATHS=()
    PACKAGE_RUNTIME_REMOVE_PATTERNS=()

    # demo-partner 通过 `python -m partners.main` 一条命令以 multiprocessing 内部拉起
    # 端口 9021-9025 的多个 Partner Agent，操作者只需要一条启动命令，因此只声明一个
    # [[components]]（与 mq-auth-server 同理，见源设计 §4.3 反例说明）。
    PACKAGE_RUNTIME_COMPONENTS=(
        "demo-partner|python-service|python -m partners.main|9021,9022,9023,9024,9025|http://127.0.0.1:9021/health||"
    )
    PACKAGE_RUNTIME_INTERNAL_WHEELS=(acps-sdk acps-cli)
    PACKAGE_RUNTIME_EXTERNAL_COMPONENTS=()
}

run_project_package_filter_requirements() {
    local input_file="$1"
    local output_file="$2"

    grep -Ev '^-e \.\./acps-(cli|sdk)$' "${input_file}" > "${output_file}"
}

run_project_package_post_stage() {
    local staging_dir="$1"

    find "${staging_dir}/partners/online" -type f \( -name '*.pem' -o -name '*.key' -o -name '*.csr' -o -name '*.srl' \) -delete
}

run_project_prep_action() {
    local action="$1"
    shift || true

    run_bootstrap_python() {
        UV_MANAGED_PYTHON=1 run_uv_with_mutating_cache uv run --python 3.14 --managed-python --no-project python "$@"
    }

    extract_aic_from_acs() {
        local acs_path="$1"
        run_bootstrap_python -c 'import json, sys; from pathlib import Path; data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); aic = data.get("aic") or data.get("agentAic"); sys.exit(1) if not isinstance(aic, str) or not aic.strip() else None; print(aic.strip())' "${acs_path}"
    }

    case "${action}" in
        certs)
            local agent_dir=""

            if ! command -v openssl >/dev/null 2>&1; then
                echo "[ERROR] 未找到 openssl，just prep certs 依赖共享开发 PKI 脚本使用 openssl。" >&2
                exit 1
            fi

            if [[ ! -x "../acps-infra/dev-infra/dev-cert.sh" ]]; then
                echo "[ERROR] 未找到共享开发 PKI 脚本：../acps-infra/dev-infra/dev-cert.sh" >&2
                exit 1
            fi

            for agent_dir in partners/online/*; do
                local agent_name=""
                local cert_file=""
                local key_file=""
                local ca_file=""
                local mq_cert_file=""
                local mq_key_file=""
                local agent_aic=""
                local required_file=""

                if [[ ! -d "${agent_dir}" ]] || [[ ! -f "${agent_dir}/config.toml" ]]; then
                    continue
                fi

                agent_name="$(basename "${agent_dir}")"
                cert_file="${agent_dir}/server.pem"
                key_file="${agent_dir}/server.key"
                ca_file="${agent_dir}/trust-bundle.pem"
                mq_cert_file="${agent_dir}/client.pem"
                mq_key_file="${agent_dir}/client.key"

                if ! agent_aic="$(extract_aic_from_acs "${agent_dir}/acs.json")"; then
                    echo "[ERROR] 无法从 ${agent_dir}/acs.json 提取 Partner AIC。" >&2
                    exit 1
                fi

                bash ../acps-infra/dev-infra/dev-cert.sh issue-leaf \
                    --ca agent \
                    --common-name "${agent_aic}" \
                    --usage serverAuth \
                    --san "DNS:localhost,DNS:${agent_name},DNS:host.docker.internal,IP:127.0.0.1" \
                    --cert-out "${cert_file}" \
                    --key-out "${key_file}" \
                    --bundle-out "${ca_file}" \
                    --relative-to "$PWD"

                bash ../acps-infra/dev-infra/dev-cert.sh issue-leaf \
                    --ca agent \
                    --common-name "${agent_aic}" \
                    --usage clientAuth \
                    --cert-out "${mq_cert_file}" \
                    --key-out "${mq_key_file}" \
                    --relative-to "$PWD"

                for required_file in "${cert_file}" "${key_file}" "${ca_file}" "${mq_cert_file}" "${mq_key_file}"; do
                    if [[ ! -f "${required_file}" ]]; then
                        echo "[ERROR] 共享开发 PKI 签发完成后仍缺少证书文件：${required_file}" >&2
                        exit 1
                    fi
                done
            done

            echo "[INFO]  开发 mTLS 证书已按各 Partner 的 acs.json 声明签发完成。"
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_help_section() {
    local section="$1"

    case "${section}" in
        prep)
            echo "  certs    基于各 Partner 的 acs.json 声明签发本地 mTLS 开发证书"
            ;;
    esac
}

load_demo_partner_cert_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/cert-lib.sh"
}

load_demo_partner_pytest_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/pytest-lib.sh"
}

load_demo_partner_app_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/app-lib.sh"
}

demo_partner_collect_required_cert_files() {
    local agent_dir=""

    for agent_dir in partners/online/*; do
        if [[ ! -d "${agent_dir}" ]] || [[ ! -f "${agent_dir}/config.toml" ]]; then
            continue
        fi

        printf '%s\n' \
            "${agent_dir}/server.pem" \
            "${agent_dir}/server.key" \
            "${agent_dir}/trust-bundle.pem" \
            "${agent_dir}/client.pem" \
            "${agent_dir}/client.key"
    done
}

demo_partner_load_cert_files() {
    demo_partner_cert_files=()

    while IFS= read -r cert_path; do
        if [[ -n "${cert_path}" ]]; then
            demo_partner_cert_files+=("${cert_path}")
        fi
    done < <(demo_partner_collect_required_cert_files)
}

_fix_demo_partner_certs() {
    run_project_prep_action certs
}

describe_project_check_step() {
    local check_id="$1"

    case "${check_id}" in
        demo-partner-dev-ready)
            echo "检查 .env 已就绪；支持通过 SKIP_DOTENV_DOCTOR=1 显式跳过。"
            ;;
        demo-partner-test-ready)
            echo "测试轻量前置检查；当前测试模式会跳过 .env 与 shared infra 的重检查。"
            ;;
        demo-partner-certs)
            echo "检查各 Partner 目录下的本地 mTLS 证书材料已按 acs.json 签发；需要修复时等价于 just prep certs。"
            ;;
        *)
            return 1
            ;;
    esac
}

run_project_check() {
    local check_id="$1"

    case "${check_id}" in
        demo-partner-dev-ready)
            if [[ "${SKIP_DOTENV_DOCTOR:-0}" == "1" ]]; then
                emit_check_result "${check_id}" ready info project "已跳过 .env 检查（SKIP_DOTENV_DOCTOR=1）。" ""
            elif [[ -f .env ]]; then
                emit_check_result "${check_id}" ready info project ".env 已就绪。" ""
            else
                emit_check_result "${check_id}" missing error project ".env 缺失。" "执行 just prep env。"
            fi
            ;;
        demo-partner-test-ready)
            emit_check_result "${check_id}" ready info project "test check 使用轻量模式，跳过 .env / shared infra 检查。" ""
            ;;
        demo-partner-certs)
            demo_partner_load_cert_files
            if [[ "${#demo_partner_cert_files[@]}" -eq 0 ]]; then
                emit_check_result "${check_id}" ready info project "未发现需要签发证书的 Partner 目录。" ""
                return 0
            fi
            load_demo_partner_cert_helpers
            check_cert_group_files_ready "${check_id}" project "${demo_partner_cert_files[@]}"
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_ensure() {
    local check_id="$1"

    case "${check_id}" in
        demo-partner-certs)
            demo_partner_load_cert_files
            if [[ "${#demo_partner_cert_files[@]}" -eq 0 ]]; then
                return 0
            fi
            load_demo_partner_cert_helpers
            ensure_cert_group_files_ready "${check_id}" _fix_demo_partner_certs "${demo_partner_cert_files[@]}"
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_dev_runtime() {
    local action="$1"
    shift || true

    load_demo_partner_app_helpers

    start_background() {
        local app_pid=""

        ensure_log_dir_for_file "${app_log_file}"
        if app_pid="$(read_live_pid "${app_pid_file}")"; then
            echo "[INFO]  ${project_name} 已在后台运行（pid=${app_pid}）。"
            return 0
        fi

        app_pid="$(launch_detached "${app_log_file}" uv run python -m partners.main)"
        echo "${app_pid}" > "${app_pid_file}"

        sleep 3
        if ! kill -0 "${app_pid}" 2>/dev/null; then
            rm -f "${app_pid_file}"
            echo "[ERROR] ${project_name} 启动失败，请检查 ${app_log_file}。" >&2
            exit 1
        fi

        echo "[INFO]  ${project_name} 已在后台启动。"
        echo "[INFO]  日志文件：${app_log_file}"
        echo "[INFO]  执行 just dev status 查看进程状态"
    }

    stop_background() {
        local app_pid=""

        if ! app_pid="$(read_live_pid "${app_pid_file}")"; then
            echo "[INFO]  ${project_name} 当前未在后台运行。"
            return 0
        fi

        kill_wait "${app_pid}"
        rm -f "${app_pid_file}"
        echo "[INFO]  ${project_name} 已停止（pid=${app_pid}）。"
    }

    show_status() {
        local app_pid=""

        if app_pid="$(read_live_pid "${app_pid_file}")"; then
            echo "[INFO]  ${project_name} 正在运行（pid=${app_pid}）。"
        else
            echo "[INFO]  ${project_name} 当前未运行。"
        fi
    }

    case "${action}" in
        start)
            if [[ "$#" -gt 1 ]]; then
                echo "[ERROR] dev start 只接受一个可选参数：bg / fg。" >&2
                exit 2
            fi
            if [[ "$#" -gt 0 ]] && [[ "$1" == "fg" ]]; then
                if app_pid="$(read_live_pid "${app_pid_file}")"; then
                    echo "[ERROR] ${project_name} 已有后台实例在运行（pid=${app_pid}）。请先执行 just dev stop，再执行 just dev start fg。" >&2
                    exit 2
                fi
                just dev bootstrap
                echo "[INFO]  启动 ${project_name}（前台）..."
                exec uv run python -m partners.main
            fi
            if [[ "$#" -gt 0 ]] && [[ "$1" != "bg" ]]; then
                echo "[ERROR] 未知 dev start 参数：$1。支持：bg / fg" >&2
                exit 2
            fi
            if app_pid="$(read_live_pid "${app_pid_file}")"; then
                echo "[INFO]  ${project_name} 已在后台运行（pid=${app_pid}）。"
                return 0
            fi
            just dev bootstrap
            start_background
            ;;
        stop)
            stop_background
            kill_port 9021
            kill_port 9022
            kill_port 9023
            kill_port 9024
            kill_port 9025
            ;;
        status)
            show_status
            ;;
        logs)
            ensure_log_dir_for_file "${app_log_file}"
            if [[ ! -f "${app_log_file}" ]]; then
                echo "[ERROR] 未找到日志文件：${app_log_file}" >&2
                exit 1
            fi

            if [[ "${1:-}" == "follow" ]]; then
                exec tail -f "${app_log_file}"
            fi

            tail -n 200 "${app_log_file}"
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

    load_demo_partner_pytest_helpers

    case "${action}" in
        unit)
            if [[ "$#" -gt 0 ]]; then
                run_requested_tests tests/unit/ "" "$@"
            else
                run_requested_tests tests/unit/
            fi
            ;;
        coverage)
            if [[ "$#" -gt 0 ]]; then
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/unit/ "$@" --cov=partners --cov-report=term-missing --cov-fail-under=70
            else
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/unit/ --cov=partners --cov-report=term-missing --cov-fail-under=70
            fi
            ;;
        integration)
            just test bootstrap
            if [[ "$#" -gt 0 ]]; then
                run_requested_tests tests/integration/ "" "$@"
            else
                run_requested_tests tests/integration/
            fi
            ;;
        all)
            if [[ "$#" -gt 0 ]]; then
                just test unit "$@"
                just test integration "$@"
                just test e2e "$@"
                just test coverage "$@"
            else
                just test unit
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

run_project_test_e2e_profile() {
    local profile="$1"
    shift || true

    load_demo_partner_pytest_helpers

    if [[ "${profile}" != "local" ]]; then
        return 127
    fi

    just test bootstrap
    if [[ "$#" -gt 0 ]]; then
        run_requested_tests tests/e2e/ "" "$@"
    else
        run_requested_tests tests/e2e/
    fi
}
