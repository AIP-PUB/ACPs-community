[Home](../README.md)

**[English](install-package-ansible-deploy-3nodes_en.md) | [中文](install-package-ansible-deploy-3nodes.md)**

# Three-Business-Node Deployment (Ansible: image / host)

Prerequisite: you have already worked through [Deploy ACPs from the Installation Package](./install-package-ansible-deploy_en.md) and can unpack, fill in `secrets.yml`, use a self-signed CA, and run `site.yml` / `business.yml`. This document only covers **how to distribute the components across three business machines**; the Ansible / secrets / CA / acceptance commands are not repeated. For renewal / upgrade after installation, see [Day-2 Operations](./install-package-day2-ops_en.md). To "install from scratch again" on the same set of machines, see the [Clean-Slate Tutorial](./install-package-clean-slate_en.md).

**image and host share the same component grouping**; the only differences are the business-machine prerequisites (Docker vs Rocky 8/9 · Ubuntu 20.04/22.04) and the installation package type, see §0 / §2 of the main tutorial.

For a two-machine (app + dependency base) topology you can copy `inventories/hosts-multi.example.yml` directly from the installation package (no need to hand-write the three-machine layout). This document gives a **three-business-node** split example.

Machines: **1 control node** (runs only Ansible + acps-cli) + **3 business nodes**. The control node can SSH to all three without a password and can reach each machine's `acps_advertise_host` ports.

- 〔image〕All three machines have Docker installed; the deployment user must be able to use sudo without a password and must be able to run `docker` (usually by joining the `docker` group and logging in again).  
- 〔host〕All three machines must run a supported operating system: **Rocky Linux 8/9** or **Ubuntu 20.04/22.04** (they may all be the same major version, or mixed on site — every single machine must satisfy the host prerequisites). For a four-OS mixed deployment example, see §8 below.

**Additional multi-machine prerequisites (easy to miss):**

| Item | Description |
| --- | --- |
| **MTU** | When migrating PostgreSQL across machines / sending large LLM prompts through the tunnel, set the three business network interfaces to **MTU ≤1400** (the installer does not change this). If "small requests work but large packets stall", check this first. |
| **Firewall** | 〔host〕Most components idempotently allow the advertise ports; **〔image〕does not modify firewalld/ufw automatically**. Before going cross-machine, allow the ports in the table below, or temporarily disable the firewall to get things working and then tighten it. |
| **Reachability** | Each machine's `acps_advertise_host` must be an IP/DNS that both of the other two machines and the control node can reach (`127.0.0.1` is forbidden). |

Common cross-machine ports (for the topology in this document; the list is not exhaustive, but missing these is what most often breaks things):

| Direction (illustrative) | Port | Purpose |
| --- | --- | --- |
| → biz-1 | `5432` | PostgreSQL |
| → biz-1 | `5671` | RabbitMQ AMQPS (demo EXTERNAL / inbox) |
| → biz-1 | `9080` (default) | Keycloak |
| → biz-2 | `9001` / `9002` | Registry public / mTLS |
| → biz-2 | `9003` | CA |
| → biz-2 | `9005` | Discovery |
| → biz-2 | `9007` / `9008` | mq-auth group API / RabbitMQ `auth_http` |
| → biz-2 | `9030` | demo Leader Web |
| → biz-3 | `9009` | Monitor |

---

## 1. How to Split

| Host | Role | Components |
| --- | --- | --- |
| **biz-1** | Foundation: database / cache / messaging / login | `postgresql` `redis` `rabbitmq` `keycloak` |
| **biz-2** | Platform + the two demos | `registry_server` `ca_server` `discovery_server` `mq_auth_server` `demo_partner` `demo_leader` |
| **biz-3** | Observability and AMP storage | `redpanda` `victoria_metrics` `clickhouse` `minio` `opensearch` `monitor_server` |

The AMP Forwarder lands on **biz-2** together with the demos (the playbook attaches it automatically according to the host the demos are on, so no separate group changes are needed).

---

## 2. Edit the Inventory

In `$PKG/ansible` after unpacking on the control node:

**〔image〕**

```bash
cd "$PKG/ansible"
cp inventories/hosts.example.yml inventories/hosts.yml
```

**〔host〕**You can copy `hosts.rocky8.yml` / `hosts.rocky9.yml` / `hosts.ubuntu20.yml` / `hosts.ubuntu22.yml` and change it to three machines, or create your own `hosts.yml` and set `acps_deploy_mode: host` (do not set `ansible_become` **globally** in the inventory). For a four-OS mixed deployment you can use `hosts.4os-multi.yml` from the package directly (see §8).

Change the single host into the following (host names are up to you, but the later `host_vars` file names must match).

**You must keep `all.vars.acps_deploy_mode`**: it is already present when you only change `children` in `hosts.example.yml`; if you replace the whole section with the example below and omit it, `preflight` will fail.

