#!/usr/bin/env bash
# DEPRECATED: 请改用 build-install-package.sh（简化决策 2026-07-23）。
#
# 旧路径：短名映射到 artifacts/ 再 assemble。新产品路径直接从 IMAGE_OUT + 应用发布包打 tar。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

has_image_dir=0
has_app_release=0
for a in "$@"; do
  [[ "$a" == "--image-dir" ]] && has_image_dir=1
  [[ "$a" == "--app-release-dir" ]] && has_app_release=1
done

if [[ "${has_image_dir}" -eq 1 && "${has_app_release}" -eq 1 ]]; then
  echo "[WARN] materialize_artifacts.sh：请改用 build-install-package.sh（本调用将转发）" >&2
  exec bash "${SCRIPT_DIR}/build-install-package.sh" "$@"
fi

echo "[ERROR] materialize_artifacts.sh 已废弃。" >&2
echo "请改用：./scripts/build-install-package.sh --image-dir <IMAGE_OUT> --app-release-dir <APP_RELEASE_DIR> \\" >&2
echo "         --image-platform <linux-arm64|linux-amd64> --control-platform <…> --out-dir <dir>" >&2
echo "（本树不再做短名映射，也不在安装阶段 pull/save demo-nginx / fluent-bit。）" >&2
exit 2
