#!/usr/bin/env bash
# 共享 dev-infra check/bootstrap 辅助函数。
#
# 兼容旧式 helper：
#   check_python3_toolchain
#   check_uv_sync_locked
#   check_just_available
#   check_dotenv_file
#   check_dev_infra_tooling
#   check_dev_infra_service_health
#
# 新式协议：
#   emit_check_result / run_scope_doctor / run_scope_bootstrap

set -euo pipefail

_dev_infra_doctor_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_check_results_file=""

resolve_dev_infra_script() {
    if [[ -n "${dev_infra_script:-}" ]]; then
        printf '%s\n' "${dev_infra_script}"
        return
    fi

    printf '%s\n' "${_dev_infra_doctor_lib_dir}/../dev-infra.sh"
}

resolve_project_python_version() {
    if [[ -n "${PROJECT_PYTHON_VERSION:-}" ]]; then
        printf '%s\n' "${PROJECT_PYTHON_VERSION}"
        return
    fi

    if [[ -f .python-version ]]; then
        head -n 1 .python-version
        return
    fi

    printf '3.14\n'
}

resolve_mutating_uv_cache_dir() {
    local cache_dir="${TMPDIR:-/tmp}/acps-uv-cache"
    printf '%s\n' "${cache_dir}"
}

resolve_mutating_uv_python_install_dir() {
    local python_dir="${TMPDIR:-/tmp}/acps-uv-python"
    printf '%s\n' "${python_dir}"
}

run_uv_with_mutating_cache() {
    local cache_dir=""
    local python_dir=""
    cache_dir="$(resolve_mutating_uv_cache_dir)"
    python_dir="$(resolve_mutating_uv_python_install_dir)"
    mkdir -p "${cache_dir}" "${python_dir}"
    UV_CACHE_DIR="${cache_dir}" UV_PYTHON_INSTALL_DIR="${python_dir}" "$@"
}

print_infra_help_lines() {
    cat <<'EOF'
  status [service...] [--format=tsv]   查看共享依赖状态
  up [service...]                      启动共享依赖（不传参数时默认 postgres）
  down                                 停止全部共享依赖
  wait [service...]                    等待共享依赖就绪
  logs [service...] [--tail N] [--since DURATION] [--follow]
                                      查看共享依赖日志
  reset [service...] [--volumes] [--yes] 修复性重建共享依赖
  check                                检查 dev-infra 工具链与 compose 定义
EOF
}

load_dotenv() {
    [[ -f .env ]] || return

    # `.env` supplies defaults only.  Test and CI invocations rely on exported
    # variables (for example TEST_DATABASE_URL) to select an isolated runtime,
    # so never let a repository-local `.env` silently replace those values.
    local raw_line=""
    local dotenv_key=""
    local index=""
    local -a existing_keys=()
    local -a existing_values=()

    while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
        if [[ "${raw_line}" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)= ]]; then
            dotenv_key="${BASH_REMATCH[2]}"
            if [[ -n "${!dotenv_key+x}" ]]; then
                existing_keys+=("${dotenv_key}")
                existing_values+=("${!dotenv_key}")
            fi
        fi
    done < .env

    set -a
    # shellcheck disable=SC1091
    source .env
    set +a

    for index in "${!existing_keys[@]}"; do
        dotenv_key="${existing_keys[${index}]}"
        printf -v "${dotenv_key}" '%s' "${existing_values[${index}]}"
        export "${dotenv_key}"
    done
}

normalize_check_id() {
    printf '%s\n' "$1" | tr '.:-' '___'
}

sanitize_tsv_field() {
    local value="${1//$'\t'/ }"
    value="${value//$'\n'/ }"
    printf '%s' "${value}"
}

validate_check_enum() {
    local kind="$1"
    local value="$2"

    case "${kind}:${value}" in
        status:ready|status:missing|status:stale|status:invalid|status:blocked)
            ;;
        severity:info|severity:warn|severity:error)
            ;;
        owner:project|owner:shared-infra|owner:sibling|owner:external|owner:user-config|owner:invalid)
            ;;
        *)
            echo "[ERROR] 非法 ${kind} 枚举值：${value}" >&2
            exit 2
            ;;
    esac
}

