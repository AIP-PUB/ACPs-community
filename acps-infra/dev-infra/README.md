# dev-infra

`dev-infra` 是 ACPs 多项目共享的本地开发依赖集合。日常开发通过 [dev-infra.sh](./dev-infra.sh) 管理底层 [compose.yml](./compose.yml) 中的服务，而不是直接手写 `docker compose` 命令。

## 目标

- 统一 `registry-server`、`ca-server`、`discovery-server`、`acps-cli` 等项目的共享依赖入口
- 对外只暴露稳定的 service 名，不暴露 `profile` 之类的 Compose 实现细节
- 宿主机端口与各服务原生监听端口一致（如 Redis `6379`、PostgreSQL `5432`），**但 90xx 预留给各应用服务**；Kafka / MinIO / ClickHouse 原生 TCP 等落在 90xx 的服务改映射到 `19xxx` 段
- 项目级 `Justfile` 通过 [just/infra.just](./just/infra.just) 统一委托 `dev-infra.sh`

## 公开 service

| 公开 service       | Compose service        | 容器名                 | 宿主机端口                         | volume                       | 说明 |
| ------------------ | ---------------------- | ---------------------- | ---------------------------------- | ---------------------------- | ---- |
| `postgres`         | `dev-postgres`         | `dev-postgres`         | `5432`                             | `dev-infra_dev-pgdata`       | 默认依赖，含各项目开发库与测试库 |
| `redis`            | `dev-redis`            | `dev-redis`            | `6379`                             | `dev-infra_dev-redisdata`    | 可选；逻辑库号按项目+环境划分（见下文） |
| `rabbitmq`         | `dev-rabbitmq`         | `dev-rabbitmq`         | `5671`（TLS）, `15672`（管理面）   | `dev-infra_dev-mqdata`       | 可选；开发环境仅 TLS broker |
| `gateway`          | `dev-nginx`            | `dev-nginx`            | `80`                               | 无                           | 开发网关 |
| `keycloak`         | `dev-keycloak`         | `dev-keycloak`         | `9080`                             | `dev-infra_dev-keycloakdata` | 可选；真人 OIDC / Keycloak 联调 |
| `kafka`            | `dev-redpanda`         | `dev-redpanda`         | `19092`, `19644`                     | `dev-infra_dev-redpandadata` | 宿主机 `19092`（避开应用 90xx）；容器网络内 `dev-redpanda:9092` |
| `victoria-metrics` | `dev-victoria-metrics` | `dev-victoria-metrics` | `8428`                             | `dev-infra_dev-vmdata`       | 时序数据库 |
| `clickhouse`       | `dev-clickhouse`       | `dev-clickhouse`       | `8123`（HTTP）, `19010`（原生 TCP） | `dev-infra_dev-chdata`       | 原生 TCP 映射到 `19010`，避开应用 90xx |
| `minio`            | `dev-minio`            | `dev-minio`            | `19000`（API）, `19001`（Console）   | `dev-infra_dev-miniodata`    | 避开 registry `9001` 等应用端口 |
| `opensearch`       | `dev-opensearch`       | `dev-opensearch`       | `9200`                             | `dev-infra_dev-opensearchdata` | System 全文检索 |

兼容旧写法：

- `dev-postgres`
- `dev-redis`
- `dev-rabbitmq`
- `dev-nginx`

脚本仍接受旧名称，但会输出弃用提示；新文档和项目级入口统一使用 `postgres`、`redis`、`rabbitmq`、`gateway`。

## 快速开始

```bash
# 检查 Docker / Compose / compose.yml / service 映射
./dev-infra.sh check

# 启动默认依赖（postgres）
./dev-infra.sh up

# 查看全部服务状态
./dev-infra.sh status

# 启动额外依赖
./dev-infra.sh up redis rabbitmq

# 启动 Keycloak（会自动完成 realm / EdDSA / registry-cli、monitor-cli、registry-e2e、monitor-e2e、leader-e2e client bootstrap）
./dev-infra.sh up keycloak

# 启动 monitor-server Access 所需依赖
./dev-infra.sh up redis kafka clickhouse

# 启动 monitor-server Message 全链路（含 Writer 消费）
./dev-infra.sh up redis kafka clickhouse

# 等待就绪
./dev-infra.sh wait postgres rabbitmq

# 查看日志
./dev-infra.sh logs postgres rabbitmq --follow

# 停止整个 dev-infra
./dev-infra.sh down
```

