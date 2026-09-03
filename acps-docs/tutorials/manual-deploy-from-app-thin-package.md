[首页](../README.md)

# 从应用薄包手工部署

这篇教程讲的是**部署一套 ACPs 到底要做哪些事**：把应用装进一个 Python 环境、铺好配置、迁移数据库、签发证书、按正确顺序把进程拉起来、探活。全程手工命令，不依赖 Docker，不依赖 Ansible，也不绑定某个操作系统。

这是 ACPs 的**基础部署流程**。[Ansible 部署](./install-package-ansible-deploy.md)做的是同一件事——它把下面这些步骤写成了 playbook，并额外承担多机编排、操作系统差异适配、幂等升级回滚。实现这套自动化的源码树（`acps-infra/release/install-packaging`）下文统称**自动化安装层**。两者不是两条路线，是同一条流程的两种执行方式：

| 部署步骤 | 本文（手工执行） | 自动化安装层（自动执行） |
| --- | --- | --- |
| 准备应用 | 解开薄包，联网按锁文件装依赖 | 预先装配好 wheelhouse 的应用发布包，离线 `pip install --no-index` |
| 目录规划 | 你自己定 `$INSTALL_ROOT` | `/opt/acps` + `releases/{version}/` + `current` 软链 |
| 写配置 | 手工编辑 `config/*.toml` 与 `.env` | 从 inventory 渲染同名文件的 Jinja2 模板 |
| 数据库迁移 | `python -m alembic upgrade head` | 同一条命令，由 `db_migrate_host.yml` 调用 |
| 签发证书 | 手工跑 `acps-cli` 的 bootstrap | 同一个 `acps-cli`，由 `cert_provision` 角色调用 |
| 拉起进程 | 前台运行，或自己接进程管理器（如写 systemd unit） | 从 `runtime-package.toml` 渲染 systemd unit |
| 探活 | `curl /health` | 带重试的 `health_http.yml` |

命令基本是同一批，差别在谁来敲。所以这篇文档有两种读法：**要在装器覆盖不到的环境里部署**（不能装 Docker、操作系统不在 Rocky 8/9 与 Ubuntu 20.04/22.04 支持矩阵内、或者要接入自家的 supervisor / k8s / Nomad / SaltStack），就照着做；**用 Ansible 部署但想搞清楚装器在干什么、出问题时怎么手工介入**，就当参考手册查。

起点是**应用薄包**（`{app}-wheel-{version}.tar.gz`，`just package wheel` 的产物），它是整条流水线里唯一与操作系统无关的一层，也是自动化路径的上游输入。

```text
              just package wheel
                      |
                   应用薄包
                  /         \
        本文：直接部署        装配成应用发布包（带 wheelhouse，平台相关）
              |                        ↓ 组装安装包
              |                        ↓ Ansible
              \_______________________/
                        |
        同一套步骤：装依赖 / 铺配置 / 迁移 / 签证书 / 起进程 / 探活
```

---

## 0. 先读这一节：本文的三条边界

**一、依赖组件只提要求，不教安装。** PostgreSQL、Redis、RabbitMQ 这些怎么装、怎么调优，请按你所在环境的规范来。本文只给版本要求、必须开启的能力、以及 **ACPs 侧必须的接线**（这部分不能省，见 §6）。

**二、部署机需要联网访问 PyPI。** 薄包按设计**不含** `wheelhouse/`，第三方依赖在部署时从 PyPI（或你的私有镜像）装。要完全离线，可以按 §4.4 的提示自己预下载，或者用装配好 wheelhouse 的[应用发布包](./app-release-package-build.md)。

**三、进程守护由你决定。** 薄包不含 systemd unit、不含启动脚本、不含健康检查脚本。本文给出**权威的启动命令**（来自薄包内 `runtime-package.toml`），你把它接到 systemd / launchd / supervisor / 容器里都行。

---

## 1. 薄包里有什么

以 registry-server 为例，解包后：

```text
registry-server-wheel-2.2.0/
|-- dist/
|   `-- registry_server-2.2.0-py3-none-any.whl   # 只有应用自己的 wheel
|-- requirements-runtime.lock                     # 第三方依赖，带 hash，跨平台通用
|-- runtime-package.toml                          # 启动命令 / 端口 / 健康检查的权威声明
|-- config/                                       # default.toml + 各环境覆盖
|-- alembic/  alembic.ini                         # 数据库迁移
|-- .env.example                                  # 敏感项模板
|-- README.md
`-- checksums.txt
```

`runtime-package.toml` 是手工部署最该先看的文件——启动命令、端口、健康检查地址都在里面，照抄即可，不要自己猜：

```toml
[[components]]
id = "registry-server-api"
type = "python-service"
entrypoint = "uvicorn app.main:app --host 0.0.0.0 --port 9001"
ports = [9001]
health_check = "http://127.0.0.1:9001/health"
```

**薄包里没有的东西**（这几条决定了后面的步骤）：

| 没有 | 后果 |
| --- | --- |
| `wheelhouse/` | 装依赖要联网，见 §4.4 |
| 内部 wheel（`acps-sdk` / `acps-cli`） | 必须单独构建再装，见 §3 |
| Python 解释器 | 目标机自备 3.14，见 §4.1 |
| 启动脚本 / systemd unit | 你自己接，见 §4.8 |
| 证书 | 单独签发，见 §7 |

---

## 2. 构建薄包

在**开发机 / 构建机**上做（不是目标机）。仓库要按兄弟目录摆放，这一点手工和自动化完全相同：

