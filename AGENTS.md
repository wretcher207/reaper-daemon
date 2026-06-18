# REAPER Agent Bridge — Agent Instructions (macOS)

You can control REAPER through this local file bridge. No network, no socket:
you read and write JSON files in shared folders, and a Lua script running
inside REAPER executes them.

This bridge is plugin-agnostic. It knows nothing about any specific synth, amp
sim, or drum tool. You discover what a project contains, then act on it. If a
human asks you to set up controls for plugins you have never seen, use
`scan_fx` to learn the project, then build and save a recipe.

## Bridge paths

The bridge root is this repository folder. All paths below are relative to it.

```text
inbox/                  write command JSON here
outbox/                 read result JSON here (same id)
bridge/heartbeat.json   liveness check
recipes/                saved recipes (JSON) — you can save and replay these
```

## Required workflow

1. Check `bridge/heartbeat.json`. If missing or its `alive_at` is stale (more
   than a few seconds old), the bridge is not running — tell the user to
   (re)start REAPER, or run the bridge action manually (see README). On macOS
   the bridge auto-starts via `Scripts/__startup.lua`.
2. Send a command: write `inbox/<id>.json.tmp`, then rename to
   `inbox/<id>.json`. The rename must be atomic; never write `.json` directly.
3. Poll for `outbox/<id>.json`. Read the result.
4. Report what happened. On `ok: false`, `error.code` is an `UPPER_SNAKE` code
   (e.g. `NO_TARGET_TRACK`, `AMBIGUOUS_FX`).

Run `get_context` before any ambiguous edit. Use `dry_run: true` on a mutating
command to preview without changing the project.

## Sending a command

Easiest — the helper auto-fills `id`/`created_at`/`created_by`, writes
atomically, and (with `--wait`) polls the outbox and prints the result:

```bash
./send_reaper_command.sh commands/examples/get_context.json --wait
```

Doing it directly in bash (what an agent that drives its own shell does):

```bash
root="$(pwd)"
id="agent-$(date +%Y-%m-%dT%H-%M-%S)-$(openssl rand -hex 2)"
python3 - "$id" > "inbox/$id.json.tmp" <<'PY'
import json, sys, datetime
print(json.dumps({
  "id": sys.argv[1], "version": 3, "type": "get_context",
  "created_by": "agent", "created_at": datetime.datetime.now().astimezone().isoformat(),
  "dry_run": False, "payload": {"include_fx": True}
}, separators=(",", ":")))
PY
mv -f "inbox/$id.json.tmp" "inbox/$id.json"
# poll
for _ in $(seq 1 120); do [ -f "outbox/$id.json" ] && cat "outbox/$id.json" && break; sleep 0.25; done
```

## Commands

Full payloads and examples: `bridge/command_schema.md` and `commands/examples/`.

**Read / discover**
- `get_context` — project, tracks, FX names, transport, markers, regions.
- `get_fx_parameters` — full parameter list for one FX (paged).
- `scan_fx` — every FX and its parameters across the project. Use this first
  when you do not know what plugins a project uses.

**Transport / project**
- `play`, `stop`, `pause`, `record`
- `set_cursor`, `set_time_selection`, `set_tempo`
- `render` (gated — needs `allow_risk_level_3`)

**Tracks**
- `add_track`, `delete_track`, `rename_track`, `select_track`
- `set_track_volume`, `set_track_pan`, `mute_track`, `solo_track`, `arm_track`
- `set_track_color`

**FX**
- `add_fx`, `remove_fx`, `bypass_fx`, `move_fx`
- `set_fx_param`, `write_fx_param_automation`

**Markers / regions / items**
- `add_marker`, `add_region`, `delete_marker`, `delete_items_in_range`

**MIDI**
- `insert_midi_file`, `audition_groove`

**Composition**
- `batch` — run several commands as one undo block.
- `save_recipe`, `list_recipes`, `get_recipe`, `apply_recipe`

## Targeting tracks, FX, and parameters

Every command that acts on a track resolves it the same way, in order:
`target_track_guid`, then `target_track_name` (exact, case-insensitive), then
the selected track. FX resolve by `fx_index` or `fx_name_contains`; parameters
by `param_index` or `param_name_contains`. Ambiguous matches throw
`AMBIGUOUS_*` — narrow the selector. Never hardcode FX or parameter indices;
they shift between plugin versions. Query with `scan_fx` or
`get_fx_parameters` first, then act.

## FX parameter workflow (read this before setting params)

1. **Scan first.** Send `get_fx_parameters` with `fx_name_contains` and a
   `page_size` (default 50, bump to 100+ for FabFilter or other deep plugins).
   Read the full param list: `index`, `name`, `normalized_value`,
   `formatted_value`. The `formatted_value` tells you what the knob actually
   reads (e.g. "-16.00 dB", "Punch", "4.00:1") — more useful than the
   normalized float.
2. **Prefer `param_index` over `param_name_contains`.** Many plugins have
   params whose names share a word: FabFilter Pro-C 3 has "Threshold", "Auto
   Threshold", and "Lock Auto Threshold". `param_name_contains: "Threshold"`
   throws `AMBIGUOUS_PARAM`. Use the `index` from step 1 instead — it's
   unambiguous. Reserve `param_name_contains` for names you confirmed are
   unique in the scan (e.g. "Auto Gain", "Oversampling").
3. **Batch the sets.** Wrap multiple `set_fx_param` calls in a `batch`
   command with `stop_on_error: true`. One undo block, one round-trip, and a
   failure stops cleanly instead of half-applying.
4. **Verify.** Re-scan with `get_fx_parameters` after the batch to confirm
   the `formatted_value` on each param matches what you intended.
5. **Normalized values are 0.0-1.0, not the displayed value.** A threshold
   of -24 dB on Pro-C 3 is `normalized_value: 0.6`, not `-24`. The scan tells
   you the current normalized value; interpolate from there. When unsure, set
   a value, re-scan, read the `formatted_value`, and adjust.

## Generating MIDI (macOS)

On macOS you generally run your own shell, so the Windows "job worker" is not
needed: write the `.mid` file yourself (anywhere on disk), then send
`insert_midi_file` with its absolute `midi_path`, a `target_track_name`, and a
`position` (`{"type":"cursor"}` or a bar/beat). The bridge inserts it inside an
undo block. The optional job-worker runner from the Windows repo was left out
of this port for that reason.

## Recipes — set up reusable controls

A recipe is a named, saved list of commands. Use recipes to capture a control
setup once and replay it, on this project or another.

1. Discover: `scan_fx` to see the FX and parameters in the project.
2. Build: assemble the commands that create the setup.
3. Save: `save_recipe` with a `name`, `description`, and the `commands` array.
4. Replay: `apply_recipe` with the `name` runs them as one undo block.

Recipes are plain JSON in `recipes/`. They are plugin-agnostic — a recipe that
references FX by name works on any project that has those plugins.

## Safety

- Check the heartbeat before sending commands.
- Run `get_context` before ambiguous edits.
- Prefer the selected track only when the user clearly means it or exactly one
  track is selected. Do not act on all tracks unless explicitly asked.
- Do not overwrite existing media items unless `replace_existing_in_range` is
  true. Do not delete items, tracks, or FX without clear intent.
- `render` is gated behind a config flag; do not assume it is enabled.
- Every mutating command is wrapped in a REAPER undo block, so a mistake is
  recoverable with Cmd+Z.
