[Home](../README.md)

**[English](install-package-clean-slate_en.md) | [中文](install-package-clean-slate.md)**

# Clean Slate Before Acceptance / Reinstallation (Destructive)

This tutorial is for: **people who want to "install once more from scratch" on the same batch of machines** for acceptance or troubleshooting.  
The goal is to bring the **application state of the business machines / control plane close to that of a first-time installation on a blank machine** — no old database roles, old middleware state, old volumes, old units, or cross-contaminated old CA workspaces.

- **What is cleared is data and runtime state**, not OS software packages.  
- This is **not** routine day-2 operations. For routine renewal / trust / upgrade / rollback see [Day-2 Operations](./install-package-day2-ops_en.md) (**do not** treat `docker compose down -v` as a routine measure).  
- After clearing, install again: go back to [Ansible Deployment](./install-package-ansible-deploy_en.md) and run `site.yml`.

```text
Day-2 operations (preserve data)        →  day2-ops (renew / upgrade / rollback)
Acceptance clean slate + reinstall (destroy data) →  this tutorial → site.yml
```

After the clean slate, do **not** manually `systemctl start` PostgreSQL / Redis / RabbitMQ; wait for `site.yml` to reconfigure and start them according to this run's secrets.

---

## 0. When to Use / When Not to Use

| Scenario | Use this tutorial? |
| --- | --- |
| First-time installation on a blank machine | **No** — run `site.yml` directly |
| Installed environment, only changing component versions | **No** — `upgrade.yml` / `rollback.yml` |
| Remote acceptance, failure after a half-cleared/half-kept state, need to reproduce a "clean first-time installation" | **Yes** |
| Parallel acceptance across topologies (single machine / multiple machines / image / host) | **Yes** — and the control node must also isolate `acps_control_root` |
| Production troubleshooting "wipe first and ask later" | **Use with caution** — back up first; confirm this is an acceptance machine |

### 0.0 Master Table: Must Clear vs Should Keep

Remote Linux default paths are used as the example; for same-machine acceptance on a local machine, replace the three paths with the `~/.local/share/acps…` paths from §0.1.

#### Business Machine — Must Clear (clean first-time installation)

| Category | Path / Object | Notes |
| --- | --- | --- |
| Three paths | `$RUNTIME` `/opt/acps`, `$DATA` `/var/lib/acps`, `$LOG` `/var/log/acps` | Includes compose, app/vendor releases, certs, state, and **all** data under `$DATA` for MinIO/OpenSearch/ClickHouse/Redpanda/Keycloak/VM, etc. |
| 〔host〕systemd | `/etc/systemd/system/acps-*.service` | Stop and disable, then delete and `daemon-reload`; deleting only the tree but leaving the unit → false `CHDIR` failure |
| 〔host〕PostgreSQL **data** | All majors: `/var/lib/pgsql/*/data`, `/var/lib/postgresql/*/main` | Installer runs init when `PG_VERSION` is missing; **missing any major** → old roles + new secrets (Keycloak, etc.) |
| 〔host〕Redis / RabbitMQ **data** | `/var/lib/redis`, `/var/lib/rabbitmq` | After deleting, you **must** recreate the empty directories + owner (and SELinux `restorecon`) |
| 〔host〕Redpanda compatibility link | `/opt/redpanda` (symlink to `$RUNTIME/redpanda`) | Dangles once the runtime is deleted; the installer recreates it |
| 〔image〕Compose | Project `acps`: `down -v` + leftover containers / networks / volumes | The project lives in `$RUNTIME/compose`; do not run compose bare at the `$RUNTIME` root |
| 〔image〕This acceptance's images | `acps/*` images (recommended to clear by default) | Avoids a half-broken state where "the directory is gone but the images remain" |
| Control plane | This run's `$PKG` unpack tree and the corresponding `acps_control_root` | Otherwise the CA / issuer / token workspace is cross-contaminated |
| Control plane state | `~/.local/share/acps/state` on the control node (including `acps_cli.json`) | A different path from the `control*` workspace; not clearing it leaves CLI deployment records behind |
| Three paths accidentally present on the control node | If `/opt/acps` (or `/var/lib/acps`) exists | The control node is **not** a business machine, but TLS digests etc. may still be left in `state/`; a clean start should delete them |
| Build output | The builder's app-release / image / install **output** directories | Do not let the "latest tar" point at the previous run |