```text
acps/
|-- acps-infra/     # 提供共享 just 模块，必须存在
|-- acps-sdk/
|-- registry-server/
`-- ...
```

构建机需要 `just`、`uv`、`python3`（3.11+，正式目标 3.14）。然后逐个项目：

```bash
cd /path/to/acps/registry-server
just package wheel
# 产物：dist/registry-server-wheel-2.2.0.tar.gz
```

`just package wheel` 会先自动跑一遍 bootstrap（准备构建用的 venv 等），不需要你手动 `just package bootstrap`。

支持这条命令的项目：`registry-server`、`ca-server`、`discovery-server`、`monitor-server`、`mq-auth-server`、`demo-leader`、`demo-partner`、`acps-cli`。

### 2.1 discovery-server 要指定架构与变体

discovery-server 是唯一**不跨平台**的薄包：它的锁文件按 `{cpu|gpu}-{amd64|arm64}` 生成，架构在打包时就定死了，且面向 manylinux。

```bash
cd /path/to/acps/discovery-server

# 默认只出 CPU 变体，架构取构建机的 uname -m
just package wheel
# 产物含 requirements-runtime-cpu-arm64.lock（或 -amd64）

# 需要 GPU 变体（只能在 Linux 上构建）
DISCOVERY_PACKAGE_VARIANTS=gpu just package wheel
```

**必须在与目标机相同架构的机器上构建 discovery-server 的薄包。** 其它项目的薄包平台无关，在哪台机器打都一样。

---

## 3. 单独构建内部 wheel（这一步不能跳）

薄包的锁文件是用 `uv export --no-emit-local` 导出的，本地路径依赖被主动剔除了；薄包的 `dist/` 也只放应用自己的 wheel。所以 `acps-sdk` **既不在锁文件里，也不在薄包里**——`runtime-package.toml` 只是用 `internal_wheels` 记了一笔元数据，自动化路径靠装配阶段读这个字段去补。手工部署就必须自己补。先看目标服务要哪些内部 wheel：

```bash
grep internal_wheels <解包目录>/runtime-package.toml
```

| 服务 | 需要的内部 wheel |
| --- | --- |
| registry-server / discovery-server / monitor-server | `acps-sdk` |
| ca-server / mq-auth-server | 无 |
| demo-leader / demo-partner | `acps-sdk`、`acps-cli` |
| acps-cli | `acps-sdk` |

**构建 `acps-sdk`**（它是共享库，没有 Justfile，直接用 uv）：

```bash
cd /path/to/acps/acps-sdk
uv build --wheel --out-dir /tmp/acps-internal-wheels
# 产物：acps_sdk-2.2.0-py3-none-any.whl
```

**取 `acps-cli` 的 wheel**（它有 Justfile，从自己的薄包里取，别用裸 `uv build`）：

```bash
cd /path/to/acps/acps-cli
just package wheel
tar -xzf dist/acps-cli-wheel-*.tar.gz -C /tmp
cp /tmp/acps-cli-wheel-*/dist/acps_cli-*.whl /tmp/acps-internal-wheels/
```

把 `/tmp/acps-internal-wheels/` 和各薄包一起拷到目标机。

---

## 4. 通用部署流程（每个服务都一样）

下面用 `registry-server` 演示。换服务只需要换薄包名、启动命令和环境变量，骨架完全一致。

### 4.1 目标机前置条件

| 项 | 要求 |
| --- | --- |
| Python | **3.14**；带 `venv` 模块 |
| 网络 | 能访问 PyPI 或你的私有 index |
| 工具 | `tar`、`sha256sum`（macOS 用 `shasum -a 256`） |
| 账号 | 建议建一个专用非特权账号（自动化安装层用的是 `acps`），本文不代你决定 |

#### 4.1.1 装 Python 3.14

在不同的系统中安装 Python 有多种方法，这里推荐用 uv 装 standalone 构建，自动化安装层走的也是这条路：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

# 装到全局位置，别装进某个用户的 HOME（原因见下）
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
sudo mkdir -p "$UV_PYTHON_INSTALL_DIR"
sudo chown "$USER" "$UV_PYTHON_INSTALL_DIR"
~/.local/bin/uv python install --no-bin 3.14

PYBIN="$(UV_PYTHON_PREFERENCE=only-managed ~/.local/bin/uv python find 3.14)"
echo "$PYBIN"    # /opt/uv-python/cpython-3.14.x-linux-x86_64-gnu/bin/python3.14
```

下面**一律用 `$PYBIN` 这个绝对路径**建 venv，别指望 `python3.14` 在 PATH 里：Debian/Ubuntu 只在 login shell 里通过 `~/.profile` 把 `~/.local/bin` 加进 PATH，非交互 SSH 和 systemd 都取不到；RHEL 系则默认就有。`--no-bin` 就是为了不去 `~/.local/bin` 放这个不可靠的软链。

装到 `/opt` 而不是 `~/.local` 同样重要：venv 会记住创建它的解释器的绝对路径，解释器要是藏在某个用户的 HOME 下，服务换用户跑（比如切到 `acps`）就会因为读不到解释器而起不来。

解释器版本要在所有节点上保持一致，自动化安装层把它钉在 `acps-infra/release/install-packaging/baseline-matrix.toml` 的 `[python].version`。

### 4.2 定目录布局

自动化安装层用的是 `/opt/acps`（运行时）、`/var/lib/acps`（数据）、`/var/log/acps`（日志）。手工部署你可以自己定，沿用这套也行——后续想切到 Ansible 会少一次搬家。唯一的硬约束是**同一个服务的 venv、`config/`、`.env`、`alembic/` 必须在同一个目录下**，原因见 §4.7。本文用：

```bash
INSTALL_ROOT=/opt/acps/registry-server
sudo mkdir -p "$INSTALL_ROOT"
sudo chown "$USER" "$INSTALL_ROOT"
```

`chown` 那行不能省。`sudo mkdir` 建出来的目录属主是 root，而后面解包、建 venv、`pip install` 全是以普通用户身份跑的，漏了这一步第一条 `tar` 就 `Permission denied`。如果你打算让服务以专用账号（比如 `acps`）运行，这里直接 `chown` 给那个账号，别用当前登录用户。

### 4.3 解包并校验

```bash
cd "$INSTALL_ROOT"
tar -xzf /path/to/registry-server-wheel-2.2.0.tar.gz --strip-components=1
sha256sum -c checksums.txt      # macOS: shasum -a 256 -c checksums.txt
```

