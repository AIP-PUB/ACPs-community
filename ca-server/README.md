# ca-server

ca-server 是 ACPs 的证书服务，负责 ATR 场景下的证书申请、签发、吊销与查询。本文说明项目定位与日常开发；全栈打包与部署见第 3 章。

## 1. 概述

### 1.1. 项目定位

- 对外提供 ACME、CRL、OCSP 与 trust bundle 相关协议端点
- 为 Agent 签发业务证书，并维护证书状态与吊销信息
- 为其他 ACPs 组件提供 internal/admin 证书管理接口

### 1.2. 项目特点

- 同时承载 ACME、CRL、OCSP 三类协议能力
- 本地开发证书材料由 `../acps-infra/dev-infra` 统一下发
- 默认开发模式可 mock `registry-server`，需要时再切换真实联调

### 1.3. 目录概览

```text
ca-server/
├── app/                         # 业务代码
├── alembic/                     # 数据库迁移
├── certs/                       # 本地开发证书材料
├── tests/                       # unit / integration / e2e
└── Justfile                     # 本地开发、测试、质量检查入口
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
cd ca-server

# 建议先显式复制模板并检查关键配置（缺失时 just prep env / just dev start 也会生成）。
cp .env.example .env
# 编辑 .env：确认数据库、CA 材料路径、服务 token 等敏感项

just dev start
```

启动后常用地址：

- API: `http://localhost:9003`
- Docs: `http://localhost:9003/docs`
- Health: `http://localhost:9003/health`

### 2.3. 常用命令

```bash
# 帮助
just help                         # 输出命令总览，直接执行 just 也会显示帮助

# 共享依赖
just infra up postgres            # 启动 ca-server 需要的共享依赖
just infra status                 # 查看共享依赖状态

# 环境准备
just prep env                     # 缺失时根据 .env.example 生成 .env
just prep sync                    # 下载 managed Python 3.14，并把依赖同步到 .venv/
just prep hooks                   # 安装/更新 Git hooks
just prep certs                   # 准备本地 CA 开发材料
just prep migrate dev             # 迁移开发数据库
just prep migrate test            # 迁移测试数据库

# 开发
just dev check                    # 只读检查开发环境、证书与关键配置
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
just test e2e                     # 黑盒 e2e
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
- `just prep certs` 会从共享开发 PKI 导出 `ca-server` 需要的 CA 套件。
- `just dev start` 启动前会自动完成环境准备（也可单独执行 `just dev bootstrap` 只预热、不启动）。
- 开发模式下默认不会请求真实 `registry-server`；如需联调，请在 `config/development.toml` 中将
  `[registry_server].mock` 改为 `false`，或在启动命令前临时注入 `REGISTRY_SERVER_MOCK=false` 作为 override。
- 本仓的 `tests/e2e/` 只验证 `ca-server` 自身黑盒行为；跨服务联调请转到 `acps-cli/tests/e2e/`。
- 生产配置中的证书对外地址、OCSP 和 CRL 地址应在部署前确认，不建议依赖默认占位值。

## 3. 打包与部署

全栈交付已统一到 **`acps-infra` 安装层**：应用发布包 →（image）镜像包 → 安装包 → Ansible 首装 / 升级 / 回滚。本仓库不再作为独立的 standalone / 逐仓 wheel 全栈部署入口。

- 概念与目录入口：[`acps-infra/README.md`](../acps-infra/README.md)
- 安装层细节：[`acps-infra/release/install-packaging/README.md`](../acps-infra/release/install-packaging/README.md)
- 逐步命令（acps-docs）：
  - [组装安装包](../acps-docs/tutorials/install-package-build.md)
  - [Ansible 部署](../acps-docs/tutorials/install-package-ansible-deploy.md)
  - [应用发布包构建](../acps-docs/tutorials/app-release-package-build.md)

开发联调仍用本仓 `just` / `scripts/`。仅当需要手工产出本组件制品供安装层采集时，才使用本仓的 `just package wheel`（见 `justfile`，此处不展开）。
