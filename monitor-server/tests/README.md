# 测试目录说明

## 测试分层

| 目录 | 类型 | 特点 |
|------|------|------|
| `unit/` | 单元测试 | mock 外部依赖（DB、Kafka），运行极快，无需基础设施 |
| `integration/` | 集成测试 | 使用真实 PostgreSQL（`agent_monitor_test`），事务回滚隔离 |
| `e2e/` | 端到端测试 | 通过 HTTP Query API 验证，需要真实 DB + Kafka |

## 运行方式

```bash
# 初始化测试环境（首次）
just test bootstrap

# 运行全部测试
just test all

# 只运行单元测试（无需基础设施）
just test unit

# 只运行集成测试
just test integration

# 只运行端到端测试
just test e2e

# 只运行 Keycloak OIDC 黑盒联调测试
just test e2e -- tests/e2e/test_oidc_keycloak_flow.py

# 覆盖率统计
just test coverage
```

## 前置条件

- 集成测试和 E2E 测试需要 dev-infra 提供的 PostgreSQL 和 Kafka：
  ```bash
  just infra up postgres kafka
  just infra wait postgres kafka
  ```
- `just test e2e -- tests/e2e/test_oidc_keycloak_flow.py` 会额外走 OIDC profile；命令会自动执行 `just infra up keycloak && just infra wait keycloak`。
- 数据库迁移需执行 `just prep migrate test`。

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TEST_DATABASE_URL` | 测试数据库连接串 | `postgresql+asyncpg://monitor:monitor@localhost:5432/agent_monitor_test` |
| `APP_ENV` | 应用环境（conftest 自动设为 testing） | `testing` |
