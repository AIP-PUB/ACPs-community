# 开发模式 System 联合验证

**前置**：请先按 [dev-runbook.md](./dev-runbook.md) 完成服务启动（monitor-server、demo-leader、
demo-partner、Fluent Bit、OpenSearch）。本文只讲 System 链路在开发模式下的验证内容。

**开始验证前的快速检查**（任一不满足则先修复再继续）：

```bash
# 1. amp.system 必须为 LogAppendTime
docker exec dev-redpanda rpk topic describe amp.system -c | grep timestamp.type
# 期望：LogAppendTime

# 2. Fluent Bit 必须含 kafka.5 worker（system OUTPUT）
# 启动/重启后标准输出应出现：[output:kafka:kafka.5] worker #0 started

# 3. OpenSearch 可达
curl -s 'http://localhost:9200/_cluster/health?pretty'
# 期望：status 为 green 或 yellow

# 4. demo 代码、acps-sdk 或 monitor-server System 模块更新后须重启（从 acps/ 根目录）：
cd demo-leader && just dev restart && cd ..
cd demo-partner && just dev restart && cd ..
cd monitor-server && just dev restart && cd ..
# System 为事件驱动，除 lifecycle 外须真实业务请求才有日志
```

与 Heartbeat / Metrics 相比，System 有三点根本差异：

- **事件驱动**：必须触发业务请求（leader `/api/v1/submit` → partner 内部 LLM/Skill）才有日志。
- **单侧发射**：partner 与 leader 各自写本地 NDJSON，无 traceparent 传播；用 `correlation_id=task_id` 关联。
- **真相源 OpenSearch**：单 Query 端点 `POST /system/events/query`；severity 是核心过滤维度。

