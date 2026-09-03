#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EDDSA_BOOTSTRAP_SCRIPT="${SCRIPT_DIR}/bootstrap-eddsa.sh"
KEYCLOAK_CONTAINER="${KEYCLOAK_CONTAINER:-dev-keycloak}"
KEYCLOAK_ADMIN="${KEYCLOAK_ADMIN:-admin}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-devpass}"
REGISTRY_WEB_BASE_URL="${REGISTRY_WEB_BASE_URL:-http://localhost:9001}"
MONITOR_WEB_BASE_URL="${MONITOR_WEB_BASE_URL:-http://localhost:9009}"
LEADER_WEB_BASE_URL="${LEADER_WEB_BASE_URL:-http://localhost:9030}"
REGISTRY_E2E_CLIENT_ID="${REGISTRY_E2E_CLIENT_ID:-registry-e2e}"
MONITOR_E2E_CLIENT_ID="${MONITOR_E2E_CLIENT_ID:-monitor-e2e}"
LEADER_E2E_CLIENT_ID="${LEADER_E2E_CLIENT_ID:-leader-e2e}"

log() {
    echo "[dev-keycloak-bootstrap] $*"
}

kcadm() {
    docker exec "${KEYCLOAK_CONTAINER}" /opt/keycloak/bin/kcadm.sh "$@"
}

