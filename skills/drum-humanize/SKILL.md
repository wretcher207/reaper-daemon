---
name: drum-humanize
description: Humanize existing REAPER drum MIDI with phrase-aware dynamics and optional micro-timing. Use for flat programmed drums, mechanical fills, or extending a user's humanized example. Preserve authored feel and explicit velocity-only scope; use the shared drum-apparatus engine.
---

# Drum humanize

Make the part feel performed: phrase shape, articulation contrast, believable
accents and coordinated motion. Randomness is a small finishing layer. A passing
MIDI check proves edits, not that the drums sound better.

## Read once, work narrowly

Check bridge liveness and current context. Resolve the requested drum track by
GUID, then inventory only its relevant MIDI items. Scan the whole project only
when the user requests a session-wide pass or the target cannot be resolved.
Prefer REAPER MCP tools when available; otherwise use `reaperd.py` from the repo.

Read complete notes and note names, checking truncation. Keep a local before
snapshot of note identity, pitch/channel, velocities, starts and ends. Identify
kit roles from a verified kit map or exposed note names. No blind GM fallback:
unnamed MIDI might be a bass or a non-GM drum library. Resolve ambiguous roles
before editing those notes; preserve choke/mute triggers and keyswitches.

Do not require a new map approval on every pass. A verified map for the same
instrument/articulations is sufficient; invalidate it when the patch or names
change. Ask only about unresolved targets, roles or musical intent. Once the
requested edit is clear, proceed through planning and verification without
separate permission rounds for scan, plan and apply.

Reuse the note read for role analysis and planning. Immediately before writing,
re-read the take and compare the relevant note fields to the snapshot; a note
count alone does not catch same-count changes. Replan on drift. Do not reuse a
cached take across edits, project switches, or a later request.

## Choose the musical treatment

- **Flat programmed part:** use `drumgen.humanize.plan_humanize` with the verified
  map. Its role balance, metric accents, fast-kick alternation and detected fill
  crescendos provide structure before jitter. Kit-specific bands are starting
  points, not universal drumming laws or safe values for every library.
- **An approved humanized example:** prefer `--follow-lead` to learn its velocity
  bands and accents and extend them into the flat remainder. Preserve the example.
  Report extrapolated roles absent from it. Check the inferred boundary rather
  than assuming the first varied bars are a deliberate example.
- **Already expressive playing:** preserve its ghosts, accents, swells, swing and
  intentional flams. Fix the specific mechanical passage or repeated values.
  Do not feed it through an absolute role-band rewrite by default. If a broad
  rewrite is explicitly requested, explain that it replaces existing dynamics.

Read [references/planning.md](references/planning.md) for supported native commands,
velocity-only planning, timing-only filtering, scope limits and verification.

## Human feel before variation

Use the actual meter, backbeat placement, phrase length, density and section energy.
Do not impose beats 2 and 4 on a halftime or odd-meter groove. The native default
model assumes 4/4; a `bar_ticks` change alone does not teach it another accent map.

Maintain ghost-to-backbeat contrast, including ghosts sharing a pitch with accents.
Separate a real tom/snare fill from an ostinato. Let fills build through the hands,
with a controlled arrival rather than an arbitrary last-hit spike. Validate the
final contour after no-repeat enforcement; a late nudge can disturb a crescendo.
Long rolls may need several gesture arcs rather than an impossible strict rise
across dozens of hits in a narrow band. Never widen an instrument's approved
velocity band solely to force every hit higher than the preceding one.

Treat simultaneous hits as one musical moment. Preserve intentional flams and
their spacing. Keep dense kick runs stable; avoid exaggerated fatigue or a rigid
two-value alternation. Hats and rides should carry pulse and phrasing rather than
independent random numbers. Section changes should follow the arrangement or a
user example, not a universal crescendo imposed across the song.

David's no-repeat constraint is mandatory: same drum, in time order, even with
other drums between hits. This is a project preference, not proof that identical
MIDI velocities can never occur in real playing. Every velocity plan must use
`drumgen.goldenrule.enforce()` and prove `violations()` before the write and on
readback. Preserve deliberate articulation contrasts while fixing repeats.
When several items form one passage, check the joins in project-time order too;
the native planner handles one take at a time and does not know those joins.

## Timing is a separate decision

Honor velocity-only requests exactly: no changed starts, ends or note lengths.
Do not add timing to an already performed part just to increase a humanization
score. When the request is simply "humanize," use the musical evidence: flat
velocities need dynamics; actual grid rigidity may also justify subtle timing.
State which dimensions will change before applying.

For timing work, preserve unison accents and deliberate flam offsets. Bound moves
against local note spacing and tempo, not a universal 15 ms setting. Check order,
item edges, same-pitch overlaps and collisions after planning. Avoid compounding
timing nudges across repeated passes; regenerate from the stored original when
iterating, after confirming it still corresponds to the intended passage.

Do not automatically trim drum tails. Some instruments use note-offs, sustained
cymbals, choke groups or articulations. Prefer reducing/rejecting the colliding
move when duration matters or behavior is unknown. A bridge end-trim advisory
does not mean the audible result is harmless.

## Apply and prove

Use the shared engine plus index-addressed `apply_note_edits`, with explicit track
GUID/item, note count, and `allow_partial: false`. Combined velocity/timing edits
on one take belong in one command. This supports stacked notes; the older
position/pitch writer can reject them. Do not promise one undo for a multi-item
edit that doesn't run as a single batch. A batch is not transactional rollback:
earlier commands may have applied if a later one fails.

Re-read all targeted notes. Check note count, pitch/channel, requested values,
start AND end, unedited dimensions, repeat violations, fill contours and joins.
If a failure/timeout occurs, inspect before retrying. Report partial or unverified
changes with their undo scope. `ok: true` alone is insufficient.

Audition representative groove, dense run, fill, and section transition with the
same instrument/mix. If the user hears no improvement, diagnose the audible
response before generating another numeric pass. Capture only with the required
authorization; use equal bounds and valid non-silent provenance for audio claims.
Report briefly: target, dimensions changed, preserved intent, checked invariants,
and what remains for listening. Do not automatically save the RPP.

## Maintenance

Use the repo's current `drumgen/humanize.py`, `learn.py`, and `goldenrule.py` as the
engine source of truth. Older standalone DeHumanizer scripts/references may remain
on a user's machine for reproduction; they are not the default live workflow.
Do not claim that the native model implements every musical judgment above.
Unsupported cases need a scoped custom plan or clarification, not invented flags.
