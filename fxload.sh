#!/usr/bin/env bash
# Fast one-shot FX loader. Resolves the EXACT REAPER-listed plugin name from the
# installed-plugin cache (no guessing, no failed add_fx round-trips), then adds it.
# Usage: ./fxload.sh "<name query>" "<track name|master>"
set -euo pipefail
BRIDGE=~/workspace/audio/reaper-bridge
RP="$HOME/Library/Application Support/REAPER"
Q="$1"; TRACK="${2:-master}"

# Make the query punctuation/space-insensitive. Split letter<->digit boundaries too
# (so "Q4" becomes "Q 4"), then tokenize on non-alphanumerics and rejoin tokens with a
# class that eats any separators. "pro q 4" / "pro-q 4" / "Pro Q4" / "Pro Q4 FabFilter"
# all match the cache's "Pro-Q 4 (FabFilter)".
QPAT=$(printf '%s' "$Q" \
  | sed -E 's/([A-Za-z])([0-9])/\1 \2/g; s/([0-9])([A-Za-z])/\1 \2/g' \
  | tr -cs 'A-Za-z0-9' ' ' \
  | sed -E 's/^ +//; s/ +$//; s/ +/[^A-Za-z0-9]*/g')

# Pull display names from the VST/CLAP/AU caches; the name after the first '=' line
# value or the trailing field is what TrackFX_AddByName expects.
# A display name is "Name (Vendor)" at END of a cache line. Exclude , | { = from the
# name part so junk prefixes like CLAP's "0|" or "{GUID," are never captured. Pick the
# SHORTEST clean candidate (the bare "Name (Vendor)", not a vendor-doubled VST2 variant).
NAME=$(grep -rhiE "$QPAT" "$RP"/reaper-vstplugins*.ini "$RP"/reaper-clap*.ini "$RP"/reaper-auplugins*.ini 2>/dev/null \
  | grep -oE '[^,|{=]+\([A-Za-z0-9 .&_-]+\)[[:space:]]*$' \
  | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
  | awk '{ print length, $0 }' | sort -n | head -1 | cut -d' ' -f2-)

if [ -z "$NAME" ]; then
  echo "NO_MATCH: nothing installed matching '$Q'" >&2; exit 1
fi
echo "[fxload] resolved '$Q' -> $NAME  (track: $TRACK)"
"$BRIDGE/send_cmd.sh" add_fx "$(jq -n --arg n "$NAME" --arg t "$TRACK" '{target_track_name:$t,fx_name:$n,show:false}')"
