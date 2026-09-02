[首页](../README.md)

# 用安装包部署 ACPs（Ansible：image / host）

这篇教程面向：**会基本的 Linux 操作，但没接触过 Ansible**。你已经拿到一份安装包（`acps-image-install-*.tar` 或 `acps-host-install-*.tar`），要**从零装全套 ACPs**（含 Keycloak OIDC）。

安装包怎么来的见 [组装安装包](./install-package-build.md)。

playbook 自动执行的是 ACPs 的基础部署流程——装依赖、铺配置、迁移数据库、签发证书、按顺序拉起进程、探活。想知道每一步具体在做什么，或者某个环节失败了要手工介入，对照 [从应用薄包手工部署](./manual-deploy-from-app-thin-package.md)。

```text
源码 → 应用发布包 ─┬→ [image] 镜像包 → image 安装包 ─┐
                  └→ [host]  vendor  → host 安装包  ─┴→ 本教程（Ansible）
```

**两模式共用**：Ansible 概念、解包习惯、`secrets.yml`、CA 材料、`preflight` / `site.yml` / `business.yml`、demo Web 入口 **`:9030`**（同源 `/api`）。  
**两种模式不一样的地方**：业务机前置条件、inventory 示例、包内文件、装完后怎么检查。文中用 **〔image〕** / **〔host〕** 标出。

---

## 0. 先选 mode

| | **〔image〕** | **〔host〕** |
| --- | --- | --- |
| 安装包 | `acps-image-install-*.tar` | `acps-host-install-*.tar` |
| `acps_deploy_mode` | `image`（包内默认 / `hosts.example`） | `host`（`hosts.rocky9.yml` / `hosts.ubuntu22.yml` / `hosts.rocky8.yml` / `hosts.ubuntu20.yml` 已写好） |
| 业务机 | Docker Engine + Compose v2 | Rocky 8/9 **或** Ubuntu 20.04/22.04；systemd；**不要**指望用 Docker 跑业务进程 |
| 控制节点与业务同机 | 支持（§4.7，常见于 Mac 本机验收） | **不推荐 / 正式交付不支持**（业务机只支持 Linux 服务器：Rocky 8/9 / Ubuntu 20.04/22.04） |
| 升级 playbook | `upgrade.yml` / `rollback.yml` 已产品化（image + host） | 同左 |

选定后，下文凡未标注 〔image〕/〔host〕 的步骤，**两种模式一样做**。

---

## 1. 先弄懂 Ansible 是怎么干活的（30 秒版）

Ansible 是一个**在一台机器上、远程指挥其它机器装软件**的工具。记住几点就够：

1. **控制节点（Control Node）**：你敲命令的那台机器，装 Ansible。它通过 **SSH** 登录到业务节点去干活。
2. **业务节点（Managed Node / 目标机）**：真正跑 ACPs 的机器。**它上面不用装 Ansible**。
3. **Inventory（清单）**：写明「有哪些机器」「哪台机器扮演什么角色」。
4. **Playbook（剧本）**：按顺序执行的安装步骤（本项目是 `site.yml`）。
5. **变量**：`group_vars/all.yml`（全局默认）、`host_vars/<机器名>.yml`（连接与路径）、`secrets.yml`（密码密钥）。
6. **幂等**：同一个 playbook 反复跑结果一致——**跑失败了、改完配置再跑一遍**是正常操作。

```text
┌─────────────────────────┐        SSH         ┌──────────────────────────────┐
│  控制节点 (你的机器)     │ ─────────────────▶ │  业务节点                     │
│  • Ansible               │                    │  〔image〕Docker + Compose   │
│  • acps-cli（自动装）    │   服务端口访问     │  〔host〕venv/systemd + OS 包 │
│  • 给各组件签发证书      │ ◀───────────────── │    + 厂商包（Keycloak/AMP）   │
└─────────────────────────┘                    └──────────────────────────────┘
```

> 控制节点自己**不需要**在业务节点上装 Ansible。  
> 〔image〕控制节点**不需要 Docker**；〔host〕控制节点通常是 macOS/Linux 工作站，**不是** Rocky/Ubuntu 业务机本身。

