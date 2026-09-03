#!/usr/bin/env bash
# Metrics 开发模式全链路演示：前置——所有服务已启动且 demo 正在周期发 metrics 日志。
#
# 用法：
#   bash monitor-server/scripts/demo_metrics.sh
#
# 前置条件：
#   - acps-infra 基础设施（kafka / redis / victoria-metrics）已就绪
#   - monitor-server 已启动（port 9009，MetricsRuntime 已启动）
#   - Fluent Bit 正在运行（metrics INPUT/OUTPUT 已配置）
#   - demo-leader 已启动（metrics 后台任务已开始）
#   - demo-partner 已启动（各 partner metrics 后台任务已开始）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACPS_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

MONITOR_URL="http://localhost:9009"
METRICS_PREFIX="/acps-amp-v1/metrics"

# demo-leader 的 AIC（从 atr/acs.json 读取，或手动指定）
LEADER_AIC="${LEADER_AIC:-$(python3 -c "import json; print(json.load(open('${ACPS_ROOT}/demo-leader/leader/atr/acs.json'))['aic'])" 2>/dev/null || echo "")}"

echo "=== AMP Metrics Dev Demo ==="
echo ""

# ── 1. 健康检查 ───────────────────────────────────────────────────────────────
echo "[1] 服务健康检查..."

MONITOR_HEALTH=$(curl -sf "${MONITOR_URL}/health" 2>/dev/null || echo "{}")
MONITOR_STATUS=$(echo "${MONITOR_HEALTH}" | python3 -c \
    'import sys,json; print(json.load(sys.stdin).get("status","error"))' 2>/dev/null || echo "error")
VM_STATUS=$(echo "${MONITOR_HEALTH}" | python3 -c \
    'import sys,json; print(json.load(sys.stdin).get("checks",{}).get("victoria_metrics","unknown"))' \
    2>/dev/null || echo "unknown")

# DLQ 基线（用于 §6 判断本次验证期间是否新增坏消息）
DLQ_BASELINE="N/A"
if command -v docker &>/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -q dev-redpanda; then
    DLQ_BASELINE=$(docker exec dev-redpanda rpk topic describe amp.metrics.dlq -p 2>/dev/null \
        | awk '$1=="0"{print $6}' || echo "N/A")
fi

if [ "${MONITOR_STATUS}" = "ok" ] || [ "${MONITOR_STATUS}" = "degraded" ]; then
    echo "    ✓ monitor-server status=${MONITOR_STATUS}, victoria_metrics=${VM_STATUS}"
else
    echo "    ✗ FAIL: monitor-server 不可达 (status=${MONITOR_STATUS})"
    exit 1
fi

# ── 2. 等待 metrics 传播 ──────────────────────────────────────────────────────
echo ""
echo "[2] 等待 40s（metrics 周期 30s + Fluent Bit 2s + Writer 批量处理）..."
sleep 40

# ── 3. 检查本地 metrics 文件 ──────────────────────────────────────────────────
echo ""
echo "[3] 本地 metrics 文件检查..."

LEADER_M_FILE="${ACPS_ROOT}/demo-leader/logs/amp_metrics.jsonl"
if [ -f "${LEADER_M_FILE}" ]; then
    LEADER_LINES=$(wc -l < "${LEADER_M_FILE}" | tr -d ' ')
    echo "    ✓ demo-leader metrics 文件存在 (${LEADER_LINES} 条记录)"
else
    echo "    ✗ WARN: demo-leader metrics 文件未找到 (${LEADER_M_FILE})"
fi

PARTNER_FILES=$(ls "${ACPS_ROOT}"/demo-partner/logs/amp_metrics_*.jsonl 2>/dev/null | wc -l | tr -d ' ')
if [ "${PARTNER_FILES}" -gt 0 ]; then
    echo "    ✓ demo-partner metrics 文件存在 (${PARTNER_FILES} 个 Agent)"
else
    echo "    ✗ WARN: demo-partner metrics 文件未找到"
fi

# ── 4. 验证 snapshots/query ───────────────────────────────────────────────────
echo ""
echo "[4] Query API: snapshots/query (无 filter，取前 10 条)..."

SNAPS=$(curl -sf -X POST "${MONITOR_URL}${METRICS_PREFIX}/snapshots/query" \
    -H "Content-Type: application/json" \
    -d '{"page":{"limit":10}}' 2>/dev/null || echo "{}")

SNAP_COUNT=$(echo "${SNAPS}" | python3 -c \
    'import sys,json; d=json.load(sys.stdin); print(len(d.get("items",[])))' 2>/dev/null || echo "0")
TOTAL=$(echo "${SNAPS}" | python3 -c \
    'import sys,json; d=json.load(sys.stdin); print(d.get("meta",{}).get("total") or len(d.get("items",[])))' 2>/dev/null || echo "?")

echo "    返回 items=${SNAP_COUNT}, total=${TOTAL}"

