# mq-auth-server

mq-auth-server 是 ACPs 的 RabbitMQ 鉴权与群组 ACL 服务，负责为 RabbitMQ 提供 HTTP auth backend，
同时为 Leader 提供群组管理接口。本文说明项目定位与日常开发；全栈打包与部署见第 3 章。

## 1. 概述

### 1.1. 项目定位

- `9007` 提供 Group API，供 Leader 通过 mTLS 管理群组 ACL
- `9008` 提供 Auth API，供 RabbitMQ 执行 allow / deny 鉴权决策
- 使用 Redis 保存群组 ACL，并按需调用 RabbitMQ Management API 断开连接

### 1.2. 项目特点

- 双 listener 架构：Group API 与 Auth API 分端口运行
- 无数据库，ACL 完全保存在 Redis
- 两个端口都要求真实 mTLS 证书

### 1.3. 目录概览

```text
mq-auth-server/
├── app/                  # API、服务与基础设施
├── config/               # TOML 分层配置
├── certs/                # 本地开发证书
├── tests/                # unit / integration / e2e
└── Justfile              # 本地开发、测试、质量检查入口
```

## 2. 开发

### 2.1. 前置条件

- [uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)
- [just 官方安装文档](https://just.systems/man/en/packages.html)
- [Docker Desktop 官方下载](https://www.docker.com/products/docker-desktop/)
- 同级目录已存在 `../acps-infra/`

### 2.2. 快速开始

```bash
git clone <仓库地址>
cd mq-auth-server

# 建议先显式复制模板并检查关键配置（缺失时 just prep env / just dev start 也会生成）。
cp .env.example .env
# 编辑 .env：确认 Redis、RabbitMQ、证书路径等敏感项

just dev start
```

启动后常用地址：

- Group API: `https://localhost:9007`
- Auth API: `https://localhost:9008`

### 2.3. 常用命令

```bash
# 帮助
just help                         # 输出命令总览，直接执行 just 也会显示帮助

# 共享依赖
just infra up redis rabbitmq      # 启动 mq-auth-server 需要的共享依赖
just infra status                 # 查看共享依赖状态

# 环境准备
just prep env                     # 缺失时根据 .env.example 生成 .env
just prep sync                    # 下载 managed Python 3.14，并把依赖同步到 .venv/
just prep hooks                   # 安装/更新 Git hooks
just prep certs                   # 准备开发证书
just prep certs reset             # 清理本地证书后重新签发
just prep migrate test            # 无数据库项目，显式 skip

# 开发
just dev check                    # 只读检查开发环境、Redis、RabbitMQ、证书与关键配置
just dev bootstrap                # 可选：只预热环境、不启动服务
just dev start                    # 后台启动双 listener
just dev start fg                 # 前台启动，便于调试
just dev logs follow              # 持续跟踪日志
just dev stop                     # 停止本地实例

# 测试
just test check                   # 只读检查测试环境
just test bootstrap               # 手动预热测试环境；integration/e2e/coverage 会按需隐含执行
just test unit                    # 单元测试
just test integration             # 集成测试
just test e2e                     # 黑盒 e2e
just test coverage                # 覆盖率统计
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
- `just prep certs` 会从共享开发 PKI 准备服务端证书、信任锚和健康检查客户端证书。
- `just dev start` 启动前会自动完成环境准备（也可单独执行 `just dev bootstrap` 只预热、不启动）。
- `9007` 仅供 Leader 通过 mTLS 调用，`9008` 仅供 RabbitMQ 调用，两类接口不要混用。
- 本仓 `tests/e2e/` 会启动临时双 listener 实例做黑盒验证，不依赖 sibling 服务。
- 生产部署时的证书挂载、stage 前置条件和升级方式，统一在 `acps-infra/README.md` 中说明。

## 3. 打包与部署

全栈交付已统一到 **`acps-infra` 安装层**：应用发布包 →（image）镜像包 → 安装包 → Ansible 首装 / 升级 / 回滚。本仓库不再作为独立的 standalone / 逐仓 wheel 全栈部署入口。

- 概念与目录入口：[`acps-infra/README.md`](../acps-infra/README.md)
- 安装层细节：[`acps-infra/release/install-packaging/README.md`](../acps-infra/release/install-packaging/README.md)
- 逐步命令（acps-docs）：
  - [组装安装包](../acps-docs/tutorials/install-package-build.md)
  - [Ansible 部署](../acps-docs/tutorials/install-package-ansible-deploy.md)
  - [应用发布包构建](../acps-docs/tutorials/app-release-package-build.md)

开发联调仍用本仓 `just` / `scripts/`。仅当需要手工产出本组件制品供安装层采集时，才使用本仓的 `just package wheel`（见 `justfile`，此处不展开）。
