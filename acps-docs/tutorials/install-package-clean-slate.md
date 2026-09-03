[首页](../README.md)

# 验收 / 重装前清场（破坏性）

这篇教程写给：**要在同一批机器上「从零再装一遍」做验收或排障**的人。  
目标是业务机 / 控制面的**应用状态接近空白机首装**——无旧库角色、旧中间件状态、旧卷、旧 unit、旧 CA 工作区串味。

- **清的是数据与运行时状态**，不是卸载 OS 软件包。  
- **不是**日常运维。日常续签 / trust / 升级 / 回滚见 [日常运维](./install-package-day2-ops.md)（**禁止**把 `docker compose down -v` 当日常手段）。  
- 清完后再装：回到 [Ansible 部署](./install-package-ansible-deploy.md) 跑 `site.yml`。

```text
日常运维（保留数据）     →  day2-ops（renew / upgrade / rollback）
验收清场再装（破坏数据） →  本教程 → site.yml
```

清场后**不要**手工 `systemctl start` PostgreSQL / Redis / RabbitMQ；等 `site.yml` 按本轮 secrets 重配并启动。

---

## 0. 何时用 / 何时不用

| 场景 | 用本教程？ |
| --- | --- |
| 空白机首次安装 | **否** — 直接 `site.yml` |
| 已装环境只换组件版本 | **否** — `upgrade.yml` / `rollback.yml` |
| 远端验收、半清半留后失败、要复现「干净首装」 | **是** |
| 多拓扑并行验收（单机 / 多机 / image / host） | **是** — 且控制节点还要隔离 `acps_control_root` |
| 生产环境排障「先 wipe 再说」 | **慎用** — 先备份；确认这是验收机 |

### 0.0 总表：必清 vs 应留

以远端 Linux 默认路径为例；本机同机验收把三路径换成 §0.1 的 `~/.local/share/acps…`。

#### 业务机 — 必须清（干净首装）

| 类别 | 路径 / 对象 | 说明 |
| --- | --- | --- |
| 三路径 | `$RUNTIME` `/opt/acps`、`$DATA` `/var/lib/acps`、`$LOG` `/var/log/acps` | 含 compose、app/vendor releases、certs、state、MinIO/OpenSearch/ClickHouse/Redpanda/Keycloak/VM 等**全部**落在 `$DATA` 下的数据 |
| 〔host〕systemd | `/etc/systemd/system/acps-*.service` | 停禁后删除并 `daemon-reload`；只删树留 unit → `CHDIR` 假失败 |
| 〔host〕PostgreSQL **数据** | 所有 major：`/var/lib/pgsql/*/data`、`/var/lib/postgresql/*/main` | 缺 `PG_VERSION` 时装器 init；**漏清任一 major** → 旧角色 + 新 secrets（Keycloak 等） |
| 〔host〕Redis / RabbitMQ **数据** | `/var/lib/redis`、`/var/lib/rabbitmq` | 删后**必须**重建空目录 + 属主（及 SELinux `restorecon`） |
| 〔host〕Redpanda 兼容链 | `/opt/redpanda`（指向 `$RUNTIME/redpanda` 的 symlink） | runtime 删掉后会悬空；装器会重建 |
| 〔image〕Compose | 项目 `acps`：`down -v` + 残留容器 / 网络 / 卷 | 工程在 `$RUNTIME/compose`，勿在 `$RUNTIME` 根裸跑 compose |
| 〔image〕本验收镜像 | `acps/*` 镜像（建议默认清） | 避免「目录没了、镜像还在」半残 |
| 控制面 | 本轮 `$PKG` 解包树、对应 `acps_control_root` | 否则 CA / issuer / token 工作区串味 |
| 控制面 state | 控制节点上的 `~/.local/share/acps/state`（含 `acps_cli.json`） | 与 `control*` 工作区不同路径；漏清会留下 CLI 部署记录 |
| 控制节点误落三路径 | 若存在 `/opt/acps`（或 `/var/lib/acps`） | 控制节点**不是**业务机，但仍可能残留 TLS digest 等 `state/`；干净起步应删 |
| 构建输出 | builder 的 app-release / image / install **输出**目录 | 勿与「最新 tar」指向上一轮 |

