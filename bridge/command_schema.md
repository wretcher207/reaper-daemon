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
the project. It is honored in the envelope OR inside `payload` (`reaperd.py
cmd` can only reach the payload, and a dry_run that silently executes is the
worst available inversion). Read-only commands ignore `dry_run` and execute
normally — except `reload_bridge`, which honors it despite never touching the
project.

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

**Parameter** — `param_index` (0-based) or `param_name_contains`. **Prefer
`param_index`**, taken from a `get_fx_parameters` scan. Plugin parameter names
collide constantly (FabFilter Pro-C 3 has "Threshold", "Auto Threshold" and
"Lock Auto Threshold"), and a substring matching more than one throws
`AMBIGUOUS_PARAM`. Reserve `param_name_contains` for names the scan confirmed
are unique.

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
Also `sample_rate` and `sample_rate_overridden`. `sample_rate` is the rate
stored in the project and reads back whether or not it is in force; when
`sample_rate_overridden` is false, REAPER runs at the audio device's rate
instead, so the number alone does not tell you what is playing. Check the flag
before treating the rate as the session's.

`media_offline_when_inactive` reports REAPER's "set media items offline when
application is not active" preference (`offlineinact`). **When it is true,
every bridge read sees unloaded media**, because every command arrives from
another process (CLI, MCP server, console sidecar) while REAPER is in the
background. Sources then report `source_state: "offline"` and a capture may
measure unloaded media. `nil` means the preference could not be read, which is
NOT the same as off. It is read live, not from `reaper.ini` — REAPER only
writes that file on exit, so the on-disk copy is stale while REAPER runs.

Each FX entry carries `offline` and `bypassed` alongside `name`, `index`,
`api_index`, and `scope` (added 2026-09-01, from `TrackFX_GetOffline` and
`TrackFX_GetEnabled` — both authoritative reads, unlike item offline state).
An offline FX is loaded but not processing: it renders silence and reports no
parameters, which used to look identical to a working plugin from here.

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
once in REAPER's Render dialog. `output_file` is split into `RENDER_FILE` (the
directory) and `RENDER_PATTERN` (the filename, minus a trailing `.wav` — the
extension comes from the sink format), the same split `capture_track_audio`
uses. The reply's `target` is read back from `RENDER_TARGETS`, so it is what
REAPER says it will write; `output_file` in the reply is the raw `RENDER_FILE`
directory. Render is synchronous — it blocks the bridge
for the entire render duration, so `heartbeat.alive_at` goes stale. The
heartbeat written just before render includes `"busy": "render"` so an agent
can distinguish "rendering" from "bridge died".

Two REAPER render preferences can block the bridge behind modal UI:
"Automatically close when finished" (`renderclosewhendone` bit 0) and, on
REAPER 7.75+, "Save render statistics" (bit 21). The bridge temporarily forces
both bits for the render, verifies the write, then restores and verifies the
user's exact prior value. This needs SWS (`SNM_*`), the bridge's only SWS use.
If SWS is missing, the setting cannot be read, or either write fails, the
bridge refuses before opening the render window with
`RENDER_PREFERENCES_UNSAFE` or `RENDER_PREFERENCES_RESTORE_FAILED`. Install
SWS, or enable both preferences manually, then rerun capture preflight.

### get_render_settings
Read-only. No payload.
```json
{}
```
Returns the project's live render configuration: `settings` (source: 0 master
mix, 2 stems of selected tracks — the same values `RENDER_SETTINGS` takes),
`bounds_flag`, `start_pos`, `end_pos`, `sample_rate`, `channels`, `file`,
`pattern`, and `targets` (the files REAPER says it would write). Exists because
`render` and `capture_track_audio` write these values and nothing could read
them back — the render-path clobber and the 2026-08-18 silent-render diagnosis
both stalled on exactly this gap.

### set_render_settings
```json
{ "settings": 0, "file": "C:/renders", "pattern": "mix", "bounds_flag": 2 }
```
Writes any subset of the keys `get_render_settings` reads (`targets` is
read-only). Every written key is read straight back from REAPER into the
reply's `applied` — check it, don't trust the request. Use it to restore a
project's render path after `render` clobbers it. Render settings live outside
the undo history: no undo point, and Ctrl+Z will not revert this.

