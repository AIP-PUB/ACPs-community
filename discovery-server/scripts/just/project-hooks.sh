#!/usr/bin/env bash

: "${app_port:=9005}"
: "${app_pid_file:=logs/discovery-server.pid}"
: "${app_log_file:=logs/discovery-server.log}"
: "${default_test_database_url:=postgresql+asyncpg://discovery:discovery@localhost:5432/agent_discovery_test}"
: "${legacy_pid_file:=logs/service.pid}"
: "${test_e2e_log_file:=logs/discovery-server-e2e.log}"

configure_project_package_runtime() {
    PACKAGE_RUNTIME_REQUIRED_PATHS=(
        config
        alembic
        alembic.ini
        .env.example
        README.md
        pyproject.toml
        scripts/prompts/planner_prompt.txt
        scripts/prompts/cluster_prompt.txt
    )

    PACKAGE_RUNTIME_SIBLING_REPOS=(
        acps-sdk
    )

    PACKAGE_RUNTIME_BUNDLE_MAP=(
        "config|config|config"
        "alembic|alembic|migration"
        "alembic.ini|alembic.ini|migration"
        ".env.example|.env.example|env_template"
        "README.md|README.md|doc"
        "scripts/prompts|scripts/prompts|prompt"
    )

    PACKAGE_RUNTIME_BUNDLE_EXCLUDE_MAP=(
        "${DEFAULT_BUNDLE_EXCLUDE_MAP[@]}"
        "._*"
        "*/._*"
    )

    PACKAGE_RUNTIME_CHMOD_PATHS=()
    PACKAGE_RUNTIME_REMOVE_PATTERNS=()

    PACKAGE_RUNTIME_COMPONENTS=(
        "discovery-server-api|python-service|uvicorn app.main:app --host 0.0.0.0 --port 9005|9005|http://127.0.0.1:9005/health||config/production.toml"
    )
    PACKAGE_RUNTIME_INTERNAL_WHEELS=(acps-sdk)
    PACKAGE_RUNTIME_EXTERNAL_COMPONENTS=(postgresql)
    # discovery-server 的业务运行模式是 DISCOVERY_MODE=cpu（外部 Embedding/LLM API）和
    # DISCOVERY_MODE=gpu（本地 BGE-M3/FlagEmbedding 推理栈）两种，对应 pyproject.toml 的
    # base dependencies 和 [project.optional-dependencies].gpu。variant 对外命名统一为
    # cpu/gpu，与业务运行模式一致；不再使用 cpu/cuda（cuda 只是 Linux 目标平台解析 GPU
    # 依赖时可能带出的底层 backend 名，不是业务 variant 名）。具体差异化解析交给
    # run_project_package_export_variant_lockfile 钩子处理，不在 acps-infra 装配脚本里
    # 硬编码，也不修改本项目默认的 dependencies/uv.lock 解析结果（不影响本地开发
    # `uv sync` 的默认行为）。
    #
    # variant_lockfiles 的 key 仅为纯 variant（"cpu" / "gpu"）。lock 文件名带本机
    # host_arch（如 requirements-runtime-cpu-arm64.lock）便于人读；assemble 只按
    # --variant 取 key，不从文件名解析 arch。每次 just package wheel 只为当前
    # host_arch 生成声明需要的 lock，不预置另一 arch。
    #
    # 必须用具体的 `--python-platform {arch}-manylinux_2_28`（见
    # run_project_package_export_variant_lockfile）：泛化 `linux` 在多候选 manylinux
    # wheel 时可能锁到与实际运行环境不匹配的 hash。
    #
    # DISCOVERY_PACKAGE_VARIANTS：逗号分隔的 variant 列表，控制薄包声明/生成哪些
    # lock。未设置时默认仅 cpu；含 cpu 则声明 cpu，含 gpu 则声明 gpu（例如
    # cpu,gpu 或仅 gpu）。一键发布脚本可按 --discovery-variant 导出本变量后再
    # collect，确保 GPU 发布包所需 lock 已生成。
    local host_arch=""
    local package_variants=""
    local package_variant=""
    local want_cpu=0
    local want_gpu=0
    local -a package_variant_list=()

    host_arch="$(_discovery_resolve_host_arch)" || return 1

    package_variants="${DISCOVERY_PACKAGE_VARIANTS:-cpu}"
    package_variants="${package_variants// /}"
    IFS=',' read -r -a package_variant_list <<< "${package_variants}"
    for package_variant in "${package_variant_list[@]}"; do
        case "${package_variant}" in
            "") continue ;;
            cpu) want_cpu=1 ;;
            gpu) want_gpu=1 ;;
            *)
                echo "[ERROR] DISCOVERY_PACKAGE_VARIANTS 含非法项：${package_variant}（合法：cpu、gpu；示例：cpu 或 cpu,gpu）" >&2
                return 1
                ;;
        esac
    done
    if [[ "${want_cpu}" -eq 0 && "${want_gpu}" -eq 0 ]]; then
        echo "[ERROR] DISCOVERY_PACKAGE_VARIANTS 为空或未解析出任何 variant（当前：${DISCOVERY_PACKAGE_VARIANTS:-}）" >&2
        return 1
    fi

    PACKAGE_RUNTIME_VARIANT_LOCKFILES=()
    if [[ "${want_cpu}" -eq 1 ]]; then
        PACKAGE_RUNTIME_VARIANT_LOCKFILES+=("cpu=requirements-runtime-cpu-${host_arch}.lock")
    fi
    if [[ "${want_gpu}" -eq 1 ]]; then
        PACKAGE_RUNTIME_VARIANT_LOCKFILES+=("gpu=requirements-runtime-gpu-${host_arch}.lock")
    fi
}

# 解析本机构建 arch：x86_64→amd64，arm64/aarch64→arm64。
_discovery_resolve_host_arch() {
    local machine=""

    machine="$(uname -m)"
    case "${machine}" in
        x86_64) printf '%s\n' "amd64" ;;
        arm64|aarch64) printf '%s\n' "arm64" ;;
        *)
            echo "[ERROR] discovery-server 不支持的宿主机架构：${machine}（合法：x86_64、arm64、aarch64）" >&2
            return 1
            ;;
    esac
}

