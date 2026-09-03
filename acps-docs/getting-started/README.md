[首页](../README.md)

# ACPs 快速开始

本文是 ACPs 的入口说明，用来帮助你判断自己应该走哪条路径：本地开发测试，或部署一套环境。部署又分**手工部署**（基础流程）和 **Ansible 安装包部署**（同一套流程的自动化）两种执行方式。各路径的详细步骤已拆到独立文档；本文只保留路线和最小命令。

概念总览见 [`acps-infra/README.md`](../../acps-infra/README.md)。

## 1. 先理解几类工作

| 目标 | 适合对象 | 详细文档 |
| --- | --- | --- |
| 本地开发与测试 | 修改服务、SDK、CLI、demo 代码的开发者 | [开发与测试总览](../development/development-testing-overview.md) |
| **开发 Agent（AIP）** | 写 Leader / Partner 业务逻辑 | [AIP 开发教程](../tutorials/agent-development.md) |
| **Agent 可观测性（AMP）** | 在 Agent 里打 AMP 日志并查询 | [AMP 可观测性教程](../tutorials/amp-agent-observability.md) |
| **手工部署（基础流程）** | 想弄清部署到底做了什么，或目标环境不能用 Docker / 操作系统不在支持矩阵内 | [从应用薄包手工部署](../tutorials/manual-deploy-from-app-thin-package.md) |
| **安装包 Ansible 部署** | 交付 image-mode 或 host-mode 环境的构建者 / 部署者 | [组装安装包](../tutorials/install-package-build.md)、[Ansible 部署](../tutorials/install-package-ansible-deploy.md)、[三节点](../tutorials/install-package-ansible-deploy-3nodes.md) |
| **验收/重装前清场** | 同一批机器要从零再装（破坏数据） | [清场教程](../tutorials/install-package-clean-slate.md) |
| **安装后日常运维** | 已经装好的环境：续签 / trust / 升级 / 回滚 | [日常运维](../tutorials/install-package-day2-ops.md) |

**部署要做的事**（手工和自动化都是这一套）：装依赖 → 铺配置 → 迁移数据库 → 签发证书 → 按顺序拉起进程 → 探活。逐步命令见[手工部署](../tutorials/manual-deploy-from-app-thin-package.md)。

**Ansible 自动化**：组装 `acps-image-install-*` / `acps-host-install-*` → 控制节点跑 `ansible-playbook playbooks/site.yml` → 装完后用 `renew-certs.yml` / `refresh-trust-bundle.yml` / `upgrade.yml` / `rollback.yml`（**不要**用完整 `site.yml` 当日常升级）。它额外解决多机编排、操作系统差异适配和幂等升级回滚。

如果你只是想开始参与开发，优先读开发测试文档。要交付环境：目标机在支持矩阵内、又是多机，用 Ansible 安装包最省事；环境特殊或想先看清每一步，走手工部署。

## 2. 本地开发测试怎么开始

开发者通常需要在同一个工作区中放置多个 ACPs 项目，例如：

```text
acps/
  registry-server/
  ca-server/
  discovery-server/
  mq-auth-server/
  demo-partner/
  demo-leader/
  acps-cli/
  acps-sdk/
  acps-infra/
  acps-docs/
```

多数 Python 服务项目统一使用 `uv` 管理 Python 与依赖，使用 `just` 统一开发、测试、质量检查命令。第一次进入某个服务项目时，一般是：

```bash
cp .env.example .env
# 按项目需要填写数据库、LLM、RabbitMQ、证书等配置

just dev start
```

常用检查与测试命令：

```bash
just dev check
just test unit
just test integration
just test e2e
just qa
```

这些命令在不同项目里的细节略有差异，但整体模型一致：`infra -> prep -> dev -> test -> qa`。完整解释请看 [开发与测试总览](../development/development-testing-overview.md)。

## 3. 用安装包部署

产品路径是组装安装包后在控制节点跑 Ansible：

```bash
# 组装（在 acps-infra/release/install-packaging）
./scripts/build-install-package.sh --mode image --target-platform linux/amd64   # 或 --mode host …
# 控制节点解包后：
cd "$PKG/ansible"
cp inventories/hosts.example.yml inventories/hosts.yml   # 或多机 hosts-multi.example.yml
cp inventories/secrets.example.yml inventories/secrets.yml
ansible-playbook -i inventories/hosts.yml playbooks/site.yml -e @inventories/secrets.yml
ansible-playbook -i inventories/hosts.yml playbooks/business.yml -e @inventories/secrets.yml   # demo 双开后
```

逐步说明见 [组装安装包](../tutorials/install-package-build.md) 与 [Ansible 部署教程](../tutorials/install-package-ansible-deploy.md)。  
同一批机器要「从零再装」见 [清场教程](../tutorials/install-package-clean-slate.md)。  
装完后的续签 / trust / 升级 / 回滚见 [日常运维教程](../tutorials/install-package-day2-ops.md)。

## 4. 开发智能体从哪里继续

环境准备好之后，如果你的目标是开发 Leader / Partner 智能体，请继续阅读 [AIP 开发教程](../tutorials/agent-development.md)。  
若还要让 Agent 的运行情况可被 Monitor / Discovery 看见，请再读 [AMP 可观测性教程](../tutorials/amp-agent-observability.md)。

教程只讲代码和协议理解，不再重复开发环境搭建、打包、部署步骤。环境、测试和部署问题分别回到本文上面的文档中查。