### get_items
Read-only. Track selectors as usual.
```json
{ "target_track_name": "Kick - stem" }
```
Item inventory for one track: per item `position`, `length`, `muted`,
`volume`, `selected`, active take `take_name`, `source_type`, `source_file`,
`source_length`, and `source_readable` — an `io.open` check made from INSIDE
REAPER's process, which tests the file access REAPER actually has (an outside
shell's check can pass while REAPER's fails, or vice versa).

Also `source_channels`, `source_sample_rate`, `source_chunk_line` (just the
`SOURCE` header line from the item chunk), and `take_fx` — every take FX with
`offline` and `bypassed`, both read from `TakeFX_GetOffline`/`GetEnabled`.

**`source_state` is INFERRED, not read.** REAPER exposes no ReaScript call for
media-item offline state; the only `*_GetOffline` functions are `TrackFX_` and
`TakeFX_`, which are about plugins. `source_state_inferred: true` ships beside
it so nothing mistakes it for an API read. Values:

| `source_state` | Means | Signal |
| --- | --- | --- |
| `loaded` | REAPER has the audio | sample rate or length is non-zero |
| `offline` | REAPER is not loading media that is right there | rate 0 AND length 0, file readable |
| `unresolved` | Missing or unreachable media | rate 0 AND length 0, file NOT readable |
| `midi` | In-project MIDI, which has no sample rate by nature | `source_type` is MIDI/MIDIPOOL |

`offline` and `unresolved` are deliberately separate: identical numbers, very
different fixes. Measured live on drones.rpp 2026-09-01 — offline items report
length 0 and rate 0 while the same file reads 48000 Hz / 27.73 s off disk.

**Reading `offline` usually says more about focus than about the project.**
With `media_offline_when_inactive` on, items go offline the moment REAPER loses
focus and come back when it regains it — and the bridge always reads from the
background, so it always catches them offline. Check
`get_context.media_offline_when_inactive` before treating `source_state:
"offline"` as a property of the media rather than of the preference.

### set_media_offline_when_inactive
Gated — requires `allow_risk_level_3: true`, or the reply is
`PREFERENCE_BLOCKED`. Needs SWS (`SNM_SetIntConfigVar`); REAPER exposes no
native config-var setter, so without it the reply is `SWS_REQUIRED`.
```json
{ "enabled": false }
```
Turns REAPER's "set media items offline when application is not active"
preference (`offlineinact`) on or off. `enabled` is required and must be a real
boolean — a missing or string value is `BAD_PAYLOAD`, never an assumption.

This is deliberately NOT a general config-var setter. One named preference with
a known hazard is the whole requirement; an arbitrary "write any REAPER
preference" command is a footgun with no upside.

The value is treated as a whole, not a bitfield: off writes 0, and the reply
carries `raw_before` so the previous value can be restored exactly via
`{ "enabled": true, "raw": <raw_before> }`. REAPER's encoding for this key is
not documented in the ReaScript surface, and an earlier cut that assumed bit 0
was the enable made the writer disagree with the reader.

The write is always read back. If SWS silently no-ops, the reply is
`PREFERENCE_NOT_APPLIED` rather than a false success.

Not wrapped in an Undo block: it changes an application preference, not project
state, so an undo entry would be a spurious history item that undoes nothing.
It honors `dry_run` in its own handler instead, and reports `before` alongside
`would_set`.

**Preferences persist to `reaper.ini` on exit**, so a change survives a clean
quit and is lost by a crash before one.

### save_project
Gated — requires `allow_risk_level_3: true` in `bridge_config.json`, or the
reply is `SAVE_BLOCKED`. No payload.
```json
{}
```
Saves the open project over its existing `.rpp` (no Save As prompt). Gated with
render and capture rather than on its own, because overwriting the file on disk
is the one mutation an undo block cannot take back. Returns `saved` and
`project_name`.

Returns `project_path` too — the file that was actually written, so the reply
can be checked against the disk rather than believed.

A project with no file has nothing to overwrite, so REAPER would open a Save As
dialog — a modal that blocks the bridge's defer loop until a human dismisses it,
the same failure mode the render preferences exist to prevent. The bridge
refuses that case with `SAVE_UNSAFE` before calling REAPER at all. Save the
project once by hand and it works from then on. (`get_context`'s `project_name`
reads `Untitled` in the same situation.)

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
`volume_db`, `pan`, `mode` — the same names `create_send` takes, or the raw
integer if REAPER ever reports one that is not named — `mute`, `mono`,
`phase_inverted`, channel mapping),
`parent_track` (name/index/guid or null), track `volume_db`, `pan`,
`master_send`, `phase_inverted`, and `automation_mode`. All volumes are
converted to dB in the bridge (`D_VOL` is linear); hardware outputs are
excluded. This is the read side of `create_send` and `set_master_send`: what
those two write is what `sends` and `master_send` report back.