# 应用发布包实现结果 §2.7：discovery-server CPU/GPU 依赖分层。
# discovery-server 的 cpu/gpu variant 差异化解析，两段式生成，确保锁定版本来自 uv.lock：
#
#   第一段：`uv export --locked` 从 uv.lock 导出无 hash 的版本约束文件（cpu 不带
#           gpu extra，gpu 带 --extra gpu），保证版本来源可追溯到 uv.lock，而不是
#           `uv pip compile` 自由解析出的新版本。
#   第二段：以该约束文件为 `uv pip compile` 的 --constraints，按本机 host_arch
#           对应的 "{arch}-manylinux_2_28"（不是泛化的 "linux"——原因见上方注释）
#           生成带 hash 的最终 lockfile；cpu 不传 --extra gpu，gpu 传 --extra gpu。
#
# 入参为纯 variant（cpu / gpu），不再接受 cpu-arm64 一类复合 key。
#
# 不修改 pyproject.toml 的 dependencies/[tool.uv.sources]（那会改变本地 `uv sync` 默认行为，
# 影响所有开发者的日常环境）。`uv pip compile` 不感知 `--no-emit-local`，因此复用既有的
# run_project_package_filter_requirements 过滤本地可编辑依赖行（-e ../acps-sdk）。
run_project_package_export_variant_lockfile() {
    local variant="$1"
    local output_path="$2"
    local host_arch=""
    local python_platform=""
    local constraints_file=""
    local temp_file=""
    local -a extra_args=()

    host_arch="$(_discovery_resolve_host_arch)" || return 1

    case "${host_arch}" in
        arm64) python_platform="aarch64-manylinux_2_28" ;;
        amd64) python_platform="x86_64-manylinux_2_28" ;;
        *)
            echo "[ERROR] discovery-server 不支持的架构：${host_arch}（合法取值：arm64、amd64）" >&2
            return 1
            ;;
    esac

    case "${variant}" in
        cpu)
            extra_args=()
            ;;
        gpu)
            extra_args=(--extra gpu)
            ;;
        *)
            echo "[ERROR] discovery-server 不支持的 variant：${variant}（合法取值：cpu、gpu）" >&2
            return 1
            ;;
    esac

    constraints_file="$(mktemp)"
    temp_file="$(mktemp)"

    (
        cd "${PROJECT_DIR}" || exit 1

        # 第一段：导出版本约束（不含 hash，不含本项目自身/本地路径依赖）。
        if [[ "${#extra_args[@]}" -gt 0 ]]; then
            run_uv_with_mutating_cache uv export --locked --format requirements-txt --no-dev \
                --no-emit-project --no-emit-local --no-hashes \
                "${extra_args[@]}" \
                --output-file "${constraints_file}"
        else
            run_uv_with_mutating_cache uv export --locked --format requirements-txt --no-dev \
                --no-emit-project --no-emit-local --no-hashes \
                --output-file "${constraints_file}"
        fi

        # 第二段：以上述约束 + 目标架构重新编译带 hash 的最终 lockfile。对
        # PACKAGE_RUNTIME_INTERNAL_WHEELS 里声明的每个内部 wheel（当前只有 acps-sdk）都单独
        # 追加一个 --no-emit-package，不硬编码某一个包名；数组展开前先判断长度非零，
        # 避免在旧版 bash（macOS 系统自带 3.2）下对空数组展开触发 "unbound variable"。
        local -a no_emit_args=()
        local internal_wheel=""
        for internal_wheel in "${PACKAGE_RUNTIME_INTERNAL_WHEELS[@]}"; do
            no_emit_args+=(--no-emit-package "${internal_wheel}")
        done

        local -a compile_args=(
            pyproject.toml
            --constraints "${constraints_file}"
            --output-file "${temp_file}"
            --python-version 3.14
            --python-platform "${python_platform}"
            --no-strip-markers
            --generate-hashes
        )
        if [[ "${#no_emit_args[@]}" -gt 0 ]]; then
            compile_args+=("${no_emit_args[@]}")
        fi
        if [[ "${#extra_args[@]}" -gt 0 ]]; then
            compile_args+=("${extra_args[@]}")
        fi

        run_uv_with_mutating_cache uv pip compile "${compile_args[@]}"
    )

    run_project_package_filter_requirements "${temp_file}" "${output_path}"
    rm -f "${temp_file}" "${constraints_file}"
}

# 应用发布包设计 §5.5：薄包阶段 GPU deny-list / presence-list 审计。
#
# package-wheel-runtime.sh 在导出完所有 variant lockfile、生成 runtime-package.toml 之前
# 会调用 run_project_package_post_stage(staging_dir)；此时 staging_dir 下已经有每个
# variant 对应的 lockfile 文件，是在薄包真正打包前拦截"CPU 包混入 GPU 依赖"这类问题的
# 最后位置。校验必须基于 PEP 503 canonical package name 精确匹配，不能用简单字符串
# grep（例如 "datasets" 不应该误伤 "ir-datasets"）。
_DISCOVERY_GPU_DENY_LIST=(torch flagembedding transformers sentence-transformers peft accelerate datasets sentencepiece)
_DISCOVERY_GPU_REQUIRED_LIST=(torch flagembedding)

_discovery_canonicalize_pkg_name() {
    # 必须以换行结尾：本函数的调用方（_discovery_lockfile_package_names 的内层循环）
    # 把每次调用的输出当作生产者管道里的"一行"消费；`printf '%s'`（不带 \n）会导致
    # 多次调用的输出被拼成一整块没有任何换行的字符串，consumer 端 `while read` 在
    # EOF 前读不到任何一个完整"行"，`read` 直接以失败退出，循环体一次都不会执行——
    # 曾经在这里踩过这个坑，务必保留 \n。
    printf '%s\n' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[-_.]+/-/g'
}