`--strip-components=1` 把内容直接铺到 `$INSTALL_ROOT`，避免多套一层版本号目录。

### 4.4 建 venv 并装第三方依赖

**每个服务必须有独立的 venv。** registry / ca / discovery / monitor 四个服务的 wheel 装进去都是同一个顶层包名 `app`，共用 venv 会互相静默覆盖。

```bash
cd "$INSTALL_ROOT"
"$PYBIN" -m venv venv          # $PYBIN 来自 §4.1.1
./venv/bin/python -m pip install --upgrade pip

./venv/bin/pip install --require-hashes -r requirements-runtime.lock
```

锁文件是 uv 的通用导出，同一份文件在 Linux amd64/arm64 和 macOS 上都能装，win32 相关的包会被 marker 自动跳过（registry-server 的锁文件有 109 条，Linux 上实际装 106 个）。venv 建好之后就自包含了，后续所有命令都走 `./venv/bin/...`，不再依赖 PATH。

最后这条命令是整个流程里最慢的一步，全看你到 PyPI 的带宽——实测在一条约 25 kB/s 的链路上要 50 分钟。同一台机器上装第二个服务时会命中 pip 缓存，快得多。

如果目标机不能直连 PyPI，可以在一台能联网、**架构和 libc 与目标机一致**的机器上预下载，再拷过去离线装：

```bash
# 联网机
pip download --require-hashes -r requirements-runtime.lock -d ./wheelhouse
# 目标机
./venv/bin/pip install --no-index --find-links ./wheelhouse -r requirements-runtime.lock
```

预下载碰到没有预编译 wheel 的包会退化成源码编译，需要工具链。装配阶段正是用 manylinux 构建容器加 auditwheel 来根除这个问题——这也是本文默认走联网安装的原因。

### 4.5 装应用 wheel 和内部 wheel

必须**单独一条命令**、且带 `--no-deps`。`--require-hashes` 模式要求所有条目都带 hash，混进来会直接失败；依赖已经在上一步按锁文件装全了。

```bash
./venv/bin/pip install --no-deps \
  dist/*.whl \
  /path/to/acps-internal-wheels/acps_sdk-*.whl
```

这一步之后只能验证「装进去了」，还不能验证「能导入」：

```bash
./venv/bin/pip list | grep -Ei 'acps-sdk|registry-server'
```

**不要在这里就跑 `import app.main`。** 各服务的 `app.core.config` 是在模块级实例化配置对象的，`import app.main` 会立刻读 `.env`，而 `.env` 要到 §4.6 才写，此时必然抛 pydantic `Field required`。完整的导入验证放在 §4.6 之后、并且必须在 `$INSTALL_ROOT` 下执行：

```bash
cd "$INSTALL_ROOT"
./venv/bin/python -c "import app.main, acps_sdk; print('ok')"     # 无内部 wheel 的服务去掉 acps_sdk
```

### 4.6 写配置

配置分两层，不要混：

- **`config/*.toml`** — 非敏感运行参数（端口、日志、连接池、OIDC、CORS）。薄包带的 `config/production.toml` 多数项开箱可用，按需改。§8 的逐服务清单会点明。
- **`.env`** — 敏感项和必须先于 TOML 生效的启动参数（`APP_ENV` 就属于后者）。

```bash
cd "$INSTALL_ROOT"
cp .env.example .env
chmod 600 .env
# 编辑 .env：APP_ENV=production，填 DATABASE_URL 等
```

`APP_ENV` 决定加载哪个 `config/{APP_ENV}.toml` 覆盖文件，生产环境填 `production`。

各服务的必填项见 §8。`.env.example` 里带 `TEST_DATABASE_URL` 之类的测试专用项，生产可以删掉。

### 4.7 关键约定：必须从安装根目录启动

服务解析配置目录的逻辑是**先看当前工作目录下的 `config/`**，找不到才回落到源码树。`.env` 同理按当前目录找。所以：

- 进程的工作目录必须是 `$INSTALL_ROOT`
- systemd 就是 `WorkingDirectory=$INSTALL_ROOT`，其它进程管理器同理
- `alembic upgrade` 也要在这个目录下跑

工作目录不对时，服务会因为读不到 `.env` 而在启动瞬间抛 pydantic 的 `Field required` 校验错误——看到这个报错先查工作目录，不要急着怀疑环境变量。

### 4.8 迁移、启动、健康检查

有 alembic 的服务（registry / ca / discovery / monitor）先迁移：

```bash
cd "$INSTALL_ROOT"
./venv/bin/python -m alembic upgrade head
```

再按 `runtime-package.toml` 的 `entrypoint` 启动。手工前台验证：

```bash
cd "$INSTALL_ROOT"
./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 9001
```

接 systemd 的话，最小 unit 长这样——自动化安装层从 `runtime-package.toml` 渲染出来的 unit 就是这个形状：

```ini
[Unit]
Description=ACPs registry-server API
After=network-online.target

[Service]
Type=simple
User=acps
WorkingDirectory=/opt/acps/registry-server
EnvironmentFile=/opt/acps/registry-server/.env
ExecStart=/bin/sh -c '/opt/acps/registry-server/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 9001'
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`ExecStart` 套一层 `/bin/sh -c` 是为了让 `runtime-package.toml` 里的命令字符串能原样使用（有些服务的 entrypoint 是 shell 脚本）。

健康检查用 `runtime-package.toml` 里声明的地址：

```bash
curl -fsS http://127.0.0.1:9001/health
```

mTLS 端口（registry 9002、mq-auth 9007/9008、demo-partner）要带客户端证书才能探活：

```bash
curl -fsS --cert client.pem --key client.key --cacert trust-bundle.pem \
  https://127.0.0.1:9002/health
