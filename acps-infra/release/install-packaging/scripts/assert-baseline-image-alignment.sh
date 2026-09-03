#!/usr/bin/env bash
# CI/local gate: host baseline-matrix.toml ↔ image-inputs.lock (+ release-manifest).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ROOT}/.venv-tools/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi
exec "$PY" "${ROOT}/scripts/assert_baseline_image_alignment.py" "$@"
