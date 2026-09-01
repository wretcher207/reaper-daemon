# Brief: build the mix SOP skill (mix-apparatus)

Written 2026-08-18 for a session starting cold. Everything needed to begin is
here, so you do not have to scroll `HANDOFF.md` (66 KB, newest at the bottom).
Read that only when this brief points you at it.

**The goal behind the whole project:** drive an entire mix of David's real
session shape start to finish through the bridge. The bridge can already move
every control a mix needs. What is missing is the judgment layer: which control,
by how much, in what order, and how you know when it is done. That is this
skill.

## The deliverable

A skill in the shape of `skills/drum-apparatus/`: a `SKILL.md` that IS the
procedure (ordered steps, not a feature tour), plus whatever calibration data
and helper code it needs alongside.

One decision to make early, and it is worth asking David: `drum-apparatus` lives
in this repo under `skills/`, while `drum-humanize` is a real copy in
`~/.claude/skills/`. Pick one and say why. The repo location keeps the SOP next
to the bridge it drives and under the same tests; the `~/.claude` location is
what actually loads in every session.

Read `skills/drum-apparatus/SKILL.md` before writing a line of this one. It is
the model for tone and structure, and its opening lesson transfers directly:
**the cardinal sin is starting from a named preset instead of from the source
material.** Its equivalent here is reaching for "a vocal chain" or "a drum bus
recipe" instead of listening to what the session actually needs.

## The session shape this targets

