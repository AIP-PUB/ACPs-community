#!/usr/bin/env bash
# ACPs 共享开发基础设施管理脚本
#
# 用途：
#   统一管理 acps-infra/dev-infra/compose.yml 中定义的共享依赖容器。
#
# 公开 service 名：
#   postgres | redis | rabbitmq | gateway | keycloak | kafka
#
# 兼容旧写法：
#   dev-postgres | dev-redis | dev-rabbitmq | dev-nginx

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 可通过 DEV_INFRA_COMPOSE_FILE 指定隔离的 Compose 文件（例如测试时默认端口
# 已被其他本地部署占用的场景）；未设置时保持标准 dev-infra 配置不变。
COMPOSE_FILE="${DEV_INFRA_COMPOSE_FILE:-${SCRIPT_DIR}/compose.yml}"
COMPOSE_PROJECT_NAME="dev-infra"
NETWORK_NAME="acps-dev-net"
DEFAULT_LOG_TAIL=200
WAIT_TIMEOUT=90
RABBITMQ_WAIT_TIMEOUT=150
KEYCLOAK_WAIT_TIMEOUT=240
WAIT_INTERVAL=2

PUBLIC_SERVICES=("postgres" "redis" "rabbitmq" "gateway" "keycloak" "kafka" "victoria-metrics" "clickhouse" "minio" "opensearch")
COMPOSE_SERVICES=("dev-postgres" "dev-redis" "dev-rabbitmq" "dev-nginx" "dev-keycloak" "dev-redpanda" "dev-victoria-metrics" "dev-clickhouse" "dev-minio" "dev-opensearch")
CONTAINER_NAMES=("dev-postgres" "dev-redis" "dev-rabbitmq" "dev-nginx" "dev-keycloak" "dev-redpanda" "dev-victoria-metrics" "dev-clickhouse" "dev-minio" "dev-opensearch")
VOLUME_NAMES=("dev-pgdata" "dev-redisdata" "dev-mqdata" "" "dev-keycloakdata" "dev-redpandadata" "dev-vmdata" "dev-chdata" "dev-miniodata" "dev-opensearchdata")
PORTS=("5432:5432" "6379:6379" "5671:5671,15672:15672" "80:80" "9080:8080" "19092:19092,19644:9644" "8428:8428" "8123:8123,19010:9000" "19000:9000,19001:9001" "9200:9200")
SERVICE_MODES=("default" "optional" "optional" "optional" "optional" "optional" "optional" "optional" "optional" "optional")
HEALTHCHECK_FLAGS=("yes" "yes" "yes" "no" "yes" "yes" "yes" "yes" "yes" "yes")
DESCRIPTIONS=(
    "共享 PostgreSQL，供 registry/ca/discovery 开发使用"
    "共享 Redis，可按需启用"
    "共享 RabbitMQ，可按需启用"
    "开发网关 Nginx，可按需启用"
    "共享 Keycloak，供真人 OIDC 联调使用"
    "Redpanda（Kafka 兼容），供 monitor-server 开发使用"
    "VictoriaMetrics（时序数据库），供 monitor-server Metrics 开发使用"
    "ClickHouse（列存数据库），供 monitor-server Access 开发使用"
    "MinIO（对象存储），供 monitor-server Access 冷归档使用"
    "OpenSearch（全文搜索），供 monitor-server System 开发使用"
)

NORMALIZED_SERVICES=()
DOCTOR_ERRORS=0
DOCTOR_WARNINGS=0

log_info() {
    echo "[INFO]  $*"
}

log_warn() {
    echo "[WARN]  $*" >&2
}

log_error() {
    echo "[ERROR] $*" >&2
}

usage() {
    cat <<'EOF'
用法：
  ./dev-infra.sh up [service ...]
  ./dev-infra.sh down
  ./dev-infra.sh status [service ...] [--format=tsv]
  ./dev-infra.sh wait [service ...]
  ./dev-infra.sh logs [service ...] [--tail N] [--since DURATION] [--follow]
  ./dev-infra.sh reset [service ...] [--volumes] [--yes]
  ./dev-infra.sh check
  ./dev-infra.sh help

公开 service：
  postgres   共享 PostgreSQL（默认）
  redis      共享 Redis
  rabbitmq   共享 RabbitMQ
  gateway    开发网关 Nginx
  keycloak   共享 Keycloak，宿主机通过 localhost:9080 访问
  kafka      Redpanda（Kafka 兼容），宿主机通过 localhost:19092 连接（90xx 预留给应用）
  victoria-metrics VictoriaMetrics，宿主机通过 localhost:8428 访问
  clickhouse ClickHouse，HTTP: localhost:8123，原生 TCP: localhost:19010
  minio      MinIO，API: localhost:19000，Console: localhost:19001（避开应用 90xx）
  opensearch OpenSearch（全文搜索），宿主机通过 localhost:9200 连接

说明：
  - up 不传 service 时默认启动 postgres。
  - status 合并静态定义和动态状态；不传 service 时显示全部服务。
  - status 默认输出宽表格；--format=tsv 输出制表符分隔行，供脚本 awk 解析（列序：service/mode/state/health/ports）。
  - wait 不传 service 时等待当前已创建容器的全部服务。
  - logs 默认输出最后 200 行；加 --follow 持续跟随。
  - reset 用于修复性重建；全量 reset 或带 --volumes 时必须显式传 --yes。
  - 兼容旧 service 名：dev-postgres / dev-redis / dev-rabbitmq / dev-nginx。
EOF
}

