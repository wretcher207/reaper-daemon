# Musical Automation System Plan

Status: active, Phase 1 complete / Phase 2 next (revised 2026-08-21 after review; the original draft's cut
machinery is preserved in "Deferred" at the bottom)
Scope: `reaper-daemon` bridge, automation evidence + safety layer, verification,
and live-use procedure
Primary goal: produce safe, stereo-coherent automation that responds to the
actual arrangement without turning one accepted move into a universal preset

## Outcome

Build a two-layer automation system:

1. A fixed engineering layer, in code, controls identity, scope, reversibility,
   stereo integrity, and proof.
2. An adaptive musical layer — **the agent in the session, not a Python
   scorer** — chooses gestures from the live arrangement, source behavior,
   section function, and project-specific feedback.

The dividing line: every hard rule must be about **provability**, never about
**what the music does**. The fixed layer decides whether a plan is safe to
execute and whether it actually landed. It never contains a preferred Laser
curve, Glitch rhythm, filter sweep, default contour, or any other musical
recipe — and it never picks the musical winner. Taste stays in the model;
proof stays in the code.

The finished system supports a one-shot structural workflow:

`inspect -> model arrangement -> agent plans gestures -> repetition check ->
apply atomically -> reread -> audition -> report`

An unattended run may finish with `structurally_verified: true`; it may not
claim `musically_accepted: true`. Musical acceptance requires David to audition
the result. A supervised run can perform both gates in one session.

If track/FX identity, parameter meaning, existing automation, or rollback
cannot be resolved, the system stops before mutation. A valid plan may choose
no gesture for a section or for the entire request.

## Problem to solve

The current bridge can write automation but lacks enough judgment and readback
to distinguish these cases:

- one macro section containing several marked activation windows;
- several independent sections that should receive separate gestures;
- a deliberate reprise that should repeat a recognizable motif;
- unrelated sections where repeating the same move is lazy or musically wrong;
- matching stereo automation versus coincidentally similar plugin state;
- a successful API response versus the automation that actually exists in
  REAPER;
- a failed batch with no write versus a failed batch that left a partial
  stereo edit.

The failure mode to prevent is literal stamping: treating every marker pair as
an independent section, applying the same full gesture to each pair, and then
calling the result verified from command replies alone. (Observed 2026-08-21:
an agent applied one identical gesture to all sixteen marker windows without
assessing the arrangement.)

## Design principles

### 1. Safety rules are invariant; musical decisions are not

The safety layer may enforce:

- stable track and FX identity (GUIDs);
- exact parameter resolution;
- fixed marker boundaries;
- left/right point equality when tracks form a stereo pair;
- preservation of unrelated and out-of-range automation;
- atomic rollback;
- live envelope readback;
- an audible, non-silent verification capture.

It must not enforce:

- any default contour, gesture location, or envelope shape;
- a fixed number of gestures per song;
- a numeric "musical fit" threshold;
- a global library of moves learned from one project.

### 2. Arrangement hierarchy comes before gesture generation

Markers are observations, not a complete musical model. Before any gesture is
chosen, the evidence layer builds a hierarchy from marker order, spacing,
numbering, item boundaries, regions, time signatures, and tempo:

```text
Song
  MacroSection
    Phrase
      ActivationWindow
```

