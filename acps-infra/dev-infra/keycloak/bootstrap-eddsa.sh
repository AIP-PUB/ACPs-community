#!/usr/bin/env bash
set -euo pipefail

KEYCLOAK_CONTAINER="${KEYCLOAK_CONTAINER:-dev-keycloak}"
KEYCLOAK_ADMIN="${KEYCLOAK_ADMIN:-admin}"
: "${KEYCLOAK_ADMIN_PASSWORD:?KEYCLOAK_ADMIN_PASSWORD is required}"

if [[ "$#" -gt 0 ]]; then
    REALMS=("$@")
else
    REALMS=(acps-registry acps-monitor acps-leader)
fi

log() {
    echo "[keycloak-bootstrap] $*"
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

get_realm_id() {
    local realm="$1"
    kcadm get "realms/${realm}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])'
}

get_eddsa_component_id() {
    local realm="$1"
    kcadm get components -r "${realm}" | python3 -c '
import json
import sys

for item in json.load(sys.stdin):
    if item.get("providerId") == "eddsa-generated" and item.get("providerType") == "org.keycloak.keys.KeyProvider":
        print(item.get("id", ""))
        break
' || true
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

get_client_scope_mapper_id_by_name() {
    local realm="$1"
    local scope_id="$2"
    local mapper_name="$3"
    kcadm get "client-scopes/${scope_id}/protocol-mappers/models" -r "${realm}" | python3 -c '
import json
import sys

mapper_name = sys.argv[1]
for item in json.load(sys.stdin):
    if item.get("name") == mapper_name:
        print(item.get("id", ""))
        break
' "${mapper_name}"
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

ensure_basic_client_scope() {
    local realm="$1"
    local basic_scope_id
    local sub_mapper_id
    local auth_time_mapper_id

    basic_scope_id="$(get_client_scope_id_by_name "${realm}" "basic")"
    if [[ -z "${basic_scope_id}" ]]; then
        log "create ${realm} basic client scope"
        kcadm create client-scopes -r "${realm}" \
            -s name=basic \
            -s 'description=OpenID Connect scope for add all basic claims to the token' \
            -s protocol=openid-connect \
            -s 'attributes."include.in.token.scope"=false' \
            -s 'attributes."display.on.consent.screen"=false' >/dev/null
        basic_scope_id="$(get_client_scope_id_by_name "${realm}" "basic")"
    fi

    if [[ -z "${basic_scope_id}" ]]; then
        log "failed to reconcile basic client scope for ${realm}"
        return 1
    fi

    sub_mapper_id="$(get_client_scope_mapper_id_by_name "${realm}" "${basic_scope_id}" "sub")"
    if [[ -z "${sub_mapper_id}" ]]; then
        log "add ${realm} basic/sub mapper"
        kcadm create "client-scopes/${basic_scope_id}/protocol-mappers/models" -r "${realm}" \
            -s name=sub \
            -s protocol=openid-connect \
            -s protocolMapper=oidc-sub-mapper \
            -s consentRequired=false \
            -s 'config."introspection.token.claim"=true' \
            -s 'config."access.token.claim"=true' >/dev/null
    fi

    auth_time_mapper_id="$(get_client_scope_mapper_id_by_name "${realm}" "${basic_scope_id}" "auth_time")"
    if [[ -z "${auth_time_mapper_id}" ]]; then
        log "add ${realm} basic/auth_time mapper"
        kcadm create "client-scopes/${basic_scope_id}/protocol-mappers/models" -r "${realm}" \
            -s name=auth_time \
            -s protocol=openid-connect \
            -s protocolMapper=oidc-usersessionmodel-note-mapper \
            -s consentRequired=false \
            -s 'config."user.session.note"=AUTH_TIME' \
            -s 'config."id.token.claim"=true' \
            -s 'config."introspection.token.claim"=true' \
            -s 'config."access.token.claim"=true' \
            -s 'config."claim.name"=auth_time' \
            -s 'config."jsonType.label"=long' >/dev/null
    fi
}

ensure_client_has_default_scope() {
    local realm="$1"
    local client_name="$2"
    local scope_name="$3"
    local client_uuid
    local scope_id
    local current_scope_names

    client_uuid="$(get_client_id_by_client_id "${realm}" "${client_name}")"
    scope_id="$(get_client_scope_id_by_name "${realm}" "${scope_name}")"
    if [[ -z "${client_uuid}" || -z "${scope_id}" ]]; then
        log "missing client or client scope in ${realm}: client=${client_name}, scope=${scope_name}"
        return 1
    fi

    current_scope_names="$(
        kcadm get "clients/${client_uuid}" -r "${realm}" \
            | python3 -c 'import json, sys; client = json.load(sys.stdin); print("\n".join(client.get("defaultClientScopes") or []))'
    )"
    if printf '%s\n' "${current_scope_names}" | grep -Fxq "${scope_name}"; then
        return 0
    fi

    log "add ${scope_name} default client scope to ${realm}/${client_name}"
    kcadm update "clients/${client_uuid}/default-client-scopes/${scope_id}" -r "${realm}" >/dev/null
}

api_client_for_realm() {
    local realm="$1"
    case "${realm}" in
        acps-registry)
            echo "registry-api"
            ;;
        acps-monitor)
            echo "monitor-api"
            ;;
        acps-leader)
            echo "leader-api"
            ;;
        *)
            log "unsupported realm for role scope bootstrap: ${realm}"
            return 1
            ;;
    esac
}

