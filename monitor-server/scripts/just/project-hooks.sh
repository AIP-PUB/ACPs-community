#!/usr/bin/env bash

: "${app_module:=app.main:app}"
: "${test_server_host:=127.0.0.1}"

configure_project_package_runtime() {
    PACKAGE_RUNTIME_REQUIRED_PATHS=(
        config
        alembic
        alembic.ini
        .env.example
        README.md
        pyproject.toml
    )

    PACKAGE_RUNTIME_SIBLING_REPOS=(
        acps-sdk
    )

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
        "monitor-server-api|python-service|uvicorn app.main:app --host 0.0.0.0 --port 9009|9009|http://127.0.0.1:9009/health||config/production.toml"
    )
    PACKAGE_RUNTIME_INTERNAL_WHEELS=(acps-sdk)
    PACKAGE_RUNTIME_EXTERNAL_COMPONENTS=(postgresql kafka redis victoria-metrics)
}

run_project_package_filter_requirements() {
    local input_file="$1"
    local output_file="$2"

    awk '
        $0 == "-e ../acps-sdk" { skip_next_via = 1; next }
        skip_next_via == 1 && $0 ~ /^    # via / { skip_next_via = 0; next }
        { skip_next_via = 0; print }
    ' "${input_file}" > "${output_file}"
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

        if ! TEST_DATABASE_URL="${test_database_url}" run_uv_with_mutating_cache uv run python -c 'import os; from sqlalchemy.engine import make_url; dsn = os.environ["TEST_DATABASE_URL"]; db = make_url(dsn).database; raise SystemExit(0 if db and db.endswith("_test") else 1)'; then
            echo "[ERROR] TEST_DATABASE_URL 必须指向独立测试库（数据库名须以 _test 结尾）。" >&2
            exit 1
        fi
    }

    case "${action}" in
        migrate)
            local migrate_target="dev"
            local test_database_url=""

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
        test)
            echo "  coverage-report  生成包含 HTML 输出的覆盖率报告"
            ;;
    esac
}

load_monitor_db_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/db-lib.sh"
}

load_monitor_pytest_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/pytest-lib.sh"
}

monitor_run_venv_python() {
    run_uv_with_mutating_cache uv run --no-sync python "$@"
}

monitor_resolve_oidc_enabled() {
    local oidc_enabled_output=""

    if [[ -f .env ]] && command -v uv >/dev/null 2>&1; then
        if oidc_enabled_output="$(monitor_run_venv_python -c 'from app.core.config import settings; print("true" if settings.oidc_enabled else "false")' 2>/dev/null)"; then
            printf '%s\n' "${oidc_enabled_output}"
            return
        fi
    fi

    printf 'false\n'
}

monitor_resolve_test_database_url() {
    load_dotenv

    if [[ -n "${TEST_DATABASE_URL:-}" ]]; then
        printf '%s\n' "${TEST_DATABASE_URL}"
        return
    fi

    monitor_run_venv_python -c 'from tests.support.constants import DEFAULT_TEST_DATABASE_DSN; print(DEFAULT_TEST_DATABASE_DSN)'
}

monitor_validate_test_database_url() {
    local test_database_url="$1"

    TEST_DATABASE_URL="${test_database_url}" monitor_run_venv_python -c 'import os; from sqlalchemy.engine import make_url; dsn = os.environ["TEST_DATABASE_URL"]; db = make_url(dsn).database; raise SystemExit(0 if db and db.endswith("_test") else 1)'
}

monitor_check_test_database_connectivity() {
    local test_database_url="$1"

    TEST_DATABASE_URL="${test_database_url}" monitor_run_venv_python -c 'import os, psycopg; from sqlalchemy.engine import make_url; url = make_url(os.environ["TEST_DATABASE_URL"]); conn = psycopg.connect(host=url.host, port=url.port or 5432, user=url.username, password=url.password, dbname=url.database); cur = conn.cursor(); cur.execute("SELECT 1"); cur.fetchone(); cur.close(); conn.close()'
}

