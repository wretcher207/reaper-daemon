#!/usr/bin/env bash
# Fast bridge command sender. Uses jq, no Python subprocess.
# Usage: ./send_cmd.sh <type> <payload-json>
# Example: ./send_cmd.sh add_fx '{"target_track_guid":"{...}","fx_name":"Pro-Q 4","show":true}'
set -euo pipefail
BRIDGE=~/workspace/audio/reaper-bridge
TYPE=$1
PAYLOAD=$2
ID="${TYPE}_$(date +%s%3N)"
INBOX="$BRIDGE/inbox/${ID}.json"
OUTBOX="$BRIDGE/outbox/${ID}.json"

jq -n \
  --arg id "$ID" \
  --arg type "$TYPE" \
  --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson payload "$PAYLOAD" \
  '{id:$id,version:3,type:$type,created_by:"claude",created_at:$ts,dry_run:false,payload:$payload}' \
  > "${INBOX}.tmp"
mv "${INBOX}.tmp" "$INBOX"

for i in $(seq 1 400); do
  sleep 0.025
  if [ -f "$OUTBOX" ]; then
    cat "$OUTBOX"
    exit 0
  fi
done
echo '{"ok":false,"error":"TIMEOUT: no reply in 10s"}' >&2
exit 1
