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
# Shreddage 3 default keyswitch + CC map (from David's shreddage.txt).
# Values are MIDI note numbers for keyswitches; CC1 (mod wheel) is the palm-mute
# morph and is not a note.
# ---------------------------------------------------------------------------
SHREDDAGE3_KS = {
    # fretting styles
    "fret_polyphonic": 117, "fret_moving_lead": 116, "fret_sweep": 115,
    "fret_natural": 114,
    # picking direction
    "pick_alternate": 110, "pick_down": 109, "pick_up": 108,
    # holds / strums (rarely needed for chug riffs)
    "hold_choke": 97, "hold_mute": 96,
    "strum_up_partial": 95, "strum_down_partial": 94,
    "strum_up_all": 93, "strum_down_all": 92,
    "toggle_strum": 91, "hold_strum": 90,
    # utility
    "highest_note": 88, "lowest_note": 33, "repeat_last": 32,
    "slide_note": 31, "play_noise": 30, "slide": 29,
    # articulations (the ones a riff actually switches between)
    "fx": 21, "tremolo": 20, "pinch_harmonic": 19, "harmonic": 18,
    "tapping": 17, "choke": 16, "pwr_staccato": 15, "staccato": 14,
    "pwr_sustain": 13, "sustain": 12,
    # neck position / string forcing
    "hand_position": 10, "force_string_off": 7,
    "force_string_1e": 6, "force_string_2b": 5, "force_string_3g": 4,
    "force_string_4d": 3, "force_string_5d": 2, "force_string_6e": 1,
    "force_string_7a": 0,
}

MODWHEEL = 1  # CC1 morphs Shreddage sustain (0) <-> palm mute (127)


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
        "library": "Shreddage 3 Hydra",
        "ks": SHREDDAGE3_KS,
        "low_string": 33,                 # A1 concert — CONFIRM BY EAR
        "strings": [33, 40, 45, 50, 55, 59, 64],  # A1 E2 A2 D3 G3 B3 E4
        "base_articulation": "sustain",   # single-note chugs
        "chord_articulation": "pwr_sustain",
        "range_lo": 33, "range_hi": 87,
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
        "low_string": 21,                 # A0 concert — CONFIRM BY EAR
        "strings": [21, 28, 33, 38],      # A0 E1 A1 D2 (drop-A 4-string span)
        "base_articulation": None,
        "range_lo": 12, "range_hi": 60,
    },
}


def get_map(name):
    if name in GUITAR_MAPS:
        return dict(GUITAR_MAPS[name])
    if name in BASS_MAPS:
        return dict(BASS_MAPS[name])
    raise KeyError(f"unknown guitar/bass map: {name!r} "
                   f"(have {sorted(GUITAR_MAPS)} / {sorted(BASS_MAPS)})")
