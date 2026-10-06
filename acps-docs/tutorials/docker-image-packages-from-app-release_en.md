[Home](../README.md)

**[English](docker-image-packages-from-app-release_en.md) | [中文](docker-image-packages-from-app-release.md)**

# Building Docker Image Packages from an Application Release Package

Follow this tutorial to turn the application release package you built in the previous stage into a set of standalone `.image.tar.gz` files that can be loaded directly with `docker load`. These image packages are the input for assembling the installation package later.

Three things worth remembering:

1. **Only the host architecture is built by default**: the script filters targets by `linux/<host arch>` (on Apple Silicon that is `linux/arm64`), so you no longer need to write `--platform` by hand.
2. **The day-to-day commands are short**: `targets` / `lock` / `strategy` all have in-directory defaults; run `plan` first, then `build`.
3. **The only artifacts are standalone image packages**, laid out flat in the output directory; `acps-images-*.tar` is no longer produced.

The whole pipeline is: source code → application release package → standalone image package → installation package. When delivering a cross-architecture build, prefer shipping the installation package.

> **host-mode does not use this tutorial**: the host installation package consumes the application release package + vendor directly, see [Assembling the Installation Package §2](./install-package-build_en.md).

---

## 1. Preparation

You need:

1. A set of application release packages for the host architecture (see [Building an Application Release Package from Source](./app-release-package-build_en.md)), laid out flat at the top level of a directory.
2. `acps-infra` present on the machine.
3. Docker able to run `docker buildx` and able to run the Linux platform that **matches the host** (for example `linux/arm64` on an arm64 machine).
4. `python3` available (the same as for the application release package build; a user-level 3.14 is recommended).

Self-check:

```bash
cd /path/to/acps/acps-infra

docker version
docker buildx version
python3 --version

# Apple Silicon / arm64
docker run --rm --platform linux/arm64 python:3.14-slim python --version
# On amd64 Linux, switch to linux/amd64
```

Set up the directory variables (use the `--output` from the previous stage directly as the input):

```bash
APP_RELEASE_DIR=/tmp/acps-app-release-output
IMAGE_OUT=/tmp/acps-image-packages

# Clears the image output directory; do not point it at important data
rm -rf "${IMAGE_OUT:?}"
mkdir -p "$IMAGE_OUT"

ls "$APP_RELEASE_DIR"/*.tar.gz
```

By default there are about 8 application release packages (discovery is CPU-only, the CLI is linux-only). If you built GPU or Darwin CLI packages, there will be more; note that **the Darwin CLI release package does not enter the Linux application image matrix**.

The default image inventory is aligned with this: discovery **contains cpu only**; infra contains **fluent-bit** (the AMP Forwarder, in the same category as PG/Redis). **The demo Web no longer gets a separate `demo-nginx` image**: static pages and the same-origin reverse proxy for `/api/v1/` are provided by the `demo-leader-web` entry point inside the `demo-leader` application image. When you need a discovery **gpu** image, first build the GPU application release package, then use `image-targets.with-gpu.toml` (see §6.1).

---

## 2. Planning: Which Image Packages Will Be Built

Before actually building, scan the inputs once and list the expected artifacts for the host platform:

```bash
cd /path/to/acps/acps-infra

release/image-packaging/plan-image-packages.sh \
  --app-release-dir "$APP_RELEASE_DIR"
```

It will:

1. Validate the default `image-targets.toml` and `image-inputs.lock` in the directory
2. Scan `$APP_RELEASE_DIR` for `*-app-release-*.tar.gz` (Darwin packages are annotated as not entering the Linux matrix)
3. List the expected application image packages and infrastructure image packages for the host arch

Without `--output` it only plans: it exits non-zero only when validation fails or the target count after filtering is 0; missing release package inputs produce warnings but still exit 0, so you can get the full picture first.

---

## 3. Building Application Image Packages

```bash
cd /path/to/acps/acps-infra

release/image-packaging/build-app-images.sh \
  --app-release-dir "$APP_RELEASE_DIR" \
  --output "$IMAGE_OUT"
```

When `--platform` is not passed, the log will contain something like "only `linux/<host_arch>` is built by default". Each target will: match the unique application release package → buildx export → `docker load` → smoke test → write out the `.image.tar.gz`. If any target fails, the whole run fails.

---

## 4. Building Infrastructure Image Packages

```bash
cd /path/to/acps/acps-infra

release/image-packaging/build-infra-images.sh \
  --output "$IMAGE_OUT"
```

Here too only the host arch is built by default. Just put the artifacts in the same `$IMAGE_OUT` as the application image packages. Common infrastructure includes: `redis`, `postgres-pgvector`, `rabbitmq`, `gateway-nginx`, `keycloak`, `redpanda`, `victoria-metrics`, `clickhouse`, `minio`, `opensearch`, `fluent-bit`, and so on (subject to `image-targets.toml`). `gateway-nginx` is still built, but **the image-mode installation package does not consume it**; the installation package does not consume `demo-nginx` either.

