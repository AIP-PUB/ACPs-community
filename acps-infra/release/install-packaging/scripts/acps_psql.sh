#!/usr/bin/env bash
# acps_psql.sh — mode 无关 PostgreSQL 执行入口（host 直装设计 §5.3 / 实施计划 Step 2）。
#
# 统一各 role 内联 SQL 的执行方式，按部署模式派生等价命令：
#   image：docker exec -e PGPASSWORD=… {{prefix}}-postgresql-1 psql -v ON_ERROR_STOP=1 -U <su> -d <db> …
#   host ：PGPASSWORD=… psql -h <host> -p <port> -v ON_ERROR_STOP=1 -U <su> -d <db> …
#
# 用法示例：
#   acps_psql.sh --mode image --container acps-postgresql-1 \
#     --user postgres --db postgres --sql 'SELECT 1'
#   PGPASSWORD='...' acps_psql.sh --mode host --host 10.0.0.5 --port 5432 \
#     --user postgres --db postgres --file /path/to.sql
#   PGPASSWORD='...' acps_psql.sh --mode host --host 127.0.0.1 --user postgres --db postgres <<SQL
#   SELECT 1;
#   SQL
#
# 口令：优先经环境变量传递（PGPASSWORD 或 ACPS_PSQL_PASSWORD），不出现在进程 argv
# 中（避免 `ps` 可见）；亦兼容 --password 供直接命令行调用，但脚本自身绝不回显/记录
# 口令内容。所有分支恒加 -v ON_ERROR_STOP=1（本设计 §5.3 拍板）。
set -euo pipefail

PROG="$(basename "$0")"

usage() {
  cat <<'USAGE'
用法：acps_psql.sh --mode <image|host> [选项]

必填：
  --mode <image|host>       部署模式（或环境变量 ACPS_DEPLOY_MODE）
  --user <user>             psql -U user（或环境变量 ACPS_PSQL_USER）
  --db <dbname>             psql -d dbname（或环境变量 ACPS_PSQL_DB）

image 模式：
  --container <name>        docker 容器名（或环境变量 ACPS_PSQL_CONTAINER）

host 模式：
  --host <host>             PG 主机，默认 127.0.0.1（或环境变量 ACPS_PSQL_HOST）
  --port <port>             PG 端口，默认 5432（或环境变量 ACPS_PSQL_PORT）

口令（不回显/不记录；优先用环境变量）：
  --password <pw>           亦可用环境变量 ACPS_PSQL_PASSWORD 或 PGPASSWORD

SQL 输入（三选一；--sql / --file 均未给出时，从 stdin 读取多语句 SQL，
适合 heredoc 调用）：
  --sql <sql>                单条语句/表达式（psql -c）
  --file <path>              SQL 文件（image 模式经 stdin 重定向进容器；host 模式用 psql -f）

输出整形（可选，转发给 psql）：
  --tuples-only              psql -t（仅数据行，无表头/行数）
  --no-align                 psql -A（非对齐输出，配合 -t 常用于脚本读取单值）
  --quiet                    psql -q

  -h, --help                 显示本帮助
USAGE
}

MODE="${ACPS_DEPLOY_MODE:-}"
CONTAINER="${ACPS_PSQL_CONTAINER:-}"
PG_HOST="${ACPS_PSQL_HOST:-127.0.0.1}"
PG_PORT="${ACPS_PSQL_PORT:-5432}"
PG_USER="${ACPS_PSQL_USER:-}"
PG_PASSWORD="${ACPS_PSQL_PASSWORD:-${PGPASSWORD:-}}"
PG_DB="${ACPS_PSQL_DB:-}"
SQL_TEXT=""
SQL_FILE=""
HAVE_SQL=0
HAVE_FILE=0
declare -a PSQL_EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) shift; MODE="${1:-}" ;;
    --container) shift; CONTAINER="${1:-}" ;;
    --host) shift; PG_HOST="${1:-}" ;;
    --port) shift; PG_PORT="${1:-}" ;;
    --user) shift; PG_USER="${1:-}" ;;
    --password) shift; PG_PASSWORD="${1:-}" ;;
    --db) shift; PG_DB="${1:-}" ;;
    --sql) shift; SQL_TEXT="${1:-}"; HAVE_SQL=1 ;;
    --file) shift; SQL_FILE="${1:-}"; HAVE_FILE=1 ;;
    --tuples-only) PSQL_EXTRA_ARGS+=(-t) ;;
    --no-align) PSQL_EXTRA_ARGS+=(-A) ;;
    --quiet) PSQL_EXTRA_ARGS+=(-q) ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "[ERROR] ${PROG}: 未知参数：$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "${HAVE_SQL}" -eq 1 && "${HAVE_FILE}" -eq 1 ]]; then
  echo "[ERROR] ${PROG}: --sql 与 --file 互斥" >&2
  exit 2
fi
if [[ -z "${MODE}" ]]; then
  echo "[ERROR] ${PROG}: 缺少 --mode（或环境变量 ACPS_DEPLOY_MODE），须为 image|host" >&2
  exit 2
fi
if [[ "${MODE}" != "image" && "${MODE}" != "host" ]]; then
  echo "[ERROR] ${PROG}: --mode 须为 image|host，收到：${MODE}" >&2
  exit 2
fi
if [[ -z "${PG_USER}" ]]; then
  echo "[ERROR] ${PROG}: 缺少 --user（或环境变量 ACPS_PSQL_USER）" >&2
  exit 2
fi
if [[ -z "${PG_DB}" ]]; then
  echo "[ERROR] ${PROG}: 缺少 --db（或环境变量 ACPS_PSQL_DB）" >&2
  exit 2
fi
if [[ "${MODE}" == "image" && -z "${CONTAINER}" ]]; then
  echo "[ERROR] ${PROG}: --mode image 须提供 --container（或环境变量 ACPS_PSQL_CONTAINER）" >&2
  exit 2
fi
if [[ "${HAVE_FILE}" -eq 1 && ! -r "${SQL_FILE}" ]]; then
  echo "[ERROR] ${PROG}: --file 不可读：${SQL_FILE}" >&2
  exit 2
fi

declare -a PSQL_ARGS=(-v ON_ERROR_STOP=1 -U "${PG_USER}" -d "${PG_DB}")
if [[ ${#PSQL_EXTRA_ARGS[@]} -gt 0 ]]; then
  PSQL_ARGS+=("${PSQL_EXTRA_ARGS[@]}")
fi
if [[ "${HAVE_SQL}" -eq 1 ]]; then
  PSQL_ARGS+=(-c "${SQL_TEXT}")
fi

run_image() {
  # -i 恒加：heredoc/--file 需转发 stdin；--sql/无输入时对端读到 EOF 即返回，无副作用。
  declare -a docker_args=(exec -i -e "PGPASSWORD=${PG_PASSWORD}" "${CONTAINER}" psql "${PSQL_ARGS[@]}")
  if [[ "${HAVE_FILE}" -eq 1 ]]; then
    docker "${docker_args[@]}" < "${SQL_FILE}"
  else
    docker "${docker_args[@]}"
  fi
}

run_host() {
  declare -a host_args=(-h "${PG_HOST}" -p "${PG_PORT}" "${PSQL_ARGS[@]}")
  if [[ "${HAVE_FILE}" -eq 1 ]]; then
    host_args+=(-f "${SQL_FILE}")
  fi
  PGPASSWORD="${PG_PASSWORD}" psql "${host_args[@]}"
}

case "${MODE}" in
  image) run_image ;;
  host) run_host ;;
esac
