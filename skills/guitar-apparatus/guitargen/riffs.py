"""Riff specs: a compact step notation, the range probe, and the demo riff.

A riff is authored as one 16-character string per bar over this alphabet, which
keeps a part editable by eye the way the drum DSL's step grid is:

    .   rest
    x   root chug, palm-muted            X   root chug, accented (power chord)
    o   root, let ring (power chord)     g   root, ghost (very muted, soft)
    4   fourth  (root+5),  muted         5   fifth   (root+7),  let ring
    b   tritone (root+6),  let ring      7   min7th  (root+10), let ring
    h   octave  (root+12), let ring

  dissonant palm-muted RING CLUSTERS (Mute keyswitch, top vel; root struck with
  the clashing note so you hear it crunch):
    r   root ring (single)  2   E+b2            t   E+tritone
    9   E+b9                j   E+maj7

Let-ring / sustained / palm-muted-ring notes hold until the next onset (min two
16ths); muted and accented notes are short and tight. Intervals are semitones
above the low string root, so the whole part follows one `low_string` value. On a
map with power chords (Argent), a ROOT accent/hold voices a power chord and a fast
root chug stays single-note; melodic (non-root) notes and every mute_ring stay
single. See perform.py.
"""

# token -> (interval semitones above low string, articulation[, dyad]).
# `dyad` (optional 3rd field) adds a SECOND note that many semitones above the
# first, struck together — a real cluster you hear clash, not a single note.
#
# Lowercase let-ring/power-chord tokens (o 4 5 b 7 h) ring OPEN (Sustain / power
# chord). The dissonant palm-muted-RING tokens (r 2 t 9 j) fire the Mute
# keyswitch at top velocity (half-muted so it sings) and hang; the numbered/lettered
# ones strike the low-E ROOT together with a clashing interval — b2, tritone, b9,
# maj7 — so the dissonance actually bites.
_TOKENS = {
    "x": (0, "mute"), "X": (0, "accent"), "o": (0, "let_ring"), "g": (0, "ghost"),
    "4": (5, "mute"), "5": (7, "let_ring"), "b": (6, "let_ring"),
    "7": (10, "let_ring"), "h": (12, "let_ring"),
    # dissonant palm-muted RING CLUSTERS: root + the clashing interval, together
    "r": (0, "mute_ring"),          # root ring, single note
    "2": (0, "mute_ring", 1),       # E + b2  (minor-2nd crunch)
    "t": (0, "mute_ring", 6),       # E + tritone
    "9": (0, "mute_ring", 13),      # E + b9  (minor-2nd, octave up)
    "j": (0, "mute_ring", 11),      # E + maj7
}
_RING_ARTS = {"let_ring", "sustain", "mute_ring"}


def parse_bars(bars, steps_per_bar=16):
    """Turn a list of bar strings into hit dicts on an absolute step grid."""
    raw = []
    for bi, bar in enumerate(bars):
        if len(bar) != steps_per_bar:
            raise ValueError(
                f"bar {bi} has {len(bar)} steps, expected {steps_per_bar}: {bar!r}")
        for si, ch in enumerate(bar):
            if ch == ".":
                continue
            if ch not in _TOKENS:
                raise ValueError(f"bar {bi} step {si}: unknown token {ch!r}")
            tok = _TOKENS[ch]
            hit = {"step": bi * steps_per_bar + si,
                   "interval": tok[0], "art": tok[1]}
            if len(tok) > 2:
                hit["dyad"] = tok[2]
            raw.append(hit)
    # length: ring notes hold until the next onset; others are short (1 step).
    steps = [h["step"] for h in raw]
    for i, h in enumerate(raw):
        if h["art"] in _RING_ARTS:
            nxt = steps[i + 1] if i + 1 < len(steps) else (
                (max(steps) // steps_per_bar + 1) * steps_per_bar)
            h["len_steps"] = max(2, nxt - h["step"])
        else:
            h["len_steps"] = 1
    return raw


def make_spec(bars, map_name, *, ppq=480, steps_per_bar=16, **extra):
    hits = parse_bars(bars, steps_per_bar)
    spec = {"ppq": ppq, "steps_per_bar": steps_per_bar,
            "bars": len(bars), "map": map_name, "hits": hits}
    spec.update(extra)
    return spec


def bass_from_guitar(spec):
    """Derive a locked bass line: the root of every guitar onset, one line, no
    keyswitches. Melodic intervals collapse to the root (the bass holds the low
    end under the riff); articulations map through so the bass breathes with the
    guitar. A little behind-the-beat bias gives it finger-style feel.
    """
    art_map = {"accent": "accent", "let_ring": "sustain", "ghost": "ghost",
               "mute": "mute", "sustain": "sustain", "mute_ring": "sustain"}
    hits = []
    for h in spec["hits"]:
        hits.append({"step": h["step"], "interval": 0,
                     "art": art_map.get(h["art"], "mute"),
                     "len_steps": h.get("len_steps", 1)})
    return {"ppq": spec["ppq"], "steps_per_bar": spec["steps_per_bar"],
            "bars": spec["bars"], "map": "nolly_e", "hits": hits,
            "timing_bias": 0.05, "timing_sigma": 0.05}


# ---------------------------------------------------------------------------
# The demo: 8 bars, key of E, 120 BPM. Riff A (driving syncopated chug) x2, a
# more open melodic lift (riff B), then riff A returns and builds to a ringing
# resolve. Content is a jam starting point — the point of the first pass is the
# feel and the pipeline; the notes are David's to redirect.
# ---------------------------------------------------------------------------
DEMO_BARS = [
    "Xxx.Xx.xXx.xXxx.",   # 1  riff A: accents on the quarters, gaps breathe
    "Xx.xXx.xXx.x2...",   # 2  answer: hangs a dissonant b2, palm-muted, ringing
    "Xxx.Xx.xXx.xXxx.",   # 3  riff A again
    "Xx.xXx.xXx.xt...",   # 4  answer: hangs a tritone, palm-muted, ringing
    "2...t...j...9...",   # 5  lift: palm-muted dissonant CLUSTERS over the E pedal
    "2...t...j...r...",   # 6  lift answer, settles back onto the root ring
    "Xxx.Xx.xXx.xXxx.",   # 7  riff A returns
    "XxxXxxXxxXxxXX.r",   # 8  build: 3+3+3+3 gallop into a palm-muted root ring
]


def demo_guitar_spec():
    return make_spec(DEMO_BARS, "argent_e", timing_sigma=0.06)


def range_probe_spec(candidates=(25, 28, 33, 40)):
    """A general low-string probe: sustained single notes, one per bar, at the
    candidate MIDI values, so a new/unknown instrument's lowest sounding note can
    be found by ear. `low_string_override=0` makes each hit's interval an absolute
    MIDI note. Defaults probe Argent's low C# (25), E (28), A (33), and E2 (40).
    """
    hits = [{"step": i * 16, "interval": midi, "art": "sustain", "len_steps": 12}
            for i, midi in enumerate(candidates)]
    return {"ppq": 480, "steps_per_bar": 16, "bars": len(hits), "map": "argent_e",
            "hits": hits, "low_string_override": 0, "timing_sigma": 0.0}