Measured on `used-claude-mix.rpp`
(`C:\media\audio\multitracks\ntm-2020-08-loathe-44k24b-165bpm\`), 20 tracks,
162 bpm, 44.1 kHz. David opened it mid-session, so it is real, not a fixture.

- `GTR_Bus` folder over `GTR_Left` (pan -1) and `GTR_Right` (pan +1). Both run
  DIFIX then **thall amp (Odeholm Audio)**. Not Neural DSP.
- `Bass - stem`, already printed. No amp sim live on it in this project.
- `Kontakt 8` with the original MIDI, alongside the printed drums.
- `Drum Buss` folder: Kick, Snare, a `Toms` sub-folder (Rack 1, Rack 2, Floor L,
  Floor R1, Floor R2), Overheads, Close Room, Far Room, Cymbals, Hi Hats, Ride.
  All printed stems.

**Drums arrive as separate printed audio from a VST kit.** No bleed, no phase
coupling between mics, nothing to cut or crossfade. That is why the bridge has
no item-editing surface and does not need one. The one sliver deliberately left
open is take/clip gain, if it ever proves necessary.

David's stated FX vocabulary for a mix: Pro-Q 4, Pro-C 3, BSA Clipper,
Renaissance Axe, BSA Telos Bass, and a mix-bus chain.

## What the bridge already does

Full command reference: `bridge/command_schema.md`. Driving it:
`python reaperd.py cmd <type> '<json>'`, and `python reaperd.py status` for the
heartbeat. The mix-relevant surface:

- **Levels and routing:** `set_track_volume`, `set_track_pan`, `mute_track`,
  `solo_track`, `create_send`, `set_master_send`, `get_track_routing`.
- **FX:** `add_fx`, `remove_fx`, `bypass_fx`, `move_fx`, `set_fx_param`,
  `write_fx_param_automation`, `get_fx_parameters`, `scan_fx`.
- **Safety net:** `snapshot_track_state` then `restore_track_state`, both
  keyed on real GUIDs so a moved FX still restores.
- **The audition strip:** `preview_change` applies ONE change with no undo
  point, then `cancel_preview` or `commit_preview` (which lands it as exactly
  one named undo block). Crash-safe: a leftover preview is restored at bridge
  startup. This is the mechanism for "try it, listen, keep or drop it", and the
  first supervised mixes should run through it.
- **Measurement:** `capture_track_audio` (gated, needs
  `allow_risk_level_3: true`) and `render`, plus `get_render_settings` /
  `set_render_settings` so a render no longer clobbers David's output path.
- **Reading the session:** `get_context` (include_fx), `get_items`,
  `get_selected_track`, `get_capture_preflight`.
- **Bridge edits are cheap now:** `reload_bridge` loads a changed
  `bridge/reaper_agent_bridge.lua` without restarting REAPER. Confirm it worked
  by watching `loaded_at` in `bridge/heartbeat.json` move. `alive_at` cannot
  tell a fresh instance from the old one.

## What is calibrated, and what is not

`set_fx_param` takes normalized 0 to 1, so an uncalibrated plugin is unusable
from code. Maps live in `docs/`:

- `docs/proq4_parameter_map.md` (Pro-Q 4, 740 params) and
  `docs/proc3_parameter_map.md` (Pro-C 3, 240 params). Done 2026-08-18: band
  strides, every curve as a formula, every enum as a table, fresh-instance
  defaults, and the per-plugin traps.
- `docs/odeholm_thall_parameter_map.md`, the guitar amp on this project.
- `docs/misha_parameter_map.md` is a stub. Nothing is captured in it.
- **Not calibrated at all: BSA Clipper, Renaissance Axe, BSA Telos Bass.** If
  the SOP calls for them, they need the same one-time probing pass.

Probing method, if you do that work: insert the plugin on a scratch track, write
a display value with `set_fx_param`'s `formatted_value`, read the normalized
result back with `get_fx_parameters`, and fit. Two points fit a log curve, three
confirm it. Sweeping normalized values and reading the display back is how you
enumerate an enum. Every probe is a real write, so scratch tracks only. The
"How to re-probe" section at the bottom of either map has runnable commands.

## Traps that have already cost hours

- **Disk-media items render as silence when REAPER is not the active app.**
  Originally recorded (2026-08-18) as "a minimized window renders digital
  silence": -180 dBFS minimized, -14.1 restored, proven both directions, with
  virtual instruments still rendering, which is what made one bug look like
  six.

  **Root cause found 2026-09-01: REAPER's "set media items offline when
  application is not active" preference (`offlineinact`).** Minimizing is not
  the trigger, it is just one way to stop being the active app. The preference
  unloads disk media on deactivation; VSTi output has no media source to
  unload, which is exactly the split the original note observed.

  Measured on drones.rpp, same 5 s bounds, REAPER minimized throughout, the
  only variable being whether the media was loaded:

  | media state | LUFS-I | RMS |
  | --- | --- | --- |
  | loaded | -17.3 | -19.5 |
  | offline | -22.3 | -23.2 |

  A ~5 dB drop, being the two disk-media guitar tracks contributing nothing,
  with 0% silence throughout because the Kontakt instruments still rendered.
  The original -180 was the same mechanism on a render whose content was ALL
  disk media -- inferred, not re-reproduced here.

  **`IsIconic` is the wrong check.** It tests a symptom of one trigger. Read
  `get_items.source_state` (`offline` vs `loaded`) or
  `get_context.media_offline_when_inactive` instead, which test the thing that
  actually matters. `get_capture_preflight` warns when the preference is on.

  **The unloaded state is sticky, but it is fixable.** Once media is offline it
  does NOT come back from `ShowWindow`/`SetForegroundWindow` driven by another
  process, from starting playback, or from turning the preference back off --
  all four measured on 2026-09-01. Nor, on that occasion, from a real click on
  the window. What DOES clear it is REAPER action 40101, exposed as the
  `set_all_media_online` command: 13/13 stranded items came back, confirmed by
  re-reading the sources (48000 Hz, 27.73 s, matching the off-disk control).
- **`RENDER_STATS` LUFS goes stale.** A repeated LUFS across different renders
  means the last stats-bearing render, not this one. Never treat LUFS equality
  as evidence of anything but staleness.
- **`capture_track_audio`'s "full_mix" scope is mislabelled.** It sets
  `RENDER_SETTINGS=2` plus `SetOnlyTrackSelected` on every path, so it is really
  a selected-track stem render. It behaves correctly; the label overstates it.
- **Never save David's project.** `save_project` exists and is gated, but no
  session has needed it and every session so far has left his projects unsaved
  and unmodified on disk. Verify with the file's mtime before and after. Undo
  history in REAPER is fine; bytes on disk are not.
- **REAPER takes 45 to 50 seconds after launch before startup scripts run**
  (eval-license screen). A `STALE` from `reaperd.py status` inside the first
  minute of a boot is not a failure.
- **Payload paths use forward slashes.** Git Bash mangles backslashes inside
  single-quoted JSON into invalid escapes.
- **The bridge's main chunk hit Lua's 200-local limit.** New commands go in a
  `do` block assigning onto `handlers`, like `get_items` does, or they will not
  compile.

## The one open question the plan already flagged

**Print-stems is proven on this project, but not with Neural DSP live.** A
soloed offline render through the Odeholm thall amp produced real signal
(-36 dBFS RMS over 0 to 4 s), so printing guitars is available here. The older
offline amp-sim silence trap was observed on Archetype Misha X. The first time a
project has Parallax or an Archetype live, re-run that exact test before
trusting print-stems on it.

This matters to the SOP's first step: if printing works, a mix begins by
printing guitars and bass, and every track becomes independently measurable. If
it does not, those tracks have to be mixed off full-mix differential captures
instead, which is a different procedure.

## Design constraints David has already set

- **A session scan gate comes first.** Classify every track into a role, show
  David the map, and get one confirmation before touching anything. Same
  reasoning as drum-humanize's kit-map gate: guessing wrong about what a track
  is poisons everything downstream, and the check is one message.
- **First mixes run supervised** through the audition strip, and corrections get
  written back into the SOP the way the cymbal lesson was written into
  drum-apparatus. The SOP is expected to grow scars.
- **Do not explain audio or production concepts to David.** He is the expert
  there. Explain agentic and technical decisions instead.

## Suggested first moves

1. Read `skills/drum-apparatus/SKILL.md` end to end.
2. Ask David to talk through one mix in his own order: what he touches first,
   what he is listening for, when he decides a track is done. The SOP should be
   his procedure, not a generic one. This is the highest-value hour available
   and nothing in the repo substitutes for it.
3. Draft the session scan and role map, since it is step 0 of every mix and can
   be proven immediately against `used-claude-mix.rpp`.
4. Only then decide which remaining plugins need parameter maps, because the SOP
   determines which controls actually get automated.

## State when this was written

main @ `6ab708a`, pushed. Suites: 291 bridge Lua, 110 console, 40 json, 453
Python, all green. REAPER 7.79 running with the bridge CONNECTED on
`used-claude-mix.rpp`, which is unsaved and unmodified on disk (08:33:26,
5391561 bytes).