#### 业务机 / 构建机 — 应保留（装器会重配或与首装无关）

| 类别 | 路径 / 对象 | 说明 |
| --- | --- | --- |
| OS 软件包 | postgresql / redis / rabbitmq / docker 等 **rpm/deb** | **不要** `dnf remove` / `apt purge` 冒充清场 |
| 发行版仓库 | PGDG、redis.io、RabbitMQ yum/apt source | 装器幂等 ensure |
| 〔host〕中间件**配置文件** | `/etc/redis/redis.conf`、`/etc/rabbitmq/*` | `site.yml` 会按本轮 secrets **重渲染**；清数据即可 |
| 〔host〕RabbitMQ drop-in | `/etc/systemd/system/rabbitmq-server.service.d/limits.conf` | 无状态；可留 |
| sysctl | `/etc/sysctl.d/99-acps-opensearch.conf` | 可留 |
| 〔image〕Docker daemon | `/etc/docker/daemon.json`（含 MTU） | **不要**当清场删掉 |
| 防火墙规则 | firewalld / ufw 已放行端口 | 幂等；不必为「首装感」收回 |
| `/etc/hosts` 广告名条目 | Redpanda 等写入的本机解析行 | 可留；装器幂等 |
| 客户端软链 | `/usr/local/bin/psql` 等 | 可留 |
| builder 缓存 | 持久 `--vendor-bundle-dir`、已对齐的源码树 | **禁止**当清场目标 |
| SELinux fcontext | 针对已删路径的旧标签规则 | 一般无妨；装器会对新路径再打标 |

#### 刻意不 wipe 时的口径

| 你留下了… | 验收口径 |
| --- | --- |
| PostgreSQL 数据目录（任一 major 仍有 `PG_VERSION`） | **复用旧库**，不算干净首装；记录须写明 |
| 旧 `secrets.yml` + 已 wipe 数据 | 允许（口令与空库一致） |
| **新** `secrets.yml` + **未** wipe PG/Redis/RMQ/`$DATA` | **禁止**当干净首装——必失败或鉴权漂移 |

host 路径下装器可对已有 PG 角色做口令同步，**不能替代**本教程对数据目录的必做 wipe。

### 半清半留（曾导致验收失败）

- 只删 runtime，留下 `acps-*.service`  
- 删了 `/var/lib/redis|rabbitmq` 却不重建空目录  
- `cd` 错目录，compose 没真正 `down -v`  
- 清了 runtime，image 模式镜像 / 匿名卷还在  
- 多拓扑共用同一个 `control/work` 或同一解包树改 inventory 再装  
- 只清业务机，控制节点脏解包 / 构建机旧「最新包」  
- 只清了 `control*`，留下 `~/.local/share/acps/state` 或控制节点上的 `/opt/acps/state`  
- **只清了 15/16 的 PG data，现场实际是 17**（或其它 major）  
- 用普通用户枚举/判断 PG 数据目录（`700`/`postgres`）导致循环跳过，闸门仍见 `PG_VERSION`  
- 换了一轮随机 secrets，却复用未 wipe 的 PG / Redis / RabbitMQ / `$DATA`  
- runtime 已删，留下悬空 `/opt/redpanda`

---

## 0.1 先确认三路径（再动手删）

以 **inventory / `host_vars` 实际值为准**，不要死记一套路径。

| 变量 | 远端 Linux 默认 | 〔image〕本机同机验收（包内 `acps-node-1.yml`） |
| --- | --- | --- |
| `acps_runtime_root` | `/opt/acps` | `~/.local/share/acps` |
| `acps_data_root` | `/var/lib/acps` | `~/.local/share/acps/data` |
| `acps_log_root` | `/var/log/acps` | `~/.local/share/acps/logs` |
| Compose 目录 | `{{ acps_runtime_root }}/compose` | 同上 |
| Compose 项目名 | `acps`（`acps_compose_project_prefix`） | 同上 |
| PostgreSQL major | 包内 `postgresql_os_major_version`（当前默认 **17**） | image 数据在 `$DATA/postgresql`，随 `$DATA` 删除 |

