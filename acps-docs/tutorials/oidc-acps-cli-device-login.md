[首页](../README.md)

# acps-cli OIDC Device 登录教程

这篇教程说明如何使用 `acps-cli` 在纯命令行场景中完成真人用户 OIDC 登录。它面向常见的 SSH 远程主机场景：CLI 运行在远程终端里，浏览器可以运行在你的本机、跳板机或任意可访问 Keycloak 的设备上。

`acps-cli` 的正式 OIDC 人类登录路径是 OAuth 2.0 Device Authorization Grant。CLI 不启动浏览器，不监听本地 callback 端口，也不会要求你在终端里输入 Keycloak 用户名和密码。它只会打印一个浏览器 URL 和一次性用户码；你在浏览器打开 URL 后输入或确认用户码，再在 Keycloak 页面完成登录授权。

本文使用本地开发默认值作为示例：

| 服务域 | Keycloak realm | CLI client | API audience | 示例用户 |
| --- | --- | --- | --- | --- |
| registry 普通用户 | `acps-registry` | `registry-cli` | `registry-api` | `registry-client / demo123` |
| registry 管理员 | `acps-registry` | `registry-cli` | `registry-api` | `registry-admin / demo123` |
| monitor 查询用户 | `acps-monitor` | `monitor-cli` | `monitor-api` | `monitor-viewer / demo123` |

对应地址：

- Keycloak：`http://localhost:9080`
- registry-server：`http://localhost:9001`
- monitor-server：`http://localhost:9009`

如果你验证的是非本地环境，请替换 issuer、server URL 和测试账号。

---

## 1. CLI 登录和 Web 登录有什么不同

Web 应用通常使用 Authorization Code Flow：浏览器跳到 Keycloak，用户登录后，Keycloak 再把浏览器重定向回应用的 callback URL。

`acps-cli` 不适合这种模式。CLI 经常运行在远程 SSH 主机上，不能假设远程主机能启动浏览器，也不能假设 Keycloak 可以回跳到远程主机上的临时端口。

所以 `acps-cli` 使用 Device Authorization Grant：

1. CLI 向 Keycloak 申请一个 device code。
2. CLI 在终端打印浏览器 URL 和 user code。
3. 操作者在浏览器中打开 URL，并完成 Keycloak 登录和授权。
4. CLI 在终端里轮询 token endpoint。
5. 授权完成后，CLI 保存本服务域自己的本地 session/token 文件。

这个流程有两个重要特点：

- Keycloak 用户名和密码只在浏览器登录页输入，不在 CLI 终端输入。
- registry 与 monitor 分属不同 realm 和账号体系，不共享登录态，也不复用同一个 token 文件。

---

## 2. 前置条件

开始前请确认：

1. 已经启动 `acps-infra/dev-infra` 中的 Keycloak。
2. Keycloak 已完成 dev bootstrap，并创建了正式 CLI client：
   - `acps-registry` realm 下的 `registry-cli`
   - `acps-monitor` realm 下的 `monitor-cli`
3. `registry-server` 和 `monitor-server` 已按 OIDC 模式启动。
4. 你有一个可访问 Keycloak URL 的浏览器。
5. 你知道本次测试要使用的 token 文件位置，避免读到旧 session。

本地开发时，可以先启动 Keycloak：

```bash
cd /Users/huxiaofeng/Projects/acps/acps-infra/dev-infra
./dev-infra.sh up keycloak
./dev-infra.sh wait keycloak
```

然后分别按 OIDC 模式启动服务端。以下命令使用本地开发默认 realm：

```bash
cd /Users/huxiaofeng/Projects/acps/registry-server
REGISTRY_OIDC_ENABLED=true \
REGISTRY_OIDC_ISSUER=http://localhost:9080/realms/acps-registry \
REGISTRY_OIDC_ALLOWED_AZP=registry-cli \
REGISTRY_OIDC_REQUIRE_HTTPS=false \
just dev start
```

