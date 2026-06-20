# REAPER bridge — DO THE TASK, DON'T STUDY THIS FILE

Control David's live REAPER session by writing one command. The round-trip is
0.3s. If a request feels slow it is because YOU added steps. Send ONE command.
Everything below is the single cross-platform CLI: `python3 reaperd.py`.

## Step 1 — always first

```bash
python3 ~/workspace/audio/reaper-bridge/reaperd.py status
```

## Drums (groove)

```bash
# write the beat to /tmp/groove.dsl, then ONE command:
python3 ~/workspace/audio/reaper-bridge/reaperd.py groove /tmp/groove.dsl --track Drums
```

- Inserts on the track David has SELECTED if `--track` is omitted. He selects
  it before prompting. NEVER guess a track. Do NOT run get_context to pick one.
  That is how a groove hit "KT Out 1" and enraged David. Only pass `--track NAME`
  if David names it.

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
`@map` is optional (defaults to GM Standard). Ask which kit if unstated.

### Drum kits — auto-discover any library

If the kit isn't one of the built-ins (RS Monarch, Odeholm, MDL Tone, Sleep
Token II, GM Standard), discover it from the library's own .midnam:

```bash
python3 ~/workspace/audio/reaper-bridge/reaperd.py discover-map Drums --save MyKit
python3 ~/workspace/audio/reaper-bridge/reaperd.py groove beat.dsl --track Drums --map MyKit
```
Kits with no .midnam (some Kontakt libs) report no note names; build by hand:
`reaperd.py add-map MyKit --roles '{"KICK_R":36,"SNARE":38,...}'`.

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
python3 ~/workspace/audio/reaper-bridge/reaperd.py fxload "<plugin words>" "<track|master>"
```
Resolves the exact installed name from REAPER's plugin cache and loads in ~1s.
`reaperd.py cmd add_fx '{"target_track_name":"master","fx_name":"<sloppy name>"}'`
self-resolves the name too. Do not guess + retry, do not scan_fx to "find" a name.

### Setting plugin parameters ("set the kick EQ to 80 Hz")

**Easy way (any plugin):**
```bash
python3 ~/workspace/audio/reaper-bridge/reaperd.py setparam Kick "Pro-Q" "Band 1 Frequency" "80 Hz"
python3 ~/workspace/audio/reaper-bridge/reaperd.py setparam Kick "ReaEQ" "Gain-Band 2" "-3"
```
Binary-searches the normalized value that produces your target display, sets it,
verifies. Handles any plugin, ±inf endpoints, log/linear scaling.
Use `norm=0.267` for direct normalized, or for enum/string params ("Bell", "Off").
EQ band shortcut: `reaperd.py eq Kick "Pro-Q" 1 80 -3 0.7`.

**Manual way:** scan with `get_fx_parameters`, use `param_index` (not name —
FabFilter shares words), convert to normalized, `reaperd.py cmd set_fx_param` with
`normalized_value`. Batch in one `batch` with `stop_on_error:true`.

## Everything else

One command: `python3 ~/workspace/audio/reaper-bridge/reaperd.py cmd <type> '<payload>'`
(mute/solo/tempo/volume/pan/markers/remove_fx/bypass/automation/transport). Target
master with `{"target_track_name":"master", ...}`. Confirm `ok:true` in the reply;
don't narrate it.

## Hard rules

- ONE command per request, then stop. No preamble, no "ok:true" reports, no demos.
- Gate with `reaperd.py status` first. If it's dead, say so — don't fake success.
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