下文命令用变量写法；远端可先：

```bash
RUNTIME=/opt/acps
DATA=/var/lib/acps
LOG=/var/log/acps
```

本机 macOS 同机验收可先：

```bash
RUNTIME="$HOME/.local/share/acps"
DATA="$HOME/.local/share/acps/data"
LOG="$HOME/.local/share/acps/logs"
# 注意：默认 control 也在 $RUNTIME/control 下；清 runtime 父树会一并清掉 control
```

### 0.2 secrets / CA 与清场的耦合

| 本轮计划 | 清场要求 |
| --- | --- |
| 干净首装 + **重新**生成 `secrets.yml` / **新** CA（`--force`） | 业务机清单 A/B **完整**执行（含所有 PG major 数据 + Redis/RMQ + `$DATA`）+ 本场景 control / `$PKG` 空起步 |
| 干净首装 + **复用**上一轮同一份 secrets 与同一 CA 材料 | 仍须完整 wipe 业务机数据；control 若保留旧 cert work，可能与「新解包」混用——场景切换仍建议整树清 control |
| 只想「复用旧库」排障 | **不要**改 secrets 里的 DB/中间件口令；并在记录中写明非干净首装 |

---

## 1. 构建机 / 控制节点（非业务 runtime）

业务机走下方清单 A/B。若验收还涉及**单独的构建机（builder）**和 **Ansible 控制节点（controller）**，这两台**不要**按 A/B 去停 PostgreSQL / Redis；但要清「本轮产物与解包树」，否则常见：

- 磁盘被旧安装包 / 构建产物占满  
- glob「最新 tar」捡到上一轮包  
- 多拓扑共用脏的 `control/work`，证书 / issuer 指纹串味  

顺序建议：**先本节 → 再业务机清单 A/B**。场景切换时：本轮要用的解包目录与对应 `acps_control_root` 必须再清一次。

### 1.1 控制节点：隔离解包与 control root

每次验收用**单独的解包目录**，不要多个拓扑共用同一份 `$PKG` 下的工作区缓存。

`acps_control_root` 默认是 `~/.local/share/acps/control`（role defaults）。  
多拓扑并行时，在 `hosts.yml` 的 `all.vars` 或命令行 `-e` 设独立路径，例如：

```yaml
# inventories/hosts.yml → all.vars
acps_control_root: "{{ lookup('env', 'HOME') }}/.local/share/acps/control-image-multi"
```

```bash
# 或一次性覆盖
ansible-playbook ... -e acps_control_root="$HOME/.local/share/acps/control-host-single"
```

> `group_vars/all.yml` **不再**硬编码 `acps_control_root`，inventory / `-e` 可以覆盖默认值。

**进入本轮场景前**（换 CA / 换拓扑 / 复用同机再装时建议整树清掉，不要只在脏树上改 inventory）：

```bash
# 解包根：本轮将使用的目录必须空目录起步（稍后重新 tar 解包）
PKG="${PKG:?set to this-run unpack root}"
rm -rf "$PKG"
mkdir -p "$PKG"

# control 工作区：本场景路径 + 常见 control-* 变体（多拓扑）
CONTROL="${ACPS_CONTROL_ROOT:-$HOME/.local/share/acps/control}"
rm -rf "$CONTROL"
rm -rf "$HOME"/.local/share/acps/control-*
# 若只清缓存：rm -rf "$CONTROL/work"

# CLI / 组件 state（与 control 工作区不同路径；漏清会留 acps_cli.json）
rm -rf "$HOME/.local/share/acps/state"

# 控制节点若曾误落业务三路径（常见只剩 state/*.sha256），一并去掉
# 不要在「同时当业务机」的同机验收上盲删——那种情况走清单 A/B
sudo rm -rf /opt/acps /var/lib/acps /var/log/acps
```

闸门（控制节点）：

