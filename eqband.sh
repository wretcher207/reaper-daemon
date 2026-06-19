#!/usr/bin/env bash
# Set ONE EQ band correctly on a band-style EQ (FabFilter Pro-Q, ReaEQ, etc.) and
# VERIFY it took. Fixes the recurring failure where a session sets Frequency/Gain but
# never marks the band Used, leaving the curve FLAT, then falsely reports "done".
#
# Usage:
#   eqband.sh <track> <fx_query|#idx> <band#> <freqHz> <gainDb> [Q]
# Examples:
#   eqband.sh KickSum "Pro-Q" 1 80 +4 0.7
#   eqband.sh KickSum "#0"    2 250 -3 1.2      # target FX by index when duplicates exist
#
# Param indices are discovered at RUNTIME from get_fx_parameters by name
# ("Band N Used/Frequency/Gain/Q") — nothing about the plugin is hardcoded.
set -euo pipefail
BRIDGE="$( cd "$( dirname "$0" )" && pwd )"
SEND="$BRIDGE/send_cmd.sh"

TRACK="${1:?usage: eqband.sh <track> <fx_query|#idx> <band#> <freqHz> <gainDb> [Q]}"
FXSEL="${2:?need fx query or #index}"
BAND="${3:?need band number}"
FREQ="${4:?need frequency Hz}"
GAIN="${5:?need gain dB}"
Q="${6:-}"

