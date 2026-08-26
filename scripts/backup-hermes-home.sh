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

# --- Secrets ---------------------------------------------------------------
# ~/.hermes holds live credentials: .env (provider keys, bot tokens),
# secrets/ (service-account JSON), auth.json (provider credential store).
# A plain tar of the directory copies all of them into an unencrypted archive
# that then sits on disk indefinitely, gets synced, and — as happened here —
# lands in a Docker build context. The backup is for state you cannot
# regenerate; keys are not that, and they are the one thing worth stealing.
#
# Excluded by default. Set HERMES_BACKUP_INCLUDE_SECRETS=1 to override, which
# is a reasonable choice for an archive you are about to encrypt yourself.
INCLUDE_SECRETS="${HERMES_BACKUP_INCLUDE_SECRETS:-0}"
# Expanded below as ${arr[@]+"${arr[@]}"}: under `set -u`, bash 3.2 (what
# macOS ships) treats "${arr[@]}" on an empty array as an unbound variable and
# aborts — which broke exactly the opt-in path this array is empty on.
secret_excludes=()
if [[ "$INCLUDE_SECRETS" != "1" ]]; then
  base="$(basename "$HERMES_HOME")"
  secret_excludes=(
    --exclude="$base/.env"
    --exclude="$base/.env.*"
    --exclude="$base/auth.json"
    --exclude="$base/secrets"
  )
fi

tar \
  --exclude='.DS_Store' \
  ${secret_excludes[@]+"${secret_excludes[@]}"} \
  -czf "$archive" \
  -C "$(dirname "$HERMES_HOME")" \
  "$(basename "$HERMES_HOME")"

echo "$archive"

if [[ "$INCLUDE_SECRETS" != "1" ]]; then
  # Say this loudly: a restore from this archive starts an agent with no
  # provider keys, and silently discovering that later is worse than the
  # warning being noisy now.
  echo "note: credentials excluded (.env, auth.json, secrets/) — restore will need them re-supplied." >&2
  echo "      keep them somewhere encrypted; HERMES_BACKUP_INCLUDE_SECRETS=1 overrides." >&2
fi

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

