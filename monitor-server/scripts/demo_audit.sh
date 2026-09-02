#!/usr/bin/env bash
# Audit 开发模式全链路演示
#
# 前置条件（所有服务已启动）：
#   1. acps-infra:     dev-infra/dev-infra.sh up postgres kafka
#   2. monitor-server: just dev start          (port 9009)
#   3. fluent-bit:     fluent-bit -c monitor-server/config/fluent-bit/fluent-bit.conf &
#   4. demo-partner:   uv run python -m partners.main
#   5. demo-leader:    uv run uvicorn leader.main:app --host 0.0.0.0 --port 9031
#
# 用法（从 acps/ 根目录）：
#   bash monitor-server/scripts/demo_audit.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACPS_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

LEADER_URL="http://localhost:9031"
MONITOR_URL="http://localhost:9009"
LEADER_AIC="1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ"
LEADER_LOG="${ACPS_ROOT}/demo-leader/logs/amp_audit.jsonl"
PARTNER_LOG_GLOB="${ACPS_ROOT}/demo-partner/logs/amp_audit_*.jsonl"

echo "=== AMP Audit E2E Demo ==="
echo ""

# 1. Health checks
echo "[1] Health checks..."
# demo-leader 健康检查在 /api/v1/health（router 带 /api/v1 前缀），返回 {"status":"healthy",...}
if ! curl -sf "$LEADER_URL/api/v1/health" >/dev/null 2>&1; then
    echo "✗ demo-leader not running on $LEADER_URL"
    echo "  启动命令: cd demo-leader && uv run uvicorn leader.main:app --host 0.0.0.0 --port 9031"
    exit 1
fi
# monitor-server 健康检查在顶层 /health，返回 {"status":"ok"}（DB 可达时）
STATUS=$(curl -sf "$MONITOR_URL/health" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "unreachable")
if [ "$STATUS" != "ok" ]; then
    echo "✗ monitor-server not healthy: $STATUS"
    echo "  启动命令: cd monitor-server && just dev start"
    exit 1
fi
echo "    ✓ All services healthy"

# 2. Clear old audit logs
echo "[2] Clearing old audit logs..."
> "$LEADER_LOG" 2>/dev/null || true
shopt -s nullglob
for f in ${PARTNER_LOG_GLOB}; do
    [ -f "$f" ] && > "$f" || true
done
shopt -u nullglob
echo "    ✓ Cleared"

# 3. Submit travel planning request
# 注意：请求体字段是 query（不是 userInput）
echo "[3] Submitting travel planning request..."
RESPONSE=$(curl -sf -X POST "$LEADER_URL/api/v1/submit" \
    -H "Content-Type: application/json" \
    -d '{"query":"我想去北京玩三天，帮我规划一下：住宿、美食、城区景点和郊区景点。","clientRequestId":"demo-audit-001","mode":"direct_rpc"}')

# 响应被 CommonResponse 包裹：sessionId 在 result.sessionId
SESSION_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['sessionId'])")
echo "    ✓ Session: $SESSION_ID"

# 4. Wait for Fluent Bit forwarding and AuditWriter processing
echo "[4] Waiting 35s for Kafka forwarding + AuditWriter processing..."
sleep 35

_audit_line_count() {
    local file=$1
    if [ ! -f "${file}" ]; then
        printf '0'
        return
    fi
    local count
    count=$(grep -c '"log_type":"audit"' "${file}" 2>/dev/null) || count=0
    printf '%s' "${count}"
}

# 5. Count local audit log lines
LOCAL_COUNT=$(_audit_line_count "${LEADER_LOG}")
PARTNER_COUNT=0
shopt -s nullglob
for f in ${PARTNER_LOG_GLOB}; do
    PARTNER_COUNT=$((PARTNER_COUNT + $(_audit_line_count "${f}")))
done
shopt -u nullglob
LOCAL_TOTAL=$((LOCAL_COUNT + PARTNER_COUNT))
echo "[5] Local audit log lines: leader=$LOCAL_COUNT, partner=$PARTNER_COUNT, total=$LOCAL_TOTAL"

# 6. Query monitor-server
echo "[6] Querying audit records in monitor-server..."
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
HOUR_AGO=$(python3 -c "
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc)-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ'))
")

# 按 traceId（leader session ID）过滤 A1/A2 记录
# 注意：filter.field 使用白名单字段名 traceId（camelCase）
RESULT=$(curl -sf -X POST "$MONITOR_URL/acps-amp-v1/audit/records/query" \
    -H "Content-Type: application/json" \
    -d "{
        \"timeRange\":{\"startAt\":\"$HOUR_AGO\",\"endAt\":\"$NOW\"},
        \"filter\":{\"conditions\":[{\"field\":\"traceId\",\"op\":\"eq\",\"value\":\"$SESSION_ID\"}]}
    }")
DB_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))")
FRESHNESS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('meta',{}).get('dataFreshnessAt','N/A'))")

# 按时间范围查询全部（含 partner B1/B2）
RESULT_ALL=$(curl -sf -X POST "$MONITOR_URL/acps-amp-v1/audit/records/query" \
    -H "Content-Type: application/json" \
    -d "{\"timeRange\":{\"startAt\":\"$HOUR_AGO\",\"endAt\":\"$NOW\"}}")
DB_TOTAL=$(echo "$RESULT_ALL" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))")

echo ""
echo "=== Demo Result ==="
printf "  Session ID:             %s\n" "$SESSION_ID"
printf "  Local log lines:        leader=%s partner=%s total=%s\n" "$LOCAL_COUNT" "$PARTNER_COUNT" "$LOCAL_TOTAL"
printf "  DB records (by traceId):%s\n" "$DB_COUNT"
printf "  DB records (all recent):%s\n" "$DB_TOTAL"
printf "  dataFreshnessAt:        %s\n" "$FRESHNESS"
echo ""
echo "  Actions in DB (by traceId):"
# 响应项是 AuditRecordView：aic 在顶层，actionName/resultStatus 在嵌套的 body.action / body.result
echo "$RESULT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for item in d.get('items', []):
    aic = item.get('aic', '')
    action = item.get('body', {}).get('action', {}).get('name', '?')
    status = item.get('body', {}).get('result', {}).get('status', '?')
    print(f'    {aic[-20:]} | {action:<30s} | {status}')
"
echo ""
echo "  All recent actions in DB:"
echo "$RESULT_ALL" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for item in d.get('items', []):
    aic = item.get('aic', '')
    action = item.get('body', {}).get('action', {}).get('name', '?')
    status = item.get('body', {}).get('result', {}).get('status', '?')
    print(f'    {aic[-20:]} | {action:<30s} | {status}')
"

if [ "$DB_COUNT" -ge 2 ]; then
    echo ""
    echo "✓ PASS: $DB_COUNT audit records found for session $SESSION_ID"
else
    echo ""
    echo "✗ FAIL: expected >= 2 records by traceId, got $DB_COUNT"
    echo "  (total recent DB records: $DB_TOTAL — check if partner B1/B2 are included there)"
    exit 1
fi
