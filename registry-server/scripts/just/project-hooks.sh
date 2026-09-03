#!/usr/bin/env bash

: "${app_module:=app.main:app}"
: "${app_host:=0.0.0.0}"
: "${app_port:=9001}"
: "${app_pid_file:=logs/registry-server.pid}"
: "${app_log_file:=logs/registry-server.log}"
: "${test_server_host:=127.0.0.1}"
: "${mtls_entry_module:=app.main_mtls}"
: "${mtls_app_port:=9002}"
: "${local_mtls_cert_file:=certs/server.pem}"
: "${local_mtls_key_file:=certs/server.key}"
: "${local_mtls_ca_cert_file:=certs/trust-bundle.pem}"
: "${local_mtls_probe_cert_file:=certs/client.pem}"
: "${local_mtls_probe_key_file:=certs/client.key}"

configure_project_package_runtime() {
    PACKAGE_RUNTIME_REQUIRED_PATHS=(
        config
        alembic
        alembic.ini
        .env.example
        README.md
        pyproject.toml
    )

    PACKAGE_RUNTIME_SIBLING_REPOS=(acps-sdk)

    PACKAGE_RUNTIME_BUNDLE_MAP=(
        "config|config|config"
        "alembic|alembic|migration"
        "alembic.ini|alembic.ini|migration"
        ".env.example|.env.example|env_template"
        "README.md|README.md|doc"
    )

    PACKAGE_RUNTIME_BUNDLE_EXCLUDE_MAP=(
        "${DEFAULT_BUNDLE_EXCLUDE_MAP[@]}"
        "._*"
        "*/._*"
    )

    PACKAGE_RUNTIME_CHMOD_PATHS=()
    PACKAGE_RUNTIME_REMOVE_PATTERNS=()

    # 应用发布包 runtime-package.toml 元数据：
    # registry-server 对外是两个独立进程（主 API + mTLS API），因此声明两个 [[components]]，
    # 而不是把两个端口塞进同一个 entrypoint。
    PACKAGE_RUNTIME_COMPONENTS=(
        "registry-server-api|python-service|uvicorn app.main:app --host 0.0.0.0 --port 9001|9001|http://127.0.0.1:9001/health||config/production.toml"
        # 必须用 python -m app.main_mtls（内部构建 CERT_REQUIRED TLS 上下文）。
        # 裸 uvicorn app.main_mtls:app 只会起明文 HTTP，导致安装包/镜像入口与 health_check 的 https:// 不一致。
        "registry-server-mtls|python-service|python -m app.main_mtls|9002|https://127.0.0.1:9002/health||config/production.toml"
    )
    PACKAGE_RUNTIME_INTERNAL_WHEELS=(acps-sdk)
    PACKAGE_RUNTIME_EXTERNAL_COMPONENTS=(postgresql)
}

generate_project_dotenv() {
    cp .env.example .env
    UV_MANAGED_PYTHON=1 run_uv_with_mutating_cache uv run --python 3.14 --managed-python --no-project python -c 'from functools import reduce; from pathlib import Path; import re, secrets; env_path = Path(".env"); replacements = {"SECRET_KEY": (secrets.token_hex(32), "  # noqa: S105"), "SM4_ENCRYPTION_KEY": (secrets.token_hex(16), ""), "AIC_CRC_SALT": (f"0x{secrets.token_hex(4).upper()}", "")}; content = env_path.read_text(encoding="utf-8"); content = reduce(lambda acc, item: re.sub(rf"^{item[0]}=.*$", f"{item[0]}={item[1][0]}{item[1][1]}", acc, flags=re.MULTILINE), replacements.items(), content); env_path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")'
    echo "[INFO]  已根据 .env.example 生成 .env，并写入新的敏感默认值。"
}

