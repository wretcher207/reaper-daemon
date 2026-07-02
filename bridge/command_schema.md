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

### scan_fx
Every FX and its parameters across the project. Omit the track selector to scan
all tracks.
```json
{ "include_values": false, "max_params": 500 }
```
`include_values: true` adds current value / formatted value per parameter (much
larger). With `include_values: false` you get parameter names and indices only.

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

### capture_track_audio
Gated — requires `allow_risk_level_3: true` in `bridge_config.json`.
```json
{ "target_track_name": "Rhythm L", "duration_seconds": 30,
  "output_file": "/tmp/reaper-diagnosis/rhythm-l-20260702T143000.wav",
  "sample_rate": 48000 }
```
Renders ONE track's post-FX output to a WAV via the stems render source
(`RENDER_SETTINGS=2`, selected tracks, pre-master — parent-bus and master-bus
FX are NOT printed) with custom bounds (`RENDER_BOUNDSFLAG=0`); the user's
solo states and time selection are never touched. Optional `start_seconds`
overrides the default range (active time selection if any, else cursor +
`duration_seconds`, max 600). Use a unique/timestamped `output_file` so
REAPER never raises an overwrite prompt mid-render. Track selection and all
render settings are captured before and restored after, even on error.
Synchronous like `render` (same `busy: "render"` heartbeat). Returns
`file_path` (from `RENDER_TARGETS`, authoritative), `file_size_bytes`,
`render_loudness_lufs` (LUFS-I parsed from `RENDER_STATS`), and
`render_stats_raw`. The client should verify the file's mtime is newer than
the command's `created_at` before trusting it.

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
