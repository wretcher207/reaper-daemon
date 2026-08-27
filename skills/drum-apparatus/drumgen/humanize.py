"""Humanize an existing flat/quantized drum take.

The groove renderer (groovekit.render) humanizes a beat it is generating from a
DSL, where it knows each hit's articulation (accent / ghost / flam). This module
solves the sibling problem: take a MIDI drum part that already exists -- typically
programmed dead-flat at velocity 127 and hard-quantized -- and give it dynamics
and micro-timing after the fact, with no articulation metadata to lean on.

It reuses the same taste constants as the renderer (David's kick ceiling, the RS
Monarch rimshot band, the closed-hat curve, the cymbal+shell boost) so a
humanized take and a rendered one share one voice. What it cannot borrow -- which
hit is an accent vs a ghost -- it infers from metric position, the way a player
reads a chart.

Pure and deterministic: same (notes, params, seed) -> same edits. The caller
(reaperd humanize) reads the take, calls plan_humanize, and ships the edits
through the bridge's index-addressed `apply_note_edits`, which -- unlike the
(tick,pitch) write path -- can touch two notes stacked on one tick (flams,
double-triggers), so nothing gets skipped.
"""
import random

try:  # reuse the renderer's taste constants when the package is importable
    from .groovekit import (KICK_VEL_MAX, VOICE_PROFILE, HAT_CURVE,
                            CYMBAL_SHELL_BOOST)
except Exception:  # pragma: no cover - standalone fallback keeps the module usable
    KICK_VEL_MAX = 114
    VOICE_PROFILE = {"RS Monarch": {"snare_role": "SNARE_RIM", "snare_vel": (90, 110)}}
    HAT_CURVE = {"HH_CLOSED_TIP": 0.8, "HH_CLOSED_EDGE": 0.8, "HH_PEDAL": 0.8,
                 "HH_OPEN_1": 1.0, "HH_OPEN_2": 1.0, "HH_OPEN_3": 1.0}
    CYMBAL_SHELL_BOOST = 1.12

# Role -> family. Families carry the humanize behaviour; the kit map resolves a
# MIDI pitch to a role, and this resolves the role to a family. Kept aligned with
# groovekit's LANE_ROLE / catalog role names.
ROLE_FAMILY = {
    "KICK_R": "kick", "KICK_L": "kick",
    "SNARE": "snare_center", "SNARE_RIM": "snare_rim",
    "SNARE_FLAM": "snare_flam", "SNARE_GHOST": "snare_ghost",
    "TOM_1": "tom", "TOM_2": "tom", "TOM_3": "tom", "TOM_4": "tom",
    "HH_PEDAL": "hat_pedal",
    "HH_CLOSED_TIP": "hat_closed", "HH_CLOSED_EDGE": "hat_closed",
    "HH_OPEN_1": "hat_open", "HH_OPEN_2": "hat_open", "HH_OPEN_3": "hat_open",
    "CRASH_L": "crash", "CRASH_R": "crash", "BIG_CRASH": "crash",
    "CHINA_L": "china", "CHINA_R": "china",
    "SPLASH_L": "splash", "SPLASH_R": "splash",
    "STACK": "stack",
    "BELL": "bell", "RIDE_BELL": "bell", "RIDE_CRASH": "bell",
    "RIDE_TIP": "ride",
}

# family -> (center, lo, hi). Bands hold David's approved tone from the first
# Monarch pass: kick under his ceiling, rimshot in its band, closed hats pulled
# under the open ones, cymbals loud. `kick` and `snare_rim` hi are overridden
# per-kit from VOICE_PROFILE where present.
# Centers sit with headroom below the ceiling so the jitter + metric lift spread
# hits ACROSS the band instead of piling them against the top (a pile at the
# ceiling reads as "flat" -- consecutive hits clamp to the same value).
FAMILY_BAND = {
    "kick":         (102, 1, KICK_VEL_MAX),   # 114
    "snare_center": (100, 86, 116),
    "snare_rim":    (100, 90, 110),
    "snare_flam":   (100, 90, 112),
    "snare_ghost":  (48, 30, 70),
    "tom":          (102, 70, 120),
    "hat_pedal":    (66, 45, 90),
    "hat_closed":   (98, 55, 112),    # HAT_CURVE x0.8 applied on top
    "hat_open":     (106, 80, 122),
    "crash":        (112, 96, 126),
    "china":        (112, 96, 126),
    "splash":       (104, 86, 122),
    "stack":        (106, 86, 122),
    "bell":         (108, 90, 124),
    "ride":         (92, 66, 114),
    "other":        (100, 60, 120),
}

SHELL_FAMILIES = {"kick", "snare_center", "snare_rim", "snare_flam",
                  "snare_ghost", "tom"}
