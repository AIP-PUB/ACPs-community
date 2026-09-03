#!/usr/bin/env bash

: "${app_port:=9007}"
: "${app_pid_file:=logs/mq-auth-server.pid}"
: "${app_log_file:=logs/mq-auth-server.log}"

configure_project_package_runtime() {
    PACKAGE_RUNTIME_REQUIRED_PATHS=(
        config
        .env.example
        README.md
        pyproject.toml
        app/acs
    )

    PACKAGE_RUNTIME_SIBLING_REPOS=()

    PACKAGE_RUNTIME_BUNDLE_MAP=(
        "config|config|config"
        ".env.example|.env.example|env_template"
        "README.md|README.md|doc"
        "app/acs|acs|acs_descriptor"
    )

    PACKAGE_RUNTIME_BUNDLE_EXCLUDE_MAP=(
        "${DEFAULT_BUNDLE_EXCLUDE_MAP[@]}"
        "._*"
        "*/._*"
    )

    PACKAGE_RUNTIME_CHMOD_PATHS=()
    PACKAGE_RUNTIME_REMOVE_PATTERNS=()

    # mq-auth-server 对外暴露 Group API(9007)/Auth API(9008) 两个端口，但由同一个
    # `mq-auth-server` console script（app.main:main）通过 multiprocessing 内部拉起，
    # 操作者只需要一条启动命令，因此只声明一个 [[components]]（源设计 §4.3 反例说明）。
    # health_check 必须是 https://：两个 listener 都强制 mTLS，明文 HTTP 探针只会握手失败。
    # 探活还需要客户端证书，标准做法是 `python -m app.core.health_probe --url ...`
    # （读 HEALTHCHECK_TLS_* 三件套），此处的 URL 是给部署方的探活目标地址。
    PACKAGE_RUNTIME_COMPONENTS=(
        "mq-auth-server|python-service|mq-auth-server|9007,9008|https://127.0.0.1:9007/health||config/production.toml"
    )
    PACKAGE_RUNTIME_INTERNAL_WHEELS=()
    PACKAGE_RUNTIME_EXTERNAL_COMPONENTS=(redis rabbitmq)
}

