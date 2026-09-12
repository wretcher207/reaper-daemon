# Keyboard performance workflow

The daemon can discover local instruments, configure MIDI input, insert controller tests, link CCs to FX parameters, and save a playable setup. Use a separate audition project when adding test MIDI. Capture and rebuild an existing setup with `mix_recipe` when you need a working copy.

## Commands

These bridge commands work through `python reaperd.py cmd COMMAND JSON` and the matching MCP tools. Python callers can use `reaperd.send_type(COMMAND, payload)` to avoid shell quoting.

| Command | Payload beyond the track selector | Result |
| --- | --- | --- |
| `get_midi_inputs` | None | Available named inputs; indices above 63 are reported but this input setter doesn't support them |
| `configure_midi_input` | `device` (63 all, 62 virtual keyboard), `channel` (0 all, 1..16), optional boolean `arm`, `monitor` | Input, arm and monitor readback |
| `insert_midi_events` | `start_seconds`, `length_seconds`, `events` | New item; verified note and control counts |
| `link_fx_midi_cc` | FX selector, scanned `param_index`, `controller`, optional `channel` (1..16), `scale`, `offset` (-1..1) | Verified native MIDI parameter link; bus 0 |
| `get_fx_preset` | FX selector | Current host preset name, index and count |
| `set_fx_preset` | FX selector, exact `name` | Loaded host preset name, verified by readback |
| `save_project_as` | Absolute new `path`; optional `template: true`, `track_guids` | New `.rpp` filename or media-free `.RTrackTemplate` |

Track selectors use `target_track_guid` or `target_track_name` on the bridge. MCP also accepts `track` for the exact name. FX selectors use `fx_guid`, `fx_name_contains`, or `fx_index` with explicit `fx_scope`. Writes support `dry_run`; generic dry runs describe the operation without validating every payload field.

`save_project_as` requires the existing `allow_project_save` gate. Existing filenames are refused. A project save changes the current project's filename and references its media; it doesn't copy media into a portable package. A template includes only explicitly targeted tracks, excludes media and envelopes, and restores track selection. Supply `track_guids` for a whole layered performance, or one track selector for a single instrument. Files written to disk aren't reverted by REAPER undo.

## MIDI events

Event `time` and note `duration` are seconds relative to the item start. Channels are 0..15 for events, unlike the input selector. Supported event objects:

```json
[
  {"type":"cc","time":0,"controller":11,"value":127},
  {"type":"note","time":0.1,"duration":1,"pitch":69,"velocity":90,"channel":0},
  {"type":"pitch_bend","time":0.5,"value":8192},
  {"type":"program_change","time":1.5,"value":0}
]
```

Pitch bend uses 0..16383, with 8192 at center. Its sounding range depends on the instrument. The command validates all events before creating an item, sorts events, and checks counts. If insertion fails, it removes only the item it just created. For drum notes, apply and verify `drum-apparatus` goldenrule before sending.

## Discovery, presets and audition

`instrument_inventory` is an MCP tool and a standalone CLI:

```powershell
python instrument_inventory.py --query flute
python instrument_inventory.py --root C:\MyPresets --limit 100
```

Discovery reads REAPER's plugin caches and explicit content folders. On this Windows setup it also recognizes the usual Serum 2 preset folder under Documents. Cache entries don't prove loading, licensing or available samples. `.SerumPreset`, `.nki`, `.sfz` and `.fxp` files are reported as plugin-specific. The host preset setter doesn't import them. Load those through the plugin when necessary, then use `save_fx_chain` and `add_fx_chain` to retain and restore the configured sound.

The MCP tool `insert_performance_audition` takes `target_track_guid`, optional `start_seconds` and `pitch`. It inserts a deterministic 15-second melodic test. Python/CLI workflows can obtain the same payload from `performance_audition.payload()` and send it to `insert_midi_events`. Capture that range with `capture_track_audio` and use the existing measurement tools. The sequence tests repeated notes at CC11=127 and 40, modulation, sustain release, and notes an octave apart. It resets its controllers to stated defaults, not to unknown earlier controller values.

Splits, transposition, scene changes and articulation behavior remain part of the instrument or MIDI FX configuration. For the My Heart prototype they're implemented in its custom JSFX and retained in the template. Native CC links update the chosen parameter's link and baseline while retaining other modulation sources, so inspect the target first.

An event count or parameter readback doesn't prove that a controller affects sound. Compare the same rendered range, reject silent captures, retain the capture caveats, and audition the result against the reference. Physical keyboard feel and reference fidelity still need a listening and playing check.

The native save options follow [REAPER's ReaScript API](https://www.reaper.fm/sdk/reascript/reascripthelp.html#Main_SaveProjectEx).