join_by() {
    local separator="$1"
    shift

    local output=""
    local item=""
    for item in "$@"; do
        if [[ -n "${output}" ]]; then
            output="${output}${separator}${item}"
        else
            output="${item}"
        fi
    done

    printf '%s' "${output}"
}

ensure_compose_file() {
    if [[ ! -f "${COMPOSE_FILE}" ]]; then
        log_error "未找到 compose 文件：${COMPOSE_FILE}"
        exit 1
    fi
}

ensure_docker_cli() {
    if ! docker_cli_available; then
        log_error "未找到 docker，请先安装 Docker Desktop 或 Docker Engine。"
        exit 1
    fi
}

ensure_compose_v2() {
    if ! docker_compose_v2_available; then
        log_error "未检测到 Docker Compose V2，请确认 docker compose 可用。"
        exit 1
    fi
}

ensure_docker_daemon() {
    if ! docker_daemon_available; then
        log_error "Docker 未启动，或当前用户无权访问 Docker daemon。"
        exit 1
    fi
}

ensure_runtime_requirements() {
    ensure_compose_file
    ensure_docker_cli
    ensure_compose_v2
    ensure_docker_daemon
}

compose_cmd() {
    docker compose -f "${COMPOSE_FILE}" "$@"
}

compose_cmd_all_profiles() {
    COMPOSE_PROFILES=redis,rabbitmq,gateway,keycloak,victoria-metrics,clickhouse,minio,opensearch \
        docker compose -f "${COMPOSE_FILE}" "$@"
}

compose_up_detached() {
    COMPOSE_PROGRESS=plain docker compose --ansi never -f "${COMPOSE_FILE}" up -d "$@" </dev/null
}

ensure_rabbitmq_tls_assets() {
    local cert_dir="${SCRIPT_DIR}/certs/issued/rabbitmq"
    local required_paths=(
        "${cert_dir}/rabbitmq-server.pem"
        "${cert_dir}/rabbitmq-server.key"
        "${cert_dir}/rabbitmq-client.pem"
        "${cert_dir}/rabbitmq-client.key"
        "${cert_dir}/trust-bundle.pem"
    )
    local path=""

    for path in "${required_paths[@]}"; do
        if [[ ! -f "${path}" ]]; then
            log_info "RabbitMQ TLS 资产缺失，调用 ./dev-cert.sh issue-leaf 生成证书。"

            "${SCRIPT_DIR}/dev-cert.sh" issue-leaf \
                --ca infra \
                --common-name "rabbitmq" \
                --usage serverAuth \
                --san "DNS:localhost,DNS:rabbitmq,DNS:stage-rabbitmq,DNS:dev-rabbitmq,DNS:host.docker.internal,IP:127.0.0.1" \
                --cert-out "${cert_dir}/rabbitmq-server.pem" \
                --key-out "${cert_dir}/rabbitmq-server.key" \
                --bundle-out "${cert_dir}/trust-bundle.pem" \
                --relative-to "${SCRIPT_DIR}"

            "${SCRIPT_DIR}/dev-cert.sh" issue-leaf \
                --ca infra \
                --common-name "rabbitmq-auth-http-client" \
                --usage clientAuth \
                --cert-out "${cert_dir}/rabbitmq-client.pem" \
                --key-out "${cert_dir}/rabbitmq-client.key" \
                --relative-to "${SCRIPT_DIR}"

            return 0
        fi
    done
}

ensure_rabbitmq_dev_vhost() {
    local container_name="dev-rabbitmq"
    local deadline=$((SECONDS + 30))

    if ! wait_for_service_ready "rabbitmq"; then
        log_error "RabbitMQ 容器未能通过健康检查，无法初始化 acps vhost。"
        return 1
    fi

    while (( SECONDS < deadline )); do
        if docker exec "${container_name}" rabbitmqctl await_startup >/dev/null 2>&1; then
            log_info "确保 RabbitMQ 开发 vhost：acps"
            docker exec "${container_name}" rabbitmqctl add_vhost acps >/dev/null 2>&1 || true

            if docker exec "${container_name}" rabbitmqctl set_permissions -p acps admin '.*' '.*' '.*' >/dev/null 2>&1; then
                return 0
            fi
        fi
        sleep "${WAIT_INTERVAL}"
    done

    log_error "RabbitMQ 健康检查已通过，但未能及时完成 acps vhost 初始化。"
    return 1
}