web_client_for_realm() {
    local realm="$1"
    case "${realm}" in
        acps-registry)
            echo "registry-web"
            ;;
        acps-monitor)
            echo "monitor-web"
            ;;
        acps-leader)
            echo "leader-web"
            ;;
        *)
            log "unsupported realm for web client bootstrap: ${realm}"
            return 1
            ;;
    esac
}

cli_client_for_realm() {
    local realm="$1"
    case "${realm}" in
        acps-registry)
            echo "registry-cli"
            ;;
        acps-monitor)
            echo "monitor-cli"
            ;;
        *)
            echo ""
            ;;
    esac
}

ensure_client_audience_mapper() {
    local realm="$1"
    local client_uuid="$2"
    local mapper_name="$3"
    local audience="$4"
    local mapper_id

    mapper_id="$(get_client_mapper_id_by_name "${realm}" "${client_uuid}" "${mapper_name}")"
    if [[ -z "${mapper_id}" ]]; then
        log "add ${realm}/${client_uuid} mapper ${mapper_name}"
        kcadm create "clients/${client_uuid}/protocol-mappers/models" -r "${realm}" \
            -s name="${mapper_name}" \
            -s protocol=openid-connect \
            -s protocolMapper=oidc-audience-mapper \
            -s consentRequired=false \
            -s 'config."access.token.claim"=true' \
            -s 'config."id.token.claim"=false' \
            -s "config.\"included.client.audience\"=${audience}" >/dev/null
        return
    fi

    log "update ${realm}/${client_uuid} mapper ${mapper_name}"
    kcadm update "clients/${client_uuid}/protocol-mappers/models/${mapper_id}" -r "${realm}" \
        -s name="${mapper_name}" \
        -s protocol=openid-connect \
        -s protocolMapper=oidc-audience-mapper \
        -s consentRequired=false \
        -s 'config."access.token.claim"=true' \
        -s 'config."id.token.claim"=false' \
        -s "config.\"included.client.audience\"=${audience}" >/dev/null
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

ensure_device_grant_public_client() {
    local realm="$1"
    local client_name="$2"
    local client_uuid
    local current_client_json

    client_uuid="$(get_client_id_by_client_id "${realm}" "${client_name}")"
    if [[ -z "${client_uuid}" ]]; then
        log "create ${realm} device-grant client ${client_name}" >&2
        kcadm create clients -r "${realm}" \
            -s clientId="${client_name}" \
            -s name="${client_name}" \
            -s enabled=true \
            -s protocol=openid-connect \
            -s publicClient=true \
            -s standardFlowEnabled=false \
            -s implicitFlowEnabled=false \
            -s directAccessGrantsEnabled=false \
            -s serviceAccountsEnabled=false \
            -s fullScopeAllowed=false \
            -s consentRequired=false \
            -s 'attributes."oauth2.device.authorization.grant.enabled"=true' \
            -s 'redirectUris=[]' \
            -s 'webOrigins=[]' \
            -s 'defaultClientScopes=["basic","roles","profile","email"]' >/dev/null
        client_uuid="$(get_client_id_by_client_id "${realm}" "${client_name}")"
    fi

    log "update ${realm} device-grant client ${client_name}" >&2
    kcadm update "clients/${client_uuid}" -r "${realm}" \
        -s name="${client_name}" \
        -s enabled=true \
        -s protocol=openid-connect \
        -s publicClient=true \
        -s standardFlowEnabled=false \
        -s implicitFlowEnabled=false \
        -s directAccessGrantsEnabled=false \
        -s serviceAccountsEnabled=false \
        -s fullScopeAllowed=false \
        -s consentRequired=false \
        -s 'attributes."oauth2.device.authorization.grant.enabled"=true' \
        -s 'redirectUris=[]' \
        -s 'webOrigins=[]' \
        -s 'defaultClientScopes=["basic","roles","profile","email"]' >/dev/null

    current_client_json="$(kcadm get "clients/${client_uuid}" -r "${realm}")"
    CURRENT_CLIENT_JSON="${current_client_json}" python3 - "${client_name}" <<'PY'
import json
import os
import sys

expected_client_id = sys.argv[1]
client = json.loads(os.environ["CURRENT_CLIENT_JSON"])
attributes = client.get("attributes") or {}
device_enabled = str(attributes.get("oauth2.device.authorization.grant.enabled", "")).lower() == "true"

if client.get("clientId") != expected_client_id:
    raise SystemExit(f"unexpected clientId: {client.get('clientId')!r}")
if not client.get("publicClient"):
    raise SystemExit(f"{expected_client_id} must remain a public client")
if client.get("directAccessGrantsEnabled"):
    raise SystemExit(f"{expected_client_id} must keep direct access grants disabled")
if client.get("standardFlowEnabled"):
    raise SystemExit(f"{expected_client_id} must keep standard flow disabled for CLI usage")
if client.get("implicitFlowEnabled"):
    raise SystemExit(f"{expected_client_id} must keep implicit flow disabled")
if client.get("serviceAccountsEnabled"):
    raise SystemExit(f"{expected_client_id} must keep service accounts disabled")
if client.get("fullScopeAllowed"):
    raise SystemExit(f"{expected_client_id} must keep fullScopeAllowed disabled")
if not device_enabled:
    raise SystemExit(f"{expected_client_id} must enable device authorization grant")
PY

    printf '%s\n' "${client_uuid}"
}

ensure_cli_client_for_realm() {
    local realm="$1"
    local client_name
    local client_uuid

    client_name="$(cli_client_for_realm "${realm}")"
    if [[ -z "${client_name}" ]]; then
        return 0
    fi

    client_uuid="$(ensure_device_grant_public_client "${realm}" "${client_name}")"
    ensure_client_has_default_scope "${realm}" "${client_name}" "basic"
    ensure_client_audience_mapper "${realm}" "${client_uuid}" "audience-$(api_client_for_realm "${realm}")" "$(api_client_for_realm "${realm}")"
    if [[ "${realm}" == "acps-monitor" ]]; then
        ensure_client_attribute_mapper "${realm}" "${client_uuid}" "tenant_id" "tenant_id" "tenant_id" "false"
        ensure_client_attribute_mapper "${realm}" "${client_uuid}" "allowed_aics" "allowed_aics" "allowed_aics" "true"
    fi
}

web_base_url_for_realm() {
    local realm="$1"
    case "${realm}" in
        acps-registry)
            echo "${REGISTRY_WEB_BASE_URL:-http://localhost:${NGINX_PORT:-9000}/registry}"
            ;;
        acps-monitor)
            echo "${MONITOR_WEB_BASE_URL:-http://localhost:9009}"
            ;;
        acps-leader)
            echo "${LEADER_WEB_BASE_URL:-http://localhost:${LEADER_WEB_PORT:-9010}}"
            ;;
        *)
            log "unsupported realm for web base url bootstrap: ${realm}"
            return 1
            ;;
    esac
}