monitor_check_test_database_schema_ready() {
    local test_database_url="$1"

    TEST_DATABASE_URL="${test_database_url}" monitor_run_venv_python -c 'import os, psycopg; from sqlalchemy.engine import make_url; url = make_url(os.environ["TEST_DATABASE_URL"]); conn = psycopg.connect(host=url.host, port=url.port or 5432, user=url.username, password=url.password, dbname=url.database); cur = conn.cursor(); cur.execute("SELECT version_num FROM alembic_version LIMIT 1"); row = cur.fetchone(); cur.close(); conn.close(); raise SystemExit(0 if row and row[0] else 1)'
}

monitor_check_opensearch_connectivity() {
    monitor_run_venv_python -c 'import asyncio; from app.core.opensearch_client import check_opensearch; raise SystemExit(0 if asyncio.run(check_opensearch()) else 1)'
}

monitor_build_test_env_prefix() {
    local test_database_url=""

    test_database_url="$(monitor_resolve_test_database_url)"
    printf 'APP_ENV=testing DATABASE_URL=%q TEST_DATABASE_URL=%q' "${test_database_url}" "${test_database_url}"
}

_fix_monitor_oidc_keycloak() {
    just infra up keycloak
    just infra wait keycloak
}

describe_project_check_step() {
    local check_id="$1"

    case "${check_id}" in
        monitor-oidc-keycloak)
            echo "当 OIDC 启用时检查共享 Keycloak 已启动且健康；需要修复时等价于 just infra up keycloak && just infra wait keycloak。"
            ;;
        monitor-audit-keys)
            echo "检查本地 mock 审计密钥 config/audit_keys.json 已就绪；需要修复时等价于 uv run python scripts/gen_audit_keys.py。"
            ;;
        monitor-test-db-ready)
            echo "检查 TEST_DATABASE_URL 指向独立测试库、数据库可连接且 schema 已迁移完成。"
            ;;
        monitor-opensearch-ready)
            echo "检查 OpenSearch 可达且健康；需要修复时等价于 just infra up opensearch && just infra wait opensearch。"
            ;;
        *)
            return 1
            ;;
    esac
}

_fix_monitor_audit_keys() {
    run_uv_with_mutating_cache uv run python scripts/gen_audit_keys.py
}

run_project_check() {
    local check_id="$1"
    local oidc_enabled=""
    local test_database_url=""

    case "${check_id}" in
        monitor-oidc-keycloak)
            oidc_enabled="$(monitor_resolve_oidc_enabled)"
            if [[ "${oidc_enabled}" != "true" ]]; then
                emit_check_result "${check_id}" ready info project "OIDC 未启用，跳过 Keycloak 检查。" ""
                return 0
            fi
            check_infra_service_ready keycloak "${check_id}"
            ;;
        monitor-audit-keys)
            if [[ -f config/audit_keys.json ]]; then
                emit_check_result "${check_id}" ready info project "本地 mock 审计密钥已就绪。" ""
            else
                emit_check_result "${check_id}" missing error project "缺少 config/audit_keys.json。" "执行 uv run python scripts/gen_audit_keys.py，或 just dev bootstrap。"
            fi
            ;;
        monitor-test-db-ready)
            test_database_url="$(monitor_resolve_test_database_url)"
            if ! monitor_validate_test_database_url "${test_database_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" invalid error project "TEST_DATABASE_URL 必须指向独立测试库（数据库名须以 _test 结尾）。" "修正 TEST_DATABASE_URL 后重试。"
                return 0
            fi
            if ! monitor_check_test_database_connectivity "${test_database_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" missing error project "测试数据库不可连接。" "执行 just infra up postgres && just infra wait postgres，并检查 TEST_DATABASE_URL。"
                return 0
            fi
            if ! monitor_check_test_database_schema_ready "${test_database_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" stale error project "测试数据库 schema 未准备好。" "执行 just prep migrate test。"
                return 0
            fi
            emit_check_result "${check_id}" ready info project "测试数据库已就绪。" ""
            ;;
        monitor-opensearch-ready)
            if monitor_check_opensearch_connectivity >/dev/null 2>&1; then
                emit_check_result "${check_id}" ready info project "OpenSearch 已可用。" ""
            else
                emit_check_result "${check_id}" missing error shared-infra "OpenSearch 不可达或未健康。" "执行 just infra up opensearch && just infra wait opensearch。"
            fi
            ;;
        migrate-dev)
            load_monitor_db_helpers
            check_alembic_at_head "${check_id}"
            ;;
        migrate-test)
            load_monitor_db_helpers
            check_alembic_at_head "${check_id}" "$(monitor_build_test_env_prefix)"
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_ensure() {
    local check_id="$1"

    case "${check_id}" in
        monitor-oidc-keycloak)
            ensure_check_with_fix "${check_id}" _fix_monitor_oidc_keycloak
            ;;
        monitor-audit-keys)
            ensure_check_with_fix "${check_id}" _fix_monitor_audit_keys
            ;;
        migrate-dev)
            load_monitor_db_helpers
            ensure_alembic_at_head "${check_id}"
            ;;
        migrate-test)
            load_monitor_db_helpers
            ensure_alembic_at_head "${check_id}" "$(monitor_build_test_env_prefix)"
            ;;
        *)
            return 127
            ;;
    esac
}

