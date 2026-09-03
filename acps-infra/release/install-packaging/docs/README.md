# install-packaging 操作文档索引

> 产品主路径：`acps-image-install-*` / `acps-host-install-*` + Ansible `playbooks/*.yml`。  
> 概念见 [`acps-infra/README.md`](../../../README.md)。

本目录是安装树内的短索引；逐步操作以 **acps-docs** 教程为准。

## 读哪篇

| 场景 | 文档 |
| --- | --- |
| 组装安装包 | [acps-docs …/install-package-build.md](../../../../acps-docs/tutorials/install-package-build.md) |
| 本机 / 远端单机首装（image / host） | [acps-docs …/install-package-ansible-deploy.md](../../../../acps-docs/tutorials/install-package-ansible-deploy.md) |
| 多机（三节点拆分） | [acps-docs …/install-package-ansible-deploy-3nodes.md](../../../../acps-docs/tutorials/install-package-ansible-deploy-3nodes.md) |
| 多机（app + deps 两机） | 包内 `inventories/hosts-multi.example.yml` + 下文 §多机 |
| OIDC Web / Device | [oidc-web-app-manual-verification.md](../../../../acps-docs/tutorials/oidc-web-app-manual-verification.md)、[oidc-acps-cli-device-login.md](../../../../acps-docs/tutorials/oidc-acps-cli-device-login.md) |
| 升级 / 回滚 | 本树 `README.md`「升级」/「回滚」节 |
| Monitor / AMP | 产品默认随 Monitor 启用；业务验收 D 见 `business.yml` |
| 已知限制 | [known-limitations.md](./known-limitations.md) |
| 旁路入口契约 | [known-limitations.md](./known-limitations.md)「旁路契约」 |

## Inventory 速查

| 文件 | 用途 |
| --- | --- |
| `inventories/hosts.example.yml` | image 单机（`acps-node-1`） |
| `inventories/hosts.rocky9.yml` / `hosts.ubuntu22.yml` / `hosts.rocky8.yml` / `hosts.ubuntu20.yml` | host 单机 |
| `inventories/hosts.4os-multi.yml` + `host_vars/*.acps.local.yml` | host 四 OS 混部（每组件一组一机） |
| `inventories/hosts-multi.example.yml` | image 多机：app + deps |
| `inventories/secrets.example.yml` | 复制为 `secrets.yml`（勿入库真秘密） |

```bash
cd "$PKG/ansible"   # 或源码树 release/install-packaging/ansible
cp inventories/hosts-multi.example.yml inventories/hosts.yml
# 按注释改主机名，并落盘对应 host_vars
cp inventories/secrets.example.yml inventories/secrets.yml
# CA：scripts/generate_ca_materials.sh …
ansible-playbook -i inventories/hosts.yml playbooks/preflight.yml -e @inventories/secrets.yml
ansible-playbook -i inventories/hosts.yml playbooks/site.yml -e @inventories/secrets.yml
ansible-playbook -i inventories/hosts.yml playbooks/smoke.yml -e @inventories/secrets.yml
# demo 双开后：
ansible-playbook -i inventories/hosts.yml playbooks/business.yml -e @inventories/secrets.yml
```

## 半安装恢复（失败即停之后）

1. 看控制节点 play 日志与目标机 `journalctl -u acps-*` / `docker compose ps`。  
2. **同一 inventory / secrets** 下重跑失败步骤：优先 `ansible-playbook … playbooks/site.yml --tags <phase_tag>`（见 `site.yml` tags）；或全量 `site.yml`（**终态收敛**：无材料/配置变更时应跳过签发并避免无条件重启；**不要用 site 当 upgrade**——升级/回滚用 `upgrade.yml` / `rollback.yml`，续签用 `renew-certs.yml` / `refresh-trust-bundle.yml`）。  
3. **「site 再跑一次」是收敛验收必测**，不是可选；重跑后关键观测（active、TLS、alive-sync）应与稳定终态一致。  
4. **慎用** `--limit`：只重跑一台时可能留下跨机证书/广告地址不一致；多机修复后建议再跑一次 `smoke.yml`。  
5. 升级中途失败：保留 `releases/` 与 state；修因后重跑 `upgrade.yml`（勿对生产机 `down -v`）。

## controller 工作区隔离（推荐 / 收敛验证必用）

在同一 `controller` 上做多套部署（image / host Rocky / host Ubuntu、或并行门控）时，**每次部署使用独立工作目录（WS）**，避免互相覆盖 inventory、secrets、CA 与控制节点 state：

```text
~/acps-ws/conv-<step>-<topo>-<YYYYMMDD-HHMMSS>/
  pkg/     # 本次数安装包解压根（唯一执行树）
  logs/    # 本过程全部 tee 日志
  meta.txt
```

解包、改配置、跑 `ansible-playbook`、写日志均在该 WS 内。目标业务机不相交时可多 WS 并发；**禁止**两套过程同时对同一业务机跑 `site`，也禁止共用同一解压目录。旁路验证建议：`~/acps-ws/bypass-sbN-<topo>-YYYYMMDD-HHMMSS/`。

## 旁路 playbook（续签 / trust / 升级 / 回滚）

| Playbook | 职责 | 勿混用 |
| --- | --- | --- |
| `renew-certs.yml` | 叶证临期/force 续签 | **不修 SAN**；SAN → `site` 或显式 force 全量 |
| `refresh-trust-bundle.yml` | 拉取并分发 trust | 不续叶子 |
| `upgrade.yml` | 显式组件列表升级 | **不是** `site.yml`；升 `ca_server` 目标默认 follow refresh（SB9） |
| `rollback.yml` | 切 `previous` | **不**回滚证书/DB；回滚后勿用 site 盖回 |
| CLI 回滚（image/host） | 适配器可分叉；CSV/state/previous/smoke 同构 | 见 known-limitations BP15 契约 |

site 再跑一次所覆盖的 renew/refresh 切片，只证明旁路入口可用；旁路实现本身已按 [known-limitations.md](./known-limitations.md)「旁路契约」收敛。

## Monitor / AMP 启用前提（摘要）

- `monitor_server_enabled: true`（产品默认）时随装 Redpanda / VM / CH / MinIO / OpenSearch + Forwarder。  
- 磁盘与内存按现场评估（见 `group_vars` 与组件默认值）。  
- ClickHouse / OpenSearch 当前为明文 HTTP。  
- 双 OS / 换业务机名串行部署：advertise/SAN 指纹变更自动重签；issuer 变更清空叶证并强制重签；证书/trust 落地后重启依赖闭包；smoke/renew/refresh 对真实 TLS 平面 fail-closed 握手验收。
