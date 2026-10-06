[Home](../README.md)

**[English](install-package-build_en.md) | [中文](install-package-build.md)**

# Assembling an Installation Package (image-mode / host-mode)

Follow this tutorial to produce, through the single entry point `build-install-package.sh`, an installation package that can be extracted and deployed. Choose the mode first, then follow the corresponding section.

```text
source → application release package ─┬→ [image] standalone image package → acps-image-install-*.tar → Ansible
                                      └→ [host]  vendor bundle            → acps-host-install-*.tar  → Ansible
```

Three things to remember:

1. **Single entry point**: `release/install-packaging/scripts/build-install-package.sh` (`--mode image|host`).
2. **The application release package is shared by both modes** (see [Building an Application Release Package from Source](./app-release-package-build_en.md)); image needs one extra step for the image packages, host needs the vendor directory prepared.
3. **The control node CLI platform** uses `--control-platform` (for example `darwin-arm64` on a Mac); it may differ from the business machine platform.

For deployment, see [Deploying ACPs with an Installation Package](./install-package-ansible-deploy_en.md).

---

## 0. Choose the Mode First

| | **image** | **host** |
| --- | --- | --- |
| Command | `--mode image` (may be omitted; the default) | `--mode host` (required) |
| Input | flat `*.image.tar.gz` + app-release | app-release + `--vendor-bundle-dir` |
| Business platform parameter | `--image-platform` | `--target-platform` (`--image-platform` also works as an alias) |
| Artifact | `acps-image-install-{ver}-{plat}.tar` | `acps-host-install-{ver}-{plat}.tar` |
| Package contents | `artifacts/images/` + `control/` | `artifacts/apps\|control\|vendor/` (**no** `images/`, **no** `bin/acps-install`) |
| Business machine | any Linux that can run Docker (matching arch) | **only** Rocky 8/9 / Ubuntu 20.04/22.04 |

---

## 1. image-mode: Assembling from Image Packages

### 1.1 Preparation

1. A flat directory of image packages (see [Building Docker Image Packages from an Application Release Package](./docker-image-packages-from-app-release_en.md)). To enable the **AMP Forwarder**, **fluent-bit** must be built first. **The demo Web does not need a separate image**: just install the `demo-leader` application image (Compose starts `demo_leader` + `demo_leader_web`, default port **9030**).
2. An application release package directory containing at least the `acps-cli-*-app-release-*.tar.gz` for the control node platform.
3. `acps-infra` present locally, with `python3` available.

```bash
cd /path/to/acps/acps-infra

APP_RELEASE_DIR=/tmp/acps-app-release-output
IMAGE_OUT=/tmp/acps-image-packages
INSTALL_OUT=/tmp/acps-install-packages

# Example on a local Apple Silicon machine
IMAGE_PLATFORM=linux-arm64
CONTROL_PLATFORM=darwin-arm64

ls "$IMAGE_OUT"/*.image.tar.gz | head
ls "$APP_RELEASE_DIR"/acps-cli-*-app-release-*.tar.gz
```

After the amd64 images built on the builder are copied back to the local machine: `IMAGE_PLATFORM=linux-amd64`, while `CONTROL_PLATFORM` still uses the local CLI.

### 1.2 One-Command Assembly

```bash
cd /path/to/acps/acps-infra/release/install-packaging
mkdir -p "$INSTALL_OUT"

./scripts/build-install-package.sh \
  --mode image \
  --image-dir "$IMAGE_OUT" \
  --app-release-dir "$APP_RELEASE_DIR" \
  --image-platform "$IMAGE_PLATFORM" \
  --control-platform "$CONTROL_PLATFORM" \
  --out-dir "$INSTALL_OUT"
```

On success the output looks similar to:

```text
/tmp/acps-install-packages/acps-image-install-2.2.0-linux-arm64.tar
```

Inside the package, `artifacts/images/` holds the **long-named** image packages; the assembly stage does **not** run `docker pull` / save again. You can check it like this:

```bash
tar -tf "$INSTALL_OUT"/acps-image-install-*.tar | grep -E 'artifacts/(images|control)/' | sort
# demo-nginx should no longer appear; if the AMP Forwarder is enabled there should be a long-named fluent-bit package
```

### 1.3 Platform Combinations (image)

| Scenario | `--image-platform` | `--control-platform` |
| --- | --- | --- |
| Build an arm64 business package on a Mac, control from the local machine | `linux-arm64` | `darwin-arm64` |
| amd64 images on the builder, Mac as control | `linux-amd64` | `darwin-arm64` |
| Linux amd64 end to end | `linux-amd64` | `linux-amd64` |

The Darwin CLI goes only into `artifacts/control/`, never into `artifacts/images/`.

### 1.4 demo Web and fluent-bit (image)

| Capability | File source | Notes |
| --- | --- | --- |
| demo-leader Web UI | `demo-leader` **application** image | Compose: `demo_leader` + `demo_leader_web`; port **9030**; same-origin `/api/v1/` reverse proxy; `backendBase=''`. The installation package does **not** use a standalone `demo-nginx`. |
| AMP Forwarder | infra **`fluent-bit`** | It must be packed into the installation package during the image-packaging stage. |

`gateway-nginx` may appear in the image inventory, but it does **not** enter the installation consumption inventory.

---

## 2. host-mode: Assembling from app-release + vendor

### 2.1 Preparation

1. An application release package directory (the same one as for image is fine): business app-release + control node `acps-cli-*-app-release-*.tar.gz`. Image packages are **not** needed.
2. **Vendor packages**: the default cache directory is `release/install-packaging/.vendor-bundle/` (already gitignored). During the build, `ensure_vendor_bundle` fills it in automatically according to the **url + sha256** of every `[vendor.*]` entry in `baseline-matrix.toml`: if a file is present and passes verification it is reused; if missing it is downloaded (some components undergo a fetch transformation, such as MinIO wrapped in a tar layer, ClickHouse flattened, fluent-bit reorganized from a deb). You can also **place the correct files into the cache directory manually** to speed up the build, or pre-place them in an offline environment.
3. The target business machines are **Rocky 8/9 or Ubuntu 20.04/22.04** (arch matching `--target-platform`).
4. Optional: `--bundle-python-dir` packs an offline `tools/` into the package; `--vendor-offline` forbids downloading (use only the existing cache).

```bash
cd /path/to/acps/acps-infra

APP_RELEASE_DIR=/tmp/acps-app-release-output
INSTALL_OUT=/tmp/acps-install-packages

# Example: amd64 business machines with a Mac as the control node
TARGET_PLATFORM=linux-amd64
CONTROL_PLATFORM=darwin-arm64

ls "$APP_RELEASE_DIR"/*-app-release-*.tar.gz | head
```

When you only need to pre-fetch the vendor packages separately (optional):

```bash
cd /path/to/acps/acps-infra/release/install-packaging
python3 scripts/ensure_vendor_bundle.py \
  --matrix baseline-matrix.toml \
  --arch amd64 \
  --cache-dir .vendor-bundle
```

### 2.2 One-Command Assembly

```bash
cd /path/to/acps/acps-infra/release/install-packaging
mkdir -p "$INSTALL_OUT"

./scripts/build-install-package.sh \
  --mode host \
  --app-release-dir "$APP_RELEASE_DIR" \
  --target-platform "$TARGET_PLATFORM" \
  --control-platform "$CONTROL_PLATFORM" \
  --out-dir "$INSTALL_OUT"
# By default the .vendor-bundle/ of this tree is used; --vendor-bundle-dir /other-cache is also available
# Offline environment: first put the needed files into the cache, then add --vendor-offline
```

On success the output looks similar to:

```text
/tmp/acps-install-packages/acps-host-install-2.2.0-linux-amd64.tar
```

You can check it like this:

```bash
tar -tf "$INSTALL_OUT"/acps-host-install-*.tar | grep -E 'artifacts/(apps|control|vendor)/' | sort
# baseline-matrix.toml should be present; artifacts/images/ or bin/acps-install should not be
```

During the build, `acps_deploy_mode=host` and similar values are written back into the package's `group_vars`. The vendor url / `sha256_amd64|arm64` are written in `baseline-matrix.toml`; to upgrade a vendor version, change the set of three: version+url+sha256.

### 2.3 Platform Combinations (host)

| Scenario | `--target-platform` | `--control-platform` |
| --- | --- | --- |
| Rocky/Ubuntu amd64, Mac as control | `linux-amd64` | `darwin-arm64` |
| Linux amd64 end to end | `linux-amd64` | `linux-amd64` |
| arm64 business machines | `linux-arm64` | the same as the control node CLI |

For arm64, if `sha256_arm64` in the matrix is still empty: ensure can still download, but it will warn; before a product release, the printed sha256 should be filled back into the matrix.

---

## 3. How to Use It After Extraction (Preview)

Both modes are extracted the same way; only the package name differs:

```bash
rm -rf /tmp/acps-install-verify && mkdir -p /tmp/acps-install-verify
# image:
tar -xf "$INSTALL_OUT"/acps-image-install-*.tar -C /tmp/acps-install-verify
# or host:
# tar -xf "$INSTALL_OUT"/acps-host-install-*.tar -C /tmp/acps-install-verify

PKG=$(echo /tmp/acps-install-verify/acps-*-install-*-linux-*)
cp -a "$PKG/ansible/inventories/secrets.example.yml" "$PKG/ansible/inventories/secrets.yml"
# inventory: image commonly uses hosts.example.yml; host commonly uses hosts.rocky9.yml / hosts.ubuntu22.yml / hosts.rocky8.yml / hosts.ubuntu20.yml
```

For the complete steps, see [Deploying ACPs with an Installation Package](./install-package-ansible-deploy_en.md). For renewal / trust / upgrade / rollback after installation, see [Day-2 Operations](./install-package-day2-ops_en.md).

---

## 4. Advanced Notes (Can Be Skipped in Daily Use)

- The old `ingest_image_artifacts.sh` / `materialize_artifacts.sh` are deprecated; `assemble_install_package.sh` only forwards. Use `build-install-package.sh`.
- image and host **share** the same Ansible tree; at deployment time, `acps_deploy_mode` and `stage_artifact_{{mode}}_{{kind}}` decide how things are placed.
- Deprecated `acps-images-*.tar` packages are not consumed.
- For finer-grained gating steps and operations entry points, see `acps-infra/release/install-packaging/README.md`; for a step-by-step operations tutorial, see [Day-2 Operations](./install-package-day2-ops_en.md).

---

## 5. If Something Goes Wrong, Look Here First

| Symptom | What you can do |
| --- | --- |
| No image matching the platform (image) | Check whether the filenames under `$IMAGE_OUT` contain `linux-arm64` / `linux-amd64` |
| Multiple CLIs; control cannot be inferred | Specify `--control-platform` explicitly |
| AMP Forwarder is missing its image (image) | Build `fluent-bit` in image-packaging, then rebuild the installation package |
| Missing vendor items / glob matches several entries (host) | Check the ensure log; compare against the url/file in `baseline-matrix.toml`; or place them into `.vendor-bundle/` manually |
| Vendor download fails / sha mismatch | Check external network access and the url; a wrong file is deleted and re-fetched; before using `--vendor-offline`, prepare the correct cache files first |
| Mixing up mode parameters | image requires `--image-dir`; host must not pass `--image-dir` |

---

## 6. What to Do Next

[Deploying ACPs with an Installation Package](./install-package-ansible-deploy_en.md) (the same document for image / host, with separate explanations per mode). When using image and the local Apple Silicon control node is also a business node, see **§4.7** of that tutorial.