run_project_prep_action() {
    local action="$1"
    shift || true

    resolve_test_database_url() {
        load_dotenv

        if [[ -n "${TEST_DATABASE_URL:-}" ]]; then
            printf '%s\n' "${TEST_DATABASE_URL}"
            return
        fi

        run_uv_with_mutating_cache uv run python -c 'from tests.support.constants import DEFAULT_TEST_DATABASE_DSN; print(DEFAULT_TEST_DATABASE_DSN)'
    }

    validate_test_database_url() {
        local test_database_url="$1"

        if ! TEST_DATABASE_URL="${test_database_url}" run_uv_with_mutating_cache uv run python -c 'import os; from sqlalchemy.engine import make_url; dsn = os.environ["TEST_DATABASE_URL"]; db = make_url(dsn).database; raise SystemExit(0 if db and db != "agent_registry" else 1)'; then
            echo "[ERROR] TEST_DATABASE_URL 必须指向独立测试库，不能指向 agent_registry。" >&2
            exit 1
        fi
    }

    case "${action}" in
        certs)
            if [[ ! -x "../acps-infra/dev-infra/dev-cert.sh" ]]; then
                echo "[ERROR] 未找到共享开发 PKI 脚本：../acps-infra/dev-infra/dev-cert.sh" >&2
                exit 1
            fi

            bash ../acps-infra/dev-infra/dev-cert.sh issue-leaf \
                --ca infra \
                --common-name "registry-server" \
                --usage serverAuth \
                --san "DNS:localhost,DNS:registry-server,DNS:host.docker.internal,IP:127.0.0.1" \
                --cert-out certs/server.pem \
                --key-out certs/server.key \
                --relative-to "$PWD"

            bash ../acps-infra/dev-infra/dev-cert.sh issue-leaf \
                --ca infra \
                --common-name "healthcheck-client" \
                --usage clientAuth \
                --cert-out certs/client.pem \
                --key-out certs/client.key \
                --relative-to "$PWD"

            bash ../acps-infra/dev-infra/dev-cert.sh export-ca \
                --ca bundle \
                --bundle-out certs/trust-bundle.pem \
                --relative-to "$PWD"

            for cert_path in certs/server.pem certs/server.key certs/trust-bundle.pem certs/client.pem certs/client.key; do
                if [[ ! -f "${cert_path}" ]]; then
                    echo "[ERROR] 开发证书签发完成后仍缺少文件：${cert_path}" >&2
                    exit 1
                fi
            done
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

            if [[ "${migrate_target}" == "dev" ]]; then
                run_uv_with_mutating_cache uv run alembic upgrade head
            else
                local test_database_url=""
                test_database_url="$(resolve_test_database_url)"
                validate_test_database_url "${test_database_url}"
                APP_ENV=testing DATABASE_URL="${test_database_url}" TEST_DATABASE_URL="${test_database_url}" run_uv_with_mutating_cache uv run alembic upgrade head
            fi
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
            echo "  certs    直接调用共享开发 PKI 签发本地 mTLS 开发证书"
            ;;
        examples)
            echo "  just prep certs"
            ;;
    esac
}

load_registry_cert_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/cert-lib.sh"
}

load_registry_db_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/db-lib.sh"
}

load_registry_pytest_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/pytest-lib.sh"
}

load_registry_app_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/app-lib.sh"
}

registry_run_venv_python() {
    run_uv_with_mutating_cache uv run --no-sync python "$@"
}

registry_resolve_mtls_listener_enabled() {
    if registry_run_venv_python -c 'from app.core.config import settings; print("true" if settings.enable_mtls_listener else "false")' 2>/dev/null; then
        return 0
    fi

    printf 'true\n'
}

registry_resolve_oidc_enabled() {
    if registry_run_venv_python -c 'from app.core.config import settings; print("true" if settings.oidc_enabled else "false")' 2>/dev/null; then
        return 0
    fi

    printf 'false\n'
}

registry_resolve_test_database_url() {
    load_dotenv

    if [[ -n "${TEST_DATABASE_URL:-}" ]]; then
        printf '%s\n' "${TEST_DATABASE_URL}"
        return
    fi

    registry_run_venv_python -c 'from tests.support.constants import DEFAULT_TEST_DATABASE_DSN; print(DEFAULT_TEST_DATABASE_DSN)'
}

registry_validate_test_database_url() {
    local test_database_url="$1"

    TEST_DATABASE_URL="${test_database_url}" registry_run_venv_python -c 'import os; from sqlalchemy.engine import make_url; dsn = os.environ["TEST_DATABASE_URL"]; db = make_url(dsn).database; raise SystemExit(0 if db and db != "agent_registry" else 1)'
}

