[Home](../README.md)

**[English](manual-deploy-from-app-thin-package_en.md) | [中文](manual-deploy-from-app-thin-package.md)**

# Manual Deployment from an Application Thin Package

This tutorial covers **what it actually takes to deploy an ACPs installation**: install the application into a Python environment, lay down configuration, migrate the database, issue certificates, bring the processes up in the correct order, and probe liveness. Everything is done with manual commands — no Docker, no Ansible, and no binding to a particular operating system.

This is ACPs' **baseline deployment procedure**. [Ansible deployment](./install-package-ansible-deploy_en.md) does the same thing — it turns the steps below into playbooks, and additionally takes on multi-machine orchestration, operating-system differences, and idempotent upgrade and rollback. The source tree that implements this automation (`acps-infra/release/install-packaging`) is referred to below as the **automation installation layer**. The two are not two routes; they are two ways of executing the same procedure:

| Deployment step | This document (manual execution) | Automation installation layer (automatic execution) |
| --- | --- | --- |
| Prepare the application | Unpack the thin package, install dependencies online from the lock file | Application release package with a pre-assembled wheelhouse, offline `pip install --no-index` |
| Directory layout | You choose `$INSTALL_ROOT` | `/opt/acps` + `releases/{version}/` + `current` symlink |
| Write configuration | Manually edit `config/*.toml` and `.env` | Jinja2 templates that render same-named files from the inventory |
| Database migration | `python -m alembic upgrade head` | The same command, invoked by `db_migrate_host.yml` |
| Issue certificates | Manually run the `acps-cli` bootstrap | The same `acps-cli`, invoked by the `cert_provision` role |
| Bring up processes | Run in the foreground, or wire up your own process manager (for example, write systemd units) | Render systemd units from `runtime-package.toml` |
| Liveness probing | `curl /health` | `health_http.yml` with retries |

The commands are essentially the same set; the difference is who types them. So there are two ways to read this document: if you **need to deploy in an environment the installer cannot cover** (you cannot install Docker, the operating system is outside the Rocky 8/9 and Ubuntu 20.04/22.04 support matrix, or you need to plug into your own supervisor / k8s / Nomad / SaltStack), follow it step by step; if you **deploy with Ansible but want to understand what the installer is doing and how to intervene manually when something goes wrong**, use it as a reference manual.

The starting point is the **application thin package** (`{app}-wheel-{version}.tar.gz`, the artifact of `just package wheel`). It is the only operating-system-independent layer in the whole pipeline, and it is also the upstream input to the automation path.

```text
                        just package wheel
                                |
                      Application thin package
                            /         \
     This document: deploy directly    assembled into an application release package (with wheelhouse, platform-specific)
              |                        ↓ assemble the installation package
              |                        ↓ Ansible
              \_______________________/
                        |
        The same steps: install dependencies / lay down configuration / migrate / issue certificates / start processes / liveness probing
```

---

## 0. Read This Section First: The Three Boundaries of This Document

**One: dependency components are only required, not taught.** How PostgreSQL, Redis, RabbitMQ, and the rest are installed and tuned is up to the standards of your own environment. This document gives only the version requirements, the capabilities that must be enabled, and **the wiring that is mandatory on the ACPs side** (this part cannot be skipped; see §6).

**Two: the deployment machine needs network access to PyPI.** By design, the thin package does **not** contain `wheelhouse/`; third-party dependencies are installed at deployment time from PyPI (or your private mirror). For a fully offline deployment, either pre-download them yourself as suggested in §4.4, or use the [application release package](./app-release-package-build_en.md) that already has a wheelhouse assembled.

**Three: process supervision is your decision.** The thin package contains no systemd unit, no start script, and no health-check script. This document gives the **authoritative start command** (from `runtime-package.toml` inside the thin package); whether you wire it into systemd / launchd / supervisor / a container is up to you.

---

## 1. What Is in the Thin Package

Taking registry-server as the example, after unpacking:

```text
registry-server-wheel-2.2.0/
|-- dist/
|   `-- registry_server-2.2.0-py3-none-any.whl   # only the application's own wheel
|-- requirements-runtime.lock                    # third-party dependencies, with hashes, cross-platform
|-- runtime-package.toml                         # authoritative declaration of start commands / ports / health checks
|-- config/                                      # default.toml + per-environment overrides
|-- alembic/  alembic.ini                        # database migration
|-- .env.example                                 # template for sensitive settings
|-- README.md
`-- checksums.txt
```

`runtime-package.toml` is the file you should read first for a manual deployment — the start commands, ports, and health-check addresses are all in it. Copy them verbatim; do not guess:

```toml
[[components]]
id = "registry-server-api"
type = "python-service"
entrypoint = "uvicorn app.main:app --host 0.0.0.0 --port 9001"
ports = [9001]
health_check = "http://127.0.0.1:9001/health"
```

**What is not in the thin package** (these points determine the later steps):

| Not present | Consequence |
| --- | --- |
| `wheelhouse/` | Installing dependencies requires network access; see §4.4 |
| Internal wheels (`acps-sdk` / `acps-cli`) | Must be built separately and then installed; see §3 |
| Python interpreter | The target machine must provide 3.14 itself; see §4.1 |
| Start script / systemd unit | You wire it up yourself; see §4.8 |
| Certificates | Issued separately; see §7 |

---

## 2. Building the Thin Package

Do this on the **development machine / build machine** (not the target machine). The repositories must be laid out as sibling directories; this is exactly the same for manual and automated deployment:

```text
acps/
|-- acps-infra/     # provides the shared just modules; must exist
|-- acps-sdk/
|-- registry-server/
`-- ...
```

The build machine needs `just`, `uv`, and `python3` (3.11+, with 3.14 as the formal target). Then, project by project:

```bash
cd /path/to/acps/registry-server
just package wheel
# Artifact: dist/registry-server-wheel-2.2.0.tar.gz
```

`just package wheel` automatically runs bootstrap first (preparing the build venv and so on); you do not need to run `just package bootstrap` manually.

Projects that support this command: `registry-server`, `ca-server`, `discovery-server`, `monitor-server`, `mq-auth-server`, `demo-leader`, `demo-partner`, `acps-cli`.

### 2.1 discovery-server Requires a Specified Architecture and Variant

discovery-server is the only thin package that is **not cross-platform**: its lock file is generated per `{cpu|gpu}-{amd64|arm64}`, the architecture is fixed at packaging time, and it targets manylinux.

```bash
cd /path/to/acps/discovery-server

# By default only the CPU variant is produced; the architecture is taken from the build machine's uname -m
just package wheel
# The artifact contains requirements-runtime-cpu-arm64.lock (or -amd64)

# For the GPU variant (can only be built on Linux)
DISCOVERY_PACKAGE_VARIANTS=gpu just package wheel
```

**The discovery-server thin package must be built on a machine with the same architecture as the target machine.** Thin packages for the other projects are platform-independent and are the same no matter which machine builds them.

---

## 3. Building the Internal Wheels Separately (This Step Cannot Be Skipped)

The thin package's lock file is exported with `uv export --no-emit-local`, which deliberately strips out local path dependencies; the thin package's `dist/` also contains only the application's own wheel. So `acps-sdk` is **neither in the lock file nor in the thin package** — `runtime-package.toml` merely records it as metadata under `internal_wheels`, and the automation path fills it in by reading that field during the assembly stage. In a manual deployment you must fill it in yourself. First check which internal wheels the target service needs:

```bash
grep internal_wheels <unpacked-dir>/runtime-package.toml
```

| Service | Internal wheels required |
| --- | --- |
| registry-server / discovery-server / monitor-server | `acps-sdk` |
| ca-server / mq-auth-server | None |
| demo-leader / demo-partner | `acps-sdk`, `acps-cli` |
| acps-cli | `acps-sdk` |

**Build `acps-sdk`** (it is a shared library with no Justfile, so use uv directly):

```bash
cd /path/to/acps/acps-sdk
uv build --wheel --out-dir /tmp/acps-internal-wheels
# Artifact: acps_sdk-2.2.0-py3-none-any.whl
```

**Obtain the `acps-cli` wheel** (it has a Justfile, so take it from its own thin package; do not use a bare `uv build`):

```bash
cd /path/to/acps/acps-cli
just package wheel
tar -xzf dist/acps-cli-wheel-*.tar.gz -C /tmp
cp /tmp/acps-cli-wheel-*/dist/acps_cli-*.whl /tmp/acps-internal-wheels/
```

Copy `/tmp/acps-internal-wheels/` to the target machine together with the thin packages.

---

## 4. Generic Deployment Procedure (the Same for Every Service)

The following uses `registry-server` as the demonstration. To switch services you only change the thin package name, the start command, and the environment variables; the skeleton is exactly the same.

### 4.1 Target Machine Prerequisites

| Item | Requirement |
| --- | --- |
| Python | **3.14**; with the `venv` module |
| Network | Access to PyPI or your private index |
| Tools | `tar`, `sha256sum` (on macOS use `shasum -a 256`) |
| Account | We recommend creating a dedicated unprivileged account (the automation installation layer uses `acps`); this document does not decide it for you |

#### 4.1.1 Install Python 3.14

There are many ways to install Python on different systems; here we recommend using uv to install a standalone build, which is also the path the automation installation layer takes:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install to a global location, not into some user's HOME (see the reason below)
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
sudo mkdir -p "$UV_PYTHON_INSTALL_DIR"
sudo chown "$USER" "$UV_PYTHON_INSTALL_DIR"
~/.local/bin/uv python install --no-bin 3.14

PYBIN="$(UV_PYTHON_PREFERENCE=only-managed ~/.local/bin/uv python find 3.14)"
echo "$PYBIN"    # /opt/uv-python/cpython-3.14.x-linux-x86_64-gnu/bin/python3.14
```

Below, **always use the `$PYBIN` absolute path** to create the venv; do not count on `python3.14` being on PATH: Debian/Ubuntu only add `~/.local/bin` to PATH in a login shell via `~/.profile`, so non-interactive SSH and systemd cannot see it, whereas RHEL-family systems have it by default. `--no-bin` exists precisely to avoid placing that unreliable symlink in `~/.local/bin`.

Installing to `/opt` rather than `~/.local` is equally important: a venv remembers the absolute path of the interpreter that created it, and if that interpreter is hidden under some user's HOME, running the service as a different user (for example, switching to `acps`) will fail to start because the interpreter cannot be read.

The interpreter version must be consistent across all nodes; the automation installation layer pins it in `[python].version` in `acps-infra/release/install-packaging/baseline-matrix.toml`.

### 4.2 Decide the Directory Layout

The automation installation layer uses `/opt/acps` (runtime), `/var/lib/acps` (data), and `/var/log/acps` (logs). For a manual deployment you can decide for yourself, and reusing this layout is fine — it saves you one migration later if you want to switch to Ansible. The only hard constraint is that **a service's venv, `config/`, `.env`, and `alembic/` must be in the same directory**; see §4.7 for the reason. This document uses:

```bash
INSTALL_ROOT=/opt/acps/registry-server
sudo mkdir -p "$INSTALL_ROOT"
sudo chown "$USER" "$INSTALL_ROOT"
```

The `chown` line cannot be omitted. A directory created by `sudo mkdir` is owned by root, while the later unpacking, venv creation, and `pip install` all run as an ordinary user; skip this step and the very first `tar` fails with `Permission denied`. If you plan to run the service under a dedicated account (for example, `acps`), `chown` directly to that account here rather than to the currently logged-in user.

