---
name: drum-apparatus
description: Generate death-metal / heavy drum MIDI on command and insert it into the user's live REAPER session via the agent bridge. Use when the user asks for a drum groove, beat, blast, breakdown, fill, or a multi-section drum arrangement, or says "give me a <subgenre> groove". Ports the Dead Pixel Drum Apparatus vocabulary (36 grooves, humanization, fills).
---

# Drum Apparatus

Generate humanized heavy-music drum MIDI and drop it into REAPER.

## When to use
The user asks for drums: a groove, a blast, a breakdown, a fill, or a full
section arrangement. Default kit: **GM Standard** (or a map you discover with
`reaperd.py discover-map` — see below).

## Workflow (always)
1. **Pick grooves** from `catalog/grooves.json` by name. Translate the user's words
   to real groove names (e.g. "tech death blast into a breakdown" →
   `Tech Death Pulse` / `Hammer Blast` → `The Pit Opener`).
2. **Compose** a single groove or an arrangement spec (sections with bars, and
   optional per-section `power_hand` / `fill`).
3. **Generate**:
   `python generate.py --spec spec.json --out /tmp/drums.mid` (or `--groove`).
4. **Insert** via the bridge `insert_midi_file` (target track = the user's
   drum track; position = the user's requested bar). Use the verified
   `python3 reaperd.py groove <dsl> --track <name>` (render + insert + verify
   in one round trip), or `reaperd.py cmd insert_midi_file '<payload>'`.
5. **VERIFY AUDIBLY (hard gate):** set cursor to the clip, `play`, and ask the
   user if it sounds right. NEVER claim a groove is good without their ear —
   this is the audio-must-be-audible rule. Parse-back proves notes exist, not
   that it slaps.

## Params that matter
`humanize` (0-100, default 45), `push_pull` (-100..100; negative = laid back /
positive = pushed), `power_hand` (hh_closed|hh_open|ride|crash|china|stack|none),
`fills` (auto tom fill + turnaround crash on the last bar).

## Musicality notes
- Blasts (Hammer/Traditional/Bomb) usually want `power_hand: none` or `ride`,
  not a busy hi-hat.
- Drop `humanize` lower (~20) for machine-tight tech death; raise (~60) for
  loose, human grooves.
- Put a `fill: true` on the section before a transition, not every section.

## Breakdowns (read this — they are NOT grooves)
A breakdown is spaced stabs + silence + naked kick chug, with a cymbal on the
BIG accents only (the stab, the snare smash) — not a steady groove with a hat.
Use the breakdown grooves that carry a `cymbal` accent lane: **"World Ending
Stomp"** (2-bar) and **"Chug Breakdown"** (1-bar). The cymbal lane auto-suppresses
the power-hand grid. Generate with `--no-fills` and low `--humanize` (~15) so the
stabs stay tight, e.g.
`python generate.py --groove "World Ending Stomp" --bars 8 --no-fills --humanize 15`.
Authoring your own breakdown groove: add a `cymbal` step-string (`X`=crash,
`C`=china, `p`=splash, `r`=ride, `b`=bell, `-`=rest) same length as `kick`;
patterns may be 2 bars (32 steps) — cross-bar indexing handles it.

## Catalog
36 grooves across 10 subgenres: DEATH METAL, SLAM DEATH, BLACK METAL, GRINDCORE,
METALCORE, DOOM & SLUDGE, PROGRESSIVE METAL, ROCK, THRASH METAL, BREAKDOWNS.
Read `catalog/grooves.json` for exact names.

## Drum kit maps (MIDI note assignments)
`catalog/maps.json` holds named maps of drum roles (KICK_R, SNARE, etc.) to MIDI
note numbers. `--map <name>` selects one at render time. Ships with: GM Standard,
plus a few example library maps (RS Monarch, Odeholm Default, MDL Tone, Sleep
Token II by MixWave) as references. `python generate.py --list-maps` prints them.

If the user's kit isn't in `maps.json`, **auto-discover** it from the library's
own `.midnam`:
```bash
python3 reaperd.py discover-map Drums --save MyKit
```
For kits with no `.midnam` (some Kontakt libraries), ask the user for the note
assignments and add a map:
```bash
python3 reaperd.py add-map MyKit --roles '{"KICK_R":36,"SNARE":38,...}'
```
Discovered/added maps save to the user overlay (`maps/<name>.json`, gitignored),
so a `git pull` never clobbers them.