registry_check_test_database_connectivity() {
    local test_database_url="$1"

    TEST_DATABASE_URL="${test_database_url}" registry_run_venv_python -c 'import os, psycopg; from sqlalchemy.engine import make_url; url = make_url(os.environ["TEST_DATABASE_URL"]); conn = psycopg.connect(host=url.host, port=url.port or 5432, user=url.username, password=url.password, dbname=url.database); cur = conn.cursor(); cur.execute("SELECT 1"); cur.fetchone(); cur.close(); conn.close()'
}

registry_check_test_database_schema_ready() {
    local test_database_url="$1"

    TEST_DATABASE_URL="${test_database_url}" registry_run_venv_python -c 'import os, psycopg; from sqlalchemy.engine import make_url; url = make_url(os.environ["TEST_DATABASE_URL"]); conn = psycopg.connect(host=url.host, port=url.port or 5432, user=url.username, password=url.password, dbname=url.database); cur = conn.cursor(); cur.execute("SELECT version_num FROM alembic_version LIMIT 1"); row = cur.fetchone(); cur.close(); conn.close(); raise SystemExit(0 if row and row[0] else 1)'
}

registry_build_test_env_prefix() {
    local test_database_url="$1"
    local env_parts=(
        "APP_ENV=testing"
        "DATABASE_URL=$(printf '%q' "${test_database_url}")"
        "TEST_DATABASE_URL=$(printf '%q' "${test_database_url}")"
    )
    local passthrough_vars=(
        CA_SERVER_MOCK
        REGISTRY_OIDC_ENABLED
        REGISTRY_OIDC_ISSUER
        REGISTRY_OIDC_AUDIENCE
        REGISTRY_OIDC_ALLOWED_AZP
        REGISTRY_OIDC_CLIENT_ID
        REGISTRY_OIDC_ALGORITHMS
        REGISTRY_OIDC_JWKS_CACHE_TTL_SECONDS
        REGISTRY_OIDC_DISCOVERY_CACHE_TTL_SECONDS
        REGISTRY_OIDC_LEEWAY_SECONDS
        REGISTRY_OIDC_REQUIRE_HTTPS
        REGISTRY_OIDC_ROLE_SOURCE_CLIENT_ID
    )
    local var_name=""
    local var_value=""

    for var_name in "${passthrough_vars[@]}"; do
        var_value="${!var_name:-}"
        if [[ -n "${var_value}" ]]; then
            env_parts+=("${var_name}=$(printf '%q' "${var_value}")")
        fi
    done

    printf '%s ' "${env_parts[@]}"
}

_fix_registry_dev_certs() {
    run_project_prep_action certs
}

_fix_registry_oidc_keycloak() {
    just infra up keycloak
    just infra wait keycloak
}

describe_project_check_step() {
    local check_id="$1"

    case "${check_id}" in
        registry-dev-certs)
            echo "当 mTLS listener 启用时检查本地开发证书与 probe 证书已就绪；需要修复时等价于 just prep certs。"
            ;;
        registry-oidc-keycloak)
            echo "当 OIDC 启用时检查共享 Keycloak 已启动且健康；需要修复时等价于 just infra up keycloak && just infra wait keycloak。"
            ;;
        registry-test-db-ready)
            echo "检查 TEST_DATABASE_URL 指向独立测试库、数据库可连接且 schema 已迁移完成。"
            ;;
        *)
            return 1
            ;;
    esac
}

