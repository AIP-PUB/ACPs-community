# 开发模式 Metrics 联合验证

**前置**：请先按 [dev-runbook.md](./dev-runbook.md) 完成服务启动（monitor-server、demo-leader、
demo-partner、Fluent Bit、VictoriaMetrics）。本文只讲 Metrics 链路在开发模式下的验证内容。

**开始验证前的快速检查**（任一不满足则先修复再继续）：

```bash
# 1. amp.metrics 必须为 LogAppendTime
docker exec dev-redpanda rpk topic describe amp.metrics -c | grep timestamp.type
# 期望：LogAppendTime（否则执行 just -f monitor-server/Justfile infra up kafka）

# 2. Fluent Bit 必须含 kafka.2 worker（metrics OUTPUT）
# 启动/重启后标准输出应出现：[output:kafka:kafka.2] worker #0 started

# 3. demo 进程需含 metrics 后台任务（partner 代码更新后须 restart）
just -f demo-leader/Justfile app logs | grep "AMP metrics started"
just -f demo-partner/Justfile app logs | grep "AMP metrics started"
```

与 Heartbeat 链路（见 [`dev-runbook-heartbeat.md`](./dev-runbook-heartbeat.md)）相比，Metrics 有三点根本差异：

- **存储双写**：采样指标写入 **Redis 快照缓存**（最新态）和 **VictoriaMetrics**（时序真相源）。Heartbeat 只写 Redis。
- **批量 Remote Write**：MetricsWriter 攒批（5s 或 10k 样本）后一次性 Remote Write，非逐条写入。
- **五端点无 Sync**：Query API 提供 5 个端点（snapshots/series/rankings/slo/capacity），**无** Heartbeat 的 Sync 平面。