### get_selected_track
Read-only, no payload. The Post Mortem panel's Track-screen idle card
(P3-002; replaces the pre-3.11 undocumented minimal reply — no-selection
returns a result instead of erroring). With nothing selected returns
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

### create_send
```json
{ "target_track_name": "Snare",
  "destination": { "target_track_name": "Drum Buss" },
  "volume_db": -6.0, "mode": "post_fader" }
```
Creates a track send from the source to the destination. `destination` is an
object taking the same shared selectors as the source, so
both ends resolve by GUID, exact name, substring, or selection under identical
rules; it is required, and `BAD_PAYLOAD` if it is missing or resolves to the
source track itself. `volume_db` and `mode` are both optional; a send REAPER
creates starts at unity, post-fader. Both are validated before the send is
created, so a refused command leaves no send behind.

`mode` is one of `post_fader` (REAPER's 0), `pre_fx` (1), or `pre_fader` (3) —
named because REAPER's integers are not contiguous. Anything else is
`BAD_PAYLOAD` rather than a silent post-fader send. Returns `source` and
`destination` track summaries plus `send_index`, which is the index
`get_track_routing` lists the new send at.

Sends are the whole of the bridge's routing writes. Folder routing would need
tracks reordered and folder depths rewritten, which the API makes fragile
enough to lose an arrangement; a send is safe and `Ctrl+Z`-reversible.

### set_master_send
`{ "target_track_name": "Snare", "enabled": false }` — turns the track's direct
feed to the master bus on or off (`B_MAINSEND`). `enabled` must be a boolean;
anything else is `BAD_PAYLOAD`, since a defaulted value here silently changes
what the mix sums. A source feeding a bus usually stops feeding the master
directly, and this is that switch. Read it back as `master_send` from
`get_track_routing`.

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

### add_fx_chain
```json
{ "target_track_name": "Gtr Bus", "chain_name": "my-guitar-chain" }
{ "target_track_guid": "...", "chain_path": "C:/chains/bus.RfxChain" }
```
Splices a saved `.RfxChain` file onto a track's FX chain. `chain_name` resolves
against REAPER's `FXChains` resource folder (extension optional); `chain_path` is
an explicit file. Read-verify: the handler snapshots the FX count, splices the
chunk, and restores the original chain on any mismatch, so a parse bug can
ever leave a half-merged track. Result carries `chain.fx_in_chain`,
`fx_count_after`, and the added FX names.

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
those that expose it. Two things it handles that trip a naive search: a display
that changes unit partway up its range (Pro-C 3's Release goes `944.9 ms` to
`1.151 sec`; time is compared in ms, frequency in Hz), and a display that goes
non-numeric at one end (Pro-Q 4's band Threshold reads `Auto` at 1.0; the search
narrows to the widest numeric bracket instead of refusing the parameter). Every
probe is a real write, and the original value is restored on any failure. Numeric display values only — enum/string params like
"Bell", "Punch", or "Off" are rejected with `FORMATTED_VALUE_UNSUPPORTED` (use
`normalized_value` for those: scan to find the value that formats right). When
precision matters, scan with `get_fx_parameters` and send `normalized_value`
directly.

### write_fx_param_automation
```json
{ "target_track_guid": "{TRACK-GUID}", "fx_guid": "{FX-GUID}",
  "param_index": 13,
  "ranges": [ { "start_time": 10.0, "end_time": 20.0 } ],
  "points": [
    { "time": 10.0, "value": 1.0, "shape": "square" },
    { "time": 15.0, "value": 0.5, "shape": "square" },
    { "time": 20.0, "value": 0.2897, "shape": "linear" }
  ] }
```
Point time: `time`, `seconds`, or `bar` (+ optional `beat`). Values normalized
0.0–1.0 and refused (not clamped) outside it. `shape`: `linear`, `square`,
`slow`, `fast`, `bezier`. `tension` and `selected` are optional.

The write is transactional. Before any mutation the bridge snapshots the
envelope (existence, all points, active/visible/arm/lane state, track
automation mode). A declared replacement scope — `ranges` (an array of
inclusive `{start_time, end_time}`), a single `range`/`position`, or the
legacy `clear_existing_in_range: true` inference from the point extent —
means replacement: existing points inside the scope are deleted (inclusive
ends, same 1e-7 tolerance as the read command) and the submitted points are
inserted. Every submitted point must lie inside a declared range
(`POINT_OUTSIDE_RANGE`). With no declared scope and no clear flag the write
appends without deleting. A submitted point that exactly reproduces an
existing in-scope point is skipped, not deleted and rewritten, so re-applying
the same write is idempotent. Duplicate times inside one request are refused
(`DUPLICATE_POINT_TIME`), as is a write whose scope intersects an automation
item (`AUTOMATION_ITEM_UNSUPPORTED` — automation items are read-only for now).

After writing, the bridge rereads the envelope and proves it: every submitted
point must reread back exactly, and the whole envelope must equal the intended
final state (kept preimage plus new points), which simultaneously proves
points outside the scope are untouched. Any failure — insert rejected, reread
mismatch, outside point changed — restores the complete preimage and rereads
the restoration before reporting, so the error is
`AUTOMATION_ROLLED_BACK: <cause>; restored and reread N envelope(s)` (or
`ROLLBACK_UNCONFIRMED` if the restore itself could not be proven; one REAPER
undo reverts the whole thing). An envelope the transaction created is restored
to empty — ReaScript has neither an envelope-delete call nor an envelope-state
setter in this build — and the error says so; one REAPER undo removes it
outright. When the transaction creates an envelope, REAPER seeds it with a
time-0 point at the parameter's current value; that point is kept (the
parameter holds its current value until the first written point) and counts
in the verified final state.