run_project_check() {
    local check_id="$1"
    local oidc_enabled=""
    local mtls_enabled=""
    local test_database_url=""

    case "${check_id}" in
        registry-dev-certs)
            mtls_enabled="$(registry_resolve_mtls_listener_enabled)"
            if [[ "${mtls_enabled}" != "true" ]]; then
                emit_check_result "${check_id}" ready info project "mTLS listener 未启用，跳过开发证书检查。" ""
                return 0
            fi
            if [[ "${SKIP_LOCAL_MTLS_DOCTOR:-0}" == "1" ]]; then
                emit_check_result "${check_id}" ready info project "已跳过本地 mTLS listener 证书检查（SKIP_LOCAL_MTLS_DOCTOR=1）。" ""
                return 0
            fi
            load_registry_cert_helpers
            check_cert_group_files_ready "${check_id}" project \
                "${local_mtls_cert_file}" \
                "${local_mtls_key_file}" \
                "${local_mtls_ca_cert_file}" \
                "${local_mtls_probe_cert_file}" \
                "${local_mtls_probe_key_file}"
            ;;
        registry-oidc-keycloak)
            oidc_enabled="$(registry_resolve_oidc_enabled)"
            if [[ "${oidc_enabled}" != "true" ]]; then
                emit_check_result "${check_id}" ready info project "OIDC 未启用，跳过 Keycloak 检查。" ""
                return 0
            fi
            check_infra_service_ready keycloak "${check_id}"
            ;;
        registry-test-db-ready)
            test_database_url="$(registry_resolve_test_database_url)"
            if ! registry_validate_test_database_url "${test_database_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" invalid error project "TEST_DATABASE_URL 必须指向独立测试库，不能指向 agent_registry。" "修正 TEST_DATABASE_URL 后重试。"
                return 0
            fi
            if ! registry_check_test_database_connectivity "${test_database_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" missing error project "测试数据库不可连接。" "执行 just infra up postgres && just infra wait postgres，并检查 TEST_DATABASE_URL。"
                return 0
            fi
            if ! registry_check_test_database_schema_ready "${test_database_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" stale error project "测试数据库 schema 未准备好。" "执行 just prep migrate test。"
                return 0
            fi
            emit_check_result "${check_id}" ready info project "测试数据库已就绪。" ""
            ;;
        migrate-dev)
            load_registry_db_helpers
            check_alembic_at_head "${check_id}"
            ;;
        migrate-test)
            load_registry_db_helpers
            test_database_url="$(registry_resolve_test_database_url)"
            check_alembic_at_head "${check_id}" "$(registry_build_test_env_prefix "${test_database_url}")"
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_ensure() {
    local check_id="$1"
    local test_database_url=""
    local mtls_enabled=""

    case "${check_id}" in
        registry-dev-certs)
            mtls_enabled="$(registry_resolve_mtls_listener_enabled)"
            if [[ "${mtls_enabled}" != "true" ]]; then
                return 0
            fi
            load_registry_cert_helpers
            ensure_cert_group_files_ready "${check_id}" _fix_registry_dev_certs \
                "${local_mtls_cert_file}" \
                "${local_mtls_key_file}" \
                "${local_mtls_ca_cert_file}" \
                "${local_mtls_probe_cert_file}" \
                "${local_mtls_probe_key_file}"
            ;;
        registry-oidc-keycloak)
            ensure_check_with_fix "${check_id}" _fix_registry_oidc_keycloak
            ;;
        migrate-dev)
            load_registry_db_helpers
            ensure_alembic_at_head "${check_id}"
            ;;
        migrate-test)
            load_registry_db_helpers
            test_database_url="$(registry_resolve_test_database_url)"
            ensure_alembic_at_head "${check_id}" "$(registry_build_test_env_prefix "${test_database_url}")"
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_dev_runtime() {
    local action="$1"
    shift || true
    local requested_args=("$@")
    local public_health_url="http://127.0.0.1:${app_port}/health"
    local mtls_health_url="https://127.0.0.1:${mtls_app_port}/health"

    load_registry_app_helpers

    build_extra_uvicorn_args() {
        if [[ "${#requested_args[@]}" -eq 0 ]]; then
            return
        fi

        printf '%q ' "${requested_args[@]}"
    }

    build_listener_pair_command() {
        local reload_flag="$1"
        local mtls_enabled="$2"
        local extra_uvicorn_args=""
        local app_module_q=""
        local mtls_entry_module_q=""
        local host_q=""
        local port_q=""
        local mtls_port_q=""
        local mtls_cert_file_q=""
        local mtls_key_file_q=""
        local mtls_ca_cert_file_q=""

        extra_uvicorn_args="$(build_extra_uvicorn_args)"
        app_module_q="$(printf '%q' "${app_module}")"
        mtls_entry_module_q="$(printf '%q' "${mtls_entry_module}")"
        host_q="$(printf '%q' "${app_host}")"
        port_q="$(printf '%q' "${app_port}")"
        mtls_port_q="$(printf '%q' "${mtls_app_port}")"
        mtls_cert_file_q="$(printf '%q' "${local_mtls_cert_file}")"
        mtls_key_file_q="$(printf '%q' "${local_mtls_key_file}")"
        mtls_ca_cert_file_q="$(printf '%q' "${local_mtls_ca_cert_file}")"

        if [[ "${mtls_enabled}" != "true" ]]; then
            printf '%s\n' \
                'set -eu -o pipefail' \
                'public_pid=""' \
                'cleanup() {' \
                '    if [[ -n "${public_pid}" ]] && kill -0 "${public_pid}" 2>/dev/null; then' \
                '        kill "${public_pid}" 2>/dev/null || true' \
                '    fi' \
                '    if [[ -n "${public_pid}" ]]; then' \
                '        wait "${public_pid}" 2>/dev/null || true' \
                '    fi' \
                '}' \
                'trap cleanup EXIT TERM INT' \
                "uv run uvicorn ${app_module_q} ${reload_flag} --host ${host_q} --port ${port_q}${extra_uvicorn_args:+ ${extra_uvicorn_args}} &" \
                'public_pid=$!' \
                'wait "${public_pid}"' \
                'exit $?'
            return
        fi

        printf '%s\n' \
            'set -eu -o pipefail' \
            'public_pid=""' \
            'mtls_pid=""' \
            'cleanup() {' \
            '    if [[ -n "${public_pid}" ]] && kill -0 "${public_pid}" 2>/dev/null; then' \
            '        kill "${public_pid}" 2>/dev/null || true' \
            '    fi' \
            '    if [[ -n "${mtls_pid}" ]] && kill -0 "${mtls_pid}" 2>/dev/null; then' \
            '        kill "${mtls_pid}" 2>/dev/null || true' \
            '    fi' \
            '    if [[ -n "${public_pid}" ]]; then' \
            '        wait "${public_pid}" 2>/dev/null || true' \
            '    fi' \
            '    if [[ -n "${mtls_pid}" ]]; then' \
            '        wait "${mtls_pid}" 2>/dev/null || true' \
            '    fi' \
            '}' \
            'trap cleanup EXIT TERM INT' \
            "uv run uvicorn ${app_module_q} ${reload_flag} --host ${host_q} --port ${port_q}${extra_uvicorn_args:+ ${extra_uvicorn_args}} &" \
            'public_pid=$!' \
            "REGISTRY_SERVER_MTLS_CERT_FILE=${mtls_cert_file_q} REGISTRY_SERVER_MTLS_KEY_FILE=${mtls_key_file_q} REGISTRY_SERVER_MTLS_CA_CERT_FILE=${mtls_ca_cert_file_q} REGISTRY_SERVER_MTLS_PORT=${mtls_port_q} uv run python -m ${mtls_entry_module_q} &" \
            'mtls_pid=$!' \
            'while kill -0 "${public_pid}" 2>/dev/null && kill -0 "${mtls_pid}" 2>/dev/null; do' \
            '    sleep 1' \
            'done' \
            'if ! kill -0 "${public_pid}" 2>/dev/null; then' \
            '    wait "${public_pid}"' \
            '    exit $?' \
            'fi' \
            'wait "${mtls_pid}"' \
            'exit $?'
    }

    case "${action}" in
        start)
            background=1
            if [[ "${#requested_args[@]}" -gt 0 ]] && [[ "${requested_args[0]}" == "fg" ]]; then
                background=0
                requested_args=("${requested_args[@]:1}")
            elif [[ "${#requested_args[@]}" -gt 0 ]] && [[ "${requested_args[0]}" == "bg" ]]; then
                background=1
                requested_args=("${requested_args[@]:1}")
            fi

            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                echo "[ERROR] 未知 dev start 参数：${requested_args[0]}。支持：bg / fg" >&2
                exit 2
            fi

            if [[ "${background}" -eq 1 ]]; then
                if pid="$(read_live_pid "${app_pid_file}")"; then
                    echo "[INFO]  ${project_name} 已在后台运行（pid=${pid}）。"
                    return 0
                fi
                just dev bootstrap
                ensure_log_dir_for_file "${app_log_file}"

                mtls_enabled="$(registry_resolve_mtls_listener_enabled)"
                command_string="$(build_listener_pair_command "--reload" "${mtls_enabled}")"
                pid="$(launch_detached "${app_log_file}" bash -c "${command_string}")"
                printf '%s\n' "${pid}" > "${app_pid_file}"

                sleep 3
                if ! kill -0 "${pid}" 2>/dev/null; then
                    rm -f "${app_pid_file}"
                    echo "[ERROR] ${project_name} 后台启动失败，请检查日志：${app_log_file}" >&2
                    exit 1
                fi
                echo "[INFO]  ${project_name} 已在后台启动（pid=${pid}）。"
                echo "[INFO]  日志文件：${app_log_file}"
                echo "[INFO]  Public 平面：http://localhost:${app_port}"
                if [[ "${mtls_enabled}" == "true" ]]; then
                    echo "[INFO]  mTLS 平面：https://localhost:${mtls_app_port}"
                fi
            else
                if pid="$(read_live_pid "${app_pid_file}")"; then
                    echo "[ERROR] ${project_name} 已有后台实例在运行（pid=${pid}）。请先执行 just dev stop，再执行 just dev start fg。" >&2
                    exit 2
                fi
                just dev bootstrap
                ensure_log_dir_for_file "${app_log_file}"
                mtls_enabled="$(registry_resolve_mtls_listener_enabled)"
                command_string="$(build_listener_pair_command "--reload" "${mtls_enabled}")"
                if [[ "${mtls_enabled}" == "true" ]]; then
                    echo "[INFO]  启动 ${project_name}（前台，热重载，双 listener）..."
                else
                    echo "[INFO]  启动 ${project_name}（前台，热重载，public listener）..."
                fi
                echo "[INFO]  OpenAPI 文档：http://localhost:${app_port}/docs"
                echo "[INFO]  Public 平面健康检查：${public_health_url}"
                if [[ "${mtls_enabled}" == "true" ]]; then
                    echo "[INFO]  mTLS 平面健康检查：${mtls_health_url}"
                fi
                exec bash -c "${command_string}"
            fi
            ;;
        stop)
            if pid="$(read_live_pid "${app_pid_file}")"; then
                kill_wait "${pid}"
                rm -f "${app_pid_file}"
                echo "[INFO]  已停止 ${project_name} 后台实例（pid=${pid}）。"
            else
                echo "[INFO]  ${project_name} 当前没有后台实例。"
            fi
            kill_port "${app_port}"
            kill_port "${mtls_app_port}"
            ;;
        status)
            if pid="$(read_live_pid "${app_pid_file}")"; then
                echo "[INFO]  ${project_name} 正在运行（pid=${pid}）。"
            else
                echo "[INFO]  ${project_name} 当前未运行。"
            fi
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
            run_project_dev_runtime stop
            if [[ "${#requested_args[@]}" -eq 0 ]] || [[ "${requested_args[0]}" == "bg" ]]; then
                run_project_dev_runtime start
            else
                echo "[ERROR] dev restart 只管理后台实例；如需前台运行，请直接执行 just dev start fg。" >&2
                exit 2
            fi
            ;;
        *)
            return 127
            ;;
    esac
}

