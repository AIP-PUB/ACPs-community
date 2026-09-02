# ACPs install-packaging

本树是 ACPs **安装层**源码：消费 `image-packaging` / 应用发布包 / 厂商包制品，用 Ansible 完成 **image-mode**（Docker Compose）与 **host-mode**（venv/systemd + OS 包 + vendor tarball）首装与运维。

逐步教程（推荐阅读顺序）：

- 组装安装包 → [`acps-docs/tutorials/install-package-build.md`](../../../acps-docs/tutorials/install-package-build.md)
- 首装部署 → [`acps-docs/tutorials/install-package-ansible-deploy.md`](../../../acps-docs/tutorials/install-package-ansible-deploy.md)
- 日常运维（续签 / trust / 升级 / 回滚）→ [`acps-docs/tutorials/install-package-day2-ops.md`](../../../acps-docs/tutorials/install-package-day2-ops.md)

下文为包内权威开关与命令；教程不替代本 README 的细节表。

## 边界

| 本树做什么 | 本树不做什么 |
| --- | --- |
| image-mode 首装编排（`acps_deploy_mode: image`） | 镜像构建（见 `../image-packaging/`） |
| host-mode 首装编排（`acps_deploy_mode: host`；Rocky 8/9、Ubuntu 20.04/22.04） | 厂商包下载管线（构建期自备 `vendor-bundle-dir`） |
| 组装安装包所需的 ansible / templates / scripts | 应用发布包装配（见 `../app-packaging/`） |
| 证书 provision / 临期续签 / trust-bundle 刷新 / 基础 smoke | CH/OS TLS（已延期）；常驻续签守护 |
| 版本状态登记；image / host 组件升级与制品回滚 | 自动 DB schema 回滚；默认升级路径重签证书；os_package 自动降包 |

**不回流打包**：不修改业务 wheel 构建；不把拓扑写进镜像包。

## Playbook 入口一览

| 用途 | Playbook | 说明 |
| --- | --- | --- |
| 首装 | `playbooks/site.yml` | 完整安装；**不是**日常升级入口 |
| 预检 | `playbooks/preflight.yml` | 集群预检 |
| 临期续签 | `playbooks/renew-certs.yml` | 按临期窗口续签叶子证书 |
| trust-bundle 刷新 | `playbooks/refresh-trust-bundle.yml` | 分发信任链；不重签叶子 |
| 版本状态回填 | `playbooks/register-state.yml` | 回填组件版本状态；不 recreate |
| 组件升级 | `playbooks/upgrade.yml` | image / host 共用；须显式组件列表 |
| 制品回滚 | `playbooks/rollback.yml` | image / host 共用；切回 `previous` |
| Smoke | `playbooks/smoke.yml` | **basic only**（产品路径） |
| 业务验收 | `playbooks/business.yml` | demo 后可选；固定 Step A–D；须双开 demo |
| Discovery alive 等待 | `playbooks/wait-discovery-alive.yml` | **可选**；day2 / 强 recreate 后、`business.yml` 前的 per-AIC 就绪门禁（仅 D2；默认 480s + 失败后 settle 再试一次） |

### 禁令（升级 / 回滚）

- **禁止** `docker compose down -v`（及等价毁卷）：升级与回滚仅 `up` / `up-recreate`，保留数据目录与命名卷。
- **禁止**把完整 `site.yml` 当升级入口；改拓扑 / 全新安装才再跑首装。
- **禁止**默认把证书续签绑进每次升级；续签 / 刷链走独立 playbook。
- **不**自动 `migrate downgrade`；回滚不承诺撤销已执行的正向迁移。

## 最低工具

- Ansible：`ansible-core` **≥ 2.16**（推荐 2.18.x）；装在**控制节点**
- **控制节点** Python **3.11+**（`tomllib`）：跑 `scripts/manifest_lookup.py` 等；推荐解包后先跑 `scripts/bootstrap_control_ansible.sh`（需 uv）
- **目标机**：Docker Engine + Compose v2 插件；系统 `python3` 可用即可（`resolve_image_uid.py` **不**要求 3.11 / `tomllib`）
- 本仓开发门控：`./scripts/syntax-check.sh`（可用树内 `.venv-tools`；含 baseline ↔ image 版本对齐断言）
- 仅跑版本对齐：`./scripts/assert-baseline-image-alignment.sh`（S12 子集：`--s12-only`）
- **操作文档索引**：[`docs/README.md`](./docs/README.md)；已知限制：[`docs/known-limitations.md`](./docs/known-limitations.md)

## Inventory 示例

