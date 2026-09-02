#!/usr/bin/env bash
# Generate CA materials for the control-node inventories/ca-materials/ directory.
#
# Writes into OUT (deployable set only):
# ca.crt — intermediate CA certificate
# ca.key — intermediate CA private key (0600)
# root-ca.crt — root CA certificate (trust anchor)
#
# Root private key is written to OUT/offline/root-ca.key and is NEVER part of
# the deployable set (see ). Keep it offline or delete it.
#
# Usage:
# ./scripts/generate_ca_materials.sh --out inventories/ca-materials/
# ./scripts/generate_ca_materials.sh inventories/ca-materials/ # positional OK
# ./scripts/generate_ca_materials.sh --out DIR --force # overwrite
set -euo pipefail

OUT=""
FORCE=0

usage() {
  cat <<'EOF'
Usage: generate_ca_materials.sh --out <dir> [--force]
       generate_ca_materials.sh <dir> [--force]

Generate a self-signed root + intermediate CA into <dir> for ca-server deploy:
  ca.crt, ca.key, root-ca.crt
Root private key goes to <dir>/offline/root-ca.key (not distributed).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)
      OUT="${2:?--out requires a directory}"
      shift 2
      ;;
    --force|-f)
      FORCE=1
      shift
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
      if [[ -z "$OUT" ]]; then
        OUT="$1"
        shift
      else
        echo "[ERROR] unexpected argument: $1" >&2
        usage >&2
        exit 2
      fi
      ;;
  esac
done

if [[ -z "$OUT" ]]; then
  echo "[ERROR] --out <dir> (or positional dir) is required" >&2
  usage >&2
  exit 2
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "[ERROR] openssl is required" >&2
  exit 2
fi

mkdir -p "$OUT/offline"
OUT="$(cd "$OUT" && pwd)"
OFFLINE="$OUT/offline"

CERT="$OUT/ca.crt"
KEY="$OUT/ca.key"
ROOT_CERT="$OUT/root-ca.crt"
ROOT_KEY="$OFFLINE/root-ca.key"
CSR="$OUT/.ca.csr.$$"
EXT="$OUT/.intermediate.ext.$$"
SRL="$OUT/.root-ca.srl.$$"
ROOT_CNF="$OUT/.root-req.cnf.$$"

cleanup() {
  rm -f "$CSR" "$EXT" "$SRL" "$ROOT_CNF"
}
trap cleanup EXIT

if [[ -f "$CERT" || -f "$KEY" || -f "$ROOT_CERT" ]]; then
  if [[ "$FORCE" -ne 1 ]]; then
    echo "[ERROR] CA materials already exist in $OUT (use --force to overwrite):" >&2
    ls -la "$CERT" "$KEY" "$ROOT_CERT" 2>/dev/null || true
    exit 2
  fi
fi

# Refuse to leave a root private key in the deployable set
if [[ -f "$OUT/root-ca.key" ]]; then
  echo "[ERROR] refusing to proceed: $OUT/root-ca.key must not sit in the deployable set" >&2
  echo "        move/remove it (root key belongs in offline/ only)" >&2
  exit 2
fi

DAYS="${AUTO_GENERATED_CA_VALID_DAYS:-3650}"

# Self-sign the root against a minimal req config instead of the distro openssl.cnf.
# Distro configs set [req] x509_extensions=v3_ca, which already carries a
# basicConstraints. OpenSSL 3.x lets -addext override it, but 1.1.1 appends: the
# root then has two basicConstraints, violating RFC 5280, and OpenSSL rejects the
# cert as a trust anchor. The failure is silent at generation time and only shows
# up later during chain verification / TLS handshakes.
cat > "$ROOT_CNF" <<'EOF'
[req]
distinguished_name = dn
prompt = no

[dn]
C  = CN
ST = Beijing
L  = Beijing
O  = Agent CA
OU = Root Certificate Authority
CN = Agent CA Root Certificate
EOF

openssl req -x509 -newkey rsa:4096 -sha256 -nodes \
  -days "$DAYS" \
  -keyout "$ROOT_KEY" \
  -out "$ROOT_CERT" \
  -config "$ROOT_CNF" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:1" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash"

openssl req -new -newkey rsa:4096 -sha256 -nodes \
  -keyout "$KEY" \
  -out "$CSR" \
  -subj "/C=CN/ST=Beijing/L=Beijing/O=Agent CA/OU=Intermediate Certificate Authority/CN=Agent CA Intermediate Certificate"

cat > "$EXT" <<'EOF'
basicConstraints=critical,CA:TRUE,pathlen:0
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF

openssl x509 -req -sha256 \
  -in "$CSR" \
  -CA "$ROOT_CERT" \
  -CAkey "$ROOT_KEY" \
  -CAcreateserial \
  -CAserial "$SRL" \
  -out "$CERT" \
  -days "$DAYS" \
  -extfile "$EXT"

# A malformed root still parses and still lets ca-server start; it only fails
# later, during chain verification or a TLS handshake. Verify here so a broken
# toolchain surfaces at generation time instead of in the field.
if ! openssl verify -CAfile "$ROOT_CERT" "$CERT" >/dev/null 2>&1; then
  echo "[ERROR] generated intermediate does not verify against the generated root" >&2
  openssl verify -CAfile "$ROOT_CERT" "$CERT" >&2 || true
  echo "        (openssl: $(openssl version))" >&2
  exit 1
fi

chmod 600 "$ROOT_KEY" "$KEY"
chmod 644 "$ROOT_CERT" "$CERT"
chmod 700 "$OFFLINE"

# Never leave chain/trust in this control-node input dir — ca_server role assembles them.
rm -f "$OUT/ca-chain.pem" "$OUT/trust-bundle.pem" "$OUT/root-ca.key"

echo "generated CA materials in $OUT"
echo "  deployable: ca.crt ca.key root-ca.crt"
echo "  offline (do NOT distribute): offline/root-ca.key"
echo "Keep the root private key offline, or delete offline/ after recording it securely."
