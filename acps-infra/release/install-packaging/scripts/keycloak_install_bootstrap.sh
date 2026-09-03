#!/usr/bin/env bash
# Post-import Keycloak install bootstrap: EdDSA + install Direct Access clients + convenience users.
# Extends keycloak_bootstrap_eddsa.sh with install-layer accounts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EDDSA_SCRIPT="${SCRIPT_DIR}/keycloak_bootstrap_eddsa.sh"

KEYCLOAK_HOME="${KEYCLOAK_HOME:-}"
KEYCLOAK_CONTAINER="${KEYCLOAK_CONTAINER:-}"
KEYCLOAK_ADMIN_URL="${KEYCLOAK_ADMIN_URL:-http://127.0.0.1:8080}"
KEYCLOAK_ADMIN="${KEYCLOAK_ADMIN:?}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:?}"

if [[ -z "${KEYCLOAK_HOME}" && -z "${KEYCLOAK_CONTAINER}" ]]; then
  echo "[keycloak-install-bootstrap] KEYCLOAK_HOME or KEYCLOAK_CONTAINER is required" >&2
  exit 1
fi

REGISTRY_ADMIN_USERNAME="${REGISTRY_ADMIN_USERNAME:-admin}"
REGISTRY_ADMIN_PASSWORD="${REGISTRY_ADMIN_PASSWORD:?}"
REGISTRY_USER_USERNAME="${REGISTRY_USER_USERNAME:-installer}"
REGISTRY_USER_PASSWORD="${REGISTRY_USER_PASSWORD:?}"
MONITOR_ADMIN_USERNAME="${MONITOR_ADMIN_USERNAME:-monitor-admin}"
MONITOR_ADMIN_PASSWORD="${MONITOR_ADMIN_PASSWORD:-${REGISTRY_ADMIN_PASSWORD}}"

REGISTRY_INSTALL_CLIENT_ID="${REGISTRY_INSTALL_CLIENT_ID:-registry-install}"
MONITOR_INSTALL_CLIENT_ID="${MONITOR_INSTALL_CLIENT_ID:-monitor-install}"
LEADER_INSTALL_CLIENT_ID="${LEADER_INSTALL_CLIENT_ID:-leader-install}"
LEADER_USER_USERNAME="${LEADER_USER_USERNAME:-leader-user}"
LEADER_USER_PASSWORD="${LEADER_USER_PASSWORD:-demo123}"

export KEYCLOAK_HOME KEYCLOAK_CONTAINER KEYCLOAK_ADMIN_URL KEYCLOAK_ADMIN KEYCLOAK_ADMIN_PASSWORD
export REGISTRY_WEB_BASE_URL="${REGISTRY_WEB_BASE_URL:-http://127.0.0.1:9001}"
export MONITOR_WEB_BASE_URL="${MONITOR_WEB_BASE_URL:-http://127.0.0.1:9009}"
export LEADER_WEB_BASE_URL="${LEADER_WEB_BASE_URL:-http://127.0.0.1:9030}"

log() { echo "[keycloak-install-bootstrap] $*"; }

kcadm() {
  if [[ -n "${KEYCLOAK_HOME}" ]]; then
    "${KEYCLOAK_HOME}/bin/kcadm.sh" "$@"
  else
    docker exec "${KEYCLOAK_CONTAINER}" /opt/keycloak/bin/kcadm.sh "$@"
  fi
}

login_admin() {
  local attempt=1
  while (( attempt <= 30 )); do
    if kcadm config credentials \
      --server "${KEYCLOAK_ADMIN_URL}" \
      --realm master \
      --user "${KEYCLOAK_ADMIN}" \
      --password "${KEYCLOAK_ADMIN_PASSWORD}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
    attempt=$((attempt + 1))
  done
  log "failed to authenticate to Keycloak admin API"
  return 1
}

get_client_uuid() {
  local realm="$1" client_name="$2"
  kcadm get clients -r "${realm}" -q clientId="${client_name}" | python3 -c '
import json,sys
items=json.load(sys.stdin)
print(items[0]["id"] if items else "")
'
}