| 文件 | 用途 |
| --- | --- |
| `inventories/hosts.example.yml` | image 单机 |
| `inventories/hosts.rocky9.yml` / `hosts.ubuntu22.yml` / `hosts.rocky8.yml` / `hosts.ubuntu20.yml` | host 单机 |
| `inventories/hosts.4os-multi.yml` + `host_vars/*.acps.local.yml` | host 四 OS 混部（每组件一组一机） |
| `inventories/hosts-multi.example.yml` | image 多机（app + deps） |
| `inventories/host_vars/*.example.yml` | 多机 advertise / SSH 示例 |

## 组装 → 解压验收（闭环）

**推荐路径**：正式 `image-packaging` 平铺长名 `*.image.tar.gz` + 应用发布包目录 → **单一脚本** `build-install-package.sh`。包内保留长名；不做短名改名；不在本树 pull/save demo-nginx / fluent-bit。

```bash
cd release/install-packaging

APP_RELEASE_DIR=/tmp/acps-app-release-output
IMAGE_OUT=/tmp/acps-image-packages

# Mac 控制节点示例：镜像 linux-arm64，CLI darwin-arm64
./scripts/build-install-package.sh \
 --image-dir "$IMAGE_OUT" \
 --app-release-dir "$APP_RELEASE_DIR" \
 --image-platform linux-arm64 \
 --control-platform darwin-arm64 \
 --out-dir ./dist
# → dist/acps-image-install-{version}-{image_platform}.tar
```

远端 amd64 业务机、本机仍作控制节点时：`--image-platform linux-amd64`，`--control-platform` 仍用本机 CLI（如 `darwin-arm64`）。

教程：[组装安装包（image / host）](../../../acps-docs/tutorials/install-package-build.md) · [Ansible 部署](../../../acps-docs/tutorials/install-package-ansible-deploy.md)。

```bash
# 3) 模拟传输到控制节点：解压到独立目录（禁止直接用 git 源码树验收）
rm -rf /tmp/acps-image-install-verify
mkdir -p /tmp/acps-image-install-verify
tar -xf ./dist/acps-image-install-*.tar -C /tmp/acps-image-install-verify
PKG=/tmp/acps-image-install-verify/acps-image-install-*-linux-arm64

# 4) 配置 inventory（本机单机：控制节点=业务节点）
cp -a "$PKG/ansible/inventories/hosts.example.yml" "$PKG/ansible/inventories/hosts.yml"
cp -a "$PKG/ansible/inventories/secrets.example.yml" "$PKG/ansible/inventories/secrets.yml"
# 编辑 secrets.yml；确认 host_vars/acps-node-1.yml、monitor_server_enabled=true、keycloak_enabled=false
# CA 中间证书：放到 inventories/ca-materials/（ca.crt / ca.key / root-ca.crt）
# "$PKG/scripts/generate_ca_materials.sh" --out "$PKG/ansible/inventories/ca-materials/"

# 5) 验收前清空预装 acps/*，强制走包内 docker load
docker images --format '{{.Repository}}:{{.Tag}}' | grep '^acps/' | xargs -r docker image rm -f

# 6) 仅用解压目录内 ansible + artifacts 部署
export ANSIBLE_CONFIG="$PKG/ansible/ansible.cfg"
cd "$PKG/ansible"
ansible-playbook playbooks/site.yml -i inventories/hosts.yml -e @inventories/secrets.yml
# 终态收敛再跑一次（无材料/配置变更时应少 recreate；勿把 site 当 upgrade）
# 日志应出现 docker load，且无「using local acps-cli source」
```

## host-mode 首装（Rocky 8/9 · Ubuntu 20.04/22.04）

教程入口：[组装安装包 §2](../../../acps-docs/tutorials/install-package-build.md) · [Ansible 部署（〔host〕分岔）](../../../acps-docs/tutorials/install-package-ansible-deploy.md)。

### 拓扑

| 角色 | 主机 | 说明 |
| --- | --- | --- |
| 构建 + Ansible 控制节点 | **本机 macOS** | 打 host 安装包、跑 `ansible-playbook`；**不作**业务节点 |
| 业务节点 A | `rhel9.acps.local` | Rocky Linux 9 / RHEL 9 系列；单机承载全部组件 |
| 业务节点 B | `ubuntu22.acps.local` | Ubuntu 22.04；单机承载全部组件 |
| 业务节点 C | `rhel8.acps.local` | Rocky Linux 8 / RHEL 8 系列；单机承载全部组件 |
| 业务节点 D | `ubuntu20.acps.local` | Ubuntu 20.04；单机承载全部组件 |

业务节点须从控制节点**无密 SSH + 无密 sudo** 可达。inventory 示例：

