#!/usr/bin/env bash
# macos-uninstall.sh: remove the Reaper Daemon auto-start block from REAPER.
#
# Idempotent: strips the marker-delimited block written by macos-install.sh
# out of REAPER's Scripts/__startup.lua. Leaves the rest of the file alone.
# Does NOT delete your clone of the repo, your recipes, or the bridge's
# inbox/outbox/archive folders — remove those yourself if you want.
#
# After running, fully quit REAPER (Cmd+Q) and relaunch to drop the bridge.
set -euo pipefail

RES_DIR="${REAPER_RESOURCE_PATH:-$HOME/Library/Application Support/REAPER}"
STARTUP="$RES_DIR/Scripts/__startup.lua"

BEGIN="-- >>> reaper-agent-bridge (managed) >>>"
END="-- <<< reaper-agent-bridge (managed) <<<"

if [[ ! -f "$STARTUP" ]]; then
  echo "Nothing to remove: $STARTUP does not exist."
  exit 0
fi

if ! grep -qF -e "$BEGIN" "$STARTUP"; then
  echo "Nothing to remove: no managed bridge block found in $STARTUP."
  exit 0
fi

tmp="$STARTUP.tmp"
awk -v b="$BEGIN" -v e="$END" '
  $0==b {skip=1; next}
  $0==e {skip=0; next}
  !skip {print}
' "$STARTUP" > "$tmp"

# Trim a single trailing blank line if removal left one.
# (awk preserves everything else exactly.)
sed -e '${/^$/d}' "$tmp" > "$tmp.2"
mv -f "$tmp.2" "$STARTUP"
rm -f "$tmp"

echo "Removed managed bridge block from $STARTUP"
echo
echo "Quit REAPER (Cmd+Q) and relaunch to finish unloading."
echo "The bridge files in your clone are untouched — delete the clone folder"
echo "if you want to remove them too."
