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
    # Shreddage 3.5 Argent — 9-string (Schecter Damien Platinum), lowest string
    # tuned to C#. Plays at concert pitch. Confirmed against the official manual
    # and David's instrument UI:
    #   * lowest PLAYABLE note = MIDI 25 (low C# string open); MIDI 24 is the
    #     "Thrash note" keyswitch, NOT a pitch — do not root a riff there.
    #   * palm mute = the Mute keyswitch (C#-2 = 1); the mute DEPTH (very->half
    #     muted) is set by the played note's VELOCITY, so no mod wheel.
    #   * Power Chord Mute (4) / Power Chord Sustain (3) auto-voice root+5th+
    #     octave from a single root note.
    # low_string is the riff ROOT. Chosen E (28) so guitar and bass lock on the
    # same low note (bass floors at E); the engine frets E on the low strings.
    "argent_e": {
        "library": "Shreddage 3.5 Argent (9-string, low C#)",
        "ks_notes": KS_NOTES,             # articulation slot -> (ks pitch, ks vel)
        "low_string": 28,                 # riff root E; physical low string = 25
        "modwheel_mute": False,           # Argent mutes via keyswitch + velocity
        "power_chords": True,             # root hits can voice power chords
    },
}

BASS_MAPS = {
    # GGD The Nolly Bass Library — Nolly's Dingwall NG2 6-string (the Periphery V
    # tone). Pick-based with automatic alternate picking; no slaps/taps/bends/
    # slides. Confirmed range E0-G4 = MIDI 28-79; the floor is E0 = 28 (why the
    # earlier A0 = 21 was silent). Driven as plain notes, locked to the guitar's
    # low E.
    "nolly_e": {
        "library": "GGD Nolly Bass (Dingwall NG2 6-string)",
        "low_string": 28,                 # E0, the library's lowest note
        "modwheel_mute": False,
        "power_chords": False,
    },
}


def get_map(name):
    if name in GUITAR_MAPS:
        return dict(GUITAR_MAPS[name])
    if name in BASS_MAPS:
        return dict(BASS_MAPS[name])
    raise KeyError(f"unknown guitar/bass map: {name!r} "
                   f"(have {sorted(GUITAR_MAPS)} / {sorted(BASS_MAPS)})")
