"""Riff specs: a compact step notation, the range probe, and the demo riff.

A riff is authored as one 16-character string per bar over this alphabet, which
keeps a part editable by eye the way the drum DSL's step grid is:

    .   rest
    x   root chug, palm-muted            X   root chug, accented (power chord)
    o   root, let ring (power chord)     g   root, ghost (very muted, soft)
    4   fourth  (root+5),  muted         5   fifth   (root+7),  let ring
    b   tritone (root+6),  let ring      7   min7th  (root+10), let ring
    h   octave  (root+12), let ring

  moving palm-muted single notes (the riff melody — walk the pitch, no monotone):
    m   b2 (root+1)   n   b3 (root+3)   v   b5 (root+6)   f   5th (root+7)
    k   b7 (root+10)

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
    # moving palm-muted single notes (the riff MELODY, E phrygian): the chugs
    # walk to these between the low-E anchors so the part reads as notes, not a
    # monotone muted rake. m=b2 n=b3 v=b5/tritone f=5th k=b7
    "m": (1, "mute"), "n": (3, "mute"), "v": (6, "mute"),
    "f": (7, "mute"), "k": (10, "mute"),
    # OPEN let-ring melodic note for the ringing intro (b2). The other open
    # melodic notes already exist: 5=5th b=tritone 7=b7 h=octave (all let_ring),
    # o=root (power chord, rings).
    "M": (1, "let_ring"),
    # dissonant palm-muted RING CLUSTERS: root + the clashing interval, together
    "r": (0, "mute_ring"),          # root ring, single note
    "2": (0, "mute_ring", 1),       # E + b2  (minor-2nd crunch)
    "t": (0, "mute_ring", 6),       # E + tritone
    "9": (0, "mute_ring", 13),      # E + b9  (minor-2nd, octave up)
    "j": (0, "mute_ring", 11),      # E + maj7
}
_RING_ARTS = {"let_ring", "sustain", "mute_ring"}

# Two cells that are NOT notes — they shape how a note connects to what follows.
#   _   TIE: hold the previous note through this step instead of resting. This is
#       how a riff gets note VALUES (dotted 8ths, held-over-the-barline halves)
#       instead of a machine-gun row of equal 16ths.
#   ~   SLIDE INTO the next note: fires Argent's Slide keyswitch and forces the
#       previous note to overlap the next attack, so the engine slurs rather than
#       re-picks. Put it in the cell immediately BEFORE the note it leads into.
TIE = "_"
SLIDE = "~"


def parse_bars(bars, steps_per_bar=16):
    """Turn a list of bar strings into hit dicts on an absolute step grid.

    Spaces inside a bar are visual only (group cells for readability, like the
    drum DSL), so `"Xxx. mx.x Xx.v Xxk."` is the same as `"Xxx.mx.xXx.vXxk."`.
    """
    raw = []
    pending_slide = False
    for bi, bar in enumerate(bars):
        cells = [c for c in bar if not c.isspace()]
        if len(cells) != steps_per_bar:
            raise ValueError(
                f"bar {bi+1} ({bar!r}) has {len(cells)} cells, need exactly "
                f"{steps_per_bar} (spaces don't count)")
        for si, ch in enumerate(cells):
            if ch == ".":
                continue
            if ch == SLIDE:
                if not raw:
                    raise ValueError(
                        f"bar {bi+1} cell {si+1}: {SLIDE!r} slides INTO the next "
                        f"note, so it needs a note before it to slide from")
                pending_slide = True
                continue
            if ch == TIE:
                if not raw:
                    raise ValueError(
                        f"bar {bi+1} cell {si+1}: {TIE!r} holds the PREVIOUS note, "
                        f"but no note has been played yet")
                raw[-1]["ties"] = raw[-1].get("ties", 0) + 1
                continue
            if ch not in _TOKENS:
                raise ValueError(
                    f"bar {bi+1} cell {si+1}: unknown token {ch!r} — "
                    f"valid: {' '.join(sorted(_TOKENS))} {TIE} {SLIDE} .")
            tok = _TOKENS[ch]
            hit = {"step": bi * steps_per_bar + si,
                   "interval": tok[0], "art": tok[1]}
            if len(tok) > 2:
                hit["dyad"] = tok[2]
            if pending_slide:
                hit["slide"] = True
                pending_slide = False
            raw.append(hit)
    # length: ring notes hold until the next onset; others are short (1 step) —
    # unless tied, in which case the tie count is the floor and the note is held
    # for its full written value (a chug that rings out, a half note over a bar).
    steps = [h["step"] for h in raw]
    for i, h in enumerate(raw):
        if h["art"] in _RING_ARTS:
            nxt = steps[i + 1] if i + 1 < len(steps) else (
                (max(steps) // steps_per_bar + 1) * steps_per_bar)
            h["len_steps"] = max(2, nxt - h["step"])
        else:
            h["len_steps"] = 1
        ties = h.get("ties", 0)
        if ties:
            h["len_steps"] = max(h["len_steps"], ties + 1)
            h["hold"] = True
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
                     "len_steps": h.get("len_steps", 1),
                     "hold": h.get("hold", False)})
    return {"ppq": spec["ppq"], "steps_per_bar": spec["steps_per_bar"],
            "bars": spec["bars"], "map": "nolly_e", "hits": hits,
            "timing_bias": 0.05, "timing_sigma": 0.05}


# ---------------------------------------------------------------------------
# The demo: 8 bars, key of E, 120 BPM. Riff A is a MOVING E-phrygian riff (the
# palm-muted line walks to b2/b3/b5/b7 between low-E power-chord anchors, so it
# reads as notes, not a monotone chug). Bars 5-6 are a dissonant palm-muted
# cluster lift; bar 8 builds and resolves. A jam start — notes are David's to
# redirect.
# ---------------------------------------------------------------------------
DEMO_BARS = [
    # Written as two four-bar PHRASES, not eight one-bar loops. Every bar either
    # ties over the barline or slides into the next one, so the part is carried
    # forward instead of restarting; `_` gives real note values instead of a row
    # of equal 16ths, and `~` slurs rather than re-picks.
    "o_______~7___b__",   # 1  E5 rings a half, slides into D(b7), then Bb
    "5___~b___M___7__",   # 2  B slides to Bb, F(b2), D — one connected line
    "Xx_.mx.xXx.v_k_.",   # 3  HEAVY riff kicks in; chugs hold, line walks F/Bb/D
    "Xx_.kx.xXx.x2___",   # 4  answer hangs an E+b2 cluster ACROSS the barline
    "2___~t___j___9__",   # 5  lift: dissonant clusters, each sliding to the next
    "2___~t___j___~r_",   # 6  lift answer slides home to the root ring
    "Xx_.mx.xXx.n_v~f",   # 7  riff returns VARIED, with a 5th pickup into bar 8
    "XxxXxfXxvXxk~o__",   # 8  gallop builds and RESOLVES on a ringing E5
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