monitor_export_local_oidc_test_env() {
    export MONITOR_OIDC_ENABLED="${MONITOR_OIDC_ENABLED:-true}"
    export MONITOR_OIDC_ISSUER="${MONITOR_OIDC_ISSUER:-http://localhost:9080/realms/acps-monitor}"
    export MONITOR_OIDC_AUDIENCE="${MONITOR_OIDC_AUDIENCE:-monitor-api}"
    export MONITOR_OIDC_ALLOWED_AZP="${MONITOR_OIDC_ALLOWED_AZP:-monitor-e2e,monitor-cli}"
    export MONITOR_OIDC_CLIENT_ID="${MONITOR_OIDC_CLIENT_ID:-monitor-api}"
    export MONITOR_OIDC_ALGORITHMS="${MONITOR_OIDC_ALGORITHMS:-EdDSA}"
    export MONITOR_OIDC_REQUIRE_HTTPS="${MONITOR_OIDC_REQUIRE_HTTPS:-false}"
    export MONITOR_OIDC_ROLE_SOURCE_CLIENT_ID="${MONITOR_OIDC_ROLE_SOURCE_CLIENT_ID:-monitor-api}"
    export TEST_OIDC_ISSUER="${TEST_OIDC_ISSUER:-${MONITOR_OIDC_ISSUER}}"
    export TEST_OIDC_E2E_CLIENT_ID="${TEST_OIDC_E2E_CLIENT_ID:-monitor-e2e}"
    export TEST_OIDC_VIEWER_USERNAME="${TEST_OIDC_VIEWER_USERNAME:-monitor-viewer}"
    export TEST_OIDC_VIEWER_PASSWORD="${TEST_OIDC_VIEWER_PASSWORD:-demo123}"
    export TEST_OIDC_OPERATOR_USERNAME="${TEST_OIDC_OPERATOR_USERNAME:-monitor-operator}"
    export TEST_OIDC_OPERATOR_PASSWORD="${TEST_OIDC_OPERATOR_PASSWORD:-demo123}"
    export TEST_OIDC_ADMIN_USERNAME="${TEST_OIDC_ADMIN_USERNAME:-monitor-admin}"
    export TEST_OIDC_ADMIN_PASSWORD="${TEST_OIDC_ADMIN_PASSWORD:-demo123}"
    export TEST_OIDC_FOREIGN_ISSUER="${TEST_OIDC_FOREIGN_ISSUER:-http://localhost:9080/realms/acps-registry}"
    export TEST_OIDC_FOREIGN_CLIENT_ID="${TEST_OIDC_FOREIGN_CLIENT_ID:-registry-e2e}"
    export TEST_OIDC_FOREIGN_USERNAME="${TEST_OIDC_FOREIGN_USERNAME:-registry-client}"
    export TEST_OIDC_FOREIGN_PASSWORD="${TEST_OIDC_FOREIGN_PASSWORD:-demo123}"
}

