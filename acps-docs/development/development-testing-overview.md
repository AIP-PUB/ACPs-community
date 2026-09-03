# ACPs 开发与测试总览

## 1. 文档目的与范围

本文面向 ACPs 各项目的开发者，说明本地开发、环境准备、服务启动、测试分层和质量检查的统一方法。它综合了各项目 `README.md`、`tests/README.md` 与 `Justfile` 中的约定，重点解释基于 `just` 的命令体系，而不是替代每个仓库自己的详细说明。

如果某个命令在本文和项目 `Justfile` 中出现差异，以当前项目的 `just help` 和 `Justfile` 为准。

## 2. 适用项目

本文主要覆盖这些带有统一开发测试入口的 Python 项目：

| 项目 | 类型 | 本地开发入口 | 主要外部依赖 |
| --- | --- | --- | --- |
| `registry-server` | 核心服务 | `just dev start` | PostgreSQL、本地 mTLS 证书 |
| `ca-server` | 核心服务 | `just dev start` | PostgreSQL、CA 证书材料 |
| `discovery-server` | 核心服务 | `just dev start` | PostgreSQL / pgvector、embedding / LLM 配置 |
| `mq-auth-server` | 核心服务 | `just dev start` | Redis、RabbitMQ、本地 mTLS 证书 |
| `demo-partner` | 示例应用 | `just dev start` | RabbitMQ、LLM 配置、本地 mTLS 证书 |
| `demo-leader` | 示例应用 | `just dev start` | RabbitMQ、LLM 配置、`demo-partner` |
| `acps-cli` | CLI / 联调工具 | `just dev bootstrap` | 本地 dev-infra、可选后端服务（本仓无长期运行服务） |

`acps-sdk` 当前没有统一 `Justfile`，开发方式仍以 `uv sync`、`uv build` 等 Python 包常规命令为主。

## 3. 先记住一套统一模型

多数服务类项目的 `Justfile` 都按同一组 domain 组织命令：

| domain | 用途 | 常用命令 |
| --- | --- | --- |
| `help` | 查看命令说明 | `just`、`just help` |
| `infra` | 管理共享开发依赖 | `just infra up postgres`、`just infra status`、`just infra logs rabbitmq --follow` |
| `prep` | 准备或修复本仓环境 | `just prep env`、`just prep sync`、`just prep hooks`、`just prep certs`、`just prep migrate test` |
| `dev` | 开发环境检查与本地应用生命周期 | `just dev check`、`just dev start`、`just dev start fg`、`just dev stop`（可选 `just dev bootstrap` 只预热） |
| `test` | 测试入口 | `just test check`、`just test bootstrap`、`just test unit`、`just test integration`、`just test e2e`、`just test` |
| `qa` | 格式化、lint、类型和审计 | `just qa`、`just qa fmt`、`just qa type`、`just qa lint` |
| `package` | wheel 运行包构建 | `just package check`、`just package bootstrap`、`just package wheel` |

可以把它理解成一条从轻到重的链路：

```text
infra -> prep -> dev -> test -> qa
```

`infra` 准备共享依赖，`prep` 准备本仓环境，`dev check` 做只读检查，`dev start/stop` 管理本地服务，`test` 执行分层验证，`qa` 做提交前质量检查。`package` 用于构建运行包，开发时只在需要验证制品时使用。

## 4. 第一次进入仓库怎么做

服务类项目推荐从项目根目录执行：

```bash
cp .env.example .env
# 按 README 修改 .env 中的数据库、token、证书、LLM、RabbitMQ 等配置

just dev start
```

`just dev start` 启动前会自动完成环境准备（也可单独执行 `just dev bootstrap` 只预热、不启动），通常包括：

- 启动本仓需要的共享依赖，例如 PostgreSQL、Redis、RabbitMQ。
- 生成 `.env`，如果文件已经存在则保留。
- 通过 `uv` 安装 managed Python 3.14，并同步 `.venv/`。
- 安装或更新 pre-commit / commit-msg hooks。
- 按需从 `../acps-infra/dev-infra/dev-cert.sh` 准备本地开发证书。
- 对有数据库的服务执行开发库迁移。

`acps-cli` 也使用统一的 `dev` domain，但它没有本地服务运行时。第一次进入 `acps-cli` 时使用：

```bash
just dev bootstrap
```

它会准备 CLI 自身环境，并启动共享 `postgres`、`redis`、`rabbitmq` 依赖，供后续测试夹具或手工联调使用；日常检查使用 `just dev check`，而不是 `just dev start`。

## 5. `infra`：共享依赖只从一个入口管理

各服务不直接操作 `acps-infra/dev-infra` 下面的 compose 文件，而是通过本仓 `Justfile` 代理：

```bash
just infra status
just infra up postgres
just infra up redis rabbitmq
just infra wait redis rabbitmq
just infra logs rabbitmq --follow
just infra down
```