- `ansible/inventories/hosts.rocky9.yml` / `hosts.ubuntu22.yml`
- `ansible/inventories/hosts.rocky8.yml` / `hosts.ubuntu20.yml`（须设独立 `acps_control_root`）
- `ansible/inventories/hosts.4os-multi.yml`（四 OS 混部；配套 `host_vars/*.acps.local.yml`，python3.8 仅写在 rhel8 host_vars）

（复制为 `hosts.yml` 并替换 SSH 用户；`secrets.yml` 见 `secrets.example.yml`。）

**四 OS 验收**：host 路径完整验收须在 **rocky8 / rocky9 / ubuntu20 / ubuntu22 各自**跑通；扩展新 OS 时至少新 OS 单机绿灯，且既有 Rocky9/Ubuntu22 默认制品（jammy fluent-bit 等）零变更。

### 前置条件

| 类别 | 要求 |
| --- | --- |
| **控制节点** | Ansible `ansible-core` ≥ 2.16；Python 3.11+（`tomllib`）；`scripts/bootstrap_control_ansible.sh` |
| **业务节点 OS** | Rocky Linux 8/9 或 Ubuntu 20.04/22.04（`acps_os_id` ∈ baseline-matrix `[os_whitelist]`）；systemd；白名单外发行版不支持。**Rocky 8** 须预装 **Python ≥3.8**（`python38` 模块；`hosts.rocky8.yml` 已设 `ansible_python_interpreter: /usr/bin/python3.8`），因 ansible-core ≥2.16 不支持托管节点 Python 3.6 |
| **OS 在线源** | PG（PGDG + pgvector；**Ubuntu 20 走 apt-archive**，live focal 已下线）、Redis ≥ 7（Ubuntu：`packages.redis.io`；**Rocky 8：Remi `redis:remi-7.2`**；Rocky 9：AppStream `redis:7`）、RabbitMQ（Team RabbitMQ 源；**Ubuntu 20 钉 `rabbitmq-server=4.2.8-1`**，因 focal erlang 最高 26.x）；`tools/` **不能**替代 apt/dnf |
| **厂商 tarball** | 构建机默认缓存 `release/install-packaging/.vendor-bundle/`；`build-install-package.sh --mode host` 会按 `baseline-matrix.toml` 的 url/sha256 **自动下载缺失项**（可手动预置加速；`--vendor-offline` 仅用缓存）。含 **Temurin JRE17**（Keycloak `JAVA_HOME`）。`amp_forwarder` 为双制品：jammy（rocky9/ubuntu22）+ bionic/glibc228（rocky8/ubuntu20） |
| **Java（Keycloak）** | host：安装包内 **Temurin JRE17** vendor（`[vendor.temurin_jre17]`），role 写入 `JAVA_HOME`，**不再**在线装 OpenJDK OS 包。OpenSearch 仍用官方 tarball 自带 JDK。image 模式 Keycloak 镜像自带 JRE |
| **Python 离线（可选）** | `--bundle-python-dir` 打入 `tools/`（pinned uv + CPython）；否则 role 按 `baseline-matrix [python]` 在线装 |
| **出网 MTU（LLM/embedding）** | 宿主机业务网卡建议 **MTU ≤1400**（现场网络项）。image-mode：共享网 `acps-net` 设为 `acps_docker_mtu`（默认 **1400**）；**Linux Engine** 另合并 `/etc/docker/daemon.json` mtu；**Docker Desktop** 跳过 daemon.json。已有错误 MTU 的网络需 `-e acps_docker_network_recreate=true` 重建。可用 `ping -M do -s 1372 <gateway-ip>` 自检 |

preflight 会校验 `mode ∈ {image,host}`、OS 白名单（`acps_os_id`）、OS 源可达（`preflight_host_os_repos.yml`）、rocky8/ubuntu20 上 vendor glibc 早期门禁、vendor 文件存在（安装包内）。

须从**源树**跑 `build-install-package.sh --mode host` 重建安装包后再验；禁止只改解包树里的 ansible/matrix 却沿用旧 `artifacts/vendor`。

### 组装 host 安装包

```bash
cd release/install-packaging

APP_RELEASE_DIR=/tmp/acps-app-release-output

# 默认缓存 .vendor-bundle/：缺失则按 baseline-matrix url 下载并校验 sha256
./scripts/build-install-package.sh \
  --mode host \
  --app-release-dir "$APP_RELEASE_DIR" \
  --target-platform linux-amd64 \
  --control-platform darwin-arm64 \
  --out-dir ./dist
# → dist/acps-host-install-{version}-linux-amd64.tar

# 可选：显式缓存目录 / 气隙
#   --vendor-bundle-dir /path/to/cache
#   --vendor-offline

# 也可单独预取：
# python3 scripts/ensure_vendor_bundle.py --matrix baseline-matrix.toml --arch amd64 --cache-dir .vendor-bundle
```