```bash
cd /Users/huxiaofeng/Projects/acps/monitor-server
MONITOR_OIDC_ENABLED=true \
MONITOR_OIDC_ISSUER=http://localhost:9080/realms/acps-monitor \
MONITOR_OIDC_ALLOWED_AZP=monitor-cli \
MONITOR_OIDC_REQUIRE_HTTPS=false \
just dev start
```

如果你使用 `just test e2e -- tests/e2e/test_oidc_keycloak_flow.py` 或其它临时测试入口，它们可能会自动拉起临时服务实例。做手工 CLI 验证时，建议明确确认当前 CLI 指向的是你准备验证的服务实例。

---

## 3. 配置 acps-cli

`acps-cli` 使用统一配置文件和环境变量解析 OIDC 设置。最小 TOML 示例：

```toml
[registry]
base_url = "http://localhost:9001"

[registry.auth]
mode = "oidc"
issuer = "http://localhost:9080/realms/acps-registry"
client_id = "registry-cli"
require_https = false

[monitor]
base_url = "http://localhost:9009"

[monitor.auth]
mode = "oidc"
issuer = "http://localhost:9080/realms/acps-monitor"
client_id = "monitor-cli"
require_https = false

[auth]
user_token_file = "./.acps-cli/tokens/registry-user.json"
admin_token_file = "./.acps-cli/tokens/registry-admin.json"
monitor_token_file = "./.acps-cli/tokens/monitor-user.json"
```

也可以用环境变量临时覆盖，适合一次性验证：

```bash
export ACPS_CLI_REGISTRY_AUTH_MODE=oidc
export ACPS_CLI_REGISTRY_OIDC_ISSUER=http://localhost:9080/realms/acps-registry
export ACPS_CLI_REGISTRY_OIDC_CLIENT_ID=registry-cli
export ACPS_CLI_REGISTRY_OIDC_REQUIRE_HTTPS=false
export AUTH_USER_TOKEN_FILE=/tmp/acps-registry-user.json
export AUTH_ADMIN_TOKEN_FILE=/tmp/acps-registry-admin.json

export ACPS_CLI_MONITOR_AUTH_MODE=oidc
export ACPS_CLI_MONITOR_OIDC_ISSUER=http://localhost:9080/realms/acps-monitor
export ACPS_CLI_MONITOR_OIDC_CLIENT_ID=monitor-cli
export ACPS_CLI_MONITOR_OIDC_REQUIRE_HTTPS=false
export AUTH_MONITOR_TOKEN_FILE=/tmp/acps-monitor-user.json
```

本地 HTTP issuer 只应在开发环境中配合 `require_https = false` 使用。正式环境应使用 HTTPS issuer，并保留 HTTPS 校验。

---

## 4. registry 普通用户登录

进入 `acps-cli` 仓库：

```bash
cd /Users/huxiaofeng/Projects/acps/acps-cli
```

执行登录：

```bash
uv run acps-cli auth login
```

终端会输出类似内容：

```text
Open this URL in a browser: http://localhost:9080/realms/acps-registry/device?user_code=ABCD-EFGH
Enter this code if prompted: ABCD-EFGH
```

在浏览器中打开 URL。如果浏览器没有自动带入 code，就手工输入终端里的 user code。随后使用测试账号登录：

- 用户名：`registry-client`
- 密码：`demo123`

授权成功后，CLI 会返回登录摘要。可以继续验证：

```bash
uv run acps-cli auth status --json
uv run acps-cli auth whoami --json
uv run acps-cli agent list --json
uv run acps-cli auth refresh --json
```

预期结果：

1. `auth status` 显示 `authenticated = true`。
2. `auth whoami` 能从 registry-server 的 `/account/me` 返回用户摘要。
3. `agent list` 成功即可，列表为空也正常。
4. `auth refresh` 成功刷新 session。
5. 输出中不会出现 raw access token、refresh token 或 id token。

---

## 5. registry 管理员登录

registry 管理员使用独立 token 文件，但仍位于 `acps-registry` realm，client 仍是 `registry-cli`。

执行：

```bash
uv run acps-cli admin auth login
```

