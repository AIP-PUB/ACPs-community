#!/usr/bin/env bash
# Heartbeat 开发模式全链路演示：前置——所有服务已启动且 demo 正在周期发心跳。
#
# 用法：
#   bash monitor-server/scripts/demo_heartbeat.sh
#
# 前置条件：
#   - acps-infra 基础设施（postgres / kafka / redis）已就绪
#   - monitor-server 已启动（port 9009，HeartbeatRuntime 已启动）
#   - Fluent Bit 正在运行（heartbeat INPUT/OUTPUT 已配置）
#   - demo-leader 已启动（port 9031，心跳后台任务已开始）
#   - demo-partner 已启动（各 partner 心跳后台任务已开始）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACPS_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

MONITOR_URL="http://localhost:9009"
PREFIX="/acps-amp-v1/heartbeat"
LEADER_URL="http://localhost:9031"
LEADER_AIC="1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ"

echo "=== AMP Heartbeat Dev Demo ==="
echo ""

# ── 1. 健康检查 ───────────────────────────────────────────────────────────────
echo "[1] 服务健康检查..."

if ! curl -sf "${LEADER_URL}/api/v1/health" >/dev/null 2>&1; then
    echo "✗ FAIL: demo-leader 不可达 (${LEADER_URL})"
    exit 1
fi
echo "    ✓ demo-leader healthy"

MONITOR_STATUS=$(curl -sf "${MONITOR_URL}/health" | python3 -c \
    'import sys,json; print(json.load(sys.stdin)["status"])' 2>/dev/null || echo "error")
if [ "${MONITOR_STATUS}" != "ok" ]; then
    echo "✗ FAIL: monitor-server 状态异常 (status=${MONITOR_STATUS})"
    exit 1
fi
echo "    ✓ monitor-server healthy"

# ── 2. 等待心跳传播 ───────────────────────────────────────────────────────────
echo ""
echo "[2] 等待 25s（心跳周期 15s + Fluent Bit 2s + Writer 批量处理）..."
sleep 25

# ── 3. 检查本地心跳文件 ───────────────────────────────────────────────────────
echo ""
echo "[3] 本地心跳文件检查..."

LEADER_HB_FILE="${ACPS_ROOT}/demo-leader/logs/amp_heartbeat.jsonl"
if [ -f "${LEADER_HB_FILE}" ]; then
    LEADER_LINES=$(wc -l < "${LEADER_HB_FILE}" | tr -d ' ')
    echo "    ✓ demo-leader 心跳文件存在 (${LEADER_LINES} 条记录)"
else
    echo "    ✗ WARN: demo-leader 心跳文件未找到 (${LEADER_HB_FILE})"
fi

PARTNER_FILES=$(ls "${ACPS_ROOT}"/demo-partner/logs/amp_heartbeat_*.jsonl 2>/dev/null | wc -l | tr -d ' ')
if [ "${PARTNER_FILES}" -gt 0 ]; then
    echo "    ✓ demo-partner 心跳文件存在 (${PARTNER_FILES} 个 Agent)"
else
    echo "    ✗ WARN: demo-partner 心跳文件未找到"
fi

# ── 4. 验证 summary ───────────────────────────────────────────────────────────
echo ""
echo "[4] Query API: summary..."

SUMMARY=$(curl -sf "${MONITOR_URL}${PREFIX}/summary" 2>/dev/null || echo "{}")
ALIVE=$(echo "${SUMMARY}" | python3 -c \
    'import sys,json; print(json.load(sys.stdin).get("data",{}).get("aliveCount",0))' \
    2>/dev/null || echo "0")
echo "    aliveCount = ${ALIVE}"

# ── 5. 验证 leader liveness ───────────────────────────────────────────────────
echo ""
echo "[5] Query API: leader liveness..."

LV=$(curl -sf "${MONITOR_URL}${PREFIX}/liveness/${LEADER_AIC}" 2>/dev/null || echo "{}")
IS_ALIVE=$(echo "${LV}" | python3 -c \
    'import sys,json; print(json.load(sys.stdin).get("data",{}).get("isAlive",False))' \
    2>/dev/null || echo "False")
LIVENESS_STATE=$(echo "${LV}" | python3 -c \
    'import sys,json; print(json.load(sys.stdin).get("data",{}).get("livenessState","unknown"))' \
    2>/dev/null || echo "unknown")
SILENCE_DUR=$(echo "${LV}" | python3 -c \
    'import sys,json; print(json.load(sys.stdin).get("data",{}).get("silenceDurationSeconds","N/A"))' \
    2>/dev/null || echo "N/A")
echo "    leader isAlive=${IS_ALIVE}, livenessState=${LIVENESS_STATE}, silenceDuration=${SILENCE_DUR}s"

# ── 6. 验证 Sync API ──────────────────────────────────────────────────────────
echo ""
echo "[6] Sync API: snapshot 行数..."

SNAP_LINES=$(curl -sf "${MONITOR_URL}${PREFIX}/sync/snapshot" 2>/dev/null | grep -c . || echo "0")
echo "    snapshot lines = ${SNAP_LINES} (含首行 snapshot-meta)"

# ── 7. DLQ 核对 ───────────────────────────────────────────────────────────────
echo ""
echo "[7] Kafka DLQ 核对..."
if command -v docker &>/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -q dev-redpanda; then
    DLQ_HW=$(docker exec dev-redpanda rpk topic describe amp.heartbeat.dlq -p 2>/dev/null \
        | awk '$1=="0"{print $6}' || echo "N/A")
    echo "    amp.heartbeat.dlq HIGH-WATERMARK = ${DLQ_HW}"
    if [ "${DLQ_HW}" = "0" ] || [ "${DLQ_HW}" = "N/A" ]; then
        echo "    ✓ DLQ 为空（正常）"
    else
        echo "    ✗ WARN: DLQ 不为空（可能 topic 非 LogAppendTime，见 §5.1）"
    fi
else
    echo "    跳过（Redpanda 容器未运行）"
fi

# ── 判定 ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== 判定 ==="

if [ "${ALIVE}" -ge 1 ] && [ "${IS_ALIVE}" = "True" ]; then
    echo "✓ PASS: aliveCount=${ALIVE}, leader isAlive=True (${LIVENESS_STATE})"
    exit 0
else
    echo "✗ FAIL: aliveCount=${ALIVE}, leader isAlive=${IS_ALIVE} (${LIVENESS_STATE})"
    echo ""
    echo "排查建议："
    echo "  1. 确认 demo-leader / demo-partner 已启动且心跳文件在增长"
    echo "  2. 确认 Fluent Bit 正在运行（heartbeat OUTPUT worker 已启动）"
    echo "  3. 确认 amp.heartbeat topic 为 LogAppendTime（见文档 §5.1）"
    echo "  4. 确认 monitor-server HeartbeatRuntime 已启动（日志含 'HeartbeatRuntime started'）"
    exit 1
fi