```

---

## 5. 部署顺序

服务之间有硬性的启动依赖，顺序错了会卡在互相等待或者 fail-fast。

```text
1. 基础依赖组件（PostgreSQL / Redis / RabbitMQ / Kafka / Keycloak ...）   §6
2. 定信任根，准备 CA 材料（自签根，或向上级根申请中间 CA）                 §7.1
3. ca-server        ← 启动前 CA 材料必须就位
4. registry-server 9001
5. 部署 acps-cli 控制端，签发各服务叶子证书                                §7.2
6. registry-server 9002（mTLS 监听）
7. mq-auth-server   ← 启动前证书必须就位；RabbitMQ 要先配好 HTTP 认证后端
8. discovery-server / monitor-server
9. demo-leader / demo-partner
```

两个共享密钥要在部署前就统一好，写进对应服务的 `.env`：

- `REGISTRY_SERVER_INTERNAL_API_TOKEN`：registry-server 和 ca-server 必须完全一致
- `AIC_CRC_SALT`：**一旦设定不可更改**，改了历史身份码全部失效

---

## 6. 依赖组件：要求与必须的接线

按你的规范安装，但下表右列是 ACPs 特有的、不能省的配置。

| 组件 | 版本要求 | ACPs 侧必须做的接线 |
| --- | --- | --- |
| PostgreSQL | 17（自动化安装层默认） | 为 registry / ca / discovery / monitor 各建独立库与账号；discovery 的库要 `CREATE EXTENSION vector;`（pgvector 扩展需先在服务端安装） |
| Redis | 7+ | monitor 的心跳用到 Redis Functions，别用裁剪过的兼容实现；mq-auth 用它存 ACL |
| RabbitMQ | 4.2（自动化安装层默认） | 开 AMQPS（5671）与 Management API（15672）；**必须**把认证后端配成 mq-auth-server 的 HTTP 接口，否则 Agent 连不上 |
| Kafka | Redpanda v26.1.x 或等价 Kafka | monitor 用；topic 由你预建 |
| Keycloak | 26.6.x | 需要导入 `acps-registry` / `acps-monitor` / `acps-leader` 三个 realm，realm JSON 在 `acps-infra/dev-infra/keycloak/realms/` |
| VictoriaMetrics | v1.111.x | monitor 指标存储 |
| ClickHouse | 25.5.x | monitor 访问日志 |
| OpenSearch | 2.19.x | monitor 系统日志真相源 |
| MinIO | 兼容 S3 即可 | 仅 monitor 开启冷归档时需要 |

只装核心五个服务（registry / ca / discovery / monitor / mq-auth）时，PostgreSQL 是唯一全局必需的；Redis、RabbitMQ、Kafka 等按上表逐服务对照。

discovery 的 pgvector 扩展由一个 `scripts/ensure_vector_extension.py` 在迁移前自动处理，**这个脚本不在薄包里**，手工部署要自己执行一次等价的 SQL：

```sql
-- 连到 discovery 的库，用有建扩展权限的账号
CREATE EXTENSION IF NOT EXISTS vector;
```

---

## 7. 证书与 PKI

ACPs 的 mTLS 平面不是可选装饰：ca-server 和 mq-auth-server 在启动时找不到证书材料会直接退出。

### 7.1 准备 CA 材料

ca-server 在 ACPs 里的角色是**签发 Agent 证书的中间 CA**（issuing CA）：它自己持有一张中间 CA 证书和对应私钥，用它去签所有叶子证书。中间 CA 证书本身必须由某个**根 CA** 签发，这个根就是整套 mTLS 平面的信任锚。

所以第一件事不是敲命令，而是回答：**这个根从哪来？**

| 情形 | 你的处境 | 怎么做 |
| --- | --- | --- |
| **A** | 没有可用的上级 CA（自建环境、PoC、隔离网络） | 自签一个根，再用它签出中间 CA —— §7.1.1 |
| **B** | 已有企业 / 上级根 CA（内部 PKI、行业 CA） | **本地只生成中间 CA 私钥和 CSR，把 CSR 送上级签发**，取回中间证书 —— §7.1.2 |

两种情形下 ca-server 拿到的东西是同构的（一张中间 CA 证书 + 私钥 + 根证书），区别只在中间证书由谁签、根私钥由谁保管。

**不要在已有上级根的环境里用情形 A。** 那样会凭空造出第二个信任根，ACPs 签出来的 Agent 证书在你既有的 PKI 体系里不被承认，反过来企业已签发的证书在 ACPs 这边也验不过——最后只能整个 mTLS 平面重签一次。信任根一旦定下并分发出去（它会作为 `trust-bundle.pem` 铺到每个服务），更换的代价就是全平面重签，所以在部署前就要定清楚。

#### 7.1.1 情形 A：自签根 + 自签中间

`acps-infra` 里有现成脚本，纯 shell + openssl，不依赖 Ansible：

```bash
cd /path/to/acps/acps-infra/release/install-packaging
./scripts/generate_ca_materials.sh --out /path/to/ca-materials
```

它做的正是两步：先自签一张根证书（`CA:TRUE, pathlen:1`），再用这个根签出中间 CA 证书（`CA:TRUE, pathlen:0`）。产出：

| 文件 | 用途 |
| --- | --- |
| `ca.crt` / `ca.key` | 中间 CA 证书与私钥，ca-server 用它签叶子 |
| `root-ca.crt` | 自签根证书，信任锚 |
| `offline/root-ca.key` | **根私钥**，绝不分发；离线保存，或记录后删除 |

有效期默认 3650 天，可以用环境变量 `AUTO_GENERATED_CA_VALID_DAYS` 调整。`--out` 收绝对路径，脚本不依赖当前工作目录，在哪儿调都行。

脚本在收尾前会自己跑一遍 `openssl verify`，确认签出来的中间证书确实能被根验证通过，不通过就直接失败退出。所以只要它成功返回，材料就是可用的，不需要你再补校验。

根私钥留在联网机器上等于整个信任体系没有兜底——脚本已经把它单独放进 `offline/` 并拒绝把它留在可部署集合里，你还需要自己把 `offline/` 挪到离线介质或删掉。

#### 7.1.2 情形 B：中间 CA 由上级根签发

核心原则：**中间 CA 私钥在 ACPs 侧生成，永不外流；上级 CA 只看到 CSR。上级的根私钥也永远不会进入 ACPs。**

第一步，在本地生成中间 CA 私钥和 CSR（不要用情形 A 的脚本，它会连根一起造）：

```bash
openssl req -new -newkey rsa:4096 -sha256 -nodes \
  -keyout ca.key \
  -out ca.csr \
  -subj "/C=CN/ST=Beijing/L=Beijing/O=<你的组织>/OU=Intermediate Certificate Authority/CN=<ACPs 签发 CA 名称>"