建议首次阅读先用[第 4 章快速脚本](#4-快速验证脚本)确认端到端通路，
再按[第 2–3 章](#2-产生-metrics-数据)跑通 demo → Fluent Bit → Kafka → Writer → Redis + VM → API 全链路。

## 1. 链路概览

### 1.1 数据流与端口

```text
demo-leader / demo-partner（进程启动后每 ~30s 一条 metrics 日志）
  └─ acps-sdk MetricsEmitter
        │ 写入 NDJSON metrics 日志（含 resource 字段）
        ▼
  logs/amp_metrics.jsonl           (demo-leader，一个文件)
  logs/amp_metrics_*.jsonl         (demo-partner，每个 Agent 一个文件)
        │
        │ Fluent Bit tail + JSON parse（第三个 Kafka OUTPUT kafka.2）
        ▼
  Kafka  amp.metrics  (localhost:19092, Redpanda, message.timestamp.type=LogAppendTime)
        │
        │ MetricsWriter 消费（攒批 5s / 10k 样本后 Remote Write）
        │ observedAt ← Kafka LogAppendTime；或回退到 observed_timestamp 字段
        ▼
  Redis（快照缓存: amp:metrics:snapshot:{aic}）         ← 最新态（snapshots/query 优先）
  VictoriaMetrics（localhost:8428, Remote Write）       ← 时序真相源（series/rankings/slo/capacity）
        │
        │ Query API
        ▼
  POST /acps-amp-v1/metrics/snapshots/query    ← 最新快照（Redis 优先，TSDB 兜底）
  POST /acps-amp-v1/metrics/series/query       ← 时序数据（VictoriaMetrics）
  POST /acps-amp-v1/metrics/rankings/query     ← TopN 排行（VictoriaMetrics instant query）
  POST /acps-amp-v1/metrics/slo/evaluate       ← SLO 批量评估（VictoriaMetrics）
  POST /acps-amp-v1/metrics/capacity/saturation← 容量饱和度（VictoriaMetrics）
```

### 1.2 端口一览

| 组件 | 端口 | 说明 |
|------|------|------|
| acps-infra Redpanda (Kafka) | 19092 | 指标事件流（宿主机访问） |
| acps-infra Redis | 6379 | 快照缓存 + 去重 + freshness 水位 |
| acps-infra VictoriaMetrics | 8428 | Remote Write 接收 + PromQL 查询 |
| demo-leader | 9031 | Leader 业务接口（metrics 后台任务同进程启动） |
| demo-partner | 9021-9025 | 5 个 Partner Agent 实例（各自独立 metrics 后台任务） |
| monitor-server Query API | 9009 | Metrics 查询接口（5 个端点） |

### 1.3 涉及 Kafka 主题

| 主题 | 分区 | 关键配置 | 用途 |
|------|------|---------|------|
| `amp.metrics` | **1** | **`message.timestamp.type=LogAppendTime`** | 指标输入流（Writer 消费） |
| `amp.metrics.dlq` | 1 | retention 7d | 坏消息死信（时间戳缺失、JSON 损坏等） |

> `amp.metrics` 必须是 1 分区 + `LogAppendTime`，由 `acps-infra/dev-infra/dev-infra.sh`
> 幂等创建。若手动创建或重置过，务必确认（见 §5.1）。

### 1.4 与 Heartbeat 链路差异对比

| 维度 | Heartbeat | Metrics |
|------|-----------|---------|
| 发射器 | `HeartbeatEmitter`（轻量，仅 uptimeSeconds） | `MetricsEmitter`（完整负载/窗口指标 + `resource` 标签） |
| 时序存储 | 无（纯 Redis） | VictoriaMetrics（Remote Write Prometheus wire format） |
| 真相源 | Redis | VictoriaMetrics（时序）+ Redis（最新快照） |
| 写入模式 | 逐条写 | 攒批 5s 或 10k 样本后一次 Remote Write |
| observedAt 来源 | Kafka LogAppendTime | Kafka LogAppendTime（优先）；`observed_timestamp` 字段回退 |
| Query 端点数 | 4（liveness / summary / query / silence/top） | 5（snapshots / series / rankings / slo / capacity） |
| Sync 平面 | 有（sync/info + sync/snapshot） | **无** |
| VictoriaMetrics 依赖 | 非必须 | 必须（series/rankings/slo/capacity 均走 TSDB） |

## 2. 产生 Metrics 数据

### 2.1 自动产生（默认，方法 A：demo 周期发射）

demo-leader 启动后，`start_metrics()` 自动在后台**每 30s** 发射一条 metrics 日志：

```bash
cd demo-leader
just dev start        # 含 Heartbeat + Metrics 后台任务

# 查看日志确认 Metrics 后台已启动
just dev logs | grep "AMP metrics started"
# 期望: AMP metrics started (aic=..., interval=30s)
```

demo-partner 每个 Agent 同样独立发射（**代码更新后需 restart 才会启动 metrics 任务**）：

```bash
cd demo-partner
just dev restart       # 或 stop + start
just dev logs | grep "AMP metrics started"
```

### 2.2 查看已写入的本地文件

```bash
# demo-leader（单文件）
tail -1 demo-leader/logs/amp_metrics.jsonl | python3 -m json.tool
# {
#   "schema_version": "1.0",
#   "log_type": "metrics",
#   "timestamp": "2026-06-13T...",
#   "aic": "...",
#   "body": {"uptimeSeconds": 45.12, "loadMetrics": {...}, "windowMetrics": [...]},
#   "resource": {"service.name": "demo-leader", "service.namespace": "acps-demo", ...}
# }

# demo-partner（每个 Agent 一个文件）
for f in demo-partner/logs/amp_metrics_*.jsonl; do
  echo "== ${f} =="; tail -1 "${f}" | python3 -m json.tool
done
```

### 2.3 备用：手动发一条（方法 B：SDK 内联，无需 demo 在跑）

```bash
cd demo-leader
uv run python - <<'EOF'
import sys
sys.path.insert(0, '../acps-sdk')
from pathlib import Path
from acps_sdk.amp import MetricsEmitter
from acps_sdk.amp.metrics_demo import DemoMetricsSampler

aic = '1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ'
sampler = DemoMetricsSampler(aic=aic)
e = MetricsEmitter(
    Path('logs/amp_metrics.jsonl'),
    aic=aic,
    sampler=sampler,
    resource={"service.name": "demo-leader", "service.namespace": "acps-demo",
              "deployment.environment.name": "dev"},
)
log_id = e.emit_sync()
print('emitted log_id:', log_id)
EOF
```

## 3. 验证各环节

### 3.1 验证 Kafka（Fluent Bit 已转发）

**前置**：确认 `amp.metrics` 为 `LogAppendTime`（见 [dev-runbook.md §3.1](./dev-runbook.md)）。

```bash
docker exec dev-redpanda rpk topic describe amp.metrics -c | grep -i timestamp
# 期望：message.timestamp.type  LogAppendTime
```
# HIGH-WATERMARK 应随指标日志写入递增
docker exec dev-redpanda rpk topic describe amp.metrics -p
# 期望：HIGH-WATERMARK > 0 且递增中

# 消费最新一条，确认内容
HW=$(docker exec dev-redpanda rpk topic describe amp.metrics -p | awk '$1=="0"{print $6}')
docker exec dev-redpanda rpk topic consume amp.metrics --partitions=0 --offset="$((HW-1))" --num=1 \
  | python3 -c "import sys,json; d=json.load(sys.stdin); v=json.loads(d['value']); print(json.dumps(v, indent=2, ensure_ascii=False))"
# 期望：log_type == "metrics"，含 body.uptimeSeconds，含 resource 字段（service.name 等）

# DLQ 不应在本次验证期间增长（已有历史水位可接受，但不应新增）
docker exec dev-redpanda rpk topic describe amp.metrics.dlq -p
```

> 若 DLQ 水位**持续增长**，最常见根因是 `amp.metrics` 未设 `LogAppendTime`（见 §5.1）。

### 3.2 验证消费组 LAG

```bash
docker exec dev-redpanda rpk group describe monitor-server.metrics.writer.v1
# 期望：LAG 趋近 0（MetricsWriter 已消费所有消息）
```

### 3.3 验证 VictoriaMetrics（时序真相源）

```bash
# 探活
curl -s http://localhost:8428/health
# 期望：Alive

# Instant query：确认指标样本已写入（用实际 aic 替换占位）
AIC="1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ"
curl -s "http://localhost:8428/api/v1/query?query=amp_load_uptime_seconds%7Baic%3D%22${AIC}%22%7D" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'], len(d['data']['result']), 'series')"
# 期望：success N series（N >= 1）

# 浏览器可打开 vmui 交互查询
# http://localhost:8428/vmui/
```

### 3.4 验证 Redis 快照缓存

```bash
# ZSet 索引（member=aic, score=observed_at_ms）
redis-cli -n 2 zrange 'amp:metrics:snapshot:index' 0 -1 withscores | head -20

# 单 AIC Hash（最新快照字段）
AIC="1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ"
redis-cli -n 2 hgetall "amp:metrics:snapshot:${AIC}"
# 期望字段：observed_at  uptime_seconds  load_metrics_json  window_metrics_json
#            service_name  service_namespace  deployment_env

# freshness 水位（dataFreshnessAt，用于 503 保护）
redis-cli -n 2 get 'amp:metrics:data_freshness_at_ms'
# 期望：毫秒时间戳（非空）
```

### 3.5 验证 Query API（5 个端点）

以下 curl 均可独立运行（不依赖 demo），只需 infra + monitor-server 在线。
用实际 AIC 替换占位，或省略 filter 查全部。

#### snapshots/query — 最新快照（Redis 优先）

```bash
# 查询所有 Agent 最新快照（取前 5 条）
curl -s -X POST http://localhost:9009/acps-amp-v1/metrics/snapshots/query \
  -H "Content-Type: application/json" \
  -d '{"page":{"limit":5}}' | python3 -m json.tool
# 期望：items 列表，每项含 aic、observedAt、uptimeSeconds、loadMetrics、windowMetrics

# 按 AIC 精确查询
AIC="1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ"
curl -s -X POST http://localhost:9009/acps-amp-v1/metrics/snapshots/query \
  -H "Content-Type: application/json" \
  -d "{\"filter\":{\"conditions\":[{\"field\":\"aic\",\"op\":\"eq\",\"value\":\"${AIC}\"}]},\"page\":{\"limit\":1}}" \
  | python3 -m json.tool

# 按 service_name 查询（demo-leader 的 resource）
curl -s -X POST http://localhost:9009/acps-amp-v1/metrics/snapshots/query \
  -H "Content-Type: application/json" \
  -d '{"filter":{"conditions":[{"field":"service_name","op":"eq","value":"demo-leader"}]},"page":{"limit":5}}' \
  | python3 -m json.tool
```

#### series/query — 时序数据（VictoriaMetrics）

```bash
# 最近 1 小时的 uptimeSeconds 趋势（按 AIC 分组）
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
HOUR_AGO=$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)
curl -s -X POST http://localhost:9009/acps-amp-v1/metrics/series/query \
  -H "Content-Type: application/json" \
  -d "{
    \"metric\": \"uptimeSeconds\",
    \"timeRange\": {\"startAt\": \"${HOUR_AGO}\", \"endAt\": \"${NOW}\"},
    \"groupByAic\": true
  }" | python3 -m json.tool
