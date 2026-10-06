[Home](../README.md)

**[English](install-package-ansible-deploy_en.md) | [中文](install-package-ansible-deploy.md)**

# Deploying ACPs with an Installation Package (Ansible: image / host)

This tutorial is for readers who **know basic Linux operations but have never touched Ansible**. You already have an installation package (`acps-image-install-*.tar` or `acps-host-install-*.tar`) and want to **install the full ACPs stack from scratch** (including Keycloak OIDC).

For how the installation package is produced, see [Assembling an Installation Package](./install-package-build_en.md).

What the playbook automates is the ACPs baseline deployment flow — installing dependencies, laying down configuration, migrating databases, issuing certificates, starting processes in order, and running liveness probes. To understand exactly what each step does, or to intervene manually when a step fails, consult [Manual Deployment from an Application Thin Package](./manual-deploy-from-app-thin-package_en.md).

```text
Source → application release package ─┬→ [image] image package  → image installation package ─┐
                                      └→ [host]  vendor        → host installation package   ─┴→ This tutorial (Ansible)
```

**Shared by both modes**: Ansible concepts, unpacking conventions, `secrets.yml`, CA materials, `preflight` / `site.yml` / `business.yml`, and the demo Web entry point **`:9030`** (same-origin `/api`).  
**Where the two modes differ**: business machine prerequisites, inventory examples, files inside the package, and how to check the result after installation. These are marked in the text with **〔image〕** / **〔host〕**.

---

## 0. Choose the Mode First

| | **〔image〕** | **〔host〕** |
| --- | --- | --- |
| Installation package | `acps-image-install-*.tar` | `acps-host-install-*.tar` |
| `acps_deploy_mode` | `image` (package default / `hosts.example`) | `host` (`hosts.rocky9.yml` / `hosts.ubuntu22.yml` / `hosts.rocky8.yml` / `hosts.ubuntu20.yml` are already written) |
| Business machine | Docker Engine + Compose v2 | Rocky 8/9 **or** Ubuntu 20.04/22.04; systemd; do **not** expect to run business processes with Docker |
| Control node on the same machine as the business node | Supported (§4.7, common for local acceptance on a Mac) | **Not recommended / not supported for formal delivery** (business machines support only Linux servers: Rocky 8/9 / Ubuntu 20.04/22.04) |
| Upgrade playbook | `upgrade.yml` / `rollback.yml` are productized (image + host) | Same as the left |

Once you have chosen, every step below that is not marked 〔image〕/〔host〕 is **performed the same way in both modes**.

---

## 1. First Understand How Ansible Does Its Work (30-Second Version)

Ansible is a tool that **runs on one machine and remotely directs other machines to install software**. Just remember a few points:

1. **Control node (Control Node)**: the machine where you type commands, with Ansible installed. It logs in to business nodes over **SSH** to do the work.
2. **Business node (Managed Node / target machine)**: the machine that actually runs ACPs. **Ansible does not need to be installed on it**.
3. **Inventory**: it states which machines exist and which role each machine plays.
4. **Playbook**: the installation steps executed in order (in this project, `site.yml`).
5. **Variables**: `group_vars/all.yml` (global defaults), `host_vars/<machine name>.yml` (connection and paths), `secrets.yml` (passwords and secrets).
6. **Idempotence**: running the same playbook repeatedly yields the same result — **a run fails, you fix the configuration, and you run it again** is normal practice.

```text
┌──────────────────────────────┐       SSH        ┌────────────────────────────────┐
│  Control node (your machine) │ ───────────────▶ │  Business node                 │
│  • Ansible                   │  service port    │  〔image〕Docker + Compose     │
│  • acps-cli (auto-installed) │  access          │  〔host〕venv/systemd + OS pkgs│
│  • issues component certs    │ ◀─────────────── │    + vendor (Keycloak/AMP)     │
└──────────────────────────────┘                  └────────────────────────────────┘
```

> The control node itself does **not** need to install Ansible on the business nodes.  
> 〔image〕The control node does **not need Docker**; 〔host〕the control node is usually a macOS/Linux workstation, **not** the Rocky/Ubuntu business machine itself.

