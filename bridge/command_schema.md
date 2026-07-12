# Command Schema

Every command is a JSON file in `inbox/`. Every result is a JSON file in
`outbox\` with the same `id`.

## Envelope

```json
{
  "id": "agent-2026-05-18T21-15-00-3f9a",
  "version": 3,
  "type": "get_context",
  "created_by": "agent",
  "created_at": "2026-05-18T21:15:00-04:00",
  "dry_run": false,
  "payload": {}
}
```

`dry_run: true` on a mutating command returns what *would* run without changing
the project. Read-only commands ignore `dry_run` and execute normally.

`token` (optional): when `auth_token` is set in `bridge_config.json`, every
command must include a matching `token` or it's rejected with `AUTH_FAILED`.
`reaperd.py` fills it in automatically from the same config. Off by default.

`id` is a queue filename component: it must contain only letters, numbers,
dot, underscore, and hyphen, and must not contain `..`. `reaperd.py` rejects
unsafe supplied IDs before it reads or writes any queue path, then verifies the
constructed path remains inside its intended queue directory.

## Result

```json
{ "id": "...", "ok": true, "type": "...", "finished_at": "...",
  "message": "...", "data": { ... } }
```

On failure: `ok: false`, no `data`, and `error: { code, details }` where `code`
is an `UPPER_SNAKE` token (`NO_TARGET_TRACK`, `AMBIGUOUS_FX`, `AMBIGUOUS_SCOPE`,
`NO_PARAM`, `AUTH_FAILED`, ...).

## Shared selectors

**Track** — every track-targeting command resolves, in order:
`target_track_guid`, `target_track_name` (exact, case-insensitive),
`track_name_contains` (substring, case-insensitive; errors
`AMBIGUOUS_TARGET_TRACK` on multiple matches), then `use_selected_track: true`
(errors `NO_TARGET_TRACK` when nothing is selected). There is deliberately NO
implicit selected-track fallback: targeting the selection requires the explicit
`use_selected_track` flag (post-"KT Out 1" rule — a groove once landed on the
wrong track via silent fallback).

**FX** — `fx_index` (0-based) or `fx_name_contains` (substring,
case-insensitive). `fx_scope`: `track`, `input`, or `all`. A name search
defaults `fx_scope` to `all`; **`fx_index` requires an explicit `fx_scope`**
(`track` or `input`) — a bare index silently meant track-FX-N and could hit the
wrong plugin (→ `AMBIGUOUS_SCOPE`).

**Parameter** — `param_index` (0-based) or `param_name_contains`.

### Stable discovery identities

Read-only discovery exposes REAPER's real identities; the bridge never derives
them from display names or array positions:

- `scan_fx.tracks[].guid`
- `scan_fx.tracks[].fx[].guid`
- `scan_fx.tracks[].fx[].index`
- `scan_fx.tracks[].fx[].api_index`
- `scan_fx.tracks[].fx[].scope`
- `get_fx_parameters.track.guid`
- `get_fx_parameters.fx.guid`
- `get_fx_parameters.fx.index`
- `get_fx_parameters.fx.api_index`
- `get_fx_parameters.fx.scope`

Track and FX GUIDs are the stable identity pair. `index` is zero-based within
`scope`; `api_index` is REAPER's encoded index and is an implementation detail
for clients that need to correlate raw API output. Names can be duplicated and
indices can change whenever the project or FX chain is edited.

These fields are additive to the existing response objects. Consumers should
ignore fields they do not use and must not require a fixed object-key order.
The bridge will not silently replace a GUID with a synthetic value. If REAPER
does not provide an identity, a safety-sensitive consumer must fail closed
rather than reconstructing one from a name or index. Before a later mutation,
rescan and verify that the GUID, scope, index, and verified name still describe
the same object.

**Position object** — used by cursor, markers, automation, MIDI placement:

```json
{ "type": "cursor" }
{ "type": "time", "seconds": 12.5 }
{ "type": "bar", "bar": 33 }
{ "type": "marker", "name": "Chorus" }
{ "type": "region", "name": "Verse 1" }
{ "type": "time_selection" }
{ "type": "selected_item" }
```

---

## Read / discover

### get_context
`{ "include_fx": true }` — project name, tempo, cursor, transport, time
selection, every track (with FX names when `include_fx`), markers, regions.

### get_fx_parameters
```json
{ "target_track_name": "Bass", "fx_name_contains": "EQ",
  "fx_scope": "all", "param_name_contains": "Gain",
  "limit": 200, "offset": 0, "include_empty": false }
