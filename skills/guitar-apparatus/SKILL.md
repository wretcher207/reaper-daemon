# Guitar apparatus — humanized virtual-guitar/bass MIDI

The guitar-side counterpart to `drum-apparatus`. Renders believable, human-feeling
guitar and bass MIDI (Shreddage-style keyswitches + mod-wheel palm-mute morph) and
inserts it onto REAPER tracks through the same bridge write path the drum groove
uses (`insert_midi_file`).

## What it does

- **Tuning + articulation maps** (`guitargen/maps.py`) — data-driven, like kit
  maps. `hydra_drop_a` (Shreddage 3 Hydra, 7-string Drop A) and `nolly_drop_a`
  (Nolly bass, plain-note, no map was available).
- **Riff notation** (`guitargen/riffs.py`) — one 16-char bar per line:
  `.`=rest `x`=muted chug `X`=accented chug `o`=let-ring root `g`=ghost
  `4`=fourth `5`=fifth `b`=tritone `7`=min7 `h`=octave (let-ring). Let-ring notes
  hold until the next onset; mutes are short.
- **Performance engine** (`guitargen/perform.py`) — deterministic musical contour
  drives dynamics (downbeats / pushes accent, ghosts sit back), RNG is only
  garnish, and a golden no-repeat rule keeps consecutive chugs off one velocity —
  the same taste model as the drum humanizer. Palm mutes ride **CC1 (mod wheel)**:
  high for a tight mute, near zero for a let-ring. **Double-tracking** is two
  *performances*, not a copy: same riff, different `--seed` on argent-l vs argent-r.
- **CC-aware SMF writer** (`guitargen/smf.py`) — notes + control change, so the
  mod-wheel ride actually reaches the instrument.

## Commands

```bash
# render + insert the built-in demo riff onto the double-tracked pair + bass
python reaperd.py shred --track argent-l --part guitar --seed 101
python reaperd.py shred --track argent-r --part guitar --seed 202
python reaperd.py shred --track nolly-bass-library --part bass --seed 303

# a custom riff (one 16-char bar per line), onto the selected spot
python reaperd.py shred --track argent-l --bars-file myriff.txt --position 0.0

# the range probe that confirms where the low string sounds
python reaperd.py shred --track argent-l --riff probe

# override the low-string mapping once the probe confirms it
python reaperd.py shred --track argent-l --seed 101 --low-string 33
```

Standalone (no REAPER): `python skills/guitar-apparatus/shredgen.py --riff demo
--part guitar --seed 101 --out out.mid`.

## Two things to know

1. **THE low-string knob (confirm by ear).** Shreddage's default keyswitches
   reserve MIDI 0–33 and 88–117, and a concert Drop-A low string (A1 = 33) sits ON
   the "Lowest note" keyswitch. Which candidate actually sounds, and at what
   octave, depends on the instrument's tuning page — so the demo is preceded by a
   **range probe** (4 sustained notes at 33/40/45/52). Hear which sounds where,
   set `low_string` in `maps.py` (or pass `--low-string`), and every interval in
   every riff follows from it. This is the one value that needs David's ear; it is
   a one-line change.

2. **REAPER imports MIDI by reference on this build.** The inserted item points at
   the `.mid` file on disk — it is not copied into the project. So `shred` writes
   renders to `rendered-midi/` (gitignored) and **keeps** them; deleting the file
   empties the take (proven live). The `.mid` is the take's source of truth. If a
   project is moved, its `rendered-midi/` goes with it.

## Verify loop

A part is not done until it is heard in REAPER. After `shred`, read it back with
`get_midi_notes` (structure) and audition it (feel). Trust the ears over the
plan — re-cut the riff string, re-render, re-insert.
