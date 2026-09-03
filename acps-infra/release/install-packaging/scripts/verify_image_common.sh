#!/usr/bin/env bash
# gate: load fixture image (optional), normalize fake certs, assert modes/owner path.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ROOT}/.venv-tools/bin/python"
export PYTHONPATH="${ROOT}/ansible/filter_plugins:${ROOT}/scripts:${PYTHONPATH:-}"

"$PY" -m pytest "${ROOT}/tests" -q
echo "verify_image_common / unit tests OK"
