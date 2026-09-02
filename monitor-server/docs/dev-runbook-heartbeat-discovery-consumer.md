# 开发模式联合验证：discovery-server 作为 Heartbeat Sync API Consumer

**前置**：请先按 [dev-runbook-heartbeat.md](./dev-runbook-heartbeat.md) 将 monitor-server
Heartbeat 链路跑通（至少完成 §3.4 Query API + §3.5 Sync API 验证，确认
`/acps-amp-v1/heartbeat/sync/info` 正常返回，且 `amp.heartbeat.alive-delta` 主题有数据）。
本文在此基础上，独立描述**discovery-server 接入 Sync API 作为 Consumer** 的联合调试步骤。

与 Heartbeat Runbook 相比，本文新增的链路段：

- **Provider → Consumer**：monitor-server 通过 `/sync/info` 与 `/sync/snapshot` 对外暴露全量
  快照，通过 `amp.heartbeat.alive-delta` 主题持续发布增量信封。
- **Consumer 本地落地**：discovery-server 消费快照与增量，原子持久化到 PostgreSQL
  （`agent_alive_status` + `alive_sync_shard_state`），并在每次 `POST /acps-adp-v2/discover`
  响应中将 `aliveMap` 附加到 `result` 字段。

---

## 1. 链路概览

### 1.1 数据流与端口

```text
demo-leader / demo-partner（进程周期心跳）
  └─ acps-sdk HeartbeatEmitter → logs/amp_heartbeat*.jsonl
        │ Fluent Bit → Kafka amp.heartbeat
        ▼
  monitor-server HeartbeatWriter → Redis liveness
        │
        ├─ HeartbeatRelay ──► Kafka  amp.heartbeat.alive-delta
        │                      （enter_alive / refresh_alive / leave_alive 信封）
        │
        └─ Sync API（Provider）
               GET /acps-amp-v1/heartbeat/sync/info       ← 元信息（topic、shard）
               GET /acps-amp-v1/heartbeat/sync/snapshot   ← 全量 NDJSON 快照
               │
               │ discovery-server heartbeat_sync（Consumer）
               │   Phase 1 bootstrap：AliveSyncSourceClient
               │     → /sync/info 验证 Provider 在线
               │     → /sync/snapshot 流式拉取，apply_snapshot 写入 PG
               │   Phase 2 delta 消费：AliveDeltaKafkaConsumer
               │     → 订阅 amp.heartbeat.alive-delta
               │     → engine.poll_apply：upsert / delete alive 行
               │   Phase 3 resync：检测到 seq 缺口或 503 → reset → backoff → bootstrap
               ▼
  PostgreSQL  agent_alive_status（AIC 存活表）
              alive_sync_shard_state（per-shard checkpoint）
               │
               └─ POST /acps-adp-v2/discover
                     响应 result.aliveMap = {aic: {alive, lastSeenAt}}
```

涉及组件与端口：

| 组件 | 端口 | 说明 |
|------|------|------|
| acps-infra Redpanda (Kafka) | 19092 | alive-delta 主题 |
| acps-infra PostgreSQL | 5432 | discovery DB（alive 状态落地） |
| monitor-server Sync/Query API | 9009 | Provider：`/sync/info` + `/sync/snapshot` |
| discovery-server | 9005 | Consumer；discover + admin alive-sync API |

涉及 Kafka 主题：

| 主题 | 说明 |
|------|------|
| `amp.heartbeat.alive-delta` | monitor-server HeartbeatRelay 产出；discovery-server 订阅 |

### 1.2 三阶段 bootstrap 机制

discovery-server 启动 alive-sync 时按如下顺序自举：

| 阶段 | 说明 |
|------|------|
| **① bootstrap** | GET `/sync/info`（验证 Provider 在线）→ GET `/sync/snapshot`（NDJSON 流式读取，原子写入 PG）→ 计算 Kafka seek plan（cutover + 5 min 回看裕量） |
| **② delta 消费** | Kafka `poll_apply`：`enter_alive / refresh_alive` → upsert；`leave_alive` → `alive=false`；每条同事务推进 checkpoint |
| **③ resync** | 检测到 seq 缺口或 503 降级 → `store.reset()`（清空两张表）→ 退避 10s → 重走 ① |