ensure_redpanda_topics() {
    local container_name="dev-redpanda"
    local deadline=$((SECONDS + WAIT_TIMEOUT))

    while (( SECONDS < deadline )); do
        if docker exec "${container_name}" rpk cluster health >/dev/null 2>&1; then
            log_info "确保 Redpanda 开发主题：amp.audit, amp.audit.dlq, amp.heartbeat, amp.heartbeat.alive-delta, amp.heartbeat.dlq"
            # amp.audit：3 分区，保留 72 小时（259200000ms）
            docker exec "${container_name}" rpk topic create amp.audit \
                -p 3 -r 1 \
                -c retention.ms=259200000 >/dev/null 2>&1 || true
            # amp.audit.dlq：1 分区，保留 7 天（604800000ms）
            docker exec "${container_name}" rpk topic create amp.audit.dlq \
                -p 1 -r 1 \
                -c retention.ms=604800000 >/dev/null 2>&1 || true
            # amp.heartbeat：1 分区（input_partition_count=1），LogAppendTime，保留 7 天
            docker exec "${container_name}" rpk topic create amp.heartbeat \
                -p 1 -r 1 \
                -c retention.ms=604800000 \
                -c message.timestamp.type=LogAppendTime >/dev/null 2>&1 || true
            # amp.heartbeat.alive-delta：1 分区（heartbeat_shard_count=1），保留 7 天（delta_retention_hours=168）
            docker exec "${container_name}" rpk topic create amp.heartbeat.alive-delta \
                -p 1 -r 1 \
                -c retention.ms=604800000 >/dev/null 2>&1 || true
            # amp.heartbeat.dlq：1 分区，保留 7 天
            docker exec "${container_name}" rpk topic create amp.heartbeat.dlq \
                -p 1 -r 1 \
                -c retention.ms=604800000 >/dev/null 2>&1 || true
            # amp.metrics：1 分区，LogAppendTime，保留 7 天（O-2：时间戳由 broker 赋予）
            docker exec "${container_name}" rpk topic create amp.metrics \
                -p 1 -r 1 \
                -c retention.ms=604800000 \
                -c message.timestamp.type=LogAppendTime >/dev/null 2>&1 || true
            # amp.metrics.dlq：1 分区，保留 7 天
            docker exec "${container_name}" rpk topic create amp.metrics.dlq \
                -p 1 -r 1 \
                -c retention.ms=604800000 >/dev/null 2>&1 || true
            # amp.access：4 分区，LogAppendTime，保留 7 天（Access Writer 消费，与 Writer 并发对齐）
            docker exec "${container_name}" rpk topic create amp.access \
                -p 4 -r 1 \
                -c retention.ms=604800000 \
                -c message.timestamp.type=LogAppendTime >/dev/null 2>&1 || true
            # amp.access.dlq：1 分区，保留 7 天
            docker exec "${container_name}" rpk topic create amp.access.dlq \
                -p 1 -r 1 \
                -c retention.ms=604800000 >/dev/null 2>&1 || true
            # 已存在主题不会继承 create -c 参数，幂等修正 LogAppendTime（Heartbeat / Metrics / Access 必需）
            docker exec "${container_name}" rpk topic alter-config amp.heartbeat \
                --set message.timestamp.type=LogAppendTime >/dev/null 2>&1 || true
            docker exec "${container_name}" rpk topic alter-config amp.metrics \
                --set message.timestamp.type=LogAppendTime >/dev/null 2>&1 || true
            docker exec "${container_name}" rpk topic alter-config amp.access \
                --set message.timestamp.type=LogAppendTime >/dev/null 2>&1 || true
            # 旧环境可能仅有 1 分区，幂等扩至 4 分区（与 create -p 4 对齐）
            docker exec "${container_name}" rpk topic add-partitions amp.access -n 3 >/dev/null 2>&1 || true
            # amp.message：4 分区，LogAppendTime，保留 7 天（Message Writer 消费）
            docker exec "${container_name}" rpk topic create amp.message \
                -p 4 -r 1 \
                -c retention.ms=604800000 \
                -c message.timestamp.type=LogAppendTime >/dev/null 2>&1 || true
            # amp.message.dlq：1 分区，保留 7 天
            docker exec "${container_name}" rpk topic create amp.message.dlq \
                -p 1 -r 1 \
                -c retention.ms=604800000 >/dev/null 2>&1 || true
            docker exec "${container_name}" rpk topic alter-config amp.message \
                --set message.timestamp.type=LogAppendTime >/dev/null 2>&1 || true
            # 已存在单分区 topic 幂等扩到 4 分区（create 对已存在 topic 不生效）
            docker exec "${container_name}" rpk topic add-partitions amp.message -n 3 >/dev/null 2>&1 || true
            # amp.system：4 分区，LogAppendTime，保留 7 天（System Writer 消费，与 Writer 并发对齐）
            docker exec "${container_name}" rpk topic create amp.system \
                -p 4 -r 1 \
                -c retention.ms=604800000 \
                -c message.timestamp.type=LogAppendTime >/dev/null 2>&1 || true
            # amp.system.dlq：1 分区，保留 7 天
            docker exec "${container_name}" rpk topic create amp.system.dlq \
                -p 1 -r 1 \
                -c retention.ms=604800000 >/dev/null 2>&1 || true
            docker exec "${container_name}" rpk topic alter-config amp.system \
                --set message.timestamp.type=LogAppendTime >/dev/null 2>&1 || true
            log_info "Redpanda 主题确保完成（幂等，已存在则跳过）。"
            return 0
        fi
        sleep "${WAIT_INTERVAL}"
    done

    log_error "Redpanda 启动后未能及时响应，无法初始化 AMP 主题。"
    return 1
}

ensure_minio_buckets() {
    local container_name="dev-minio"
    local deadline=$((SECONDS + WAIT_TIMEOUT))

    while (( SECONDS < deadline )); do
        if docker exec "${container_name}" mc ready local >/dev/null 2>&1; then
            log_info "确保 MinIO 开发存储桶：amp-access-archive"
            # 配置本地别名
            docker exec "${container_name}" mc alias set devminio http://localhost:9000 admin devpass >/dev/null 2>&1 || true
            # 创建 Access 冷归档存储桶（幂等）
            docker exec "${container_name}" mc mb devminio/amp-access-archive >/dev/null 2>&1 || true
            log_info "MinIO 存储桶确保完成（幂等，已存在则跳过）。"
            return 0
        fi
        sleep "${WAIT_INTERVAL}"
    done

    log_error "MinIO 启动后未能及时响应，无法初始化存储桶。"
    return 1
}

