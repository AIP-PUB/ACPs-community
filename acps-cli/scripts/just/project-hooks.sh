#!/usr/bin/env bash

: "${config_file:=acps-cli.toml}"

configure_project_package_runtime() {
    PACKAGE_RUNTIME_REQUIRED_PATHS=(
        acps-cli.toml
        .env.example
        README.md
        pyproject.toml
        scripts/bootstrap.sh
        scripts/bootstrap_runtime.py
        scripts/acs/registry-server-9002-service-acs.json
        scripts/acs/registry-server-9002-probe-acs.json
        scripts/acs/mq-auth-server-acs.json
        scripts/acs/healthcheck-client-acs.json
        scripts/acs/rabbitmq-acs.json
        scripts/acs/redis-acs.json
        scripts/smoke-test-business.sh
        scripts/smoke_test_runtime.py
    )

    PACKAGE_RUNTIME_SIBLING_REPOS=(
        acps-sdk
    )

    PACKAGE_RUNTIME_BUNDLE_MAP=(
        "acps-cli.toml|acps-cli.toml|config"
        ".env.example|.env.example|env_template"
        "README.md|README.md|doc"
        "scripts/bootstrap.sh|scripts/bootstrap.sh|bootstrap_script"
        "scripts/bootstrap_runtime.py|scripts/bootstrap_runtime.py|bootstrap_script"
        "scripts/acs/registry-server-9002-service-acs.json|scripts/acs/registry-server-9002-service-acs.json|acs_descriptor"
        "scripts/acs/registry-server-9002-probe-acs.json|scripts/acs/registry-server-9002-probe-acs.json|acs_descriptor"
        "scripts/acs/mq-auth-server-acs.json|scripts/acs/mq-auth-server-acs.json|acs_descriptor"
        "scripts/acs/healthcheck-client-acs.json|scripts/acs/healthcheck-client-acs.json|acs_descriptor"
        "scripts/acs/rabbitmq-acs.json|scripts/acs/rabbitmq-acs.json|acs_descriptor"
        "scripts/acs/redis-acs.json|scripts/acs/redis-acs.json|acs_descriptor"
        "scripts/smoke-test-business.sh|scripts/smoke-test-business.sh|smoke_test"
        "scripts/smoke_test_runtime.py|scripts/smoke_test_runtime.py|other"
    )

    PACKAGE_RUNTIME_BUNDLE_EXCLUDE_MAP=(
        "${DEFAULT_BUNDLE_EXCLUDE_MAP[@]}"
        "._*"
        "*/._*"
    )

    PACKAGE_RUNTIME_CHMOD_PATHS=(
        "scripts/bootstrap.sh"
        "scripts/smoke-test-business.sh"
    )
    PACKAGE_RUNTIME_REMOVE_PATTERNS=()

    # acps-cli 是独立分发的 CLI 工具（console script），不是长期运行服务；
    # component type 用 cli-tool 区分，不声明 ports/health_check。
    PACKAGE_RUNTIME_COMPONENTS=(
        "acps-cli|cli-tool|acps-cli|||scripts/smoke-test-business.sh|acps-cli.toml"
    )
    PACKAGE_RUNTIME_INTERNAL_WHEELS=(acps-sdk)
    PACKAGE_RUNTIME_EXTERNAL_COMPONENTS=()
}

load_cli_pytest_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/pytest-lib.sh"
}

cli_run_venv_python() {
    run_uv_with_mutating_cache uv run --no-sync python "$@"
}

cli_get_config_value() {
    local section="$1"
    local key="$2"

    cli_run_venv_python -c 'exec("from pathlib import Path\nimport re\nimport sys\nconfig_path, section_name, key_name = sys.argv[1:4]\ntext = Path(config_path).read_text(encoding=\"utf-8\")\nsection_pattern = rf\"(?ms)^\\[{re.escape(section_name)}\\]\\s*(.*?)(?=^\\[|\\Z)\"\nsection_match = re.search(section_pattern, text)\nif section_match is None:\n    raise SystemExit(1)\nbody = section_match.group(1)\nkey_pattern = rf\"(?m)^\\s*{re.escape(key_name)}\\s*=\\s*\\\"([^\\\"]+)\\\"\"\nkey_match = re.search(key_pattern, body)\nif key_match is None:\n    raise SystemExit(1)\nprint(key_match.group(1))")' "${config_file}" "${section}" "${key}"
}

