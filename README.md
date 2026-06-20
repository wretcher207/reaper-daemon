# Reaper Daemon

A local file bridge for controlling REAPER from an AI agent — Claude Code,
Codex, Cursor's terminal agent, anything that can read and write files. No
network, no socket, no MCP server: the agent drops JSON command files in a
folder, a Lua script inside REAPER executes them and writes JSON results back.

Works on **macOS, Windows, and Linux**. The bridge itself is pure Lua using
native REAPER API only (path separators are derived from `package.config`, so
the same script runs everywhere). The agent-facing CLI is a single Python 3
file (`reaperd.py`) with no third-party dependencies.

The bridge is **plugin-agnostic and drum-library-agnostic**. It ships with no
knowledge of any specific synth, amp sim, or instrument. An agent discovers
what a project contains with `scan_fx`, acts on tracks/FX/parameters by name,
saves reusable setups as **recipes**, and can **auto-discover** a drum kit's
note map from the library's own `.midnam`.

## What it does

- **Project control** — transport, tempo, cursor, time selection, render.
- **Tracks** — add, delete, rename, select, volume, pan, mute, solo, arm, color.
- **FX** — add, remove, bypass, reorder, set parameters, write parameter
  automation envelopes. Any plugin (VST2/3, CLAP, AU, JS).
- **Markers, regions, media items.**
- **MIDI** — insert and audition MIDI files; a creative drum DSL engine with
  humanization (velocity model, fatigue, timing jitter).
- **Discovery** — `scan_fx` dumps every FX and parameter; `discover_drum_map`
  reads a drum track's note names and builds a kit map for any library.
- **Recipes** — save command sequences and replay them on any project.

Every mutating command runs inside a REAPER undo block (Cmd+Z / Ctrl+Z to revert).

## How it works

A single runtime: the **Lua bridge** (`bridge/reaper_agent_bridge.lua`) runs
forever as a `reaper.defer` loop inside REAPER. It polls `inbox/`, executes one
command per tick, writes results to `outbox/`, and a heartbeat to
`bridge/heartbeat.json`. This is the only thing that touches the REAPER API.

All JSON writes are atomic (write `.tmp`, then rename). Command files move
`inbox/` → `processing/` → `archive/` (ok) or `failed/` (error).

## Install (cross-platform, one command)

Requires REAPER and Python 3.8+ (no pip packages needed).

```bash
git clone https://github.com/wretcher207/reaper-daemon.git
cd reaper-daemon
python3 setup/install.py        # macOS/Linux  (use `python` on Windows)
```

`setup/install.py` detects your OS, locates REAPER's per-user resource
directory, and writes a marker-delimited block into `Scripts/__startup.lua`
that auto-loads the bridge on every launch, pointing at this clone. It is
idempotent — re-run it any time you move the repo. Then **(re)start REAPER**.

Options:

```bash
python3 setup/install.py --dry-run     # preview, change nothing
python3 setup/install.py --uninstall   # remove the managed auto-start block
python3 setup/install.py --bridge-root /path/to/clone
REAPER_RESOURCE_PATH=/custom/dir python3 setup/install.py
```

Prefer to load it manually instead of at startup? In REAPER:
`Actions > Show action list > ReaScript: Load…`, pick
`bridge/reaper_agent_bridge.lua`, run it once. It runs as a background deferred
script and regenerates `bridge/bridge_config.json` and its working folders on
first run.

## Install via ReaPack (REAPER-native, alternative)

Prefer REAPER's own package manager? Add this repo to ReaPack:

1. In REAPER: `Extensions > ReaPack > Import repositories`.
2. Paste: `https://github.com/wretcher207/reaper-daemon/raw/main/index.xml`
3. `Extensions > ReaPack > Browse packages`, find **Reaper Daemon**, install.

Two things to know, because ReaPack delivers the script but not the rest of
the setup the clone install handles for you:

- **It does not auto-start.** ReaPack installs the bridge as an Action but does
  not run it on launch. Run the action once per session, or add it to your
  `Scripts/__startup.lua` (`python3 setup/install.py` from a clone does this
  for you).