build_web_client_redirect_config() {
    local base_url="$1"

    python3 - "${base_url}" <<'PY'
import json
import sys
from urllib.parse import urlsplit, urlunsplit

base_url = sys.argv[1].strip()
if not base_url:
    raise SystemExit("web base url is required")

parsed = urlsplit(base_url)
if not parsed.scheme or not parsed.netloc:
    raise SystemExit(f"invalid web base url: {base_url!r}")

path = parsed.path.rstrip("/")
hosts = [parsed.hostname]
if parsed.hostname in {"localhost", "127.0.0.1"}:
    hosts.append("127.0.0.1" if parsed.hostname == "localhost" else "localhost")

redirect_uris = []
web_origins = []
for host in dict.fromkeys(hosts):
    netloc = host
    if parsed.port is not None:
        netloc = f"{host}:{parsed.port}"
    web_origins.append(urlunsplit((parsed.scheme, netloc, "", "", "")))
    redirect_path = f"{path}/*" if path else "/*"
    redirect_uris.append(urlunsplit((parsed.scheme, netloc, redirect_path, "", "")))

print(
    json.dumps(
        {
            "redirectUris": redirect_uris,
            "webOrigins": web_origins,
        }
    )
)
PY
}

has_active_ed25519_key() {
    local realm="$1"
    kcadm get keys -r "${realm}" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
active_kid = (data.get("active") or {}).get("EdDSA")
for item in data.get("keys") or []:
    if (
        item.get("kid") == active_kid
        and item.get("type") == "OKP"
        and item.get("algorithm") == "EdDSA"
        and item.get("use") == "SIG"
    ):
        raise SystemExit(0)
raise SystemExit(1)
'
}