# 只匹配位于行首、形如 "pkg-name==1.2.3" 的 requirement 行；hash 续行（前导空白 +
# --hash=...）和 "# via ..." 注释行都不会匹配到行首非空白字符，天然被排除。
_discovery_lockfile_package_names() {
    local lockfile_path="$1"
    local raw_name=""

    while IFS= read -r raw_name; do
        _discovery_canonicalize_pkg_name "${raw_name}"
    done < <(grep -oE '^[A-Za-z0-9_.-]+' "${lockfile_path}")
}

_discovery_audit_cpu_lockfile_deny_list() {
    local lockfile_path="$1"
    local canonical_name=""
    local deny_name=""
    local violated=0

    while IFS= read -r canonical_name; do
        [[ -z "${canonical_name}" ]] && continue

        if [[ "${canonical_name}" == nvidia-* ]]; then
            echo "[ERROR] ${lockfile_path} 中出现 GPU 依赖：${canonical_name}（nvidia- 前缀）" >&2
            violated=1
            continue
        fi

        for deny_name in "${_DISCOVERY_GPU_DENY_LIST[@]}"; do
            if [[ "${canonical_name}" == "${deny_name}" ]]; then
                echo "[ERROR] ${lockfile_path} 中出现 GPU 依赖：${canonical_name}" >&2
                violated=1
            fi
        done
    done < <(_discovery_lockfile_package_names "${lockfile_path}")

    return "${violated}"
}

_discovery_audit_gpu_lockfile_presence() {
    local lockfile_path="$1"
    local canonical_name=""
    local required_name=""
    local -a found_names=()
    local missing=0

    while IFS= read -r canonical_name; do
        [[ -z "${canonical_name}" ]] && continue
        found_names+=("${canonical_name}")
    done < <(_discovery_lockfile_package_names "${lockfile_path}")

    for required_name in "${_DISCOVERY_GPU_REQUIRED_LIST[@]}"; do
        local present=0
        if [[ "${#found_names[@]}" -gt 0 ]]; then
            for canonical_name in "${found_names[@]}"; do
                if [[ "${canonical_name}" == "${required_name}" ]]; then
                    present=1
                    break
                fi
            done
        fi
        if [[ "${present}" -eq 0 ]]; then
            echo "[ERROR] ${lockfile_path} 缺少 GPU 必需依赖：${required_name}" >&2
            missing=1
        fi
    done

    return "${missing}"
}

run_project_package_post_stage() {
    local staging_dir="$1"
    local variant_entry=""
    local variant_name=""
    local variant_lockfile=""
    local variant_prefix=""
    local audit_failed=0

    if [[ "${#PACKAGE_RUNTIME_VARIANT_LOCKFILES[@]}" -eq 0 ]]; then
        return 0
    fi

    for variant_entry in "${PACKAGE_RUNTIME_VARIANT_LOCKFILES[@]}"; do
        variant_name="${variant_entry%%=*}"
        variant_lockfile="${variant_entry#*=}"
        variant_prefix="${variant_name%%-*}"

        if [[ ! -f "${staging_dir}/${variant_lockfile}" ]]; then
            echo "[ERROR] 薄包校验：声明的 variant lockfile 不存在：${variant_lockfile}" >&2
            audit_failed=1
            continue
        fi

        case "${variant_prefix}" in
            cpu)
                if ! _discovery_audit_cpu_lockfile_deny_list "${staging_dir}/${variant_lockfile}"; then
                    audit_failed=1
                fi
                ;;
            gpu)
                if ! _discovery_audit_gpu_lockfile_presence "${staging_dir}/${variant_lockfile}"; then
                    audit_failed=1
                fi
                ;;
            *)
                echo "[ERROR] 薄包校验：无法识别的 variant 前缀：${variant_prefix}（来自 ${variant_name}）" >&2
                audit_failed=1
                ;;
        esac
    done

    if [[ "${audit_failed}" -ne 0 ]]; then
        echo "[ERROR] discovery-server 薄包 GPU 依赖边界校验失败，终止打包。" >&2
        return 1
    fi

    echo "[OK] discovery-server 薄包 GPU 依赖边界校验通过（cpu lockfile 不含 GPU deny-list；gpu lockfile 如声明则含必需依赖）。"
}

run_project_help_section() {
    local section="$1"

    case "${section}" in
        test)
            echo "  coverage-report      生成包含 HTML 输出的覆盖率报告"
            ;;
        prep)
            echo "  seed [app|test]     导入 demo ACS 样本并建立本地技能索引"
            echo "  reseed [app|test]   清空后重导 demo ACS 样本"
            echo "  sync-embedding-dimension [app|test] [args...]  同步 embedding 维度"
            ;;
    esac
}