Each node carries exact start/end time, bar/beat/meter/tempo context, source
markers or regions, and a stated reason for its placement. Hierarchy decisions
must be **explainable, not scored**: the model reports what it inferred and
why (e.g. "windows 1–6 share a numbering family and sit inside one item; the
4-bar gap and numbering restart before window 7 suggests a new macro
section"). When the evidence is genuinely ambiguous, the tool says so and
stops for the agent (or David) to confirm — it never silently flattens every
marker pair into an independent section, and it never gates on an invented
confidence float.

The regression fixture from the 2026-08-21 session must resolve to three macro
sections containing sixteen activation windows — not sixteen independent macro
sections.

### 3. The agent is the planner

Gesture choice belongs to the agent in the session, working from:

- the normalized evidence object (arrangement, current parameter state,
  existing automation, source items);
- section length, density, and neighboring-section behavior;
- project-local accepted/rejected history;
- explicit user constraints;
- its own musical judgment.

Code supports this with evidence and warnings; it does not rank candidates,
weight scores, or select winners. `no_gesture` is always a legitimate choice,
including for a whole request. Repetition across sections is a **flag the
agent must acknowledge**, not an automatic rejection (see Repetition check).

Plans are structured data, not a series of ad-hoc bridge calls:

```json
{
  "section_id": "...",
  "intent": "build|release|transition|punctuation|reprise|none",
  "motif_id": null,
  "controls": [],
  "activation_windows": [],
  "reset": {},
  "rationale": "why this gesture, in terms of this arrangement"
}
```

The `rationale` field is mandatory and section-specific. A plan whose
rationales are copy-pasted across sections is itself evidence of stamping.

### 4. No completion claim without production truth

A returned `ok: true` proves command handling. It does not prove that envelope
points landed correctly, that both stereo sides match, or that the move sounds
musical.

Completion requires:

- live envelope reread;
- stereo comparison;
- outside-range preservation proof;
- reset and activation proof;
- audition of the affected ranges from a non-silent capture or the live
  session.

## Architecture

### A. Live evidence collector

A read-only collector produces one normalized session evidence object:

```json
{
  "project": {},
  "tempo_map": [],
  "tracks": [],
  "items": [],
  "markers": [],
  "regions": [],
  "automation": [],
  "selection": {},
  "transport": {}
}
```

Requirements:

- Resolve tracks and FX by GUID.
- Include hard pan, mute, solo, automation mode, FX enabled/offline state, and
  parameter count.
- Page through complete parameter scans.
- Read existing envelopes without creating new ones.
- Record envelope visibility, arm state, automation mode, points, shapes,
  tension, and any automation items present.
- Fingerprint the sources used by a plan so a stale plan can be rejected
  before apply (markers, FX chain, envelopes, tempo, items).

Audio-feature extraction (RMS, spectral measures, transient density) is
deferred; marker/region/tempo/item structure must prove insufficient in real
use before that complexity is added.

### B. Repetition check (flag, not rejector)

Compute a normalized gesture signature per section from: control set,
direction/contour, normalized event positions, point values, shapes/tension,
activation pattern, and reset behavior.

Rules:

- Exact or near-exact signatures across **unrelated** macro sections raise a
  warning the agent must explicitly acknowledge before apply, stating whether
  the repetition is intentional (reprise, ostinato, utility gating, silence)
  or a mistake to fix.
- Sections sharing a declared `motif_id` or reprise relationship repeat
  freely.
- One complete gesture stamped across many short nested windows raises the
  same warning.
- The check never forces novelty when repetition is the musical point; it
  exists to make accidental sameness impossible to miss, not to generate
  variation.

"Near-exact" means the point lists match after quantization (time to 1/960
quarter note, value to 1e-6) up to a uniform time offset. No weighted
similarity mathematics.

### C. Stereo lock compiler

Compile one shared musical plan and instantiate it onto both stereo tracks.
Never generate the left and right plans independently.

Before apply:

- verify both track GUIDs and hard-pan positions;
- verify matching plugin identity, parameter surface, and target parameter
  names/indices;
- allow irrelevant current knob differences when the written automation and
  reset contract are explicitly shared;
- compare canonical point hashes for left and right;
- reject the batch if any time, value, shape, activation state, or reset
  differs.

After apply, recompute the hashes from live envelope reread. Pre-existing
asymmetric automation outside the requested ranges is reported, never silently
normalized.

### D. Transactional automation writer

Before mutation:

- snapshot every touched envelope, including whether it existed;
- snapshot all points inside the replacement ranges;
- fingerprint points outside the ranges;
- snapshot track automation mode and envelope arm/visibility state;
- verify the plan's source fingerprint is still current; refuse with
  `PROJECT_CHANGED_DURING_PLAN` if not.

During mutation:

- replace points only in declared ranges and parameters (inclusive start,
  inclusive end; neighbor points are never part of the replacement set);
- stop on first error;
- restore the complete preimage automatically on any failure;
- return `rolled_back: true` only after rereading the restored state.

On success:

- close one named REAPER undo block;
- leave the project unsaved (the bridge has no save command by design; the
  ultimate recovery path is always David's undo/close-without-save);
- return the transaction ID and exact touched envelopes.

Automation items: phase 1 reads them (pool IDs, offsets, rates, loops) but
does not mutate them. If a target range intersects an automation item, apply
refuses with `AUTOMATION_ITEM_UNSUPPORTED`. Refusal beats flattening or
silently editing a pooled source.

### E. Independent envelope readback

Add `get_fx_param_automation` to the bridge.

Payload:

```json
{
  "target_track_guid": "{...}",
  "fx_guid": "{...}",
  "param_index": 13,
  "start_time": 10.0,
  "end_time": 20.0,
  "include_neighbors": true
}
```

Return: resolved track/FX/parameter identities; envelope existence and state;
automation mode; all points in range; nearest point before and after the
range; point count, shape, tension, selected state, automation-item identity;
a canonical hash.

Canonical ordering is `(time, value, shape, tension, selected,
automation_item)` after the quantization above. Duplicate-time points are
preserved in REAPER enumeration order and reported explicitly.

`write_fx_param_automation` also rereads its writes before reporting success.
Its reply includes requested, inserted, confirmed, replaced, skipped, and
refused counts.

### F. Audition and musical acceptance

Verification has two separate gates:

1. Structural acceptance: the live envelopes exactly match the plan.
2. Musical acceptance: the result works in context.

Unattended checks: capture each affected macro section with short handles;
confirm the capture is not silent; confirm both guitar sides remain present
and balanced; compare activation windows with intended event times; flag
clipping, missing signal, or side-only activation.

Automated audio checks cannot declare a move tasteful. They return
`structurally_verified`, `capture_verified`, and measured warnings. Until
David auditions, the run status is `awaiting_musical_acceptance`, not
complete. The first real use of a new kind of gesture is supervised, and its
accepted/rejected decision is written to the project-local profile.

## Project-local learning

Store feedback beside the project evidence, never as a universal preference:

```text
state/automation-projects/<project-fingerprint>/
  arrangement.json
  accepted-gestures.json
  rejected-gestures.json
  parameter-maps.json
```

Rules:

- A decision accepted in one song does not become the default for another.
- Store why a gesture was accepted or rejected, not only its points.
- Invalidate project-local data when track, item, marker, FX, or tempo
  fingerprints change materially; corrupt state is quarantined, never used.
- Never commit private project state or creative preferences to the
  repository.

## Deliverables

### Bridge (Lua)

- `get_fx_param_automation`
- inclusive, documented range semantics for envelope replacement
- readback verification in `write_fx_param_automation`
- automation preimage snapshot and restore
- automation transaction with automatic rollback
- automation mode and envelope-state reporting

### Python

```text
automation/
  evidence.py       # collector + fingerprints
  arrangement.py    # hierarchy inference, explainable
  repetition.py     # signature + stamping flag
  stereo.py         # shared-plan compile + hash compare
  transaction.py    # preimage, apply, rollback, verify
  schemas/
    evidence.schema.json
    plan.schema.json
```

CLI:

```text
python reaperd.py automation inspect
python reaperd.py automation apply <plan.json>
python reaperd.py automation verify <transaction-id>
```

There is no `plan` subcommand: the plan file is authored by the agent from the
inspect output. A one-shot wrapper may run inspect/apply/verify together but
must persist the inspect artifact and plan for auditability.

### Agent procedure (project skill, added after the command surface is proven)

1. Confirm bridge round-trip.
2. Collect live evidence.
3. Resolve arrangement hierarchy; confirm or correct it if flagged ambiguous.
4. Resolve and compare stereo targets.
5. Scan/calibrate parameters.
6. **Assess the whole arrangement first**, then plan section-specific
   gestures — different where the music differs, repeated where a motif or
   reprise calls for it, absent where restraint serves the track.
7. Acknowledge any repetition flags with stated intent.
8. Compile one shared stereo plan.
9. Apply one automation transaction.
10. Reread every touched envelope.
11. Audition affected ranges.
12. Report verified facts and leave the project unsaved.

The skill constrains procedure and proof only. It contains no gesture recipes,
contour defaults, or per-plugin musical rules.

## Test plan

### Offline unit tests

- Marker hierarchy: three macro sections with sixteen nested windows resolves
  correctly.
- Ambiguous hierarchy stops and reports instead of flattening.
- A valid zero-gesture plan is selectable and applies no mutation.
- Meter and tempo changes preserve musical subdivision positions.
- Identical signatures across unrelated sections raise the stamping flag.
- Motif/reprise-linked signatures pass without a flag.
- Stereo compiler produces byte-identical canonical point lists; any one-sided
  difference refuses before apply.
- Boundary points exactly at section starts/ends have defined replacement
  behavior.
- Existing points outside ranges retain the same canonical hash.
- An injected failure restores the full envelope preimage.
- Pre-existing asymmetric stereo automation outside the ranges is reported,
  not normalized.
- A stale plan refuses after a marker, FX-chain, envelope, tempo, or item
  edit.
- Duplicate-time and reordered equal-time points hash deterministically.
- A target intersecting an automation item refuses before mutation.
- Corrupt project-local state is quarantined and never used for planning.

### Bridge integration tests

- Read an existing envelope without creating or arming it.
- Write, reread, and confirm all point fields.
- Replace only points in range; preserve immediate neighbors.
- Restore an envelope that did not exist before the transaction.
- Restore track automation mode, visibility, and arm state.
- Fail the right-channel write and prove the left channel was rolled back.
- Prove one successful transaction creates one REAPER undo entry, and a failed
  transaction creates no misleading successful entry.
- Simulate a project edit between planning and apply and prove the fingerprint
  check refuses.

### Live acceptance fixture

A throwaway REAPER project with: a hard-panned stereo pair; matching Misha X
instances (or a deterministic test plugin); three macro sections; nested
marker windows with restarted numbering; existing related and unrelated
envelope points; a deliberate reprise; one unrelated section that demands
contrast.

The fixture must resolve three macro sections and their nested windows.
Acceptance of an applied plan requires:

- three macro-level gestures (or fewer with stated restraint), not sixteen
  stamped copies;
- contrast between unrelated sections, intentional motif handling for the
  reprise;
- matching left/right readback hashes;
- no changed point outside declared ranges;
- verified resets and gating;
- automatic restoration after an injected mid-batch failure;
- a non-silent audition capture;
- a typed refusal when the source is silent or the plugin cannot expose the
  requested parameter/envelope surface;
- no project save.

## Rollout phases

### Phase 1: Readback foundation

`get_fx_param_automation`; envelope/automation-mode state in reads; canonical
hashing and boundary semantics; offline and live read-only tests.
Exit gate: a cold agent can reconstruct existing live automation without
creating or changing an envelope.

### Phase 2: Transactional writes

Preimage snapshots; automatic rollback; write reread; outside-range
fingerprint verification.
Exit gate: an injected right-channel failure leaves neither channel changed;
one undo entry on success.

### Phase 3: Arrangement hierarchy

Normalized evidence object; macro/phrase/window inference with explainable
reasons; ambiguity stop; the three-sections/sixteen-windows fixture.
Exit gate: the fixture resolves the intended hierarchy and ambiguous variants
stop for confirmation.

### Phase 4: Repetition flag + stereo workflow

Gesture signatures and the stamping flag; shared-plan stereo compilation and
hashing; the inspect/apply/verify CLI; the agent skill and concise final
report.
Exit gate: one command reaches structural verification or stops before
mutation with a typed reason; a stamped plan cannot apply without an explicit
acknowledgment.

### Phase 5: Real-session proof

Run on a throwaway copy first; visually inspect the arrangement; apply to a
real stereo performance with David supervising; audition every affected macro
section; write accepted corrections into project-local evidence and
procedural safeguards — never into a global musical preset.
Exit gate: David accepts the musical result, readback is exact, stereo remains
locked, and the source project is not saved.

## Definition of done

- The bridge can read existing automation independently.
- Automation writes verify themselves by rereading REAPER.
- A partial stereo failure automatically restores both sides.
- The evidence layer models nested arrangement structure before any gesture is
  chosen, and stops on genuine ambiguity.
- Safety logic contains no hardcoded musical move, contour, threshold, or
  scoring formula.
- Accidental stamping cannot apply without an explicit, stated intent.
- Intentional motifs and reprises remain possible.
- Musical feedback stays project-local.
- Real-session acceptance includes visual inspection and audible context.
- The project remains unsaved unless David saves it manually.

## First implementation slice

Do not begin with arrangement inference. Start with the proof surface:

1. Implement `get_fx_param_automation`.
2. Add the three-sections/sixteen-windows regression fixture.
3. Implement automation preimage snapshot and rollback.
4. Make `write_fx_param_automation` reread and confirm its points.
5. Only then build arrangement inference and the repetition flag.

That order prevents a more capable planner from making unprovable or partially
reversible edits.

## Deferred (cut from the original draft; revisit only on demonstrated need)

Preserved so the original thinking is not lost. Each item was cut because it
either re-encoded musical judgment as arithmetic or added recovery machinery
disproportionate to a solo local tool whose backstop is REAPER undo on an
unsaved project.

- **Deterministic candidate scorer**: fixed component weights summing to 1.0,
  `musical_fit_min` threshold, six-decimal ranking, hash tie-breaks, and
  versioned weight bumps. Cut because it converts the adaptive musical layer
  into a second fixed layer and reduces the agent to a candidate generator;
  the candidate-family list would become the de facto preset library this
  plan forbids. Seed-determinism of plans is likewise dropped as a
  requirement — explainability replaces reproducibility.
- **Numeric hierarchy confidence gates** (0.85 / 0.60 thresholds, tiered
  `ARRANGEMENT_AMBIGUOUS`): replaced by explainable inference plus a binary
  "ambiguous → stop and confirm."
- **Weighted repetition similarity** (0.92 near-exact score): replaced by
  quantized point-list comparison.
- **Crash-safe transaction journal, per-project transaction lock, per-envelope
  compare-and-swap, restart recovery, `RECOVERY_CONFLICT` /
  `ROLLBACK_UNCONFIRMED` supervised-merge flows**: revisit only if a real
  mid-write bridge death ever produces damage that REAPER undo cannot fix.
- **Audio-feature evidence** (RMS, spectral centroid, transient density,
  capture latency metadata) and **typed visual/audio evidence provenance**:
  revisit if structural evidence proves insufficient for hierarchy in real
  use.
- **Pooled automation-item mutation**: only with an exact preimage/restore
  model and dedicated overlap tests.
