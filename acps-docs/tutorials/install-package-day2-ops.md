[首页](../README.md)

# 安装后日常运维（续签 / trust / 升级 / 回滚）

这篇教程写给：**已经按 [用安装包部署 ACPs](./install-package-ansible-deploy.md) 跑通 `site.yml`（可选再跑 `business.yml`）的人**。  
装完以后，日常不要再拿完整 `site.yml` 当「升级按钮」。下面说明该用哪条 playbook，以及几条容易踩坑的约定。

变量名、组件列表、host 模式下 vendor / 系统包的细节，以安装包里的  
`README.md` 和 `docs/known-limitations.md` 为准（源码树对应  
`acps-infra/release/install-packaging/`）。

```text
首次安装，或改机器拓扑     →  site.yml
证书快到期，只续叶子证书   →  renew-certs.yml
只更新信任链（trust）      →  refresh-trust-bundle.yml
更换某个组件的版本         →  upgrade.yml
回到上一个版本             →  rollback.yml
演示业务验收               →  business.yml（可选）
day2 后再跑业务前就绪      →  wait-discovery-alive.yml（可选；仅等 Discovery per-AIC）
```
**image 与 host 用同一套运维 playbook**；差别主要是目标机怎么跑服务（Docker Compose，或 systemd / venv / 系统包）。host 模式旁路已在 **Rocky 8/9** 与 **Ubuntu 20.04/22.04** 上验证（续签 / trust / 应用与 vendor 升级回滚 / os_package 回滚拒绝）。下面没特别标注时，两种模式命令相同。

---

## 0. 先选对 playbook

| 你要做的事 | 该跑 | 不要这样做 |
| --- | --- | --- |
| 空白机首次安装；改 inventory 拓扑或 `advertise`；证书 SAN / issuer 要跟地址对齐 | `site.yml` | 把完整 `site.yml` 当成日常升级 |
| 证书快到期，只续**叶子证书** | `renew-certs.yml` | 以为默认 renew 已经改好了 SAN |
| CA 信任链变了，只把 trust 发到各机 | `refresh-trust-bundle.yml` | 指望它顺便重签叶子证书 |
| 环境已经在跑，只换某些组件的版本 | `upgrade.yml`，并写出组件列表 | 再跑一遍完整 `site.yml` 当升级 |
| 升级失败，或要回到上一次成功的版本 | `rollback.yml`，并写出组件列表 | 回滚之后又跑完整 `site.yml` 把结果冲掉 |
| 演示 NL / RPC / AMP 验收 | `business.yml` | 用它代替 smoke，或代替升级 |
| day2 / 强 recreate 后再跑 business | 先 `wait-discovery-alive.yml`，再 `business.yml` | 用裸 sleep 代替 per-AIC 等待；把 D2 永久挪进 `business.yml` 开头 |

两条模式都要遵守：

1. **升级或回滚成功后，不要马上再跑完整 `site.yml`。** 否则新包里的模板、digest 很容易把刚回滚的意图冲掉。  
2. **不要把 `docker compose down -v`（以及会删数据卷的同类命令）当成日常操作。** 验收机要「从零再装」请走 [清场教程](./install-package-clean-slate.md)，不要半清半留。  
3. 正式验收请保持默认的 smoke（`*_skip_smoke=false`）。只有紧急排障才考虑 `skip_smoke=true`，而且**不能**把跳过 smoke 的结果当成验收通过。

控制节点的习惯与首装相同：每次部署用**单独的解包目录**，进入 `$PKG/ansible`，用  
`-i inventories/hosts.yml -e @inventories/secrets.yml`（host 单机也可用  
`hosts.rocky8.yml` / `hosts.rocky9.yml` / `hosts.ubuntu20.yml` / `hosts.ubuntu22.yml`）。  
两套环境并行时，请用两套目录，并隔离 `acps_control_root`，不要共用同一个 `control/work`。

---

## 1. 临期续签：`renew-certs.yml`

按「快到期」窗口扫描并续签**叶子证书**，发到目标机，并重启用到这些证书的服务（含依赖它们的服务）。

**契约：续签保持 Agent AIC 不变**（对已有材料走同 AIC 的 `cert renew`，不会 `agent delete` / 重建注册）。

**默认扫描不会因为 advertise / SAN 和现状不一致就自动续签。**  
若主机名、对外地址、SAN 需要对齐，请跑 `site.yml`（地址面变更**可能换 AIC**，与 renew 保号不同）；不要在运维记录里写「renew 已经修好 SAN」。

```bash
cd "$PKG/ansible"
export ANSIBLE_CONFIG="$PKG/ansible/ansible.cfg"

ansible-playbook -i inventories/hosts.yml playbooks/renew-certs.yml \
  -e @inventories/secrets.yml

# 强制重签叶子（仍保 AIC；排障，或 CA 材料 --force 之后覆盖叶子证书）：
#   -e acps_force_cert_renew=true
# 只续一部分 profile：
#   -e '{"acps_cert_renew_profiles":["redis","registry-9002"]}'
```

