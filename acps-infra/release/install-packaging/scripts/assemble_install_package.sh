#!/usr/bin/env bash
# Deprecated entrypoint: prefer build-install-package.sh (简化决策 2026-07-23).
#
# If --image-dir and --app-release-dir are passed, delegates to build-install-package.sh.
# Legacy mode (pre-staged artifacts/images + artifacts/control) still packs those trees
# with long-name archives — no short-name rename.
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
  echo "[WARN] assemble_install_package.sh：请改用 build-install-package.sh（本调用将转发）" >&2
  exec bash "${SCRIPT_DIR}/build-install-package.sh" "$@"
fi

# Legacy: require pre-populated artifacts/ with long-name images + control CLI
echo "[WARN] assemble_install_package.sh legacy mode：请迁移到 build-install-package.sh" >&2
exec bash "${SCRIPT_DIR}/build-install-package.sh" \
  --image-dir "${ROOT}/artifacts/images" \
  --app-release-dir "${ROOT}/artifacts/control" \
  "$@"