## 命令说明

### `check`

检查运行前置条件：

- `docker` 是否可用
- `docker compose` 是否可用
- `compose.yml` 是否可解析
- 顶层 project name 是否与脚本常量一致
- service 和 volume 映射是否完整
- 外部网络 `acps-dev-net` 是否存在

示例：

```bash
./dev-infra.sh check
```

### `up [service ...]`

启动指定服务；不传 service 时默认启动 `postgres`。

示例：

```bash
./dev-infra.sh up
./dev-infra.sh up postgres
./dev-infra.sh up postgres rabbitmq
```

说明：

- 首次启动会自动创建外部网络 `acps-dev-net`
- `up` 只负责提交启动命令；需要等待健康检查时，再执行 `wait`

### `down`

停止整个 `dev-infra` compose 项目，保留 volume。

示例：

```bash
./dev-infra.sh down
```

说明：

- 这是共享依赖的整体关闭操作，会影响所有正在使用 `dev-infra` 的本地项目
- 默认不删除 volume，不会清空数据库或消息数据

### `status [service ...]`

输出静态定义和动态状态。

示例：

```bash
./dev-infra.sh status
./dev-infra.sh status postgres rabbitmq
```

输出内容包括：

- 公开 service 名
- Compose service 名
- 容器名
- 端口映射
- volume 名
- 当前状态和健康状态
- 服务说明

如果 Docker daemon 当前不可访问，`status` 会退化为静态视图，并把动态字段标成 `unavailable`。

### `wait [service ...]`

等待服务就绪。

示例：

```bash
./dev-infra.sh wait
./dev-infra.sh wait postgres
./dev-infra.sh wait postgres rabbitmq
```

说明：

- 不传 service 时，默认等待当前已创建的服务容器
- 对带 healthcheck 的服务，等待 `healthy`
- 对无 healthcheck 的服务，等待 `running`

### `logs [service ...] [--tail N] [--since DURATION] [--follow]`

查看日志，支持单服务、多服务和跟随模式。

示例：

```bash
./dev-infra.sh logs
./dev-infra.sh logs postgres
./dev-infra.sh logs postgres rabbitmq --tail 300
./dev-infra.sh logs rabbitmq --since 10m --follow
```

说明：

- 默认输出最近 `200` 行
- 默认不阻塞；只有加 `--follow` 才持续跟随
- 不传 service 时，默认只输出当前运行中的服务日志
- 如果需要查看已停止容器的日志，请显式指定 service

### `reset [service ...] [--volumes] [--yes]`

做修复性重建或显式数据清理。

示例：

```bash
./dev-infra.sh reset postgres
./dev-infra.sh reset postgres --volumes --yes
./dev-infra.sh reset --volumes --yes
```

说明：

- 不带 `--volumes`：删除容器并重建，保留数据
- 带 `--volumes`：删除对应 volume，下次 `up` 时重建数据
- 全量 `reset` 或任何带 `--volumes` 的操作，都要求显式传 `--yes`
- `gateway` 没有 volume，执行 `reset gateway --volumes --yes` 只会删除容器，不会删除数据卷

## 数据库

`postgres` 启动后会通过 [postgres/init/01-create-databases.sh](./postgres/init/01-create-databases.sh) 初始化开发库和测试库。

为避免将共享 `dev-postgres` 建立在 `pgvector/pgvector:pg17` 这类第三方预构建镜像之上，当前改为基于本地 [postgres/Dockerfile](./postgres/Dockerfile) 构建：底座镜像使用官方 `postgres:17-bookworm`，再通过 Debian 包安装 `postgresql-17-pgvector`。

