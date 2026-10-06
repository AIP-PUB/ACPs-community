[Home](../README.md)

**[English](app-release-package-build_en.md) | [中文](app-release-package-build.md)**

# Building an Application Release Package from Source

Follow this tutorial to build a set of `*-app-release-*.tar.gz` packages from the ACPs source code. These packages are the shared input for building Docker image packages and **image / host installation packages** later; when the script runs through successfully, every package has already passed basic validation.

Three rules to remember for the build:

1. Business applications are packaged only for the **local architecture** as Linux packages (`linux/arm64` on a Mac, `linux/amd64` on a common Linux server); the other architecture is not built along the way.
2. The built files all land at the **top level** of the output directory; no subdirectory tidying is needed.
3. By default it builds the CPU variant of discovery and the Linux version of `acps-cli`; when you need the GPU variant of discovery or the Mac version of the CLI, just add a flag.

---

## 1. Prepare the Directory

Put the relevant repositories under a single parent directory (exactly as in day-to-day development):

```text
acps/
|-- acps-infra/
|-- acps-sdk/
|-- registry-server/
|-- ca-server/
|-- discovery-server/
|-- monitor-server/
|-- mq-auth-server/
|-- demo-leader/
|-- demo-partner/
`-- acps-cli/
```

The script does not clone repositories or switch branches for you. If any directory is missing, the build fails immediately. You can take a quick look first:

```bash
cd /path/to/acps

for repo in \
  acps-infra acps-sdk registry-server ca-server discovery-server \
  monitor-server mq-auth-server demo-leader demo-partner acps-cli
do
  test -d "$repo" || echo "missing: $repo"
done
```

---

## 2. Prepare the Tools

The local machine needs:

- Docker (the current user must be able to run `docker` / `docker buildx`)
- `just`, `uv` (install them following their own official documentation)
- The `python3` used by the release scripts: at least **3.11**, with **3.14** as the official target
- System tools used for packaging: `tar`, `gzip`, `rsync`, `sha256sum`

The system-provided `python3` is often too old (for example 3.9). Just use uv to install a 3.14 for the **current user** and set it as your default `python3`.

### 2.1 Install Python 3.14 for the Current User with uv

```bash
uv python install 3.14 --default
```

Notes:

- The interpreter itself lives in the user directory (for example `~/.local/share/uv/python/…`) and does not touch the system-level Python.
- By default it places the versioned command `python3.14` in `~/.local/bin`; with `--default` it also places the unversioned `python` and `python3`, both pointing to this 3.14.
- As long as `~/.local/bin` comes before `/usr/bin`, typing `python3` uses this user-local copy rather than the old system-provided version.

If it reports that an existing executable cannot be overwritten, add `--force` (it still affects only the links in the user directory and does not modify system files):

```bash
uv python install 3.14 --default --force
```

### 2.2 Self-Check the Other Tools

```bash
docker version
docker buildx version
just --version
uv --version
python3 --version
uname -sm
```

Then confirm that Docker can run a Linux container **matching the local machine**, for example Apple Silicon:

```bash
docker run --rm --platform linux/arm64 python:3.14-slim python --version
```

On amd64 Linux, replace `linux/arm64` with `linux/amd64`. The first build pulls images and downloads dependencies, so taking longer is normal.

---

## 3. One-Command Build (Default Matrix)

Enter `acps-infra`:

```bash
cd /path/to/acps/acps-infra

release/app-packaging/build-app-release-packages.sh \
  --output /tmp/acps-app-release-output
