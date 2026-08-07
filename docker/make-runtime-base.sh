#!/usr/bin/env bash
set -Eeuo pipefail

# Convert an already transferred application image into a reusable, source-free
# runtime base. The source image is never retagged or removed.

SOURCE_IMAGE="${1:-qasystem-backend:latest}"
TARGET_IMAGE="${2:-qasystem-base-v1}"
KEEP_CONTAINER="${KEEP_CONTAINER:-0}"
WORK_DIR="${QASYSTEM_BASE_TMPDIR:-${TMPDIR:-/tmp}}"
DOCKER_COMMAND="${DOCKER_COMMAND:-docker}"

docker() { "$DOCKER_COMMAND" "$@"; }

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
human_bytes() {
  awk -v bytes="${1:-0}" 'BEGIN {
    split("B KiB MiB GiB TiB", units, " "); index = 1;
    while (bytes >= 1024 && index < 5) { bytes /= 1024; index++ }
    printf "%.1f %s", bytes, units[index]
  }'
}
wait_with_progress() {
  local pid="$1" description="$2" progress_file="${3:-}" started now elapsed size
  started="$(date +%s)"
  while kill -0 "$pid" >/dev/null 2>&1; do
    sleep 10
    now="$(date +%s)"
    elapsed=$((now - started))
    if [[ -n "$progress_file" && -f "$progress_file" ]]; then
      size="$(wc -c <"$progress_file" | tr -d ' ')"
      log "$description: running ${elapsed}s, written $(human_bytes "$size")"
    else
      log "$description: running ${elapsed}s"
    fi
  done
  wait "$pid"
}
cleanup() {
  if [[ -n "${CONTAINER_ID:-}" && "$KEEP_CONTAINER" != "1" ]]; then
    log "Cleanup: removing temporary container ${CONTAINER_ID:0:12}"
    docker rm -f "$CONTAINER_ID" >/dev/null 2>&1 || true
  fi
  [[ -n "${ROOTFS_TAR:-}" && -e "$ROOTFS_TAR" ]] && log "Cleanup: removing temporary rootfs $ROOTFS_TAR"
  rm -f "${ROOTFS_TAR:-}" "${IMPORT_LOG:-}" 2>/dev/null || true
}
trap cleanup EXIT

