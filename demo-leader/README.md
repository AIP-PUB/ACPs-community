# demo-leader

demo-leader 是 ACPs 的 Leader Agent 示例应用，负责接收用户输入、编排 Partner Agents、聚合结果，
并以 API / SSE 和 Web UI 的方式输出。本文说明项目定位与日常开发；全栈打包与部署见第 3 章。

## 1. 概述

### 1.1. 项目定位

- 提供 Leader API，负责接收请求、编排 Partner 和聚合结果
- 提供本地 Web UI，便于手工联调与演示
- 作为 demo-partner、mq-auth-server、registry / ca / discovery 的上游业务入口

### 1.2. 项目特点

- 双进程运行：`9031` Leader API，`9030` Web UI
- 无数据库，业务状态主要由编排逻辑与会话流转维护
- `leader/config.toml` 保留非敏感配置，敏感值统一走环境变量
- 运行时 mTLS 证书位于 `leader/atr/`

### 1.3. 目录概览

```text
demo-leader/
├── leader/                      # Leader API、运行时配置、ACS 与场景
├── web_app/                     # 本地静态前端
├── tests/                       # unit / integration / e2e
├── scripts/smoke-test-business.sh
└── Justfile                     # 本地开发、测试、质量检查入口
```

## 2. 开发

### 2.1. 前置条件

- [uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)
- [just 官方安装文档](https://just.systems/man/en/packages.html)
- [Docker Desktop 官方下载](https://www.docker.com/products/docker-desktop/)
- 同级目录已存在 `../acps-sdk/`、`../acps-cli/`、`../acps-infra/`
- 如需跑集成测试或 e2e，通常还需要先启动 sibling `demo-partner`

### 2.2. 快速开始

```bash
git clone <仓库地址>
cd demo-leader

# 建议先显式复制模板并检查关键配置（缺失时 just prep env / just dev start 也会生成）。
cp .env.example .env
# 编辑 .env：填入 LLM 敏感信息；本地 OIDC / Web 端口等非敏感默认值已收敛到仓库配置

just dev start
```

启动后常用地址：

- Web UI: `http://localhost:9030`
- Leader API: `http://localhost:9031`

### 2.3. 常用命令

```bash
# 帮助
just help                         # 输出命令总览，直接执行 just 也会显示帮助

# 共享依赖
just infra status                 # 查看共享依赖状态

# 环境准备
just prep env                     # 缺失时根据 .env.example 生成 .env
just prep sync                    # 下载 managed Python 3.14，并把依赖同步到 .venv/
just prep hooks                   # 安装/更新 Git hooks
just prep certs                   # 基于 leader/atr/acs.json 生成本地证书

# 开发
just dev check                    # 只读检查开发环境、证书、sibling 前置和关键配置
just dev bootstrap                # 可选：只预热环境、不启动服务
just dev start                    # 后台启动 Leader API + Web UI
just dev status                   # 查看后台进程状态
just dev logs follow              # 持续跟踪日志
just dev stop                     # 停止本地实例

# 测试
just test check                   # 只读检查测试环境
just test bootstrap               # 手动预热测试环境；api/integration/e2e 会按需隐含执行
just test unit                    # 单元测试
just test api                     # API 级测试
just test integration             # 集成测试
just test e2e                     # 黑盒 e2e
just test coverage                # 单元测试覆盖率
just test                         # 默认执行 all，依次执行 unit / api / integration / e2e

# 打包
just package check                # 只读检查打包前置条件
just package bootstrap            # 按需补齐打包前置条件（不生成发布物）
just package wheel                # 构建在线 wheel 运行包

# 质量
just qa                           # 显示 qa 帮助
just qa precommit                 # 执行 pre-commit 全量门禁
just qa pip-audit                 # 依赖漏洞审计
```

### 2.4. 开发说明

- 项目运行所需 Python 不依赖本机预装版本；`just prep sync` 会通过 `uv` 下载 managed Python 3.14，
  并把依赖安装到当前项目的 `.venv/`。
- `leader/atr/` 下的本地 mTLS 证书由 `just prep certs` 生成，不应提交到 Git。
- `just dev start` 启动前会自动完成环境准备（也可单独执行 `just dev bootstrap` 只预热、不启动）。
- 集成测试和 e2e 通常需要先启动 sibling `demo-partner`。
- 真实 LLM 密钥等敏感信息由 `.env` 注入；本地开发使用的 OIDC 默认配置已提交在
  `leader/config.toml` 与 `web_app/runtime-config.js` 中，并与 `acps-infra/dev-infra` 的 Keycloak 对齐。
- Web UI 默认服务于本地调试与演示；standalone 交付侧端口仍由 `acps-infra` 的 `LEADER_WEB_PORT` 控制。

## 3. 打包与部署

全栈交付已统一到 **`acps-infra` 安装层**：应用发布包 →（image）镜像包 → 安装包 → Ansible 首装 / 升级 / 回滚。本仓库不再作为独立的 standalone / 逐仓 wheel 全栈部署入口。

- 概念与目录入口：[`acps-infra/README.md`](../acps-infra/README.md)
- 安装层细节：[`acps-infra/release/install-packaging/README.md`](../acps-infra/release/install-packaging/README.md)
- 逐步命令（acps-docs）：
  - [组装安装包](../acps-docs/tutorials/install-package-build.md)
  - [Ansible 部署](../acps-docs/tutorials/install-package-ansible-deploy.md)
  - [应用发布包构建](../acps-docs/tutorials/app-release-package-build.md)

开发联调仍用本仓 `just` / `scripts/`。仅当需要手工产出本组件制品供安装层采集时，才使用本仓的 `just package wheel`（见 `justfile`，此处不展开）。