```bash
CONTROL="${ACPS_CONTROL_ROOT:-$HOME/.local/share/acps/control}"
FAIL=0
if [[ -e "$CONTROL" ]] && [[ -n "$(ls -A "$CONTROL" 2>/dev/null)" ]]; then
  echo "FAIL: $CONTROL not clean"; FAIL=1
else
  echo "OK: control work clean"
fi
if [[ -e "$HOME/.local/share/acps/state" ]]; then
  echo "FAIL: $HOME/.local/share/acps/state still present"; FAIL=1
else
  echo "OK: local acps state gone"
fi
if [[ -e /opt/acps ]] || [[ -e /var/lib/acps ]]; then
  echo "FAIL: controller still has /opt/acps or /var/lib/acps"; FAIL=1
else
  echo "OK: no biz three-paths on controller"
fi
# exit "$FAIL"  # 纳入验收脚本时取消注释
```

安装包落盘目录（例如 `~/…/packages/`）建议：

- 按 `<run-id>/` 隔离，或  
- 删除**非本轮**将引用的旧 `acps-*-install-*.tar*`，避免 scp / 手工拷贝时选错文件  

〔image〕本机同机验收：若 `acps_control_root` 落在 `acps_runtime_root` 之下，删 runtime 父目录会一并清掉 control——验收清场通常一起清；本机同时扮演「业务机 + 控制面」时，做完清单 A 后再确认 control / 解包 / `~/.local/share/acps/state` 已空。**不要**在同机验收场景对业务三路径执行上面的「控制节点误落」`sudo rm -rf /opt/acps…` 替代清单 A——顺序仍是清单 A（含 compose）再确认 control/state。

### 1.2 构建机：保留缓存、清本轮产物

| 动作 | 说明 |
| --- | --- |
| **保留** | 持久 `--vendor-bundle-dir`（vendor 缓存；**不要**当清场删掉） |
| **保留** | 源码同步目录（若用 `rsync --delete` 对齐，无需手工 wipe） |
| **删除 / 换目录** | 旧 app-release、镜像 tar、安装包 **输出目录**；或改用带 `<run-id>` 的 out，避免「最新包」指向上一轮 |
| 可选 | 磁盘紧时清理过旧构建日志 |

闸门（重新构建 / 解包前）：控制节点用 §1.1 闸门；构建机额外核对即将使用的安装包文件名与 mtime 属于本轮（写入验收记录）。

---

## 2. 〔image〕业务机清单 A

在**每台**业务机上执行（已按 §0.1 设好 `RUNTIME` / `DATA` / `LOG`）。  
image 模式的 PostgreSQL / Redis / RabbitMQ 等状态在 **Docker 卷或 `$DATA` bind** 下，**没有** host 的 `/var/lib/pgsql` 必清项；`down -v` + 删三路径即覆盖。

### 2.1 停栈并删卷（仅验收）

Compose **不在** `$RUNTIME` 根目录，而在 `$RUNTIME/compose`，且为多文件 + 固定项目名 `acps`。  
**不要**只执行 `cd "$RUNTIME" && docker compose down`（通常找不到正确工程，栈停不干净）。

```bash
COMPOSE_DIR="$RUNTIME/compose"
COMPOSE_YML="$COMPOSE_DIR/docker-compose.yml"
PREFIX=acps   # 与 group_vars acps_compose_project_prefix 一致；若改过请同步

if [[ -f "$COMPOSE_YML" ]]; then
  ARGS=(-p "$PREFIX" -f "$COMPOSE_YML")
  if [[ -d "$COMPOSE_DIR/services" ]]; then
    while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      ARGS+=(-f "$f")
    done < <(find "$COMPOSE_DIR/services" -maxdepth 1 -name '*.yml' 2>/dev/null | sort)
  fi
  docker compose "${ARGS[@]}" down -v --remove-orphans
fi
```

若 compose 文件已不在，按**项目前缀**清理残留（容器名形如 `acps-postgresql-1`）：

```bash
# 可移植：勿依赖 GNU xargs -r（macOS 无 -r）
docker ps -aq --filter "name=${PREFIX:-acps}" | xargs docker rm -f 2>/dev/null || true
docker network ls --filter "name=${PREFIX:-acps}" -q | xargs docker network rm 2>/dev/null || true
docker volume ls --filter "name=${PREFIX:-acps}" -q | xargs docker volume rm 2>/dev/null || true
```

干净首装建议**一并**清掉本验收加载的 `acps/` 镜像：

