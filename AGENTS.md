# Reaper Daemon

Cross-platform local bridge for controlling a running REAPER instance through MCP or
the zero-dependency `reaperd.py` CLI.

## Read only what the task needs

- `HANDOFF.md`: current repo state, verified live behavior, and next work. Read the
  current section before material development or release work.
- `README.md`: installation, CLI, MCP, measurement, and agent usage.
- `bridge/command_schema.md`: authoritative command payloads and result shapes. Open
  the relevant command section instead of loading the entire catalog by default.
- `skills/drum-apparatus/`: drum DSL and kit-map behavior when that workflow is used.
- `skills/guitar-apparatus/`: riff notation, tuning/keyswitch maps, and the performance
  engine behind `shred` and `band`. Read it before writing or editing a riff file.
- `docs/CONSOLE.md`: the Daemon Console (in-REAPER panel + `console_sidecar.py`) —
  architecture, file protocol, money/liveness rules. Read it only when the task
  touches the console; its session rules live in `docs/console_contract.md`.
- Writing drum velocities — by any path, including a one-off script — means
  `skills/drum-apparatus/drumgen/goldenrule.py`. No drum ever hits the same velocity
  twice in a row, per drum in time order, whatever lands in between. `enforce()` to
  apply it, `violations()` to prove it before the write.

Prefer the MCP server when its tools are available. Use `reaperd.py` when MCP is not
connected or when testing the CLI itself. Do not write ad hoc bridge JSON unless the
task is specifically testing the file protocol.

The part-writing path is on both surfaces: `shred`/`band`/`humanize` on the CLI map
to the `insert_riff`/`cut_band`/`humanize_take` MCP tools (`insert_groove` covers
`groove`). One shared write path underneath, so behavior does not differ by surface.

## Action boundary

- For a request to inspect, explain, diagnose, or review, use read-only discovery and
  report the result. Do not mutate the REAPER project.
- For a request to change the live project, check bridge status, inspect context, make
  the narrow requested change, and verify it without asking for routine local steps.
- Deleting tracks, items, FX, or markers; overwriting media; and rendering require
  clear intent in the current request. Do not broaden a track-scoped request to every
  track.

## Safe mutation loop

1. Check liveness once before the first live command. If the bridge is dead or stale,
   report that REAPER or the startup bridge must be started.
2. Run `get_context` before an ambiguous edit. Use `dry_run: true` when target or effect
   is uncertain.
3. Resolve tracks and FX by stable GUID or verified name. Scan parameters before
   setting them; never guess plugin indices or normalized values.
4. Batch related parameter changes into one undo block with `stop_on_error: true`.
5. Re-read the changed state or use `verify_change`, `tune_param`, or CLI `verify` to
   prove the result.

An `ok: true` response proves command handling, not audio improvement. For an audio
claim, measure the same frozen time range before and after. Repeat the tool's caveat if
the capture was not an isolated track or was based on a saved `.rpp` rather than live
state. Never build a verdict on a silent capture.

CLI verify exit code 2 means the mutation may already have happened but verification
failed. Do not retry blindly. Report it as unverified and explain that one REAPER undo
reverts the attempted change.

Current status and release facts belong in `HANDOFF.md`, not here.