不同项目默认需要的共享依赖不同：

| 项目 | `just dev start` / `bootstrap` 默认依赖 |
| --- | --- |
| `registry-server`、`ca-server`、`discovery-server` | `postgres` |
| `mq-auth-server` | `redis rabbitmq` |
| `demo-partner`、`demo-leader` | `rabbitmq` |
| `acps-cli` | `postgres redis rabbitmq` |

如果只是改纯业务逻辑并跑单元测试，通常不需要先启动共享依赖。只要进入集成测试、e2e、本地服务启动或 CLI 联调，再按需启动即可。

## 6. `prep`：局部准备和局部修复

`prep` 是环境准备的可拆分入口，适合在某一块坏掉时单独修复：

| 命令 | 作用 |
| --- | --- |
| `just prep env` | 缺失时根据 `.env.example` 生成 `.env` |
| `just prep sync` | 使用 `uv` 同步 Python、依赖和 `.venv/` |
| `just prep hooks` | 安装或更新 Git hooks |
| `just prep certs` | 准备本地开发证书或 CA 材料 |
| `just prep certs reset` | 目前主要用于 `mq-auth-server`，清理后重新签发开发证书 |
| `just prep migrate app` | 对开发库执行迁移 |
| `just prep migrate test` | 对测试库执行迁移 |
| `just prep seed app` / `test` | `discovery-server` 专用，导入 demo ACS 样本 |
| `just prep reseed app` / `test` | `discovery-server` 专用，清空后重新导入样本 |
| `just prep sync-embedding-dimension app` / `test` | `discovery-server` 专用，同步 embedding 维度 |

几个容易混淆的点：

- `.env` 已存在时，`prep env` 不会覆盖现有 `.env` 文件。
- `prep sync` 会按项目锁定版本同步环境，多数项目固定 Python 3.14。
- `prep certs` 生成的是本地开发材料，不应提交到 Git。
- `mq-auth-server`、`demo-partner`、`demo-leader` 没有业务数据库，`prep migrate` 对它们是 no-op 或不存在。
- 有数据库的服务应区分开发库和测试库，测试库不要指向开发库。

## 7. `dev check`：先检查，再启动或联调

`just dev check` 是只读检查入口，通常不会修复环境。它的价值是把“为什么服务启动不了、测试跑不起来”提前讲清楚。

常见检查包括：

- `python3`、`uv`、`just`、`openssl` 是否可用。
- `.env` 或 `acps-cli.toml` 是否存在。
- `uv sync --check --locked` 是否通过。
- 共享 `dev-infra` 是否可用。
- PostgreSQL、Redis、RabbitMQ 是否 running / healthy。
- 本地 mTLS 证书、CA 材料是否齐备。
- `acps-cli` 还会检查 registry、ca、discovery、mq 目标地址是否可达。

推荐习惯：

```bash
just dev check
```

在手工联调前先跑一次。如果它失败，优先按输出修复环境，而不是直接进入 `uv run pytest` 或手工启动服务。

## 8. `dev`：本地服务生命周期

服务类项目的常用本地启动命令是：

```bash
just dev start
```

`just dev start` 默认后台启动，并会按需自动准备环境。常用动作如下：

| 命令 | 说明 |
| --- | --- |
| `just dev start` | 后台启动服务，日志写入 `logs/` |
| `just dev start bg` | 显式后台启动 |
| `just dev start fg` | 前台启动，适合调试 |
| `just dev stop` | 停止后台实例 |
| `just dev status` | 查看后台实例状态 |
| `just dev logs` | 查看最近日志 |
| `just dev logs follow` | 持续跟随日志 |
| `just dev restart` | 重启后台实例 |

各服务默认端口：

| 项目 | 默认地址 |
| --- | --- |
| `registry-server` | `http://localhost:9001`，mTLS 平面 `https://localhost:9002` |
| `ca-server` | `http://localhost:9003` |
| `discovery-server` | `http://localhost:9005` |
| `mq-auth-server` | Group API `https://localhost:9007`，Auth API `https://localhost:9008` |
| `demo-leader` | Web UI `http://localhost:9030`，Leader API `http://localhost:9031` |
| `demo-partner` | 多 Agent 端口，默认范围常见为 `9021-9025` |

前台启动适合看热重载和异常栈；后台启动适合配合 CLI、e2e 或浏览器手工联调。

## 9. `test`：测试分层和边界

各项目的标准测试入口是：

```bash
just test bootstrap
just test unit
just test integration
just test e2e
just test
```

`just test` 默认执行 `all`，也就是按项目约定顺序跑完整测试套件。多数项目是：

```text
unit -> integration -> e2e
```

### 9.1. 单元测试

`unit` 是最轻量的日常入口，原则上不依赖真实数据库、真实网络服务或兄弟进程。

```bash
just test unit
```

修改纯函数、配置解析、业务规则、命令参数解析时，优先跑它。大多数项目也提供覆盖率入口：

