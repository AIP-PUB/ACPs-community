#!/usr/bin/env bash
# 本机装配：在 Darwin（或其它非 manylinux）构建机上，将应用薄包 + internal_wheels +
# 第三方依赖装配成自包含的应用最终发布包。
#
# 与 assemble.sh（容器内 manylinux）对应；由 assemble-and-validate.sh 在
# --platform darwin/* 时调用。当前正式用途：acps-cli 的 darwin/<host_arch>。
#
# 假设当前工作目录为已解开的 assembly kit 根（含 packages/）。

set -euo pipefail
shopt -s nullglob

PACKAGES_DIR="${PWD}/packages"

APP=""
PYTHON_TAG=""
VARIANT=""
OUTPUT_DIR=""
PLATFORM=""

usage() {
    cat <<'EOF'
用法：assemble-host.sh --app <id> --python-tag <tag> --variant <variant|""> \
         --output <dir> --platform <os/arch>

在构建机本机运行（不经 manylinux Buildx）。优先 --require-hashes + --only-binary=:all:；
sdist-only 依赖退回允许现场编译。策略写入 build-manifest.toml 的
dependency_resolution_strategy。
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app) shift; APP="${1:-}" ;;
        --python-tag) shift; PYTHON_TAG="${1:-}" ;;
        --variant) shift; VARIANT="${1:-}" ;;
        --output) shift; OUTPUT_DIR="${1:-}" ;;
        --platform) shift; PLATFORM="${1:-}" ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "[ERROR] 未知参数：$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ -z "${APP}" || -z "${PYTHON_TAG}" || -z "${OUTPUT_DIR}" || -z "${PLATFORM}" ]]; then
    echo "[ERROR] --app、--python-tag、--output、--platform 均为必填参数" >&2
    usage >&2
    exit 2
fi

case "${PLATFORM}" in
    darwin/*) ;;
    *)
        echo "[ERROR] assemble-host.sh 仅支持 darwin/* platform，收到：${PLATFORM}" >&2
        exit 2
        ;;
esac

host_os="$(uname -s | tr '[:upper:]' '[:lower:]')"
if [[ "${host_os}" != "darwin" ]]; then
    echo "[ERROR] darwin 目标包必须在 Darwin 构建机上本机装配（当前 OS=${host_os}）" >&2
    exit 2
fi

host_arch="$(uname -m)"
case "${host_arch}" in
    arm64|aarch64) host_arch_slug="arm64" ;;
    x86_64|amd64) host_arch_slug="amd64" ;;
    *)
        echo "[ERROR] 不支持的本机架构：${host_arch}" >&2
        exit 2
        ;;
esac
want_arch="${PLATFORM##*/}"
if [[ "${want_arch}" != "${host_arch_slug}" ]]; then
    echo "[ERROR] 不做跨 arch 交叉：platform=${PLATFORM} 但本机 arch=${host_arch_slug}" >&2
    exit 2
fi

# cp314 → 3.14
py_major_minor=""
if [[ "${PYTHON_TAG}" =~ ^cp([0-9])([0-9]+)$ ]]; then
    py_major_minor="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
else
    echo "[ERROR] 无法从 python_tag=${PYTHON_TAG} 推导本机解释器版本（期望如 cp314）" >&2
    exit 2
fi

resolve_host_python() {
    local cand
    for cand in \
        "${ACPS_HOST_ASSEMBLE_PYTHON:-}" \
        "python${py_major_minor}" \
        "python3"
    do
        [[ -n "${cand}" ]] || continue
        if command -v "${cand}" >/dev/null 2>&1; then
            local bin
            bin="$(command -v "${cand}")"
            if "${bin}" -c "import sys; raise SystemExit(0 if sys.version_info[:2]==tuple(map(int,'${py_major_minor}'.split('.'))) else 1)"; then
                if "${bin}" -m pip --version >/dev/null 2>&1; then
                    printf '%s\n' "${bin}"
                    return 0
                fi
            fi
        fi
    done
    return 1
}

PYTHON_BIN="$(resolve_host_python || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[ERROR] 未找到带 pip 的 Python ${py_major_minor}（python_tag=${PYTHON_TAG}）" >&2
    echo "        可设置 ACPS_HOST_ASSEMBLE_PYTHON=/path/to/python${py_major_minor}" >&2
    exit 1
fi
PIP_BIN=("${PYTHON_BIN}" -m pip)
echo "=== 本机解释器：${PYTHON_BIN} ==="

APP_DIR="${PACKAGES_DIR}/${APP}"
if [[ ! -d "${APP_DIR}" ]]; then
    echo "[ERROR] 装配包中不存在 app：${APP}（${APP_DIR}）" >&2
    exit 1
fi