emit_check_result() {
    local check_id="$1"
    local status="$2"
    local severity="$3"
    local owner="$4"
    local message
    local remediation

    if [[ ! "${check_id}" =~ ^[a-z0-9._:-]+$ ]]; then
        echo "[ERROR] 非法 check_id：${check_id}" >&2
        exit 2
    fi

    validate_check_enum status "${status}"
    validate_check_enum severity "${severity}"
    validate_check_enum owner "${owner}"

    message="$(sanitize_tsv_field "${5:-}")"
    remediation="$(sanitize_tsv_field "${6:-}")"

    if [[ -z "${_check_results_file}" ]]; then
        echo "[ERROR] emit_check_result 缺少结果收集器。" >&2
        exit 2
    fi

    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "${check_id}" \
        "${status}" \
        "${severity}" \
        "${owner}" \
        "${message}" \
        "${remediation}" >> "${_check_results_file}"
}

results_file_has_errors() {
    local results_file="$1"
    local _id=""
    local status=""
    local severity=""
    local _owner=""
    local _message=""
    local _remediation=""

    while IFS=$'\t' read -r _id status severity _owner _message _remediation; do
        if [[ "${severity}" == "error" ]] && [[ "${status}" != "ready" ]]; then
            return 0
        fi
    done < "${results_file}"

    return 1
}

results_file_only_fixable_errors() {
    local results_file="$1"
    local _id=""
    local status=""
    local severity=""
    local owner=""
    local _message=""
    local _remediation=""

    while IFS=$'\t' read -r _id status severity owner _message _remediation; do
        if [[ "${severity}" != "error" ]] || [[ "${status}" == "ready" ]]; then
            continue
        fi

        case "${owner}" in
            project|shared-infra|sibling)
                ;;
            *)
                return 1
                ;;
        esac
    done < "${results_file}"

    return 0
}

results_file_is_ready() {
    local results_file="$1"
    local _id=""
    local status=""
    local _severity=""
    local _owner=""
    local _message=""
    local _remediation=""

    while IFS=$'\t' read -r _id status _severity _owner _message _remediation; do
        if [[ "${status}" != "ready" ]]; then
            return 1
        fi
    done < "${results_file}"

    return 0
}

render_result_label() {
    local mode="$1"
    local status="$2"
    local severity="$3"

    if [[ "${mode}" == "bootstrap" ]] && [[ "${status}" == "ready" ]]; then
        printf '[SKIP]'
        return
    fi

    if [[ "${status}" == "ready" ]]; then
        printf '[OK]'
        return
    fi

    case "${severity}" in
        info)
            printf '[INFO]'
            ;;
        warn)
            printf '[WARN]'
            ;;
        error)
            printf '[ERROR]'
            ;;
        *)
            printf '[ERROR]'
            ;;
    esac
}

render_results_file() {
    local mode="$1"
    local results_file="$2"
    local check_id=""
    local status=""
    local severity=""
    local _owner=""
    local message=""
    local remediation=""

    while IFS=$'\t' read -r check_id status severity _owner message remediation; do
        local label=""
        label="$(render_result_label "${mode}" "${status}" "${severity}")"
        printf '%-8s %s\n' "${label}" "${message}"
        if [[ "${status}" != "ready" ]] && [[ -n "${remediation}" ]]; then
            printf '         remediation: %s\n' "${remediation}"
        fi
    done < "${results_file}"
}

default_run_project_check() {
    return 127
}

default_run_project_ensure() {
    return 127
}

dispatch_check() {
    local check_id="$1"
    local normalized=""

    normalized="$(normalize_check_id "${check_id}")"
    if declare -F "check_${normalized}" >/dev/null 2>&1; then
        "check_${normalized}" "${check_id}"
        return
    fi

    if [[ "${check_id}" == infra-* ]]; then
        check_infra_service_ready "${check_id#infra-}" "${check_id}"
        return
    fi

    if declare -F run_project_check >/dev/null 2>&1; then
        if run_project_check "${check_id}"; then
            return
        fi
    fi

    if declare -F default_run_project_check >/dev/null 2>&1; then
        if default_run_project_check "${check_id}"; then
            return
        fi
    fi

    echo "[ERROR] 未知 check id：${check_id}" >&2
    exit 2
}