chmod 600 ca.key
```

第二步，把 `ca.csr` 交给上级 CA，**明确要求按「中间 CA / subordinate CA」签发**，而不是普通服务器证书。签发出来的证书必须满足：

| 要求 | 值 | 为什么 |
| --- | --- | --- |
| `basicConstraints` | `critical, CA:TRUE`（`pathlen:0` 即可） | 不是 CA 证书就没法签叶子；ca-server 只签叶子，不需要再往下分层 |
| `keyUsage` | `critical, keyCertSign, cRLSign` | 签证书和签 CRL 两项能力都要，ca-server 会发布 CRL |
| 有效期 | 覆盖叶子证书最大有效期 | 叶子上限由 `[ca].max_certificate_validity_days` 控制，默认 1825 天 |
| 上级根的 `pathlen` | 允许这一层 | 根上如果有 `pathlen:0`，就签不出能再签发的中间 CA |

第三步，取回材料并落成三个文件：签发结果存为 `ca.crt`，上级**根证书**存为 `root-ca.crt`，加上第一步的 `ca.key`。

第四步，验一遍再往下走：

```bash
# 中间证书确实由这个根签发
openssl verify -CAfile root-ca.crt ca.crt

# 确实是 CA 证书，且带 keyCertSign / cRLSign
openssl x509 -in ca.crt -noout -ext basicConstraints,keyUsage

# 私钥与证书配对（两条命令输出必须一致）
openssl pkey -in ca.key -pubout -outform DER | openssl dgst -sha256
openssl x509 -in ca.crt -pubkey -noout | openssl pkey -pubin -outform DER | openssl dgst -sha256
```

**上级链路多于两层时**（根 → 上级中间 → ACPs 中间），把上级中间证书**追加到 `ca.crt` 里**，顺序为「ACPs 中间证书在前，上级中间在后」；`root-ca.crt` 里始终只放根。这样 §7.1.3 拼出来的 `ca-chain.pem` 才是完整链。此时上面第一条校验命令要改成把中间层显式传进去：

```bash
openssl verify -CAfile root-ca.crt -untrusted upper-intermediate.crt acps-ca.crt
```

#### 7.1.3 组装 ca-server 需要的四个文件

不管走 A 还是 B，ca-server 的 `[ca]` 段要的是**四个**文件，而上面只准备了三个——`ca-chain.pem` 和 `trust-bundle.pem` 要自己拼（情形 A 的脚本会主动删掉这两个，避免在控制节点留下过期副本）：

```bash
cd /path/to/ca-materials
cat ca.crt root-ca.crt > ca-chain.pem     # 链：中间（含上级中间，若有）→ 根
cp  root-ca.crt         trust-bundle.pem  # 信任锚：只放根
chmod 644 ca.crt root-ca.crt ca-chain.pem trust-bundle.pem
chmod 600 ca.key
```

然后按 ca-server `[ca]` 段的路径放好，默认是相对安装根目录的 `certs/`（这四条路径定义在 `config/default.toml`，`production.toml` 不覆盖它们）：

| 配置项 | 默认路径 | 内容 |
| --- | --- | --- |
| `cert_path` | `certs/ca.crt` | 中间 CA 证书 |
| `key_path` | `certs/ca.key` | 中间 CA 私钥 |
| `chain_path` | `certs/ca-chain.pem` | 中间 → 根的完整链 |
| `trust_bundle_path` | `certs/trust-bundle.pem` | 仅根证书 |

**根私钥不属于这四个文件中的任何一个，绝不能出现在 ca-server 主机上。** 情形 B 下它本来就在上级 CA 那里，这条自动满足。

### 7.2 签发叶子证书

叶子证书由 acps-cli 加薄包里自带的 `scripts/bootstrap_runtime.py` 签发。先按 §4 把 acps-cli 部署成一个控制端（它是 `cli-tool`，不需要迁移和常驻进程），然后：

```bash
cd /opt/acps/acps-cli
./venv/bin/python scripts/bootstrap_runtime.py <profile> \
  --config ./acps-cli.toml \
  --cli-bin ./venv/bin/acps-cli \
  --output-dir /path/to/certs-out \
  --admin-username <registry 管理员> \
  --admin-password <口令>
```

可用的 `<profile>`：

| profile | 签给谁 |
| --- | --- |
| `all` | 一次出 `registry-9002` 和 `mq-auth-server` 两套 |
| `registry-9002` | registry-server 的 mTLS 监听 |
| `mq-auth-server` | mq-auth-server 的两个监听 |
| `rabbitmq` | RabbitMQ 的 AMQPS 服务端与客户端 |
| `redis` | Redis TLS |
| `demo-leader` / `demo-partner` | demo 侧的 ATR 身份证书 |

签发要求 registry-server 已经起来且能用管理员账号登录，所以顺序上排在 §5 的第 5 步。

**注意 trust bundle 的语义**：服务端配置里的 `*_CA_CERT_FILE` 是用来**校验客户端证书链**的信任锚，通常应该指向包含 root 的 bundle，而不是只填中间 CA 的 `ca.crt`，否则握手阶段可能直接拒绝客户端。

---

## 8. 逐服务清单

统一约定：`$INSTALL_ROOT` 是该服务的安装根目录，所有命令都在这个目录下执行，venv 在 `$INSTALL_ROOT/venv`。

### 8.1 registry-server

| 项 | 值 |
| --- | --- |
| 薄包 | `registry-server-wheel-{version}.tar.gz` |
| 内部 wheel | `acps-sdk` |
| 外部依赖 | PostgreSQL；ca-server；可选 Keycloak |
| 迁移 | `./venv/bin/python -m alembic upgrade head` |
| 组件 1 | `uvicorn app.main:app --host 0.0.0.0 --port 9001` → `http://127.0.0.1:9001/health` |
| 组件 2 | `python -m app.main_mtls` → `https://127.0.0.1:9002/health` |