建议首次阅读先用[第 4 章快速脚本](#4-快速验证脚本)确认端到端通路，
再按[第 2–3 章](#2-产生系统事件数据)跑通 demo → Fluent Bit → Kafka → Writer → OpenSearch → API 全链路。

## 1. 链路概览

### 1.1 数据流与端口

```text
用户 → demo-leader /api/v1/submit
  → demo-partner GenericRunner._call_llm / _execute_skill（→ logs/amp_system_*.jsonl）
  → demo-leader executor 错误/超时（→ logs/amp_system.jsonl）
  → Fluent Bit kafka.5 → Kafka amp.system
  → SystemWriter → OpenSearch amp-system-events-*
  → Query API POST /system/events/query
```

| 组件 | 端口 | 说明 |
|------|------|------|
| OpenSearch | 9200 | System 真相源 |
| Kafka amp.system | 19092 | ≥4 分区，LogAppendTime |
| Redis | 6379 | 去重 + 水位 |
| demo-leader | 9031 | 编排错误/超时 + 生命周期 |
| demo-partner | 9021-9025 | LLM/Skill/容量/生命周期（每 Agent 一文件） |
| monitor-server | 9009 | System Query API |

### 1.2 涉及 Kafka 主题

| 主题 | 分区 | 关键配置 | 用途 |
|------|------|---------|------|
| `amp.system` | **≥4** | **`message.timestamp.type=LogAppendTime`** | 系统事件输入流 |
| `amp.system.dlq` | 1 | retention 7d | 坏消息死信 |

## 2. 产生系统事件数据

### 2.1 触发业务请求（推荐）

```bash
curl -X POST http://localhost:9031/api/v1/submit \
  -H 'Content-Type: application/json' \
  -d '{"query":"我要在北京订三晚酒店，预算每晚500元，2人入住","clientRequestId":"system-demo-001","mode":"direct_rpc"}'
```

Partner 侧 LLM 事件的 `correlation_id` 形如 `{activeTaskId}:{partner_aic}`（非 leader 返回的 `activeTaskId`  alone）。全链路脚本会从 `amp_system_*.jsonl` 解析完整值再查 Query API。

### 2.2 查看本地 NDJSON 文件

以下命令从 **acps/ 根目录**执行。`tail` 对 glob 多文件会插入 `==> file <==` 分隔行，不能直接管道给 `json.tool`；应查看单文件最后一行（§2.1 酒店请求主要写入 `china_hotel`）：

```bash
tail -1 demo-partner/logs/amp_system_china_hotel.jsonl | python3 -m json.tool
tail -1 demo-leader/logs/amp_system.jsonl | python3 -m json.tool
```

期望看到 `body.category` 为 `llm`、`skill`、`lifecycle` 等，`severity_number` 为 9/13/17。

### 2.3 备用：直写 leader system 文件测试 Fluent Bit

```bash
echo '{"schema_version":"1.0","log_type":"system","log_id":"manual-test-001","timestamp":"2026-06-14T10:00:00.000000Z","aic":"test-aic","severity_number":9,"severity_text":"INFO","body":{"message":"manual fluent bit test","category":"test"}}' \
  >> demo-leader/logs/amp_system.jsonl
```

> timestamp 须含小数秒（与 partner 写入格式一致），否则 Fluent Bit `amp_audit_json` parser 会告警。

## 3. 分层验证

### 3.1 Kafka 水位

```bash
docker exec dev-redpanda rpk topic describe amp.system -p
docker exec dev-redpanda rpk topic consume amp.system --num 1 --format json
```

### 3.2 消费组 LAG

```bash
docker exec dev-redpanda rpk group describe monitor-server.system.writer.v1
```

### 3.3 OpenSearch 文档

```bash
curl -s 'http://localhost:9200/amp-system-events-*/_count'
curl -s 'http://localhost:9200/amp-system-events-*/_search?q=category:llm&size=3' | python3 -m json.tool
```

### 3.4 Redis 水位

```bash
redis-cli -n 2 --scan --pattern 'amp:system:*'
```

### 3.5 Query API（events/query）

```bash
AIC=<某个在线 partner 的 aic>
TASK_ID=<从 amp_system_*.jsonl 取一条的 correlation_id（非 activeTaskId alone）>
START=$(date -u -v-30M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ)
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)   # §2.1 submit 后须重新执行，使 endAt 覆盖新事件
BASE=http://localhost:9009/acps-amp-v1/system

# 最近 20 条 system 事件
curl -s -X POST "$BASE/events/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"page\":{\"limit\":20}}" \
  | python3 -m json.tool

# 按 aic 过滤
curl -s -X POST "$BASE/events/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"filter\":{\"conditions\":[{\"field\":\"aic\",\"op\":\"eq\",\"value\":\"$AIC\"}]},\"page\":{\"limit\":20}}" \
  | python3 -m json.tool

# 按 correlationId 过滤（§3.5 须用 NDJSON 中的完整 correlation_id，非 activeTaskId alone）
# timeRange.endAt 在轮询中动态刷新，避免 submit 之后写入的事件落在固定 endAt 之外
curl -s -X POST "$BASE/events/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"filter\":{\"conditions\":[{\"field\":\"correlationId\",\"op\":\"eq\",\"value\":\"$TASK_ID\"}]},\"page\":{\"limit\":50}}" \
  | python3 -m json.tool

# 按 severityNumber 过滤错误事件
curl -s -X POST "$BASE/events/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"filter\":{\"conditions\":[{\"field\":\"severityNumber\",\"op\":\"gte\",\"value\":17}]},\"page\":{\"limit\":20}}" \
  | python3 -m json.tool

# keyword 搜索
curl -s -X POST "$BASE/events/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"keyword\":\"LLM call completed\",\"page\":{\"limit\":20}}" \
  | python3 -m json.tool
```

## 4. 快速验证脚本

### 4.1 直连 Kafka（不依赖 demo）

```bash
cd monitor-server
APP_ENV=development uv run python scripts/e2e_system_verify.py
```

### 4.2 全链路 demo

脚本会自动解析 `demo-partner/logs/amp_system_*.jsonl` 的绝对路径，**任意当前目录均可**：

```bash
bash monitor-server/scripts/e2e_system_demo.sh
```

## 5. 故障排查

| 症状 | 可能原因 | 处理 |
|------|---------|------|
| 完全没有 system 日志 | System **事件驱动**，无业务请求时不产日志（生命周期 S-P6/S-P7/S-L3/S-L4 除外） | 重启 demo 确认 lifecycle 行，再 `POST /api/v1/submit` |
| `amp.system` 水位不涨 | Fluent Bit 未启动或路径错误 | 确认 `kafka.5` worker；检查 `amp_system*.jsonl` 有新行 |
| Kafka 有消息但 OpenSearch 无数据 | SystemWriter 未启动 / OpenSearch 不可达 | `rpk group describe monitor-server.system.writer.v1` 应为 Stable；`cd monitor-server && just dev restart`；`curl http://localhost:9200/_cat/indices/amp-system*` |
| `monitor-server.system.writer.v1` STATE Dead | monitor-server 在 System 模块合入前启动，或进程已退出 | `cd monitor-server && just dev restart`；确认 LAG 下降、OpenSearch `_count` 增长 |
| demo-partner `just dev start` 端口占用 | 残留 partner 子进程占 9021–9025 | `cd demo-partner && just dev restart`；查 `logs/partners_base.log` 中 `address already in use` |
| `events/query` keyword 无结果 | `search_text` 未生成或 body 缺 `message` | 直查 OpenSearch：`/_search?q=message:*failed*` |
| `correlationId` 过滤无结果 | partner `correlation_id` 为 `{activeTaskId}:{partner_aic}`，非 leader `activeTaskId`  alone | 从 `amp_system_*.jsonl` 取完整 `correlation_id` 再查询 |
| Fluent Bit 起了但 system 不进 Kafka | macOS 缺 `Workers 1` / 路径错 / 缺第六个 OUTPUT | 确认 `kafka.5 worker #0 started`；核对绝对路径；删 `/tmp/fluentbit-*-system.db` 后重启 |
| 全进 DLQ | `amp.system` topic 非 LogAppendTime | `rpk topic describe amp.system -c` 确认 LogAppendTime |
| severityNumber 过滤不生效 | `emit_sync` 未传 `severity_number` | 检查顶层 `severityNumber`（非 body 内） |

## 6. 与自动化测试关系

| 层次 | 入口 |
|------|------|
| 单元测试 | acps-sdk / demo-partner / demo-leader `tests/unit/test_*_system*.py` |
| monitor E2E | `just test e2e -k system` |
| 冒烟脚本 | `scripts/e2e_system_verify.py` |
| 全链路 demo | `scripts/e2e_system_demo.sh` |