run_project_prep_action() {
    local action="$1"
    shift || true

    ensure_dev_certs() {
        local mode="${1:-ensure}"
        local cert_file=""
        local key_file=""
        local ca_file=""
        local health_cert_file=""
        local health_key_file=""
        local health_ca_file=""
        local healthcheck_client_aic=""

        if [[ -f .env ]]; then
            set -a
            # shellcheck source=/dev/null
            source .env
            set +a
        fi

        cert_file="${TLS_CERT_FILE:-certs/server.pem}"
        key_file="${TLS_KEY_FILE:-certs/server.key}"
        ca_file="${TLS_CA_CERT_FILE:-certs/acps-root-ca.pem}"
        health_cert_file="${HEALTHCHECK_TLS_CERT_FILE:-certs/client.pem}"
        health_key_file="${HEALTHCHECK_TLS_KEY_FILE:-certs/client.key}"
        health_ca_file="${HEALTHCHECK_TLS_CA_CERT_FILE:-${ca_file}}"
        healthcheck_client_aic="${HEALTHCHECK_TLS_CLIENT_AIC:-1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ}"

        if [[ ! -x "../acps-infra/dev-infra/dev-cert.sh" ]]; then
            echo "[ERROR] 未找到共享开发 PKI 脚本：../acps-infra/dev-infra/dev-cert.sh" >&2
            exit 1
        fi

        if [[ "${mode}" == "reset" ]]; then
            rm -f "${cert_file}" "${key_file}" "${ca_file}" "${health_cert_file}" "${health_key_file}" "${health_ca_file}"
        fi

        bash ../acps-infra/dev-infra/dev-cert.sh issue-leaf \
            --ca infra \
            --common-name "mq-auth-server" \
            --usage serverAuth \
            --san "DNS:localhost,DNS:mq-auth-server,DNS:host.docker.internal,IP:127.0.0.1" \
            --cert-out "${cert_file}" \
            --key-out "${key_file}" \
            --relative-to "$PWD"

        bash ../acps-infra/dev-infra/dev-cert.sh issue-leaf \
            --ca agent \
            --common-name "${healthcheck_client_aic}" \
            --usage clientAuth \
            --cert-out "${health_cert_file}" \
            --key-out "${health_key_file}" \
            --relative-to "$PWD"

        bash ../acps-infra/dev-infra/dev-cert.sh export-ca \
            --ca bundle \
            --bundle-out "${ca_file}" \
            --relative-to "$PWD"

        if [[ "${health_ca_file}" != "${ca_file}" ]]; then
            bash ../acps-infra/dev-infra/dev-cert.sh export-ca \
                --ca bundle \
                --bundle-out "${health_ca_file}" \
                --relative-to "$PWD"
        fi

        for path in "${cert_file}" "${key_file}" "${ca_file}" "${health_cert_file}" "${health_key_file}"; do
            if [[ ! -f "${path}" ]]; then
                echo "[ERROR] 共享开发 PKI 签发完成后仍缺少证书文件：${path}" >&2
                exit 1
            fi
        done

        echo "[INFO]  development server cert ready: ${cert_file}"
        echo "[INFO]  development server key ready: ${key_file}"
        echo "[INFO]  development client cert ready: ${health_cert_file}"
        echo "[INFO]  development client key ready: ${health_key_file}"
        echo "[INFO]  development ca ready: ${ca_file}"
    }

    case "${action}" in
        certs)
            local certs_mode="ensure"

            if [[ "$#" -gt 1 ]]; then
                echo "[ERROR] just prep certs 仅支持一个可选参数：reset。" >&2
                exit 2
            fi
            if [[ "$#" -eq 1 ]]; then
                if [[ "$1" != "reset" ]]; then
                    echo "[ERROR] just prep certs 仅支持 reset 参数，当前为：$1" >&2
                    exit 2
                fi
                certs_mode="reset"
            fi
            ensure_dev_certs "${certs_mode}"
            ;;
        migrate)
            local migrate_target="dev"

            if [[ "$#" -gt 1 ]]; then
                echo "[ERROR] just prep migrate 只接受一个可选目标参数：dev 或 test。" >&2
                exit 2
            fi
            if [[ "$#" -eq 1 ]]; then
                if [[ "$1" != "dev" && "$1" != "test" ]]; then
                    echo "[ERROR] just prep migrate 仅支持 dev 或 test，当前为：$1" >&2
                    exit 2
                fi
                migrate_target="$1"
            fi

            echo "[INFO]  mq-auth-server 无数据库迁移，跳过 migrate ${migrate_target}。"
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
            echo "  certs [reset]  按本仓声明签发开发证书；传 reset 时先清理再重签"
            echo "  migrate [dev|test]  无数据库项目，显式 skip"
            ;;
    esac
}

load_mq_auth_pytest_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/pytest-lib.sh"
}

load_mq_auth_cert_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/cert-lib.sh"
}

load_mq_auth_app_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/app-lib.sh"
}

mq_auth_run_venv_python() {
    run_uv_with_mutating_cache uv run --no-sync python "$@"
}

mq_auth_resolve_dev_cert_paths() {
    load_dotenv
    MQ_AUTH_CERT_FILE="${TLS_CERT_FILE:-certs/server.pem}"
    MQ_AUTH_KEY_FILE="${TLS_KEY_FILE:-certs/server.key}"
    MQ_AUTH_CA_FILE="${TLS_CA_CERT_FILE:-certs/acps-root-ca.pem}"
    MQ_AUTH_HEALTH_CERT_FILE="${HEALTHCHECK_TLS_CERT_FILE:-certs/client.pem}"
    MQ_AUTH_HEALTH_KEY_FILE="${HEALTHCHECK_TLS_KEY_FILE:-certs/client.key}"
    MQ_AUTH_HEALTH_CA_FILE="${HEALTHCHECK_TLS_CA_CERT_FILE:-${MQ_AUTH_CA_FILE}}"
}

mq_auth_check_redis_connectivity() {
    local redis_url="$1"

    REDIS_URL="${redis_url}" mq_auth_run_venv_python -c "import os, redis; url=os.environ['REDIS_URL']; client=redis.from_url(url, socket_connect_timeout=2); client.ping()"
}

_fix_mq_auth_dev_certs() {
    run_project_prep_action certs
}

