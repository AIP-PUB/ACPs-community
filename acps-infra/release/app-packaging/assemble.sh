#!/usr/bin/env bash
# 装配脚本：在目标平台兼容的 manylinux builder 容器内，将一个应用薄包 + 声明的
# internal_wheels + 第三方依赖装配成自包含的应用最终发布包。
#
# 
# 实现结果：最终包装配脚本已在当前发布包实现中落地。
#
# 只在装配容器内运行（由 assembly/Dockerfile 的 RUN 调用），假设当前目录结构为：
# kit/packages/{id}/... —— 采集阶段产出的应用薄包 / shared-library wheel
# kit/assemble.sh —— 本脚本自身

set -euo pipefail
shopt -s nullglob

KIT_DIR="/kit"
PACKAGES_DIR="${KIT_DIR}/packages"

APP=""
PYTHON_TAG=""
VARIANT=""
OUTPUT_DIR=""
PLATFORM="${TARGETPLATFORM:-}"

usage() {
    cat <<'EOF'
用法：assemble.sh --app <id> --python-tag <tag> --variant <variant|""> --output <dir> [--platform <os/arch>]

只在装配容器内运行。优先以 --require-hashes + --only-binary=:all: 严格模式下载第三方依赖；
如果存在完全没有目标平台预编译 wheel 的依赖（sdist-only），退回允许 sdist 现场编译
（builder 镜像自带完整编译工具链），实际采用的策略记录在 build-manifest.toml 的
dependency_resolution_strategy 字段（"only-binary" 或 "only-binary-with-sdist-fallback"）。
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app)
            shift
            APP="${1:-}"
            ;;
        --python-tag)
            shift
            PYTHON_TAG="${1:-}"
            ;;
        --variant)
            shift
            VARIANT="${1:-}"
            ;;
        --output)
            shift
            OUTPUT_DIR="${1:-}"
            ;;
        --platform)
            shift
            PLATFORM="${1:-}"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[ERROR] 未知参数：$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ -z "${APP}" || -z "${PYTHON_TAG}" || -z "${OUTPUT_DIR}" ]]; then
    echo "[ERROR] --app、--python-tag、--output 均为必填参数" >&2
    usage >&2
    exit 2
fi

if [[ -z "${PLATFORM}" ]]; then
    echo "[ERROR] 未能确定目标 platform（既未传 --platform，也未提供 TARGETPLATFORM）" >&2
    exit 2
fi

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

# variant_lockfiles 的 key 可以是纯 "cpu"/"cuda"，也可以是 "cpu-arm64"/"cpu-amd64"
# 这种带架构后缀的复合形式——后者用于像 discovery-server 这类需要按架构分别生成
# 锁定清单摘要的场景（同一 variant 在不同架构下实际下载的 wheel 及其 hash 不同）。
# 用户侧（--variant）只需要传基础名（cpu/cuda），不需要感知架构后缀。
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

PYTHON_BIN="/opt/python/${PYTHON_TAG}-${PYTHON_TAG}/bin/python"
PIP_BIN="/opt/python/${PYTHON_TAG}-${PYTHON_TAG}/bin/pip"
if [[ ! -x "${PYTHON_BIN}" || ! -x "${PIP_BIN}" ]]; then
    echo "[ERROR] builder 镜像中不存在 python_tag=${PYTHON_TAG} 对应的解释器：${PYTHON_BIN}" >&2
    echo "        builder 镜像内可用的 CPython 版本：" >&2
    ls -d /opt/python/*/ >&2 2>/dev/null || true
    exit 1
fi

lockfile_path="${APP_DIR}/${LOCKFILE}"
if [[ ! -f "${lockfile_path}" ]]; then
    echo "[ERROR] 未找到锁定清单：${lockfile_path}" >&2
    exit 1
fi

work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT
wheelhouse_tmp="${work_dir}/wheelhouse-download"
mkdir -p "${wheelhouse_tmp}"

echo "=== 使用 ${PIP_BIN} 下载第三方依赖 ==="
echo "    策略：优先 --require-hashes（hash-checking 严格模式） + --only-binary=:all:；"
echo "    如果存在完全没有目标平台预编译 wheel 的依赖（sdist-only 包，例如 cbor），"
echo "    再退回到允许 sdist 现场编译（builder 镜像自带完整编译工具链）。"
echo "    两个分支都用 'pip wheel'（而不是 'pip download'）——'pip download' 对 sdist-only"
echo "    依赖只会保存原始 .tar.gz/.zip，不会产出 .whl，导致后续只拷贝 *.whl 时静默丢件；"
echo "    'pip wheel' 无论源材料是 wheel 还是 sdist，落地到 --wheel-dir 的都是构建好的 .whl。"
echo "    最终采用的策略记录在 build-manifest.toml 的 dependency_resolution_strategy 字段。"
dependency_resolution_strategy="only-binary"
if "${PIP_BIN}" wheel \
    --no-cache-dir \
    --disable-pip-version-check \
    --only-binary=:all: \
    --require-hashes \
    -r "${lockfile_path}" \
    --wheel-dir "${wheelhouse_tmp}"; then
    echo "  全部依赖均有目标平台预编译 wheel（only-binary 策略成功）"
else
    echo "[WARN] --only-binary=:all: 模式失败，可能存在 sdist-only 依赖；" >&2
    echo "        改为允许 sdist 现场编译后重试（仍保留 --require-hashes 对下载材料做 hash 校验）" >&2
    rm -rf "${wheelhouse_tmp:?}"
    mkdir -p "${wheelhouse_tmp}"
    dependency_resolution_strategy="only-binary-with-sdist-fallback"
    "${PIP_BIN}" wheel \
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
lockfile_sha256="$(sha256sum "${lockfile_path}" | awk '{print $1}')"
{
    echo "app = \"${APP}\""
    echo "version = \"${VERSION}\""
    echo "platform = \"${PLATFORM}\""
    echo "python_tag = \"${PYTHON_TAG}\""
    if [[ -n "${VARIANT}" ]]; then
        echo "variant = \"${VARIANT}\""
        # runtime_mode 目前直接复用 variant 取值：本设计里 variant 命名本身就与业务
        # 运行模式（例如 discovery-server 的 DISCOVERY_MODE=cpu/gpu）保持一致，记录为
        # 独立字段是为了让下游 image-mode/host-mode 消费方能显式校验"依赖 profile"和
        # "业务运行模式"两者一致，而不需要回头解释 variant 命名本身的业务含义。
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
    echo "generated_at = \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
} > "${build_manifest}"

echo "=== 生成最终包 checksums.txt ==="
# 注意：只排除顶层的./checksums.txt 自身；app/checksums.txt 是应用薄包自带的另一份
# 校验文件（覆盖 app/ 内容），根目录 checksums.txt 必须把它当作普通文件一并记录，
# 不能因为 basename 相同就被 -name 过滤掉。
(
    cd "${final_root}"
    find . -type f ! -path './checksums.txt' -print0 \
        | LC_ALL=C sort -z \
        | xargs -0 sha256sum > checksums.txt
)

mkdir -p "${OUTPUT_DIR}"
tar -czf "${OUTPUT_DIR}/${final_name}.tar.gz" -C "${work_dir}/final" "${final_name}"

echo "=== 装配完成 ==="
echo "  最终包：${OUTPUT_DIR}/${final_name}.tar.gz"
