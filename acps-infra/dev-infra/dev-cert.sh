#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CERTS_DIR="${SCRIPT_DIR}/certs"
ROOT_CA_DIR="${CERTS_DIR}/root"
INFRA_CA_DIR="${CERTS_DIR}/infra-intermediate"
AGENT_CA_DIR="${CERTS_DIR}/agent-intermediate"
BUNDLES_DIR="${CERTS_DIR}/bundles"

ROOT_CA_KEY="${ROOT_CA_DIR}/root-ca.key"
ROOT_CA_CERT="${ROOT_CA_DIR}/root-ca.crt"

INFRA_CA_KEY="${INFRA_CA_DIR}/infra-ca.key"
INFRA_CA_CERT="${INFRA_CA_DIR}/infra-ca.crt"
INFRA_CA_CHAIN="${INFRA_CA_DIR}/infra-ca-chain.pem"

AGENT_CA_KEY="${AGENT_CA_DIR}/agent-ca.key"
AGENT_CA_CERT="${AGENT_CA_DIR}/agent-ca.crt"
AGENT_CA_CHAIN="${AGENT_CA_DIR}/agent-ca-chain.pem"

TRUST_BUNDLE="${BUNDLES_DIR}/trust-bundle.pem"

ROOT_DAYS="3650"
INTERMEDIATE_DAYS="1825"
LEAF_DAYS="825"
FIELD_SEPARATOR=$'\x1f'

log_info() {
    echo "[INFO]  $*"
}

log_warn() {
    echo "[WARN]  $*" >&2
}

log_error() {
    echo "[ERROR] $*" >&2
}

usage() {
    cat <<'EOF'
用法：
  ./dev-cert.sh init-ca
  ./dev-cert.sh issue-leaf --ca <infra|agent> --common-name <CN> --usage <EKU> --cert-out <path> --key-out <path> [--san <SAN>] [--bundle-out <path>] [--relative-to <dir>] [--algorithm <ed25519|rsa>]
  ./dev-cert.sh export-ca --ca <root|infra|agent|bundle> [--cert-out <path>] [--key-out <path>] [--chain-out <path>] [--bundle-out <path>] [--relative-to <dir>]
  ./dev-cert.sh issue-batch <manifest.toml>
  ./dev-cert.sh status
  ./dev-cert.sh clean --yes
  ./dev-cert.sh help

说明：
  - `init-ca` 初始化开发根 CA、基础设施中间 CA、业务中间 CA。
  - `issue-leaf` 按调用方传入的 CN / SAN / EKU / 输出路径签发单张 leaf 证书。
  - `export-ca` 导出根 CA / 中间 CA / trust bundle；输出路径由调用方决定。
  - `issue-batch` 读取调用方自有 manifest，批量执行 leaf 签发与 CA 导出。
  - 输出路径完全由调用方决定；manifest 内相对路径默认相对 manifest 所在目录解析。
  - 当前 trust bundle 包含 root CA + infra intermediate + agent intermediate，便于本地开发链路直接校验证书链。
  - `clean` 会删除 `dev-infra/certs/`，要求显式传 `--yes`。
EOF
}

require_tool() {
    local tool_name="$1"
    if ! command -v "${tool_name}" >/dev/null 2>&1; then
        log_error "未找到必需工具：${tool_name}"
        exit 1
    fi
}

require_runtime() {
    require_tool openssl
    require_tool python3
}

ensure_dir() {
    mkdir -p "$1"
}

