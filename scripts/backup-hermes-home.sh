#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
BACKUP_DIR="${HERMES_BACKUP_DIR:-$ROOT_DIR/backups/hermes-home}"
CONTAINER_NAME="${HERMES_CONTAINER_NAME:-hermes}"
STOP_CONTAINER="${HERMES_BACKUP_STOP_CONTAINER:-1}"

timestamp="$(date +%Y%m%d-%H%M%S)"
archive="$BACKUP_DIR/hermes-home-$timestamp.tar.gz"

if [[ ! -d "$HERMES_HOME" ]]; then
  echo "Hermes home not found: $HERMES_HOME" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

was_running=0
if [[ "$STOP_CONTAINER" == "1" ]] && docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  was_running=1
  docker stop "$CONTAINER_NAME" >/dev/null
fi

restore_container() {
  if [[ "$was_running" == "1" ]]; then
    docker start "$CONTAINER_NAME" >/dev/null
  fi
}
trap restore_container EXIT

tar \
  --exclude='.DS_Store' \
  -czf "$archive" \
  -C "$(dirname "$HERMES_HOME")" \
  "$(basename "$HERMES_HOME")"

echo "$archive"

# --- Retention -------------------------------------------------------------
# Without this the directory grows without bound: each run adds ~400 MB and
# nothing ever removes it. Four archives had reached 1.6 GB and were also being
# copied into the Docker build context, which bloated the image and eventually
# failed layer extraction on a full disk. Keep the N most recent.
KEEP="${HERMES_BACKUP_KEEP:-3}"
if [[ "$KEEP" =~ ^[0-9]+$ ]] && (( KEEP > 0 )); then
  # `ls -1t` = newest first, then drop everything past the Nth. Line-based on
  # purpose: BSD/macOS tail and sort have no -z, and these filenames are
  # generated here as hermes-home-<timestamp>.tar.gz — no spaces or newlines.
  pruned=0
  while IFS= read -r old; do
    [[ -n "$old" ]] || continue
    rm -f -- "$old" && pruned=$((pruned + 1))
  done < <(ls -1t "$BACKUP_DIR"/hermes-home-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)))
  (( pruned > 0 )) && echo "pruned $pruned old backup(s), kept $KEEP" >&2
fi