ensure_keycloak_postgres_database() {
    local container_name="dev-postgres"

    if ! wait_for_service_ready "postgres"; then
        log_error "PostgreSQL 尚未就绪，无法准备 Keycloak 数据库。"
        return 1
    fi

    log_info "确保 Keycloak PostgreSQL 用户与数据库存在。"
    docker exec -i "${container_name}" psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<'SQL' >/dev/null
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'keycloak') THEN
        CREATE ROLE keycloak LOGIN PASSWORD 'keycloak';
    ELSE
        ALTER ROLE keycloak WITH LOGIN PASSWORD 'keycloak';
    END IF;
END
$$;

SELECT 'CREATE DATABASE keycloak OWNER keycloak'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'keycloak')\gexec

ALTER DATABASE keycloak OWNER TO keycloak;
SQL
}

ensure_keycloak_dev_bootstrap() {
    local bootstrap_script="${SCRIPT_DIR}/keycloak/bootstrap-dev-keycloak.sh"

    if [[ ! -x "${bootstrap_script}" ]]; then
        log_error "未找到 Keycloak 开发 bootstrap 脚本：${bootstrap_script}"
        return 1
    fi

    if ! wait_for_service_ready "keycloak"; then
        log_error "Keycloak 容器未能通过健康检查，无法执行开发 bootstrap。"
        return 1
    fi

    log_info "执行 Keycloak 开发 bootstrap（realm / EdDSA / registry-cli、monitor-cli、registry-e2e、monitor-e2e、leader-e2e clients）。"
    KEYCLOAK_CONTAINER="${KEYCLOAK_CONTAINER:-dev-keycloak}" \
        KEYCLOAK_ADMIN="${KEYCLOAK_ADMIN:-admin}" \
        KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-devpass}" \
        REGISTRY_WEB_BASE_URL="${REGISTRY_WEB_BASE_URL:-http://localhost:9001}" \
        MONITOR_WEB_BASE_URL="${MONITOR_WEB_BASE_URL:-http://localhost:9009}" \
        LEADER_WEB_BASE_URL="${LEADER_WEB_BASE_URL:-http://localhost:9030}" \
        bash "${bootstrap_script}"
}

docker_cli_available() {
    command -v docker >/dev/null 2>&1
}

docker_compose_v2_available() {
    docker_cli_available && docker compose version >/dev/null 2>&1
}

docker_daemon_available() {
    docker_cli_available && docker info >/dev/null 2>&1
}

ensure_network() {
    if ! docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
        log_info "创建共享网络（已存在则跳过）：${NETWORK_NAME}"
        docker network create "${NETWORK_NAME}" >/dev/null
    fi
}

doctor_error() {
    log_error "$*"
    DOCTOR_ERRORS=$((DOCTOR_ERRORS + 1))
}

doctor_warn() {
    log_warn "$*"
    DOCTOR_WARNINGS=$((DOCTOR_WARNINGS + 1))
}

service_index_by_public() {
    local target="$1"
    local index=0

    for ((index = 0; index < ${#PUBLIC_SERVICES[@]}; index++)); do
        if [[ "${PUBLIC_SERVICES[$index]}" == "${target}" ]]; then
            printf '%s\n' "${index}"
            return 0
        fi
    done

    return 1
}

service_index_by_legacy() {
    local target="$1"

    case "${target}" in
        dev-postgres)
            printf '0\n'
            return 0
            ;;
        dev-redis)
            printf '1\n'
            return 0
            ;;
        dev-rabbitmq)
            printf '2\n'
            return 0
            ;;
        dev-nginx)
            printf '3\n'
            return 0
            ;;
        dev-keycloak)
            printf '4\n'
            return 0
            ;;
    esac

    return 1
}

service_compose_name() {
    local index=""
    index="$(service_index_by_public "$1")" || return 1
    printf '%s\n' "${COMPOSE_SERVICES[$index]}"
}

service_container_name() {
    local index=""
    index="$(service_index_by_public "$1")" || return 1
    printf '%s\n' "${CONTAINER_NAMES[$index]}"
}

service_volume_name() {
    local index=""
    index="$(service_index_by_public "$1")" || return 1
    printf '%s\n' "${VOLUME_NAMES[$index]}"
}

service_volume_resource_name() {
    local volume_key=""
    volume_key="$(service_volume_name "$1")" || return 1

    if [[ -z "${volume_key}" ]]; then
        printf '\n'
        return 0
    fi

    printf '%s\n' "${COMPOSE_PROJECT_NAME}_${volume_key}"
}

service_port_mapping() {
    local index=""
    index="$(service_index_by_public "$1")" || return 1
    printf '%s\n' "${PORTS[$index]}"
}

service_mode() {
    local index=""
    index="$(service_index_by_public "$1")" || return 1
    printf '%s\n' "${SERVICE_MODES[$index]}"
}

service_has_healthcheck() {
    local index=""
    index="$(service_index_by_public "$1")" || return 1
    printf '%s\n' "${HEALTHCHECK_FLAGS[$index]}"
}