**两个组件是两个独立进程**，要分别托管——用 systemd 的话就是两个 service unit，换别的进程管理器同理，一个进程一个托管条目。9002 那个必须用 `python -m app.main_mtls`——它在模块内部构建 `CERT_REQUIRED` 的 TLS 上下文；换成裸 `uvicorn app.main_mtls:app` 只会起明文 HTTP，端口通了但根本没有 mTLS。

`.env` 必填：

```bash
APP_ENV=production
DATABASE_URL=postgresql+asyncpg://registry:<pw>@<host>:5432/agent_registry
SECRET_KEY=<openssl rand -hex 32>
SM4_ENCRYPTION_KEY=<openssl rand -hex 16>    # 必须正好 32 个十六进制字符
AIC_CRC_SALT=0x<至少两字节十六进制>            # 一旦设定不可更改
REGISTRY_SERVER_INTERNAL_API_TOKEN=<与 ca-server 一致>
CA_SERVER_BASE_URL=http://<ca-host>:9003
```

`SM4_ENCRYPTION_KEY` 是唯一有长度校验的（不多不少 32 个十六进制字符，对应 SM4 的 128 位密钥），写错直接启动失败。`SECRET_KEY` 代码不校验长度，`openssl rand -hex 32` 给的 256 位足够。

`CA_SERVER_BASE_URL` 严格说是可选覆写，不填会回落到 `config/*.toml` 里 `[ca_server].base_url` 的默认值 `http://localhost:9003`；ca-server 不在同一台机器上就必须显式写，否则要到调用 CA 时才报错。

`DATABASE_URL` 用 `postgresql+asyncpg://`（ca-server 那边用 `postgresql://`，两者别混）。alembic 迁移时框架会自己把它换成同步驱动，不用你手工改。口令里含 URL 保留字符要百分号编码，最常见的是 `#` 写成 `%23`。

启用 9002 时补上 `REGISTRY_SERVER_ENABLE_MTLS_LISTENER=true` 和三个 `REGISTRY_SERVER_MTLS_*` 证书路径，并确认 `config/production.toml` 里 `[server].enable_mtls_listener = true`。反过来，只验证 9001 时不必先准备 mTLS 证书——`enable_mtls_listener` 是给 9002 那个独立进程用的，开着也不影响 9001 起来。

### 8.2 ca-server

| 项 | 值 |
| --- | --- |
| 薄包 | `ca-server-wheel-{version}.tar.gz`（wheel 名是 `agent_ca_server`） |
| 内部 wheel | 无 |
| 外部依赖 | PostgreSQL；registry-server |
| 迁移 | `./venv/bin/python -m alembic upgrade head` |
| 组件 | `uvicorn app.main:app --host 0.0.0.0 --port 9003` → `http://127.0.0.1:9003/health` |

**启动前 CA 材料必须就位。** 服务在启动过程中就会加载 `[ca]` 段指向的 `cert_path` / `key_path` / `chain_path` / `trust_bundle_path`，缺文件直接失败。这四条路径定义在 `config/default.toml`（`production.toml` 只覆盖三个 URL，路径靠深度合并继承）。四个文件怎么准备见 §7.1，注意后两个要自己拼（§7.1.3）。

`.env` 必填：

```bash
APP_ENV=production
DATABASE_URL=postgresql://ca:<pw>@<host>:5432/agent_ca
REGISTRY_SERVER_INTERNAL_API_TOKEN=<与 registry-server 一致>
CA_SERVER_ADMIN_API_TOKEN=<强随机串>

# 证书发现地址：取值取决于本次部署对外可达的主机名，所以没有默认值
ACME_DIRECTORY_URL=https://<ca-host>:9003/acps-atr-v2/acme
OCSP_RESPONDER_URL=https://<ca-host>:9003/acps-atr-v2/ocsp
CRL_DISTRIBUTION_POINT_URL=https://<ca-host>:9003/acps-atr-v2/crl/current
```

后三个必须填 ACME 客户端和证书使用者**实际能访问到的**主机名，`localhost` / `127.0.0.1` 通不过校验。漏填时 `import`、`alembic upgrade`、`uvicorn` 会一起 fail-fast，报错会点名该注入哪个变量：

```text
ca.ocsp_responder_url must be explicitly configured to an externally reachable hostname in production (inject OCSP_RESPONDER_URL)
```

这是有意为之：OCSP 和 CRL 两个地址会被写进签发出去的每一张证书，事后变更要全平面重签，所以宁可起不来，也不要签出一批指向错误地址的证书。部署前就把这三个地址定好。

### 8.3 discovery-server

| 项 | 值 |
| --- | --- |
| 薄包 | `discovery-server-wheel-{version}.tar.gz`，**架构相关** |
| 内部 wheel | `acps-sdk` |
| 外部依赖 | PostgreSQL + pgvector；外部 Embedding / LLM API；registry-server |
| 锁文件 | `requirements-runtime-{cpu\|gpu}-{arch}.lock`，不是 `requirements-runtime.lock` |
| 迁移 | `./venv/bin/python -m alembic upgrade head`（**前置**：手工建好 vector 扩展） |
| 组件 | `uvicorn app.main:app --host 0.0.0.0 --port 9005` → `http://127.0.0.1:9005/health` |

§4.4 装依赖时文件名要换成实际的变体锁文件：

```bash
./venv/bin/pip install --require-hashes -r requirements-runtime-cpu-amd64.lock
```