ensure_default_signature_algorithm() {
    local realm="$1"
    local current

    current="$(kcadm get "realms/${realm}" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("defaultSignatureAlgorithm", ""))')"
    if [[ "${current}" == "EdDSA" ]]; then
        return 0
    fi

    log "set ${realm} default signature algorithm to EdDSA"
    kcadm update "realms/${realm}" -s defaultSignatureAlgorithm=EdDSA >/dev/null
}

ensure_eddsa_component() {
    local realm="$1"
    local realm_id
    local component_id

    ensure_default_signature_algorithm "${realm}"
    if has_active_ed25519_key "${realm}"; then
        log "${realm} already has an active Ed25519 signing key"
        return 0
    fi

    component_id="$(get_eddsa_component_id "${realm}")"
    if [[ -n "${component_id}" ]]; then
        log "update ${realm} eddsa-generated key provider"
        kcadm update "components/${component_id}" -r "${realm}" \
            -s name=eddsa-generated \
            -s 'config.priority=["200"]' \
            -s 'config.enabled=["true"]' \
            -s 'config.active=["true"]' \
            -s 'config.eddsaEllipticCurveKey=["Ed25519"]' >/dev/null
    else
        realm_id="$(get_realm_id "${realm}")"
        log "create ${realm} eddsa-generated key provider"
        kcadm create components -r "${realm}" \
            -s name=eddsa-generated \
            -s providerId=eddsa-generated \
            -s providerType=org.keycloak.keys.KeyProvider \
            -s parentId="${realm_id}" \
            -s 'config.priority=["200"]' \
            -s 'config.enabled=["true"]' \
            -s 'config.active=["true"]' \
            -s 'config.eddsaEllipticCurveKey=["Ed25519"]' >/dev/null
    fi

    if has_active_ed25519_key "${realm}"; then
        log "${realm} Ed25519 signing key is active"
        return 0
    fi

    log "failed to activate Ed25519 signing key for ${realm}"
    return 1
}