dispatch_ensure() {
    local check_id="$1"
    local normalized=""

    normalized="$(normalize_check_id "${check_id}")"
    if declare -F "ensure_${normalized}" >/dev/null 2>&1; then
        "ensure_${normalized}" "${check_id}"
        return
    fi

    if [[ "${check_id}" == infra-* ]]; then
        ensure_infra_service_ready "${check_id#infra-}" "${check_id}"
        return
    fi

    if declare -F run_project_ensure >/dev/null 2>&1; then
        if run_project_ensure "${check_id}"; then
            return
        fi
    fi

    if declare -F default_run_project_ensure >/dev/null 2>&1; then
        if default_run_project_ensure "${check_id}"; then
            return
        fi
    fi

    echo "[ERROR] 未知 ensure id：${check_id}" >&2
    exit 2
}

run_check_capture() {
    local check_id="$1"
    local results_file="$2"

    : > "${results_file}"
    _check_results_file="${results_file}"
    dispatch_check "${check_id}"
    if [[ ! -s "${results_file}" ]]; then
        echo "[ERROR] check ${check_id} 未产生结果。" >&2
        exit 2
    fi
}

run_scope_doctor() {
    local scope="$1"
    local checks_string="$2"
    local check_id=""
    local results_file=""
    local exit_code=0

    IFS=' ' read -r -a _scope_checks <<< "${checks_string}"

    if [[ "${#_scope_checks[@]}" -eq 0 ]]; then
        echo "[ERROR] scope ${scope} 未声明任何检查项。" >&2
        exit 2
    fi

    results_file="$(mktemp)"
    trap 'rm -f "${results_file:-}"' RETURN

    for check_id in "${_scope_checks[@]}"; do
        run_check_capture "${check_id}" "${results_file}"
        render_results_file doctor "${results_file}"
        if results_file_has_errors "${results_file}"; then
            exit_code=1
        fi
    done

    return "${exit_code}"
}

run_scope_bootstrap() {
    local scope="$1"
    local checks_string="$2"
    local check_id=""

    IFS=' ' read -r -a _scope_checks <<< "${checks_string}"

    if [[ "${#_scope_checks[@]}" -eq 0 ]]; then
        echo "[ERROR] scope ${scope} 未声明任何 bootstrap 检查项。" >&2
        exit 2
    fi

    for check_id in "${_scope_checks[@]}"; do
        dispatch_ensure "${check_id}"
    done
}

check_python_toolchain() {
    local check_id="${1:-python-toolchain}"
    local requires_python=""
    local version_output=""

    if ! command -v python3 >/dev/null 2>&1; then
        emit_check_result "${check_id}" blocked error external "未找到 python3。" "安装 python3 后重试。"
        return
    fi

    if [[ ! -f pyproject.toml ]]; then
        emit_check_result "${check_id}" invalid error project "缺少 pyproject.toml。" "补齐 pyproject.toml 后重试。"
        return
    fi

    if ! requires_python="$(python3 - <<'PY'
import re
from pathlib import Path

text = Path("pyproject.toml").read_text(encoding="utf-8")

try:
    import tomllib  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - fallback for older python3
    tomllib = None

requires_python = ""
if tomllib is not None:
    data = tomllib.loads(text)
    requires_python = str((data.get("project", {}) or {}).get("requires-python", "")).strip()
else:
    in_project = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project = (line == "[project]")
            continue
        if in_project and line.startswith("requires-python"):
            match = re.match(r"requires-python\s*=\s*[\"']([^\"']+)[\"']", line)
            if match:
                requires_python = match.group(1).strip()
                break

if not requires_python:
    raise SystemExit("pyproject.toml 缺少 project.requires-python")
print(requires_python)
PY
    )"; then
        emit_check_result "${check_id}" invalid error project "无法读取 pyproject.toml 的 project.requires-python。" "补齐 project.requires-python 后重试。"
        return
    fi

    if version_output="$(PROJECT_REQUIRES_PYTHON="${requires_python}" python3 - <<'PY'
