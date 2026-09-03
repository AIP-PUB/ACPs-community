# mq-auth-server 测试说明

## 目录结构

```
tests/
├── conftest.py                    # 全局 fixtures（所有层共用）
├── unit/                          # 单元测试（无外部依赖）
│   ├── conftest.py
│   ├── test_auth_api.py           # RabbitMQ HTTP Auth 后端路由
│   ├── test_groups_api.py         # Group ACL 管理路由
│   ├── test_group_acl_service.py  # GroupAclService 业务逻辑
│   ├── test_authz_service.py      # AuthorizationService 鉴权逻辑
│   ├── test_validation.py         # AIC / Group ID 格式校验工具函数
│   ├── test_config.py             # TOML 配置加载 & Settings 属性
│   └── test_middleware.py         # RequestIdMiddleware
├── integration/                   # 集成测试（需要 Redis 或 mock HTTP）
│   ├── conftest.py
│   ├── test_group_acl_redis.py    # 真实 Redis 的 GroupAclService 集成测试
│   └── test_rabbitmq_mgmt_client.py  # RabbitMqManagementClient + mock HTTP
└── e2e/                           # 端到端测试（需要已部署的真实服务）
    ├── conftest.py
    ├── test_auth_api_e2e.py       # Auth API 完整 mTLS e2e 验证
    ├── test_groups_api_e2e.py     # Group API CRUD lifecycle e2e 验证
    └── test_rabbitmq_user_id_identity.py  # RabbitMQ validated user-id e2e 验证
```

---

## 推荐测试环境：宿主机本地 + 共享 dev-infra

本项目默认采用“应用本地运行 + 共享 dev-infra 提供 Redis / RabbitMQ”的测试路径。

- 共享依赖来自 `../acps-infra/dev-infra/`
- 本地命令入口统一为顶层 `Justfile`
- 本地服务默认监听 `https://localhost:9007` / `https://localhost:9008`

推荐准备步骤：

```bash
cp .env.example .env
just test bootstrap
```

---

## 运行测试

### 前提条件

```bash
uv sync
```

### 运行单元测试（推荐日常开发，任何环境均可）

```bash
just test unit
```

单元测试**无需外部服务**，全部使用内存 fake / mock，可在任何环境直接运行。

---

### 运行集成测试

集成测试中：

- `test_group_acl_redis.py`：需要真实 Redis；推荐通过 `just test bootstrap` 启动共享 `dev-infra` 后直接运行。
- `test_rabbitmq_mgmt_client.py`：使用 `unittest.mock.AsyncMock` mock httpx，**无需真实 RabbitMQ**，任何环境均可运行。

#### 推荐命令

```bash
just test integration
```

#### 手动运行（宿主机或 CI 环境）

```bash
# 方式一：使用本地 Redis
REDIS_URL=redis://localhost:6379/0 uv run pytest tests/integration/ -v

# 方式二：用 Docker 临时启动 Redis
docker run -d --name test-redis -p 6379:6379 redis:7-alpine
uv run pytest tests/integration/ -v
docker rm -f test-redis

# 仅运行无 Redis 依赖的测试
uv run pytest tests/integration/test_rabbitmq_mgmt_client.py -v
```

Redis 不可用时，`test_group_acl_redis.py` 中的测试将自动 **skip**（不会失败）。

---

### 运行端到端测试

e2e 测试对运行中的真实 mq-auth-server 实例发起 mTLS HTTP 请求，验证完整鉴权流程。

#### 本地 e2e（推荐）

先确保本地服务已经启动：

```bash
just dev start
```

若 `certs/` 下缺少证书，development 模式会自动生成：

- 本地开发 CA：`certs/acps-root-ca.pem`
- 服务端证书：`certs/server.pem`
- 客户端证书：`certs/client.pem`

也可以替换为由 ACPs CA 颁发的真实 Agent 证书：

```bash
# 确认服务已启动
python -m app.core.health_probe --url https://localhost:9007/health

# 使用 development 自动生成的 Leader 证书进行本地 e2e
export GROUP_API_URL=https://localhost:9007
export AUTH_API_URL=https://localhost:9008
export TLS_CERT_FILE=certs/client.pem
export TLS_KEY_FILE=certs/client.key
export TLS_CA_CERT_FILE=certs/acps-root-ca.pem
export E2E_TEST_AIC=1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
export E2E_LEADER_AIC=1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
export E2E_MEMBER_AIC=<Member AIC>       # 被操作的成员 AIC
export AMQP_BROKER_HOST=localhost
export AMQP_BROKER_PORT=5671
export AMQP_BROKER_VHOST=acps

just test e2e
```

说明：`just test e2e` 会把临时 Auth API 默认监听在 `https://localhost:9008`，
以匹配本地 RabbitMQ dev-infra 中固定的 HTTP auth backend 回调地址。
如果本机已有其它进程占用 `9008`（例如后台运行的 `mq-auth-server`），请先停止它。

#### 在已部署的生产/预发布环境

