# Guitar apparatus — humanized virtual-guitar/bass MIDI

The guitar-side counterpart to `drum-apparatus`. Renders believable, human-feeling
guitar and bass MIDI (real instrument keyswitches, palm mutes, power chords) and
inserts it onto REAPER tracks through the same bridge write path the drum groove
uses (`insert_midi_file`). Maps confirmed against the instruments' own manuals and
David's live UI (see `docs/instruments.md`).

## The instruments (this rig)

- **Guitar — Shreddage 3.5 Argent**: 9-string (Schecter Damien Platinum), lowest
  string C#; plays at concert pitch. Palm mute = the **Mute keyswitch** (C#-2 / 1),
  and the mute *depth* is the played note's **velocity** — not the mod wheel.
  **Power Chord Mute/Sustain** (E-2 / D#-2) auto-voice root+5th+octave from one
  note. Lowest *playable* note is MIDI 25 (low C#); MIDI 24 is the Thrash-note
  keyswitch, not a pitch.
- **Bass — GGD The Nolly Bass Library**: Dingwall NG2 6-string, pick-based (auto
  alternate picking), no slaps/taps/bends. Range MIDI 28–79, floor E0 = 28.
- Map `argent_e` roots the riff at **E (28)** so guitar and bass lock on the same
  low note; `nolly_e` sits the bass at the same E.

## What it does

- **Tuning + articulation maps** (`guitargen/maps.py`) — data-driven, like kit
  maps: `argent_e`, `nolly_e`.
- **Riff notation** (`guitargen/riffs.py`) — one 16-char bar per line:
  `.`=rest `x`=muted chug `X`=accented (power chord) `o`=let-ring root (power
  chord) `g`=ghost `4`=fourth `5`=fifth `b`=tritone `7`=min7 `h`=octave (let-ring).
  Let-ring notes hold until the next onset; mutes are short.
- **Performance engine** (`guitargen/perform.py`) — deterministic musical contour
  drives dynamics (downbeats / pushes accent, ghosts sit back), RNG is only
  garnish, and a golden no-repeat rule keeps consecutive chugs off one velocity —
  the same taste model as the drum humanizer. Articulation is set by **keyswitch**:
  a fast root chug is Mute (single note), a root accent/hold voices a **power
  chord** (Argent auto-voices it), and melodic non-root notes stay single so a
  lead line reads as a line. **Double-tracking** is two *performances*, not a copy:
  same riff, different `--seed` on argent-l vs argent-r (live-proven width).
- **CC-aware SMF writer** (`guitargen/smf.py`) — notes + control change (CC is
  ready for mod-wheel maps; Argent does not need it).

## Commands

```bash
# THE one-liner: lay down / re-cut the whole 4-track jam (2 guitars + bass +
# drums), replacing whatever is on those tracks. Re-run it after any riff edit.
python reaperd.py band
# custom riff + drum DSL, other tracks/tempo:
python reaperd.py band --bars-file myriff.txt --dsl mydrums.dsl --tempo 138
```

`band` defaults to the session tracks (argent-l/argent-r/nolly-bass-library/
rs-drums-monarch), the demo riff, the bundled `examples/jam-e.dsl` drums, seeds
101/202/303, and `--replace` on. It's the smooth path — one command instead of
clear+insert four times.

Single-part commands (what `band` is built from):

```bash
# one part at a time; --replace clears the track region first
python reaperd.py shred --track argent-l --part guitar --seed 101 --replace
python reaperd.py shred --track nolly-bass-library --part bass --seed 303 --replace
python reaperd.py groove examples/jam-e.dsl --track rs-drums-monarch --replace

# a custom riff — one bar per line; spaces inside a bar are visual only
python reaperd.py shred --track argent-l --bars-file myriff.txt --position 0.0

# a general low-string probe for a NEW/unknown instrument
python reaperd.py shred --track argent-l --riff probe

# override the riff root once (e.g. try the low C# string)
python reaperd.py shred --track argent-l --seed 101 --low-string 25
```

Standalone (no REAPER): `python skills/guitar-apparatus/shredgen.py --riff demo
--part guitar --seed 101 --out out.mid`.

## Two things to know

1. **Root note = `low_string`.** Every interval in a riff is relative to the map's
   `low_string` (the riff root, E=28 here). To move the key, change that one value
   or pass `--low-string`. For a brand-new instrument whose range is unknown, the
   `probe` riff plays sustained candidates so the floor can be found by ear.

2. **REAPER imports MIDI by reference on this build.** The inserted item points at
   the `.mid` file on disk — it is not copied into the project. So `shred` writes
   renders to `rendered-midi/` (gitignored) and **keeps** them; deleting the file
   empties the take (proven live). The `.mid` is the take's source of truth. If a
   project is moved, its `rendered-midi/` goes with it.

## Verify loop

A part is not done until it is heard in REAPER. After `band`/`shred`, read it
back with `get_midi_notes` (structure) and audition it (feel). Trust the ears
over the plan — re-cut the riff string, re-run `band`.

Note: `get_midi_notes` returns each note's position as **`ppq`** (with
`ppq_per_quarter`), not `tick` — a common trip-up, since the engine and tests
speak `tick`. Read `n["ppq"]`, and a track with more than one item needs an
explicit `item_index` (else `AMBIGUOUS_ITEM`).

A distorted high-gain tone flattens dynamics (the RMS envelope pins near max
whether a note is muted or open), and it masks pitch movement to the ear. If a
riff "sounds the same," the fix is usually musical — move the notes (pitch),
not just the velocity/articulation.