```bash
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
  | awk '/^acps\// {print $2}' | xargs docker rmi -f 2>/dev/null || true
```

**保留** `/etc/docker/daemon.json`（MTU 等）；不要卸载 Docker。

### 2.2 删三路径

```bash
# 远端默认常需 sudo；本机同机路径在 $HOME 下时可不加 sudo
rm -rf "$RUNTIME" "$DATA" "$LOG"
# 若权限不够：
# sudo rm -rf "$RUNTIME" "$DATA" "$LOG"
```

### 2.3 闸门（image）

```bash
PREFIX="${PREFIX:-acps}"
left="$(docker ps -a --format '{{.Names}}' | grep -E "^${PREFIX}-" || true)"
if [[ -n "$left" ]]; then echo "FAIL: containers left:"; echo "$left"; else echo "OK: no acps project containers"; fi

vols="$(docker volume ls --format '{{.Name}}' | grep -E "^${PREFIX}" || true)"
if [[ -n "$vols" ]]; then echo "FAIL: volumes left:"; echo "$vols"; else echo "OK: no acps volumes"; fi

nets="$(docker network ls --format '{{.Name}}' | grep -E "${PREFIX}" || true)"
if [[ -n "$nets" ]]; then echo "FAIL: networks left:"; echo "$nets"; else echo "OK: no acps networks"; fi

test ! -e "$RUNTIME" && echo "OK: runtime gone" || echo "FAIL: $RUNTIME still exists"
test ! -e "$DATA" && echo "OK: data gone" || echo "FAIL: $DATA still exists"
```

然后回到控制节点跑 `preflight` → `site.yml`。

---

## 3. 〔host〕业务机清单 B

### 3.1 停禁并删除 `acps-*` unit（必做）

**禁止**只删 `$RUNTIME` 却留下 unit：下次 `systemctl start` 会出现 `CHDIR` / WorkingDirectory 不存在，掩盖根因。

```bash
# 已加载的实例（无匹配时部分发行版 list-* 非 0；管道后加 || true，避免 set -e 误中断）
sudo systemctl list-units --type=service --all 'acps-*' --no-legend 2>/dev/null \
  | awk '{print $1}' | while read -r u; do
      [[ -n "$u" ]] || continue
      sudo systemctl disable --now "$u" 2>/dev/null || true
    done || true

# 仅安装未加载的 unit 文件也要清掉
sudo systemctl list-unit-files 'acps-*' --no-legend 2>/dev/null \
  | awk '{print $1}' | while read -r u; do
      [[ -n "$u" ]] || continue
      sudo systemctl disable "$u" 2>/dev/null || true
    done || true

sudo rm -f /etc/systemd/system/acps-*.service
sudo systemctl daemon-reload
sudo systemctl reset-failed || true
```

### 3.2 停本验收占用的系统服务

仅在本机是**验收机**、确认可以停库时。先列出再停——**不要**只停某一个写死的 major：

```bash
sudo systemctl list-units --type=service --all --no-legend 2>/dev/null \
  | awk '{print $1}' | grep -iE '^(postgresql|redis|rabbitmq)' || true

# 停所有 postgresql* / redis* / rabbitmq*（名称随发行版变化）
sudo systemctl list-units --type=service --all --no-legend 2>/dev/null \
  | awk '{print $1}' | grep -iE '^(postgresql|redis|rabbitmq)' \
  | while read -r u; do
      [[ -n "$u" ]] || continue
      sudo systemctl stop "$u" 2>/dev/null || true
    done || true

# 兼容显式常见名（list 未加载到时）
sudo systemctl stop redis redis-server rabbitmq-server 2>/dev/null || true
sudo systemctl stop postgresql-17 postgresql-16 'postgresql@17-main' 'postgresql@16-main' 2>/dev/null || true
```

### 3.3 清三路径 + OS 数据目录，并**重建**空家目录

**不要** `dnf remove` / `apt purge` PostgreSQL、Redis、RabbitMQ 软件包——清的是数据目录；包与 `/etc/redis`、`/etc/rabbitmq` 配置可留，`site.yml` 会按本轮 secrets 重渲染配置。