# FX selector as a jq object fragment, merged into every payload.
if [[ "$FXSEL" == \#* ]]; then
  FXOBJ=$(jq -n --argjson i "${FXSEL#\#}" '{fx_index:$i}')
else
  FXOBJ=$(jq -n --arg s "$FXSEL" '{fx_name_contains:$s}')
fi
base(){ jq -nc --arg t "$TRACK" --argjson fx "$FXOBJ" '{target_track_name:$t} + $fx'; }

# 1. Discover this band's param indices by name.
PARAMS=$("$SEND" get_fx_parameters "$(base | jq -c '. + {include_values:true,max_params:2000}')" 2>/dev/null)
if [[ "$(printf '%s' "$PARAMS" | jq -r '.ok')" != "true" ]]; then
  echo "[eqband] FAILED reading params: $(printf '%s' "$PARAMS" | jq -r '.error.code // "?"')" >&2
  echo "[eqband] (AMBIGUOUS_FX = duplicate instances; target one with #0 / #1)" >&2
  exit 1
fi
IDX=$(printf '%s' "$PARAMS" | python3 -c "
import json,sys
d=json.load(sys.stdin)
def walk(o):
    if isinstance(o,dict):
        if 'parameters' in o: return o['parameters']
        for v in o.values():
            r=walk(v)
            if r is not None: return r
    if isinstance(o,list):
        for v in o:
            r=walk(v)
            if r is not None: return r
ps=walk(d) or []
import re
B=$BAND
# Match this band's params across EQ naming conventions: 'Band 1 Frequency',
# 'EQ Band 1 Freq', '1: Frequency', 'Band1 Gain', etc. Require the band number, then
# the role keyword. Nothing plugin-specific is hardcoded.
def band_match(n):
    return re.search(r'(^|\b)(eq\s*)?band\s*0*%d\b' % B, n) or re.match(r'\s*0*%d\s*[:\-]' % B, n)
roles=[('used',  r'\bused\b'),
       ('freq',  r'\b(freq(uency)?)\b'),
       ('gain',  r'\bgain\b'),
       ('q',     r'\b(q|bandwidth|width)\b')]
out={}
for p in ps:
    n=(p.get('name') or '').strip().lower()
    if not band_match(n): continue
    for key,pat in roles:
        if key not in out and re.search(pat,n):
            out[key]=p.get('index')
print(json.dumps(out))
")
get(){ printf '%s' "$IDX" | python3 -c "import json,sys;v=json.load(sys.stdin).get('$1');print('' if v is None else v)"; }
USED=$(get used); IFREQ=$(get freq); IGAIN=$(get gain); IQ=$(get q)
if [[ -z "$USED" || -z "$IFREQ" || -z "$IGAIN" ]]; then
  echo "[eqband] could not find Band $BAND Used/Frequency/Gain params. This EQ names bands differently." >&2
  exit 1
fi

# The bridge REJECTS formatted_value for scaled params (ok=false) — it only takes
# normalized_value (0..1). So convert each target to normalized. Standard parametric-EQ
# ranges (verified against Pro-Q: norm 0.26 -> 80.18 Hz): freq log 10Hz..30kHz,
# gain linear -30..+30 dB, Q log 0.025..40. Then SEARCH-correct against the read-back so
# it's exact regardless of the plugin's actual range.
setn(){ "$SEND" set_fx_param "$(base | jq -c --argjson i "$1" --argjson v "$2" '. + {param_index:$i,normalized_value:$v}')" >/dev/null; }
readfmt(){ # param_index -> numeric value parsed from its formatted string
  "$SEND" get_fx_parameters "$(base | jq -c --argjson i "$1" '. + {include_values:true,max_params:2000}')" 2>/dev/null \
  | python3 -c "
import json,sys,re
d=json.load(sys.stdin)
def walk(o):
    if isinstance(o,dict):
        if 'parameters' in o: return o['parameters']
        for v in o.values():
            r=walk(v)
            if r is not None: return r
    if isinstance(o,list):
        for v in o:
            r=walk(v)
            if r is not None: return r
ps={p.get('index'):p for p in (walk(d) or [])}
s=str(ps.get($1,{}).get('formatted_value',''))
m=re.search(r'-?[0-9.]+',s)
v=float(m.group()) if m else 0.0
if 'khz' in s.lower(): v*=1000
print(v)
"; }
# Seed with the analytic normalized, verify against the real read-back, and binary-search
# correct if the plugin's range differs. Monotonic params, so direction is unambiguous.
# When the seed is right (Pro-Q), it breaks on iteration 1 — fast.
dial(){ # param_index target_number seed_norm
  local idx="$1" target="$2" mid="$3" lo=0 hi=1 cur
  for _ in 1 2 3 4 5 6 7; do
    setn "$idx" "$mid"
    cur=$(readfmt "$idx")
    python3 -c "import sys;sys.exit(0 if abs($cur-($target))<=abs($target)*0.01+1e-9 else 1)" && break
    read lo hi mid <<<"$(python3 -c "
cur,t,lo,hi,mid=$cur,$target,$lo,$hi,$mid
lo,hi=(mid,hi) if cur<t else (lo,mid)
print(lo,hi,(lo+hi)/2)")"
  done
}

# 2. Mark band Used FIRST (else the curve stays flat), then dial freq/gain/Q exact.
setn "$USED" 1.0
F=$(python3 -c "import math;f=float('${FREQ}');print(max(0,min(1,math.log(f/10)/math.log(3000))))")
G=$(python3 -c "g=float('${GAIN}');print(max(0,min(1,(g+30)/60)))")
dial "$IFREQ" "$FREQ" "$F"
dial "$IGAIN" "$GAIN" "$G"
if [[ -n "$Q" && -n "$IQ" ]]; then
  Qn=$(python3 -c "import math;q=float('${Q}');print(max(0,min(1,math.log(q/0.025)/math.log(40/0.025))))")
  dial "$IQ" "$Q" "$Qn"
fi

# 3. VERIFY — read the band back and print real values. Never claim success blind.
"$SEND" get_fx_parameters "$(base | jq -c '. + {include_values:true,max_params:2000}')" 2>/dev/null \
| python3 -c "
import json,sys
d=json.load(sys.stdin)
def walk(o):
    if isinstance(o,dict):
        if 'parameters' in o: return o['parameters']
        for v in o.values():
            r=walk(v)
            if r is not None: return r
    if isinstance(o,list):
        for v in o:
            r=walk(v)
            if r is not None: return r
ps={p.get('index'):p for p in (walk(d) or [])}
def fv(i):
    if i=='' : return '-'
    p=ps.get(int(i))
    return p.get('formatted_value', p.get('value')) if p else '?'
used=fv('$USED')
line=f\"[eqband] Band $BAND -> Used={used}  Freq={fv('$IFREQ')}  Gain={fv('$IGAIN')}\"
if '$IQ': line+=f\"  Q={fv('$IQ')}\"
print(line)
ok = str(used).strip().lower() in ('used','on','enabled','1','true')
print('[eqband] RESULT:', 'BAND IS LIVE on the curve.' if ok else 'BAND DID NOT TAKE (Used is off) — do NOT claim success.')
"
