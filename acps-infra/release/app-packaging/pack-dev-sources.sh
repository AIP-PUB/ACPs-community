#!/usr/bin/env bash
# 开发态工作区源代码快照（方案 1）。
# 用途：把本机「含未提交/未跟踪改动」的工作区打成一份源码包，传到远端构建机做
# 应用发布包等验证。语义是磁盘快照，不是 git archive / clone；包内不含 .git。
#
# 包含范围：
# 1. acps-infra 本仓（打包脚本所在仓）
# 2. release/app-packaging/projects.toml 列出的全部兄弟项目
#
# 归档内目录布局（解压后相对路径与本机 sibling 约定一致）：
#   <bundle>/
#     acps-infra/
#     registry-server/
#     ...
#     MANIFEST.txt
#
# 远端解压后可从 <bundle>/acps-infra 直接跑：
#   ./release/app-packaging/build-app-release-packages.sh ...

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACPS_INFRA_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECTS_TOML="${SCRIPT_DIR}/projects.toml"
OUTPUT_PATH=""
WORK_DIR=""
KEEP_WORK_DIR=0

usage() {
    cat <<'EOF'
用法：pack-dev-sources.sh --output <file.tar.gz> [选项]

按 projects.toml + acps-infra 对本机工作区做快照，产出单个 tar.gz（不含 .git）。
包含未暂存、已暂存与未跟踪文件（受排除规则约束）。

选项：
  --output <file.tar.gz>  最终归档路径（父目录须已存在；同名文件会被覆盖）
  --work-dir <dir>        暂存目录；默认 mktemp，结束后删除
  --keep-work-dir         保留暂存目录（调试用）
  -h, --help              打印帮助

排除（任意深度，见脚本 RSYNC_EXCLUDES）：
  .git、虚拟环境、dist/build/.build/artifacts、logs/tmp/temp、
  .uv-cache*、真实 .env / .env.local（保留 .env.example）、keyfiles、常见 IDE/OS 噪声等
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output)
            shift
            OUTPUT_PATH="${1:-}"
            ;;
        --work-dir)
            shift
            WORK_DIR="${1:-}"
            ;;
        --keep-work-dir)
            KEEP_WORK_DIR=1
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

if [[ -z "${OUTPUT_PATH}" ]]; then
    echo "[ERROR] 必须提供 --output <file.tar.gz>" >&2
    usage >&2
    exit 2
fi

if [[ ! -f "${PROJECTS_TOML}" ]]; then
    echo "[ERROR] 未找到固定项目清单：${PROJECTS_TOML}" >&2
    exit 1
fi

