[首页](../README.md)

# 从源代码构建应用发布包

跟着这篇教程，你可以从 ACPs 源码打出一组 `*-app-release-*.tar.gz`。这些包是后面做 Docker 镜像包、**image / host 安装包**的共用输入；脚本跑通时，每个包都已经做过基本校验。

构建规则可以记三句：

1. 业务应用只打 **本机架构** 的 Linux 包（Mac 上是 `linux/arm64`，常见 Linux 服务器是 `linux/amd64`），不会顺带打另一种架构。
2. 打好的文件都在输出目录**最外层**，不用再整理子目录。
3. 默认打 discovery 的 CPU 版、以及 Linux 版的 `acps-cli`；需要 GPU 版 discovery 或 Mac 版 CLI 时，加参数即可。

---

## 1. 准备目录

把相关仓库放在同一个父目录下（和日常开发一样）：

```text
acps/
|-- acps-infra/
|-- acps-sdk/
|-- registry-server/
|-- ca-server/
|-- discovery-server/
|-- monitor-server/
|-- mq-auth-server/
|-- demo-leader/
|-- demo-partner/
`-- acps-cli/
```

脚本不会帮你 clone 或切分支。缺哪个目录，构建会直接报错。可以先扫一眼：

```bash
cd /path/to/acps

for repo in \
  acps-infra acps-sdk registry-server ca-server discovery-server \
  monitor-server mq-auth-server demo-leader demo-partner acps-cli
do
  test -d "$repo" || echo "missing: $repo"
done
```

---

## 2. 准备工具

本机需要：

- Docker（当前用户能跑 `docker` / `docker buildx`）
- `just`、`uv`（按各自官方文档安装即可）
- 发布脚本用到的 `python3`：至少 **3.11**，正式目标是 **3.14**
- 打包用系统工具：`tar`、`gzip`、`rsync`、`sha256sum`

系统自带的 `python3` 往往偏旧（例如 3.9）。用 uv 给**当前用户**装一份 3.14，并设成你的默认 `python3` 即可。

### 2.1 用 uv 给当前用户安装 Python 3.14

```bash
uv python install 3.14 --default
```

说明：

- 解释器本体在用户目录（例如 `~/.local/share/uv/python/…`），不碰系统级 Python。
- 默认会在 `~/.local/bin` 放上版本号命令 `python3.14`；加上 `--default` 后，还会放上无版本号的 `python` 和 `python3`，都指向这份 3.14。
- 只要 `~/.local/bin` 排在 `/usr/bin` 前面，你敲 `python3` 用到的就是用户这份，而不是系统自带的旧版本。

若提示无法覆盖已有可执行文件，可再加 `--force`（仍只作用于用户目录里的链接，不会改系统文件）：

```bash
uv python install 3.14 --default --force
```

### 2.2 其它工具自检

```bash
docker version
docker buildx version
just --version
uv --version
python3 --version
uname -sm
```

再确认 Docker 能跑**本机对应**的 Linux 容器，例如 Apple Silicon：

```bash
docker run --rm --platform linux/arm64 python:3.14-slim python --version
```

在 amd64 Linux 上把 `linux/arm64` 换成 `linux/amd64`。第一次构建会拉镜像、下依赖，时间长一些是正常的。

---

## 3. 一键构建（默认矩阵）

进入 `acps-infra`：

```bash
cd /path/to/acps/acps-infra

release/app-packaging/build-app-release-packages.sh \
  --output /tmp/acps-app-release-output
