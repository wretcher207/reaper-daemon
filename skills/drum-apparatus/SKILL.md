---
name: drum-apparatus
description: Program heavy / death-metal drums into the user's live REAPER session the way David does it — kicks built off the guitar riff first, snare by his call, cymbals by feel, spicy fills, humanized at placement time. Use when the user asks for a drum groove, beat, blast, breakdown, fill, or a multi-section drum part, or says "give me a <subgenre> groove" / "program drums for this riff". Drops generated MIDI into REAPER via the agent bridge.
---

# Drum Apparatus — David's drum-programming SOP

This is how David programs drums. Follow the order. The cardinal sin is starting
from a named groove in the catalog — David does NOT think that way. He builds the
beat from the riff up. The catalog is a vocabulary to reach into mid-build, not a
menu to order from.

## The procedure (in this order, always)

### 1. Kicks first — built off the guitar riff
Drums start as kicks ALONE, matched to the guitar. David tracks the guitar as
audio before drumming.

- Find the riff's guitar take (he points you at the track, or it's the obvious
  guitar audio item under the cursor) and **read its transients** to get the hit
  timing, quantize to the grid, and propose kicks on the strong/open hits.
- HONEST LIMIT: transients tell you *when* he picks, not which notes ring open vs
  palm-muted. So this is a **proposal he corrects**, not a magic auto-beat. Show
  the kick grid, let him fix the few you get wrong. That's still miles faster than
  him describing a riff in words (which he says is basically impossible).
- If there's no guitar take yet (drums-first), ask him for the kick rhythm. Don't
  invent one.
- Transient reading: `python -m drumgen.riff <project.RPP> <track> <bars> <start_bar>`
  (parses the .RPP for the track's stem, detects onsets, prints the kick grid at
  100/50/30% attack strength).
- **Kick density = a real flavor choice (David's words):**
  - **Slam / breakdown (his DEFAULT):** one single kick per transient. Sparse,
    heavy, slamming. Use a LOW keep_pct (~30) so only the strong open hits become
    kicks. This is how David's own hand plays it.
  - **Gallops / triplets:** keep_pct=100 turns every pick attack into a kick, so a
    fast riff reads as gallops/triplets. David does NOT instinctively do this but
    LIKES being pushed to it ("not what I would've done, but that's why I like it").
    Offer it as the adventurous option, don't make it the default.
- **Phase calibration:** the grid is anchored to item time 0; a stem's real downbeat
  can sit a step off. GTR_1 read one 16th early -> pass `offset_steps=1`. Per stem,
  so check the first render against the riff and adjust.
- It's a PROPOSAL David corrects (the percentile is a strength heuristic, not true
  open-vs-muted). He has ears; let him pick density and dial the offset.

### 2. Snare — David calls it, every time
There is **no default snare placement.** It's pure feel and it sets the whole
section's pace and feel. Once kick + snare are down, the backbone is LOCKED —
you do not go back and move kicks.

- ASK for the snare placement, or take what he gives. Never guess it.
- If he describes it loosely ("backbeat", "half-time", "on the e of 3"), place
  that — but the call is his.

### 3. Cymbals — the feel knob, by ear
Kick + snare are the backbone; the cymbal choice is what sets the mood. There is
**no fixed section→cymbal rule** (the old rigid table was wrong). It can be
anything, and it's usually several cymbals at once.

- Hi-hats → more mellow groove. Crashes / chinas → loud, lots of noise.
- The ONE default: in heavy parts, **steer away from closed hi-hats** unless he
  asks — closed-hat breakdowns are the off-putting tell of a bad beat.