浏览器打开 CLI 打印的 URL，使用管理员测试账号登录：

- 用户名：`registry-admin`
- 密码：`demo123`

授权成功后验证：

```bash
uv run acps-cli admin auth status --json
uv run acps-cli admin auth whoami --json
uv run acps-cli admin registry review list --json
uv run acps-cli admin auth refresh --json
```

预期结果：

1. `admin auth status` 显示 `account_kind = "admin"`。
2. `admin auth whoami` 返回 registry 管理员身份摘要。
3. `admin registry review list` 成功即可，允许返回空列表。
4. registry 普通用户 token 与管理员 token 不能混用。

---

## 6. monitor 用户登录

monitor 使用独立 realm：`acps-monitor`。不要复用 registry 的 token 文件或浏览器授权 URL。

执行：

```bash
uv run acps-cli monitor auth login
```

浏览器打开 CLI 打印的 URL，使用 monitor 测试账号登录：

- 用户名：`monitor-viewer`
- 密码：`demo123`

授权成功后验证：

```bash
uv run acps-cli monitor auth status --json
uv run acps-cli monitor auth whoami --json
uv run acps-cli monitor status
uv run acps-cli monitor heartbeat summary
uv run acps-cli monitor auth refresh --json
```

预期结果：

1. `monitor auth status` 显示 `service = "monitor"`。
2. `monitor auth whoami` 展示本地 token claims 摘要，不表示服务端存在 `/me` 权威接口。
3. `monitor status` 访问 `/health`，不需要 Authorization header。
4. `monitor heartbeat summary` 会携带 Bearer token，并按 role / scope / AIC scope 鉴权。
5. 输出中可看到 `tenant_id` 与 `allowed_aics` 摘要。

---

## 7. 验证 token 文件和 claims

登录后，可以检查 token 文件权限和非敏感 claims 摘要。不要把 raw token 粘贴到日志、工单或文档里。

registry 用户示例：

```bash
TOKEN_FILE=/tmp/acps-registry-user.json RESOURCE_CLIENT_ID=registry-api python3 - <<'PY'
import base64
import json
import os
import stat
from pathlib import Path

path = Path(os.environ["TOKEN_FILE"])
resource_client_id = os.environ["RESOURCE_CLIENT_ID"]
payload = json.loads(path.read_text(encoding="utf-8"))
segment = payload["access_token"].split(".")[1]
segment += "=" * (-len(segment) % 4)
claims = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")))
roles = ((claims.get("resource_access") or {}).get(resource_client_id) or {}).get("roles")
summary = {
    "token_file": str(path),
    "mode": oct(stat.S_IMODE(path.stat().st_mode)),
    "iss": claims.get("iss"),
    "aud": claims.get("aud"),
    "azp": claims.get("azp"),
    "preferred_username": claims.get("preferred_username"),
    "roles": roles,
    "tenant_id": claims.get("tenant_id"),
    "allowed_aics": claims.get("allowed_aics"),
}
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY
```

期望：

- 文件权限为 `0o600`，或等价的 owner-only 权限。
- registry token 的 `aud` 包含 `registry-api`，`azp = registry-cli`。
- monitor token 的 `aud` 包含 `monitor-api`，`azp = monitor-cli`。
- monitor token 还应包含 `tenant_id` / `allowed_aics`。

---

## 8. 验证 OIDC mode 下本地密码命令不可用

当 `registry.auth.mode = "oidc"` 时，以下命令不再走 registry-server 的本地账号接口：

```bash
uv run acps-cli auth login --username alice
uv run acps-cli auth change-password
uv run acps-cli admin registry user reset-password --user-id test-user
```

预期结果：

1. `auth login --username` 直接报错，提示 OIDC mode 不接受用户名密码参数。
2. `auth change-password` 报错，提示密码由 OIDC 身份提供方管理。
3. `admin registry user reset-password` 报错，提示重置密码由 OIDC 身份提供方管理。

这样可以确认 CLI 没有在 OIDC mode 下退回 password grant 或本地密码登录路径。

