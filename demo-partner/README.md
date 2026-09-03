# demo-partner

demo-partner 是 ACPs 的 Partner Agent 示例应用，以多 Agent 方式运行，每个 Agent 暴露独立端口，
用于演示基于 ACPs SDK 的 Partner 调用与编排。本文说明项目定位与日常开发；全栈打包与部署见第 3 章。

## 1. 概述

### 1.1. 项目定位

- 提供多个可独立运行的 Partner Agent 示例
- 作为 demo-leader、discovery-server 和整体链路联调的下游服务
- 展示基于 ACS / AIC / mTLS 的 Partner 运行方式

### 1.2. 项目特点

- 多 Agent、多端口、配置驱动
- 无数据库，不依赖 PostgreSQL / Alembic
- 本地证书与发布证书都按 Agent 维度管理在 `partners/online/*/`

### 1.3. 目录概览

```text
demo-partner/
├── partners/              # Partner 业务代码与在线配置
├── tests/                 # unit / integration / e2e
└── Justfile               # 本地开发、测试、质量检查入口
```

## 2. 开发

### 2.1. 前置条件

- [uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)
- [just 官方安装文档](https://just.systems/man/en/packages.html)
- [Docker Desktop 官方下载](https://www.docker.com/products/docker-desktop/)
- 同级目录已存在 `../acps-infra/`
- 如需跑完整联调链路，宿主机还应运行 `registry-server`、`ca-server`、`discovery-server`

### 2.2. 快速开始

```bash
git clone <仓库地址>
cd demo-partner

# 建议先显式复制模板并检查关键配置（缺失时 just prep env / just dev start 也会生成）。
cp .env.example .env
# 编辑 .env：确认 RabbitMQ、LLM 与各 Agent 运行参数

just dev start
```

默认会启动多个 Partner Agent，端口范围为 `9021-9025`。

### 2.3. 常用命令

```bash
# 帮助
just help                         # 输出命令总览，直接执行 just 也会显示帮助

# 共享依赖
just infra up rabbitmq            # 启动 demo-partner 需要的共享依赖
just infra status                 # 查看共享依赖状态

# 环境准备
just prep env                     # 缺失时根据 .env.example 生成 .env
just prep sync                    # 下载 managed Python 3.14，并把依赖同步到 .venv/
just prep hooks                   # 安装/更新 Git hooks
just prep certs                   # 准备本地 mTLS 开发证书

# 开发
just dev check                    # 只读检查开发环境、证书与关键配置
just dev bootstrap                # 可选：只预热环境、不启动服务
just dev start                    # 后台启动 Partner Agents
just dev start fg                 # 前台启动，便于调试
just dev logs follow              # 持续跟踪日志
just dev stop                     # 停止本地实例

# 测试
just test check                   # 只读检查测试环境
just test bootstrap               # 手动预热测试环境；integration/e2e 会按需隐含执行
just test unit                    # 单元测试
just test integration             # 集成测试
just test e2e                     # 黑盒 e2e
just test                         # 默认执行 all

# 打包
just package check                # 只读检查打包前置条件
just package bootstrap            # 按需补齐打包前置条件（不生成发布物）
just package wheel                # 构建在线运行包

# 质量
just qa                           # 显示 qa 帮助
just qa precommit                 # 执行 pre-commit 全量门禁
just qa pip-audit                 # 依赖漏洞审计
```

### 2.4. 开发说明

- 项目运行所需 Python 不依赖本机预装版本；`just prep sync` 会通过 `uv` 下载 managed Python 3.14，
  并把依赖安装到当前项目的 `.venv/`。
- `partners/online/*/acs.json` 定义 Agent 能力，`config.toml` 定义运行配置。
- `just prep certs` 会按各 Agent 的 AIC 声明生成本地 mTLS 证书，不应把这些临时文件提交到 Git。
- `just dev start` 启动前会自动完成环境准备（也可单独执行 `just dev bootstrap` 只预热、不启动）。
- 集成测试和 e2e 依赖 RabbitMQ；如需完整业务链路验证，通常还需要同时启动 `demo-leader`。
- 已部署实例也可通过 `TEST_E2E_BASE_URLS` 提供给 e2e 测试复用。

## 3. 打包与部署

全栈交付已统一到 **`acps-infra` 安装层**：应用发布包 →（image）镜像包 → 安装包 → Ansible 首装 / 升级 / 回滚。本仓库不再作为独立的 standalone / 逐仓 wheel 全栈部署入口。

- 概念与目录入口：[`acps-infra/README.md`](../acps-infra/README.md)
- 安装层细节：[`acps-infra/release/install-packaging/README.md`](../acps-infra/release/install-packaging/README.md)
- 逐步命令（acps-docs）：
  - [组装安装包](../acps-docs/tutorials/install-package-build.md)
  - [Ansible 部署](../acps-docs/tutorials/install-package-ansible-deploy.md)
  - [应用发布包构建](../acps-docs/tutorials/app-release-package-build.md)

开发联调仍用本仓 `just` / `scripts/`。仅当需要手工产出本组件制品供安装层采集时，才使用本仓的 `just package wheel`（见 `justfile`，此处不展开）。