resolve_path_from_base() {
    local base_dir="$1"
    local path_value="$2"

    if [[ "${path_value}" = /* ]]; then
        printf '%s\n' "${path_value}"
    else
        printf '%s\n' "${base_dir}/${path_value}"
    fi
}

copy_output() {
    local source_path="$1"
    local destination_path="$2"

    ensure_dir "$(dirname "${destination_path}")"
    cp "${source_path}" "${destination_path}"
}

ensure_absent_or_complete() {
    local label="$1"
    shift

    local paths=("$@")
    local existing=0
    local path=""
    for path in "${paths[@]}"; do
        if [[ -e "${path}" ]]; then
            existing=$((existing + 1))
        fi
    done

    if (( existing > 0 && existing < ${#paths[@]} )); then
        log_error "检测到 ${label} 处于半生成状态，请先修复或执行 clean --yes 重置。"
        printf '  - %s\n' "${paths[@]}" >&2
        exit 1
    fi
}

ensure_ca_layout_state() {
    ensure_absent_or_complete "root CA 套件" "${ROOT_CA_KEY}" "${ROOT_CA_CERT}"
    ensure_absent_or_complete "infra intermediate 套件" "${INFRA_CA_KEY}" "${INFRA_CA_CERT}" "${INFRA_CA_CHAIN}"
    ensure_absent_or_complete "agent intermediate 套件" "${AGENT_CA_KEY}" "${AGENT_CA_CERT}" "${AGENT_CA_CHAIN}"
    ensure_absent_or_complete "trust bundle 套件" "${TRUST_BUNDLE}"
}

is_ca_initialized() {
    [[ -f "${ROOT_CA_KEY}" && -f "${ROOT_CA_CERT}" && -f "${INFRA_CA_KEY}" && -f "${INFRA_CA_CERT}" && -f "${INFRA_CA_CHAIN}" && -f "${AGENT_CA_KEY}" && -f "${AGENT_CA_CERT}" && -f "${AGENT_CA_CHAIN}" && -f "${TRUST_BUNDLE}" ]]
}

update_trust_bundle() {
    ensure_dir "${BUNDLES_DIR}"
    cat "${ROOT_CA_CERT}" "${INFRA_CA_CERT}" "${AGENT_CA_CERT}" >"${TRUST_BUNDLE}"
}

new_tmpfile() {
    mktemp "${TMPDIR:-/tmp}/acps-dev-cert.XXXXXX"
}

is_ed25519_private_key() {
    local key_path="$1"

    openssl pkey -in "${key_path}" -text -noout 2>/dev/null | grep -q '^ED25519 Private-Key:'
}

write_root_extfile() {
    local extfile="$1"
    cat >"${extfile}" <<'EOF'
[root_ca]
basicConstraints = critical, CA:true, pathlen:1
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
EOF
}

write_intermediate_extfile() {
    local extfile="$1"
    cat >"${extfile}" <<'EOF'
[intermediate_ca]
basicConstraints = critical, CA:true, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
EOF
}

write_leaf_extfile() {
    local extfile="$1"
    local usage="$2"
    local san="$3"
    local algorithm="${4:-ed25519}"

    {
        echo "[leaf_cert]"
        echo "basicConstraints = critical, CA:false"
        if [[ "${algorithm}" == "ed25519" ]]; then
            # Ed25519 是纯签名算法，RFC 8410 §3 明确禁止设 keyEncipherment
            echo "keyUsage = critical, digitalSignature"
        else
            echo "keyUsage = critical, digitalSignature, keyEncipherment"
        fi
        echo "extendedKeyUsage = ${usage}"
        echo "subjectKeyIdentifier = hash"
        echo "authorityKeyIdentifier = keyid,issuer"
        if [[ -n "${san}" ]]; then
            echo "subjectAltName = ${san}"
        fi
    } >"${extfile}"
}

create_root_ca() {
    ensure_dir "${ROOT_CA_DIR}"

    local csr extfile
    csr="$(new_tmpfile)"
    extfile="$(new_tmpfile)"

    openssl genpkey -algorithm ED25519 -out "${ROOT_CA_KEY}" >/dev/null 2>&1
    openssl req -new -key "${ROOT_CA_KEY}" -out "${csr}" -subj "/CN=ACPs Development Root CA" >/dev/null 2>&1
    write_root_extfile "${extfile}"
    if is_ed25519_private_key "${ROOT_CA_KEY}"; then
        openssl x509 -req \
            -in "${csr}" \
            -signkey "${ROOT_CA_KEY}" \
            -out "${ROOT_CA_CERT}" \
            -days "${ROOT_DAYS}" \
            -extfile "${extfile}" \
            -extensions root_ca >/dev/null 2>&1
    else
        openssl x509 -req \
            -in "${csr}" \
            -signkey "${ROOT_CA_KEY}" \
            -out "${ROOT_CA_CERT}" \
            -days "${ROOT_DAYS}" \
            -sha256 \
            -extfile "${extfile}" \
            -extensions root_ca >/dev/null 2>&1
    fi

    rm -f "${csr}" "${extfile}"
}

create_intermediate_ca() {
    local label="$1"
    local key_path="$2"
    local cert_path="$3"
    local chain_path="$4"
    local subject_cn="$5"

    ensure_dir "$(dirname "${key_path}")"

    local csr extfile
    csr="$(new_tmpfile)"
    extfile="$(new_tmpfile)"

    openssl genpkey -algorithm ED25519 -out "${key_path}" >/dev/null 2>&1
    openssl req -new -key "${key_path}" -out "${csr}" -subj "/CN=${subject_cn}" >/dev/null 2>&1
    write_intermediate_extfile "${extfile}"
    if is_ed25519_private_key "${ROOT_CA_KEY}"; then
        openssl x509 -req \
            -in "${csr}" \
            -CA "${ROOT_CA_CERT}" \
            -CAkey "${ROOT_CA_KEY}" \
            -CAcreateserial \
            -out "${cert_path}" \
            -days "${INTERMEDIATE_DAYS}" \
            -extfile "${extfile}" \
            -extensions intermediate_ca >/dev/null 2>&1
    else
        openssl x509 -req \
            -in "${csr}" \
            -CA "${ROOT_CA_CERT}" \
            -CAkey "${ROOT_CA_KEY}" \
            -CAcreateserial \
            -out "${cert_path}" \
            -days "${INTERMEDIATE_DAYS}" \
            -sha256 \
            -extfile "${extfile}" \
            -extensions intermediate_ca >/dev/null 2>&1
    fi

    cat "${cert_path}" "${ROOT_CA_CERT}" >"${chain_path}"
    rm -f "${csr}" "${extfile}"

    openssl verify -CAfile "${ROOT_CA_CERT}" "${cert_path}" >/dev/null
    log_info "已生成 ${label}"
}

init_ca() {
    require_runtime
    ensure_ca_layout_state

    if is_ca_initialized; then
        update_trust_bundle
        log_info "开发 CA 套件已存在，跳过初始化。"
        return 0
    fi

    ensure_dir "${ROOT_CA_DIR}"
    ensure_dir "${INFRA_CA_DIR}"
    ensure_dir "${AGENT_CA_DIR}"
    ensure_dir "${BUNDLES_DIR}"

    log_info "初始化开发根 CA"
    create_root_ca

    log_info "初始化基础设施中间 CA"
    create_intermediate_ca \
        "基础设施中间 CA" \
        "${INFRA_CA_KEY}" \
        "${INFRA_CA_CERT}" \
        "${INFRA_CA_CHAIN}" \
        "ACPs Development Infrastructure Intermediate CA"

    log_info "初始化业务中间 CA"
    create_intermediate_ca \
        "业务中间 CA" \
        "${AGENT_CA_KEY}" \
        "${AGENT_CA_CERT}" \
        "${AGENT_CA_CHAIN}" \
        "ACPs Development Agent Intermediate CA"

    update_trust_bundle
    log_info "开发 CA 初始化完成"
}

resolve_leaf_ca_materials() {
    local ca_kind="$1"
    case "${ca_kind}" in
        infra)
            printf '%s\t%s\n' "${INFRA_CA_KEY}" "${INFRA_CA_CERT}"
            ;;
        agent)
            printf '%s\t%s\n' "${AGENT_CA_KEY}" "${AGENT_CA_CERT}"
            ;;
        *)
            log_error "leaf 证书只支持 infra 或 agent 中间 CA，当前为：${ca_kind}"
            exit 1
            ;;
    esac
}

resolve_export_source_materials() {
    local source_kind="$1"
    case "${source_kind}" in
        root)
            printf '%s\t%s\t%s\n' "${ROOT_CA_KEY}" "${ROOT_CA_CERT}" ""
            ;;
        infra)
            printf '%s\t%s\t%s\n' "${INFRA_CA_KEY}" "${INFRA_CA_CERT}" "${INFRA_CA_CHAIN}"
            ;;
        agent)
            printf '%s\t%s\t%s\n' "${AGENT_CA_KEY}" "${AGENT_CA_CERT}" "${AGENT_CA_CHAIN}"
            ;;
        bundle)
            printf '%s\t%s\t%s\n' "" "" ""
            ;;
        *)
            log_error "未知导出源：${source_kind}"
            exit 1
            ;;
    esac
}

issue_leaf_certificate() {
    local ca_kind="$1"
    local subject_cn="$2"
    local usage="$3"
    local san="$4"
    local cert_path="$5"
    local key_path="$6"
    local algorithm="${7:-ed25519}"

    init_ca
    ensure_dir "$(dirname "${cert_path}")"
    ensure_dir "$(dirname "${key_path}")"

    local ca_key ca_cert csr extfile
    IFS=$'\t' read -r ca_key ca_cert <<<"$(resolve_leaf_ca_materials "${ca_kind}")"
    csr="$(new_tmpfile)"
    extfile="$(new_tmpfile)"

    case "${algorithm}" in
        ed25519)
            openssl genpkey -algorithm ED25519 -out "${key_path}" >/dev/null 2>&1
            ;;
        rsa)
            openssl genrsa -out "${key_path}" 2048 >/dev/null 2>&1
            ;;
        *)
            log_error "issue-leaf 只支持 ed25519 或 rsa，当前算法：${algorithm}"
            exit 1
            ;;
    esac
    openssl req -new -key "${key_path}" -out "${csr}" -subj "/CN=${subject_cn}" >/dev/null 2>&1
    write_leaf_extfile "${extfile}" "${usage}" "${san}" "${algorithm}"
    if is_ed25519_private_key "${ca_key}"; then
        openssl x509 -req \
            -in "${csr}" \
            -CA "${ca_cert}" \
            -CAkey "${ca_key}" \
            -CAcreateserial \
            -out "${cert_path}" \
            -days "${LEAF_DAYS}" \
            -extfile "${extfile}" \
            -extensions leaf_cert >/dev/null 2>&1
    else
        openssl x509 -req \
            -in "${csr}" \
            -CA "${ca_cert}" \
            -CAkey "${ca_key}" \
            -CAcreateserial \
            -out "${cert_path}" \
            -days "${LEAF_DAYS}" \
            -sha256 \
            -extfile "${extfile}" \
            -extensions leaf_cert >/dev/null 2>&1
    fi

    rm -f "${csr}" "${extfile}"
    openssl verify -CAfile "${ROOT_CA_CERT}" -untrusted "${ca_cert}" "${cert_path}" >/dev/null
}

issue_leaf_artifact() {
    local ca_kind="$1"
    local subject_cn="$2"
    local usage="$3"
    local san="$4"
    local cert_out="$5"
    local key_out="$6"
    local bundle_out="$7"
    local base_dir="$8"
    local algorithm="${9:-ed25519}"

    local cert_path key_path bundle_path=""
    cert_path="$(resolve_path_from_base "${base_dir}" "${cert_out}")"
    key_path="$(resolve_path_from_base "${base_dir}" "${key_out}")"
    if [[ -n "${bundle_out}" ]]; then
        bundle_path="$(resolve_path_from_base "${base_dir}" "${bundle_out}")"
    fi

    issue_leaf_certificate "${ca_kind}" "${subject_cn}" "${usage}" "${san}" "${cert_path}" "${key_path}" "${algorithm}"
    if [[ -n "${bundle_path}" ]]; then
        copy_output "${TRUST_BUNDLE}" "${bundle_path}"
    fi

    log_info "已签发 leaf 证书: ${cert_path}"
}

export_ca_materials() {
    local source_kind="$1"
    local cert_out="$2"
    local key_out="$3"
    local chain_out="$4"
    local bundle_out="$5"
    local base_dir="$6"

    init_ca

    if [[ -z "${cert_out}" && -z "${key_out}" && -z "${chain_out}" && -z "${bundle_out}" ]]; then
        log_error "export-ca 至少需要一个输出路径。"
        exit 1
    fi

    local source_key="" source_cert="" source_chain=""
    IFS=$'\t' read -r source_key source_cert source_chain <<<"$(resolve_export_source_materials "${source_kind}")"

    if [[ "${source_kind}" == "bundle" ]]; then
        if [[ -n "${cert_out}" || -n "${key_out}" || -n "${chain_out}" ]]; then
            log_error "ca=bundle 仅支持 bundle-out。"
            exit 1
        fi
    fi

    if [[ -n "${cert_out}" ]]; then
        copy_output "${source_cert}" "$(resolve_path_from_base "${base_dir}" "${cert_out}")"
    fi

    if [[ -n "${key_out}" ]]; then
        copy_output "${source_key}" "$(resolve_path_from_base "${base_dir}" "${key_out}")"
    fi

    if [[ -n "${chain_out}" ]]; then
        if [[ -z "${source_chain}" ]]; then
            log_error "ca=${source_kind} 不支持 chain-out。"
            exit 1
        fi
        copy_output "${source_chain}" "$(resolve_path_from_base "${base_dir}" "${chain_out}")"
    fi

    if [[ -n "${bundle_out}" ]]; then
        copy_output "${TRUST_BUNDLE}" "$(resolve_path_from_base "${base_dir}" "${bundle_out}")"
    fi

    log_info "已导出 CA 资产: ${source_kind}"
}

manifest_to_records() {
    local manifest_path="$1"

    python3 - <<'PY' "${manifest_path}"
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

FIELD_SEPARATOR = "\x1f"


def fail(message: str) -> None:
    print(f"[ERROR] {message}", file=sys.stderr)
    raise SystemExit(1)


def get_text(item: dict[str, object], key: str, *, required: bool) -> str:
    value = item.get(key)
    if value is None:
        if required:
            fail(f"manifest 缺少字段: {key}")
        return ""
    if not isinstance(value, str):
        fail(f"manifest 字段必须是字符串: {key}")
    if any(ch in value for ch in ("\t", "\n", "\r")):
        fail(f"manifest 字段不能包含制表符或换行: {key}")
    if required and not value.strip():
        fail(f"manifest 字段不能为空: {key}")
    return value


manifest_path = Path(sys.argv[1]).expanduser()
if not manifest_path.is_file():
    fail(f"manifest 不存在: {manifest_path}")

with manifest_path.open("rb") as handle:
    data = tomllib.load(handle)

items = data.get("items")
if not isinstance(items, list) or not items:
    fail("manifest 必须包含非空的 [[items]] 列表")

for index, item in enumerate(items, start=1):
    if not isinstance(item, dict):
        fail(f"manifest 第 {index} 项必须是对象")

    item_type = get_text(item, "type", required=True)
    if item_type == "leaf":
        row = [
            "leaf",
            get_text(item, "ca", required=True),
            get_text(item, "common_name", required=True),
            get_text(item, "usage", required=True),
            get_text(item, "san", required=False),
            get_text(item, "cert_path", required=True),
            get_text(item, "key_path", required=True),
            "",
            get_text(item, "bundle_path", required=False),
            get_text(item, "algorithm", required=False) or "ed25519",
        ]
    elif item_type == "ca-materials":
        row = [
            "ca-materials",
            get_text(item, "ca", required=True),
            "",
            "",
            "",
            get_text(item, "cert_path", required=False),
            get_text(item, "key_path", required=False),
            get_text(item, "chain_path", required=False),
            get_text(item, "bundle_path", required=False),
        ]
        if not any(row[5:]):
            fail(f"manifest 第 {index} 项至少需要一个输出路径")
    else:
        fail(f"manifest 第 {index} 项类型未知: {item_type}")

    print(FIELD_SEPARATOR.join(row))
PY
}

issue_batch_from_manifest() {
    local manifest_path="$1"
    if [[ ! -f "${manifest_path}" ]]; then
        log_error "未找到 manifest：${manifest_path}"
        exit 1
    fi

    local manifest_dir manifest_abs
    manifest_dir="$(cd "$(dirname "${manifest_path}")" && pwd)"
    manifest_abs="${manifest_dir}/$(basename "${manifest_path}")"

    while IFS="${FIELD_SEPARATOR}" read -r item_type ca_kind common_name usage san cert_path key_path chain_path bundle_path algorithm; do
        case "${item_type}" in
            leaf)
                issue_leaf_artifact "${ca_kind}" "${common_name}" "${usage}" "${san}" "${cert_path}" "${key_path}" "${bundle_path}" "${manifest_dir}" "${algorithm:-ed25519}"
                ;;
            ca-materials)
                export_ca_materials "${ca_kind}" "${cert_path}" "${key_path}" "${chain_path}" "${bundle_path}" "${manifest_dir}"
                ;;
            *)
                log_error "manifest 解析后出现未知 item 类型：${item_type}"
                exit 1
                ;;
        esac
    done < <(manifest_to_records "${manifest_abs}")

    log_info "批量签发完成：${manifest_abs}"
}

print_status_line() {
    local label="$1"
    local path="$2"
    local state="missing"
    if [[ -e "${path}" ]]; then
        state="present"
    fi
    printf '%-32s %s\n' "${label}" "${state}"
}

status() {
    echo "[CA]"
    print_status_line "root-ca.key" "${ROOT_CA_KEY}"
    print_status_line "root-ca.crt" "${ROOT_CA_CERT}"
    print_status_line "infra-ca.key" "${INFRA_CA_KEY}"
    print_status_line "infra-ca.crt" "${INFRA_CA_CERT}"
    print_status_line "infra-ca-chain.pem" "${INFRA_CA_CHAIN}"
    print_status_line "agent-ca.key" "${AGENT_CA_KEY}"
    print_status_line "agent-ca.crt" "${AGENT_CA_CERT}"
    print_status_line "agent-ca-chain.pem" "${AGENT_CA_CHAIN}"
    echo
    echo "[Bundle]"
    print_status_line "trust-bundle.pem" "${TRUST_BUNDLE}"
}

clean() {
    local confirm="${1:-}"
    if [[ "${confirm}" != "--yes" ]]; then
        log_error "clean 是破坏性操作，请显式传入 --yes。"
        exit 1
    fi

    rm -rf "${CERTS_DIR}"
    log_info "已删除 ${CERTS_DIR}"
}

run_issue_leaf_command() {
    local ca_kind=""
    local subject_cn=""
    local usage=""
    local san=""
    local cert_out=""
    local key_out=""
    local bundle_out=""
    local base_dir="$PWD"
    local algorithm="ed25519"

    while (($#)); do
        case "$1" in
            --ca)
                ca_kind="$2"
                shift 2
                ;;
            --common-name|--cn)
                subject_cn="$2"
                shift 2
                ;;
            --usage)
                usage="$2"
                shift 2
                ;;
            --san)
                san="$2"
                shift 2
                ;;
            --cert-out)
                cert_out="$2"
                shift 2
                ;;
            --key-out)
                key_out="$2"
                shift 2
                ;;
            --bundle-out)
                bundle_out="$2"
                shift 2
                ;;
            --relative-to)
                base_dir="$2"
                shift 2
                ;;
            --algorithm)
                algorithm="$2"
                if [[ "${algorithm}" != "ed25519" && "${algorithm}" != "rsa" ]]; then
                    log_error "issue-leaf --algorithm 只支持 ed25519 或 rsa，当前值：${algorithm}"
                    exit 1
                fi
                shift 2
                ;;
            *)
                log_error "issue-leaf 不支持的参数：$1"
                exit 1
                ;;
        esac
    done

    if [[ -z "${ca_kind}" || -z "${subject_cn}" || -z "${usage}" || -z "${cert_out}" || -z "${key_out}" ]]; then
        log_error "issue-leaf 缺少必填参数。"
        usage
        exit 1
    fi

    issue_leaf_artifact "${ca_kind}" "${subject_cn}" "${usage}" "${san}" "${cert_out}" "${key_out}" "${bundle_out}" "${base_dir}" "${algorithm}"
}

run_export_ca_command() {
    local source_kind=""
    local cert_out=""
    local key_out=""
    local chain_out=""
    local bundle_out=""
    local base_dir="$PWD"

    while (($#)); do
        case "$1" in
            --ca)
                source_kind="$2"
                shift 2
                ;;
            --cert-out)
                cert_out="$2"
                shift 2
                ;;
            --key-out)
                key_out="$2"
                shift 2
                ;;
            --chain-out)
                chain_out="$2"
                shift 2
                ;;
            --bundle-out)
                bundle_out="$2"
                shift 2
                ;;
            --relative-to)
                base_dir="$2"
                shift 2
                ;;
            *)
                log_error "export-ca 不支持的参数：$1"
                exit 1
                ;;
        esac
    done

    if [[ -z "${source_kind}" ]]; then
        log_error "export-ca 缺少 --ca 参数。"
        usage
        exit 1
    fi

    export_ca_materials "${source_kind}" "${cert_out}" "${key_out}" "${chain_out}" "${bundle_out}" "${base_dir}"
}

run_issue_batch_command() {
    local manifest_path=""

    while (($#)); do
        case "$1" in
            --manifest)
                manifest_path="$2"
                shift 2
                ;;
            *)
                if [[ -n "${manifest_path}" ]]; then
                    log_error "issue-batch 只接受一个 manifest 路径。"
                    exit 1
                fi
                manifest_path="$1"
                shift
                ;;
        esac
    done

    if [[ -z "${manifest_path}" ]]; then
        log_error "issue-batch 缺少 manifest 路径。"
        usage
        exit 1
    fi

    issue_batch_from_manifest "${manifest_path}"
}

main() {
    local command="${1:-help}"
    shift || true

    case "${command}" in
        init-ca)
            init_ca
            ;;
        issue-leaf)
            run_issue_leaf_command "$@"
            ;;
        export-ca)
            run_export_ca_command "$@"
            ;;
        issue-batch)
            run_issue_batch_command "$@"
            ;;
        status)
            status
            ;;
        clean)
            clean "$@"
            ;;
        help|-h|--help)
            usage
            ;;
        *)
            log_error "未知命令：${command}"
            usage
            exit 1
            ;;
    esac
}

main "$@"