已有 checkpoint 时优先**续跑**（跳过 snapshot，直接续 Kafka offset），仅在 hydrate 失败时回退到全量 bootstrap。

---

## 2. 前置条件

1. 已完成 [dev-runbook-heartbeat.md §3.5](./dev-runbook-heartbeat.md) Sync API 验证：

   ```bash
   curl -s "http://localhost:9009/acps-amp-v1/heartbeat/sync/info" | python3 -m json.tool
   # 期望：type="amp-alive-delta"，kafkaTopic="amp.heartbeat.alive-delta"，shardCount=1
   ```

2. `amp.heartbeat.alive-delta` 主题有数据（monitor-server Relay 已产出至少一条信封）：

   ```bash
   docker exec dev-redpanda rpk topic describe amp.heartbeat.alive-delta -p
   # 期望：HIGH-WATERMARK >= 1
   ```

3. discovery-server 已执行 `just dev start`（DB 已迁移，包含 alive-sync 两张表）。

   > 若 bootstrap 在 `CREATE EXTENSION vector` 处失败（`permission denied`），需以 postgres 超级用户
   > 在 `agent_discovery` 库中预先创建扩展后重试：
   >
   > ```bash
   > psql postgresql://postgres:devpass@localhost:5432/agent_discovery \
   >   -c "CREATE EXTENSION IF NOT EXISTS vector;"
   > cd discovery-server && just dev start
   > ```

4. **开发模式环境变量**：`.env` 中 `APP_ENV=development`（否则 `config/development.toml`
   中的 `[alive_sync]` 不会加载）。可用以下命令确认：

   ```bash
   cd discovery-server && APP_ENV=development uv run python - <<'EOF'
   from app.core.config import settings
   print('APP_ENV:', settings.APP_ENV)
   print('ALIVE_SYNC_ENABLED:', settings.ALIVE_SYNC_ENABLED)
   EOF
   # 期望：APP_ENV: development，ALIVE_SYNC_ENABLED: True
   ```

5. **Heartbeat 数据在流动**：monitor-server 侧当前应有在线 Agent（`aliveCount > 0`）。
   若 `curl -s http://localhost:9009/acps-amp-v1/heartbeat/summary` 显示 `aliveCount: 0`，
   请先按 [dev-runbook-heartbeat.md](./dev-runbook-heartbeat.md) 排查，并确认
   [dev-runbook.md §3.5](./dev-runbook.md) 中 Fluent Bit 已前台运行。

6. **Discover 样本数据**（§5.4 aliveMap 验证需要）：discovery DB 中需有 Agent/Skill 索引，
   否则 discover 响应不含 AIC，`aliveMap` 无法注入：

   ```bash
   cd discovery-server && just prep seed app
   ```

---

## 3. 配置 alive-sync

在 `discovery-server/config/development.toml` 末尾追加（若 `[alive_sync]` 节尚不存在）：

```toml
[alive_sync]
enabled                    = true
auto_start                 = true
provider_base_url          = "http://localhost:9009/acps-amp-v1/heartbeat"
kafka_bootstrap_servers    = "localhost:19092"
# kafka_topic 留空：启动时自动从 /sync/info 的 kafkaTopic 字段获取（推荐）
# 其余参数保持默认值（可按需调整）
# bootstrap_lookback_seconds     = 300    # Kafka seek 回看裕量（秒），默认 5 分钟
# resync_backoff_seconds         = 10     # resync 退避间隔（秒）
# retry_interval_seconds         = 30     # 503 降级重试间隔（秒）
```

同时在 `config/development.toml` 的 `[server]` 节建议设 `reload = false`。
alive-sync 后台 Kafka 消费任务在 Uvicorn 热重载模式下会被频繁打断；
联合验证期间关闭 reload 可保持消费稳定。

> **注意**：`provider_base_url` 末尾**不加** `/`，路径为 `heartbeat`（不含 `/sync/...`）。
> discovery-server 会在此基础上拼接 `/sync/info` 和 `/sync/snapshot`。