`.env` 必填（CPU 模式）：

```bash
APP_ENV=production
DISCOVERY_MODE=cpu                # 必须与薄包的 variant 一致
DATABASE_URL=postgresql+asyncpg://discovery:<pw>@<host>:5432/agent_discovery
EMBEDDING_API_KEY=<key>
EMBEDDING_BASE_URL=<endpoint>
EMBEDDING_MODEL_NAME=<model>
DISCOVERY_LLM_API_KEY=<key>
DISCOVERY_LLM_BASE_URL=<endpoint>
DISCOVERY_LLM_MODEL_NAME=<model>
```

GPU 模式（`DISCOVERY_MODE=gpu`）改用本地 BGE-M3 推理栈，不需要外部 Embedding API，但目标机要有 CUDA 运行时，且薄包必须是 GPU 变体。

### 8.4 monitor-server

| 项 | 值 |
| --- | --- |
| 薄包 | `monitor-server-wheel-{version}.tar.gz` |
| 内部 wheel | `acps-sdk` |
| 外部依赖 | PostgreSQL、Redis、Kafka、VictoriaMetrics、ClickHouse、OpenSearch；可选 MinIO、Keycloak |
| 迁移 | `./venv/bin/python -m alembic upgrade head` |
| 组件 | `uvicorn app.main:app --host 0.0.0.0 --port 9009` → `http://127.0.0.1:9009/health` |

基础设施占用最大的一个。`/health` 会同时探 PostgreSQL、Redis、VictoriaMetrics、ClickHouse，任一不通就返回 503——先把这四个接通再看服务本身。各子系统可以在 `config/*.toml` 里按 `*_enabled` 开关裁剪。

`.env` 必填：

```bash
APP_ENV=production
DATABASE_URL=postgresql+asyncpg://monitor:<pw>@<host>:5432/agent_monitor
REDIS_URL=redis://<host>:6379/0
VM_QUERY_URL=http://<host>:8428
VM_REMOTE_WRITE_URL=http://<host>:8428/api/v1/write
CLICKHOUSE_HOST=<host>
CLICKHOUSE_PORT=8123
CLICKHOUSE_DATABASE=amp
OPENSEARCH_HOSTS=http://<host>:9200
```

### 8.5 mq-auth-server

| 项 | 值 |
| --- | --- |
| 薄包 | `mq-auth-server-wheel-{version}.tar.gz` |
| 内部 wheel | 无 |
| 外部依赖 | Redis；RabbitMQ |
| 迁移 | **无**，这个服务不用 alembic |
| 组件 | `mq-auth-server`（console script，在 `venv/bin/` 下）→ 端口 9007、9008 |

一个进程内部 fork 出两个 uvicorn 监听（9007 Group API、9008 Auth API），**两个都强制 mTLS**，证书缺失直接启动失败。

健康检查也必须走 mTLS：光把 `http` 换成 `https` 不够，还得带上服务端认可的客户端证书，所以裸 `curl` 探不通。薄包里带了现成的探针：

```bash
cd "$INSTALL_ROOT"
HEALTHCHECK_TLS_CERT_FILE=<客户端证书> \
HEALTHCHECK_TLS_KEY_FILE=<客户端私钥> \
HEALTHCHECK_TLS_CA_CERT_FILE=<含根的信任 bundle> \
./venv/bin/python -m app.core.health_probe --url https://127.0.0.1:9007/health
```

`APP_ENV` 只要不是 `development`，前两个变量就是必填的，缺了探针会直接报 `HEALTHCHECK_TLS_CERT_FILE and HEALTHCHECK_TLS_KEY_FILE are required`。客户端证书得是这个服务愿意接受的对端身份（§7.2），不能拿服务端证书顶。探针要求 TLS 1.3，退出码非 0 即为不健康。

`.env` 必填：

```bash
APP_ENV=production
REDIS_URL=redis://<host>:6379/0
RABBITMQ_MGMT_URL=http://<host>:15672
RABBITMQ_MGMT_PASS=<pw>
TLS_CERT_FILE=<path>/server.pem
TLS_KEY_FILE=<path>/server.key
TLS_CA_CERT_FILE=<path>/trust-bundle.pem
```

薄包里的 `acs/` 目录是 ACS 描述符，别删。

### 8.6 demo-leader

| 项 | 值 |
| --- | --- |
| 薄包 | `demo-leader-wheel-{version}.tar.gz` |
| 内部 wheel | `acps-sdk`、`acps-cli` |
| 外部依赖 | RabbitMQ + mq-auth-server；discovery-server；Keycloak（realm `acps-leader`）；LLM API |
| 迁移 | 无 |
| 组件 1 | `scripts/start-leader-api.sh` → `http://127.0.0.1:9031/api/v1/health` |
| 组件 2 | `scripts/start-web-ui.sh` → 9030（静态 Web） |

启动入口是薄包自带的 shell 脚本（已经带执行位），内部分别拉起 `python -m leader.main` 和 `python -m web_app.webserver`。脚本默认以自身所在目录的上级为运行根，业务数据要放在别处时用 `LEADER_RUNTIME_ROOT` / `LEADER_SCENARIO_ROOT` / `LEADER_CONFIG_FILE` 覆盖。

主配置是 `leader/config.toml`（端口、OIDC、RabbitMQ、discovery 地址、mTLS 路径），`.env` 只放 LLM 密钥：

```bash
APP_ENV=production
LEADER_LLM_DEFAULT_API_KEY=<key>
LEADER_LLM_DEFAULT_BASE_URL=<endpoint>
LEADER_LLM_DEFAULT_MODEL=<model>
# FAST / PRO 三档按需配置
```

出站访问 Partner 和 Discovery 需要 `leader/atr/` 下的 mTLS 客户端证书——薄包在打包时**故意剥掉了**所有 `*.pem` / `*.key`，要用 §7.2 的 `demo-leader` profile 重新签发并放回去。

### 8.7 demo-partner