cli_resolve_service_base_url() {
    local env_name="$1"
    local section="$2"
    local key="$3"
    local fallback="$4"
    local env_value="${!env_name:-}"
    local config_value=""

    if [[ -n "${env_value}" ]]; then
        printf '%s\n' "${env_value}"
        return
    fi

    if config_value="$(cli_get_config_value "${section}" "${key}" 2>/dev/null)"; then
        printf '%s\n' "${config_value}"
        return
    fi

    printf '%s\n' "${fallback}"
}

cli_derive_health_url() {
    local base_url="$1"

    cli_run_venv_python -c 'from urllib.parse import urlsplit; import sys; parts = urlsplit(sys.argv[1]); sys.stdout.write(f"{parts.scheme}://{parts.netloc}/health\n") if (parts.scheme and parts.netloc) else sys.exit(1)' "${base_url}"
}

cli_health_url_reachable() {
    local health_url="$1"

    cli_run_venv_python -c 'exec("import sys\nimport urllib.error\nimport urllib.request\nurl = sys.argv[1]\ntry:\n    with urllib.request.urlopen(url, timeout=5) as response:\n        raise SystemExit(0 if 200 <= response.status < 400 else 1)\nexcept urllib.error.HTTPError as exc:\n    raise SystemExit(0 if 200 <= exc.code < 400 else 1)\nexcept Exception:\n    raise SystemExit(1)")' "${health_url}"
}

cli_print_service_start_hint() {
    local service_name="$1"

    case "${service_name}" in
        registry-server)
            echo "[HINT] 启动命令：cd ../registry-server && APP_ENV=development CA_SERVER_MOCK=false just dev bootstrap && APP_ENV=development CA_SERVER_MOCK=false just dev start" >&2
            ;;
        ca-server)
            echo "[HINT] 启动命令：cd ../ca-server && APP_ENV=development REGISTRY_SERVER_MOCK=false just dev bootstrap && APP_ENV=development REGISTRY_SERVER_MOCK=false just dev start" >&2
            ;;
        discovery-server)
            echo "[HINT] 启动命令：cd ../discovery-server && just dev bootstrap && just dev start" >&2
            ;;
        mq-auth-server)
            echo "[HINT] 启动命令：cd ../mq-auth-server && just dev bootstrap && just dev start" >&2
            ;;
        monitor-server)
            echo "[HINT] 启动命令：cd ../monitor-server && APP_ENV=development DATABASE_URL=postgresql+asyncpg://monitor:monitor@localhost:5432/agent_monitor_test TEST_DATABASE_URL=postgresql+asyncpg://monitor:monitor@localhost:5432/agent_monitor_test REDIS_URL=redis://localhost:6379/3 CLICKHOUSE_DATABASE=amp_test OPENSEARCH_HOSTS=http://localhost:9200 OPENSEARCH_VERIFY_CERTS=false just dev bootstrap && APP_ENV=development DATABASE_URL=postgresql+asyncpg://monitor:monitor@localhost:5432/agent_monitor_test TEST_DATABASE_URL=postgresql+asyncpg://monitor:monitor@localhost:5432/agent_monitor_test REDIS_URL=redis://localhost:6379/3 CLICKHOUSE_DATABASE=amp_test OPENSEARCH_HOSTS=http://localhost:9200 OPENSEARCH_VERIFY_CERTS=false just dev start" >&2
            ;;
    esac

    echo "[HINT] 若当前不是本地联调环境，请检查 acps-cli.toml 或 REGISTRY_URL / CA_URL / DISCO_URL / MQ_GROUP_API_URL / MQ_AUTH_API_URL / MONITOR_BASE_URL 配置。" >&2
}

cli_resolve_mq_file() {
    local env_name="$1"
    local key="$2"
    local fallback_rel="$3"
    local env_value="${!env_name:-}"
    local config_value=""

    if [[ -n "${env_value}" ]]; then
        printf '%s\n' "${env_value}"
        return
    fi

    if config_value="$(cli_get_config_value mq "${key}" 2>/dev/null)"; then
        printf '%s\n' "${config_value}"
        return
    fi

    if [[ -f "./bootstrap-artifacts/mq-auth-server/${fallback_rel}" ]]; then
        printf '%s\n' "./bootstrap-artifacts/mq-auth-server/${fallback_rel}"
        return
    fi

    if [[ -f "../mq-auth-server/certs/${fallback_rel}" ]]; then
        printf '%s\n' "../mq-auth-server/certs/${fallback_rel}"
        return
    fi
}

