"""Turn a riff spec into a humanized guitar/bass performance (notes + CC).

Same taste philosophy as the drum apparatus' humanize: a deterministic musical
contour carries the dynamics, and randomness is only garnish that keeps the part
off the grid. What a drummer's metric accents are, a guitarist's are too —
downbeats and the syncopated pushes land harder; ghosted chugs sit back; a
let-ring opens up. On top of that, two things specific to a virtual guitar:

  * **Palm-mute morph (CC1).** Modern-metal chugs are a mod-wheel ride: high for a
    tight palm mute, dropped near zero for a let-ring or a sustained accent. The
    morph is what makes it read as a real player and not a sampler on sustain.
  * **Double-tracking width.** argent-l and argent-r play the *same* riff with
    *different* seeds. Two independently-humanized takes is exactly how a real
    double-tracked rhythm gets its width — not a stereo copy, two performances.

A riff spec is a plain dict (see riffs.py). Pure and deterministic:
same (spec, map, seed) -> same events.
"""
import random

from .maps import get_map, MODWHEEL

# articulation -> (velocity center, lo, hi). Chugs live loud but with headroom so
# the contour + jitter spread them across a band instead of piling at the top
# (a pile at the ceiling reads as machine-flat — the same lesson as the drums).
ART_VEL = {
    "mute":     (98, 82, 116),
    "accent":   (116, 104, 127),
    "sustain":  (104, 90, 122),
    "let_ring": (108, 94, 124),
    "ghost":    (64, 44, 84),
}

# articulation -> which keyswitch SLOT the instrument should be in. On the Hydra,
# a palm-muted chug is the Mute keyswitch and a ring is Sustain — the keyswitch,
# not the mod wheel, is what mutes. Accents stay muted (a djent accent is a harder
# palm mute, not an open note).
ART_TO_KS = {
    "mute": "mute", "ghost": "mute", "accent": "mute",
    "sustain": "sustain", "let_ring": "sustain",
}

# articulation -> mod-wheel palm-mute depth center, used ONLY on maps that mute
# via the mod wheel (map flag modwheel_mute). Off for the Hydra.
ART_MUTE = {
    "mute": 116, "accent": 96, "sustain": 10, "let_ring": 4, "ghost": 122,
}

# articulation -> note length as a fraction of its authored step length. Mutes are
# short and tight; sustains ring for their written value.
ART_LEN = {
    "mute": 0.55, "accent": 0.7, "sustain": 1.0, "let_ring": 1.0, "ghost": 0.4,
}

NO_REPEAT_GAP = 4        # successive same-pitch chugs differ by at least this
KS_TICKS = 16            # keyswitch note length
KS_LEAD = 8              # place a keyswitch/CC this many ticks before its note


def _clamp(v, lo, hi):
    return int(round(max(lo, min(hi, v))))


def _occupied_steps(hits):
    return {h["step"] for h in hits}