```
The response identifies the resolved objects with stable REAPER GUIDs:
```json
{
  "track": { "index": 4, "name": "Bass", "guid": "{TRACK-GUID}" },
  "fx": {
    "index": 0, "api_index": 0, "scope": "track",
    "name": "VST3: Pro-Q 4", "guid": "{FX-GUID}",
    "parameter_count": 347
  },
  "parameters": []
}
```
`fx.index` is zero-based within `fx.scope`; `fx.api_index` is REAPER's encoded
index. Use `track.guid` + `fx.guid` as stable identity. Names can be duplicated,
and both indices can shift when a chain is edited.

### scan_fx
Every FX and its parameters across the project. Omit the track selector to scan
all tracks.
```json
{ "include_values": false, "max_params": 500 }
```
`include_values: true` adds current value / formatted value per parameter (much
larger). With `include_values: false` you get parameter names and indices only.
Every `tracks[]` entry carries its real track `guid`; every `tracks[].fx[]`
entry carries its real FX `guid` plus `index`, `api_index`, and `scope` using the
same identity rules as `get_fx_parameters`.

### discover_drum_map
Dump a drum track's MIDI note names (the `.midnam` the drum library installed)
so the agent can auto-build a groovekit kit map. Returns `notes` as
`{ "<pitch>": { "name": str, "channel": int } }` plus the track's FX list and
`has_note_names` (false when the library exposes no note names -> fall back to
GM Standard or a hand-built map).
```json
{ "target_track_name": "Drums", "channels": [0], "max_pitch": 127 }
```
The classification into groovekit roles (KICK_R, SNARE, HH_OPEN_1, ...) is done
client-side by `reaperd.py discover-map`, which prints a report and can `--save`
the result to the user map overlay. Read-only; no undo block.

---

## Transport / project

### play / stop / pause / record
`{}` — no payload.

### set_cursor
`{ "position": { "type": "bar", "bar": 17 }, "seek_play": false }`

### set_time_selection
```json
{ "start": { "type": "bar", "bar": 9 }, "end": { "type": "bar", "bar": 17 } }
```
Or `"length_bars": 8` instead of `end`. `{ "clear": true }` clears it.

### set_tempo
`{ "bpm": 174 }`

### render
Gated — requires `allow_risk_level_3: true` in `bridge_config.json`.
```json
{ "output_file": "/path/to/out.wav", "bounds": "time_selection" }
```
`bounds`: `project`, `time_selection`, `regions`, `selected_items`. Uses
REAPER's most recent render settings (format, sample rate); configure those
once in REAPER's Render dialog. Render is synchronous — it blocks the bridge
for the entire render duration, so `heartbeat.alive_at` goes stale. The
heartbeat written just before render includes `"busy": "render"` so an agent
can distinguish "rendering" from "bridge died".

REAPER's render-progress window blocks the bridge until it is dismissed unless
its "Automatically close when finished" checkbox is ticked (config var
`renderclosewhendone` bit 0). The bridge forces that bit on for the render and
restores it afterward, which needs SWS (`SNM_*`) — the bridge's only SWS use.
Without SWS it can't force it, so the reply carries `render_autoclose_warning`
and the first render will hang until the window is closed by hand unless the
user ticks that checkbox once themselves.

### capture_track_audio
Gated — requires `allow_risk_level_3: true` in `bridge_config.json`.
```json
{ "target_track_name": "Rhythm L", "duration_seconds": 30,
  "output_file": "/tmp/reaper-diagnosis/rhythm-l-20260702T143000.wav",
  "sample_rate": 48000 }