CYMBAL_FAMILIES = {"crash", "china", "splash", "stack", "bell", "ride"}
# Families whose timing is left exactly as authored. Flams are a deliberate
# two-note gesture a blind micro-nudge would smear or collide.
NO_MOVE_FAMILIES = {"snare_flam"}

# note-name keyword -> family, for pitches the kit map does not cover.
NAME_KEYWORDS = [
    ("kick", "kick"), ("flam", "snare_flam"), ("ghost", "snare_ghost"),
    ("rim", "snare_rim"), ("snare", "snare_center"),
    ("floor", "tom"), ("rack", "tom"), ("tom", "tom"),
    ("pedal", "hat_pedal"), ("closed", "hat_closed"),
    ("open", "hat_open"), ("hat", "hat_closed"),
    ("china", "china"), ("splash", "splash"), ("crash", "crash"),
    ("ride", "ride"), ("bell", "bell"), ("stack", "stack"),
]


def pitch_families(kit_map=None, note_names=None):
    """Build {pitch: family}. Kit map wins; note-name keywords fill the gaps.

    kit_map: {role: pitch} (a maps.json entry). note_names: {pitch: name}.
    A pitch the map assigns to several roles takes the first family seen; that
    only differs for kits that double-map, and any of those families is a fine
    read for a shared pitch.
    """
    fam = {}
    if kit_map:
        for role, pitch in kit_map.items():
            f = ROLE_FAMILY.get(role)
            if f is not None:
                fam.setdefault(int(pitch), f)
    if note_names:
        for pitch, name in note_names.items():
            p = int(pitch)
            if p in fam or not name:
                continue
            low = str(name).lower()
            for kw, f in NAME_KEYWORDS:
                if kw in low:
                    fam[p] = f
                    break
    return fam


def _clamp(v, lo, hi):
    return int(round(max(lo, min(hi, v))))