service_description() {
    local index=""
    index="$(service_index_by_public "$1")" || return 1
    printf '%s\n' "${DESCRIPTIONS[$index]}"
}

normalize_service_name() {
    local raw_name="$1"
    local index=""

    if service_index_by_public "${raw_name}" >/dev/null 2>&1; then
        printf '%s\n' "${raw_name}"
        return 0
    fi

    index="$(service_index_by_legacy "${raw_name}")" || {
        log_error "未知 service：${raw_name}"
        log_error "可用 service：$(join_by ', ' "${PUBLIC_SERVICES[@]}")"
        return 1
    }

    log_warn "service 名 ${raw_name} 已弃用，请改用 ${PUBLIC_SERVICES[$index]}。"
    printf '%s\n' "${PUBLIC_SERVICES[$index]}"
}

append_unique_service() {
    local service_name="$1"
    local existing=""

    for existing in "${NORMALIZED_SERVICES[@]:-}"; do
        if [[ "${existing}" == "${service_name}" ]]; then
            return 0
        fi
    done

    NORMALIZED_SERVICES+=("${service_name}")
}

normalize_service_args() {
    NORMALIZED_SERVICES=()

    if [[ "$#" -eq 0 ]]; then
        return 0
    fi

    local raw_name=""
    local normalized_name=""
    for raw_name in "$@"; do
        normalized_name="$(normalize_service_name "${raw_name}")" || return 1
        append_unique_service "${normalized_name}"
    done
}

container_exists() {
    docker inspect --type container "$1" >/dev/null 2>&1
}

container_running() {
    [[ "$(docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null || printf 'false')" == "true" ]]
}

container_state() {
    local container_name="$1"

    if ! container_exists "${container_name}"; then
        printf 'not-created\n'
        return 0
    fi

    docker inspect --format '{{.State.Status}}' "${container_name}" 2>/dev/null || printf 'unknown\n'
}

container_health() {
    local container_name="$1"

    if ! container_exists "${container_name}"; then
        printf 'not-created\n'
        return 0
    fi

    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${container_name}" 2>/dev/null || printf 'unknown\n'
}

service_wait_timeout() {
    local service_name="$1"

    case "${service_name}" in
        rabbitmq)
            printf '%s\n' "${RABBITMQ_WAIT_TIMEOUT}"
            ;;
        keycloak)
            printf '%s\n' "${KEYCLOAK_WAIT_TIMEOUT}"
            ;;
        *)
            printf '%s\n' "${WAIT_TIMEOUT}"
            ;;
    esac
}

wait_for_service_ready() {
    local service_name="$1"
    local container_name=""
    local state=""
    local health=""
    local elapsed=0
    local has_healthcheck=""
    local timeout_value=""

    container_name="$(service_container_name "${service_name}")"
    has_healthcheck="$(service_has_healthcheck "${service_name}")"
    timeout_value="$(service_wait_timeout "${service_name}")"

    if ! container_exists "${container_name}"; then
        log_error "service ${service_name} 尚未创建容器，请先运行 up ${service_name}。"
        return 1
    fi

    log_info "等待 ${service_name} 就绪（最长 ${timeout_value}s）..."
    while [[ "${elapsed}" -lt "${timeout_value}" ]]; do
        state="$(container_state "${container_name}")"
        health="$(container_health "${container_name}")"

        if [[ "${has_healthcheck}" == "yes" && "${health}" == "healthy" ]]; then
            log_info "${service_name} 已就绪（health=healthy）。"
            return 0
        fi

        if [[ "${has_healthcheck}" == "no" && "${state}" == "running" ]]; then
            log_info "${service_name} 已就绪（state=running）。"
            return 0
        fi

        if [[ "${state}" == "exited" || "${state}" == "dead" ]]; then
            log_error "${service_name} 提前退出，最后状态：state=${state}, health=${health}"
            return 1
        fi

        sleep "${WAIT_INTERVAL}"
        elapsed=$((elapsed + WAIT_INTERVAL))
    done

    log_error "等待 ${service_name} 就绪超时，最后状态：state=${state}, health=${health}"
    return 1
}

resolve_existing_services() {
    NORMALIZED_SERVICES=()

    local service_name=""
    local container_name=""
    for service_name in "${PUBLIC_SERVICES[@]}"; do
        container_name="$(service_container_name "${service_name}")"
        if container_exists "${container_name}"; then
            NORMALIZED_SERVICES+=("${service_name}")
        fi
    done
}

resolve_running_services() {
    NORMALIZED_SERVICES=()

    local service_name=""
    local container_name=""
    for service_name in "${PUBLIC_SERVICES[@]}"; do
        container_name="$(service_container_name "${service_name}")"
        if container_running "${container_name}"; then
            NORMALIZED_SERVICES+=("${service_name}")
        fi
    done
}

require_yes_for_reset() {
    local target_label="$1"
    local reset_with_volumes="$2"
    local confirmed="$3"

    if [[ "${confirmed}" == "yes" ]]; then
        return 0
    fi

    if [[ "${reset_with_volumes}" == "yes" ]]; then
        log_error "${target_label} 且删除 volume 时必须显式传入 --yes。"
        return 1
    fi

    if [[ "${target_label}" == "全量 reset" ]]; then
        log_error "全量 reset 必须显式传入 --yes。"
        return 1
    fi

    return 0
}

