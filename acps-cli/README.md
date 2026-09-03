# acps-cli

`acps-cli` 是 ACPs 的统一命令行工具集，提供 Registry、CA、Discovery、MQ、Monitor 五类客户端能力，面向开发联调、调试验证、安装层 provision 引导和日常运维脚本使用。

## 1. 概述

### 1.1. 项目定位

本项目是纯 CLI 工具，不启动 FastAPI、数据库或消息队列服务；无论是本地开发还是通用打包部署，都需要通过外部后端服务完成联调。

主要能力包括：

- Registry 用户端：登录、登出、修改密码、注册或更新 Agent、提交审核、获取 EAB、同步 ACS
- Registry 管理端：登录、登出、修改密码、审核、启用/禁用 Agent、重置指定用户密码
- CA 客户端：申请、续期、吊销证书，轮转 ACME 账户密钥，检查证书状态
- Discovery 客户端：触发 DSP 同步、执行查询、检查服务健康状态
- MQ 客户端：检查 mq-auth-server 健康状态，管理 Group ACL，并探测 Auth API allow / deny 决策
- Monitor 客户端：查询 monitor-server 的 heartbeat、metrics、access、message、system、audit 读模型

### 1.2. 命令与文档

当前统一入口是 `acps-cli`，主要命令域如下：

- `acps-cli auth` / `agent` / `entity`：Registry 用户侧操作
- `acps-cli cert`：证书生命周期与 EAB 相关操作
- `acps-cli discover`：Discovery 查询与状态查看
- `acps-cli admin registry ...`：Registry 管理面命令
- `acps-cli admin ca ...`：CA 管理面命令
- `acps-cli admin discovery ...`：Discovery 管理面命令
- `acps-cli admin mq ...`：mq-auth-server 管理面命令
- `acps-cli monitor ...`：monitor-server 查询命令

所有 CLI 均支持：

- `--config PATH`：显式指定 `acps-cli.toml`
- `--verbose`：输出 DEBUG 日志，默认输出 INFO 及以上日志

其中需要按服务域临时覆盖地址时，可在对应命令组上使用 `--server-url`；Registry 相关命令额外支持 `--timeout`。

## 2. 开发

### 2.1. 开发环境与前置条件

本项目通常与以下兄弟仓库一起组建开发环境：

```text
acps/
  acps-infra/
  registry-server/
  ca-server/
  discovery-server/
  mq-auth-server/
  monitor-server/
  acps-cli/
```