run_project_prep_action() {
    local action="$1"
    shift || true

    resolve_test_database_url() {
        if [[ -n "${TEST_DATABASE_URL:-}" ]]; then
            printf '%s\n' "${TEST_DATABASE_URL}"
            return
        fi

        if [[ -f .env ]]; then
            local dot_env_test_database_url=""
            dot_env_test_database_url="$(run_uv_with_mutating_cache uv run python -c 'from pathlib import Path; from dotenv import dotenv_values; env_path = Path(".env"); print((dotenv_values(env_path).get("TEST_DATABASE_URL") or "").strip())' 2>/dev/null || true)"
            if [[ -n "${dot_env_test_database_url}" ]]; then
                printf '%s\n' "${dot_env_test_database_url}"
                return
            fi
        fi

        printf '%s\n' "postgresql+asyncpg://discovery:discovery@localhost:5432/agent_discovery_test"
    }

    case "${action}" in
        migrate)
            local migrate_target="dev"
            local test_database_url=""

            if [[ "$#" -gt 1 ]]; then
                echo "[ERROR] just prep migrate 只接受一个可选目标参数：dev 或 test。" >&2
                exit 2
            fi
            if [[ "$#" -eq 1 ]]; then
                if [[ "$1" != "dev" && "$1" != "test" ]]; then
                    echo "[ERROR] just prep migrate 仅支持 dev 或 test，当前为：$1" >&2
                    exit 2
                fi
                migrate_target="$1"
            fi

            if [[ "${migrate_target}" == "dev" ]]; then
                run_uv_with_mutating_cache uv run python scripts/ensure_vector_extension.py --database-url-env DATABASE_URL --admin-url-env DATABASE_ADMIN_URL
                run_uv_with_mutating_cache uv run alembic upgrade head
            else
                test_database_url="$(resolve_test_database_url)"
                TEST_DATABASE_URL="${test_database_url}" run_uv_with_mutating_cache uv run python scripts/ensure_test_database.py
                APP_ENV=testing DATABASE_URL="${test_database_url}" TEST_DATABASE_URL="${test_database_url}" run_uv_with_mutating_cache uv run alembic upgrade head
            fi
            ;;
        seed)
            local seed_target="app"
            local seed_is_dry_run=0
            local seed_arg=""
            local test_database_url=""

            if [[ "$#" -gt 0 ]] && [[ "$1" == "app" || "$1" == "test" ]]; then
                seed_target="$1"
                shift
            fi
            for seed_arg in "$@"; do
                if [[ "${seed_arg}" == "--dry-run" ]]; then
                    seed_is_dry_run=1
                    break
                fi
            done
            if [[ "${seed_target}" == "test" && "${seed_is_dry_run}" -eq 0 ]]; then
                test_database_url="$(resolve_test_database_url)"
                TEST_DATABASE_URL="${test_database_url}" run_uv_with_mutating_cache uv run python scripts/check_test_database_ready.py
            fi
            run_uv_with_mutating_cache uv run python scripts/seed.py "${seed_target}" "$@"
            ;;
        reseed)
            local seed_target="app"
            local seed_is_dry_run=0
            local seed_arg=""
            local test_database_url=""

            if [[ "$#" -gt 0 ]] && [[ "$1" == "app" || "$1" == "test" ]]; then
                seed_target="$1"
                shift
            fi
            for seed_arg in "$@"; do
                if [[ "${seed_arg}" == "--dry-run" ]]; then
                    seed_is_dry_run=1
                    break
                fi
            done
            if [[ "${seed_target}" == "test" && "${seed_is_dry_run}" -eq 0 ]]; then
                test_database_url="$(resolve_test_database_url)"
                TEST_DATABASE_URL="${test_database_url}" run_uv_with_mutating_cache uv run python scripts/check_test_database_ready.py
            fi
            run_uv_with_mutating_cache uv run python scripts/seed.py "${seed_target}" --reset "$@"
            ;;
        sync-embedding-dimension)
            local sync_target="app"
            local test_database_url=""

            if [[ "$#" -gt 0 ]] && [[ "$1" == "app" || "$1" == "test" ]]; then
                sync_target="$1"
                shift
            fi

            if [[ "${sync_target}" == "app" ]]; then
                run_uv_with_mutating_cache uv run python scripts/maintenance/sync_embedding_dimension.py "$@"
            else
                test_database_url="$(resolve_test_database_url)"
                TEST_DATABASE_URL="${test_database_url}" run_uv_with_mutating_cache uv run python scripts/check_test_database_ready.py
                APP_ENV=testing DATABASE_URL="${test_database_url}" TEST_DATABASE_URL="${test_database_url}" \
                    run_uv_with_mutating_cache uv run python scripts/maintenance/sync_embedding_dimension.py "$@"
            fi
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_qa_action() {
    local action="$1"

    case "${action}" in
        pip-audit)
            local tmpfile=""

            tmpfile="$(mktemp)"
            trap 'rm -f "${tmpfile:-}"' RETURN
            run_uv_with_mutating_cache uv export --format requirements.txt --all-groups --no-hashes --no-header --no-emit-project --prune pip-audit --prune pip-api --prune pip --output-file "${tmpfile}"
            run_uv_with_mutating_cache uv run pip-audit -r "${tmpfile}" --progress-spinner off --no-deps --disable-pip --ignore-vuln CVE-2025-3000
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_package_filter_requirements() {
    local input_file="$1"
    local output_file="$2"

    awk '
        $0 == "-e ../acps-sdk" { skip_next_via = 1; next }
        skip_next_via == 1 && $0 ~ /^    # via / { skip_next_via = 0; next }
        { skip_next_via = 0; print }
    ' "${input_file}" > "${output_file}"
}

load_discovery_db_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/db-lib.sh"
}

load_discovery_pytest_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/pytest-lib.sh"
}

load_discovery_app_helpers() {
    # shellcheck source=/dev/null
    source "../acps-infra/dev-infra/just/app-lib.sh"
}

discovery_run_venv_python() {
    run_uv_with_mutating_cache uv run --no-sync python "$@"
}

discovery_resolve_test_database_url() {
    if [[ -n "${TEST_DATABASE_URL:-}" ]]; then
        printf '%s\n' "${TEST_DATABASE_URL}"
        return
    fi

    if [[ -f .env ]]; then
        local dot_env_test_database_url=""
        dot_env_test_database_url="$(run_uv_with_mutating_cache uv run python -c 'from pathlib import Path; from dotenv import dotenv_values; env_path = Path(".env"); print((dotenv_values(env_path).get("TEST_DATABASE_URL") or "").strip())' 2>/dev/null || true)"
        if [[ -n "${dot_env_test_database_url}" ]]; then
            printf '%s\n' "${dot_env_test_database_url}"
            return
        fi
    fi

    printf '%s\n' "${default_test_database_url}"
}

discovery_ensure_test_ready() {
    local test_database_url=""

    test_database_url="$(discovery_resolve_test_database_url)"
    TEST_DATABASE_URL="${test_database_url}" run_uv_with_mutating_cache uv run python scripts/check_test_database_ready.py
}

discovery_build_test_env_prefix() {
    local test_database_url=""

    test_database_url="$(discovery_resolve_test_database_url)"
    printf 'APP_ENV=testing DATABASE_URL=%q TEST_DATABASE_URL=%q' "${test_database_url}" "${test_database_url}"
}