ensure_role_scope_mappings() {
    local realm="$1"
    local api_client
    local roles_scope_id
    local api_client_id
    local tmp_file
    local available_count
    local mapped_count

    api_client="$(api_client_for_realm "${realm}")"
    roles_scope_id="$(get_client_scope_id_by_name "${realm}" "roles")"
    api_client_id="$(get_client_id_by_client_id "${realm}" "${api_client}")"

    if [[ -z "${roles_scope_id}" || -z "${api_client_id}" ]]; then
        log "missing roles client scope or api client in ${realm}"
        return 1
    fi

    tmp_file="$(mktemp)"
    kcadm get "client-scopes/${roles_scope_id}/scope-mappings/clients/${api_client_id}/available" -r "${realm}" > "${tmp_file}"
    available_count="$(python3 -c 'import json, sys; print(len(json.load(sys.stdin)))' < "${tmp_file}")"
    if [[ "${available_count}" != "0" ]]; then
        log "add ${available_count} role scope mappings to ${realm} roles client scope"
        docker exec -i "${KEYCLOAK_CONTAINER}" /opt/keycloak/bin/kcadm.sh \
            create "client-scopes/${roles_scope_id}/scope-mappings/clients/${api_client_id}" \
            -r "${realm}" -f - < "${tmp_file}" >/dev/null
    fi
    rm -f "${tmp_file}"

    mapped_count="$(
        kcadm get "client-scopes/${roles_scope_id}/scope-mappings/clients/${api_client_id}" -r "${realm}" \
            | python3 -c 'import json, sys; print(len(json.load(sys.stdin)))'
    )"
    if [[ "${mapped_count}" == "0" ]]; then
        log "failed to configure role scope mappings for ${realm}"
        return 1
    fi

    log "${realm} roles client scope exposes ${mapped_count} api roles"
}

ensure_web_client_redirects() {
    local realm="$1"
    local web_client
    local web_base_url
    local web_client_id
    local redirect_config_json
    local redirect_uris_json
    local web_origins_json

    web_client="$(web_client_for_realm "${realm}")"
    web_base_url="$(web_base_url_for_realm "${realm}")"
    web_client_id="$(get_client_id_by_client_id "${realm}" "${web_client}")"
    if [[ -z "${web_client_id}" ]]; then
        log "missing web client in ${realm}: ${web_client}"
        return 1
    fi

    redirect_config_json="$(build_web_client_redirect_config "${web_base_url}")"
    redirect_fields="$(
        python3 - "${redirect_config_json}" <<'PY'
import json
import sys

config = json.loads(sys.argv[1])
print(json.dumps(config["redirectUris"]))
print(json.dumps(config["webOrigins"]))
PY
    )"
    redirect_uris_json="$(printf '%s\n' "${redirect_fields}" | sed -n '1p')"
    web_origins_json="$(printf '%s\n' "${redirect_fields}" | sed -n '2p')"

    log "reconcile ${realm} ${web_client} redirectUris/webOrigins -> ${web_base_url}"
    kcadm update "clients/${web_client_id}" -r "${realm}" \
        -s "redirectUris=${redirect_uris_json}" \
        -s "webOrigins=${web_origins_json}" >/dev/null

    current_client_json="$(kcadm get "clients/${web_client_id}" -r "${realm}")"
    CURRENT_CLIENT_JSON="${current_client_json}" python3 - "${redirect_config_json}" "${realm}" "${web_client}" <<'PY'
import json
import os
import sys

expected = json.loads(sys.argv[1])
realm = sys.argv[2]
client_id = sys.argv[3]
current = json.loads(os.environ["CURRENT_CLIENT_JSON"])

def normalize(values):
    return sorted(dict.fromkeys(values or []))

if normalize(current.get("redirectUris")) != normalize(expected["redirectUris"]):
    raise SystemExit(f"failed to update redirectUris for {realm}/{client_id}")

if normalize(current.get("webOrigins")) != normalize(expected["webOrigins"]):
    raise SystemExit(f"failed to update webOrigins for {realm}/{client_id}")
PY
}

login_admin

for realm in "${REALMS[@]}"; do
    ensure_eddsa_component "${realm}"
    ensure_basic_client_scope "${realm}"
    ensure_role_scope_mappings "${realm}"
    ensure_client_has_default_scope "${realm}" "$(web_client_for_realm "${realm}")" "basic"
    ensure_web_client_redirects "${realm}"
    ensure_cli_client_for_realm "${realm}"
done
