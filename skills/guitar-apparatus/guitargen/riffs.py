"""Riff specs: a compact step notation, the range probe, and the demo riff.

A riff is authored as one 16-character string per bar over this alphabet, which
keeps a part editable by eye the way the drum DSL's step grid is:

    .   rest
    x   root chug, palm-muted            X   root chug, accented (muted, harder)
    o   root, let ring (mod wheel down)  g   root, ghost (very muted, soft)
    4   fourth  (root+5),  muted         5   fifth   (root+7),  let ring
    b   tritone (root+6),  let ring      7   min7th  (root+10), let ring
    h   octave  (root+12), let ring

Let-ring / sustained notes ring until the next onset (min two 16ths); muted and
accented notes are short and tight. Intervals are semitones above the low string,
so the whole part follows one `low_string` value — the knob the probe confirms.
"""

# token -> (interval semitones above low string, articulation)
_TOKENS = {
    "x": (0, "mute"), "X": (0, "accent"), "o": (0, "let_ring"), "g": (0, "ghost"),
    "4": (5, "mute"), "5": (7, "let_ring"), "b": (6, "let_ring"),
    "7": (10, "let_ring"), "h": (12, "let_ring"),
}
_RING_ARTS = {"let_ring", "sustain"}


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
            interval, art = _TOKENS[ch]
            raw.append({"step": bi * steps_per_bar + si,
                        "interval": interval, "art": art})
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
               "mute": "mute", "sustain": "sustain"}
    hits = []
    for h in spec["hits"]:
        hits.append({"step": h["step"], "interval": 0,
                     "art": art_map.get(h["art"], "mute"),
                     "len_steps": h.get("len_steps", 1)})
    return {"ppq": spec["ppq"], "steps_per_bar": spec["steps_per_bar"],
            "bars": spec["bars"], "map": "nolly_drop_a", "hits": hits,
            "timing_bias": 0.05, "timing_sigma": 0.05}


# ---------------------------------------------------------------------------
# The demo: 8 bars, Drop A, 120 BPM. Riff A (driving syncopated chug) x2, a
# more open melodic lift (riff B), then riff A returns and builds to a ringing
# resolve. Content is a jam starting point — the point of the first pass is the
# feel and the pipeline; the notes are David's to redirect.
# ---------------------------------------------------------------------------
DEMO_BARS = [
    "Xxx.Xx.xXx.xXxx.",   # 1  riff A: accents on the quarters, gaps breathe
    "Xx.xXx.xXx.xb...",   # 2  riff A answer: opens to a ringing tritone on 4
    "Xxx.Xx.xXx.xXxx.",   # 3  riff A again
    "Xx.xXx.xXx.xh...",   # 4  answer, resolves up to a ringing octave
    "5...b...7...h...",   # 5  lift: fifth / tritone / b7 / octave, let ring
    "5...b...7...o...",   # 6  lift answer, settles back onto the root
    "Xxx.Xx.xXx.xXxx.",   # 7  riff A returns
    "XxxXxxXxxXxxXX.o",   # 8  build: 3+3+3+3 gallop into a ringing root resolve
]


def demo_guitar_spec():
    return make_spec(DEMO_BARS, "hydra_drop_a", timing_sigma=0.06)


def range_probe_spec():
    """Four sustained single notes, well apart in time, at candidate low-string
    MIDI values, so David can say which sounds and at what pitch. This locks
    `low_string` before a full riff is written. Notes as absolute-ish anchors:
    33 (A1 concert, on the "Lowest note" keyswitch), 40 (E2), 45 (A2), 52 (E3).
    """
    # authored directly as absolute pitches via interval-from-0 with low=0 trick:
    # we set low_string_override=0 so interval == absolute MIDI note.
    hits = []
    for i, midi in enumerate([33, 40, 45, 52]):
        hits.append({"step": i * 16, "interval": midi, "art": "sustain",
                     "len_steps": 12})
    return {"ppq": 480, "steps_per_bar": 16, "bars": 4, "map": "hydra_drop_a",
            "hits": hits, "low_string_override": 0, "timing_sigma": 0.0}