```yaml
all:
  vars:
    acps_deploy_mode: image   # For 〔host〕, change to host
  children:
    postgresql:       { hosts: { biz-1: } }
    redis:            { hosts: { biz-1: } }
    rabbitmq:         { hosts: { biz-1: } }
    keycloak:         { hosts: { biz-1: } }

    registry_server:  { hosts: { biz-2: } }
    ca_server:        { hosts: { biz-2: } }
    discovery_server: { hosts: { biz-2: } }
    mq_auth_server:   { hosts: { biz-2: } }
    demo_partner:     { hosts: { biz-2: } }
    demo_leader:      { hosts: { biz-2: } }

    redpanda:         { hosts: { biz-3: } }
    victoria_metrics: { hosts: { biz-3: } }
    clickhouse:       { hosts: { biz-3: } }
    minio:            { hosts: { biz-3: } }
    opensearch:       { hosts: { biz-3: } }
    monitor_server:   { hosts: { biz-3: } }
```

The rule is unchanged: each enabled component group can still map to **exactly 1** host.

---

## 3. Write Three `host_vars` Files

Create one file for each business machine. **`acps_advertise_host` must be an IP/DNS that the other two machines and the control node can both reach**; do not write `127.0.0.1`.

`inventories/host_vars/biz-1.yml`:

```yaml
ansible_host: 10.0.0.11
ansible_user: deploy
ansible_become: true
ansible_python_interpreter: /usr/bin/python3
acps_advertise_host: 10.0.0.11
```

Write `biz-2.yml` / `biz-3.yml` the same way, changing only to their respective IPs. Keep the default paths `/opt/acps`, `/var/lib/acps`, `/var/log/acps` (together with `become`).

When running acceptance for multiple topologies in parallel, set a separate `acps_control_root` in `all.vars` of `hosts.yml` (or use `-e`); do not share the default `~/.local/share/acps/control` with the single-machine topology. For reinstalling after a clean slate, see the [Clean-Slate Tutorial](./install-package-clean-slate_en.md).

---

## 4. secrets / CA: Same as Single Machine

```bash
cp inventories/secrets.example.yml inventories/secrets.yml
chmod 600 inventories/secrets.yml
# Fill in CHANGE_ME as in the single-machine tutorial; passwords must not contain # @ : / ?; business acceptance requires a real LLM / embedding (use 1024 for embedding_dim)

mkdir -p inventories/ca-materials
"$PKG/scripts/generate_ca_materials.sh" --out inventories/ca-materials/
# If the directory already contains ca.crt / ca.key / root-ca.crt (including materials pre-shipped in the installation package), you must add --force for them to be overwritten:
# "$PKG/scripts/generate_ca_materials.sh" --out inventories/ca-materials/ --force
```

---

## 5. Self-Check Before Installing

```bash
# If the control node's Ansible is too old or tomllib is missing, first run:
# "$PKG/scripts/bootstrap_control_ansible.sh" && export PATH="$HOME/.local/bin:$PATH"

cd "$PKG/ansible"
export ANSIBLE_CONFIG="$PKG/ansible/ansible.cfg"

ansible all -i inventories/hosts.yml -m ping

ansible-playbook playbooks/preflight.yml -i inventories/hosts.yml -e @inventories/secrets.yml
ansible-playbook playbooks/site.yml      -i inventories/hosts.yml -e @inventories/secrets.yml
ansible-playbook playbooks/business.yml  -i inventories/hosts.yml -e @inventories/secrets.yml
```

The commands are the same as for a single machine; orchestration across machines is automatic according to the inventory, and there is no need to change the playbooks.

---

## 6. Common Multi-Machine Pitfalls

| Symptom | Remedy |
| --- | --- |
| Preflight complains about a missing / unknown `acps_deploy_mode` | Add `image` or `host` to `all.vars` in the inventory (see §2) |
| Certificates / CLI cannot connect to a component | That component's machine `acps_advertise_host` is unreachable, or was written as `127.0.0.1` |
| Preflight reports `must have exactly 1 host` | A component group is missing a host or has an extra one |
| SSH reaches only one machine | The control node needs passwordless access to **biz-1/2/3**; `ansible all -m ping` should return ok for all three |
| 〔image〕`docker` permission failure on one machine | Add the deployment user to the `docker` group, log in again, then run `docker ps` |
| Cannot connect to PG / AMQPS / mq-auth across machines | Check the firewall and the port table above; when RabbitMQ is on biz-1 and mq-auth is on biz-2, `5671` and `9007/9008` in particular must be allowed |
| 〔image〕biz-2 writes back to the Registry ACS to pull the postgres image | This has been changed to `acps_psql.sh` (docker exec on the same machine / local psql across machines); do not rely on Docker Hub any more |
| Alembic / large SQL / large LLM times out or hangs | Set the three business network interfaces to **MTU ≤1400** |
| `generate_ca_materials` reports already exist | Add `--force`, or switch to your own three files |
| demo / Forwarder not found | Confirm that `demo_*` is on **biz-2**; do not mistakenly change it to biz-3 |
| 〔host〕OS/repository failure on one machine | That machine must independently satisfy the Rocky 8/9 or Ubuntu 20/22 and online repository requirements |
| Business acceptance C (group inbox) fails | First confirm that EXTERNAL can connect to RabbitMQ (mq-auth `9008` reachable), then check the LLM; see §6 / §7 of the main tutorial |
| Leader reports `All connection attempts failed` when connecting to Partner / ACS is still `localhost:902x` | The installer should already have rewritten the ACS; check whether the biz-2 Partner ACS and the Leader `scenario/expert` are `https://<biz-2 advertise>:902x`; see §7 |
| Partner reports `UNKNOWN_CA` when connecting to RabbitMQ after CA `--force` | Run `renew-certs.yml` first, then recreate/restart; see §7 |

