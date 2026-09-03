#!/usr/bin/env bash
# 本地构建全部正式应用最终发布包。
#
# 先采集平台无关的 assembly kit，再按本机 arch 的发布矩阵执行
# assemble-and-validate.sh。每个组合在装配后立即完成结构校验、wheelhouse 审计
# 和 runtime smoke；末尾再逐项检查发布矩阵中的最终包是否都真实产出。
#
# 默认：业务包 = linux/<host_arch>；acps-cli = linux；discovery = cpu。
# Mac 控制节点：--cli-target-os darwin,linux 额外产出 acps-cli darwin 包。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECT_SCRIPT="${SCRIPT_DIR}/collect-app-release-kit.sh"

OUTPUT_DIR=""
WORK_DIR=""
ASSEMBLY_KIT_DIR=""
PYTHON_TAG="cp314"
BASELINE="manylinux_2_28"
CLI_TARGET_OS_RAW="linux"
DISCOVERY_VARIANT="cpu"

usage() {
    cat <<'EOF'
用法：build-app-release-packages.sh --output <dir> [选项]

选项：
  --output <dir>              最终发布包输出目录；脚本会清空后重建（顶层平铺 tar.gz）
  --work-dir <dir>            采集 assembly kit 的工作目录；默认使用临时目录
  --assembly-kit <dir>        复用已经采集好的 assembly kit 目录，跳过采集步骤
  --python-tag <tag>          CPython ABI 标签；默认 cp314
  --cli-target-os <list>      acps-cli 目标 OS，逗号分隔；默认 linux；合法值 linux,darwin
  --discovery-variant <v>     discovery 变体：cpu（默认）| gpu | both
  -h, --help                  打印帮助

本机矩阵（不做跨 arch / 跨 OS 交叉）：
  - 业务应用：仅 linux/<host_arch>
  - acps-cli：按 --cli-target-os（darwin 仅允许在 Darwin 构建机上请求）
  - discovery-server：按 --discovery-variant（gpu/both 仅允许在 Linux 构建机上）

兼容环境变量（可选覆盖，优先于自动检测的 linux 平台列表）：
  ACPS_APP_RELEASE_PLATFORMS="linux/amd64 linux/arm64"
  ACPS_APP_RELEASE_INCLUDE_GPU=1   # 等价于 --discovery-variant 含 gpu（与 both 组合时见脚本）
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output) shift; OUTPUT_DIR="${1:-}" ;;
        --work-dir) shift; WORK_DIR="${1:-}" ;;
        --assembly-kit) shift; ASSEMBLY_KIT_DIR="${1:-}" ;;
        --python-tag) shift; PYTHON_TAG="${1:-}" ;;
        --cli-target-os) shift; CLI_TARGET_OS_RAW="${1:-}" ;;
        --discovery-variant) shift; DISCOVERY_VARIANT="${1:-}" ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "[ERROR] 未知参数：$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ -z "${OUTPUT_DIR}" ]]; then
    echo "[ERROR] 必须提供 --output <dir>" >&2
    usage >&2
    exit 2
fi