The reply carries the legacy `inserted_count`, `points`, and `cleared_range`
fields, plus the envelope state, track automation mode, `final_hash` (the
same canonical hash the read command returns), and
`verification: { requested, replaced, inserted, skipped, confirmed, refused }`
— `refused` is nonzero only in a refused reply, which never mutates. The
write touches points only: this REAPER build exports no
`SetEnvelopeInfo_Value`, so envelope visibility/arm cannot be set (or
changed) from the bridge — the reply reports that state as-is, and the
single-write command still calls `SetCursorContext` so REAPER displays the
envelope it just wrote. The command sits inside one named undo block
(`undo_label` on the command envelope).

### apply_automation_transaction
```json
{ "writes": [
    { "target_track_guid": "{L}", "fx_guid": "{FX-L}", "param_index": 13,
      "ranges": [ { "start_time": 10.0, "end_time": 20.0 } ],
      "points": [ { "time": 10.0, "value": 1.0 } ] },
    { "target_track_guid": "{R}", "fx_guid": "{FX-R}", "param_index": 13,
      "ranges": [ { "start_time": 10.0, "end_time": 20.0 } ],
      "points": [ { "time": 10.0, "value": 1.0 } ] }
  ],
  "transaction_id": "optional-caller-id" }
```
Atomically applies several envelope writes — the stereo-pair case — in ONE
undo block. Every write is validated and snapshotted before anything mutates;
two writes targeting the same envelope are a `BAD_PAYLOAD` (merge their
points). If any write fails or fails verification, every mutated envelope is
restored to its preimage and reread before the command reports
`AUTOMATION_ROLLED_BACK`, so a failed right channel leaves the left channel
unchanged. The reply carries `transaction_id` (echoed from the payload or the
command id), `rolled_back: false`, `touched_envelopes` (exact track/FX/
parameter identities), and per-write summaries with the same `verification`
counts and `final_hash` as `write_fx_param_automation`. Same per-write
semantics (inclusive replacement, idempotent skips, automation-item refusal)
as that command.

### get_fx_param_automation

Read one existing FX-parameter envelope without creating, showing, or arming
it. Prefer GUID selectors for the track and FX when using the raw bridge.

```json
{ "target_track_guid": "{TRACK-GUID}", "fx_guid": "{FX-GUID}",
  "param_index": 13, "start_time": 10.0, "end_time": 20.0,
  "include_neighbors": true }
```