RUNTIME_TOML="${APP_DIR}/runtime-package.toml"
if [[ ! -f "${RUNTIME_TOML}" ]]; then
    echo "[ERROR] ${APP} 缺少 runtime-package.toml：${RUNTIME_TOML}" >&2
    exit 1
fi

echo "=== 解析 runtime-package.toml，选择 lockfile ==="
arch_from_platform="${PLATFORM##*/}"
parsed="$(RUNTIME_TOML_PATH="${RUNTIME_TOML}" VARIANT_ARG="${VARIANT}" ARCH_ARG="${arch_from_platform}" python3 - <<'PY'
import os
import sys
import tomllib
from pathlib import Path

path = Path(os.environ["RUNTIME_TOML_PATH"])
variant = os.environ.get("VARIANT_ARG", "")
arch = os.environ.get("ARCH_ARG", "")
data = tomllib.loads(path.read_text(encoding="utf-8"))

artifacts = data.get("artifacts", {})
variant_lockfiles = artifacts.get("variant_lockfiles")
lockfile = artifacts.get("lockfile")

KNOWN_ARCHES = ("arm64", "amd64")


def base_variant_name(key: str) -> str:
    for arch_name in KNOWN_ARCHES:
        suffix = f"-{arch_name}"
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


if variant_lockfiles:
    base_variants = sorted({base_variant_name(k) for k in variant_lockfiles})
    if not variant:
        sys.exit(
            "该 app 声明了 variant_lockfiles，必须显式传 --variant；合法取值："
            + ",".join(base_variants)
        )
    if variant not in base_variants:
        sys.exit("非法 --variant=" + variant + "；合法取值：" + ",".join(base_variants))

    composite_key = f"{variant}-{arch}"
    if composite_key in variant_lockfiles:
        chosen_lockfile = variant_lockfiles[composite_key]
    elif variant in variant_lockfiles:
        chosen_lockfile = variant_lockfiles[variant]
    else:
        sys.exit(
            f"未找到 variant={variant} 在架构 {arch} 下对应的锁定清单"
            f"（尝试过 key={composite_key} 和 key={variant}；已声明的 key："
            + ",".join(sorted(variant_lockfiles))
            + "）"
        )
else:
    if variant:
        sys.exit("该 app 未声明 variant_lockfiles，不应传 --variant")
    if not lockfile:
        sys.exit("runtime-package.toml 既未声明 lockfile 也未声明 variant_lockfiles")
    chosen_lockfile = lockfile

package = data.get("package", {})
version = package.get("version", "")
internal_wheels = data.get("dependencies", {}).get("internal_wheels", [])

print(f"LOCKFILE={chosen_lockfile}")
print(f"VERSION={version}")
print(f"INTERNAL_WHEELS={','.join(internal_wheels)}")
PY
)"

LOCKFILE=""
VERSION=""
INTERNAL_WHEELS=""
while IFS='=' read -r key value; do
    case "${key}" in
        LOCKFILE) LOCKFILE="${value}" ;;
        VERSION) VERSION="${value}" ;;
        INTERNAL_WHEELS) INTERNAL_WHEELS="${value}" ;;
    esac
done <<< "${parsed}"

echo "  lockfile=${LOCKFILE} version=${VERSION} internal_wheels=${INTERNAL_WHEELS}"