registry_export_local_oidc_test_env() {
    export REGISTRY_OIDC_ENABLED="${REGISTRY_OIDC_ENABLED:-true}"
    export REGISTRY_OIDC_ISSUER="${REGISTRY_OIDC_ISSUER:-http://localhost:9080/realms/acps-registry}"
    export REGISTRY_OIDC_AUDIENCE="${REGISTRY_OIDC_AUDIENCE:-registry-api}"
    export REGISTRY_OIDC_ALLOWED_AZP="${REGISTRY_OIDC_ALLOWED_AZP:-registry-e2e,registry-cli}"
    export REGISTRY_OIDC_CLIENT_ID="${REGISTRY_OIDC_CLIENT_ID:-registry-api}"
    export REGISTRY_OIDC_ALGORITHMS="${REGISTRY_OIDC_ALGORITHMS:-EdDSA}"
    export REGISTRY_OIDC_REQUIRE_HTTPS="${REGISTRY_OIDC_REQUIRE_HTTPS:-false}"
    export REGISTRY_OIDC_ROLE_SOURCE_CLIENT_ID="${REGISTRY_OIDC_ROLE_SOURCE_CLIENT_ID:-registry-api}"
    export TEST_OIDC_ISSUER="${TEST_OIDC_ISSUER:-${REGISTRY_OIDC_ISSUER}}"
    export TEST_OIDC_E2E_CLIENT_ID="${TEST_OIDC_E2E_CLIENT_ID:-registry-e2e}"
    export TEST_OIDC_CLIENT_USERNAME="${TEST_OIDC_CLIENT_USERNAME:-registry-client}"
    export TEST_OIDC_CLIENT_PASSWORD="${TEST_OIDC_CLIENT_PASSWORD:-demo123}"
    export TEST_OIDC_ADMIN_USERNAME="${TEST_OIDC_ADMIN_USERNAME:-registry-admin}"
    export TEST_OIDC_ADMIN_PASSWORD="${TEST_OIDC_ADMIN_PASSWORD:-demo123}"
}