```
Renders a track capture to WAV. For a verified isolated item-less routing track,
it uses the stems render source (`RENDER_SETTINGS=2`, selected tracks,
pre-master; parent-bus and master-bus FX are not printed) with custom bounds
(`RENDER_BOUNDSFLAG=0`). Tracks with media items can fall back to a full-mix
render because offline isolation may produce silence for their FX. Optional
`start_seconds` overrides the default range (active time selection if any, else
cursor + `duration_seconds`, max 600). Use a unique/timestamped `output_file`
so REAPER never raises an overwrite prompt mid-render. Track selection and all
render settings are captured before and restored after, even on error.
Synchronous like `render` (same `busy: "render"` heartbeat). Returns
`file_path` (from `RENDER_TARGETS`, authoritative), `file_size_bytes`,
`render_loudness_lufs` (LUFS-I parsed from `RENDER_STATS`), and
`render_stats_raw`, plus capture provenance: `capture_scope` is one of
`isolated_track`, `full_mix`, or `master_output`; `isolation_verified` is true
only for `isolated_track`. Clients must use only that true/isolated combination
as per-track evidence. The client should verify the file's mtime is newer than
the command's `created_at` before trusting it. Same render-window auto-close
handling as `render` (see above); `render_autoclose_warning` is present only
when the bridge could not force auto-close.

### get_track_routing
Read-only.
```json
{ "target_track_name": "Rhythm L" }
```
Returns `sends` and `receives` (per entry: target/source track name,
`volume_db`, `pan`, `mute`, `mono`, `phase_inverted`, channel mapping),
`parent_track` (name/index/guid or null), track `volume_db`, `pan`,
`phase_inverted`, and `automation_mode`. All volumes are converted to dB in
the bridge (`D_VOL` is linear); hardware outputs are excluded.

### get_selected_track
Read-only, no payload. The Post Mortem panel's Track-screen idle card
(P3-002; replaces the pre-3.11 undocumented minimal reply — top-level
`name`/`guid` are kept for compatibility, and no-selection now returns a
result instead of erroring). With nothing selected returns
`{ "selected": false, "selected_count": 0 }`. Otherwise returns the FIRST
selected track:
`track` (index/name/guid), `selected_count`, `fx_count`, `item_count`,
`receive_count`, plus what a capture would do right now — `capture_source`
(`time_selection` when one is active, else `edit_cursor`, the same
resolution `capture_track_audio` uses), `capture_start_seconds`,
`items_at_capture_start`, and `expected_capture_scope`
(`isolated_track` | `full_mix` | `master_output`, the provenance a capture
of this track would carry). The master track is not part of REAPER's
selected-track enumeration and is never reported here.

### get_capture_preflight
Read-only. Everything that would gate or degrade a capture WITHOUT rendering
(P3-002) — powers onboarding's checklist and "Test Again".
```json
{ "target_track_name": "Kick" }
```
All track selectors are optional (`target_track_name`, `target_track_guid`,
`track_name_contains`, `use_selected_track`); with one, the reply's `target`
carries that track's summary, `item_count`, and `expected_capture_scope`.
Returns `capture_allowed` (false only for hard blockers), `blockers[]` and
`warnings[]` (each `{ code, message }` — `capture_gated` blocks;
`render_hang_risk` warns when auto-close can be neither observed on nor
forced), `risk_gate` (`allow_risk_level_3`, `requires_restart_to_change`:
the flag is read once at REAPER startup), `sws_installed`, and
`render_autoclose` (true/false, or null when unreadable without SWS).

---

## Tracks

### add_track
`{ "name": "Lead Synth", "index": 3, "color": {"r":200,"g":40,"b":40}, "select": true }`
`index` is 1-based insert position; omit to append.

### delete_track / rename_track / select_track
```json
{ "target_track_name": "Scratch" }
{ "target_track_name": "Gtr 1", "new_name": "Rhythm L" }
{ "target_track_name": "Bass", "exclusive": true }
```
`select_track`: `exclusive: false` adds to selection; `select: false` deselects.

### set_track_volume / set_track_pan / mute_track / solo_track / arm_track
```json
{ "target_track_name": "Drums", "volume_db": -3.0 }
{ "target_track_name": "Drums", "pan": -0.25 }
{ "target_track_name": "Drums", "mute": true }
{ "target_track_name": "Drums", "solo": true }
{ "target_track_name": "Vox", "armed": true }
```
`set_track_volume` also accepts `volume` (linear). `mute`/`solo`/`armed`
default to `true` when omitted.

### set_track_color
`{ "target_track_name": "Drums", "color": {"r":180,"g":20,"b":20} }` — `color`
may also be a native REAPER color integer.

### snapshot_track_state
```json
{ "target_track_guid": "{...}",
  "parameters": [ { "fx_guid": "{...}", "parameter_index": 17 } ] }
