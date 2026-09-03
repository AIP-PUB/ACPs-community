#!/usr/bin/env bash
# 单个基础设施镜像目标 build/export/load/smoke/gzip 一体化脚本
# 。
#
# 与 build-app-image.sh 的关键差异：
# - 不消费应用最终发布包，构建上下文来自 infra/<id>/ 或 infra/wrapper/。
# - smoke 不执行 Python pip check/import，而是执行 infra/smoke-commands.toml
# 里为该 infra id 声明的服务特定命令。
#
# 用法：
# build-infra-image.sh --id <infra-id> --kind <wrapper|derived> \
# --upstream-version <v> --acps-version <v> --platform <linux/amd64|linux/arm64> \
# --output <dir> [--lock <image-inputs.lock>...] [--result-file <path>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/lib"
INFRA_DIR="${SCRIPT_DIR}/infra"
DEFAULT_LOCK="${SCRIPT_DIR}/../image-inputs.lock"
SMOKE_COMMANDS="${INFRA_DIR}/smoke-commands.toml"

INFRA_ID=""
INFRA_KIND=""
UPSTREAM_VERSION=""
ACPS_VERSION=""
PLATFORM=""
OUTPUT_DIR=""
declare -a LOCK_PATHS=()
RESULT_FILE=""

usage() {
    cat <<'EOF'
用法：build-infra-image.sh --id <infra-id> --kind <wrapper|derived> \
    --upstream-version <v> --acps-version <v> --platform <linux/amd64|linux/arm64> \
    --output <dir> [--lock <image-inputs.lock> ...] [--result-file <path>]
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --id) shift; INFRA_ID="${1:-}" ;;
        --kind) shift; INFRA_KIND="${1:-}" ;;
        --upstream-version) shift; UPSTREAM_VERSION="${1:-}" ;;
        --acps-version) shift; ACPS_VERSION="${1:-}" ;;
        --platform) shift; PLATFORM="${1:-}" ;;
        --output) shift; OUTPUT_DIR="${1:-}" ;;
        --lock) shift; LOCK_PATHS+=("${1:-}") ;;
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

if [[ -z "${INFRA_ID}" || -z "${INFRA_KIND}" || -z "${UPSTREAM_VERSION}" || -z "${ACPS_VERSION}" || -z "${PLATFORM}" || -z "${OUTPUT_DIR}" ]]; then
    echo "[ERROR] --id、--kind、--upstream-version、--acps-version、--platform、--output 均为必填参数" >&2
    usage >&2
    exit 2
fi
if [[ "${#LOCK_PATHS[@]}" -eq 0 ]]; then
    LOCK_PATHS=("${DEFAULT_LOCK}")
fi

mkdir -p "${OUTPUT_DIR}"
OUTPUT_DIR="$(cd "${OUTPUT_DIR}" && pwd)"

VARS_FILE="$(mktemp "${TMPDIR:-/tmp}/acps-infra-image-vars.XXXXXX.sh")"
TMP_TAR=""
GZIP_TMP=""

cleanup() {
    local ret=$?
    rm -f "${VARS_FILE}"
    if [[ -n "${TMP_TAR}" && -f "${TMP_TAR}" ]]; then
        rm -f "${TMP_TAR}"
    fi
    if [[ -n "${GZIP_TMP}" && -f "${GZIP_TMP}" ]]; then
        rm -f "${GZIP_TMP}"
    fi
    return "${ret}"
}
trap cleanup EXIT

echo "=== 准备构建参数：id=${INFRA_ID} kind=${INFRA_KIND} upstream_version=${UPSTREAM_VERSION} acps_version=${ACPS_VERSION} platform=${PLATFORM} ==="
declare -a lock_args=()
for lock_path in "${LOCK_PATHS[@]}"; do
    lock_args+=(--lock "${lock_path}")
done
python3 "${LIB_DIR}/build_infra_image.py" prepare \
    --id "${INFRA_ID}" --kind "${INFRA_KIND}" \
    --upstream-version "${UPSTREAM_VERSION}" --acps-version "${ACPS_VERSION}" \
    --platform "${PLATFORM}" \
    "${lock_args[@]}" \
    --infra-dir "${INFRA_DIR}" \
    --smoke-commands "${SMOKE_COMMANDS}" \
    > "${VARS_FILE}"
# shellcheck source=/dev/null
source "${VARS_FILE}"
echo "  UPSTREAM_IMAGE=${UPSTREAM_IMAGE}"
echo "  DOCKERFILE_DIR=${DOCKERFILE_DIR}"
echo "  IMAGE_TAG=${IMAGE_TAG}"
echo "  IMAGE_FILENAME=${IMAGE_FILENAME}"
echo "  SMOKE_COMMAND=${SMOKE_COMMAND}"

TMP_TAR="$(mktemp "${TMPDIR:-/tmp}/acps-infra-image-export.XXXXXX.tar")"

echo "=== 构建并导出 Docker archive（不隐式加载） ==="
docker buildx build \
    --platform "${PLATFORM}" \
    --build-arg "UPSTREAM_IMAGE=${UPSTREAM_IMAGE}" \
    --build-arg "UPSTREAM_IMAGE_NAME=${UPSTREAM_IMAGE_NAME}" \
    --build-arg "UPSTREAM_DIGEST=${UPSTREAM_DIGEST}" \
    --build-arg "INFRA_ID=${INFRA_ID}" \
    --build-arg "UPSTREAM_VERSION=${UPSTREAM_VERSION}" \
    --build-arg "ACPS_VERSION=${ACPS_VERSION}" \
    --build-arg "PLATFORM=${PLATFORM}" \
    --build-arg "IMAGE_CREATED=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --build-arg "SOURCE_REVISION=${SOURCE_REVISION:-}" \
    -t "${IMAGE_TAG}" \
    -f "${DOCKERFILE_DIR}/Dockerfile" \
    --output "type=docker,dest=${TMP_TAR}" \
    "${DOCKERFILE_DIR}"

echo "=== 显式 docker load（不依赖 buildx 隐式加载） ==="
docker load -i "${TMP_TAR}"

echo "=== smoke：服务特定命令（不执行 Python pip check/import） ==="
if docker run --rm --platform "${PLATFORM}" --entrypoint sh "${IMAGE_TAG}" -c "true" >/dev/null 2>&1; then
  docker run --rm --platform "${PLATFORM}" --entrypoint sh "${IMAGE_TAG}" -c "${SMOKE_COMMAND}"
else
  # distroless / no-shell images（如 fluent-bit）：直接 exec smoke 命令
  # shellcheck disable=SC2086
  docker run --rm --platform "${PLATFORM}" --entrypoint "" "${IMAGE_TAG}" ${SMOKE_COMMAND}
fi

echo "=== smoke 通过，压缩为最终镜像包 ==="
final_path="${OUTPUT_DIR}/${IMAGE_FILENAME}"
# 原子写入：先写同目录下的临时文件，成功后再 mv 到 final_path，避免 gzip 中途失败
# 时在 --output 目录下留下一个损坏/不完整的.image.tar.gz。
GZIP_TMP="${final_path}.tmp.$$"
gzip -c "${TMP_TAR}" > "${GZIP_TMP}"
mv "${GZIP_TMP}" "${final_path}"
GZIP_TMP=""
echo "  ${final_path}"
if [[ -n "${RESULT_FILE}" ]]; then
    printf '%s\n' "${final_path}" > "${RESULT_FILE}"
fi