也可通过环境变量覆盖（`.env` 或启动时 export），优先级高于 TOML：

```bash
export ALIVE_SYNC_ENABLED=true
export ALIVE_SYNC_PROVIDER_BASE_URL=http://localhost:9009/acps-amp-v1/heartbeat
export ALIVE_SYNC_KAFKA_BOOTSTRAP_SERVERS=localhost:19092
```

---

## 4. 启动 discovery-server 并确认 alive-sync 已拉起

```bash
cd discovery-server
just dev restart   # 若已在运行则重启（加载新配置）
# 首次启动：
# just dev start
```

查看启动日志确认三阶段 bootstrap 完成：

```bash
just -f discovery-server/Justfile app logs
# 期望出现（顺序）：
# "alive-sync 后台任务启动成功"
# "alive-sync bootstrap 开始"
# "alive-sync bootstrap 完成，cutover=..."
```

> `alive-sync 后台任务启动成功` 表示守卫条件全部满足（`ALIVE_SYNC_ENABLED=true`、
> `ALIVE_SYNC_AUTO_START=true`、`ALIVE_SYNC_PROVIDER_BASE_URL` 已配置、非 testing 态）。
> 若日志只出现"跳过"字样，请检查 §3 配置是否已保存并重启生效。

**启动后等待**：进程监听 `:9005` 后，alive-sync 的 snapshot 拉取与 Kafka 订阅仍需约 **15–20s**
（见 [dev-runbook.md §1.2 ③](./dev-runbook.md#12-运行时注意)）。此期间 `curl .../admin/alive-sync/status`
可能返回空 body 或 `kafkaNextOffset: null`；属正常现象，待日志出现 `alive-sync bootstrap 完成`
或 `alive-sync 续跑成功` 后再进入 §5 验证。

---

## 5. 验证各环节

### 5.1 Admin API：服务状态

```bash
curl -s http://localhost:9005/admin/alive-sync/status | python3 -m json.tool
# 期望：
# {
#   "running": true,
#   "aliveCount": <N>,          ← PG 中 alive=true 的行数（bootstrap 完成后 > 0）
#   "checkpointCount": 1,       ← shard 数（开发默认 1）
#   "shards": {
#     "hb-000": {
#       "lastSeenSeq": <N>,
#       "cutoverSeq": <N>,
#       "kafkaNextOffset": <N>,
#       "snapshotGeneratedAt": "2026-..."
#     }
#   }
# }
```

`aliveCount > 0` 且 `kafkaNextOffset` 有值，说明 bootstrap 成功、delta 消费已启动。

### 5.2 PostgreSQL：本地 alive 状态

```bash
# alive 状态表（bootstrap 写入）
psql postgresql://discovery:discovery@localhost:5432/agent_discovery \
  -c "SELECT aic, alive, last_seen_at, shard FROM agent_alive_status
      ORDER BY last_seen_at DESC LIMIT 10;"
# 期望：能查到 demo AIC，alive=true，last_seen_at 接近当前时间

# checkpoint 表（per-shard 进度）
psql postgresql://discovery:discovery@localhost:5432/agent_discovery \
  -c "SELECT shard, last_seen_seq, cutover_seq, kafka_next_offset
      FROM alive_sync_shard_state;"
# 期望：shard=hb-000，last_seen_seq / kafka_next_offset 随心跳递增
```

### 5.3 Kafka 消费组

```bash
docker exec dev-redpanda rpk group describe discovery-server.alive-sync.v1
# 期望：STATE=Stable，MEMBERS=1，LAG 趋近 0（delta 消费及时）
```

> 若 `STATE=Dead`，可能是 alive-sync 正处于 resync 退避或 bootstrap 重试间隙（见 §8.6），
> 等待 30s 后重查。联合验证期间请保持 `reload = false`（§3）。

### 5.4 Discover 接口：aliveMap 注入

任意一次 discover 请求，响应中 `result.aliveMap` 应携带本次发现到的各 AIC 活跃状态：

```bash
curl -s -X POST http://localhost:9005/acps-adp-v2/discover \
  -H 'Content-Type: application/json' \
  -d '{"query": "预订北京到上海的高铁"}' \
  | python3 -m json.tool
# 在 result 字段中期望：
# "aliveMap": {
#   "1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ": {
#     "alive": true,
#     "aliveLastSeenAt": "2026-..."
#   },
#   ...
# }
```

> `aliveMap` 只包含**本次查询命中的 AIC**，未命中的 AIC 不出现。
> 若返回结果为转发回传（非本地产出），`aliveMap` 保持原值透传，不被 discovery-server 覆盖。
> 若 alive-sync 未启用，`aliveMap` 字段缺失（零破坏兼容）。

### 5.5 确认增量 delta 正在消费

等待一个心跳周期（约 15s），再次查询 checkpoint 确认 `last_seen_seq` 和 `kafka_next_offset` 已推进：

```bash
psql postgresql://discovery:discovery@localhost:5432/agent_discovery \
  -c "SELECT shard, last_seen_seq, kafka_next_offset FROM alive_sync_shard_state;"
# 对比前后两次，数值应递增
```

---

## 6. 生命周期验证（可选：alive → silent → alive）

停止一个 Agent，验证 `aliveMap` 中对应 AIC 的 `alive` 变为 `false`：

```bash
AIC=1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ
just -f demo-leader/Justfile app stop      # 停止 leader，心跳停发

# 等待 monitor-server 判 silent 并发出 leave_alive delta（> silence_threshold=90s）
sleep 95

# 确认 monitor-server 侧已发出 leave_alive（消费最近消息，避免 --offset start 扫全量卡住）
HW=$(docker exec dev-redpanda rpk topic describe amp.heartbeat.alive-delta -p | awk '$1=="0"{print $6}')
docker exec dev-redpanda rpk topic consume amp.heartbeat.alive-delta \
  --partitions=0 --offset="$((HW-50))" --num=50 --format '%v\n' | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if line:
        v = json.loads(line)
        if v.get('id','').endswith('${AIC}'):
            print(v['kind'], v['op'], v['id'])"
# 期望末尾出现：leave_alive delete urn:amp:alive:<AIC>

# 若 discovery-server 侧未及时更新，可触发一次手动 resync（§7.1）后再查 PG / discover

# 确认 discovery-server 侧已应用
psql postgresql://discovery:discovery@localhost:5432/agent_discovery \
  -c "SELECT aic, alive, last_seen_at FROM agent_alive_status WHERE aic='${AIC}';"
# 期望：alive=false（leave_alive 后行保留），或 resync 后该行从 snapshot 中移除（0 rows）

# 确认 discover 响应中 aliveMap 已更新
curl -s -X POST http://localhost:9005/acps-adp-v2/discover \
  -H 'Content-Type: application/json' \
  -d '{"query": "预订北京到上海的高铁"}' \
  | python3 -c "
import json, sys
resp = json.load(sys.stdin)
alive_map = (resp.get('result') or {}).get('aliveMap') or {}
status = alive_map.get('${AIC}', '（未在结果中）')
print('aliveMap[${AIC}]:', status)"
# 期望：alive=false 或该 AIC 不在本次结果中
```

恢复：重启 demo-leader，等待一个心跳周期后再次 discover，`alive` 应回到 `true`：

```bash
just -f demo-leader/Justfile app start
sleep 20   # 等待一个心跳周期 + delta 传播
curl -s -X POST http://localhost:9005/acps-adp-v2/discover \
  -H 'Content-Type: application/json' \
  -d '{"query": "预订北京到上海的高铁"}' \
  | python3 -c "
import json, sys
resp = json.load(sys.stdin)
alive_map = (resp.get('result') or {}).get('aliveMap') or {}
print('aliveMap:', json.dumps(alive_map, ensure_ascii=False, indent=2))"
```

---

## 7. 手动管理操作

### 7.1 触发手动重同步

适用于 Provider 恢复后需立即重建本地状态、或调试 bootstrap 流程：

```bash
curl -s -X POST http://localhost:9005/admin/alive-sync/resync | python3 -m json.tool
# 期望：{"message": "重同步已触发"}
```

触发后，discovery-server 日志应出现：

```
alive-sync 请求重同步，原因: admin_manual_trigger
alive-sync 重同步退避 10 秒
alive-sync bootstrap 开始
alive-sync bootstrap 完成，cutover=...
```

### 7.2 临时禁用（不停服）

```bash
# 在 config/development.toml 中将 enabled = false，然后重启
# 或通过环境变量：
ALIVE_SYNC_ENABLED=false just -f discovery-server/Justfile app restart
```

禁用后 `discover` 响应中 `aliveMap` 字段缺失，其余行为不受影响。

---

## 8. 故障排查

### 8.1 alive-sync 未启动（日志只有"跳过"）

**现象**：`/admin/alive-sync/status` 返回 `{"running": false, ...}`，日志出现"跳过"。

**排查顺序**：

```bash
# 确认配置已生效
cd discovery-server && APP_ENV=development uv run python - <<'EOF'
from app.core.config import settings
print('ALIVE_SYNC_ENABLED:', settings.ALIVE_SYNC_ENABLED)
print('ALIVE_SYNC_AUTO_START:', settings.ALIVE_SYNC_AUTO_START)
print('ALIVE_SYNC_PROVIDER_BASE_URL:', settings.ALIVE_SYNC_PROVIDER_BASE_URL)
print('ALIVE_SYNC_KAFKA_BOOTSTRAP_SERVERS:', settings.ALIVE_SYNC_KAFKA_BOOTSTRAP_SERVERS)
print('APP_ENV:', settings.APP_ENV)
EOF
```

检查各项守卫条件：

| 条件 | 期望值 | 修复方式 |
|------|--------|---------|
| `ALIVE_SYNC_ENABLED` | `True` | `config/development.toml` 设 `enabled = true` |
| `ALIVE_SYNC_AUTO_START` | `True` | `config/development.toml` 设 `auto_start = true` |
| `ALIVE_SYNC_PROVIDER_BASE_URL` | 非空 http(s) 地址 | 补充 `provider_base_url` |
| `APP_ENV` | `development` | `.env` 设 `APP_ENV=development` 后重启 |
| `UVICORN_RELOAD` | `False`（建议） | `config/development.toml` 设 `reload = false` 后重启 |

### 8.2 bootstrap 失败：Provider 不可达（PROVIDER_UNAVAILABLE / CONNECTION_FAIL）

**现象**：日志出现 `AliveSyncError(PROVIDER_UNAVAILABLE)` 或 `CONNECTION_FAIL`；
`/admin/alive-sync/status` 返回 `running: false`。

**排查**：

```bash
# 确认 monitor-server 在线
curl -s http://localhost:9009/health | python3 -m json.tool

# 确认 Sync API 已启用（monitor-server 开发默认 sync_enabled=true）
curl -s http://localhost:9009/acps-amp-v1/heartbeat/sync/info | python3 -m json.tool
# 若返回 404：monitor-server 的 sync_enabled=false，参见 dev-runbook-heartbeat.md §6.6

# 确认 provider_base_url 配置正确（末尾不能含 /sync/... 或多余 /）
# 正确：http://localhost:9009/acps-amp-v1/heartbeat
# 错误：http://localhost:9009/acps-amp-v1/heartbeat/
```

### 8.3 bootstrap 失败：snapshot 不可用（SNAPSHOT_UNAVAILABLE / DELTA_LOG_UNHEALTHY）

**现象**：`/sync/snapshot` 返回 503，日志含 `SNAPSHOT_UNAVAILABLE` 或 `DELTA_LOG_UNHEALTHY`。

**含义**：monitor-server Relay 或 snapshot 物化尚未就绪（通常刚启动时短暂出现）。
discovery-server 会按 `retry_interval_seconds`（默认 30s）自动重试，**无需手动干预**。

若长时间不恢复，排查 monitor-server：

```bash
# 确认 HeartbeatRelay 是否在运行
just -f monitor-server/Justfile app logs | grep -i relay

# 确认 alive-delta topic 有数据
docker exec dev-redpanda rpk topic describe amp.heartbeat.alive-delta -p
```

### 8.4 Kafka 消费组不前进（LAG 持续增大）

**现象**：`docker exec dev-redpanda rpk group describe discovery-server.alive-sync.v1`
显示 LAG 持续增大，而非趋近 0。

**排查**：

```bash
# 确认 Kafka bootstrap servers 配置正确
cd discovery-server && APP_ENV=development uv run python -c "
from app.core.config import settings
print('ALIVE_SYNC_KAFKA_BOOTSTRAP_SERVERS:', settings.ALIVE_SYNC_KAFKA_BOOTSTRAP_SERVERS)"
# 开发环境期望：localhost:19092

# 确认 Redpanda 可达
docker exec dev-redpanda rpk cluster health

# 查看 discovery-server 日志有无 Kafka 错误
just -f discovery-server/Justfile app logs | grep -i kafka
```

若消费组从未创建（rpk group describe 报错），说明 Kafka 消费者连接阶段就失败，
检查 `ALIVE_SYNC_KAFKA_BOOTSTRAP_SERVERS` 是否指向宿主机可访问的端口（`localhost:19092`
而非容器内地址 `dev-redpanda:9092`）。

### 8.5 aliveMap 始终为空或缺失

依次排查：

1. **alive-sync 未启用**：`/admin/alive-sync/status` 的 `running` 是否为 `true`。
2. **bootstrap 尚未完成**：`aliveCount=0` 说明 PG 中无 alive 行，等待或触发 resync（§7.1）。
3. **查询命中的 AIC 在 PG 中不存在**：

   ```bash
   psql postgresql://discovery:discovery@localhost:5432/agent_discovery \
     -c "SELECT COUNT(*) FROM agent_alive_status WHERE alive=true;"
   # 若为 0，说明 bootstrap 结果为空或 delta 未追上
   ```

4. **Discover 结果来自转发（非本地产出）**：转发回传结果携带上游 `aliveMap`，
   不被 discovery-server 覆盖（ADP §4.2.3 透传语义，属正常行为）。

### 8.6 resync 循环不停（日志不断出现"重同步退避"）

**现象**：日志频繁出现 `alive-sync ResyncRequired` 或 `alive-sync 请求重同步`。

**常见原因**：

1. **seq 缺口**：Kafka `amp.heartbeat.alive-delta` 主题被截断（`LogStartOffset` 跳变），
   discovery-server 检测到 seq 不连续 → 触发 resync。
   检查 topic 水位：

   ```bash
   docker exec dev-redpanda rpk topic describe amp.heartbeat.alive-delta -p
   # LOGSTART-OFFSET 是否大于上次消费 offset
   ```

2. **monitor-server 重启 + snapshot 内容变化**：自举后第一次 bootstrap 的 cutover
   与续跑时的 checkpoint 对不上。此情况下让 resync 自然完成即可。

3. **时钟偏差**：`bootstrap_lookback_seconds` 过小，seek 时遗漏了 cutover 之前的消息。
   将其调大（如 600s）：`config/development.toml` → `bootstrap_lookback_seconds = 600`。

---

## 9. 与自动化测试的关系

| 层次 | 入口 | 说明 |
|------|------|------|
| 手动联合验证 | 本文 | monitor-server + discovery-server 全链路，人工按步骤操作 |
| discovery-server 单元测试 | `just -f discovery-server/Justfile test unit` | heartbeat_sync 模块全 mock，无外部依赖 |
| discovery-server 集成测试 | `just -f discovery-server/Justfile test integration` | 需要 PostgreSQL；alive-sync 以 `APP_ENV=testing` 跳过自动启动，通过 fixture 注入 |
| discovery-server e2e 测试 | `just -f discovery-server/Justfile test e2e alive_sync` | 需要 PostgreSQL；alive-sync 流程通过 mock HTTP/Kafka 模拟 Provider 与 Relay |

> 开发模式联合验证与自动化测试互补：自动化测试在 CI 中以 mock/stub 替代真实 Provider 和
> Kafka，本文覆盖的是**真实多服务协作场景**（monitor-server Relay → Kafka → discovery-server
> bootstrap → aliveMap 注入），确保跨服务协议契约与配置正确性。
