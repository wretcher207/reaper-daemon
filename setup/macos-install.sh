#!/usr/bin/env bash
# macos-install.sh — wire the Agent Bridge into REAPER's startup on macOS.
#
# Idempotent: writes (or refreshes) a marker-delimited block in REAPER's
# Scripts/__startup.lua that auto-loads the bridge on every launch, pointing it
# at THIS clone of the repo. Re-run safely after moving the repo.
#
# After running, (re)start REAPER, then verify with:
#   ./send_reaper_command.sh commands/examples/get_context.json --wait
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_DIR="$REPO_DIR/bridge"
RES_DIR="${REAPER_RESOURCE_PATH:-$HOME/Library/Application Support/REAPER}"
STARTUP="$RES_DIR/Scripts/__startup.lua"

BEGIN="-- >>> reaper-agent-bridge (managed) >>>"
END="-- <<< reaper-agent-bridge (managed) <<<"

if [[ ! -d "$RES_DIR" ]]; then
  echo "error: REAPER resource dir not found at: $RES_DIR" >&2
  echo "       Set REAPER_RESOURCE_PATH and re-run." >&2
  exit 1
fi
if [[ ! -f "$BRIDGE_DIR/reaper_agent_bridge.lua" ]]; then
  echo "error: bridge not found at $BRIDGE_DIR/reaper_agent_bridge.lua" >&2
  exit 1
fi
mkdir -p "$RES_DIR/Scripts"

block() {
  # Escape backslashes and double quotes so BRIDGE_DIR is safe inside a Lua
  # double-quoted string. Default paths don't need this, but a repo cloned
  # into a path containing " or \ would otherwise emit broken Lua that fails
  # to load on startup (silently — the pcall in __startup swallows it).
  local esc="${BRIDGE_DIR//\\/\\\\}"
  esc="${esc//\"/\\\"}"
  cat <<LUA
$BEGIN
-- Auto-load the REAPER Agent Bridge watcher. Managed by setup/macos-install.sh.
do
  local BRIDGE_DIR = "$esc"
  local bridge_file = BRIDGE_DIR .. "/reaper_agent_bridge.lua"
  local f = io.open(bridge_file, "r")
  if f then
    f:close()
    REAPER_AGENT_BRIDGE_DIR = BRIDGE_DIR
    local ok, err = pcall(dofile, bridge_file)
    if not ok then
      reaper.ShowConsoleMsg("[agent-bridge] startup load failed: " .. tostring(err) .. "\n")
    end
  end
end
$END
LUA
}

if [[ -f "$STARTUP" ]] && grep -qF -e "$BEGIN" "$STARTUP"; then
  # Replace the existing managed block in place.
  tmp="$STARTUP.tmp"
  awk -v b="$BEGIN" -v e="$END" '
    $0==b {skip=1}
    !skip {print}
    $0==e {skip=0}
  ' "$STARTUP" > "$tmp"
  { cat "$tmp"; echo; block; } > "$STARTUP"
  rm -f "$tmp"
  echo "Updated managed bridge block in $STARTUP"
else
  { [[ -f "$STARTUP" ]] && cat "$STARTUP" && echo; block; } > "$STARTUP.new"
  mv -f "$STARTUP.new" "$STARTUP"
  echo "Installed bridge auto-start into $STARTUP"
fi

echo
echo "Done. (Re)start REAPER, then verify the bridge is live:"
echo "  cd \"$REPO_DIR\" && ./send_reaper_command.sh commands/examples/get_context.json --wait"
