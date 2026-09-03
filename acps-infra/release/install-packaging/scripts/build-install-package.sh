#!/usr/bin/env bash
# 从正式产出组装安装包；支持两种承载模式（简化决策 2026-07-23 + host 直装设计 2026-07-24 §3）。
#
# --mode image（默认，向后兼容）：
#   有什么镜像包打什么；包内保留长名 *.image.tar.gz；不改名、不 pull 旁路镜像。
#   build-install-package.sh \
#     --image-dir /tmp/acps-image-packages \
#     --app-release-dir /tmp/acps-app-release-output \
#     --image-platform linux-arm64 \
#     --control-platform darwin-arm64 \
#     --out-dir ./dist
#
# --mode host：不消费镜像包；打业务 app-release + 控制 CLI + 厂商包（Keycloak/AMP，按
#   baseline-matrix 解析；缺缓存则按 url 自动下载到 .vendor-bundle/）；不产出 images/、
#   不产出 bin/acps-install。
#   build-install-package.sh --mode host \
#     --app-release-dir /tmp/acps-app-release-output \
#     --target-platform linux-amd64 \
#     --control-platform darwin-arm64 \
#     --out-dir ./dist
#   # 可选：--vendor-bundle-dir /path/to/cache（默认本树 .vendor-bundle）
#   # 可选：--vendor-offline（禁止下载，仅用已有缓存）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ROOT}/.venv-tools/bin/python"
[[ -x "${PY}" ]] || PY="$(command -v python3)"

MODE="image"
IMAGE_DIR=""
APP_RELEASE_DIR=""
VENDOR_BUNDLE_DIR=""
VENDOR_OFFLINE=0
BUNDLE_PYTHON_DIR=""
BASELINE_MATRIX=""
IMAGE_PLATFORM=""
TARGET_PLATFORM=""
CONTROL_PLATFORM=""
OUT_DIR=""
MANIFEST="${ROOT}/release-manifest.toml"
# 全局（非 local）：EXIT trap 在函数返回后触发，local 变量此时已出作用域。
stage=""

detect_host_linux_slug() {
  case "$(uname -m)" in
    x86_64|amd64) echo "linux-amd64" ;;
    arm64|aarch64) echo "linux-arm64" ;;
    *)
      echo "[ERROR] 不支持的 arch：$(uname -m)" >&2
      exit 2
      ;;
  esac
}

detect_host_control_slug() {
  local os arch
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"
  case "${arch}" in
    x86_64|amd64) arch=amd64 ;;
    arm64|aarch64) arch=arm64 ;;
  esac
  echo "${os}-${arch}"
}

usage() {
  cat <<'EOF'
用法：
  build-install-package.sh --mode image --image-dir <dir> --app-release-dir <dir> [选项]
  build-install-package.sh --mode host  --app-release-dir <dir> --target-platform <slug> [选项]

--mode（默认 image；向后兼容既有调用方）：
  image   消费 image-packaging 镜像包 + app-release；产出 acps-image-install-*（含 images/、bin/acps-install）
  host    消费 app-release + 厂商包（Keycloak/AMP）；产出 acps-host-install-*（不含 images/、不产出 bin/acps-install）

--mode image 必填：
  --image-dir <dir>           image-packaging 平铺 *.image.tar.gz
  --app-release-dir <dir>     含控制节点 acps-cli-*-app-release-*.tar.gz 与业务 app-release

--mode image 可选：
  --image-platform <slug>     默认本机 linux-<arch>（linux-arm64 / linux-amd64）
  --manifest <path>           默认本树 release-manifest.toml

--mode host 必填：
  --app-release-dir <dir>     业务 app-release + 控制 CLI（与 image 相同目录即可）
  --target-platform <slug>    业务机 linux-<arch>（linux-arm64 / linux-amd64）；亦可用 --image-platform 作为别名

--mode host 可选：
  --vendor-bundle-dir <dir>   厂商包缓存目录（默认本树 .vendor-bundle/）；可手动预置加速
  --vendor-offline            禁止从 url 下载；缓存缺失或 sha256 不匹配则失败
  --bundle-python-dir <dir>   打入 tools/：仅 pinned uv + CPython standalone
  --baseline-matrix <path>    默认本树 baseline-matrix.toml，其次 baseline-matrix.host.toml

两模式共用可选：
  --control-platform <slug>   默认：唯一 CLI 或本机 OS/arch；多份时建议显式指定
  --out-dir <dir>             默认 ./dist

行为（image）：
  - 按 image_platform 过滤并原样拷贝匹配的长名镜像包（有什么打什么）
  - 拷贝匹配 control_platform 的 CLI 发布包
  - 打入 ansible / templates / scripts / manifest（双写 platform，改写 tag 后缀）
  - 不消费 acps-images-*.tar；不在本阶段 pull/save nginx 或 fluent-bit

行为（host）：
  - 按 target_platform 过滤并拷贝匹配的业务 app-release（discovery 等 variant 按文件名识别）
  - 拷贝匹配 control_platform 的 CLI 发布包
  - 构建前 ensure_vendor_bundle：按 baseline-matrix [vendor.*] 的 url/sha256 填充缓存目录
    （存在且校验通过则复用；否则下载，可选 fetch 变换）；再拷入 artifacts/vendor/
  - 打入 ansible / templates / scripts + baseline-matrix.toml + 构建期生成 release-manifest.toml
    （含 artifact_kind）；回写 group_vars（acps_deploy_mode=host、平台、Python 版本）
  - 不产出 artifacts/images/、不产出 bin/acps-install
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) shift; MODE="${1:-}" ;;
    --image-dir) shift; IMAGE_DIR="${1:-}" ;;
    --app-release-dir) shift; APP_RELEASE_DIR="${1:-}" ;;
    --vendor-bundle-dir) shift; VENDOR_BUNDLE_DIR="${1:-}" ;;
    --vendor-offline) VENDOR_OFFLINE=1 ;;
    --bundle-python-dir) shift; BUNDLE_PYTHON_DIR="${1:-}" ;;
    --baseline-matrix) shift; BASELINE_MATRIX="${1:-}" ;;
    --image-platform) shift; IMAGE_PLATFORM="${1:-}" ;;
    --target-platform) shift; TARGET_PLATFORM="${1:-}" ;;
    --control-platform) shift; CONTROL_PLATFORM="${1:-}" ;;
    --out-dir) shift; OUT_DIR="${1:-}" ;;
    --manifest) shift; MANIFEST="${1:-}" ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "[ERROR] 未知参数：$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

