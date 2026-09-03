# 安装层已知限制

以下为当前产品行为边界（不是待办清单）。

任务向导（何时用 `renew` / `refresh` / `upgrade` / `rollback` vs `site`）见  
[`acps-docs/tutorials/install-package-day2-ops.md`](../../../../acps-docs/tutorials/install-package-day2-ops.md)。

## site 契约（终态收敛）

| 项 | 约定 |
| --- | --- |
| `site.yml` | **终态收敛**：无材料/配置变更时跳过签发、避免无条件重启；「site 再跑一次」为运维收敛必测 |
| 非升级入口 | **不要用 site 当 upgrade**；升级/回滚用 `upgrade.yml` / `rollback.yml` |
| 续签 / trust | `renew-certs.yml` / `refresh-trust-bundle.yml` |
| controller 隔离 | 多套部署各用独立工作目录（见 [README.md](./README.md)「controller 工作区隔离」）；禁止共目录改配置 |

site 与旁路（renew / refresh / upgrade / rollback）均已按本文件契约收敛。逐步运维见 [日常运维教程](../../../../acps-docs/tutorials/install-package-day2-ops.md)。

## 旁路契约（运维入口）

| 项 | 约定 |
| --- | --- |
| `renew-certs.yml` | 扫描范围 = **到期窗口 / force**；**不**根据 advertise/SAN 漂移自动进 due。**同 AIC 重签叶子**（不 delete+recreate Agent）。SAN/issuer 地址面修复走 **`site.yml`**（可能换 AIC）或显式身份重建；禁止宣称「renew 已修 SAN」 |
| `refresh-trust-bundle.yml` | 只刷 trust；不续叶子 |
| `upgrade.yml` / `rollback.yml` | 制品生命周期；**回滚/升级后勿用 `site.yml` 盖回**（易用新包模板/digest 破坏回滚意图） |
| 升 `ca_server` | 默认 follow `refresh-trust-bundle`（`acps_upgrade_follow_refresh_trust`：CSV 含 `ca_server` 时默认 true；显式 `false` → WARNING，不得作收敛证据）。不内嵌 `renew-certs` |
| 回滚含 `ca_server` | **不**回滚证书/trust；强制至少 TLS smoke + WARN；需对齐时手动 `refresh-trust-bundle.yml` |
| site 再跑与旁路 | site 再跑一次所覆盖的 renew/refresh 切片只证明旁路入口可用；旁路实现已按本表契约收敛 |
| host 四 OS | rocky8 / rocky9 / ubuntu20 / ubuntu22 共用旁路入口；新 OS 扩展后至少在该 OS 上跑通 renew / refresh / app+vendor upgrade·rollback / os_package 回滚拒绝 |
| image/host CLI 回滚 | 适配器可分叉（image：previous 制品重装；host：`rollback_one` releases）；CSV/state/previous/smoke 语义同构 |

## 其它边界

| 项 | 现状 |
| --- | --- |
| ClickHouse / OpenSearch TLS | 明文 HTTP |
| 证书续签 | 调用 `renew-certs.yml` / `refresh-trust-bundle.yml`；无内置常驻守护 / cron |
| 跨大版本制品升级/回滚 | 门控与演练多为同 tag / `acps_force_app_reinstall`；跨大版本需人工准备第二版本制品 |
| 编排目标 | Docker Compose（image）与 host systemd/venv；不覆盖 Podman / Swarm / K8s / 统一网关 |
| DB 回滚 | 不自动执行 `migrate downgrade`；回滚只切制品指针 |
| `amp_forwarder` | 可列入 `upgrade.yml` / `rollback.yml` CSV（`acps_upgrade_components=amp_forwarder`）；与其它 vendor_bundle / image 组件同构 |
| Keycloak Java（host） | 使用安装包内 Temurin JRE17 vendor（`JAVA_HOME`）；**不**再依赖目标机在线 OpenJDK。PG/Redis/RabbitMQ 仍为 OS 包 |
| 同 controller 串行多业务机 | `control/work/certs` 叶证 marker 可能复用；advertise / extra SAN 变更时自动检测指纹并重签（仍可用 `acps_force_cert_renew=true` 手动强制）。issuer（root+intermediate）指纹变更会自动清空叶证/demo staging 并强制重签。**多套并行部署请用独立 WS**，勿共用解压目录 |
| TLS 热重载 | 无；叶证/trust 落地后经直接重启 + 依赖闭包重启（redis→mq-auth/monitor/amp_forwarder，rabbitmq→demos，mq-auth→rabbitmq） |
| TLS 验收 | `smoke_basic` 对 Redis TLS / AMQPS / mq-auth mTLS / registry:9002 / demo HTTPS 做 fail-closed 握手；renew/refresh 复用同一门禁 |
| PostgreSQL 主版本 | host 路径 fail fast；大版本须人工 `pg_upgrade` / dump-restore |
| Docker Desktop / macOS image 同机 | **不**写 `/etc/docker/daemon.json`（Desktop 不走该路径）；仍强制 `acps-net` MTU=`acps_docker_mtu`。宿主机出口 MTU 须现场 ≤1400。`acps_advertise_host` **禁止** loopback（预检）；`ansible_host: 127.0.0.1` + `connection: local` 仅表示 Ansible 连本机 |
| `acps_advertise_host` 门禁 | image：**禁**空/loopback。host 单机：允许 `127.0.0.1`。host 多机：**禁** loopback |
| Colocated TLS smoke（B14） | 仅 image 且 RMQ 与业务同 inventory 主机；委托 connection 取自目标 `host_vars`（默认 ssh）。docker 可执行文件经 `PATH` / 常见路径解析，不硬编码 `/usr/bin/docker` |

更细的操作说明见 [README.md](./README.md) 与 [acps-docs](../../../../acps-docs/README.md)。