#### Business Machine / Build Machine — Should Keep (the installer reconfigures these, or they are unrelated to first-time installation)

| Category | Path / Object | Notes |
| --- | --- | --- |
| OS software packages | **rpm/deb** for postgresql / redis / rabbitmq / docker, etc. | Do **not** use `dnf remove` / `apt purge` as a substitute for a clean slate |
| Distribution repositories | PGDG, redis.io, RabbitMQ yum/apt sources | Idempotently ensured by the installer |
| 〔host〕middleware **config files** | `/etc/redis/redis.conf`, `/etc/rabbitmq/*` | `site.yml` **re-renders** them according to this run's secrets; clearing data is sufficient |
| 〔host〕RabbitMQ drop-in | `/etc/systemd/system/rabbitmq-server.service.d/limits.conf` | Stateless; may be kept |
| sysctl | `/etc/sysctl.d/99-acps-opensearch.conf` | May be kept |
| 〔image〕Docker daemon | `/etc/docker/daemon.json` (including MTU) | Do **not** delete it as part of the clean slate |
| Firewall rules | Ports already allowed through firewalld / ufw | Idempotent; no need to withdraw them for a "first-time installation feel" |
| `/etc/hosts` advertise-name entries | Local resolution lines written by Redpanda, etc. | May be kept; the installer is idempotent |
| Client symlinks | `/usr/local/bin/psql`, etc. | May be kept |
| builder cache | Persistent `--vendor-bundle-dir`, already-aligned source trees | **Do not** use as clean-slate targets |
| SELinux fcontext | Old labeling rules for paths already deleted | Generally harmless; the installer labels new paths again |

#### Stance When Deliberately Not Wiping

| What you kept… | Acceptance stance |
| --- | --- |
| PostgreSQL data directories (any major still has `PG_VERSION`) | **Reusing the old database**; not a clean first-time installation, and the record must state this |
| Old `secrets.yml` + wiped data | Allowed (passwords match an empty database) |
| **New** `secrets.yml` + **not** wiped PG/Redis/RMQ/`$DATA` | **Do not** call this a clean first-time installation — it will certainly fail or drift in authentication |

Under the host path the installer can sync passwords for existing PG roles, but that **cannot replace** the mandatory wipe of data directories required by this tutorial.

#### Half-Cleared, Half-Kept (Has Previously Caused Acceptance Failures)

- Deleted only the runtime, leaving `acps-*.service`  
- Deleted `/var/lib/redis|rabbitmq` without recreating the empty directories  
- `cd`'d into the wrong directory, so compose never really ran `down -v`  
- Cleared the runtime but image-mode images / anonymous volumes remained  
- Multiple topologies sharing the same `control/work` or the same unpack tree with a modified inventory, then installing  
- Cleared only the business machines, while the control node had a dirty unpack and the build machine an old "latest package"  
- Cleared only `control*`, leaving `~/.local/share/acps/state` or `/opt/acps/state` on the control node  
- **Cleared only the PG data of 15/16 while the site was actually on 17** (or another major)  
- Enumerated/judged PG data directories as an ordinary user (`700`/`postgres`), causing the loop to skip them while the gating step still saw `PG_VERSION`  
- Rotated a new set of random secrets while reusing un-wiped PG / Redis / RabbitMQ / `$DATA`  
- Runtime already deleted, leaving a dangling `/opt/redpanda`

---

### 0.1 Confirm the Three Paths First (before deleting anything)

Go by the **actual values in the inventory / `host_vars`**; do not memorize one fixed set of paths.

| Variable | Remote Linux default | 〔image〕same-machine acceptance on a local machine (in-package `acps-node-1.yml`) |
| --- | --- | --- |
| `acps_runtime_root` | `/opt/acps` | `~/.local/share/acps` |
| `acps_data_root` | `/var/lib/acps` | `~/.local/share/acps/data` |
| `acps_log_root` | `/var/log/acps` | `~/.local/share/acps/logs` |
| Compose directory | `{{ acps_runtime_root }}/compose` | Same as above |
| Compose project name | `acps` (`acps_compose_project_prefix`) | Same as above |
| PostgreSQL major | In-package `postgresql_os_major_version` (currently defaults to **17**) | image data lives in `$DATA/postgresql` and is deleted along with `$DATA` |

