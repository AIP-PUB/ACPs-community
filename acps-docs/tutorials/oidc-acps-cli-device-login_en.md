[Home](../README.md)

**[English](oidc-acps-cli-device-login_en.md) | [中文](oidc-acps-cli-device-login.md)**

# acps-cli OIDC Device Login Tutorial

This tutorial explains how to use `acps-cli` to complete a human-user OIDC login in a pure command-line scenario. It targets the common SSH remote host setup: the CLI runs in the remote terminal, while the browser can run on your local machine, a jump host, or any device that can reach Keycloak.

The official OIDC human login path for `acps-cli` is the OAuth 2.0 Device Authorization Grant. The CLI does not launch a browser, does not listen on a local callback port, and does not ask you to type a Keycloak username and password in the terminal. It only prints a browser URL and a one-time user code; you open the URL in a browser, enter or confirm the user code, and then complete the login and authorization on the Keycloak page.

This document uses the local development defaults as examples:

| Service domain | Keycloak realm | CLI client | API audience | Example user |
| --- | --- | --- | --- | --- |
| registry regular user | `acps-registry` | `registry-cli` | `registry-api` | `registry-client / demo123` |
| registry administrator | `acps-registry` | `registry-cli` | `registry-api` | `registry-admin / demo123` |
| monitor query user | `acps-monitor` | `monitor-cli` | `monitor-api` | `monitor-viewer / demo123` |

Corresponding addresses:

- Keycloak: `http://localhost:9080`
- registry-server: `http://localhost:9001`
- monitor-server: `http://localhost:9009`

If you are verifying a non-local environment, replace the issuer, the server URL, and the test accounts.

---

## 1. How CLI login differs from web login

Web applications typically use the Authorization Code Flow: the browser is sent to Keycloak, the user logs in, and Keycloak then redirects the browser back to the application's callback URL.

`acps-cli` is not suited to that model. The CLI often runs on a remote SSH host, and you cannot assume that the remote host can launch a browser, nor that Keycloak can redirect back to an ephemeral port on the remote host.

So `acps-cli` uses the Device Authorization Grant:

1. The CLI requests a device code from Keycloak.
2. The CLI prints the browser URL and the user code in the terminal.
3. The operator opens the URL in a browser and completes the Keycloak login and authorization.
4. The CLI polls the token endpoint in the terminal.
5. Once authorization completes, the CLI saves its own local session/token file for that service domain.

This flow has two important characteristics:

- The Keycloak username and password are entered only on the browser login page, never in the CLI terminal.
- registry and monitor belong to different realms and account systems, so they do not share login state and do not reuse the same token file.

---

## 2. Prerequisites

Before you start, confirm that:

1. Keycloak in `acps-infra/dev-infra` has been started.
2. Keycloak dev bootstrap has been completed and the official CLI clients have been created:
   - `registry-cli` in the `acps-registry` realm
   - `monitor-cli` in the `acps-monitor` realm
3. `registry-server` and `monitor-server` have been started in OIDC mode.
4. You have a browser that can reach the Keycloak URL.
5. You know which token file locations this test will use, so you do not read an old session.

For local development, you can start Keycloak first:

```bash
cd /Users/huxiaofeng/Projects/acps/acps-infra/dev-infra
./dev-infra.sh up keycloak
./dev-infra.sh wait keycloak
```

Then start the servers in OIDC mode respectively. The following commands use the local development default realms:

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

If you use `just test e2e -- tests/e2e/test_oidc_keycloak_flow.py` or another temporary test entry point, they may bring up temporary service instances automatically. For manual CLI verification, it is best to explicitly confirm that the CLI is pointed at the service instance you intend to verify.

---

## 3. Configure acps-cli

`acps-cli` resolves OIDC settings from a unified configuration file and environment variables. Minimal TOML example:

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

You can also override the settings temporarily with environment variables, which is handy for one-off verification:

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

A local HTTP issuer should only be used in development environments, together with `require_https = false`. Production environments should use an HTTPS issuer and keep HTTPS verification enabled.

---

## 4. registry regular user login

Enter the `acps-cli` repository:

```bash
cd /Users/huxiaofeng/Projects/acps/acps-cli
```

Run the login:

```bash
uv run acps-cli auth login
```

The terminal prints output similar to:

```text
Open this URL in a browser: http://localhost:9080/realms/acps-registry/device?user_code=ABCD-EFGH
Enter this code if prompted: ABCD-EFGH
```

Open the URL in a browser. If the browser does not carry the code over automatically, type in the user code shown in the terminal manually. Then log in with the test account:

- Username: `registry-client`
- Password: `demo123`

After authorization succeeds, the CLI returns a login summary. You can continue to verify:

```bash
uv run acps-cli auth status --json
uv run acps-cli auth whoami --json
uv run acps-cli agent list --json
uv run acps-cli auth refresh --json
```

Expected results:

1. `auth status` shows `authenticated = true`.
2. `auth whoami` can return a user summary from registry-server's `/account/me`.
3. `agent list` succeeds; an empty list is fine.
4. `auth refresh` refreshes the session successfully.
5. The output contains no raw access token, refresh token, or id token.

---

## 5. registry administrator login

The registry administrator uses a separate token file, but is still in the `acps-registry` realm and still uses the `registry-cli` client.

Run:

```bash
uv run acps-cli admin auth login
```

Open the URL printed by the CLI in a browser and log in with the administrator test account:

- Username: `registry-admin`
- Password: `demo123`

After authorization succeeds, verify:

```bash
uv run acps-cli admin auth status --json
uv run acps-cli admin auth whoami --json
uv run acps-cli admin registry review list --json
uv run acps-cli admin auth refresh --json
```

Expected results:

1. `admin auth status` shows `account_kind = "admin"`.
2. `admin auth whoami` returns a summary of the registry administrator identity.
3. `admin registry review list` succeeds; an empty list is allowed.
4. A registry regular user token and an administrator token must not be used interchangeably.

---

## 6. monitor user login

monitor uses a separate realm: `acps-monitor`. Do not reuse the registry token file or the registry browser authorization URL.

Run:

```bash
uv run acps-cli monitor auth login
```

Open the URL printed by the CLI in a browser and log in with the monitor test account:

- Username: `monitor-viewer`
- Password: `demo123`

After authorization succeeds, verify:

```bash
uv run acps-cli monitor auth status --json
uv run acps-cli monitor auth whoami --json
uv run acps-cli monitor status
uv run acps-cli monitor heartbeat summary
uv run acps-cli monitor auth refresh --json
```

Expected results:

1. `monitor auth status` shows `service = "monitor"`.
2. `monitor auth whoami` shows a summary of the local token claims; it does not mean the server exposes an authoritative `/me` endpoint.
3. `monitor status` accesses `/health` and does not require an Authorization header.
4. `monitor heartbeat summary` carries the Bearer token and is authorized according to role / scope / AIC scope.
5. The output shows a `tenant_id` and `allowed_aics` summary.

---

## 7. Verify the token file and claims

After logging in, you can inspect the token file permissions and a summary of non-sensitive claims. Do not paste raw tokens into logs, tickets, or documents.

registry user example:

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

Expectations:

- File permissions are `0o600`, or equivalent owner-only permissions.
- The registry token's `aud` contains `registry-api`, and `azp = registry-cli`.
- The monitor token's `aud` contains `monitor-api`, and `azp = monitor-cli`.
- The monitor token should also contain `tenant_id` / `allowed_aics`.

---

## 8. Verify that local password commands are unavailable in OIDC mode

When `registry.auth.mode = "oidc"`, the following commands no longer go through registry-server's local account endpoints:

```bash
uv run acps-cli auth login --username alice
uv run acps-cli auth change-password
uv run acps-cli admin registry user reset-password --user-id test-user
```

Expected results:

1. `auth login --username` fails immediately, indicating that OIDC mode does not accept username/password arguments.
2. `auth change-password` fails, indicating that the password is managed by the OIDC identity provider.
3. `admin registry user reset-password` fails, indicating that password reset is managed by the OIDC identity provider.

This confirms that the CLI does not fall back to the password grant or to the local password login path in OIDC mode.

---

## 9. Verify token isolation

The three kinds of session cannot be mixed:

- registry regular user: `service=registry`, `account_kind=user`
- registry administrator: `service=registry`, `account_kind=admin`
- monitor user: `service=monitor`, `account_kind=user`

You can run a negative check:

```bash
AUTH_MONITOR_TOKEN_FILE=/tmp/acps-registry-user.json \
uv run acps-cli monitor auth status --json
```

The CLI is expected to reject that token, and the error reason should point to a mismatch in `service`, `account_kind`, `issuer`, or `client_id`.

---

## 10. Log out

Run the following separately:

```bash
uv run acps-cli auth logout --json
uv run acps-cli admin auth logout --json
uv run acps-cli monitor auth logout --json
```

Expected results:

1. The local token file is deleted or cleared.
2. If the provider exposes a revocation endpoint, the CLI attempts to revoke the refresh token.
3. Running the corresponding `status --json` afterwards should show `authenticated = false`.

Example:

```bash
uv run acps-cli auth status --json
uv run acps-cli admin auth status --json
uv run acps-cli monitor auth status --json
```

---

## 11. FAQ and troubleshooting

### 11.1 The CLI reports that discovery is missing the device endpoint

Check whether the issuer is correct:

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

If `device_authorization_endpoint` is empty, the Keycloak client or realm has not enabled the Device Authorization Grant, or the issuer points to the wrong realm.

### 11.2 Login stays at authorization_pending

This usually means you have not yet completed authorization in the browser. Go back to the browser page, confirm that you have logged in as a user, and click to confirm the authorization.

### 11.3 The browser has authorized, but the CLI still returns 401

Check these first:

1. Whether the server-side `*_OIDC_ISSUER` is exactly the same as the CLI issuer.
2. Whether the server-side `*_OIDC_ALLOWED_AZP` includes `registry-cli` or `monitor-cli`.
3. Whether the token's `aud` includes `registry-api` or `monitor-api`.
4. Whether the user in Keycloak has the role for the corresponding API client.

### 11.4 monitor queries return 403

`monitor-server` verifies not only login, but also role / scope / AIC scope. Confirm that the monitor user token contains:

- `resource_access.monitor-api.roles`
- `tenant_id`
- `allowed_aics`

If the AIC you are querying is not in `allowed_aics`, returning 403 is expected behavior.

### 11.5 local mode encounters 410

If `registry.auth.mode = "local"` but registry-server has OIDC enabled, the local `/auth/login`, `/auth/register`, and `/auth/refresh-token` endpoints return 410. In that case, switch the CLI configuration to:

```toml
[registry.auth]
mode = "oidc"
```

and run `auth login` or `admin auth login` again.

---

## 12. Conclusion

After completing the steps in this document, you have verified the main OIDC human login path of `acps-cli`:

1. registry regular users, registry administrators, and monitor users each use their own realm, client, and token file.
2. The CLI obtains tokens through the Device Authorization Grant and never touches the Keycloak user password.
3. Protected API calls automatically carry the Bearer token and refresh it when needed.
4. Local password commands are explicitly unavailable in OIDC mode.
5. logout cleans up the local session and revokes the refresh token when the provider supports it.

If you want to verify a web application's browser redirect login and redirect-back flow, read [OIDC Web App Manual Verification Tutorial](./oidc-web-app-manual-verification_en.md).