产物含 `artifacts/apps|control|vendor/`、`baseline-matrix.toml`、构建期 `release-manifest.toml`（含 `artifact_kind`）、`ansible/`、`templates/`、`scripts/`；**无** `images/`、**无** `bin/acps-install`。

`baseline-matrix.toml` 为每个 `[vendor.*]` 钉 `url`（或 `url_amd64`/`url_arm64`）与 `sha256_amd64`/`sha256_arm64`（或统一 `sha256`）；部分组件用 `fetch=` 做下载后变换（`clickhouse_flat` / `minio_wrap` / `fluentbit_deb`）。`amp_forwarder` 另可钉 `file_glibc228` / `url_glibc228` / `sha256_*_glibc228`（rocky8/ubuntu20 选型）。

### 解压 → 多 OS 部署（示意）

```bash
rm -rf /tmp/acps-host-install-verify
mkdir -p /tmp/acps-host-install-verify
tar -xf ./dist/acps-host-install-*.tar -C /tmp/acps-host-install-verify
PKG=/tmp/acps-host-install-verify/acps-host-install-*-linux-amd64

cp -a "$PKG/ansible/inventories/secrets.example.yml" "$PKG/ansible/inventories/secrets.yml"
# 编辑 secrets.yml；CA 材料放 inventories/ca-materials/

export ANSIBLE_CONFIG="$PKG/ansible/ansible.cfg"
cd "$PKG/ansible"

# 连通性（每台业务机各跑一次）
ansible -i inventories/hosts.rocky9.yml all -m ping
ansible -i inventories/hosts.ubuntu22.yml all -m ping
ansible -i inventories/hosts.rocky8.yml all -m ping
ansible -i inventories/hosts.ubuntu20.yml all -m ping

# 首装（各 OS 须分别执行；以下为 Rocky 9 示例）
ansible-playbook -i inventories/hosts.rocky9.yml playbooks/preflight.yml -e @inventories/secrets.yml
ansible-playbook -i inventories/hosts.rocky9.yml playbooks/site.yml -e @inventories/secrets.yml
ansible-playbook -i inventories/hosts.rocky9.yml playbooks/smoke.yml -e @inventories/secrets.yml
# 产品路径（demo 双开 + LLM + AMP 全量后）：business.yml

# 其它 OS：换 hosts.ubuntu22.yml / hosts.rocky8.yml / hosts.ubuntu20.yml 重复上述命令
```

规范工作目录为解压包内 **`ansible/`**；入口为 `ansible-playbook playbooks/site.yml`（**不是** `bin/acps-install`）。

### host-mode 能力摘要

- **承载**：应用 venv/systemd、OS 包 PostgreSQL/Redis/RabbitMQ、Keycloak/AMP vendor、demo host 路径均由 Ansible role 落地。
- **四 OS**：Rocky 8/9 + Ubuntu 20.04/22.04 各自支持 `preflight` / `site` / `smoke` / `business.yml`（A–D）。Ubuntu 20 PG 依赖 apt-archive（无持续安全更新）；Rocky 8 Redis 走 Remi。
- **业务验收 knobs**：`secrets.example.yml` 含 `discovery_skip_cpu_llm` / `embedding_timeout` / `acps_llm6_force_fallback` / `partner_force_accept_decision`；Step A 默认跳过 `run-sync`（`ACPS_FORCE_DISCOVERY_SYNC=1` 强制同步）。
- **image-mode demo Web**：使用 `demo-leader-web`（无独立 demo-nginx）；验收顺序为 `site.yml` → `smoke.yml` → `business.yml` A–D，并可检查 `http://<host>:9030/api/v1/health`。
- **升级 / 回滚**：`upgrade.yml` / `rollback.yml` 支持显式组件列表（含应用与部分 vendor）；**四 OS**（rocky8/9、ubuntu20/22）host 路径可用。ClickHouse / OpenSearch 当前为明文 HTTP。vendor 下载见 `scripts/ensure_vendor_bundle.py`。
- **多 OS 串行注意**：同一 controller 的 `~/.local/share/acps/control/work/certs` leaf marker 会跨业务机复用；换 OS/主机名（advertise / SAN）后 `cert_provision` 会按指纹自动重签。issuer 变更同样自动清空并重签。证书/trust 落地后重启直接消费方及依赖闭包。`smoke_basic` / renew / refresh 对 Redis TLS、AMQPS、mq-auth mTLS、registry:9002、demo HTTPS 做 fail-closed 握手验收。各 OS 拓扑须用独立 `acps_control_root`（见 `hosts.rocky8.yml` / `hosts.ubuntu20.yml`）。
- **勿**对混合 localhost+远程 inventory 全局 `-e acps_python_bin=/opt/...`（会污染控制节点 CLI 安装）；依赖 `install_python` 按主机产出 fact。