---

## 9. 验证 token 隔离

三类 session 不能混用：

- registry 普通用户：`service=registry`、`account_kind=user`
- registry 管理员：`service=registry`、`account_kind=admin`
- monitor 用户：`service=monitor`、`account_kind=user`

可以做一个负向检查：

```bash
AUTH_MONITOR_TOKEN_FILE=/tmp/acps-registry-user.json \
uv run acps-cli monitor auth status --json
```

预期 CLI 拒绝使用该 token，错误原因应指向 `service`、`account_kind`、`issuer` 或 `client_id` 不匹配。

---

## 10. 退出登录

分别执行：

```bash
uv run acps-cli auth logout --json
uv run acps-cli admin auth logout --json
uv run acps-cli monitor auth logout --json
```

预期结果：

1. 本地 token 文件被删除或清空。
2. 如果 provider 暴露 revocation endpoint，CLI 会尝试撤销 refresh token。
3. 随后执行对应 `status --json`，应显示 `authenticated = false`。

示例：

```bash
uv run acps-cli auth status --json
uv run acps-cli admin auth status --json
uv run acps-cli monitor auth status --json
```

---

## 11. 常见问题排查

### 11.1 CLI 提示 discovery 缺少 device endpoint

检查 issuer 是否正确：

```bash
python3 - <<'PY'
import json
import urllib.request

issuer = "http://localhost:9080/realms/acps-registry"
with urllib.request.urlopen(f"{issuer}/.well-known/openid-configuration") as response:
    payload = json.load(response)
print(json.dumps({
    "issuer": payload.get("issuer"),
    "device_authorization_endpoint": payload.get("device_authorization_endpoint"),
    "token_endpoint": payload.get("token_endpoint"),
}, ensure_ascii=False, indent=2))
PY
```

如果 `device_authorization_endpoint` 为空，说明 Keycloak client 或 realm 没有启用 Device Authorization Grant，或者 issuer 指错了 realm。

### 11.2 登录一直 authorization_pending

这通常表示你还没有在浏览器完成授权。回到浏览器页面，确认已经登录用户并点击授权确认。

### 11.3 浏览器已经授权，但 CLI 仍 401

优先检查：

1. server 端 `*_OIDC_ISSUER` 是否与 CLI issuer 完全一致。
2. server 端 `*_OIDC_ALLOWED_AZP` 是否包含 `registry-cli` 或 `monitor-cli`。
3. token 的 `aud` 是否包含 `registry-api` 或 `monitor-api`。
4. Keycloak 中用户是否具备对应 API client 的角色。

### 11.4 monitor 查询返回 403

`monitor-server` 不只校验登录，还会校验 role / scope / AIC scope。请确认 monitor 用户 token 中有：

- `resource_access.monitor-api.roles`
- `tenant_id`
- `allowed_aics`

如果查询的 AIC 不在 `allowed_aics` 内，返回 403 是预期行为。

### 11.5 local mode 遇到 410

如果 `registry.auth.mode = "local"`，但 registry-server 已启用 OIDC，本地 `/auth/login`、`/auth/register`、`/auth/refresh-token` 会返回 410。此时应把 CLI 配置切到：

```toml
[registry.auth]
mode = "oidc"
```

并重新执行 `auth login` 或 `admin auth login`。

---

## 12. 结论

完成本文步骤后，你已经验证了 `acps-cli` 的 OIDC 人类登录主链路：

1. registry 普通用户、registry 管理员、monitor 用户分别使用自己的 realm、client 和 token 文件。
2. CLI 通过 Device Authorization Grant 获取 token，不接触 Keycloak 用户密码。
3. 受保护 API 调用能自动携带 Bearer token，并在需要时刷新。
4. 本地密码类命令在 OIDC mode 下明确不可用。
5. logout 能清理本地 session，并在 provider 支持时撤销 refresh token。

如果你要验证 Web 应用的浏览器重定向登录与回跳流程，请阅读 [OIDC Web 应用手工验证教程](./oidc-web-app-manual-verification.md)。