`start_time` and `end_time` are optional as a pair. Their boundaries are both
inclusive, with a 1e-7-second equality tolerance for nanosecond differences
introduced when the same bar position is converted separately for a marker and
an envelope point. The result reports resolved track/FX/parameter identity, track
automation mode, non-mutating envelope state, base and automation-item points,
nearest outside neighbors when requested, exact duplicate-time groups, and a
canonical hash. Canonical time is quantized to 1/960 quarter note and value to
1e-6; tuple ordering makes the hash stable if REAPER reorders equal-time points.
An absent envelope is a successful read with `envelope.exists: false`, zero
points, and the canonical empty hash.

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

### get_midi_notes

Read the notes in a take. Read-only.

```json
{ "target_track_name": "Monarch", "item_index": 0,
  "pitches": [24, 26], "max_notes": 4000,
  "include_bars": true, "include_note_names": true }
```

`item_index` is optional when the track holds exactly one item; a track with
more than one and no `item_index` is `AMBIGUOUS_ITEM` rather than a guess.

Returns `notes` (`index`, `ppq`, `end_ppq`, `pitch`, `velocity`, `channel`,
`muted`, `selected`, and `bar` unless `include_bars` is false),
`ppq_per_quarter`, `note_count`, `returned`, `truncated`, a per-pitch
`velocity_summary`, and `note_names` from the kit's `.midnam` when it has one.

`note_count` is the take's total and `returned` is how many came back, so a
`max_notes` cut is visible rather than looking like a short take.

### set_note_velocities

Set velocity on specific existing notes. One undo block.

```json
{ "target_track_name": "Monarch",
  "notes": [[0, 24, 112], [480, 24, 110], [960, 54, 123]],
  "allow_partial": false }
```

Each row is `[start_ppq, pitch, velocity]`, or the object form
`{"ppq": 0, "pitch": 24, "velocity": 112}`. Velocity is 1-127; 0 is a note-off,
not a quiet note, and is refused.

**Notes are addressed by position and pitch, never by take index.** An index
shifts as soon as anything is inserted or deleted, so an index-addressed edit
built from an older read can land on the wrong note and still report success.
A `(ppq, pitch)` pair either names the same note it named at read time or names
nothing, which fails visibly.

The default is all-or-nothing: if any row is missing, ambiguous, or out of
range, the command changes nothing and fails `NOTE_MATCH_FAILED` with the count
in each bucket. A velocity pass is one musical gesture, and half of one applied
is worse than none, because by ear the half that landed is indistinguishable
from the half that did not. `allow_partial: true` applies what resolved and
reports the rest in `missing` / `ambiguous` / `invalid`.

Two notes on the same pitch at the same tick are reported as ambiguous rather
than picked between, as are two rows aiming at one note.

After writing, the handler re-reads the take and confirms every applied note by
`(ppq, pitch)`; a mismatch fails `VELOCITY_WRITE_UNCONFIRMED` rather than
reporting an unchecked success. Results carry `applied`, `confirmed`, and
`velocity_before` / `velocity_after` per-pitch summaries.

---

### set_note_positions

Move specific existing notes in time. One undo block. This is the write path a
timing / humanize pass needs; `set_note_velocities` deliberately passes every
position argument as nil and cannot move anything.

```json
{ "target_track_name": "Kontakt 8",
  "notes": [[0, 24, 6, 126], [480, 24, 472, 592]],
  "allow_partial": false }
```

Each row is `[start_ppq, pitch, new_ppq, new_end_ppq]`, or the object form
`{"ppq": 0, "pitch": 24, "new_ppq": 6, "new_end_ppq": 126}`. The first two
fields address the note as it is now; the last two say where it goes.
`new_ppq` must be >= 0 and `new_end_ppq` must be greater than it.

Notes are addressed by position and pitch, for the same reason as
`set_note_velocities`: an index shifts as soon as anything is inserted or
deleted, so an index-addressed edit built from an older read can land on the
wrong note and still report success.

**Destination collisions are refused.** This is the hazard a velocity write does
not have: moving notes rewrites the very key notes are addressed by, so two hits
nudged onto the same tick and pitch would leave a take that no later read can
address unambiguously. Both cases are caught — two moved notes converging, and a
moved note landing on a note the request left in place — and reported in
`collisions`. Two different pitches on one tick is a chord, not a collision, and
is allowed. Two notes trading places is allowed, because neither destination is
occupied once both moves are taken together.