---

## 2. What Each of the Two Machine Types Must Prepare

### 2.1 Control Node (Shared)

- **Ansible**: `ansible-core` **≥ 2.16** (2.18.x recommended), and the Python that runs `ansible-playbook` must be **≥ 3.11** (`tomllib`, required by `manifest_lookup.py`).
- **Recommended one-command install** ([uv](https://docs.astral.sh/uv/) must already be installed): after unpacking, run  
  `"$PKG/scripts/bootstrap_control_ansible.sh"`  
  (`uv tool install` installs `ansible-core` into CPython 3.14; failure messages also point to that script).
- **uv** (recommended): required by the bootstrap above; it also provides Python 3.14 for the automatically installed `acps-cli`.
- **Passwordless SSH login to the business nodes**, and that user must be able to `sudo` (to write `/opt/acps` and the like).
- **The network must be able to reach the service ports on the business nodes** (for issuing certificates and running acps-cli).

### 2.2 Business Node (Requirements Differ by Mode)

**〔image〕**

- Docker Engine + Compose v2 (`docker compose version` works).
- The deployment user must be able to run `docker` (usually by joining the `docker` group and logging in again; when sudo alone is not enough, `become` inside `ansible` also cannot reach a passwordless docker socket).
- The system `python3` merely needs to be available (3.11 is not required).
- The CPU architecture must match the installation package (`linux-amd64` / `linux-arm64`).

**〔host〕**

- Supported operating systems: **Rocky Linux 8/9** or **Ubuntu 20.04/22.04** (preflight fails on other distributions). On Ubuntu 20 PostgreSQL comes from apt-archive; on Rocky 8 Redis comes from Remi; on Ubuntu 20 RabbitMQ is pinned to `4.2.8-1` (focal erlang ≤26).
- **systemd**; passwordless sudo.
- **Online OS repositories must be reachable**: PostgreSQL (PGDG + pgvector), Redis ≥ 7 (commonly `packages.redis.io` on Ubuntu), and the official RabbitMQ repository; the `tools/` directory inside the installation package **cannot** replace these apt/dnf repositories.
- **Keycloak Java**: the host installation package ships Temurin JRE17 (`JAVA_HOME`), so no online OpenJDK installation is needed; the OpenSearch tarball bundles its own JDK.
- The architecture must match `--target-platform` / the package name.

**Shared by both modes — egress / interconnect MTU:**

- When reaching the LLM gateway through a tunnel, if "small requests get through but large prompts time out", set the network interface **MTU to ≤1400** (a site-network item). The image-mode installer sets the MTU of the shared `acps-net` network to `acps_docker_mtu` (default 1400); on a **Linux Docker Engine** it additionally merges this into `/etc/docker/daemon.json`. **Docker Desktop** (including macOS) does not write `daemon.json`, but it still manages `acps-net`; you must still set the host's egress MTU to ≤1400 yourself. If the machine already has an old network whose MTU does not match, add `-e acps_docker_network_recreate=true`.
- With **multiple business nodes**: cross-machine PostgreSQL migration and large SQL packages can likewise stall because the MTU is too high; it is recommended to set the business network interface on every business machine uniformly to **MTU ≤1400** (do not wait until business acceptance to adjust it).

**〔image〕Multi-machine firewall:** the installation playbook does **not** allow ports through with `firewall-cmd` / `ufw` automatically the way host mode does. Before going cross-machine, open each component's advertised ports yourself (see the port table in [Three-Business-Node Deployment](./install-package-ansible-deploy-3nodes_en.md)), or temporarily allow them through to verify connectivity first.

### 2.3 Quick Self-Check

```bash
# Control node (shared) — bootstrap first is recommended
"$PKG/scripts/bootstrap_control_ansible.sh"   # needs uv; installs ansible-core≥2.16 + Python 3.14
export PATH="$HOME/.local/bin:$PATH"
ansible --version          # core >= 2.16; python version >= 3.11
ssh <business-node-user>@<business-node-address> 'echo ok && sudo -n true && echo sudo-ok'

# 〔image〕business node
docker version && docker compose version && python3 --version

# 〔host〕business node
cat /etc/os-release        # Rocky 8/9 or Ubuntu 20.04/22.04
systemctl --version | head -1
```

---

## 3. Copy the Installation Package to the Control Node and Unpack It

Do this on the **control node**. What you get after unpacking is a self-contained directory carrying `ansible/`, `artifacts/`, and `release-manifest.toml`; do **not** run it inside a git source tree.

```bash
# 1) Transfer it to the control node (example)
scp acps-image-install-2.2.0-linux-amd64.tar <you>@<control-node>:~/
# or: scp acps-host-install-2.2.0-linux-amd64.tar ...

# 2) Unpack
mkdir -p ~/acps-deploy && cd ~/acps-deploy
tar -xf ~/acps-*-install-*.tar
cd acps-*-install-*-linux-*          # referred to as $PKG below; use tab completion for the real directory name
```

What is in the package:

| Directory / file | 〔image〕 | 〔host〕 |
| --- | --- | --- |
| `ansible/` | Deployment playbooks, inventory examples | Same as the left (shared tree) |
| `artifacts/images/` | Present: `*.image.tar.gz` | **Absent** |
| `artifacts/apps/` | Usually absent | Present: business app-release |
| `artifacts/vendor/` | Absent | Present: Keycloak / AMP / fluent-bit and so on |
| `artifacts/control/` | Control-node `acps-cli` release package | Same as the left |
| `baseline-matrix.toml` | Absent | Present |
| `bin/acps-install` | May exist but is not used | **Absent**; always use `ansible-playbook` |
| `scripts/` | Includes CA self-signing and so on | Same as the left |

The canonical working directory: the unpacked **`$PKG/ansible/`**.

---

## 4. Configuration (the Important Part)

Everything you need to change is under `ansible/inventories/`: `hosts.yml` (or an OS-specific inventory), `host_vars/`, `secrets.yml`, and **`ca-materials/`**.

> **The default is "all components + Keycloak"**: most of the relevant switches in `group_vars/all.yml` are `true`. Your main job is to get the machine addresses and the passwords right.

### 4.1 Inventory: Point Out the Business Nodes (Different Files per Mode)

**〔image〕** — start from the generic example:

```bash
cd "$PKG/ansible"
cp inventories/hosts.example.yml    inventories/hosts.yml
cp inventories/secrets.example.yml  inventories/secrets.yml
chmod 600 inventories/secrets.yml
```

The example maps every component group to `acps-node-1`. To install everything on a single business node, simply rename it globally:

```bash
sed -i.bak 's/acps-node-1/acps-biz-1/' inventories/hosts.yml
```

After editing it looks like this (every group points to the same machine; **keep** `acps_deploy_mode`):

```yaml
all:
  vars:
    acps_deploy_mode: image
  children:
    postgresql:   { hosts: { acps-biz-1: } }
    redis:        { hosts: { acps-biz-1: } }
    # ... other component groups ...
    demo_leader:  { hosts: { acps-biz-1: } }
```

> Rule: **every enabled component group must land on exactly 1 host**. For splitting across multiple machines, see [Three-Business-Node Deployment](./install-package-ansible-deploy-3nodes_en.md).

**〔host〕** — use the in-package OS example (it already sets `acps_deploy_mode: host`):

```bash
cd "$PKG/ansible"
cp inventories/secrets.example.yml inventories/secrets.yml
chmod 600 inventories/secrets.yml

# Rocky 9 example (hosts.rocky8.yml / hosts.ubuntu20.yml / hosts.ubuntu22.yml also work)
cp inventories/hosts.rocky9.yml inventories/hosts.yml
# Change ansible_user, host name / ansible_host to match reality (the example defaults to rhel9.acps.local)
```

You can also pass `-i inventories/hosts.rocky9.yml` (or another OS example) directly instead of copying it to `hosts.yml`.  
**Note**: with the host examples, do **not** set `ansible_become: true` globally in the inventory (it contaminates the control node's `delegate_to: localhost`); become is enabled by the playbook/role as needed. Just set `ansible_become: true` for the business machine in the corresponding `host_vars`.

### 4.2 `host_vars/<host name>.yml`: How to Connect to Business Machines (Shared Skeleton)

The host name must match the inventory. Remote single-machine example:

```yaml
ansible_host: 10.0.0.11
ansible_user: deploy
ansible_become: true
ansible_python_interpreter: /usr/bin/python3

# Must be an address the control node / other nodes can actually reach (certificate SANs and acps-cli both rely on it)
acps_advertise_host: 10.0.0.11

# Default paths (used together with become)
# acps_runtime_root: /opt/acps
# acps_data_root:    /var/lib/acps
# acps_log_root:     /var/log/acps
```

Key points:

- **`acps_advertise_host`**: certificate SANs, the ACS external HTTPS endpoint, and the control node's CLI/smoke tests all rely on it.
  - 〔image〕`127.0.0.1` / `localhost` / an empty value are **forbidden** (preflight fails). Even on a single machine you must use a resolvable host name or a LAN IP — Leader/Partner/Keycloak are separate containers, and loopback is unreachable.
  - 〔host〕On a single machine `127.0.0.1` is allowed, though a host name is still recommended; with **multiple machines loopback is forbidden** (preflight fails).
  - For ACS rewriting and the three-machine checklist, see [Three Nodes §7](./install-package-ansible-deploy-3nodes_en.md#7-acs-and-certificate-address-checks-shared-by-image-and-host).
- 〔image〕Inside the package you can refer to `host_vars/docker.acps.local.example.yml`.  
- 〔host〕Example host names are usually `rhel9.acps.local` / `ubuntu22.acps.local`; the `hosts.*.yml` files already carry part of the connection information, which you can adjust for your environment or supplement in `host_vars`.

### 4.3 `secrets.yml`: Replace Every `CHANGE_ME` with a Real Value (Shared)

Preflight **rejects** core secrets that are still `CHANGE_ME`.

The **passwords and user names** interpolated into `REDIS_URL` / `DATABASE_URL` / AMQP must not contain `# @ : / ?` or whitespace (preflight rejects them; although the templates URL-encode, it is still recommended to use only alphanumerics and `. _ - !`).

| Category | Key | Notes |
| --- | --- | --- |
| Database | `postgresql_superuser_password`, `registry_db_password`, `ca_db_password`, `discovery_db_password`, `monitor_db_password`, `keycloak_db_password` | Password for each database; must not contain URL-reserved characters (such as # @ : / ?) |
| Redis / RabbitMQ | `redis_password`, `rabbitmq_password`, `mq_auth_mgmt_pass` | Messaging and caching; must not contain URL-reserved characters |
| Registry | `registry_secret_key` (**64-digit hex**), `registry_server_internal_api_token`, `registry_admin_password`, `registry_bootstrap_password` | Be sure to change these |
| **Keycloak** | `keycloak_admin_password`, `monitor_oidc_admin_password` | Administrator and Monitor OIDC |
| Heartbeat sync | `monitor_heartbeat_sync_internal_token` | **Length ≥ 16**; required when Monitor is enabled |
| Object storage / search | `minio_root_password`, `opensearch_initial_admin_password` | AMP (the OpenSearch password has complexity requirements) |
| LLM (optional) | `discovery_llm_*`, `embedding_*`, `demo_llm_*`, plus the per-tier `demo_{leader,partner}_llm_{fast,default,pro}_*` | A real key is needed only to run **business acceptance**; `embedding_dim` defaults to **1024**. Changing only `demo_llm_*` is **not enough**: demo rendering reads the tier fields first (`*_api_key` / `*_base_url` / `*_model`); after changing the LLM you must re-render the affected templates and then run `business.yml` |

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"      # registry_secret_key
python3 -c "import secrets; print(secrets.token_urlsafe(24))"  # general token
```

> When `keycloak_enabled: true`, authentication goes through OIDC; the realm / client import is done automatically during installation, so you **only need to fill in the passwords correctly**.  
> If business acceptance Step B fails intermittently, just rerun `business.yml` (no need for a full clean slate on the machine).

### 4.4 `ca-materials/`: CA Intermediate Certificates (Shared)

You need `ca.crt`, `ca.key` (chmod 600), and `root-ca.crt`, placed in `inventories/ca-materials/` on the control node. **Do not put the root private key here.**

**A. Production**: sign an intermediate CA with the offline root, then copy in the three files.  
**B. Test / demo**:

```bash
cd "$PKG/ansible"
mkdir -p inventories/ca-materials
"$PKG/scripts/generate_ca_materials.sh" --out inventories/ca-materials/
# If the directory already contains ca.crt / ca.key / root-ca.crt (pre-seeded in the package or generated last time), add --force:
# "$PKG/scripts/generate_ca_materials.sh" --out inventories/ca-materials/ --force
```

The self-signed root private key is in `inventories/ca-materials/offline/`; keep it offline or delete it. Preflight validates that the materials are self-consistent.

### 4.5 `group_vars/all.yml`: Usually No Changes Needed

- **Deployment mode**: it follows the installation package / inventory (do not use an image package as a host one, and vice versa).
- **Control-node work root**: `acps_control_root` defaults to `~/.local/share/acps/control` (role defaults, **not** hard-coded in `group_vars/all.yml`). When running several topologies in parallel or avoiding collisions on `control/work`, set an independent path in `all.vars` of `hosts.yml`, or use `-e acps_control_root=...`.
- **Platform slug**: the build script has already set it per package.
- **discovery variant**: defaults to `cpu`; change it to `gpu` only for GPU images/release packages.
- **Ports**: change them only on conflict (for example `demo_leader_web_port: 9030`).
- Installing fewer components: set the corresponding `*_enabled: false` (this tutorial assumes a full install by default).

### 4.6 Configuration Self-Check: Preflight

```bash
cd "$PKG/ansible"
export ANSIBLE_CONFIG="$PKG/ansible/ansible.cfg"
ansible-playbook playbooks/preflight.yml -i inventories/hosts.yml -e @inventories/secrets.yml
# 〔host〕this also works: -i inventories/hosts.rocky9.yml (or rocky8 / ubuntu20 / ubuntu22 / 4os-multi)
```

Shared checks: Ansible version, secrets, CA, exactly 1 host per group, writable directories, and so on.  
**〔image〕** additionally verifies Docker / Compose.  
**〔host〕** also checks whether the operating system is Rocky 8/9 or Ubuntu 20.04/22.04, systemd, whether the system software repositories are reachable, whether the installation package carries vendor, and so on.

### 4.7 Only One Machine? (Control Node = Business Node)

**〔image〕** is supported (common for local acceptance on Mac Apple Silicon):

1. Keep the host name `acps-node-1` (do not do the remote renaming from §4.1).
2. Use the in-package `host_vars/acps-node-1.yml` (`ansible_connection: local`, user home directory paths, and so on).
3. You **must** set a non-loopback `acps_advertise_host` (the machine's LAN IP or a resolvable name). By default the package reads it from an environment variable:
   `export ACPS_ADVERTISE_HOST=$(ipconfig getifaddr en0)` (adjust to your actual network interface), or edit `host_vars/acps-node-1.yml` directly. Preflight rejects `127.0.0.1` / an empty value.
4. You still need to do §4.3 secrets and §4.4 CA.
5. Docker Desktop does not modify `/etc/docker/daemon.json`; the installer still sets the `acps-net` MTU to `acps_docker_mtu`. Please set the host's egress MTU to ≤1400 yourself.

```bash
cd "$PKG/ansible"
cp inventories/hosts.example.yml inventories/hosts.yml
cp inventories/secrets.example.yml inventories/secrets.yml
chmod 600 inventories/secrets.yml
# Apple Silicon example: replace en0 with your business network interface
export ACPS_ADVERTISE_HOST="$(ipconfig getifaddr en0)"
test -n "$ACPS_ADVERTISE_HOST"  # must be non-empty
"$PKG/scripts/generate_ca_materials.sh" --out inventories/ca-materials/
# add --force when materials already exist, see §4.4
```

**〔host〕**: the product acceptance path uses a **standalone Linux business machine** (Rocky/Ubuntu). Do not treat a macOS control node as a host business machine.

---

## 5. Running the Deployment

### 5.0 Choose the Right Entry Point First

| Target machine state | What to run | Notes |
| --- | --- | --- |
| **Blank machine** or a dedicated verification machine | `playbooks/site.yml` | Install from scratch |
| **An already installed environment** that needs a version change | `upgrade.yml` (it prints the component list; the same entry point for image / host) | Do **not** use the complete `site.yml` as a routine upgrade; for the steps see [Day-2 Operations](./install-package-day2-ops_en.md) |
| **You want to install it again from scratch for acceptance** | Do a destructive clean slate as described in the [Clean Slate tutorial](./install-package-clean-slate_en.md), then run `site.yml` | Or switch to a clean target machine |

**〔image〕/〔host〕clean slate**: the checklist, gate commands, and prohibitions are in [Clean Slate Before Acceptance/Reinstall](./install-package-clean-slate_en.md) (including deleting units, recreating `/var/lib/redis|rabbitmq`, and isolating `acps_control_root`). Do **not** treat `docker compose down -v` as a routine operation. If the same tag behaves like an older version, add `-e acps_force_image_load=true` while troubleshooting.

### 5.1 Run `site.yml`

```bash
cd "$PKG/ansible"
export ANSIBLE_CONFIG="$PKG/ansible/ansible.cfg"

ansible-playbook playbooks/site.yml -i inventories/hosts.yml -e @inventories/secrets.yml
```

Installation proceeds roughly in this order (the orchestration is identical in both modes): preflight → PostgreSQL → Keycloak → Registry → CA → control-node `acps-cli` → Discovery → Redis/RabbitMQ → mq-auth → monitoring and AMP → baseline smoke → demo-partner → **demo-leader (including Web)** → AMP Forwarder → heartbeat sync check.

**demo Web (identical in both modes)**: open **`http://<advertise-host>:9030/`** in a browser; the process itself provides a same-origin reverse proxy for `/api/v1/`, with `backendBase=''`; there is **no** separate `demo-nginx`.

After installation you can confirm it like this:

```bash
curl -fsS "http://<reachable-business-node-address>:9030/api/v1/health"
# 〔image〕
docker ps --format '{{.Names}}' | grep -E 'demo_leader'   # demo_leader / demo_leader_web present; no nginx
# 〔host〕
ssh <business-machine> 'systemctl is-active acps-demo_leader_web.service'   # unit name per the actual installation
```

> Standard practice: fix the configuration and **rerun the same command** (idempotent).  
> You can also run `playbooks/smoke.yml` again. A playbook exit code of 0 means the baseline smoke tests have passed.

---

## 6. Business Acceptance (`business.yml`) (Shared)

The fixed sequence is **A→B→C→D**, executed in order with fast failure. Requirements: `demo_*` enabled, a real LLM/embedding key, and Monitor + Discovery enabled.

```bash
cd "$PKG/ansible"
ansible-playbook playbooks/business.yml -i inventories/hosts.yml -e @inventories/secrets.yml
# expected: the command exits with code 0 and all four steps A–D succeed
```

| Step | What is verified |
| --- | --- |
| **A** | Natural-language queries reach each demo Partner |
| **B** | Leader↔Partner direct_rpc |
| **C** | Team formation / inbox invitations are genuinely delivered |
| **D** | D1: the five categories of Monitor AMP records; D2: Discovery liveness coverage of Leader+Partner |

Failure hints: for A check the LLM/embedding; for B/C check `demo_llm_*` and RabbitMQ/mq-auth connectivity (on multiple machines especially `5671` / `9008`); for large-prompt or cross-machine migration timeouts check **MTU≤1400**; for D check the Forwarder / alive-sync.

Optional human login: [OIDC Web Manual Verification](./oidc-web-app-manual-verification_en.md), [acps-cli Device Login](./oidc-acps-cli-device-login_en.md).

---

## 7. If Something Goes Wrong, Look Here First

| Symptom | Possible cause / action |
| --- | --- |
| Preflight reports `CHANGE_ME` | Complete §4.3 |
| Preflight reports URL-reserved / `#` in password | Replace the secrets passwords containing `# @ : / ?` (see §4.3) |
| Preflight reports tomllib / Ansible too old | Run `"$PKG/scripts/bootstrap_control_ansible.sh"` on the control node and confirm that PATH contains `~/.local/bin` |
| Preflight reports `ca-materials` | Add or self-sign them per §4.4; add `--force` when files already exist in the directory |
| Preflight reports `must have exactly 1 host` | Some inventory group has 0 hosts or several |
| Preflight reports `acps_deploy_mode` | `all.vars` in the inventory must contain `image` or `host` (see `hosts.example.yml`) |
| SSH / permission failure | Passwordless SSH; `ansible_become: true` on the business machine; with 〔host〕 do not write become globally in the inventory |
| 〔image〕`docker` permission / permission denied | Add the deployment user to the `docker` group and log in again |
| Certificate / CLI cannot connect | `acps_advertise_host` is unreachable; with 〔image〕 it was written as `127.0.0.1` (preflight should already have blocked this) |
| Preflight reports advertise loopback / empty | 〔image〕or multi-machine 〔host〕: change it to a reachable IP/DNS name; on the same Mac set `ACPS_ADVERTISE_HOST` |
| daemon.json / `/etc/docker` on Desktop | Expected to be skipped; look only at the `acps-net` MTU and the host network interface MTU |
| 〔image〕Docker / Compose not found | Engine or the compose plugin is not installed on the business machine |
| 〔image〕Images missing | The installation package lacks the long-named `*.image.tar.gz`; go back to the assembly tutorial and rebuild |
| 〔image〕Ports unreachable across machines | Allow them through the firewall yourself (the playbook does not change it); see the port table in [Three Nodes](./install-package-ansible-deploy-3nodes_en.md) |
| 〔host〕OS / repository / vendor preflight failure | Confirm Rocky 8/9 or Ubuntu 20.04/22.04, that the repositories are reachable, and that vendor was packed into the package |
| 〔host〕Java / OpenSearch will not start | Keycloak: confirm that `temurin_jre17` from the package has landed and that `.env` has `JAVA_HOME`; OpenSearch: check `OPENSEARCH_JAVA_HOME` |
| The same tag behaves like an older version (image) | `-e acps_force_image_load=true`, or clean up per §5.0 |
| The demo certificate exists but A syncs 0 ACS | You can use `-e acps_force_demo_bootstrap=true` |
| `tomllib` not found | The control node's Python is < 3.11 |
| Business acceptance / NL failure | Real LLM key; `embedding_dim=1024`; MTU≤1400 |
| Business acceptance C (groups) failure | On multiple machines check RabbitMQ EXTERNAL / mq-auth `9008`; then check the LLM |
| Business acceptance D (AMP) failure | Agent side Emitter / AIC: see [AMP Observability](./amp-agent-observability_en.md); platform side check the Forwarder / Monitor |
| Still looking for demo-nginx / connecting directly to `:9031` as the Web | Use `demo_leader_web:9030` + same-origin `/api` uniformly |
| Leader→Partner `connection failed` / ACS contains `localhost:902` | The installer should rewrite the ACS; for how to check see [Three Nodes §7](./install-package-ansible-deploy-3nodes_en.md#7-acs-and-certificate-address-checks-shared-by-image-and-host) |
| `UNKNOWN_CA` after CA `--force` | First force-run `renew-certs.yml`, then restart the TLS-related services; for the order see [Three Nodes §7](./install-package-ansible-deploy-3nodes_en.md#7-acs-and-certificate-address-checks-shared-by-image-and-host) / [Day-2 Operations §1](./install-package-day2-ops_en.md#1-certificate-renewal-before-expiry-renew-certsyml) |

---

## 8. What to Do Next

- **Three business nodes** (either image or host): [Three-Business-Node Deployment](./install-package-ansible-deploy-3nodes_en.md).
- **Day-2 operations** (certificate renewal / trust / upgrade / rollback): [Day-2 Operations After Installation](./install-package-day2-ops_en.md). For details of the switches inside the package, see `acps-infra/release/install-packaging/README.md`.
- **Revisit assembly**: [Assembling an Installation Package](./install-package-build_en.md).
