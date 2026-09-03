# 开发模式 Message 联合验证

**前置**：请先按 [dev-runbook.md](./dev-runbook.md) 完成服务启动（monitor-server、demo-leader、
demo-partner、Fluent Bit、ClickHouse、RabbitMQ）。**群组模式还须 mq-auth-server**（RabbitMQ EXTERNAL 认证与群组 ACL）：

```bash
cd mq-auth-server
just dev bootstrap && just dev restart
# Group API :9007 要求 mTLS 客户端证书；开发环境用官方探针（默认加载 certs/client.pem 三件套）
APP_ENV=development uv run python -m app.core.health_probe
# 期望：退出码 0（HTTP 200）。勿用 curl -sk（无客户端证书会握手失败；macOS LibreSSL 即带 --cert 也可能不支持 dev 私钥格式）
# dev-infra Redis 映射宿主机 6379；mq-auth 使用 DB 0（REDIS_URL=redis://localhost:6379/0）
```

demo-leader 须启用**群组模式**。本文只讲 Message 链路在开发模式下的验证内容。

**开始验证前的快速检查**（任一不满足则先修复再继续）：

```bash
# 1. amp.message 必须为 LogAppendTime，分区数 ≥ 4
docker exec dev-redpanda rpk topic describe amp.message -c | grep -E 'timestamp.type|partition'
docker exec dev-redpanda rpk topic describe amp.message -p
# 期望：LogAppendTime；PARTITION 行数 ≥ 4（仅 1 分区时执行 dev-runbook.md §3.1 的 just infra up kafka）

# 2. Fluent Bit 必须含 kafka.4 worker（message OUTPUT）
# 启动/重启后标准输出应出现：[output:kafka:kafka.4] worker #0 started

# 3. ClickHouse 可达
curl -s http://localhost:8123/ping
# 期望：Ok.

# 4. demo-leader 群组模式已启用，且 demo 代码或 acps-sdk 更新后须 restart demo-leader / demo-partner

# 5. Message Writer 消费组须稳定（联合验证期间避免 Uvicorn 热重载打断后台任务）
#    config/development.toml [server] reload = false 后 just dev restart；
#    或确认 Justfile 已对 logs/* 做 reload-exclude（默认已配置）
docker exec dev-redpanda rpk group describe monitor-server.message.writer.v1
# 期望：STATE Stable，MEMBERS ≥ 1
```

与 Access / Metrics 链路相比，Message 有三点根本差异：

- **事件驱动 + 仅群组模式**：必须触发群组任务（leader `/api/v1/submit` + `mode=group`）才有日志；直连 RPC 模式不产生 message 日志（属 Access 覆盖）。
- **一次消息 = 多条独立日志**：send / receive / ack（及异常 nack）各自一条 NDJSON；lifecycle 按 `messageId` 归并。
- **真相源 ClickHouse**：6 个 Query 端点；`destination.name` 统一取群组 FANOUT exchange（queue 记入 `subscriptionName`）。