# 期望：items 列表，每项含 metric、labels（含 aic）、points（时间戳 + 值）

# 可用 metric 名（camelCase 公共名，见 §7 对照表）：
#   uptimeSeconds  activeTasks  queuedTasks  cpuUsage  memoryUsage
#   successRate  requestTotal  requestPerSecond  p50LatencyMs  p95LatencyMs  p99LatencyMs
```

#### rankings/query — TopN 排行

```bash
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
HOUR_AGO=$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)
curl -s -X POST http://localhost:9009/acps-amp-v1/metrics/rankings/query \
  -H "Content-Type: application/json" \
  -d "{
    \"metric\": \"uptimeSeconds\",
    \"timeRange\": {\"startAt\": \"${HOUR_AGO}\", \"endAt\": \"${NOW}\"},
    \"aggregation\": \"latest\",
    \"direction\": \"desc\",
    \"topN\": 5
  }" | python3 -m json.tool
# 期望：items 列表，按 uptimeSeconds 降序，含 aic、value、evaluatedAt
```

#### slo/evaluate — SLO 批量评估

```bash
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
HOUR_AGO=$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)
curl -s -X POST http://localhost:9009/acps-amp-v1/metrics/slo/evaluate \
  -H "Content-Type: application/json" \
  -d "{
    \"timeRange\": {\"startAt\": \"${HOUR_AGO}\", \"endAt\": \"${NOW}\"},
    \"rules\": [{\"sli\": \"success_rate\", \"window\": \"PT5M\", \"target\": 99.0}]
  }" | python3 -m json.tool