import os
import re
import sys

requires_python = os.environ["PROJECT_REQUIRES_PYTHON"]
version = (sys.version_info.major, sys.version_info.minor)
lower = re.search(r">=\s*(\d+)\.(\d+)", requires_python)
upper = re.search(r"<\s*(\d+)\.(\d+)", requires_python)
ok = True
if lower and version < (int(lower.group(1)), int(lower.group(2))):
    ok = False
if upper and version >= (int(upper.group(1)), int(upper.group(2))):
    ok = False
print(requires_python)
raise SystemExit(0 if ok else 1)
PY
    )"; then
        emit_check_result "${check_id}" ready info project "python3 满足 pyproject 要求（${version_output}）。" ""
        return
    fi

    emit_check_result "${check_id}" blocked error external "python3 版本不满足 pyproject 要求（${requires_python}）。" "安装满足版本要求的 python3。"
}

check_just_tool() {
    local check_id="${1:-just-tool}"

    if command -v just >/dev/null 2>&1; then
        emit_check_result "${check_id}" ready info project "just 已安装。" ""
    else
        emit_check_result "${check_id}" blocked error external "未找到 just。" "安装 just 后重试。"
    fi
}

check_uv_sync() {
    local check_id="${1:-uv-sync}"
    local tmpfile=""

    if ! command -v uv >/dev/null 2>&1; then
        emit_check_result "${check_id}" blocked error external "未找到 uv。" "安装 uv 后重试。"
        return
    fi

    tmpfile="$(mktemp)"
    trap 'rm -f "${tmpfile:-}"' RETURN

    if [[ ! -d .venv ]]; then
        emit_check_result "${check_id}" missing error project "Python 虚拟环境缺失。" "执行 just prep sync。"
        return
    fi

    if uv sync --check --locked --no-cache >"${tmpfile}" 2>&1; then
        emit_check_result "${check_id}" ready info project "uv lock 与 .venv 已同步。" ""
    else
        emit_check_result "${check_id}" stale error project "uv lock 与 .venv 未同步。" "执行 just prep sync。"
    fi
}

check_dotenv() {
    local check_id="${1:-dotenv}"

    if [[ -f .env ]]; then
        emit_check_result "${check_id}" ready info project ".env 已就绪。" ""
        return
    fi

    if [[ -f .env.example ]]; then
        emit_check_result "${check_id}" missing error project ".env 缺失。" "执行 just prep env。"
        return
    fi

    emit_check_result "${check_id}" invalid error project ".env.example 缺失，无法自动生成 .env。" "补齐 .env.example。"
}

check_hooks() {
    local check_id="${1:-hooks}"
    local pre_commit_hook=""
    local commit_msg_hook=""

    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        emit_check_result "${check_id}" ready info project "当前目录不是 Git 仓库，跳过 hooks 检查。" ""
        return
    fi

    pre_commit_hook="$(git rev-parse --git-path hooks/pre-commit)"
    commit_msg_hook="$(git rev-parse --git-path hooks/commit-msg)"

    if [[ -x "${pre_commit_hook}" ]] && [[ -x "${commit_msg_hook}" ]]; then
        emit_check_result "${check_id}" ready info project "Git hooks 已安装。" ""
    else
        emit_check_result "${check_id}" missing error project "Git hooks 未安装完整。" "执行 just prep hooks。"
    fi
}

check_infra_tooling() {
    local check_id="${1:-infra-tooling}"
    local script_path=""
    local tmpfile=""

    if [[ "${SKIP_SHARED_INFRA_DOCTOR:-0}" == "1" ]]; then
        emit_check_result "${check_id}" ready info shared-infra "共享 infra 检查已跳过（SKIP_SHARED_INFRA_DOCTOR=1）。" ""
        return
    fi

    script_path="$(resolve_dev_infra_script)"
    if [[ ! -x "${script_path}" ]]; then
        emit_check_result "${check_id}" blocked error external "未找到共享 infra 工具：${script_path}" "检查 acps-infra/dev-infra。"
        return
    fi

    tmpfile="$(mktemp)"
    trap 'rm -f "${tmpfile:-}"' RETURN
    if "${script_path}" check >"${tmpfile}" 2>&1; then
        emit_check_result "${check_id}" ready info shared-infra "共享 infra 工具链检查通过。" ""
    else
        emit_check_result "${check_id}" blocked error external "共享 infra 工具链检查失败。" "执行 just infra check 并修复输出中的问题。"
    fi
}

