---
name: drum-apparatus
description: Generate death-metal / heavy drum MIDI on command and insert it into David's live REAPER session via the agent bridge. Use when David asks for a drum groove, beat, blast, breakdown, fill, or a multi-section drum arrangement, or says "give me a <subgenre> groove". Ports the Dead Pixel Drum Apparatus vocabulary (36 grooves, RS Monarch map, humanization, fills).
---

# Drum Apparatus

Generate humanized heavy-music drum MIDI and drop it into REAPER.

## When to use
David asks for drums: a groove, a blast, a breakdown, a fill, or a full
section arrangement. Default kit: **RS Monarch (Kontakt 8), MIDI channel 1.**

## Workflow (always)
1. **Pick grooves** from `catalog/grooves.json` by name. Translate David's words
   to real groove names (e.g. "tech death blast into a breakdown" →
   `Tech Death Pulse` / `Hammer Blast` → `The Pit Opener`).
2. **Compose** a single groove or an arrangement spec (sections with bars, and
   optional per-section `power_hand` / `fill`).
3. **Generate**:
   `python generate.py --spec spec.json --out /tmp/drums.mid` (or `--groove`).
4. **Insert** via the bridge `insert_midi_file` (target track "Kontakt 8",
   position = David's requested bar; default channel 1). Use the verified
   `send_reaper_command.sh`.
5. **VERIFY AUDIBLY (hard gate):** set cursor to the clip, `play`, and ask David
   if it sounds right. NEVER claim a groove is good without his ear — this is
   the audio-must-be-audible rule. Parse-back proves notes exist, not that it
   slaps.

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

## Catalog
36 grooves across 10 subgenres: DEATH METAL, SLAM DEATH, BLACK METAL, GRINDCORE,
METALCORE, DOOM & SLUDGE, PROGRESSIVE METAL, ROCK, THRASH METAL, BREAKDOWNS.
Read `catalog/grooves.json` for exact names.
