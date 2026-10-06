[Home](../README.md)

**[English](oidc-web-app-manual-verification_en.md) | [中文](oidc-web-app-manual-verification.md)**

# OIDC Web Application Manual Verification Tutorial

This tutorial explains how to manually verify OIDC login for real human users. It uses `demo-leader + Keycloak` as the example, but the verification approach itself is generic: if you are integrating with `registry-server`, `monitor-server`, or another project that has adopted OIDC, you only need to replace the URLs, realm, client, and test users described in this document.

If what you want to verify is the Device Authorization Grant login flow of a pure command-line tool such as `acps-cli`, see [acps-cli OIDC Device Login Tutorial](./oidc-acps-cli-device-login_en.md).

This document focuses on three things:

1. Check, as an administrator, whether the login-related key configuration in Keycloak is in place.
2. Check, as an administrator, whether the test users and role mappings are correct.
3. Walk through the complete flow as an ordinary user: browser redirect login, redirect back, logout, and redirect back again.

This document is written against the local development defaults already committed in the current repository. The example conventions are as follows:

- Application Web UI: `http://localhost:9030`
- Application API: `http://localhost:9031`
- Keycloak: `http://localhost:9080`
- Realm: `acps-leader`
- Browser login client: `leader-web`
- API resource client: `leader-api`

> **If you are working with a deployment assembled from the install package** (see [Deploying ACPs with the Install Package](./install-package-ansible-deploy_en.md)): the browser entry point is **`http://<host>:9030/`** on the business node (`demo_leader_web`), the Web app ships with a `/api/v1/` same-origin reverse proxy, and `backendBase=''`; there is **no** separate `demo-nginx`. The API is still on **9031**, the same as in local development (same image / host).

The local development OIDC configuration used in the example is already committed in the following files:

- [demo-leader/leader/config.toml](../../demo-leader/leader/config.toml)
- [demo-leader/web_app/runtime-config.js](../../demo-leader/web_app/runtime-config.js)

The descriptions of Keycloak administration console settings in this tutorial apply only to version `26.6.3`; other versions may differ.

---

## 1. What Problem Does OIDC Solve

If a system needs to support login for real human users, the most direct — but also the most troublesome — approach is for every project to handle accounts, passwords, login state, and permissions on its own. This leads to several common problems:

1. Every system has to store or handle passwords itself, which spreads the security responsibility around.
2. Web login, logout, session timeout, and permission mapping tend to be implemented differently in each place, so behavior is inconsistent.
3. Once several systems are connected, it is hard to unify user identity and permission information.

The point of OIDC is to hand off "who the user is, how they log in, and how to obtain standard identity information after login" to a unified identity provider. For an application, the focus becomes:

1. Redirect the user to the identity provider to log in.
2. Receive the redirect back after a successful login.
3. Use the standard protocol to exchange for a token.
4. Decide in-application permissions based on the identity and role information in the token.

In the example used in this document, `Keycloak` is the identity provider. The application itself is not responsible for storing user passwords; instead, it cooperates with Keycloak over OIDC to complete the login.

---

## 2. Concepts at a Glance

If you are not yet familiar with OIDC and Keycloak, remembering the following terms is enough:

- `OIDC`: OpenID Connect, a login protocol built on top of OAuth 2.0 that solves "how the application knows who the user is after they log in".
- `Keycloak`: the identity provider (IdP), responsible for the login page, user directory, client configuration, token issuance, and so on.
- `realm`: an independent identity space in Keycloak. It acts as an isolation boundary, with its own users, clients, roles, and login settings.
- `client`: the identity of an "application" or "resource service" registered in Keycloak — not a user account. A browser front end, a back-end API, and a test program can each have their own client.
- `user`: a real human user account, for example `leader-user`.
- `role`: an authorization label used to express a user's permissions in a given system, for example `user`, `operator`, `admin`.
- `token`: the standard credential issued by Keycloak to the application after a successful login. The application usually relies on it to determine login state and permissions.

The pair that is easiest to confuse is `client` and `user`:

- `client` represents "which application is connecting to Keycloak".
- `user` represents "which real person is logging in".

For example, in this document `leader-web` is the client used for browser login, while `leader-user` is the user who actually logs in.

---

## 3. Prerequisites

Before you start, confirm the following:

1. You have prepared `demo-leader/.env` on your machine and filled in valid LLM sensitive information.
2. The sibling directories `acps-infra/`, `acps-sdk/`, and `acps-cli/` exist.
3. Docker runs correctly on your machine.

If you are verifying a different project, replace `demo-leader` above with that project and confirm that OIDC is enabled in that project's local configuration.

---

## 4. Start the Local Environment

In the `demo-leader` directory, run:

```bash
cd /Users/huxiaofeng/Projects/acps/demo-leader
just dev start
```

Notes:

- Because the example configuration enables OIDC by default, `just dev start` automatically brings up Keycloak from `acps-infra/dev-infra` and starts the local application processes:
  - Web UI: `http://localhost:9030`
  - Leader API: `http://localhost:9031`

If you want to confirm the background status, run:

```bash
just dev status
just infra status keycloak
```

---

## 5. Check Keycloak as an Administrator

### 5.1 Log In to the Administration Console

Open in your browser:

```text
http://localhost:9080
```

Administrator account:

- Username: `admin`
- Password: `devpass`

After logging in, switch to the realm:

