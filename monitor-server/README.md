# monitor-server

monitor-server 是 ACPs 的监控服务，负责 AMP（Agent Monitoring Protocol）监控日志的收集、入库与查询。
本文重点覆盖项目定位、日常开发与测试说明。

## 1. 概述

### 1.1. 项目定位

- 消费 Kafka `amp.audit` 主题，验证签名，构建哈希链，幂等写入 PostgreSQL
- 对外提供 FastAPI Query API，支持按 AMP spec 定义的过滤、排序与游标分页查询

### 1.2. 项目特点

- 双链路架构：写入链路（Kafka Consumer）与查询链路（FastAPI）独立运行
- 本地开发固定采用"宿主机进程 + `../acps-infra/dev-infra`"，含 PostgreSQL 与 Kafka（Redpanda）
- 日志转发由 Fluent Bit（`config/fluent-bit/`）或 Python tail 脚本负责，与应用进程解耦

### 1.3. 目录概览

```text
monitor-server/
├── app/                  # 业务代码（writer / query / audit / core）
├── alembic/              # 数据库迁移
├── config/               # 运行时配置（TOML + Fluent Bit）
├── tests/                # unit / integration / e2e
├── scripts/              # 运维与调试脚本
├── plans/                # 构建与需求计划（不提交）
└── Justfile              # 本地开发、测试、质量检查入口
```

## 2. 开发

### 2.1. 前置条件

- [uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)
- [just 官方安装文档](https://just.systems/man/en/packages.html)
- [Docker Desktop 官方下载](https://www.docker.com/products/docker-desktop/)
- 同级目录已存在 `../acps-infra/`
- 本机已安装 Fluent Bit（`brew install fluent-bit`）

### 2.2. 快速开始

```bash
git clone <仓库地址>
cd monitor-server

# 建议先显式复制模板并检查关键配置（缺失时 just prep env / just dev start 也会生成）。
cp .env.example .env
# 编辑 .env：确认数据库、Kafka 地址等敏感项

just dev start
```

启动后常用地址：

- Query API: `http://localhost:9009`
- OpenAPI 文档: `http://localhost:9009/docs`
- Health: `http://localhost:9009/health`

### 2.3. 常用命令

```bash
# 帮助
just help                         # 输出命令总览，直接执行 just 也会显示帮助

# 共享依赖
just infra up postgres kafka      # 启动 monitor-server 需要的共享依赖
just infra status                 # 查看共享依赖状态

# 环境准备
just prep env                     # 缺失时根据 .env.example 生成 .env
just prep sync                    # 下载 managed Python 3.14，并把依赖同步到 .venv/
just prep hooks                   # 安装/更新 Git hooks
just prep migrate dev             # 迁移开发数据库
just prep migrate test            # 迁移测试数据库

# 开发
just dev check                    # 只读检查开发环境、数据库、Kafka 与关键配置
just dev bootstrap                # 可选：只预热环境、不启动服务
just dev start                    # 后台启动服务
just dev start fg                 # 前台启动，便于调试
just dev logs follow              # 持续跟踪日志
just dev stop                     # 停止本地实例

# 测试
just test check                   # 只读检查测试环境
just test bootstrap               # 手动预热测试环境；integration/e2e/coverage 会按需隐含执行
just test unit                    # 单元测试
just test integration             # 集成测试
just test e2e                     # 黑盒 E2E
just test coverage                # 生成覆盖率统计
just test                         # 默认执行 all，依次执行 unit / integration / e2e

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
- `just dev start` 启动前会自动准备 `.venv`、hooks、开发库迁移（含 Kafka 就绪等待）；也可单独执行 `just dev bootstrap` 只预热、不启动。
- 日志转发：开发环境使用 Fluent Bit，配置文件位于 `config/fluent-bit/fluent-bit.conf`；
  macOS 下必须在 `[OUTPUT]` 段保留 `Workers 1`，否则 Kafka 插件会静默退出。
- `AUDIT_SIGNING_PRIVATE_KEY` 需要与 demo-leader / demo-partner 的公钥对匹配，
  对齐后审计日志签名验证才能通过。
- 如需验证完整审计链路（日志输出→Kafka→入库→查询），请参考 [docs/local-audit-e2e.md](docs/local-audit-e2e.md)；
  该文档同时覆盖 Mock 验签模式（默认，纯本地）与 CA 联合验签模式（接入 ca-server，生产级）。

## 3. 打包与部署

全栈交付已统一到 **`acps-infra` 安装层**：应用发布包 →（image）镜像包 → 安装包 → Ansible 首装 / 升级 / 回滚。本仓库不再作为独立的 standalone / 逐仓 wheel 全栈部署入口。

- 概念与目录入口：[`acps-infra/README.md`](../acps-infra/README.md)
- 安装层细节：[`acps-infra/release/install-packaging/README.md`](../acps-infra/release/install-packaging/README.md)
- 逐步命令（acps-docs）：
  - [组装安装包](../acps-docs/tutorials/install-package-build.md)
  - [Ansible 部署](../acps-docs/tutorials/install-package-ansible-deploy.md)
  - [应用发布包构建](../acps-docs/tutorials/app-release-package-build.md)

开发联调仍用本仓 `just` / `scripts/`。仅当需要手工产出本组件制品供安装层采集时，才使用本仓的 `just package …` / `scripts/release-app/`（见 `justfile` 与 `scripts/`，此处不展开）。

## 4. 相关文档

- [AMP 规范](../acps-specs/09-ACPs-spec-AMP/ACPs-spec-AMP.md)
- AMP 设计与 Audit API 设计请从内部设计资料查阅。
