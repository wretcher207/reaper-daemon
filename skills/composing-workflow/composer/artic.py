"""Articulation layer for Shreddage 3.5 Argent and Submission Audio Umansky Bass.

Write a part as one string of tokens. Each token is

    <steps><tag>[!][:<note>[+12|-12]][@<vel>]

  steps  16th-note steps the event OCCUPIES (onset to next onset)
  tag    articulation tag (GTAGS / BTAGS below); "r" is a rest
  !      accent (top of the tag's velocity band)
  note   letter name, folded to sit at/above the instrument root. Default: the
         root. Letters are written relative to a root called "E" (the riff's
         tonic letter); what that SOUNDS like depends on the patch -- measure it
         (composer.measure) and set `root`.
  vel    explicit velocity override
"|" is visual only. A phrase must be a whole number of 16-step bars.

    ev, steps = render("3pc! 2pm4 1pm2 3pcm!:F 2pm4 1st 4lr:G",
                       instrument="argent", bpm=140, swing=0.6, seed=7)

Returned events are insert_midi_events note dicts in beats relative to the item
start; keyswitch events carry ks=True, played notes carry tag=<tag>.
Sources: docs/composing-workflow.md (Argent manual p12/p19/p31-33, Umansky manual
p8-10, live plugin params). Anything marked UNVERIFIED is a best guess.
"""
import random
import re

NOTE = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6,
        "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}

# ---------------------------------------------------------------- Argent ----
# Default TACT map (Kontakt C-2 = MIDI 0), latching (manual p19, p31, p33).
ARGENT_KS = {"sustain": 0, "mute": 1, "staccato": 2, "pc_sustain": 3, "pc_mute": 4,
             "pc_staccato": 5, "tremolo": 6, "choke": 7, "tapping": 8,
             "nat_harm": 9, "art_harm": 10, "fx": 11,
             "slide_ks": 21, "fret_noise": 22, "slide_note": 23, "thrash": 24}
ARGENT_FLOOR = 25            # low open string; 24 is the Thrash (repeat) keyswitch
ARGENT_TOP = 64              # practical ceiling used by verify ranges
ARGENT_ROOT = 28
# Palm-mute DEPTH = played-note velocity (manual p31; product page: up to 5 layers).
# Layer split points are not published -> even bands (UNVERIFIED).
PM_DEPTH = {1: (34, 50), 2: (52, 68), 3: (70, 86), 4: (88, 104), 5: (106, 122)}

# tag -> (keyswitch, ks_velocity, length_rule, vel_range)
GTAGS = {
    "sus":  ("sustain", 64, "ring",   (82, 112)),
    "lr":   ("sustain", 64, "ring",   (90, 114)),
    "pc":   ("pc_sustain", 64, "ring", (92, 118)),
    "pcm":  ("pc_mute", 64, "full",   (88, 116)),
    "pcs":  ("pc_staccato", 64, "short", (92, 118)),
    "st":   ("staccato", 64, "short", (90, 118)),
    "ph":   ("sustain", 127, "ring",  (127, 127)),   # pinch = KS 0 at velocity 127
    "rk":   ("sustain", 120, "ring",  (100, 114)),   # rake = KS 0 at velocity 117-126
    "nh":   ("nat_harm", 64, "ring",  (90, 112)),
    "ah":   ("art_harm", 64, "ring",  (90, 112)),
    "trem": ("tremolo", 64, "full",   (95, 118)),
    "chk":  ("choke", 64, "short",    (90, 118)),
    "tap":  ("tapping", 64, "legato", (85, 110)),
    "lg":   ("sustain", 64, "legato", (80, 104)),
    "sl":   ("sustain", 64, "slide",  (90, 112)),
    "pcsl": ("pc_sustain", 64, "slide", (92, 114)),
}
for _d, _band in PM_DEPTH.items():
    GTAGS[f"pm{_d}"] = ("mute", 64, "full", _band)
GTAGS["pm"] = GTAGS["pm3"]

