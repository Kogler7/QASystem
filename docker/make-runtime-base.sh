#!/usr/bin/env bash
set -Eeuo pipefail

# Convert an already transferred application image into a reusable, source-free
# runtime base. The source image is never retagged or removed.

SOURCE_IMAGE="${1:-qasystem-backend:latest}"
TARGET_IMAGE="${2:-qasystem-backend:runtime-base-$(date -u +%Y%m%d)}"
KEEP_CONTAINER="${KEEP_CONTAINER:-0}"
WORK_DIR="${QASYSTEM_BASE_TMPDIR:-${TMPDIR:-/tmp}}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
cleanup() {
  if [[ -n "${CONTAINER_ID:-}" && "$KEEP_CONTAINER" != "1" ]]; then
    docker rm -f "$CONTAINER_ID" >/dev/null 2>&1 || true
  fi
  rm -f "${ROOTFS_TAR:-}" "${IMPORT_LOG:-}" 2>/dev/null || true
}
trap cleanup EXIT

command -v docker >/dev/null || die "docker is required"
docker image inspect "$SOURCE_IMAGE" >/dev/null 2>&1 || die "source image not found: $SOURCE_IMAGE"
[[ "$SOURCE_IMAGE" != "$TARGET_IMAGE" ]] || die "source and target image must differ"
docker image inspect "$TARGET_IMAGE" >/dev/null 2>&1 && die "target image already exists: $TARGET_IMAGE"

SOURCE_ID="$(docker image inspect "$SOURCE_IMAGE" --format '{{.Id}}')"
SOURCE_SIZE="$(docker image inspect "$SOURCE_IMAGE" --format '{{.Size}}')"
DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}')"
mkdir -p "$WORK_DIR"
AVAILABLE_BYTES="$(df -PB "$DOCKER_ROOT" | awk 'NR==2 {print $4 * 1024}')"
# During export/import the source layers, an uncompressed rootfs tar and the
# imported layer coexist. Three times the source size is a conservative floor.
REQUIRED_BYTES=$((SOURCE_SIZE * 3 + 2147483648))
(( AVAILABLE_BYTES >= REQUIRED_BYTES )) || die "insufficient Docker disk space; need about $((REQUIRED_BYTES / 1073741824)) GiB free"

printf 'Source: %s (%s, %s bytes)\n' "$SOURCE_IMAGE" "$SOURCE_ID" "$SOURCE_SIZE"
printf 'Target: %s\n' "$TARGET_IMAGE"

CONTAINER_ID="$(docker create --user 0:0 --entrypoint /bin/sh "$SOURCE_IMAGE" -c 'while :; do sleep 3600; done')"
docker start "$CONTAINER_ID" >/dev/null

# Keep dependency/runtime files and delete only mutable application content.
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
docker export "$CONTAINER_ID" -o "$ROOTFS_TAR" >/dev/null

# Restore portable runtime metadata supported by docker import. The final code
# overlay is responsible for the application command and health check.
IMPORT_ARGS=(--change "ENV QASYSTEM_RUNTIME_BASE_SOURCE=$SOURCE_ID" --change 'WORKDIR /app/backend')
while IFS= read -r env_line; do
  [[ -n "$env_line" ]] && IMPORT_ARGS+=(--change "ENV $env_line")
done < <(docker image inspect "$SOURCE_IMAGE" --format '{{range .Config.Env}}{{println .}}{{end}}')
while IFS= read -r port; do
  [[ -n "$port" ]] && IMPORT_ARGS+=(--change "EXPOSE $port")
done < <(docker image inspect "$SOURCE_IMAGE" --format '{{range $port, $_ := .Config.ExposedPorts}}{{println $port}}{{end}}')

docker import "${IMPORT_ARGS[@]}" "$ROOTFS_TAR" "$TARGET_IMAGE" 2>"$IMPORT_LOG" >/dev/null || {
  cat "$IMPORT_LOG" >&2
  die "docker import failed"
}

docker image inspect "$TARGET_IMAGE" >/dev/null || die "target image was not created"
docker run --rm --entrypoint /bin/sh "$TARGET_IMAGE" -c '
  test ! -e /app/backend/app/main.py
  test ! -e /app/agent/pyproject.toml
  python -c "import fastapi, torch, vllm"
  command -v tini >/dev/null
' >/dev/null || die "runtime-base validation failed"

printf 'Created %s\n' "$TARGET_IMAGE"
printf 'Source image remains available for rollback: %s\n' "$SOURCE_IMAGE"
printf 'Next code image should use: FROM %s\n' "$TARGET_IMAGE"