cli_check_mq_health() {
    local group_url="$1"
    local auth_url="$2"
    local probe_cert=""
    local probe_key=""
    local ca_file=""
    local output=""

    probe_cert="$(cli_resolve_mq_file MQ_PROBE_CERT_FILE probe_cert_file client.pem || true)"
    probe_key="$(cli_resolve_mq_file MQ_PROBE_KEY_FILE probe_key_file client.key || true)"
    ca_file="$(cli_resolve_mq_file MQ_CA_FILE ca_cert_file acps-root-ca.pem || true)"

    if [[ -z "${probe_cert}" || -z "${probe_key}" || -z "${ca_file}" ]]; then
        echo "[ERROR] mq-auth-server 缺少可用的 probe 证书或 CA 文件，无法执行统一可达性检查。" >&2
        echo "[HINT] 本地联调可复用 ../mq-auth-server/certs/client.pem、client.key、acps-root-ca.pem；部署场景请先执行 bash scripts/bootstrap.sh mq-auth-server。" >&2
        cli_print_service_start_hint "mq-auth-server"
        return 1
    fi

    if ! output="$(MQ_GROUP_API_URL="${group_url}" MQ_AUTH_API_URL="${auth_url}" MQ_PROBE_CERT_FILE="${probe_cert}" MQ_PROBE_KEY_FILE="${probe_key}" MQ_CA_FILE="${ca_file}" run_uv_with_mutating_cache uv run acps-cli --config "${config_file}" admin mq health --json 2>&1)"; then
        echo "[ERROR] mq-auth-server 健康检查失败：${output}" >&2
        cli_print_service_start_hint "mq-auth-server"
        return 1
    fi

    cli_run_venv_python -c 'import json, sys; payload = json.loads(sys.argv[1]); ok = payload.get("group_api", {}).get("status") == "ok" and payload.get("auth_api", {}).get("status") == "ok"; raise SystemExit(0 if ok else 1)' "${output}"
}

cli_ensure_public_docker_config() {
    if [[ -n "${DOCKER_CONFIG:-}" ]]; then
        return 0
    fi

    mkdir -p .tmp/docker-public-config
    printf '{}\n' > .tmp/docker-public-config/config.json
    if [[ -d "${HOME}/.docker/cli-plugins" ]]; then
        ln -snf "${HOME}/.docker/cli-plugins" .tmp/docker-public-config/cli-plugins
    fi
    export DOCKER_CONFIG="$(pwd)/.tmp/docker-public-config"
    echo "[INFO]  DOCKER_CONFIG 未设置，使用 ${DOCKER_CONFIG} 拉取 public dev-infra 镜像。"
}

cli_check_managed_service_runtime() {
    local repo_path="$1"
    local service_name="$2"
    local runtime_path="$3"
    shift 3
    local required_path=""

    [[ -d "${repo_path}" ]] || return 1
    [[ -x "${repo_path}/${runtime_path}" ]] || return 1

    for required_path in "$@"; do
        [[ -f "${repo_path}/${required_path}" ]] || return 1
    done

    return 0
}

cli_ensure_managed_service_runtime() {
    local repo_path="$1"
    local service_name="$2"
    local runtime_path="$3"
    shift 3
    local bootstrap_required=0
    local required_path=""

    if [[ ! -d "${repo_path}" ]]; then
        echo "[ERROR] 缺少兄弟项目 ${repo_path}；acps-cli 受管测试服务需要它。" >&2
        return 1
    fi

    if [[ ! -x "${repo_path}/${runtime_path}" ]]; then
        bootstrap_required=1
    fi

    if [[ "${bootstrap_required}" -eq 0 ]]; then
        for required_path in "$@"; do
            if [[ ! -f "${repo_path}/${required_path}" ]]; then
                bootstrap_required=1
                break
            fi
        done
    fi

    if [[ "${bootstrap_required}" -eq 1 ]]; then
        echo "[INFO]  准备受管测试依赖：${service_name}"
        (
            cd "${repo_path}"
            just test bootstrap
        )
    fi

    cli_check_managed_service_runtime "${repo_path}" "${service_name}" "${runtime_path}" "$@"
}