describe_project_check_step() {
    local check_id="$1"

    case "${check_id}" in
        mq-auth-dev-ready)
            echo "检查 .env、Redis 连通性、RabbitMQ 管理密码和本地 e2e mTLS 证书前置。"
            ;;
        mq-auth-test-ready)
            echo "检查测试所需 Redis 连通性。"
            ;;
        mq-auth-dev-certs)
            echo "检查 mq-auth-server 本地开发/healthcheck 证书材料已就绪；需要修复时等价于 just prep certs。"
            ;;
        *)
            return 1
            ;;
    esac
}

run_project_check() {
    local check_id="$1"
    local redis_url=""

    case "${check_id}" in
        mq-auth-dev-ready)
            redis_url="redis://localhost:6379/0"

            if [[ -f .env ]]; then
                load_dotenv
                redis_url="${REDIS_URL:-${redis_url}}"
                emit_check_result "${check_id}" ready info project ".env 已就绪。" ""
            elif [[ "${SKIP_DOTENV_DOCTOR:-0}" == "1" ]]; then
                emit_check_result "${check_id}" ready info project "已跳过 .env 检查（SKIP_DOTENV_DOCTOR=1）。" ""
            else
                emit_check_result "${check_id}" missing error project ".env 缺失。" "执行 just prep env。"
            fi

            if mq_auth_check_redis_connectivity "${redis_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" ready info project "Redis 可连接（REDIS_URL=${redis_url}）。" ""
            else
                emit_check_result "${check_id}" missing error project "Redis 不可连接（REDIS_URL=${redis_url}）。" "执行 just infra up redis && just infra wait redis，并检查 REDIS_URL。"
            fi

            if [[ "${SKIP_RABBITMQ_MGMT_PASS_DOCTOR:-0}" == "1" ]]; then
                emit_check_result "${check_id}" ready info project "已跳过 RABBITMQ_MGMT_PASS 检查（SKIP_RABBITMQ_MGMT_PASS_DOCTOR=1）。" ""
            elif [[ -n "${RABBITMQ_MGMT_PASS:-}" ]]; then
                emit_check_result "${check_id}" ready info project "RABBITMQ_MGMT_PASS 已配置。" ""
            else
                emit_check_result "${check_id}" missing error user-config "RABBITMQ_MGMT_PASS 未配置。" "补齐 .env 中的 RABBITMQ_MGMT_PASS。"
            fi

            mq_auth_resolve_dev_cert_paths
            for path in "${MQ_AUTH_HEALTH_CERT_FILE}" "${MQ_AUTH_HEALTH_KEY_FILE}" "${MQ_AUTH_HEALTH_CA_FILE}"; do
                if [[ ! -f "${path}" ]]; then
                    emit_check_result "${check_id}" missing warn project "开发证书缺失：${path}（e2e 需要 mTLS 证书）。" "执行 just prep certs。"
                fi
            done
            ;;
        mq-auth-test-ready)
            redis_url="${REDIS_URL:-redis://localhost:6379/0}"
            if mq_auth_check_redis_connectivity "${redis_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" ready info project "Redis 可连接（REDIS_URL=${redis_url}）。" ""
            else
                emit_check_result "${check_id}" missing error project "Redis 不可连接（REDIS_URL=${redis_url}）。" "执行 just infra up redis && just infra wait redis。"
            fi
            ;;
        mq-auth-dev-certs)
            load_mq_auth_cert_helpers
            mq_auth_resolve_dev_cert_paths
            check_cert_group_files_ready "${check_id}" project \
                "${MQ_AUTH_CERT_FILE}" \
                "${MQ_AUTH_KEY_FILE}" \
                "${MQ_AUTH_CA_FILE}" \
                "${MQ_AUTH_HEALTH_CERT_FILE}" \
                "${MQ_AUTH_HEALTH_KEY_FILE}"
            ;;
        migrate-dev)
            emit_check_result "${check_id}" ready info project "mq-auth-server 无数据库迁移，跳过 dev migrate。" ""
            ;;
        migrate-test)
            emit_check_result "${check_id}" ready info project "mq-auth-server 无数据库迁移，跳过 test migrate。" ""
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_ensure() {
    local check_id="$1"

    case "${check_id}" in
        mq-auth-dev-certs)
            load_mq_auth_cert_helpers
            mq_auth_resolve_dev_cert_paths
            ensure_cert_group_files_ready "${check_id}" _fix_mq_auth_dev_certs \
                "${MQ_AUTH_CERT_FILE}" \
                "${MQ_AUTH_KEY_FILE}" \
                "${MQ_AUTH_CA_FILE}" \
                "${MQ_AUTH_HEALTH_CERT_FILE}" \
                "${MQ_AUTH_HEALTH_KEY_FILE}"
            ;;
        migrate-dev|migrate-test)
            ensure_check_with_fix "${check_id}" ""
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_dev_runtime() {
    local action="$1"
    shift || true
    local mode=""

    load_mq_auth_app_helpers

    case "${action}" in
        start|"")
            if [[ "$#" -gt 1 ]]; then
                echo "[ERROR] dev start 只接受一个可选参数：bg / fg。" >&2
                exit 2
            fi
            mode="${1:-bg}"
            case "${mode}" in
                bg)
                    if pid="$(read_live_pid "${app_pid_file}")"; then
                        echo "[INFO]  ${project_name} 已在后台运行（pid=${pid}）。"
                        return 0
                    fi
                    just dev bootstrap
                    ensure_log_dir_for_file "${app_log_file}"

                    pid="$(launch_detached "${app_log_file}" uv run mq-auth-server)"
                    echo "${pid}" > "${app_pid_file}"

                    sleep 3
                    if ! kill -0 "${pid}" 2>/dev/null; then
                        rm -f "${app_pid_file}"
                        echo "[ERROR] ${project_name} 后台启动失败，请检查日志：${app_log_file}" >&2
                        exit 1
                    fi

                    echo "[INFO]  ${project_name} 已后台启动（pid=${pid}，log=${app_log_file}）。"
                    ;;
                fg)
                    if pid="$(read_live_pid "${app_pid_file}")"; then
                        echo "[ERROR] ${project_name} 已有后台实例在运行（pid=${pid}）。请先执行 just dev stop，再执行 just dev start fg。" >&2
                        exit 2
                    fi
                    just dev bootstrap
                    exec uv run mq-auth-server
                    ;;
                *)
                    echo "[ERROR] 未知 dev start 模式：${mode}（仅支持 bg / fg）。" >&2
                    exit 2
                    ;;
            esac
            ;;
        stop)
            if pid="$(read_live_pid "${app_pid_file}")"; then
                kill_wait "${pid}"
                rm -f "${app_pid_file}"
                echo "[INFO]  ${project_name} 已停止（pid=${pid}）。"
            else
                echo "[INFO]  ${project_name} 当前未运行。"
            fi
            kill_port "${app_port}"
            ;;
        status)
            if pid="$(read_live_pid "${app_pid_file}")"; then
                echo "[INFO]  ${project_name} 正在运行（pid=${pid}，log=${app_log_file}）。"
            else
                echo "[INFO]  ${project_name} 当前未运行。"
            fi
            ;;
        logs)
            if [[ ! -f "${app_log_file}" ]]; then
                echo "[ERROR] 未找到日志文件：${app_log_file}" >&2
                exit 1
            fi

            if [[ "${1:-}" == "follow" ]]; then
                exec tail -n 200 -f "${app_log_file}"
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

