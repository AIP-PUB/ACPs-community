[首页](../README.md)

# 组装安装包（image-mode / host-mode）

跟着这篇教程，用同一入口 `build-install-package.sh` 打出可解压部署的安装包。先选模式，再按对应章节操作。

```text
源码 → 应用发布包 ─┬→ [image] 独立镜像包 → acps-image-install-*.tar → Ansible
                  └→ [host]  厂商 vendor  → acps-host-install-*.tar  → Ansible
```

可以记三句：

1. **单一入口**：`release/install-packaging/scripts/build-install-package.sh`（`--mode image|host`）。
2. **应用发布包两模式共用**（见 [从源代码构建应用发布包](./app-release-package-build.md)）；image 多一步镜像包，host 多准备 vendor 目录。
3. **控制节点 CLI 平台**用 `--control-platform`（例如 Mac 上 `darwin-arm64`），与业务机平台可以不同。

部署见 [用安装包部署 ACPs](./install-package-ansible-deploy.md)。

---

## 0. 先选 mode

| | **image** | **host** |
| --- | --- | --- |
| 命令 | `--mode image`（可省略，默认） | `--mode host`（必填） |
| 输入 | 平铺 `*.image.tar.gz` + app-release | app-release + `--vendor-bundle-dir` |
| 业务平台参数 | `--image-platform` | `--target-platform`（亦可用 `--image-platform` 作别名） |
| 产物 | `acps-image-install-{ver}-{plat}.tar` | `acps-host-install-{ver}-{plat}.tar` |
| 包内内容 | `artifacts/images/` + `control/` | `artifacts/apps\|control\|vendor/`（**无** `images/`、**无** `bin/acps-install`） |
| 业务机 | 任意能跑 Docker 的 Linux（arch 匹配） | **仅** Rocky 8/9 / Ubuntu 20.04/22.04 |

---

## 1. image-mode：从镜像包装配

### 1.1 准备

1. 平铺镜像包目录（见 [从应用发布包构建 Docker 镜像包](./docker-image-packages-from-app-release.md)）。若要开 **AMP Forwarder**，需要先构建好 **fluent-bit**。**demo Web 不需要单独镜像**：装 `demo-leader` 应用镜像即可（Compose 起 `demo_leader` + `demo_leader_web`，默认端口 **9030**）。
2. 应用发布包目录，至少有控制节点平台的 `acps-cli-*-app-release-*.tar.gz`。
3. 本机有 `acps-infra`，`python3` 可用。

```bash
cd /path/to/acps/acps-infra

APP_RELEASE_DIR=/tmp/acps-app-release-output
IMAGE_OUT=/tmp/acps-image-packages
INSTALL_OUT=/tmp/acps-install-packages

# Apple Silicon 本机示例
IMAGE_PLATFORM=linux-arm64
CONTROL_PLATFORM=darwin-arm64

ls "$IMAGE_OUT"/*.image.tar.gz | head
ls "$APP_RELEASE_DIR"/acps-cli-*-app-release-*.tar.gz
```

builder 上打好的 amd64 镜像拷回本机后：`IMAGE_PLATFORM=linux-amd64`，`CONTROL_PLATFORM` 仍用本机 CLI。

### 1.2 一键组装

```bash
cd /path/to/acps/acps-infra/release/install-packaging
mkdir -p "$INSTALL_OUT"

./scripts/build-install-package.sh \
  --mode image \
  --image-dir "$IMAGE_OUT" \
  --app-release-dir "$APP_RELEASE_DIR" \
  --image-platform "$IMAGE_PLATFORM" \
  --control-platform "$CONTROL_PLATFORM" \
  --out-dir "$INSTALL_OUT"
```

成功后类似：

```text
/tmp/acps-install-packages/acps-image-install-2.2.0-linux-arm64.tar
```

包内 `artifacts/images/` 为**长名**镜像包；组装阶段**不会**再 `docker pull` / save。可以这样检查：