```bash
RUNTIME="${RUNTIME:-/opt/acps}"
DATA="${DATA:-/var/lib/acps}"
LOG="${LOG:-/var/log/acps}"

sudo rm -rf "$RUNTIME" "$DATA" "$LOG"

# Redis / RabbitMQ：删空后必须重建，否则 unit 会 CHDIR 失败
sudo rm -rf /var/lib/redis /var/lib/rabbitmq
sudo mkdir -p /var/lib/redis /var/lib/rabbitmq
if id redis >/dev/null 2>&1; then sudo chown redis:redis /var/lib/redis; fi
if id rabbitmq >/dev/null 2>&1; then sudo chown rabbitmq:rabbitmq /var/lib/rabbitmq; fi
command -v restorecon >/dev/null && sudo restorecon -Rv /var/lib/redis /var/lib/rabbitmq || true

# Redpanda 兼容 symlink（指向已删的 $RUNTIME/redpanda）
if [[ -L /opt/redpanda ]] || [[ -e /opt/redpanda ]]; then
  sudo rm -f /opt/redpanda
fi
```

**干净首装必做**：wipe **磁盘上所有 major** 的 PostgreSQL 数据目录（不要只删文档里写过的某一个版本）。路径以角色变量 `postgresql_os_data_dir` 为准；扫描比死记更安全。

数据目录多为 `postgres` 属主且模式 `700`：普通用户对路径做 `[[ -e ]]` / 无 `sudo` 的 glob **会「看不见」而跳过删除**，闸门用 `sudo find` 却仍能扫到 `PG_VERSION`——看起来像「按教程清了却过不了闸门」。**枚举与删除都必须在 root 下做**（先完成 §3.2 停库）：

```bash
# Rocky / RHEL：/var/lib/pgsql/<major>/data
# Ubuntu：/var/lib/postgresql/<major>/main
# nullglob：无匹配时不进入循环；勿在非 root 下先 [[ -e ]] 再 sudo rm
sudo bash -c 'shopt -s nullglob
for d in /var/lib/pgsql/*/data /var/lib/postgresql/*/main; do
  echo "wipe PG data: $d"
  rm -rf "$d"
done'
# 装器在缺 PG_VERSION 时会 init / pg_createcluster（Ubuntu 若 config 残留会 drop 再 create）
```

若 §3.4 仍报 `PG_VERSION` 残留：确认对应 `postgresql*` unit 已停（必要时 `sudo systemctl disable --now postgresql-17` 或发行版实际单元名），再重跑上面的 `sudo bash -c` 循环。

可选加码（一般不必）：Ubuntu 上对目标 major 执行 `pg_dropcluster --stop <major> main`，或删 `/etc/postgresql/<major>/main`；装器已有残留 config 的恢复逻辑。**保留 OS 包即可。**

若本轮**故意不** wipe PG，须在验收记录写明「复用旧库」，并改用非「干净首装」验收口径。

### 3.4 闸门（host）

端口安静只是辅助；**必须以无任何 `PG_VERSION`、三路径已删、Redis/RMQ 空家目录已重建、无 `acps-*` unit** 为准。