The commands below use the variable form; on a remote machine you can first run:

```bash
RUNTIME=/opt/acps
DATA=/var/lib/acps
LOG=/var/log/acps
```

For same-machine acceptance on a local macOS machine you can first run:

```bash
RUNTIME="$HOME/.local/share/acps"
DATA="$HOME/.local/share/acps/data"
LOG="$HOME/.local/share/acps/logs"
# Note: control is also under $RUNTIME/control by default; clearing the runtime parent tree also clears control
```

### 0.2 Coupling Between secrets / CA and the Clean Slate

| Plan for this run | Clean-slate requirement |
| --- | --- |
| Clean first-time installation + **newly** generated `secrets.yml` / **new** CA (`--force`) | Business machine checklist A/B executed **in full** (including all PG major data + Redis/RMQ + `$DATA`) + in this scenario a control / `$PKG` start from empty |
| Clean first-time installation + **reusing** the same secrets and the same CA material from the previous run | Business machine data must still be fully wiped; if control keeps an old cert work it may be mixed with the "new unpack" — when switching scenarios, still preferable to clear the whole control tree |
| Just wanting to "reuse the old database" for troubleshooting | Do **not** change the DB/middleware passwords in secrets; and state in the record that this is not a clean first-time installation |

---

## 1. Build Machine / Control Node (non-business runtime)

Business machines follow checklist A/B below. If acceptance also involves a **separate build machine (builder)** and an **Ansible control node (controller)**, those two machines must **not** stop PostgreSQL / Redis per A/B; but you must clear "this run's artifacts and unpack tree", otherwise the usual problems are:

- The disk is filled up by old installation packages / build artifacts  
- A glob for the "latest tar" picks up the previous run's package  
- Multiple topologies share a dirty `control/work`, cross-contaminating certificate / issuer fingerprints  

Recommended order: **this section first → then business machine checklist A/B**. When switching scenarios: the unpack directory to be used in this run and the corresponding `acps_control_root` must be cleared once more.

### 1.1 Control Node: Isolate the Unpack and the control root

Use a **separate unpack directory** for every acceptance run; do not let multiple topologies share the same workspace cache under `$PKG`.

`acps_control_root` defaults to `~/.local/share/acps/control` (role defaults).  
For parallel topologies, set an independent path in `all.vars` of `hosts.yml` or via `-e` on the command line, for example:

```yaml
# inventories/hosts.yml → all.vars
acps_control_root: "{{ lookup('env', 'HOME') }}/.local/share/acps/control-image-multi"
```

```bash
# or override once

ansible-playbook ... -e acps_control_root="$HOME/.local/share/acps/control-host-single"
```

> `group_vars/all.yml` **no longer** hard-codes `acps_control_root`; the inventory / `-e` can override the default value.

**Before entering this run's scenario** (when changing CA / changing topology / reinstalling on the same machine, it is recommended to clear the whole tree rather than only editing the inventory on a dirty tree):

```bash
# Unpack root: the directory to be used in this run must start empty (it will be re-extracted with tar later)
PKG="${PKG:?set to this-run unpack root}"
rm -rf "$PKG"
mkdir -p "$PKG"

# control workspace: this scenario's path + common control-* variants (multiple topologies)
CONTROL="${ACPS_CONTROL_ROOT:-$HOME/.local/share/acps/control}"
rm -rf "$CONTROL"
rm -rf "$HOME"/.local/share/acps/control-*
# If only clearing the cache: rm -rf "$CONTROL/work"

# CLI / component state (a different path from the control workspace; not clearing it leaves acps_cli.json)
rm -rf "$HOME/.local/share/acps/state"

# If the control node ever accidentally held the business three paths (commonly only state/*.sha256 remains), remove those too
# Do not blindly delete on same-machine acceptance that also acts as a business machine — in that case use checklist A/B
sudo rm -rf /opt/acps /var/lib/acps /var/log/acps
```

Gating step (control node):