get_user_id() {
  local realm="$1" username="$2"
  kcadm get users -r "${realm}" -q username="${username}" | python3 -c '
import json,sys
uname=sys.argv[1]
items=json.load(sys.stdin)
for u in items:
  if u.get("username")==uname:
    print(u.get("id",""))
    break
' "${username}"
}

ensure_direct_grant_client() {
  local realm="$1" client_name="$2" audience_client="$3"
  local client_uuid
  client_uuid="$(get_client_uuid "${realm}" "${client_name}")"
  if [[ -z "${client_uuid}" ]]; then
    log "create ${realm} install client ${client_name}"
    kcadm create clients -r "${realm}" \
      -s clientId="${client_name}" \
      -s name="${client_name}" \
      -s enabled=true \
      -s protocol=openid-connect \
      -s publicClient=true \
      -s directAccessGrantsEnabled=true \
      -s standardFlowEnabled=false \
      -s serviceAccountsEnabled=false \
      -s fullScopeAllowed=false \
      -s consentRequired=false \
      -s 'defaultClientScopes=["basic","roles","profile","email"]' >/dev/null
    client_uuid="$(get_client_uuid "${realm}" "${client_name}")"
  else
    kcadm update "clients/${client_uuid}" -r "${realm}" \
      -s enabled=true \
      -s publicClient=true \
      -s directAccessGrantsEnabled=true \
      -s standardFlowEnabled=false \
      -s fullScopeAllowed=false \
      -s 'defaultClientScopes=["basic","roles","profile","email"]' >/dev/null
  fi

  # Audience mapper → API client
  local mapper_id
  mapper_id="$(kcadm get "clients/${client_uuid}/protocol-mappers/models" -r "${realm}" | python3 -c '
import json,sys
name=sys.argv[1]
for m in json.load(sys.stdin):
  if m.get("name")==name:
    print(m.get("id",""))
    break
' "audience-${audience_client}" || true)"
  if [[ -z "${mapper_id}" ]]; then
    kcadm create "clients/${client_uuid}/protocol-mappers/models" -r "${realm}" \
      -s name="audience-${audience_client}" \
      -s protocol=openid-connect \
      -s protocolMapper=oidc-audience-mapper \
      -s consentRequired=false \
      -s "config.\"included.client.audience\"=${audience_client}" \
      -s 'config."id.token.claim"=false' \
      -s 'config."access.token.claim"=true' >/dev/null || true
  fi
  printf '%s\n' "${client_uuid}"
}

ensure_user_with_client_role() {
  local realm="$1" username="$2" password="$3" api_client="$4" role_name="$5"
  local uid client_uuid
  uid="$(get_user_id "${realm}" "${username}")"
  if [[ -z "${uid}" ]]; then
    log "create user ${realm}/${username}"
    kcadm create users -r "${realm}" \
      -s username="${username}" \
      -s enabled=true \
      -s emailVerified=true \
      -s "email=${username}@acps.local" \
      -s "firstName=${username}" \
      -s lastName=acps \
      -s 'requiredActions=[]' >/dev/null
    uid="$(get_user_id "${realm}" "${username}")"
  else
    kcadm update "users/${uid}" -r "${realm}" \
      -s enabled=true \
      -s emailVerified=true \
      -s "firstName=${username}" \
      -s lastName=acps \
      -s 'requiredActions=[]' >/dev/null
  fi
  kcadm set-password -r "${realm}" --userid "${uid}" --new-password "${password}" --temporary=false >/dev/null
  # Clear any post-password required actions (Keycloak may attach UPDATE_PASSWORD).
  kcadm update "users/${uid}" -r "${realm}" -s 'requiredActions=[]' -s emailVerified=true >/dev/null
  client_uuid="$(get_client_uuid "${realm}" "${api_client}")"
  if [[ -n "${client_uuid}" ]]; then
    kcadm add-roles -r "${realm}" --uid "${uid}" --cclientid "${api_client}" --rolename "${role_name}" >/dev/null || true
  fi
}