```text
acps-leader
```

If you are verifying another project, switch to its corresponding realm.

### 5.2 Check the Realm-Level Signature Algorithm

Go to:

```text
Realm settings -> Tokens
```

Confirm:

- `Default signature algorithm = EdDSA`

Then go to:

```text
Realm settings -> Keys
```

Confirm:

- A key used for signing is present
- The key is enabled and active
- Its algorithm is `Ed25519`

This shows that the current realm has `EdDSA / Ed25519` signing enabled according to the development convention.

### 5.3 Check the Browser Login Client

Go to:

```text
Clients -> leader-web
```

On the `Settings` page, there are two areas worth checking closely.

The first is:

```text
Settings -> Capability config
```

Confirm:

1. `leader-web` is a browser login client, not a user account.
2. `Client authentication = Off`
3. `Standard flow = On`
4. `Direct access grants = Off`

The second is:

```text
Settings -> Access settings
```

Confirm that `Valid redirect URIs` and `Web origins` cover the actual development entry point. In the example in this document, they should include at least:

- `http://localhost:9030/*`
- `http://localhost:9030`

If your local environment uses `127.0.0.1`, different ports, or the front-end address of another project, these must also match the actual entry point.

### 5.4 Check the API Client and Role Ownership

Go to:

```text
Clients
```

Confirm that the `leader-api` client in the example exists. Its purpose is not to carry browser login, but to serve as the owner of the API-side roles and permissions.

---

## 6. Check Users and Roles as an Administrator

Go to:

```text
Users
```

Confirm that the following example users exist:

| Username | Password | `leader-api` role | Purpose |
| --- | --- | --- | --- |
| `leader-user` | `demo123` | `user` | Regular human user |
| `leader-operator` | `demo123` | `operator` | Operations / operator |
| `leader-admin` | `demo123` | `admin` | Administrator |

You can open each user in turn and, under:

```text
User details -> Role mappings
```

confirm that:

- `leader-user` has `leader-api:user`
- `leader-operator` has `leader-api:operator`
- `leader-admin` has `leader-api:admin`

If you are verifying another project, replace the example usernames and role names with the values that project uses.

---

## 7. Verify Browser Login and Redirect Back as an Ordinary User

It is recommended that you use a browser incognito window, so that the administrator session and the ordinary user session do not interfere with each other.

### 7.1 Trigger Login

Open:

```text
http://localhost:9030
```

Because the example configuration enables OIDC by default, the login flow is triggered automatically once the page initializes, and the browser is redirected to the Keycloak login page.

### 7.2 Log In with an Ordinary User

On the Keycloak login page, enter:

- Username: `leader-user`
- Password: `demo123`

After a successful login, the browser is redirected back to the application Web UI.

### 7.3 Observe the Redirect-Back Process

During the redirect back, the address bar usually shows the following briefly:

```text
http://localhost:9030/?code=...&state=...&session_state=...
```

The front end then completes the authorization code exchange for a token and cleans the URL back to a plain page address:

```text
http://localhost:9030/
```

On the page you should see:

1. A `Logout` button appears.
2. The identifier of the currently logged-in user appears, usually showing `leader-user`.

This shows that the following chain works end to end:

```text
App Web -> Keycloak login page -> user authentication -> redirect back to app -> front end completes code exchange
```

---

## 8. Verify Logout and Redirect Back

On the logged-in page, click:

```text
Logout
```

Expected behavior:

1. The browser is redirected to Keycloak's logout endpoint.
2. Keycloak then redirects the browser back to the application home page.
3. If the application is configured to automatically initiate OIDC login when the user is not logged in, the page jumps to the login page again.

This shows that the logout redirect-back chain also works correctly.

---

## 9. Observe Protocol Details with Browser Developer Tools

If you want to further confirm that the browser really follows the standard OIDC flow, open the `Network` panel in DevTools and go through the login again.

You will usually see these key requests:

1. Read discovery:

```text
GET /realms/acps-leader/.well-known/openid-configuration
```

2. Redirect to the authorization endpoint, usually of the form:

```text
GET /realms/acps-leader/protocol/openid-connect/auth?...client_id=leader-web...
```

3. After redirecting back, call the token endpoint, usually of the form:

```text
POST /realms/acps-leader/protocol/openid-connect/token
```

4. On logout, call the end session or logout endpoint.

If all of these requests are present and the page behavior matches the phenomena described above, you can generally conclude that this OIDC login chain is working correctly.

---

## 10. Conclusion

Once you have completed the checks in this document, you have verified the local human-user OIDC capability at three levels:

1. The basic configuration of the Keycloak realm and clients is correct.
2. The test users and role mappings are correct.
3. The overall flow of browser redirect login, code exchange, and logout redirect is correct.

If one of these steps fails, it is recommended to start troubleshooting with the following categories of problems:

- Whether the `authority`, `client_id`, and `redirect_uri` configured on the application side are consistent with Keycloak.
- Whether `Valid redirect URIs` / `Web origins` in Keycloak cover the address that is actually being accessed.
- Whether the current browser has a mixture of an administrator session, an old token, or an old cookie.
- Whether the test users exist and whether they have the correct API roles.

This set of checks is not limited to `demo-leader`. As long as a project uses the OIDC pattern of "the Web front end redirects to Keycloak for login and then redirects back to the local application", you can apply the approach in this document directly.
