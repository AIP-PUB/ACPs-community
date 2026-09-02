[首页](../README.md)

# 从应用发布包构建 Docker 镜像包

跟着这篇教程，你可以把上一阶段打好的应用发布包，再变成一组可直接 `docker load` 的独立 `.image.tar.gz`。这些镜像包是后面组装安装包的输入。

可以记三句：

1. **默认只打本机架构**：脚本按 `linux/<本机 arch>` 过滤目标（Apple Silicon 上就是 `linux/arm64`），不必再手写 `--platform`。
2. **日常命令很短**：`targets` / `lock` / `strategy` 都有目录内默认值；先 `plan`，再 `build`。
3. **产物只有独立镜像包**，平铺在输出目录；不再生成 `acps-images-*.tar`。

整条流水线是：源码 → 应用发布包 → 独立镜像包 → 安装包。跨机构建交付时，优先传安装包。

> **host-mode 不走本教程**：host 安装包直接消费应用发布包 + vendor，见 [组装安装包 §2](./install-package-build.md)。

---

## 1. 准备

你需要：

1. 一组本机架构的应用发布包（见 [从源代码构建应用发布包](./app-release-package-build.md)），平铺在某个目录顶层。
2. 本机有 `acps-infra`。
3. Docker 能跑 `docker buildx`，且能跑**本机对应**的 Linux 平台（例如 arm64 机上的 `linux/arm64`）。
4. `python3` 可用（与应用发布包构建相同，建议用户级 3.14）。

自检：

```bash
cd /path/to/acps/acps-infra

docker version
docker buildx version
python3 --version

# Apple Silicon / arm64
docker run --rm --platform linux/arm64 python:3.14-slim python --version
# amd64 Linux 则换成 linux/amd64
```

准备目录变量（把上一阶段的 `--output` 直接当作输入）：

```bash
APP_RELEASE_DIR=/tmp/acps-app-release-output
IMAGE_OUT=/tmp/acps-image-packages

# 会清空镜像输出目录；不要指向重要数据
rm -rf "${IMAGE_OUT:?}"
mkdir -p "$IMAGE_OUT"

ls "$APP_RELEASE_DIR"/*.tar.gz
```

默认应用发布包大约 8 个（discovery 仅 CPU、CLI 仅 linux）。若你打了 GPU 或 Darwin CLI，数量会更多；其中 **Darwin 的 CLI 发布包不会进入 Linux 应用镜像矩阵**。

默认镜像清单与之对齐：discovery **只含 cpu**；infra 含 **fluent-bit**（AMP Forwarder，与 PG/Redis 同类）。**demo Web 不再单独打 `demo-nginx` 镜像**：由 `demo-leader` 应用镜像内的 `demo-leader-web` 入口提供静态页与 `/api/v1/` 同源反代。需要 discovery **gpu** 镜像时，先打出 GPU 应用发布包，再用 `image-targets.with-gpu.toml`（见 §6.1）。

---

## 2. 规划：将打哪些镜像包

在真正 build 之前，先扫一遍输入、列出本机平台下的期望产物：

```bash
cd /path/to/acps/acps-infra

release/image-packaging/plan-image-packages.sh \
  --app-release-dir "$APP_RELEASE_DIR"
```

它会：

1. 校验目录内默认的 `image-targets.toml` 与 `image-inputs.lock`
2. 扫描 `$APP_RELEASE_DIR` 里的 `*-app-release-*.tar.gz`（Darwin 包会注明不进 Linux 矩阵）
3. 列出本机 arch 下期望的应用镜像包、基础设施镜像包

没有 `--output` 时只做规划：校验失败或过滤后目标数为 0 才退出非 0；缺发布包输入会告警，但仍退出 0，方便你先看全貌。

---

## 3. 构建应用镜像包

```bash
cd /path/to/acps/acps-infra

release/image-packaging/build-app-images.sh \
  --app-release-dir "$APP_RELEASE_DIR" \
  --output "$IMAGE_OUT"
```

未传 `--platform` 时，日志里会有类似「默认只构建 `linux/<host_arch>`」。每个目标会：匹配唯一的应用发布包 → buildx 导出 → `docker load` → smoke → 写出 `.image.tar.gz`。任一目标失败即整体失败。

---

## 4. 构建基础设施镜像包

```bash
cd /path/to/acps/acps-infra

release/image-packaging/build-infra-images.sh \
  --output "$IMAGE_OUT"
```

同样默认只打本机 arch。产物与应用镜像包放在同一 `$IMAGE_OUT` 即可。常见基础设施包括：`redis`、`postgres-pgvector`、`rabbitmq`、`gateway-nginx`、`keycloak`、`redpanda`、`victoria-metrics`、`clickhouse`、`minio`、`opensearch`、`fluent-bit` 等（以 `image-targets.toml` 为准）。`gateway-nginx` 仍会构建，但 **image-mode 安装包不消费它**；安装包也不消费 `demo-nginx`。