- `uv`（[安装文档](https://docs.astral.sh/uv/getting-started/installation/)）—— `uv` 会根据 `.python-version` 自动下载并管理 Python 3.14，无需手动安装 Python
- `just`（[官方安装文档](https://just.systems/man/en/packages.html)）
- Docker Desktop（仅用于启动 `acps-infra/dev-infra` 依赖）
- 同级目录已存在 `../acps-infra/` 与五个后端兄弟仓库

补充说明：本仓开发统一使用 Python `3.14`，并通过仓库根目录 `.python-version` 固定版本请求；`just dev bootstrap` 会通过 `uv` 强制使用 managed Python `3.14` 创建与同步 `.venv`。

### 2.2. 建立 CLI 开发环境

`acps-cli` 是纯 CLI 工具，没有本地长期运行的服务进程；公开开发主路径使用 `just dev check` 与 `just dev bootstrap`，负责准备 CLI 自身运行环境和 shared `dev-infra` 依赖。首次进入仓库时执行一次即可；`just test integration` / `just test e2e` 也会按需补齐测试前置条件。

```bash
just dev bootstrap   # 建立 CLI 开发环境与 shared 依赖
```

`just dev bootstrap` 会执行 `infra up postgres/redis/rabbitmq + prep env + prep sync + prep hooks` 等操作。

开发配置约定：

- 仓库根目录已提供 `acps-cli.toml` 作为本地开发默认配置；请根据实际情况调整其中的服务地址，确保它们能联通后端服务实例。
- `registry.auth.mode = "local" | "oidc"`：`local` 仍使用用户名/密码；`oidc` 固定走 Device Authorization Grant，不再接受 `--username/--password`
- `monitor.auth.mode = "none" | "oidc"`：`none` 仅做开发态匿名查询；`oidc` 需要先执行 `monitor auth login`
- 若使用 OIDC，请在 `acps-cli.toml` 或环境变量中提供 `issuer` / `client_id`，并按服务域分别登录：`auth login` / `admin auth login` / `monitor auth login`
- 如需给 auto-register 提供默认显示名称和组织名，可在 `acps-cli.toml` 的 `[registry]` 中设置 `display_name` / `org_name`
- `just prep env` 仍可用于生成 `.env` 占位文件，供未来的环境变量覆盖场景使用
- 具体命令树可通过 `acps-cli --help`、`acps-cli cert --help`、`acps-cli admin --help` 查看

如果当前只是在修改 CLI 本体、查看帮助或调试单个命令，不需要先启动五个兄弟服务；只有在需要真实联调时，才进入下一节。

### 2.3. 启动本地联调环境

当你需要手工联调 Registry、CA、Discovery、MQ、Monitor 五个后端服务时，先在 `acps-cli` 仓库执行：

```bash
just dev bootstrap
```

然后在**独立终端**中分别启动五个后端服务。请自行配置这些应用服务，确保它们能联通对方，以下为命令示例：

```bash
# 终端 1
cd ../registry-server && APP_ENV=development CA_SERVER_MOCK=false just dev start

# 终端 2
cd ../ca-server && APP_ENV=development REGISTRY_SERVER_MOCK=false just dev start

# 终端 3
cd ../discovery-server && just dev start

# 终端 4
cd ../mq-auth-server && just dev start

# 终端 5
cd ../monitor-server && APP_ENV=development DATABASE_URL=postgresql+asyncpg://monitor:monitor@localhost:5432/agent_monitor_test TEST_DATABASE_URL=postgresql+asyncpg://monitor:monitor@localhost:5432/agent_monitor_test REDIS_URL=redis://localhost:6379/3 CLICKHOUSE_DATABASE=amp_test OPENSEARCH_HOSTS=http://localhost:9200 OPENSEARCH_VERIFY_CERTS=false just dev start
```

联调时建议注意以下几点：

- `registry-server` 联调时建议使用 `APP_ENV=development CA_SERVER_MOCK=false`，以启用真实 CA 吊销通知链路。
- `registry-server` 与 `ca-server` 需要使用同一个 `REGISTRY_SERVER_INTERNAL_API_TOKEN`，上面的示例统一使用 `local-registry-server-internal-api-token`。
- `ca-server` 的 `.env` 需设置非空的 `CA_SERVER_ADMIN_API_TOKEN`（`.env.example` 本地默认值为 `local-ca-admin-token`），否则 CRL 刷新等 admin 端点会返回 authentication not configured；CLI 联调/测试默认也使用该本地默认值。
- `mq-auth-server` 的 `9007` / `9008` 都要求 mTLS；本地联调时请确保 `../mq-auth-server/certs/` 或你自己的 `[mq]` 客户端证书配置已经就绪，`just dev check` 会把 MQ 与其它三个服务一起检查。
- `monitor-server` 若需要和 monitor live integration / e2e 共用同一套本地环境，建议直接复用 `agent_monitor_test` / Redis DB 3 / ClickHouse `amp_test` / 本地 OpenSearch，并在 `just dev start` 前显式覆盖这些环境变量；启动时会在缺失时生成 `config/audit_keys.json`（mock 审计验签密钥）。

五个服务启动完成后，回到 `acps-cli` 仓库执行：

```bash
just dev check
```

如果 `check` 失败，它会明确告诉你缺的是哪个 HTTP 服务，并打印对应仓库的启动命令。

本地常用地址：

| 服务                     | 地址                     | 说明                                 |
| ------------------------ | ------------------------ | ------------------------------------ |
| registry-server          | `http://localhost:9001`  | `acps-cli.toml` `[registry]` 直连    |
| ca-server                | `http://localhost:9003`  | `acps-cli.toml` `[ca]` 直连          |
| discovery-server         | `http://localhost:9005`  | `acps-cli.toml` `[discovery]` 直连   |
| mq-auth-server Group API | `https://localhost:9007` | `acps-cli.toml` `[mq].group_api_url` |
| mq-auth-server Auth API  | `https://localhost:9008` | `acps-cli.toml` `[mq].auth_api_url`  |
| monitor-server           | `http://localhost:9009`  | `acps-cli.toml` `[monitor].base_url` |

联调命令示例：

```bash
uv run acps-cli auth login --username alice --password 'S3cret!'
uv run acps-cli agent save --acs-file acs.json
uv run acps-cli cert status --aic <AIC>
uv run acps-cli discover query "北京旅游推荐"
uv run acps-cli monitor heartbeat liveness <AIC>
```

若 `registry-server` / `monitor-server` 已启用 OIDC，则对应登录示例改为：

```bash
uv run acps-cli auth login
uv run acps-cli admin auth login
uv run acps-cli monitor auth login
uv run acps-cli auth status --json
uv run acps-cli monitor auth status --json
```

补充说明：

- `discover query` 默认直接输出 JSON，不需要再追加 `--json`。
- 如果要做稳定的 discovery 可见性 gate，建议改用 `discover query --type filtered --filter-json ...`。

例如：

```bash
uv run acps-cli discover query \
  --type filtered \
  --filter-json '{"conditions":[{"field":"aic","op":"eq","value":"<partner-aic>"},{"field":"active","op":"eq","value":true}]}'
```

### 2.4. 日常开发命令

常用开发命令：

```bash
# 帮助
just help                 # 输出命令总览

# 开发（CLI 无长期运行服务）
just dev bootstrap        # 建立 CLI 开发环境与 shared 依赖
just dev check            # 只读检查联调环境

# 打包
just package check        # 只读检查打包前置条件
just package bootstrap    # 按需补齐打包前置条件（不生成发布物）
just package wheel        # 构建在线运行包

# 质量
just qa                   # 显示 qa 帮助
just qa precommit         # 执行 pre-commit 全量门禁
just qa pip-audit         # 依赖漏洞审计
```

## 3. 测试

`acps-cli` 是多个 server 仓库之外，唯一承载真实跨服务联调 e2e 的仓库。

### 3.1. 测试职责与分层

测试边界约定如下：

- `registry-server`、`ca-server`、`discovery-server`、`mq-auth-server`、`monitor-server` 各自负责本服务的 `unit`、`integration` 和 self-contained `e2e`。
- 只要测试需要同时验证多个兄弟服务的真实交互，就应该归到 `acps-cli/tests/e2e/`，而不是继续留在 server 仓库。
- 典型联调场景包括：ATR / EAB / 证书申请主链路、证书生命周期状态传播、discovery snapshot / incremental / webhook / runtime 协作，以及 mq group / auth-probe 工作流。
- 少数明确标注为未来工作的场景允许保留 `skip`；除此之外，联调测试的目标是通过自动准备前置条件实现尽可能全绿。

| 层级       | 命令                    | 说明                                                                                    |
| ---------- | ----------------------- | --------------------------------------------------------------------------------------- |
| 单元测试   | `just test unit`        | 纯 mock，无外部服务依赖                                                                 |
| 集成测试   | `just test integration` | 以 CLI 自身参数、配置、输出和单服务命令契约验证为主；默认本地地址缺服务时由夹具自动托管 |
| 端到端测试 | `just test e2e`         | 真实跨服务联调主入口；默认本地地址缺服务时由夹具自动托管                                |
| 全量测试   | `just test`             | 运行全部测试                                                                            |

职责划分建议：

- `just test integration`：侧重 CLI 命令面、配置解析、输出格式、单服务命令契约。
- `just test e2e`：侧重跨服务用户旅程、真实状态传播、联调拓扑协作。
- `just test`：顺序执行 CLI 的 unit / integration / e2e；默认本地地址缺服务时由测试夹具补齐。

与各 server 仓库的对应关系：

- `registry-server`、`ca-server`、`discovery-server`、`mq-auth-server`、`monitor-server` 各自的 `integration` 与 `e2e` 负责本服务自闭环验证。
- `acps-cli/tests/e2e/` 负责把多个服务串起来做真实联调回归。
- 如果某个场景必须同时启动多个兄弟服务，它应优先进入 `acps-cli/tests/e2e/`，而不是回流到 server 仓库。

### 3.2. 建立测试环境

测试入口统一使用：

```bash
just test check      # 只读检查测试环境
just test bootstrap   # 手动预热测试环境；integration / e2e / all 会按需隐含执行
```

`just test bootstrap` 与 `just dev bootstrap` 复用同一段共享准备逻辑，但语义上专门面向测试环境准备。`just test unit` 无需 bootstrap；integration / e2e / all 会在需要时自动补齐前置条件。

测试环境有两种使用方式：

- 自动托管模式：测试使用默认本地地址，即 `REGISTRY_URL=http://localhost:9001`、`CA_URL=http://localhost:9003`、`DISCO_URL=http://localhost:9005`、`MQ_GROUP_API_URL=https://localhost:9007`、`MQ_AUTH_API_URL=https://localhost:9008`、`MONITOR_BASE_URL=http://localhost:9009`；当这些地址在测试启动时不可达，`tests/_local_services.py` 才会按测试模式受管启动所需兄弟服务。
- 手工托管模式：测试启动时，如果你配置的目标地址已经可达，无论它们是不是默认端口，测试都会直接复用这些已运行服务，不会再拉起新的 sibling 进程。
- 自定义地址模式：如果你把上述环境变量改成了非默认地址或端口，测试会把它视为“由你自己托管的目标环境”；此时即使目标服务不可达，夹具也不会代你启动，而是直接报错，要求你先把这些自定义目标服务启动好。

区分规则可以收敛成一句话：先看“测试要访问的 base_url 是不是默认本地地址”，再看“这个地址在测试开始时是否已经可达”。只有“默认本地地址 + 当前不可达”这一种组合，才会进入自动托管。

补充说明：当前自动托管并不是“随机端口模式”。`tests/_local_services.py` 受管启动的仍然是固定默认端口 `9001/9003/9005/9007/9008/9009`，只是由 pytest 进程代替你手工执行各兄弟仓库的 bootstrap/start。

如果你选择手工托管服务，建议先执行：

```bash
just dev check
```

`just dev check` 会把 registry / ca / discovery / mq / monitor 五个服务一起检查；对 MQ 来说，它会优先使用 `[mq]` 配置、`bootstrap-artifacts/`，或本地 `../mq-auth-server/certs/` 中的 probe 证书材料。

为了让 `integration` / `e2e` / `just test` 更稳定：

- `registry-server` 联调时建议使用 `APP_ENV=development CA_SERVER_MOCK=false`，以启用真实 CA 吊销通知链路。
- `registry-server` 与 `ca-server` 需要使用同一个 `REGISTRY_SERVER_INTERNAL_API_TOKEN`。
- `mq-auth-server` 的 `9007` / `9008` 要求 mTLS；如果测试涉及 `admin mq ...`，需要提前准备可用客户端证书。
- `monitor-server` 相关的 live integration / e2e 会依赖 Kafka、VictoriaMetrics、ClickHouse、OpenSearch；默认本地地址下，这些依赖会在 pytest 受管启动 monitor-server 时按需准备。若你选择手工托管 monitor-server，请按第 2.3 节使用 `just dev start`，并覆盖对应测试环境变量。
- 若未显式设置 `DOCKER_CONFIG`，`just test bootstrap` 与 pytest 自动托管会使用 `acps-cli/.tmp/docker-public-config/` 拉取 public dev-infra 镜像，避免本机 Docker credential helper 卡住；若你希望继续复用自己的 Docker 登录态，请先自行导出 `DOCKER_CONFIG`。

### 3.3. 运行测试与约定

推荐执行顺序：

1. 在 `acps-cli` 仓库执行 `just dev bootstrap`。
2. 如需运行测试，再执行 `just test bootstrap`。
3. 若要手工联调或使用自定义服务地址，按第 2.3 节启动本地联调实例。
4. 回到 `acps-cli` 执行 `just dev check`，确认 registry / ca / discovery / mq / monitor 五个目标都可达。
5. 若使用默认本地地址且这些地址当前还没有服务监听，可直接运行 `just test integration`、`just test e2e` 或 `just test`；测试夹具会自动托管所需兄弟服务。
6. 若你已经手工启动了默认端口服务，或者通过环境变量改成了自定义地址，则测试只会复用这些现有目标；尤其是自定义地址不可达时，测试不会自动补拉服务。

`just dev check` 的角色是对手工联调前置条件做集中检查；如果它失败，优先修复启动矩阵、端口、token 或证书问题。对默认本地地址的测试路径，`tests/_local_services.py` 会在这些默认地址不可达时按测试模式受管启动兄弟服务，因此不再要求 `just test integration` / `just test e2e` 先显式通过 `check`。如果你改成了非默认地址，`just dev check` 和测试都只会检查/使用你指定的那些目标，不会自动回退到本机默认端口。

当前稳定基线：

- 在 README 约定的联调启动方式下，`just test` 应能跑通 unit / integration / e2e。

当前允许保留 `skip` 的场景，应限于 README / 测试中明确标注的未覆盖能力（例如多实例 discovery forwarder/fallback 联调与 fanout 聚合）；因环境未准备而触发的 `skip` 应先修好环境再跑。

## 4. 打包与部署

全栈交付已统一到 **`acps-infra` 安装层**：应用发布包 →（image）镜像包 → 安装包 → Ansible 首装 / 升级 / 回滚。本仓库不再作为独立的 standalone / 逐仓 wheel 全栈部署入口。

- 概念与目录入口：[`acps-infra/README.md`](../acps-infra/README.md)
- 安装层细节：[`acps-infra/release/install-packaging/README.md`](../acps-infra/release/install-packaging/README.md)
- 逐步命令（acps-docs）：
  - [组装安装包](../acps-docs/tutorials/install-package-build.md)
  - [Ansible 部署](../acps-docs/tutorials/install-package-ansible-deploy.md)
  - [应用发布包构建](../acps-docs/tutorials/app-release-package-build.md)

开发联调仍用本仓 `just` / `scripts/`。仅当需要手工产出本组件制品供安装层采集时，才使用本仓的 `just package wheel`（见 `justfile`，此处不展开）。
