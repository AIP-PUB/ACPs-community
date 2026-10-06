[Home](../README.md)

**[English](install-package-day2-ops_en.md) | [中文](install-package-day2-ops.md)**

# Day-2 Operations After Installation (Renewal / Trust / Upgrade / Rollback)

This tutorial is written for **anyone who has already run `site.yml` (and optionally `business.yml`) by following [Deploying ACPs with the Installation Package](./install-package-ansible-deploy_en.md)**.  
After installation, do not keep treating the full `site.yml` as an "upgrade button" in day-to-day operations. The following explains which playbook to use, and a few conventions that are easy to trip over.

For variable names, component lists, and the details of vendor / OS packages in host mode, the installation package's  
`README.md` and `docs/known-limitations.md` are authoritative (the corresponding source tree is  
`acps-infra/release/install-packaging/`).

```text
First installation, or changing the machine topology  →  site.yml
Near expiry, renew only leaf certificates             →  renew-certs.yml
Update only the trust chain (trust)                   →  refresh-trust-bundle.yml
Change the version of a specific component            →  upgrade.yml
Return to the previous version                        →  rollback.yml
Demonstrate business acceptance                       →  business.yml (optional)
Readiness before running business after day2          →  wait-discovery-alive.yml (optional; waits only for Discovery per-AIC)
```
**image and host use the same set of day-2 playbooks**; the difference is mainly how the target machine runs services (Docker Compose, or systemd / venv / OS packages). The host-mode bypass has been verified on **Rocky 8/9** and **Ubuntu 20.04/22.04** (renewal / trust / application and vendor upgrade and rollback / os_package rollback refusal). Unless noted otherwise below, the commands are the same in both modes.

---

## 0. Choose the Right Playbook First

| What you want to do | Run this | Do not do this |
| --- | --- | --- |
| First installation on a blank machine; change the inventory topology or `advertise`; certificate SAN / issuer must be aligned with the addresses | `site.yml` | Treat the full `site.yml` as a day-to-day upgrade |
| Certificates near expiry, renew only the **leaf certificates** | `renew-certs.yml` | Assume the default renew has already fixed the SAN |
| The CA trust chain changed, distribute only trust to each machine | `refresh-trust-bundle.yml` | Expect it to also re-sign leaf certificates along the way |
| The environment is already running, change only the versions of certain components | `upgrade.yml`, with an explicit component list | Run the full `site.yml` again as an upgrade |
| The upgrade failed, or you want to return to the last successful version | `rollback.yml`, with an explicit component list | Run the full `site.yml` after the rollback and overwrite the result |
| Demonstrate NL / RPC / AMP acceptance | `business.yml` | Use it instead of smoke, or instead of an upgrade |
| Run business again after day2 / a forced recreate | `wait-discovery-alive.yml` first, then `business.yml` | Replace the per-AIC wait with a bare sleep; permanently move D2 into the beginning of `business.yml` |

Both modes must follow these rules:

1. **After a successful upgrade or rollback, do not immediately run the full `site.yml` again.** Otherwise the templates and digests in the new package can easily overwrite the intent you just rolled back to.  
2. **Do not treat `docker compose down -v` (and similar commands that delete data volumes) as a day-to-day operation.** If an acceptance machine needs a "fresh install from scratch", follow the [clean slate tutorial](./install-package-clean-slate_en.md); do not half-clean and half-keep.  
3. For formal acceptance, keep the default smoke (`*_skip_smoke=false`). Consider `skip_smoke=true` only for urgent troubleshooting, and you **must not** treat a result that skipped smoke as a passed acceptance.

Control node conventions are the same as for the first installation: use a **separate unpacked directory** for each deployment, enter `$PKG/ansible`, and use  
`-i inventories/hosts.yml -e @inventories/secrets.yml` (for a single host in host mode you can also use  
`hosts.rocky8.yml` / `hosts.rocky9.yml` / `hosts.ubuntu20.yml` / `hosts.ubuntu22.yml`).  
When two environments run in parallel, use two sets of directories and isolate `acps_control_root`; do not share the same `control/work`.

---

## 1. Certificate Renewal Before Expiry (renew-certs.yml)

Scans according to the "near expiry" window and renews the **leaf certificates**, distributes them to the target machines, and restarts the services that use these certificates (including the services that depend on them).

**Contract: renewal keeps the Agent AIC unchanged** (for existing materials it runs `cert renew` with the same AIC and does not `agent delete` / re-create the registration).

**The default scan will not renew automatically just because advertise / SAN is inconsistent with the current state.**  
If the hostname, external address, or SAN needs to be aligned, run `site.yml` (changing the address surface **may change the AIC**, which differs from renew keeping the same identity); do not write "renew has already fixed the SAN" in the operations log.

```bash
cd "$PKG/ansible"
export ANSIBLE_CONFIG="$PKG/ansible/ansible.cfg"

ansible-playbook -i inventories/hosts.yml playbooks/renew-certs.yml \
  -e @inventories/secrets.yml

# Force-renew leaf certificates (the AIC is still preserved; for troubleshooting, or to overwrite leaf certificates after CA materials were changed with --force):
#   -e acps_force_cert_renew=true
# Renew only some profiles:
#   -e '{"acps_cert_renew_profiles":["redis","registry-9002"]}'
```