```
Writes a crash-safe state file (`state/snapshots/<snapshot_id>.json`) BEFORE
any mutation, covering exactly what the preview operations can change: track
volume (raw `D_VOL` plus informational dB), pan, every FX's enabled state with
its GUID/index/scope/name identity, and each named parameter's normalized
value. `parameters[]` entries take `fx_guid` or `fx_index`+`fx_scope`; an
entry that does not resolve fails the whole snapshot closed (`NO_FX`,
`NO_FX_PARAM`) — nothing has been mutated yet, so refusing is free. Read-only
with respect to the project (no undo block). Returns `snapshot_id`, `path`,
and the full snapshot.

### restore_track_state
```json
{ "snapshot_id": "snap-20260711T235959Z-a1b2c3", "delete_after": true }
```
Resolves the snapshot's track by GUID only — a deleted track fails closed with
`NO_TARGET_TRACK` and nothing is written. FX are matched by GUID against the
live chain; writes target the live API index, so a moved FX still restores
correctly. State that no longer resolves is skipped and reported in
`unrestored[]` with a typed reason (`FX_NOT_FOUND`) while everything else
restores. Restore never touches state the snapshot does not contain.
`delete_after` removes the state file only when `fully_restored` is true.
Returns `restored[]`, `unrestored[]`, `fully_restored`, `deleted`.

### preview_change
```json
{ "operation": "set_fx_param",
  "target": { "track_guid": "{...}", "track_name": "Kick",
              "fx_guid": "{...}", "fx_index": 2, "fx_scope": "track",
              "fx_name": "VST3: Pro-Q 4",
              "parameter_index": 17, "parameter_name": "Band 3 Gain" },
  "proposed_value": 0.42 }
```
Applies ONE temporary change after snapshotting (P2-001) and persisting the
preview state to `state/preview.json`. Operations: `set_track_volume`
(`proposed_value` in dB), `set_track_pan` (-1..1), `set_fx_param` (normalized
0..1), `set_fx_bypass` (boolean, true = bypassed). Every identity field the
caller supplies (`track_name`, `fx_index`, `fx_scope`, `fx_name`,
`parameter_name`) is re-verified against the live project; any mismatch
refuses with `STALE_IDENTITY` and mutates nothing. Only one preview may be
active (`PREVIEW_ACTIVE`); an expired one (30 min) is restored first. Creates
NO undo point. Returns `preview_token`, `snapshot_id`, `applied`
(before/after), `expires_at`.

### cancel_preview
`{ "preview_token": "pv-..." }` — restores the full snapshot and deletes the
preview state. Token required and must match (`BAD_PREVIEW_TOKEN`,
`NO_ACTIVE_PREVIEW`). Creates no undo point.

### commit_preview
`{ "preview_token": "pv-..." }` — re-verifies identities (an FX-chain edit
mid-preview refuses, restores baseline, and errors `STALE_IDENTITY`), restores
the baseline value, then re-applies the proposed value inside EXACTLY ONE
named undo block ("Post Mortem: <operation> on <track>"). Undoing that single
point returns the user to their pre-preview state. Expired previews restore
and refuse (`PREVIEW_EXPIRED`). Deletes the snapshot and preview state.

Crash recovery: a leftover `state/preview.json` (crash, killed bridge, REAPER
restart) is restored with cancel semantics at bridge startup, logged, and
surfaced in the heartbeat as `preview_recovered_at`; an active preview's token
and expiry ride the heartbeat as `active_preview_token` / `preview_expires_at`.

---

## FX

### add_fx
`{ "target_track_name": "Gtr DI", "fx_name": "ReaEQ (Cockos)", "fx_scope": "track", "show": false }`
`fx_name` must match the plugin as REAPER lists it.

### remove_fx / bypass_fx / move_fx
```json
{ "target_track_name": "Gtr DI", "fx_name_contains": "ReaEQ" }
{ "target_track_name": "Gtr DI", "fx_name_contains": "ReaEQ", "bypass": true }
{ "target_track_name": "Gtr DI", "fx_name_contains": "ReaEQ", "to_index": 0 }
```

### set_fx_param
```json
{ "target_track_name": "Gtr DI", "fx_name_contains": "ReaEQ",
  "param_name_contains": "Gain", "normalized_value": 0.65 }
