#!/usr/bin/env bash
# DEPRECATED: 短名映射已取消（简化决策 2026-07-23）。
# 请改用：./scripts/build-install-package.sh --image-dir … --app-release-dir …
set -euo pipefail
echo "[ERROR] ingest_image_artifacts.sh 已废弃（不再做长名→短名）。" >&2
echo "请改用：./scripts/build-install-package.sh --image-dir <IMAGE_OUT> --app-release-dir <APP_RELEASE_DIR> \\" >&2
echo "         --image-platform <linux-arm64|linux-amd64> --control-platform <…> --out-dir <dir>" >&2
exit 2