to_abs_path() {
    local path="$1"
    if [[ "${path}" == /* ]]; then
        printf '%s\n' "${path}"
    else
        printf '%s/%s\n' "${PWD}" "${path}"
    fi
}

assert_safe_clear_path() {
    local path="$1"
    local label="$2"

    case "${path}" in
        ""|"/"|"//"|"${HOME}")
            echo "[ERROR] ${label} 解析为危险路径，拒绝清空：${path}" >&2
            exit 2
            ;;
    esac
    if [[ "$(printf '%s' "${path}" | tr -cd '/' | wc -c)" -lt 2 ]]; then
        echo "[ERROR] ${label} 路径层级过浅，拒绝清空：${path}" >&2
        exit 2
    fi
}

platform_slug() {
    printf '%s\n' "${1//\//-}"
}

import_module_for_app() {
    case "$1" in
        demo-leader) printf '%s\n' "leader" ;;
        demo-partner) printf '%s\n' "partners" ;;
        acps-cli) printf '%s\n' "acps_cli" ;;
        *) printf '%s\n' "app" ;;
    esac
}

detect_host_os() {
    case "$(uname -s | tr '[:upper:]' '[:lower:]')" in
        linux*) printf '%s\n' "linux" ;;
        darwin*) printf '%s\n' "darwin" ;;
        *)
            echo "[ERROR] 不支持的本机 OS：$(uname -s)" >&2
            exit 2
            ;;
    esac
}

detect_host_arch() {
    case "$(uname -m)" in
        x86_64|amd64) printf '%s\n' "amd64" ;;
        arm64|aarch64) printf '%s\n' "arm64" ;;
        *)
            echo "[ERROR] 不支持的本机架构：$(uname -m)" >&2
            exit 2
            ;;
    esac
}

HOST_OS="$(detect_host_os)"
HOST_ARCH="$(detect_host_arch)"
DEFAULT_LINUX_PLATFORM="linux/${HOST_ARCH}"

# --- 解析 --cli-target-os ---
declare -a CLI_TARGET_OS=()
IFS=',' read -r -a _cli_raw_parts <<< "${CLI_TARGET_OS_RAW}"
for raw in "${_cli_raw_parts[@]}"; do
    os="$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    [[ -n "${os}" ]] || continue
    case "${os}" in
        linux|darwin) ;;
        *)
            echo "[ERROR] --cli-target-os 含非法值：${os}（合法：linux,darwin）" >&2
            exit 2
            ;;
    esac
    # 去重保序
    skip=0
    for existing in "${CLI_TARGET_OS[@]+"${CLI_TARGET_OS[@]}"}"; do
        if [[ "${existing}" == "${os}" ]]; then
            skip=1
            break
        fi
    done
    if [[ "${skip}" -eq 0 ]]; then
        CLI_TARGET_OS+=("${os}")
    fi
done
if [[ "${#CLI_TARGET_OS[@]}" -eq 0 ]]; then
    echo "[ERROR] --cli-target-os 解析后为空" >&2
    exit 2
fi
for os in "${CLI_TARGET_OS[@]}"; do
    if [[ "${os}" == "darwin" && "${HOST_OS}" != "darwin" ]]; then
        echo "[ERROR] --cli-target-os 含 darwin，但当前构建机不是 Darwin（不做跨 OS 交叉）" >&2
        exit 2
    fi
done

# --- 解析 --discovery-variant（兼容旧 env） ---
case "${DISCOVERY_VARIANT}" in
    cpu|gpu|both) ;;
    *)
        echo "[ERROR] --discovery-variant 须为 cpu|gpu|both，收到：${DISCOVERY_VARIANT}" >&2
        exit 2
        ;;
esac
if [[ "${ACPS_APP_RELEASE_INCLUDE_GPU:-0}" == "1" ]]; then
    if [[ "${DISCOVERY_VARIANT}" == "cpu" ]]; then
        DISCOVERY_VARIANT="both"
    fi
fi
if [[ "${DISCOVERY_VARIANT}" == "gpu" || "${DISCOVERY_VARIANT}" == "both" ]]; then
    if [[ "${HOST_OS}" != "linux" ]]; then
        echo "[ERROR] --discovery-variant=${DISCOVERY_VARIANT} 需要 Linux 构建机（GPU 依赖本机 Linux 工具链编译）" >&2
        exit 2
    fi
    if [[ "${HOST_ARCH}" != "arm64" ]]; then
        echo "[ERROR] discovery GPU 包仅支持 linux/arm64（本机 arch=${HOST_ARCH}）" >&2
        exit 2
    fi
fi

OUTPUT_DIR="$(to_abs_path "${OUTPUT_DIR}")"
assert_safe_clear_path "${OUTPUT_DIR}" "--output"

if [[ -n "${ASSEMBLY_KIT_DIR}" ]]; then
    ASSEMBLY_KIT_DIR="$(to_abs_path "${ASSEMBLY_KIT_DIR}")"
else
    if [[ -z "${WORK_DIR}" ]]; then
        WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/acps-release-build.XXXXXX")"
    else
        WORK_DIR="$(to_abs_path "${WORK_DIR}")"
        assert_safe_clear_path "${WORK_DIR}" "--work-dir"
        mkdir -p "${WORK_DIR}"
    fi
    ASSEMBLY_KIT_DIR="${WORK_DIR}/assembly-kit"
    assert_safe_clear_path "${ASSEMBLY_KIT_DIR}" "assembly-kit"

    case "${DISCOVERY_VARIANT}" in
        cpu) export DISCOVERY_PACKAGE_VARIANTS="cpu" ;;
        gpu) export DISCOVERY_PACKAGE_VARIANTS="gpu" ;;
        both) export DISCOVERY_PACKAGE_VARIANTS="cpu,gpu" ;;
    esac

    echo "=== 采集 assembly kit：${ASSEMBLY_KIT_DIR}（DISCOVERY_PACKAGE_VARIANTS=${DISCOVERY_PACKAGE_VARIANTS}） ==="
    "${COLLECT_SCRIPT}" --output "${ASSEMBLY_KIT_DIR}"
fi

if [[ ! -d "${ASSEMBLY_KIT_DIR}/packages" || ! -d "${ASSEMBLY_KIT_DIR}/assembly" ]]; then
    echo "[ERROR] assembly kit 目录非法，缺少 packages/ 或 assembly/：${ASSEMBLY_KIT_DIR}" >&2
    exit 1
fi

rm -rf "${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"

declare -a EXPECTED_PATTERNS=()

run_package() {
    local app="$1"
    local platform="$2"
    local variant="$3"
    local import_module="$4"
    shift 4

    local slug
    slug="$(platform_slug "${platform}")"

    # assemble-and-validate 会 rm -rf --output；用独立临时目录，成功后把 tar 平铺到 OUTPUT_DIR。
    local combo_output
    combo_output="$(mktemp -d "${TMPDIR:-/tmp}/acps-app-release-assemble.XXXXXX")"

    echo "=== 构建最终发布包：app=${app} platform=${platform} variant=${variant:-<none>} ==="

    local -a args=(
        --app "${app}"
        --platform "${platform}"
        --python-tag "${PYTHON_TAG}"
        --output "${combo_output}"
        --import-check "${import_module}"
        --baseline "${BASELINE}"
    )
    if [[ -n "${variant}" ]]; then
        args+=(--variant "${variant}")
    fi
    while [[ $# -gt 0 ]]; do
        args+=("$1")
        shift
    done

    (
        cd "${ASSEMBLY_KIT_DIR}"
        bash assembly/assemble-and-validate.sh "${args[@]}"
    )

    local produced=""
    local count=0
    local candidate
    for candidate in "${combo_output}"/*.tar.gz; do
        produced="${candidate}"
        count=$((count + 1))
    done
    if [[ "${count}" -ne 1 ]]; then
        echo "[ERROR] 期望在 ${combo_output} 产出恰好 1 个 tar.gz，实际 ${count}" >&2
        rm -rf "${combo_output}"
        exit 1
    fi
    mv "${produced}" "${OUTPUT_DIR}/"
    rm -rf "${combo_output}"

    if [[ -n "${variant}" ]]; then
        EXPECTED_PATTERNS+=("${app}-${slug}-${PYTHON_TAG}-${variant}-app-release-*.tar.gz")
    else
        EXPECTED_PATTERNS+=("${app}-${slug}-${PYTHON_TAG}-app-release-*.tar.gz")
    fi
}

declare -a BUSINESS_APPS=(
    registry-server
    ca-server
    mq-auth-server
    monitor-server
    demo-leader
    demo-partner
)

# 业务应用：默认本机 linux；可用 ACPS_APP_RELEASE_PLATFORMS 覆盖（仍应为 linux/*）
declare -a LINUX_PLATFORMS=()
if [[ -n "${ACPS_APP_RELEASE_PLATFORMS:-}" ]]; then
    # shellcheck disable=SC2206
    LINUX_PLATFORMS=(${ACPS_APP_RELEASE_PLATFORMS})
else
    LINUX_PLATFORMS=("${DEFAULT_LINUX_PLATFORM}")
fi

for app in "${BUSINESS_APPS[@]}"; do
    import_module="$(import_module_for_app "${app}")"
    for platform in "${LINUX_PLATFORMS[@]}"; do
        run_package "${app}" "${platform}" "" "${import_module}"
    done
done

# acps-cli：按 --cli-target-os
for os in "${CLI_TARGET_OS[@]}"; do
    cli_platform="${os}/${HOST_ARCH}"
    run_package "acps-cli" "${cli_platform}" "" "acps_cli"
done

# discovery
run_discovery_cpu() {
    local platform="$1"
    run_package \
        discovery-server "${platform}" cpu app \
        --deny-package torch \
        --deny-package flagembedding \
        --deny-package transformers \
        --deny-package sentence-transformers \
        --deny-package peft \
        --deny-package accelerate \
        --deny-package datasets \
        --deny-package sentencepiece \
        --deny-package-prefix nvidia- \
        --assert-absent torch \
        --assert-absent FlagEmbedding
}

run_discovery_gpu() {
    run_package \
        discovery-server linux/arm64 gpu app \
        --require-package torch \
        --require-package flagembedding \
        --require-package transformers \
        --require-package sentence-transformers \
        --require-package peft \
        --require-package accelerate \
        --require-package datasets \
        --require-package sentencepiece \
        --assert-present torch \
        --assert-present FlagEmbedding
}

case "${DISCOVERY_VARIANT}" in
    cpu)
        for platform in "${LINUX_PLATFORMS[@]}"; do
            run_discovery_cpu "${platform}"
        done
        ;;
    gpu)
        run_discovery_gpu
        ;;
    both)
        for platform in "${LINUX_PLATFORMS[@]}"; do
            run_discovery_cpu "${platform}"
        done
        run_discovery_gpu
        ;;
esac

echo "=== 发布矩阵完备性检查（顶层平铺） ==="
missing=0
for pattern in "${EXPECTED_PATTERNS[@]}"; do
    if find "${OUTPUT_DIR}" -maxdepth 1 -type f -name "${pattern}" -print -quit | grep -q .; then
        echo "[OK]      ${pattern}"
    else
        echo "[MISSING] ${pattern}" >&2
        missing=1
    fi
done

# 拒绝残留嵌套目录产物
nested="$(find "${OUTPUT_DIR}" -mindepth 2 -type f -name '*-app-release-*.tar.gz' 2>/dev/null | head -n 5 || true)"
if [[ -n "${nested}" ]]; then
    echo "[ERROR] --output 下存在嵌套的 app-release（应仅顶层平铺）：" >&2
    printf '%s\n' "${nested}" >&2
    missing=1
fi

if [[ "${missing}" -ne 0 ]]; then
    echo "[ERROR] 发布矩阵中至少一个组合缺少最终包，或输出布局不符合扁平约定。" >&2
    exit 1
fi

echo "=== 发布矩阵全部通过 ==="
find "${OUTPUT_DIR}" -maxdepth 1 -type f -name '*-app-release-*.tar.gz' | LC_ALL=C sort
