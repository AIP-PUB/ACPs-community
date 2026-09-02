#!/usr/bin/env bash

set -euo pipefail
export COPYFILE_DISABLE=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/package-build-lib.sh"

PROJECT_DIR="$(resolve_package_project_dir)"
RAW_OUTPUT_DIR="${PACKAGE_OUTPUT_DIR:-dist}"
if [[ "${RAW_OUTPUT_DIR}" == /* ]]; then
    OUTPUT_DIR="${RAW_OUTPUT_DIR}"
else
    OUTPUT_DIR="${PROJECT_DIR}/${RAW_OUTPUT_DIR#./}"
fi

PYTHON_VERSION="${PACKAGE_PYTHON_VERSION:-3.14}"

resolve_mutating_uv_cache_dir() {
    local cache_dir="${TMPDIR:-/tmp}/acps-uv-cache"
    mkdir -p "${cache_dir}"
    printf '%s\n' "${cache_dir}"
}

resolve_mutating_uv_python_install_dir() {
    local python_dir="${TMPDIR:-/tmp}/acps-uv-python"
    mkdir -p "${python_dir}"
    printf '%s\n' "${python_dir}"
}

resolve_mutating_pip_cache_dir() {
    local cache_dir="${TMPDIR:-/tmp}/acps-pip-cache"
    mkdir -p "${cache_dir}"
    printf '%s\n' "${cache_dir}"
}

run_uv_with_mutating_cache() {
    local cache_dir=""
    local python_dir=""
    local pip_cache_dir=""
    local -a env_args=()

    cache_dir="$(resolve_mutating_uv_cache_dir)"
    python_dir="$(resolve_mutating_uv_python_install_dir)"
    pip_cache_dir="$(resolve_mutating_pip_cache_dir)"
    env_args=(
        "UV_CACHE_DIR=${cache_dir}"
        "UV_PYTHON_INSTALL_DIR=${python_dir}"
        "PIP_CACHE_DIR=${pip_cache_dir}"
    )
    if [[ -n "${MACOSX_DEPLOYMENT_TARGET:-}" ]]; then
        env_args+=("MACOSX_DEPLOYMENT_TARGET=${MACOSX_DEPLOYMENT_TARGET}")
    fi
    if [[ -n "${PIP_NO_CACHE_DIR:-}" ]]; then
        env_args+=("PIP_NO_CACHE_DIR=${PIP_NO_CACHE_DIR}")
    fi
    if [[ -n "${_PYTHON_HOST_PLATFORM:-}" ]]; then
        env_args+=("_PYTHON_HOST_PLATFORM=${_PYTHON_HOST_PLATFORM}")
    fi
    if [[ -n "${ARCHFLAGS:-}" ]]; then
        env_args+=("ARCHFLAGS=${ARCHFLAGS}")
    fi
    env "${env_args[@]}" "$@"
}

usage() {
    cat <<'EOF'
用法：package-wheel-runtime.sh [--python-version <version>]

说明：
  --python-version <ver>    目标 Python 版本，默认 3.14

产出平台无关的应用薄包（自身 wheel + requirements-runtime.lock + 运行期资源 +
runtime-package.toml + checksums.txt），不含 wheelhouse/。
EOF
}

require_command() {
    local command_name="$1"

    if ! command -v "${command_name}" >/dev/null 2>&1; then
        echo "错误：未找到 ${command_name} 命令" >&2
        exit 1
    fi
}

package_build_env_python_path() {
    local raw_build_env_dir="${PACKAGE_BUILD_ENV_DIR:-.package-build-venv}"
    local build_env_dir=""

    if [[ "${raw_build_env_dir}" == /* ]]; then
        build_env_dir="${raw_build_env_dir}"
    else
        build_env_dir="${PROJECT_DIR}/${raw_build_env_dir#./}"
    fi

    printf '%s/bin/python\n' "${build_env_dir}"
}

require_build_python() {
    local build_python=""

    build_python="$(package_build_env_python_path)"
    if [[ ! -x "${build_python}" ]]; then
        echo "错误：未找到 package 构建 Python：${build_python}" >&2
        echo "请先执行 just package bootstrap。" >&2
        exit 1
    fi
}

resolve_project_version() {
    local project_dir="${1:-${PROJECT_DIR}}"
    local pyproject_path="${project_dir}/pyproject.toml"

    if [[ ! -f "${pyproject_path}" ]]; then
        echo "错误：未找到 pyproject.toml：${pyproject_path}" >&2
        exit 1
    fi

    PACKAGE_PYPROJECT_PATH="${pyproject_path}" python3 - <<'PY'
import os
import re
from pathlib import Path

text = Path(os.environ["PACKAGE_PYPROJECT_PATH"]).read_text(encoding="utf-8")

try:
    import tomllib  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - fallback for older python3
    tomllib = None

version = ""
if tomllib is not None:
    data = tomllib.loads(text)
    version = str((data.get("project", {}) or {}).get("version", "")).strip()
else:
    in_project = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project = (line == "[project]")
            continue
        if in_project and line.startswith("version"):
            match = re.match(r'version\s*=\s*["\']([^"\']+)["\']', line)
            if match:
                version = match.group(1).strip()
                break

if not version:
    raise SystemExit("pyproject.toml 缺少 project.version")
print(version)
PY
}

resolve_release_name_prefix() {
    if [[ -n "${PACKAGE_RELEASE_NAME_PREFIX:-}" ]]; then
        printf '%s\n' "${PACKAGE_RELEASE_NAME_PREFIX}"
        return
    fi

    printf '%s-wheel\n' "$(basename "${PROJECT_DIR}")"
}

resolve_project_wheel_distribution() {
    if [[ -n "${PACKAGE_WHEEL_DISTRIBUTION:-}" ]]; then
        printf '%s\n' "${PACKAGE_WHEEL_DISTRIBUTION}"
        return
    fi

    resolve_wheel_distribution_from_pyproject "${PROJECT_DIR}"
}

build_wheel_for_project() {
    local project_dir="$1"
    local output_dir="$2"
    local build_mode="${PACKAGE_BUILD_MODE:-uv-build}"
    local build_python=""

    mkdir -p "${output_dir}"

    case "${build_mode}" in
        uv-build|"")
            (
                cd "${project_dir}"
                run_uv_with_mutating_cache uv build --wheel --out-dir "${output_dir}"
            )
            ;;
        hatchling-build-env)
            build_python="$(package_build_env_python_path)"
            (
                cd "${project_dir}"
                "${build_python}" -m hatchling build -t wheel -d "${output_dir}"
            )
            ;;
        project)
            if ! declare -F run_project_package_build_wheels >/dev/null 2>&1; then
                echo "错误：PACKAGE_BUILD_MODE=project，但未声明 run_project_package_build_wheels hook。" >&2
                exit 1
            fi
            run_project_package_build_wheels "${project_dir}" "${output_dir}"
            ;;
        *)
            echo "错误：未知 PACKAGE_BUILD_MODE=${build_mode}" >&2
            exit 1
            ;;
    esac
}

apply_runtime_chmod_paths() {
    local staging_dir="$1"
    local relative_path=""

    if [[ "${#PACKAGE_RUNTIME_CHMOD_PATHS[@]}" -eq 0 ]]; then
        return
    fi

    for relative_path in "${PACKAGE_RUNTIME_CHMOD_PATHS[@]}"; do
        chmod +x "${staging_dir}/${relative_path}"
    done
}

apply_runtime_remove_patterns() {
    local staging_dir="$1"
    local pattern=""
    local match=""

    if [[ "${#PACKAGE_RUNTIME_REMOVE_PATTERNS[@]}" -eq 0 ]]; then
        return
    fi

    shopt -s nullglob dotglob globstar
    for pattern in "${PACKAGE_RUNTIME_REMOVE_PATTERNS[@]}"; do
        for match in "${staging_dir}"/${pattern}; do
            rm -rf "${match}"
        done
    done
    shopt -u nullglob dotglob globstar
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --python-version)
            shift
            if [[ $# -eq 0 ]]; then
                echo "错误：--python-version 需要一个值" >&2
                exit 2
            fi
            PYTHON_VERSION="$1"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "错误：未知参数 $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

export PACKAGE_PYTHON_VERSION="${PYTHON_VERSION}"

require_command uv
require_command python3
load_project_package_runtime_config

if package_requires_build_env_enabled; then
    require_build_python
fi

validate_required_files "${PROJECT_DIR}" "${PACKAGE_RUNTIME_REQUIRED_PATHS[@]}"

if declare -F run_project_package_before_build >/dev/null 2>&1; then
    run_project_package_before_build "$@"
fi

project_version="$(resolve_project_version "${PROJECT_DIR}")"
project_distribution="$(resolve_project_wheel_distribution)"
release_prefix="$(resolve_release_name_prefix)"

# 应用发布包契约：平台无关的应用薄包，命名不包含目标平台后缀。
release_name="${release_prefix}-${project_version}"

echo "=== 构建应用 wheel ==="
build_wheel_for_project "${PROJECT_DIR}" "${OUTPUT_DIR}"

project_wheel_path="$(find_wheel "${OUTPUT_DIR}" "${project_distribution}-${project_version}-*.whl")"
if [[ -z "${project_wheel_path}" ]]; then
    echo "错误：未在 ${OUTPUT_DIR} 下找到 ${project_distribution}-${project_version}-*.whl" >&2
    exit 1
fi

staging_parent_dir="$(mktemp -d)"
staging_dir="${staging_parent_dir}/${release_name}"
trap 'rm -rf "${staging_parent_dir}"' EXIT
mkdir -p "${staging_dir}/dist"

copy_bundle_files "${PROJECT_DIR}" "${staging_dir}" PACKAGE_RUNTIME_BUNDLE_MAP PACKAGE_RUNTIME_BUNDLE_EXCLUDE_MAP
copy_wheel "${project_wheel_path}" "${staging_dir}/dist"

lockfile_name=""
if [[ "${#PACKAGE_RUNTIME_VARIANT_LOCKFILES[@]}" -gt 0 ]]; then
    echo "=== 导出多 variant 运行时锁定清单 ==="
    for variant_entry in "${PACKAGE_RUNTIME_VARIANT_LOCKFILES[@]}"; do
        variant_name="${variant_entry%%=*}"
        variant_lockfile="${variant_entry#*=}"
        echo "  -> variant=${variant_name} -> ${variant_lockfile}"
        if declare -F run_project_package_export_variant_lockfile >/dev/null 2>&1; then
            run_project_package_export_variant_lockfile "${variant_name}" "${staging_dir}/${variant_lockfile}"
        else
            export_runtime_lockfile "${staging_dir}/${variant_lockfile}"
        fi
    done
else
    echo "=== 导出运行时依赖锁定清单（requirements-runtime.lock，带 hash） ==="
    lockfile_name="requirements-runtime.lock"
    export_runtime_lockfile "${staging_dir}/${lockfile_name}"
fi

apply_runtime_remove_patterns "${staging_dir}"
apply_runtime_chmod_paths "${staging_dir}"
if declare -F run_project_package_post_stage >/dev/null 2>&1; then
    run_project_package_post_stage "${staging_dir}"
fi

echo "=== 生成 runtime-package.toml ==="
generate_runtime_package_toml "${staging_dir}" "${staging_dir}/runtime-package.toml" "$(basename "${PROJECT_DIR}")" "${project_version}" "${lockfile_name}"
echo "=== 校验 runtime-package.toml ==="
validate_runtime_package_toml "${staging_dir}/runtime-package.toml" "${staging_dir}"

sha256_cmd=($(detect_sha256_cmd))
create_checksums "${staging_dir}" "${sha256_cmd[@]}"
release_tar="$(create_release_tar "${staging_parent_dir}" "${release_name}" "${OUTPUT_DIR}")"

echo "=== 构建完成 ==="
echo "  运行包: ${release_tar}"
echo "  应用 wheel: $(basename "${project_wheel_path}")"
echo "  模式: 平台无关应用薄包（正式发布契约）"