log "running EdDSA / client-scope bootstrap"
bash "${EDDSA_SCRIPT}" acps-registry acps-monitor acps-leader

login_admin

log "ensure install Direct Access clients"
ensure_direct_grant_client acps-registry "${REGISTRY_INSTALL_CLIENT_ID}" registry-api >/dev/null
ensure_direct_grant_client acps-monitor "${MONITOR_INSTALL_CLIENT_ID}" monitor-api >/dev/null
ensure_direct_grant_client acps-leader "${LEADER_INSTALL_CLIENT_ID}" leader-api >/dev/null

log "ensure convenience users (match secrets usernames; )"
# Registry: admin + client (bootstrap user)
ensure_user_with_client_role acps-registry "${REGISTRY_ADMIN_USERNAME}" "${REGISTRY_ADMIN_PASSWORD}" registry-api ADMIN
ensure_user_with_client_role acps-registry "${REGISTRY_USER_USERNAME}" "${REGISTRY_USER_PASSWORD}" registry-api CLIENT
# Also keep realm-imported demo users password-aligned if present
for u in registry-admin registry-client registry-staff; do
  if [[ -n "$(get_user_id acps-registry "${u}")" ]]; then
    case "${u}" in
      registry-admin) ensure_user_with_client_role acps-registry "${u}" "${REGISTRY_ADMIN_PASSWORD}" registry-api ADMIN ;;
      registry-staff) ensure_user_with_client_role acps-registry "${u}" "${REGISTRY_ADMIN_PASSWORD}" registry-api STAFF ;;
      registry-client) ensure_user_with_client_role acps-registry "${u}" "${REGISTRY_USER_PASSWORD}" registry-api CLIENT ;;
    esac
  fi
done

ensure_user_with_client_role acps-monitor "${MONITOR_ADMIN_USERNAME}" "${MONITOR_ADMIN_PASSWORD}" monitor-api admin
if [[ -n "$(get_user_id acps-monitor monitor-admin)" ]]; then
  ensure_user_with_client_role acps-monitor monitor-admin "${MONITOR_ADMIN_PASSWORD}" monitor-api admin
fi

# Leader realm: install Direct Access client + demo user for business acceptance B/C.
ensure_user_with_client_role acps-leader "${LEADER_USER_USERNAME}" "${LEADER_USER_PASSWORD}" leader-api user
for u in leader-user leader-operator leader-admin; do
  if [[ -n "$(get_user_id acps-leader "${u}")" ]]; then
    case "${u}" in
      leader-user) ensure_user_with_client_role acps-leader "${u}" "${LEADER_USER_PASSWORD}" leader-api user ;;
      leader-operator) ensure_user_with_client_role acps-leader "${u}" "${LEADER_USER_PASSWORD}" leader-api operator ;;
      leader-admin) ensure_user_with_client_role acps-leader "${u}" "${LEADER_USER_PASSWORD}" leader-api admin ;;
    esac
  fi
done

verify_access_token_lifespans() {
  local realm want="${KEYCLOAK_ACCESS_TOKEN_LIFESPAN_SECONDS:-1800}" current failed=0
  for realm in acps-registry acps-monitor acps-leader; do
    current="$(kcadm get "realms/${realm}" | python3 -c '
import json, sys
v = json.load(sys.stdin).get("accessTokenLifespan")
print("" if v is None else int(v))
')"
    if [[ -z "${current}" || ! "${current}" =~ ^[0-9]+$ || "${current}" -lt "${want}" ]]; then
      log "VERIFY FAIL: ${realm} accessTokenLifespan=${current:-unset} < ${want}"
      failed=1
    else
      log "VERIFY OK: ${realm} accessTokenLifespan=${current}"
    fi
  done
  if [[ "${failed}" -ne 0 ]]; then
    return 1
  fi
}

log "verify access token lifespan (>= ${KEYCLOAK_ACCESS_TOKEN_LIFESPAN_SECONDS:-1800}s on all install realms)"
verify_access_token_lifespans

log "keycloak install bootstrap complete"
