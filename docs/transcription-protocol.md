# Drum transcription bridge commands

## get_transcription_source

Read-only. Select by `item_guid`, by `target_track_name` or `target_track_guid`
with optional zero-based `item_index`, or by selecting exactly one audio item
in REAPER. Multiple items on a track require an index.

Returns live project/item/take identities, source path, source offset, timeline
position and length. It accepts up to 600 seconds at original speed and pitch.
Take FX, take envelopes, stretch markers, reversed/section sources and ranges
past the source file are refused. Reads source audio before item fades/gain and
track FX. No project save is needed.

## insert_drum_transcription

The shared Python workflow supplies `source` (the snapshot above), `job_id`, a
destination track selector, `expected_note_names` (pitch strings to names,
possibly empty for an explicit map), and `events` in the `insert_midi_events`
note format. Event times are seconds relative to the source item's start.
`dry_run` or `validate_only` checks preconditions without creating an undo
point or item.

The command refuses changed sources, active transport, changed note names,
duplicate job markers, the source track as destination and occupied ranges.
It inserts native notes, verifies their values and the per-pitch velocity rule,
and rolls back the new item on failure. A successful reply includes `verified`,
`item_guid`, `job_id`, `notes`, `track_guid`, `start_seconds` and `length_seconds`.
The Python workflow verifies the source file fingerprint before this command.
Insertion doesn't mute the reference, change tempo or save the project.

See [Audio to drum MIDI](transcription.md) for the CLI and MCP workflow.