cmd_up() {
    ensure_runtime_requirements
    ensure_network

    local services=()
    local needs_keycloak_setup="no"
    if [[ "$#" -eq 0 ]]; then
        services=("postgres")
    else
        normalize_service_args "$@" || return 1
        services=("${NORMALIZED_SERVICES[@]}")
    fi

    local compose_services=()
    local service_name=""
    for service_name in "${services[@]}"; do
        if [[ "${service_name}" == "rabbitmq" ]]; then
            ensure_rabbitmq_tls_assets
        fi
        if [[ "${service_name}" == "keycloak" ]]; then
            needs_keycloak_setup="yes"
        fi
        compose_services+=("$(service_compose_name "${service_name}")")
    done

    if [[ "${needs_keycloak_setup}" == "yes" ]]; then
        compose_up_detached "$(service_compose_name postgres)"
        ensure_keycloak_postgres_database
    fi

    log_info "启动服务：$(join_by ', ' "${services[@]}")"
    compose_up_detached "${compose_services[@]}"

    for service_name in "${services[@]}"; do
        if [[ "${service_name}" == "rabbitmq" ]]; then
            ensure_rabbitmq_dev_vhost
        fi
        if [[ "${service_name}" == "keycloak" ]]; then
            ensure_keycloak_dev_bootstrap
        fi
        if [[ "${service_name}" == "kafka" ]]; then
            ensure_redpanda_topics
        fi
        if [[ "${service_name}" == "minio" ]]; then
            ensure_minio_buckets
        fi
    done

    log_info "启动命令已提交，可执行 ./dev-infra.sh wait $(join_by ' ' "${services[@]}") 等待就绪。"
}

cmd_down() {
    ensure_runtime_requirements

    log_warn "将停止整个 dev-infra，可能影响其他正在运行的本地项目。"
    compose_cmd_all_profiles down
    log_info "已停止 dev-infra（volume 保留）。"
}

cmd_status() {
    ensure_compose_file

    # 解析 --format 参数（tsv），其余参数视为 service 名
    # 默认格式：宽表格输出
    # --format=tsv：制表符分隔，列序固定：service\tmode\tstate\thealth\tports，供脚本解析
    local use_tsv=0
    local remaining_args=()
    for arg in "$@"; do
        case "${arg}" in
            --format=tsv) use_tsv=1 ;;
            --format=*)
                log_error "未知 --format 值：${arg}（可用：tsv）"
                return 1
                ;;
            *) remaining_args+=("${arg}") ;;
        esac
    done

    local services=()
    if [[ "${#remaining_args[@]}" -eq 0 ]]; then
        services=("${PUBLIC_SERVICES[@]}")
    else
        normalize_service_args "${remaining_args[@]}" || return 1
        services=("${NORMALIZED_SERVICES[@]}")
    fi

    local docker_runtime_available="no"
    local network_state="unavailable"
    if docker_daemon_available; then
        docker_runtime_available="yes"
        network_state="missing"
        if docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
            network_state="present"
        fi
    else
        log_warn "Docker daemon 当前不可访问，status 仅输出静态定义，动态状态标记为 unavailable。"
    fi

    local service_name=""
    local state=""
    local health=""
    local port_mapping=""
    local mode=""
    local container_name=""

    if [[ "${use_tsv}" -eq 1 ]]; then
        # TSV 格式：无 header，适合脚本 awk -F'\t' 解析
        # 列序：service  mode  state  health  ports
        for service_name in "${services[@]}"; do
            container_name="$(service_container_name "${service_name}")"
            port_mapping="$(service_port_mapping "${service_name}")"
            mode="$(service_mode "${service_name}")"
            if [[ "${docker_runtime_available}" == "yes" ]]; then
                state="$(container_state "${container_name}")"
                health="$(container_health "${container_name}")"
            else
                state="unavailable"
                health="unavailable"
            fi
            printf '%s\t%s\t%s\t%s\t%s\n' \
                "${service_name}" "${mode}" "${state}" "${health}" "${port_mapping}"
        done
        return 0
    fi

    # 默认：宽表格输出
    echo "dev-infra 状态"
    echo "compose: ${COMPOSE_FILE}"
    echo "project: ${COMPOSE_PROJECT_NAME}"
    echo "network: ${NETWORK_NAME} (${network_state})"
    echo ""

    printf '%-10s %-14s %-14s %-9s %-22s %-42s %-12s %-12s %s\n' \
        "SERVICE" "COMPOSE" "CONTAINER" "MODE" "PORTS" "VOLUME" "STATE" "HEALTH" "DESCRIPTION"

    local compose_service=""
    local description=""
    local volume_name=""
    local volume_resource_name=""
    for service_name in "${services[@]}"; do
        compose_service="$(service_compose_name "${service_name}")"
        container_name="$(service_container_name "${service_name}")"
        volume_name="$(service_volume_name "${service_name}")"
        volume_resource_name="$(service_volume_resource_name "${service_name}")"
        port_mapping="$(service_port_mapping "${service_name}")"
        mode="$(service_mode "${service_name}")"
        description="$(service_description "${service_name}")"
        if [[ "${docker_runtime_available}" == "yes" ]]; then
            state="$(container_state "${container_name}")"
            health="$(container_health "${container_name}")"
        else
            state="unavailable"
            health="unavailable"
        fi
        if [[ -z "${volume_name}" ]]; then
            volume_name="-"
        elif [[ -n "${volume_resource_name}" && "${volume_resource_name}" != "${volume_name}" ]]; then
            volume_name="${volume_name} -> ${volume_resource_name}"
        fi
        printf '%-10s %-14s %-14s %-9s %-22s %-42s %-12s %-12s %s\n' \
            "${service_name}" \
            "${compose_service}" \
            "${container_name}" \
            "${mode}" \
            "${port_mapping}" \
            "${volume_name}" \
            "${state}" \
            "${health}" \
            "${description}"
    done
}

