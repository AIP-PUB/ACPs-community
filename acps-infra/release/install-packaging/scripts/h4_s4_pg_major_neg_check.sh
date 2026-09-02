#!/usr/bin/env bash
# H4 Step 4 负向门控：注入 PG major 漂移 → check_postgresql_os_major 必须拒绝。
# 日志：/tmp/acps-h4-s4-pg-major-neg.log（可用 ACPS_H4_S4_LOG 覆盖）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOG="${ACPS_H4_S4_LOG:-/tmp/acps-h4-s4-pg-major-neg.log}"
AP="${ROOT}/.venv-tools/bin/ansible-playbook"
if [[ ! -x "$AP" ]]; then
  echo "[ERROR] missing .venv-tools ansible-playbook; create with:" >&2
  echo "  python3 -m venv .venv-tools && .venv-tools/bin/pip install 'ansible-core>=2.16,<2.19'" >&2
  exit 1
fi

export ANSIBLE_CONFIG="${ROOT}/ansible/ansible.cfg"
INV="${ROOT}/ansible/inventories/hosts.example.yml"
PLAY="${ROOT}/ansible/playbooks/_h4_s4_pg_major_neg.yml"

{
  echo "=== H4 S4 PG major negative gate $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  echo "play=$PLAY"
  echo "log=$LOG"
  "$AP" -i "$INV" "$PLAY" -v
  echo "=== PASS ==="
} 2>&1 | tee "$LOG"

echo "h4_s4_pg_major_neg_check OK (log=$LOG)"
