# 开发模式 Access 联合验证

**前置**：请先按 [dev-runbook.md](./dev-runbook.md) 完成服务启动（monitor-server、demo-leader、
demo-partner、Fluent Bit、ClickHouse）。本文只讲 Access 链路在开发模式下的验证内容。

**开始验证前的快速检查**（任一不满足则先修复再继续）：

```bash
# 1. amp.access 必须为 LogAppendTime，分区数 ≥ 4
docker exec dev-redpanda rpk topic describe amp.access -c | grep timestamp.type
docker exec dev-redpanda rpk topic describe amp.access -p
# 期望：LogAppendTime；PARTITION 行数 ≥ 4（仅 1 分区时执行 dev-runbook.md §3.1 的 just infra up kafka）

# 2. Fluent Bit 必须含 kafka.3 worker（access OUTPUT）
# 启动/重启后标准输出应出现：[output:kafka:kafka.3] worker #0 started

# 3. ClickHouse 可达
curl -s http://localhost:8123/ping
# 期望：Ok.

# 4. demo 代码或 acps-sdk 更新后须 restart demo-leader / demo-partner（Access 为事件驱动，需真实业务请求）
#    demo-partner 若启动报 cannot import name 'AccessEmitter'，先确认 acps-sdk 已导出 AccessEmitter 并 uv sync
```

与 Heartbeat / Metrics 链路相比，Access 有三点根本差异：

- **事件驱动**：必须触发业务请求（leader `/api/v1/submit` → partner `/rpc`）才有日志；进程在线不会自动周期发射。
- **双侧发射**：leader 发 caller client span（SDK `AipRpcClient`），partner 发 callee server span（`/rpc` 端点）。
- **真相源 ClickHouse**：7 个 Query 端点；trace/topology 依赖 `traceparent` 传播。
- **不发射边界（O-A1/O-A4）**：用户→leader 入站、群组 `/group/rpc`、partner→leader 回调均不发射。