## 快速本地开发验证（非闭环门）

仅编排调试时可在源码树内跑（允许 `acps_cli_allow_source_fallback=true`），**不能**替代安装包闭环验收：

```bash
cd release/install-packaging
cp -a ansible/inventories/hosts.example.yml ansible/inventories/hosts.yml
cp -a ansible/inventories/secrets.example.yml ansible/inventories/secrets.yml
./scripts/syntax-check.sh
```

## 临期续签

首装完成后，按临期窗口扫描叶子证书并续签分发（未临期且未 force 则跳过）。**续签保持 Agent AIC 不变**（同 AIC `cert renew`；禁止因 force 对已注册 Agent delete+recreate）。**扫描不含 advertise/SAN 漂移**；修 SAN 请走 `site.yml`（可能换 AIC），勿宣称「renew 已修 SAN」。

```bash
cd "$PKG/ansible" # 本地开发也可用 release/install-packaging/ansible
ansible-playbook -i inventories/hosts.yml playbooks/renew-certs.yml \
 -e @inventories/secrets.yml
# 强制重签叶子（仍保 AIC）：-e acps_force_cert_renew=true
# 仅部分 profile：-e '{"acps_cert_renew_profiles":["registry-9002"]}'
```

## trust-bundle 刷新

从 inventory 中的 `ca_server` 拉取最新信任材料并分发（按 profile 文件名：`trust-bundle.pem` / `acps-root-ca.pem`）。**不**重签叶子证书：

```bash
cd "$PKG/ansible"
ansible-playbook -i inventories/hosts.yml playbooks/refresh-trust-bundle.yml \
 -e @inventories/secrets.yml
# 强制覆盖：-e acps_force_trust_bundle_refresh=true
# 仅部分消费方：-e '{"acps_trust_bundle_profiles":["registry-9002","redis"]}'
```

## 版本状态登记

首装成功路径在 `deploy_compose_service` / `control_acps_cli` 写入 `current`。对已有栈可回填（不 recreate）：

```bash
cd "$PKG/ansible"
ansible-playbook -i inventories/hosts.yml playbooks/register-state.yml \
 -e @inventories/secrets.yml
# 子集：-e '{"acps_register_components":["registry_server","discovery_server_cpu"]}'
```

路径：`{{ acps_runtime_root }}/state/components/<component>.json`、`…/state/control/acps_cli.json`。

## 组件升级

独立入口（**不是**再跑完整 `site.yml`）。按组件升级制品与进程；保留数据目录与证书；Compose 仅 `up` / `up-recreate`（**禁止** `down -v`）。默认**不**调用叶子续签。CSV 含 `ca_server` 时目标契约为默认 follow trust 刷新（`acps_upgrade_follow_refresh_trust`；见 `docs/known-limitations.md`）。**升级/回滚成功后勿用 `site.yml` 盖回。**

```bash
cd "$PKG/ansible" # 本地开发也可用 release/install-packaging/ansible
ansible-playbook -i inventories/hosts.yml playbooks/upgrade.yml \
 -e @inventories/secrets.yml \
 -e acps_upgrade_components=registry_server
# 多组件（顺序对齐 C 阶段依赖）：-e acps_upgrade_components=registry_server,ca_server
# 跳过升级后 smoke（不推荐）：-e acps_upgrade_skip_smoke=true
# discovery 可用别名 discovery_server（展开为 discovery_server_{{ variant }}）
```

`acps_upgrade_components` **必填**（显式更安全）。流程：preflight → `save_previous_state` → stage/load → render → migrate（角色内 alembic / 钩子占位）→ recreate → health/smoke → `commit_state`。

### host-mode 升级 / 回滚（S5 已定稿）

`acps_deploy_mode: host` 时同一入口；应用落 `releases/<ver>/` + `current` symlink，unit 经 `current`。首次升级会把扁平布局迁入该结构。

```bash
# 业务机（示例 ubuntu22 / rocky9 inventory）
ansible-playbook -i inventories/hosts.ubuntu22.yml playbooks/upgrade.yml \
  -e @inventories/secrets.yml \
  -e acps_upgrade_components=registry_server,ca_server
# 含 CLI 时 CLI 先于业务 app：
# -e acps_upgrade_components=acps_cli,registry_server
# 同 artifact 强制重装：-e acps_force_app_reinstall=true

ansible-playbook -i inventories/hosts.ubuntu22.yml playbooks/rollback.yml \
  -e @inventories/secrets.yml \
  -e acps_rollback_components=registry_server,ca_server
# 若 state.current.migrate_id 新于 previous：须显式确认（不 downgrade schema）
# -e acps_rollback_acknowledge_migrate=true
```