cli_check_all_managed_service_runtimes() {
    cli_check_managed_service_runtime ../registry-server registry-server .venv/bin/python \
        certs/server.pem \
        certs/server.key \
        certs/trust-bundle.pem \
        certs/client.pem \
        certs/client.key \
    && cli_check_managed_service_runtime ../ca-server ca-server .venv/bin/python \
        certs/ca.crt \
        certs/ca.key \
        certs/ca-chain.pem \
        certs/trust-bundle.pem \
    && cli_check_managed_service_runtime ../discovery-server discovery-server .venv/bin/python \
    && cli_check_managed_service_runtime ../mq-auth-server mq-auth-server .venv/bin/mq-auth-server \
        certs/server.pem \
        certs/server.key \
        certs/client.pem \
        certs/client.key \
        certs/acps-root-ca.pem \
    && cli_check_managed_service_runtime ../monitor-server monitor-server .venv/bin/python
}

cli_ensure_all_managed_service_runtimes() {
    cli_ensure_managed_service_runtime ../registry-server registry-server .venv/bin/python \
        certs/server.pem \
        certs/server.key \
        certs/trust-bundle.pem \
        certs/client.pem \
        certs/client.key \
    && cli_ensure_managed_service_runtime ../ca-server ca-server .venv/bin/python \
        certs/ca.crt \
        certs/ca.key \
        certs/ca-chain.pem \
        certs/trust-bundle.pem \
    && cli_ensure_managed_service_runtime ../discovery-server discovery-server .venv/bin/python \
    && cli_ensure_managed_service_runtime ../mq-auth-server mq-auth-server .venv/bin/mq-auth-server \
        certs/server.pem \
        certs/server.key \
        certs/client.pem \
        certs/client.key \
        certs/acps-root-ca.pem \
    && cli_ensure_managed_service_runtime ../monitor-server monitor-server .venv/bin/python
}

describe_project_check_step() {
    local check_id="$1"

    case "${check_id}" in
        acps-cli-public-docker-config)
            echo "确保 public Docker 配置已就绪，便于拉取 shared dev-infra 公共镜像；需要修复时由 just dev/test bootstrap 自动补齐。"
            ;;
        acps-cli-upstream-services)
            echo "检查 registry / ca / discovery / mq-auth / monitor 五个本地上游服务的健康探针均可达。"
            ;;
        acps-cli-managed-test-runtimes)
            echo "检查受管测试依赖运行时（registry / ca / discovery / mq-auth / monitor）的 Python 运行时、证书与必要文件已就绪；需要修复时等价于 just test bootstrap。"
            ;;
        *)
            return 1
            ;;
    esac
}

