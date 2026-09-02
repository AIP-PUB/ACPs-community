#!/usr/bin/env bash
# Temporary closed-loop app images (linux/arm64) with /opt/acps layout expected by
# install-packaging compose templates. Prefer formal image-packaging when
# app-release artifacts exist.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${ROOT}/artifacts/images"
CTX_ROOT="${ROOT}/.build/app-image-ctx"
WS="$(cd "${ROOT}/../../.." && pwd)"
PLATFORM="${ACPS_PLATFORM:-linux/arm64}"

mkdir -p "${OUT}" "${CTX_ROOT}"

build_one() {
  local app="$1" tag="$2" file="$3" start_cmd="$4"
  local ctx="${CTX_ROOT}/${app}"
  echo "=== Building ${tag} ==="
  rm -rf "${ctx}"
  mkdir -p "${ctx}"

  cat > "${ctx}/app-run.sh" <<EOF
#!/bin/sh
set -e
# Default start command for this image; ACPS_RUN_ENTRY selects optional variants.
run_entry="\${ACPS_RUN_ENTRY:-}"
case "\$run_entry" in
  *mtls*)
    if python -c 'import importlib; importlib.import_module("app.main_mtls")' 2>/dev/null; then
      exec python -m app.main_mtls
    fi
    ;;
esac
exec ${start_cmd}
EOF
  chmod +x "${ctx}/app-run.sh"

  cat > "${ctx}/Dockerfile" <<EOF
FROM python:3.14-slim
RUN (getent group acps >/dev/null || groupadd --system acps) \\
 && (id -u acps >/dev/null 2>&1 || useradd --system --gid acps --home-dir /opt/acps --shell /usr/sbin/nologin acps) \\
 && mkdir -p /opt/acps/app /opt/acps/venv /opt/acps/bin /var/lib/acps /var/log/acps \\
 && apt-get update && apt-get install -y --no-install-recommends curl ca-certificates build-essential \\
 && rm -rf /var/lib/apt/lists/*
COPY ${app} /src/${app}
COPY acps-sdk /src/acps-sdk
COPY app-run.sh /opt/acps/bin/app-run
WORKDIR /src/${app}
RUN pip install --no-cache-dir uv \\
 && sed -i 's|path = "../acps-sdk"|path = "/src/acps-sdk"|g' pyproject.toml \\
 && uv sync --no-dev \\
 && cp -a /src/${app}/.venv/. /opt/acps/venv/ \\
 && mkdir -p /opt/acps/app \\
 && cp -a /src/${app}/app /opt/acps/app/app \\
 && if [ -d /src/${app}/config ]; then cp -a /src/${app}/config /opt/acps/app/config; fi \\
 && if [ -d /src/${app}/alembic ]; then cp -a /src/${app}/alembic /opt/acps/app/alembic; fi \\
 && if [ -f /src/${app}/alembic.ini ]; then cp /src/${app}/alembic.ini /opt/acps/app/alembic.ini; fi \\
 && ln -sfn /opt/acps/venv/bin/python /usr/local/bin/python \\
 && (ln -sfn /opt/acps/venv/bin/alembic /usr/local/bin/alembic || true) \\
 && chmod +x /opt/acps/bin/app-run \\
 && chown -R acps:acps /opt/acps /var/lib/acps /var/log/acps
USER acps
WORKDIR /opt/acps/app
ENV PATH="/opt/acps/venv/bin:/usr/local/bin:\$PATH" \\
    PYTHONPATH="/opt/acps/app"
ENTRYPOINT []
CMD ["/opt/acps/bin/app-run"]
EOF

  rsync -a --delete --exclude .venv --exclude __pycache__ --exclude .git --exclude logs --exclude dist \
    --exclude '.mypy_cache' --exclude '.ruff_cache' --exclude '.pytest_cache' \
    "${WS}/${app}/" "${ctx}/${app}/"
  rsync -a --delete --exclude .venv --exclude __pycache__ --exclude .git \
    "${WS}/acps-sdk/" "${ctx}/acps-sdk/"

  docker buildx build --platform "${PLATFORM}" -t "${tag}" --load "${ctx}"
  tmp="$(mktemp "${TMPDIR:-/tmp}/acps-save.XXXXXX")"
  docker save "${tag}" -o "${tmp}"
  gzip -c "${tmp}" > "${OUT}/${file}.tmp"
  mv "${OUT}/${file}.tmp" "${OUT}/${file}"
  rm -f "${tmp}"
  echo "wrote ${OUT}/${file} ($(du -h "${OUT}/${file}" | awk '{print $1}'))"
}

for f in registry-server.image.tar.gz ca-server.image.tar.gz discovery-server-cpu.image.tar.gz mq-auth-server.image.tar.gz; do
  rm -f "${OUT}/${f}"
done

build_one registry-server \
  "acps/registry-server:2.2.0-linux-arm64" \
  "registry-server.image.tar.gz" \
  'python -m uvicorn app.main:app --host 0.0.0.0 --port 9001'

build_one ca-server \
  "acps/ca-server:2.2.0-linux-arm64" \
  "ca-server.image.tar.gz" \
  'python -m uvicorn app.main:app --host 0.0.0.0 --port 9003'

build_one discovery-server \
  "acps/discovery-server:2.2.0-linux-arm64-cpu" \
  "discovery-server-cpu.image.tar.gz" \
  'python -m uvicorn app.main:app --host 0.0.0.0 --port 9005'

build_one mq-auth-server \
  "acps/mq-auth-server:2.2.0-linux-arm64" \
  "mq-auth-server.image.tar.gz" \
  'python -m app.main'

echo ALL_APP_IMAGES_OK
ls -lh "${OUT}"/registry-server.image.tar.gz "${OUT}"/ca-server.image.tar.gz \
  "${OUT}"/discovery-server-cpu.image.tar.gz "${OUT}"/mq-auth-server.image.tar.gz