Common case: after you have replaced the intermediate CA with `generate_ca_materials.sh --force`, you must force-renew the leaf certificates, and then have the TLS-dependent services load the new materials. For the order, see [Three-Node §7](./install-package-ansible-deploy-3nodes_en.md#7-acs-and-certificate-address-checks-shared-by-image-and-host). After renewal, the AIC should be the same as before renewal.

After the renewal finishes, you can run the package's `playbooks/smoke.yml` for a basic check; `renew-certs.yml` itself also performs TLS-related checks at the end.

---

## 2. Refreshing the Trust Chain: `refresh-trust-bundle.yml`

Pulls the latest trust from `ca_server` in the inventory and distributes it to the target machines under the file names agreed for each component (for example `trust-bundle.pem`, `acps-root-ca.pem`). It does **not** re-sign leaf certificates.

```bash
cd "$PKG/ansible"

ansible-playbook -i inventories/hosts.yml playbooks/refresh-trust-bundle.yml \
  -e @inventories/secrets.yml

# Force overwrite: -e acps_force_trust_bundle_refresh=true
# Refresh only some: -e '{"acps_trust_bundle_profiles":["registry-9002","redis"]}'
```

Relationship with upgrades: when the component list of `upgrade.yml` **includes `ca_server`**, by default it automatically runs the trust refresh once more after the upgrade. You can turn this off with a variable, but turning it off prints a WARNING, and **do not turn it off during formal acceptance.**  
When the rollback list includes `ca_server`: it **will not** roll back certificates or trust along with it; if you need them aligned, **manually** run this playbook again.

---

## 3. Component Upgrade: `upgrade.yml`

Used to change component versions and processes, **preserving** data directories and certificates. Compose / systemd will only start or recreate services as the upgrade requires, and **will not** delete data volumes.  
You must provide the component list: `acps_upgrade_components` (comma-separated).

```bash
cd "$PKG/ansible"

ansible-playbook -i inventories/hosts.yml playbooks/upgrade.yml \
  -e @inventories/secrets.yml \
  -e acps_upgrade_components=registry_server

# With multiple components, mind the dependency order, for example upgrade ca first, then the services that use it:
#   -e acps_upgrade_components=registry_server,ca_server
# discovery can use the alias discovery_server (it expands to the current variant)
# When the version number is unchanged but you still want to walk the full upgrade flow:
#   〔image〕add -e acps_force_image_load=true as needed
#   〔host〕applications add -e acps_force_app_reinstall=true
#   〔host〕vendor (including amp_forwarder) add -e acps_force_vendor_reinstall=true
```

In 〔host〕 mode, remember these first:

| Type | Behavior |
| --- | --- |
| Application (`releases/` + `current`) | If the package contents are unchanged, by default there is no real upgrade; to practice rollback you must add `acps_force_app_reinstall=true`, which is what leaves behind a revertible `previous` |
| vendor_bundle (Keycloak, `amp_forwarder`, etc.) | Same as above; when practicing `amp_forwarder` rollback you commonly use `acps_force_vendor_reinstall=true` |
| OS packages (PostgreSQL / Redis / RabbitMQ) | Minor versions within the same major version can be upgraded; **mismatched PostgreSQL major versions fail immediately**, and you must perform `pg_upgrade` or export/import manually — the installer will not change the data directory |

By default, an upgrade does **not** also renew leaf certificates along the way. When the component list includes `ca_server`, it refreshes trust automatically per §2.

---

## 4. Rolling Back to the Previous Version: `rollback.yml`

Switches the selected components back to the `previous` recorded in the state (you can also specify `acps_rollback_to` explicitly, but it must match that `previous`).  
It does **not** automatically downgrade the database schema, and **by default it does not roll back certificates either**. A failure part-way stops and preserves the scene as far as possible.

**Before the first upgrade there is often no `previous`.** If you run `rollback.yml` immediately after the first installation, it fails because there is no rollback pointer — this is expected behavior. First run a successful `upgrade.yml` that switches out a `previous` (〔host〕 applications commonly need `acps_force_app_reinstall=true`; vendor commonly needs `acps_force_vendor_reinstall=true`), then practice rollback; or skip that practice step.

```bash
cd "$PKG/ansible"

ansible-playbook -i inventories/hosts.yml playbooks/rollback.yml \
  -e @inventories/secrets.yml \
  -e acps_rollback_components=registry_server

# If the state shows the current migrate is newer than previous, you must confirm explicitly (it still will not downgrade the schema):
#   -e acps_rollback_acknowledge_migrate=true
```

In 〔host〕 mode, for `postgresql` / `redis` / `rabbitmq`: automatic package downgrade is **refused by default**. To recover, handle the OS packages manually, or remove these components from the rollback list.

If TLS-related checks warn after a rollback, run `refresh-trust-bundle.yml` from §2 as needed, and do **not** use the full `site.yml` as a "one-click fix".

---

## 5. Recommended: Practice Once in a Lab Environment

On an environment where the first installation has already succeeded, use **the same installation package** to practice upgrade / rollback / renewal.  
The same version numbers can still verify whether the flow works; this **cannot** substitute for upgrade validation with "a different installation package of a larger version".  
**Note**: the rollback step below depends on the preceding upgrade having written out a `previous`; if you skip the upgrade, or the upgrade did not actually switch versions, rollback will fail (see §4).

```bash
# 1) Application upgrade + rollback (using registry as an example)
ansible-playbook -i inventories/hosts.yml playbooks/upgrade.yml \
  -e @inventories/secrets.yml \
  -e acps_upgrade_components=registry_server \
  -e acps_force_app_reinstall=true          # 〔host〕; 〔image〕 can add force load as needed

ansible-playbook -i inventories/hosts.yml playbooks/rollback.yml \
  -e @inventories/secrets.yml \
  -e acps_rollback_components=registry_server

# 2) Force-renew only redis's leaf certificates; if you also changed the topology, then run site.yml as needed
ansible-playbook -i inventories/hosts.yml playbooks/renew-certs.yml \
  -e @inventories/secrets.yml \
  -e '{"acps_cert_renew_profiles":["redis"]}' \
  -e acps_force_cert_renew=true

# 3) Refresh trust
ansible-playbook -i inventories/hosts.yml playbooks/refresh-trust-bundle.yml \
  -e @inventories/secrets.yml
```

When practicing `amp_forwarder` rollback in 〔host〕 mode: include `-e acps_force_vendor_reinstall=true` in the upgrade, otherwise there is often no revertible `previous`.

After practicing day2 and **before running** `business.yml`: after a forced renewal / recreate, Discovery's `aliveMap` may briefly be missing an individual AIC (the business plane is already working, but the heartbeat into Discovery still lags; waiting a while usually converges). It is recommended to run the readiness gating step first (it waits only for D2, does not run Monitor D1, and does not replace business A–D):

```bash
ansible-playbook -i inventories/hosts.yml playbooks/wait-discovery-alive.yml \
  -e @inventories/secrets.yml
# Default: single attempt timeout 480s; after a failure, settle 90s and retry once.
# Adjustable: -e acps_business_wait_alive_timeout_seconds=600
#       -e acps_business_wait_alive_retry=false
#       -e acps_business_wait_alive_retry_settle_seconds=120

ansible-playbook -i inventories/hosts.yml playbooks/business.yml \
  -e @inventories/secrets.yml
```

Immediately after a clean `site.yml`, running business usually does **not** require running `wait-discovery-alive.yml` first.

---

## 6. If Something Goes Wrong, Look Here First

| Symptom | What you can do |
| --- | --- |
| You changed advertise / the hostname, but the certificate SAN is still the old one | Run `site.yml` (may change the AIC; this is the address-surface exception), and do **not** run only the default renew and then claim the SAN is fixed |
| `UNKNOWN_CA` appears after `--force` on the CA materials | Force-run `renew-certs` (it should preserve the AIC), then restart / recreate the TLS-dependent services; see Three-Node §7 |
| After upgrading `ca_server`, some services do not trust the certificate chain | Check whether the upgrade refreshed trust automatically; or manually run `refresh-trust-bundle.yml` |
| After a rollback it looks like the new version again | Check whether the full `site.yml` was run by mistake and overwrote the rollback result |
| 〔host〕 rolling back amp / vendor reports no `previous` | Add `acps_force_vendor_reinstall=true` during the upgrade |
| Rollback fails immediately after the first installation (no `previous`) | **Expected**: first run a successful upgrade to switch out a previous, or skip that practice step |
| 〔host〕 rolling back postgresql fails | **This is expected behavior**: the product refuses automatic package downgrade by default |
| Right after a refresh / upgrade, smoke immediately reports 9009 and cannot connect | The monitor may still be starting; wait a moment and run smoke again |
| business right after day2, Step D2 reports Leader/Partner not in aliveMap | Run `wait-discovery-alive.yml` first (extended timeout by default + one settle retry), then `business.yml`; if it still times out twice, investigate heartbeat→alive-delta |
| You want to upgrade across major versions | You need to prepare **another** installation package for the corresponding version; a forced reinstall of the same version only validates the flow and cannot serve as cross-version evidence |

---

## 7. What to Do Next

- Back to the first installation: [Deploying ACPs with the Installation Package](./install-package-ansible-deploy_en.md)
- The acceptance machine needs a fresh install from scratch: [clean slate tutorial](./install-package-clean-slate_en.md) (≠ this day-2 operations guide)
- Multi-machine topology: [Three-Business-Node Deployment](./install-package-ansible-deploy-3nodes_en.md)
- In-package documentation: `install-packaging/README.md`, `docs/known-limitations.md`
- Boundaries such as ClickHouse / OpenSearch currently being plaintext, having no long-running renewal process, and rollback not automatically downgrading the schema are governed by `known-limitations.md`