# 期望：items（每个 AIC×window 评估结果，含 meets=true/false、actual、target）
#        summary（total / meetsCount / breachCount）
#
# 注意：sli 值必须 snake_case：success_rate / p95_latency_ms / p99_latency_ms / avg_latency_ms
```

#### capacity/saturation — 容量饱和度

```bash
curl -s -X POST http://localhost:9009/acps-amp-v1/metrics/capacity/saturation \
  -H "Content-Type: application/json" \
  -d '{"activeRatioThreshold": 0.8, "queueRatioThreshold": 0.8}' \
  | python3 -m json.tool
# 期望：items，含 aic、activeRatio、queueRatio、activeTasks、maxActiveTasks 等
# 注：demo 负载通常较低，阈值 0.8 时 items 为空属正常；可降至 0.1 验证端点可用性
```

## 4. 快速验证脚本

### 4.1 smoke_metrics.py（最短路径，不依赖 demo）

`scripts/smoke_metrics.py` 直连 Kafka 投递一条 metrics 消息，20s 内轮询
`snapshots/query` 确认快照可见，同时验证 freshness watermark 已推进。
仅需 **infra（kafka+redis）+ monitor-server** 在线，跳过 Fluent Bit 与 demo。

```bash
cd monitor-server
APP_ENV=development uv run python scripts/smoke_metrics.py
```

成功输出：

```text
=== Metrics Dev Smoke ===
[INFO] monitor: http://localhost:9009, topic: amp.metrics, aic: urn:test:metrics:e2e
[OK] 已投递 metrics 消息: aic=urn:test:metrics:e2e, log_id=...
[OK] 快照可见: aic=urn:test:metrics:e2e, uptimeSeconds=42.0
[OK] freshness watermark 已推进: ... ms

