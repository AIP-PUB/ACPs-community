"""tests/support/constants.py — 测试常量定义。"""

import os

# 测试数据库默认连接串（当 TEST_DATABASE_URL 环境变量未设置时使用）
DEFAULT_TEST_DATABASE_DSN = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://monitor:monitor@localhost:5432/agent_monitor_test",
)

# 测试数据库名称（conftest 校验用）
TEST_DATABASE_NAME = "agent_monitor_test"

# 测试数据库同步连接串（pytest 中 alembic migrate 用）
DEFAULT_TEST_DATABASE_SYNC_DSN = os.getenv(
    "TEST_DATABASE_SYNC_URL",
    "postgresql+psycopg://monitor:monitor@localhost:5432/agent_monitor_test",
)

# Kafka 测试地址（dev-infra 提供的 Redpanda）
DEFAULT_KAFKA_BOOTSTRAP_SERVERS = os.getenv(
    "TEST_KAFKA_BOOTSTRAP_SERVERS",
    os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092"),
)

# AMP Audit Kafka 主题（与 config/default.toml 保持一致）
AUDIT_KAFKA_TOPIC = "amp.audit"
AUDIT_KAFKA_DLQ_TOPIC = "amp.audit.dlq"

# AMP Heartbeat Kafka 主题与 Redis
HEARTBEAT_KAFKA_TOPIC = "amp.heartbeat"
HEARTBEAT_DELTA_TOPIC = "amp.heartbeat.alive-delta"
HEARTBEAT_DLQ_TOPIC = "amp.heartbeat.dlq"
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", os.getenv("REDIS_URL", "redis://localhost:6379/3"))

# AMP Metrics Kafka 主题
METRICS_KAFKA_TOPIC = "amp.metrics"
METRICS_DLQ_TOPIC = "amp.metrics.dlq"

# VictoriaMetrics 测试基址（respx 拦截用）
TEST_VM_QUERY_URL = "http://vm.test/select/0/prometheus"
TEST_VM_REMOTE_WRITE_URL = "http://vm.test/insert/0/prometheus"

# AMP Access Kafka 主题
ACCESS_KAFKA_TOPIC = "amp.access"
ACCESS_DLQ_TOPIC = "amp.access.dlq"

# AMP Message Kafka 主题
MESSAGE_KAFKA_TOPIC = "amp.message"
MESSAGE_DLQ_TOPIC = "amp.message.dlq"

# AMP System Kafka 主题
SYSTEM_KAFKA_TOPIC = "amp.system"
SYSTEM_DLQ_TOPIC = "amp.system.dlq"

# ClickHouse 测试库
TEST_CLICKHOUSE_DATABASE = "amp_test"

# OpenSearch 测试地址
TEST_OPENSEARCH_HOST = "localhost"
TEST_OPENSEARCH_PORT = 9200
