# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A local **file bridge** that lets an agent control REAPER with no network
protocol. The agent writes JSON command files into `inbox/`; a deferred Lua
script running inside REAPER executes them and writes JSON results to
`outbox/`. No server, no socket — just atomic file writes in shared folders.

This is the **macOS port** of David's Windows bridge
(`github.com/wretcher207/reaper-agent-bridge`). The JSON protocol and the Lua
command handlers are unchanged; the port only fixed path separators and
swapped the Windows helper scripts for macOS equivalents.

## Mandatory command protocol (NO EXCEPTIONS)

1. Use `./send_cmd.sh <type> '<payload>'` for ALL commands. No Python. No manual JSON.
2. ALWAYS use `target_track_guid` to identify tracks. Never name-based guessing.
3. Confirm `ok:true` AND correct track name in the outbox response before reporting done.

### NTM Opeth Reaper.RPP track GUIDs (skip get_context entirely)

| Track | GUID |
|-------|------|
| DrumBuss | {EFFA0D78-5CDB-2343-893F-DFD27143CBB9} |
| KickSum | {675FBA25-1991-7D41-A7BF-91541FC2F2DA} |
| 01 Kicks mic | {7F631DC6-CCB1-CF47-BB6C-525F7B929F23} |
| 02 Kicks trig | {BE9EAE7D-9FB8-D44B-875D-6747E357B237} |
| SnareSum | {E582491D-4F1F-9446-B802-244F8871C5A4} |
| 03 Sd Trig Hi | {3A91A1B3-8E71-4444-AE11-3DCF2E8AA6B3} |
| 04 Sd Trig Lo | {0857A694-01D0-F44B-90A7-6295ADD85048} |
| 05 Sd 57 | {B7B83BED-3F1A-EC4B-ADEC-99C8EF1717C2} |
| 06 Sd Bot 57 | {DB94EA70-3B21-064B-8F4F-DA25547579BE} |
| TomsSum | {66888A19-9672-FD49-A217-45D0DA93573F} |
| CymbalSum | {0D2A623E-1277-214D-BF50-18827BE6A525} |
| OH Sum | {961155B2-62F3-484F-A504-155BDBDAAD92} |
| RoomSum | {7DC64D2E-7210-3E4A-97E0-1272F3BA8A72} |
| BassBuss | {4E8D0898-3706-6641-B22D-F9E109A13930} |
| Rhyth_GEET | {CAB013DF-6C33-FF46-976B-4DF96941AEF1} |
| Leads | {9979D401-428B-A74A-B34D-C3B6ACBD12FF} |
| MASTER | {45D29BD9-2A52-184B-B231-BF6E49FE4397} |

## Verify before you claim (read this first)

A command file landing in `inbox/`, or even moving to `archive/`, is **not**
proof the edit happened. The only authoritative confirmation is the
`outbox/<id>.json` reply with `ok: true` and the data you expected — and, for
anything audible or visible, REAPER actually reflecting it. Do not tell David a
MIDI clip was inserted / a tempo was set / an FX was added until you have read
that reply. If the bridge isn't running, say so; don't assume a silent success.

## Architecture

One runtime: the **REAPER Lua bridge** (`bridge/reaper_agent_bridge.lua`),
loaded via `Scripts/__startup.lua` (auto-start) or the Action List (manual).
It runs forever as a `reaper.defer` loop, polls `inbox/` every 0.25s, executes
one command per tick, and writes a heartbeat to `bridge/heartbeat.json`. This
is the only thing that touches the REAPER API; all command handlers live here.

### Command file lifecycle

`inbox/<id>.json` → moved to `processing/` → executed → result written to
`outbox/<id>.json` → source moved to `archive/` (success) or `failed/` (error).
Files are sorted by name, so command IDs are timestamped to preserve order.
One command in flight at a time.

### Atomic writes — mandatory everywhere

Every JSON write is: write `<path>.tmp`, then rename to `<path>`. The poller
skips `.tmp` files. The Lua, `send_reaper_command.sh`, and any agent code all
follow this. Breaking it means a reader can see a half-written file.

