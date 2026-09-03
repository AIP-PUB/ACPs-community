#!/usr/bin/env bash
# Access 开发模式全链路演示：触发 leader /submit → 等待传播 → 断言 Query API。
#
# 用法（从 acps/ 根目录）：
#   bash monitor-server/scripts/e2e_access_demo.sh
#
# 前置：dev-infra、monitor-server、demo-leader、demo-partner、Fluent Bit 均已启动。
set -euo pipefail

MONITOR_URL="${MONITOR_URL:-http://localhost:9009}"
LEADER_URL="${LEADER_URL:-http://localhost:9031}"
ACCESS_PREFIX="/acps-amp-v1/access"
CLIENT_REQ_ID="access-demo-$(date +%s)"

echo "=== AMP Access Dev Demo ==="
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
echo "[2] 触发 leader /api/v1/submit ..."
SUBMIT_RESP=$(curl -sf -X POST "${LEADER_URL}/api/v1/submit" \
    -H "Content-Type: application/json" \
    -d "{\"query\":\"我要在北京订三晚酒店，预算每晚500元，2人入住\",\"clientRequestId\":\"${CLIENT_REQ_ID}\",\"mode\":\"direct_rpc\"}" \
    2>/dev/null || echo "{}")
echo "    submit 响应已返回"

echo ""
echo "[3] 轮询 events/query（最多 60s）..."
FOUND=0
for i in $(seq 1 30); do
    COUNT=$(curl -sf -X POST "${MONITOR_URL}${ACCESS_PREFIX}/events/query" \
        -H "Content-Type: application/json" \
        -d '{"timeRange":{"startAt":"'"$(date -u -v-30M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ)"'","endAt":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"},"page":{"limit":5}}' \
        2>/dev/null | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("items",[])))' 2>/dev/null || echo "0")
    if [ "${COUNT}" -gt 0 ]; then
        FOUND=1
        echo "    ✓ events/query 返回 ${COUNT} 条（第 $((i*2))s）"
        break
    fi
    sleep 2
done

echo ""
echo "[4] traces/query 探活..."
TRACE_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" -X POST "${MONITOR_URL}${ACCESS_PREFIX}/traces/query" \
    -H "Content-Type: application/json" \
    -d '{"timeRange":{"startAt":"'"$(date -u -v-30M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ)"'","endAt":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"},"page":{"limit":5}}' \
    2>/dev/null || echo "000")

echo ""
echo "[5] topology/query 探活..."
TOPO_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" -X POST "${MONITOR_URL}${ACCESS_PREFIX}/topology/query" \
    -H "Content-Type: application/json" \
    -d '{"timeRange":{"startAt":"'"$(date -u -v-30M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ)"'","endAt":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"},"page":{"limit":5}}' \
    2>/dev/null || echo "000")

echo ""
echo "=== 判定 ==="
if [ "${FOUND}" -eq 1 ] && [ "${TRACE_STATUS}" = "200" ] && [ "${TOPO_STATUS}" = "200" ]; then
    echo "✓ PASS: submit 后 events/traces/topology API 均可访问"
    exit 0
fi

echo "✗ FAIL: events=${FOUND}, traces HTTP=${TRACE_STATUS}, topology HTTP=${TOPO_STATUS}"
echo "排查：Fluent Bit kafka.3、amp.access LogAppendTime、AccessRuntime、demo 访问日志文件是否增长"
exit 1