run_project_check() {
    local check_id="$1"
    local registry_base_url=""
    local ca_base_url=""
    local disco_base_url=""
    local mq_group_url=""
    local mq_auth_url=""
    local monitor_base_url=""
    local registry_health_url=""
    local ca_health_url=""
    local disco_health_url=""
    local monitor_health_url=""

    case "${check_id}" in
        acps-cli-public-docker-config)
            if [[ -n "${DOCKER_CONFIG:-}" ]] || [[ -f .tmp/docker-public-config/config.json ]]; then
                emit_check_result "${check_id}" ready info project "public Docker 配置已就绪。" ""
            else
                emit_check_result "${check_id}" missing error project "public Docker 配置缺失。" "执行 just dev bootstrap 或 just test bootstrap。"
            fi
            ;;
        acps-cli-upstream-services)
            registry_base_url="$(cli_resolve_service_base_url REGISTRY_URL registry base_url http://localhost:9001)"
            ca_base_url="$(cli_resolve_service_base_url CA_URL ca base_url http://localhost:9003)"
            disco_base_url="$(cli_resolve_service_base_url DISCO_URL discovery base_url http://localhost:9005)"
            mq_group_url="$(cli_resolve_service_base_url MQ_GROUP_API_URL mq group_api_url https://localhost:9007)"
            mq_auth_url="$(cli_resolve_service_base_url MQ_AUTH_API_URL mq auth_api_url https://localhost:9008)"
            monitor_base_url="$(cli_resolve_service_base_url MONITOR_BASE_URL monitor base_url http://localhost:9009)"

            if ! registry_health_url="$(cli_derive_health_url "${registry_base_url}" 2>/dev/null)" || ! cli_health_url_reachable "${registry_health_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" missing error sibling "registry-server 不可达。" "先启动 registry-server 本地运行时。"
                cli_print_service_start_hint "registry-server"
                return 0
            fi
            if ! ca_health_url="$(cli_derive_health_url "${ca_base_url}" 2>/dev/null)" || ! cli_health_url_reachable "${ca_health_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" missing error sibling "ca-server 不可达。" "先启动 ca-server 本地运行时。"
                cli_print_service_start_hint "ca-server"
                return 0
            fi
            if ! disco_health_url="$(cli_derive_health_url "${disco_base_url}" 2>/dev/null)" || ! cli_health_url_reachable "${disco_health_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" missing error sibling "discovery-server 不可达。" "先启动 discovery-server 本地运行时。"
                cli_print_service_start_hint "discovery-server"
                return 0
            fi
            if ! cli_check_mq_health "${mq_group_url}" "${mq_auth_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" missing error sibling "mq-auth-server 不可达。" "先启动 mq-auth-server 本地运行时，并准备 probe 证书。"
                return 0
            fi
            if ! monitor_health_url="$(cli_derive_health_url "${monitor_base_url}" 2>/dev/null)" || ! cli_health_url_reachable "${monitor_health_url}" >/dev/null 2>&1; then
                emit_check_result "${check_id}" missing error sibling "monitor-server 不可达。" "先启动 monitor-server 本地运行时。"
                cli_print_service_start_hint "monitor-server"
                return 0
            fi

            emit_check_result "${check_id}" ready info sibling "CLI 依赖的本地上游服务均可达。" ""
            ;;
        acps-cli-managed-test-runtimes)
            if cli_check_all_managed_service_runtimes; then
                emit_check_result "${check_id}" ready info sibling "受管测试依赖运行时已就绪。" ""
            else
                emit_check_result "${check_id}" missing error sibling "受管测试依赖运行时未就绪。" "执行 just test bootstrap；必要时检查兄弟项目工作区。"
            fi
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_ensure() {
    local check_id="$1"

    case "${check_id}" in
        acps-cli-public-docker-config)
            ensure_check_with_fix "${check_id}" cli_ensure_public_docker_config
            ;;
        acps-cli-managed-test-runtimes)
            ensure_check_with_fix "${check_id}" cli_ensure_all_managed_service_runtimes
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_package_filter_requirements() {
    local input_file="$1"
    local output_file="$2"

    grep -Ev '^-e \.\./acps-sdk$' "${input_file}" > "${output_file}"
}

run_project_test_action() {
    local action="$1"
    shift || true
    local requested_args=("$@")

    load_cli_pytest_helpers

    case "${action}" in
        unit)
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                run_requested_tests tests/unit/ "${TEST_E2E_BASE_URL:-}" "${requested_args[@]}"
            else
                run_requested_tests tests/unit/ "${TEST_E2E_BASE_URL:-}"
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
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                run_uv_with_mutating_cache uv run pytest tests/unit/ "${requested_args[@]}" --cov=acps_cli --cov-report=term-missing --cov-fail-under=70
            else
                run_uv_with_mutating_cache uv run pytest tests/unit/ --cov=acps_cli --cov-report=term-missing --cov-fail-under=70
            fi
            ;;
        coverage-report)
            run_uv_with_mutating_cache uv run pytest --cov=acps_cli --cov-report=term-missing --cov-report=html tests/unit/ tests/integration/
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

    load_cli_pytest_helpers

    case "${profile}" in
        local)
            just test bootstrap
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                run_requested_tests tests/e2e/ "${TEST_E2E_BASE_URL:-}" "${requested_args[@]}"
            else
                run_requested_tests tests/e2e/ "${TEST_E2E_BASE_URL:-}"
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

run_project_help_section() {
    local section="$1"

    case "${section}" in
        test)
            echo "  coverage-report  生成包含 HTML 输出的覆盖率报告"
            ;;
        prep)
            return 0
            ;;
    esac
}