echo "=== 校验 app wheel 输入（必须恰好一个） ==="
app_wheel=""
app_wheel_count=0
for f in "${APP_DIR}"/dist/*.whl; do
    app_wheel_count=$((app_wheel_count + 1))
    app_wheel="${f}"
done
if [[ "${app_wheel_count}" -ne 1 ]]; then
    echo "[ERROR] ${APP} 的 dist/ 下 wheel 数量异常：${app_wheel_count}（应恰好 1 个）" >&2
    exit 1
fi
echo "  app_wheel=$(basename "${app_wheel}")"

echo "=== 校验 internal_wheels 输入（每项必须恰好一个） ==="
declare -a internal_wheel_paths=()
if [[ -n "${INTERNAL_WHEELS}" ]]; then
    IFS=',' read -r -a internal_wheel_ids <<< "${INTERNAL_WHEELS}"
    for wid in "${internal_wheel_ids[@]}"; do
        [[ -n "${wid}" ]] || continue
        wid_dir="${PACKAGES_DIR}/${wid}"
        if [[ ! -d "${wid_dir}" ]]; then
            echo "[ERROR] internal_wheels 声明的项目在装配包中不存在：${wid}（${wid_dir}）" >&2
            exit 1
        fi
        wcount=0
        wpath=""
        for f in "${wid_dir}"/dist/*.whl; do
            wcount=$((wcount + 1))
            wpath="${f}"
        done
        if [[ "${wcount}" -ne 1 ]]; then
            echo "[ERROR] internal wheel ${wid} 的 dist/ 下 wheel 数量异常：${wcount}（应恰好 1 个）" >&2
            exit 1
        fi
        echo "  internal_wheel[${wid}]=$(basename "${wpath}")"
        internal_wheel_paths+=("${wpath}")
    done
fi

lockfile_path="${APP_DIR}/${LOCKFILE}"
if [[ ! -f "${lockfile_path}" ]]; then
    echo "[ERROR] 未找到锁定清单：${lockfile_path}" >&2
    exit 1
fi

sha256_cmd=()
if command -v sha256sum >/dev/null 2>&1; then
    sha256_cmd=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
    sha256_cmd=(shasum -a 256)
else
    echo "[ERROR] 未找到 sha256sum 或 shasum 命令" >&2
    exit 1
fi

work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT
wheelhouse_tmp="${work_dir}/wheelhouse-download"
mkdir -p "${wheelhouse_tmp}"

echo "=== 使用本机 pip 下载第三方依赖 ==="
dependency_resolution_strategy="only-binary"
if "${PIP_BIN[@]}" wheel \
    --no-cache-dir \
    --disable-pip-version-check \
    --only-binary=:all: \
    --require-hashes \
    -r "${lockfile_path}" \
    --wheel-dir "${wheelhouse_tmp}"; then
    echo "  全部依赖均有目标平台预编译 wheel（only-binary 策略成功）"
else
    echo "[WARN] --only-binary=:all: 模式失败，可能存在 sdist-only 依赖；" >&2
    echo "        改为允许 sdist 现场编译后重试（仍保留 --require-hashes）" >&2
    rm -rf "${wheelhouse_tmp:?}"
    mkdir -p "${wheelhouse_tmp}"
    dependency_resolution_strategy="only-binary-with-sdist-fallback"
    "${PIP_BIN[@]}" wheel \
        --no-cache-dir \
        --disable-pip-version-check \
        --require-hashes \
        -r "${lockfile_path}" \
        --wheel-dir "${wheelhouse_tmp}"
fi

platform_slug="${PLATFORM//\//-}"
variant_suffix=""
if [[ -n "${VARIANT}" ]]; then
    variant_suffix="-${VARIANT}"
fi
final_name="${APP}-${platform_slug}-${PYTHON_TAG}${variant_suffix}-app-release-${VERSION}"
final_root="${work_dir}/final/${final_name}"
mkdir -p "${final_root}/wheelhouse" "${final_root}/app"

echo "=== 组装最终包：${final_name} ==="
cp -R "${APP_DIR}/." "${final_root}/app/"

cp "${app_wheel}" "${final_root}/wheelhouse/"
for p in "${internal_wheel_paths[@]}"; do
    cp "${p}" "${final_root}/wheelhouse/"
done
for p in "${wheelhouse_tmp}"/*.whl; do
    cp "${p}" "${final_root}/wheelhouse/"
done

build_manifest="${final_root}/build-manifest.toml"
lockfile_sha256="$("${sha256_cmd[@]}" "${lockfile_path}" | awk '{print $1}')"
{
    echo "app = \"${APP}\""
    echo "version = \"${VERSION}\""
    echo "platform = \"${PLATFORM}\""
    echo "python_tag = \"${PYTHON_TAG}\""
    if [[ -n "${VARIANT}" ]]; then
        echo "variant = \"${VARIANT}\""
        echo "runtime_mode = \"${VARIANT}\""
    fi
    echo "lockfile = \"${LOCKFILE}\""
    echo "lockfile_sha256 = \"${lockfile_sha256}\""
    echo "app_wheel = \"$(basename "${app_wheel}")\""
    if [[ "${#internal_wheel_paths[@]}" -gt 0 ]]; then
        printf 'internal_wheels = ['
        first=1
        for p in "${internal_wheel_paths[@]}"; do
            [[ "${first}" -eq 1 ]] || printf ', '
            printf '"%s"' "$(basename "${p}")"
            first=0
        done
        printf ']\n'
    else
        echo "internal_wheels = []"
    fi
    echo "dependency_resolution_strategy = \"${dependency_resolution_strategy}\""
    echo "assembler = \"host-native\""
    echo "generated_at = \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
} > "${build_manifest}"

echo "=== 生成最终包 checksums.txt ==="
(
    cd "${final_root}"
    find . -type f ! -path './checksums.txt' -print0 \
        | LC_ALL=C sort -z \
        | xargs -0 "${sha256_cmd[@]}" > checksums.txt
)

mkdir -p "${OUTPUT_DIR}"
# 关闭 macOS AppleDouble，避免 tar 写入 ._* 破坏单一顶层目录约定。
COPYFILE_DISABLE=1 tar -czf "${OUTPUT_DIR}/${final_name}.tar.gz" -C "${work_dir}/final" "${final_name}"

echo "=== 装配完成 ==="
echo "  最终包：${OUTPUT_DIR}/${final_name}.tar.gz"