host 要点：`artifact_id` 未变且未 force → no-op（仍可 health）；migrate 失败不切 `current`；回滚只切制品指针，**不**执行 alembic downgrade。

**vendor_bundle（host）**：`keycloak`（依赖同包内 `temurin_jre17`，不单独进 CSV）/ `redpanda` / `victoria_metrics` / `clickhouse` / `minio` / `opensearch` / `amp_forwarder` 可在 `upgrade.yml` / `rollback.yml` 的组件 CSV 中选中；切流经 `releases/`+`current`，**不**清空 `acps_data_root`。`amp_forwarder` 亦随 `site.yml` Forwarder 阶段首装（tag `phase_14_amp_forwarder`）；同 artifact 默认 upgrade 为 no-op（不写 `previous`），演练回滚可用 `-e acps_force_vendor_reinstall=true` 留 previous。详见 `docs/known-limitations.md`。

**os_package（PostgreSQL / Redis / RabbitMQ）**：

- 升级：`ensure_repos →`（PG）**主版本门控** → `package state=latest`（同 major 小版本）→ 配置幂等渲染 → restart → health；state 记**包版本字符串**，**无** `releases/`。
- PostgreSQL：已装 cluster major（`PG_VERSION`）须等于 `postgresql_os_major_version`（baseline `17`）；漂移 → **fail fast**，提示人工 `pg_upgrade` / dump-restore；**不**改 data 目录。
- 回滚：对 `postgresql` / `redis` / `rabbitmq` **默认拒绝**（不自动 `dnf`/`apt` 降包）。请从 `acps_rollback_components` 去掉这些组件，或人工恢复包集。
- 负向门控（本机可跑）：`./scripts/h4_s4_pg_major_neg_check.sh`（日志 `/tmp/acps-h4-s4-pg-major-neg.log`）。

```bash
# 示例：同 major 小版本升级 PG（须已 host 首装）
ansible-playbook -i inventories/hosts.ubuntu22.yml playbooks/upgrade.yml \
  -e @inventories/secrets.yml \
  -e acps_upgrade_components=postgresql
# 下列会失败（产品拒绝自动降包）：
# -e acps_rollback_components=postgresql
```

## 制品回滚

独立入口（**不是** `site.yml` / 也不是自动连锁回滚）。将所选组件运行指针切回状态中的 `previous`（或显式 `acps_rollback_to` 且须匹配该 previous）；**不**自动回滚 DB/schema；**不**把证书纳入默认回滚集；Compose 仅 `up` / `up-recreate`（**禁止** `down -v`）。失败即停并保留现场。**回滚后勿用 `site.yml` 盖回**（易破坏回滚意图）。

```bash
cd "$PKG/ansible" # 本地开发也可用 release/install-packaging/ansible
ansible-playbook -i inventories/hosts.yml playbooks/rollback.yml \
 -e @inventories/secrets.yml \
 -e acps_rollback_components=registry_server
# 可选目标（须等于 retained previous.version 或 previous.image_tag）：
# -e acps_rollback_to=2.2.0
# 跳过回滚后 smoke（不推荐）：-e acps_rollback_skip_smoke=true
```

`acps_rollback_components` **必填**。流程：preflight → 读 state → 校验 previous / 镜像可用（本机 tag 或包内 artifact）→ 复用组件 role（pin `acps_image_tag`）→ recreate → health/smoke → `commit_state`（`current=restored`，`previous` 清空）。同 tag 升级后的回滚仍可演练全路径（指针切换 + state 更新），但不构成跨版本制品证据。

## AMP Forwarder

Demo 部署后，安装层用 **Fluent Bit**（`roles/amp_forwarder`）tail Leader/Partner 的六类 `amp_*.jsonl`，转发到 Redpanda 的 `amp.audit` / `amp.access` / `amp.message` / `amp.metrics` / `amp.heartbeat` / `amp.system`。应用进程不直连 Kafka。

