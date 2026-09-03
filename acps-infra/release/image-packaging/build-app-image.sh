#!/usr/bin/env bash
# 单个应用镜像目标 build/export/load/smoke/gzip 一体化脚本
# 。
#
# 对应 步骤 2-6：
# 2. 校验应用最终发布包（lib/app_release.py，）。
# 3. 生成 build context（lib/build_context.py，；Dockerfile/entrypoint.sh 来自 app/）。
# 4. docker buildx build --output type=docker,dest=<tmp>.image.tar（不隐式加载）。
# 5. 显式 docker load，再执行 `pip check` + 关键包最小 import（两者都是必检项）。
# 6. smoke 通过后才 gzip 成最终 `.image.tar.gz`；任何一步失败都不产出/不保留
# 被误认为正式产物的文件。
#
# 用法：
# build-app-image.sh --app <id> --platform <linux/amd64|linux/arm64> \
# --python-tag <tag> [--variant <variant>] \
# --package <final-release.tar.gz> --output <dir> \
# [--lock <image-inputs.lock>...] [--strategy <path>] \
# [--keep-build-context]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/lib"
APP_DOCKERFILE_DIR="${SCRIPT_DIR}/app"
DEFAULT_LOCK="${SCRIPT_DIR}/../image-inputs.lock"
DEFAULT_STRATEGY="${SCRIPT_DIR}/startup-strategies.toml"

APP=""
PLATFORM=""
PYTHON_TAG=""
VARIANT=""
PACKAGE=""
OUTPUT_DIR=""
STRATEGY="${DEFAULT_STRATEGY}"
declare -a LOCK_PATHS=()
KEEP_BUILD_CONTEXT=0
RESULT_FILE=""

usage() {
    cat <<'EOF'
用法：build-app-image.sh --app <id> --platform <linux/amd64|linux/arm64> \
    --python-tag <tag> [--variant <variant>] \
    --package <final-release.tar.gz> --output <dir> \
    [--lock <image-inputs.lock> ...] [--strategy <path>] [--keep-build-context] \
    [--result-file <path>]
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app) shift; APP="${1:-}" ;;
        --platform) shift; PLATFORM="${1:-}" ;;
        --python-tag) shift; PYTHON_TAG="${1:-}" ;;
        --variant) shift; VARIANT="${1:-}" ;;
        --package) shift; PACKAGE="${1:-}" ;;
        --output) shift; OUTPUT_DIR="${1:-}" ;;
        --strategy) shift; STRATEGY="${1:-}" ;;
        --lock) shift; LOCK_PATHS+=("${1:-}") ;;
        --keep-build-context) KEEP_BUILD_CONTEXT=1 ;;
        --result-file) shift; RESULT_FILE="${1:-}" ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "[ERROR] 未知参数：$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ -z "${APP}" || -z "${PLATFORM}" || -z "${PYTHON_TAG}" || -z "${PACKAGE}" || -z "${OUTPUT_DIR}" ]]; then
    echo "[ERROR] --app、--platform、--python-tag、--package、--output 均为必填参数" >&2
    usage >&2
    exit 2
fi
if [[ "${#LOCK_PATHS[@]}" -eq 0 ]]; then
    LOCK_PATHS=("${DEFAULT_LOCK}")
fi

mkdir -p "${OUTPUT_DIR}"
OUTPUT_DIR="$(cd "${OUTPUT_DIR}" && pwd)"

# build context 使用 mktemp 生成的临时目录，不使用调用方可控的路径做 rm -rf，
# 避免重蹈"--output 会被清空导致危险路径"的问题（对照 assembly/assemble-and-validate.sh）。
BUILD_CONTEXT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/acps-image-build.XXXXXX")"
VARS_FILE="$(mktemp "${TMPDIR:-/tmp}/acps-image-vars.XXXXXX.sh")"
TMP_TAR=""
GZIP_TMP=""

cleanup() {
    local ret=$?
    if [[ "${KEEP_BUILD_CONTEXT}" -eq 0 ]]; then
        rm -rf "${BUILD_CONTEXT_DIR}"
    else
        echo "[INFO] 保留 build context 供排查：${BUILD_CONTEXT_DIR}" >&2
    fi
    rm -f "${VARS_FILE}"
    if [[ -n "${TMP_TAR}" && -f "${TMP_TAR}" ]]; then
        rm -f "${TMP_TAR}"
    fi
    # 若在 gzip 写入过程中失败（磁盘满/权限错误等），不能在 --output 目录下留下
    # 一个会被误认为正式产物的损坏/不完整.image.tar.gz（见下方 gzip 步骤的原子写入说明）。
    if [[ -n "${GZIP_TMP}" && -f "${GZIP_TMP}" ]]; then
        rm -f "${GZIP_TMP}"
    fi
    return "${ret}"
}
trap cleanup EXIT