mq_auth_stop_test_server() {
    if [[ -n "${mq_auth_group_server_pid:-}" ]] && kill -0 "${mq_auth_group_server_pid}" 2>/dev/null; then
        kill "${mq_auth_group_server_pid}" 2>/dev/null || true
        wait "${mq_auth_group_server_pid}" 2>/dev/null || true
        echo "[INFO]  已停止临时 Group API 实例。"
    fi

    if [[ -n "${mq_auth_auth_server_pid:-}" ]] && kill -0 "${mq_auth_auth_server_pid}" 2>/dev/null; then
        kill "${mq_auth_auth_server_pid}" 2>/dev/null || true
        wait "${mq_auth_auth_server_pid}" 2>/dev/null || true
        echo "[INFO]  已停止临时 Auth API 实例。"
    fi

    if [[ -n "${mq_auth_test_server_log:-}" ]] && [[ -f "${mq_auth_test_server_log}" ]]; then
        echo "[INFO]  e2e 临时实例日志保留于：${mq_auth_test_server_log}"
    fi
}

mq_auth_wait_for_test_server() {
    local group_url="$1"
    local auth_url="$2"
    local attempt=""
    local probe_cert_file=""
    local probe_key_file=""
    local probe_ca_file=""

    probe_cert_file="${E2E_TLS_CERT_FILE:-certs/client.pem}"
    probe_key_file="${E2E_TLS_KEY_FILE:-certs/client.key}"
    probe_ca_file="${E2E_TLS_CA_CERT_FILE:-certs/acps-root-ca.pem}"

    for attempt in {1..45}; do
        if APP_ENV=development \
            HEALTHCHECK_TLS_CERT_FILE="${probe_cert_file}" \
            HEALTHCHECK_TLS_KEY_FILE="${probe_key_file}" \
            HEALTHCHECK_TLS_CA_CERT_FILE="${probe_ca_file}" \
            run_uv_with_mutating_cache uv run python -m app.core.health_probe --url "${group_url}/health" >/dev/null 2>&1 \
            && APP_ENV=development \
            HEALTHCHECK_TLS_CERT_FILE="${probe_cert_file}" \
            HEALTHCHECK_TLS_KEY_FILE="${probe_key_file}" \
            HEALTHCHECK_TLS_CA_CERT_FILE="${probe_ca_file}" \
            run_uv_with_mutating_cache uv run python -m app.core.health_probe --url "${auth_url}/health" >/dev/null 2>&1; then
            return 0
        fi

        if [[ -n "${mq_auth_group_server_pid:-}" ]] && ! kill -0 "${mq_auth_group_server_pid}" 2>/dev/null; then
            echo "[ERROR] 临时 Group API 实例启动失败。" >&2
            cat "${mq_auth_test_server_log}" >&2
            return 1
        fi

        if [[ -n "${mq_auth_auth_server_pid:-}" ]] && ! kill -0 "${mq_auth_auth_server_pid}" 2>/dev/null; then
            echo "[ERROR] 临时 Auth API 实例启动失败。" >&2
            cat "${mq_auth_test_server_log}" >&2
            return 1
        fi

        sleep 1
    done

    echo "[ERROR] 等待临时 e2e 实例就绪超时。" >&2
    cat "${mq_auth_test_server_log}" >&2
    return 1
}