```bash
CONTROL="${ACPS_CONTROL_ROOT:-$HOME/.local/share/acps/control}"
FAIL=0
if [[ -e "$CONTROL" ]] && [[ -n "$(ls -A "$CONTROL" 2>/dev/null)" ]]; then
  echo "FAIL: $CONTROL not clean"; FAIL=1
else
  echo "OK: control work clean"
fi
if [[ -e "$HOME/.local/share/acps/state" ]]; then
  echo "FAIL: $HOME/.local/share/acps/state still present"; FAIL=1
else
  echo "OK: local acps state gone"
fi
if [[ -e /opt/acps ]] || [[ -e /var/lib/acps ]]; then
  echo "FAIL: controller still has /opt/acps or /var/lib/acps"; FAIL=1
else
  echo "OK: no biz three-paths on controller"
fi
# exit "$FAIL"  # uncomment when incorporating into an acceptance script
```

Destination directory for installation packages (e.g. `~/…/packages/`) is recommended to be:

- isolated per `<run-id>/`, or  
- have old `acps-*-install-*.tar*` files **not referenced by this run** deleted, to avoid picking the wrong file during scp / manual copying  

〔image〕same-machine acceptance on a local machine: if `acps_control_root` falls under `acps_runtime_root`, deleting the runtime parent directory also clears control — in acceptance the clean slate usually clears them together; when a local machine simultaneously plays "business machine + control plane", after completing checklist A confirm that control / unpack / `~/.local/share/acps/state` are empty. **Do not** run the above "control node accidentally held" `sudo rm -rf /opt/acps…` against the business three paths in a same-machine acceptance scenario as a substitute for checklist A — the order is still checklist A (including compose) and then confirming control/state.

### 1.2 Build Machine: Keep Caches, Clear This Run's Artifacts

| Action | Notes |
| --- | --- |
| **Keep** | Persistent `--vendor-bundle-dir` (vendor cache; **do not** delete it as part of the clean slate) |
| **Keep** | Source sync directory (if aligned with `rsync --delete`, no manual wipe needed) |
| **Delete / switch directory** | Old app-release, image tar, installation package **output directories**; or switch to an out with a `<run-id>` so the "latest package" does not point at the previous run |
| Optional | Clean up overly old build logs when disk space is tight |

Gating step (before rebuilding / unpacking): the control node uses the §1.1 gating step; the build machine additionally verifies that the installation package filename and mtime about to be used belong to this run (record it in the acceptance record).

---

## 2. 〔image〕Business Machine Checklist A

Execute on **every** business machine (with `RUNTIME` / `DATA` / `LOG` already set per §0.1).  
In image mode, the state of PostgreSQL / Redis / RabbitMQ etc. lives in **Docker volumes or `$DATA` binds**, and there are **no** host `/var/lib/pgsql` must-clear items; `down -v` + deleting the three paths covers it.

### 2.1 Stop the Stack and Delete Volumes (acceptance only)

Compose is **not** at the `$RUNTIME` root but in `$RUNTIME/compose`, and it is multi-file with the fixed project name `acps`.  
Do **not** run only `cd "$RUNTIME" && docker compose down` (it usually will not find the correct project, and the stack will not stop cleanly).

```bash
COMPOSE_DIR="$RUNTIME/compose"
COMPOSE_YML="$COMPOSE_DIR/docker-compose.yml"
PREFIX=acps   # consistent with group_vars acps_compose_project_prefix; sync it if you changed it

if [[ -f "$COMPOSE_YML" ]]; then
  ARGS=(-p "$PREFIX" -f "$COMPOSE_YML")
  if [[ -d "$COMPOSE_DIR/services" ]]; then
    while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      ARGS+=(-f "$f")
    done < <(find "$COMPOSE_DIR/services" -maxdepth 1 -name '*.yml' 2>/dev/null | sort)
  fi
  docker compose "${ARGS[@]}" down -v --remove-orphans
fi
```

If the compose files are already gone, clean up the leftovers by **project prefix** (container names look like `acps-postgresql-1`):

```bash
# Portable: do not rely on GNU xargs -r (macOS has no -r)
docker ps -aq --filter "name=${PREFIX:-acps}" | xargs docker rm -f 2>/dev/null || true
docker network ls --filter "name=${PREFIX:-acps}" -q | xargs docker network rm 2>/dev/null || true
docker volume ls --filter "name=${PREFIX:-acps}" -q | xargs docker volume rm 2>/dev/null || true
```

For a clean first-time installation it is recommended to **also** clear the `acps/` images loaded by this acceptance:

```bash
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
  | awk '/^acps\// {print $2}' | xargs docker rmi -f 2>/dev/null || true
```

**Keep** `/etc/docker/daemon.json` (MTU etc.); do not uninstall Docker.

### 2.2 Delete the Three Paths

```bash
# Remote default usually needs sudo; local same-machine paths under $HOME may not need sudo
rm -rf "$RUNTIME" "$DATA" "$LOG"
# If permissions are insufficient:
# sudo rm -rf "$RUNTIME" "$DATA" "$LOG"
```

### 2.3 Gating Step (image)

```bash
PREFIX="${PREFIX:-acps}"
left="$(docker ps -a --format '{{.Names}}' | grep -E "^${PREFIX}-" || true)"
if [[ -n "$left" ]]; then echo "FAIL: containers left:"; echo "$left"; else echo "OK: no acps project containers"; fi

vols="$(docker volume ls --format '{{.Name}}' | grep -E "^${PREFIX}" || true)"
if [[ -n "$vols" ]]; then echo "FAIL: volumes left:"; echo "$vols"; else echo "OK: no acps volumes"; fi

nets="$(docker network ls --format '{{.Name}}' | grep -E "${PREFIX}" || true)"
if [[ -n "$nets" ]]; then echo "FAIL: networks left:"; echo "$nets"; else echo "OK: no acps networks"; fi

test ! -e "$RUNTIME" && echo "OK: runtime gone" || echo "FAIL: $RUNTIME still exists"
test ! -e "$DATA" && echo "OK: data gone" || echo "FAIL: $DATA still exists"
```

Then go back to the control node and run `preflight` → `site.yml`.

---

## 3. 〔host〕Business Machine Checklist B

### 3.1 Stop, Disable and Delete the `acps-*` units (mandatory)

**Do not** delete only `$RUNTIME` while leaving the units: the next `systemctl start` will produce `CHDIR` / WorkingDirectory does not exist, masking the root cause.

```bash
# Loaded instances (on some distributions list-* is non-zero when there is no match; add || true after the pipe to avoid a false interruption under set -e)
sudo systemctl list-units --type=service --all 'acps-*' --no-legend 2>/dev/null \
  | awk '{print $1}' | while read -r u; do
      [[ -n "$u" ]] || continue
      sudo systemctl disable --now "$u" 2>/dev/null || true
    done || true

# Unit files that were installed but not loaded must also be cleared
sudo systemctl list-unit-files 'acps-*' --no-legend 2>/dev/null \
  | awk '{print $1}' | while read -r u; do
      [[ -n "$u" ]] || continue
      sudo systemctl disable "$u" 2>/dev/null || true
    done || true

sudo rm -f /etc/systemd/system/acps-*.service
sudo systemctl daemon-reload
sudo systemctl reset-failed || true
```

### 3.2 Stop the System Services Used by This Acceptance

Only when this machine is an **acceptance machine** and you have confirmed the databases can be stopped. List them first, then stop — **do not** stop only one hard-coded major:

```bash
sudo systemctl list-units --type=service --all --no-legend 2>/dev/null \
  | awk '{print $1}' | grep -iE '^(postgresql|redis|rabbitmq)' || true

# Stop all postgresql* / redis* / rabbitmq* (names vary by distribution)
sudo systemctl list-units --type=service --all --no-legend 2>/dev/null \
  | awk '{print $1}' | grep -iE '^(postgresql|redis|rabbitmq)' \
  | while read -r u; do
      [[ -n "$u" ]] || continue
      sudo systemctl stop "$u" 2>/dev/null || true
    done || true

# Cover the explicit common names (when list did not find them as loaded)
sudo systemctl stop redis redis-server rabbitmq-server 2>/dev/null || true
sudo systemctl stop postgresql-17 postgresql-16 'postgresql@17-main' 'postgresql@16-main' 2>/dev/null || true
```

### 3.3 Clear the Three Paths + OS Data Directories, and **Recreate** the Empty Home Directories

Do **not** `dnf remove` / `apt purge` the PostgreSQL, Redis, RabbitMQ software packages — what is cleared is the data directories; the packages and the `/etc/redis`, `/etc/rabbitmq` configuration may be kept, and `site.yml` will re-render the configuration according to this run's secrets.