| 数据库                 | 用户        | 密码        | 用途                    |
| ---------------------- | ----------- | ----------- | ----------------------- |
| `agent_registry`       | `registry`  | `registry`  | registry-server 开发库  |
| `agent_registry_test`  | `registry`  | `registry`  | registry-server 测试库  |
| `agent_ca`             | `ca`        | `ca`        | ca-server 开发库        |
| `agent_ca_test`        | `ca`        | `ca`        | ca-server 测试库        |
| `agent_discovery`      | `discovery` | `discovery` | discovery-server 开发库 |
| `agent_discovery_test` | `discovery` | `discovery` | discovery-server 测试库 |
| `agent_monitor`        | `monitor`   | `monitor`   | monitor-server 开发库   |
| `agent_monitor_test`   | `monitor`   | `monitor`   | monitor-server 测试库   |
| `keycloak`             | `keycloak`  | `keycloak`  | Keycloak 开发库         |

PostgreSQL superuser 固定为：

- 用户：`postgres`
- 密码：`devpass`

## Keycloak

`keycloak` 服务用于 ACPs 多项目的真人 OIDC 联调。它使用 `dev-infra/keycloak/realms/` 下的 realm 定义，并在容器启动后再执行一次开发态 bootstrap，把本地端口、签名算法和黑盒测试 client 收敛到当前约定。

### 连接信息

| 项目 | 值 |
| ---- | --- |
| 管理控制台 / Realm 入口 | `http://localhost:9080` |
| 管理员用户名 | `admin` |
| 管理员密码 | `devpass` |
| 数据库 | `keycloak` |
| 开发 bootstrap 脚本 | [keycloak/bootstrap-dev-keycloak.sh](./keycloak/bootstrap-dev-keycloak.sh) |

### 默认导入的 realm

- `acps-registry`
- `acps-monitor`
- `acps-leader`

这些 realm 的用户、角色、基础 client 配置来自 `dev-infra/keycloak/realms/*.json`。  
`dev-infra` 额外负责做两类“开发态对齐”：

- 确保各 realm 默认签名算法为 `EdDSA`，并存在可用的 `Ed25519` 签名 key
- 根据本地开发端口，回写各 Web client 的 `redirectUris` / `webOrigins`

当前默认对齐的本地 Web 入口为：

- `registry-web` -> `http://localhost:9001`
- `monitor-web` -> `http://localhost:9009`
- `leader-web` -> `http://localhost:9030`

### CLI 与黑盒测试 client 约定

`dev-infra` 当前会额外确保两个 **正式 CLI Device Grant client** 存在：

| Realm | Client ID | 用途 | 关键特征 |
| ---- | ---- | ---- | ---- |
| `acps-registry` | `registry-cli` | `acps-cli auth login` / `acps-cli admin auth login` | public client，开启 Device Authorization Grant，关闭 direct grant，面向 `registry-api` |
| `acps-monitor` | `monitor-cli` | `acps-cli monitor auth login` | public client，开启 Device Authorization Grant，关闭 direct grant，面向 `monitor-api` |

说明：

- `registry-cli` 与 `monitor-cli` 面向真人 CLI 登录，不共享 session，也不跨 realm 复用
- 它们不配置 loopback redirect，不作为 Authorization Code 回跳 client
- `monitor-cli` 会携带 `tenant_id` / `allowed_aics` claim，和 `monitor-web` 一致

### 黑盒测试 client 约定

`dev-infra` 当前会额外确保两个 **测试专用 OIDC client** 存在：

| Realm | Client ID | 用途 | 关键特征 |
| ---- | ---- | ---- | ---- |
| `acps-registry` | `registry-e2e` | `registry-server` 的 `just test e2e` OIDC profile | public client，开启 direct grant，面向 `registry-api` |
| `acps-monitor` | `monitor-e2e` | `monitor-server` 的 `just test e2e` OIDC profile | public client，开启 direct grant，面向 `monitor-api` |
| `acps-leader` | `leader-e2e` | `demo-leader` 的 `just test e2e` OIDC profile | public client，开启 direct grant，面向 `leader-api` |

说明：

