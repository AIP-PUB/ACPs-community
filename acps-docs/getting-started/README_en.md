[Home](../README.md)

**[English](README_en.md) | [中文](README.md)**

# ACPs Quick Start

This document is the entry point for ACPs, helping you determine which path you should take: local development and testing, or deploying an environment. Deployment is further divided into **manual deployment** (the basic process) and **Ansible installation package deployment** (automation of the same process). The detailed steps for each path have been split into separate documents; this document only retains the roadmap and minimum commands.

See [`acps-infra/README.md`](../../acps-infra/README.md) for an overview of the concepts.

## 1. First Understand the Types of Work

| Goal | Suitable For | Detailed Documentation |
| --- | --- | --- |
| Local development and testing | Developers modifying services, SDKs, CLIs, and demo code | [Development and Testing Overview](../development/development-testing-overview_en.md) |
| **Developing Agents (AIP)** | Writing Leader / Partner business logic | [AIP Development Tutorial](../tutorials/agent-development_en.md) |
| **Agent Observability (AMP)** | Adding AMP logs in Agents and querying them | [AMP Observability Tutorial](../tutorials/amp-agent-observability_en.md) |
| **Manual Deployment (Basic Process)** | Those who want to understand exactly what deployment does, or whose target environment cannot use Docker / whose operating system is not in the support matrix | [Manual Deployment from Application Thin Package](../tutorials/manual-deploy-from-app-thin-package_en.md) |
| **Installation Package Ansible Deployment** | Builders / deployers of image-mode or host-mode environments | [Assemble Installation Package](../tutorials/install-package-build_en.md), [Ansible Deployment](../tutorials/install-package-ansible-deploy_en.md), [Three-Node Deployment](../tutorials/install-package-ansible-deploy-3nodes_en.md) |
| **Pre-acceptance / Pre-reinstallation Cleanup** | When the same batch of machines needs to be reinstalled from scratch (data will be destroyed) | [Cleanup Tutorial](../tutorials/install-package-clean-slate_en.md) |
| **Post-installation Day-to-Day Operations** | Already-installed environments: renewal / trust / upgrade / rollback | [Daily Operations](../tutorials/install-package-day2-ops_en.md) |

**What deployment needs to do** (the same process applies to both manual and automated deployment): install dependencies → deploy configuration → migrate the database → issue certificates → start processes in order → perform health checks. See [Manual Deployment](../tutorials/manual-deploy-from-app-thin-package_en.md) for step-by-step commands.

**Ansible automation**: assemble `acps-image-install-*` / `acps-host-install-*` → run `ansible-playbook playbooks/site.yml` on the control node → after installation, use `renew-certs.yml` / `refresh-trust-bundle.yml` / `upgrade.yml` / `rollback.yml` (**do not** use the full `site.yml` for routine upgrades). It additionally handles multi-machine orchestration, operating system differences, and idempotent upgrades and rollbacks.

If you simply want to start contributing to development, prioritize the development and testing documentation. To deliver an environment: if the target machines are in the support matrix and there are multiple machines, using the Ansible installation package is the easiest option; for special environments or if you want to understand each step first, use manual deployment.

## 2. How to Get Started with Local Development and Testing

Developers typically need to place multiple ACPs projects in the same workspace, for example:

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

Most Python service projects use `uv` to manage Python and dependencies, and use `just` to provide unified development, testing, and quality-check commands. When entering a service project for the first time, the typical steps are:

```bash
cp .env.example .env
# Fill in the database, LLM, RabbitMQ, certificate, and other configuration as needed by the project

just dev start
```

Common check and test commands:

```bash
just dev check
just test unit
just test integration
just test e2e
just qa
```

The details of these commands vary slightly across projects, but the overall model is consistent: `infra -> prep -> dev -> test -> qa`. See [Development and Testing Overview for a complete explanation](../development/development-testing-overview_en.md).

## 3. Deploy Using the Installation Package
The product deployment path is to assemble the installation package and then run Ansible on the control node:

```bash
# Assemble (in acps-infra/release/install-packaging)
./scripts/build-install-package.sh --mode image --target-platform linux/amd64   # or --mode host …
# After unpacking on the control node:
cd "$PKG/ansible"
cp inventories/hosts.example.yml inventories/hosts.yml   # or hosts-multi.example.yml
cp inventories/secrets.example.yml inventories/secrets.yml
ansible-playbook -i inventories/hosts.yml playbooks/site.yml -e @inventories/secrets.yml
ansible-playbook -i inventories/hosts.yml playbooks/business.yml -e @inventories/secrets.yml   # after running both demos
```

See [Assemble Installation Package](../tutorials/install-package-build_en.md) and [Ansible Deployment Tutorial for step-by-step instructions](../tutorials/install-package-ansible-deploy_en.md).  
To reinstall the same batch of machines "from scratch", see [Cleanup Tutorial](../tutorials/install-package-clean-slate_en.md).  
For certificate renewal / trust / upgrade / rollback after installation, see [Daily Operations Tutorial](../tutorials/install-package-day2-ops_en.md).  

## 4. Where to Continue with Agent Development
Once the environment is ready, if your goal is to develop Leader / Partner Agents, continue reading [AIP Development Tutorial](../tutorials/agent-development_en.md).  
If you also want the Agent's runtime status to be visible to Monitor / Discovery, read [AMP Observability Tutorial](../tutorials/amp-agent-observability_en.md) as well.   

The tutorials only cover code and protocol understanding and do not repeat development environment setup, packaging, or deployment steps. For environment, testing, and deployment issues, refer back to the documents above.