```bash
export GROUP_API_URL=https://your-mq-auth-host:9007
export AUTH_API_URL=https://your-mq-auth-host:9008
export TLS_CERT_FILE=/path/to/client.pem
export TLS_KEY_FILE=/path/to/client-key.pem
export TLS_CA_CERT_FILE=/path/to/ca.pem
export E2E_TEST_AIC=1.2.156.3088.1.1.XXXX.YYYYYY.ZZZZZZ.AAAA
export E2E_LEADER_AIC=1.2.156.3088.1.1.AAAA.BBBBBB.CCCCCC.DDDD
export E2E_MEMBER_AIC=1.2.156.3088.1.1.EEEE.FFFFFF.GGGGGG.HHHH
export AMQP_BROKER_HOST=your-rabbitmq-host
export AMQP_BROKER_PORT=5671
export AMQP_BROKER_VHOST=acps

uv run pytest tests/e2e/ -v
```

e2e 所需环境变量未配置时，测试将自动 **skip**（不会失败）。

---

### 运行全部测试

```bash
just test
```

---

## 全局 Fixtures（`tests/conftest.py`）

| Fixture / Helper               | 用途                                                                         |
| ------------------------------ | ---------------------------------------------------------------------------- |
| `VALID_LEADER_AIC`             | 合法 Leader AIC 常量（用于构造测试数据）                                     |
| `VALID_MEMBER_AIC`             | 合法 Member AIC 常量                                                         |
| `VALID_OTHER_AIC`              | 第三方合法 AIC 常量                                                          |
| `VALID_GROUP_ID`               | 合法 Group ID 常量                                                           |
| `InMemoryGroupAclStore`        | 实现 `GroupAclStore` 协议的内存 fake，支持 `unavailable` 标志模拟 Redis 故障 |
| `FakeRabbitMqManagementClient` | 记录 `(username, reason)` 调用的 fake 客户端                                 |
| `build_test_client(...)`       | 构建 `TestClient` + 注入依赖，返回 `(client, service, store, mgmt_client)`   |
| `group_cache_key` fixture      | 返回 `GroupAclService.build_cache_key(VALID_LEADER_AIC, VALID_GROUP_ID)`     |

---

## 测试分层设计

### 单元测试

- **原则**：完全隔离，不依赖任何外部服务（Redis、RabbitMQ、文件系统）
- **工具**：`InMemoryGroupAclStore`、`FakeRabbitMqManagementClient`、FastAPI `TestClient`
- **覆盖范围**：HTTP 路由 → 请求验证 → 状态码；服务层 → 业务逻辑 → 正常 / 异常路径；工具函数 → 格式校验 / 数据解析

### 集成测试

- **原则**：测试真实外部依赖，但仍可在 CI 中运行（不可用时 skip）
- `test_group_acl_redis.py`：使用真实 Redis 验证持久化、TTL、缓存同步
- `test_rabbitmq_mgmt_client.py`：使用 `AsyncMock` 注入 httpx 客户端，验证 HTTP 交互协议（URL 编码、Header、状态码处理）

### 端到端测试

- **原则**：黑盒验证，模拟 RabbitMQ 和 Leader Agent 的真实调用
- 需要 mTLS 证书 + 已部署的服务实例
- 验证内容：证书鉴权、vhost 权限、inbox 队列权限、group 生命周期、RabbitMQ validated user-id
- development 环境可直接复用自动生成的开发 Leader 证书

---

## 环境变量汇总

| 变量               | 测试层      | 说明                                            |
| ------------------ | ----------- | ----------------------------------------------- |
| `REDIS_URL`        | integration | Redis 连接 URL，默认 `redis://localhost:6379/0` |
| `GROUP_API_URL`    | e2e         | Group API 地址，默认 `https://localhost:9007`   |
| `AUTH_API_URL`     | e2e         | Auth API 地址，默认 `https://localhost:9008`    |
| `TLS_CERT_FILE`    | e2e         | mTLS 客户端证书路径（PEM）                      |
| `TLS_KEY_FILE`     | e2e         | mTLS 客户端私钥路径（PEM）                      |
| `TLS_CA_CERT_FILE` | e2e         | CA 证书路径（验证服务端）                       |
| `E2E_TEST_AIC`     | e2e         | Auth API 测试用的合法 AIC                       |
| `E2E_LEADER_AIC`   | e2e         | Group API 测试用 Leader AIC                     |
| `E2E_MEMBER_AIC`   | e2e         | Group API 测试用 Member AIC                     |
| `E2E_FORGED_AIC`   | e2e         | 可选；RabbitMQ forged `user_id` 测试用伪造 AIC  |
| `AMQP_BROKER_HOST` | e2e         | RabbitMQ AMQPS 主机，默认 `localhost`           |
| `AMQP_BROKER_PORT` | e2e         | RabbitMQ AMQPS 端口，默认 `5671`                |
| `AMQP_BROKER_VHOST` | e2e         | RabbitMQ vhost，默认 `acps`                     |

---

## 覆盖率

运行覆盖率报告：

```bash
uv run pytest tests/unit/ --cov=app --cov-report=term-missing
```