# --------------------------------------------------------------- Umansky ----
# Keymap from the Umansky manual p8 (C-2 = 0). Latch vs momentary is a per-
# articulation user setting with no stated default, so render() re-sends the
# keyswitch whenever the articulation changes, including back to finger (0).
UMANSKY_KS = {"finger": 0, "index": 1, "middle": 2, "ghost": 3, "slap": 4,
              "ghost_slap": 5, "pop": 6, "ghost_pop": 7, "harmonic": 8,
              "hammer_pull": 9, "tapping": 10, "slide": 11, "dead_dbl_thumb": 12,
              "thump": 13, "dbl_thumb": 14, "dbl_thumb_pop": 15, "fh_mute": 16}
UMANSKY_FORCE = {"C": 122, "G": 123, "D": 124, "A": 125, "E": 126, "B": 127}
UMANSKY_PLAY = (24, 84)       # manual p10 "C0 - C5"
UMANSKY_STD_LOW = 35          # default tuning low string B0
UMANSKY_ROOT = 40

BTAGS = {
    "f":   ("finger", "full",  (88, 116)),
    "fs":  ("finger", "short", (84, 110)),
    "lr":  ("finger", "ring",  (92, 116)),
    "gh":  ("ghost", "short",  (40, 70)),
    "fhm": ("fh_mute", "short", (70, 100)),
    "sl":  ("slide", "slide",  (90, 112)),
    "hp":  ("hammer_pull", "legato", (80, 104)),
    "hm":  ("harmonic", "ring", (90, 112)),
    "th":  ("thump", "full",   (96, 120)),
}

LEGATO_TAGS = {"lg", "sl", "pcsl", "tap", "hp"}

# ------------------------------------------------- rig-specific measurements --
# MEASURED on ONE user's patches (2026-10-21, FFT of isolated probe renders).
# They are NOT properties of the libraries. Re-measure on any new rig/patch with
# composer.measure before trusting them, and pass root= explicitly.
EXAMPLE_RIG = {
    "argent_sounding_offset": -3,   # MIDI 28 sounded C#1 although Transpose read 0 st
    "umansky_sounding_offset": -12, # MIDI 40 sounded 41.2 Hz: labels are real bass pitch
    "umansky_root_for_argent_28": 37,  # bass MIDI 37 = C#1 = same pitch as Argent 28
    "mysteria_atmosphere_pitched": False,  # that snapshot ignored note pitch
}

TOK = re.compile(r"^(\d+)([a-z]+[0-9]?)(!)?(?::([A-G][#b]?)([+-]\d+)?)?(?:@(\d+))?$")


def parse(text, steps_per_bar=16):
    out, pos = [], 0
    for raw in text.replace("|", " ").split():
        m = TOK.match(raw)
        if not m:
            raise ValueError(f"bad token {raw!r}")
        steps, tag, acc, note, octv, vel = m.groups()
        steps = int(steps)
        if tag != "r":
            out.append(dict(step=pos, steps=steps, tag=tag, accent=bool(acc),
                            note=note or "E", oct=int(octv or 0),
                            vel=int(vel) if vel else None))
        pos += steps
    if pos % steps_per_bar:
        raise ValueError(f"phrase is {pos} steps, not a whole number of bars")
    return out, pos


def pitch_of(root, note, octv=0):
    base = root - NOTE["E"] + NOTE[note]
    if base < root:
        base += 12
    return base + octv