```bash
FAIL=0

if systemctl list-unit-files 'acps-*' --no-legend 2>/dev/null | grep -q .; then
  echo 'FAIL: acps units still registered'
  systemctl list-unit-files 'acps-*' --no-legend
  FAIL=1
else
  echo 'OK: no acps unit files'
fi

test ! -e "${RUNTIME:-/opt/acps}" && echo 'OK: runtime gone' || { echo "FAIL: ${RUNTIME:-/opt/acps} still exists"; FAIL=1; }
test ! -e "${DATA:-/var/lib/acps}" && echo 'OK: data gone' || { echo "FAIL: ${DATA:-/var/lib/acps} still exists"; FAIL=1; }

if test -d /var/lib/redis && test -d /var/lib/rabbitmq; then
  echo 'OK: redis/rabbitmq data dirs exist'
else
  echo 'FAIL: recreate redis/rabbitmq data dirs'
  FAIL=1
fi

# 任一残留 PG_VERSION 都算未干净首装
pg_left="$(sudo find /var/lib/pgsql /var/lib/postgresql \
  \( -path '/var/lib/pgsql/*/data/PG_VERSION' -o -path '/var/lib/postgresql/*/main/PG_VERSION' \) \
  2>/dev/null || true)"
if [[ -n "$pg_left" ]]; then
  echo "FAIL: PostgreSQL data still present:"
  echo "$pg_left"
  FAIL=1
else
  echo 'OK: no PG_VERSION under OS data dirs'
fi

if [[ -e /opt/redpanda ]]; then
  echo 'FAIL: /opt/redpanda still present (remove dangling symlink)'
  FAIL=1
else
  echo 'OK: /opt/redpanda absent'
fi

# 端口抽查（按拓扑加减；干净机上应安静——服务应仍为 stop）
ss -lntp 2>/dev/null | grep -E ':9001|:9002|:9003|:9005|:9080|:5671|:5432|:6379|:9200|:19092' \
  && echo 'WARN: some ACPs-related ports still listening (expect stop until site.yml)' \
  || echo 'OK: sample ports quiet'

exit "$FAIL"
```

---

## 4. 禁止事项

1. **不要**对 `$DATA` / `/var/lib/acps`（尤其是 PostgreSQL 数据）递归 `chown` 到部署用户。容器 / 系统用户属主会被破坏，后续迁移与启动会失败。  
2. **不要**只删树、留 `acps-*.service`。  
3. **不要**删空 `/var/lib/redis` / `/var/lib/rabbitmq` 后不重建空目录与属主。  
4. **不要**把本教程的 `down -v` / wipe 写进生产日常 runbook。  
5. 〔image〕清 runtime 时尽量一并处理本验收相关镜像与卷，避免半残布局；**不要**删 `/etc/docker/daemon.json`。  
6. **不要**在 `$RUNTIME` 根目录裸跑 `docker compose down`（工程在 `$RUNTIME/compose`，项目名默认 `acps`）。  
7. **不要**把持久 `vendor-bundle` 当清场目标删掉（应清的是构建 **输出** 与本轮解包 / control）。  
8. **不要**在未清的解包树或未隔离的 `acps_control_root` 上「改改 inventory 再装」冒充干净首装。  
9. **不要**只清 `control*` 却留下控制节点 `~/.local/share/acps/state` 或误落的 `/opt/acps`。  
10. **不要**用卸 OS 包代替清数据；也**不要**只清某一个写死的 PG major——用 **sudo** 扫描并删掉所有 `PG_VERSION`（勿在非 root 下 `[[ -e ]]` 再决定是否删）。  
11. **不要**把「端口安静」当成数据已 wipe——host 须查无 `PG_VERSION`；image 须查无项目容器 / 卷。  
12. **不要**在清场后、`site.yml` 前手工启动 PostgreSQL / Redis / RabbitMQ。  
13. **不要**在未 wipe 数据的情况下轮换 `secrets.yml` 里的库 / 中间件口令还宣称干净首装。

防火墙：〔image〕安装器通常只在 firewalld/ufw **已 active** 时幂等放行；跨机验收可临时放通端口，通后再收紧（见 [三节点](./install-package-ansible-deploy-3nodes.md)）。清场**不必**为「首装感」收回这些规则。

---

## 5. 清完再装

控制节点（`-i` 按现场改成 `hosts.rocky8.yml` / `hosts.rocky9.yml` / `hosts.ubuntu20.yml` / `hosts.ubuntu22.yml` / `hosts.4os-multi.yml` 等）：

```bash
cd "$PKG/ansible"
export ANSIBLE_CONFIG="$PKG/ansible/ansible.cfg"

ansible-playbook playbooks/preflight.yml -i inventories/hosts.yml -e @inventories/secrets.yml
ansible-playbook playbooks/site.yml      -i inventories/hosts.yml -e @inventories/secrets.yml
# demo 双开且需业务验收时：
ansible-playbook playbooks/business.yml  -i inventories/hosts.yml -e @inventories/secrets.yml
```

多机拓扑与端口表见 [三节点部署](./install-package-ansible-deploy-3nodes.md)。  
LLM / embedding 等 secrets 细节见部署教程 §4.3。
```
