# acps-infra

ACPs 的基础设施与**产品交付**仓库：本地开发共享依赖（`dev-infra`）、以及统一的**打包 / 安装 / 升级 / 回滚**（`release/install-packaging`）。

产品主路径是：

```text
应用发布包 (app-release)
    ├─→ [image] 镜像包 → acps-image-install-*.tar
    └─→ [host]  vendor  → acps-host-install-*.tar
                              │
                              ▼
                    Ansible（site / upgrade / rollback / business）
```

概念见下文；**具体命令与操作步骤一律见 [acps-docs 教程](../acps-docs/README.md)**，本 README 不重复长命令清单。

## 1. 概述

### 1.1. 这个仓库负责什么

| 目录 | 职责 |
| --- | --- |
| `dev-infra/` | 本地开发共享依赖（Postgres / Redis / RabbitMQ 等） |
| `release/install-packaging/` | **安装层**：组装安装包 + Ansible 首装 / 证书 / smoke / business / 升级回滚 |
| `release/app-packaging/` | 应用发布包装配（供安装层采集） |
| `release/image-packaging/` | image-mode 镜像包（供 image 安装包消费） |
| `dev-infra/keycloak/` | 开发联调 Keycloak realm / bootstrap |

### 1.2. 关键目录（交付相关）

```text
acps-infra/
├── dev-infra/                      # 本地开发共享依赖
├── release/
│   ├── app-packaging/              # 应用发布包
│   ├── image-packaging/            # Docker 镜像包（仅 image）
│   └── install-packaging/          # 安装包组装 + Ansible
│       ├── ansible/                # site / upgrade / rollback / business …
│       ├── docs/                   # 安装层短索引
│       └── README.md               # 安装层细节入口
└── …
```

### 1.3. 什么时候用哪个入口

| 目标 | 入口 |
| --- | --- |
| 启动本地开发共享依赖 | `dev-infra/dev-infra.sh`（见 §2） |
| 理解安装层边界 / playbook 一览 | [`release/install-packaging/README.md`](release/install-packaging/README.md) |
| **组装安装包（逐步命令）** | [acps-docs：组装安装包](../acps-docs/tutorials/install-package-build.md) |
| **Ansible 部署 / 验收** | [acps-docs：Ansible 部署](../acps-docs/tutorials/install-package-ansible-deploy.md) |
| 多机拓扑 | [三节点教程](../acps-docs/tutorials/install-package-ansible-deploy-3nodes.md) 或包内 `hosts-multi.example.yml` |
| **日常运维（续签 / trust / 升级 / 回滚）** | [acps-docs：日常运维](../acps-docs/tutorials/install-package-day2-ops.md) |
| 应用发布包 / 镜像包 | [应用发布包](../acps-docs/tutorials/app-release-package-build.md)、[镜像包](../acps-docs/tutorials/docker-image-packages-from-app-release.md) |

## 2. 开发（共享依赖）

### 2.1. 前置条件

- [uv](https://docs.astral.sh/uv/getting-started/installation/)、[just](https://just.systems/man/en/packages.html)（sibling 项目常用）
- [Docker](https://www.docker.com/products/docker-desktop/)（`dev-infra`；image-mode 目标机也需要）

### 2.2. 本地开发共享依赖

```bash
./dev-infra/dev-infra.sh check
./dev-infra/dev-infra.sh up                  # 默认 postgres
./dev-infra/dev-infra.sh up redis rabbitmq   # 按需
./dev-infra/dev-infra.sh status
./dev-infra/dev-infra.sh down
```

说明见 [dev-infra/README.md](dev-infra/README.md)。`dev-infra` 只服务本地开发，**不是**生产部署入口。

## 3. 打包与部署（产品主路径）

### 3.1. 概念

- **两种部署模式**（`acps_deploy_mode`）  
  - **image**：目标机 Docker Compose；安装包装镜像 + 控制面 CLI。  
  - **host**：目标机 Rocky 9 / Ubuntu 22.04；venv/systemd + OS 包 + vendor tarball。
- **制品链**：各业务仓产出应用制品 → `app-packaging` / `image-packaging` → **单一安装包** → 控制节点解包后跑 Ansible。
- **编排入口**（均在安装包或源码树 `release/install-packaging/ansible/playbooks/`）  
  - 首装：`site.yml`  
  - 基础探针：`smoke.yml`  
  - 业务验收：`business.yml`（demo 双开后，固定 A–D）  
  - 升级 / 回滚：`upgrade.yml` / `rollback.yml`（须显式组件列表；**禁止** `down -v`）  
  - 证书：`renew-certs.yml` / `refresh-trust-bundle.yml`
- **控制节点 vs 业务节点**：Ansible 与 acps-cli 在控制节点；业务节点按 mode 跑 Compose 或 systemd。

### 3.2. 去哪看命令

| 主题 | 文档 |
| --- | --- |
| 快速开始（选路径） | [acps-docs getting-started](../acps-docs/getting-started/README.md) |
| 组装 `acps-*-install-*.tar` | [install-package-build.md](../acps-docs/tutorials/install-package-build.md) |
| 解包、secrets、CA、`site.yml` / `business.yml` | [install-package-ansible-deploy.md](../acps-docs/tutorials/install-package-ansible-deploy.md) |
| 三业务节点拆分 | [install-package-ansible-deploy-3nodes.md](../acps-docs/tutorials/install-package-ansible-deploy-3nodes.md) |
| 续签 / trust / 升级 / 回滚 | [install-package-day2-ops.md](../acps-docs/tutorials/install-package-day2-ops.md) |
| 安装层 playbook / 变量 / 已知限制 | [`release/install-packaging/README.md`](release/install-packaging/README.md)、[`docs/`](release/install-packaging/docs/README.md) |

各 sibling 仓库（registry / ca / discovery / …）的 README **只保留一章**指向本文；组件级 `just package` 仅用于制品被安装层采集，不构成独立全栈部署入口。
