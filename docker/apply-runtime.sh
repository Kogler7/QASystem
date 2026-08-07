#!/usr/bin/env bash
set -Eeuo pipefail

BASE_IMAGE="${QASYSTEM_BASE_IMAGE:-qasystem-base-v1}"
RUNTIME_IMAGE="${QASYSTEM_RUNTIME_IMAGE:-qasystem-runtime-v1}"
ARCHIVE="${QASYSTEM_RUNTIME_ARCHIVE:-runtime-context.tar}"
DEPLOY=1
[[ "${1:-}" == "--build-only" ]] && DEPLOY=0

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVE_PATH="$ROOT_DIR/$ARCHIVE"
TMP_DIR=""
CANDIDATE="${RUNTIME_IMAGE}-candidate"
OLD_IMAGE_ID=""
PROMOTED=0

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
env_value() {
  sed -n "s/^${1}=//p" "$ROOT_DIR/.env" | tail -n 1 | tr -d '\r'
}
cleanup() {
  [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]] && rm -rf "$TMP_DIR"
  docker image rm "$CANDIDATE" >/dev/null 2>&1 || true
}
rollback() {
  local exit_code=$?
  if (( exit_code != 0 && PROMOTED == 1 )) && [[ -n "$OLD_IMAGE_ID" ]]; then
    log "Deployment failed; restoring $RUNTIME_IMAGE from $OLD_IMAGE_ID"
    docker tag "$OLD_IMAGE_ID" "$RUNTIME_IMAGE" || true
    (cd "$ROOT_DIR" && docker compose up -d --force-recreate backend agent) || true
  fi
  cleanup
  exit "$exit_code"
}
trap rollback EXIT

log "[1/7] Verifying prerequisites and delivery checksums"
command -v docker >/dev/null || die "docker is required"
docker compose version >/dev/null || die "Docker Compose v2 is required"
docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || die "base image not found: $BASE_IMAGE"
[[ -f "$ARCHIVE_PATH" ]] || die "runtime context not found: $ARCHIVE_PATH"
[[ -f "$ROOT_DIR/.env" ]] || die "missing $ROOT_DIR/.env; create it from .env.example and fill deployment values"
for required_key in BACKEND_IMAGE GPU_DEVICE_IDS HOST_MODELS_DIR EXECUTION_MODE EXECUTION_REDIS_URL EXECUTION_DATA_DIR EXECUTION_RUNTIME_READY_FILE AGENT_MCP_URL TASK_STORE_REDIS_URL; do
  [[ -n "$(env_value "$required_key")" ]] || die "missing required .env value: $required_key; merge the current .env.example before deployment"
done
[[ "$(env_value BACKEND_IMAGE)" == "$RUNTIME_IMAGE" ]] || die "BACKEND_IMAGE must be $RUNTIME_IMAGE"
if [[ -f "$ROOT_DIR/SHA256SUMS" ]]; then
  (cd "$ROOT_DIR" && sha256sum --ignore-missing -c SHA256SUMS)
fi

log "[2/7] Extracting runtime build context"
TMP_DIR="$(mktemp -d "${QASYSTEM_APPLY_TMPDIR:-/tmp}/qasystem-runtime.XXXXXX")"
tar -xf "$ARCHIVE_PATH" -C "$TMP_DIR"
[[ -f "$TMP_DIR/Dockerfile.runtime" ]] || die "archive does not contain Dockerfile.runtime"

log "[3/7] Building $CANDIDATE on top of $BASE_IMAGE"
docker build --progress=plain \
  --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  --file "$TMP_DIR/Dockerfile.runtime" \
  --tag "$CANDIDATE" \
  "$TMP_DIR"

log "[4/7] Verifying the candidate runtime"
docker run --rm --entrypoint python "$CANDIDATE" /usr/local/bin/verify_qasystem_runtime.py

log "[5/7] Promoting the candidate and preserving rollback metadata"
if docker image inspect "$RUNTIME_IMAGE" >/dev/null 2>&1; then
  OLD_IMAGE_ID="$(docker image inspect "$RUNTIME_IMAGE" --format '{{.Id}}')"
  docker tag "$OLD_IMAGE_ID" "qasystem-runtime-rollback:$(date '+%Y%m%d%H%M%S')"
fi
docker tag "$CANDIDATE" "$RUNTIME_IMAGE"
PROMOTED=1

log "[6/7] Validating Compose configuration"
(cd "$ROOT_DIR" && docker compose config --quiet)
if (( DEPLOY == 0 )); then
  log "[7/7] Build-only mode complete: $RUNTIME_IMAGE"
  PROMOTED=0
  exit 0
fi

log "[7/7] Recreating services and waiting for Backend health"
(cd "$ROOT_DIR" && docker compose up -d --force-recreate redis backend agent)
for _ in $(seq 1 120); do
  status="$(docker inspect qasystem_backend --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' 2>/dev/null || true)"
  [[ "$status" == "healthy" ]] && { log "Deployment healthy: $RUNTIME_IMAGE"; PROMOTED=0; exit 0; }
  [[ "$status" == "unhealthy" || "$status" == "exited" ]] && die "Backend entered state: $status"
  sleep 5
done
die "Backend did not become healthy within 600 seconds"