| 项 | 约定 |
| --- | --- |
| 形态 | **独立 compose 服务** + **主机共享日志目录**（非 demo sidecar）。demo compose 将 `/opt/acps/app/logs` bind 到 `{{ acps_log_root }}/demo_{leader,partner}`；Forwarder 只读挂载同一目录。 |
| 何时部署 | `site.yml` 中 AMP Forwarder 阶段；tag `phase_14_amp_forwarder`（在 demo 阶段之后）。demo 均关闭时 role no-op。 |
| Broker | 由 inventory 推导：与 Redpanda 同机用 `redpanda:9092`，跨机用 `acps_group_addr(redpanda):redpanda_kafka_port`。 |
| 镜像 | 来自 image-packaging 的 `acps/fluent-bit:…`（manifest `[images.fluent_bit]`）。须在组装安装包前打进镜像包。 |
| Kafka topics | `roles/redpanda` 在 up 后幂等预建六类 `amp.*` + DLQ + `amp.heartbeat.alive-delta`（分区 / `LogAppendTime` 对齐 `dev-infra.sh`）。非 audit Forwarder 关闭 auto-create。 |
| Monitor Writers | `monitor_server` 生产 overlay（`production.toml.j2`）指向安装态 Redpanda；message/system `writer_enabled=true`；heartbeat 入站分区与建题一致。 |
| Monitor→Discovery 心跳同步 | 见下一节（Relay + alive-sync）。 |

```bash
# 仅部署 / 重跑 Forwarder（demo 已启用且日志目录已存在）
cd "$PKG/ansible"
ansible-playbook -i inventories/hosts.yml playbooks/site.yml \
 -e @inventories/secrets.yml --tags phase_14_amp_forwarder
```

未补齐 Forwarder + topics + Writers + 心跳同步前，不得宣称业务验收通过。

## Monitor→Discovery 心跳同步

安装层**显式**配置并启用 Monitor Heartbeat Relay 与 Discovery alive-sync（由 inventory 渲染进 TOML，**不要**进容器手改配置作为主路径）。

| 项 | 约定 |
| --- | --- |
| Monitor Relay | `roles/monitor_server/templates/production.toml.j2`：`[heartbeat] sync_enabled=true`、`delta_topic=amp.heartbeat.alive-delta`、分片/入站分区与建题一致；Kafka bootstrap 同机 `redpanda:9092` / 跨机 `acps_group_addr(redpanda):redpanda_kafka_port`。 |
| Discovery alive-sync | `roles/discovery_server/templates/production.toml.j2`：`monitor_server_enabled=true` 时 `enabled=true` + `auto_start=true`；`provider_base_url` → Monitor `/acps-amp-v1/heartbeat`（同机 Compose DNS `monitor_server:9009` / 跨机 advertise）；`kafka_bootstrap_servers` / `kafka_topic=amp.heartbeat.alive-delta` / `kafka_group_id=discovery-server.alive-sync.v1`。 |
| TLS / 认证 | Kafka **PLAINTEXT**（与 Writers/Forwarder 一致）。Bootstrap 走 Monitor `/sync/info` + `/sync/snapshot`：共享服务间 Bearer（`secrets.yml` → `monitor_heartbeat_sync_internal_token` → Monitor `HEARTBEAT_SYNC_INTERNAL_TOKEN` + Discovery `ALIVE_SYNC_PROVIDER_BEARER_TOKEN`）。**Keycloak OIDC on 时必填**（Monitor `/sync/*` 否则需 operator JWT）；OIDC off 时无 Bearer 仍可用，但安装层仍要求该 secret（roles assert）。人类 operator 也可凭 OIDC Bearer 调 `/sync/*`。 |
| 业务验收矩阵 | 启用 demo（`demo_leader` + `demo_partner`）且要跑业务验收时，须同时启用 **Monitor + Discovery + 本同步链路**（及 Forwarder / topics / Writers）。**支持 Keycloak**（共享 sync token）。缺任一则不得宣称业务验收通过。 |
| D2 通过条件 | **只**在 Discovery 查心跳/存活（`/admin/alive-sync/status` 的 `aliveCount` / discover `aliveMap`）。**禁止**用 Monitor heartbeat summary / Query API 作为 D2 通过条件。失败信息指向 **Monitor Relay** 或 **Discovery alive-sync consumer**。 |

```bash
# 门控（示意；控制面可达时）
# 1) Monitor Provider（非 D2；仅确认 Relay/Sync Profile）
# Keycloak/OIDC on：须带共享 sync Bearer（与 secrets.yml 一致）
curl -s -H "Authorization: Bearer <monitor_heartbeat_sync_internal_token>" \
 "http://<monitor>:9009/acps-amp-v1/heartbeat/sync/info" | python3 -m json.tool
# 期望 type=amp-alive-delta kafkaTopic=amp.heartbeat.alive-delta

# 2) Discovery consumer（D2 路径）
curl -s "http://<discovery>:9005/admin/alive-sync/status" | python3 -m json.tool
# 期望 running=true 且 checkpointCount>=1；demo+Forwarder 心跳周期后 aliveCount>=1（全量 Leader+Partner 属 ）

# 基础烟测在 Monitor+Discovery 均启用时自动探针 Sync Profile + running（Bearer 从 secrets 注入）
# demo 启用且跑完 Forwarder 后，site.yml 的 alive-sync 门控要求 aliveCount>=1
ansible-playbook -i inventories/hosts.yml playbooks/smoke.yml -e @inventories/secrets.yml
```

