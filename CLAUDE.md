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

## Conventions

- All files UTF-8 without BOM.
- `commands/examples/` holds a working JSON example for every command type —
  copy these. (Examples still show Windows-style `midi_path` values; use
  absolute macOS paths.)
- The optional Windows job worker was not ported (see README).
