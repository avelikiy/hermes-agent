#!/usr/bin/env bash
# Build the deployment image on a throwaway arm64 VM in GCP.
#
# Why not Cloud Build: its managed pool is x86-only (e2-*/n1-*), and this
# deployment is Apple Silicon. Emulating arm64 under QEMU turns a 3-minute
# build into tens of minutes, which defeats the point. GCE has native arm64
# (Tau T2A, Ampere Altra), so we rent one for the length of the build.
#
# Why not build locally: a local build peaks at ~15 GB of disk on a laptop that
# has hit 0 bytes free and taken Docker — and the agent — down with it. Here the
# laptop only pulls the finished ~1 GB image.
#
# The VM is deleted on every exit path, including failure and Ctrl-C: a
# forgotten VM bills by the hour for nothing.
set -euo pipefail

PROJECT="${HERMES_GCP_PROJECT:-$(cat /tmp/hermes_proj 2>/dev/null || echo hermes-build-260824)}"
ACCOUNT="${HERMES_GCP_ACCOUNT:-alexander.velikiy@gmail.com}"
ZONE="${HERMES_GCP_ZONE:-us-central1-a}"
MACHINE="${HERMES_GCP_MACHINE:-t2a-standard-4}"
REGION="${ZONE%-*}"
REPO="${HERMES_GCP_REPO:-hermes}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/hermes-agent"
TAG="${1:-latest}"
VM="hermes-build-$(date +%s)"

g() { gcloud --project="$PROJECT" --account="$ACCOUNT" "$@"; }

cleanup() {
  local rc=$?
  if g compute instances describe "$VM" --zone="$ZONE" >/dev/null 2>&1; then
    echo "==> удаляю VM $VM"
    g compute instances delete "$VM" --zone="$ZONE" --quiet >/dev/null 2>&1 || \
      echo "!! VM $VM НЕ удалена — удалите вручную, она тарифицируется" >&2
  fi
  exit $rc
}
trap cleanup EXIT INT TERM

echo "==> проект: $PROJECT | образ: $IMAGE:$TAG"
echo "==> поднимаю $MACHINE ($ZONE, arm64)"
g compute instances create "$VM" \
  --zone="$ZONE" --machine-type="$MACHINE" \
  --image-family=ubuntu-2404-lts-arm64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=60GB --boot-disk-type=pd-balanced \
  --scopes=https://www.googleapis.com/auth/cloud-platform \
  --quiet >/dev/null

echo "==> жду SSH"
for i in $(seq 1 30); do
  g compute ssh "$VM" --zone="$ZONE" --command="true" --quiet >/dev/null 2>&1 && break
  sleep 10
done

echo "==> ставлю docker на VM"
g compute ssh "$VM" --zone="$ZONE" --quiet --command="
  set -e
  sudo apt-get update -qq
  # docker-buildx too: the Dockerfile uses COPY with --chmod, which the classic
  # builder rejects outright as requiring BuildKit. Ubuntu docker.io ships the
  # legacy builder by default, so the first attempt died at step 17 of 51.
  # NOTE: no backticks or double quotes in this heredoc -- it is interpolated
  # inside a double-quoted --command=, where they would be executed.
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-buildx >/dev/null
  sudo usermod -aG docker \$USER
  gcloud auth configure-docker ${REGION}-docker.pkg.dev --quiet
" >/dev/null 2>&1

echo "==> копирую исходники (без мусора — .dockerignore тут не действует)"
tar czf /tmp/hermes-src.tgz \
  --exclude='.git' --exclude='node_modules' --exclude='.venv' \
  --exclude='backups' --exclude='*.tar.gz' --exclude='.worktrees' \
  --exclude='__pycache__' --exclude='.beads' --exclude='.great_cto' .
g compute scp /tmp/hermes-src.tgz "$VM":/tmp/src.tgz --zone="$ZONE" --quiet >/dev/null
rm -f /tmp/hermes-src.tgz

echo "==> собираю на arm64 (нативно)"
g compute ssh "$VM" --zone="$ZONE" --quiet --command="
  set -e
  mkdir -p ~/build && tar xzf /tmp/src.tgz -C ~/build
  cd ~/build
  # DOCKER_BUILDKIT=1 belts-and-braces in case the buildx plugin is missing.
  sudo DOCKER_BUILDKIT=1 docker build -t '${IMAGE}:${TAG}' -t '${IMAGE}:latest' .
  sudo gcloud auth configure-docker ${REGION}-docker.pkg.dev --quiet
  sudo docker push '${IMAGE}:${TAG}'
  [ '${TAG}' = latest ] || sudo docker push '${IMAGE}:latest'
"

echo
echo "готово: ${IMAGE}:${TAG}"
echo "  локально:  docker compose pull && docker compose up -d"