def perform(spec, map_name, *, seed=0x5152, is_bass=False):
    """Render one performance of a riff spec. Returns (events, info).

    events: smf event dicts (notes + cc). info: counts for the summary line.
    """
    gm = get_map(map_name)
    ppq = int(spec.get("ppq", 480))
    steps_per_bar = int(spec.get("steps_per_bar", 16))
    step_ticks = (ppq * 4) // steps_per_bar          # 16th grid -> ppq/4
    bar_ticks = ppq * 4
    low = int(spec.get("low_string_override", gm["low_string"]))
    hits = sorted(spec["hits"], key=lambda h: h["step"])
    occupied = _occupied_steps(hits)

    vrng = random.Random(seed)
    trng = random.Random(seed ^ 0x9E3779B9)
    mrng = random.Random(seed ^ 0x2545F491)

    tsigma = spec.get("timing_sigma", 0.06) * step_ticks  # micro-timing spread
    tcap = 2.0 * tsigma
    tbias = spec.get("timing_bias", 0.0) * step_ticks      # +behind, -ahead

    events = []
    ks_notes = gm.get("ks_notes") or {}
    modwheel_mute = bool(gm.get("modwheel_mute"))

    # ---- pass 1: per-hit velocity from the deterministic contour -------------
    planned = []           # [hit, tick, pitch, vel, art, mute_depth, dur, ks_slot]
    for h in hits:
        step = int(h["step"])
        art = h.get("art", "mute")
        interval = int(h.get("interval", 0))
        pitch = low + interval
        base_tick = step * step_ticks

        c, lo, hi = ART_VEL.get(art, ART_VEL["mute"])
        v = float(c)
        pos = step % steps_per_bar
        downbeat = (pos == 0)
        quarter = (pos % 4 == 0)
        eighth_off = (pos % 4 == 2)
        # a "push": an onset on an off-16th with a rest right before it, driving
        # into the next beat — a guitarist leans on those.
        push = (pos % 2 == 1) and ((step - 1) not in occupied)

        if art == "accent":
            v += 4
        if downbeat:
            v += 6
        elif quarter:
            v += 4
        elif eighth_off:
            v += 2
        if push:
            v += 5
        if art == "ghost":
            v -= 4

        v += vrng.gauss(0, 5.0)          # garnish only
        vel = _clamp(v, lo, hi)

        # mod-wheel palm-mute depth (only used when the map mutes via mod wheel)
        if is_bass or not modwheel_mute:
            depth = None
        else:
            depth = _clamp(ART_MUTE.get(art, 100) + mrng.randint(-4, 4), 0, 127)

        ks_slot = None if is_bass else ART_TO_KS.get(art, "mute")

        dur = max(1, int(round(int(h.get("len_steps", 1)) * step_ticks
                               * ART_LEN.get(art, 0.55))))
        planned.append([h, base_tick, pitch, vel, art, depth, dur, ks_slot])

    # ---- golden no-repeat: never the same velocity twice in a row on one pitch
    by_pitch = {}
    for p in planned:
        by_pitch.setdefault(p[2], []).append(p)
    for pitch, lst in by_pitch.items():
        lst.sort(key=lambda p: p[1])
        art0 = lst[0][4]
        _, lo, hi = ART_VEL.get(art0, ART_VEL["mute"])
        prev = None
        for p in lst:
            v = p[3]
            if prev is not None and abs(v - prev) < NO_REPEAT_GAP:
                mag = NO_REPEAT_GAP + (p[1] * 2654435761 + pitch) % 5   # 4..8
                v = _clamp(prev + mag if v >= prev else prev - mag, lo, hi)
                if abs(v - prev) < NO_REPEAT_GAP:
                    v = _clamp(prev - mag if prev >= hi - NO_REPEAT_GAP
                               else prev + mag, lo, hi)
                p[3] = v
            prev = p[3]

    # ---- pass 2: emit CC (mod wheel) + notes with micro-timing --------------
    # one timing offset per tick so a dyad's two notes stay locked together.
    toff = {}

    def tick_offset(t):
        if t not in toff:
            o = 0.0 if trng.random() < 0.12 else trng.gauss(0, tsigma)
            toff[t] = max(-tcap, min(tcap, o))
        return toff[t]

    last_slot = None
    last_depth = None
    for h, base_tick, pitch, vel, art, depth, dur, ks_slot in planned:
        off = int(round(tick_offset(base_tick) + tbias))
        tick = max(0, base_tick + off)

        # articulation keyswitch: fire only when the slot changes (Mute <-> Sustain
        # as the riff moves between chugs and let-rings). A KS note sits a few
        # ticks before the note it governs so the engine has switched in time.
        if ks_slot is not None and ks_slot != last_slot and ks_slot in ks_notes:
            ks_pitch, ks_vel = ks_notes[ks_slot]
            events.append({"type": "note", "tick": max(0, tick - KS_LEAD),
                           "pitch": ks_pitch, "vel": ks_vel,
                           "dur": KS_TICKS, "chan": 0})
            last_slot = ks_slot

        # mod-wheel ride only on maps that mute that way
        if depth is not None and depth != last_depth:
            events.append({"type": "cc", "tick": max(0, tick - KS_LEAD),
                           "cc": MODWHEEL, "val": depth, "chan": 0})
            last_depth = depth

        events.append({"type": "note", "tick": tick, "pitch": pitch,
                       "vel": vel, "dur": dur, "chan": 0})

        dyad = h.get("dyad")
        if dyad is not None:
            events.append({"type": "note", "tick": tick, "pitch": pitch + int(dyad),
                           "vel": _clamp(vel - 6, 1, 127), "dur": dur, "chan": 0})

    info = {
        "notes": sum(1 for e in events if e["type"] == "note"),
        "ccs": sum(1 for e in events if e["type"] == "cc"),
        "bars": int(spec.get("bars", 0)),
        "seed": seed,
        "map": map_name,
        "low_string": low,
    }
    return events, info
