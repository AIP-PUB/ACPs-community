[首页](../README.md)

# 三业务节点部署（Ansible：image / host）

前提：你已按 [用安装包部署 ACPs](./install-package-ansible-deploy.md) 会解包、填 `secrets.yml`、自签 CA、跑 `site.yml` / `business.yml`。本文只讲**怎么把组件分到三台业务机**；Ansible / secrets / CA / 验收命令不重复。装完后的续签 / 升级等见 [日常运维](./install-package-day2-ops.md)。同一批机器要「从零再装」见 [清场教程](./install-package-clean-slate.md)。

**image 与 host 共用同一套组件分组**；差别只在业务机前置条件（Docker vs Rocky 8/9 · Ubuntu 20.04/22.04）与安装包类型，见主教程 §0 / §2。

两机（app + 依赖基座）拓扑可直接从安装包复制 `inventories/hosts-multi.example.yml`（不必手写三机）。本文给出**三业务节点**拆分示例。

机器：**1 台控制节点**（只跑 Ansible + acps-cli）+ **3 台业务节点**。控制节点能免密 SSH 三台，且能访问各机的 `acps_advertise_host` 端口。

- 〔image〕三台都装 Docker；部署用户要能免密使用 sudo，并且能跑 `docker`（通常加入 `docker` 组后重新登录）。  
- 〔host〕三台都要是支持的操作系统：**Rocky Linux 8/9** 或 **Ubuntu 20.04/22.04**（可以全是同一大版本、或按现场混用——每一台都要满足 host 前置条件）。四 OS 混部示例见下文 §8。

**多机额外前置（易漏）：**

| 项 | 说明 |
| --- | --- |
| **MTU** | 跨机 PostgreSQL 迁移 / LLM 大 prompt 经隧道时，三台业务网卡建议 **MTU ≤1400**（安装器不改）。「小请求通、大包卡住」先查这个。 |
| **防火墙** | 〔host〕多数组件会幂等放行广告端口；**〔image〕不会自动改 firewalld/ufw**。跨机前请放行下文端口表，或临时关防火墙做通再收紧。 |
| **互通** | 每台的 `acps_advertise_host` 必须是另两台与控制节点都能访问的 IP/DNS（禁止 `127.0.0.1`）。 |

跨机常用端口（按本文拓扑；未列全但漏这些最容易挂）：

| 方向（示意） | 端口 | 用途 |
| --- | --- | --- |
| → biz-1 | `5432` | PostgreSQL |
| → biz-1 | `5671` | RabbitMQ AMQPS（demo EXTERNAL / inbox） |
| → biz-1 | `9080`（默认） | Keycloak |
| → biz-2 | `9001` / `9002` | Registry 公网 / mTLS |
| → biz-2 | `9003` | CA |
| → biz-2 | `9005` | Discovery |
| → biz-2 | `9007` / `9008` | mq-auth 群组 API / RabbitMQ `auth_http` |
| → biz-2 | `9030` | demo Leader Web |
| → biz-3 | `9009` | Monitor |

---

## 1. 怎么分

| 主机 | 干什么 | 组件 |
| --- | --- | --- |
| **biz-1** | 基础：库 / 缓存 / 消息 / 登录 | `postgresql` `redis` `rabbitmq` `keycloak` |
| **biz-2** | 平台 + 两个 demo | `registry_server` `ca_server` `discovery_server` `mq_auth_server` `demo_partner` `demo_leader` |
| **biz-3** | 观测与 AMP 存储 | `redpanda` `victoria_metrics` `clickhouse` `minio` `opensearch` `monitor_server` |

AMP Forwarder 会跟 demo 落在 **biz-2**（playbook 按 demo 所在主机自动挂，不用单独改组）。

---

## 2. 改 inventory

在控制节点解包后的 `$PKG/ansible` 里：

**〔image〕**

```bash
cd "$PKG/ansible"
cp inventories/hosts.example.yml inventories/hosts.yml
```

**〔host〕**可从 `hosts.rocky8.yml` / `hosts.rocky9.yml` / `hosts.ubuntu20.yml` / `hosts.ubuntu22.yml` 复制后改成三机，或自建 `hosts.yml` 并设 `acps_deploy_mode: host`（不要在 inventory **全局**设 `ansible_become`）。四 OS 混部可直接用包内 `hosts.4os-multi.yml`（见 §8）。

把单一主机改成下面这样（主机名可自定，后面 `host_vars` 文件名要一致）。

**必须保留 `all.vars.acps_deploy_mode`**：从 `hosts.example.yml` 只改 `children` 时已有该字段；若整段替换成下面示例却漏写，`preflight` 会失败。