The default is all-or-nothing across `missing` / `ambiguous` / `invalid` /
`collisions`, on the same reasoning as the velocity pass: half a timing pass
applied is a part that drifts in and out of feel, which is harder to diagnose by
ear than no change at all. `allow_partial: true` applies what resolved.

Writes use `noSort` and sort once at the end, because `MIDI_Sort` renumbers
notes and sorting inside the loop would invalidate the indices the plan holds.

After writing, the handler re-reads the take and confirms every applied note at
its NEW `(ppq, pitch)`, checking the end position too; a mismatch fails
`POSITION_WRITE_UNCONFIRMED`. Results carry `applied`, `confirmed`,
`collisions`, and `max_move_ticks` / `mean_move_ticks` so a caller can sanity
check the size of the move against the tempo without re-reading the take.

---

### apply_note_edits

Bulk velocity and/or position edit addressed by **take note index**, in one undo
block. This is the write path a whole-take pass (e.g. humanize) uses.

```json
{ "target_track_name": "Monarch-kit", "note_count": 801,
  "edits": [ {"index": 0, "velocity": 108},
             {"index": 1, "velocity": 121, "new_ppq": 6, "new_end_ppq": 126} ],
  "allow_partial": false }
```

Each edit carries an `index` (from a `get_midi_notes` read) plus any of
`velocity`, and `new_ppq` + `new_end_ppq` (which must be given together). A
velocity-only edit keeps the note's position; a position-only edit keeps its
velocity.

**Why index, not `(ppq, pitch)`.** The `set_note_velocities` / `set_note_positions`
path addresses by `(ppq, pitch)` so an edit built from a stale read fails visibly
instead of hitting the wrong note — the right default for hand-built edits. But
`(ppq, pitch)` **cannot name one of two notes stacked on the same tick and
pitch** (a flam, a double-trigger); those come back `ambiguous` and are skipped.
A whole-take pass reads the take and writes it straight back with nothing editing
in between, so index addressing is both safe here and able to touch every note.
The optional `note_count` is a staleness guard: if the take no longer holds that
many notes, the write is refused (`TAKE_CHANGED`) rather than applied against a
take that moved.

Default is all-or-nothing across invalid edits (bad index, velocity outside
1-127, a half-given position, `new_end_ppq <= new_ppq`); `allow_partial: true`
applies what resolved and reports the rest in `invalid`.

**Confirmation matches velocity and note START exactly; the note END is
advisory.** On a one-shot drum sampler the sound is triggered by note-on, so the
note-off length is inaudible, and REAPER's overlap correction routinely trims a
shifted note's end by a few ticks. Failing the pass on that would reject a
musically perfect edit (it did, on the first Monarch timing pass), so end drift
is counted in `end_advisory_drift`, not treated as failure. A real velocity or
start mismatch still fails `NOTE_EDIT_UNCONFIRMED`. Writes use `noSort` and the
confirm read happens before the single closing `MIDI_Sort`, so the plan's indices
stay valid through confirmation. Results carry `applied`, `confirmed`, `invalid`,
`end_advisory_drift`, and `velocity_before` / `velocity_after` summaries.

The `reaperd humanize` CLI is the intended caller: it reads the take, plans the
pass with `drumgen.humanize` (the shared taste model), and ships the edits here.

---

## Bridge lifecycle

### reload_bridge
No payload.
```json
{}
```
Replaces the running bridge with a fresh instance loaded from
`bridge/reaper_agent_bridge.lua` on disk — no REAPER restart. The reply is
written by the OLD instance before the handover, so `reload: "scheduled"`
means it has not happened yet. Confirm it by reading `bridge/heartbeat.json`:
its `loaded_at` stamp changes when the fresh instance is up, and the reply
carries the old `loaded_at` to compare against. Config
(`bridge_config.json`) is re-read by the fresh instance, so this also applies
config changes.

Refusals: `RELOAD_COMPILE_FAILED` when the file on disk does not compile —
the running bridge is left untouched, so a typo costs a reply, not the
bridge — and `RELOAD_BLOCKED` while a preview is active, because the fresh
instance's startup recovery would silently cancel it (`commit_preview` or
`cancel_preview` first).

If the file compiles but throws while starting, the old instance re-claims
its lock and keeps running; the failure is logged in `logs/bridge.log`. The
handover happens between commands, never mid-command, and anything still in
`inbox/` (including later commands of the same drain) is picked up by the
fresh instance.

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