- 这里的 `*-e2e` 是 **OIDC client**，不是用户账户
- 它们专门给黑盒联调测试用，不承担浏览器登录入口角色
- 统一命名约定为 `<project>-e2e`
- 每个项目在自己的 realm 内维护自己的 e2e client，不跨 realm 共用
- 后续新增项目时，沿用同样规则，例如 `leader-e2e`

### `monitor-e2e` 的额外约定

`monitor-server` 不只校验 token 的 issuer / audience / role，还依赖 token 中的资源作用域 claim 做查询过滤。因此 `monitor-e2e` 除了 direct grant 基础能力外，还会被补齐与 `monitor-web` 对齐的业务 claim：

| Claim | 来源用户属性 | 用途 |
| ---- | ---- | ---- |
| `tenant_id` | Keycloak user attribute `tenant_id` | 租户级作用域 |
| `allowed_aics` | Keycloak user attribute `allowed_aics` | AIC 级作用域过滤 |

这样 `monitor-server` 的 OIDC 黑盒联调，测到的不只是“能不能验签”，还包括：

- viewer 是否只能看到自己的 `allowed_aics`
- operator 端点是否只允许 operator/admin
- 跨 realm token 是否会被拒绝

### 默认测试用户

#### `acps-monitor`

| 用户名 | 密码 | `monitor-api` 角色 | 备注 |
| ---- | ---- | ---- | ---- |
| `monitor-viewer` | `demo123` | `viewer` | 预置 `tenant_id=tenant-demo`，`allowed_aics=["AIC-DEMO-001","AIC-DEMO-002"]` |
| `monitor-auditor` | `demo123` | `auditor` | 同上 |
| `monitor-operator` | `demo123` | `operator` | 同上 |
| `monitor-admin` | `demo123` | `admin` | 管理员，默认不依赖 AIC 作用域 |

#### `acps-registry`

| 用户名 | 密码 | `registry-api` 角色 |
| ---- | ---- | ---- |
| `registry-client` | `demo123` | `CLIENT` |
| `registry-staff` | `demo123` | `STAFF` |
| `registry-admin` | `demo123` | `ADMIN` |

#### `acps-leader`

| 用户名 | 密码 | `leader-api` 角色 | 备注 |
| ---- | ---- | ---- | ---- |
| `leader-user` | `demo123` | `user` | 供普通真人提交与读取自己 session |
| `leader-operator` | `demo123` | `operator` | 可读取/取消其他用户 session，并签发 elevated stream token |
| `leader-admin` | `demo123` | `admin` | 管理员；能力覆盖 `operator` |

### 常用命令

```bash
# 启动 Keycloak（幂等导入 realm，并补齐 CLI client、e2e client、EdDSA、redirectUris）
./dev-infra.sh up keycloak
./dev-infra.sh wait keycloak

# 查看 Keycloak 运行状态
./dev-infra.sh status keycloak

# 查看 Keycloak 日志
./dev-infra.sh logs keycloak --follow
```

### 与项目测试入口的配合

`registry-server`、`monitor-server` 和 `demo-leader` 的测试入口都已经接入这套 Keycloak：

```bash
cd registry-server
just test e2e -- tests/e2e/test_oidc_keycloak_flow.py

cd ../monitor-server
just test e2e -- tests/e2e/test_oidc_keycloak_flow.py

cd ../demo-leader
just test e2e -- tests/e2e/test_oidc_keycloak_flow.py
```

如果项目启用了本地 OIDC 配置，`just dev bootstrap` / `just test bootstrap` 也会自动拉起 `keycloak` 并等待健康检查完成。

## Redis 逻辑库划分

共享 `dev-redis` 仅暴露原生端口 `6379`。各项目通过 **逻辑库号（URL 路径 `/N`）** 隔离，避免开发/测试数据互相污染：