建议首次阅读先用[第 4 章快速脚本](#4-快速验证脚本)确认端到端通路，
再按[第 2–3 章](#2-产生消息数据)跑通 demo → Fluent Bit → Kafka → Writer → ClickHouse → API 全链路。

## 1. 链路概览

### 1.1 数据流与端口

```text
用户 → demo-leader /api/v1/submit（mode=group，不发射 HTTP 入站日志）
  → GroupLeaderMqClient publish（send → logs/amp_message.jsonl）
  → RabbitMQ FANOUT exchange
  → GroupPartnerMqClient consume（receive/ack → logs/amp_message_*.jsonl）
  → Fluent Bit kafka.4 → Kafka amp.message
  → MessageWriter → ClickHouse + Redis
  → Query API（6 端点）
```

| 组件 | 端口 | 说明 |
|------|------|------|
| RabbitMQ | 5671 / 15672 | 群组 broker |
| ClickHouse | 8123 / 19010 | Message 真相源 |
| Kafka amp.message | 19092 | ≥4 分区，LogAppendTime |
| Redis | 6379 | 去重 + 读模型水位 |
| demo-leader | 9031 | leader send 消息日志 |
| demo-partner | 9021-9025 | partner receive/ack 日志（每 Agent 一文件） |
| monitor-server | 9009 | Message Query API |

### 1.3 涉及 Kafka 主题

| 主题 | 分区 | 关键配置 | 用途 |
|------|------|---------|------|
| `amp.message` | **≥4** | **`message.timestamp.type=LogAppendTime`** | 消息输入流（Writer 消费） |
| `amp.message.dlq` | 1 | retention 7d | 坏消息死信 |

### 1.4 与 Access 链路差异

| 维度 | Access | Message |
|------|--------|---------|
| 触发方式 | 每次 RPC 交互 | **仅群组模式** RabbitMQ 广播 |
| 日志条数/消息 | 1 次 RPC = 2 条（caller+callee） | 1 次 task-command = 1 send + N receive + N ack |
| 追踪 | traceparent HTTP 传播 | traceparent AMQP header + O-M6 ContextVar |
| Query 端点 | 7 | 6（events/lifecycles/deadletters/destinations/throughput） |

## 2. 产生消息数据

### 2.1 触发群组任务（推荐）

在 **`acps/` 仓库根目录** 执行：

```bash
curl -s -X POST http://localhost:9031/api/v1/submit \
  -H "Content-Type: application/json" \
  -d '{"query":"我要在北京订三晚酒店，预算每晚500元，2人入住","clientRequestId":"message-dev-1","mode":"group"}'
```

提交后等待约 30–240s（LLM 规划 + Partner 群组处理；`e2e_message_demo.sh` 轮询上限 240s），再执行 §2.2 / §3。

### 2.2 查看本地文件

```bash
tail -1 demo-leader/logs/amp_message.jsonl | python3 -m json.tool
# partner 在成功 join 群组并消费 exchange 后才会创建对应文件，例如：
tail -1 demo-partner/logs/amp_message_china_hotel.jsonl | python3 -m json.tool
ls demo-partner/logs/amp_message_*.jsonl
```

期望：`log_type` 为 `message`，含 `eventType`（send/receive/ack）、`messageId`、`destination`、`trace_id`。若仅有 leader 侧 `amp_message.jsonl`，请确认 mq-auth-server 与群组任务是否成功建群（见 §5 排查）。

### 2.3 备用：直连 Kafka 投一组（无需 demo）

见 [§4.1](#41-e2e_message_verifypy)。

## 3. 分环节验证

### 3.1 Kafka

```bash
docker exec dev-redpanda rpk topic describe amp.message -p
# 从已有水位读取（-o end 在无新消息时会阻塞，可改用 -p/-o）：
docker exec dev-redpanda rpk topic consume amp.message -p 1 -o 0 --num 3 | python3 -m json.tool
docker exec dev-redpanda rpk topic describe amp.message.dlq -p
```

期望：`amp.message` 水位随群组任务递增；消费到的记录 `log_type=="message"`；DLQ 不增长。

### 3.2 消费组

```bash
docker exec dev-redpanda rpk group describe monitor-server.message.writer.v1
```

期望：LAG 趋近 0。

### 3.3 ClickHouse

```bash
docker exec dev-clickhouse clickhouse-client -q \
  "SELECT event_type, count() FROM amp.message_events GROUP BY event_type ORDER BY count() DESC"
```

期望：send/receive/ack 行数随任务增长；Compactor 周期重算 `message_lifecycle`。

### 3.4 Redis

```bash
redis-cli -n 2 --scan --pattern 'amp:message:*' | head
```

期望：去重键与读模型水位键存在。

### 3.5 验证 Query API（6 端点）

```bash
MID=<从 amp.message 取一条的 messageId>
START=$(date -u -v-15M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '15 minutes ago' +%Y-%m-%dT%H:%M:%SZ)
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
BASE=http://localhost:9009/acps-amp-v1/message

# events：原始消息事件
curl -s -X POST "$BASE/events/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"filter\":{\"conditions\":[{\"field\":\"system\",\"op\":\"eq\",\"value\":\"rabbitmq\"}]},\"page\":{\"limit\":20}}" | python3 -m json.tool

# lifecycles：按 messageId 归并
curl -s -X POST "$BASE/lifecycles/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"filter\":{\"conditions\":[{\"field\":\"messageId\",\"op\":\"eq\",\"value\":\"$MID\"}]}}" | python3 -m json.tool

# lifecycles/{messageId}：单消息生命周期
curl -s "$BASE/lifecycles/$MID" | python3 -m json.tool

# deadletters：死信消息（须带 lifecycle 键过滤，如 traceId）
curl -s -X POST "$BASE/deadletters/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"filter\":{\"conditions\":[{\"field\":\"traceId\",\"op\":\"eq\",\"value\":\"<trace_id>\"}]},\"page\":{\"limit\":10}}" | python3 -m json.tool

# destinations：目的地点时状态（demo 默认 Null source → 503 STATE_SNAPSHOT_UNAVAILABLE，属预期）
curl -s -X POST "$BASE/destinations/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"}}" | python3 -m json.tool

# destinations/throughput：群组 exchange 的 produced/consumed 趋势（destinationName + system 为顶层字段）
curl -s -X POST "$BASE/destinations/throughput" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"destinationName\":\"<群组exchange>\",\"system\":\"rabbitmq\",\"step\":\"PT5M\"}" | python3 -m json.tool
```

> `lifecycles`/`deadletters` 需 `reliability_enabled=true`；`destinations/throughput` 需 `destination_enabled=true`（`config/development.toml` `[message]` 段）。
> `lifecycles/query` 与 `deadletters/query` 须带选择性过滤（`messageId` / `traceId` / `correlationId`，或 `system` + `destination.name` + `timeRange`）。

## 4. 快速验证脚本

### 4.1 `e2e_message_verify.py`

仅需 infra + ClickHouse + monitor-server（不依赖 demo / Fluent Bit）：

```bash
cd monitor-server
APP_ENV=development uv run python scripts/e2e_message_verify.py
```

### 4.2 `e2e_message_demo.sh`

全服务在线 + 群组模式：

```bash
# 推荐：bash monitor-server/scripts/e2e_message_demo.sh（任意当前目录均可，仅调 HTTP API）
bash monitor-server/scripts/e2e_message_demo.sh
```

## 5. 故障排查

| 现象 | 根因 | 处置 |
| --- | --- | --- |
| 消息日志全进 `amp.message.dlq` | `amp.message` 非 LogAppendTime 且发射未带 `observedTimestamp` | `rpk topic alter-config amp.message --set message.timestamp.type=LogAppendTime` 或 `just infra reset kafka` |
| **完全没有消息日志** | Message **仅群组模式产生**；直连模式或无群组任务 | 以 `mode=group` 发 `/submit`；确认 RabbitMQ 可达、partner 已加群 |
| `lifecycles/{messageId}` 只有 send 没有 receive/ack | send/receive 的 `destination`/`messageId` 不一致 | 确认 receive 的 `destination.name`==exchange（非 queue） |
| `events` 有数据但 trace 串不起来 | leader/partner 未注入 emitter 或 AMQP `traceparent` 未透传 | 确认 `create_group_manager` 注入 `message_emitter`；partner `GroupPartnerMqClient` 注入 emitter |
| `receiveCount` ≈ N×`sendCount`（N=群组成员数）| **正确**：FANOUT 投递到全部 partner 队列 | 无需处理 |
| `receiveCount` 异常翻倍 | 自身发送未过滤或他人 task-result 误发 receive | 确认 O-M2 过滤规则 |
| Query 端点 503 | ClickHouse 不可达 / Compactor 水位滞后 | `curl :8123/ping`；确认 Writer + Compactor 运行 |
| `lifecycles`/`deadletters`/`throughput` 返回 404 | Profile 未启用 | `[message]` 置 `reliability_enabled=true`/`destination_enabled=true` 后重启 |
| `destinations/query` 恒 503 `STATE_SNAPSHOT_UNAVAILABLE` | demo 默认 `NullDestinationStateSource` | **属预期** |
| Fluent Bit 起了但 message 不进 Kafka | macOS 缺 `Workers 1` / 路径错 / 缺 kafka.4 | 确认 `kafka.4 worker #0 started`；核对 `amp_message*.jsonl` 绝对路径 |
| `monitor-server.message.writer.v1` STATE Dead / LAG 不降 | Uvicorn `--reload` 因写 `logs/` 反复重启 lifespan | `just dev restart`；或 `[server] reload=false`；确认 Justfile 含 `--reload-exclude 'logs/*'` |
| 群组 `/submit` 后无 `amp_message*.jsonl` | mq-auth-server 未启动或 Redis 端口错误 | 启动 `mq-auth-server`；`REDIS_URL=redis://localhost:6379/0`（**勿用旧版 16379**）；`just dev restart` demo-leader/partner；`just dev doctor` 会探测 Redis 连通性 |
| `rpk topic consume ... -o end` 长时间无输出 | 该命令等待**新**消息，分区已消费完时会阻塞 | 改用 `rpk topic consume amp.message -p <N> -o 0 --num 3` |

## 6. 与自动化测试关系

| 层次 | 入口 | 说明 |
|------|------|------|
| 开发模式 runbook | 本文档 | 人工按步骤走通全链路 |
| 冒烟测试 | `scripts/e2e_message_verify.py` | 直连 Kafka，仅需 infra+monitor |
| 全链路演示 | `scripts/e2e_message_demo.sh` | 全服务在线，群组 submit 后断言 |
| 自动化 E2E | `just test e2e -k message` | pytest 黑盒测试，CI 可跑 |

详见 `plans/message-emitter-and-local-e2e-design.md` 与 `plans/message-code-structure-design.md`。
