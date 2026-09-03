#!/usr/bin/env bash
# 简单集合 tar 生成脚本。
#
# 只把同一 Docker platform 对应 platform slug 的 `.image.tar.gz` 文件原样打包在一起，
# 方便一次性传输；不表达组件清单、拓扑、安装顺序或版本矩阵，不生成任何外层
# manifest/checksum。安装阶段仍然只消费其中的独立 `.image.tar.gz`。
#
# 用法：
# build-image-collection.sh --platform <linux/amd64|linux/arm64> \
# --version <acps-version> --input <dir> --output <dir>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/lib"

PLATFORM=""
VERSION=""
INPUT_DIR=""
OUTPUT_DIR=""

usage() {
    cat <<'EOF'
用法：build-image-collection.sh --platform <linux/amd64|linux/arm64> \
    --version <acps-version> --input <dir> --output <dir>
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --platform) shift; PLATFORM="${1:-}" ;;
        --version) shift; VERSION="${1:-}" ;;
        --input) shift; INPUT_DIR="${1:-}" ;;
        --output) shift; OUTPUT_DIR="${1:-}" ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "[ERROR] 未知参数：$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ -z "${PLATFORM}" || -z "${VERSION}" || -z "${INPUT_DIR}" || -z "${OUTPUT_DIR}" ]]; then
    echo "[ERROR] --platform、--version、--input、--output 均为必填参数" >&2
    usage >&2
    exit 2
fi
if [[ ! -d "${INPUT_DIR}" ]]; then
    echo "[ERROR] --input 不存在或不是目录：${INPUT_DIR}" >&2
    exit 2
fi

platform_slug="$(python3 "${LIB_DIR}/common.py" platform-slug --platform "${PLATFORM}")"

mkdir -p "${OUTPUT_DIR}"
OUTPUT_DIR="$(cd "${OUTPUT_DIR}" && pwd)"

# collection_path 在下方解析出后赋值；tar -cf 写到一半失败（磁盘满等）时不能在
# --output 目录下留下一个损坏/不完整的集合 tar。
collection_path=""
cleanup() {
    local ret=$?
    if [[ -n "${collection_path}" && -f "${collection_path}" && "${ret}" -ne 0 ]]; then
        rm -f "${collection_path}"
    fi
    return "${ret}"
}
trap cleanup EXIT

# 两种命名场景都要匹配：
# - 无 variant / infra 镜像：acps-<id>-...-<platform_slug>.image.tar.gz
# - 带 variant 的应用镜像： acps-<app>-<version>-<platform_slug>-<variant>.image.tar.gz
declare -a matches=()
shopt -s nullglob
for candidate in "${INPUT_DIR}"/*-"${platform_slug}".image.tar.gz "${INPUT_DIR}"/*-"${platform_slug}"-*.image.tar.gz; do
    matches+=("${candidate}")
done
shopt -u nullglob

if [[ "${#matches[@]}" -eq 0 ]]; then
    echo "[ERROR] 在 ${INPUT_DIR} 下找不到平台为 ${PLATFORM}（slug=${platform_slug}）的 .image.tar.gz 文件" >&2
    exit 1
fi

# 去重（两个 glob 可能重叠匹配同一个文件）。不使用关联数组（bash 3.2/macOS 默认
# bin/bash 不支持 `declare -A`），改用线性扫描比对。
_contains() {
    local needle="$1"
    shift
    local item
    for item in "$@"; do
        [[ "${item}" == "${needle}" ]] && return 0
    done
    return 1
}

declare -a unique_matches=()
for m in "${matches[@]}"; do
    if [[ "${#unique_matches[@]}" -eq 0 ]] || ! _contains "${m}" "${unique_matches[@]}"; then
        unique_matches+=("${m}")
    fi
done

collection_name="acps-images-${VERSION}-${platform_slug}.tar"
collection_path="${OUTPUT_DIR}/${collection_name}"

declare -a basenames=()
for m in "${unique_matches[@]}"; do
    basenames+=("$(basename "${m}")")
done

echo "=== 打包 ${#unique_matches[@]} 个镜像包到 ${collection_name} ==="
tar -cf "${collection_path}" -C "${INPUT_DIR}" "${basenames[@]}"

echo "=== 校验集合 tar 只包含 .image.tar.gz 文件 ==="
while IFS= read -r entry; do
    case "${entry}" in
        *.image.tar.gz) ;;
        *)
            echo "[ERROR] 集合 tar 中出现非 .image.tar.gz 条目：${entry}" >&2
            rm -f "${collection_path}"
            exit 1
            ;;
    esac
done < <(tar -tf "${collection_path}")

echo "  ${collection_path}"
for m in "${unique_matches[@]}"; do
    echo "    + $(basename "${m}")"
done