[PASS] Metrics 消息→Redis快照→Query API 通路验证通过！
```

### 4.2 demo_metrics.sh（全链路，需 demo 在线）

`scripts/demo_metrics.sh` 假设所有服务已启动且 demo 正在周期发 metrics 日志，
等待 40s 传播后断言 `snapshots/query` 有数据，并检查 DLQ 与 VictoriaMetrics。

```bash
# 推荐：bash monitor-server/scripts/demo_metrics.sh（任意当前目录均可，脚本自动解析 acps/ 根）
bash monitor-server/scripts/demo_metrics.sh
```

成功输出（等待约 40s）：

```text
=== AMP Metrics Dev Demo ===
[1] 服务健康检查...
    ✓ monitor-server status=ok, victoria_metrics=ok
[2] 等待 40s...
[3] 本地 metrics 文件检查...
    ✓ demo-leader metrics 文件存在 (...条记录)
    ✓ demo-partner metrics 文件存在 (5 个 Agent)
[4] Query API: snapshots/query (无 filter，取前 10 条)...
    返回 items=6, total=6
...
✓ PASS: snapshots/query 返回 6 条（total=6），Metrics 通路验证通过！
```

## 5. 故障排查

### 5.1 DLQ 持续增长（最常见）

**现象**：`amp.metrics.dlq` 水位在本次验证期间持续增长；monitor-server 日志含
`UntimedMetricsError`（aic=..., log_id=...）。

**根因**：`amp.metrics` topic 不是 `LogAppendTime`，且消息体未携带 `observed_timestamp` 字段。
Writer 取 `observedAt` 的优先级：Kafka LogAppendTime（`timestamp_type=1`）→ `observed_timestamp` 字段 → DLQ。
Fluent Bit 直接转发文件内容，不会注入 `observed_timestamp`，因此 **必须** 靠 LogAppendTime。

**修复**：

```bash
# 确认 topic 配置
docker exec dev-redpanda rpk topic describe amp.metrics -c | grep -i timestamp
# 期望：message.timestamp.type  LogAppendTime

# 若非 LogAppendTime，修改或重建：
docker exec dev-redpanda rpk topic alter-config amp.metrics --set message.timestamp.type=LogAppendTime
# 或由 dev-infra 按规范重建（会清空数据）：
just -f monitor-server/Justfile infra reset kafka
```

### 5.2 VictoriaMetrics 不可达（series/rankings/slo/capacity 返回 503 或 degraded）

**现象**：`/health` 返回 `"victoria_metrics":"degraded"`；series 类端点报 503 或超时。
snapshots/query（Redis 路径）**仍可用**，不受影响。

**修复**：

```bash
# 确认容器状态
just -f monitor-server/Justfile infra status
# dev-victoria-metrics 应显示 running + healthy

# 若 unhealthy，重启：
docker compose -f acps-infra/dev-infra/compose.yml --profile victoria-metrics restart dev-victoria-metrics
# 或：
just -f monitor-server/Justfile infra up victoria-metrics
```

### 5.3 Redis 未启动 / snapshots/query 返回 503

**现象**：`snapshots/query` 返回 `503 AMP_READ_MODEL_LAGGING`；Redis 不可达。

```bash
redis-cli -n 2 ping        # 期望 PONG；若超时则 Redis 未启动
just -f monitor-server/Justfile infra up redis
just -f monitor-server/Justfile infra wait redis
```

> `503 AMP_READ_MODEL_LAGGING` 也可能在 monitor-server 刚启动时出现（freshness watermark 尚未建立），
> 等待第一批消息被 MetricsWriter 处理后自动恢复（通常 5s 内）。

### 5.4 snapshots/query 返回空但 series/query 有数据

**现象**：`snapshots/query` 返回 `items=[]`，但 VictoriaMetrics 已有样本（series 有数据）。

**根因**：Redis 快照 TTL 过期，或 Writer 批量处理尚未完成（snapshots 写入在 Remote Write 成功后）。

**排查**：

```bash
# 确认 Redis 索引有条目
redis-cli -n 2 zcard 'amp:metrics:snapshot:index'
# 若为 0 则快照已过期或从未写入

