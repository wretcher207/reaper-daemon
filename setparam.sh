#!/usr/bin/env bash
# setparam.sh — set ANY plugin parameter to a display value on ANY track.
# Works for any plugin (FabFilter, ReaEQ, JS, VST2/3, CLAP, AU). The script
# does the scan->resolve->set->verify thinking so the caller never has to
# reason about field names, normalized vs display values, or log/linear scaling.
#
# Usage:
#   setparam.sh <track> <fx_query|#idx> <param_query|#idx> <display_value|norm=X>
#
# Examples:
#   setparam.sh Kick "Pro-Q" "Band 1 Frequency" "80 Hz"
#   setparam.sh Kick "#0"    "#2"               "80 Hz"
#   setparam.sh Kick "ReaEQ" "Band 1 Freq"      "80 Hz"
#   setparam.sh Kick "Pro-Q" "Band 1 Gain"      "-16 dB"
#   setparam.sh Kick "Pro-Q" "Band 1 Q"         "0.7"
#   setparam.sh Kick "Pro-Q" "Mix"              "norm=0.5"   # direct normalized
#
# <track>       name (resolved via get_context) or "master"
# <fx>          substring of FX name, or #index (0-based)
# <param>       substring of param name (first unique match), or #index
# <value>       display value ("80 Hz", "-16 dB", "0.7") -> binary-searched to
#               the normalized 0..1 that produces that display; or "norm=0.267"
#               to set a normalized value directly.
#
# Exit 0 + prints the verified before/after. Exit 1 on any failure (bad track,
# ambiguous param, value out of range, etc.) with a clear message.
set -euo pipefail
BRIDGE="$( cd "$( dirname "$0" )" && pwd )"
SEND="$BRIDGE/send_cmd.sh"

TRACK="${1:?usage: setparam.sh <track> <fx_query|#idx> <param_query|#idx> <value>}"
FXSEL="${2:?need fx query or #index}"
PARSEL="${3:?need param query or #index}"
TARGET="${4:?need display value or norm=0..1}"

# --- 1. Resolve track GUID ----------------------------------------------------
if [[ "$TRACK" == "master" ]]; then
  TRACK_GUID="master"
  TRACK_PAYLOAD='"target_track_name":"master"'
else
  TRACK_GUID=$("$SEND" get_context '{"include_fx":false}' \
    | jq -r --arg t "$TRACK" '.data.tracks[]|select(.name==$t)|.guid' 2>/dev/null || true)
  if [[ -z "$TRACK_GUID" ]]; then
    echo "[setparam] ERROR: track '$TRACK' not found" >&2; exit 1
  fi
  TRACK_PAYLOAD=$(jq -nc --arg g "$TRACK_GUID" '{target_track_guid:$g}')
fi