mq_auth_start_test_server() {
    local group_port="$1"
    local auth_port="$2"

    load_dotenv
    mq_auth_test_server_log="$(mktemp "${TMPDIR:-/tmp}/mq-auth-server-e2e.XXXXXX")"

    APP_ENV=development \
    RABBITMQ_MGMT_PASS="${RABBITMQ_MGMT_PASS:-devpass}" \
    REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}" \
    RABBITMQ_MGMT_URL="${RABBITMQ_MGMT_URL:-http://localhost:15672}" \
    run_uv_with_mutating_cache uv run python -c 'import sys; from app.main import GROUP_LISTENER, _serve_listener; _serve_listener(GROUP_LISTENER, int(sys.argv[1]))' "${group_port}" >"${mq_auth_test_server_log}" 2>&1 &
    mq_auth_group_server_pid="$!"

    APP_ENV=development \
    RABBITMQ_MGMT_PASS="${RABBITMQ_MGMT_PASS:-devpass}" \
    REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}" \
    RABBITMQ_MGMT_URL="${RABBITMQ_MGMT_URL:-http://localhost:15672}" \
    run_uv_with_mutating_cache uv run python -c 'import sys; from app.main import AUTH_LISTENER, _serve_listener; _serve_listener(AUTH_LISTENER, int(sys.argv[1]))' "${auth_port}" >>"${mq_auth_test_server_log}" 2>&1 &
    mq_auth_auth_server_pid="$!"
}