cmd_wait() {
    ensure_runtime_requirements

    local services=()
    if [[ "$#" -eq 0 ]]; then
        resolve_existing_services
        if [[ "${#NORMALIZED_SERVICES[@]}" -eq 0 ]]; then
            log_error "当前没有已创建的服务容器，无法执行 wait。请先运行 up。"
            return 1
        fi
        services=("${NORMALIZED_SERVICES[@]}")
    else
        normalize_service_args "$@" || return 1
        services=("${NORMALIZED_SERVICES[@]}")
    fi

    local service_name=""
    for service_name in "${services[@]}"; do
        if ! wait_for_service_ready "${service_name}"; then
            return 1
        fi
    done
}

cmd_logs() {
    ensure_runtime_requirements

    local log_tail="${DEFAULT_LOG_TAIL}"
    local log_since=""
    local follow_logs="no"
    local raw_services=()

    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --tail)
                if [[ "$#" -lt 2 ]]; then
                    log_error "--tail 需要一个正整数参数。"
                    return 1
                fi
                log_tail="$2"
                shift 2
                ;;
            --since)
                if [[ "$#" -lt 2 ]]; then
                    log_error "--since 需要一个时长参数，例如 10m。"
                    return 1
                fi
                log_since="$2"
                shift 2
                ;;
            --follow|-f)
                follow_logs="yes"
                shift
                ;;
            --help|-h)
                usage
                return 0
                ;;
            --)
                shift
                while [[ "$#" -gt 0 ]]; do
                    raw_services+=("$1")
                    shift
                done
                ;;
            -*)
                log_error "未知 logs 参数：$1"
                return 1
                ;;
            *)
                raw_services+=("$1")
                shift
                ;;
        esac
    done

    if ! [[ "${log_tail}" =~ ^[0-9]+$ ]]; then
        log_error "--tail 需要一个非负整数，当前为：${log_tail}"
        return 1
    fi

    local services=()
    if [[ "${#raw_services[@]}" -eq 0 ]]; then
        resolve_running_services
        if [[ "${#NORMALIZED_SERVICES[@]}" -eq 0 ]]; then
            log_error "当前没有运行中的服务容器；默认 logs 仅查看运行中服务。"
            log_error "如需查看已停止容器的历史日志，请显式指定 service。"
            return 1
        fi
        services=("${NORMALIZED_SERVICES[@]}")
    else
        normalize_service_args "${raw_services[@]}" || return 1
        services=("${NORMALIZED_SERVICES[@]}")
    fi

    local compose_services=()
    local service_name=""
    local container_name=""
    for service_name in "${services[@]}"; do
        container_name="$(service_container_name "${service_name}")"
        if container_exists "${container_name}"; then
            compose_services+=("$(service_compose_name "${service_name}")")
        else
            log_warn "service ${service_name} 尚未创建容器，跳过日志输出。"
        fi
    done

    if [[ "${#compose_services[@]}" -eq 0 ]]; then
        log_error "没有可输出日志的服务容器。"
        return 1
    fi

    local args=("logs" "--tail" "${log_tail}")
    if [[ -n "${log_since}" ]]; then
        args+=("--since" "${log_since}")
    fi
    if [[ "${follow_logs}" == "yes" ]]; then
        args+=("--follow")
    fi
    args+=("${compose_services[@]}")

    compose_cmd "${args[@]}"
}

cmd_reset() {
    ensure_runtime_requirements

    local reset_with_volumes="no"
    local reset_confirmed="no"
    local raw_services=()

    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --volumes)
                reset_with_volumes="yes"
                shift
                ;;
            --yes)
                reset_confirmed="yes"
                shift
                ;;
            --help|-h)
                usage
                return 0
                ;;
            --)
                shift
                while [[ "$#" -gt 0 ]]; do
                    raw_services+=("$1")
                    shift
                done
                ;;
            -*)
                log_error "未知 reset 参数：$1"
                return 1
                ;;
            *)
                raw_services+=("$1")
                shift
                ;;
        esac
    done

    if [[ "${#raw_services[@]}" -eq 0 ]]; then
        require_yes_for_reset "全量 reset" "${reset_with_volumes}" "${reset_confirmed}" || return 1

        log_warn "将对整个 dev-infra 执行 reset。"
        if [[ "${reset_with_volumes}" == "yes" ]]; then
            log_warn "将删除全部关联 volume，下次 up 时会重建数据库与消息数据。"
            compose_cmd_all_profiles down --volumes --remove-orphans
        else
            compose_cmd_all_profiles down --remove-orphans
        fi
        log_info "全量 reset 完成。"
        return 0
    fi

    normalize_service_args "${raw_services[@]}" || return 1
    local services=("${NORMALIZED_SERVICES[@]}")

    if [[ "${reset_with_volumes}" == "yes" ]]; then
        require_yes_for_reset "指定 service reset" "${reset_with_volumes}" "${reset_confirmed}" || return 1
    fi

    local service_name=""
    local container_name=""
    local volume_name=""
    local volume_resource_name=""
    local volume_removed="no"
    for service_name in "${services[@]}"; do
        container_name="$(service_container_name "${service_name}")"
        volume_name="$(service_volume_name "${service_name}")"
        volume_resource_name="$(service_volume_resource_name "${service_name}")"

        if container_exists "${container_name}"; then
            log_info "移除服务容器：${service_name} (${container_name})"
            docker rm -f "${container_name}" >/dev/null
        else
            log_warn "service ${service_name} 当前没有已创建的容器，跳过容器删除。"
        fi

        if [[ "${reset_with_volumes}" == "yes" ]]; then
            if [[ -n "${volume_name}" ]]; then
                volume_removed="no"

                if [[ -n "${volume_resource_name}" ]] && docker volume inspect "${volume_resource_name}" >/dev/null 2>&1; then
                    log_info "删除 volume：${volume_resource_name}（compose volume key: ${volume_name}）"
                    docker volume rm -f "${volume_resource_name}" >/dev/null
                    volume_removed="yes"
                fi

                if [[ "${volume_removed}" == "no" ]] && docker volume inspect "${volume_name}" >/dev/null 2>&1; then
                    log_info "删除 volume：${volume_name}"
                    docker volume rm -f "${volume_name}" >/dev/null
                    volume_removed="yes"
                fi

                if [[ "${volume_removed}" == "no" ]]; then
                    log_warn "volume ${volume_name} 不存在，跳过删除。"
                fi
            else
                log_warn "service ${service_name} 没有可删除的 volume，跳过 volume 删除。"
            fi
        fi
    done

    log_info "指定 service reset 完成。"
}

