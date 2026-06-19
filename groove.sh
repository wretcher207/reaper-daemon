#!/usr/bin/env bash
# groove.sh — author a beat in the groovekit DSL, render humanized MIDI, and
# insert it onto a REAPER track in one command.
#
# Usage:
#   ./groove.sh <dsl_file> --track <NAME> [--position SEC] [--tempo BPM] [--seed N]
#
# The DSL file carries @tempo/@map/sections. --tempo here is optional and only
# overrides the tempo passed to REAPER's item placement (the MIDI itself keeps
# the DSL @tempo); pass it when the project tempo differs.
set -euo pipefail

BRIDGE="$( cd "$( dirname "$0" )" && pwd )"
GROOVEGEN="$BRIDGE/skills/drum-apparatus/groovegen.py"
SEND="$BRIDGE/send_cmd.sh"

DSL="${1:?Usage: groove.sh <dsl_file> --track <NAME> [--position SEC] [--tempo BPM] [--seed N]}"
shift

if [[ ! -f "$DSL" ]]; then
  echo "[groove] ERROR: DSL file not found: $DSL" >&2
  exit 1
fi

TRACK=""
POSITION=""        # empty = land at the REAPER edit cursor (DAW default); a value forces that time in seconds
TEMPO=""
SEED=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --track)    TRACK="$2";    shift 2 ;;
    --position) POSITION="$2"; shift 2 ;;
    --tempo)    TEMPO="$2";    shift 2 ;;
    --seed)     SEED="$2";     shift 2 ;;
    *) echo "[groove] ERROR: unknown arg '$1'" >&2; exit 1 ;;
  esac
done

# Position object: no --position => place at the edit cursor (so a second groove
# lands where the cursor is, NOT slammed onto bar 1 stacking the first one).
# Pass --position SEC only when an exact time is wanted.
if [[ -n "$POSITION" ]]; then
  POS_OBJ=$(jq -n --argjson s "$POSITION" '{type:"time", seconds:$s}')
else
  POS_OBJ='{"type":"cursor"}'
fi

# TRACK stays empty — payload will use use_selected_track if unset

MIDI="/tmp/groove_$(date +%s%3N).mid"

echo "[groove] Rendering DSL: $DSL"
GEN_ARGS=(--dsl "$DSL" --out "$MIDI")
[[ -n "$SEED" ]] && GEN_ARGS+=(--seed "$SEED")
python3 "$GROOVEGEN" "${GEN_ARGS[@]}"

if [[ -n "$TRACK" ]]; then
  PAYLOAD=$(jq -n --arg t "$TRACK" --arg path "$MIDI" --argjson pos "$POS_OBJ" \
    '{target_track_name:$t, midi_path:$path, position:$pos}')
else
  PAYLOAD=$(jq -n --arg path "$MIDI" --argjson pos "$POS_OBJ" \
    '{use_selected_track:true, midi_path:$path, position:$pos}')
fi
RESULT=$("$SEND" insert_midi_file "$PAYLOAD")

OK=$(echo "$RESULT" | jq -r '.ok')
if [[ "$OK" != "true" ]]; then
  echo "[groove] FAILED: $(echo "$RESULT" | jq -r '.error // "unknown error"')" >&2
  rm -f "$MIDI"
  exit 1
fi

echo "$RESULT" | jq -r --arg pos "$POSITION" \
  '"[groove] OK: inserted on \(.data.track.name // "track") at \($pos)s"'
rm -f "$MIDI"