```bash
tar -tf "$INSTALL_OUT"/acps-image-install-*.tar | grep -E 'artifacts/(images|control)/' | sort
# 不应再出现 demo-nginx；若开了 AMP Forwarder，应有 fluent-bit 长名包
```

### 1.3 平台组合（image）

| 场景 | `--image-platform` | `--control-platform` |
| --- | --- | --- |
| Mac 打 arm64 业务包，本机控制 | `linux-arm64` | `darwin-arm64` |
| builder amd64 镜像，Mac 控制 | `linux-amd64` | `darwin-arm64` |
| 全程 Linux amd64 | `linux-amd64` | `linux-amd64` |

Darwin CLI 只进 `artifacts/control/`，不会进 `artifacts/images/`。

### 1.4 demo Web 与 fluent-bit（image）

| 能力 | 文件来源 | 说明 |
| --- | --- | --- |
| demo-leader Web UI | `demo-leader` **应用**镜像 | Compose：`demo_leader` + `demo_leader_web`；端口 **9030**；同源 `/api/v1/` 反代；`backendBase=''`。安装包**不使用**独立 `demo-nginx`。 |
| AMP Forwarder | infra **`fluent-bit`** | 需要在打镜像包阶段一并打进安装包。 |

`gateway-nginx` 可能出现在镜像清单，但**不进入**安装消费清单。

---

## 2. host-mode：从 app-release + vendor 装配

### 2.1 准备

1. 应用发布包目录（与 image 相同即可）：业务 app-release + 控制节点 `acps-cli-*-app-release-*.tar.gz`。**不需要**镜像包。
2. **厂商包**：默认缓存目录为 `release/install-packaging/.vendor-bundle/`（已 gitignore）。构建时 `ensure_vendor_bundle` 会按 `baseline-matrix.toml` 里每个 `[vendor.*]` 的 **url + sha256** 自动补齐：文件在且校验通过则复用；缺失则下载（部分组件会做 fetch 变换，如 MinIO 包一层 tar、ClickHouse 扁平化、fluent-bit 从 deb 整理）。也可**手动把正确文件放进缓存目录**加快构建，或在离线环境里预先放好。
3. 目标业务机为 **Rocky 8/9 或 Ubuntu 20.04/22.04**（arch 与 `--target-platform` 一致）。
4. 可选：`--bundle-python-dir` 打入离线 `tools/`；`--vendor-offline` 禁止下载（仅用已有缓存）。

```bash
cd /path/to/acps/acps-infra

APP_RELEASE_DIR=/tmp/acps-app-release-output
INSTALL_OUT=/tmp/acps-install-packages

# 业务机 amd64、Mac 作控制节点示例
TARGET_PLATFORM=linux-amd64
CONTROL_PLATFORM=darwin-arm64

ls "$APP_RELEASE_DIR"/*-app-release-*.tar.gz | head
```

只需单独预取厂商包时（可选）：

```bash
cd /path/to/acps/acps-infra/release/install-packaging
python3 scripts/ensure_vendor_bundle.py \
  --matrix baseline-matrix.toml \
  --arch amd64 \
  --cache-dir .vendor-bundle
```

### 2.2 一键组装

```bash
cd /path/to/acps/acps-infra/release/install-packaging
mkdir -p "$INSTALL_OUT"

./scripts/build-install-package.sh \
  --mode host \
  --app-release-dir "$APP_RELEASE_DIR" \
  --target-platform "$TARGET_PLATFORM" \
  --control-platform "$CONTROL_PLATFORM" \
  --out-dir "$INSTALL_OUT"
# 默认使用本树 .vendor-bundle/；也可 --vendor-bundle-dir /其它缓存
# 离线环境：先把需要的文件放进缓存，再加 --vendor-offline
```

成功后类似：

```text
/tmp/acps-install-packages/acps-host-install-2.2.0-linux-amd64.tar
```

可以这样检查：