```yaml
all:
  vars:
    acps_deploy_mode: image   # 〔host〕改为 host
  children:
    postgresql:       { hosts: { biz-1: } }
    redis:            { hosts: { biz-1: } }
    rabbitmq:         { hosts: { biz-1: } }
    keycloak:         { hosts: { biz-1: } }

    registry_server:  { hosts: { biz-2: } }
    ca_server:        { hosts: { biz-2: } }
    discovery_server: { hosts: { biz-2: } }
    mq_auth_server:   { hosts: { biz-2: } }
    demo_partner:     { hosts: { biz-2: } }
    demo_leader:      { hosts: { biz-2: } }

    redpanda:         { hosts: { biz-3: } }
    victoria_metrics: { hosts: { biz-3: } }
    clickhouse:       { hosts: { biz-3: } }
    minio:            { hosts: { biz-3: } }
    opensearch:       { hosts: { biz-3: } }
    monitor_server:   { hosts: { biz-3: } }
```

规则没变：每个启用的组件组仍只能对应 **正好 1 台** 主机。

---

## 3. 写三份 `host_vars`

为每台业务机各建一个文件。**`acps_advertise_host` 必须是另外两台和控制节点都能访问到的 IP/DNS**，不要写 `127.0.0.1`。

`inventories/host_vars/biz-1.yml`：

```yaml
ansible_host: 10.0.0.11
ansible_user: deploy
ansible_become: true
ansible_python_interpreter: /usr/bin/python3
acps_advertise_host: 10.0.0.11
```

`biz-2.yml` / `biz-3.yml` 同样写，只改成各自的 IP。路径仍用默认 `/opt/acps`、`/var/lib/acps`、`/var/log/acps`（配合 `become`）。

多拓扑并行验收时，在 `hosts.yml` 的 `all.vars` 设独立 `acps_control_root`（或 `-e`），不要与单机拓扑共用默认 `~/.local/share/acps/control`。清场再装见 [清场教程](./install-package-clean-slate.md)。

---

## 4. secrets / CA：与单机相同

```bash
cp inventories/secrets.example.yml inventories/secrets.yml
chmod 600 inventories/secrets.yml
# 按单机教程填完 CHANGE_ME；口令不要含 # @ : / ?；业务验收需要真实 LLM / embedding（embedding_dim 用 1024）

mkdir -p inventories/ca-materials
"$PKG/scripts/generate_ca_materials.sh" --out inventories/ca-materials/
# 若目录里已有 ca.crt / ca.key / root-ca.crt（含安装包预置材料），要加 --force 才会覆盖：
# "$PKG/scripts/generate_ca_materials.sh" --out inventories/ca-materials/ --force
```

---

## 5. 先自检再装

```bash
# 控制节点 Ansible 过旧或缺 tomllib 时先：
# "$PKG/scripts/bootstrap_control_ansible.sh" && export PATH="$HOME/.local/bin:$PATH"

cd "$PKG/ansible"
export ANSIBLE_CONFIG="$PKG/ansible/ansible.cfg"

ansible all -i inventories/hosts.yml -m ping

ansible-playbook playbooks/preflight.yml -i inventories/hosts.yml -e @inventories/secrets.yml
ansible-playbook playbooks/site.yml      -i inventories/hosts.yml -e @inventories/secrets.yml
ansible-playbook playbooks/business.yml  -i inventories/hosts.yml -e @inventories/secrets.yml
```

命令与单机相同；按 inventory 自动跨机编排，不必改 playbook。

---

## 6. 多机常见坑

| 现象 | 处理 |
| --- | --- |
| 预检抱怨缺少 / 未知 `acps_deploy_mode` | inventory 的 `all.vars` 补上 `image` 或 `host`（见 §2） |
| 证书 / CLI 连不上某组件 | 该组件所在机的 `acps_advertise_host` 不可达，或写成了 `127.0.0.1` |
| 预检 `must have exactly 1 host` | 某个组件组漏写或多写了主机 |
| SSH 只通一台 | 控制节点要对 **biz-1/2/3** 都免密；`ansible all -m ping` 应三台都 ok |
| 〔image〕某台 `docker` 权限失败 | 部署用户加入 `docker` 组后重新登录，再 `docker ps` |
| 跨机连不上 PG / AMQPS / mq-auth | 查防火墙与上文端口表；RabbitMQ 在 biz-1、mq-auth 在 biz-2 时尤其要放行 `5671` 与 `9007/9008` |
| 〔image〕biz-2 回写 Registry ACS 拉 postgres 镜像 | 已改为 `acps_psql.sh`（同机 docker exec / 跨机本机 psql）；不要再依赖 Docker Hub |
| Alembic / 大 SQL / 大 LLM 超时或挂死 | 三台业务网卡 **MTU ≤1400** |
| `generate_ca_materials` 报 already exist | 加 `--force`，或改用自备三文件 |
| demo / Forwarder 找不到 | 确认 `demo_*` 在 **biz-2**，不要误改到 biz-3 |
| 〔host〕某一台 OS/源失败 | 该台需要单独满足 Rocky 8/9 或 Ubuntu 20/22 与在线源要求 |
| 业务验收 C（群组 inbox）失败 | 先确认 EXTERNAL 能连上 RabbitMQ（mq-auth `9008` 可达），再查 LLM；见主教程 §6 / §7 |
| Leader 连 Partner 报 `All connection attempts failed` / ACS 仍是 `localhost:902x` | 安装器应已改写 ACS；检查 biz-2 Partner ACS 与 Leader `scenario/expert` 是否为 `https://<biz-2 advertise>:902x`；见 §7 |
| CA `--force` 后 Partner 连 RabbitMQ `UNKNOWN_CA` | 先跑 `renew-certs.yml` 再 recreate/restart；见 §7 |

