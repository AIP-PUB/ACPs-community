#!/usr/bin/env bash
# compose_ctl.sh <compose_dir> <project> <network_compose_yml> <action> [service...]
set -euo pipefail
COMPOSE_DIR="$1"
PROJECT="$2"
ROOT_YML="$3"
ACTION="$4"
shift 4

ARGS=(-p "$PROJECT" -f "$ROOT_YML")
if [[ -d "${COMPOSE_DIR}/services" ]]; then
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    ARGS+=(-f "$f")
  done < <(find "${COMPOSE_DIR}/services" -maxdepth 1 -name '*.yml' 2>/dev/null | sort)
fi

cd "$COMPOSE_DIR"
case "$ACTION" in
  up)
    exec docker compose "${ARGS[@]}" up -d "$@"
    ;;
  up-recreate)
    exec docker compose "${ARGS[@]}" up -d --force-recreate "$@"
    ;;
  run-alembic)
    svc="$1"
    # Prefer python -m so nested/mis-copied venv layouts still work when alembic
    # is on PYTHONPATH but not as a top-level PATH entry.
    exec docker compose "${ARGS[@]}" run --rm --no-deps "$svc" \
      python -m alembic upgrade head
    ;;
  *)
    echo "unknown action: $ACTION" >&2
    exit 2
    ;;
esac