case "${MODE}" in
  image|host) ;;
  *)
    echo "[ERROR] --mode 须为 image 或 host，收到：${MODE}" >&2
    usage >&2
    exit 2
    ;;
esac

# --- 两模式共用：解析控制节点 CLI（写入 CLI_PATH / CONTROL_PLATFORM） ---
resolve_control_cli() {
  CLI_PATH="$("${PY}" - <<'PY' "${APP_RELEASE_DIR}" "${CONTROL_PLATFORM}"
import platform
import sys
from pathlib import Path

app_dir = Path(sys.argv[1])
control = (sys.argv[2] or "").strip().replace("/", "-")
all_cli = sorted(app_dir.glob("acps-cli-*-app-release-*.tar.gz"))
if not all_cli:
    raise SystemExit(f"[ERROR] {app_dir} 下找不到 acps-cli-*-app-release-*.tar.gz")

def pick(slug: str):
    matched = [p for p in all_cli if p.name.startswith(f"acps-cli-{slug}-")]
    if not matched:
        avail = ", ".join(p.name for p in all_cli)
        raise SystemExit(f"[ERROR] 无 control_platform={slug} 的 CLI；可用：{avail}")
    if len(matched) > 1:
        raise SystemExit(f"[ERROR] control_platform={slug} 匹配多个：" + ", ".join(p.name for p in matched))
    return matched[0]

if control:
    print(pick(control))
elif len(all_cli) == 1:
    print(all_cli[0])
else:
    os_name = platform.system().lower()
    machine = platform.machine()
    arch = "arm64" if machine in ("arm64", "aarch64") else ("amd64" if machine in ("x86_64", "amd64") else machine)
    print(pick(f"{os_name}-{arch}"))
PY
)"

  if [[ -z "${CONTROL_PLATFORM}" ]]; then
    local base
    base="$(basename "${CLI_PATH}")"
    if [[ "${base}" =~ ^acps-cli-([a-z0-9]+)-([a-z0-9]+)- ]]; then
      CONTROL_PLATFORM="${BASH_REMATCH[1]}-${BASH_REMATCH[2]}"
    else
      echo "[ERROR] 无法从 ${base} 推断 control_platform" >&2
      exit 2
    fi
  fi
  CONTROL_PLATFORM="${CONTROL_PLATFORM//\//-}"
}