### Paths are OS-derived

`SEP = package.config:sub(1,1)` and a `join()` helper build every path, so the
Lua runs on macOS (`/`) and Windows (`\`). Never hardcode a separator. When
loaded from `__startup.lua`, the launcher sets the global
`REAPER_AGENT_BRIDGE_DIR` so the bridge finds its own folder (otherwise
`get_action_context()` reports the startup script's path).

### Command schema

```json
{ "id": "...", "version": 3, "type": "get_context", "created_by": "...",
  "created_at": "ISO-8601", "dry_run": false, "payload": {} }
```

Result schema: `{ id, ok, type, finished_at, message, data | error }`. Error
codes are `UPPER_SNAKE` prefixes parsed from the thrown message (e.g.
`NO_TARGET_TRACK:`, `AMBIGUOUS_FX:`). Full schema: `bridge/command_schema.md`.

## Adding a command (most common task)

All handlers are in `reaper_agent_bridge.lua`:

1. Write a `command_<name>(command)` function returning a plain table (becomes
   `data`).
2. Register it: `handlers.<name> = command_<name>`.
3. If it mutates the project, leave it out of the `NO_UNDO_BLOCK` set so
   `is_mutating()` returns true. Mutating commands are auto-wrapped in
   `Undo_BeginBlock`/`EndBlock` (skipped inside `batch`/`apply_recipe`, which
   wrap the whole set). Read-only commands skip the `dry_run` short-circuit so
   they always return real data.
4. `dry_run: true` short-circuits before any handler runs (except
   `get_context`), so handlers never need to check it.
5. Resolve tracks via `find_track`, FX via `find_fx`, params via
   `find_fx_param`. These throw `NO_*` / `AMBIGUOUS_*` — don't reimplement
   matching.

`pcall` wraps every command, so a thrown error becomes a failed result and
never kills the defer loop. Throw `error("CODE: human message")`.

## Running things (macOS)

| Action | How |
|--------|-----|
| Install auto-start | `./setup/macos-install.sh`, then restart REAPER |
| Load manually | REAPER → Actions → Show action list → ReaScript: Load → `bridge/reaper_agent_bridge.lua` |
| Send / test a command | `./send_reaper_command.sh <file.json> --wait` |
| Syntax-check the Lua | `lua -e "assert(loadfile('bridge/reaper_agent_bridge.lua'))"` (brew install lua) |

## Before sending commands

- Check `bridge/heartbeat.json` is fresh. Stale/missing → bridge not running;
  point David to `./setup/macos-install.sh` or a manual load.
- Run `get_context` before any ambiguous edit.
- Never assume any plugin's FX or parameter indices — they shift between
  versions. Run `scan_fx`, or `get_fx_parameters` for one FX, then act.
- Don't touch all tracks unless David explicitly asks. Prefer the selected
  track only when he says "selected track" or exactly one is selected.
- Don't overwrite items unless `replace_existing_in_range: true`.

## Drum session fast path (ALWAYS use this — skips the context probe)

Known project layout: drum track = **"Drums"** (Kontakt 8, channel 1). Map = **RS Monarch** (default in generate.py).

```bash
# 1. Bridge check (always)
./bridge_status.sh

# 2. Generate MIDI — never write raw Python; always use the CLI
cd skills/drum-apparatus
python generate.py --groove "GROOVE NAME" --bars N --tempo BPM \
  --no-fills --humanize 15 --out /tmp/drums.mid

# 3. Insert — target track is "Drums", not "Monarch" or "Kontakt 8"
./send_reaper_command.sh /tmp/insert_drums.json
```

No `get_context` probe needed for drum tasks unless David mentions a different project or track. The probe costs 4–5 seconds and the answer is always "Drums".

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

## Conventions

- All files UTF-8 without BOM.
- `commands/examples/` holds a working JSON example for every command type —
  copy these. (Examples still show Windows-style `midi_path` values; use
  absolute macOS paths.)
- The optional Windows job worker was not ported (see README).
