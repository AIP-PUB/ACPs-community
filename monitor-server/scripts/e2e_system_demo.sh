#!/usr/bin/env bash
# System 开发模式全链路演示：触发 leader /submit → 等待传播 → 断言 Query API。
#
# 用法（从 acps/ 根目录）：
#   bash monitor-server/scripts/e2e_system_demo.sh
#
# 前置：dev-infra、monitor-server、demo-leader、demo-partner、Fluent Bit 均已启动。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACPS_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

MONITOR_URL="${MONITOR_URL:-http://localhost:9009}"
LEADER_URL="${LEADER_URL:-http://localhost:9031}"
SYSTEM_PREFIX="/acps-amp-v1/system"
CLIENT_REQ_ID="system-demo-$(date +%s)"
PARTNER_LOG_GLOB="${PARTNER_LOG_GLOB:-${ACPS_ROOT}/demo-partner/logs/amp_system_*.jsonl}"
TIMEOUT_S="${TIMEOUT_S:-90}"
POLL_INTERVAL_S="${POLL_INTERVAL_S:-2}"

echo "=== AMP System Dev Demo ==="
echo ""

echo "[1] 服务健康检查..."
MONITOR_STATUS=$(curl -sf "${MONITOR_URL}/health" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status","error"))' 2>/dev/null || echo "error")
LEADER_STATUS=$(curl -sf "${LEADER_URL}/api/v1/health" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status","error"))' 2>/dev/null || echo "error")
if [ "${MONITOR_STATUS}" != "ok" ] && [ "${MONITOR_STATUS}" != "degraded" ]; then
    echo "    ✗ FAIL: monitor-server 不可达 (status=${MONITOR_STATUS})" >&2
    exit 1
fi
if [ "${LEADER_STATUS}" != "healthy" ]; then
    echo "    ✗ FAIL: demo-leader 不可达 (status=${LEADER_STATUS})" >&2
    exit 1
fi
if ! curl -sf "http://localhost:9200/_cluster/health" >/dev/null 2>&1; then
    echo "    ✗ FAIL: OpenSearch 不可达 (:9200)" >&2
    exit 1
fi
echo "    ✓ monitor=${MONITOR_STATUS}, leader=${LEADER_STATUS}, opensearch=ok"

echo ""
echo "[2] 触发 leader /api/v1/submit ..."
SUBMIT_RESP=$(curl -sf -X POST "${LEADER_URL}/api/v1/submit" \
    -H "Content-Type: application/json" \
    -d "{\"query\":\"我要在北京订三晚酒店，预算每晚500元，2人入住\",\"clientRequestId\":\"${CLIENT_REQ_ID}\",\"mode\":\"direct_rpc\"}" \
    2>/dev/null || echo "{}")
echo "    submit 响应已返回"

ACTIVE_TASK_ID=$(python3 - <<'PY' "$SUBMIT_RESP"
import json, sys
data = json.loads(sys.argv[1])
result = data.get("result") or {}
print(result.get("activeTaskId") or result.get("active_task_id") or "")
PY
)

if [ -z "${ACTIVE_TASK_ID}" ]; then
    echo "    ✗ FAIL: submit 响应无 result.activeTaskId: ${SUBMIT_RESP}" >&2
    exit 1
fi
echo "    activeTaskId=${ACTIVE_TASK_ID}"

START=$(date -u -v-30M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ)
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DEADLINE=$(( $(date +%s) + TIMEOUT_S ))

echo ""
echo "[3] 轮询 partner system 日志获取 correlationId（最多 ${TIMEOUT_S}s）..."
CORRELATION_ID=""
while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
    CORRELATION_ID=$(python3 - <<'PY' "$ACTIVE_TASK_ID" "$PARTNER_LOG_GLOB"
import glob, json, sys
active_task_id, pattern = sys.argv[1], sys.argv[2]
for path in sorted(glob.glob(pattern)):
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        continue
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        body = rec.get("body") or {}
        if body.get("category") != "llm":
            continue
        cid = rec.get("correlation_id") or body.get("task_id") or ""
        if cid and active_task_id in cid:
            print(cid)
            raise SystemExit
PY
)
    if [ -n "${CORRELATION_ID}" ]; then
        echo "    ✓ correlationId=${CORRELATION_ID}"
        break
    fi
    sleep "${POLL_INTERVAL_S}"
done

if [ -z "${CORRELATION_ID}" ]; then
    echo "    ✗ FAIL: 未在 ${PARTNER_LOG_GLOB} 找到含 activeTaskId 的 LLM system 事件" >&2
    exit 1
fi

echo ""
echo "[4] 轮询 events/query（correlationId + keyword，最多 ${TIMEOUT_S}s）..."
FOUND_CORR=0
FOUND_KEYWORD=0
DEADLINE=$(( $(date +%s) + TIMEOUT_S ))
while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
    END=$(date -u -v+1M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+1 minute' +%Y-%m-%dT%H:%M:%SZ)
    if [ "${FOUND_CORR}" -eq 0 ]; then
        COUNT=$(curl -sf -X POST "${MONITOR_URL}${SYSTEM_PREFIX}/events/query" \
            -H "Content-Type: application/json" \
            -d "{\"timeRange\":{\"startAt\":\"${START}\",\"endAt\":\"${END}\"},\"filter\":{\"conditions\":[{\"field\":\"correlationId\",\"op\":\"eq\",\"value\":\"${CORRELATION_ID}\"}]},\"page\":{\"limit\":50}}" \
            2>/dev/null | python3 -c 'import json,sys; data=json.load(sys.stdin); items=data.get("items",[]); llm=[i for i in items if (i.get("category") or (i.get("body") or {}).get("category"))=="llm"]; print(len(llm))' \
            2>/dev/null || echo "0")
        if [ "${COUNT}" -ge 1 ]; then
            FOUND_CORR=1
            echo "    ✓ correlationId 命中 ${COUNT} 条 LLM 事件"
        fi
    fi
    if [ "${FOUND_KEYWORD}" -eq 0 ]; then
        KW_COUNT=$(curl -sf -X POST "${MONITOR_URL}${SYSTEM_PREFIX}/events/query" \
            -H "Content-Type: application/json" \
            -d "{\"timeRange\":{\"startAt\":\"${START}\",\"endAt\":\"${END}\"},\"filter\":{\"conditions\":[{\"field\":\"correlationId\",\"op\":\"eq\",\"value\":\"${CORRELATION_ID}\"}]},\"keyword\":\"LLM call completed\",\"page\":{\"limit\":20}}" \
            2>/dev/null | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("items",[])))' \
            2>/dev/null || echo "0")
        if [ "${KW_COUNT}" -ge 1 ]; then
            FOUND_KEYWORD=1
            echo "    ✓ keyword（本任务）命中 ${KW_COUNT} 条"
        fi
    fi
    if [ "${FOUND_CORR}" -eq 1 ] && [ "${FOUND_KEYWORD}" -eq 1 ]; then
        break
    fi
    sleep "${POLL_INTERVAL_S}"
done

echo ""
echo "=== 判定 ==="
if [ "${FOUND_CORR}" -eq 1 ] && [ "${FOUND_KEYWORD}" -eq 1 ]; then
    echo "✓ PASS: submit 后 events/query 按 correlationId/keyword 可见 LLM system 事件"
    exit 0
fi

echo "✗ FAIL: FOUND_CORR=${FOUND_CORR} FOUND_KEYWORD=${FOUND_KEYWORD}" >&2
echo "排查：Fluent Bit kafka.5、amp.system LogAppendTime、SystemRuntime、demo-partner amp_system_*.jsonl 是否增长" >&2
exit 1
