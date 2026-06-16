#!/usr/bin/env bash
# send_reaper_command.sh — macOS port of send_reaper_command.ps1
#
# Sends one JSON command file to the bridge's inbox and (optionally) waits for
# the matching reply in outbox. Auto-fills id / created_at / created_by, writes
# atomically (.tmp then rename), and prints the result JSON when --wait is set.
#
# Usage:
#   ./send_reaper_command.sh commands/examples/get_context.json --wait
#   ./send_reaper_command.sh path/to/cmd.json --wait --timeout 30000
#   ./send_reaper_command.sh path/to/cmd.json            # fire-and-forget
#
# An id of "<auto>" (or a missing id) forces a fresh timestamped id.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_ROOT="$SCRIPT_DIR"
WAIT=0
TIMEOUT_MS=30000
COMMAND_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wait) WAIT=1; shift ;;
    --timeout) TIMEOUT_MS="$2"; shift 2 ;;
    --bridge-root) BRIDGE_ROOT="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) COMMAND_PATH="$1"; shift ;;
  esac
done

if [[ -z "$COMMAND_PATH" || ! -f "$COMMAND_PATH" ]]; then
  echo "error: command JSON file not found: '$COMMAND_PATH'" >&2
  exit 1
fi

# Fill id/created_at/created_by and emit the finalized command + its id.
# python3 ships with macOS; keeps us free of a jq dependency.
read -r ID FINAL_JSON < <(python3 - "$COMMAND_PATH" <<'PY'
import json, sys, datetime, secrets

with open(sys.argv[1], "r", encoding="utf-8") as f:
    cmd = json.load(f)

cid = str(cmd.get("id", "")).strip()
if not cid or cid == "<auto>":
    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    cmd["id"] = f"manual-{stamp}-{secrets.token_hex(2)}"

cmd["created_at"] = datetime.datetime.now().astimezone().isoformat()
if not cmd.get("created_by"):
    cmd["created_by"] = "manual"

# One line: "<id> <compact-json>" — the json has no spaces after separators,
# so the first token is always the id.
print(cmd["id"], json.dumps(cmd, separators=(",", ":")))
PY
)

INBOX="$BRIDGE_ROOT/inbox/$ID.json"
OUTBOX="$BRIDGE_ROOT/outbox/$ID.json"

# Atomic write: tmp then rename. Poller skips *.tmp.
printf '%s' "$FINAL_JSON" > "$INBOX.tmp"
mv -f "$INBOX.tmp" "$INBOX"
echo "Sent command $ID"

if [[ "$WAIT" -eq 0 ]]; then
  exit 0
fi

# Poll outbox until the reply lands or we time out.
elapsed=0
while [[ "$elapsed" -lt "$TIMEOUT_MS" ]]; do
  if [[ -f "$OUTBOX" ]]; then
    cat "$OUTBOX"
    echo
    exit 0
  fi
  sleep 0.25
  elapsed=$((elapsed + 250))
done

echo "error: timed out after ${TIMEOUT_MS}ms waiting for $OUTBOX" >&2
echo "       (is the bridge running in REAPER? check bridge/heartbeat.json)" >&2
exit 1
