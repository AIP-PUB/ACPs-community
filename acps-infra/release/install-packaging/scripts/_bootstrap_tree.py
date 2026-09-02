#!/usr/bin/env python3
"""install-packaging 一次性脚手架写入器（开发辅助；非运行时依赖）。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def w(rel: str, content: str) -> None:
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")
    print(f"wrote {rel}")


# --- top-level ---
w(
    ".gitignore",
    """\
.venv-tools/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
ansible/inventories/secrets.yml
ansible/inventories/hosts.yml
artifacts/images/*
!artifacts/images/.gitkeep
artifacts/control/*
!artifacts/control/.gitkeep
.runtime/
*.retry
""",
)

w(
    "README.md",
    """\
# ACPs install-packaging（image-mode）

本树是 ACPs **安装层**源码：消费 `image-packaging` / 应用发布包制品，用 Ansible + Docker Compose 完成 **image-mode** 首装。

## 边界

| 本树做什么 | 本树不做什么 |
| --- | --- |
| image-mode 首装编排（`acps_deploy_mode: image`） | 镜像构建（见 `../image-packaging/`） |
| 组装安装包所需的 ansible / templates / scripts | 应用发布包装配（见 `../app-packaging/`） |
| 证书 provision 编排与基础 smoke | host-mode / 升级回滚产品化（另开计划） |

**不回流打包**：不修改业务 wheel 构建；不把拓扑写进镜像包。

## 最低工具

- Ansible：`ansible-core` **≥ 2.16**（推荐 2.18.x）；控制节点预装
- 目标机：Docker Engine + Compose v2 插件
- 本仓开发门控：`./scripts/syntax-check.sh`（可用树内 `.venv-tools`）

## 快速本地验证

```bash
cd release/install-packaging
cp -a ansible/inventories/hosts.example.yml ansible/inventories/hosts.yml
cp -a ansible/inventories/secrets.example.yml ansible/inventories/secrets.yml
# 编辑 secrets.yml 替换 CHANGE_ME
chmod 600 ansible/inventories/secrets.yml

./scripts/syntax-check.sh
ansible-playbook -i ansible/inventories/hosts.yml ansible/playbooks/site.yml \\
  -e @ansible/inventories/secrets.yml
```


""",
)

w(
    "release-manifest.toml",
    """\
# 最小组件 → 镜像映射。
# Tags 匹配本地可用 acps/* arm64 镜像；可按需经 group_vars 覆盖。

[meta]
acps_version = "2.2.0"
platform = "linux-arm64"

[images.postgresql]
file = "postgres-pgvector.image.tar.gz"
tag = "acps/postgres-pgvector:17-bookworm-2.2.0-linux-arm64"
# run_user 运行时经 docker inspect 解析（勿硬编码 uid）

[images.redis]
file = "redis.image.tar.gz"
tag = "acps/redis:7-alpine-2.2.0-linux-arm64"
cert_owner_user = "redis"
# 设置 cert_owner_user 时从镜像 /etc/passwd 解析 uid:gid

[images.rabbitmq]
file = "rabbitmq.image.tar.gz"
tag = "acps/rabbitmq:4.2-management-alpine-2.2.0-linux-arm64"
cert_owner_user = "rabbitmq"

[images.registry_server]
file = "registry-server.image.tar.gz"
tag = "acps/registry-server:2.2.0-linux-arm64"
cert_owner_user = "acps"

[images.ca_server]
file = "ca-server.image.tar.gz"
tag = "acps/ca-server:2.2.0-linux-arm64"
cert_owner_user = "acps"

[images.discovery_server_cpu]
file = "discovery-server-cpu.image.tar.gz"
tag = "acps/discovery-server:2.2.0-linux-arm64-cpu"
cert_owner_user = "acps"

[images.discovery_server_gpu]
file = "discovery-server-gpu.image.tar.gz"
tag = "acps/discovery-server:2.2.0-linux-arm64-gpu"
cert_owner_user = "acps"

[images.mq_auth_server]
file = "mq-auth-server.image.tar.gz"
tag = "acps/mq-auth-server:2.2.0-linux-arm64"
cert_owner_user = "acps"

[images.monitor_server]
file = "monitor-server.image.tar.gz"
tag = "acps/monitor-server:2.2.0-linux-arm64"
cert_owner_user = "acps"

[images.keycloak]
file = "keycloak.image.tar.gz"
tag = "acps/keycloak:26.6.3-2.2.0-linux-arm64"

[images.redpanda]
file = "redpanda.image.tar.gz"
tag = "acps/redpanda:26.1.9-2.2.0-linux-arm64"

[images.victoria_metrics]
file = "victoria-metrics.image.tar.gz"
tag = "acps/victoria-metrics:1.111.0-2.2.0-linux-arm64"

[images.clickhouse]
file = "clickhouse.image.tar.gz"
tag = "acps/clickhouse:25.5-alpine-2.2.0-linux-arm64"

[images.minio]
file = "minio.image.tar.gz"
tag = "acps/minio:2025-04-22T22-12-26Z-2.2.0-linux-arm64"

[images.opensearch]
file = "opensearch.image.tar.gz"
tag = "acps/opensearch:2.19.2-2.2.0-linux-arm64"

[control.acps_cli]
# artifacts/control/ 下的 app-release tarball
file_glob = "acps-cli-*-app-release-*.tar.gz"
""",
)

print("bootstrap partial done")
