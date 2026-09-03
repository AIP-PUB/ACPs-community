#!/usr/bin/env bash

set -euo pipefail

_package_build_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── thin-package 文件系统助手（原 scripts/lib/build.sh，仅保留 package 路径仍用到的部分）──

DEFAULT_BUNDLE_EXCLUDE_MAP=(
    "__pycache__"
    "*/__pycache__"
    "*.pyc"
    "*.pyo"
    ".pytest_cache"
    "*/.pytest_cache"
    ".mypy_cache"
    "*/.mypy_cache"
    ".ruff_cache"
    "*/.ruff_cache"
    ".DS_Store"
)

validate_required_files() {
    local project_dir="$1"
    shift
    local missing=0
    local file_path

    if [[ $# -eq 0 ]]; then
        echo "错误：validate_required_files 至少需要一个待检查路径" >&2
        return 1
    fi

    echo "=== 检查文件完整性 ==="
    for file_path in "$@"; do
        if [[ ! -e "${project_dir}/${file_path}" ]]; then
            echo "  缺失: ${file_path}" >&2
            missing=$((missing + 1))
        fi
    done

    if [[ $missing -gt 0 ]]; then
        echo "错误：缺少 ${missing} 个必要文件" >&2
        return 1
    fi

    echo "  所有必要文件检查通过"
}

detect_sha256_cmd() {
    if command -v sha256sum &>/dev/null; then
        printf '%s\n' sha256sum
    elif command -v shasum &>/dev/null; then
        printf '%s\n' shasum -a 256
    else
        echo "错误：未找到 sha256sum 或 shasum 命令，无法生成校验文件" >&2
        return 1
    fi
}

copy_bundle_files() {
    local project_dir="$1"
    local staging_dir="$2"
    local bundle_map_name="${3:-}"
    local exclude_map_name="${4:-}"
    local entry
    local src_path
    local dest_path
    local _entry_kind
    local src
    local dest
    local pattern
    local exclude_args=()
    local bundle_map=()
    local exclude_map=()

    if [[ -z "$bundle_map_name" ]]; then
        echo "错误：copy_bundle_files 需要提供打包映射数组名" >&2
        return 1
    fi

    eval "bundle_map=(\"\${${bundle_map_name}[@]}\")"
    if [[ ${#bundle_map[@]} -eq 0 ]]; then
        echo "错误：copy_bundle_files 至少需要一个打包映射" >&2
        return 1
    fi

    if [[ -n "$exclude_map_name" ]]; then
        eval "exclude_map=(\"\${${exclude_map_name}[@]}\")"
    fi

    for pattern in "${exclude_map[@]}"; do
        exclude_args+=("--exclude=${pattern}")
    done

    for entry in "${bundle_map[@]}"; do
        # bundle map 条目支持两段式 "src|dest" 或三段式 "src|dest|kind"；
        # 第三段（kind）只供 runtime-package.toml 的 [[assets]] 生成器使用，
        # 拷贝阶段忽略它，只取前两段。
        IFS='|' read -r src_path dest_path _entry_kind <<< "$entry"
        src="${project_dir}/${src_path}"
        dest="${staging_dir}/${dest_path}"
        mkdir -p "$(dirname "$dest")"
        if [[ -d "$src" ]]; then
            mkdir -p "$dest"
            tar "${exclude_args[@]}" -cf - -C "$src" . | tar -xf - -C "$dest"
        else
            cp "$src" "$dest"
        fi
    done
}

generate_checksums() {
    local staging_dir="$1"
    shift

    if [[ $# -eq 0 ]]; then
        echo "错误：generate_checksums 需要显式传入校验命令" >&2
        return 1
    fi

    (
        cd "$staging_dir" || exit 1
        # 注意：用 ! -path './checksums.txt' 而不是 ! -name 'checksums.txt'——按
        # basename 过滤会把任何嵌套的同名 checksums.txt（例如某些 bundle 资源里
        # 恰好也带一份 checksums.txt）一并排除掉，导致顶层 checksums.txt 覆盖不完整
        # （同一类问题已在 assemble.sh 和 collect-app-release-kit.sh 中发现并修复）。
        find . -type f ! -path './checksums.txt' -print0 \
            | LC_ALL=C sort -z \
            | xargs -0 "$@" > checksums.txt
    )
}

create_release_tar() {
    local staging_parent_dir="$1"
    local release_name="$2"
    local output_dir="$3"
    local release_tar
    local tar_args=()

    release_tar="${output_dir}/${release_name}.tar.gz"
    # macOS 自带 bsdtar 会把 Apple 扩展属性写进归档；显式关闭，
    # 避免目标 Linux 解包时出现 LIBARCHIVE.xattr.com.apple.* 警告。
    if [[ "$(uname -s)" == "Darwin" ]]; then
        tar_args+=(--no-mac-metadata --no-xattrs --no-acls)
    fi

    tar_args+=(-czf "$release_tar" -C "$staging_parent_dir" "$release_name")
    tar "${tar_args[@]}"
    printf '%s\n' "$release_tar"
}

resolve_package_project_dir() {
    local project_dir="${PACKAGE_PROJECT_DIR:-${PWD}}"

    if [[ ! -d "${project_dir}" ]]; then
        echo "[ERROR] package 项目目录不存在：${project_dir}" >&2
        return 1
    fi

    (
        cd "${project_dir}"
        pwd
    )
}

resolve_project_hook_lib_path() {
    local hook_path="${PROJECT_JUST_HOOK_LIB:-}"
    local project_dir=""

    if [[ -z "${hook_path}" ]]; then
        return 0
    fi

    project_dir="$(resolve_package_project_dir)"
    if [[ "${hook_path}" == /* ]]; then
        printf '%s\n' "${hook_path}"
        return 0
    fi

    printf '%s/%s\n' "${project_dir}" "${hook_path}"
}

package_requires_build_env_enabled() {
    if [[ -n "${PACKAGE_REQUIRES_BUILD_ENV:-}" ]]; then
        [[ "${PACKAGE_REQUIRES_BUILD_ENV}" == "1" ]]
        return
    fi

    [[ "${PACKAGE_BUILD_MODE:-}" == "hatchling-build-env" ]]
}

resolve_wheel_distribution_from_pyproject() {
    local project_dir="${1:-}"
    local pyproject_path=""

    if [[ -z "${project_dir}" ]]; then
        project_dir="$(resolve_package_project_dir)"
    fi

    pyproject_path="${project_dir}/pyproject.toml"
    if [[ ! -f "${pyproject_path}" ]]; then
        echo "[ERROR] 未找到 pyproject.toml：${pyproject_path}" >&2
        return 1
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

name = ""
if tomllib is not None:
    data = tomllib.loads(text)
    name = str((data.get("project", {}) or {}).get("name", "")).strip()
else:
    in_project = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project = (line == "[project]")
            continue
        if in_project and line.startswith("name"):
            match = re.match(r'name\s*=\s*["\']([^"\']+)["\']', line)
            if match:
                name = match.group(1).strip()
                break

if not name:
    raise SystemExit("pyproject.toml 缺少 project.name")
print(re.sub(r"[-_.]+", "_", name))
PY
}

export_runtime_requirements() {
    local output_file="$1"
    local project_dir=""
    local temp_file=""

    project_dir="$(resolve_package_project_dir)"
    temp_file="$(mktemp)"

    (
        cd "${project_dir}"
        if declare -F run_uv_with_mutating_cache >/dev/null 2>&1; then
            run_uv_with_mutating_cache uv export --locked --format requirements-txt --no-dev --no-emit-project --no-hashes > "${temp_file}"
        else
            uv export --locked --format requirements-txt --no-dev --no-emit-project --no-hashes > "${temp_file}"
        fi
    )

    if declare -F run_project_package_filter_requirements >/dev/null 2>&1; then
        run_project_package_filter_requirements "${temp_file}" "${output_file}"
    else
        cp "${temp_file}" "${output_file}"
    fi

    rm -f "${temp_file}"
}

# 应用发布包方案的 hash 锁定导出路径。
#
# 与 export_runtime_requirements 的关键差异：
#   - 不传 --no-hashes，锁定输出携带完整 hash（uv export 默认行为）。
#   - 传 --no-emit-local，由 uv 自己剔除本地路径/可编辑依赖（例如 ../acps-sdk），
#     不再依赖项目侧的 run_project_package_filter_requirements 二次过滤。
#   - 使用 --locked 而不是 --frozen：uv.lock 与 pyproject.toml 不一致时直接报错退出，
#     适合发布流程这种需要快失败的场景。
#
# export_runtime_requirements（无 hash）仍供 package check 的 runtime-requirements
# 预检使用；正式薄包发布路径走 export_runtime_lockfile。
export_runtime_lockfile() {
    local output_file="$1"
    local project_dir=""

    project_dir="$(resolve_package_project_dir)"

    (
        cd "${project_dir}"
        if declare -F run_uv_with_mutating_cache >/dev/null 2>&1; then
            run_uv_with_mutating_cache uv export --locked --format requirements-txt --no-dev --no-emit-project --no-emit-local > "${output_file}"
        else
            uv export --locked --format requirements-txt --no-dev --no-emit-project --no-emit-local > "${output_file}"
        fi
    )
}

# 定位共享的 runtime-package.toml 生成/校验工具（权威路径：release/lib/runtime_package.py）。
# 该工具同时供应用侧薄包生成（本文件）和 release/app-packaging 采集/装配/发布包校验复用，
# 避免 shell 和 Python 出现两套隐式 schema 定义。
resolve_runtime_package_tool_path() {
    local lib_dir=""

    lib_dir="$(cd "${_package_build_lib_dir}/../../release/lib" && pwd)"
    printf '%s/runtime_package.py\n' "${lib_dir}"
}

# 生成应用发布包 schema 的 runtime-package.toml。
#
# 依赖调用方已经加载好 load_project_package_runtime_config，并且以下数组按需声明：
#   PACKAGE_RUNTIME_COMPONENTS         "id|type|entrypoint|ports|health_check|smoke_test|config_templates"
#   PACKAGE_RUNTIME_INTERNAL_WHEELS    内部依赖项目 id 列表（对应 release/app-packaging/projects.toml 的 id）
#   PACKAGE_RUNTIME_EXTERNAL_COMPONENTS 外部依赖组件名（如 postgresql），可选
#   PACKAGE_RUNTIME_VARIANT_LOCKFILES  "variant=文件名"，声明后 lockfile_name 参数将被忽略
#
# asset_root 必须是已经完成拷贝、post-stage 清理之后的最终 staging 目录。
generate_runtime_package_toml() {
    local staging_dir="$1"
    local output_path="$2"
    local project_name="$3"
    local project_version="$4"
    local lockfile_name="${5:-}"
    local runtime_package_tool=""
    local build_id=""
    local entry
    local -a cmd_args=()

    runtime_package_tool="$(resolve_runtime_package_tool_path)"
    build_id="$(date -u +%Y%m%dT%H%M%SZ)"

    cmd_args=(
        generate
        --name "${project_name}"
        --version "${project_version}"
        --build-id "${build_id}"
        --dist-dir "dist/"
        --checksums "checksums.txt"
        --asset-root "${staging_dir}"
        --output "${output_path}"
    )

    if [[ "${#PACKAGE_RUNTIME_VARIANT_LOCKFILES[@]}" -gt 0 ]]; then
        for entry in "${PACKAGE_RUNTIME_VARIANT_LOCKFILES[@]}"; do
            cmd_args+=(--variant-lockfile "${entry}")
        done
    else
        if [[ -z "${lockfile_name}" ]]; then
            echo "[ERROR] generate_runtime_package_toml：未声明 PACKAGE_RUNTIME_VARIANT_LOCKFILES 时必须提供 lockfile_name 参数" >&2
            return 1
        fi
        cmd_args+=(--lockfile "${lockfile_name}")
    fi

    if [[ "${#PACKAGE_RUNTIME_COMPONENTS[@]}" -gt 0 ]]; then
        for entry in "${PACKAGE_RUNTIME_COMPONENTS[@]}"; do
            cmd_args+=(--component "${entry}")
        done
    fi

    if [[ "${#PACKAGE_RUNTIME_INTERNAL_WHEELS[@]}" -gt 0 ]]; then
        for entry in "${PACKAGE_RUNTIME_INTERNAL_WHEELS[@]}"; do
            cmd_args+=(--internal-wheel "${entry}")
        done
    fi

    if [[ "${#PACKAGE_RUNTIME_EXTERNAL_COMPONENTS[@]}" -gt 0 ]]; then
        for entry in "${PACKAGE_RUNTIME_EXTERNAL_COMPONENTS[@]}"; do
            cmd_args+=(--external-component "${entry}")
        done
    fi

    if [[ "${#PACKAGE_RUNTIME_BUNDLE_MAP[@]}" -gt 0 ]]; then
        for entry in "${PACKAGE_RUNTIME_BUNDLE_MAP[@]}"; do
            cmd_args+=(--bundle-map "${entry}")
        done
    fi

    if [[ "${#PACKAGE_RUNTIME_REQUIRED_PATHS[@]}" -gt 0 ]]; then
        for entry in "${PACKAGE_RUNTIME_REQUIRED_PATHS[@]}"; do
            cmd_args+=(--required-path "${entry}")
        done
    fi

    if [[ "${#PACKAGE_RUNTIME_CHMOD_PATHS[@]}" -gt 0 ]]; then
        for entry in "${PACKAGE_RUNTIME_CHMOD_PATHS[@]}"; do
            cmd_args+=(--chmod-path "${entry}")
        done
    fi

    python3 "${runtime_package_tool}" "${cmd_args[@]}"
}

# 校验薄包（或发布包 app/ 目录）内的 runtime-package.toml 是否符合新 schema，
# 并确认 [[assets]] 声明的路径在 asset_root 下真实存在。
validate_runtime_package_toml() {
    local path="$1"
    local asset_root="$2"
    local runtime_package_tool=""

    runtime_package_tool="$(resolve_runtime_package_tool_path)"
    python3 "${runtime_package_tool}" validate --path "${path}" --asset-root "${asset_root}"
}

find_wheel() {
    local search_dir="$1"
    local pattern="$2"

    find "${search_dir}" -maxdepth 1 -type f -name "${pattern}" | LC_ALL=C sort | tail -n 1
}

copy_wheel() {
    local wheel_path="$1"
    local destination_dir="$2"

    mkdir -p "${destination_dir}"
    cp "${wheel_path}" "${destination_dir}/"
}

create_checksums() {
    generate_checksums "$@"
}

load_project_package_runtime_config() {
    local project_dir=""
    local hook_path=""

    if [[ "${PACKAGE_RUNTIME_CONFIG_LOADED:-0}" == "1" ]]; then
        return 0
    fi

    project_dir="$(resolve_package_project_dir)"
    hook_path="$(resolve_project_hook_lib_path || true)"

    PACKAGE_RUNTIME_REQUIRED_PATHS=()
    if [[ -n "${PACKAGE_REQUIRED_PATHS:-}" ]]; then
        read -r -a PACKAGE_RUNTIME_REQUIRED_PATHS <<< "${PACKAGE_REQUIRED_PATHS}"
    fi

    PACKAGE_RUNTIME_SIBLING_REPOS=()
    if [[ -n "${PACKAGE_SIBLING_REPOS:-}" ]]; then
        read -r -a PACKAGE_RUNTIME_SIBLING_REPOS <<< "${PACKAGE_SIBLING_REPOS}"
    fi
    PACKAGE_RUNTIME_BUNDLE_MAP=()
    PACKAGE_RUNTIME_BUNDLE_EXCLUDE_MAP=(
        "${DEFAULT_BUNDLE_EXCLUDE_MAP[@]}"
    )
    PACKAGE_RUNTIME_CHMOD_PATHS=()
    PACKAGE_RUNTIME_REMOVE_PATTERNS=()
    # 应用发布包 runtime-package.toml 生成所需的元数据；
    # 项目 hook 里的 configure_project_package_runtime 可按需覆盖。
    PACKAGE_RUNTIME_COMPONENTS=()
    PACKAGE_RUNTIME_INTERNAL_WHEELS=()
    PACKAGE_RUNTIME_EXTERNAL_COMPONENTS=()
    PACKAGE_RUNTIME_VARIANT_LOCKFILES=()

    if [[ -n "${hook_path}" ]]; then
        if [[ ! -f "${hook_path}" ]]; then
            echo "[ERROR] 项目 hook 文件不存在：${hook_path}" >&2
            return 1
        fi

        # shellcheck source=/dev/null
        source "${hook_path}"
    fi

    if declare -F configure_project_package_runtime >/dev/null 2>&1; then
        configure_project_package_runtime
    fi

    PACKAGE_RUNTIME_PROJECT_DIR="${project_dir}"
    PACKAGE_RUNTIME_HOOK_PATH="${hook_path}"
    PACKAGE_RUNTIME_CONFIG_LOADED=1
}