### 4.3 Unpack and Verify

```bash
cd "$INSTALL_ROOT"
tar -xzf /path/to/registry-server-wheel-2.2.0.tar.gz --strip-components=1
sha256sum -c checksums.txt      # macOS: shasum -a 256 -c checksums.txt
```

`--strip-components=1` lays the contents directly into `$INSTALL_ROOT`, avoiding an extra version-numbered directory level.

### 4.4 Create the venv and Install Third-Party Dependencies

**Every service must have its own venv.** The wheels of the four services registry / ca / discovery / monitor all install under the same top-level package name `app`, so sharing a venv makes them silently overwrite each other.

```bash
cd "$INSTALL_ROOT"
"$PYBIN" -m venv venv          # $PYBIN comes from §4.1.1
./venv/bin/python -m pip install --upgrade pip

./venv/bin/pip install --require-hashes -r requirements-runtime.lock
```

The lock file is a universal export from uv; the same file installs on Linux amd64/arm64 and on macOS, and win32-related packages are skipped automatically by markers (registry-server's lock file has 109 entries, of which 106 are actually installed on Linux). Once the venv is created it is self-contained; all subsequent commands go through `./venv/bin/...` and no longer depend on PATH.

That last command is the slowest step in the whole procedure, entirely dependent on your bandwidth to PyPI — measured at about 50 minutes over a link of roughly 25 kB/s. Installing a second service on the same machine hits the pip cache and is much faster.

If the target machine cannot reach PyPI directly, pre-download on a machine with network access whose **architecture and libc match the target machine**, then copy the files over and install offline:

```bash
# machine with network access
pip download --require-hashes -r requirements-runtime.lock -d ./wheelhouse
# target machine
./venv/bin/pip install --no-index --find-links ./wheelhouse -r requirements-runtime.lock
```

During pre-download, packages without a prebuilt wheel degrade into a source build and require a toolchain. The assembly stage uses a manylinux build container plus auditwheel precisely to eliminate this problem — which is also why this document defaults to online installation.

### 4.5 Install the Application Wheel and Internal Wheels

It must be a **separate command**, and it must carry `--no-deps`. The `--require-hashes` mode requires every entry to have a hash, so mixing them in fails outright; the dependencies were already fully installed from the lock file in the previous step.

```bash
./venv/bin/pip install --no-deps \
  dist/*.whl \
  /path/to/acps-internal-wheels/acps_sdk-*.whl
```

After this step you can only verify that things are "installed"; you cannot yet verify that they "import":

```bash
./venv/bin/pip list | grep -Ei 'acps-sdk|registry-server'
```

**Do not run `import app.main` here.** Each service's `app.core.config` instantiates the configuration object at module level, so `import app.main` immediately reads `.env` — and `.env` is not written until §4.6, so at this point it necessarily raises a pydantic `Field required` error. Put the full import verification after §4.6, and it must be run from within `$INSTALL_ROOT`:

```bash
cd "$INSTALL_ROOT"
./venv/bin/python -c "import app.main, acps_sdk; print('ok')"     # drop acps_sdk for services with no internal wheels
```

### 4.6 Write the Configuration

Configuration has two layers; do not mix them up:

- **`config/*.toml`** — non-sensitive runtime parameters (ports, logging, connection pools, OIDC, CORS). Most entries in the `config/production.toml` shipped in the thin package work out of the box; change what you need. The per-service checklist in §8 points these out.
- **`.env`** — sensitive items and startup parameters that must take effect before the TOML (`APP_ENV` is one of the latter).

```bash
cd "$INSTALL_ROOT"
cp .env.example .env
chmod 600 .env
# Edit .env: APP_ENV=production, fill in DATABASE_URL, etc.
```

`APP_ENV` determines which `config/{APP_ENV}.toml` override file is loaded; for production, set it to `production`.

See §8 for each service's required entries. `.env.example` contains test-only entries such as `TEST_DATABASE_URL`, which you can delete in production.

### 4.7 Key Convention: You Must Start from the Installation Root Directory

The logic by which a service resolves its configuration directory is to **look first at `config/` under the current working directory**, and only fall back to the source tree if it is not found. `.env` is likewise looked up relative to the current directory. Therefore:

- The process's working directory must be `$INSTALL_ROOT`
- For systemd that is `WorkingDirectory=$INSTALL_ROOT`; the same applies to other process managers
- `alembic upgrade` must also be run in this directory

When the working directory is wrong, the service cannot read `.env` and throws a pydantic `Field required` validation error the instant it starts — if you see this error, check the working directory first; do not rush to suspect the environment variables.

### 4.8 Migrate, Start, Health Check

Services that have alembic (registry / ca / discovery / monitor) migrate first:

```bash
cd "$INSTALL_ROOT"
./venv/bin/python -m alembic upgrade head
```

Then start according to the `entrypoint` in `runtime-package.toml`. For a manual foreground check:

```bash
cd "$INSTALL_ROOT"
./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 9001
```

If you wire it into systemd, a minimal unit looks like this — the units the automation installation layer renders from `runtime-package.toml` have exactly this shape:

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

`ExecStart` wraps `/bin/sh -c` so that the command string in `runtime-package.toml` can be used as-is (some services' entrypoint is a shell script).

For the health check use the address declared in `runtime-package.toml`:

```bash
curl -fsS http://127.0.0.1:9001/health
```

The mTLS ports (registry 9002, mq-auth 9007/9008, demo-partner) require a client certificate for liveness probing:

```bash
curl -fsS --cert client.pem --key client.key --cacert trust-bundle.pem \
  https://127.0.0.1:9002/health
```

---

## 5. Deployment Order

There are hard startup dependencies between services; the wrong order leaves them stuck waiting on each other or failing fast.

```text
1. Base dependency components (PostgreSQL / Redis / RabbitMQ / Kafka / Keycloak ...)   §6
2. Decide the trust root, prepare CA material (self-signed root, or request an intermediate CA from a superior root)   §7.1
3. ca-server        ← CA material must be in place before startup
4. registry-server 9001
5. Deploy the acps-cli control side, issue each service's leaf certificate   §7.2
6. registry-server 9002 (mTLS listener)
7. mq-auth-server   ← certificates must be in place before startup; RabbitMQ must first be configured with the HTTP authentication backend
8. discovery-server / monitor-server
9. demo-leader / demo-partner
```

Two shared secrets must be settled before deployment and written into the corresponding services' `.env`:

- `REGISTRY_SERVER_INTERNAL_API_TOKEN`: registry-server and ca-server must be exactly identical
- `AIC_CRC_SALT`: **once set it cannot be changed**; changing it invalidates every historical identity code

---

## 6. Dependency Components: Requirements and Mandatory Wiring

Install them according to your own standards, but the right-hand column of the table below lists ACPs-specific configuration that cannot be omitted.

| Component | Version requirement | Mandatory wiring on the ACPs side |
| --- | --- | --- |
| PostgreSQL | 17 (automation installation layer default) | Create a separate database and account for each of registry / ca / discovery / monitor; the discovery database needs `CREATE EXTENSION vector;` (the pgvector extension must first be installed on the server) |
| Redis | 7+ | monitor's heartbeat uses Redis Functions, so do not use a stripped-down compatible implementation; mq-auth uses it to store ACLs |
| RabbitMQ | 4.2 (automation installation layer default) | Enable AMQPS (5671) and the Management API (15672); you **must** configure the authentication backend as mq-auth-server's HTTP interface, otherwise Agents cannot connect |
| Kafka | Redpanda v26.1.x or an equivalent Kafka | Used by monitor; you pre-create the topics |
| Keycloak | 26.6.x | You must import the three realms `acps-registry` / `acps-monitor` / `acps-leader`; the realm JSON is in `acps-infra/dev-infra/keycloak/realms/` |
| VictoriaMetrics | v1.111.x | monitor's metric storage |
| ClickHouse | 25.5.x | monitor's access logs |
| OpenSearch | 2.19.x | the source of truth for monitor's system logs |
| MinIO | S3-compatible is enough | Needed only when monitor enables cold archiving |

When you install only the five core services (registry / ca / discovery / monitor / mq-auth), PostgreSQL is the only globally required one; for Redis, RabbitMQ, Kafka, and the rest, check the table above service by service.

The discovery pgvector extension is handled automatically before migration by a `scripts/ensure_vector_extension.py`, but **this script is not in the thin package**, so a manual deployment must run the equivalent SQL once itself:

```sql
-- Connect to the discovery database with an account that has permission to create extensions
CREATE EXTENSION IF NOT EXISTS vector;
```

---

## 7. Certificates and PKI

ACPs' mTLS plane is not optional decoration: if ca-server and mq-auth-server cannot find the certificate material at startup, they exit outright.

### 7.1 Prepare the CA Material

ca-server's role in ACPs is an **intermediate CA that issues Agent certificates** (issuing CA): it holds an intermediate CA certificate and the corresponding private key, and uses them to sign every leaf certificate. The intermediate CA certificate itself must be issued by some **root CA**, and that root is the trust anchor for the entire mTLS plane.

So the first thing is not to type commands, but to answer: **where does this root come from?**

| Case | Your situation | What to do |
| --- | --- | --- |
| **A** | No usable superior CA (self-built environment, PoC, isolated network) | Self-sign a root, then use it to sign out an intermediate CA — §7.1.1 |
| **B** | There is already an enterprise / superior root CA (internal PKI, industry CA) | **Generate only the intermediate CA private key and CSR locally, and send the CSR to the superior for issuance**, then take back the intermediate certificate — §7.1.2 |

In both cases what ca-server receives is isomorphic (an intermediate CA certificate + private key + root certificate); the only difference is who signs the intermediate certificate and who safeguards the root private key.

**Do not use Case A in an environment that already has a superior root.** That would conjure up a second trust root out of thin air: the Agent certificates ACPs issues would not be recognized in your existing PKI system, and conversely the certificates your enterprise has already issued would not validate on the ACPs side — in the end the entire mTLS plane has to be re-signed once. Once the trust root is settled and distributed (it is rolled out to every service as `trust-bundle.pem`), the cost of changing it is a full-plane re-sign, so settle it clearly before deployment.

#### 7.1.1 Case A: Self-Signed Root + Self-Signed Intermediate

`acps-infra` has a ready-made script — pure shell + openssl, no Ansible dependency:

```bash
cd /path/to/acps/acps-infra/release/install-packaging
./scripts/generate_ca_materials.sh --out /path/to/ca-materials
```

What it does is exactly two steps: first self-sign a root certificate (`CA:TRUE, pathlen:1`), then use that root to sign out an intermediate CA certificate (`CA:TRUE, pathlen:0`). Outputs:

| File | Purpose |
| --- | --- |
| `ca.crt` / `ca.key` | Intermediate CA certificate and private key; ca-server uses them to sign leaves |
| `root-ca.crt` | Self-signed root certificate, the trust anchor |
| `offline/root-ca.key` | **Root private key**, never to be distributed; keep it offline, or delete it after recording it |

The validity period defaults to 3650 days and can be adjusted with the environment variable `AUTO_GENERATED_CA_VALID_DAYS`. `--out` takes an absolute path; the script does not depend on the current working directory and can be invoked from anywhere.

Before finishing, the script runs `openssl verify` itself to confirm that the intermediate certificate it signed really validates against the root; if not, it fails and exits outright. So as long as it returns successfully, the material is usable and you need not add your own verification.

Leaving the root private key on a networked machine means the whole trust system has no fallback — the script already puts it separately under `offline/` and refuses to leave it in the deployable set, but you still need to move `offline/` to offline media or delete it yourself.

#### 7.1.2 Case B: Intermediate CA Issued by a Superior Root

Core principle: **the intermediate CA private key is generated on the ACPs side and never leaves it; the superior CA sees only the CSR. The superior's root private key likewise never enters ACPs.**

Step one: generate the intermediate CA private key and CSR locally (do not use the Case A script — it creates the root along with it):

```bash
openssl req -new -newkey rsa:4096 -sha256 -nodes \
  -keyout ca.key \
  -out ca.csr \
  -subj "/C=CN/ST=Beijing/L=Beijing/O=<your organization>/OU=Intermediate Certificate Authority/CN=<ACPs issuing CA name>"
chmod 600 ca.key
```

Step two: hand `ca.csr` to the superior CA and **explicitly require issuance as an "intermediate CA / subordinate CA"**, not as an ordinary server certificate. The issued certificate must satisfy:

| Requirement | Value | Why |
| --- | --- | --- |
| `basicConstraints` | `critical, CA:TRUE` (`pathlen:0` is enough) | Without a CA certificate you cannot sign leaves; ca-server signs only leaves and does not need to layer further down |
| `keyUsage` | `critical, keyCertSign, cRLSign` | Both the ability to sign certificates and the ability to sign CRLs are needed; ca-server publishes CRLs |
| Validity period | Covers the maximum validity of leaf certificates | The leaf upper limit is controlled by `[ca].max_certificate_validity_days`, default 1825 days |
| The superior root's `pathlen` | Allows this level | If the root has `pathlen:0`, it cannot sign out an intermediate CA that is itself allowed to issue |

Step three: take back the material and lay it down as three files: store the issuance result as `ca.crt`, the superior **root certificate** as `root-ca.crt`, plus `ca.key` from step one.

Step four: verify once before going on:

```bash
# The intermediate certificate really is issued by this root
openssl verify -CAfile root-ca.crt ca.crt

# It really is a CA certificate, and carries keyCertSign / cRLSign
openssl x509 -in ca.crt -noout -ext basicConstraints,keyUsage

# The private key matches the certificate (the two commands must output the same value)
openssl pkey -in ca.key -pubout -outform DER | openssl dgst -sha256
openssl x509 -in ca.crt -pubkey -noout | openssl pkey -pubin -outform DER | openssl dgst -sha256
```

**When the superior chain has more than two levels** (root → superior intermediate → ACPs intermediate), **append the superior intermediate certificate to `ca.crt`**, in the order "ACPs intermediate certificate first, superior intermediate after"; `root-ca.crt` always contains only the root. That is the only way the `ca-chain.pem` assembled in §7.1.3 is a complete chain. In that case the first verification command above must be changed to pass the intermediate level in explicitly:

```bash
openssl verify -CAfile root-ca.crt -untrusted upper-intermediate.crt acps-ca.crt
```

#### 7.1.3 Assemble the Four Files ca-server Needs

Whether you take A or B, the ca-server `[ca]` section wants **four** files, and above you have prepared only three — `ca-chain.pem` and `trust-bundle.pem` you must assemble yourself (the Case A script actively deletes these two, to avoid leaving stale copies on the control node):

```bash
cd /path/to/ca-materials
cat ca.crt root-ca.crt > ca-chain.pem     # chain: intermediate (including the superior intermediate, if any) → root
cp  root-ca.crt         trust-bundle.pem  # trust anchor: root only
chmod 644 ca.crt root-ca.crt ca-chain.pem trust-bundle.pem
chmod 600 ca.key
```

Then place them according to the paths in the ca-server `[ca]` section; by default that is `certs/` relative to the installation root (these four paths are defined in `config/default.toml`, and `production.toml` does not override them):

| Configuration item | Default path | Content |
| --- | --- | --- |
| `cert_path` | `certs/ca.crt` | Intermediate CA certificate |
| `key_path` | `certs/ca.key` | Intermediate CA private key |
| `chain_path` | `certs/ca-chain.pem` | The complete chain from intermediate to root |
| `trust_bundle_path` | `certs/trust-bundle.pem` | Root certificate only |

**The root private key is not any one of these four files and must never appear on the ca-server host.** In Case B it is with the superior CA anyway, so this is satisfied automatically.

### 7.2 Issue Leaf Certificates

Leaf certificates are issued by acps-cli plus the `scripts/bootstrap_runtime.py` shipped in the thin package. First deploy acps-cli as a control side per §4 (it is a `cli-tool` and needs no migration and no resident process), then:

```bash
cd /opt/acps/acps-cli
./venv/bin/python scripts/bootstrap_runtime.py <profile> \
  --config ./acps-cli.toml \
  --cli-bin ./venv/bin/acps-cli \
  --output-dir /path/to/certs-out \
  --admin-username <registry admin> \
  --admin-password <password>
```

The available `<profile>` values:

| profile | Issued to whom |
| --- | --- |
| `all` | Produces both `registry-9002` and `mq-auth-server` sets at once |
| `registry-9002` | registry-server's mTLS listener |
| `mq-auth-server` | mq-auth-server's two listeners |
| `rabbitmq` | RabbitMQ's AMQPS server side and client side |
| `redis` | Redis TLS |
| `demo-leader` / `demo-partner` | The ATR identity certificates on the demo side |

Issuance requires registry-server to be up and reachable with an administrator account login, so it comes after step 5 of §5 in the ordering.

**Note the semantics of the trust bundle**: `*_CA_CERT_FILE` in the server-side configuration is the trust anchor used to **verify the client certificate chain**, and should normally point to a bundle that contains the root, rather than only the intermediate CA's `ca.crt`; otherwise the handshake may reject the client outright.

---

## 8. Per-Service Checklist

Convention throughout: `$INSTALL_ROOT` is the service's installation root directory, all commands are executed in that directory, and the venv is at `$INSTALL_ROOT/venv`.

### 8.1 registry-server

| Item | Value |
| --- | --- |
| Thin package | `registry-server-wheel-{version}.tar.gz` |
| Internal wheel | `acps-sdk` |
| External dependencies | PostgreSQL; ca-server; optionally Keycloak |
| Migration | `./venv/bin/python -m alembic upgrade head` |
| Component 1 | `uvicorn app.main:app --host 0.0.0.0 --port 9001` → `http://127.0.0.1:9001/health` |
| Component 2 | `python -m app.main_mtls` → `https://127.0.0.1:9002/health` |

**The two components are two independent processes** and must be supervised separately — with systemd that means two service units; with any other process manager it is the same, one supervision entry per process. The 9002 one must use `python -m app.main_mtls` — it builds a `CERT_REQUIRED` TLS context inside the module; swapping in a bare `uvicorn app.main_mtls:app` only starts plaintext HTTP, the port is reachable but there is no mTLS at all.

Required `.env` entries:

```bash
APP_ENV=production
DATABASE_URL=postgresql+asyncpg://registry:<pw>@<host>:5432/agent_registry
SECRET_KEY=<openssl rand -hex 32>
SM4_ENCRYPTION_KEY=<openssl rand -hex 16>    # must be exactly 32 hexadecimal characters
AIC_CRC_SALT=0x<at least two bytes of hexadecimal>   # once set it cannot be changed
REGISTRY_SERVER_INTERNAL_API_TOKEN=<same as ca-server>
CA_SERVER_BASE_URL=http://<ca-host>:9003
```

`SM4_ENCRYPTION_KEY` is the only one with a length check (exactly 32 hexadecimal characters, corresponding to SM4's 128-bit key); get it wrong and startup fails outright. `SECRET_KEY` has no length check in the code, and the 256 bits from `openssl rand -hex 32` are plenty.

Strictly speaking `CA_SERVER_BASE_URL` is an optional override; if you leave it out it falls back to the default value `http://localhost:9003` of `[ca_server].base_url` in `config/*.toml`. If ca-server is not on the same machine you must set it explicitly, otherwise the error only appears when the CA is actually called.

`DATABASE_URL` uses `postgresql+asyncpg://` (the ca-server side uses `postgresql://` — do not mix the two). During alembic migration the framework swaps it for the synchronous driver itself; you do not need to change it by hand. If the password contains URL reserved characters, percent-encode them; the most common case is `#` written as `%23`.

When enabling 9002, add `REGISTRY_SERVER_ENABLE_MTLS_LISTENER=true` and the three `REGISTRY_SERVER_MTLS_*` certificate paths, and confirm that `[server].enable_mtls_listener = true` in `config/production.toml`. Conversely, when only validating 9001 you need not prepare mTLS certificates first — `enable_mtls_listener` is for the separate 9002 process, and having it on does not prevent 9001 from coming up.

### 8.2 ca-server

| Item | Value |
| --- | --- |
| Thin package | `ca-server-wheel-{version}.tar.gz` (the wheel name is `agent_ca_server`) |
| Internal wheel | none |
| External dependencies | PostgreSQL; registry-server |
| Migration | `./venv/bin/python -m alembic upgrade head` |
| Component | `uvicorn app.main:app --host 0.0.0.0 --port 9003` → `http://127.0.0.1:9003/health` |

**The CA material must be in place before startup.** During startup the service loads the `cert_path` / `key_path` / `chain_path` / `trust_bundle_path` pointed to by the `[ca]` section, and a missing file fails outright. These four paths are defined in `config/default.toml` (`production.toml` overrides only the three URLs; the paths are inherited through deep merge). For how to prepare the four files see §7.1; note that the last two you must assemble yourself (§7.1.3).

Required `.env` entries:

```bash
APP_ENV=production
DATABASE_URL=postgresql://ca:<pw>@<host>:5432/agent_ca
REGISTRY_SERVER_INTERNAL_API_TOKEN=<same as registry-server>
CA_SERVER_ADMIN_API_TOKEN=<strong random string>

# Certificate discovery addresses: the values depend on the hostname reachable from outside for this deployment, so there is no default
ACME_DIRECTORY_URL=https://<ca-host>:9003/acps-atr-v2/acme
OCSP_RESPONDER_URL=https://<ca-host>:9003/acps-atr-v2/ocsp
CRL_DISTRIBUTION_POINT_URL=https://<ca-host>:9003/acps-atr-v2/crl/current
```

The last three must be filled with hostnames that the ACME client and certificate consumers **can actually reach**; `localhost` / `127.0.0.1` will not pass validation. If you omit them, `import`, `alembic upgrade`, and `uvicorn` all fail-fast together, and the error names which variable should be injected:

```text
ca.ocsp_responder_url must be explicitly configured to an externally reachable hostname in production (inject OCSP_RESPONDER_URL)
```

This is deliberate: the OCSP and CRL addresses get written into every certificate that is issued, and changing them after the fact requires a full-plane re-sign, so it is better to fail to start than to issue a batch of certificates pointing at the wrong addresses. Settle these three addresses before deployment.

### 8.3 discovery-server

| Item | Value |
| --- | --- |
| Thin package | `discovery-server-wheel-{version}.tar.gz`, **architecture-dependent** |
| Internal wheel | `acps-sdk` |
| External dependencies | PostgreSQL + pgvector; external Embedding / LLM API; registry-server |
| Lock file | `requirements-runtime-{cpu\|gpu}-{arch}.lock`, not `requirements-runtime.lock` |
| Migration | `./venv/bin/python -m alembic upgrade head` (**prerequisite**: create the vector extension by hand first) |
| Component | `uvicorn app.main:app --host 0.0.0.0 --port 9005` → `http://127.0.0.1:9005/health` |

When installing dependencies in §4.4, swap the file name for the actual variant lock file:

```bash
./venv/bin/pip install --require-hashes -r requirements-runtime-cpu-amd64.lock
```

Required `.env` entries (CPU mode):

```bash
APP_ENV=production
DISCOVERY_MODE=cpu                # must match the thin package's variant
DATABASE_URL=postgresql+asyncpg://discovery:<pw>@<host>:5432/agent_discovery
EMBEDDING_API_KEY=<key>
EMBEDDING_BASE_URL=<endpoint>
EMBEDDING_MODEL_NAME=<model>
DISCOVERY_LLM_API_KEY=<key>
DISCOVERY_LLM_BASE_URL=<endpoint>
DISCOVERY_LLM_MODEL_NAME=<model>
```

GPU mode (`DISCOVERY_MODE=gpu`) switches to a local BGE-M3 inference stack and needs no external Embedding API, but the target machine must have a CUDA runtime and the thin package must be the GPU variant.

### 8.4 monitor-server

| Item | Value |
| --- | --- |
| Thin package | `monitor-server-wheel-{version}.tar.gz` |
| Internal wheel | `acps-sdk` |
| External dependencies | PostgreSQL, Redis, Kafka, VictoriaMetrics, ClickHouse, OpenSearch; optionally MinIO, Keycloak |
| Migration | `./venv/bin/python -m alembic upgrade head` |
| Component | `uvicorn app.main:app --host 0.0.0.0 --port 9009` → `http://127.0.0.1:9009/health` |

This is the one with the largest infrastructure footprint. `/health` probes PostgreSQL, Redis, VictoriaMetrics, and ClickHouse at the same time, and returns 503 if any one of them is unreachable — get those four connected first, then look at the service itself. Each subsystem can be trimmed in `config/*.toml` with the `*_enabled` switches.

Required `.env` entries:

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

| Item | Value |
| --- | --- |
| Thin package | `mq-auth-server-wheel-{version}.tar.gz` |
| Internal wheel | none |
| External dependencies | Redis; RabbitMQ |
| Migration | **none**; this service does not use alembic |
| Component | `mq-auth-server` (console script, under `venv/bin/`) → ports 9007, 9008 |

One process forks two uvicorn listeners internally (9007 Group API, 9008 Auth API), and **both enforce mTLS**, so missing certificates fail startup outright.

The health check must also go over mTLS: simply changing `http` to `https` is not enough, you also have to carry a client certificate the server accepts, so a bare `curl` cannot probe it. The thin package ships a ready-made probe:

```bash
cd "$INSTALL_ROOT"
HEALTHCHECK_TLS_CERT_FILE=<client certificate> \
HEALTHCHECK_TLS_KEY_FILE=<client private key> \
HEALTHCHECK_TLS_CA_CERT_FILE=<trust bundle containing the root> \
./venv/bin/python -m app.core.health_probe --url https://127.0.0.1:9007/health
```

As long as `APP_ENV` is not `development`, the first two variables are required, and without them the probe reports `HEALTHCHECK_TLS_CERT_FILE and HEALTHCHECK_TLS_KEY_FILE are required` outright. The client certificate must be a peer identity this service is willing to accept (§7.2); you cannot substitute a server certificate. The probe requires TLS 1.3, and a non-zero exit code means unhealthy.

Required `.env` entries:

```bash
APP_ENV=production
REDIS_URL=redis://<host>:6379/0
RABBITMQ_MGMT_URL=http://<host>:15672
RABBITMQ_MGMT_PASS=<pw>
TLS_CERT_FILE=<path>/server.pem
TLS_KEY_FILE=<path>/server.key
TLS_CA_CERT_FILE=<path>/trust-bundle.pem
```

The `acs/` directory in the thin package holds ACS descriptors; do not delete it.

### 8.6 demo-leader

| Item | Value |
| --- | --- |
| Thin package | `demo-leader-wheel-{version}.tar.gz` |
| Internal wheel | `acps-sdk`, `acps-cli` |
| External dependencies | RabbitMQ + mq-auth-server; discovery-server; Keycloak (realm `acps-leader`); LLM API |
| Migration | none |
| Component 1 | `scripts/start-leader-api.sh` → `http://127.0.0.1:9031/api/v1/health` |
| Component 2 | `scripts/start-web-ui.sh` → 9030 (static Web) |

The startup entry points are shell scripts shipped in the thin package (they already carry the execute bit); internally they bring up `python -m leader.main` and `python -m webapp.webserver` respectively. The scripts default to using the parent of their own directory as the runtime root; when business data must live elsewhere, override with `LEADER_RUNTIME_ROOT` / `LEADER_SCENARIO_ROOT` / `LEADER_CONFIG_FILE`.

The main configuration is `leader/config.toml` (ports, OIDC, RabbitMQ, discovery addresses, mTLS paths), and `.env` holds only the LLM keys:

```bash
APP_ENV=production
LEADER_LLM_DEFAULT_API_KEY=<key>
LEADER_LLM_DEFAULT_BASE_URL=<endpoint>
LEADER_LLM_DEFAULT_MODEL=<model>
# Configure the FAST / PRO tiers as needed
```

Outbound access to Partner and Discovery requires the mTLS client certificates under `leader/atr/` — at packaging time the thin package **deliberately strips out** all `*.pem` / `*.key` files, so re-issue them with the `demo-leader` profile from §7.2 and put them back.

### 8.7 demo-partner

| Item | Value |
| --- | --- |
| Thin package | `demo-partner-wheel-{version}.tar.gz` |
| Internal wheel | `acps-sdk`, `acps-cli` |
| External dependencies | RabbitMQ (vhost `acps`); LLM API |
| Migration | none |
| Component | `python -m partners.main` → ports 9021–9025 |

One process brings up several uvicorns according to the number of directories under `partners/online/`, one port per Agent, each with HTTPS + client certificate verification. The `config.toml` in each Agent directory defines the port and mTLS, and `atr/acs.json` is the identity description. The certificates are stripped as well; issue them with the `demo-partner` profile.

`.env` holds only the LLM keys (`PARTNER_LLM_*`).

### 8.8 acps-cli (control side)

| Item | Value |
| --- | --- |
| Thin package | `acps-cli-wheel-{version}.tar.gz` |
| Internal wheel | `acps-sdk` |
| Type | `cli-tool`, not a resident service; no port and no health check |
| Entry point | `venv/bin/acps-cli` |

Install it per §4.1–4.6 and you are done. It is not a resident service, so it needs neither a database migration nor any process supervision configured for it (a systemd unit or the like) — just type the command when you need it. It takes on two things: issuing certificates (§7.2) and day-to-day operations work. Configuration is in `acps-cli.toml`; for the commands see the [CLI reference](../references/cli-reference_en.md).

---

## 9. When Something Goes Wrong, Look Here First

| Symptom | What you can do |
| --- | --- |
| `python3.14: command not found` | Debian/Ubuntu add `~/.local/bin` to PATH only in a login shell, so non-interactive SSH and systemd cannot see it; use the absolute path of `$PYBIN` from §4.1.1 |
| Unpacking / creating the venv / `pip install` reports `Permission denied` | A directory created by `sudo mkdir` is owned by root and the `chown` was missed (§4.2) |
| pydantic `Field required` at the instant of startup | Two possibilities: the process CWD is not the installation root directory so `.env` cannot be read (§4.7), or you ran `import app.main` before writing `.env` in §4.6 (§4.5) |
| `ModuleNotFoundError: acps_sdk` | You missed the internal wheel from §3; the fact that it is not in the lock file is by design |
| `pip install` reports `hashes are required` | The internal wheel and the lock file were mixed into one command; install them separately and add `--no-deps` (§4.5) |
| You install service A and then service B, and A breaks | The two services shared a venv; registry/ca/discovery/monitor all use `app` as the top-level package name, so it must be one venv per service |
| Dependencies start compiling source from scratch on site | The target platform has no prebuilt wheel; switch to a matching architecture, or use an application release package with an assembled wheelhouse instead |
| The discovery migration reports that the vector type does not exist | The pgvector extension was not created in the database (§6) |
| ca-server / mq-auth-server exit immediately on startup | Certificate material is missing; these two services are designed to fail-fast (§7); ca-server wants **four** files, and `ca-chain.pem` / `trust-bundle.pem` must be assembled by you (§7.1.3) |
| ca-server reports `must be explicitly configured to an externally reachable hostname` | The certificate discovery addresses are missing from `.env`; the parentheses in the error name which variable should be injected (§8.2); fill in an externally reachable hostname — `localhost` will not do |
| Certificates issued by ACPs are not recognized in the enterprise's existing PKI | The wrong trust root was chosen: the environment already had a superior root but a self-signed root was used (§7.1); the only fix is to switch to an intermediate CA issued by the superior and re-sign the whole plane |
| `openssl verify -CAfile root-ca.crt ca.crt` fails | The superior chain has more than two levels and the intermediate level is missing; pass the superior intermediate with `-untrusted` and append it to `ca.crt` (§7.1.2) |
| Port 9002 is reachable but there is no mTLS | The start command was written as a bare uvicorn; it must be `python -m app.main_mtls` (§8.1) |
| The mTLS handshake is rejected by the server | The server's `*_CA_CERT_FILE` contains only the intermediate CA; switch to a bundle that includes the root (§7.2) |
| Agents cannot connect to RabbitMQ | RabbitMQ was not configured with mq-auth-server as the HTTP authentication backend (§6) |
| monitor `/health` returns 503 | Check PostgreSQL / Redis / VictoriaMetrics / ClickHouse one by one |
| The database password contains `#` and connections fail or the password is truncated | `#` starts a fragment in a URL, so it must be written as `%23` in the DSN. Separately, in `.env` a `#` is treated as an inline comment only when **preceded by a space**; when it sits flush against the password it is not — two different traps |
| The service will not start after switching to a dedicated account | The venv records the absolute path of the interpreter that created it; do not install the interpreter under some user's HOME — install it to `/opt` (§4.1.1) |

---

## 10. Routine Actions After Installation

Upgrades, certificate renewal, and trust bundle refreshes are all essentially redoing some of the steps in §4 through §7. The manual approach is as follows; the playbooks in [day2-ops](./install-package-day2-ops_en.md) are the automated version of these passages, and when something goes wrong you can use them as a reference while intervening by hand:

- **Upgrade**: unpack the new version under `releases/{version}/` with its own venv, restart after switching the `current` symlink, and switch the symlink back if it fails. The automation installation layer uses exactly this `releases/` + `current` layout, which is why §4.2 suggests following its directory convention.
- **Certificate renewal**: re-run the corresponding profile from §7.2, replace the files, then restart the affected services.
- **Configuration change**: edit `config/*.toml` or `.env` and restart; neither supports hot reload.

When the number of machines grows and you need idempotent replay and multi-machine orchestration, you can switch to the automation installation layer without changing the deployment semantics — the directory layout, systemd units, migration commands, and certificates it installs are all consistent with this document, only a playbook does the work on your behalf. To go that route, start the assembly from the [application release package](./app-release-package-build_en.md); the thin package is likewise its upstream input.