def swing_pos(step, amt):
    """Beat position of a 16th step inside a beat-pair; amt 0.5 = straight."""
    return (step // 2) * 0.5 + (step % 2) * 0.5 * amt


def _t(step, swing):
    return (step // 16) * 4 + swing_pos(step % 16, swing)


def render(text, *, instrument, bpm, swing=0.5, seed=1, lean_ms=0.0,
           jitter_ms=3.0, level=1.0, root=None, start_step=0, floor=None):
    """-> (events, total_steps). instrument: "argent" or "umansky"."""
    rng = random.Random(seed)
    ms = bpm / 60000.0                       # beats per millisecond
    gtr = instrument == "argent"
    if not gtr and instrument != "umansky":
        raise ValueError("instrument must be 'argent' or 'umansky'")
    tags = GTAGS if gtr else BTAGS
    root = root or (ARGENT_ROOT if gtr else UMANSKY_ROOT)
    floor = floor if floor is not None else (ARGENT_FLOOR if gtr else UMANSKY_STD_LOW)
    notes, total = parse(text)
    ev, cur = [], None
    for i, n in enumerate(notes):
        if n["tag"] not in tags:
            raise ValueError(f"{instrument}: unknown tag {n['tag']!r}")
        spec = tags[n["tag"]]
        if gtr:
            ks_name, ks_vel, rule, (vlo, vhi) = spec
        else:
            (ks_name, rule, (vlo, vhi)), ks_vel = spec, 100
        p = pitch_of(root, n["note"], n["oct"])
        hi = 127 if gtr else UMANSKY_PLAY[1]
        if not floor <= p <= hi:
            raise ValueError(f"{instrument}: pitch {p} outside {floor}..{hi}")
        st = start_step + n["step"]
        t = max(0.0, _t(st, swing) + rng.gauss(lean_ms, jitter_ms) * ms)
        nxt = notes[i + 1] if i + 1 < len(notes) else None
        slot_t = _t(st + n["steps"], swing)
        contig = nxt is not None and nxt["step"] == n["step"] + n["steps"]
        span = slot_t - t
        if rule == "full":
            ln = span - 12 * ms              # hold to just before the next pick
        elif rule == "ring":
            ln = span - 4 * ms
        elif rule == "short":
            ln = max(span * 0.55, 70 * ms)
        else:                                # legato / slide
            ln = span + (40 * ms if contig else -4 * ms)
        ln = max(ln, 40 * ms)
        if n["vel"]:
            v = n["vel"]
        else:
            mid = vlo + (vhi - vlo) * (0.55 + 0.25 * (level - 1.0))
            if n["accent"]:
                mid = vhi - 2
            v = round(min(vhi, max(vlo, rng.gauss(mid, (vhi - vlo) / 8))))
        if ks_name != cur or (gtr and ks_name == "sustain"):
            # sustain/pinch/rake share KS 0, split by KS velocity: resend each time
            kp = (ARGENT_KS if gtr else UMANSKY_KS)[ks_name]
            ev.append(dict(time=t, duration=0.02, pitch=kp, velocity=ks_vel, ks=True))
            cur = ks_name
        if rule == "slide" and gtr:
            ev.append(dict(time=max(t, t + ln - 0.06), duration=0.02,
                           pitch=ARGENT_KS["slide_ks"], velocity=100, ks=True))
        ev.append(dict(time=t, duration=ln, pitch=p, velocity=int(v), tag=n["tag"]))
    # overlap clamp: a non-legato note must end 3 ms before the next played note
    # (accidental overlap = unwanted slur; same-pitch overlap = 0-length in REAPER)
    played = [e for e in ev if not e.get("ks")]
    for a, b in zip(played, played[1:]):
        if a["tag"] in LEGATO_TAGS:
            continue
        lim = b["time"] - a["time"] - 3 * ms
        if a["duration"] > lim:
            a["duration"] = max(20 * ms, lim)
    return ev, total


def stats(ev, bpm):
    """Per-tag share, median length (ms) and velocity span. Keep any one tag
    under ~60% of a section."""
    msb = 60000.0 / bpm
    played = [e for e in ev if not e.get("ks")]
    by = {}
    for e in played:
        by.setdefault(e["tag"], []).append(e)
    return {t: dict(n=len(v), share=round(100 * len(v) / len(played)),
                    med_ms=round(sorted(x["duration"] for x in v)[len(v) // 2] * msb),
                    vel=f"{min(x['velocity'] for x in v)}-{max(x['velocity'] for x in v)}")
            for t, v in by.items()}


def to_events(ev, ndigits=5):
    """Strip helper keys -> insert_midi_events 'events' list."""
    return [dict(type="note", time=round(e["time"], ndigits),
                 duration=round(e["duration"], ndigits),
                 pitch=e["pitch"], velocity=e["velocity"]) for e in ev]


def pitch_classes(ev, offset=0):
    """Sounding pitch classes of played notes (apply a measured offset)."""
    return [((e["pitch"] + offset) % 12) for e in ev if not e.get("ks")]
