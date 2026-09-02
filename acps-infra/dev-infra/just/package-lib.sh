#!/usr/bin/env bash
# 共享 package scope helper。依赖 doctor-lib.sh 已被 source。

set -euo pipefail

_package_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${_package_lib_dir}/package-build-lib.sh"

package_scope_enabled() {
    [[ "${PACKAGE_SCOPE_ENABLED:-0}" == "1" ]]
}

package_script_path() {
    printf '%s\n' "${PACKAGE_SCRIPT_PATH:-scripts/package-wheel-runtime.sh}"
}

package_required_paths_array() {
    load_project_package_runtime_config >/dev/null
    printf '%s\n' "${PACKAGE_RUNTIME_REQUIRED_PATHS[*]}"
}

package_sibling_repos_array() {
    load_project_package_runtime_config >/dev/null
    printf '%s\n' "${PACKAGE_RUNTIME_SIBLING_REPOS[*]}"
}

package_output_dir() {
    printf '%s\n' "${PACKAGE_OUTPUT_DIR:-dist}"
}

package_build_env_dir() {
    local env_dir="${PACKAGE_BUILD_ENV_DIR:-.package-build-venv}"
    env_dir="${env_dir%/}"
    printf '%s\n' "${env_dir:-.package-build-venv}"
}

package_build_env_python() {
    printf '%s/bin/python\n' "$(package_build_env_dir)"
}

package_build_backend_state_file() {
    printf '%s/.build-backend-state\n' "$(package_build_env_dir)"
}

build_package_check_list() {
    local -a checks=(
        package-toolchain
        package-script
        package-runtime-config
        package-files
        runtime-requirements
        package-sibling-inputs
        package-output
    )

    if package_requires_build_env_enabled; then
        checks=(package-toolchain package-build-backend "${checks[@]:1}")
    fi

    printf '%s\n' "${checks[*]}"
}

read_package_build_system_field() {
    local field="$1"
    local pyproject_path=""

    pyproject_path="$(resolve_package_project_dir)/pyproject.toml"
    if [[ ! -f "${pyproject_path}" ]]; then
        return 1
    fi

    PACKAGE_PYPROJECT_PATH="${pyproject_path}" PACKAGE_BUILD_FIELD="${field}" python3 - <<'PY'
import ast
import os
import re
from pathlib import Path

text = Path(os.environ["PACKAGE_PYPROJECT_PATH"]).read_text(encoding="utf-8")

build_backend = ""
requires: list[str] = []

try:
    import tomllib  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - fallback for older python3
    tomllib = None

if tomllib is not None:
    data = tomllib.loads(text)
    build_system = (data.get("build-system", {}) or {})
    build_backend = str(build_system.get("build-backend", "")).strip()
    requires = list(build_system.get("requires", []) or [])
else:
    in_build_system = False
    collecting_requires = False
    requires_lines: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_build_system = (line == "[build-system]")
            if not in_build_system:
                collecting_requires = False
            continue
        if not in_build_system:
            continue
        if collecting_requires:
            requires_lines.append(line)
            if "]" in line:
                collecting_requires = False
            continue
        if line.startswith("build-backend"):
            match = re.match(r'build-backend\s*=\s*["\']([^"\']+)["\']', line)
            if match:
                build_backend = match.group(1).strip()
        elif line.startswith("requires"):
            after = line.split("=", 1)[1].strip()
            requires_lines = [after]
            if "]" not in after:
                collecting_requires = True

    if requires_lines:
        try:
            requires = list(ast.literal_eval(" ".join(requires_lines)))
        except (SyntaxError, ValueError):
            requires = []

field = os.environ["PACKAGE_BUILD_FIELD"]
if field == "backend-module":
    print(build_backend.split(".", 1)[0] if build_backend else "")
elif field == "requires":
    for item in requires:
        print(item)
else:
    raise SystemExit(f"未知 build-system 字段：{field}")
PY
}

resolve_package_build_backend_module() {
    read_package_build_system_field backend-module
}

resolve_package_build_backend_requirements() {
    read_package_build_system_field requires
}

render_package_build_backend_state() {
    local python_version="${PACKAGE_PYTHON_VERSION:-$(resolve_project_python_version)}"
    local backend_module=""
    local requirement=""

    backend_module="$(resolve_package_build_backend_module)"
    printf 'python=%s\n' "${python_version}"
    printf 'backend=%s\n' "${backend_module}"
    while IFS= read -r requirement; do
        if [[ -n "${requirement}" ]]; then
            printf 'require=%s\n' "${requirement}"
        fi
    done < <(resolve_package_build_backend_requirements)
}

