# Reaper Daemon (macOS)

A local file bridge for controlling REAPER from an AI agent — Claude Code,
Codex, anything that can read and write files. No network, no socket, no MCP
server: the agent drops JSON command files in a folder, a Lua script inside
REAPER executes them and writes JSON results back.

This is the **macOS port** of the original Windows bridge. The protocol and the
Lua handlers are identical; only path handling and the helper scripts changed
(`/` separators, a bash `send_reaper_command.sh`, and a `__startup.lua`
auto-loader instead of `.bat`/`.ps1`).

The bridge is **plugin-agnostic**. It ships with no knowledge of any specific
synth, amp sim, or instrument. An agent discovers what a project contains with
`scan_fx`, acts on tracks/FX/parameters by name, and can save reusable setups
as **recipes**.

## What it does

- **Project control** — transport, tempo, cursor, time selection, render.
- **Tracks** — add, delete, rename, select, volume, pan, mute, solo, arm, color.
- **FX** — add, remove, bypass, reorder, set parameters, write parameter
  automation envelopes.
- **Markers, regions, media items.**
- **MIDI** — insert and audition MIDI files.
- **Discovery** — `scan_fx` dumps every FX and parameter in the project.
- **Recipes** — save command sequences and replay them on any project.

Every mutating command runs inside a REAPER undo block (Cmd+Z to revert).

## How it works

A single runtime: the **Lua bridge** (`bridge/reaper_agent_bridge.lua`) runs
forever as a `reaper.defer` loop inside REAPER. It polls `inbox/`, executes one
command per tick, writes results to `outbox/`, and a heartbeat to
`bridge/heartbeat.json`. This is the only thing that touches the REAPER API.

All JSON writes are atomic (write `.tmp`, then rename). Command files move
`inbox/` → `processing/` → `archive/` (ok) or `failed/` (error).

## One-shot install via your AI agent (easiest)

If you already have a coding agent on your Mac (Claude Code, Codex CLI,
Cursor's terminal agent, etc.), copy [INSTALL.md](INSTALL.md) and paste it
into the agent. It will clone the repo, wire up REAPER's startup, pause for
you to restart REAPER once, and run a real smoke test to confirm the bridge
is live. No shell commands to type yourself.

## Setup (macOS, manual)

Requires REAPER (no third-party extensions — uses native REAPER API only).

```bash
./setup/macos-install.sh
```

This writes a managed block into REAPER's `Scripts/__startup.lua` that
auto-loads the bridge on every launch, pointing at this clone. Re-run it any
time you move the repo. Then **(re)start REAPER**.

Prefer to load it manually instead of at startup? In REAPER:
`Actions > Show action list > ReaScript: Load…`, pick
`bridge/reaper_agent_bridge.lua`, run it once. It runs as a background deferred
script and regenerates `bridge/bridge_config.json` and its working folders on
first run.

To remove the auto-loader later, run `./setup/macos-uninstall.sh` and restart
REAPER. Your clone, recipes, and working folders are left untouched.

## Test it

With REAPER open and a project loaded:

```bash
./send_reaper_command.sh commands/examples/get_context.json --wait
```

You should get a JSON result describing the open project. If it times out,
check that `bridge/heartbeat.json` exists and is fresh.

## For agents

Read `AGENTS.md` for the workflow and `bridge/command_schema.md` for every
command. Working JSON examples are in `commands/examples/`.

## Layout

```text
bridge/reaper_agent_bridge.lua   the bridge (runs inside REAPER)
bridge/bridge_config.json        machine-specific config (regenerated)
bridge/command_schema.md         full command reference
setup/macos-install.sh           wire auto-start into REAPER
send_reaper_command.sh           send a command + poll for the result
commands/examples/               one JSON example per command
recipes/                         saved, replayable command sequences
inbox/ outbox/ processing/ ...   runtime folders
```

## Differences from the Windows repo

- Path separators are OS-derived (`package.config`), so the Lua works on both
  macOS and Windows.
- `.bat`/`.ps1` helpers → `send_reaper_command.sh` + `setup/macos-install.sh`.
- The optional PowerShell **job worker** was not ported. On macOS the agent
  runs its own shell, so it generates MIDI/audio directly and feeds the file to
  `insert_midi_file` — no external-tool runner needed.

## License

MIT. See [LICENSE](LICENSE).