registry_run_coverage_tests() {
    run_uv_with_mutating_cache uv run pytest --cov=app --cov-report=term-missing --cov-report=html tests/unit/ tests/integration/
}

registry_stop_test_server() {
    if [[ -n "${registry_test_server_pid:-}" ]] && kill -0 "${registry_test_server_pid}" 2>/dev/null; then
        kill "${registry_test_server_pid}" 2>/dev/null || true
        wait "${registry_test_server_pid}" 2>/dev/null || true
        echo "[INFO]  已停止临时测试实例。"
    fi

    if [[ -n "${registry_test_server_log:-}" ]] && [[ -f "${registry_test_server_log}" ]]; then
        rm -f "${registry_test_server_log}"
    fi
}

registry_check_test_server_health() {
    registry_run_venv_python scripts/check_test_server_health.py "${registry_test_server_base_url}" "${registry_test_server_mtls_base_url}"
}

registry_wait_for_test_server() {
    local attempt=""

    for attempt in {1..30}; do
        if registry_check_test_server_health; then
            return 0
        fi

        if ! kill -0 "${registry_test_server_pid}" 2>/dev/null; then
            echo "[ERROR] 临时测试实例启动失败。" >&2
            cat "${registry_test_server_log}" >&2
            return 1
        fi

        sleep 1
    done

    echo "[ERROR] 等待临时测试实例就绪超时：${registry_test_server_base_url}" >&2
    cat "${registry_test_server_log}" >&2
    return 1
}

