#!/bin/bash
# acps-infra/dev-infra/postgres/init/01-create-databases.sh
# 初始化 ACPs 各服务的数据库用户和数据库。
# 此脚本由 postgres 容器的 docker-entrypoint-initdb.d 机制自动执行（仅首次初始化时）。
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    CREATE USER registry WITH PASSWORD 'registry';
    CREATE DATABASE agent_registry OWNER registry;
    CREATE DATABASE agent_registry_test OWNER registry;

    CREATE USER ca WITH PASSWORD 'ca';
    CREATE DATABASE agent_ca OWNER ca;
    CREATE DATABASE agent_ca_test OWNER ca;

    CREATE USER discovery WITH PASSWORD 'discovery';
    CREATE DATABASE agent_discovery OWNER discovery;
    CREATE DATABASE agent_discovery_test OWNER discovery;

    CREATE USER monitor WITH PASSWORD 'monitor';
    CREATE DATABASE agent_monitor OWNER monitor;
    CREATE DATABASE agent_monitor_test OWNER monitor;

    CREATE USER keycloak WITH PASSWORD 'keycloak';
    CREATE DATABASE keycloak OWNER keycloak;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname agent_discovery <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS vector;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname agent_discovery_test <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS vector;
EOSQL

echo "ACPs 开发数据库初始化完成：agent_registry, agent_registry_test, agent_ca, agent_ca_test, agent_discovery, agent_discovery_test, agent_monitor, agent_monitor_test, keycloak（含 discovery pgvector 扩展）"