```

Note: the `--output` directory is emptied and recreated, so do not point it at a path containing important files.

On success you get about **8** packages by default, all at the top level of the output directory, for example:

```bash
ls /tmp/acps-app-release-output/*.tar.gz
```

It will contain the business services, the CPU variant of discovery, and the Linux `acps-cli`. The architecture in the file names is the architecture of this machine.

If the script fails partway through it stops; a successful exit means this batch of packages has passed the structural check and the runtime smoke test. This directory can be used directly to run the [Docker image package tutorial](./docker-image-packages-from-app-release_en.md).

To keep the intermediate work, add `--work-dir /tmp/acps-app-release-work`.

---

## 4. Add Flags per Scenario

### 4.1 On a Mac, Need Both the Darwin and the Linux CLI

When you need to run the acps-cli tool on a system other than Linux — for example, if the Ansible control node for a later installation runs on a Mac — the default Linux packaging of acps-cli is not enough; you also need the Darwin version of `acps-cli`:

```bash
release/app-packaging/build-app-release-packages.sh \
  --output /tmp/acps-app-release-output \
  --cli-target-os darwin,linux
```

The other business packages are still Linux; you simply get one extra `acps-cli-darwin-…`. `darwin` can be requested only on a Mac.

### 4.2 Build Both CPU and GPU discovery on Linux

```bash
release/app-packaging/build-app-release-packages.sh \
  --output /tmp/acps-app-release-output \
  --discovery-variant both
```

You get one extra GPU variant of discovery. Measured on amd64 Linux, that package is about **2.7 GiB** (mostly GPU-related dependencies). Adding `gpu` / `both` on a Mac fails outright. This is because some dependency libraries of the GPU variant of discovery must be compiled from source, and pip does not support cross-compilation; since the target operating system we package for is linux, the toolchain on Linux must be used to compile them, which is what completes the packaging of the GPU variant of discovery.

---

## 5. Build Remotely with Local Uncommitted Code

When local changes are not yet committed or pushed but you want to verify them on a Linux build machine, first make a workspace snapshot on the development machine (it includes uncommitted content and excludes `.git`):

```bash
cd /path/to/acps/acps-infra

# macOS: avoid packing AppleDouble / xattr data into the package, which floods the terminal with warnings when remote GNU tar extracts it
export COPYFILE_DISABLE=1

release/app-packaging/pack-dev-sources.sh \
  --output /tmp/acps-app-release-dev-sources.tar.gz
```

After copying it to the remote host and extracting it, the directory relationships match the local sibling layout:

```bash
scp /tmp/acps-app-release-dev-sources.tar.gz builder.example:/tmp/
ssh builder.example '
  mkdir -p ~/acps-dev-verify && cd ~/acps-dev-verify
  tar -xzf /tmp/acps-app-release-dev-sources.tar.gz
  # If you still see many LIBARCHIVE.xattr.* warnings, they can generally be ignored; setting COPYFILE_DISABLE=1 before packaging locally eliminates them
  cd acps-app-release-dev-sources-*/acps-infra
'
```

Then, in the extracted `acps-infra`, confirm per Section 2.1 that the remote user's `python3` is already 3.14, and run the build commands from Sections 3 and 4. The remote host likewise needs Docker, `just`, and `uv`.

---

## 6. Collect and Assemble Separately (Optional)

The default one-command script collects first and then assembles. When troubleshooting, you can also split the two:

```bash
# Collect only
release/app-packaging/collect-app-release-kit.sh \
  --output /tmp/acps-app-release-work/assembly-kit

# Reuse an existing kit and reassemble (you can add --cli-target-os / --discovery-variant)
release/app-packaging/build-app-release-packages.sh \
  --assembly-kit /tmp/acps-app-release-work/assembly-kit \
  --output /tmp/acps-app-release-output
```

For day-to-day releases, still prefer the one-command script; it is one less thing to worry about.

---

## 7. If Something Goes Wrong, Look Here First

| Symptom | What you can do |
| --- | --- |
| The sibling directory is reported missing | Go back to the parent directory and add the missing repositories |
| `just` / `uv` not found | Install them and put them on `PATH` |
| Docker / buildx errors | Confirm that the current user can run Docker; if necessary add the user to the `docker` group or use `sg docker` |
| `tomllib` is reported missing | Run `uv python install 3.14 --default` as in Section 2.1, and confirm that `which python3` resolves under `~/.local/bin` |
| `.env.example` is missing | Confirm that each application repository contains the file; if you are using a source snapshot, use the current `pack-dev-sources.sh` (it preserves templates and does not delete them by mistake) |
| You need GPU on a Mac, or the Darwin CLI on Linux | Switch to a machine with the matching operating system, or drop the unsuitable flag |
| Remote extraction floods `LIBARCHIVE.xattr.*` | `export COPYFILE_DISABLE=1` before packaging locally, then run `pack-dev-sources.sh`; the warnings usually do not affect the extraction result |
| The first run is especially slow | It is most likely pulling images and dependencies, which is normal; repeated builds are faster |

If you want to rebuild just one application, first collect the kit as in Section 6, then enter the kit directory and invoke `assembly/assemble-and-validate.sh` (the `--platform` architecture must match the local machine). There is no need to take this path day to day.

---

## 8. What to Do Next

- Use these application release packages to continue building Docker image packages: [Build All Docker Image Packages from an Application Release Package](./docker-image-packages-from-app-release_en.md) (just point `--app-release-dir` at this document's `--output`).
- If the image manifest needs application packages of **both architectures**, build one on a build machine of each architecture, then put the top-level `*.tar.gz` files into the same directory.
- What this document's layer does is "pre-assemble the dependencies into a wheelhouse so that deployment can install offline". If the deployment machine can install dependencies over the network, or the target environment cannot use Docker or its operating system is not in the support matrix, you can start deployment directly from this document's upstream input — the **application thin package** produced by `just package wheel` in each project: [Manual Deployment from an Application Thin Package](./manual-deploy-from-app-thin-package_en.md). The deployment steps are the same on both sides; the only difference is whether dependencies are installed offline or over the network.

