"""Tuning + articulation maps for virtual guitars/basses.

A *guitar map* is the counterpart to the drum apparatus' kit map: it says how a
riff's abstract notes turn into concrete MIDI pitches, and how articulations turn
into keyswitches / CC on a given library. Keeping it data-driven means a retune
or a swap to another Shreddage instrument is a table edit, not a code change.

THE ONE UNVERIFIED THING (confirm by ear, then it is locked):
    Where the low string physically sounds on the keyboard. Shreddage's default
    keyswitch layout (see SHREDDAGE3_KS) reserves 0-33 and 88-117, so a concert
    Drop-A low string (A1 = 33) sits ON the "Lowest note" keyswitch. Different
    Shreddage builds/tunings resolve this differently (engine tuning page vs.
    literal pitch). `low_string` is therefore a single knob: send the range probe
    (guitargen.riffs.range_probe), hear which candidate sounds and at what pitch,
    set `low_string` to that, and every relative interval in a riff follows.
"""

# ---------------------------------------------------------------------------
# Shreddage Hydra keyswitch map — VERIFIED LIVE from David's instrument UI
# (2026-08-27), which does NOT match the older shreddage.txt. Articulation
# keyswitches sit at the very bottom (MIDI 0-11); the string-position overrides
# and utility functions ride above/below the playable range. Values are MIDI
# note numbers (Kontakt C3=60 naming: C-2 = 0).
#
# Articulation is selected by a keyswitch NOTE (not the mod wheel): the Sustain
# slot at pitch 0 is split by the KS note's own velocity — 1-115 Sustain,
# 116-126 Rake, 127 Pinch Harmonic — while Mute, Staccato, and the power-chord
# variants are their own pitches. So a palm-muted chug means firing the Mute
# keyswitch (1) before the note; a let-ring means Sustain (0).
# ---------------------------------------------------------------------------
SHREDDAGE3_KS = {
    "sustain": 0, "mute": 1, "staccato": 2,
    "pwr_sustain": 3, "pwr_mute": 4, "pwr_staccato": 5,
    "tremolo": 6, "choke": 7, "tapping": 8,
    "nat_harmonic": 9, "art_harmonic": 10, "fx": 11,
    # same pitch as sustain, selected by keyswitch-note velocity:
    "rake": 0, "pinch_harmonic": 0,
}

# articulation-slot -> (keyswitch pitch, keyswitch-note velocity). The velocity
# both stays inside Sustain's 1-115 band (so a sustain KS never trips Rake/Pinch)
# and, for the split slot, selects the sub-articulation.
KS_NOTES = {
    "sustain": (0, 64), "mute": (1, 64), "staccato": (2, 64),
    "pwr_sustain": (3, 64), "pwr_mute": (4, 64),
    "pinch_harmonic": (0, 127), "rake": (0, 120),
}

MODWHEEL = 1  # CC1. This Hydra mutes via the Mute keyswitch, not the mod wheel,
#              so the mod-wheel ride is off by default (map flag modwheel_mute).


# ---------------------------------------------------------------------------
# Tuning maps. `strings` is low -> high open-string MIDI pitch at CONCERT pitch.
# `low_string` is the sounding MIDI note of the dropped/lowest string open —
# the riff root — and is the knob the range probe confirms. The rest of the
# strings are given for chord/dyad voicing; a single-note chug only needs the
# low string plus intervals above it.
# ---------------------------------------------------------------------------
GUITAR_MAPS = {
    # Shreddage 3 Hydra, 7-string, Drop A. Concert pitch: low A1 = 33.
    # 33 collides with the "Lowest note" keyswitch, so this is exactly the value
    # to confirm by ear. If the probe shows the low string sounds an octave up,
    # set low_string 45 and transpose_octaves accordingly.
    "hydra_drop_a": {
        "library": "Shreddage Hydra",
        "ks": SHREDDAGE3_KS,
        "ks_notes": KS_NOTES,
        # VERIFIED LIVE: the instrument's lowest playable note is MIDI 24, which
        # in Drop A is the low string open (sounds A). Every interval in a riff
        # is relative to this, so a fifth (root+7) etc. lands right regardless of
        # how the sample engine fingers it.
        "low_string": 24,
        "range_lo": 24, "range_hi": 107,
        "base_articulation": "sustain",
        "modwheel_mute": False,           # this Hydra mutes via keyswitch
    },
}

BASS_MAPS = {
    # Nolly bass library — no note map was available, so this is a plain-note
    # map: no keyswitches, no mod wheel. Notes at concert pitch, root an octave
    # below the guitar low string (A0 = 21). CONFIRM the octave by ear the same
    # way; `low_string` is the knob.
    "nolly_drop_a": {
        "library": "Nolly Bass",
        "ks": {},
        # VERIFIED LIVE: the Nolly library's lowest playable note is MIDI 28 —
        # its low string open (sounds A, an octave under the guitar's low A).
        # The old value (21) was below range, so the bass was silent.
        "low_string": 28,
        "base_articulation": None,
        "modwheel_mute": False,
        "range_lo": 28, "range_hi": 67,
    },
}


def get_map(name):
    if name in GUITAR_MAPS:
        return dict(GUITAR_MAPS[name])
    if name in BASS_MAPS:
        return dict(BASS_MAPS[name])
    raise KeyError(f"unknown guitar/bass map: {name!r} "
                   f"(have {sorted(GUITAR_MAPS)} / {sorted(BASS_MAPS)})")
