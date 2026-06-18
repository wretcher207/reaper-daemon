#!/usr/bin/env bash
# bridge_status.sh — liveness preflight for the Reaper Daemon bridge.
#
# The bridge is a deferred Lua loop inside REAPER. A command landing in inbox/
# is NOT proof it's alive; only a FRESH heartbeat is. The loop can die silently
# (machine sleep, REAPER halting scripts, a moved repo) and leave a stale
# heartbeat behind that looks fine at a glance. This gate makes that loud.
#
# Run it BEFORE claiming "connected" or sending real commands:
#   ./bridge_status.sh            # human-readable, exit 0 = live, 1 = not
#   ./bridge_status.sh --quiet    # exit code only
#
# Freshness: the loop writes a heartbeat at least every 5s, so anything older
# than 15s means the loop is not running right now.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEARTBEAT="$SCRIPT_DIR/bridge/heartbeat.json"
FRESH_SECS=15
QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

say() { [[ $QUIET -eq 0 ]] && echo "$@" || true; }

if ! pgrep -x REAPER >/dev/null 2>&1; then
  say "DEAD: REAPER is not running. Launch REAPER (auto-start will load the bridge)."
  exit 1
fi

if [[ ! -f "$HEARTBEAT" ]]; then
  say "DEAD: REAPER is up but no heartbeat file. Bridge never loaded."
  say "      Fix: cd \"$SCRIPT_DIR\" && ./setup/macos-install.sh, then relaunch REAPER."
  exit 1
fi

# Age from file mtime (authoritative); alive_at/project for the human line.
# NOTE: Python passed via -c (single-quoted), NOT a heredoc. macOS system bash
# (3.2) cannot parse a heredoc nested inside <(...) process substitution and
# errors with "bad substitution". Keep this body free of apostrophes so the
# single-quoted -c string is not terminated early.
read -r AGE BUSY ALIVE PROJ < <(python3 -c '
import json, os, sys, time
p = sys.argv[1]
age = time.time() - os.path.getmtime(p)
proj, alive, busy = "?", "?", "none"
try:
    d = json.load(open(p))
    proj = d.get("project_name", "?")
    alive = d.get("alive_at", "?")
    busy = d.get("busy") or "none"
except Exception:
    pass
# busy + alive are single tokens; proj goes last so spaces in it do not shift fields.
print(f"{age:.0f} {busy} {alive} {proj}")
' "$HEARTBEAT")

if (( AGE <= FRESH_SECS )); then
  say "CONNECTED: heartbeat ${AGE}s old | project: ${PROJ} | alive_at ${ALIVE}"
  exit 0
fi

if [[ "$BUSY" != "none" ]]; then
  say "BUSY: ${BUSY} in progress | heartbeat ${AGE}s old | project: ${PROJ}"
  say "      A synchronous ${BUSY} blocks the loop on purpose; this is not a death."
  exit 0
fi

say "STALE: heartbeat ${AGE}s old (>${FRESH_SECS}s). The bridge loop has stopped."
say "       Last alive_at: ${ALIVE} | project: ${PROJ}"
say "       Revive: in REAPER re-run the bridge action (Actions list), or relaunch"
say "       REAPER so __startup.lua reloads it. Then re-run this check."
exit 1