单机流程、两种模式的不同点、业务验收 A–D：回看 [install-package-ansible-deploy.md](./install-package-ansible-deploy.md)。  
同一批机器要从零再装：[清场教程](./install-package-clean-slate.md)。

---

## 7. ACS / 证书地址检查（image 与 host 共用）

安装器改写 ACS：**Public** = 各机 `acps_advertise_host`；**Colocated AMQP** = 〔image〕同机可用 `rabbitmq`，〔host〕/跨机用 peer advertise。

**装完后可以这样检查（在控制节点或 biz-2）：**

```bash
# Partner ACS：HTTPS 不得再是 localhost
python3 -c 'import json,glob,sys
from urllib.parse import urlsplit
ok=True
for p in glob.glob("/opt/acps/components/demo_partner/partners/online/*/acs.json"):
  for ep in json.load(open(p)).get("endPoints") or []:
    u=ep.get("url") or ""; h=(urlsplit(u).hostname or "")
    if ep.get("transport")=="JSONRPC" and h in ("localhost","127.0.0.1"):
      print("BAD",p,u); ok=False
print("partner_acs_ok" if ok else "partner_acs_BAD"); sys.exit(0 if ok else 1)'

# Leader 静态 Partner 快照
grep -R "https://localhost:902" /opt/acps/components/demo_leader/leader/scenario/expert || echo "leader_scenario_ok"
```

| 项 | 期望 |
| --- | --- |
| Partner ACS HTTPS | `https://<biz-2 advertise>:902x/...` |
| Partner/Leader ACS AMQP（本拓扑 RMQ 在 biz-1） | `amqps://<biz-1 advertise>:5671/...`（〔image〕若 Partner 与 RMQ 同机才可为 `rabbitmq`） |
| 〔host〕 | ACS/env **禁止**残留 Compose 名 `rabbitmq` / `postgresql` |

**CA `--force` 之后的顺序（和地址问题无关，别和连通性故障混在一起查）：**

1. `generate_ca_materials.sh --force`（或换中间 CA）之后  
2. 必须跑 `ansible-playbook playbooks/renew-certs.yml ...`（建议加 `-e acps_force_cert_renew=true`）覆盖 RabbitMQ/Redis/Registry/mq-auth/demo 等叶子；完整步骤见 [日常运维 §1](./install-package-day2-ops.md#1-临期续签renew-certsyml)  
3. 再 recreate/restart 依赖 TLS 的服务；Partner 的 `trust-bundle` 要包含**当前** root+intermediate  

否则会出现 `UNKNOWN_CA` / issuer mismatch，看起来像「地址不通」。

---

## 8. 四 OS 混部示例（`hosts.4os-multi.yml`）

把三机拆分扩成四台、且每台 OS 不同时，安装包提供现成 inventory：

`inventories/hosts.4os-multi.yml` + `inventories/host_vars/{rhel8,rhel9,ubuntu20,ubuntu22}.acps.local.yml`

| 主机 | OS | 组件 |
| --- | --- | --- |
| `rhel8.acps.local` | Rocky 8 | `postgresql` `redis` `rabbitmq` `keycloak` |
| `ubuntu20.acps.local` | Ubuntu 20.04 | `registry_server` `ca_server` `discovery_server` `mq_auth_server` |
| `rhel9.acps.local` | Rocky 9 | `demo_partner` `demo_leader`（`amp_forwarder` 随 demo） |
| `ubuntu22.acps.local` | Ubuntu 22.04 | `redpanda` `victoria_metrics` `clickhouse` `minio` `opensearch` `monitor_server` |

要点：

- `all.vars` 设独立 `acps_control_root`（例如 `control-host-4os-multi`）；**不要**全局写 `ansible_python_interpreter: /usr/bin/python3.8`（只放在 rhel8 的 host_vars）。
- 四机与控制节点都必须能解析并访问各机的 `acps_advertise_host`（禁止 `127.0.0.1`）。跨机还要通：`5671`↔`9008`（RMQ↔mq-auth）、demo→infra 的 `5432`/`6379`/`9080`、以及 amp→Redpanda 的 `19092`。
- 业务网卡建议 **MTU ≤1400**。清场后再装见 [清场教程](./install-package-clean-slate.md)。