```bash
just test coverage
```

### 9.2. 集成测试

`integration` 用于验证本仓代码与本仓需要的真实依赖之间的集成。例如：

- `registry-server`、`ca-server`、`discovery-server` 使用测试数据库。
- `mq-auth-server` 的部分集成测试使用真实 Redis。
- `demo-partner`、`demo-leader` 的集成测试会涉及真实 LLM 或真实 Partner 服务。
- `acps-cli` 的集成测试关注 CLI 参数、配置、输出和单服务命令契约。

标准入口是：

```bash
just test integration
```

如果测试环境未准备好，先执行：

```bash
just test bootstrap
```

### 9.3. 端到端测试

`e2e` 是黑盒验证，但“黑盒”的范围分两类：

- 在 server 仓库里，`e2e` 主要验证本服务自闭环行为。很多仓库会由 Justfile 临时拉起本服务测试实例，再注入 `TEST_E2E_BASE_URL`。
- 在 `acps-cli` 里，`e2e` 是跨服务联调主入口，用来验证 registry、ca、discovery、mq 等真实协作链路。

标准入口是：

```bash
just test e2e
```

最重要的边界规则：

> 如果一个测试必须同时验证多个兄弟服务之间的真实交互，它应优先放到 `acps-cli/tests/e2e/`，而不是继续塞进某个 server 仓库的 `tests/e2e/`。

各 server 仓库自己的 `integration` / `e2e` 应尽量保持本服务自闭环：外部兄弟交互用 fake peer、stub transport、contract fixture 或受管临时实例表达。

### 9.4. `acps-cli` 的联调测试模式

`acps-cli` 是真实跨服务联调的主入口仓库。推荐流程：

```bash
cd acps-cli
just test bootstrap
just test e2e
```

如果使用默认本地地址，并且默认端口没有服务监听，`acps-cli` 的测试夹具会按需托管启动所需兄弟服务。若你已经手工启动了默认端口服务，测试会复用它们。若你通过环境变量改成了自定义地址，则测试认为目标环境由你自己托管，不会自动回退到默认端口。


## 10. `qa`：提交前质量检查

常用入口：

```bash
just qa
```

多数项目的 `just qa` 默认执行：

```text
fix -> precommit
```

也就是先格式化并自动修复 Ruff 问题，再运行 pre-commit。部分服务还把 `audit` 加入默认 all，或者提供 `lint` / `type` / `security` 等只读子入口，供你按需组合成门禁：

| 命令 | 说明 |
| --- | --- |
| `just qa fmt` | 仅格式化 |
| `just qa fix` | 格式化并执行 `ruff check --fix` |
| `just qa lint` | 只读 lint，部分项目提供 |
| `just qa type` | mypy 类型检查的兼容入口，部分项目提供 |
| `just qa type-app` | 检查业务代码 |
| `just qa type-tests` | 检查测试代码 |
| `just qa security` | Bandit 安全扫描，部分项目提供 |
| `just qa audit` | pip-audit 依赖审计，部分项目提供 |
| `just qa precommit` | 运行 `pre-commit run --all-files` |

日常开发建议：

- 小改动：`just test unit` 后跑 `just qa`。
- 触及数据库、配置、证书或服务边界：补跑 `just test integration`。
- 触及 HTTP API、进程启动、跨服务协作：补跑 `just test e2e` 或转到 `acps-cli` 跑联调 e2e。

## 11. 各项目差异速查

| 项目 | 需要特别注意的点 |
| --- | --- |
| `registry-server` | 双平面运行，`9001` public API、`9002` mTLS API；本仓测试不承载真实跨服务联调；只读门禁请组合 `qa lint` / `qa type-app` / `qa type-tests` / `qa security` |
| `ca-server` | 本地开发证书材料来自 `../acps-infra/dev-infra`；默认可 mock `registry-server`；跨服务证书链路归 `acps-cli/tests/e2e/` |
| `discovery-server` | 有 CPU / GPU 运行模式；`prep seed` / `reseed` 管理样本 ACS；标准 integration / e2e 未指定模式时会依次跑 CPU 和 GPU |
| `mq-auth-server` | 无数据库；依赖 Redis、RabbitMQ 和 mTLS；`prep migrate` 显式 skip；e2e 会使用临时本地 HTTPS listener |
| `demo-partner` | 多 Agent、多端口；集成测试依赖 LLM 配置；e2e 验证运行中的 Partner API 和任务状态机 |
| `demo-leader` | Leader API + Web UI 双进程；集成测试通常需要先启动 `demo-partner`；`test integration` 默认文件级并行 |
| `acps-cli` | 没有 `just dev start`；是跨服务联调 e2e 的主入口；默认地址缺服务时测试夹具可自动托管兄弟服务 |
| `acps-sdk` | 当前没有统一 `Justfile`；使用 `uv sync`、`uv build`、`uv publish` 等 Python 包命令 |