---

## 5. Verifying the Artifacts

After building, run plan once more, this time with `--output`: it exits non-zero when files are missing or application release package inputs are still missing.

```bash
cd /path/to/acps/acps-infra

release/image-packaging/plan-image-packages.sh \
  --app-release-dir "$APP_RELEASE_DIR" \
  --output "$IMAGE_OUT"

ls "$IMAGE_OUT"/*.image.tar.gz | sort
```

The file names look like:

```text
acps-registry-server-2.2.0-linux-arm64.image.tar.gz
acps-discovery-server-2.2.0-linux-arm64-cpu.image.tar.gz
acps-redis-7-alpine-2.2.0-linux-arm64.image.tar.gz
```

`acps-images-*.tar` **should not** appear any more.

After loading you can confirm it like this (pick either one):

```bash
pkg="$(ls "$IMAGE_OUT"/acps-registry-server-*-linux-*.image.tar.gz | head -n 1)"
docker load -i "$pkg"
docker images | head
```

---

## 6. Advanced Parameters (Can Be Skipped Day to Day)

You do not need these on the day-to-day path. Use them when you need to switch inventories, narrow the matrix, or explicitly override the platform.

### 6.1 Configuration File Defaults

The matrix scripts and `plan-image-packages.sh` use the same-named files under `release/image-packaging/` by default:

| Parameter | Default |
| --- | --- |
| `--targets` | `image-targets.toml` |
| `--lock` | `image-inputs.lock` |
| `--strategy` (application matrix only) | `startup-strategies.toml` |

Example of an explicit override:

```bash
release/image-packaging/build-app-images.sh \
  --app-release-dir "$APP_RELEASE_DIR" \
  --output "$IMAGE_OUT" \
  --targets /path/to/my-image-targets.toml \
  --lock /path/to/my-image-inputs.lock \
  --strategy /path/to/my-startup-strategies.toml
```

When you need discovery **gpu** (a GPU application release package must already exist; it cannot be built on a Mac):

```bash
# Application release package stage (Linux build machine only): --discovery-variant both
release/image-packaging/build-app-images.sh \
  --app-release-dir "$APP_RELEASE_DIR" \
  --output "$IMAGE_OUT" \
  --targets release/image-packaging/image-targets.with-gpu.toml
```

Do not use the global `--variant cpu` / `--variant gpu` to "keep only a certain variant" — `--variant` also filters out application targets that have "no variant".

### 6.2 Filtering a Single Application / Infrastructure

```bash
# Build only one app
release/image-packaging/build-app-images.sh \
  --app-release-dir "$APP_RELEASE_DIR" \
  --output "$IMAGE_OUT" \
  --app registry-server

# Build only one infra
release/image-packaging/build-infra-images.sh \
  --output "$IMAGE_OUT" \
  --id redis
```

### 6.3 `--platform` (Escape Hatch)

When not passed it is already `linux/<host_arch>`. On a Mac → when building a Linux image of the same arch as the host, you **do not** need to write `--platform linux/arm64`.

Use it only when you explicitly want to override the default filtering, for example to debug against an entry of another arch in the reference inventory (for a dual-arch product you should still switch to a build machine of the corresponding architecture rather than relying on local QEMU to build the full matrix):

```bash
release/image-packaging/build-app-images.sh \
  --app-release-dir "$APP_RELEASE_DIR" \
  --output "$IMAGE_OUT" \
  --platform linux/arm64
```

`plan-image-packages.sh` likewise supports `--targets` / `--lock` / `--platform`.

### 6.4 What If You Need Both Architectures

Run the whole sequence on an **amd64** and an **arm64** build machine separately: application release package → image package. Do not expect to build the full dual-architecture product matrix on a single machine with QEMU.

---

## 7. Troubleshooting First

| Symptom | What you can do |
| --- | --- |
| `plan` reports `MISSING_INPUT` (for example discovery gpu) | The default inventory does not contain gpu; if you explicitly used the `with-gpu` targets, build the corresponding application release package, or switch back to the default inventory |
| No matching application release package found | Confirm that the top level of the directory has a `*-app-release-*.tar.gz` for the host arch and the corresponding variant, and that there is exactly one for that combination |
| It says no target remains after filtering | Check whether the inventory contains the host `linux/<arch>`; or see §6.3 |
| Docker / buildx fails | First make sure a container for the corresponding platform can run on the host |
| You still want to generate a collection tar | `build-image-collection.sh` is deprecated; hand `$IMAGE_OUT` directly to the installation package build |

---

## 8. What to Do Next

Hand the standalone `.image.tar.gz` files in `$IMAGE_OUT` to the installation package assembly: [Assembling the Installation Package](./install-package-build_en.md) **§1 (image-mode)**. Image packaging ends here.