---

## 2. 两类机器各自要准备什么

### 2.1 控制节点（共用）

- **Ansible**：`ansible-core` **≥ 2.16**（推荐 2.18.x），且跑 `ansible-playbook` 的 Python **≥ 3.11**（`tomllib`，供 `manifest_lookup.py`）。
- **推荐一键安装**（需已装 [uv](https://docs.astral.sh/uv/)）：解包后执行  
  `"$PKG/scripts/bootstrap_control_ansible.sh"`  
  （`uv tool install` 把 `ansible-core` 装到 CPython 3.14；失败信息也会指向该脚本）。
- **uv**（建议）：上条 bootstrap 依赖；也给自动安装的 `acps-cli` 备 Python 3.14。
- **SSH 能免密登录业务节点**，且该用户能 `sudo`（写 `/opt/acps` 等）。
- **网络能访问业务节点的服务端口**（签证书、跑 acps-cli）。

### 2.2 业务节点（两种模式要求不同）

**〔image〕**

- Docker Engine + Compose v2（`docker compose version` 能跑）。
- 部署用户能跑 `docker`（通常加入 `docker` 组后重新登录；仅 sudo 不够时 `ansible` 里 `become` 也调不到无密码的 docker socket）。
- 系统 `python3` 可用即可（不必 3.11）。
- CPU 架构与安装包匹配（`linux-amd64` / `linux-arm64`）。

**〔host〕**

- 支持的操作系统：**Rocky Linux 8/9** 或 **Ubuntu 20.04/22.04**（其它发行版预检会失败）。Ubuntu 20 PostgreSQL 走 apt-archive；Rocky 8 Redis 走 Remi；Ubuntu 20 RabbitMQ 钉 `4.2.8-1`（focal erlang ≤26）。
- **systemd**；能无密 sudo。
- **OS 在线源可达**：PostgreSQL（PGDG + pgvector）、Redis ≥ 7（Ubuntu 常用 `packages.redis.io`）、RabbitMQ 官方源；安装包内 `tools/` **不能**替代这些 apt/dnf 源。
- **Keycloak Java**：host 安装包内带 Temurin JRE17（`JAVA_HOME`），无需在线装 OpenJDK；OpenSearch tarball 自带 JDK。
- 架构与 `--target-platform` / 包名一致。

**两模式共用 — 出网 / 互通 MTU：**

- 经隧道访问 LLM 网关时，若「小请求能通、大 prompt 超时」，把网卡 **MTU 调到 ≤1400**（现场网络项）。image-mode 安装器会把共享网 `acps-net` 的 MTU 设为 `acps_docker_mtu`（默认 1400）；在 **Linux Docker Engine** 上还会合并写入 `/etc/docker/daemon.json`。**Docker Desktop**（含 macOS）不写 `daemon.json`，但仍管理 `acps-net`；宿主机出口 MTU 仍须自行 ≤1400。若机器上已有旧网络 MTU 不符，加 `-e acps_docker_network_recreate=true`。
- **多业务节点**时：跨机 PostgreSQL 迁移、大包 SQL 同样可能因 MTU 过高卡住；建议各业务机业务网卡统一 **MTU ≤1400**（不必等到业务验收才调）。

**〔image〕多机防火墙：** 安装剧本**不会**像 host 模式那样自动 `firewall-cmd` / `ufw` 放行。跨机前自行开放各组件广告端口（见 [三业务节点部署](./install-package-ansible-deploy-3nodes.md) 端口表），或先用临时放通验证连通。

### 2.3 快速自检

```bash
# 控制节点（共用）— 推荐先 bootstrap
"$PKG/scripts/bootstrap_control_ansible.sh"   # 需 uv；装 ansible-core≥2.16 + Python 3.14
export PATH="$HOME/.local/bin:$PATH"
ansible --version          # core >= 2.16；python version >= 3.11
ssh <业务节点用户>@<业务节点地址> 'echo ok && sudo -n true && echo sudo-ok'

# 〔image〕业务节点
docker version && docker compose version && python3 --version

# 〔host〕业务节点
cat /etc/os-release        # Rocky 8/9 或 Ubuntu 20.04/22.04
systemctl --version | head -1
```

---

## 3. 把安装包拷到控制节点并解包

在**控制节点**上操作。解开后是自带 `ansible/`、`artifacts/`、`release-manifest.toml` 的独立目录，**不要**在 git 源码树里跑。

```bash
# 1) 传到控制节点（示例）
scp acps-image-install-2.2.0-linux-amd64.tar <你>@<控制节点>:~/
# 或：scp acps-host-install-2.2.0-linux-amd64.tar ...

# 2) 解包
mkdir -p ~/acps-deploy && cd ~/acps-deploy
tar -xf ~/acps-*-install-*.tar
cd acps-*-install-*-linux-*          # 后面记作 $PKG；用 tab 补全实际目录名
```

包里有什么：

| 目录 / 文件 | 〔image〕 | 〔host〕 |
| --- | --- | --- |
| `ansible/` | 部署剧本、inventory 示例 | 同左（共用树） |
| `artifacts/images/` | 有：`*.image.tar.gz` | **无** |
| `artifacts/apps/` | 通常无 | 有：业务 app-release |
| `artifacts/vendor/` | 无 | 有：Keycloak / AMP / fluent-bit 等 |
| `artifacts/control/` | 控制节点 `acps-cli` 发布包 | 同左 |
| `baseline-matrix.toml` | 无 | 有 |
| `bin/acps-install` | 可能存在但不使用 | **无**；一律用 `ansible-playbook` |
| `scripts/` | 含 CA 自签等 | 同左 |

规范工作目录：解压后的 **`$PKG/ansible/`**。

---

## 4. 配置（重点）

要改的都在 `ansible/inventories/`：`hosts.yml`（或 OS 专用 inventory）、`host_vars/`、`secrets.yml`，以及 **`ca-materials/`**。

> **默认就是「全部组件 + Keycloak」**：`group_vars/all.yml` 里相关开关多为 `true`。你主要把「机器地址」和「密码」填对。

### 4.1 Inventory：指出业务节点（两种模式文件不同）

**〔image〕** — 从通用示例出发：

```bash
cd "$PKG/ansible"
cp inventories/hosts.example.yml    inventories/hosts.yml
cp inventories/secrets.example.yml  inventories/secrets.yml
chmod 600 inventories/secrets.yml
```

示例把每个组件组都映射到 `acps-node-1`。单业务节点装全部时，整体改名即可：

```bash
sed -i.bak 's/acps-node-1/acps-biz-1/' inventories/hosts.yml
```

改完后形如（每个组都指向同一台；**保留** `acps_deploy_mode`）：

```yaml
all:
  vars:
    acps_deploy_mode: image
  children:
    postgresql:   { hosts: { acps-biz-1: } }
    redis:        { hosts: { acps-biz-1: } }
    # ... 其它组件组 ...
    demo_leader:  { hosts: { acps-biz-1: } }
```

> 规则：**每个启用的组件组必须正好落在 1 台主机**。多机拆分见 [三业务节点部署](./install-package-ansible-deploy-3nodes.md)。

**〔host〕** — 用包内 OS 示例（已设 `acps_deploy_mode: host`）：

```bash
cd "$PKG/ansible"
cp inventories/secrets.example.yml inventories/secrets.yml
chmod 600 inventories/secrets.yml

# Rocky 9 示例（亦可 hosts.rocky8.yml / hosts.ubuntu20.yml / hosts.ubuntu22.yml）
cp inventories/hosts.rocky9.yml inventories/hosts.yml
# 按实际改 ansible_user、主机名 / ansible_host（示例默认 rhel9.acps.local）
```

也可直接 `-i inventories/hosts.rocky9.yml`（或其它 OS 示例）而不复制为 `hosts.yml`。  
**注意**：host 示例**不要**在 inventory 全局设 `ansible_become: true`（会污染控制节点 `delegate_to: localhost`）；become 由 playbook/role 按需开启。在对应 `host_vars` 里为业务机设 `ansible_become: true` 即可。

### 4.2 `host_vars/<主机名>.yml`：怎么连业务机（共用骨架）

主机名要和 inventory 里一致。远端单机示例：

```yaml
ansible_host: 10.0.0.11
ansible_user: deploy
ansible_become: true
ansible_python_interpreter: /usr/bin/python3

# 必须是控制节点 / 其它节点真正能访问到的地址（证书 SAN、acps-cli 都靠它）
acps_advertise_host: 10.0.0.11

# 默认路径（配合 become）
# acps_runtime_root: /opt/acps
# acps_data_root:    /var/lib/acps
# acps_log_root:     /var/log/acps
```

要点：

- **`acps_advertise_host`**：证书 SAN、ACS 对外 HTTPS、控制节点 CLI/smoke 都靠它。
  - 〔image〕**禁止** `127.0.0.1` / `localhost` / 空值（预检失败）。单机也必须用可解析主机名或局域网 IP——Leader/Partner/Keycloak 是不同容器，loopback 不可达。
  - 〔host〕单机可用 `127.0.0.1`，仍推荐主机名；**多机禁止** loopback（预检失败）。
  - ACS 改写与三机检查清单见 [三节点 §7](./install-package-ansible-deploy-3nodes.md#7-acs--证书地址检查image-与-host-共用)。
- 〔image〕包内可参考 `host_vars/docker.acps.local.example.yml`。  
- 〔host〕示例主机名常为 `rhel9.acps.local` / `ubuntu22.acps.local`，在 `hosts.*.yml` 里已带部分连接信息，可按环境改或补 `host_vars`。

### 4.3 `secrets.yml`：把所有 `CHANGE_ME` 换成真实值（共用）

预检会**拒绝**仍是 `CHANGE_ME` 的核心密钥。

拼进 `REDIS_URL` / `DATABASE_URL` / AMQP 的**口令与用户名**不得含 `# @ : / ?` 或空白（预检拒绝；模板虽会 URL 编码，仍建议只用字母数字与 `. _ - !`）。

| 分类 | 键 | 说明 |
| --- | --- | --- |
| 数据库 | `postgresql_superuser_password`、`registry_db_password`、`ca_db_password`、`discovery_db_password`、`monitor_db_password`、`keycloak_db_password` | 各库口令；不要含 URL 保留字符（如 # @ : / ?） |
| Redis / RabbitMQ | `redis_password`、`rabbitmq_password`、`mq_auth_mgmt_pass` | 消息与缓存；不要含 URL 保留字符 |
| Registry | `registry_secret_key`（**64 位 hex**）、`registry_server_internal_api_token`、`registry_admin_password`、`registry_bootstrap_password` | 务必改 |
| **Keycloak** | `keycloak_admin_password`、`monitor_oidc_admin_password` | 管理员与 Monitor OIDC |
| 心跳同步 | `monitor_heartbeat_sync_internal_token` | **长度 ≥ 16**；启用 Monitor 时必填 |
| 对象存储 / 检索 | `minio_root_password`、`opensearch_initial_admin_password` | AMP（OpenSearch 口令有复杂度要求） |
| LLM（可选） | `discovery_llm_*`、`embedding_*`、`demo_llm_*`，以及按 tier 的 `demo_{leader,partner}_llm_{fast,default,pro}_*` | 跑**业务验收**才需真实 key；`embedding_dim` 默认 **1024**。仅改 `demo_llm_*` **不够**：demo 渲染优先读 tier 字段（`*_api_key` / `*_base_url` / `*_model`）；改 LLM 后需重渲染相关模板再跑 `business.yml` |

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"      # registry_secret_key
python3 -c "import secrets; print(secrets.token_urlsafe(24))"  # 一般令牌
```

> `keycloak_enabled: true` 时认证走 OIDC；realm / client 导入由安装自动完成，你**只需填对口令**。  
> 业务验收 Step B 若偶发失败，可重跑 `business.yml`（不必整机清场）。

### 4.4 `ca-materials/`：CA 中间证书（共用）

需要 `ca.crt`、`ca.key`（chmod 600）、`root-ca.crt`，放在控制节点 `inventories/ca-materials/`。**根私钥不要放这里。**

**A. 生产**：离线根签出中间 CA，拷入三文件。  
**B. 测试 / 演示**：

```bash
cd "$PKG/ansible"
mkdir -p inventories/ca-materials
"$PKG/scripts/generate_ca_materials.sh" --out inventories/ca-materials/
# 目录里若已有 ca.crt / ca.key / root-ca.crt（含包内预置或上次生成），要加 --force：
# "$PKG/scripts/generate_ca_materials.sh" --out inventories/ca-materials/ --force
```

自签根私钥在 `inventories/ca-materials/offline/`，可离线留存或删除。预检会校验材料自洽。

### 4.5 `group_vars/all.yml`：通常不用改

- **部署模式**：以安装包 / inventory 为准（不要把 image 包装成 host 来用，反过来也不行）。
- **控制节点 work 根**：`acps_control_root` 默认 `~/.local/share/acps/control`（role defaults，**不在** `group_vars/all.yml` 硬编码）。多拓扑并行或避免互相踩 `control/work` 时，在 `hosts.yml` 的 `all.vars` 设独立路径，或 `-e acps_control_root=...`。
- **平台 slug**：构建脚本已按包设好。
- **discovery 变体**：默认 `cpu`；GPU 镜像/发布包时再改 `gpu`。
- **端口**：冲突时再改（如 `demo_leader_web_port: 9030`）。
- 少装组件：对应 `*_enabled: false`（本教程默认全装）。

### 4.6 配置自检：预检

```bash
cd "$PKG/ansible"
export ANSIBLE_CONFIG="$PKG/ansible/ansible.cfg"
ansible-playbook playbooks/preflight.yml -i inventories/hosts.yml -e @inventories/secrets.yml
# 〔host〕也可：-i inventories/hosts.rocky9.yml（或 rocky8 / ubuntu20 / ubuntu22 / 4os-multi）
```

共用校验：Ansible 版本、secrets、CA、每组正好 1 台主机、目录可写等。  
**〔image〕**另验 Docker / Compose。  
**〔host〕**还会检查：操作系统是否为 Rocky 8/9 或 Ubuntu 20.04/22.04、systemd、系统软件源是否可达、安装包里是否带有 vendor 等。

### 4.7 只有一台机器？（控制节点 = 业务节点）

**〔image〕**支持（常见于 Mac Apple Silicon 本机验收）：

1. 保持主机名 `acps-node-1`（不要做 §4.1 远端改名）。
2. 用包内 `host_vars/acps-node-1.yml`（`ansible_connection: local`、用户目录路径等）。
3. **必须**设置非 loopback 的 `acps_advertise_host`（本机局域网 IP 或可解析名）。包内默认从环境变量读取：
   `export ACPS_ADVERTISE_HOST=$(ipconfig getifaddr en0)`（按实际网卡调整），或直接改 `host_vars/acps-node-1.yml`。预检会拒绝 `127.0.0.1` / 空值。
4. 仍要做 §4.3 secrets 与 §4.4 CA。
5. Docker Desktop 不会改 `/etc/docker/daemon.json`；安装器仍会把 `acps-net` MTU 设为 `acps_docker_mtu`。宿主机出口 MTU 请自行 ≤1400。

```bash
cd "$PKG/ansible"
cp inventories/hosts.example.yml inventories/hosts.yml
cp inventories/secrets.example.yml inventories/secrets.yml
chmod 600 inventories/secrets.yml
# Apple Silicon 示例：把 en0 换成你的业务网卡
export ACPS_ADVERTISE_HOST="$(ipconfig getifaddr en0)"
test -n "$ACPS_ADVERTISE_HOST"  # 必须非空
"$PKG/scripts/generate_ca_materials.sh" --out inventories/ca-materials/
# 已有材料时加 --force，见 §4.4
```

**〔host〕**：产品验收路径是 **独立 Linux 业务机**（Rocky/Ubuntu）。不要把 macOS 控制节点当成 host 业务机。

---

## 5. 执行部署

### 5.0 先选对入口

| 目标机状态 | 该跑什么 | 说明 |
| --- | --- | --- |
| **空白机**或专用验证机 | `playbooks/site.yml` | 从零安装 |
| **已经装好的环境**要换版本 | `upgrade.yml`（写出组件列表；image / host 同一入口） | **不要**拿完整 `site.yml` 当日常升级；步骤见 [日常运维](./install-package-day2-ops.md) |
| **想从头再装一遍做验收** | 按 [清场教程](./install-package-clean-slate.md) 做破坏性清场，再跑 `site.yml` | 或换一台干净的目标机 |

**〔image〕/〔host〕清场**：清单、闸门命令、禁止事项见 [验收/重装前清场](./install-package-clean-slate.md)（含删 unit、重建 `/var/lib/redis|rabbitmq`、隔离 `acps_control_root`）。**不要**把 `docker compose down -v` 当成日常操作。同一 tag 若行为像旧版，排障时可加 `-e acps_force_image_load=true`。

### 5.1 跑 `site.yml`

```bash
cd "$PKG/ansible"
export ANSIBLE_CONFIG="$PKG/ansible/ansible.cfg"

ansible-playbook playbooks/site.yml -i inventories/hosts.yml -e @inventories/secrets.yml
```

安装大致按这个顺序推进（两种模式编排相同）：预检 → PostgreSQL → Keycloak → Registry → CA → 控制节点 `acps-cli` → Discovery → Redis/RabbitMQ → mq-auth → 监控与 AMP → 基础 smoke → demo-partner → **demo-leader（含 Web）** → AMP Forwarder → 心跳同步检查。

**demo Web（两种模式一样）**：浏览器打开 **`http://<advertise-host>:9030/`**；进程自带 `/api/v1/` 同源反代，`backendBase=''`；**没有**单独的 `demo-nginx`。

装完后可以这样确认一下：

```bash
curl -fsS "http://<业务节点可达地址>:9030/api/v1/health"
# 〔image〕
docker ps --format '{{.Names}}' | grep -E 'demo_leader'   # 有 demo_leader / demo_leader_web；无 nginx
# 〔host〕
ssh <业务机> 'systemctl is-active acps-demo_leader_web.service'   # 单元名以实装为准
```

> 常规做法：修好配置，**重跑同一条命令**（幂等）。  
> 也可再跑 `playbooks/smoke.yml`。剧本 exit 0 即表示基础 smoke 已通过。

---

## 6. 业务验收（`business.yml`）（共用）

固定 **A→B→C→D**，顺序执行、快速失败。需：`demo_*` 已开、真实 LLM/embedding key、Monitor + Discovery 已启用。

```bash
cd "$PKG/ansible"
ansible-playbook playbooks/business.yml -i inventories/hosts.yml -e @inventories/secrets.yml
# 期望：命令退出码为 0，A–D 四步都成功
```

| 步骤 | 验证内容 |
| --- | --- |
| **A** | 自然语言查询能命中各 demo Partner |
| **B** | Leader↔Partner direct_rpc |
| **C** | 组队 / inbox 邀请真投递 |
| **D** | D1：Monitor 五类 AMP 记录；D2：Discovery 存活覆盖 Leader+Partner |

失败提示：A 查 LLM/embedding；B/C 查 `demo_llm_*` 与 RabbitMQ/mq-auth 互通（多机尤其是 `5671` / `9008`）；大 prompt 或跨机迁移超时查 **MTU≤1400**；D 查 Forwarder / alive-sync。

可选真人登录：[OIDC Web 手工验证](./oidc-web-app-manual-verification.md)、[acps-cli Device 登录](./oidc-acps-cli-device-login.md)。

---

## 7. 出了问题先看这里

| 现象 | 可能原因 / 处理 |
| --- | --- |
| 预检报 `CHANGE_ME` | 补齐 §4.3 |
| 预检报 URL-reserved / `#` in password | 改掉 secrets 中含 `# @ : / ?` 的口令（见 §4.3） |
| 预检报 tomllib / Ansible too old | 控制节点跑 `"$PKG/scripts/bootstrap_control_ansible.sh"`，确认 PATH 含 `~/.local/bin` |
| 预检报 `ca-materials` | 按 §4.4 放入或自签；目录里已有文件时要加 `--force` |
| 预检报 `must have exactly 1 host` | inventory 某组是 0 台或多台 |
| 预检报 `acps_deploy_mode` | inventory 的 `all.vars` 里要有 `image` 或 `host`（见 `hosts.example.yml`） |
| SSH / 权限失败 | 免密 SSH；业务机 `ansible_become: true`；〔host〕不要在 inventory 里全局写 become |
| 〔image〕`docker` 权限 / permission denied | 部署用户加入 `docker` 组并重新登录 |
| 证书 / CLI 连不上 | `acps_advertise_host` 不可达；〔image〕写成了 `127.0.0.1`（预检应已拦） |
| 预检报 advertise loopback / empty | 〔image〕或〔host〕多机：改成可达 IP/DNS；Mac 同机设 `ACPS_ADVERTISE_HOST` |
| Desktop 上 daemon.json / `/etc/docker` | 预期跳过；只看 `acps-net` MTU 与宿主机网卡 MTU |
| 〔image〕Docker / Compose 找不到 | 业务机未装 Engine 或 compose 插件 |
| 〔image〕镜像缺失 | 安装包缺长名 `*.image.tar.gz`；回组装教程重打 |
| 〔image〕多机端口不通 | 自行放行防火墙（剧本不自动改）；见 [三节点](./install-package-ansible-deploy-3nodes.md) 端口表 |
| 〔host〕OS / 源 / vendor 预检失败 | 确认 Rocky 8/9 或 Ubuntu 20.04/22.04、源可达、vendor 已打进包 |
| 〔host〕Java / OpenSearch 起不来 | Keycloak：确认包内 `temurin_jre17` 已落地且 `.env` 有 `JAVA_HOME`；OpenSearch：查 `OPENSEARCH_JAVA_HOME` |
| 同 tag 行为像旧版（image） | `-e acps_force_image_load=true` 或按 §5.0 清理 |
| demo 证书在但 A 同步 0 ACS | 可 `-e acps_force_demo_bootstrap=true` |
| `tomllib` 找不到 | 控制节点 Python < 3.11 |
| 业务验收 / NL 失败 | 真实 LLM key；`embedding_dim=1024`；MTU≤1400 |
| 业务验收 C（群组）失败 | 多机查 RabbitMQ EXTERNAL / mq-auth `9008`；再查 LLM |
| 业务验收 D（AMP）失败 | Agent 侧 Emitter / AIC：见 [AMP 可观测性](./amp-agent-observability.md)；平台侧查 Forwarder / Monitor |
| 仍在找 demo-nginx / 直连 `:9031` 当 Web | 统一 `demo_leader_web:9030` + 同源 `/api` |
| Leader→Partner `connection failed` / ACS 含 `localhost:902` | 安装器应改写 ACS；检查方法见 [三节点 §7](./install-package-ansible-deploy-3nodes.md#7-acs--证书地址检查image-与-host-共用) |
| CA `--force` 后 `UNKNOWN_CA` | 先强制跑 `renew-certs.yml` 再重启 TLS 相关服务；顺序见 [三节点 §7](./install-package-ansible-deploy-3nodes.md#7-acs--证书地址检查image-与-host-共用) / [日常运维 §1](./install-package-day2-ops.md#1-临期续签renew-certsyml) |

---

## 8. 接下来做什么

- **三台业务节点**（image / host 皆可）：[三业务节点部署](./install-package-ansible-deploy-3nodes.md)。
- **日常运维**（续签 / trust / 升级 / 回滚）：[安装后日常运维](./install-package-day2-ops.md)。包内开关细节见 `acps-infra/release/install-packaging/README.md`。
- **回看组装**：[组装安装包](./install-package-build.md)。