```

注意：`--output` 目录会被清空再建，不要指向有重要文件的路径。

成功后默认大约 **8** 个包，都在输出目录顶层，例如：

```bash
ls /tmp/acps-app-release-output/*.tar.gz
```

里面会有各业务服务、CPU 版 discovery，以及 Linux 版 `acps-cli`。文件名里的架构就是你这台机器的架构。

脚本中途失败会停下来；成功退出就表示这批包已经过完结构检查和运行冒烟。这个目录可以直接拿去跑 [Docker 镜像包教程](./docker-image-packages-from-app-release.md)。

想保留中间过程时，可以加 `--work-dir /tmp/acps-app-release-work`。

---

## 4. 按场景加参数

### 4.1 在 Mac 上同时要 Darwin 和 Linux 的 CLI

当需要在Linux之外的系统上运行acps-cli工具时，比如后续安装时Ansible的控制节点如果跑在 Mac 上，那么缺省情况下 acps-cli 的Linux打包就不够，还需要 Darwin 版 `acps-cli`：

```bash
release/app-packaging/build-app-release-packages.sh \
  --output /tmp/acps-app-release-output \
  --cli-target-os darwin,linux
```

其它业务包仍然是 Linux；额外多一个 `acps-cli-darwin-…`。只有在 Mac 上才能请求 `darwin`。

### 4.2 在 Linux 上打 CPU + GPU 的 discovery

```bash
release/app-packaging/build-app-release-packages.sh \
  --output /tmp/acps-app-release-output \
  --discovery-variant both
```

会多出一个 GPU 版 discovery。在 amd64 Linux 上实测，该包大约 **2.7 GiB**（主要是 GPU 相关依赖）。在 Mac 上加 `gpu` / `both` 会直接失败。因为 discovery GPU 版的某些依赖库，需要用源代码来编译，而 pip 不支持交叉编译，由于我们打包的目标系统是linux，就只能用Linux上的工具链进行编译，进而完成 discovery GPU 版的打包。

---

## 5. 用本机未提交代码去远端构建

本地改动还没 commit、push，但又想在 Linux 构建机上验证时，先在开发机打一份工作区快照（会带上未提交内容，不含 `.git`）：

```bash
cd /path/to/acps/acps-infra

# macOS：避免把 AppleDouble / xattr 打进包，远端 GNU tar 解压时刷屏警告
export COPYFILE_DISABLE=1

release/app-packaging/pack-dev-sources.sh \
  --output /tmp/acps-app-release-dev-sources.tar.gz
```

拷到远端、解压后，目录关系和本机 sibling 布局一致：

```bash
scp /tmp/acps-app-release-dev-sources.tar.gz builder.example:/tmp/
ssh builder.example '
  mkdir -p ~/acps-dev-verify && cd ~/acps-dev-verify
  tar -xzf /tmp/acps-app-release-dev-sources.tar.gz
  # 若仍见大量 LIBARCHIVE.xattr.* 警告，一般可忽略；本机打包前设 COPYFILE_DISABLE=1 可消除
  cd acps-app-release-dev-sources-*/acps-infra
'
```

然后在解压出来的 `acps-infra` 里，按第 2.1 节确认远端用户的 `python3` 已是 3.14，再执行第 3、4 节的构建命令。远端同样需要 Docker、`just`、`uv`。

---

## 6. 采集和装配分开做（可选）

默认一键脚本会先采集、再装配。排查问题时，也可以拆开：

```bash
# 只采集
release/app-packaging/collect-app-release-kit.sh \
  --output /tmp/acps-app-release-work/assembly-kit

# 复用已有 kit 再装配（可加 --cli-target-os / --discovery-variant）
release/app-packaging/build-app-release-packages.sh \
  --assembly-kit /tmp/acps-app-release-work/assembly-kit \
  --output /tmp/acps-app-release-output
```

日常发布仍建议直接用一键脚本，少一步操心。

---

## 7. 出了问题先看这里

| 现象 | 可以怎么做 |
| --- | --- |
| 提示兄弟目录不存在 | 回到父目录，把缺的仓库补齐 |
| 找不到 `just` / `uv` | 安装并把它们放进 `PATH` |
| Docker / buildx 报错 | 确认当前用户能跑 Docker；必要时加入 `docker` 组或用 `sg docker` |
| 报 `tomllib` 找不到 | 按第 2.1 节执行 `uv python install 3.14 --default`，并确认 `which python3` 落在 `~/.local/bin` |
| 缺 `.env.example` | 确认各应用仓库里有该文件；若用源码快照，用当前的 `pack-dev-sources.sh`（会保留模板，不会误删） |
| Mac 上要 GPU，或 Linux 上要 Darwin CLI | 换到对应系统的机器，或去掉不合适的参数 |
| 远端解压刷 `LIBARCHIVE.xattr.*` | 本机打包前 `export COPYFILE_DISABLE=1` 再跑 `pack-dev-sources.sh`；警告通常不影响解压结果 |
| 第一次特别慢 | 多半是在拉镜像和依赖，属正常；重复构建会快一些 |

只想重打某一个应用时，可以先按第 6 节采好 kit，再进 kit 目录调用 `assembly/assemble-and-validate.sh`（`--platform` 的架构必须和本机一致）。日常不必走这条路径。

---

## 8. 接下来做什么

- 用这些应用发布包继续打 Docker 镜像包：[从应用发布包构建全部 Docker 镜像包](./docker-image-packages-from-app-release.md)（把 `--app-release-dir` 指到本文的 `--output` 即可）。
- 若镜像清单需要 **两种架构** 的应用包，在各自架构的构建机上各打一份，再把顶层的 `*.tar.gz` 放进同一个目录。
- 本文这一层做的是「把依赖预先装配成 wheelhouse，好让部署时能离线安装」。如果部署机可以联网装依赖，或者目标环境不能用 Docker、操作系统不在支持矩阵内，可以直接从本文的上游输入——各项目 `just package wheel` 的**应用薄包**——开始部署：[从应用薄包手工部署](./manual-deploy-from-app-thin-package.md)。部署步骤两边一致，差别只在依赖是离线装还是联网装。

