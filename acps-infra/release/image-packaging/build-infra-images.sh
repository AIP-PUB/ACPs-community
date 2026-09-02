#!/usr/bin/env bash
# 基础设施镜像矩阵编排脚本。
#
# 读取 image-targets.toml 的 [[infra_targets]]，按 --id/--platform 过滤后依次调用
# build-infra-image.sh。
#
# 用法：
# build-infra-images.sh --targets image-targets.example.toml --output <dir> \
# [--id <infra-id>] [--platform <linux/amd64|linux/arm64>] [--lock <path>...]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/lib"

TARGETS=""
OUTPUT_DIR=""
FILTER_ID=""
FILTER_PLATFORM=""
declare -a LOCK_PATHS=()

usage() {
    cat <<'EOF'
用法：build-infra-images.sh --targets <image-targets.toml> --output <dir> \
    [--id <infra-id>] [--platform <linux/amd64|linux/arm64>] [--lock <path> ...]
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --targets) shift; TARGETS="${1:-}" ;;
        --output) shift; OUTPUT_DIR="${1:-}" ;;
        --id) shift; FILTER_ID="${1:-}" ;;
        --platform) shift; FILTER_PLATFORM="${1:-}" ;;
        --lock) shift; LOCK_PATHS+=("${1:-}") ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "[ERROR] 未知参数：$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ -z "${TARGETS}" ]]; then
    TARGETS="${SCRIPT_DIR}/image-targets.toml"
fi
if [[ -z "${OUTPUT_DIR}" ]]; then
    echo "[ERROR] --output 为必填参数" >&2
    usage >&2
    exit 2
fi
if [[ ${#LOCK_PATHS[@]} -eq 0 ]]; then
    LOCK_PATHS=("${SCRIPT_DIR}/image-inputs.lock")
fi
# 默认只打本机 arch（与教程一致）；显式 --platform 可覆盖。
if [[ -z "${FILTER_PLATFORM}" ]]; then
    case "$(uname -m)" in
        x86_64|amd64) FILTER_PLATFORM="linux/amd64" ;;
        arm64|aarch64) FILTER_PLATFORM="linux/arm64" ;;
        *)
            echo "[ERROR] 不支持的本机架构：$(uname -m)" >&2
            exit 2
            ;;
    esac
    echo "默认只构建 ${FILTER_PLATFORM}"
fi

mkdir -p "${OUTPUT_DIR}"

declare -a list_args=(--targets "${TARGETS}" list-infra-targets --format shell)
[[ -n "${FILTER_ID}" ]] && list_args+=(--id "${FILTER_ID}")
[[ -n "${FILTER_PLATFORM}" ]] && list_args+=(--platform "${FILTER_PLATFORM}")

declare -a build_infra_image_extra=()
if [[ "${#LOCK_PATHS[@]}" -gt 0 ]]; then
    for lock_path in "${LOCK_PATHS[@]}"; do
        build_infra_image_extra+=(--lock "${lock_path}")
    done
fi

total=0
succeeded=0
declare -a failed_targets=()
declare -a produced_paths=()

while IFS= read -r target_line; do
    [[ -z "${target_line}" ]] && continue
    eval "${target_line}"
    total=$((total + 1))

    label="id=${INFRA_ID} kind=${INFRA_KIND} upstream_version=${UPSTREAM_VERSION} acps_version=${ACPS_VERSION} platform=${PLATFORM}"
    echo ""
    echo "############################################################"
    echo "# 构建目标：${label}"
    echo "############################################################"

    result_file="$(mktemp "${TMPDIR:-/tmp}/acps-infra-image-result.XXXXXX")"
    build_ok=0
    if [[ "${#build_infra_image_extra[@]}" -gt 0 ]]; then
        if "${SCRIPT_DIR}/build-infra-image.sh" \
            --id "${INFRA_ID}" --kind "${INFRA_KIND}" \
            --upstream-version "${UPSTREAM_VERSION}" --acps-version "${ACPS_VERSION}" \
            --platform "${PLATFORM}" --output "${OUTPUT_DIR}" \
            --result-file "${result_file}" \
            "${build_infra_image_extra[@]}"; then
            build_ok=1
        fi
    else
        if "${SCRIPT_DIR}/build-infra-image.sh" \
            --id "${INFRA_ID}" --kind "${INFRA_KIND}" \
            --upstream-version "${UPSTREAM_VERSION}" --acps-version "${ACPS_VERSION}" \
            --platform "${PLATFORM}" --output "${OUTPUT_DIR}" \
            --result-file "${result_file}"; then
            build_ok=1
        fi
    fi

    if [[ "${build_ok}" -eq 1 ]]; then
        produced_path="$(cat "${result_file}")"
        produced_paths+=("${produced_path}")
        succeeded=$((succeeded + 1))
    else
        failed_targets+=("${label}: build-infra-image.sh 失败")
    fi
    rm -f "${result_file}"
done < <(python3 "${LIB_DIR}/targets.py" "${list_args[@]}")

echo ""
echo "============================================================"
echo "基础设施镜像矩阵构建汇总：${succeeded}/${total} 成功"
echo "============================================================"
if [[ "${#produced_paths[@]}" -gt 0 ]]; then
    for produced_path in "${produced_paths[@]}"; do
        echo "  OK   ${produced_path}"
    done
fi
if [[ "${#failed_targets[@]}" -gt 0 ]]; then
    for failure in "${failed_targets[@]}"; do
        echo "  FAIL ${failure}"
    done
fi

if [[ "${#failed_targets[@]}" -gt 0 ]]; then
    exit 1
fi
if [[ "${total}" -eq 0 ]]; then
    echo "[ERROR] 过滤条件没有匹配到任何 infra target" >&2
    exit 1
fi
