#!/usr/bin/env bash
# disk-guard — warn over Telegram before the disk fills up.
#
# Why this runs on the HOST and not as a Hermes cron job: a full disk is
# exactly what kills the Docker daemon, so an in-container alert cannot fire
# when it matters most. This must not depend on the gateway being alive.
#
# Reads the bot token from $HERMES_HOME/.env and the destination chat from
# $HERMES_HOME/channel_directory.json (first Telegram DM), so no ids are
# hardcoded here. Override with DISK_GUARD_CHAT_ID / TELEGRAM_BOT_TOKEN.
#
# Debounced: re-alerts only when severity worsens or after DISK_GUARD_QUIET_H
# hours, so a slowly-filling disk doesn't spam the chat.
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# macOS keeps the user volume separate; on Linux (and in a container) / is the
# one that matters. Pick whichever exists so the same script runs on the Mac and
# on the server.
if [[ -n "${DISK_GUARD_VOLUME:-}" ]]; then
  VOLUME="$DISK_GUARD_VOLUME"
elif [[ -d /System/Volumes/Data ]]; then
  VOLUME="/System/Volumes/Data"
else
  VOLUME="/"
fi
WARN_GB="${DISK_GUARD_WARN_GB:-25}"
CRIT_GB="${DISK_GUARD_CRIT_GB:-10}"
QUIET_H="${DISK_GUARD_QUIET_H:-12}"
STATE="${DISK_GUARD_STATE:-$HERMES_HOME/disk-guard.state}"

# `df -g` is a BSD-ism and silently absent on Linux; -k is POSIX everywhere, so
# read KB and convert. Integer division is fine — we compare whole GB.
free_kb=$(df -Pk "$VOLUME" 2>/dev/null | awk 'NR==2 {print $4}')
[[ -z "${free_kb:-}" ]] && { echo "disk-guard: cannot read df for $VOLUME" >&2; exit 1; }
free_gb=$(( free_kb / 1024 / 1024 ))

if   (( free_gb < CRIT_GB )); then level=crit; icon="🔴"; word="КРИТИЧНО"
elif (( free_gb < WARN_GB )); then level=warn; icon="🟡"; word="мало места"
else level=ok; fi

if [[ "$level" == "ok" ]]; then
  rm -f "$STATE" 2>/dev/null   # recovered — re-arm alerting
  exit 0
fi

# --- debounce -------------------------------------------------------------
now=$(date +%s)
prev_level=""; prev_ts=0
if [[ -f "$STATE" ]]; then
  prev_level=$(awk 'NR==1{print $1}' "$STATE" 2>/dev/null)
  prev_ts=$(awk 'NR==1{print $2}' "$STATE" 2>/dev/null)
  [[ "$prev_ts" =~ ^[0-9]+$ ]] || prev_ts=0
fi
worsened=0
[[ "$prev_level" == "warn" && "$level" == "crit" ]] && worsened=1
if [[ -n "$prev_level" && $worsened -eq 0 ]]; then
  age_h=$(( (now - prev_ts) / 3600 ))
  (( age_h < QUIET_H )) && exit 0
fi

# Deliberately NO `du` here. A monitoring job must be cheap and bounded: an
# earlier version ran `du -sh $HOME/*`, which walked a 100 GB tree and took
# minutes. Everything below is O(1) stat work.
docker_raw=$(ls -lh "$HOME/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw" 2>/dev/null | awk '{print $5}')
backups_dir="${HERMES_BACKUP_DIR:-$HOME/development/Personal/Hermes/hermes-agent/backups/hermes-home}"
backups_n=$(ls -1 "$backups_dir"/hermes-home-*.tar.gz 2>/dev/null | wc -l | tr -d ' ')

read -r -d '' MSG <<EOF || true
${icon} Диск: ${word} — свободно ${free_gb} ГБ

Порог: warn <${WARN_GB} ГБ / crit <${CRIT_GB} ГБ
Docker.raw: ${docker_raw:-н/д} · бэкапов: ${backups_n:-0}

Быстро освободить:
  find ~/development -name node_modules -maxdepth 4 -prune
  rm -rf <проект>/target  <проект>/build
  docker builder prune -af
EOF

# --- send ------------------------------------------------------------------
TOKEN="${TELEGRAM_BOT_TOKEN:-}"
if [[ -z "$TOKEN" && -f "$HERMES_HOME/.env" ]]; then
  TOKEN=$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$HERMES_HOME/.env" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"' ')
fi
CHAT="${DISK_GUARD_CHAT_ID:-}"
if [[ -z "$CHAT" && -f "$HERMES_HOME/channel_directory.json" ]]; then
  CHAT=$(python3 -c "
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    sys.exit()
def find(o):
    if isinstance(o,dict):
        tg=o.get('telegram')
        if isinstance(tg,list):
            for e in tg:
                if isinstance(e,dict) and e.get('id'): return e['id']
        for v in o.values():
            r=find(v)
            if r: return r
    elif isinstance(o,list):
        for v in o:
            r=find(v)
            if r: return r
print(find(d) or '')
" "$HERMES_HOME/channel_directory.json" 2>/dev/null)
fi

if [[ -n "$TOKEN" && -n "$CHAT" ]]; then
  curl -sS -o /dev/null --max-time 20 \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT}" \
    --data-urlencode "text=${MSG}" \
    && echo "$level $now" > "$STATE"
else
  echo "disk-guard: no telegram token/chat — alert not sent" >&2
  printf '%s\n' "$MSG" >&2
  echo "$level $now" > "$STATE"
fi
