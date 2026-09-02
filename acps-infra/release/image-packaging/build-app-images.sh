#!/usr/bin/env bash
# 应用镜像矩阵编排脚本。
#
# 读取 image-targets.toml 的 [[app_targets]]，按 --app/--platform/--variant 过滤后
# 依次调用 build-app-image.sh；每个 target 独立生成临时 build context，互不污染。
#
# --app-release-dir 必须是一个包含"原始 *-app-release-*.tar.gz 文件"的目录（V1 不
# 接受只包含解包后目录的输入，见 ）；本脚本按
# "{app}-{platform-slug}-{python_tag}[-{variant}]-app-release-*.tar.gz" 通配匹配，
# 要求每个 target 都能唯一匹配到一个文件，否则整体失败。
#
# 用法：
# build-app-images.sh --targets image-targets.example.toml \
# --app-release-dir <dir> --output <dir> \
# [--app <id>] [--platform <linux/amd64|linux/arm64>] [--variant <v>] \
# [--lock <path>...] [--strategy <path>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/lib"

TARGETS=""
APP_RELEASE_DIR=""
OUTPUT_DIR=""
FILTER_APP=""
FILTER_PLATFORM=""
FILTER_VARIANT_SET=0
FILTER_VARIANT=""
STRATEGY=""
declare -a LOCK_PATHS=()

usage() {
    cat <<'EOF'
用法：build-app-images.sh --targets <image-targets.toml> \
    --app-release-dir <dir> --output <dir> \
    [--app <id>] [--platform <linux/amd64|linux/arm64>] [--variant <variant>] \
    [--lock <image-inputs.lock> ...] [--strategy <path>]
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --targets) shift; TARGETS="${1:-}" ;;
        --app-release-dir) shift; APP_RELEASE_DIR="${1:-}" ;;
        --output) shift; OUTPUT_DIR="${1:-}" ;;
        --app) shift; FILTER_APP="${1:-}" ;;
        --platform) shift; FILTER_PLATFORM="${1:-}" ;;
        --variant) shift; FILTER_VARIANT_SET=1; FILTER_VARIANT="${1:-}" ;;
        --lock) shift; LOCK_PATHS+=("${1:-}") ;;
        --strategy) shift; STRATEGY="${1:-}" ;;
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
if [[ -z "${APP_RELEASE_DIR}" || -z "${OUTPUT_DIR}" ]]; then
    echo "[ERROR] --app-release-dir、--output 均为必填参数" >&2
    usage >&2
    exit 2
fi
if [[ ${#LOCK_PATHS[@]} -eq 0 ]]; then
    LOCK_PATHS=("${SCRIPT_DIR}/image-inputs.lock")
fi
if [[ -z "${STRATEGY}" ]]; then
    STRATEGY="${SCRIPT_DIR}/startup-strategies.toml"
fi
if [[ ! -d "${APP_RELEASE_DIR}" ]]; then
    echo "[ERROR] --app-release-dir 不存在或不是目录：${APP_RELEASE_DIR}" >&2
    exit 2
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

declare -a list_args=(--targets "${TARGETS}" list-app-targets --format shell)
[[ -n "${FILTER_APP}" ]] && list_args+=(--app "${FILTER_APP}")
[[ -n "${FILTER_PLATFORM}" ]] && list_args+=(--platform "${FILTER_PLATFORM}")
if [[ "${FILTER_VARIANT_SET}" -eq 1 ]]; then
    list_args+=(--variant "${FILTER_VARIANT}")
fi

declare -a build_app_image_extra=()
if [[ -n "${STRATEGY}" ]]; then
    build_app_image_extra+=(--strategy "${STRATEGY}")
fi
if [[ "${#LOCK_PATHS[@]}" -gt 0 ]]; then
    for lock_path in "${LOCK_PATHS[@]}"; do
        build_app_image_extra+=(--lock "${lock_path}")
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

    platform_slug="$(python3 "${LIB_DIR}/common.py" platform-slug --platform "${PLATFORM}")"
    if [[ -n "${VARIANT}" ]]; then
        glob_pattern="${APP_RELEASE_DIR}/${APP}-${platform_slug}-${PYTHON_TAG}-${VARIANT}-app-release-*.tar.gz"
    else
        glob_pattern="${APP_RELEASE_DIR}/${APP}-${platform_slug}-${PYTHON_TAG}-app-release-*.tar.gz"
    fi

    matches=()
    for candidate in ${glob_pattern}; do
        [[ -f "${candidate}" ]] && matches+=("${candidate}")
    done

    label="app=${APP} platform=${PLATFORM} python_tag=${PYTHON_TAG} variant=${VARIANT:-<none>}"

    if [[ "${#matches[@]}" -eq 0 ]]; then
        echo "[ERROR] [${label}] 在 ${APP_RELEASE_DIR} 下找不到匹配的应用最终发布包（期望模式：${glob_pattern}）" >&2
        failed_targets+=("${label}: 未找到应用最终发布包")
        continue
    fi
    if [[ "${#matches[@]}" -gt 1 ]]; then
        echo "[ERROR] [${label}] 在 ${APP_RELEASE_DIR} 下匹配到多个应用最终发布包，无法确定唯一输入：" >&2
        for m in "${matches[@]}"; do
            echo "  - ${m}" >&2
        done
        failed_targets+=("${label}: 匹配到多个应用最终发布包")
        continue
    fi

    package="${matches[0]}"
    echo ""
    echo "############################################################"
    echo "# 构建目标：${label}"
    echo "# 应用最终发布包：${package}"
    echo "############################################################"

    result_file="$(mktemp "${TMPDIR:-/tmp}/acps-image-result.XXXXXX")"
    build_ok=0
    if [[ "${#build_app_image_extra[@]}" -gt 0 ]]; then
        if "${SCRIPT_DIR}/build-app-image.sh" \
            --app "${APP}" --platform "${PLATFORM}" --python-tag "${PYTHON_TAG}" --variant "${VARIANT}" \
            --package "${package}" --output "${OUTPUT_DIR}" \
            --result-file "${result_file}" \
            "${build_app_image_extra[@]}"; then
            build_ok=1
        fi
    else
        if "${SCRIPT_DIR}/build-app-image.sh" \
            --app "${APP}" --platform "${PLATFORM}" --python-tag "${PYTHON_TAG}" --variant "${VARIANT}" \
            --package "${package}" --output "${OUTPUT_DIR}" \
            --result-file "${result_file}"; then
            build_ok=1
        fi
    fi
    if [[ "${build_ok}" -eq 1 ]]; then
        produced_path="$(cat "${result_file}")"
        produced_paths+=("${produced_path}")
        succeeded=$((succeeded + 1))
    else
        failed_targets+=("${label}: build-app-image.sh 失败")
    fi
    rm -f "${result_file}"
done < <(python3 "${LIB_DIR}/targets.py" "${list_args[@]}")

echo ""
echo "============================================================"
echo "应用镜像矩阵构建汇总：${succeeded}/${total} 成功"
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
    echo "[ERROR] 过滤条件没有匹配到任何 app target" >&2
    exit 1
fi