log "[1/8] Checking Docker, image names and disk space"
if [[ "$DOCKER_COMMAND" == */* ]]; then
  [[ -x "$DOCKER_COMMAND" ]] || die "Docker CLI is not executable: $DOCKER_COMMAND"
else
  command -v "$DOCKER_COMMAND" >/dev/null || die "docker is required"
fi
docker image inspect "$SOURCE_IMAGE" >/dev/null 2>&1 || die "source image not found: $SOURCE_IMAGE"
[[ "$SOURCE_IMAGE" != "$TARGET_IMAGE" ]] || die "source and target image must differ"
docker image inspect "$TARGET_IMAGE" >/dev/null 2>&1 && die "target image already exists: $TARGET_IMAGE"

SOURCE_ID="$(docker image inspect "$SOURCE_IMAGE" --format '{{.Id}}')"
SOURCE_SIZE="$(docker image inspect "$SOURCE_IMAGE" --format '{{.Size}}')"
DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}')"
mkdir -p "$WORK_DIR"
# During export/import the source layers, an uncompressed rootfs tar and the
# imported layer coexist. Three times the source size is a conservative floor.
REQUIRED_BYTES=$((SOURCE_SIZE * 3 + 2147483648))
WORK_AVAILABLE_BYTES="$(df -P -B1 "$WORK_DIR" | awk 'NR==2 {print $4}')"
(( WORK_AVAILABLE_BYTES >= REQUIRED_BYTES )) || die "insufficient temporary disk space at $WORK_DIR; need about $((REQUIRED_BYTES / 1073741824)) GiB free"
if [[ -e "$DOCKER_ROOT" ]]; then
  DOCKER_AVAILABLE_BYTES="$(df -P -B1 "$DOCKER_ROOT" | awk 'NR==2 {print $4}')"
  (( DOCKER_AVAILABLE_BYTES >= REQUIRED_BYTES )) || die "insufficient Docker disk space; need about $((REQUIRED_BYTES / 1073741824)) GiB free"
else
  log "Docker root $DOCKER_ROOT is managed by a remote VM; host-side free-space check skipped"
fi

log "Source: $SOURCE_IMAGE ($SOURCE_ID, $(human_bytes "$SOURCE_SIZE"))"
log "Target: $TARGET_IMAGE"

log "[2/8] Creating an isolated temporary container"
CONTAINER_ID="$(docker create --user 0:0 --entrypoint /bin/sh "$SOURCE_IMAGE" -c 'while :; do sleep 3600; done')"
docker start "$CONTAINER_ID" >/dev/null

# Keep dependency/runtime files and delete only mutable application content.
log "[3/8] Removing application source, agentcode package and runtime caches"
docker exec "$CONTAINER_ID" /bin/sh -c '
  set -eu
  rm -rf /app/backend /app/algorithm /app/mcp_services /app/agent
  python -m pip uninstall -y agentcode >/dev/null 2>&1 || true
  rm -rf /root/.cache /tmp/* /var/tmp/*
  mkdir -p /app/backend
'
docker stop "$CONTAINER_ID" >/dev/null

ROOTFS_TAR="$(mktemp "$WORK_DIR/qasystem-runtime-base.XXXXXX.tar")"
IMPORT_LOG="$(mktemp "$WORK_DIR/qasystem-runtime-base.XXXXXX.log")"
log "[4/8] Exporting flattened rootfs to $ROOTFS_TAR"
docker export "$CONTAINER_ID" >"$ROOTFS_TAR" &
EXPORT_PID=$!
wait_with_progress "$EXPORT_PID" "Rootfs export" "$ROOTFS_TAR" || die "docker export failed"
log "Rootfs export completed: $(human_bytes "$(wc -c <"$ROOTFS_TAR" | tr -d ' ')")"

# Restore portable runtime metadata supported by docker import. The final code
# overlay is responsible for the application command and health check.
IMPORT_ARGS=(--change "ENV QASYSTEM_RUNTIME_BASE_SOURCE=$SOURCE_ID" --change 'WORKDIR /app/backend')
while IFS= read -r env_line; do
  [[ -n "$env_line" ]] && IMPORT_ARGS+=(--change "ENV $env_line")
done < <(docker image inspect "$SOURCE_IMAGE" --format '{{range .Config.Env}}{{println .}}{{end}}')
while IFS= read -r port; do
  [[ -n "$port" ]] && IMPORT_ARGS+=(--change "EXPOSE $port")
done < <(docker image inspect "$SOURCE_IMAGE" --format '{{range $port, $_ := .Config.ExposedPorts}}{{println $port}}{{end}}')

log "[5/8] Importing rootfs as the single-layer image $TARGET_IMAGE"
docker import "${IMPORT_ARGS[@]}" - "$TARGET_IMAGE" <"$ROOTFS_TAR" 2>"$IMPORT_LOG" >/dev/null &
IMPORT_PID=$!
wait_with_progress "$IMPORT_PID" "Image import" || {
  cat "$IMPORT_LOG" >&2
  die "docker import failed"
}

log "[6/8] Checking imported image metadata"
docker image inspect "$TARGET_IMAGE" >/dev/null || die "target image was not created"
log "[7/8] Validating source removal and critical runtime dependencies"
docker run --rm --entrypoint /bin/sh "$TARGET_IMAGE" -c '
  test ! -e /app/backend/app/main.py
  test ! -e /app/agent/pyproject.toml
  python -c "import fastapi, torch, vllm"
  command -v tini >/dev/null
' >/dev/null || die "runtime-base validation failed"

log "[8/8] Runtime base completed"
log "Created: $TARGET_IMAGE"
log "Source image remains available for rollback: $SOURCE_IMAGE"
log "Next code image should use: FROM $TARGET_IMAGE"