# --- 两模式共用：装配 ansible / templates / scripts / inventories 示例（不含 manifest） ---
assemble_shared_tree() {
  local dest="$1"

  rsync -a --delete \
    --exclude '.venv-tools' \
    --exclude '.build' \
    --exclude 'dist' \
    --exclude 'artifacts' \
    --exclude 'ansible/inventories/hosts.yml' \
    --exclude 'ansible/inventories/secrets.yml' \
    --exclude 'ansible/inventories/host_vars' \
    --exclude 'ansible/inventories/ca-materials/*.crt' \
    --exclude 'ansible/inventories/ca-materials/*.key' \
    --exclude 'ansible/inventories/ca-materials/*.pem' \
    --exclude 'ansible/inventories/ca-materials/offline' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.gitignore' \
    "${ROOT}/ansible/" "${dest}/ansible/"

  # Ensure ca-materials scaffold ships (README +.gitkeep; never local certs)
  mkdir -p "${dest}/ansible/inventories/ca-materials"
  cp -a "${ROOT}/ansible/inventories/ca-materials/README.md" "${dest}/ansible/inventories/ca-materials/"
  cp -a "${ROOT}/ansible/inventories/ca-materials/.gitkeep" "${dest}/ansible/inventories/ca-materials/"

  mkdir -p "${dest}/inventories"
  cp -a "${ROOT}/ansible/inventories/hosts.example.yml" "${dest}/inventories/"
  cp -a "${ROOT}/ansible/inventories/secrets.example.yml" "${dest}/inventories/"
  cp -a "${ROOT}/ansible/inventories/hosts.example.yml" "${dest}/ansible/inventories/"
  cp -a "${ROOT}/ansible/inventories/secrets.example.yml" "${dest}/ansible/inventories/"
  if [[ -d "${ROOT}/ansible/inventories/host_vars" ]]; then
    mkdir -p "${dest}/ansible/inventories/host_vars" "${dest}/inventories/host_vars"
    cp -a "${ROOT}/ansible/inventories/host_vars/." "${dest}/ansible/inventories/host_vars/"
    cp -a "${ROOT}/ansible/inventories/host_vars/." "${dest}/inventories/host_vars/"
  fi
  cp -a "${ROOT}/ansible/inventories/group_vars" "${dest}/ansible/inventories/"
  mkdir -p "${dest}/inventories/group_vars"
  cp -a "${ROOT}/ansible/inventories/group_vars/." "${dest}/inventories/group_vars/"

  rsync -a "${ROOT}/templates/" "${dest}/templates/"
  rsync -a "${ROOT}/scripts/" "${dest}/scripts/"
  cp -a "${ROOT}/README.md" "${dest}/"
}

# --- 两模式共用：清理 AppleDouble、写 checksums.txt、打 tar ---
finalize_package() {
  local dest="$1" stage="$2" pkg_name="$3" out_dir="$4"

  find "${dest}" \( -name '._*' -o -name '.DS_Store' \) -type f -delete
  export COPYFILE_DISABLE=1

  # Portable SHA-256: Linux sha256sum / macOS shasum.
  local -a hash_cmd
  if command -v sha256sum >/dev/null 2>&1; then
    hash_cmd=(sha256sum)
  elif command -v shasum >/dev/null 2>&1; then
    hash_cmd=(shasum -a 256)
  else
    echo "[ERROR] need sha256sum or shasum to write checksums.txt" >&2
    exit 2
  fi

  (
    cd "${dest}"
    find . -type f ! -path './checksums.txt' -print0 \
      | LC_ALL=C sort -z \
      | xargs -0 "${hash_cmd[@]}" \
      | sed 's#  \./#  #' > checksums.txt
  )

  TAR_PATH="${out_dir}/${pkg_name}.tar"
  COPYFILE_DISABLE=1 tar -cf "${TAR_PATH}" -C "${stage}" "${pkg_name}"
  echo "Wrote ${TAR_PATH}"
  ls -lh "${TAR_PATH}"
}