- **Point your agent at the install folder.** ReaPack installs to
  `<REAPER resource>/Scripts/reaper-daemon/`. Right-click the package in
  ReaPack and "Show in explorer/finder" to get the exact path, then aim your
  agent at that folder.

## Verify it

With REAPER open and a project loaded:

```bash
python3 reaperd.py status
python3 reaperd.py send commands/examples/get_context.json --wait
```

`status` reports the bridge heartbeat; `send --wait` prints a JSON result
describing the open project. If it times out, check that
`bridge/heartbeat.json` exists and is fresh.

## The agent CLI — `reaperd.py`

One Python entry point for everything an agent does (no shell helpers, no
`jq`/`grep` pipelines — works identically on macOS, Windows, Linux):

```bash
python3 reaperd.py status                       # liveness check (run first)
python3 reaperd.py send <cmd.json> --wait       # send a command file
python3 reaperd.py cmd <type> '<payload-json>'  # send by type + payload
python3 reaperd.py fxload "<plugin query>" <track|master>
python3 reaperd.py setparam <track> "<fx>" "<param>" "<display value>"
python3 reaperd.py eq <track> "<fx>" <band> <freqHz> <gaindB> [Q]
python3 reaperd.py groove <beat.dsl> --track Drums [--position SEC] [--map NAME]
python3 reaperd.py jam                          # DSL drum beat from stdin -> selected track
python3 reaperd.py list-maps                    # available drum-kit maps
python3 reaperd.py discover-map <track> [--save <name>]
python3 reaperd.py add-map <name> --file <map.json>   # or --roles '{...}' / stdin
python3 reaperd.py remove-map <name>
```

`fxload` and `cmd add_fx` resolve a fuzzy plugin query to REAPER's exact
installed name from the VST/CLAP/AU cache before loading. `setparam` works on
any plugin by parameter index, binary-searching the normalized value that
produces a target display value, then verifying.

## Drum kits — any library, auto-discovered

The DSL drum engine (`skills/drum-apparatus/`) ships a few built-in kit maps
(GM Standard, RS Monarch, Odeholm, MDL Tone, Sleep Token II) in
`skills/drum-apparatus/catalog/maps.json`. For **any other library**, auto-discover:

```bash
# 1. With the drum plugin on a track that has its .midnam loaded:
python3 reaperd.py discover-map Drums --save MyKit
# 2. Use it:
python3 reaperd.py groove beat.dsl --track Drums --map MyKit
#    or in the DSL:   @map MyKit
```

`discover-map` reads the MIDI note names REAPER has for the track (the
`.midnam` the library installed), classifies each note into a groovekit role
(kick / snare / hat-closed / hat-open / ride / crash / china / tom1..4 / ...),
fills missing articulations by fallback so a sparse kit never breaks the
engine, and saves the result to the user overlay
(`skills/drum-apparatus/maps/<name>.json`, gitignored). Libraries that don't
ship a `.midnam` (some Kontakt kits) report no note names; for those, build the
map by hand with `add-map`:

```bash
python3 reaperd.py add-map MyKontactKit --roles '{"KICK_R":36,"SNARE":38,"HH_OPEN_1":46,"CRASH_R":49,"CHINA_R":52}'
```

## For agents

Read `AGENTS.md` for the workflow, `CLAUDE.md` for the fast operational
protocol, and `bridge/command_schema.md` for every command. Working JSON
examples are in `commands/examples/`.

## Layout

```text
bridge/reaper_agent_bridge.lua   the bridge (runs inside REAPER, OS-neutral)
bridge/bridge_config.json        machine-specific config (regenerated)
bridge/command_schema.md         full command reference
reaperd.py                       cross-platform agent CLI (Python 3)
setup/install.py                 wire auto-start into REAPER (cross-platform)
commands/examples/               one JSON example per command
recipes/                         saved, replayable command sequences
skills/drum-apparatus/           DSL drum engine + kit-map auto-discovery
inbox/ outbox/ processing/ ...   runtime folders
```

## License

MIT. See [LICENSE](LICENSE).