---

## 5. 核对产物

构建后再跑一次 plan，并带上 `--output`：缺文件或仍缺应用发布包输入时会退出非 0。

```bash
cd /path/to/acps/acps-infra

release/image-packaging/plan-image-packages.sh \
  --app-release-dir "$APP_RELEASE_DIR" \
  --output "$IMAGE_OUT"

ls "$IMAGE_OUT"/*.image.tar.gz | sort
```

文件名类似：

```text
acps-registry-server-2.2.0-linux-arm64.image.tar.gz
acps-discovery-server-2.2.0-linux-arm64-cpu.image.tar.gz
acps-redis-7-alpine-2.2.0-linux-arm64.image.tar.gz
```

**不应**再出现 `acps-images-*.tar`。

加载后可以这样确认（任选一个）：

```bash
pkg="$(ls "$IMAGE_OUT"/acps-registry-server-*-linux-*.image.tar.gz | head -n 1)"
docker load -i "$pkg"
docker images | head
```

---

## 6. 高级参数（日常可跳过）

日常路径不用写这些。需要换清单、收窄矩阵、或显式覆盖平台时再用。

### 6.1 配置文件默认值

矩阵脚本与 `plan-image-packages.sh` 默认使用 `release/image-packaging/` 下同名文件：

| 参数 | 默认 |
| --- | --- |
| `--targets` | `image-targets.toml` |
| `--lock` | `image-inputs.lock` |
| `--strategy`（仅应用矩阵） | `startup-strategies.toml` |

显式覆盖示例：

```bash
release/image-packaging/build-app-images.sh \
  --app-release-dir "$APP_RELEASE_DIR" \
  --output "$IMAGE_OUT" \
  --targets /path/to/my-image-targets.toml \
  --lock /path/to/my-image-inputs.lock \
  --strategy /path/to/my-startup-strategies.toml
```

需要 discovery **gpu** 时（须先有 GPU 应用发布包；Mac 上打不出）：

```bash
# 应用发布包阶段（仅 Linux 构建机）：--discovery-variant both
release/image-packaging/build-app-images.sh \
  --app-release-dir "$APP_RELEASE_DIR" \
  --output "$IMAGE_OUT" \
  --targets release/image-packaging/image-targets.with-gpu.toml
```

不要用全局 `--variant cpu` / `--variant gpu` 来「只留某 variant」——`--variant` 会把「无 variant」的应用目标一并滤掉。

### 6.2 过滤单个应用 / 基础设施

```bash
# 只构建某一个 app
release/image-packaging/build-app-images.sh \
  --app-release-dir "$APP_RELEASE_DIR" \
  --output "$IMAGE_OUT" \
  --app registry-server

# 只构建某一个 infra
release/image-packaging/build-infra-images.sh \
  --output "$IMAGE_OUT" \
  --id redis
```

### 6.3 `--platform`（逃生口）

未传时已是 `linux/<host_arch>`。Mac → 打本机同 arch 的 Linux 镜像时**不必**再写 `--platform linux/arm64`。

仅在你明确要覆盖默认过滤时使用，例如对照清单里另一 arch 的条目做调试（产品双 arch 仍应换对应架构的构建机，而不是靠本机 QEMU 打完整矩阵）：

```bash
release/image-packaging/build-app-images.sh \
  --app-release-dir "$APP_RELEASE_DIR" \
  --output "$IMAGE_OUT" \
  --platform linux/arm64
```

`plan-image-packages.sh` 同样支持 `--targets` / `--lock` / `--platform`。

### 6.4 两种架构都要怎么办

在 **amd64** 与 **arm64** 构建机上各自跑完：应用发布包 → 镜像包。不要指望在一台机器上用 QEMU 打完整双架构产品矩阵。

---

## 7. 出了问题先看这里

| 现象 | 可以怎么做 |
| --- | --- |
| `plan` 报 `MISSING_INPUT`（例如 discovery gpu） | 默认清单不含 gpu；若你显式用了 `with-gpu` targets，补打对应应用发布包，或改回默认清单 |
| 找不到匹配的应用发布包 | 确认目录顶层有本机 arch、对应 variant 的 `*-app-release-*.tar.gz`，且同组合只有一个 |
| 提示过滤后没有任何 target | 检查清单是否包含本机 `linux/<arch>`；或见 §6.3 |
| Docker / buildx 失败 | 先保证本机对应 platform 的容器能跑 |
| 仍想生成集合 tar | `build-image-collection.sh` 已废弃；请把 `$IMAGE_OUT` 直接交给安装包构建 |

---

## 8. 接下来做什么

把 `$IMAGE_OUT` 里的独立 `.image.tar.gz` 交给安装包组装：[组装安装包](./install-package-build.md) **§1（image-mode）**。镜像打包到此结束。