常见情况：你对 `generate_ca_materials.sh --force` 换过中间 CA 之后，必须强制续签叶子证书，再让依赖 TLS 的服务加载新材料。顺序见 [三节点 §7](./install-package-ansible-deploy-3nodes.md#7-acs--证书地址检查image-与-host-共用)。续签后 AIC 应与续签前相同。

续签结束后，可以再跑包内的 `playbooks/smoke.yml` 做一次基础检查；`renew-certs.yml` 末尾本身也会做 TLS 相关检查。

---

## 2. 刷新信任链：`refresh-trust-bundle.yml`

从 inventory 里的 `ca_server` 拉取最新 trust，按各组件约定的文件名发到目标机（例如 `trust-bundle.pem`、`acps-root-ca.pem`）。**不会**重签叶子证书。

```bash
cd "$PKG/ansible"

ansible-playbook -i inventories/hosts.yml playbooks/refresh-trust-bundle.yml \
  -e @inventories/secrets.yml

# 强制覆盖：-e acps_force_trust_bundle_refresh=true
# 只刷新一部分：-e '{"acps_trust_bundle_profiles":["registry-9002","redis"]}'
```

和升级的关系：`upgrade.yml` 的组件列表里**包含 `ca_server`** 时，默认会在升级后自动再跑一次 trust 刷新。可以用变量关掉，但关掉会打出 WARNING，**正式验收时不要关掉**。  
回滚列表里含 `ca_server` 时：**不会**把证书或 trust 一并回滚；需要对齐时，请**手动**再跑本 playbook。

---

## 3. 组件升级：`upgrade.yml`

用来更换组件版本和进程，**保留**数据目录和证书。Compose / systemd 只会按升级需要启动或重建服务，**不会**去删数据卷。  
必须提供组件列表：`acps_upgrade_components`（逗号分隔）。

```bash
cd "$PKG/ansible"

ansible-playbook -i inventories/hosts.yml playbooks/upgrade.yml \
  -e @inventories/secrets.yml \
  -e acps_upgrade_components=registry_server

# 多个组件时注意依赖顺序，例如先升级 ca，再升级使用它的服务：
#   -e acps_upgrade_components=registry_server,ca_server
# discovery 可用别名 discovery_server（会展开成当前 variant）
# 版本号没变、仍想完整走一遍升级流程时：
#   〔image〕按需加 -e acps_force_image_load=true
#   〔host〕应用加 -e acps_force_app_reinstall=true
#   〔host〕vendor（含 amp_forwarder）加 -e acps_force_vendor_reinstall=true
```

〔host〕模式下可以先记住这几条：

| 类型 | 行为 |
| --- | --- |
| 应用（`releases/` + `current`） | 包内容没变时，默认不会真正升级；若要练习回滚，需加 `acps_force_app_reinstall=true`，才会留下可回退的 `previous` |
| vendor_bundle（Keycloak、`amp_forwarder` 等） | 同上；练习 `amp_forwarder` 回滚时常用 `acps_force_vendor_reinstall=true` |
| 系统包（PostgreSQL / Redis / RabbitMQ） | 同一主版本内的小版本可以升级；**PostgreSQL 主版本不一致会立刻失败**，需要人工做 `pg_upgrade` 或导出导入，安装器不会改数据目录 |

升级默认**不会**顺带续签叶子证书。组件列表含 `ca_server` 时，默认会按 §2 自动刷新 trust。

---

## 4. 回滚到上一版本：`rollback.yml`

把选定组件切回状态里记录的 `previous`（也可以显式指定 `acps_rollback_to`，但必须和该 `previous` 一致）。  
**不会**自动执行数据库 schema 降级；**默认也不回滚证书**。中途失败会停住，并尽量保留现场。

**首次 upgrade 之前往往没有 `previous`。** 若首装后立刻跑 `rollback.yml`，会因缺少可回退指针而失败——这是预期行为。请先成功跑一次会切出 `previous` 的 `upgrade.yml`（〔host〕应用常需 `acps_force_app_reinstall=true`；vendor 常需 `acps_force_vendor_reinstall=true`），再练习 rollback；或跳过该练习步。

```bash
cd "$PKG/ansible"

ansible-playbook -i inventories/hosts.yml playbooks/rollback.yml \
  -e @inventories/secrets.yml \
  -e acps_rollback_components=registry_server

# 若状态显示当前 migrate 新于 previous，需要显式确认（仍然不会降级 schema）：
#   -e acps_rollback_acknowledge_migrate=true
```

〔host〕下对 `postgresql` / `redis` / `rabbitmq`：**默认拒绝**自动降包。若要恢复，请人工处理系统包，或从回滚列表里去掉这些组件。

回滚后如果 TLS 相关检查告警，可按需跑 §2 的 `refresh-trust-bundle.yml`，**不要**用完整 `site.yml` 当「一键修好」。

---

## 5. 建议在实验环境先练一遍

在首装已经成功的环境上，用**同一份安装包**练习升级 / 回滚 / 续签即可。  
版本号相同也可以验证流程是否通；这**不能**代替「换一个更大版本安装包」的升级验证。  
**注意**：下面 rollback 步依赖上一步 upgrade 已写出 `previous`；若跳过 upgrade 或 upgrade 未真正切版本，rollback 会失败（见 §4）。

```bash
# 1) 应用升级 + 回滚（以 registry 为例）
ansible-playbook -i inventories/hosts.yml playbooks/upgrade.yml \
  -e @inventories/secrets.yml \
  -e acps_upgrade_components=registry_server \
  -e acps_force_app_reinstall=true          # 〔host〕；〔image〕可按需加 force load

ansible-playbook -i inventories/hosts.yml playbooks/rollback.yml \
  -e @inventories/secrets.yml \
  -e acps_rollback_components=registry_server

# 2) 只强制续签 redis 的叶子证书；若还改了拓扑，再按需跑 site.yml
ansible-playbook -i inventories/hosts.yml playbooks/renew-certs.yml \
  -e @inventories/secrets.yml \
  -e '{"acps_cert_renew_profiles":["redis"]}' \
  -e acps_force_cert_renew=true

# 3) 刷新 trust
ansible-playbook -i inventories/hosts.yml playbooks/refresh-trust-bundle.yml \
  -e @inventories/secrets.yml
```

〔host〕练习 `amp_forwarder` 回滚时：升级请带 `-e acps_force_vendor_reinstall=true`，否则常常没有可回退的 `previous`。

练习完 day2、**再跑** `business.yml` 之前：强制续签 / recreate 后，Discovery `aliveMap` 可能短暂缺个别 AIC（业务面已通、心跳入 Discovery 仍滞后；一般等一会儿即可收敛）。建议先跑就绪门禁（只等 D2，不跑 Monitor D1、也不替代 business A–D）：

```bash
ansible-playbook -i inventories/hosts.yml playbooks/wait-discovery-alive.yml \
  -e @inventories/secrets.yml
# 默认：单次超时 480s；失败后 settle 90s 再重试一次。
# 可调：-e acps_business_wait_alive_timeout_seconds=600
#       -e acps_business_wait_alive_retry=false
#       -e acps_business_wait_alive_retry_settle_seconds=120

ansible-playbook -i inventories/hosts.yml playbooks/business.yml \
  -e @inventories/secrets.yml
```

干净 `site.yml` 后立刻 business，通常**不必**先跑 `wait-discovery-alive.yml`。

---

## 6. 出了问题先看这里

| 现象 | 可以怎么做 |
| --- | --- |
| 改了 advertise / 主机名，证书 SAN 还是旧的 | 跑 `site.yml`（可能换 AIC，属地址面例外），**不要**只跑默认的 renew 并宣称已修 SAN |
| CA 材料 `--force` 之后出现 `UNKNOWN_CA` | 强制跑 `renew-certs`（应保 AIC），再重启 / 重建依赖 TLS 的服务；见三节点 §7 |
| 升级了 `ca_server` 后，部分服务不信任证书链 | 确认升级是否自动刷新了 trust；或手动跑 `refresh-trust-bundle.yml` |
| 回滚之后看起来又像新版本 | 是否误跑了完整 `site.yml`，把回滚结果冲掉了 |
| 〔host〕回滚 amp / vendor 提示没有 `previous` | 升级时加上 `acps_force_vendor_reinstall=true` |
| 首装后立刻 rollback 失败（无 `previous`） | **预期**：先成功 upgrade 切出 previous，或跳过该练习 |
| 〔host〕回滚 postgresql 失败 | **这是预期行为**：产品默认拒绝自动降包 |
| 刚做完 refresh / 升级，立刻 smoke 报 9009 连不上 | monitor 可能还在启动；稍等再跑 smoke |
| day2 后立刻 business，Step D2 报 Leader/Partner 不在 aliveMap | 先跑 `wait-discovery-alive.yml`（默认加长超时 + 一次 settle 重试）再 `business.yml`；两次仍超时再查 heartbeat→alive-delta |
| 想跨大版本升级 | 需要准备**另一份**对应版本的安装包；同版本强制重装只验证流程，不能当成跨版本证据 |

---

## 7. 接下来做什么

- 回到首装：[用安装包部署 ACPs](./install-package-ansible-deploy.md)
- 验收机要从零再装：[清场教程](./install-package-clean-slate.md)（≠ 本篇日常运维）
- 多机拓扑：[三业务节点部署](./install-package-ansible-deploy-3nodes.md)
- 包内说明：`install-packaging/README.md`、`docs/known-limitations.md`
- ClickHouse / OpenSearch 目前是明文、没有常驻续签进程、回滚不会自动降级 schema 等边界，以 `known-limitations.md` 为准
