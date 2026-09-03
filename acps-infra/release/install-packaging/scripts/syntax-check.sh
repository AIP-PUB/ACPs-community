#!/usr/bin/env bash
# Syntax-check gate for install-packaging.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${ROOT}/.venv-tools/bin/python"
AP="${ROOT}/.venv-tools/bin/ansible-playbook"
if [[ ! -x "$AP" ]]; then
  echo "[ERROR] missing .venv-tools ansible-playbook; create with:" >&2
  echo "  python3 -m venv .venv-tools && .venv-tools/bin/pip install 'ansible-core>=2.16,<2.19'" >&2
  exit 1
fi

export ANSIBLE_CONFIG="${ROOT}/ansible/ansible.cfg"
INV="${ROOT}/ansible/inventories/hosts.example.yml"

"$AP" --syntax-check "${ROOT}/ansible/playbooks/site.yml" -i "$INV"
"$AP" --syntax-check "${ROOT}/ansible/playbooks/preflight.yml" -i "$INV"
"$AP" --syntax-check "${ROOT}/ansible/playbooks/smoke.yml" -i "$INV"
"$AP" --syntax-check "${ROOT}/ansible/playbooks/business.yml" -i "$INV"

"$AP" --syntax-check "${ROOT}/ansible/playbooks/renew-certs.yml" -i "$INV"
"$AP" --syntax-check "${ROOT}/ansible/playbooks/refresh-trust-bundle.yml" -i "$INV"
"$AP" --syntax-check "${ROOT}/ansible/playbooks/register-state.yml" -i "$INV"
"$AP" --syntax-check "${ROOT}/ansible/playbooks/upgrade.yml" -i "$INV" \
  -e acps_upgrade_components=registry_server
"$AP" --syntax-check "${ROOT}/ansible/playbooks/rollback.yml" -i "$INV" \
  -e acps_rollback_components=registry_server

"${ROOT}/scripts/assert-baseline-image-alignment.sh"
"${ROOT}/scripts/selftest_rewrite_acs_endpoints.sh"
echo "syntax-check OK"
