---
name: composing-workflow
description: Compose and write full song parts (guitars, bass, drums, atmosphere) into a live REAPER session through Reaper Daemon with sampled virtual instruments. Use before writing any guitar/bass/drum/pad MIDI for a song, choosing keyswitch articulations (Shreddage Argent, Umansky Bass), measuring what a patch actually sounds like, building a varied cymbal layer, or inserting and verifying sections. Covers look-measure-plan-build-verify and the lessons that made parts sound stabby, out of tune or static.
---

# Composing workflow

> **MANDATORY.** Every agent composing through Reaper Daemon follows this skill and
> `docs/composing-workflow.md`. Before planning: `print(history.recent())` (last ~10
> songs; never read the whole file) and pick a clearly different key/tempo/feel/riff
> concept unless the user asks for similarity. After verification:
> `history.append_song(...)` once.

Full guide: `docs/composing-workflow.md`. Read sections 1, 7 and the instrument
section you are writing before you write MIDI.

## Non-negotiables

1. **Look, never guess.** Scan tracks/FX (`get_context`, `get_fx_parameters`,
   `discover_drum_map`), read manuals on disk. Missing mapping = stop and report.
2. **Measure sounding pitch** with `composer.measure.probe` + `analyze` for every
   pitched instrument before choosing roots. Labels, octave conventions and
   Transpose knobs are not evidence. Pitch-class-check bass vs guitar.
3. **Articulate every note** (`composer.artic`). Note length IS the sound on
   sampled guitars: muted notes hold to ~12 ms before the next pick, open notes
   ring to the next onset. No fixed short chug length. Keep any one articulation
   under ~60% of a section.
4. **Double-tracks are two renders** (different seed and lean), never a copy.
5. **Vary cymbals every 4-8 bars** (`composer.cymbals`), accent with the guitar,
   choke stops, loud crashes; drum velocities pass the golden rule (0 violations).
6. **Build one section at a time and read it back** (`composer.verify`). Replace
   only items you created. Don't save the project or change plugin settings
   without approval; report plugin observations instead.

## Module (`skills/composing-workflow/composer/`)

```python
import sys; sys.path.insert(0, r"<repo>/skills/composing-workflow"); sys.path.insert(0, r"<repo>")
import reaperd
from composer import artic, cymbals, measure, verify
send = reaperd.send_type

ev, steps = artic.render("3pc! 2pm4 1pm2 3pcm!:F 2pm4 1st 4lr:G",
                         instrument="argent", bpm=140, swing=0.6, seed=7, root=28)
print(artic.stats(ev, 140))
rep = verify.replace_section(send, "Guitar L", artic.to_events(ev), bpm=140,
                             start_bar=9, nbars=1, ks_pitches=set(range(25)),
                             play_range=(25, 64), replace_existing=False,
                             expect_lengths=[e["duration"] for e in ev if e.get("tag")])
```

| File | Does |
| --- | --- |
| `composer/artic.py` | token DSL -> keyswitches, lengths, velocities, overlap clamp |
| `composer/cymbals.py` | phrase rotation, downbeats, guitar-accent hits, chokes, golden rule |
| `composer/measure.py` | temporary probe item + solo + capture + FFT; always cleans up |
| `composer/history.py` | `recent(n=10)` compact tail, `append_song(**fields)` capped line; `songs/history.jsonl` (gitignored user data) |
| `composer/verify.py` | scoped delete, insert, read-back checks, L/R differ |
| `tests/test_composer.py` | offline tests (`python -m pytest skills/composing-workflow/tests`) |

`artic.EXAMPLE_RIG` holds offsets measured on ONE user's patches. They are not
library facts. Re-measure on every rig and pass `root=` explicitly.
