# REAPER bridge — DO THE TASK, DON'T STUDY THIS FILE

Control David's live REAPER session by writing one command. The round-trip is
0.3s. If a request feels slow it is because YOU added steps. Send ONE command.

## Step 1 — always first

```bash
~/workspace/audio/reaper-bridge/bridge_status.sh
```

## Drums (groove)

```bash
# write the beat to /tmp/groove.dsl, then ONE command:
~/workspace/audio/reaper-bridge/groove.sh /tmp/groove.dsl
```

- Inserts on the track David has SELECTED. He selects it before prompting.
- NEVER guess a track. Do NOT run get_context to pick one. That is how a groove
  hit "KT Out 1" and enraged David. Only pass `--track NAME` if David names it.

DSL:
```
@tempo 145
@map RS Monarch
@seed 42

[verse] bars=2 feel=ff
grid 16
hat_o | x . x . x . x . x . x . x . x . |
snare | . . . . X . . . . . . . X . . . |
kick  | x . . . . . x . x . . . . . x . |
```
Cells `.` rest `x` hit `X` accent `o` ghost · feel `pp p mp mf f ff fff` ·
lanes `kick snare hat_o hat_c ride crash china tom1 tom2 tom3` · short lanes loop.
Maps (exact): `RS Monarch` · `Odeholm Default (Wretcher Fix)` ·
`Ultimate Heavy Drums (MDL Tone)` · `Sleep Token II by MixWave`. Ask which kit if unstated.

### Cymbal rules (mandatory — David enforces these)

| Section | Power hand | Accent cymbal |
|---------|-----------|---------------|
| Verse | `hh_open` (8th grid, ph_velocity=90) | `--accent-cymbal none` |
| Chorus | `hh_open` or `ride` | `--accent-cymbal CRASH_R --accent-every-bars 1` |
| Breakdown | cymbal lane in groove (china/crash on stabs) | `--accent-cymbal none` |

- Never default to closed hi-hat (`hh_closed`) in metal/rock. Open or ride.
- Cymbal **drives** the groove — it is NOT just an accent. Verse: 8 open hat hits/bar minimum.
- Breakdown cymbals: crash (`X`) or china (`C`) on the stab accents ONLY. Never a hi-hat grid during a breakdown.
- Crash velocity decay: first slam is the power hit (~125). cymbal_density repeat hits decay at 0.72× per repeat — already wired into render.py. Use `--cymbal-density 2` for crash sustain feel.
- "China Breakdown" groove: china only, no crashes. Use for deathcore/Black Tongue style.

### Quick groove picks by context

```
Verse (driving):        Standard 16th Stream, D-Beat Driving, D-Beat (Hardcore)
Chorus (big):           Thrash Gallop, Fighting Double Bass, 2-Step Deathcore
Breakdown (breathe):    China Breakdown, Tight Chug Breakdown, World Ending Stomp
Blast (brutal):         Hammer Blast, Traditional Blast, Bomb Blast
Half-time heavy:        Half-Time Crushing, Slam Breakdown (Lurch)
```

## Plugins (FX)

```bash
~/workspace/audio/reaper-bridge/fxload.sh "<plugin words>" "<track|master>"
```
Resolves the exact installed name from REAPER's plugin cache and loads in ~1s.
`send_cmd.sh add_fx '{"target_track_name":"master","fx_name":"<sloppy name>"}'`
self-resolves the name too. Do not guess + retry, do not scan_fx to "find" a name.

### Setting plugin parameters ("set the kick EQ to 80 Hz")

1. Scan the FX to get param `index` + `min`/`max` + current values:
   `send_cmd.sh get_fx_parameters '{"target_track_guid":"{G}","fx_name_contains":"Pro-Q","limit":200}'`
   (each row: `index`, `name`, `normalized_value`, `min`, `max`, `formatted_value`).
2. Use `param_index` (the integer), not `param_name_contains` — FabFilter shares
   words ("Threshold" vs "Auto Threshold") and a name match throws `AMBIGUOUS_PARAM`.
3. Convert the target display value to normalized yourself and set it:
   - linear: `norm = (target - min) / (max - min)`
   - log/freq (Hz): `norm = log(target/min) / log(max/min)`
   `send_cmd.sh set_fx_param '{"target_track_guid":"{G}","fx_index":0,"param_index":27,"normalized_value":0.267}'`
   `set_fx_param` also accepts `"formatted_value":"80 Hz"` (bridge parses + converts
   via the range; numeric values only). Verify by re-scanning the `formatted_value`.
4. Batch multiple sets in one `batch` command with `stop_on_error:true`.

## Everything else

One command: `~/workspace/audio/reaper-bridge/send_cmd.sh <type> '<payload>'`
(mute/solo/tempo/volume/pan/markers/remove_fx/bypass/automation/transport). Target
master with `{"target_track_name":"master", ...}`. Confirm `ok:true` in the reply;
don't narrate it.

## Hard rules

- ONE command per request, then stop. No preamble, no "ok:true" reports, no demos.
- Gate with `bridge_status.sh` first. If it's dead, say so — don't fake success.
- Never guess a track. Resolve the GUID from `get_context` and reuse it.
- Never report done before reading the outbox reply with `ok:true`.
- Never assume a Lua edit took effect — the defer loop runs the last-loaded code;
  Lua edits need a REAPER reload (David relaunches, or the watchdog restarts after
  a crash). Verify live after any reload. Do not force REAPER yourself.

---
Internals (only if David explicitly asks you to change the bridge code):
handlers live in `bridge/reaper_agent_bridge.lua`; schema in
`bridge/command_schema.md`; examples in `commands/examples/`. Add a
`command_<name>` function + `handlers.<name>` registration; resolve via the
existing `find_track`/`find_fx`/`find_fx_param` helpers. Syntax-check with
`lua -e "assert(loadfile('bridge/reaper_agent_bridge.lua'))"`. Lua edits need a
REAPER reload to take effect.
