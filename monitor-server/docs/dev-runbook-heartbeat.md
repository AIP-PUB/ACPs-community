# 开发模式 Heartbeat 联合验证

**前置**：请先按 [dev-runbook.md](./dev-runbook.md) 完成服务启动（monitor-server、demo-leader、
demo-partner、Fluent Bit）。本文只讲 Heartbeat 链路在开发模式下的验证内容。

与 Audit 链路（见 [`dev-runbook-audit.md`](./dev-runbook-audit.md)）相比，Heartbeat 有三点根本差异：

- **触发方式**：心跳是**进程在线即周期发射**的后台事件，**无需调用业务 API**（Audit 由业务动作触发）。
- **真相源**：心跳当前态存在 **Redis**（无 PostgreSQL、无哈希链、无签名验证）。
- **同步路**：除 Query API 外，额外提供 **Sync API**（全量 NDJSON 快照 + `alive-delta` Kafka 增量流）。

建议首次阅读先用[第 4 章快速脚本](#4-快速验证脚本)确认服务端通路，
再按[第 2–3 章](#2-产生心跳数据)跑通 demo → Fluent Bit → Kafka → Redis → API 全链路。

## 1. 链路概览

### 1.1 数据流与端口

```text
demo-leader / demo-partner（进程启动后每 ~15s 一条心跳）
  └─ acps-sdk HeartbeatEmitter
        │ 写入 NDJSON 心跳日志
        ▼
  logs/amp_heartbeat.jsonl          (demo-leader，一个文件)
  logs/amp_heartbeat_*.jsonl        (demo-partner，每个 Agent 一个文件)
        │
        │ Fluent Bit tail + JSON parse
        ▼
  Kafka  amp.heartbeat  (localhost:19092, Redpanda, message.timestamp.type=LogAppendTime)
        │
        │ monitor-server HeartbeatWriter 消费
        │ observed_at_ms ← Kafka LogAppendTime；hb_apply_heartbeat 原子写入
        ▼
  Redis  (liveness 真相源)  localhost:6379
        │   amp:hb:{hb-000}:latest:<aic>      (Hash：最后心跳)
        │   amp:hb:{hb-000}:liveness_zset     (ZSet：score=last_seen_at_ms)
        │   amp:hb:{hb-000}:delta_outbox      (Stream：alive-delta 待发)
        │
        ├─ Query API ───────────────► http://localhost:9009/acps-amp-v1/heartbeat/liveness|summary
        │
        └─ HeartbeatRelay ──► Kafka amp.heartbeat.alive-delta
                              Sync API ─► http://localhost:9009/acps-amp-v1/heartbeat/sync/info|snapshot
```

涉及组件与端口：

| 组件 | 端口 | 说明 |
|------|------|------|
| acps-infra Redpanda (Kafka) | 19092 | 心跳事件流（宿主机访问） |
| acps-infra Redis | 6379 | Heartbeat liveness 真相源 |
| acps-infra PostgreSQL | 5432 | 仅 monitor-server 启动/健康检查需要（心跳本身不入 PG） |
| demo-leader API | 9031 | Leader 业务接口（健康检查；心跳与业务解耦） |
| demo-partner | 9021-9025 | 5 个 Partner Agent 实例（各自周期心跳） |
| monitor-server Query/Sync API | 9009 | 心跳查询与同步接口 |

涉及 Kafka 主题：

| 主题 | 分区 | 关键配置 | 用途 |
|------|------|---------|------|
| `amp.heartbeat` | **1** | **`message.timestamp.type=LogAppendTime`** | 心跳输入流（Writer 消费） |
| `amp.heartbeat.alive-delta` | 1 | retention 7d | alive 集合增量流（Relay 产出） |
| `amp.heartbeat.dlq` | 1 | retention 7d | 坏消息死信 |

> `amp.heartbeat` 必须是 1 分区 + `LogAppendTime`，这两项由 `acps-infra/dev-infra/dev-infra.sh`
> 幂等创建。若手动创建或重置过，务必确认（见 §5.1）。

### 1.2 与 Audit 链路的关键差异

| 维度 | Audit | Heartbeat |
|------|-------|-----------|
| 发射器 | `AuditEmitter`（业务埋点触发） | `HeartbeatEmitter`（进程内周期任务） |
| 是否签名 | 是（`integrity` 必带，验签） | 否（`integrity` 省略，不验签） |
| 事件时间来源 | 记录自带 `timestamp` | **Kafka LogAppendTime** |
| 存储 | PostgreSQL `audit_records` | Redis（liveness Hash + ZSet + outbox Stream） |
| 查询端点 | `/acps-amp-v1/audit/records/query` 等 | `/acps-amp-v1/heartbeat/liveness\|summary` + `/heartbeat/sync/*` |
| 依赖服务 | PostgreSQL + (CA 模式) ca-server | Redis |

## 2. 产生心跳数据

### 2.1 自动产生（默认）

demo-leader / demo-partner 启动后，各进程的后台任务会**每 ~15s** 自动写一条心跳到本地文件，
无需任何业务请求。可调 `AMP_HEARTBEAT_INTERVAL_SECONDS` 覆盖间隔（启动前 export）：

```bash
AMP_HEARTBEAT_INTERVAL_SECONDS=10 just -f demo-leader/Justfile app start
```

### 2.2 查看已写入的本地文件

```bash
tail -1 demo-leader/logs/amp_heartbeat.jsonl | python3 -m json.tool
# {
#   "schema_version": "1.0",
#   "log_type": "heartbeat",
#   "timestamp": "2026-06-12T14:00:00.123456+00:00",
#   "aic": "1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ",
#   "body": {"uptimeSeconds": 45.12}
# }

for f in demo-partner/logs/amp_heartbeat_*.jsonl; do
  echo "== ${f} =="; tail -1 "${f}" | python3 -m json.tool
done
```

### 2.3 备用：手动发一条（SDK 内联，无需 demo 在跑）

```bash
cd demo-leader
uv run python - <<'EOF'
import sys; sys.path.insert(0, '../acps-sdk')
from pathlib import Path
from acps_sdk.amp import HeartbeatEmitter
aic = '1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ'
e = HeartbeatEmitter(Path('logs/amp_heartbeat.jsonl'), aic=aic)
print('emitted log_id:', e.emit_sync())
EOF
```

## 3. 验证各环节

### 3.1 验证 Kafka（Fluent Bit 已转发）

```bash
docker exec dev-redpanda rpk topic describe amp.heartbeat -p
# 期望：HIGH-WATERMARK 随心跳递增

# 消费最新一条确认内容
HW=$(docker exec dev-redpanda rpk topic describe amp.heartbeat -p | awk '$1=="0"{print $6}')
docker exec dev-redpanda rpk topic consume amp.heartbeat --partitions=0 --offset="$((HW-1))" --num=1 \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(json.loads(d['value']), indent=2, ensure_ascii=False))"
# 期望：log_type == "heartbeat"，aic 正确

# DLQ 不应增长（若增长说明 topic 非 LogAppendTime，见 §5.1）
docker exec dev-redpanda rpk topic describe amp.heartbeat.dlq -p
```

### 3.2 验证 monitor-server 已消费

```bash
docker exec dev-redpanda rpk group describe monitor-server.heartbeat.writer.v1
# 期望：LAG 趋近 0
```

### 3.3 验证 Redis 当前态

`heartbeat_shard_count=1`，故 shard 恒为 `hb-000`。

```bash
# 列出心跳相关键
redis-cli -n 2 --scan --pattern 'amp:hb:*' | sort | head

# liveness ZSet：member=aic，score=last_seen_at_ms
redis-cli -n 2 zrange 'amp:hb:{hb-000}:liveness_zset' 0 -1 withscores

# 单 AIC 最新心跳 Hash
AIC=1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ
redis-cli -n 2 hgetall "amp:hb:{hb-000}:latest:${AIC}"
```

### 3.4 验证 Query API

```bash
AIC=1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ

# 点查单 AIC
curl -s "http://localhost:9009/acps-amp-v1/heartbeat/liveness/${AIC}" | python3 -m json.tool
# 期望 data.isAlive=true、data.livenessState="alive"、data.silenceDurationSeconds 较小

# 全局汇总
curl -s "http://localhost:9009/acps-amp-v1/heartbeat/summary" | python3 -m json.tool
# 期望 data.aliveCount >= 在线 Agent 数；data.silenceBuckets 分桶

# 批量查询
curl -s -X POST "http://localhost:9009/acps-amp-v1/heartbeat/liveness/query" \
  -H 'Content-Type: application/json' \
  -d "{\"filter\":{\"conditions\":[{\"field\":\"aic\",\"op\":\"in\",\"value\":[\"${AIC}\"]}]},\"page\":{\"limit\":50}}" \
  | python3 -m json.tool

# 静默排行（analyticsEnabled=true 时可用）
curl -s -X POST "http://localhost:9009/acps-amp-v1/heartbeat/silence/top" \
  -H 'Content-Type: application/json' \
  -d '{"topN":10,"onlySilent":false}' | python3 -m json.tool
```

> 响应 `meta.dataFreshnessAt` 表示读模型已稳定处理到的事件时间水位；`meta.evaluatedAt`
> 为本次评估时间，`meta.silenceThresholdSeconds`/`evictAfterSeconds` 回显阈值。

### 3.5 验证 Sync API

```bash
# Sync Profile 元信息
curl -s "http://localhost:9009/acps-amp-v1/heartbeat/sync/info" | python3 -m json.tool
# 期望：type="amp-alive-delta"，kafkaTopic="amp.heartbeat.alive-delta"，
#       shardCount=1，currentPublishedSeqByShard={"hb-000":"<n>"}

# 全量快照（NDJSON：首行 snapshot-meta，后续 upsert 行）
curl -s "http://localhost:9009/acps-amp-v1/heartbeat/sync/snapshot"
# 首行 {"recordType":"snapshot-meta","type":"amp-alive-delta","cutoverSeqByShard":{...},...}
# 后续 {"shard":"hb-000","seq":"..","op":"upsert","id":"urn:amp:alive:<aic>",...}

# alive-delta 增量流：消费 Relay 产出的信封
docker exec dev-redpanda rpk topic consume amp.heartbeat.alive-delta --num 5 --offset start --format '%v\n' \
  | python3 -c "import sys,json
for line in sys.stdin:
    line=line.strip()
    if line:
        v=json.loads(line); print(v['kind'], v['op'], v['id'])"
# 期望：enter_alive / refresh_alive upsert urn:amp:alive:<aic>
```

> 确认 Sync API 正常后，可继续接入 discovery-server 作为 Consumer：
> 见 [`dev-runbook-heartbeat-discovery-consumer.md`](./dev-runbook-heartbeat-discovery-consumer.md)。

## 4. 生命周期验证（可选：alive → silent）

停掉一个 Agent，等待超过静默阈值（`silence_threshold_seconds=90`），确认其被判 silent。

```bash
AIC=1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ
just -f demo-leader/Justfile app stop      # 停止 leader，心跳停发
sleep 95                                    # 等待 > 90s 静默阈值 + Reconciler 扫描

curl -s "http://localhost:9009/acps-amp-v1/heartbeat/liveness/${AIC}" | python3 -m json.tool
# 期望：data.isAlive=false、data.livenessState="silent"、silenceDurationSeconds >= 90
```

如需在增量流上确认，对应的 `alive-delta` 会追加一条
`kind="leave_alive"`、`op="delete"` 的信封——用 §3.5 的消费命令从头查看，
序列末尾即为该 AIC 的 `leave_alive`。

恢复：`just -f demo-leader/Justfile app start`，等待一个心跳周期后再次查询应回到 `alive`。

## 5. 快速验证脚本

### 5.1 快速通路验证（跳过 Fluent Bit / demo）

`scripts/smoke_heartbeat.py` 直连 Kafka 投递一条心跳并轮询 Query API 确认 alive，
仅需 **infra（kafka+redis）+ monitor-server** 在线，最短路径验证"消息→Redis→API"。

```bash
cd monitor-server
APP_ENV=development uv run python scripts/smoke_heartbeat.py
# [OK] 已投递心跳: aic=urn:test:heartbeat:e2e, partition=0
# [OK] liveness: isAlive=True, livenessState=alive
# [PASS] Heartbeat 消息→Redis→Query API 通路验证通过！
```

### 5.2 全链路一键脚本

`scripts/demo_heartbeat.sh` 假设所有服务已启动且 demo 正在周期发心跳，
等待心跳自然传播后断言 liveness。

```bash
# 从 acps/ 根目录执行（推荐）；脚本会自动解析仓库根路径，任意当前目录均可
bash monitor-server/scripts/demo_heartbeat.sh
# 期望以 ✓ PASS 结束，aliveCount >= 1 且 leader isAlive=true
```

## 6. 故障排查

### 6.1 心跳全部进入 DLQ（最常见）

**现象**：`amp.heartbeat.dlq` 水位持续增长，Query API 查不到 alive；monitor 日志出现
`消息缺少时间戳，跳过重试直接写入 DLQ`（`UntimedHeartbeatError`）。

**根因**：`amp.heartbeat` 不是 `LogAppendTime`。心跳线体**不含** `observedTimestamp`，
Writer 取 observed_at 的优先级是 LogAppendTime → observedTimestamp → DLQ；topic 若是默认
`CreateTime`，则消费侧 `timestamp_type=0`，两路皆失败 → DLQ。

**修复**：

```bash
docker exec dev-redpanda rpk topic describe amp.heartbeat -c | grep -i timestamp
docker exec dev-redpanda rpk topic alter-config amp.heartbeat --set message.timestamp.type=LogAppendTime
# 或由 dev-infra 按规范重建：
just -f monitor-server/Justfile infra reset kafka
```

### 6.2 Writer 拒绝启动：分区数不符（C-CONF-1）

**现象**：monitor 启动报 `heartbeat_input_partition_count=1 与 topic 'amp.heartbeat' 实际分区数 N 不一致`。

**修复**：删除并按 1 分区重建：

```bash
docker exec dev-redpanda rpk topic delete amp.heartbeat
just -f monitor-server/Justfile infra reset kafka
```

### 6.3 liveness 一直 silent / 查不到

1. 确认本地文件在增长（§2.2）；不增长说明 demo 心跳任务未启动：
   ```bash
   just -f demo-leader/Justfile app logs | grep -i heartbeat
   ```
2. 确认 emit 间隔 < 90s（默认 15s）。
3. 确认 Fluent Bit 在转发（§3.1）且 DLQ 不增长（§6.1）。
4. 确认 Writer 在消费（§3.2，consumer group lag → 0）。

### 6.4 Redis 未启动 / Query 返回 503

```bash
redis-cli -n 2 ping                           # 期望 PONG
just -f monitor-server/Justfile infra up redis
just -f monitor-server/Justfile infra status      # redis healthy
```

Query API 在 Redis 连接异常时按设计返回 `503`，恢复 Redis 后即正常。

### 6.5 Fluent Bit 启动后心跳无进 Kafka

参见 [dev-runbook.md §5](./dev-runbook.md) 的通用排查，以及 Heartbeat 专项：

1. **macOS 上使用了 `-d` daemon 模式**：改为前台运行（见 `dev-runbook.md §3.5`）。
2. 检查 heartbeat OUTPUT worker：标准输出应出现 `[output:kafka:kafka.1] worker #0 started`。
3. 确认配置含 heartbeat 段（INPUT tag=`amp.heartbeat` + OUTPUT match=`amp.heartbeat` + `Workers 1`）。
4. 确认心跳文件路径正确（绝对路径）：`ls demo-leader/logs/amp_heartbeat.jsonl`。

### 6.6 `/sync/*` 返回 404

`sync_enabled=false` 时 `/sync/info` 与 `/sync/snapshot` 返回 404（预期行为）。
开发默认 `sync_enabled=true`；如被关闭，在 `config/default.toml` 的 `[heartbeat]`
设 `sync_enabled = true` 后重启 monitor-server。

### 6.7 `/silence/top` 返回 404

`analytics_enabled=false` 时该端点**不注册**（404 是路由不存在，非业务错误）。
开发默认 `analytics_enabled=true`。

## 7. 与自动化测试的关系

| 层次 | 入口 | 说明 |
|------|------|------|
| 手动联合验证 | 本文 | 多项目 + Fluent Bit + 真实 demo 周期心跳 |
| 快速通路脚本 | `scripts/smoke_heartbeat.py` | 直连 Kafka，跳过 Fluent Bit/demo |
| 全链路一键 | `scripts/demo_heartbeat.sh` | 全服务在线，断言 liveness |
| pytest e2e | `just test e2e`（`tests/e2e/test_heartbeat_*`） | 黑盒：投递→liveness/summary/query/sync，需 `just test bootstrap` |

> 代码落点与实现设计见 [`plans/heartbeat-e2e-demo-plan.md`](../plans/heartbeat-e2e-demo-plan.md)。

> **discovery-server 接入**：若需验证 discovery-server 作为 Heartbeat Sync API Consumer
> 的完整链路（bootstrap → aliveMap 注入），参见
> [`dev-runbook-heartbeat-discovery-consumer.md`](./dev-runbook-heartbeat-discovery-consumer.md)。