```bash
RUNTIME="${RUNTIME:-/opt/acps}"
DATA="${DATA:-/var/lib/acps}"
LOG="${LOG:-/var/log/acps}"

sudo rm -rf "$RUNTIME" "$DATA" "$LOG"

# Redis / RabbitMQ: after deleting them they must be recreated, otherwise the unit will fail with CHDIR
sudo rm -rf /var/lib/redis /var/lib/rabbitmq
sudo mkdir -p /var/lib/redis /var/lib/rabbitmq
if id redis >/dev/null 2>&1; then sudo chown redis:redis /var/lib/redis; fi
if id rabbitmq >/dev/null 2>&1; then sudo chown rabbitmq:rabbitmq /var/lib/rabbitmq; fi
command -v restorecon >/dev/null && sudo restorecon -Rv /var/lib/redis /var/lib/rabbitmq || true

# Redpanda compatibility symlink (pointing at the deleted $RUNTIME/redpanda)
if [[ -L /opt/redpanda ]] || [[ -e /opt/redpanda ]]; then
  sudo rm -f /opt/redpanda
fi
```

**Mandatory for a clean first-time installation**: wipe the PostgreSQL data directories of **all majors on the disk** (do not delete only the one version mentioned in the documentation). The paths follow the role variable `postgresql_os_data_dir`; scanning is safer than memorizing.

Data directories are mostly owned by `postgres` with mode `700`: as an ordinary user, `[[ -e ]]` on the path / a glob without `sudo` **will "not see" them and skip the deletion**, while the gating step using `sudo find` can still scan out a `PG_VERSION` — it looks like "cleared per the tutorial but still fails the gating step". **Both enumeration and deletion must be done as root** (complete §3.2 stopping the databases first):

```bash
# Rocky / RHEL: /var/lib/pgsql/<major>/data
# Ubuntu: /var/lib/postgresql/<major>/main
# nullglob: do not enter the loop when there is no match; do not do [[ -e ]] as non-root and then sudo rm
sudo bash -c 'shopt -s nullglob
for d in /var/lib/pgsql/*/data /var/lib/postgresql/*/main; do
  echo "wipe PG data: $d"
  rm -rf "$d"
done'
# The installer runs init / pg_createcluster when PG_VERSION is missing (on Ubuntu, if config remains it will drop and then create)
```