For the single-machine procedure, the differences between the two modes, and business acceptance A–D, go back to [install-package-ansible-deploy.md](./install-package-ansible-deploy_en.md).  
To install from scratch again on the same set of machines: [Clean-Slate Tutorial](./install-package-clean-slate_en.md).

---

## 7. ACS and Certificate Address Checks (Shared by image and host)

The installer rewrites the ACS: **Public** = each machine's `acps_advertise_host`; **Colocated AMQP** = 〔image〕may use `rabbitmq` on the same machine, while 〔host〕/cross-machine uses the peer advertise.

**After installation you can check it like this (on the control node or biz-2):**

```bash
# Partner ACS: HTTPS must no longer be localhost
python3 -c 'import json,glob,sys
from urllib.parse import urlsplit
ok=True
for p in glob.glob("/opt/acps/components/demo_partner/partners/online/*/acs.json"):
  for ep in json.load(open(p)).get("endPoints") or []:
    u=ep.get("url") or ""; h=(urlsplit(u).hostname or "")
    if ep.get("transport")=="JSONRPC" and h in ("localhost","127.0.0.1"):
      print("BAD",p,u); ok=False
print("partner_acs_ok" if ok else "partner_acs_BAD"); sys.exit(0 if ok else 1)'

# Leader static Partner snapshot
grep -R "https://localhost:902" /opt/acps/components/demo_leader/leader/scenario/expert || echo "leader_scenario_ok"
```

| Item | Expected |
| --- | --- |
| Partner ACS HTTPS | `https://<biz-2 advertise>:902x/...` |
| Partner/Leader ACS AMQP (in this topology RMQ is on biz-1) | `amqps://<biz-1 advertise>:5671/...` (〔image〕may be `rabbitmq` only if Partner and RMQ are on the same machine) |
| 〔host〕 | ACS/env **must not** retain the Compose names `rabbitmq` / `postgresql` |

**Order of operations after CA `--force` (unrelated to address problems; do not investigate it together with connectivity failures):**

1. After `generate_ca_materials.sh --force` (or switching the intermediate CA)  
2. You must run `ansible-playbook playbooks/renew-certs.yml ...` (adding `-e acps_force_cert_renew=true` is recommended) to cover leaf certificates such as RabbitMQ/Redis/Registry/mq-auth/demo; for the complete steps see [Day-2 Operations §1](./install-package-day2-ops_en.md#1-certificate-renewal-before-expiry-renew-certsyml)  
3. Then recreate/restart the services that depend on TLS; the Partner `trust-bundle` must contain the **current** root+intermediate  

Otherwise you will get `UNKNOWN_CA` / issuer mismatch, which looks like "the address is unreachable".

---

## 8. Four-OS Mixed Deployment Example (`hosts.4os-multi.yml`)

When you expand the three-machine split to four machines with a different OS on each, the installation package provides a ready-made inventory:

`inventories/hosts.4os-multi.yml` + `inventories/host_vars/{rhel8,rhel9,ubuntu20,ubuntu22}.acps.local.yml`

| Host | OS | Components |
| --- | --- | --- |
| `rhel8.acps.local` | Rocky 8 | `postgresql` `redis` `rabbitmq` `keycloak` |
| `ubuntu20.acps.local` | Ubuntu 20.04 | `registry_server` `ca_server` `discovery_server` `mq_auth_server` |
| `rhel9.acps.local` | Rocky 9 | `demo_partner` `demo_leader` (`amp_forwarder` follows the demos) |
| `ubuntu22.acps.local` | Ubuntu 22.04 | `redpanda` `victoria_metrics` `clickhouse` `minio` `opensearch` `monitor_server` |

Key points:

- Set a separate `acps_control_root` in `all.vars` (for example `control-host-4os-multi`); do **not** set `ansible_python_interpreter: /usr/bin/python3.8` globally (put it only in rhel8's host_vars).
- All four machines and the control node must be able to resolve and reach each machine's `acps_advertise_host` (`127.0.0.1` is forbidden). Cross-machine connectivity is also required for: `5671`↔`9008` (RMQ↔mq-auth), demo→infra `5432`/`6379`/`9080`, and amp→Redpanda `19092`.
- Set the business network interfaces to **MTU ≤1400**. For reinstalling after a clean slate, see the [Clean-Slate Tutorial](./install-package-clean-slate_en.md).