if [[ "${OUTPUT_PATH}" != /* ]]; then
    OUTPUT_PATH="${PWD}/${OUTPUT_PATH}"
fi

case "${OUTPUT_PATH}" in
    *.tar.gz|*.tgz) ;;
    *)
        echo "[ERROR] --output 须以 .tar.gz 或 .tgz 结尾：${OUTPUT_PATH}" >&2
        exit 2
        ;;
esac

output_parent="$(dirname "${OUTPUT_PATH}")"
if [[ ! -d "${output_parent}" ]]; then
    echo "[ERROR] --output 父目录不存在：${output_parent}" >&2
    exit 2
fi

if ! command -v rsync >/dev/null 2>&1; then
    echo "[ERROR] 需要 rsync（用于带排除规则的工作区拷贝）" >&2
    exit 1
fi

# 解析 projects.toml → "id<TAB>abs_path" 行（不含 acps-infra）。
parse_projects() {
    PROJECTS_TOML_PATH="${PROJECTS_TOML}" ACPS_INFRA_DIR_PATH="${ACPS_INFRA_DIR}" python3 - <<'PY'
import os
import tomllib
from pathlib import Path

infra = Path(os.environ["ACPS_INFRA_DIR_PATH"]).resolve()
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
    resolved = (infra / ppath).resolve()
    print(f"{pid}\t{resolved}")
PY
}

# rsync 排除：不把本机构建产物 / 密钥 / VCS / 本地缓存带上远端。
# 模式匹配任意深度目录名（rsync 无前导 / 的 exclude 对路径各段生效）。
RSYNC_EXCLUDES=(
    --exclude='.git/'
    --exclude='.git'
    --exclude='.venv/'
    --exclude='venv/'
    --exclude='.package-build-venv/'
    --exclude='.venv-tools/'
    --exclude='env/'
    --exclude='ENV/'
    --exclude='dist/'
    --exclude='build/'
    --exclude='.build/'
    --exclude='artifacts/'
    --exclude='*.egg-info/'
    --exclude='__pycache__/'
    --exclude='.pytest_cache/'
    --exclude='.mypy_cache/'
    --exclude='.ruff_cache/'
    --exclude='.tox/'
    --exclude='.uv-cache/'
    --exclude='.uv-cache-*/'
    --exclude='htmlcov/'
    --exclude='.coverage'
    --exclude='.coverage.*'
    --exclude='logs/'
    --exclude='tmp/'
    --exclude='temp/'
    # 只排除真实本地密钥环境文件；保留 .env.example（package 必需模板）
    --exclude='.env'
    --exclude='.env.local'
    --exclude='.env.*.local'
    --exclude='keyfiles/'
    --exclude='node_modules/'
    --exclude='.DS_Store'
    --exclude='.idea/'
    --exclude='.vscode/'
    --exclude='*.pyc'
    --exclude='.cursor/'
    --exclude='.vendor-bundle/'
)

snapshot_repo() {
    local name="$1"
    local src="$2"
    local dest="$3"
    if [[ ! -d "${src}" ]]; then
        echo "[ERROR] 项目目录不存在：${name} -> ${src}" >&2
        return 1
    fi
    mkdir -p "${dest}"
    # trailing slash：拷贝目录内容到 dest（dest 即项目根名）
    rsync -a \
        "${RSYNC_EXCLUDES[@]}" \
        "${src}/" \
        "${dest}/"
}

git_summary_line() {
    local src="$1"
    if [[ ! -d "${src}/.git" ]]; then
        echo "not-a-git-repo"
        return 0
    fi
    local branch head dirty untracked
    branch="$(git -C "${src}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    head="$(git -C "${src}" rev-parse --short HEAD 2>/dev/null || echo '?')"
    if git -C "${src}" diff --quiet && git -C "${src}" diff --cached --quiet; then
        dirty="clean-tracked"
    else
        dirty="dirty-tracked"
    fi
    if [[ -n "$(git -C "${src}" ls-files --others --exclude-standard 2>/dev/null || true)" ]]; then
        untracked="has-untracked"
    else
        untracked="no-untracked"
    fi
    echo "branch=${branch} head=${head} ${dirty} ${untracked}"
}

OWNED_WORK_DIR=0
if [[ -z "${WORK_DIR}" ]]; then
    WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/acps-dev-sources.XXXXXX")"
    OWNED_WORK_DIR=1
else
    if [[ "${WORK_DIR}" != /* ]]; then
        WORK_DIR="${PWD}/${WORK_DIR}"
    fi
    mkdir -p "${WORK_DIR}"
fi

cleanup() {
    if [[ "${OWNED_WORK_DIR}" -eq 1 && "${KEEP_WORK_DIR}" -eq 0 ]]; then
        rm -rf "${WORK_DIR}"
    fi
}
trap cleanup EXIT

STAMP="$(date +%Y%m%d-%H%M%S)"
BUNDLE_NAME="acps-app-release-dev-sources-${STAMP}"
STAGE="${WORK_DIR}/${BUNDLE_NAME}"
mkdir -p "${STAGE}"

echo "=== 开发态源码快照 ==="
echo "  acps-infra: ${ACPS_INFRA_DIR}"
echo "  projects:   ${PROJECTS_TOML}"
echo "  stage:      ${STAGE}"
echo "  output:     ${OUTPUT_PATH}"

# 收集清单：acps-infra 固定第一项，再跟 projects.toml。
declare -a SNAPSHOT_IDS=()
declare -a SNAPSHOT_PATHS=()
SNAPSHOT_IDS+=("acps-infra")
SNAPSHOT_PATHS+=("${ACPS_INFRA_DIR}")

declare -a missing=()
while IFS=$'\t' read -r pid abs_path; do
    [[ -n "${pid}" ]] || continue
    if [[ "${pid}" == "acps-infra" ]]; then
        echo "[ERROR] projects.toml 不应再声明 acps-infra（本脚本已固定收录）" >&2
        exit 1
    fi
    SNAPSHOT_IDS+=("${pid}")
    SNAPSHOT_PATHS+=("${abs_path}")
    if [[ ! -d "${abs_path}" ]]; then
        missing+=("${pid} -> ${abs_path}")
    fi
done < <(parse_projects)

if [[ "${#missing[@]}" -gt 0 ]]; then
    echo "[ERROR] 以下兄弟项目目录不存在：" >&2
    for item in "${missing[@]}"; do
        echo "  - ${item}" >&2
    done
    exit 1
fi

MANIFEST="${STAGE}/MANIFEST.txt"
{
    echo "ACPs app-release 开发态源码快照"
    echo "created_at=${STAMP}"
    echo "host=$(uname -s)-$(uname -m)"
    echo "source_acps_infra=${ACPS_INFRA_DIR}"
    echo "projects_toml=${PROJECTS_TOML}"
    echo "note=working-tree snapshot (includes uncommitted/untracked; excludes .git and build noise)"
    echo
    echo "projects:"
} > "${MANIFEST}"

echo "=== 拷贝工作区 ==="
for i in "${!SNAPSHOT_IDS[@]}"; do
    pid="${SNAPSHOT_IDS[$i]}"
    src="${SNAPSHOT_PATHS[$i]}"
    dest="${STAGE}/${pid}"
    summary="$(git_summary_line "${src}")"
    echo "  - ${pid} (${summary})"
    echo "  - ${pid} path=${src} ${summary}" >> "${MANIFEST}"
    snapshot_repo "${pid}" "${src}" "${dest}"
done

{
    echo
    echo "layout:"
    echo "  解压后进入 ${BUNDLE_NAME}/，兄弟目录与本机一致；"
    echo "  在 acps-infra/ 下执行 release/app-packaging/build-app-release-packages.sh 即可。"
} >> "${MANIFEST}"

echo "=== 打包 ==="
# macOS 避免把 ._ 资源叉写入归档
COPYFILE_DISABLE=1 tar -czf "${OUTPUT_PATH}" -C "${WORK_DIR}" "${BUNDLE_NAME}"

echo "=== 完成 ==="
echo "  ${OUTPUT_PATH}"
ls -lh "${OUTPUT_PATH}"
echo "  项目数：${#SNAPSHOT_IDS[@]}"
if [[ "${KEEP_WORK_DIR}" -eq 1 ]]; then
    echo "  暂存目录已保留：${WORK_DIR}"
fi