If §3.4 still reports a leftover `PG_VERSION`: confirm the corresponding `postgresql*` unit is stopped (if necessary `sudo systemctl disable --now postgresql-17` or the distribution's actual unit name), then re-run the `sudo bash -c` loop above.

Optional extra measure (generally unnecessary): on Ubuntu run `pg_dropcluster --stop <major> main` for the target major, or delete `/etc/postgresql/<major>/main`; the installer already has recovery logic for leftover config. **Just keep the OS packages.**

If this run **deliberately does not** wipe PG, the acceptance record must state "reusing the old database", and the acceptance stance must switch to a non-"clean first-time installation" one.

### 3.4 Gating Step (host)

Quiet ports are only auxiliary; **the deciding criteria are: no `PG_VERSION` anywhere, the three paths deleted, the Redis/RMQ empty home directories recreated, and no `acps-*` units**.

```bash
FAIL=0

if systemctl list-unit-files 'acps-*' --no-legend 2>/dev/null | grep -q .; then
  echo 'FAIL: acps units still registered'
  systemctl list-unit-files 'acps-*' --no-legend
  FAIL=1
else
  echo 'OK: no acps unit files'
fi

test ! -e "${RUNTIME:-/opt/acps}" && echo 'OK: runtime gone' || { echo "FAIL: ${RUNTIME:-/opt/acps} still exists"; FAIL=1; }
test ! -e "${DATA:-/var/lib/acps}" && echo 'OK: data gone' || { echo "FAIL: ${DATA:-/var/lib/acps} still exists"; FAIL=1; }

if test -d /var/lib/redis && test -d /var/lib/rabbitmq; then
  echo 'OK: redis/rabbitmq data dirs exist'
else
  echo 'FAIL: recreate redis/rabbitmq data dirs'
  FAIL=1
fi

# Any leftover PG_VERSION counts as not a clean first-time installation
pg_left="$(sudo find /var/lib/pgsql /var/lib/postgresql \
  \( -path '/var/lib/pgsql/*/data/PG_VERSION' -o -path '/var/lib/postgresql/*/main/PG_VERSION' \) \
  2>/dev/null || true)"
if [[ -n "$pg_left" ]]; then
  echo "FAIL: PostgreSQL data still present:"
  echo "$pg_left"
  FAIL=1
else
  echo 'OK: no PG_VERSION under OS data dirs'
fi

if [[ -e /opt/redpanda ]]; then
  echo 'FAIL: /opt/redpanda still present (remove dangling symlink)'
  FAIL=1
else
  echo 'OK: /opt/redpanda absent'
fi

# Port spot check (add or remove per topology; it should be quiet on a clean machine — services should still be stop)
ss -lntp 2>/dev/null | grep -E ':9001|:9002|:9003|:9005|:9080|:5671|:5432|:6379|:9200|:19092' \
  && echo 'WARN: some ACPs-related ports still listening (expect stop until site.yml)' \
  || echo 'OK: sample ports quiet'

exit "$FAIL"
```

---

## 4. Prohibitions

1. **Do not** recursively `chown` `$DATA` / `/var/lib/acps` (especially the PostgreSQL data) to the deployment user. Container / system user ownership will be broken, and subsequent migration and startup will fail.  
2. **Do not** delete only the tree and leave `acps-*.service`.  
3. **Do not** delete `/var/lib/redis` / `/var/lib/rabbitmq` and then fail to recreate the empty directories and owners.  
4. **Do not** write this tutorial's `down -v` / wipe into a production day-2 runbook.  
5. 〔image〕When clearing the runtime, deal with this acceptance's images and volumes as far as possible at the same time to avoid a half-broken layout; **do not** delete `/etc/docker/daemon.json`.  
6. **Do not** run `docker compose down` bare in the `$RUNTIME` root directory (the project is in `$RUNTIME/compose`, with the default project name `acps`).  
7. **Do not** delete the persistent `vendor-bundle` as a clean-slate target (what should be cleared is the build **output** and this run's unpack / control).  
8. **Do not** "tweak the inventory and install again" on an un-cleared unpack tree or a non-isolated `acps_control_root` and pass it off as a clean first-time installation.  
9. **Do not** clear only `control*` while leaving `~/.local/share/acps/state` or an accidentally present `/opt/acps` on the control node.  
10. **Do not** substitute uninstalling OS packages for clearing data; also **do not** clear only one hard-coded PG major — use **sudo** to scan and delete all `PG_VERSION` (do not do `[[ -e ]]` as non-root and then decide whether to delete).  
11. **Do not** treat "quiet ports" as data already wiped — host must be checked for absence of `PG_VERSION`; image must be checked for absence of project containers / volumes.  
12. **Do not** manually start PostgreSQL / Redis / RabbitMQ after the clean slate and before `site.yml`.  
13. **Do not** rotate the database / middleware passwords in `secrets.yml` without wiping the data and still claim a clean first-time installation.

Firewall: 〔image〕the installer usually only allows ports through idempotently when firewalld/ufw is **already active**; for cross-machine acceptance you may temporarily open ports and tighten them again once it works (see [Three Nodes](./install-package-ansible-deploy-3nodes_en.md)). A clean slate **does not need** to withdraw these rules for a "first-time installation feel".

---

## 5. Install Again After Clearing

Control node (`-i` changed to the actual `hosts.rocky8.yml` / `hosts.rocky9.yml` / `hosts.ubuntu20.yml` / `hosts.ubuntu22.yml` / `hosts.4os-multi.yml` etc. as appropriate):

```bash
cd "$PKG/ansible"
export ANSIBLE_CONFIG="$PKG/ansible/ansible.cfg"

ansible-playbook playbooks/preflight.yml -i inventories/hosts.yml -e @inventories/secrets.yml
ansible-playbook playbooks/site.yml      -i inventories/hosts.yml -e @inventories/secrets.yml
# demo on both + business acceptance needed:
ansible-playbook playbooks/business.yml  -i inventories/hosts.yml -e @inventories/secrets.yml
```

For multi-machine topology and the port table see [Three-Node Deployment](./install-package-ansible-deploy-3nodes_en.md).  
For details of LLM / embedding and other secrets see deployment tutorial §4.3.
