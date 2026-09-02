#!/usr/bin/env bash
# 采集应用发布装配包（app release assembly kit）。
#
# 
# 实现结果：装配包采集脚本已在当前发布包实现中落地。
#
# 职责边界：
# - 只读取 release/projects.toml 这份固定清单，不解析任何项目的 internal_wheels 声明。
# - 只检查兄弟项目目录是否存在，不执行 git clone/pull/submodule update 等 VCS 操作。
# - kind = "app" 执行该项目已声明的 `just package wheel`；kind = "shared-library" 只执行
# `uv build --wheel`。
# - 产出的装配包平台无关，只打一份，不按平台拆分。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# app-packaging 位于 release/app-packaging/；仓库根是再上两级。
ACPS_INFRA_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECTS_TOML="${SCRIPT_DIR}/projects.toml"
# 装配模板与本脚本同目录（扁平布局；采集时拷入 kit 的 assembly/）。
ASSEMBLY_SRC_DIR="${SCRIPT_DIR}"
RUNTIME_PACKAGE_TOOL="${ACPS_INFRA_DIR}/release/lib/runtime_package.py"

OUTPUT_DIR=""

usage() {
    cat <<'EOF'
用法：collect-app-release-kit.sh --output <dir>

按 release/projects.toml 固定项目清单采集应用薄包和内部共享库 wheel，产出平台无关的
应用发布装配包目录（packages/、assembly/、manifest.toml、checksums.txt）。

不会执行任何 git clone/pull 等源码获取动作；兄弟项目源码必须已经存在于约定路径下。
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output)
            shift
            if [[ $# -eq 0 ]]; then
                echo "[ERROR] --output 需要一个值" >&2
                exit 2
            fi
            OUTPUT_DIR="$1"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
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

if [[ ! -f "${PROJECTS_TOML}" ]]; then
    echo "[ERROR] 未找到固定项目清单：${PROJECTS_TOML}" >&2
    exit 1
fi

if [[ "${OUTPUT_DIR}" != /* ]]; then
    OUTPUT_DIR="${PWD}/${OUTPUT_DIR}"
fi

# --output 会被 `rm -rf` 清空重建；对明显危险的路径做最低限度的 sanity check，
# 防止误传空值/根目录/家目录等参数导致灾难性删除。
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

# 解析 projects.toml，输出 "id<TAB>path<TAB>kind<TAB>python_tags(逗号分隔)" 行。
parse_projects() {
    PROJECTS_TOML_PATH="${PROJECTS_TOML}" python3 - <<'PY'
import os
import tomllib
from pathlib import Path

path = Path(os.environ["PROJECTS_TOML_PATH"])
data = tomllib.loads(path.read_text(encoding="utf-8"))
projects = data.get("projects", [])

ids = [p.get("id", "") for p in projects]
if len(ids) != len(set(ids)):
    raise SystemExit(f"[ERROR] projects.toml 中存在重复 id：{ids}")

for project in projects:
    pid = project.get("id", "")
    ppath = project.get("path", "")
    kind = project.get("kind", "")
    if not pid or not ppath or kind not in ("app", "shared-library"):
        raise SystemExit(f"[ERROR] projects.toml 条目非法：{project}")
    tags = project.get("python_tags", []) or []
    print(f"{pid}\t{ppath}\t{kind}\t{','.join(tags)}")
PY
}

echo "=== ：检查兄弟项目目录存在性 ==="
missing=()
while IFS=$'\t' read -r pid ppath pkind ptags; do
    [[ -n "${pid}" ]] || continue
    resolved_path="${ACPS_INFRA_DIR}/${ppath}"
    if [[ ! -d "${resolved_path}" ]]; then
        missing+=("${pid} -> ${resolved_path}")
    fi
done < <(parse_projects)

if [[ "${#missing[@]}" -gt 0 ]]; then
    echo "[ERROR] 以下项目的兄弟目录不存在，采集中止：" >&2
    for m in "${missing[@]}"; do
        echo "  - ${m}" >&2
    done
    exit 1
fi
echo "  全部兄弟项目目录存在"

rm -rf "${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}/packages"

manifest_entries=()

echo "=== ：按固定清单逐项采集 ==="
while IFS=$'\t' read -r pid ppath pkind ptags; do
    [[ -n "${pid}" ]] || continue
    resolved_path="${ACPS_INFRA_DIR}/${ppath}"
    package_dir="${OUTPUT_DIR}/packages/${pid}"
    mkdir -p "${package_dir}"

    case "${pkind}" in
        app)
            echo "--- [app] ${pid} ---"
            # 应用侧 dist/ 不会被 package-wheel-runtime.sh 自动清空；若残留旧版本
            # *-wheel-*.tar.gz，下面按「唯一匹配项」取值会失败或误取。构建前先清空
            # 匹配该通配符的历史产物，构建后再校验恰好只剩一个。
            rm -f "${resolved_path}"/dist/*-wheel-*.tar.gz
            (cd "${resolved_path}" && just package wheel)

            thin_tar=""
            thin_tar_count=0
            for candidate in "${resolved_path}"/dist/*-wheel-*.tar.gz; do
                [[ -e "${candidate}" ]] || continue
                thin_tar="${candidate}"
                thin_tar_count=$((thin_tar_count + 1))
            done
            if [[ "${thin_tar_count}" -ne 1 ]]; then
                echo "[ERROR] ${pid} 的 dist/ 下匹配 *-wheel-*.tar.gz 的产物数量异常：${thin_tar_count}（应恰好 1 个，清理逻辑可能失效或本次构建产出了多份）" >&2
                exit 1
            fi

            tar -xzf "${thin_tar}" -C "${package_dir}" --strip-components=1

            if [[ ! -f "${package_dir}/runtime-package.toml" ]]; then
                echo "[ERROR] ${pid} 薄包缺少 runtime-package.toml" >&2
                exit 1
            fi
            python3 "${RUNTIME_PACKAGE_TOOL}" validate --path "${package_dir}/runtime-package.toml" --asset-root "${package_dir}"

            wheel_count="$(find "${package_dir}/dist" -maxdepth 1 -name '*.whl' 2>/dev/null | wc -l | tr -d ' ')"
            if [[ "${wheel_count}" != "1" ]]; then
                echo "[ERROR] ${pid} 的 dist/ 下 wheel 数量异常：${wheel_count}（应恰好 1 个）" >&2
                exit 1
            fi

            if [[ ! -f "${package_dir}/checksums.txt" ]]; then
                echo "[ERROR] ${pid} 薄包缺少 checksums.txt" >&2
                exit 1
            fi
            ;;
        shared-library)
            echo "--- [shared-library] ${pid} ---"
            build_dir="$(mktemp -d)"
            (cd "${resolved_path}" && uv build --wheel --out-dir "${build_dir}")

            wheel_count="$(find "${build_dir}" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')"
            if [[ "${wheel_count}" != "1" ]]; then
                echo "[ERROR] ${pid} 构建出的 wheel 数量异常：${wheel_count}（应恰好 1 个）" >&2
                rm -rf "${build_dir}"
                exit 1
            fi

            mkdir -p "${package_dir}/dist"
            cp "${build_dir}"/*.whl "${package_dir}/dist/"
            rm -rf "${build_dir}"
            ;;
        *)
            echo "[ERROR] 未知 kind：${pkind}（项目 ${pid}）" >&2
            exit 1
            ;;
    esac

    commit="unknown"
    if git -C "${resolved_path}" rev-parse HEAD >/dev/null 2>&1; then
        commit="$(git -C "${resolved_path}" rev-parse HEAD)"
    fi
    version="unknown"
    wheel_file="$(find "${package_dir}/dist" -maxdepth 1 -name '*.whl' | LC_ALL=C sort | head -n 1)"
    if [[ -n "${wheel_file}" ]]; then
        version="$(basename "${wheel_file}" | awk -F'-' '{print $2}')"
    fi
    manifest_entries+=("${pid}|${pkind}|${commit}|${version}")
done < <(parse_projects)

echo "=== ：拷贝 assembly/ 静态文件 ==="
if [[ ! -d "${ASSEMBLY_SRC_DIR}" ]]; then
    echo "[ERROR] 未找到 assembly/ 模板目录：${ASSEMBLY_SRC_DIR}" >&2
    exit 1
fi
mkdir -p "${OUTPUT_DIR}/assembly"
cp "${ASSEMBLY_SRC_DIR}/Dockerfile" "${OUTPUT_DIR}/assembly/Dockerfile"
cp "${ASSEMBLY_SRC_DIR}/assemble.sh" "${OUTPUT_DIR}/assembly/assemble.sh"
cp "${ASSEMBLY_SRC_DIR}/assemble-host.sh" "${OUTPUT_DIR}/assembly/assemble-host.sh"
cp "${ASSEMBLY_SRC_DIR}/images.lock" "${OUTPUT_DIR}/assembly/images.lock"
cp "${ASSEMBLY_SRC_DIR}/validate_app_release_package.py" "${OUTPUT_DIR}/assembly/validate_app_release_package.py"
cp "${ASSEMBLY_SRC_DIR}/audit_wheelhouse.py" "${OUTPUT_DIR}/assembly/audit_wheelhouse.py"
cp "${ASSEMBLY_SRC_DIR}/runtime_smoke.py" "${OUTPUT_DIR}/assembly/runtime_smoke.py"
cp "${ASSEMBLY_SRC_DIR}/assemble-and-validate.sh" "${OUTPUT_DIR}/assembly/assemble-and-validate.sh"
chmod +x \
    "${OUTPUT_DIR}/assembly/assemble.sh" \
    "${OUTPUT_DIR}/assembly/assemble-host.sh" \
    "${OUTPUT_DIR}/assembly/assemble-and-validate.sh"

echo "=== ：生成 manifest.toml ==="
assembler_version="unknown"
if git -C "${ACPS_INFRA_DIR}" rev-parse HEAD >/dev/null 2>&1; then
    assembler_version="$(git -C "${ACPS_INFRA_DIR}" rev-parse HEAD)"
fi

{
    echo "# 装配包自身元数据（应用最终发布包设计 §6.3）"
    echo "generated_at = \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
    echo "assembler_version = \"${assembler_version}\""
    echo ""
    for entry in "${manifest_entries[@]}"; do
        IFS='|' read -r pid pkind pcommit pversion <<< "${entry}"
        echo "[[projects]]"
        echo "id = \"${pid}\""
        echo "kind = \"${pkind}\""
        echo "version = \"${pversion}\""
        echo "source_commit = \"${pcommit}\""
        echo ""
    done
} > "${OUTPUT_DIR}/manifest.toml"

echo "=== ：生成 checksums.txt ==="
sha256_cmd=()
if command -v sha256sum >/dev/null 2>&1; then
    sha256_cmd=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
    sha256_cmd=(shasum -a 256)
else
    echo "[ERROR] 未找到 sha256sum 或 shasum 命令" >&2
    exit 1
fi

(
    cd "${OUTPUT_DIR}"
    # 注意：用 ! -path './checksums.txt' 而不是 ! -name 'checksums.txt'——按 basename
    # 过滤会把每个已采集应用自带的 packages/<id>/checksums.txt（同名嵌套文件）也一并
    # 排除掉，导致顶层 checksums.txt 覆盖不完整（与 assemble.sh 里已修复过的同类问题
    # 一致，见 ）。
    find . -type f ! -path './checksums.txt' -print0 \
        | LC_ALL=C sort -z \
        | xargs -0 "${sha256_cmd[@]}" > checksums.txt
)

echo "=== 采集完成 ==="
echo "  装配包目录：${OUTPUT_DIR}"
echo "  已采集项目数：${#manifest_entries[@]}"