def plan_humanize(notes, ppq, *, kit_map=None, note_names=None, map_name=None,
                  amount=25, seed=20260827, bar_ticks=None):
    """Plan a humanize pass over an existing take.

    notes: get_midi_notes rows (index, ppq, end_ppq, pitch, velocity).
    ppq:   ticks per quarter note.
    amount: 0-100. Scales the random velocity wobble and the timing spread; the
            dynamic contour (accents, per-role balance) is always applied, since
            a flat-127 part needs the contour even at a light amount.
    Returns {"edits": [...], "summary": {...}}. Each edit is
    {index, velocity[, new_ppq, new_end_ppq]}: velocity for every classified
    note, position only for notes actually moved.
    """
    amount = max(0, min(100, amount))
    sixteenth = ppq / 4.0
    if bar_ticks is None:
        bar_ticks = ppq * 4          # 4/4
    steps_per_beat = 4               # 16th grid
    fam = pitch_families(kit_map, note_names)

    # per-kit band overrides from the renderer's voice profile
    band = dict(FAMILY_BAND)
    profile = VOICE_PROFILE.get(map_name or "", {})
    if profile.get("snare_vel"):
        lo, hi = profile["snare_vel"]
        c = (lo + hi) // 2
        role = profile.get("snare_role", "SNARE")
        f = ROLE_FAMILY.get(role, "snare_rim")
        band[f] = (c, lo, hi)

    ordered = sorted(notes, key=lambda n: n["index"])
    tick_index = {}
    for n in ordered:
        tick_index.setdefault(n["ppq"], []).append(n)

    def family_of(n):
        return fam.get(n["pitch"], "other")

    # ---- fast-kick weak foot: alternate hits in a run drop 7-9 -----
    vrng = random.Random(seed)
    weakfoot = set()
    kicks = [n for n in ordered if family_of(n) == "kick"]
    kicks.sort(key=lambda n: n["ppq"])
    i = 0
    while i < len(kicks):
        j = i
        while j + 1 < len(kicks) and (kicks[j + 1]["ppq"] - kicks[j]["ppq"]) <= sixteenth + 1:
            j += 1
        run = kicks[i:j + 1]
        if len(run) >= 2:
            for k, n in enumerate(run):
                if k % 2 == 1:
                    weakfoot.add(n["index"])
        i = j + 1

    # ---- velocity pass -----
    # Base spread scales with amount. Cymbals get more of it: a drummer varies
    # crash/china force far more than a kick, and they are the exposed hits.
    jit = amount / 100.0 * 20.0
    new_vel = {}
    for n in ordered:
        f = family_of(n)
        c, lo, hi = band.get(f, band["other"])
        v = float(c)
        off = n["ppq"] % bar_ticks
        step = round(off / sixteenth)
        downbeat = (step == 0)
        onbeat = (step % steps_per_beat == 0)
        backbeat = step in (steps_per_beat, steps_per_beat * 3)  # beats 2 & 4

        # Metric contour. Cymbals are ACCENTS in their own right -- stacking a
        # downbeat lift on top only drove them into the ceiling and flattened
        # them there, so they take no metric lift.
        if f in ("snare_rim", "snare_center", "snare_flam"):
            if backbeat:
                v += 8
            elif onbeat:
                v += 3
        elif f == "kick":
            if downbeat:
                v += 5
            elif onbeat:
                v += 3
        elif f == "tom":
            if downbeat:
                v += 4
        elif f in ("hat_closed", "hat_open", "hat_pedal", "ride"):
            if downbeat:
                v += 6
            elif onbeat:
                v += 4
        # snare_ghost and the cymbal families: no metric lift

        if n["index"] in weakfoot:
            v -= vrng.randint(7, 9)
        if f == "hat_closed":
            v *= HAT_CURVE.get("HH_CLOSED_TIP", 0.8)
            eighth = round(off / (sixteenth * 2))
            v += 7 if eighth % 2 == 0 else -5
        if f in CYMBAL_FAMILIES:
            shelled = any(family_of(m) in SHELL_FAMILIES for m in tick_index.get(n["ppq"], []))
            if shelled:
                v += 3   # additive, not x1.12 -- the multiply overshot the ceiling
        this_jit = jit * (1.6 if f in CYMBAL_FAMILIES else 1.0)
        v += vrng.gauss(0, this_jit)
        new_vel[n["index"]] = _clamp(v, lo, hi)

    # ---- golden rule: no drummer hits the same drum at the same velocity twice
    # in a row. Enforce a real gap between consecutive hits on the SAME pitch,
    # in time order. This is the rule the whole system is built on, so it is a
    # hard backstop that runs at every amount, not a random flourish.
    by_pitch = {}
    for n in ordered:
        by_pitch.setdefault(n["pitch"], []).append(n)
    MIN_GAP = 3
    for pitch, lst in by_pitch.items():
        lst.sort(key=lambda n: n["ppq"])
        f = family_of(lst[0])
        _, lo, hi = band.get(f, band["other"])
        if hi - lo < MIN_GAP:
            continue   # band too narrow to separate; leave as-is
        prev = None
        for n in lst:
            v = new_vel[n["index"]]
            if prev is not None and abs(v - prev) < MIN_GAP:
                # a varied (deterministic) magnitude, so forced separations do
                # not settle into a mechanical two-value sawtooth.
                mag = MIN_GAP + (n["index"] * 2654435761 + pitch) % 5   # 3..7
                v = _clamp(prev + mag if v >= prev else prev - mag, lo, hi)
                if abs(v - prev) < MIN_GAP:
                    # clamping collapsed the gap (both at a bound) -> push the
                    # other way, still inside the band. This is the step the
                    # first cut was missing, which left crashes stacked at 125.
                    v = _clamp(prev - mag if prev >= hi - MIN_GAP else prev + mag, lo, hi)
                new_vel[n["index"]] = v
            prev = v

    # ---- timing pass: one offset per tick (unison lock), preserve duration ---
    trng = random.Random(seed + 1)
    tsigma = amount / 100.0 * 0.28 * sixteenth   # amount 25 -> ~0.07 x 16th
    cap = 2.0 * tsigma                            # clamp the tail to ~2 sigma
    offset_by_tick = {}

    def tick_offset(tick):
        if tick not in offset_by_tick:
            if amount == 0 or trng.random() < 0.15:
                o = 0.0
            else:
                o = trng.gauss(-0.04 * tsigma, tsigma)
                o = max(-cap, min(cap, o))
            offset_by_tick[tick] = o
        return offset_by_tick[tick]

    moved = {}
    for n in ordered:
        f = family_of(n)
        if f in NO_MOVE_FAMILIES:
            continue
        o = int(round(tick_offset(n["ppq"])))
        if o == 0:
            continue
        ns = n["ppq"] + o
        if ns < 0:
            ns = 0
        ne = n["end_ppq"] + o
        if ne <= ns:
            ne = ns + max(1, n["end_ppq"] - n["ppq"])
        moved[n["index"]] = (ns, ne)

    # ---- assemble edits + summary -----
    edits = []
    for n in ordered:
        e = {"index": n["index"], "velocity": new_vel[n["index"]]}
        if n["index"] in moved:
            e["new_ppq"], e["new_end_ppq"] = moved[n["index"]]
        edits.append(e)

    fam_counts = {}
    for n in ordered:
        fam_counts[family_of(n)] = fam_counts.get(family_of(n), 0) + 1
    moves = [abs(moved[n["index"]][0] - n["ppq"]) for n in ordered
             if n["index"] in moved]
    summary = {
        "notes": len(ordered),
        "velocity_edits": len(edits),
        "position_edits": len(moved),
        "amount": amount,
        "seed": seed,
        "families": fam_counts,
        "unclassified": fam_counts.get("other", 0),
        "max_move_ticks": max(moves) if moves else 0,
        "mean_move_ticks": round(sum(moves) / len(moves), 2) if moves else 0,
    }
    return {"edits": edits, "summary": summary}
