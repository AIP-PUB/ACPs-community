#!/usr/bin/env bash
# Message 开发模式全链路演示：触发 leader 群组 /submit → 等待传播 → 断言 Query API。
#
# 用法（从 acps/ 根目录）：
#   bash monitor-server/scripts/e2e_message_demo.sh
#
# 前置：dev-infra、monitor-server、mq-auth-server、demo-leader（群组模式）、demo-partner、Fluent Bit 均已启动。
set -euo pipefail

MONITOR_URL="${MONITOR_URL:-http://localhost:9009}"
LEADER_URL="${LEADER_URL:-http://localhost:9031}"
MESSAGE_PREFIX="/acps-amp-v1/message"
CLIENT_REQ_ID="message-demo-$(date +%s)"

echo "=== AMP Message Dev Demo ==="
echo ""

echo "[1] 服务健康检查..."
MONITOR_STATUS=$(curl -sf "${MONITOR_URL}/health" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status","error"))' 2>/dev/null || echo "error")
LEADER_STATUS=$(curl -sf "${LEADER_URL}/api/v1/health" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status","error"))' 2>/dev/null || echo "error")
if [ "${MONITOR_STATUS}" != "ok" ] && [ "${MONITOR_STATUS}" != "degraded" ]; then
    echo "    ✗ FAIL: monitor-server 不可达 (status=${MONITOR_STATUS})"
    exit 1
fi
if [ "${LEADER_STATUS}" != "healthy" ]; then
    echo "    ✗ FAIL: demo-leader 不可达 (status=${LEADER_STATUS})"
    exit 1
fi
echo "    ✓ monitor=${MONITOR_STATUS}, leader=${LEADER_STATUS}"

echo ""
echo "[2] 触发 leader 群组模式 /api/v1/submit ..."
curl -sf -X POST "${LEADER_URL}/api/v1/submit" \
    -H "Content-Type: application/json" \
    -d "{\"query\":\"我要在北京订三晚酒店，预算每晚500元，2人入住\",\"clientRequestId\":\"${CLIENT_REQ_ID}\",\"mode\":\"group\"}" \
    >/dev/null 2>&1 || true
echo "    submit 已发送（群组模式）"

START=$(date -u -v-30M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ)
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
QUERY_BODY="{\"timeRange\":{\"startAt\":\"${START}\",\"endAt\":\"${END}\"},\"filter\":{\"conditions\":[{\"field\":\"system\",\"op\":\"eq\",\"value\":\"rabbitmq\"}]},\"page\":{\"limit\":20}}"

echo ""
echo "[3] 轮询 events/query（最多 240s，群组+LLM 规划可能较慢）..."
EVENTS_FOUND=0
SAMPLE_TRACE_ID=""
SAMPLE_DEST_NAME=""
for i in $(seq 1 120); do
    EVENTS_JSON=$(curl -sf -X POST "${MONITOR_URL}${MESSAGE_PREFIX}/events/query" \
        -H "Content-Type: application/json" \
        -d "${QUERY_BODY}" 2>/dev/null || echo "{}")
    read -r COUNT SAMPLE_TRACE_ID SAMPLE_DEST_NAME <<<"$(printf '%s' "${EVENTS_JSON}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
items = data.get("items") or []
count = len(items)
trace_id = items[0].get("traceId", "") if items else ""
dest = items[0].get("destinationName", "") if items else ""
print(count, trace_id, dest)
' 2>/dev/null || echo "0  ")"
    if [ "${COUNT}" -gt 0 ]; then
        EVENTS_FOUND=1
        echo "    ✓ events/query 返回 ${COUNT} 条（第 $((i*2))s）"
        break
    fi
    sleep 2
done

LC_QUERY_BODY="${QUERY_BODY}"
if [ -n "${SAMPLE_TRACE_ID}" ]; then
    LC_QUERY_BODY="{\"timeRange\":{\"startAt\":\"${START}\",\"endAt\":\"${END}\"},\"filter\":{\"conditions\":[{\"field\":\"traceId\",\"op\":\"eq\",\"value\":\"${SAMPLE_TRACE_ID}\"}]},\"page\":{\"limit\":20}}"
fi

echo ""
echo "[4] 轮询 lifecycles/query（最多 60s）..."
LC_FOUND=0
for i in $(seq 1 30); do
    COUNT=$(curl -sf -X POST "${MONITOR_URL}${MESSAGE_PREFIX}/lifecycles/query" \
        -H "Content-Type: application/json" \
        -d "${LC_QUERY_BODY}" \
        2>/dev/null | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("items",[])))' 2>/dev/null || echo "0")
    if [ "${COUNT}" -gt 0 ]; then
        LC_FOUND=1
        echo "    ✓ lifecycles/query 返回 ${COUNT} 条（第 $((i*2))s）"
        break
    fi
    sleep 2
done

TP_DEST="${SAMPLE_DEST_NAME:-group}"
echo ""
echo "[5] destinations/throughput 探活（destination=${TP_DEST}）..."
TP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${MONITOR_URL}${MESSAGE_PREFIX}/destinations/throughput" \
    -H "Content-Type: application/json" \
    -d "{\"timeRange\":{\"startAt\":\"${START}\",\"endAt\":\"${END}\"},\"system\":\"rabbitmq\",\"destinationName\":\"${TP_DEST}\"}" \
    2>/dev/null || echo "000")

echo ""
echo "=== 判定 ==="
if [ "${EVENTS_FOUND}" -eq 1 ] && [ "${LC_FOUND}" -eq 1 ] && [ "${TP_STATUS}" = "200" ]; then
    echo "✓ PASS: 群组 submit 后 events/lifecycles/throughput API 均可访问"
    exit 0
fi

echo "✗ FAIL: events=${EVENTS_FOUND}, lifecycles=${LC_FOUND}, throughput HTTP=${TP_STATUS}"
echo "排查：mq-auth-server、Fluent Bit kafka.4、amp.message LogAppendTime、MessageRuntime、demo 群组模式、amp_message*.jsonl 是否增长"
exit 1