```bash
tar -tf "$INSTALL_OUT"/acps-host-install-*.tar | grep -E 'artifacts/(apps|control|vendor)/' | sort
# 应有 baseline-matrix.toml；不应有 artifacts/images/ 或 bin/acps-install
```

构建期会把 `acps_deploy_mode=host` 等写回包内 `group_vars`。vendor 的 url / `sha256_amd64|arm64` 写在 `baseline-matrix.toml`；升级厂商版本时改 version+url+sha256 三件套。

### 2.3 平台组合（host）

| 场景 | `--target-platform` | `--control-platform` |
| --- | --- | --- |
| Rocky/Ubuntu amd64，Mac 控制 | `linux-amd64` | `darwin-arm64` |
| 全程 Linux amd64 | `linux-amd64` | `linux-amd64` |
| arm64 业务机 | `linux-arm64` | 与控制节点 CLI 一致 |

arm64 若 matrix 里 `sha256_arm64` 仍为空：ensure 仍可下载，但会告警；产品发布前应把打印的 sha256 回填进 matrix。

---

## 3. 解压后怎么用（预告）

两模式解压方式相同，只是包名不同：

```bash
rm -rf /tmp/acps-install-verify && mkdir -p /tmp/acps-install-verify
# image：
tar -xf "$INSTALL_OUT"/acps-image-install-*.tar -C /tmp/acps-install-verify
# 或 host：
# tar -xf "$INSTALL_OUT"/acps-host-install-*.tar -C /tmp/acps-install-verify

PKG=$(echo /tmp/acps-install-verify/acps-*-install-*-linux-*)
cp -a "$PKG/ansible/inventories/secrets.example.yml" "$PKG/ansible/inventories/secrets.yml"
# inventory：image 常用 hosts.example.yml；host 常用 hosts.rocky9.yml / hosts.ubuntu22.yml / hosts.rocky8.yml / hosts.ubuntu20.yml
```

完整步骤见 [用安装包部署 ACPs](./install-package-ansible-deploy.md)。装完后的续签 / trust / 升级 / 回滚见 [日常运维](./install-package-day2-ops.md)。

---

## 4. 高级说明（日常可跳过）

- 旧的 `ingest_image_artifacts.sh` / `materialize_artifacts.sh` 已废弃；`assemble_install_package.sh` 仅作转发。请用 `build-install-package.sh`。
- image 与 host **共用** Ansible 树；部署时靠 `acps_deploy_mode` 与 `stage_artifact_{{mode}}_{{kind}}` 决定怎么落地。
- 不消费已废弃的 `acps-images-*.tar`。
- 更细的门控与运维入口见 `acps-infra/release/install-packaging/README.md`；逐步运维教程见 [日常运维](./install-package-day2-ops.md)。

---

## 5. 出了问题先看这里

| 现象 | 可以怎么做 |
| --- | --- |
| 没有匹配平台的镜像（image） | 检查 `$IMAGE_OUT` 文件名是否含 `linux-arm64` / `linux-amd64` |
| 多个 CLI、无法推断 control | 显式 `--control-platform` |
| AMP Forwarder 缺镜像（image） | 在 image-packaging 构建 `fluent-bit` 后重打安装包 |
| vendor 缺项 / glob 多命中（host） | 看 ensure 日志；对照 `baseline-matrix.toml` 的 url/file；或手动放入 `.vendor-bundle/` |
| vendor 下载失败 / sha 不匹配 | 检查外网与 url；错文件会删掉重拉；离线用 `--vendor-offline` 前，要先准备好正确的缓存文件 |
| 混用 mode 参数 | image 要 `--image-dir`；host 不要传 `--image-dir` |

---

## 6. 接下来做什么

[用安装包部署 ACPs](./install-package-ansible-deploy.md)（image / host 同一篇，按 mode 分开说明）。image 且本机 Apple Silicon 控制节点 = 业务节点时，见该教程 **§4.7**。