## LLM / secrets 注入

密钥写在 `inventories/secrets.yml`（**勿提交 git**；示例见 `secrets.example.yml`，占位符 only）。Role 渲染进各组件 env：

| 用途 | secrets 键 | 渲染目标 | 说明 |
| --- | --- | --- | --- |
| Discovery（CPU） | `discovery_llm_*`、`embedding_*` | `discovery_server` env | `discovery_server_variant: cpu`（默认）通常需要真实密钥才能 NL 查询命中 |
| Discovery（GPU） | 同上键可仍渲染 | 同上 | `discovery_server_variant: gpu` 通常**不**依赖外部 LLM；密钥可留占位 / 未用 |
| Demo Leader/Partner | `demo_llm_*`（共享默认）；可选 `demo_leader_llm_*` / `demo_partner_llm_*` 分 tier | `LEADER_LLM_*` / `PARTNER_LLM_*` | 分 tier 未设时回退 `demo_llm_*` |

**业务验收契约（`business.yml` Step A）**：

- **禁止**单独断言「Discovery LLM secrets 是否存在」作为通过/失败条件。
- Step A **只**要求对每个 demo Partner 的自然语言查询命中；密钥缺失或错误表现为查询未命中而失败。
- Step B/C 对话依赖 Leader/Partner LLM；未配置导致业务断言失败（同样不是单独的 secrets 存在性探针）。

运维提示：CPU Discovery 请在 `secrets.yml` 注入可用的 `discovery_llm_*` / `embedding_*`；GPU 变体可跳过。

## 业务验收（`business.yml`）

推荐顺序：

1. `site.yml`（含 demo + AMP Forwarder + alive-sync 门控）
2. `smoke.yml`（**basic only**）
3. （可选）`business.yml` — 固定 Step A→B→C→D 一次跑完
4. day2 / 强 recreate 后再跑 business 时：先 `wait-discovery-alive.yml`（仅 D2；force renew 后 aliveMap 可能滞后，默认 480s 超时且失败后 settle 90s 再试一次），再 `business.yml`

**无 demo 不可跑业务验收**：须 `demo_partner_enabled=true` **且** `demo_leader_enabled=true`；否则 `business.yml` 开头 assert 失败（安装成功仍只依赖基础烟测）。

```bash
cd "$PKG/ansible"
ansible-playbook -i inventories/hosts.yml playbooks/site.yml \
 -e @inventories/secrets.yml
ansible-playbook -i inventories/hosts.yml playbooks/smoke.yml \
 -e @inventories/secrets.yml
# 可选：demo 双开且已配 LLM / Forwarder / Writers / 心跳同步后
ansible-playbook -i inventories/hosts.yml playbooks/business.yml \
 -e @inventories/secrets.yml
# 期望：exit 0；A–D 全绿
```

约定：

- **首装不强制业务验收**：`site.yml` 不跑 `business.yml`；装完后可按需执行。
- **产品路径 = 一次整次流水线**：无「只跑某一段」的产品参数（无 `acps_business_tests=…`）；内部 `business_step_*` tags 仅供调试。
- **业务验收入口**：使用 `business.yml`（不要用已废弃的 `acps_smoke_kind=business` / `biz_*` 探针组）。
- **各仓 e2e 保留**：安装层业务验收不替代各应用仓 `tests/e2e`；二者互补。

## 已知限制

- 门控与 upgrade→rollback 闭环多为**同 tag**：验证路径与 state 指针，**非**跨版本制品证据。
- 本机闭环默认关闭 Monitor / OIDC / demo（可用 inventory 开启）。
- **host-mode**：`upgrade.yml` / `rollback.yml` 支持四 OS（rocky8/9、ubuntu20/22）；vendor 由 `ensure_vendor_bundle` 按 matrix url/sha256 缓存下载；出网若遇 PMTU 黑洞见前置条件 MTU≤1400。
- **image-mode**：demo Web 使用 `demo-leader-web`（无 demo-nginx）。
- PostgreSQL **主版本**升级：host 路径 **fail fast**（`check_postgresql_os_major.yml`；已装 `PG_VERSION` vs `postgresql_os_major_version` / baseline）；大版本须人工 `pg_upgrade` / dump-restore。负向门控：`./scripts/h4_s4_pg_major_neg_check.sh`。
- ClickHouse / OpenSearch 当前为明文 HTTP；证书续签走 `renew-certs.yml` / `refresh-trust-bundle.yml`（无内置常驻守护）。
- 逐步操作见 [docs/README.md](docs/README.md) 与 [acps-docs](../../../acps-docs/README.md)。
