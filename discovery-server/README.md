# discovery-server

discovery-server 是 ACPs 的发现服务，负责接收自然语言请求、维护本地索引，并基于 DSP 同步结果返回
可用 Agent。本文说明项目定位与日常开发；全栈打包与部署见第 3 章。

## 1. 概述

### 1.1. 项目定位

- 接收自然语言发现请求并返回候选 Agent
- 通过 DSP 与 `registry-server` 保持 ACS 数据同步
- 在本地维护 embedding 索引、可用性状态与可选的 forwarder 逻辑

### 1.2. 项目特点

- 同一套服务同时支持 CPU / GPU 两种运行档位
- 支持样本数据导入，便于单仓验证 discovery 行为
- 详细运行行为与高级开关统一放在 `config/default.toml` 和 `config/{APP_ENV}.toml`

### 1.3. 目录概览

```text
discovery-server/
├── app/                  # discovery / sync / core
├── config/               # TOML 分层配置
├── alembic/              # 数据库迁移
├── tests/                # unit / integration / e2e
└── Justfile              # 本地开发、测试、质量检查入口
```

## 2. 开发

### 2.1. 前置条件

- [uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)
- [just 官方安装文档](https://just.systems/man/en/packages.html)
- [Docker Desktop 官方下载](https://www.docker.com/products/docker-desktop/)
- 同级目录已存在 `../acps-infra/`
- 同级目录已存在 `../acps-sdk/`
- 如需导入样本数据，建议同级目录存在 `../demo-partner/`，可选存在 `../demo-leader/`

### 2.2. 快速开始

```bash
git clone <仓库地址>
cd discovery-server

# 建议先显式复制模板并检查关键配置（缺失时 just prep env / just dev start 也会生成）。
cp .env.example .env
# 编辑 .env：确认数据库、LLM、embedding 等敏感项

just dev start
```

启动后常用地址：

- API: `http://localhost:9005`
- Docs: `http://localhost:9005/docs`
- Health: `http://localhost:9005/health`

### 2.3. 常用命令

```bash
# 帮助
just help                             # 输出命令总览，直接执行 just 也会显示帮助

# 共享依赖
just infra up postgres                # 启动 discovery-server 需要的共享依赖
just infra status                     # 查看共享依赖状态

# 环境准备
just prep env                         # 缺失时根据 .env.example 生成 .env
just prep sync                        # 下载 managed Python 3.14，并把依赖同步到 .venv/
just prep hooks                       # 安装/更新 Git hooks
just prep migrate dev                 # 迁移开发数据库
just prep migrate test                # 迁移测试数据库
just prep seed test                   # 导入测试库 demo ACS 样本（如需要）

# 开发
just dev check                        # 只读检查开发环境与关键配置
just dev bootstrap                    # 可选：只预热环境、不启动服务
just dev start                        # 后台启动服务
just dev start fg                     # 前台启动，便于调试
just dev logs follow                  # 持续跟踪日志
just dev stop                         # 停止本地实例

# 测试
just test check                       # 只读检查测试环境
just test bootstrap                   # 手动预热测试环境；integration/e2e/coverage 会按需隐含执行
just test unit                        # 单元测试
just test integration                 # 集成测试
just test e2e                         # 黑盒 e2e
just test coverage                    # 生成覆盖率统计
just test                             # 默认执行 all，依次执行 unit / integration / e2e

# 打包
just package check                    # 只读检查打包前置条件
just package bootstrap                # 按需补齐打包前置条件（不生成发布物）
just package wheel                    # 构建在线运行包

# 质量
just qa                               # 显示 qa 帮助
just qa precommit                     # 执行 pre-commit 全量门禁
just qa pip-audit                     # 依赖漏洞审计
```

### 2.4. 开发说明

- 项目运行所需 Python 不依赖本机预装版本；`just prep sync` 会通过 `uv` 下载 managed Python 3.14，
  并把依赖安装到当前项目的 `.venv/`。
- `DISCOVERY_MODE` 控制运行时档位；CPU 使用远端 embedding API，GPU 使用本地模型路径。默认开发环境是
  CPU-only：`uv sync`（或 `just prep sync`）默认不安装 `gpu` extra，配合默认 `DISCOVERY_MODE=cpu` 即可
  启动。需要本地跑 GPU 模式（本地 BGE-M3 embedding/reranker）时，显式执行 `uv sync --extra gpu`，
  并设置 `DISCOVERY_MODE=gpu`、`EMBEDDING_MODEL_PATH`、`EMBEDDING_DEVICES`、`RERANKER_URL` 等配置；
  缺少 `gpu` extra 时启动 GPU 模式会得到明确的 `RuntimeError` 提示安装 `uv sync --extra gpu`，而不是
  裸 `ModuleNotFoundError`。
- `just test bootstrap`（以及 `just test check`）会强制确保测试环境已安装 `gpu` extra：标准
  `integration`/`e2e`/`all` 入口默认依次跑 CPU、GPU 两种模式，测试环境不区分 CPU/GPU 开发者身份。
- `just dev start` 启动前会自动完成环境准备（也可单独执行 `just dev bootstrap` 只预热、不启动）。
- `DISCOVERY_BUILD_PROFILE` 影响应用发布包 / 镜像构建时的依赖档位；真正的运行模式仍由
  `config/{APP_ENV}.toml` 中的 `[discovery].mode` 或环境变量 `DISCOVERY_MODE` 决定。
- `just prep seed app` / `just prep seed test` 会读取 sibling `demo-partner`，并按需读取
  `demo-leader` 的 ACS JSON 生成样本数据。
- 配置项较多的运行行为说明，例如 forwarder、polling、secondary instance 和详细接口边界，统一以
  `config/default.toml` 与对应环境 TOML 为准，根 README 不再重复展开。
- 真实跨服务联调仍应转到 `acps-cli/tests/e2e/`；本仓测试主要覆盖 `discovery-server` 自身边界。

## 3. 打包与部署

全栈交付已统一到 **`acps-infra` 安装层**：应用发布包 →（image）镜像包 → 安装包 → Ansible 首装 / 升级 / 回滚。本仓库不再作为独立的 standalone / 逐仓 wheel 全栈部署入口。

- 概念与目录入口：[`acps-infra/README.md`](../acps-infra/README.md)
- 安装层细节：[`acps-infra/release/install-packaging/README.md`](../acps-infra/release/install-packaging/README.md)
- 逐步命令（acps-docs）：
  - [组装安装包](../acps-docs/tutorials/install-package-build.md)
  - [Ansible 部署](../acps-docs/tutorials/install-package-ansible-deploy.md)
  - [应用发布包构建](../acps-docs/tutorials/app-release-package-build.md)

开发联调仍用本仓 `just` / `scripts/`。仅当需要手工产出本组件制品供安装层采集时，才使用本仓的 `just package wheel`（见 `justfile`，此处不展开）。
