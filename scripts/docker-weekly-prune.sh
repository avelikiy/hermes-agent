#!/usr/bin/env bash
# Weekly Docker reclaim. Docker.raw only ever grows on macOS — deleting layers
# inside the VM does not shrink the host file — so the practical defence is to
# stop cache from accumulating in the first place.
#
# Conservative on purpose: prunes only build cache and dangling images, never
# named images (the hermes-agent image must survive) and never volumes.
set -uo pipefail

docker info >/dev/null 2>&1 || { echo "$(date '+%F %T') docker not running — skipped"; exit 0; }

echo "$(date '+%F %T') before: $(df -g /System/Volumes/Data | awk 'NR==2{print $4}') GB free"

# Build cache older than a week — the big accumulator across rebuilds.
docker builder prune -af --filter until=168h 2>&1 | tail -2

# Dangling (untagged) images only. `-a` is deliberately NOT used: it would
# delete hermes-agent whenever no container happened to be running.
docker image prune -f 2>&1 | tail -2

echo "$(date '+%F %T') after:  $(df -g /System/Volumes/Data | awk 'NR==2{print $4}') GB free"