if [ "${SNAP_COUNT}" -gt 0 ]; then
    # 打印第一条快照
    FIRST_AIC=$(echo "${SNAPS}" | python3 -c \
        'import sys,json; print(json.load(sys.stdin)["items"][0]["aic"])' 2>/dev/null || echo "?")
    FIRST_UPTIME=$(echo "${SNAPS}" | python3 -c \
        'import sys,json; print(json.load(sys.stdin)["items"][0].get("uptimeSeconds","?"))' 2>/dev/null || echo "?")
    echo "    首条快照: aic=${FIRST_AIC}, uptimeSeconds=${FIRST_UPTIME}"
fi

# ── 5. 验证 leader 快照（如果 AIC 已知）─────────────────────────────────────
if [ -n "${LEADER_AIC}" ]; then
    echo ""
    echo "[5] Query API: leader 快照 (aic=${LEADER_AIC})..."
    LEADER_SNAP=$(curl -sf -X POST "${MONITOR_URL}${METRICS_PREFIX}/snapshots/query" \
        -H "Content-Type: application/json" \
        -d "{\"filter\":{\"conditions\":[{\"field\":\"aic\",\"op\":\"eq\",\"value\":\"${LEADER_AIC}\"}]},\"page\":{\"limit\":1}}" \
        2>/dev/null || echo "{}")
    LEADER_SNAP_COUNT=$(echo "${LEADER_SNAP}" | python3 -c \
        'import sys,json; print(len(json.load(sys.stdin).get("items",[])))' 2>/dev/null || echo "0")
    if [ "${LEADER_SNAP_COUNT}" -ge 1 ]; then
        LEADER_UPTIME=$(echo "${LEADER_SNAP}" | python3 -c \
            'import sys,json; print(json.load(sys.stdin)["items"][0].get("uptimeSeconds","?"))' 2>/dev/null || echo "?")
        echo "    ✓ leader 快照存在: uptimeSeconds=${LEADER_UPTIME}"
    else
        echo "    ✗ WARN: leader 快照不存在（Fluent Bit/writer 可能未处理）"
    fi
fi

# ── 6. DLQ 核对 ───────────────────────────────────────────────────────────────
echo ""
echo "[6] Kafka DLQ 核对..."
if command -v docker &>/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -q dev-redpanda; then
    DLQ_HW=$(docker exec dev-redpanda rpk topic describe amp.metrics.dlq -p 2>/dev/null \
        | awk '$1=="0"{print $6}' || echo "N/A")
    echo "    amp.metrics.dlq HIGH-WATERMARK = ${DLQ_HW}（基线=${DLQ_BASELINE}）"
    if [ "${DLQ_HW}" = "N/A" ]; then
        echo "    跳过（无法读取 DLQ 水位）"
    elif [ "${DLQ_HW}" = "0" ]; then
        echo "    ✓ DLQ 为空（正常）"
    elif [ "${DLQ_BASELINE}" != "N/A" ] && [ "${DLQ_HW}" -gt "${DLQ_BASELINE}" ] 2>/dev/null; then
        echo "    ✗ WARN: DLQ 在本次验证期间增长（${DLQ_BASELINE} → ${DLQ_HW}，见 dev-runbook-metrics §5.1）"
    else
        echo "    ✓ DLQ 未在本次验证期间增长（历史水位 ${DLQ_HW} 可接受）"
    fi
else
    echo "    跳过（Redpanda 容器未运行）"
fi

# ── 7. VictoriaMetrics 探活 ──────────────────────────────────────────────────
echo ""
echo "[7] VictoriaMetrics 健康检查..."
VM_HEALTH=$(curl -sf "http://localhost:8428/health" 2>/dev/null || echo "FAIL")
if echo "${VM_HEALTH}" | grep -qi "ok\|healthy"; then
    echo "    ✓ VictoriaMetrics healthy"
else
    echo "    ✗ WARN: VictoriaMetrics 不可达 (${VM_HEALTH})"
fi

# ── 判定 ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== 判定 ==="

if [ "${SNAP_COUNT}" -ge 1 ]; then
    echo "✓ PASS: snapshots/query 返回 ${SNAP_COUNT} 条（total=${TOTAL}），Metrics 通路验证通过！"
    exit 0
else
    echo "✗ FAIL: snapshots/query 返回 0 条"
    echo ""
    echo "排查建议："
    echo "  1. 确认 demo-leader / demo-partner 已启动且 amp_metrics*.jsonl 文件在增长"
    echo "  2. 确认 Fluent Bit 正在运行（metrics OUTPUT kafka.2 worker 已启动）"
    echo "  3. 确认 amp.metrics topic 为 LogAppendTime（见文档 §O-2）"
    echo "  4. 确认 monitor-server MetricsRuntime 已启动（日志含 'MetricsRuntime started'）"
    echo "  5. 若 VictoriaMetrics 未启动，运行：just infra up victoria-metrics"
    exit 1
fi