建议首次阅读先用[第 4 章快速脚本](#4-快速验证脚本)确认端到端通路，
再按[第 2–3 章](#2-产生访问数据)跑通 demo → Fluent Bit → Kafka → Writer → ClickHouse → API 全链路。

## 1. 链路概览

### 1.1 数据流与端口

```text
用户 → demo-leader /api/v1/submit（不发射，O-A1）
  → AipRpcClient._send_request（caller span → logs/amp_access.jsonl）
  → demo-partner POST /rpc（server span → logs/amp_access_*.jsonl）
  → Fluent Bit kafka.3 → Kafka amp.access
  → AccessWriter → ClickHouse + Redis
  → Query API（7 端点）
```

| 组件 | 端口 | 说明 |
|------|------|------|
| ClickHouse | 8123 / 19010 | Access 真相源 |
| Kafka amp.access | 19092 | ≥4 分区，LogAppendTime |
| Redis | 6379 | 去重 + 水位 + trace hint |
| demo-leader | 9031 | caller 访问日志 |
| demo-partner | 9021-9025 | callee 访问日志（每 Agent 一文件） |
| monitor-server | 9009 | Access Query API |

### 1.3 涉及 Kafka 主题

| 主题 | 分区 | 关键配置 | 用途 |
|------|------|---------|------|
| `amp.access` | **≥4** | **`message.timestamp.type=LogAppendTime`** | 访问输入流（Writer 消费） |
| `amp.access.dlq` | 1 | retention 7d | 坏消息死信 |

### 1.4 与 Heartbeat 链路差异

| 维度 | Heartbeat | Access |
|------|-----------|--------|
| 触发方式 | 进程在线即周期发射 | **每次业务 RPC 交互** |
| 真相源 | Redis | ClickHouse |
| 追踪 | 无 | trace_id/span_id + traceparent 传播 |
| Query 端点 | 4 + Sync | 7（events/operations/errors/slow/traces/topology） |
| 双侧发射 | 否 | 是（caller SDK + callee `/rpc`） |

## 2. 产生访问数据

### 2.1 触发业务请求（推荐）

以下命令须在 **`acps/` 仓库根目录** 执行（与 `dev-runbook.md` §3.5 Fluent Bit 一致）。

**推荐（双侧 `/rpc` 埋点）**：使用仅命中 **RPC Partner**（`streaming=false`，如 `china_hotel`）的酒店类请求，避免规划器同时选中 `china_transport`（`streaming=true`）而走 `/stream`——当前设计仅在 `AipRpcClient`（`/rpc`）与 partner `/rpc` 发射 Access，**`/stream` 路径不发射**。

```bash
curl -s -X POST http://localhost:9031/api/v1/submit \
  -H "Content-Type: application/json" \
  -d '{"query":"我要在北京订三晚酒店，预算每晚500元，2人入住","clientRequestId":"access-dev-1","mode":"direct_rpc"}'
```

多维度行程（如「北京三日游」）可能并发调度 hotel（RPC）+ transport（stream）；leader 侧 caller span 仍会有，但 **partner 侧 `amp_access_*.jsonl` 可能为空**（stream 未埋点）。全链路双侧验证请用上方酒店请求，或见 [§4.2](#42-e2e_access_demosh) 脚本（仅断言 Query API 可达）。

```bash
# 通用示例（可能走 stream，见上文说明）
curl -s -X POST http://localhost:9031/api/v1/submit \
  -H "Content-Type: application/json" \
  -d '{"query":"帮我规划北京三日游","clientRequestId":"access-dev-1","mode":"direct_rpc"}'
```

提交后等待约 30–60s（LLM 规划 + Partner RPC 轮询完成），再执行 §2.2 / §3。

### 2.2 查看本地文件

在 **`acps/` 根目录** 执行：

```bash
tail -1 demo-leader/logs/amp_access.jsonl | python3 -m json.tool
# partner：每个 Agent 一个文件，文件名为 partners/online/<agent_dir> 目录名，例如：
tail -1 demo-partner/logs/amp_access_china_hotel.jsonl | python3 -m json.tool
# 或列出全部：
ls demo-partner/logs/amp_access_*.jsonl
```

期望：`log_type` 为 `access`，含 `trace_id`、`span_id`、`caller`、`callee`；**不含** `integrity`。leader 与 partner 各至少一行（同一 `trace_id` 下 caller + server span）。

### 2.3 备用：直连 Kafka 投一条（无需 demo）

见 [§4.1](#41-smoke_accesspy--e2e_access_verifypy)。

## 3. 分环节验证

### 3.1 Kafka

```bash
docker exec dev-redpanda rpk topic describe amp.access -p
docker exec dev-redpanda rpk topic describe amp.access.dlq -p
# amp.access 期望 ≥ 4 个 PARTITION；仅 1 分区时：just -f monitor-server/Justfile infra up kafka
```

DLQ 不应随正常请求持续增长。

### 3.2 消费组

```bash
docker exec dev-redpanda rpk group describe monitor-server.access.writer.v1
```

LAG 应趋近 0。

### 3.3 ClickHouse

```bash
curl -s 'http://localhost:8123/?query=SELECT%20count()%20FROM%20amp.access_events'
curl -s 'http://localhost:8123/?query=SELECT%20count()%20FROM%20amp.access_topology_edge_5m'
```

### 3.4 Redis

```bash
redis-cli -n 2 --scan --pattern 'amp:access:*' | head
```

期望：去重键、分区水位、trace hint 等键存在。

### 3.5 Query API（七端点）

```bash
AIC=<某个在线 partner 的 aic>
TRACE=<从 events 或本地 jsonl 取 trace_id>
START=$(date -u -v-15M +%Y-%m-%dT%H:%M:%SZ); END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
BASE=http://localhost:9009/acps-amp-v1/access

# events
curl -s -X POST "$BASE/events/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"page\":{\"limit\":20}}" | python3 -m json.tool

# operations
curl -s -X POST "$BASE/operations/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"groupBy\":[\"endpoint\"]}" | python3 -m json.tool

# errors/attribution（需 analyticsEnabled）
curl -s -X POST "$BASE/errors/attribution" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"groupBy\":[\"endpoint\"],\"topN\":10}" | python3 -m json.tool

# slow-requests/top（需 analyticsEnabled）
curl -s -X POST "$BASE/slow-requests/top" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"topN\":10}" | python3 -m json.tool

# traces/query（需 apmEnabled）
curl -s -X POST "$BASE/traces/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"page\":{\"limit\":20}}" | python3 -m json.tool

# traces/{traceId}（响应头含 AMP-Data-Freshness-At，可选 -I 查看）
curl -s "$BASE/traces/$TRACE" | python3 -m json.tool
curl -sI "$BASE/traces/$TRACE" | grep -i '^amp-'

# topology/query（需 apmEnabled；仅 callee 入站视角单边计数）
curl -s -X POST "$BASE/topology/query" -H 'Content-Type: application/json' \
  -d "{\"timeRange\":{\"startAt\":\"$START\",\"endAt\":\"$END\"},\"groupBy\":\"aic\"}" | python3 -m json.tool
```

> Profile 开关见 `config/development.toml` `[access]` 的 `analytics_enabled` / `apm_enabled`。

## 4. 快速验证脚本

### 4.1 `smoke_access.py` / `e2e_access_verify.py`

仅需 infra + ClickHouse + monitor-server（不依赖 demo / Fluent Bit）：

```bash
cd monitor-server
APP_ENV=development uv run python scripts/smoke_access.py
# 或：uv run python scripts/e2e_access_verify.py
```

### 4.2 `e2e_access_demo.sh`

全服务在线，触发 `/submit` 后轮询 Query API：

```bash
# 推荐：bash monitor-server/scripts/e2e_access_demo.sh（任意当前目录均可，仅调 HTTP API）
bash monitor-server/scripts/e2e_access_demo.sh
```

## 5. 故障排查

| 现象 | 根因 | 处置 |
|------|------|------|
| 访问日志全进 DLQ | topic 非 LogAppendTime | `just -f monitor-server/Justfile infra up kafka` |
| 完全没有访问日志 | 未触发业务请求（非周期发射） | 发 `/api/v1/submit`（推荐 §2.1 酒店 RPC 请求） |
| partner 无 `amp_access_*.jsonl` | 走了 `/stream` 或 demo-partner 未重启 | 用 §2.1 酒店请求；`just -f demo-partner/Justfile app restart` |
| demo-partner 启动 ImportError | acps-sdk 未导出 `AccessEmitter` | 升级/同步 acps-sdk 后 restart partner |
| traces 只有一层 span | traceparent 未传播 | 确认 leader 注入 emitter；partner `/rpc` 读 header |
| topology 无边/翻倍 | 仅 caller 发射或 callee.aic 填错 | 确认 partner server span；MV 方向收敛仅计 callee 行 |
| Query 503 `AMP_READ_MODEL_LAGGING` | CH 不可达、消费 LAG 大，或 **Redis 分区水位残留** | `curl :8123/ping`；`rpk group describe monitor-server.access.writer.v1`；见下方 §5.1 |
| events/query 503 但 CH 已有数据 | `amp:access:wm:partitions` 含已不存在的 Kafka 分区，min(wm) 过旧 | §5.1 清理或 `just infra up kafka` 恢复 4 分区后 `just dev restart` |
| analytics/apm 404 | Profile 未启用 | `[access]` 开启对应开关后重启 |
| Fluent Bit 无 access | 缺 kafka.3 / 路径错 | 确认 `kafka.3 worker #0 started` |

### 5.1 Query 503：Access 读模型水位（Redis DB 2）

Access 新鲜度键在 **Redis 逻辑库 2**（`redis-cli -n 2`）。`events/query` 在整体水位滞后超过 5 分钟时返回 503。

**常见场景：Docker/Kafka 重置后**，`amp:access:wm:partitions` 仍登记分区 `1,2,3`，但 `amp.access` 已缩为 1 分区，
Writer 不再更新 1–3 的水位 → `min(wm)` 冻结 → 永久 503。

```bash
# 查看登记分区与水位（monitor 开发库 2）
redis-cli -n 2 smembers amp:access:wm:partitions
redis-cli -n 2 mget amp:access:wm:0 amp:access:wm:1 amp:access:wm:2 amp:access:wm:3

# 处置 A：恢复 Kafka 4 分区（推荐，与 dev-infra 一致）
just -f monitor-server/Justfile infra up kafka
just -f monitor-server/Justfile app restart

# 处置 B：仅清理残留分区水位（单分区开发态应急）
# 将 <ids> 换为 smembers 中超出当前 topic 分区数的成员，例如 1 2 3
redis-cli -n 2 del amp:access:wm:1 amp:access:wm:2 amp:access:wm:3
redis-cli -n 2 srem amp:access:wm:partitions 1 2 3
just -f monitor-server/Justfile app restart
```

## 6. 与自动化测试关系

| 层次 | 入口 |
|------|------|
| 手动联调 | 本文档 |
| 冒烟脚本 | `scripts/smoke_access.py` |
| 全链路演示 | `scripts/e2e_access_demo.sh` |
| pytest E2E | `just test e2e -k access` |

覆盖：`test_access_ingest_flow`、`test_access_apm_flow`、`test_access_analytics_flow`、`test_access_bilateral_flow`。