login_admin() {
    local attempt=1
    local max_attempts=20

    while (( attempt <= max_attempts )); do
        if kcadm config credentials \
            --server http://127.0.0.1:8080 \
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

get_client_id_by_client_id() {
    local realm="$1"
    local client_name="$2"

    kcadm get clients -r "${realm}" -q clientId="${client_name}" | python3 -c '
import json
import sys

items = json.load(sys.stdin)
print(items[0]["id"] if items else "")
'
}

get_client_scope_id_by_name() {
    local realm="$1"
    local scope_name="$2"

    kcadm get client-scopes -r "${realm}" | python3 -c '
import json
import sys

scope_name = sys.argv[1]
for item in json.load(sys.stdin):
    if item.get("name") == scope_name:
        print(item.get("id", ""))
        break
' "${scope_name}"
}

get_client_mapper_id_by_name() {
    local realm="$1"
    local client_uuid="$2"
    local mapper_name="$3"

    kcadm get "clients/${client_uuid}/protocol-mappers/models" -r "${realm}" | python3 -c '
import json
import sys

mapper_name = sys.argv[1]
for item in json.load(sys.stdin):
    if item.get("name") == mapper_name:
        print(item.get("id", ""))
        break
' "${mapper_name}"
}

ensure_direct_grant_public_client() {
    local realm="$1"
    local client_name="$2"
    local client_uuid
    local current_client_json

    client_uuid="$(get_client_id_by_client_id "${realm}" "${client_name}")"
    if [[ -z "${client_uuid}" ]]; then
        log "create ${realm} direct-grant client ${client_name}" >&2
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
        client_uuid="$(get_client_id_by_client_id "${realm}" "${client_name}")"
    fi

    log "update ${realm} direct-grant client ${client_name}" >&2
    kcadm update "clients/${client_uuid}" -r "${realm}" \
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

    current_client_json="$(kcadm get "clients/${client_uuid}" -r "${realm}")"
    CURRENT_CLIENT_JSON="${current_client_json}" python3 - "${client_name}" <<'PY'
import json
import os
import sys

expected_client_id = sys.argv[1]
client = json.loads(os.environ["CURRENT_CLIENT_JSON"])

if client.get("clientId") != expected_client_id:
    raise SystemExit(f"unexpected clientId: {client.get('clientId')!r}")
if not client.get("publicClient"):
    raise SystemExit(f"{expected_client_id} must remain a public client")
if not client.get("directAccessGrantsEnabled"):
    raise SystemExit(f"{expected_client_id} must enable direct access grants")
PY

    printf '%s\n' "${client_uuid}"
}

ensure_client_has_basic_scope() {
    local realm="$1"
    local client_uuid="$2"
    local basic_scope_id

    basic_scope_id="$(get_client_scope_id_by_name "${realm}" "basic")"
    if [[ -n "${basic_scope_id}" ]]; then
        kcadm update "clients/${client_uuid}/default-client-scopes/${basic_scope_id}" -r "${realm}" >/dev/null || true
    fi
}

ensure_client_attribute_mapper() {
    local realm="$1"
    local client_uuid="$2"
    local mapper_name="$3"
    local user_attribute="$4"
    local claim_name="$5"
    local multivalued="$6"
    local mapper_id

    mapper_id="$(get_client_mapper_id_by_name "${realm}" "${client_uuid}" "${mapper_name}")"
    if [[ -z "${mapper_id}" ]]; then
        log "add ${realm}/${client_uuid} mapper ${mapper_name}"
        kcadm create "clients/${client_uuid}/protocol-mappers/models" -r "${realm}" \
            -s name="${mapper_name}" \
            -s protocol=openid-connect \
            -s protocolMapper=oidc-usermodel-attribute-mapper \
            -s consentRequired=false \
            -s "config.\"user.attribute\"=${user_attribute}" \
            -s "config.\"claim.name\"=${claim_name}" \
            -s 'config."jsonType.label"=String' \
            -s "config.\"multivalued\"=${multivalued}" \
            -s 'config."access.token.claim"=true' \
            -s 'config."id.token.claim"=false' \
            -s 'config."userinfo.token.claim"=false' >/dev/null
        return
    fi

    log "update ${realm}/${client_uuid} mapper ${mapper_name}"
    kcadm update "clients/${client_uuid}/protocol-mappers/models/${mapper_id}" -r "${realm}" \
        -s name="${mapper_name}" \
        -s protocol=openid-connect \
        -s protocolMapper=oidc-usermodel-attribute-mapper \
        -s consentRequired=false \
        -s "config.\"user.attribute\"=${user_attribute}" \
        -s "config.\"claim.name\"=${claim_name}" \
        -s 'config."jsonType.label"=String' \
        -s "config.\"multivalued\"=${multivalued}" \
        -s 'config."access.token.claim"=true' \
        -s 'config."id.token.claim"=false' \
        -s 'config."userinfo.token.claim"=false' >/dev/null
}

ensure_registry_e2e_client() {
    local realm="acps-registry"
    local client_uuid

    client_uuid="$(ensure_direct_grant_public_client "${realm}" "${REGISTRY_E2E_CLIENT_ID}")"
    ensure_client_has_basic_scope "${realm}" "${client_uuid}"
}

ensure_monitor_e2e_client() {
    local realm="acps-monitor"
    local client_uuid

    client_uuid="$(ensure_direct_grant_public_client "${realm}" "${MONITOR_E2E_CLIENT_ID}")"
    ensure_client_has_basic_scope "${realm}" "${client_uuid}"
    ensure_client_attribute_mapper "${realm}" "${client_uuid}" "tenant_id" "tenant_id" "tenant_id" "false"
    ensure_client_attribute_mapper "${realm}" "${client_uuid}" "allowed_aics" "allowed_aics" "allowed_aics" "true"
}

ensure_leader_e2e_client() {
    local realm="acps-leader"
    local client_uuid

    client_uuid="$(ensure_direct_grant_public_client "${realm}" "${LEADER_E2E_CLIENT_ID}")"
    ensure_client_has_basic_scope "${realm}" "${client_uuid}"
}

if [[ ! -x "${EDDSA_BOOTSTRAP_SCRIPT}" ]]; then
    echo "[dev-keycloak-bootstrap] missing bootstrap script: ${EDDSA_BOOTSTRAP_SCRIPT}" >&2
    exit 1
fi

KEYCLOAK_CONTAINER="${KEYCLOAK_CONTAINER}" \
KEYCLOAK_ADMIN="${KEYCLOAK_ADMIN}" \
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD}" \
REGISTRY_WEB_BASE_URL="${REGISTRY_WEB_BASE_URL}" \
MONITOR_WEB_BASE_URL="${MONITOR_WEB_BASE_URL}" \
LEADER_WEB_BASE_URL="${LEADER_WEB_BASE_URL}" \
bash "${EDDSA_BOOTSTRAP_SCRIPT}"

login_admin
ensure_registry_e2e_client
ensure_monitor_e2e_client
ensure_leader_e2e_client
log "Keycloak dev bootstrap completed"
