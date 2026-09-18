# Drums

To recreate drums from a song recording, use [audio-to-MIDI transcription](transcription.md).
It reads an audio item from the live session and maps the detected hits to your kit.

The drum engine lives in `skills/drum-apparatus/`. It ships in the cloned
repo, not via ReaPack. Three jobs: map any kit, profile a stem before writing
drums, and humanize a take that already exists.

## Kit maps

Built-in maps in `skills/drum-apparatus/catalog/maps.json`: GM Standard,
RS Monarch, Odeholm, MDL Tone, Sleep Token II. For any other library,
discover the map from the plugin itself:

```bash
# with the drum plugin on a track that has its .midnam loaded
python3 reaperd.py discover-map Drums --save MyKit

# then use it
python3 reaperd.py groove beat.dsl --track Drums --map MyKit
#   or in the DSL:   @map MyKit
```

`discover-map` reads the MIDI note names REAPER has for the track, classifies
each note into a role (kick, snare, hat-closed, hat-open, ride, crash, china,
tom1 to tom4, and so on), fills missing articulations by fallback so a sparse
kit never breaks the engine, and saves the result to
`skills/drum-apparatus/maps/<name>.json` (gitignored).

Libraries without a `.midnam` (some Kontakt kits) report no note names. Build
those by hand:

```bash
python3 reaperd.py add-map MyKontaktKit --roles '{"KICK_R":36,"SNARE":38,"HH_OPEN_1":46,"CRASH_R":49,"CHINA_R":52}'
```

## Profile a stem first

`profile` reads a guitar stem bar by bar and prints the numbers an agent
needs to plan drums: onset density, timing regularity, palm-mute versus
ringing decay, silence, low and bright band balance, a 16th-note accent
grid, suggested section boundaries, and repeat groups (A / B / A). Numbers,
not verdicts. The agent proposes section labels and you correct them.

```bash
python3 reaperd.py profile song.rpp guitar-di
python3 reaperd.py profile song.rpp guitar-di --start-bar 32 --bars 8
python3 reaperd.py profile song.rpp guitar-di --start-bar 32 --max-seconds 10
```

- Point it at any bar and it analyzes only that window, plus one bar of
  pre-roll and post-roll. The cut is hop-aligned, so timing, onset, grid,
  decay, and band numbers from a window match a full pass. Only the silence
  ratio is scored against a window-local loudness reference.
- `--max-seconds` counts whole bars that fit inside the cap and refuses caps
  shorter than one bar.
- Prefer the DI track. Distortion flattens the decay contrast that separates
  open notes from palm mutes.

MCP clients get the same steps as `profile_track`, `riff_grid`, and
`insert_groove`. The two analysis tools parse the `.rpp` on disk, so save the
project first. On an unsaved project they describe stale material and say so.

## Humanize an existing take

`humanize` gives a flat drum item that is already on a track a dynamic
contour and micro-timing, in place.

```bash
python3 reaperd.py humanize --track drums --dry-run       # plan, print, write nothing
python3 reaperd.py humanize --track drums --amount 25     # 0-100 looseness (default 25)
python3 reaperd.py humanize --track drums --follow-lead   # copy the hand from your own bars
```

- The dynamic contour is always applied. `--amount` only scales the random
  spread and timing looseness on top of it.
- Fills build as a crescendo that peaks at the resolve instead of piling up
  on the ceiling.
- The golden rule is hardline (`skills/drum-apparatus/drumgen/goldenrule.py`):
  no drum hits the same velocity twice in a row, per drum, in time order.
- `--follow-lead` reads the velocity hand out of the bars you humanized by
  hand and carries it across the rest of the take instead of the shared taste
  model. It auto-detects the first flat bar. `--example-through-bar N` sets
  the boundary explicitly.
- `--seed` is fixed by default, so a run is reproducible.
