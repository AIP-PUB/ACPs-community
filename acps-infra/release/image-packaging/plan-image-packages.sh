#!/usr/bin/env bash
# 规划 / 核对镜像包产物 镜像打包 UX）。
# 校验 targets + lock，扫描应用发布包，列出本机平台过滤后的期望应用/基础设施
# 镜像包文件名；若传入 --output 则核对目录中是否已有这些产物。

# 用法：
# plan-image-packages.sh --app-release-dir <dir>
# plan-image-packages.sh --app-release-dir <dir> --output <dir>
# 可选：
# [--targets <image-targets.toml>] 默认：本目录 image-targets.toml
# [--lock <image-inputs.lock>] 默认：本目录 image-inputs.lock
# [--platform <linux/amd64|linux/arm64>] 默认：linux/<host_arch>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/lib"
DEFAULT_TARGETS="${SCRIPT_DIR}/image-targets.toml"
DEFAULT_LOCK="${SCRIPT_DIR}/image-inputs.lock"

APP_RELEASE_DIR=""
OUTPUT_DIR=""
TARGETS=""
LOCK_PATH=""
FILTER_PLATFORM=""
FILTER_PLATFORM_SET=0

detect_host_arch() {
    local uname_m
    uname_m="$(uname -m)"
    case "${uname_m}" in
        x86_64) printf '%s\n' "amd64" ;;
        arm64|aarch64) printf '%s\n' "arm64" ;;
        *)
            echo "[ERROR] 不支持的构建机 arch：${uname_m}（仅支持 amd64 / arm64）" >&2
            exit 2
            ;;
    esac
}

usage() {
    cat <<'EOF'
用法：plan-image-packages.sh --app-release-dir <dir> [--output <dir>]

必填：--app-release-dir。

可选：
  [--output <dir>]  若提供则核对期望镜像包是否已在该目录
  [--targets <image-targets.toml>]  默认：本目录 image-targets.toml
  [--lock <image-inputs.lock>]      默认：本目录 image-inputs.lock
  [--platform <linux/amd64|linux/arm64>]  默认：linux/<host_arch>

未传 --output 时只打印规划（校验失败或期望目标数为 0 时退出 1；缺少应用发布包
输入仅告警）。传入 --output 时，任一期望镜像缺失或任一 app target 缺少唯一发布包
输入则退出 1。
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app-release-dir) shift; APP_RELEASE_DIR="${1:-}" ;;
        --output) shift; OUTPUT_DIR="${1:-}" ;;
        --targets) shift; TARGETS="${1:-}" ;;
        --lock) shift; LOCK_PATH="${1:-}" ;;
        --platform) shift; FILTER_PLATFORM="${1:-}"; FILTER_PLATFORM_SET=1 ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "[ERROR] 未知参数：$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ -z "${APP_RELEASE_DIR}" ]]; then
    echo "[ERROR] --app-release-dir 为必填参数" >&2
    usage >&2
    exit 2
fi

if [[ -z "${TARGETS}" ]]; then
    TARGETS="${DEFAULT_TARGETS}"
fi
if [[ -z "${LOCK_PATH}" ]]; then
    LOCK_PATH="${DEFAULT_LOCK}"
fi
if [[ "${FILTER_PLATFORM_SET}" -eq 0 ]]; then
    FILTER_PLATFORM="linux/$(detect_host_arch)"
fi

declare -a py_args=(
    --app-release-dir "${APP_RELEASE_DIR}"
    --targets "${TARGETS}"
    --lock "${LOCK_PATH}"
    --platform "${FILTER_PLATFORM}"
)
if [[ -n "${OUTPUT_DIR}" ]]; then
    py_args+=(--output "${OUTPUT_DIR}")
fi

exec python3 "${LIB_DIR}/plan_image_packages.py" "${py_args[@]}"