discovery_resolve_test_mode() {
    local explicit_mode="${DISCOVERY_TEST_MODE:-${DISCOVERY_MODE:-cpu}}"

    explicit_mode="$(printf '%s' "${explicit_mode}" | tr '[:upper:]' '[:lower:]')"
    if [[ -z "${explicit_mode}" ]]; then
        explicit_mode="cpu"
    fi
    printf '%s\n' "${explicit_mode}"
}

discovery_resolve_e2e_mode() {
    local explicit_mode="${DISCOVERY_E2E_MODE:-${DISCOVERY_MODE:-cpu}}"

    explicit_mode="$(printf '%s' "${explicit_mode}" | tr '[:upper:]' '[:lower:]')"
    if [[ -z "${explicit_mode}" ]]; then
        explicit_mode="cpu"
    fi
    printf '%s\n' "${explicit_mode}"
}

discovery_resolve_local_env_value() {
    local env_key="$1"

    if [[ ! -f .env ]]; then
        return 0
    fi

    run_uv_with_mutating_cache uv run python -c 'from pathlib import Path; from dotenv import dotenv_values; import sys; env_path = Path(".env"); env_key = sys.argv[1]; print((dotenv_values(env_path).get(env_key) or "").strip())' "${env_key}" 2>/dev/null || true
}

discovery_has_tests_in() {
    local target_dir="$1"

    if [[ ! -d "${target_dir}" ]]; then
        return 1
    fi

    find "${target_dir}" -type f \( -name 'test_*.py' -o -name '*_test.py' \) -print -quit | grep -q .
}

discovery_stop_pid() {
    local target_pid="$1"

    kill -- "-${target_pid}" 2>/dev/null || kill "${target_pid}" 2>/dev/null || true
    sleep 2
    if kill -0 "${target_pid}" 2>/dev/null; then
        kill -9 -- "-${target_pid}" 2>/dev/null || kill -9 "${target_pid}" 2>/dev/null || true
        sleep 1
    fi
}

discovery_kill_port() {
    local port="$1"
    local pids=""

    pids="$(lsof -ti :"${port}" 2>/dev/null || true)"
    if [[ -n "${pids}" ]]; then
        echo "${pids}" | xargs kill -9 2>/dev/null || true
    fi
}

discovery_launch_detached() {
    local target_log_file="$1"
    shift
    local py_bin=""

    py_bin="$(command -v python3 || command -v python)"
    if [[ -z "${py_bin}" ]]; then
        echo "[ERROR] 未找到 python 解释器，无法后台启动 discovery-server。" >&2
        exit 1
    fi

    "${py_bin}" -c 'import os, subprocess, sys; log_file = sys.argv[1]; cmd = sys.argv[2:]; log = open(log_file, "ab", buffering=0); proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, preexec_fn=os.setsid, close_fds=True); print(proc.pid)' "${target_log_file}" "$@"
}

discovery_e2e_pid=""
discovery_e2e_started=0

discovery_cleanup_e2e() {
    if [[ "${discovery_e2e_started:-0}" -eq 1 ]] && [[ -n "${discovery_e2e_pid:-}" ]] && kill -0 "${discovery_e2e_pid}" 2>/dev/null; then
        discovery_stop_pid "${discovery_e2e_pid}"
        echo "[INFO] 已停止临时 e2e 测试实例。"
    fi

    discovery_e2e_pid=""
    discovery_e2e_started=0
}

describe_project_check_step() {
    local check_id="$1"

    case "${check_id}" in
        discovery-test-db-ready)
            echo "检查 TEST_DATABASE_URL 对应的测试数据库可连接，且测试 schema 与 vector 扩展准备完成。"
            ;;
        discovery-uv-sync-gpu)
            echo "确保测试环境与 uv.lock 同步且已包含 gpu extra（torch/FlagEmbedding）；取代通用 uv-sync 检查作为本项目测试环境的同步检查项，避免两个 check 互相视对方为 stale 而循环卸装/重装。标准 integration/e2e/all 默认 CPU+GPU 双跑，缺少 gpu extra 会导致 GPU 分支从模型文件缺失时的优雅降级变成包缺失的 ModuleNotFoundError。需要修复时等价于 uv sync --extra gpu。"
            ;;
        *)
            return 1
            ;;
    esac
}

run_project_check() {
    local check_id="$1"

    case "${check_id}" in
        discovery-test-db-ready)
            if discovery_ensure_test_ready >/dev/null 2>&1; then
                emit_check_result "${check_id}" ready info project "测试数据库已就绪。" ""
            else
                emit_check_result "${check_id}" stale error project "测试数据库未准备好。" "执行 just prep migrate test 或检查 TEST_DATABASE_URL。"
            fi
            ;;
        discovery-uv-sync-gpu)
            if [[ ! -d .venv ]]; then
                emit_check_result "${check_id}" missing error project "Python 虚拟环境缺失。" "执行 uv sync --extra gpu。"
            elif uv sync --check --locked --extra gpu --no-cache >/dev/null 2>&1; then
                emit_check_result "${check_id}" ready info project "uv lock 与 .venv 已同步（含 gpu extra）。" ""
            else
                emit_check_result "${check_id}" stale error project "uv lock 与 .venv 未同步（含 gpu extra），GPU 分支测试会因包缺失失败。" "执行 uv sync --extra gpu。"
            fi
            ;;
        migrate-dev)
            load_discovery_db_helpers
            check_alembic_at_head "${check_id}"
            ;;
        migrate-test)
            load_discovery_db_helpers
            check_alembic_at_head "${check_id}" "$(discovery_build_test_env_prefix)"
            ;;
        *)
            return 127
            ;;
    esac
}

