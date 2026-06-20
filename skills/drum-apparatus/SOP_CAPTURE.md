# Drum SOP — raw capture notes (David's words, interview)

> Scratch file. Gets distilled into SKILL.md, then deleted. ponytail: temp.

## Core procedure (the spine)

1. **Kicks first, alone.** Before anything else, lay down ONLY kicks.
2. **Kicks track the guitar.** Find the OPEN notes (non-palm-muted / ringing
   notes) being played on guitar and match kicks to them. At minimum, find the
   groove with the kicks against the riff.
3. **Snare comes next, and it decides everything.** Where the snare lands
   determines the whole feel and the pace of the section.
4. **Cymbal choice = the feel knob.** Kick+snare backbone is LOCKED after step 3.
   Then the only real decision left is which cymbal/power-hand, because that sets
   the mood: hi-hats = mellow groove; crashes/chinas = loud, making a lot of noise.

### Snare = NOT auto-decided
- No go-to snare placement. It's purely by feel, reaches for anything.
- David will TELL the snare placement every time. The skill must NOT guess snare.
  Input from David, not derived.

### Backbone is locked
- Kick + snare = the whole backbone. Once down, it's locked. No going back to
  move kicks after snare. Cymbal just dresses it.

### Why this matters for the SOP
- The model must NOT start by picking a named groove. It starts by deriving the
  kick pattern from the riff's open notes, then places snare.
- "Wrong groove choice" failure = skipping straight to a library pattern instead
  of building kick->snare from the actual riff.

## Riff input — DECIDED: read transients off the guitar audio

- Guitar is tracked as AUDIO before drums (DI/amped) on a pointable track.
- Plan: bridge hands over guitar item source path + item start + tempo →
  detect onsets in the audio → quantize to grid → propose kicks on strong/open
  hits → David corrects the misses.
- HONEST LIMIT: transients give onset TIMING, not open-vs-palm-mute. Can't fully
  tell ringing notes from chugs by timing alone. So it's a PROPOSAL David edits,
  not a magic auto-beat. (Maybe later: use transient strength/duration as a hint
  for open vs muted. ponytail: don't build that until the basic version proves out.)
- This is the ONLY new code the skill needs. Rest is procedure.
- Build approach (later): Python CLI already exists. Get item info from bridge,
  read source audio in Python, simple energy-based onset detection (REAPER's
  GetMediaItemTake_Peaks, or read the WAV directly). No heavy deps.

## Cymbals — NOT hard rules (CLAUDE.md's cymbal table is WRONG / too rigid)

- The CLAUDE.md "verse = open hat/ride, never closed" table is NOT David's rules.
  They're "good practices" at best. Cymbals are the one thing with no fixed rules.
- Reality: can be anything, and usually MULTIPLE cymbals going at once.
- Breakdowns: china + crash. NOT closed hi-hats (the off-putting thing the bad
  attempt produced last night — breakdowns full of closed hats).
- Verses: lean open hi-hat, but crashes sometimes too, and definitely ride.
- Bottom line: cymbal choice is by feel; the skill should ASK / take direction,
  not enforce a section->cymbal table. Default away from closed hats in heavy
  parts, but nothing is hard-and-fast.

## HUMANIZATION RULES — the golden rules (this is the core of the skill)

Failure #2 ("right groove, wrong details") lives entirely here. The machine-gun
effect (identical velocities) is the dead giveaway of fake programming.

1. **GOLDEN RULE: no two consecutive hits in a lane are EVER the same velocity.**
   Always differ, even by 1. No human is perfect; that imperfection is what sounds
   good.
2. **Cymbal-with-shell = louder.** When a cymbal lands together with a shell
   (kick/snare/tom), that cymbal strike should be HIGHER velocity — a human
   exerts more force on the cymbal because he's also striking a shell. True for
   essentially all cymbals.
3. **Hi-hat needs its own velocity curve.** Closed hi-hat = played softer; open
   hi-hat = louder. Obvious-bad tell: hi-hats all one velocity, or just
   alternating between two values.
4. **Double kick: every 2nd kick is lower, by -7 to -9.** Left foot is never as
   strong as the right. ONLY on fast double-kick parts where both feet are truly
   used (not slow single-foot kicks).
5. **Blast-beat snare: a downward arc over time, then lift at the end.** Blasts
   are exhausting; forearms/elbows lock up so intensity naturally drops as it
   goes. Then dig deep and lift slightly toward the end to finish strong.
6. **Fills: start lower, ramp up across the kit.** Especially coming off the
   highest tom — vary low->high to get a real rolling sound. Exceptions exist.
7. **Snare is almost never alone.** When the snare hits, something else hits with
   it, usually a cymbal. You never hear a lone snare hit as part of a groove.

## Ghost notes
- Yes, used. On the snare, during more laid-back parts: let the snare dance out
  ghost notes leading into a rimshot, or to break up a linear section. "Just a
  kiss" of flavor. Not a heavy-part thing so much as a groove-breather thing.

## Check against render.py / existing code
- [ ] Golden rule (no consecutive equal velocities) — is it enforced in render.py?
- [ ] Cymbal-with-shell louder — enforced?
- [ ] Hi-hat open/closed velocity split — enforced?
- [ ] Double-kick -7..-9 every 2nd — enforced? (CLAUDE.md mentions crash decay only)
- [ ] Blast snare fatigue arc — enforced?
- [ ] Fill low->high ramp — enforced?
- [ ] Snare-never-alone — enforced?
- NOTE: CLAUDE.md cymbal table needs CORRECTING/loosening too.

## Fills — placement + license
- Frequency is mood-dependent: some songs use them only to bridge sections,
  others are stuffed with them. David likes both.
- DEFAULT: fills come at the END of a section/phrase to break up the energy and
  lead into what's next.
- IMPORTANT LICENSE: David LOVES fills but hates writing them. The skill should
  GET SPICY / be creative with fills — this is the one place to be inventive,
  not conservative.

## "Snare never alone" — corrected/nuanced
- Not literal. There ARE bare snare hits. The real rule: don't OMIT a cymbal
  where one is obviously called for (last night's bug: grooves with something
  VERY obviously missing, a cymbal dropped for no reason).
- Focal / emphasized snare hits want something with them (usually a cymbal).
- Passing accents & backbeats in a flurry can be bare BECAUSE more is coming
  right after — they're not the focal point. Judge by whether the hit is the
  focal point.

## Workflow order — DECIDED: humanize inline, per note, as you place
- Order: (1) kick + snare for the section → (2) go back, choose cymbal sounds and
  fill them in → (3) decide where to throw fills.
- CRITICAL: the ENTIRE time placing notes, offset each note's velocity by 2-3
  "notches" as it's placed. Humanize-as-you-go, per note. NOT a separate final
  pass. He used to humanize the whole song at the end — agonizing. Finishing a
  part with zero velocity issues left to fix is the goal.
- => render.py should bake the golden rule + curves in AT PLACEMENT time, so the
  output is already humanized. No "now run the humanize pass" step.

## CAPTURE COMPLETE — ready to build SKILL.md + check render.py