package_build_env_matches_python_version() {
    local env_python="$1"
    local expected_python_version="$2"

    if [[ ! -x "${env_python}" ]]; then
        return 1
    fi

    EXPECTED_PACKAGE_PYTHON_VERSION="${expected_python_version}" \
    "${env_python}" - <<'PY' >/dev/null 2>&1
import os
import sys

expected = tuple(int(part) for part in os.environ["EXPECTED_PACKAGE_PYTHON_VERSION"].split(".")[:2])
current = (sys.version_info.major, sys.version_info.minor)
raise SystemExit(0 if current == expected else 1)
PY
}

check_package_toolchain() {
    local check_id="${1:-package-toolchain}"
    local python_version="${PACKAGE_PYTHON_VERSION:-$(resolve_project_python_version)}"

    if ! command -v uv >/dev/null 2>&1; then
        emit_check_result "${check_id}" blocked error external "未找到 uv。" "安装 uv 后重试。"
        return
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        emit_check_result "${check_id}" blocked error external "未找到 python3，package 脚本无法读取元数据。" "安装 python3 后重试。"
        return
    fi

    if [[ -z "${python_version}" ]]; then
        emit_check_result "${check_id}" invalid error project "PACKAGE_PYTHON_VERSION 未声明。" "在 Justfile 中声明 package_python_version。"
        return
    fi

    emit_check_result "${check_id}" ready info project "package 构建工具链已就绪（target python ${python_version}）。" ""
}

ensure_package_toolchain() {
    local check_id="${1:-package-toolchain}"

    ensure_check_with_fix "${check_id}" _fix_package_toolchain
}

_fix_package_toolchain() {
    local python_version="${PACKAGE_PYTHON_VERSION:-$(resolve_project_python_version)}"
    UV_MANAGED_PYTHON=1 uv python install "${python_version}"
}

check_package_build_backend() {
    local check_id="${1:-package-build-backend}"
    local backend_module=""
    local env_dir=""
    local env_python=""
    local state_file=""
    local expected_state_file=""

    if [[ ! -f pyproject.toml ]]; then
        emit_check_result "${check_id}" invalid error project "缺少 pyproject.toml，无法解析 build backend。" "补齐 pyproject.toml 后重试。"
        return
    fi

    backend_module="$(resolve_package_build_backend_module)"
    if [[ -z "${backend_module}" ]]; then
        emit_check_result "${check_id}" invalid error project "pyproject.toml 未声明 build-system.build-backend。" "在 pyproject.toml 中声明 build-system.requires 与 build-backend。"
        return
    fi

    env_dir="$(package_build_env_dir)"
    env_python="$(package_build_env_python)"
    state_file="$(package_build_backend_state_file)"
    if [[ ! -x "${env_python}" ]]; then
        emit_check_result "${check_id}" missing error project "构建后端专用环境缺失：${env_dir}" "执行 just package bootstrap，按 pyproject.toml 的 build-system.requires 准备构建环境。"
        return
    fi

    if ! "${env_python}" -c "import ${backend_module}" >/dev/null 2>&1; then
        emit_check_result "${check_id}" missing error project "构建后端专用环境缺少 ${backend_module}。" "执行 just package bootstrap，按 pyproject.toml 的 build-system.requires 安装构建后端。"
        return
    fi

    expected_state_file="$(mktemp)"
    render_package_build_backend_state > "${expected_state_file}"
    if [[ ! -f "${state_file}" ]] || ! cmp -s "${expected_state_file}" "${state_file}"; then
        rm -f "${expected_state_file}"
        emit_check_result "${check_id}" stale error project "构建后端专用环境 ${env_dir} 与当前 build-system.requires 不一致。" "执行 just package bootstrap，刷新 package 构建环境。"
        return
    fi

    rm -f "${expected_state_file}"
    emit_check_result "${check_id}" ready info project "构建后端 ${backend_module} 已就绪（${env_dir}）。" ""
}

ensure_package_build_backend() {
    local check_id="${1:-package-build-backend}"

    ensure_check_with_fix "${check_id}" _fix_package_build_backend
}

_fix_package_build_backend() {
    local env_dir=""
    local env_python=""
    local state_file=""
    local requirements_file=""
    local expected_state_file=""
    local python_version="${PACKAGE_PYTHON_VERSION:-$(resolve_project_python_version)}"

    env_dir="$(package_build_env_dir)"
    env_python="$(package_build_env_python)"
    state_file="$(package_build_backend_state_file)"

    requirements_file="$(mktemp)"
    expected_state_file="$(mktemp)"
    resolve_package_build_backend_requirements > "${requirements_file}"
    render_package_build_backend_state > "${expected_state_file}"

    if [[ ! -s "${requirements_file}" ]]; then
        rm -f "${requirements_file}" "${expected_state_file}"
        echo "[ERROR] build-system.requires 为空，无法安装构建后端。" >&2
        return 1
    fi

    UV_MANAGED_PYTHON=1 run_uv_with_mutating_cache uv python install "${python_version}"
    if ! package_build_env_matches_python_version "${env_python}" "${python_version}"; then
        rm -rf "${env_dir}"
        UV_MANAGED_PYTHON=1 run_uv_with_mutating_cache uv venv --python "${python_version}" "${env_dir}"
    fi

    run_uv_with_mutating_cache uv pip install --python "${env_python}" -r "${requirements_file}"
    cp "${expected_state_file}" "${state_file}"
    rm -f "${requirements_file}" "${expected_state_file}"
}