# ensure_check_with_fix 是共享 doctor-lib.sh 提供的通用"先查再修"包装：check 已就绪则直接
# 渲染 bootstrap 结果并跳过 fix，避免每次 bootstrap 都无条件重跑 `uv sync --extra gpu`。
_discovery_fix_uv_sync_gpu() {
    local python_version=""
    python_version="$(resolve_project_python_version)"
    UV_MANAGED_PYTHON=1 run_uv_with_mutating_cache uv python install "${python_version}"
    UV_MANAGED_PYTHON=1 run_uv_with_mutating_cache uv sync --python "${python_version}" --managed-python --extra gpu
}

run_project_ensure() {
    local check_id="$1"

    case "${check_id}" in
        discovery-uv-sync-gpu)
            ensure_check_with_fix "${check_id}" _discovery_fix_uv_sync_gpu
            ;;
        migrate-dev)
            load_discovery_db_helpers
            ensure_alembic_at_head "${check_id}"
            ;;
        migrate-test)
            load_discovery_db_helpers
            ensure_alembic_at_head "${check_id}" "$(discovery_build_test_env_prefix)"
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_dev_runtime() {
    local action="$1"
    shift || true

    load_discovery_app_helpers

    case "${action}" in
        start|"")
            if [[ "$#" -gt 1 ]]; then
                echo "[ERROR] dev start 只接受一个可选参数：bg / fg。" >&2
                exit 2
            fi

            if [[ "$#" -eq 0 ]] || [[ "$1" == "bg" ]]; then
                if pid="$(read_live_pid "${app_pid_file}")"; then
                    echo "[INFO] discovery-server 已在运行（PID=${pid}）。"
                    return 0
                fi

                if legacy_pid="$(read_live_pid "${legacy_pid_file}")"; then
                    echo "[INFO] 检测到 legacy 启动实例仍在运行（PID=${legacy_pid}）。"
                    return 0
                fi

                just dev bootstrap
                ensure_log_dir_for_file "${app_log_file}"

                app_pid="$(discovery_launch_detached "${app_log_file}" env PYTHONPATH="$PWD" uv run python -m app.main)"
                echo "${app_pid}" > "${app_pid_file}"

                sleep 3
                if ! kill -0 "${app_pid}" 2>/dev/null; then
                    rm -f "${app_pid_file}"
                    echo "[ERROR] discovery-server 启动失败，请检查 ${app_log_file}。" >&2
                    exit 1
                fi

                echo "[INFO] discovery-server 已启动（PID=${app_pid}）。"
                echo "[INFO] 日志文件：${app_log_file}"
                return 0
            fi

            if [[ "$1" == "fg" ]]; then
                if pid="$(read_live_pid "${app_pid_file}")"; then
                    echo "[ERROR] discovery-server 已有后台实例在运行（PID=${pid}）。请先执行 just dev stop，再执行 just dev start fg。" >&2
                    exit 2
                fi
                if legacy_pid="$(read_live_pid "${legacy_pid_file}")"; then
                    echo "[ERROR] discovery-server 检测到 legacy 后台实例仍在运行（PID=${legacy_pid}）。请先执行 just dev stop，再执行 just dev start fg。" >&2
                    exit 2
                fi
                just dev bootstrap
                ensure_log_dir_for_file "${app_log_file}"
                exec env PYTHONPATH="$PWD" uv run python -m app.main
            fi

            echo "[ERROR] 未知 dev start 参数：$1" >&2
            exit 2
            ;;
        stop)
            if pid="$(read_live_pid "${app_pid_file}")"; then
                discovery_stop_pid "${pid}"
                rm -f "${app_pid_file}"
                echo "[INFO] 已停止受管实例（PID=${pid}）。"
            elif legacy_pid="$(read_live_pid "${legacy_pid_file}")"; then
                discovery_stop_pid "${legacy_pid}"
                rm -f "${legacy_pid_file}"
                echo "[INFO] 已停止 legacy 实例（PID=${legacy_pid}）。"
            else
                echo "[INFO] discovery-server 当前未运行。"
            fi
            discovery_kill_port "${app_port}"
            ;;
        status)
            if pid="$(read_live_pid "${app_pid_file}")"; then
                echo "[INFO] discovery-server 正在运行（PID=${pid}，受管实例）。"
            elif legacy_pid="$(read_live_pid "${legacy_pid_file}")"; then
                echo "[INFO] discovery-server 正在运行（PID=${legacy_pid}，legacy 实例）。"
            else
                echo "[INFO] discovery-server 未运行。"
            fi
            ;;
        logs)
            ensure_log_dir_for_file "${app_log_file}"
            if [[ "${1:-}" == "follow" ]]; then
                touch "${app_log_file}"
                exec tail -n 200 -f "${app_log_file}"
            fi
            touch "${app_log_file}"
            tail -n 200 "${app_log_file}"
            ;;
        restart)
            if [[ -n "${1:-}" ]] && [[ "$1" != "bg" ]]; then
                echo "[ERROR] dev restart 只管理后台实例；如需前台运行，请直接执行 just dev start fg。" >&2
                exit 2
            fi

            run_project_dev_runtime stop
            run_project_dev_runtime start
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_test_action() {
    local action="$1"
    shift || true
    local test_mode=""
    local e2e_mode=""
    local test_database_url=""

    load_discovery_pytest_helpers

    case "${action}" in
        unit)
            if ! discovery_has_tests_in tests/unit; then
                echo "[INFO] 当前无测试文件，跳过 unit。"
                return 0
            fi
            if [[ "$#" -gt 0 ]]; then
                run_scoped_pytest tests/unit "$@"
            else
                run_scoped_pytest tests/unit
            fi
            ;;
        integration)
            if ! discovery_has_tests_in tests/integration; then
                echo "[INFO] 当前无测试文件，跳过 integration。"
                return 0
            fi

            if [[ -z "${DISCOVERY_TEST_MODE:-}" ]]; then
                echo "[INFO] 未显式指定 discovery mode，integration 将依次运行 CPU 和 GPU 两种模式。"
                if [[ "$#" -gt 0 ]]; then
                    DISCOVERY_TEST_MODE=cpu just test integration "$@"
                    DISCOVERY_TEST_MODE=gpu just test integration "$@"
                else
                    DISCOVERY_TEST_MODE=cpu just test integration
                    DISCOVERY_TEST_MODE=gpu just test integration
                fi
                return 0
            fi

            just test bootstrap
            discovery_ensure_test_ready
            test_mode="$(discovery_resolve_test_mode)"
            test_database_url="$(discovery_resolve_test_database_url)"
            if [[ "$#" -gt 0 ]]; then
                TEST_DATABASE_URL="${test_database_url}" DISCOVERY_TEST_MODE="${test_mode}" DISCOVERY_MODE="${test_mode}" run_scoped_pytest tests/integration "$@"
            else
                TEST_DATABASE_URL="${test_database_url}" DISCOVERY_TEST_MODE="${test_mode}" DISCOVERY_MODE="${test_mode}" run_scoped_pytest tests/integration
            fi
            ;;
        coverage)
            if ! discovery_has_tests_in tests/unit; then
                echo "[INFO]  当前无单元测试文件，跳过 coverage。"
                return 0
            fi
            if [[ "$#" -eq 0 ]]; then
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/unit/ --cov=app --cov-report=term-missing --cov-fail-under=70
            elif [[ "$1" == -* ]]; then
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests/unit/ "$@" --cov=app --cov-report=term-missing --cov-fail-under=70
            else
                APP_ENV=testing run_uv_with_mutating_cache uv run pytest "$@" --cov=app --cov-report=term-missing --cov-fail-under=70
            fi
            ;;
        coverage-report)
            if ! discovery_has_tests_in tests; then
                echo "[INFO]  当前无测试文件，跳过 coverage-report。"
                return 0
            fi
            discovery_ensure_test_ready
            test_database_url="$(discovery_resolve_test_database_url)"
            test_mode="$(discovery_resolve_test_mode)"
            e2e_mode="$(discovery_resolve_e2e_mode)"
            if [[ "$#" -eq 0 ]]; then
                TEST_DATABASE_URL="${test_database_url}" DISCOVERY_TEST_MODE="${test_mode}" DISCOVERY_E2E_MODE="${e2e_mode}" DISCOVERY_MODE="${test_mode}" \
                    APP_ENV=testing run_uv_with_mutating_cache uv run pytest --cov=app --cov-report=term-missing --cov-report=html tests
            elif [[ "$1" == -* ]]; then
                TEST_DATABASE_URL="${test_database_url}" DISCOVERY_TEST_MODE="${test_mode}" DISCOVERY_E2E_MODE="${e2e_mode}" DISCOVERY_MODE="${test_mode}" \
                    APP_ENV=testing run_uv_with_mutating_cache uv run pytest tests "$@" --cov=app --cov-report=term-missing --cov-report=html
            else
                TEST_DATABASE_URL="${test_database_url}" DISCOVERY_TEST_MODE="${test_mode}" DISCOVERY_E2E_MODE="${e2e_mode}" DISCOVERY_MODE="${test_mode}" \
                    APP_ENV=testing run_uv_with_mutating_cache uv run pytest "$@" --cov=app --cov-report=term-missing --cov-report=html
            fi
            ;;
        all)
            if [[ "$#" -gt 0 ]]; then
                just test unit "$@"
                just test integration "$@"
                just test e2e "$@"
                just test coverage "$@"
            else
                just test unit
                just test integration
                just test e2e
                just test coverage
            fi
            ;;
        *)
            return 127
            ;;
    esac
}

