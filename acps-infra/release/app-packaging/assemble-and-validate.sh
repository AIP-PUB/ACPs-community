#!/usr/bin/env bash
# 装配 + 全部校验一体化脚本。
#
# 装配完成后立即执行 （结构/checksum）、（auditwheel 基线）、
# （runtime smoke 离线安装 + import）三项校验；任一失败则不将最终包标记为
# 可发布（脚本以非零退出码终止，且不移动产物到"已发布"目录——调用方应只信任
# 本脚本 exit code 为 0 时打印的最终包路径）。
#
# 用法（必须在已解开的 assembly kit 根目录下执行，build context 需要同时包含
# packages/ 和 assembly/）：
# assemble-and-validate.sh --app <id> --platform <linux/amd64|linux/arm64|darwin/arm64> \
# --python-tag <tag> [--variant <variant>] --output <dir> \
# --import-check <module> [--import-check <module>...] \
# [--baseline <manylinux_2_28|manylinux2014>]
#
# darwin/*：本机 assemble-host.sh 装配；跳过 manylinux auditwheel，改做平台 tag
# 轻量检查；runtime smoke 在本机 venv 离线安装（不经 Docker）。

set -euo pipefail
shopt -s nullglob

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP=""
PLATFORM=""
PYTHON_TAG=""
VARIANT=""
OUTPUT_DIR=""
BASELINE="manylinux_2_28"
declare -a IMPORT_CHECKS=()
declare -a ASSERT_ABSENT=()
declare -a ASSERT_PRESENT=()
declare -a DENY_PACKAGE=()
declare -a DENY_PACKAGE_PREFIX=()
declare -a REQUIRE_PACKAGE=()
declare -a SKIP_PACKAGE_PREFIX=()

usage() {
    cat <<'EOF'
用法：assemble-and-validate.sh --app <id> --platform <os/arch> --python-tag <tag>
       [--variant <variant>] --output <dir> --import-check <module> [...]
    [--assert-absent <module>] [--assert-present <module>]
    [--deny-package <name>] [--deny-package-prefix <prefix>] [--require-package <name>]
    [--skip-package-prefix <prefix>]
    [--baseline <manylinux_2_28|manylinux2014>]
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app) shift; APP="${1:-}" ;;
        --platform) shift; PLATFORM="${1:-}" ;;
        --python-tag) shift; PYTHON_TAG="${1:-}" ;;
        --variant) shift; VARIANT="${1:-}" ;;
        --output) shift; OUTPUT_DIR="${1:-}" ;;
        --baseline) shift; BASELINE="${1:-}" ;;
        --import-check)
            shift
            IMPORT_CHECKS+=("${1:-}")
            ;;
        --assert-absent)
            shift
            ASSERT_ABSENT+=("${1:-}")
            ;;
        --assert-present)
            shift
            ASSERT_PRESENT+=("${1:-}")
            ;;
        --deny-package)
            shift
            DENY_PACKAGE+=("${1:-}")
            ;;
        --deny-package-prefix)
            shift
            DENY_PACKAGE_PREFIX+=("${1:-}")
            ;;
        --require-package)
            shift
            REQUIRE_PACKAGE+=("${1:-}")
            ;;
        --skip-package-prefix)
            shift
            SKIP_PACKAGE_PREFIX+=("${1:-}")
            ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "[ERROR] 未知参数：$1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ -z "${APP}" || -z "${PLATFORM}" || -z "${PYTHON_TAG}" || -z "${OUTPUT_DIR}" ]]; then
    echo "[ERROR] --app、--platform、--python-tag、--output 均为必填参数" >&2
    usage >&2
    exit 2
fi
if [[ "${#IMPORT_CHECKS[@]}" -eq 0 ]]; then
    echo "[ERROR] 至少需要一个 --import-check（显式配置，不能从 app id 推导，见 ）" >&2
    exit 2
fi

if [[ "${#SKIP_PACKAGE_PREFIX[@]}" -eq 0 && "${VARIANT}" == "gpu" ]]; then
    SKIP_PACKAGE_PREFIX+=(nvidia-)
fi

