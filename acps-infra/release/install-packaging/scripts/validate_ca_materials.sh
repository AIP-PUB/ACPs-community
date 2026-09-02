#!/usr/bin/env bash
# Validate control-node ca-materials/ for ca-server deploy.
#
# Required files: ca.crt, ca.key, root-ca.crt
# Checks: PEM present, key/cert match, intermediate is CA, verify against root,
# root private key absent from the deployable set.
#
# Usage:
# ./scripts/validate_ca_materials.sh <ca-materials-dir>
# ./scripts/validate_ca_materials.sh --dir <ca-materials-dir>
set -euo pipefail

DIR=""

usage() {
  cat <<'EOF'
Usage: validate_ca_materials.sh --dir <ca-materials-dir>
       validate_ca_materials.sh <ca-materials-dir>

Fail-fast checks for inventories/ca-materials/ (ca.crt, ca.key, root-ca.crt).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)
      DIR="${2:?--dir requires a directory}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "[ERROR] unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -z "$DIR" ]]; then
        DIR="$1"
        shift
      else
        echo "[ERROR] unexpected argument: $1" >&2
        usage >&2
        exit 2
      fi
      ;;
  esac
done

if [[ -z "$DIR" ]]; then
  echo "[ERROR] ca-materials directory required" >&2
  usage >&2
  exit 2
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "[ERROR] openssl is required to validate CA materials" >&2
  exit 2
fi

hint_generate() {
  echo "Provide external materials, or self-sign on the control node:" >&2
  echo "  ./scripts/generate_ca_materials.sh --out inventories/ca-materials/" >&2
}

if [[ ! -d "$DIR" ]]; then
  echo "[ERROR] ca-materials directory missing: $DIR" >&2
  hint_generate
  exit 2
fi

CERT="$DIR/ca.crt"
KEY="$DIR/ca.key"
ROOT="$DIR/root-ca.crt"

missing=()
for f in "$CERT" "$KEY" "$ROOT"; do
  if [[ ! -f "$f" ]]; then
    missing+=("$(basename "$f")")
  fi
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "[ERROR] ca-materials incomplete under $DIR (missing: ${missing[*]})" >&2
  hint_generate
  exit 2
fi

if [[ -f "$DIR/root-ca.key" ]]; then
  echo "[ERROR] root private key must not be in the deployable set: $DIR/root-ca.key" >&2
  echo "        keep it offline only (e.g. $DIR/offline/root-ca.key) or remove it" >&2
  exit 2
fi

for f in "$CERT" "$KEY" "$ROOT"; do
  if ! grep -q "BEGIN .*PRIVATE KEY\|BEGIN CERTIFICATE" "$f"; then
    echo "[ERROR] $f does not look like PEM" >&2
    exit 2
  fi
done

if ! openssl x509 -in "$CERT" -noout >/dev/null 2>&1; then
  echo "[ERROR] ca.crt is not a valid X.509 certificate: $CERT" >&2
  exit 2
fi
if ! openssl x509 -in "$ROOT" -noout >/dev/null 2>&1; then
  echo "[ERROR] root-ca.crt is not a valid X.509 certificate: $ROOT" >&2
  exit 2
fi
if ! openssl pkey -in "$KEY" -noout >/dev/null 2>&1 \
  && ! openssl rsa -in "$KEY" -noout >/dev/null 2>&1 \
  && ! openssl ec -in "$KEY" -noout >/dev/null 2>&1; then
  echo "[ERROR] ca.key is not a readable private key: $KEY" >&2
  exit 2
fi

# Public key / modulus match between ca.key and ca.crt
cert_pub="$(openssl x509 -in "$CERT" -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform DER 2>/dev/null | openssl dgst -sha256)"
key_pub="$(openssl pkey -in "$KEY" -pubout -outform DER 2>/dev/null | openssl dgst -sha256 \
  || openssl rsa -in "$KEY" -pubout -outform DER 2>/dev/null | openssl dgst -sha256)"
if [[ -z "$cert_pub" || -z "$key_pub" || "$cert_pub" != "$key_pub" ]]; then
  echo "[ERROR] ca.key does not match ca.crt (public key mismatch)" >&2
  exit 2
fi

# Intermediate must be a CA certificate
bc="$(openssl x509 -in "$CERT" -noout -ext basicConstraints 2>/dev/null || true)"
if ! echo "$bc" | grep -qi 'CA:TRUE'; then
  # Older openssl may need -text fallback
  if ! openssl x509 -in "$CERT" -noout -text 2>/dev/null | grep -qi 'CA:TRUE'; then
    echo "[ERROR] ca.crt is not a CA certificate (basicConstraints CA:TRUE required)" >&2
    exit 2
  fi
fi

if ! openssl verify -CAfile "$ROOT" "$CERT" >/dev/null 2>&1; then
  echo "[ERROR] openssl verify failed: ca.crt is not signed by root-ca.crt" >&2
  openssl verify -CAfile "$ROOT" "$CERT" >&2 || true
  exit 2
fi

echo "ca-materials OK: $DIR (ca.crt / ca.key / root-ca.crt)"