run_project_test_e2e_profile() {
    local profile="$1"
    shift || true
    local test_database_url=""
    local e2e_log_file=""
    local e2e_host=""
    local e2e_port=""
    local e2e_base_url=""
    local e2e_mode=""
    local e2e_health_url=""
    local embedding_api_key=""
    local embedding_base_url=""
    local embedding_model_name=""
    local embedding_model_path=""
    local embedding_devices=""
    local reranker_url=""
    local discovery_llm_api_key=""
    local discovery_llm_base_url=""
    local discovery_llm_model_name=""

    load_discovery_pytest_helpers
    load_discovery_app_helpers

    if [[ "${profile}" != "local" ]]; then
        return 127
    fi

    if ! discovery_has_tests_in tests/e2e; then
        echo "[INFO] 当前无 e2e 测试文件，跳过 e2e。"
        return 0
    fi

    if [[ -z "${DISCOVERY_E2E_MODE:-}" ]]; then
        echo "[INFO] 未显式指定 discovery mode，e2e 将依次运行 CPU 和 GPU 两种模式。"
        if [[ "$#" -gt 0 ]]; then
            DISCOVERY_E2E_MODE=cpu just test e2e "$@"
            DISCOVERY_E2E_MODE=gpu just test e2e "$@"
        else
            DISCOVERY_E2E_MODE=cpu just test e2e
            DISCOVERY_E2E_MODE=gpu just test e2e
        fi
        return 0
    fi

    just test bootstrap
    discovery_ensure_test_ready
    test_database_url="$(discovery_resolve_test_database_url)"

    e2e_log_file="${test_e2e_log_file}"
    ensure_log_dir_for_file "${e2e_log_file}"
    e2e_host="${DISCOVERY_E2E_HOST:-127.0.0.1}"
    e2e_port="${DISCOVERY_E2E_PORT:-}"
    if [[ -z "${e2e_port}" ]]; then
        e2e_port="$(find_free_port)"
    fi
    e2e_base_url="http://${e2e_host}:${e2e_port}"
    e2e_mode="${DISCOVERY_E2E_MODE:-cpu}"
    e2e_health_url="${DISCOVERY_E2E_HEALTH_URL:-}"

    validate_test_e2e_base_url "${e2e_base_url}"

    if [[ -z "${e2e_health_url}" ]]; then
        e2e_health_url="${e2e_base_url}/acps-adp-v2/health"
    fi

    trap discovery_cleanup_e2e EXIT

    embedding_api_key="${DISCOVERY_E2E_EMBEDDING_API_KEY:-$(discovery_resolve_local_env_value EMBEDDING_API_KEY)}"
    embedding_base_url="${DISCOVERY_E2E_EMBEDDING_BASE_URL:-$(discovery_resolve_local_env_value EMBEDDING_BASE_URL)}"
    embedding_model_name="${DISCOVERY_E2E_EMBEDDING_MODEL_NAME:-$(discovery_resolve_local_env_value EMBEDDING_MODEL_NAME)}"
    embedding_model_path="${DISCOVERY_E2E_EMBEDDING_MODEL_PATH:-$(discovery_resolve_local_env_value EMBEDDING_MODEL_PATH)}"
    embedding_devices="${DISCOVERY_E2E_EMBEDDING_DEVICES:-$(discovery_resolve_local_env_value EMBEDDING_DEVICES)}"
    reranker_url="${DISCOVERY_E2E_RERANKER_URL:-$(discovery_resolve_local_env_value RERANKER_URL)}"
    discovery_llm_api_key="${DISCOVERY_E2E_DISCOVERY_LLM_API_KEY:-$(discovery_resolve_local_env_value DISCOVERY_LLM_API_KEY)}"
    discovery_llm_base_url="${DISCOVERY_E2E_DISCOVERY_LLM_BASE_URL:-$(discovery_resolve_local_env_value DISCOVERY_LLM_BASE_URL)}"
    discovery_llm_model_name="${DISCOVERY_E2E_DISCOVERY_LLM_MODEL_NAME:-$(discovery_resolve_local_env_value DISCOVERY_LLM_MODEL_NAME)}"

    if [[ -z "${embedding_api_key}" ]]; then
        embedding_api_key="e2e-test-key"
    fi
    if [[ -z "${embedding_base_url}" ]]; then
        embedding_base_url="http://127.0.0.1:9/v1"
    fi
    if [[ -z "${embedding_model_name}" ]]; then
        embedding_model_name="e2e-test-model"
    fi
    if [[ -z "${discovery_llm_api_key}" ]]; then
        discovery_llm_api_key="e2e-test-key"
    fi
    if [[ -z "${discovery_llm_base_url}" ]]; then
        discovery_llm_base_url="http://127.0.0.1:9/v1"
    fi
    if [[ -z "${discovery_llm_model_name}" ]]; then
        discovery_llm_model_name="e2e-test-discovery-model"
    fi

    : > "${e2e_log_file}"

    if [[ "${e2e_mode}" == "cpu" ]]; then
        discovery_e2e_pid="$(launch_detached "${e2e_log_file}" env \
            PYTHONPATH="$PWD" \
            APP_ENV=testing \
            DATABASE_URL="${test_database_url}" \
            UVICORN_HOST="${e2e_host}" \
            UVICORN_PORT="${e2e_port}" \
            DISCOVERY_MODE="${e2e_mode}" \
            EMBEDDING_API_KEY="${embedding_api_key}" \
            EMBEDDING_BASE_URL="${embedding_base_url}" \
            EMBEDDING_MODEL_NAME="${embedding_model_name}" \
            DISCOVERY_LLM_API_KEY="${discovery_llm_api_key}" \
            DISCOVERY_LLM_BASE_URL="${discovery_llm_base_url}" \
            DISCOVERY_LLM_MODEL_NAME="${discovery_llm_model_name}" \
            DSP_AUTO_START=false \
            FORWARDER_SERVER_ENABLED=false \
            DSP_BASE_URL="http://127.0.0.1:9/acps-dsp-v2" \
            POLLING_SERVER_URL="http://127.0.0.1:9" \
            uv run python -m app.main)"
    else
        discovery_e2e_pid="$(launch_detached "${e2e_log_file}" env \
            PYTHONPATH="$PWD" \
            APP_ENV=testing \
            DATABASE_URL="${test_database_url}" \
            UVICORN_HOST="${e2e_host}" \
            UVICORN_PORT="${e2e_port}" \
            DISCOVERY_MODE="${e2e_mode}" \
            EMBEDDING_MODEL_PATH="${embedding_model_path}" \
            EMBEDDING_DEVICES="${embedding_devices}" \
            RERANKER_URL="${reranker_url}" \
            DISCOVERY_LLM_API_KEY="${discovery_llm_api_key}" \
            DISCOVERY_LLM_BASE_URL="${discovery_llm_base_url}" \
            DISCOVERY_LLM_MODEL_NAME="${discovery_llm_model_name}" \
            DSP_AUTO_START=false \
            FORWARDER_SERVER_ENABLED=false \
            DSP_BASE_URL="http://127.0.0.1:9/acps-dsp-v2" \
            POLLING_SERVER_URL="http://127.0.0.1:9" \
            uv run python -m app.main)"
    fi
    discovery_e2e_started=1
    echo "[INFO] 已启动临时 e2e 测试实例：${e2e_base_url}（PID=${discovery_e2e_pid}，mode=${e2e_mode}）。"

    if ! run_uv_with_mutating_cache uv run python scripts/wait_for_http.py \
        "${e2e_health_url}" \
        --expect-status 200 \
        --expect-json-key status \
        --expect-json-value healthy; then
        echo "[ERROR] e2e 测试实例未能按时通过健康检查，请检查 ${e2e_log_file}。" >&2
        tail -n 200 "${e2e_log_file}" >&2 || true
        exit 1
    fi

    if [[ "$#" -gt 0 ]]; then
        TEST_DATABASE_URL="${test_database_url}" TEST_E2E_BASE_URL="${e2e_base_url}" DISCOVERY_E2E_BASE_URL="${e2e_base_url}" run_scoped_pytest tests/e2e "$@"
    else
        TEST_DATABASE_URL="${test_database_url}" TEST_E2E_BASE_URL="${e2e_base_url}" DISCOVERY_E2E_BASE_URL="${e2e_base_url}" run_scoped_pytest tests/e2e
    fi
}

supports_project_test_action() {
    local action="$1"

    case "${action}" in
        coverage-report)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}
