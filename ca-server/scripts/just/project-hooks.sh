#!/usr/bin/env bash

: "${app_module:=app.main:app}"
: "${test_server_host:=127.0.0.1}"
: "${default_test_database_url:=postgresql://ca:ca@localhost:5432/agent_ca_test}"

configure_project_package_runtime() {
    PACKAGE_RUNTIME_REQUIRED_PATHS=(
        config
        alembic
        alembic.ini
        .env.example
        README.md
        pyproject.toml
    )

    PACKAGE_RUNTIME_SIBLING_REPOS=()

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

    PACKAGE_RUNTIME_COMPONENTS=(
        "ca-server-api|python-service|uvicorn app.main:app --host 0.0.0.0 --port 9003|9003|http://127.0.0.1:9003/health||config/production.toml"
    )
    PACKAGE_RUNTIME_INTERNAL_WHEELS=()
    PACKAGE_RUNTIME_EXTERNAL_COMPONENTS=(postgresql)
}

load_ca_cert_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/cert-lib.sh"
}

load_ca_db_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/db-lib.sh"
}

load_ca_pytest_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/pytest-lib.sh"
}

ca_resolve_path() {
    local path="$1"

    if [[ "${path}" = /* ]]; then
        printf '%s\n' "${path}"
    else
        printf '%s\n' "$PWD/${path}"
    fi
}

ca_run_venv_python() {
    run_uv_with_mutating_cache uv run --no-sync python "$@"
}

ca_validate_database_url_value() {
    local database_url="$1"

    [[ -n "${database_url}" ]] || return 1
    ca_run_venv_python -c 'from urllib.parse import urlsplit; import sys; parts = urlsplit(sys.argv[1]); ok = bool(parts.scheme and parts.username and parts.password not in (None, "") and parts.hostname and parts.path and parts.path != "/"); raise SystemExit(0 if ok else 1)' "${database_url}"
}

ca_get_database_name_from_url() {
    ca_run_venv_python -c 'from urllib.parse import urlsplit; import re, sys; database_name = urlsplit(sys.argv[1]).path.lstrip("/"); valid = bool(database_name) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database_name) is not None; sys.stdout.write(database_name + "\n") if valid else sys.exit(1)' "$1"
}

ca_resolve_test_database_url() {
    load_dotenv

    if [[ -n "${TEST_DATABASE_URL:-}" ]]; then
        printf '%s\n' "${TEST_DATABASE_URL}"
        return
    fi

    printf '%s\n' "${default_test_database_url}"
}

ca_resolve_development_database_url() {
    load_dotenv
    printf '%s\n' "${DATABASE_URL:-}"
}

ca_validate_test_database_url() {
    local test_database_url="$1"
    local development_database_url="$2"
    local test_database_name=""
    local development_database_name=""

    if ! ca_validate_database_url_value "${test_database_url}"; then
        return 1
    fi

    if ! test_database_name="$(ca_get_database_name_from_url "${test_database_url}")"; then
        return 1
    fi

    if [[ "${test_database_name}" != "agent_ca_test" ]]; then
        return 1
    fi

    if [[ -n "${development_database_url}" ]]; then
        if ! development_database_name="$(ca_get_database_name_from_url "${development_database_url}")"; then
            return 1
        fi
        if [[ "${development_database_name}" == "${test_database_name}" ]]; then
            return 1
        fi
    fi

    return 0
}

ca_check_test_database_connectivity() {
    local test_database_url="$1"

    TEST_DATABASE_URL="${test_database_url}" ca_run_venv_python -c 'import os, psycopg; from sqlalchemy.engine import make_url; url = make_url(os.environ["TEST_DATABASE_URL"]); conn = psycopg.connect(host=url.host, port=url.port or 5432, user=url.username, password=url.password, dbname=url.database); cur = conn.cursor(); cur.execute("SELECT 1"); cur.fetchone(); cur.close(); conn.close()'
}

ca_check_test_database_schema_ready() {
    local test_database_url="$1"

    TEST_DATABASE_URL="${test_database_url}" ca_run_venv_python -c 'import os, psycopg; from sqlalchemy.engine import make_url; url = make_url(os.environ["TEST_DATABASE_URL"]); conn = psycopg.connect(host=url.host, port=url.port or 5432, user=url.username, password=url.password, dbname=url.database); cur = conn.cursor(); cur.execute("SELECT version_num FROM alembic_version LIMIT 1"); row = cur.fetchone(); cur.close(); conn.close(); raise SystemExit(0 if row and row[0] else 1)'
}

ca_build_test_env_prefix() {
    local test_database_url=""

    test_database_url="$(ca_resolve_test_database_url)"
    printf 'APP_ENV=testing DATABASE_URL=%q TEST_DATABASE_URL=%q' "${test_database_url}" "${test_database_url}"
}

ca_build_ca_material() {
    run_project_prep_action certs
}

describe_project_check_step() {
    local check_id="$1"

    case "${check_id}" in
        ca-dev-database-url)
            echo "检查 .env 中 DATABASE_URL 是否为带用户名、密码、主机和数据库名的完整开发库连接串。"
            ;;
        ca-dev-certs)
            echo "检查本地开发业务中间 CA 证书链（ca.crt / ca.key / ca-chain.pem / trust-bundle.pem）已生成；需要修复时等价于 just prep certs。"
            ;;
        ca-test-db-ready)
            echo "检查 TEST_DATABASE_URL 指向独立测试库 agent_ca_test，且测试库可连接并已迁移到最新 schema。"
            ;;
        *)
            return 1
            ;;
    esac
}

run_project_check() {
    local check_id="$1"
    local database_url=""
    local development_database_url=""
    local test_database_url=""
    local cert_path=""
    local key_path=""
    local chain_path=""
    local bundle_path=""

    case "${check_id}" in
        ca-dev-database-url)
            if [[ "${SKIP_DATABASE_URL_DOCTOR:-0}" == "1" ]]; then
                emit_check_result "${check_id}" ready info project "已跳过 DATABASE_URL 检查（SKIP_DATABASE_URL_DOCTOR=1）。" ""
                return 0
            fi

            load_dotenv
            database_url="${DATABASE_URL:-}"
            if [[ -z "${database_url}" ]]; then
                emit_check_result "${check_id}" missing error project "DATABASE_URL 未配置。" "补齐 .env 中的 DATABASE_URL。"
                return 0
            fi
            if ! ca_validate_database_url_value "${database_url}"; then
                emit_check_result "${check_id}" invalid error project "DATABASE_URL 配置不完整。" "参考 .env.example 使用带用户名、密码、主机和数据库名的完整 URL。"
                return 0
            fi
            emit_check_result "${check_id}" ready info project "DATABASE_URL 配置有效。" ""
            ;;
        ca-dev-certs)
            load_ca_cert_helpers
            load_dotenv
            cert_path="$(ca_resolve_path "${CA_CERT_PATH:-certs/ca.crt}")"
            key_path="$(ca_resolve_path "${CA_KEY_PATH:-certs/ca.key}")"
            chain_path="$(ca_resolve_path "${CA_CHAIN_PATH:-certs/ca-chain.pem}")"
            bundle_path="$(ca_resolve_path "${TRUST_BUNDLE_PATH:-certs/trust-bundle.pem}")"
            check_cert_group_files_ready "${check_id}" project "${cert_path}" "${key_path}" "${chain_path}" "${bundle_path}"
            ;;
        ca-test-db-ready)
            test_database_url="$(ca_resolve_test_database_url)"
            development_database_url="$(ca_resolve_development_database_url)"
            if ! ca_validate_test_database_url "${test_database_url}" "${development_database_url}"; then
                emit_check_result "${check_id}" invalid error project "TEST_DATABASE_URL 无效，或与开发库冲突。" "确保 TEST_DATABASE_URL 指向独立测试库 agent_ca_test。"
                return 0
            fi
            if ! ca_check_test_database_connectivity "${test_database_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" missing error project "测试数据库不可连接。" "执行 just infra up postgres && just infra wait postgres，并检查 TEST_DATABASE_URL。"
                return 0
            fi
            if ! ca_check_test_database_schema_ready "${test_database_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" stale error project "测试数据库 schema 未准备好。" "执行 just prep migrate test。"
                return 0
            fi
            emit_check_result "${check_id}" ready info project "测试数据库已就绪。" ""
            ;;
        migrate-dev)
            load_ca_db_helpers
            check_alembic_at_head "${check_id}"
            ;;
        migrate-test)
            load_ca_db_helpers
            check_alembic_at_head "${check_id}" "$(ca_build_test_env_prefix)"
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_ensure() {
    local check_id="$1"
    local cert_path=""
    local key_path=""
    local chain_path=""
    local bundle_path=""

    case "${check_id}" in
        ca-dev-certs)
            load_ca_cert_helpers
            load_dotenv
            cert_path="$(ca_resolve_path "${CA_CERT_PATH:-certs/ca.crt}")"
            key_path="$(ca_resolve_path "${CA_KEY_PATH:-certs/ca.key}")"
            chain_path="$(ca_resolve_path "${CA_CHAIN_PATH:-certs/ca-chain.pem}")"
            bundle_path="$(ca_resolve_path "${TRUST_BUNDLE_PATH:-certs/trust-bundle.pem}")"
            ensure_cert_group_files_ready "${check_id}" ca_build_ca_material "${cert_path}" "${key_path}" "${chain_path}" "${bundle_path}"
            ;;
        migrate-dev)
            load_ca_db_helpers
            ensure_alembic_at_head "${check_id}" "" "uv run alembic upgrade head"
            ;;
        migrate-test)
            load_ca_db_helpers
            ensure_alembic_at_head "${check_id}" "$(ca_build_test_env_prefix)" "uv run alembic upgrade head"
            ;;
        *)
            return 127
            ;;
    esac
}

ca_wait_for_test_server() {
    local test_server_base_url="$1"
    local test_server_pid="$2"
    local test_server_log="$3"
    local attempt=""

    for attempt in {1..30}; do
        if curl -fsS "${test_server_base_url}/health" >/dev/null 2>&1; then
            return 0
        fi

        if ! kill -0 "${test_server_pid}" 2>/dev/null; then
            echo "[ERROR] 临时测试实例启动失败。" >&2
            cat "${test_server_log}" >&2
            return 1
        fi

        sleep 1
    done

    echo "[ERROR] 等待临时测试实例就绪超时：${test_server_base_url}" >&2
    cat "${test_server_log}" >&2
    return 1
}

ca_test_server_pid=""
ca_test_server_log=""

ca_stop_test_server() {
    if [[ -n "${ca_test_server_pid:-}" ]] && kill -0 "${ca_test_server_pid}" 2>/dev/null; then
        kill "${ca_test_server_pid}" 2>/dev/null || true
        wait "${ca_test_server_pid}" 2>/dev/null || true
        echo "[INFO]  已停止临时测试实例。"
    fi

    if [[ -n "${ca_test_server_log:-}" ]] && [[ -f "${ca_test_server_log}" ]]; then
        rm -f "${ca_test_server_log}"
    fi

    ca_test_server_pid=""
    ca_test_server_log=""
}

run_project_test_action() {
    local action="$1"
    shift || true
    local requested_args=("$@")
    local test_database_url=""
    local development_database_url=""

    load_ca_pytest_helpers

    case "${action}" in
        unit)
            if [[ "${#requested_args[@]}" -eq 0 ]]; then
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/unit/ --cov=app --cov-report=term-missing --cov-fail-under=0
            elif [[ "${requested_args[0]}" == -* ]]; then
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/unit/ "${requested_args[@]}" --cov=app --cov-report=term-missing --cov-fail-under=0
            else
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest "${requested_args[@]}" --cov=app --cov-report=term-missing --cov-fail-under=0
            fi
            ;;
        integration)
            just test bootstrap
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                run_requested_tests tests/integration/ "${TEST_E2E_BASE_URL:-}" "${requested_args[@]}"
            else
                run_requested_tests tests/integration/ "${TEST_E2E_BASE_URL:-}"
            fi
            ;;
        coverage)
            if [[ "${#requested_args[@]}" -eq 0 ]]; then
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/unit/ --cov=app --cov-report=term-missing --cov-fail-under=70
            elif [[ "${requested_args[0]}" == -* ]]; then
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/unit/ "${requested_args[@]}" --cov=app --cov-report=term-missing --cov-fail-under=70
            else
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest "${requested_args[@]}" --cov=app --cov-report=term-missing --cov-fail-under=70
            fi
            ;;
        coverage-report)
            just test bootstrap
            test_database_url="$(ca_resolve_test_database_url)"
            development_database_url="$(ca_resolve_development_database_url)"
            if ! ca_validate_test_database_url "${test_database_url}" "${development_database_url}"; then
                echo "[ERROR] TEST_DATABASE_URL 无效，无法生成 coverage-report。" >&2
                return 1
            fi
            APP_ENV=testing DATABASE_URL="${test_database_url}" TEST_DATABASE_URL="${test_database_url}" \
                run_uv_with_mutating_cache uv run pytest --cov=app --cov-report=term-missing --cov-report=html tests/unit/ tests/integration/
            ;;
        all)
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                just test unit "${requested_args[@]}"
                just test integration "${requested_args[@]}"
                just test e2e "${requested_args[@]}"
                just test coverage "${requested_args[@]}"
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
    local requested_args=("$@")
    local host="${test_server_host:-127.0.0.1}"
    local test_database_url=""
    local test_server_base_url=""
    local test_admin_token=""
    local test_internal_token=""
    local test_server_port=""

    load_ca_pytest_helpers

    case "${profile}" in
        local)
            just test bootstrap
            test_database_url="$(ca_resolve_test_database_url)"
            test_admin_token="${CA_SERVER_ADMIN_API_TOKEN:-local-ca-admin-token}"
            test_internal_token="${CA_SERVER_INTERNAL_API_TOKEN:-test-ca-internal-token}"
            test_server_port="$(find_free_port)"
            test_server_base_url="http://${host}:${test_server_port}"
            validate_test_e2e_base_url "${test_server_base_url}"
            ca_test_server_log="$(mktemp "${TMPDIR:-/tmp}/ca-server-test.XXXXXX")"

            echo "[INFO]  启动临时测试实例：${test_server_base_url}"

            APP_ENV=testing \
            DATABASE_URL="${test_database_url}" \
            TEST_DATABASE_URL="${test_database_url}" \
            CA_SERVER_ADMIN_API_TOKEN="${test_admin_token}" \
            CA_SERVER_INTERNAL_API_TOKEN="${test_internal_token}" \
            run_uv_with_mutating_cache uv run uvicorn "${app_module}" --host "${host}" --port "${test_server_port}" >"${ca_test_server_log}" 2>&1 &
            ca_test_server_pid="$!"
            trap ca_stop_test_server EXIT
            ca_wait_for_test_server "${test_server_base_url}" "${ca_test_server_pid}" "${ca_test_server_log}"

            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                run_requested_tests tests/e2e/ "${test_server_base_url}" "${requested_args[@]}"
            else
                run_requested_tests tests/e2e/ "${test_server_base_url}"
            fi
            ;;
        *)
            echo "[ERROR] 未知 e2e profile: ${profile}" >&2
            return 2
            ;;
    esac
}

supports_project_test_action() {
    local action="$1"

    case "${action}" in
        coverage-report)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

run_project_prep_action() {
    local action="$1"
    shift || true

    ensure_ca_material() {
        load_dotenv

        local ca_cert_path="${CA_CERT_PATH:-certs/ca.crt}"
        local ca_key_path="${CA_KEY_PATH:-certs/ca.key}"
        local ca_chain_path="${CA_CHAIN_PATH:-certs/ca-chain.pem}"
        local trust_bundle_path="${TRUST_BUNDLE_PATH:-certs/trust-bundle.pem}"
        local dev_pki_script="../acps-infra/dev-infra/dev-cert.sh"
        local certs_dir=""
        local existing=0

        ca_cert_path="$(ca_resolve_path "${ca_cert_path}")"
        ca_key_path="$(ca_resolve_path "${ca_key_path}")"
        ca_chain_path="$(ca_resolve_path "${ca_chain_path}")"
        trust_bundle_path="$(ca_resolve_path "${trust_bundle_path}")"
        certs_dir="$(dirname "${ca_cert_path}")"

        local all_files=(
            "${ca_cert_path}"
            "${ca_key_path}"
            "${ca_chain_path}"
            "${trust_bundle_path}"
        )

        local file_path=""
        for file_path in "${all_files[@]}"; do
            [[ -f "${file_path}" ]] && existing=$((existing + 1))
        done

        if [[ "${existing}" -eq 4 ]]; then
            echo "[INFO]  证书套件已完整存在（4/4），跳过同步。"
            for file_path in "${all_files[@]}"; do
                echo "[INFO]    ${file_path}"
            done
            return
        fi

        if [[ "${existing}" -gt 0 ]]; then
            echo "[ERROR] 证书套件不完整（${existing}/4 个文件存在），无法继续。" >&2
            echo "[ERROR] 请先清理 ${certs_dir}/ 中的旧证书文件，再重新执行 just prep certs。" >&2
            echo "[ERROR] 当前存在的文件：" >&2
            for file_path in "${all_files[@]}"; do
                [[ -f "${file_path}" ]] && echo "[ERROR]   ${file_path}" >&2
            done
            exit 1
        fi

        if [[ ! -x "${dev_pki_script}" ]]; then
            echo "[ERROR] 未找到共享开发 PKI 脚本：${dev_pki_script}" >&2
            exit 1
        fi

        echo "[INFO]  开始通过共享开发 PKI 导出业务中间 CA 套件（4 个持久化文件）..."
        bash "${dev_pki_script}" export-ca \
            --ca agent \
            --cert-out "${CA_CERT_PATH:-certs/ca.crt}" \
            --key-out "${CA_KEY_PATH:-certs/ca.key}" \
            --chain-out "${CA_CHAIN_PATH:-certs/ca-chain.pem}" \
            --bundle-out "${TRUST_BUNDLE_PATH:-certs/trust-bundle.pem}" \
            --relative-to "$PWD"

        echo "[INFO]  本地开发 CA 套件已同步（4/4）："
        for file_path in "${all_files[@]}"; do
            echo "[INFO]    ${file_path}"
        done
    }

    case "${action}" in
        certs)
            ensure_ca_material
            ;;
        migrate)
            local migrate_target="dev"
            local test_database_url=""
            local development_database_url=""

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
                test_database_url="$(ca_resolve_test_database_url)"
                development_database_url="$(ca_resolve_development_database_url)"
                if ! ca_validate_test_database_url "${test_database_url}" "${development_database_url}"; then
                    echo "[ERROR] TEST_DATABASE_URL 必须指向独立测试库 agent_ca_test，且不能与开发库冲突。" >&2
                    exit 1
                fi
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
        test)
            echo "  coverage-report  生成包含 HTML 输出的覆盖率报告"
            ;;
        prep)
            echo "  certs    检查并导出本地开发业务中间 CA 套件"
            ;;
    esac
}
