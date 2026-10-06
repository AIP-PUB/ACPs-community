# ACPs Docs

这个目录是 ACPs 项目的参考文档入口：快速开始、教程、CLI 参考与开发测试说明。

**部署**的基础流程是：装依赖 → 铺配置 → 迁移数据库 → 签发证书 → 按顺序拉起进程 → 探活，逐步命令见[从应用薄包手工部署](tutorials/manual-deploy-from-app-thin-package.md)。`acps-infra` 的**自动化安装层**（`release/install-packaging`）把这套流程自动化，并加上多机编排与幂等升级：应用发布包 →（image）镜像包 → 安装包 → Ansible。概念见 [`acps-infra/README.md`](../acps-infra/README.md)；逐步命令见下方教程。

## 目录结构

```text
acps-docs/
|-- getting-started/
|   `-- README.md
|-- tutorials/
|   |-- agent-development.md
|   |-- aip-sdk-tutorial.md
|   |-- aip-identity-binding-verification.md
|   |-- manual-deploy-from-app-thin-package.md # 部署基础流程：从薄包手工部署
|   |-- app-release-package-build.md
|   |-- docker-image-packages-from-app-release.md
|   |-- install-package-build.md
|   |-- install-package-ansible-deploy.md
|   |-- install-package-ansible-deploy-3nodes.md
|   |-- install-package-clean-slate.md        # 验收/重装前破坏性清场
|   |-- install-package-day2-ops.md            # 续签 / trust / 升级 / 回滚
|   |-- amp-agent-observability.md            # Agent 侧 AMP 可观测性
|   |-- oidc-web-app-manual-verification.md
|   `-- oidc-acps-cli-device-login.md
|-- references/
|   `-- cli-reference.md
`-- development/
    `-- development-testing-overview.md
```

## 快速导航

### 1 通用指南

- ACPs 快速开始: [getting-started/README.md](getting-started/README.md)
- ACPs AIP 开发教程: [tutorials/agent-development.md](tutorials/agent-development.md)
- 在 Agent 中接入 AMP 可观测性: [tutorials/amp-agent-observability.md](tutorials/amp-agent-observability.md)
- 部署基础流程 — 从应用薄包手工部署（不限操作系统；下面几篇是它的自动化）: [tutorials/manual-deploy-from-app-thin-package.md](tutorials/manual-deploy-from-app-thin-package.md)
- 从源代码构建应用发布包（image / host 共用）: [tutorials/app-release-package-build.md](tutorials/app-release-package-build.md)
- 从应用发布包构建 Docker 镜像包（仅 image）: [tutorials/docker-image-packages-from-app-release.md](tutorials/docker-image-packages-from-app-release.md)
- 组装安装包（`--mode image|host`）: [tutorials/install-package-build.md](tutorials/install-package-build.md)
- 用安装包做 Ansible 部署（image / host 分开说明）: [tutorials/install-package-ansible-deploy.md](tutorials/install-package-ansible-deploy.md)
- 三业务节点 Ansible 部署: [tutorials/install-package-ansible-deploy-3nodes.md](tutorials/install-package-ansible-deploy-3nodes.md)
- 验收/重装前清场（破坏性）: [tutorials/install-package-clean-slate.md](tutorials/install-package-clean-slate.md)
- 安装后日常运维（续签 / trust / 升级 / 回滚）: [tutorials/install-package-day2-ops.md](tutorials/install-package-day2-ops.md)
- macOS Apple Silicon 本机单机（**仅 image**）：见 [install-package-ansible-deploy.md §4.7](tutorials/install-package-ansible-deploy.md)
- host 单机：Rocky 8/9 / Ubuntu 20.04/22.04 业务机 + 控制节点 SSH，见组装 §2 与部署文里的 **〔host〕** 说明
- OIDC Web 应用手工验证: [tutorials/oidc-web-app-manual-verification.md](tutorials/oidc-web-app-manual-verification.md)
- acps-cli OIDC Device 登录: [tutorials/oidc-acps-cli-device-login.md](tutorials/oidc-acps-cli-device-login.md)

### 2 CLI 文档

- CLI 参考: [references/cli-reference.md](references/cli-reference.md)

### 3 开发测试文档

- ACPs 开发与测试总览: [development/development-testing-overview.md](development/development-testing-overview.md)

### 4 SDK 文档

| 文档 | 链接 |
| -- | --- |
| ACPs SDK 智能体身份码 （AIC） | [SDK: AIC DOC](../acps-sdk/acps_sdk/aip/README.md) |
| ACPs SDK 智能体能力描述 （ACS） | [SDK: ACS DOC](../acps-sdk/acps_sdk/acs/README.md) |
| ACPs SDK 智能体发现协议（ADP）| [SDK: ADP DOC](../acps-sdk/acps_sdk/adp/README.md) |
| ACPs 智能体交互协议（AIP） SDK 开发指南 | [tutorials/aip-sdk-tutorial.md](tutorials/aip-sdk-tutorial.md) |
| AIP 通信如何防止身份伪造 | [tutorials/aip-identity-binding-verification.md](tutorials/aip-identity-binding-verification.md) |

## 建议阅读顺序

1. [getting-started/README.md](getting-started/README.md) — 判断开发测试还是部署
2. 开发 Leader / Partner → [tutorials/agent-development.md](tutorials/agent-development.md)
3. Agent 侧 AMP 可观测性 → [tutorials/amp-agent-observability.md](tutorials/amp-agent-observability.md)
4. OIDC Web / Device → [oidc-web-app-manual-verification.md](tutorials/oidc-web-app-manual-verification.md)、[oidc-acps-cli-device-login.md](tutorials/oidc-acps-cli-device-login.md)
5. CLI → [references/cli-reference.md](references/cli-reference.md)
6. 开发测试 → [development/development-testing-overview.md](development/development-testing-overview.md)
7. **部署基础流程** → [manual-deploy-from-app-thin-package.md](tutorials/manual-deploy-from-app-thin-package.md)（装依赖 / 铺配置 / 迁移 / 签证书 / 起进程 / 探活；后面几步都是在自动化这套流程）
8. 应用发布包（把依赖预装配成 wheelhouse，供离线安装）→ [app-release-package-build.md](tutorials/app-release-package-build.md)
9. **image**：镜像包 → [docker-image-packages-from-app-release.md](tutorials/docker-image-packages-from-app-release.md) → 组装 [install-package-build.md](tutorials/install-package-build.md) §1
10. **host**：跳过镜像包 → 组装 [install-package-build.md](tutorials/install-package-build.md) §2
11. Ansible 部署 → [install-package-ansible-deploy.md](tutorials/install-package-ansible-deploy.md)
12. 多机 → [install-package-ansible-deploy-3nodes.md](tutorials/install-package-ansible-deploy-3nodes.md) 或包内 `hosts-multi.example.yml`
13. 要在同一批机器「从零再装」→ [install-package-clean-slate.md](tutorials/install-package-clean-slate.md) 再跑 `site.yml`
14. 日常运维（续签 / trust / 升级 / 回滚）→ [install-package-day2-ops.md](tutorials/install-package-day2-ops.md)
15. 概念总览 → [`acps-infra/README.md`](../acps-infra/README.md)
16. SDK → 上表 SDK / AIP 教程