monitor_stop_test_server() {
    if [[ -n "${monitor_test_server_pid:-}" ]] && kill -0 "${monitor_test_server_pid}" 2>/dev/null; then
        kill "${monitor_test_server_pid}" 2>/dev/null || true
        wait "${monitor_test_server_pid}" 2>/dev/null || true
        echo "[INFO]  已停止临时测试实例。"
    fi

    if [[ -n "${monitor_test_server_log:-}" ]] && [[ -f "${monitor_test_server_log}" ]]; then
        echo "[INFO]  服务器日志：${monitor_test_server_log}"
        grep -i "error\\|exception\\|clickhouse\\|traceback\\|failed" "${monitor_test_server_log}" >&2 || true
        echo "--- last 30 lines ---" >&2
        tail -30 "${monitor_test_server_log}" >&2
        rm -f "${monitor_test_server_log}"
    fi
}

monitor_wait_for_test_server() {
    local attempt=""

    for attempt in {1..30}; do
        if curl -fsS "${monitor_test_server_base_url}/health" >/dev/null 2>&1; then
            return 0
        fi

        if ! kill -0 "${monitor_test_server_pid}" 2>/dev/null; then
            echo "[ERROR] 临时测试实例启动失败。" >&2
            cat "${monitor_test_server_log}" >&2
            return 1
        fi

        sleep 1
    done

    echo "[ERROR] 等待临时测试实例就绪超时：${monitor_test_server_base_url}" >&2
    cat "${monitor_test_server_log}" >&2
    return 1
}

monitor_start_test_server() {
    local test_database_url="$1"
    local test_server_port=""

    CLICKHOUSE_DATABASE="amp_test" run_uv_with_mutating_cache uv run python -c 'import asyncio; from tests.support.clickhouse_helper import ensure_test_schema; asyncio.run(ensure_test_schema())'

    test_server_port="$(find_free_port)"
    monitor_test_server_base_url="http://${test_server_host}:${test_server_port}"
    monitor_test_server_log="$(mktemp "${TMPDIR:-/tmp}/monitor-server-e2e.XXXXXX")"

    echo "[INFO]  启动临时 Query API 测试实例：${monitor_test_server_base_url}"

    APP_ENV=testing \
    DATABASE_URL="${test_database_url}" \
    TEST_DATABASE_URL="${test_database_url}" \
    CLICKHOUSE_DATABASE="amp_test" \
    run_uv_with_mutating_cache uv run uvicorn "${app_module}" \
        --host "${test_server_host}" \
        --port "${test_server_port}" \
        >"${monitor_test_server_log}" 2>&1 &

    monitor_test_server_pid="$!"
    monitor_wait_for_test_server
}

run_project_test_action() {
    local action="$1"
    shift || true

    load_monitor_pytest_helpers

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
            if [[ "$#" -gt 0 ]]; then
                run_uv_with_mutating_cache uv run pytest tests/unit/ "$@" --cov=app --cov-report=term-missing --cov-fail-under=70
            else
                run_uv_with_mutating_cache uv run pytest tests/unit/ --cov=app --cov-report=term-missing --cov-fail-under=70
            fi
            ;;
        coverage-report)
            just test bootstrap
            run_uv_with_mutating_cache uv run pytest --cov=app --cov-report=term-missing --cov-report=html tests/unit/ tests/integration/
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

    load_monitor_pytest_helpers

    case "${profile}" in
        local)
            ;;
        oidc)
            monitor_export_local_oidc_test_env
            just infra up keycloak
            just infra wait keycloak
            test_target="tests/e2e/test_oidc_keycloak_flow.py"
            ;;
        *)
            return 127
            ;;
    esac

    just test bootstrap
    test_database_url="$(monitor_resolve_test_database_url)"
    trap monitor_stop_test_server EXIT
    monitor_start_test_server "${test_database_url}"

    if [[ "$#" -gt 0 ]]; then
        TEST_E2E_BASE_URL="${monitor_test_server_base_url}" \
        CLICKHOUSE_DATABASE="amp_test" \
        run_requested_tests "${test_target}" "${monitor_test_server_base_url}" "$@"
    else
        TEST_E2E_BASE_URL="${monitor_test_server_base_url}" \
        CLICKHOUSE_DATABASE="amp_test" \
        run_requested_tests "${test_target}" "${monitor_test_server_base_url}"
    fi
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