registry_start_test_server() {
    local test_database_url="$1"
    local command_string=""
    local test_server_env_prefix=""
    local test_server_mtls_port=""
    local test_server_port=""
    local app_module_q=""
    local mtls_entry_module_q=""
    local host_q=""
    local port_q=""
    local mtls_port_q=""
    local mtls_cert_file_q=""
    local mtls_key_file_q=""
    local mtls_ca_cert_file_q=""

    test_server_port="$(find_free_port)"
    test_server_mtls_port="$(find_free_port)"
    registry_test_server_base_url="http://${test_server_host}:${test_server_port}"
    registry_test_server_mtls_base_url="https://${test_server_host}:${test_server_mtls_port}"
    validate_test_e2e_base_url "${registry_test_server_base_url}"
    validate_test_e2e_base_url "${registry_test_server_mtls_base_url}"
    registry_test_server_log="$(mktemp "${TMPDIR:-/tmp}/registry-server-test.XXXXXX")"
    app_module_q="$(printf '%q' "${app_module}")"
    mtls_entry_module_q="$(printf '%q' "${mtls_entry_module}")"
    host_q="$(printf '%q' "${test_server_host}")"
    port_q="$(printf '%q' "${test_server_port}")"
    mtls_port_q="$(printf '%q' "${test_server_mtls_port}")"
    mtls_cert_file_q="$(printf '%q' "${local_mtls_cert_file}")"
    mtls_key_file_q="$(printf '%q' "${local_mtls_key_file}")"
    mtls_ca_cert_file_q="$(printf '%q' "${local_mtls_ca_cert_file}")"
    test_server_env_prefix="$(registry_build_test_env_prefix "${test_database_url}")"

    echo "[INFO]  启动临时测试实例：${registry_test_server_base_url}（public） / ${registry_test_server_mtls_base_url}（mtls）"

    command_string="$(printf '%s\n' \
        'set -eu -o pipefail' \
        'public_pid=""' \
        'mtls_pid=""' \
        'cleanup() {' \
        '    if [[ -n "${public_pid}" ]] && kill -0 "${public_pid}" 2>/dev/null; then' \
        '        kill "${public_pid}" 2>/dev/null || true' \
        '    fi' \
        '    if [[ -n "${mtls_pid}" ]] && kill -0 "${mtls_pid}" 2>/dev/null; then' \
        '        kill "${mtls_pid}" 2>/dev/null || true' \
        '    fi' \
        '    if [[ -n "${public_pid}" ]]; then' \
        '        wait "${public_pid}" 2>/dev/null || true' \
        '    fi' \
        '    if [[ -n "${mtls_pid}" ]]; then' \
        '        wait "${mtls_pid}" 2>/dev/null || true' \
        '    fi' \
        '}' \
        'trap cleanup EXIT TERM INT' \
        "${test_server_env_prefix}uv run uvicorn ${app_module_q} --host ${host_q} --port ${port_q} &" \
        'public_pid=$!' \
        "${test_server_env_prefix}REGISTRY_SERVER_MTLS_CERT_FILE=${mtls_cert_file_q} REGISTRY_SERVER_MTLS_KEY_FILE=${mtls_key_file_q} REGISTRY_SERVER_MTLS_CA_CERT_FILE=${mtls_ca_cert_file_q} REGISTRY_SERVER_MTLS_PORT=${mtls_port_q} uv run python -m ${mtls_entry_module_q} &" \
        'mtls_pid=$!' \
        'while kill -0 "${public_pid}" 2>/dev/null && kill -0 "${mtls_pid}" 2>/dev/null; do' \
        '    sleep 1' \
        'done' \
        'if ! kill -0 "${public_pid}" 2>/dev/null; then' \
        '    wait "${public_pid}"' \
        '    exit $?' \
        'fi' \
        'wait "${mtls_pid}"' \
        'exit $?')"

    bash -c "${command_string}" >"${registry_test_server_log}" 2>&1 &

    registry_test_server_pid="$!"
    registry_wait_for_test_server
}

