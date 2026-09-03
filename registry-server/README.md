# registry-server

registry-server 是 ACPs 的 Agent 注册中心，负责 Agent 注册、审核、ATR / EAB 相关能力，以及 DSP
同步所需的注册数据管理。本文说明项目定位与日常开发；全栈打包与部署见第 3 章。

## 1. 概述

### 1.1. 项目定位

- 对外提供 Agent 注册、查询、审核与文件上传等 API
- 为 ACPs ATR / EAB 流程提供注册与身份侧支撑
- 为 `discovery-server` 提供 DSP 同步源数据

### 1.2. 项目特点

- 双平面运行：`9001` public API，`9002` mTLS API
- 本地开发固定采用“宿主机进程 + `../acps-infra/dev-infra`”
- 真实跨服务联调统一放到 `acps-cli/tests/e2e/`，本仓测试只关注自身边界

### 1.3. 目录概览

```text
registry-server/
├── app/                  # 业务代码
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

### 2.2. 快速开始

```bash
git clone <仓库地址>
cd registry-server

# 建议先显式复制模板并检查关键配置（缺失时 just prep env / just dev start 也会生成）。
cp .env.example .env
# 编辑 .env：确认数据库、token、证书路径等敏感项

just dev start
```

启动后常用地址：

- Public API: `http://localhost:9001`
- Public Docs: `http://localhost:9001/docs`
- mTLS API: `https://localhost:9002`

### 2.3. 常用命令

```bash
# 帮助
just help                         # 输出命令总览，直接执行 just 也会显示帮助

# 共享依赖
just infra up postgres            # 启动 registry-server 需要的共享依赖
just infra status                 # 查看共享依赖状态

# 环境准备
just prep env                     # 缺失时根据 .env.example 生成 .env
just prep sync                    # 下载 managed Python 3.14，并把依赖同步到 .venv/
just prep hooks                   # 安装/更新 Git hooks
just prep certs                   # 准备本地 mTLS 开发证书
just prep migrate dev             # 迁移开发数据库
just prep migrate test            # 迁移测试数据库

# 开发
just dev check                    # 只读检查开发环境、证书与关键配置
just dev bootstrap                # 可选：只预热环境、不启动服务
just dev start                    # 后台启动 public + mTLS 双平面
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
- `just dev start` 启动前会自动准备 `.venv`、hooks、开发库迁移和本地证书（也可单独执行 `just dev bootstrap` 只预热、不启动）。
- `9002` 默认使用真实 TLS + 客户端证书强制校验。
- `REGISTRY_SERVER_INTERNAL_API_TOKEN` 需要与 `ca-server` 保持一致，真实联调时尤其要注意。
- 如果要验证 `registry-server` 与 `ca-server`、`discovery-server` 的完整联调链路，请转到
  `acps-cli/tests/e2e/`。

### 2.5. OIDC 历史 local 用户绑定

当 `registry-server` 从本地账号切换到 OIDC 时，如果希望已有 Agent 继续归属原来的 `User.id`，
需要先把历史 local 用户显式绑定到对应的 OIDC principal。仓库内提供了一个默认 dry-run 的管理命令：

```bash
uv run python -m app.account.oidc_user_link_migration \
  --mapping-file /path/to/oidc-user-links.json
```

映射文件必须是 JSON 数组，并且每条记录都要显式提供 `username` 或 `user_id`，不支持只靠 email 自动匹配：

```json
[
  {
    "username": "alice",
    "issuer": "https://keycloak.example/realms/acps-registry",
    "subject": "real-oidc-subject-from-idp",
    "expected_email": "alice@example.com"
  }
]
```

使用建议：

- 先执行 dry-run，确认 `blocking_count = 0`。
- 通过 `--apply` 才会真正写库：

```bash
uv run python -m app.account.oidc_user_link_migration \
  --mapping-file /path/to/oidc-user-links.json \
  --apply \
  --report-file /tmp/registry-oidc-link-report.json
```

- 工具不会按 email 静默合并用户；`expected_email` 只作为人工校验条件，避免误绑。
- 输出报告会包含 `principal_id` 和 `subject_hash`，不会回显 raw subject。
- 该工具只预绑定 `external_issuer` / `external_subject` / `external_principal_id`，不直接重写历史 `User.id`。

## 3. 打包与部署

全栈交付已统一到 **`acps-infra` 安装层**：应用发布包 →（image）镜像包 → 安装包 → Ansible 首装 / 升级 / 回滚。本仓库不再作为独立的 standalone / 逐仓 wheel 全栈部署入口。

- 概念与目录入口：[`acps-infra/README.md`](../acps-infra/README.md)
- 安装层细节：[`acps-infra/release/install-packaging/README.md`](../acps-infra/release/install-packaging/README.md)
- 逐步命令（acps-docs）：
  - [组装安装包](../acps-docs/tutorials/install-package-build.md)
  - [Ansible 部署](../acps-docs/tutorials/install-package-ansible-deploy.md)
  - [应用发布包构建](../acps-docs/tutorials/app-release-package-build.md)

开发联调仍用本仓 `just` / `scripts/`。仅当需要手工产出本组件制品供安装层采集时，才使用本仓的 `just package wheel`（见 `justfile`，此处不展开）。