cmd_doctor() {
    ensure_compose_file

    DOCTOR_ERRORS=0
    DOCTOR_WARNINGS=0

    if ! docker_cli_available; then
        doctor_error "未找到 docker。"
    else
        log_info "检测到 docker 可执行文件。"
    fi

    if docker_cli_available; then
        if docker_compose_v2_available; then
            log_info "检测到 Docker Compose V2。"
        else
            doctor_error "未检测到 Docker Compose V2。"
        fi

        if docker_daemon_available; then
            log_info "Docker daemon 可访问。"
        else
            doctor_error "Docker daemon 不可访问。"
        fi
    fi

    if docker_compose_v2_available; then
        if docker compose -f "${COMPOSE_FILE}" config >/dev/null 2>&1; then
            log_info "compose.yml 语法检查通过。"
        else
            doctor_error "compose.yml 解析失败，请检查 YAML 或 Compose 配置。"
        fi
    fi

    if grep -Eq "^name:[[:space:]]+${COMPOSE_PROJECT_NAME}$" "${COMPOSE_FILE}"; then
        log_info "project name 映射正常：${COMPOSE_PROJECT_NAME}"
    else
        doctor_error "compose.yml 顶层 project name 与脚本常量不一致：期望 ${COMPOSE_PROJECT_NAME}"
    fi

    if docker_daemon_available; then
        if docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
            log_info "共享网络 ${NETWORK_NAME} 已存在。"
        else
            doctor_warn "共享网络 ${NETWORK_NAME} 尚未创建；首次执行 up 时会自动创建。"
        fi
    fi

    local index=0
    local compose_service=""
    local volume_name=""
    for ((index = 0; index < ${#PUBLIC_SERVICES[@]}; index++)); do
        compose_service="${COMPOSE_SERVICES[$index]}"
        if grep -Eq "^  ${compose_service}:$" "${COMPOSE_FILE}"; then
            log_info "service 映射正常：${PUBLIC_SERVICES[$index]} -> ${compose_service}"
        else
            doctor_error "service 映射缺失：compose.yml 中未找到 ${compose_service}"
        fi

        volume_name="${VOLUME_NAMES[$index]}"
        if [[ -n "${volume_name}" ]]; then
            if grep -Eq "^  ${volume_name}:$" "${COMPOSE_FILE}"; then
                log_info "volume 映射正常：${PUBLIC_SERVICES[$index]} -> ${volume_name} (${COMPOSE_PROJECT_NAME}_${volume_name})"
            else
                doctor_error "volume 映射缺失：compose.yml 中未找到 ${volume_name}"
            fi
        fi
    done

    if [[ "${DOCTOR_ERRORS}" -gt 0 ]]; then
        log_error "check 发现 ${DOCTOR_ERRORS} 个错误，${DOCTOR_WARNINGS} 个警告。"
        return 1
    fi

    if [[ "${DOCTOR_WARNINGS}" -gt 0 ]]; then
        log_warn "check 通过，但存在 ${DOCTOR_WARNINGS} 个警告。"
        return 0
    fi

    log_info "check 通过。"
}

SUBCOMMAND="${1:-help}"

case "${SUBCOMMAND}" in
    up)
        shift
        cmd_up "$@"
        ;;
    down)
        shift
        cmd_down "$@"
        ;;
    status)
        shift
        cmd_status "$@"
        ;;
    wait)
        shift
        cmd_wait "$@"
        ;;
    logs)
        shift
        cmd_logs "$@"
        ;;
    reset)
        shift
        cmd_reset "$@"
        ;;
    check)
        shift
        cmd_doctor "$@"
        ;;
    help|--help|-h)
        usage
        ;;
    *)
        log_error "未知子命令：${SUBCOMMAND}"
        echo ""
        usage
        exit 1
        ;;
esac