check_package_script() {
    local check_id="${1:-package-script}"
    local script_path=""

    script_path="$(package_script_path)"
    if [[ -f "${script_path}" ]]; then
        emit_check_result "${check_id}" ready info project "package 脚本已存在：${script_path}" ""
        return
    fi

    emit_check_result "${check_id}" missing error project "缺少 package 脚本：${script_path}" "补齐脚本，或移除 package 入口。"
}

ensure_package_script() {
    local check_id="${1:-package-script}"
    ensure_check_with_fix "${check_id}" ""
}

package_uses_shared_builder() {
    case "$(package_script_path)" in
        ../acps-infra/dev-infra/just/package-wheel-runtime.sh|*/acps-infra/dev-infra/just/package-wheel-runtime.sh|acps-infra/dev-infra/just/package-wheel-runtime.sh)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

check_package_runtime_config() {
    local check_id="${1:-package-runtime-config}"
    local entry=""
    local src_path=""
    local dest_path=""

    if ! load_project_package_runtime_config; then
        emit_check_result "${check_id}" invalid error project "无法加载 package runtime 配置。" "检查 project_just_hook_lib 与 configure_project_package_runtime。"
        return
    fi

    if [[ "${#PACKAGE_RUNTIME_BUNDLE_MAP[@]}" -eq 0 ]]; then
        if package_uses_shared_builder; then
            emit_check_result "${check_id}" invalid error project "共享 package builder 缺少 runtime bundle map。" "在项目 hook 的 configure_project_package_runtime 中声明 PACKAGE_RUNTIME_BUNDLE_MAP。"
        else
            emit_check_result "${check_id}" ready warn project "legacy package 脚本路径已保留；runtime config 已可读，bundle map 严格校验将在切换共享 builder 后启用。" ""
        fi
        return
    fi

    for entry in "${PACKAGE_RUNTIME_BUNDLE_MAP[@]}"; do
        src_path="${entry%%|*}"
        dest_path="${entry#*|}"
        if [[ -z "${src_path}" || -z "${dest_path}" || "${src_path}" == /* || "${dest_path}" == /* ]]; then
            emit_check_result "${check_id}" invalid error project "package runtime bundle map 非法：${entry}" "使用相对项目根的 src|dest 形式声明 bundle map。"
            return
        fi
    done

    emit_check_result "${check_id}" ready info project "package runtime 配置可读且可解析。" ""
}

ensure_package_runtime_config() {
    local check_id="${1:-package-runtime-config}"
    ensure_check_with_fix "${check_id}" ""
}

check_package_files() {
    local check_id="${1:-package-files}"
    local path=""
    local missing_paths=()

    if ! load_project_package_runtime_config; then
        emit_check_result "${check_id}" invalid error project "无法加载 package runtime 配置。" "检查 project_just_hook_lib 与 configure_project_package_runtime。"
        return
    fi

    for path in "${PACKAGE_RUNTIME_REQUIRED_PATHS[@]}"; do
        if [[ ! -e "${path}" ]]; then
            missing_paths+=("${path}")
        fi
    done

    if [[ "${#missing_paths[@]}" -eq 0 ]]; then
        emit_check_result "${check_id}" ready info project "package 必需文件已就绪。" ""
        return
    fi

    emit_check_result "${check_id}" missing error project "package 必需文件缺失：${missing_paths[*]}" "补齐缺失文件后重试。"
}

ensure_package_files() {
    local check_id="${1:-package-files}"
    ensure_check_with_fix "${check_id}" ""
}

check_runtime_requirements() {
    local check_id="${1:-runtime-requirements}"
    local tmpfile=""
    local errfile=""
    local cache_dir=""
    local failure_output=""

    if ! command -v uv >/dev/null 2>&1; then
        emit_check_result "${check_id}" blocked error external "未找到 uv，无法导出 runtime requirements。" "安装 uv 后重试。"
        return
    fi

    tmpfile="$(mktemp)"
    errfile="$(mktemp)"
    cache_dir="$(mktemp -d)"
    if UV_CACHE_DIR="${cache_dir}" export_runtime_requirements "${tmpfile}" 2>"${errfile}"; then
        rm -f "${tmpfile}"
        rm -f "${errfile}"
        rm -rf "${cache_dir}"
        emit_check_result "${check_id}" ready info project "runtime requirements 可导出。" ""
        return
    fi

    failure_output="$(cat "${errfile}" 2>/dev/null || true)"
    rm -f "${tmpfile}"
    rm -f "${errfile}"
    rm -rf "${cache_dir}"
    if [[ "${failure_output}" == *"Failed to fetch"* ]] || [[ "${failure_output}" == *"dns error"* ]] || [[ "${failure_output}" == *"client error (Connect)"* ]]; then
        emit_check_result "${check_id}" blocked error external "runtime requirements 导出失败，当前环境可能缺少包源网络访问。" "检查包源可达性、私有 index 认证和网络环境。"
        return
    fi

    emit_check_result "${check_id}" blocked error external "runtime requirements 导出失败。" "检查 lockfile、路径依赖和 pyproject 配置。"
}

ensure_runtime_requirements() {
    local check_id="${1:-runtime-requirements}"
    ensure_check_with_fix "${check_id}" ""
}

check_package_sibling_inputs() {
    local check_id="${1:-package-sibling-inputs}"
    local project_dir=""
    local sibling_root=""
    local repo_name=""
    local repo_path=""
    local missing_repos=()
    local expected_paths=()

    if ! load_project_package_runtime_config; then
        emit_check_result "${check_id}" invalid error project "无法加载 package runtime 配置。" "检查 project_just_hook_lib 与 configure_project_package_runtime。"
        return
    fi

    project_dir="$(resolve_package_project_dir)"
    sibling_root="$(dirname "${project_dir}")"

    if [[ "${#PACKAGE_RUNTIME_SIBLING_REPOS[@]}" -gt 0 ]]; then
        for repo_name in "${PACKAGE_RUNTIME_SIBLING_REPOS[@]}"; do
            repo_path="${sibling_root}/${repo_name}"
            if [[ ! -d "${repo_path}" ]] || [[ ! -f "${repo_path}/pyproject.toml" ]]; then
                missing_repos+=("${repo_name}")
                expected_paths+=("${repo_path}")
            fi
        done
    fi

    if [[ "${#missing_repos[@]}" -eq 0 ]]; then
        emit_check_result "${check_id}" ready info sibling "package sibling 输入已满足。" ""
        return
    fi

    emit_check_result "${check_id}" blocked error sibling "缺少 package sibling 输入：${missing_repos[*]}（期望路径：${expected_paths[*]}）" "将 sibling repo 放到与当前项目同级的父目录下后重试；共享层不会自动 clone。"
}

ensure_package_sibling_inputs() {
    local check_id="${1:-package-sibling-inputs}"
    ensure_check_with_fix "${check_id}" ""
}

check_package_output() {
    local check_id="${1:-package-output}"
    local output_dir=""
    local parent_dir=""

    output_dir="$(package_output_dir)"
    parent_dir="$(dirname "${output_dir}")"

    if [[ -d "${output_dir}" ]]; then
        if [[ -w "${output_dir}" ]]; then
            emit_check_result "${check_id}" ready info project "package 输出目录可写：${output_dir}" ""
        else
            emit_check_result "${check_id}" invalid error project "package 输出目录不可写：${output_dir}" "修复目录权限。"
        fi
        return
    fi

    if [[ -d "${parent_dir}" ]] && [[ -w "${parent_dir}" ]]; then
        emit_check_result "${check_id}" missing error project "package 输出目录缺失：${output_dir}" "执行 just package bootstrap。"
        return
    fi

    emit_check_result "${check_id}" invalid error project "package 输出目录父目录不可写：${parent_dir}" "修复目录权限。"
}

ensure_package_output() {
    local check_id="${1:-package-output}"
    ensure_check_with_fix "${check_id}" _fix_package_output
}

_fix_package_output() {
    mkdir -p "$(package_output_dir)"
}

run_package_doctor() {
    local checks="$1"

    if ! package_scope_enabled; then
        echo "[ERROR] 当前项目未启用 package scope。" >&2
        echo "         remediation: 补齐 package 脚本与声明，或移除 package 入口。" >&2
        return 1
    fi

    run_scope_doctor package "${checks}"
}

run_package_bootstrap() {
    local checks="$1"

    if ! package_scope_enabled; then
        echo "[ERROR] 当前项目未启用 package scope。" >&2
        echo "         remediation: 补齐 package 脚本与声明，或移除 package 入口。" >&2
        return 1
    fi

    run_scope_bootstrap package "${checks}"
}