| 项 | 值 |
| --- | --- |
| 薄包 | `demo-partner-wheel-{version}.tar.gz` |
| 内部 wheel | `acps-sdk`、`acps-cli` |
| 外部依赖 | RabbitMQ（vhost `acps`）；LLM API |
| 迁移 | 无 |
| 组件 | `python -m partners.main` → 端口 9021–9025 |

一个进程按 `partners/online/` 下的目录数拉起多个 uvicorn，每个 Agent 一个端口，各自 HTTPS + 客户端证书校验。每个 Agent 目录下的 `config.toml` 定义端口和 mTLS，`atr/acs.json` 是身份描述。证书同样被剥掉了，用 `demo-partner` profile 签发。

`.env` 只放 LLM 密钥（`PARTNER_LLM_*`）。

### 8.8 acps-cli（控制端）

| 项 | 值 |
| --- | --- |
| 薄包 | `acps-cli-wheel-{version}.tar.gz` |
| 内部 wheel | `acps-sdk` |
| 类型 | `cli-tool`，不是常驻服务，没有端口和健康检查 |
| 入口 | `venv/bin/acps-cli` |

按 §4.1–4.6 装好即可。它不是常驻服务，所以既不需要数据库迁移，也不需要为它配置任何进程托管（systemd unit 之类）——用到时直接敲命令。它承担两件事：签发证书（§7.2）和日常运维操作。配置在 `acps-cli.toml`，命令详见 [CLI 参考](../references/cli-reference.md)。

---

## 9. 出了问题先看这里

| 现象 | 可以怎么做 |
| --- | --- |
| `python3.14: command not found` | Debian/Ubuntu 只在 login shell 里把 `~/.local/bin` 加进 PATH，非交互 SSH 和 systemd 都取不到；用 §4.1.1 里 `$PYBIN` 的绝对路径 |
| 解包 / 建 venv / `pip install` 报 `Permission denied` | `sudo mkdir` 建出来的目录属主是 root，漏了 `chown`（§4.2） |
| 启动瞬间报 pydantic `Field required` | 两种可能：进程 CWD 不是安装根目录导致读不到 `.env`（§4.7），或者在 §4.6 写 `.env` 之前就跑了 `import app.main`（§4.5） |
| `ModuleNotFoundError: acps_sdk` | 漏了 §3 的内部 wheel；锁文件里没有它是设计如此 |
| `pip install` 报 `hashes are required` | 内部 wheel 和锁文件混在一条命令里了；分开装并加 `--no-deps`（§4.5） |
| 装完 A 服务再装 B 服务，A 挂了 | 两个服务共用了 venv；registry/ca/discovery/monitor 顶层包名都是 `app`，必须一服务一 venv |
| 依赖开始现场编译源码 | 目标平台没有预编译 wheel；换匹配的架构，或改用装配好 wheelhouse 的应用发布包 |
| discovery 迁移报 vector 类型不存在 | 库里没建 pgvector 扩展（§6） |
| ca-server / mq-auth-server 启动即退出 | 证书材料缺失，这两个服务是 fail-fast 设计（§7）；ca-server 要的是**四个**文件，`ca-chain.pem` / `trust-bundle.pem` 需自己拼（§7.1.3） |
| ca-server 报 `must be explicitly configured to an externally reachable hostname` | `.env` 里漏了证书发现地址，报错括号里会点名该注入哪个变量（§8.2）；填对外可达的主机名，`localhost` 不行 |
| ACPs 签出的证书在企业既有 PKI 里不被认可 | 信任根选错了：环境里已有上级根却用了自签根（§7.1）；只能换成上级签发的中间 CA 并全平面重签 |
| `openssl verify -CAfile root-ca.crt ca.crt` 失败 | 上级链路多于两层，缺中间层；用 `-untrusted` 传入上级中间，并把它追加进 `ca.crt`（§7.1.2） |
| 9002 端口通了但没有 mTLS | 启动命令写成了裸 uvicorn；必须是 `python -m app.main_mtls`（§8.1） |
| mTLS 握手被服务端拒绝 | 服务端的 `*_CA_CERT_FILE` 只填了中间 CA；换成含 root 的 bundle（§7.2） |
| Agent 连不上 RabbitMQ | RabbitMQ 没配 mq-auth-server 作 HTTP 认证后端（§6） |
| monitor `/health` 返回 503 | 逐项查 PostgreSQL / Redis / VictoriaMetrics / ClickHouse |
| 数据库口令里带 `#`，连接不上或口令被截断 | `#` 在 URL 里是 fragment 起始，DSN 中要写成 `%23`。另外 `.env` 里 `#` 前面**有空格**才会被当行内注释，紧贴口令时不会——两个是不同的坑 |
| 改用专用账号运行后服务起不来 | venv 记的是创建它的解释器的绝对路径；解释器别装在某个用户 HOME 下，装到 `/opt`（§4.1.1） |

---

## 10. 装完之后的日常动作

升级、证书续签、trust bundle 刷新，本质上都是把 §4 到 §7 里的某几步再做一遍。手工做法如下，[day2-ops](./install-package-day2-ops.md) 里的 playbook 就是这几段的自动化版本，出问题时可以对照着手工介入：

- **升级**：新版本解到 `releases/{version}/` 下另建 venv，`current` 软链切换后重启，失败就把软链切回去。自动化安装层用的就是这套 `releases/` + `current` 布局，所以 §4.2 建议沿用它的目录约定。
- **证书续签**：重跑 §7.2 对应 profile，替换文件后重启相关服务。
- **配置变更**：改 `config/*.toml` 或 `.env` 后重启；两者都不支持热加载。

机器变多、需要幂等重放和多机编排时，可以在不改变部署语义的前提下换成自动化安装层——它装出来的目录布局、systemd unit、迁移命令和证书都和本文一致，只是由 playbook 代劳。要走那条路，从 [应用发布包](./app-release-package-build.md) 开始装配即可，薄包同样是它的上游输入。