echo "=== 准备 build context：app=${APP} platform=${PLATFORM} python_tag=${PYTHON_TAG} variant=${VARIANT:-<none>} ==="
declare -a lock_args=()
for lock_path in "${LOCK_PATHS[@]}"; do
    lock_args+=(--lock "${lock_path}")
done
python3 "${LIB_DIR}/build_app_image.py" prepare \
    --app "${APP}" --platform "${PLATFORM}" --python-tag "${PYTHON_TAG}" --variant "${VARIANT}" \
    --package "${PACKAGE}" --strategy "${STRATEGY}" \
    "${lock_args[@]}" \
    --build-context "${BUILD_CONTEXT_DIR}" \
    --app-dockerfile-dir "${APP_DOCKERFILE_DIR}" \
    > "${VARS_FILE}"
# shellcheck source=/dev/null
source "${VARS_FILE}"
echo "  APP_VERSION=${APP_VERSION} APP_WHEEL=${APP_WHEEL} APP_WHEEL_EXTRA=${APP_WHEEL_EXTRA:-<none>}"
echo "  PYTHON_RUNTIME_IMAGE=${PYTHON_RUNTIME_IMAGE}"
echo "  IMAGE_TAG=${IMAGE_TAG}"
echo "  IMAGE_FILENAME=${IMAGE_FILENAME}"
echo "  COMPONENT_IDS=${COMPONENT_IDS}"

TMP_TAR="$(mktemp "${TMPDIR:-/tmp}/acps-image-export.XXXXXX.tar")"

echo "=== 构建并导出 Docker archive（不隐式加载） ==="
docker buildx build \
    --platform "${PLATFORM}" \
    --build-arg "PYTHON_RUNTIME_IMAGE=${PYTHON_RUNTIME_IMAGE}" \
    --build-arg "APP_ID=${APP}" \
    --build-arg "APP_VERSION=${APP_VERSION}" \
    --build-arg "APP_WHEEL=${APP_WHEEL}" \
    --build-arg "APP_WHEEL_EXTRA=${APP_WHEEL_EXTRA}" \
    --build-arg "APP_RELEASE_FILE=$(basename "${PACKAGE}")" \
    --build-arg "APP_RELEASE_SHA256=${APP_RELEASE_SHA256}" \
    --build-arg "APP_PLATFORM=${PLATFORM}" \
    --build-arg "PYTHON_TAG=${PYTHON_TAG}" \
    --build-arg "VARIANT=${VARIANT}" \
    --build-arg "IMAGE_CREATED=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --build-arg "SOURCE_REVISION=${SOURCE_REVISION:-}" \
    -t "${IMAGE_TAG}" \
    -f "${BUILD_CONTEXT_DIR}/Dockerfile" \
    --output "type=docker,dest=${TMP_TAR}" \
    "${BUILD_CONTEXT_DIR}"

echo "=== 显式 docker load（不依赖 buildx 隐式加载） ==="
docker load -i "${TMP_TAR}"

echo "=== smoke：pip check ==="
docker run --rm --platform "${PLATFORM}" --entrypoint python "${IMAGE_TAG}" -m pip check

echo "=== smoke：关键包最小 import（IMPORT_CHECKS=${IMPORT_CHECKS} ASSERT_PRESENT=${ASSERT_PRESENT:-<none>} ASSERT_ABSENT=${ASSERT_ABSENT:-<none>}） ==="
declare -a snippet_args=()
for module in ${IMPORT_CHECKS}; do
    snippet_args+=(--import-check "${module}")
done
for module in ${ASSERT_PRESENT}; do
    snippet_args+=(--assert-present "${module}")
done
for module in ${ASSERT_ABSENT}; do
    snippet_args+=(--assert-absent "${module}")
done
smoke_snippet="$(python3 "${LIB_DIR}/common.py" smoke-snippet "${snippet_args[@]}")"
docker run --rm --platform "${PLATFORM}" --entrypoint python "${IMAGE_TAG}" -c "${smoke_snippet}"

echo "=== smoke 通过，压缩为最终镜像包 ==="
final_path="${OUTPUT_DIR}/${IMAGE_FILENAME}"
# 先写入与最终文件同目录的临时文件，成功后再 atomic rename 到 final_path：
# `gzip -c... > final_path` 会在 gzip 还没跑完时就先创建/截断目标文件，如果 gzip
# 中途失败，--output 目录下就会留下一个空/损坏的.image.tar.gz。mv 同目录内重命名
# 是原子操作，不会出现部分写入的中间状态。
GZIP_TMP="${final_path}.tmp.$$"
gzip -c "${TMP_TAR}" > "${GZIP_TMP}"
mv "${GZIP_TMP}" "${final_path}"
GZIP_TMP=""
echo "  ${final_path}"
if [[ -n "${RESULT_FILE}" ]]; then
    printf '%s\n' "${final_path}" > "${RESULT_FILE}"
fi