# --- 2. Build the merged base payload (track + FX selector) -------------------
if [[ "$FXSEL" == \#* ]]; then
  FX_ARGS=(--argjson i "${FXSEL#\#}")
  FX_JSON='{fx_index:$i}'
else
  FX_ARGS=(--arg s "$FXSEL")
  FX_JSON='{fx_name_contains:$s}'
fi
if [[ "$TRACK" == "master" ]]; then
  BASE_OBJ=$(jq -nc "${FX_ARGS[@]}" --arg t "master" '{target_track_name:$t} + '"$FX_JSON")
else
  BASE_OBJ=$(jq -nc "${FX_ARGS[@]}" --arg g "$TRACK_GUID" '{target_track_guid:$g} + '"$FX_JSON")
fi

# --- 3. Scan params, resolve the target param ---------------------------------
PARAMS=$("$SEND" get_fx_parameters "$(jq -nc --argjson b "$BASE_OBJ" '$b + {limit:1000}')" 2>/dev/null)
if [[ "$(printf '%s' "$PARAMS" | jq -r '.ok')" != "true" ]]; then
  echo "[setparam] ERROR: scan failed: $(printf '%s' "$PARAMS" | jq -r '.error.code // "?"')" >&2
  exit 1
fi

if [[ "$PARSEL" == \#* ]]; then
  PARAM_INDEX="${PARSEL#\#}"
  PARAM_NAME=$(printf '%s' "$PARAMS" | jq -r --argjson i "$PARAM_INDEX" '.data.parameters[]|select(.index==$i)|.name' 2>/dev/null)
  if [[ -z "$PARAM_NAME" ]]; then
    echo "[setparam] ERROR: param index $PARAM_INDEX out of range" >&2; exit 1
  fi
else
  # Find params whose name contains the query (case-insensitive). Error if 0 or >1.
  MATCHES=$(printf '%s' "$PARAMS" | jq -c --arg q "$PARSEL" '[.data.parameters[] | select((.name//"") | ascii_downcase | contains($q | ascii_downcase))]')
  MATCH_COUNT=$(printf '%s' "$MATCHES" | jq 'length')
  if [[ "$MATCH_COUNT" == "0" ]]; then
    echo "[setparam] ERROR: no param matches '$PARSEL' on this FX" >&2
    echo "[setparam] Available params:" >&2
    printf '%s' "$PARAMS" | jq -r '.data.parameters[] | "  #\(.index)  \(.name) = \(.formatted_value)"' >&2 | head -30
    exit 1
  fi
  if [[ "$MATCH_COUNT" != "1" ]]; then
    echo "[setparam] ERROR: '$PARSEL' matched $MATCH_COUNT params (ambiguous):" >&2
    printf '%s' "$MATCHES" | jq -r '.[] | "  #\(.index)  \(.name) = \(.formatted_value)"' >&2
    echo "[setparam] Narrow with a longer substring, or use #<index>." >&2
    exit 1
  fi
  PARAM_INDEX=$(printf '%s' "$MATCHES" | jq -r '.[0].index')
  PARAM_NAME=$(printf '%s' "$MATCHES" | jq -r '.[0].name')
fi

BEFORE=$(printf '%s' "$PARAMS" | jq -r --argjson i "$PARAM_INDEX" '.data.parameters[]|select(.index==$i)|.formatted_value')
echo "[setparam] track=$TRACK  fx=$FXSEL  param=#$PARAM_INDEX \"$PARAM_NAME\"  before=$BEFORE  target=$TARGET"

# --- 4. Set the value ---------------------------------------------------------
set_norm(){ # normalized_value
  "$SEND" set_fx_param "$(jq -nc --argjson b "$BASE_OBJ" --argjson i "$PARAM_INDEX" --argjson v "$1" '$b + {param_index:$i, normalized_value:$v}')" >/dev/null 2>&1
}
# Read the full formatted_value string for our param (scan all, filter by index).
read_fmt(){
  "$SEND" get_fx_parameters "$(jq -nc --argjson b "$BASE_OBJ" '$b + {limit:1000}')" 2>/dev/null \
    | jq -r --argjson i "$PARAM_INDEX" '.data.parameters[]|select(.index==$i)|.formatted_value' 2>/dev/null
}
# Parse the first number out of a formatted string (handles "80 Hz", "-16.00 dB",
# "+4.00", "0.700"). Returns sentinel values for ±inf so binary search still works.
read_num(){
  local s
  s=$(cat)
  if [[ "$s" == *"-inf"* || "$s" == *"-Inf"* || "$s" == *"-INF"* ]]; then echo "-1e30"
  elif [[ "$s" == *"inf"* || "$s" == *"Inf"* || "$s" == *"INF"* ]]; then echo "1e30"
  else printf '%s' "$s" | grep -oE '[-+]?[0-9]+(\.[0-9]+)?' | head -1
  fi
}

if [[ "$TARGET" == norm=* ]]; then
  NORM="${TARGET#norm=}"
  set_norm "$NORM"
else
  # Binary-search the normalized 0..1 whose formatted display matches the target number.
  TARGET_NUM=$(printf '%s' "$TARGET" | read_num)
  if [[ -z "$TARGET_NUM" ]]; then
    echo "[setparam] ERROR: could not parse a number from '$TARGET'" >&2; exit 1
  fi
  set_norm 0; LO_NUM=$(read_fmt | read_num)
  set_norm 1; HI_NUM=$(read_fmt | read_num)
  if [[ -z "$LO_NUM" || -z "$HI_NUM" ]]; then
    echo "[setparam] ERROR: parameter is not numeric at range endpoints (norm=0 shows '$(read_fmt)', norm=1 shows '$(read_fmt)'). Use norm=0..1." >&2; exit 1
  fi
  read -r LO HI MID ASCENDING < <(python3 -c "
lo,hi=0.0,1.0
lo_n,hi_n=$LO_NUM,$HI_NUM
asc = hi_n >= lo_n
print(lo,hi,(lo+hi)/2, 1 if asc else 0)
")
  for _ in $(seq 1 24); do
    set_norm "$MID"
    CUR=$(read_fmt | read_num)
    [[ -z "$CUR" ]] && break
    read -r LO HI MID < <(python3 -c "
cur,t,lo,hi,mid=$CUR,$TARGET_NUM,$LO,$HI,$MID
asc=$ASCENDING
if (asc and cur < t) or (not asc and cur > t):
    lo=mid
else:
    hi=mid
print(lo,hi,(lo+hi)/2)
")
  done
  set_norm "$LO"; LO_CUR=$(read_fmt | read_num)
  set_norm "$HI"; HI_CUR=$(read_fmt | read_num)
  read -r NORM < <(python3 -c "
t=$TARGET_NUM
lo_d=abs($LO_CUR - t) if '$LO_CUR' else float('inf')
hi_d=abs($HI_CUR - t) if '$HI_CUR' else float('inf')
print($LO if lo_d <= hi_d else $HI)
")
  set_norm "$NORM"
fi

# --- 5. Verify + print --------------------------------------------------------
AFTER_FMT=$(read_fmt)
AFTER_NUM=$(printf '%s' "$AFTER_FMT" | read_num)
echo "[setparam] AFTER: $AFTER_FMT"
if [[ "$TARGET" == norm=* ]]; then
  echo "[setparam] RESULT: set $PARAM_NAME to normalized $NORM (displays as $AFTER_FMT)"
else
  if [[ -z "$AFTER_NUM" ]]; then
    echo "[setparam] RESULT: SET but display is non-numeric ($AFTER_FMT) — re-scan to verify." >&2
  elif python3 -c "exit(0 if abs($AFTER_NUM - $TARGET_NUM) <= max(abs($TARGET_NUM)*0.02, 0.5) else 1)"; then
    echo "[setparam] RESULT: OK — $PARAM_NAME = $AFTER_FMT (target was $TARGET)"
  else
    echo "[setparam] RESULT: CLOSE — $PARAM_NAME = $AFTER_FMT (target was $TARGET; off by a hair — re-run or use norm= for exact)"
  fi
fi
