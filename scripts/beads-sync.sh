#!/usr/bin/env bash
# beads-sync — push the local Beads task database to its private Dolt remote.
#
# Why this runs on a timer rather than from Beads' own git hooks: the hooks in
# .beads/hooks/ were never installed into .git/hooks, and even installed they
# would be the wrong mechanism here. Task writes happen through `bd` at any
# time, not only at `git commit`, and commits in this repo are routinely made
# with --no-verify — so hook-driven sync would silently skip most changes.
#
# Until this existed the database had no off-machine copy at all: `.beads/` is
# git-ignored, backup-hermes-home.sh only covers ~/.hermes, and Beads' inherited
# Dolt remote pointed at the upstream NousResearch repo, where every push was
# rejected. The remote now points at a PRIVATE repo — deliberately not the
# public fork, because the task DB records unresolved security findings about a
# live deployment.
set -uo pipefail

# launchd starts jobs with a bare PATH (/usr/bin:/bin:/usr/sbin:/sbin), so the
# Homebrew prefix that holds `bd` is absent and the first run failed with
# "bd not on PATH". Prepend the usual prefixes rather than hardcoding one, so
# this keeps working on both Apple-silicon and Intel Homebrew layouts.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO_DIR="${BEADS_REPO_DIR:-$HOME/development/Personal/Hermes/hermes-agent}"
cd "$REPO_DIR" 2>/dev/null || { echo "beads-sync: no repo at $REPO_DIR" >&2; exit 1; }

command -v bd >/dev/null 2>&1 || { echo "beads-sync: bd not on PATH" >&2; exit 1; }

# Change detection has to work for both remote kinds. A git+https remote keeps
# refs/dolt/data, which `git ls-remote` can read; a file:// remote is a plain
# directory of Dolt .darc chunks with no git refs at all, so ls-remote returns
# nothing and every run looked like "no change" even when it had just pushed.
# Fingerprint whatever the remote actually is instead.
REMOTE_URL="$(bd dolt remote list 2>/dev/null | awk 'NR==1{sub(/^git\+/,"",$2); print $2}')"
fingerprint() {
  case "$REMOTE_URL" in
    file://*) find "${REMOTE_URL#file://}" -name '*.darc' -newer /dev/null -exec ls -l {} + 2>/dev/null | md5 2>/dev/null || echo none ;;
    *)        git ls-remote "$REMOTE_URL" refs/dolt/data 2>/dev/null | awk '{print $1}' ;;
  esac
}

before="$(fingerprint)"

if ! out=$(bd dolt push 2>&1); then
  echo "beads-sync: push failed: $out" >&2
  exit 1
fi

after="$(fingerprint)"
if [[ "$before" != "$after" ]]; then
  echo "beads-sync: pushed ($(date '+%F %T')) -> ${REMOTE_URL}"
fi