- Verses lean open hi-hat, but crashes and ride are all fair game.
- Breakdowns lean **china + crash on the stabs**, never a hat grid. (See
  Breakdowns below — they're not grooves.)
- When in doubt, ask which cymbal. It's a taste call, not a rule.

### 4. Fills — last, and get spicy
Fills go in last, after the backbone and cymbals. Default placement: **at the end
of a section/phrase**, to break up the energy and lead into what's next. Frequency
is mood-dependent — some songs only use fills to bridge sections, others are
stuffed with them; both are good.

- David LOVES fills but hates writing them: **be creative here.** This is the one
  place to be inventive, not conservative. Roll across the kit, vary it, surprise
  him.
- Fill velocity shape: start lower, ramp up across the kit — especially coming off
  the highest tom, low→high gives the real rolling sound.

## Humanization — the golden rules (applied at placement time)
David humanizes as he places each note (nudging velocity 2–3 notches per hit), not
in a final pass. So the engine bakes these in at render time — the output is
already humanized, nothing to fix after. These are physics, not taste:

1. **No two consecutive hits in a lane are EVER the same velocity** — differ by at
   least 1. Identical velocities = the machine-gun tell. *(in code: feel.unique_velocity)*
2. **A cymbal that lands together with a shell (kick/snare/tom) is louder** — a
   human pushes harder on the cymbal because he's also hitting a shell.
   *(in code: both engines — groovekit + render.py CYMBAL_SHELL_BOOST)*
3. **Hi-hat has its own curve: closed = softer, open = louder.** Never one flat
   hat velocity, never just two alternating.
   *(in code: both engines — groovekit + render.py HAT_CURVE)*
4. **Double kick: every 2nd kick is −7…−9 lower** (weaker left foot). ONLY on fast
   double-kick parts where both feet are truly used — not slow single-foot kicks.
   *(in code: both engines — groovekit run-scoped −7…−9 + render.py)*
5. **Blast-beat snare arcs down over time, then lifts at the end** — blasts
   exhaust the forearms so intensity sags, then he digs deep to finish. *(pending)*
6. **Fills ramp low→high across the kit.** *(in code: render._render_fill — rolls
   down the kit, ramps up, varies density; the spicy floor, can layer more later)*
7. **Don't omit a cymbal where one is obviously called for.** Bare focal snare hits
   are the bug. Passing accents in a flurry can be bare (more is coming right
   after); the focal hits want something with them. *(judgment — apply by ear; the
   engine also FLAGS bare focal snare/cymbal hits as warnings — groovekit
   exposed_focal_hits — so they surface for review)*

## Velocity targets & per-kit voices (David's mix preferences)
- **Kicks stay under ~114.** Punchy, not maxed. *(in code: groovekit KICK_VEL_MAX,
  per-render override `kick_vel_max`)*
- **Monarch kit → rimshot snare, held 90–110.** The rimshot is fuller/louder; it's
  his go-to snare on Monarch, and it runs hot (loud-as-fuck by design).
  *(in code: groovekit VOICE_PROFILE["RS Monarch"])*
- These live in `VOICE_PROFILE` (per map) + `KICK_VEL_MAX` in groovekit.render. As
  new kits get preferences, add a profile entry — don't hardcode in the render loop.

## Ground truth from David's own part (reverse-engineered, Monarch)
Decoded from a full part he programmed by hand (skill-build.RPP, bars 1–4). Treat
as the reference for how his layers actually behave:
- **Every snare and every cymbal lands ON a kick** — 100% coincidence in the
  sample. Focal hits are never bare; a shell is always under them. This is the
  hard form of the "snare/cymbal never alone" rule (#7) and the cymbal-with-shell
  rule (#2).
- **Cymbals are the loudest layer and they drive.** Crashes 101–117, china 116,
  a double-crash (CRASH_R+CRASH_L stacked) on the big accent, china+crash on the
  section-end stab. Snares 90–110 and toms 92–106 sit underneath. Cymbals are not
  garnish — they're the top of the dynamic stack.
- **Snare placement is by feel, not a fixed backbeat** (landed on 1.4, 2.3, 3.1,
  4.1, 4.3), and he'll drop a 16th-apart double rimshot for flavor.
- **Loud sections = a crash wall** on ~8th spacing, never a hi-hat grid.
- **Tom fills roll across the kit with velocity ramping up** (observed 92→106).

## Generate & insert
1. **Compose** the section(s) as a spec or DSL once kicks/snare/cymbals/fills are
   decided (sections with bars, per-section `power_hand` / `fill`).
2. **Generate**: `python generate.py --spec spec.json --out /tmp/drums.mid`
   (or `--groove`), or the one-shot `reaperd.py groove <dsl> --track <name>`
   (render + insert + verify in one round trip).
3. **Insert** on the track David SELECTED (or `--track <name>` if he names one).
   NEVER guess a track.
4. **VERIFY AUDIBLY (hard gate):** set the cursor to the clip, `play`, and ask if
   it sounds right. NEVER claim a beat is good without his ear. Parse-back proves
   the notes exist, not that it slaps.

## Params that matter
`humanize` (0-100, default 45), `push_pull` (-100..100; negative = laid back /
positive = pushed), `power_hand` (hh_closed|hh_open|ride|crash|china|stack|none),
`fills` (auto fill + turnaround crash on the last bar).
- Lower `humanize` (~20) for machine-tight tech death; raise (~60) for loose,
  human grooves.
- Put `fill: true` on the section before a transition, not every section.

## Breakdowns (read this — they are NOT grooves)
A breakdown is spaced stabs + silence + naked kick chug, with a cymbal on the BIG
accents only (the stab, the snare smash) — not a steady groove with a hat. Use the
breakdown grooves that carry a `cymbal` accent lane: **"World Ending Stomp"**
(2-bar) and **"Chug Breakdown"** (1-bar). The cymbal lane auto-suppresses the
power-hand grid. Generate with `--no-fills` and low `--humanize` (~15) so the
stabs stay tight, e.g.
`python generate.py --groove "World Ending Stomp" --bars 8 --no-fills --humanize 15`.
Authoring your own: add a `cymbal` step-string (`X`=crash, `C`=china, `p`=splash,
`r`=ride, `b`=bell, `-`=rest) same length as `kick`; 2-bar (32-step) patterns work,
cross-bar indexing handles it.

## Catalog (vocabulary, not a menu)
36 grooves across 10 subgenres (DEATH METAL, SLAM DEATH, BLACK METAL, GRINDCORE,
METALCORE, DOOM & SLUDGE, PROGRESSIVE METAL, ROCK, THRASH METAL, BREAKDOWNS) in
`catalog/grooves.json`. Reach into these for patterns mid-build — but the beat is
built from the riff (step 1), not selected from this list.

## Drum kit maps (MIDI note assignments)
`catalog/maps.json` maps drum roles (KICK_R, SNARE, etc.) to MIDI notes. `--map
<name>` selects one. Ships with GM Standard plus example library maps (RS Monarch,
Odeholm Default, MDL Tone, Sleep Token II). `python generate.py --list-maps` lists
them. If the kit isn't there, auto-discover from its `.midnam`:
`python3 reaperd.py discover-map Drums --save MyKit`. For kits with no `.midnam`
(some Kontakt libs), ask for the notes: `reaperd.py add-map MyKit --roles
'{"KICK_R":36,"SNARE":38,...}'`. Discovered maps save to the gitignored user
overlay, so `git pull` never clobbers them.