check_infra_service_ready() {
    local service_name="$1"
    local check_id="${2:-infra-${service_name}}"
    local script_path=""
    local tmpfile=""
    local status_output=""
    local service=""
    local _mode=""
    local state=""
    local health=""
    local _ports=""

    if [[ "${SKIP_SHARED_INFRA_DOCTOR:-0}" == "1" ]]; then
        emit_check_result "${check_id}" ready info shared-infra "共享 ${service_name} 检查已跳过（SKIP_SHARED_INFRA_DOCTOR=1）。" ""
        return
    fi

    script_path="$(resolve_dev_infra_script)"
    if [[ ! -x "${script_path}" ]]; then
        emit_check_result "${check_id}" blocked error external "未找到共享 infra 工具：${script_path}" "检查 acps-infra/dev-infra。"
        return
    fi

    tmpfile="$(mktemp)"
    trap 'rm -f "${tmpfile:-}"' RETURN
    if ! status_output="$("${script_path}" status "${service_name}" --format=tsv 2>"${tmpfile}")"; then
        emit_check_result "${check_id}" blocked error external "无法读取共享 ${service_name} 状态。" "执行 just infra check 或检查 Docker / compose。"
        return
    fi

    if [[ -z "${status_output}" ]]; then
        emit_check_result "${check_id}" blocked error external "共享 ${service_name} 状态输出为空。" "执行 just infra status ${service_name}。"
        return
    fi

    IFS=$'\t' read -r service _mode state health _ports <<< "${status_output%%$'\n'*}"

    if [[ "${service}" != "${service_name}" ]]; then
        emit_check_result "${check_id}" blocked error external "共享 ${service_name} 状态输出格式异常。" "执行 just infra status ${service_name} --format=tsv。"
        return
    fi

    if [[ "${state}" == "running" ]] && [[ "${health}" == "healthy" ]]; then
        emit_check_result "${check_id}" ready info shared-infra "共享 ${service_name} 已运行且健康。" ""
        return
    fi

    emit_check_result "${check_id}" missing error shared-infra "共享 ${service_name} 未就绪（state=${state}, health=${health}）。" "执行 just infra up ${service_name} && just infra wait ${service_name}。"
}

ensure_check_with_fix() {
    local check_id="$1"
    local fix_callback="$2"
    local results_file=""

    results_file="$(mktemp)"
    trap 'rm -f "${results_file:-}"' RETURN

    run_check_capture "${check_id}" "${results_file}"
    if results_file_is_ready "${results_file}"; then
        render_results_file bootstrap "${results_file}"
        return 0
    fi

    render_results_file doctor "${results_file}"
    if ! results_file_only_fixable_errors "${results_file}"; then
        return 1
    fi

    if [[ -n "${fix_callback}" ]]; then
        "${fix_callback}"
    fi

    run_check_capture "${check_id}" "${results_file}"
    render_results_file bootstrap "${results_file}"
    results_file_is_ready "${results_file}"
}

ensure_uv_sync() {
    local check_id="${1:-uv-sync}"

    ensure_check_with_fix "${check_id}" _fix_uv_sync
}

_fix_uv_sync() {
    local python_version=""

    python_version="$(resolve_project_python_version)"
    UV_MANAGED_PYTHON=1 run_uv_with_mutating_cache uv python install "${python_version}"
    UV_MANAGED_PYTHON=1 run_uv_with_mutating_cache uv sync --python "${python_version}" --managed-python
}

ensure_dotenv() {
    local check_id="${1:-dotenv}"

    ensure_check_with_fix "${check_id}" _fix_dotenv
}

_fix_dotenv() {
    if declare -F generate_project_dotenv >/dev/null 2>&1; then
        generate_project_dotenv
        return
    fi

    cp .env.example .env
}