if [[ "${OUTPUT_DIR}" != /* ]]; then
    OUTPUT_DIR="${PWD}/${OUTPUT_DIR}"
fi
# --output 会被清空重建（rm -rf），对明显危险的路径做最低限度的 sanity check。
case "${OUTPUT_DIR}" in
    "/"|"//"|"${HOME}")
        echo "[ERROR] --output 解析为危险路径，拒绝执行（会被 rm -rf 清空）：${OUTPUT_DIR}" >&2
        exit 2
        ;;
esac
if [[ "$(echo "${OUTPUT_DIR}" | tr -cd '/' | wc -c)" -lt 2 ]]; then
    echo "[ERROR] --output 解析出的路径层级过浅，拒绝执行（会被 rm -rf 清空）：${OUTPUT_DIR}" >&2
    exit 2
fi

if [[ ! -d "packages" || ! -d "assembly" ]]; then
    echo "[ERROR] 当前目录不是已解开的 assembly kit 根目录（缺少 packages/ 或 assembly/）" >&2
    exit 1
fi

# 从 images.lock 中查找 builder / audit / runtime_smoke 镜像引用。
resolve_images_lock_paths() {
    if [[ -n "${IMAGES_LOCK_PATHS:-}" ]]; then
        printf '%s\n' "${IMAGES_LOCK_PATHS}"
        return
    fi

    printf '%s\n' "assembly/images.lock"
}

lookup_image() {
    local table="$1"
    local key="$2"

    if [[ "${table}" == "audit" && -n "${AUDIT_IMAGE_OVERRIDE:-}" ]]; then
        printf '%s\n' "${AUDIT_IMAGE_OVERRIDE}"
        return 0
    fi

    IMAGES_LOCK_PATHS="$(resolve_images_lock_paths)" TABLE_NAME="${table}" LOOKUP_KEY="${key}" python3 - <<'PY'
import os
import sys
import tomllib
from pathlib import Path

paths = [Path(p) for p in os.environ["IMAGES_LOCK_PATHS"].split(":") if p]
if not paths:
    sys.exit("未配置任何 images.lock 路径")

table = {}
for path in paths:
    if not path.is_file():
        continue
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    table.update(data.get(os.environ["TABLE_NAME"], {}) or {})

key = os.environ["LOOKUP_KEY"]
value = table.get(key)
if not value:
    joined = ", ".join(str(p) for p in paths)
    suffix = ""
    if os.environ["TABLE_NAME"] == "audit":
        suffix = "；可显式设置 AUDIT_IMAGE_OVERRIDE 做本地调试"
    sys.exit(
        f"在 [{os.environ['TABLE_NAME']}] 中找不到 key={key}（查找路径：{joined}{suffix}）"
    )
print(value)
PY
}

# 从装配包根目录的 manifest.toml 中读取 assembler_version 和指定项目 id 的
# source_commit——这两项只有编排脚本（本脚本，运行在装配包根目录下）能看到，
# assemble.sh 在容器内只拿到 packages/ 和自身脚本，看不到这份文件。
lookup_kit_manifest_field() {
    local field="$1"
    MANIFEST_TOML_PATH="manifest.toml" FIELD_NAME="${field}" APP_ID="${APP}" python3 - <<'PY'
import os
import tomllib
from pathlib import Path

path = Path(os.environ["MANIFEST_TOML_PATH"])
data = tomllib.loads(path.read_text(encoding="utf-8"))
field = os.environ["FIELD_NAME"]
if field == "assembler_version":
    print(data.get("assembler_version", "unknown"))
elif field == "app_source_commit":
    app_id = os.environ["APP_ID"]
    for project in data.get("projects", []):
        if project.get("id") == app_id:
            print(project.get("source_commit", "unknown"))
            break
    else:
        print("unknown")
PY
}

# 把额外的 "key = value" TOML 行追加进最终包内的 build-manifest.toml，并同步重新生成
# 根 checksums.txt、重新打包最终 tar（保持顶层目录名不变）。用于回填只有编排脚本自己
# 知道、assemble.sh 在容器内运行时无法确定的事实（装配包版本/来源 commit、三类镜像
# digest、校验结果摘要——后者只有在三项校验都跑完之后才知道）。
enrich_build_manifest() {
    local tar_path="$1"
    shift

    local enrich_dir
    enrich_dir="$(mktemp -d)"
    # 在 macOS 上显式关闭 AppleDouble/copyfile 元数据，避免回填 manifest 后重新打包
    # 时把 `._*` 杂项文件写进最终 tar，破坏设计要求的单一顶层目录结构。
    COPYFILE_DISABLE=1 tar -xzf "${tar_path}" -C "${enrich_dir}"

    local top_dir=""
    local candidate=""
    for candidate in "${enrich_dir}"/*/; do
        top_dir="${candidate%/}"
    done
    if [[ -z "${top_dir}" ]]; then
        echo "[ERROR] enrich_build_manifest：解压后未找到顶层目录：${tar_path}" >&2
        rm -rf "${enrich_dir}"
        exit 1
    fi

    local manifest_path="${top_dir}/build-manifest.toml"
    if [[ ! -f "${manifest_path}" ]]; then
        echo "[ERROR] enrich_build_manifest：找不到 build-manifest.toml：${manifest_path}" >&2
        rm -rf "${enrich_dir}"
        exit 1
    fi

    local line
    for line in "$@"; do
        printf '%s\n' "${line}" >> "${manifest_path}"
    done

    local sha256_cmd=()
    if command -v sha256sum >/dev/null 2>&1; then
        sha256_cmd=(sha256sum)
    elif command -v shasum >/dev/null 2>&1; then
        sha256_cmd=(shasum -a 256)
    else
        echo "[ERROR] enrich_build_manifest：未找到 sha256sum 或 shasum 命令" >&2
        rm -rf "${enrich_dir}"
        exit 1
    fi

    (
        cd "${top_dir}"
        find . -type f ! -path './checksums.txt' -print0 \
            | LC_ALL=C sort -z \
            | xargs -0 "${sha256_cmd[@]}" > checksums.txt
    )

    local tar_name
    tar_name="$(basename "${top_dir}")"
    COPYFILE_DISABLE=1 tar -czf "${tar_path}" -C "${enrich_dir}" "${tar_name}"

    rm -rf "${enrich_dir}"
}

IS_DARWIN=0
case "${PLATFORM}" in
    darwin/*) IS_DARWIN=1 ;;
esac

AUDIT_IMAGE=""
RUNTIME_SMOKE_IMAGE=""
BUILDER_IMAGE=""
if [[ "${IS_DARWIN}" -eq 0 ]]; then
    AUDIT_IMAGE="$(lookup_image audit "${PLATFORM}")"
    RUNTIME_SMOKE_IMAGE="$(lookup_image runtime_smoke "${PLATFORM},${PYTHON_TAG}")"
    BUILDER_IMAGE="$(lookup_image builder "${PLATFORM}")"

    # 若调用方显式传入本地 daemon 引用（例如单机调试），继续允许运行，但给出醒目的
    # 告警，避免被误当成可跨机器复现的正式发布输入。正式发布脚本默认只读取 images.lock
    # 中的 registry digest。
    case "${AUDIT_IMAGE}" in
        acps-audit:*)
            echo "[WARN] audit 镜像（${AUDIT_IMAGE}）是本地 docker daemon 构建产物，不是可跨机器复现的" >&2
            echo "        正式发布应使用 assembly/images.lock 中可 docker pull 的 registry digest。" >&2
            ;;
    esac
else
    BUILDER_IMAGE="host-native"
    AUDIT_IMAGE="host-native-darwin-tag-check"
    RUNTIME_SMOKE_IMAGE="host-native"
fi

echo "=== 装配：app=${APP} platform=${PLATFORM} python_tag=${PYTHON_TAG} variant=${VARIANT:-<none>} ==="
# 每次都从干净目录开始装配，避免同一 --output 目录多次调用时，上一次运行留下的旧 tar.gz
# 被下面的 glob 错误引用（无法区分本次新产物与历史产物）。
rm -rf "${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"

if [[ "${IS_DARWIN}" -eq 1 ]]; then
    if [[ ! -x "${SCRIPT_DIR}/assemble-host.sh" ]]; then
        echo "[ERROR] darwin 装配需要 ${SCRIPT_DIR}/assemble-host.sh（未找到或不可执行）" >&2
        exit 1
    fi
    bash "${SCRIPT_DIR}/assemble-host.sh" \
        --app "${APP}" \
        --python-tag "${PYTHON_TAG}" \
        --variant "${VARIANT}" \
        --platform "${PLATFORM}" \
        --output "${OUTPUT_DIR}"
else
    docker buildx build \
        --platform "${PLATFORM}" \
        --build-arg "APP=${APP}" \
        --build-arg "PYTHON_TAG=${PYTHON_TAG}" \
        --build-arg "VARIANT=${VARIANT}" \
        --target export \
        -f assembly/Dockerfile \
        --output "type=local,dest=${OUTPUT_DIR}" \
        .
fi

final_tar=""
match_count=0
for candidate in "${OUTPUT_DIR}"/*.tar.gz; do
    final_tar="${candidate}"
    match_count=$((match_count + 1))
done
if [[ "${match_count}" -eq 0 ]]; then
    echo "[ERROR] 装配完成但未在 ${OUTPUT_DIR} 找到最终包 tar" >&2
    exit 1
fi
if [[ "${match_count}" -gt 1 ]]; then
    echo "[ERROR] ${OUTPUT_DIR} 下存在多个 tar.gz，无法确定哪个是本次装配产物：" >&2
    for candidate in "${OUTPUT_DIR}"/*.tar.gz; do
        echo "  - ${candidate}" >&2
    done
    exit 1
fi
echo "  最终包：${final_tar}"

echo "=== 回填装配期事实（装配包版本/来源 commit、三类镜像 digest） ==="
assembly_kit_version="$(lookup_kit_manifest_field assembler_version)"
app_source_commit="$(lookup_kit_manifest_field app_source_commit)"
enrich_build_manifest "${final_tar}" \
    "assembly_kit_version = \"${assembly_kit_version}\"" \
    "app_source_commit = \"${app_source_commit}\"" \
    "builder_image = \"${BUILDER_IMAGE}\"" \
    "audit_image = \"${AUDIT_IMAGE}\"" \
    "runtime_smoke_image = \"${RUNTIME_SMOKE_IMAGE}\""

echo "=== ：结构与 checksum 校验 ==="
python3 "${SCRIPT_DIR}/validate_app_release_package.py" --package "${final_tar}"

echo "=== ：wheelhouse 审计（linux=auditwheel；darwin=平台 tag） ==="
audit_args=(--package "${final_tar}" --platform "${PLATFORM}" --baseline "${BASELINE}" --audit-image "${AUDIT_IMAGE}")
if [[ "${#DENY_PACKAGE[@]}" -gt 0 ]]; then
    for pkg in "${DENY_PACKAGE[@]}"; do
        audit_args+=(--deny-package "${pkg}")
    done
fi
if [[ "${#DENY_PACKAGE_PREFIX[@]}" -gt 0 ]]; then
    for prefix in "${DENY_PACKAGE_PREFIX[@]}"; do
        audit_args+=(--deny-package-prefix "${prefix}")
    done
fi
if [[ "${#REQUIRE_PACKAGE[@]}" -gt 0 ]]; then
    for pkg in "${REQUIRE_PACKAGE[@]}"; do
        audit_args+=(--require-package "${pkg}")
    done
fi
if [[ "${#SKIP_PACKAGE_PREFIX[@]}" -gt 0 ]]; then
    for prefix in "${SKIP_PACKAGE_PREFIX[@]}"; do
        audit_args+=(--skip-package-prefix "${prefix}")
    done
fi
python3 "${SCRIPT_DIR}/audit_wheelhouse.py" "${audit_args[@]}"

echo "=== ：runtime smoke 离线安装 + import 校验 ==="
smoke_args=(--package "${final_tar}" --platform "${PLATFORM}" --python-tag "${PYTHON_TAG}" --smoke-image "${RUNTIME_SMOKE_IMAGE}")
for module in "${IMPORT_CHECKS[@]}"; do
    smoke_args+=(--import-check "${module}")
done
if [[ "${#ASSERT_ABSENT[@]}" -gt 0 ]]; then
    for module in "${ASSERT_ABSENT[@]}"; do
        smoke_args+=(--assert-absent "${module}")
    done
fi
if [[ "${#ASSERT_PRESENT[@]}" -gt 0 ]]; then
    for module in "${ASSERT_PRESENT[@]}"; do
        smoke_args+=(--assert-present "${module}")
    done
fi
python3 "${SCRIPT_DIR}/runtime_smoke.py" "${smoke_args[@]}"

echo "=== 回填校验结果摘要 ==="
# 只有三项校验都真正跑完并成功（脚本走到这里，前面任何一步失败都已经 exit 非零）才
# 回填 validation_passed = true——避免中途失败的产物里出现"声称已通过"的虚假记录。
import_checks_toml="["
first=1
for module in "${IMPORT_CHECKS[@]}"; do
    [[ "${first}" -eq 1 ]] || import_checks_toml+=", "
    import_checks_toml+="\"${module}\""
    first=0
done
import_checks_toml+="]"
validation_baseline_value="${BASELINE}"
if [[ "${IS_DARWIN}" -eq 1 ]]; then
    validation_baseline_value="darwin-platform-tags"
fi
enrich_build_manifest "${final_tar}" \
    "validated_at = \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"" \
    "validation_baseline = \"${validation_baseline_value}\"" \
    "validation_import_checks = ${import_checks_toml}" \
    "validation_passed = true"

echo "=== 全部校验通过，可标记为可发布 ==="
echo "  ${final_tar}"
