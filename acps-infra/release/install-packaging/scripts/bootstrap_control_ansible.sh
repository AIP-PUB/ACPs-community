#!/usr/bin/env bash
# 控制节点：安装满足 ACPs 预检的 ansible-core（≥2.16），并绑定带 tomllib 的 CPython。
# 须在控制节点、解包安装包后、跑任何 playbook 之前执行（预检本身也依赖 ansible）。
#
# 环境变量（可选）：
#   ACPS_ANSIBLE_MIN_VERSION      默认 2.16
#   ACPS_ANSIBLE_MAX_VERSION_EXCL 默认 2.19（上界不含）
#   ACPS_ANSIBLE_PYTHON           默认 3.14
set -euo pipefail

MIN_CORE="${ACPS_ANSIBLE_MIN_VERSION:-2.16}"
MAX_CORE_EXCL="${ACPS_ANSIBLE_MAX_VERSION_EXCL:-2.19}"
PY_VER="${ACPS_ANSIBLE_PYTHON:-3.14}"

log() { echo "[bootstrap-control-ansible] $*"; }
die() { echo "[bootstrap-control-ansible] ERROR: $*" >&2; exit 1; }

if ! command -v uv >/dev/null 2>&1; then
  die "uv not found. Install: https://docs.astral.sh/uv/ then re-run."
fi

log "Installing ansible-core>=${MIN_CORE},<${MAX_CORE_EXCL} on CPython ${PY_VER} via uv tool ..."
uv tool install --force --python "${PY_VER}" "ansible-core>=${MIN_CORE},<${MAX_CORE_EXCL}"

export PATH="${HOME}/.local/bin:${PATH}"
command -v ansible-playbook >/dev/null 2>&1 || die "ansible-playbook not on PATH (expected ~/.local/bin)"

log "$(ansible-playbook --version | head -1)"

# 与 ansible-playbook 同环境的解释器（uv tools 目录下的 python）
TOOL_ROOT="${HOME}/.local/share/uv/tools/ansible-core"
ANSIBLE_PY=""
if [[ -x "${TOOL_ROOT}/bin/python" ]]; then
  ANSIBLE_PY="${TOOL_ROOT}/bin/python"
elif [[ -x "${TOOL_ROOT}/bin/python3" ]]; then
  ANSIBLE_PY="${TOOL_ROOT}/bin/python3"
else
  AP="$(command -v ansible-playbook)"
  if [[ -f "${AP}" ]] && head -1 "${AP}" | grep -q '^#!'; then
    ANSIBLE_PY="$(head -1 "${AP}" | sed 's|^#![[:space:]]*||; s|[[:space:]].*||')"
  else
    ANSIBLE_PY="$(uv python find "${PY_VER}")"
  fi
fi

[[ -n "${ANSIBLE_PY}" && -x "${ANSIBLE_PY}" ]] || die "could not resolve Ansible Python at ${ANSIBLE_PY:-<empty>}"
log "Ansible Python: ${ANSIBLE_PY}"

VER="$("${ANSIBLE_PY}" -c 'import tomllib, sys; print(sys.version.split()[0])')" \
  || die "tomllib missing (need CPython >=3.11). Re-run with ACPS_ANSIBLE_PYTHON=3.14"

log "tomllib OK (Python ${VER})"
log "Ready. Ensure PATH includes ${HOME}/.local/bin before system ansible."
echo "OK"
