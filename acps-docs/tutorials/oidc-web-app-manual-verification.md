[首页](../README.md)

# OIDC Web 应用手工验证教程

这篇教程说明如何手工验证真人用户的 OIDC 登录能力。它以 `demo-leader + Keycloak` 为示例，但验证思路本身是通用的；如果你在 `registry-server`、`monitor-server` 或其他接入 OIDC 的项目中联调，只需要替换本文中的 URL、realm、client 和测试用户即可。

如果你要验证的是 `acps-cli` 这种纯命令行工具的 Device Authorization Grant 登录流程，请阅读 [acps-cli OIDC Device 登录教程](./oidc-acps-cli-device-login.md)。

本文关注三件事：

1. 以管理员身份检查 Keycloak 中与登录有关的关键配置是否落地。
2. 以管理员身份检查测试用户和角色映射是否正确。
3. 以普通用户身份走一遍浏览器重定向登录、回跳、退出再回跳的完整流程。

本文针对当前仓库内已经提交的本地开发默认值编写，示例约定如下：

- 应用 Web UI：`http://localhost:9030`
- 应用 API：`http://localhost:9031`
- Keycloak：`http://localhost:9080`
- Realm：`acps-leader`
- 浏览器登录 client：`leader-web`
- API 资源 client：`leader-api`

> **若你走的是安装包装配后的部署**（见 [用安装包部署 ACPs](./install-package-ansible-deploy.md)）：浏览器入口为业务节点 **`http://<host>:9030/`**（`demo_leader_web`），Web 自带 `/api/v1/` 同源反代，`backendBase=''`；**没有**独立的 `demo-nginx`。API 仍为 **9031**，与本地开发一致（image / host 相同）。

示例中的本地开发 OIDC 配置已经提交在以下文件中：

- [demo-leader/leader/config.toml](../../demo-leader/leader/config.toml)
- [demo-leader/web_app/runtime-config.js](../../demo-leader/web_app/runtime-config.js)

本教程对 Keycloak 管理控制台中的配置项的描述，仅针对 `26.6.3` 版本，其它版本可能会有差异。

---

## 1. OIDC 解决什么问题

如果一个系统要支持真人用户登录，最直接但也最麻烦的做法，是每个项目自己处理账号、密码、登录状态和权限。这样会带来几个常见问题：

1. 每个系统都要自己保存或处理密码，安全责任分散。
2. Web 登录、退出、会话超时、权限映射容易各做各的，行为不一致。
3. 多个系统接入后，用户身份和权限信息很难统一。

OIDC 的作用，就是把“用户是谁、怎么登录、登录后怎么拿到标准身份信息”这件事交给统一的身份提供方处理。对应用来说，重点变成：

1. 把用户重定向到身份提供方登录。
2. 登录成功后接收回跳。
3. 使用标准协议换取 token。
4. 根据 token 中的身份与角色信息决定应用内权限。

在本文的示例里，`Keycloak` 就是身份提供方，应用本身不负责保存用户密码，而是通过 OIDC 与 Keycloak 协作完成登录。

---

## 2. 概念速览

如果你对 OIDC 和 Keycloak 还不熟，先记住下面几个词就够用了：

- `OIDC`：OpenID Connect，建立在 OAuth 2.0 之上的登录协议，解决“用户登录后如何让应用知道他是谁”。
- `Keycloak`：身份提供方（Identity Provider，简称 IdP），负责登录页、用户目录、client 配置、token 签发等。
- `realm`：Keycloak 中的一套独立身份空间。它像一个隔离边界，里面有各自的用户、client、角色和登录配置。
- `client`：在 Keycloak 中登记过的“应用”或“资源服务”身份，不是用户账号。浏览器前端、后端 API、测试程序都可以各自对应一个 client。
- `user`：真人用户账号，例如 `leader-user`。
- `role`：授权标签，用来表达用户在某个系统中的权限，例如 `user`、`operator`、`admin`。
- `token`：登录成功后由 Keycloak 签发给应用的标准凭证。应用通常根据它判断登录状态和权限。

最容易混淆的是 `client` 和 `user`：

- `client` 代表“哪个应用在接入 Keycloak”。
- `user` 代表“哪个真人正在登录”。

例如本文中的 `leader-web` 是浏览器登录用的 client，`leader-user` 才是实际登录的用户。

---

## 3. 前置条件

开始前请确认：

1. 你已经在本机准备好 `demo-leader/.env`，并填入可用的 LLM 敏感信息。
2. 同级目录存在 `acps-infra/`、`acps-sdk/`、`acps-cli/`。
3. 本机 Docker 可正常运行。

如果你验证的是其他项目，请把上面的 `demo-leader` 替换成对应项目，并确认该项目的本地配置已经启用 OIDC。

---

## 4. 启动本地环境

在 `demo-leader` 目录执行：

```bash
cd /Users/huxiaofeng/Projects/acps/demo-leader
just dev start
```

说明：

- 由于示例配置默认启用了 OIDC，`just dev start` 会自动拉起 `acps-infra/dev-infra` 中的 Keycloak，并启动本地应用进程：
  - Web UI：`http://localhost:9030`
  - Leader API：`http://localhost:9031`

如果你想确认后台状态，可以执行：

```bash
just dev status
just infra status keycloak
```

---

## 5. 以管理员身份检查 Keycloak

### 5.1 登录管理控制台

浏览器打开：

```text
http://localhost:9080
```

管理员账号：

- 用户名：`admin`
- 密码：`devpass`