# ============================== --mode image ==============================
build_image() {
  if [[ -z "${IMAGE_DIR}" || -z "${APP_RELEASE_DIR}" ]]; then
    echo "[ERROR] --mode image 下 --image-dir 与 --app-release-dir 均为必填" >&2
    usage >&2
    exit 2
  fi
  if [[ ! -d "${IMAGE_DIR}" ]]; then
    echo "[ERROR] --image-dir 不存在：${IMAGE_DIR}" >&2
    exit 2
  fi
  if [[ ! -d "${APP_RELEASE_DIR}" ]]; then
    echo "[ERROR] --app-release-dir 不存在：${APP_RELEASE_DIR}" >&2
    exit 2
  fi
  if [[ ! -f "${MANIFEST}" ]]; then
    echo "[ERROR] manifest 不存在：${MANIFEST}" >&2
    exit 2
  fi

  IMAGE_PLATFORM="${IMAGE_PLATFORM:-$(detect_host_linux_slug)}"
  IMAGE_PLATFORM="${IMAGE_PLATFORM//\//-}"
  if [[ ! "${IMAGE_PLATFORM}" =~ ^linux-(amd64|arm64)$ ]]; then
    echo "[ERROR] --image-platform 须为 linux-amd64 或 linux-arm64，收到：${IMAGE_PLATFORM}" >&2
    exit 2
  fi

  OUT_DIR="${OUT_DIR:-${ROOT}/dist}"
  mkdir -p "${OUT_DIR}"
  OUT_DIR="$(cd "${OUT_DIR}" && pwd)"

  local version
  version="$("${PY}" - <<'PY' "${MANIFEST}"
import tomllib, sys
from pathlib import Path
print(tomllib.loads(Path(sys.argv[1]).read_text())["meta"]["acps_version"])
PY
)"

  # 注意：stage 不可 local——EXIT trap 在函数返回后（脚本退出时）才触发，
  # 若 local 则 trap 触发时变量已出作用域，set -u 下报 unbound variable。
  local pkg_name dest
  pkg_name="acps-image-install-${version}-${IMAGE_PLATFORM}"
  stage="$(mktemp -d "${TMPDIR:-/tmp}/${pkg_name}.XXXXXX")"
  trap 'rm -rf "${stage}"' EXIT
  dest="${stage}/${pkg_name}"
  mkdir -p "${dest}/artifacts/images" "${dest}/artifacts/control"

  echo "=== Building ${pkg_name} ==="
  echo "image_platform=${IMAGE_PLATFORM}"
  echo "image-dir=${IMAGE_DIR}"
  echo "app-release-dir=${APP_RELEASE_DIR}"

  # --- images: copy long names matching platform slug ---
  local copied=0
  shopt -s nullglob
  local f base
  for f in "${IMAGE_DIR}"/*.image.tar.gz; do
    base="$(basename "${f}")"
    # host-arch filter: require -<image_platform> before.image.tar.gz or -cpu/-gpu
    if [[ "${base}" != *"-${IMAGE_PLATFORM}.image.tar.gz" \
       && "${base}" != *"-${IMAGE_PLATFORM}-cpu.image.tar.gz" \
       && "${base}" != *"-${IMAGE_PLATFORM}-gpu.image.tar.gz" ]]; then
      continue
    fi
    cp -a "${f}" "${dest}/artifacts/images/"
    copied=$((copied + 1))
    echo "  image  ${base}"
  done
  shopt -u nullglob

  if [[ "${copied}" -eq 0 ]]; then
    echo "[ERROR] ${IMAGE_DIR} 下没有匹配 ${IMAGE_PLATFORM} 的 *.image.tar.gz" >&2
    exit 1
  fi
  echo "copied ${copied} image archive(s)"

  # --- control CLI ---
  resolve_control_cli
  cp -a "${CLI_PATH}" "${dest}/artifacts/control/"
  echo "control_platform=${CONTROL_PLATFORM} ← $(basename "${CLI_PATH}")"

  # --- tree: ansible / templates / scripts / inventories examples ---
  assemble_shared_tree "${dest}"
  cp -a "${MANIFEST}" "${dest}/release-manifest.toml"

  # Nail dual platforms + rewrite tag suffixes
  "${PY}" - <<PY "${dest}/release-manifest.toml" "${IMAGE_PLATFORM}" "${CONTROL_PLATFORM}"
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
image_platform, control_platform = sys.argv[2], sys.argv[3]
text = path.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)
out = []
in_meta = False
seen = {"image_platform": False, "control_platform": False, "platform": False}
for line in lines:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        if in_meta:
            if not seen["image_platform"]:
                out.append(f'image_platform = "{image_platform}"\n')
            if not seen["control_platform"]:
                out.append(f'control_platform = "{control_platform}"\n')
            if not seen["platform"]:
                out.append(f'platform = "{image_platform}"\n')
        in_meta = stripped == "[meta]"
        out.append(line)
        continue
    if in_meta and "=" in stripped and not stripped.startswith("#"):
        key = stripped.split("=", 1)[0].strip()
        if key == "image_platform":
            out.append(f'image_platform = "{image_platform}"\n')
            seen["image_platform"] = True
            continue
        if key == "control_platform":
            out.append(f'control_platform = "{control_platform}"\n')
            seen["control_platform"] = True
            continue
        if key == "platform":
            out.append(f'platform = "{image_platform}"\n')
            seen["platform"] = True
            continue
    out.append(line)
if in_meta:
    if not seen["image_platform"]:
        out.append(f'image_platform = "{image_platform}"\n')
    if not seen["control_platform"]:
        out.append(f'control_platform = "{control_platform}"\n')
    if not seen["platform"]:
        out.append(f'platform = "{image_platform}"\n')
text = "".join(out)
pat = re.compile(
    r'(tag\s*=\s*"[^"]*?-)(linux-(?:amd64|arm64))((?:-(?:cpu|gpu))?")'
)
text, n = pat.subn(lambda m: f"{m.group(1)}{image_platform}{m.group(3)}", text)
path.write_text(text, encoding="utf-8")
print(f"manifest: image_platform={image_platform} control_platform={control_platform} tag_rewrites={n}")
PY

  "${PY}" - <<PY "${dest}/ansible/inventories/group_vars/all.yml" "${IMAGE_PLATFORM}" "${CONTROL_PLATFORM}"
from pathlib import Path
import sys
path = Path(sys.argv[1])
image_platform, control_platform = sys.argv[2], sys.argv[3]
lines = []
for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
    if line.startswith("acps_platform:"):
        lines.append(f"acps_platform: {image_platform}\n")
    elif line.startswith("acps_control_platform:"):
        lines.append(f'acps_control_platform: "{control_platform}"\n')
    else:
        lines.append(line)
path.write_text("".join(lines), encoding="utf-8")
PY
  cp -a "${dest}/ansible/inventories/group_vars/all.yml" "${dest}/inventories/group_vars/all.yml"

  mkdir -p "${dest}/bin" "${dest}/docs"
  cat > "${dest}/bin/acps-install" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ANSIBLE_CONFIG="${ROOT}/ansible/ansible.cfg"
INV="${1:-${ROOT}/ansible/inventories/hosts.yml}"
SECRETS="${2:-${ROOT}/ansible/inventories/secrets.yml}"
shift $(( $# >= 1 ? 1 : 0 )) || true
shift $(( $# >= 1 ? 1 : 0 )) || true
exec ansible-playbook -i "${INV}" "${ROOT}/ansible/playbooks/site.yml" -e @"${SECRETS}" "$@"
EOF
  chmod +x "${dest}/bin/acps-install"

  finalize_package "${dest}" "${stage}" "${pkg_name}" "${OUT_DIR}"
  echo "images: ${copied}"
  echo "control: $(basename "${CLI_PATH}")"
  echo "platforms: image=${IMAGE_PLATFORM} control=${CONTROL_PLATFORM}"
}

# ============================== --mode host ===============================
build_host() {
  if [[ -z "${APP_RELEASE_DIR}" ]]; then
    echo "[ERROR] --mode host 下 --app-release-dir 为必填" >&2
    usage >&2
    exit 2
  fi
  if [[ ! -d "${APP_RELEASE_DIR}" ]]; then
    echo "[ERROR] --app-release-dir 不存在：${APP_RELEASE_DIR}" >&2
    exit 2
  fi

  # --target-platform 必填；--image-platform 可作别名传入
  TARGET_PLATFORM="${TARGET_PLATFORM:-${IMAGE_PLATFORM}}"
  if [[ -z "${TARGET_PLATFORM}" ]]; then
    echo "[ERROR] --mode host 下 --target-platform 为必填（或用 --image-platform 作为别名）" >&2
    usage >&2
    exit 2
  fi
  TARGET_PLATFORM="${TARGET_PLATFORM//\//-}"
  if [[ ! "${TARGET_PLATFORM}" =~ ^linux-(amd64|arm64)$ ]]; then
    echo "[ERROR] --target-platform 须为 linux-amd64 或 linux-arm64，收到：${TARGET_PLATFORM}" >&2
    exit 2
  fi
  local target_arch="${TARGET_PLATFORM#linux-}"

  if [[ -z "${BASELINE_MATRIX}" ]]; then
    if [[ -f "${ROOT}/baseline-matrix.toml" ]]; then
      BASELINE_MATRIX="${ROOT}/baseline-matrix.toml"
    elif [[ -f "${ROOT}/baseline-matrix.host.toml" ]]; then
      BASELINE_MATRIX="${ROOT}/baseline-matrix.host.toml"
    else
      echo "[ERROR] 未找到本树 baseline-matrix.toml / baseline-matrix.host.toml，且未显式指定 --baseline-matrix" >&2
      exit 2
    fi
  fi
  if [[ ! -f "${BASELINE_MATRIX}" ]]; then
    echo "[ERROR] --baseline-matrix 不存在：${BASELINE_MATRIX}" >&2
    exit 2
  fi

  # 厂商包缓存：默认本树 .vendor-bundle/；可手动预置或由 ensure 按 url 拉取
  if [[ -z "${VENDOR_BUNDLE_DIR}" ]]; then
    VENDOR_BUNDLE_DIR="${ROOT}/.vendor-bundle"
  fi
  mkdir -p "${VENDOR_BUNDLE_DIR}"
  VENDOR_BUNDLE_DIR="$(cd "${VENDOR_BUNDLE_DIR}" && pwd)"

  if [[ -n "${BUNDLE_PYTHON_DIR}" && ! -d "${BUNDLE_PYTHON_DIR}" ]]; then
    echo "[ERROR] --bundle-python-dir 不存在：${BUNDLE_PYTHON_DIR}" >&2
    exit 2
  fi

  echo "=== Ensuring host vendor cache ==="
  echo "vendor-bundle-dir=${VENDOR_BUNDLE_DIR}"
  echo "baseline-matrix=${BASELINE_MATRIX}"
  echo "arch=${target_arch} offline=${VENDOR_OFFLINE}"
  local ensure_args=(
    "${ROOT}/scripts/ensure_vendor_bundle.py"
    --matrix "${BASELINE_MATRIX}"
    --arch "${target_arch}"
    --cache-dir "${VENDOR_BUNDLE_DIR}"
  )
  if [[ "${VENDOR_OFFLINE}" == "1" ]]; then
    ensure_args+=(--offline)
  fi
  "${PY}" "${ensure_args[@]}"

  resolve_control_cli

  OUT_DIR="${OUT_DIR:-${ROOT}/dist}"
  mkdir -p "${OUT_DIR}"
  OUT_DIR="$(cd "${OUT_DIR}" && pwd)"

  echo "=== Building host install package ==="
  echo "target_platform=${TARGET_PLATFORM}"
  echo "app-release-dir=${APP_RELEASE_DIR}"
  echo "vendor-bundle-dir=${VENDOR_BUNDLE_DIR}"
  echo "baseline-matrix=${BASELINE_MATRIX}"
  echo "control_platform=${CONTROL_PLATFORM} ← $(basename "${CLI_PATH}")"

  # 注意：stage 不可 local——理由见 build_image 同名变量注释。
  local build
  stage="$(mktemp -d "${TMPDIR:-/tmp}/acps-host-install.XXXXXX")"
  trap 'rm -rf "${stage}"' EXIT
  build="${stage}/build"
  mkdir -p "${build}"

  # --- 选包（apps + control）+ vendor 解析（含 sha256 校验）+ 生成 release-manifest.toml ---
  # 产出：${build}/artifacts/{apps,control,vendor}/、${build}/release-manifest.toml、
  #       ${stage}/host_stage.env（HOST_VERSION / HOST_PYTHON_VERSION / HOST_UV_VERSION / 计数）
  "${PY}" - <<'PY' "${APP_RELEASE_DIR}" "${TARGET_PLATFORM}" "${VENDOR_BUNDLE_DIR}" "${BASELINE_MATRIX}" "${build}" "${CLI_PATH}" "${CONTROL_PLATFORM}" "${stage}/host_stage.env"
import fnmatch
import hashlib
import re
import shutil
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11
    import tomli as tomllib  # type: ignore

app_release_dir = Path(sys.argv[1])
target_platform = sys.argv[2]
vendor_bundle_dir = Path(sys.argv[3])
baseline_matrix_path = Path(sys.argv[4])
build_dir = Path(sys.argv[5])
cli_path = Path(sys.argv[6])
control_platform = sys.argv[7]
env_out = Path(sys.argv[8])

arch = target_platform.split("-", 1)[1]

apps_dir = build_dir / "artifacts" / "apps"
control_dir = build_dir / "artifacts" / "control"
vendor_dir = build_dir / "artifacts" / "vendor"
for d in (apps_dir, control_dir, vendor_dir):
    d.mkdir(parents=True, exist_ok=True)

# --- baseline-matrix 结构校验 ---
matrix = tomllib.loads(baseline_matrix_path.read_text(encoding="utf-8"))
for required in ("os_whitelist", "python", "vendor"):
    if required not in matrix:
        print(f"[ERROR] baseline-matrix {baseline_matrix_path} 缺少 [{required}]", file=sys.stderr)
        sys.exit(2)
python_meta = matrix["python"]
python_version = str(python_meta.get("version", "")).strip()
uv_version = str(python_meta.get("uv_version", "")).strip()
if not python_version or not uv_version:
    print(f"[ERROR] baseline-matrix [python] 缺少 version / uv_version 精确钉版本", file=sys.stderr)
    sys.exit(2)

# --- apps：按 target_platform 精确匹配业务 app-release（含 variant），排除 acps-cli ---
app_pat = re.compile(
    r"^(?P<app>.+)-" + re.escape(target_platform) + r"-(?P<pytag>cp\d+)(?:-(?P<variant>cpu|gpu))?-app-release-(?P<ver>.+)\.tar\.gz$"
)
apps: dict[str, dict[str, str]] = {}
versions: set[str] = set()
for f in sorted(app_release_dir.glob("*-app-release-*.tar.gz")):
    if f.name.startswith("acps-cli-"):
        continue
    m = app_pat.match(f.name)
    if not m:
        continue
    component = m.group("app").replace("-", "_")
    variant = m.group("variant")
    if variant:
        component = f"{component}_{variant}"
    if component in apps:
        print(f"[ERROR] 组件 {component} 匹配到多份 app-release：{apps[component]['file']} 与 {f.name}", file=sys.stderr)
        sys.exit(1)
    shutil.copy2(f, apps_dir / f.name)
    apps[component] = {"file": f.name, "version": m.group("ver")}
    versions.add(m.group("ver"))

if not apps:
    print(f"[ERROR] {app_release_dir} 下没有匹配 target_platform={target_platform} 的业务 app-release 包", file=sys.stderr)
    sys.exit(1)

m_cli = re.match(r"^acps-cli-.+-app-release-(?P<ver>.+)\.tar\.gz$", cli_path.name)
if not m_cli:
    print(f"[ERROR] 无法从 {cli_path.name} 解析版本号", file=sys.stderr)
    sys.exit(1)
versions.add(m_cli.group("ver"))

if len(versions) != 1:
    print(f"[ERROR] app-release 与控制 CLI 版本不一致：{sorted(versions)}", file=sys.stderr)
    sys.exit(1)
version = versions.pop()
shutil.copy2(cli_path, control_dir / cli_path.name)

# --- vendor：按 baseline-matrix [vendor.*] 精确名或 glob 单命中解析（占位 {arch}/{platform}） ---
vendor_matrix = matrix.get("vendor", {})
if not vendor_matrix:
    print(f"[ERROR] baseline-matrix {baseline_matrix_path} 未声明任何 [vendor.*]（产品必装）", file=sys.stderr)
    sys.exit(2)


def substitute(pattern: str) -> str:
    return pattern.replace("{arch}", arch).replace("{platform}", target_platform)


search_roots = []
if vendor_bundle_dir.is_dir():
    search_roots.append(vendor_bundle_dir)
    # 一层子目录可放各厂商分目录；跳过隐藏目录（如 .work 中间产物）以免 glob 多命中。
    search_roots.extend(
        sorted(p for p in vendor_bundle_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
    )

vendor_files: dict[str, str] = {}
vendor_files_glibc228: dict[str, str] = {}
missing: list[tuple[str, str]] = []
for key, meta in vendor_matrix.items():
    exact = meta.get("file")
    pattern = meta.get("glob")
    if not exact and not pattern:
        print(f"[ERROR] [vendor.{key}] 既未声明 file 也未声明 glob", file=sys.stderr)
        sys.exit(2)

    candidates: list[Path] = []
    if exact:
        name = substitute(exact)
        for root in search_roots:
            for f in root.glob("*"):
                if f.is_file() and f.name == name:
                    candidates.append(f)
    else:
        pat = substitute(pattern)
        for root in search_roots:
            for f in root.glob("*"):
                if f.is_file() and fnmatch.fnmatch(f.name, pat):
                    candidates.append(f)

    candidates = sorted(set(candidates))
    if not candidates:
        missing.append((key, substitute(exact or pattern)))
        continue
    if len(candidates) > 1:
        print(
            f"[ERROR] [vendor.{key}] glob 命中多个文件（须唯一）："
            + ", ".join(c.name for c in candidates),
            file=sys.stderr,
        )
        sys.exit(1)

    chosen = candidates[0]
    sha256 = str(meta.get(f"sha256_{arch}", "") or meta.get("sha256", "") or "").strip()
    if sha256:
        digest = hashlib.sha256(chosen.read_bytes()).hexdigest()
        if digest != sha256:
            print(
                f"[ERROR] [vendor.{key}] sha256 不匹配：期望 {sha256}，实得 {digest}（文件 {chosen.name}）",
                file=sys.stderr,
            )
            sys.exit(1)

    shutil.copy2(chosen, vendor_dir / chosen.name)
    vendor_files[key] = chosen.name

    # Optional secondary glibc228 artifact (amp_forwarder for rocky8/ubuntu20).
    glibc_pat = str(meta.get("file_glibc228", "") or "").strip()
    if glibc_pat:
        glibc_name = substitute(glibc_pat)
        glibc_hits: list[Path] = []
        for root in search_roots:
            for f in root.glob("*"):
                if f.is_file() and f.name == glibc_name:
                    glibc_hits.append(f)
        glibc_hits = sorted(set(glibc_hits))
        if not glibc_hits:
            missing.append((f"{key}/glibc228", glibc_name))
            continue
        if len(glibc_hits) > 1:
            print(
                f"[ERROR] [vendor.{key}] file_glibc228 命中多个文件："
                + ", ".join(c.name for c in glibc_hits),
                file=sys.stderr,
            )
            sys.exit(1)
        glibc_chosen = glibc_hits[0]
        glibc_sha = str(
            meta.get(f"sha256_{arch}_glibc228", "")
            or meta.get("sha256_glibc228", "")
            or ""
        ).strip()
        if glibc_sha:
            digest = hashlib.sha256(glibc_chosen.read_bytes()).hexdigest()
            if digest != glibc_sha:
                print(
                    f"[ERROR] [vendor.{key}] glibc228 sha256 不匹配：期望 {glibc_sha}，"
                    f"实得 {digest}（文件 {glibc_chosen.name}）",
                    file=sys.stderr,
                )
                sys.exit(1)
        shutil.copy2(glibc_chosen, vendor_dir / glibc_chosen.name)
        vendor_files_glibc228[key] = glibc_chosen.name

if missing:
    detail = "\n".join(f"  - [vendor.{k}] 未在 {vendor_bundle_dir} 下找到匹配：{pat}" for k, pat in missing)
    print(f"[ERROR] 缺少产品必装 vendor 制品（构建 FAIL）：\n{detail}", file=sys.stderr)
    sys.exit(1)

# --- release-manifest.toml（host 形态；含 artifact_kind） ---
infra = matrix.get("infra", {})
lines: list[str] = [
    "# 由 build-install-package.sh --mode host 构建期生成；勿手改，重跑构建将覆盖。",
    "[meta]",
    'deploy_mode = "host"',
    f'acps_version = "{version}"',
    f'target_platform = "{target_platform}"',
    f'platform = "{target_platform}"',
    f'control_platform = "{control_platform}"',
    "",
]
for component in sorted(apps):
    lines += [
        f"[apps.{component}]",
        'artifact_kind = "app_release"',
        f'app_release = "{apps[component]["file"]}"',
        "",
    ]
lines += [
    "[apps.control_acps_cli]",
    'artifact_kind = "app_release"',
    f'app_release = "{cli_path.name}"',
    "",
]
for key in sorted(vendor_files):
    lines += [
        f"[vendor.{key}]",
        'artifact_kind = "vendor_bundle"',
        f'file = "{vendor_files[key]}"',
    ]
    if key in vendor_files_glibc228:
        lines.append(f'file_glibc228 = "{vendor_files_glibc228[key]}"')
    vendor_version = vendor_matrix[key].get("version")
    if vendor_version:
        lines.append(f'version = "{vendor_version}"')
    lines.append("")
for key in sorted(infra):
    lines += [
        f"[os_packages.{key}]",
        'artifact_kind = "os_package"',
        "",
    ]

(build_dir / "release-manifest.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

env_out.write_text(
    "\n".join(
        [
            f'HOST_VERSION="{version}"',
            f'HOST_APPS_COUNT="{len(apps)}"',
            f'HOST_VENDOR_COUNT="{len(vendor_files)}"',
            f'HOST_PYTHON_VERSION="{python_version}"',
            f'HOST_UV_VERSION="{uv_version}"',
            "",
        ]
    ),
    encoding="utf-8",
)
print(f"apps matched: {len(apps)} ({', '.join(sorted(apps))})", file=sys.stderr)
print(f"vendor matched: {len(vendor_files)} ({', '.join(sorted(vendor_files))})", file=sys.stderr)
PY

  # shellcheck disable=SC1091
  source "${stage}/host_stage.env"

  local pkg_name dest
  pkg_name="acps-host-install-${HOST_VERSION}-${TARGET_PLATFORM}"
  dest="${stage}/${pkg_name}"
  mkdir -p "${dest}"
  mv "${build}/artifacts" "${dest}/artifacts"
  mv "${build}/release-manifest.toml" "${dest}/release-manifest.toml"

  if [[ -n "${BUNDLE_PYTHON_DIR}" ]]; then
    mkdir -p "${dest}/tools"
    rsync -a "${BUNDLE_PYTHON_DIR}/" "${dest}/tools/"
  fi

  # --- tree: ansible / templates / scripts / inventories examples（不产出 bin/acps-install） ---
  assemble_shared_tree "${dest}"
  cp -a "${BASELINE_MATRIX}" "${dest}/baseline-matrix.toml"

  "${PY}" - <<PY "${dest}/ansible/inventories/group_vars/all.yml" "${TARGET_PLATFORM}" "${CONTROL_PLATFORM}" "${HOST_PYTHON_VERSION}" "${HOST_UV_VERSION}"
from pathlib import Path
import sys
path = Path(sys.argv[1])
target_platform, control_platform, python_version, uv_version = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
lines = []
for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
    if line.startswith("acps_deploy_mode:"):
        lines.append("acps_deploy_mode: host # V1 集群级；禁止按主机覆盖\n")
    elif line.startswith("acps_platform:"):
        lines.append(f"acps_platform: {target_platform}\n")
    elif line.startswith("acps_control_platform:"):
        lines.append(f'acps_control_platform: "{control_platform}"\n')
    elif line.startswith("acps_python_version:"):
        lines.append(f'acps_python_version: "{python_version}"\n')
    elif line.startswith("acps_uv_version:"):
        lines.append(f'acps_uv_version: "{uv_version}"\n')
    else:
        lines.append(line)
path.write_text("".join(lines), encoding="utf-8")
PY
  cp -a "${dest}/ansible/inventories/group_vars/all.yml" "${dest}/inventories/group_vars/all.yml"

  finalize_package "${dest}" "${stage}" "${pkg_name}" "${OUT_DIR}"
  echo "apps: ${HOST_APPS_COUNT}"
  echo "control: $(basename "${CLI_PATH}")"
  echo "vendor: ${HOST_VENDOR_COUNT}"
  echo "python: ${HOST_PYTHON_VERSION} (uv ${HOST_UV_VERSION})"
  echo "platforms: target=${TARGET_PLATFORM} control=${CONTROL_PLATFORM}"
}

case "${MODE}" in
  image) build_image ;;
  host) build_host ;;
esac
