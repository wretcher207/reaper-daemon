# Drum workshop

The workshop prepares separate briefs for drum ideas, compares the resulting
MIDI structures, and keeps audition feedback attached to the version you heard.
The composing agent still writes the notes. This tool doesn't call a model,
train one, or decide which part sounds best.

## Prepare a comparison

Save a brief as JSON:

```json
{
  "request_id": "song-a-first-pass",
  "project_id": "song-a",
  "description": "Eight bars of death metal groove with room to write riffs. Develop a motif and lead into the next section.",
  "tempo": 182,
  "bars": 8,
  "map": "RS Monarch",
  "seed": 182,
  "exclude_families": ["ride", "choke"],
  "preferences": [],
  "references": []
}
```

The exclusions in this example belong to this request. Leave the list empty
when the user hasn't excluded anything. Supported families are kick, snare,
tom, hat, ride, crash, china, splash, stack, bell and choke.

```powershell
python reaperd.py drum-workshop prepare brief.json --output workshop
```

This creates three folders, each with its own `request.json`:

| Candidate | Input |
| --- | --- |
| fresh | Current brief, without reference patterns or stored preferences |
| contrast | Another independent composition from the current brief |
| wildcard | Current brief, with a request to explore an unexpected musical idea |

If you supply approved references, `reference` replaces `contrast`. Only that
composition request includes the reference notes. References are DSL files:

```json
{"name": "Approved phrase", "path": "reference.dsl", "approved": true}
```

Paths are relative to the brief file unless absolute. The workshop stores a
snapshot, so editing the original reference later won't change the comparison.
Use `approved: true` only for material the user has actually approved.

## Compose without leaking the examples

Give each composer only its `request.json`, the drum DSL syntax and the kit
mapping. A genuinely independent composition needs a separate model context
that hasn't seen the references or sibling candidates. The folders themselves
aren't a sandbox. If one agent already knows the examples, disclose that the
comparison wasn't reference-blind.

Write `candidate.dsl` and `intent.json` in each candidate folder. The intent
file contains three short strings:

```json
{
  "premise": "The rhythmic idea this candidate establishes",
  "development": "How it repeats, changes and lands",
  "exploration": "What this candidate tries beyond the expected treatment"
}
```

Choose a musical premise before writing the grid. Give cymbals a purpose within
each phrase, such as keeping time, opening a phrase or answering an accent.
Avoid converting a preference into a fixed cymbal quota. Leave room for a
deliberately repetitive passage when the music calls for it.

The workshop currently supports 4/4, 1 to 64 bars, 40 to 320 BPM, and grids of
8, 12, 16, 24, 32, 48 or 64 steps per bar. Each candidate must match the brief's
tempo, bar count and kit. Exact second-based endings need a separate arrangement
step when the length doesn't fall on a bar boundary.

## Evaluate and audition

```powershell
python reaperd.py drum-workshop evaluate workshop
```

Each evaluation writes a new folder with MIDI files, frozen candidate DSL and
`report.json`. It uses the existing drum humanizer and the shared velocity
rule, then checks the serialized MIDI notes. Trailing silence extends to the
requested bar count. It doesn't insert anything into REAPER.

The report compares kick/snare onsets separately from drum-family onsets.
Changing velocities, kit pitches or cymbals won't hide an unchanged kick/snare
pattern. It also searches for the closest one- or two-bar phrase at different
bar positions. Similarity runs from 0 to 1 and measures shared onsets. A common
backbeat can score highly; that isn't proof of copying. The comparison doesn't
recognize every transformation, including half-time or double-time rewrites.

All valid candidates remain available, including the wildcard. Similarity
doesn't remove a candidate or select a winner. Explicit exclusions still apply
to every candidate. The simultaneous-hand warning is a rough review prompt,
not a complete test of whether a drummer could play the part.

Audition the candidates through the same verified kit and routing. When live
insertion is authorized, use the existing bridge workflow with an explicit
destination and length, preserve occupied material, and verify the inserted
notes. A structural pass alone doesn't establish musical quality.

## Record what the user heard

Save the user's feedback in a JSON file:

```json
{
  "candidate_id": "wildcard",
  "report": "evaluation-REPLACE_WITH_ACTUAL_ID/report.json",
  "usefulness": "revise",
  "novelty": "new",
  "reason": "The displaced snare gave me a riff idea. Keep that and simplify the fill.",
  "scope": "request"
}
```

```powershell
python reaperd.py drum-workshop feedback workshop --feedback feedback.json
```

`usefulness` accepts `use`, `revise` or `reject`. `novelty` accepts `familiar`,
`new` or `unsure`. The tool binds the record to the evaluated DSL hash, even if
someone has since edited the working candidate. The caller must supply the
user's actual feedback; the tool can't verify who wrote it or whether they listened.

Feedback defaults to the request. Project scope applies to the named project.
Global scope requires explicit `confirmed: true`. Nothing automatically turns
a rating into a preference or changes a shared taste model.

Future briefs can carry explicit preference records with `text` and `scope`.
Request and project records also need `target_id`; unrelated records are
ignored. Global preferences require `confirmed: true`. Example scope preserves
an observation without treating it as a general rule. Active preferences appear
in the evaluation report for judging, not in the fresh composition requests.

The MCP tool `drum_workshop` exposes the same three actions with `path`, optional
`output`, and an inline `feedback` object. Both surfaces operate on local files
without contacting REAPER.
