#!/usr/bin/env bash
# Fast bridge command sender. Uses jq, no Python subprocess.
# Usage: ./send_cmd.sh <type> <payload-json>
# Example: ./send_cmd.sh add_fx '{"target_track_guid":"{...}","fx_name":"Pro-Q 4","show":true}'
set -euo pipefail
BRIDGE=~/workspace/audio/reaper-bridge
TYPE=$1
PAYLOAD=$2

# --- add_fx self-correction (no reload, no flailing) -------------------------
# Every plugin load goes through here. If fx_name doesn't match REAPER's exact
# listing, add_fx fails and a fresh session wastes minutes guessing. So we
# resolve the EXACT installed name from REAPER's on-disk plugin cache up front.
# Only touches add_fx; only substitutes on a confident single match; otherwise
# the original payload passes through unchanged (never worse than before).
if [ "$TYPE" = "add_fx" ]; then
  RAW=$(printf '%s' "$PAYLOAD" | jq -r '.fx_name // empty' 2>/dev/null || true)
  if [ -n "$RAW" ]; then
    RP="$HOME/Library/Application Support/REAPER"
    # Same robust resolver as fxload.sh. Split letter<->digit boundaries, tokenize on
    # non-alphanumerics, rejoin with a class that eats separators. Extract the clean
    # "Name (Vendor)" at line end (excludes , | { = so junk prefixes AND hyphens survive)
    # and pick the shortest clean candidate. Must NOT strip hyphens (the old regex turned
    # "Pro-Q 4 (FabFilter)" into "Q 4 (FabFilter)" and REAPER rejected it).
    QPAT=$(printf '%s' "$RAW" \
      | sed -E 's/([A-Za-z])([0-9])/\1 \2/g; s/([0-9])([A-Za-z])/\1 \2/g' \
      | tr -cs 'A-Za-z0-9' ' ' | sed -E 's/^ +//; s/ +$//; s/ +/[^A-Za-z0-9]*/g')
    NAME=$(grep -rhiE "$QPAT" "$RP"/reaper-vstplugins*.ini "$RP"/reaper-clap*.ini "$RP"/reaper-auplugins*.ini 2>/dev/null \
      | grep -oE '[^,|{=]+\([A-Za-z0-9 .&_-]+\)[[:space:]]*$' \
      | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
      | awk '{print length,$0}' | sort -n | head -1 | cut -d' ' -f2-)
    if [ -n "$NAME" ]; then
      PAYLOAD=$(printf '%s' "$PAYLOAD" | jq --arg n "$NAME" '.fx_name=$n')
    fi
  fi
fi
# ----------------------------------------------------------------------------

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
