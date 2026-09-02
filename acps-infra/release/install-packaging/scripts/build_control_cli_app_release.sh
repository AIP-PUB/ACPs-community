#!/usr/bin/env bash
# Build a real acps-cli app-release tarball for the *control node* platform.
# Consumed by artifacts/control/. Install path must not use source fallback.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${1:-${ROOT}/artifacts/control}"
CLI_SRC="${ACPS_CLI_SRC:-$(cd "${ROOT}/../../../acps-cli" && pwd)}"
SDK_SRC="${ACPS_SDK_SRC:-$(cd "${ROOT}/../../../acps-sdk" && pwd)}"
VERSION="${ACPS_CLI_VERSION:-2.2.0}"
PYTHON_TAG="${ACPS_CLI_PYTHON_TAG:-cp314}"

if [[ ! -f "${CLI_SRC}/pyproject.toml" ]]; then
  echo "[ERROR] acps-cli source not found: ${CLI_SRC}" >&2
  exit 2
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "[ERROR] uv is required to build control-node acps-cli app-release" >&2
  exit 2
fi

mkdir -p "${OUT_DIR}"
OUT_DIR="$(cd "${OUT_DIR}" && pwd)"

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"
case "${arch}" in
  arm64|aarch64) arch=arm64 ;;
  x86_64|amd64) arch=amd64 ;;
esac
platform_slug="${os}-${arch}"
pkg_name="acps-cli-${platform_slug}-${PYTHON_TAG}-app-release-${VERSION}"
work="$(mktemp -d "${TMPDIR:-/tmp}/acps-cli-app-release.XXXXXX")"
trap 'rm -rf "${work}"' EXIT

root="${work}/${pkg_name}"
mkdir -p "${root}/wheelhouse" "${root}/app/dist" "${root}/scripts"

echo "=== Building control-node app-release ${pkg_name} ==="
uv python install 3.14 >/dev/null 2>&1 || true
PY="$(uv python find 3.14)"

uv build --wheel --out-dir "${work}/built" --python "${PY}" "${SDK_SRC}"
uv build --wheel --out-dir "${work}/built" --python "${PY}" "${CLI_SRC}"

# Third-party wheels only first — never let PyPI acps-sdk overwrite local build.
uv export --project "${CLI_SRC}" --no-dev --no-hashes -o "${work}/requirements.txt"
grep -v '^-e' "${work}/requirements.txt" \
  | grep -viE '^(acps-cli|acps-sdk|acps_cli|acps_sdk)([=<>[:space:]]|$)' \
  | grep -v '^#' \
  | grep -v '^[[:space:]]*$' > "${work}/requirements.third.txt"

uv venv "${work}/venv" --python "${PY}"
uv pip install --python "${work}/venv" pip wheel
"${work}/venv/bin/pip" download \
  -d "${root}/wheelhouse" \
  -r "${work}/requirements.third.txt"

# Force local project wheels last (overwrite any registry copies of same version).
cp -f "${work}/built/"*.whl "${root}/wheelhouse/"
cp -f "${work}/built/"acps_cli-*.whl "${root}/app/dist/"
cli_wheel="$(basename "$(ls "${root}/app/dist"/acps_cli-*.whl | head -1)")"

rsync -a --delete "${CLI_SRC}/scripts/" "${root}/scripts/"

cat > "${root}/build-manifest.toml" <<EOF
app = "acps-cli"
version = "${VERSION}"
platform = "${os}/${arch}"
python_tag = "${PYTHON_TAG}"
app_wheel = "${cli_wheel}"
internal_wheels = []
dependency_resolution_strategy = "control-node-uv-export-pip-download"
generated_at = "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
note = "Control-node app-release for image-mode artifacts/control (host=${platform_slug})"
EOF

cat > "${root}/app/runtime-package.toml" <<EOF
[package]
name = "acps-cli"
version = "${VERSION}"
EOF

(
  cd "${root}"
  if command -v sha256sum >/dev/null 2>&1; then
    HASH_CMD=(sha256sum)
  elif command -v shasum >/dev/null 2>&1; then
    HASH_CMD=(shasum -a 256)
  else
    echo "[ERROR] need sha256sum or shasum" >&2
    exit 2
  fi
  find . -type f ! -path './checksums.txt' -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 "${HASH_CMD[@]}" \
    | sed 's#  \./#  #' > checksums.txt
)

# Smoke: offline install from this wheelhouse must yield working acps-cli
uv venv "${work}/smoke" --python "${PY}"
uv pip install --python "${work}/smoke" pip
"${work}/smoke/bin/pip" install --no-index --find-links "${root}/wheelhouse" 'acps-sdk[oidc]' acps-cli
"${work}/smoke/bin/acps-cli" --help >/dev/null
"${work}/smoke/bin/python" -c "import acps_sdk.oidc"

tar_path="${OUT_DIR}/${pkg_name}.tar.gz"
# Avoid macOS AppleDouble (._*) entries inside the tarball.
COPYFILE_DISABLE=1 tar -czf "${tar_path}" -C "${work}" "${pkg_name}"
echo "Wrote ${tar_path}"
ls -lh "${tar_path}"
