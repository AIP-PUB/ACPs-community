# 开发模式联合验证操作手册：环境与服务启动

本文是各日志类型**开发模式（dev）联合验证**的共用前置，只讲"把环境跑起来"。

> **开发模式联合验证**：在各子项目以 `APP_ENV=development` 运行、共享本地 acps-infra 基础设施
> 的前提下，跨 monitor-server / demo-leader / demo-partner / Fluent Bit 手工走通整条链路。
> 与 `just test e2e`（pytest 自动化）不同，本手册面向**人工按步骤操作**的验证场景。

验证某种日志类型时，先按本文完成服务启动，再查阅对应的专项 runbook。

## 1. 各日志类型速查

| 日志类型 | Kafka 主题 | 存储层 | 专项文档 |
|---------|-----------|-------|---------|
| Audit（审计） | `amp.audit` | PostgreSQL | [dev-runbook-audit.md](./dev-runbook-audit.md)（Mock 主链路；[CA 模式](./dev-runbook-audit.md#4-进阶ca-联合验签ca-模式) 可选） |
| Heartbeat（心跳） | `amp.heartbeat` | Redis | [dev-runbook-heartbeat.md](./dev-runbook-heartbeat.md)（[生命周期验证](./dev-runbook-heartbeat.md#4-生命周期验证可选alive--silent) 可选） |
| Metrics（指标） | `amp.metrics` | Redis + VictoriaMetrics | [dev-runbook-metrics.md](./dev-runbook-metrics.md) |
| Access（访问） | `amp.access` | ClickHouse | [dev-runbook-access.md](./dev-runbook-access.md) |
| Message（消息） | `amp.message` | ClickHouse | [dev-runbook-message.md](./dev-runbook-message.md) |
| System（系统） | `amp.system` | OpenSearch | [dev-runbook-system.md](./dev-runbook-system.md) |

> 后续新增日志类型时，在此表追加一行，并新建 `dev-runbook-{type}.md`。

### 1.1 验证层次与脚本命名

| 层次 | 入口 | 说明 |
|------|------|------|
| 开发模式 runbook | `docs/dev-runbook-*.md` | 人工按步骤走通全链路（本文档体系） |
| 冒烟测试 | `scripts/smoke_*.py` | 30s 内确认通路（仅需 infra + monitor，不依赖 demo）；Access 见 `smoke_access.py` |
| 全链路演示 | `scripts/demo_*.sh` / `e2e_*_demo.sh` | 全服务在线，等待自然传播后断言（见 [§1.2](#12-运行时注意) 工作目录说明） |
| 自动化 E2E | `just test e2e` | pytest 黑盒测试，CI 可跑 |

### 1.2 运行时注意

联调时常见的三点操作约束（建议默认遵守）：

**① 启动 monitor-server 前先等待 infra 就绪**

`just dev start` / `just dev restart` 会执行同一套 check-first `just dev bootstrap` 逻辑，除 postgres / kafka / redis 外，还会检查
VictoriaMetrics、ClickHouse、OpenSearch 等**已启用 profile** 的容器是否 healthy。首次启动、
`docker compose up` 或机器休眠恢复后，若 doctor 报某服务 `starting` / `unhealthy`，先等待再启应用：

```bash
cd monitor-server
just infra up postgres kafka redis victoria-metrics clickhouse opensearch
just infra wait postgres kafka redis victoria-metrics clickhouse opensearch
just dev start
```

仅验证 Audit / Heartbeat 时可只 `wait postgres kafka redis`；跑全链路（Metrics / Access / Message / System）
须把对应存储一并 `up` + `wait`（见各专项 runbook 前置检查）。

**② 全链路脚本的工作目录**

| 脚本类型 | 推荐执行方式 | 说明 |
|---------|-------------|------|
| `scripts/smoke_*.py`、`e2e_*_verify.py` | `cd monitor-server` 后 `uv run python ...` | 不读 demo 本地日志路径 |
| `scripts/demo_heartbeat.sh`、`demo_metrics.sh`、`demo_audit.sh` | `bash monitor-server/scripts/...`（任意当前目录均可） | 脚本内自动解析 `acps/` 仓库根（`demo-leader` / `demo-partner` 日志路径） |
| `scripts/e2e_access_demo.sh`、`e2e_message_demo.sh` | 任意目录均可 | 仅调 HTTP API，不依赖本地日志路径 |
| `scripts/e2e_system_demo.sh` | 任意目录均可 | 自动解析 partner system 日志绝对路径 |
| Fluent Bit（§3.5） | **必须在 `acps/` 根目录** | 配置中 tail 路径相对于仓库根 |

**③ discovery-server alive-sync 启动后稍等再验**

`just dev restart` 后 HTTP 端口虽立即可用，但 alive-sync 的 bootstrap / Kafka 订阅需约 **15–20s**。
过早请求 `GET /admin/alive-sync/status` 可能得到空响应或 `kafkaNextOffset: null`。
应等日志出现 `alive-sync bootstrap 完成` 或 `alive-sync 续跑成功` 后再执行
[dev-runbook-heartbeat-discovery-consumer.md](./dev-runbook-heartbeat-discovery-consumer.md) §5。

## 2. 前置条件

各项目已用 `just dev start`（或 `restart`）拉起服务，且宿主机已安装：

- `uv`、`just`
- Fluent Bit（`brew install fluent-bit`，macOS ≥ v2）
- Docker Desktop（运行 acps-infra 基础设施）
- `redis-cli`（`brew install redis`，Heartbeat/Metrics 验证时用于直查 Redis）

> **Redis 逻辑库**：monitor-server 开发态使用 `redis://localhost:6379/2`（`config/default.toml`）。
> 手册中直查 Redis 的命令统一写作 `redis-cli -n 2`；`redis-cli -p 6379` 默认 DB 0，**看不到** monitor 的水位与心跳数据。

Metrics 验证还需启动 VictoriaMetrics：

```bash
just infra up victoria-metrics   # 或：
docker compose -f acps-infra/dev-infra/compose.yml --profile victoria-metrics up -d
```

Access 验证还需启动 ClickHouse：

```bash
just infra up clickhouse   # 或：
docker compose -f acps-infra/dev-infra/compose.yml --profile clickhouse up -d
curl -s http://localhost:8123/ping   # 期望：Ok.
```

System 验证还需启动 OpenSearch：

```bash
just infra up opensearch   # 或：
docker compose -f acps-infra/dev-infra/compose.yml --profile opensearch up -d
curl -s 'http://localhost:9200/_cluster/health?pretty'   # 期望：status green/yellow（URL 须加引号，避免 zsh 将 ? 当作 glob）
```

同级目录结构：

```text
acps/
├── acps-infra/        # 共享基础设施（Kafka / Redis / PostgreSQL）
├── acps-sdk/          # 共享 SDK（含 AuditEmitter、HeartbeatEmitter、MetricsEmitter）
├── ca-server/         # 证书与公钥服务（Audit CA 模式需要）
├── demo-leader/       # Leader 应用
├── demo-partner/      # Partner 应用
└── monitor-server/    # 本项目
```

## 3. 服务启动

各步骤各占一个终端，保持运行。

### 3.1 基础设施（acps-infra）

`just dev start` / `restart` 会拉起 infra 并执行 `prep migrate app`（见 §3.2）。**清空 Docker 卷 / 首次克隆**后须对 monitor-server 执行
`just dev restart`（见 §3.2），避免旧进程仍连着空库（`/health` 可能发现不了缺表）。

若仅需检查 infra 状态：

```bash
just -f monitor-server/Justfile infra status
# 期望：dev-postgres / dev-redpanda / dev-redis 均显示 running + healthy
```

Kafka 心跳 / Metrics 主题需确认时间戳类型（Heartbeat / Metrics 专项必查）：

```bash
docker exec dev-redpanda rpk topic describe amp.heartbeat -c | grep -i timestamp
# 期望：message.timestamp.type  LogAppendTime

docker exec dev-redpanda rpk topic describe amp.metrics -c | grep -i timestamp
# 期望：message.timestamp.type  LogAppendTime

docker exec dev-redpanda rpk topic describe amp.access -c | grep -i timestamp
# 期望：message.timestamp.type  LogAppendTime

docker exec dev-redpanda rpk topic describe amp.access -p
# 期望：PARTITION 行数 ≥ 4（仅 1 分区时执行下方 just infra up kafka）

docker exec dev-redpanda rpk topic describe amp.message -c | grep -E 'timestamp|partition'
# 期望：message.timestamp.type  LogAppendTime，分区数 ≥ 4
```

若 `amp.metrics` 仍为 `CreateTime`，Fluent Bit 转发的 metrics 会进入 DLQ（见
[dev-runbook-metrics.md §5.1](./dev-runbook-metrics.md)）。修正：

```bash
just -f monitor-server/Justfile infra up kafka   # 幂等修正 LogAppendTime
```

Metrics 验证需确认 VictoriaMetrics 已起：

```bash
curl -s http://localhost:8428/health
# 期望：Alive（部分版本返回 OK）
```

### 3.2 monitor-server

```bash
cd monitor-server
just dev restart
# restart/start：infra up + wait + prep env/sync/hooks + prep migrate app
# restart：停旧进程并启动 API（:9009）+ 后台 Writer（Audit/Heartbeat/Metrics/Access/Message/System）
# 若 doctor 因 infra 未就绪失败，先 wait（见 §1.2 ①）：
# just infra wait postgres kafka redis victoria-metrics clickhouse opensearch
```

日常仅 pull 了新 migration 时，同样可重复执行上述命令（幂等）。
若确定无旧进程在跑，也可 `just dev start`。

验证：

```bash
curl -s http://localhost:9009/health | python3 -m json.tool
# {
#   "status": "ok",
#   "checks": {"database": "ok", "redis": "ok"}
# }
```

联合验证（含 Message / Heartbeat Kafka 消费）期间，建议在 `config/development.toml` 的 `[server]` 节设 `reload = false` 后执行 `just dev restart`，避免 Uvicorn 热重载打断后台任务。`just dev start` 会读取该配置决定是否启用 `--reload`。

### 3.3 demo-leader

```bash
cd demo-leader
just dev restart
# 首次、Docker 卷重置或 pull 含配置变更后建议成对执行
```

验证：

```bash
curl -s http://localhost:9031/api/v1/health
# {"status":"healthy","version":"1.0.0",...}
```

### 3.4 demo-partner

```bash
cd demo-partner
just dev restart
```

验证：

```bash
just -f demo-partner/Justfile app status
```

### 3.5 Fluent Bit

Fluent Bit 一个进程覆盖所有日志类型（audit + heartbeat + metrics + access + message + system），使用同一配置文件。
**在独立终端前台运行，保持窗口开着即可**：

```bash
# 从 acps/ 根目录执行（配置文件中使用了绝对路径）
fluent-bit -c "$(pwd)/monitor-server/config/fluent-bit/fluent-bit.conf"
```

启动成功后应看到六个 Kafka OUTPUT 的 worker 均已启动：

```
[output:kafka:kafka.0] worker #0 started      # amp.audit
[output:kafka:kafka.1] worker #0 started      # amp.heartbeat
[output:kafka:kafka.2] worker #0 started      # amp.metrics
[output:kafka:kafka.3] worker #0 started      # amp.access
[output:kafka:kafka.4] worker #0 started      # amp.message
[output:kafka:kafka.5] worker #0 started      # amp.system
```

> **macOS 注意**：
> - 所有 `[OUTPUT]` 段必须保留 `Workers 1`，否则 Kafka 插件会因 kqueue 兼容性问题静默退出。
> - **不要使用 `-d` daemon 模式**：在 macOS 上 `-d` 会导致 fork 后事件循环不工作，
>   日志实际无法被转发。直接在独立终端前台运行即可。
> - **配置变更后必须重启 Fluent Bit**（如新增 system INPUT/OUTPUT）：旧进程不会自动加载新段。
>   停止旧进程后重新执行上述命令，确认出现六个 worker（含 `kafka.5` # amp.system）。
> - **Access 埋点依赖 acps-sdk `AccessEmitter`**：`acps-sdk` 或 demo 代码更新后须 `just dev restart`
>   demo-leader / demo-partner（见 [dev-runbook-access.md](./dev-runbook-access.md) 快速检查）。

## 4. 通用 Kafka 检查命令

各日志类型对应的主题名和消费组名：

| 类型 | 主题 | 消费组 | DLQ 主题 |
|------|------|--------|---------|
| Audit | `amp.audit` | `amp.audit.writer` | `amp.audit.dlq` |
| Heartbeat | `amp.heartbeat` | `monitor-server.heartbeat.writer.v1` | `amp.heartbeat.dlq` |
| Metrics | `amp.metrics` | `monitor-server.metrics.writer.v1` | `amp.metrics.dlq` |
| Access | `amp.access` | `monitor-server.access.writer.v1` | `amp.access.dlq` |
| Message | `amp.message` | `monitor-server.message.writer.v1` | `amp.message.dlq` |
| System | `amp.system` | `monitor-server.system.writer.v1` | `amp.system.dlq` |

```bash
# 查看主题分区水位（HIGH-WATERMARK 随日志写入递增）
docker exec dev-redpanda rpk topic describe {topic} -p

# 查看消费组 LAG（应趋近 0）
docker exec dev-redpanda rpk group describe {consumer-group}

# 查看 DLQ 水位
# 全新环境应为 0；复用已有环境时，关注水位是否在本次验证期间增长（非增长则正常）
docker exec dev-redpanda rpk topic describe {topic}.dlq -p
```

## 5. 常见启动问题

### Fluent Bit 启动后日志无进 Kafka

1. **macOS 上使用了 `-d` daemon 模式**：改为前台运行（见 §3.5）。
2. 确认标准输出出现 `worker #0 started`。
3. 确认配置中所有 `[OUTPUT]` 都设置了 `Workers 1`。
4. 确认日志文件路径使用绝对路径，且文件实际存在。
5. 确认 Redpanda 可连：`docker exec dev-redpanda rpk cluster health`。

### monitor-server 启动失败

```bash
# 查看实时日志
just -f monitor-server/Justfile app logs

# 检查基础设施健康
just -f monitor-server/Justfile infra status
```

### 基础设施（Kafka / Redis / PostgreSQL 等）未启动或未 healthy

```bash
cd monitor-server
just infra up postgres kafka redis victoria-metrics clickhouse opensearch
just infra wait postgres kafka redis victoria-metrics clickhouse opensearch
```

若只需部分链路，可缩减 `up` / `wait` 的服务列表（见 §1.2 ①）。`just dev start` 前
`redis-cli -n 2 ping` 应返回 `PONG`；OpenSearch 刚拉起时可能为 `starting`，须 `wait` 后再启 monitor。

### Docker 卷重置后

清空 Postgres / Redis / Kafka 等 Docker 卷后，按顺序对各服务执行 **`just dev restart`**
（monitor-server、demo-leader、demo-partner；Message 另需 `just infra up rabbitmq` 与 mq-auth-server，见
[dev-runbook-message.md](./dev-runbook-message.md)）。勿只杀进程重启而不走 `just dev restart`——开发库 migrate 在启动准备逻辑内，
`just test bootstrap` 仅迁移**测试库**，不能代替开发态 `just dev start` / `restart`。