| 逻辑库 | 项目              | 环境        | 示例 `REDIS_URL`              | 键前缀（应用内）   |
| ------ | ----------------- | ----------- | ----------------------------- | ------------------ |
| `0`    | `mq-auth-server`  | development | `redis://localhost:6379/0`    | `group_acl:`       |
| `1`    | `mq-auth-server`  | testing     | `redis://localhost:6379/1`    | `group_acl:`       |
| `2`    | `monitor-server`  | development | `redis://localhost:6379/2`    | `amp:`             |
| `3`    | `monitor-server`  | testing     | `redis://localhost:6379/3`    | `amp:`             |

新增项目时在此表追加一行，并在对应 `.env.example` / `config/*.toml` 中引用，不要复用已有逻辑库号。

## ClickHouse / MinIO 命名

- ClickHouse 开发库：`amp`；monitor-server 测试库：`amp_test`（由测试套件幂等创建）
- MinIO 开发桶：`amp-access-archive`（`dev-infra.sh up minio` 时自动确保）

## 端口策略

| 范围 | 用途 | 示例 |
| ---- | ---- | ---- |
| **90xx** | ACPs 各应用服务（宿主机直连） | registry `9001`、ca `9003`、discovery `9005`、mq-auth `9007`、monitor `9009`、demo-leader Web `9030` |
| **原生一致** | 与行业标准相同的共享依赖 | PostgreSQL `5432`、Redis `6379`、RabbitMQ `5671`/`15672`、OpenSearch `9200` |
| **19xxx 映射** | 原生端口落在 90xx 的 infra 服务 | Kafka `19092`、MinIO `19000`/`19001`、ClickHouse TCP `19010` |

`kafka`（Redpanda）首次 `up` 时由 [dev-infra.sh](./dev-infra.sh) 幂等创建 `amp.*` 主题。宿主机应用连接 `localhost:19092`；同一 Docker 网络内的容器使用 `dev-redpanda:9092`。

## 底层实现说明

- `dev-infra.sh` 是推荐入口
- [compose.yml](./compose.yml) 是底层实现细节，仍可用于排障和理解编排结构
- 日常开发文档和项目级 `Justfile` 不再直接暴露 `profile` 或 `dev-*` service 名

## ClickHouse

`clickhouse` 服务为 `monitor-server` Access 功能提供列存数据库。

### 连接信息

| 项目                 | 值          |
| -------------------- | ----------- |
| HTTP 端口（查询）    | `8123`      |
| TCP 端口（客户端）   | `19010`（宿主机）→ 容器内 `9000` |
| 数据库               | `amp`       |
| 用户名               | `default`   |
| 密码                 | （空）       |

### 启动方式

```bash
./dev-infra.sh up clickhouse
./dev-infra.sh wait clickhouse
```

### 测试库隔离

`monitor-server` 集成测试使用独立数据库 `amp_test`，避免与开发数据冲突。`amp_test` 由测试套件启动时通过 `store.ensure_access_schema()` 自动创建（IF NOT EXISTS 幂等）。

运行 ClickHouse 相关集成测试：

```bash
cd monitor-server
./dev-infra.sh up redis clickhouse
./dev-infra.sh wait redis clickhouse
uv run pytest tests/integration/test_access_clickhouse_schema.py \
              tests/integration/test_access_dedupe.py \
              tests/integration/test_access_store_query.py \
              tests/integration/test_access_writer_integration.py \
              -v
```

### 手动连接

```bash
# HTTP API
curl "http://localhost:8123/?query=SELECT+1"

# clickhouse-client（容器内）
docker exec -it dev-clickhouse clickhouse-client
```

## Kafka（Message / Access）

`kafka`（Redpanda）服务在首次 `up` 时由 [dev-infra.sh](./dev-infra.sh) 自动创建以下 topic（幂等）：

| Topic | 分区 | 说明 |
| --- | --- | --- |
| `amp.access` | 4 | Access Writer 消费（`LogAppendTime`） |
| `amp.message` | 4 | Message Writer 消费（`LogAppendTime`） |
| `amp.message.dlq` | 1 | Message Writer DLQ 回退 |

验证：

```bash
docker exec dev-redpanda rpk topic list | grep amp.message
docker exec dev-redpanda rpk topic describe amp.message
```

Message 模块联调详见 `monitor-server/docs/dev-runbook-message.md`。