# 查看 monitor-server 日志确认 Remote Write 成功
just -f monitor-server/Justfile app logs | grep -E "remote_write|snapshot_cache"
```

### 5.5 Fluent Bit 启动后 metrics 无进 Kafka

**现象**：`amp.metrics` 水位不增长，但 amp.audit / amp.heartbeat 正常。

**排查**：

1. 确认 Fluent Bit 标准输出有 `[output:kafka:kafka.2] worker #0 started`（第三个 OUTPUT）。
2. 检查 `fluent-bit.conf` 是否含 metrics INPUT/OUTPUT 段（`tag=amp.metrics`）。
3. 确认 `demo-leader/logs/amp_metrics.jsonl` 文件存在且在增长：
   ```bash
   ls -la demo-leader/logs/amp_metrics.jsonl
   wc -l demo-leader/logs/amp_metrics.jsonl
   ```
4. 若 conf 中路径不存在（绝对路径硬编码问题），Fluent Bit 会跳过该 INPUT 但不报错：
   确认 `config/fluent-bit/fluent-bit.conf` 中的路径与本机实际路径一致。

参见 [dev-runbook.md §5](./dev-runbook.md) 的通用 Fluent Bit 排查。

## 6. 与自动化测试的关系

| 层次 | 入口 | 覆盖内容 | 是否需要 demo |
|------|------|----------|--------------|
| 手动全链路 | 本文 §3 | Fluent Bit + 真实 demo 周期发射 + 全部 5 端点 | 是 |
| 快速通路脚本 | `scripts/smoke_metrics.py` | Kafka → Writer → Redis → snapshots/query | 否 |
| 全链路一键 | `scripts/demo_metrics.sh` | 全服务在线，断言 snapshots/query 有数据 | 是 |
| pytest e2e | `just test e2e -k metrics`（`tests/e2e/test_metrics_*`） | 黑盒：ingest / query / retention，9 条用例 | 否 |
| pytest integration | `just test integration -k metrics` | ASGI + respx mock，隔离验证每个端点 | 否 |
| pytest unit | `just test unit -k metrics` | 纯函数：series 映射 / freshness / cursor / encode | 否 |

> 自动化 E2E 用例（`tests/e2e/`）与本手册互补：pytest 覆盖"协议正确性"，本手册覆盖
> "多项目 + 真实 Fluent Bit + demo 周期发射"的全链路集成场景。

## 7. 指标命名对照

Query API 接受 camelCase **公共名**；内部 Prometheus series 名不对外暴露。

| 公共名（API 入参）| 内部 Prometheus 名 | 说明 |
|---------------------|--------------------------|------|
| `uptimeSeconds` | `amp_load_uptime_seconds` | 系统运行时长（秒） |
| `activeTasks` | `amp_load_active_tasks` | 当前执行中任务数 |
| `queuedTasks` | `amp_load_queued_tasks` | 队列中等待任务数 |
| `maxActiveTasks` | `amp_load_max_active_tasks` | 最大并发任务数 |
| `maxQueuedTasks` | `amp_load_max_queued_tasks` | 最大队列容量 |
| `cpuUsage` | `amp_load_cpu_usage` | CPU 使用率（0–100%） |
| `memoryUsage` | `amp_load_memory_usage` | 内存使用率（0–100%） |
| `diskUsage` | `amp_load_disk_usage` | 磁盘使用率（0–100%） |
| `successRate` | `amp_window_success_rate` | 滑动窗口成功率（%） |
| `requestTotal` | `amp_window_request_total` | 窗口内总请求数 |
| `requestPerSecond` | `amp_window_request_per_second` | 请求每秒 |
| `avgLatencyMs` | `amp_window_avg_latency_ms` | 平均时延（毫秒） |
| `p50LatencyMs` | `amp_window_latency_ms` (quantile=p50) | P50 时延（毫秒） |
| `p95LatencyMs` | `amp_window_latency_ms` (quantile=p95) | P95 时延（毫秒） |
| `p99LatencyMs` | `amp_window_latency_ms` (quantile=p99) | P99 时延（毫秒） |