登录后切换到 realm：

```text
acps-leader
```

如果你验证的是其他项目，请切换到它对应的 realm。

### 5.2 检查 realm 级签名算法

进入：

```text
Realm settings -> Tokens
```

确认：

- `Default signature algorithm = EdDSA`

再进入：

```text
Realm settings -> Keys
```

确认：

- 能看到一条用于签名的 key
- 该 key 处于启用且生效状态
- 其算法为 `Ed25519`

这说明当前 realm 已按开发约定启用 `EdDSA / Ed25519` 签名。

### 5.3 检查浏览器登录 client

进入：

```text
Clients -> leader-web
```

在 `Settings` 页面里，重点检查两个区域。

第一处是：

```text
Settings -> Capability config
```

确认：

1. `leader-web` 是浏览器登录 client，不是用户账户。
2. `Client authentication = Off`
3. `Standard flow = On`
4. `Direct access grants = Off`

第二处是：

```text
Settings -> Access settings
```

确认 `Valid redirect URIs` 与 `Web origins` 覆盖了实际开发入口。以本文示例为例，至少应包含：

- `http://localhost:9030/*`
- `http://localhost:9030`

如果你的本地环境使用 `127.0.0.1`、其他端口，或其他项目的前端地址，这里也要与实际入口保持一致。

### 5.4 检查 API client 与角色承载关系

进入：

```text
Clients
```

确认示例中的 `leader-api` client 存在。它的作用不是承载浏览器登录，而是作为 API 侧角色与权限的归属对象。

---

## 6. 以管理员身份检查用户与角色

进入：

```text
Users
```

确认以下示例用户存在：

| 用户名 | 密码 | `leader-api` 角色 | 用途 |
| --- | --- | --- | --- |
| `leader-user` | `demo123` | `user` | 普通真人用户 |
| `leader-operator` | `demo123` | `operator` | 运维/操作员 |
| `leader-admin` | `demo123` | `admin` | 管理员 |

可以逐个点开用户，在：

```text
User details -> Role mappings
```

中确认：

- `leader-user` 具备 `leader-api:user`
- `leader-operator` 具备 `leader-api:operator`
- `leader-admin` 具备 `leader-api:admin`

如果你验证的是其他项目，请把示例用户名和角色名替换成该项目的约定值。

---

## 7. 以普通用户身份验证浏览器登录回跳

建议使用浏览器无痕窗口，避免管理员会话与普通用户会话互相干扰。

### 7.1 触发登录

打开：

```text
http://localhost:9030
```

由于示例配置默认启用了 OIDC，页面初始化后会自动触发登录流程，浏览器会跳转到 Keycloak 登录页。

### 7.2 使用普通用户登录

在 Keycloak 登录页输入：

- 用户名：`leader-user`
- 密码：`demo123`

登录成功后，浏览器会被重定向回应用 Web UI。

### 7.3 观察回跳过程

回跳时，地址栏通常会短暂出现：

```text
http://localhost:9030/?code=...&state=...&session_state=...
```

随后前端会完成授权码换 token，并把 URL 清理回干净的页面地址：

```text
http://localhost:9030/
```

页面上应看到：

1. `退出` 按钮出现。
2. 当前登录用户标识出现，通常会显示 `leader-user`。

这说明以下链路已经跑通：

```text
应用 Web -> Keycloak 登录页 -> 用户认证 -> 回跳应用 -> 前端完成 code exchange
```

---

## 8. 验证退出再回跳

在已登录页面点击：

```text
退出
```

预期现象：

1. 浏览器跳转到 Keycloak 的 logout endpoint。
2. Keycloak 再把浏览器重定向回应用首页。
3. 如果应用配置为未登录即自动发起 OIDC 登录，页面会再次跳到登录页。

这说明 logout 回跳链路也正常。

---

## 9. 用浏览器开发者工具观察协议细节

如果你想进一步确认浏览器确实按 OIDC 标准流执行，可以打开 DevTools 的 `Network` 面板，再重新做一遍登录。

通常可以看到这些关键请求：

1. 读取 discovery：

```text
GET /realms/acps-leader/.well-known/openid-configuration
```

2. 跳转授权端点，通常形如：

```text
GET /realms/acps-leader/protocol/openid-connect/auth?...client_id=leader-web...
```

3. 回跳后调用 token endpoint，通常形如：

```text
POST /realms/acps-leader/protocol/openid-connect/token
```

4. 退出时调用 end session 或 logout endpoint。

如果这些请求都存在，而且页面行为与前述现象一致，通常就可以认定这条 OIDC 登录链路已经正常。

---

## 10. 结论

当你完成本文中的检查后，就等于从三个层面验证了本地真人用户 OIDC 能力：

1. Keycloak realm 与 client 的基础配置正确。
2. 测试用户和角色映射正确。
3. 浏览器重定向登录、code exchange、logout redirect 整体流程正确。

如果其中某一步失败，建议优先从以下几类问题排查：

- 应用侧配置的 `authority`、`client_id`、`redirect_uri` 是否与 Keycloak 一致。
- Keycloak 中 `Valid redirect URIs` / `Web origins` 是否覆盖当前实际访问地址。
- 当前浏览器是否混入管理员会话、旧 token 或旧 cookie。
- 测试用户是否存在，以及是否具备正确的 API 角色。

这套检查方法并不局限于 `demo-leader`。只要项目采用“Web 前端重定向到 Keycloak 登录，再回跳到本地应用”的 OIDC 模式，都可以直接套用本文的思路。