run_project_test_action() {
    local action="$1"
    shift || true

    load_registry_pytest_helpers

    case "${action}" in
        unit)
            if [[ "$#" -gt 0 ]]; then
                run_requested_tests tests/unit/ "" "$@"
            else
                run_requested_tests tests/unit/
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
        coverage)
            just test bootstrap
            registry_run_coverage_tests
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
    local test_database_url=""
    local test_target="tests/e2e/"

    load_registry_pytest_helpers

    case "${profile}" in
        local)
            ;;
        oidc)
            registry_export_local_oidc_test_env
            just infra up keycloak
            test_target="tests/e2e/test_oidc_keycloak_flow.py"
            ;;
        *)
            return 127
            ;;
    esac

    just test bootstrap
    test_database_url="$(registry_resolve_test_database_url)"
    trap registry_stop_test_server EXIT
    registry_start_test_server "${test_database_url}"

    if [[ "$#" -gt 0 ]]; then
        TEST_E2E_MTLS_BASE_URL="${registry_test_server_mtls_base_url}" run_requested_tests "${test_target}" "${registry_test_server_base_url}" "$@"
    else
        TEST_E2E_MTLS_BASE_URL="${registry_test_server_mtls_base_url}" run_requested_tests "${test_target}" "${registry_test_server_base_url}"
    fi
}