ensure_hooks() {
    local check_id="${1:-hooks}"

    ensure_check_with_fix "${check_id}" _fix_hooks
}

_fix_hooks() {
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        return
    fi

    run_uv_with_mutating_cache uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
}

ensure_infra_tooling() {
    local check_id="${1:-infra-tooling}"

    ensure_check_with_fix "${check_id}" ""
}

ensure_infra_service_ready() {
    local service_name="$1"
    local check_id="${2:-infra-${service_name}}"
    # bash 函数名不能含连字符；公开 service 名（如 victoria-metrics）映射为下划线
    local fix_callback="_fix_infra_service_${service_name//-/_}"

    ensure_check_with_fix "${check_id}" "${fix_callback}"
}

_fix_infra_service_postgres() { just infra up postgres; just infra wait postgres; }
_fix_infra_service_redis() { just infra up redis; just infra wait redis; }
_fix_infra_service_rabbitmq() { just infra up rabbitmq; just infra wait rabbitmq; }
_fix_infra_service_kafka() { just infra up kafka; just infra wait kafka; }
_fix_infra_service_victoria_metrics() { just infra up victoria-metrics; just infra wait victoria-metrics; }
_fix_infra_service_clickhouse() { just infra up clickhouse; just infra wait clickhouse; }
_fix_infra_service_opensearch() { just infra up opensearch; just infra wait opensearch; }
_fix_infra_service_keycloak() { just infra up keycloak; just infra wait keycloak; }

# 旧式兼容 helper
check_dev_infra_tooling() {
    local tmpfile="${1:-}"
    local results_file=""

    results_file="$(mktemp)"
    trap 'rm -f "${results_file:-}"' RETURN
    run_check_capture infra-tooling "${results_file}"
    if results_file_has_errors "${results_file}"; then
        if [[ -n "${tmpfile}" ]]; then
            cat "${results_file}" > "${tmpfile}"
        fi
        render_results_file doctor "${results_file}" >&2
        issues=$((issues + 1))
        infra_ok=0
    fi
}

check_dev_infra_service_health() {
    local service_name="$1"
    local tmpfile="${2:-}"
    local results_file=""

    results_file="$(mktemp)"
    trap 'rm -f "${results_file:-}"' RETURN
    run_check_capture "infra-${service_name}" "${results_file}"
    if results_file_has_errors "${results_file}"; then
        if [[ -n "${tmpfile}" ]]; then
            cat "${results_file}" > "${tmpfile}"
        fi
        render_results_file doctor "${results_file}" >&2
        issues=$((issues + 1))
    fi
}

check_python3_toolchain() {
    local results_file=""

    results_file="$(mktemp)"
    trap 'rm -f "${results_file:-}"' RETURN
    run_check_capture python-toolchain "${results_file}"
    if results_file_has_errors "${results_file}"; then
        render_results_file doctor "${results_file}" >&2
        issues=$((issues + 1))
    fi
}

check_uv_sync_locked() {
    local tmpfile="${1:-}"
    local results_file=""

    results_file="$(mktemp)"
    trap 'rm -f "${results_file:-}"' RETURN
    run_check_capture uv-sync "${results_file}"
    if results_file_has_errors "${results_file}"; then
        if [[ -n "${tmpfile}" ]]; then
            cat "${results_file}" > "${tmpfile}"
        fi
        render_results_file doctor "${results_file}" >&2
        issues=$((issues + 1))
    else
        uv_ready=1
    fi
}

check_just_available() {
    local results_file=""

    results_file="$(mktemp)"
    trap 'rm -f "${results_file:-}"' RETURN
    run_check_capture just-tool "${results_file}"
    if results_file_has_errors "${results_file}"; then
        render_results_file doctor "${results_file}" >&2
        issues=$((issues + 1))
    fi
}

check_dotenv_file() {
    local results_file=""

    results_file="$(mktemp)"
    trap 'rm -f "${results_file:-}"' RETURN
    run_check_capture dotenv "${results_file}"
    if results_file_has_errors "${results_file}"; then
        render_results_file doctor "${results_file}" >&2
        issues=$((issues + 1))
    else
        dotenv_ready=1
    fi
}