run_project_test_action() {
    local action="$1"
    shift || true

    load_mq_auth_pytest_helpers

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
                run_uv_with_mutating_cache uv run pytest tests/unit/ "$@" --cov=app --cov-report=term-missing --cov-fail-under=70
            else
                run_uv_with_mutating_cache uv run pytest tests/unit/ --cov=app --cov-report=term-missing --cov-fail-under=70
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
    local group_port=""
    local auth_port=""
    local group_api_url=""
    local auth_api_url=""
    local tls_cert_file=""
    local tls_key_file=""
    local tls_ca_cert_file=""
    local derived_leader_aic=""

    load_mq_auth_pytest_helpers

    if [[ "${profile}" != "local" ]]; then
        return 127
    fi

    just test bootstrap
    just prep certs

    group_port="${E2E_GROUP_API_PORT:-$(find_free_port)}"
    auth_port="${E2E_AUTH_API_PORT:-9008}"

    if ! mq_auth_run_venv_python -c 'import socket, sys; host=sys.argv[1]; port=int(sys.argv[2]); sock=socket.socket(); sock.settimeout(0.2); rc=sock.connect_ex((host, port)); sock.close(); raise SystemExit(1 if rc == 0 else 0)' 127.0.0.1 "${group_port}"; then
        echo "[ERROR] Group API 端口 127.0.0.1:${group_port} 已被占用；请先停止占用该端口的进程（例如 just dev stop）后再运行 just test e2e。" >&2
        exit 1
    fi
    if ! mq_auth_run_venv_python -c 'import socket, sys; host=sys.argv[1]; port=int(sys.argv[2]); sock=socket.socket(); sock.settimeout(0.2); rc=sock.connect_ex((host, port)); sock.close(); raise SystemExit(1 if rc == 0 else 0)' 127.0.0.1 "${auth_port}"; then
        echo "[ERROR] Auth API 端口 127.0.0.1:${auth_port} 已被占用；请先停止占用该端口的进程（例如 just dev stop）后再运行 just test e2e。" >&2
        exit 1
    fi

    group_api_url="https://localhost:${group_port}"
    auth_api_url="https://localhost:${auth_port}"
    tls_cert_file="${E2E_TLS_CERT_FILE:-certs/client.pem}"
    tls_key_file="${E2E_TLS_KEY_FILE:-certs/client.key}"
    tls_ca_cert_file="${E2E_TLS_CA_CERT_FILE:-certs/acps-root-ca.pem}"
    derived_leader_aic="$(openssl x509 -in "${tls_cert_file}" -noout -subject -nameopt RFC2253 | sed -E 's/^subject=//; s/.*CN=([^,]+).*/\1/')"
    if [[ -z "${derived_leader_aic}" ]]; then
        echo "[ERROR] 无法从测试客户端证书解析 Leader AIC: ${tls_cert_file}" >&2
        exit 1
    fi

    TEST_E2E_ALLOWED_SCHEMES=https
    TEST_E2E_BASE_URL_EXAMPLE=https://127.0.0.1:19123
    validate_test_e2e_base_url "${group_api_url}"
    validate_test_e2e_base_url "${auth_api_url}"

    trap mq_auth_stop_test_server EXIT
    mq_auth_start_test_server "${group_port}" "${auth_port}"
    mq_auth_wait_for_test_server "${group_api_url}" "${auth_api_url}"

    if [[ "$#" -gt 0 ]]; then
        GROUP_API_URL="${group_api_url}" \
        AUTH_API_URL="${auth_api_url}" \
        TLS_CERT_FILE="${tls_cert_file}" \
        TLS_KEY_FILE="${tls_key_file}" \
        TLS_CA_CERT_FILE="${tls_ca_cert_file}" \
        E2E_TEST_AIC="${E2E_TEST_AIC:-${derived_leader_aic}}" \
        E2E_LEADER_AIC="${E2E_LEADER_AIC:-${derived_leader_aic}}" \
        E2E_MEMBER_AIC="${E2E_MEMBER_AIC:-1.2.156.3088.1.1.9ABC.123456.654321.2XYZ}" \
        run_requested_tests tests/e2e/ "" "$@"
    else
        GROUP_API_URL="${group_api_url}" \
        AUTH_API_URL="${auth_api_url}" \
        TLS_CERT_FILE="${tls_cert_file}" \
        TLS_KEY_FILE="${tls_key_file}" \
        TLS_CA_CERT_FILE="${tls_ca_cert_file}" \
        E2E_TEST_AIC="${E2E_TEST_AIC:-${derived_leader_aic}}" \
        E2E_LEADER_AIC="${E2E_LEADER_AIC:-${derived_leader_aic}}" \
        E2E_MEMBER_AIC="${E2E_MEMBER_AIC:-1.2.156.3088.1.1.9ABC.123456.654321.2XYZ}" \
        run_requested_tests tests/e2e/
    fi
}