```
Instead of `normalized_value` (0.0–1.0): `relative` (`"+0.1"`) or
`formatted_value` (`"65 %"`, `"80 Hz"`, `"-16.00 dB"`). The bridge binary-searches
the normalized value whose formatted display matches the target number, so it
works on plugins that hide their real range (FabFilter, most VST3) as well as
those that expose it. Numeric display values only — enum/string params like
"Bell", "Punch", or "Off" are rejected with `FORMATTED_VALUE_UNSUPPORTED` (use
`normalized_value` for those: scan to find the value that formats right). When
precision matters, scan with `get_fx_parameters` and send `normalized_value`
directly.

### write_fx_param_automation
```json
{ "target_track_name": "Lead", "fx_name_contains": "Filter",
  "param_name_contains": "Cutoff", "clear_existing_in_range": true,
  "points": [
    { "bar": 33, "beat": 1, "value": 0.0, "shape": "linear" },
    { "bar": 37, "beat": 1, "value": 1.0, "shape": "linear" }
  ] }
```
Point time: `time`, `seconds`, or `bar` (+ optional `beat`). Values normalized
0.0–1.0. `shape`: `linear`, `square`, `slow`, `fast`, `bezier`.

---

## Markers / regions / items

### add_marker / add_region / delete_marker
```json
{ "position": { "type": "bar", "bar": 33 }, "name": "Chorus", "color": {"r":40,"g":120,"b":220} }
{ "start": { "type": "bar", "bar": 33 }, "length_bars": 8, "name": "Chorus" }
{ "name": "Chorus" }
```
`delete_marker` also takes `marker_index` and `is_region: true`.

### delete_items_in_range
```json
{ "target_track_name": "Drums", "range": { "type": "time_selection" } }
```
Or a `range` position plus `length_bars` / `length_seconds`. `all_tracks: true`
deletes across every track.

---

## MIDI

### insert_midi_file
```json
{ "midi_path": "/path/to/groove.mid", "target_track_name": "Drums",
  "position": { "type": "cursor" }, "length": { "type": "bars", "bars": 4 },
  "loop": true, "replace_existing_in_range": false }
```
`length.type`: `bars`, `region`, `time_selection`, `seconds`, `as_generated`.

> Note: depending on REAPER's "Import MIDI as" preference, inserting a `.mid`
> can pop a modal import dialog that blocks the bridge until dismissed. For
> unattended use, set that preference to "in-project MIDI" once, or pre-set the
> project's MIDI import mode.

---

## Composition

### batch
```json
{ "stop_on_error": true, "undo_label": "Agent: setup",
  "commands": [
    { "type": "add_track", "payload": { "name": "Lead" } },
    { "type": "add_fx", "payload": { "target_track_name": "Lead", "fx_name": "ReaSynth (Cockos)" } }
  ] }
```
The whole batch is one undo block.
