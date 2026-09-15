<p align="center">
  <img src="docs/assets/readme-header.png" alt="Reaper Daemon. Nothing is listening." width="100%">
</p>

# Reaper Daemon

[![Reaper Daemon MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/wretcher207/reaper-daemon/badges/score.svg)](https://glama.ai/mcp/servers/wretcher207/reaper-daemon)

Reaper Daemon is a REAPER MCP server and file bridge for driving REAPER from
an AI agent, including mixing your session with Claude or any other agent.
No network socket, no port, no extensions.

The agent drops a JSON command file in a folder. A Lua script inside REAPER
runs it and writes a JSON result back. That's the whole protocol. It works
with Claude Code, Codex, Cursor, or anything else that can read and write
files, and an optional stdio MCP server, `reaper_mcp.py`, exposes the same
bridge as tools for Claude Desktop and any other MCP client.

macOS, Windows, Linux. Pure Lua inside REAPER, plain Python 3 outside, no
pip packages. Every change runs inside a REAPER undo block, so Ctrl+Z
reverts anything an agent does.

## Install

```bash
git clone https://github.com/wretcher207/reaper-daemon.git
cd reaper-daemon
python3 setup/install.py
```

Restart REAPER, then check the bridge is alive:

```bash
python3 reaperd.py status
```

Prefer ReaPack, or want to load the bridge by hand? See [docs/install.md](docs/install.md).

## Mix with an AI agent

Point Claude, or any agent talking to Reaper Daemon, at your open REAPER
session and ask it to work on the mix. It reads every plugin and parameter
with `scan_fx`, sets FX values in real units like `"-2.5 dB"`, and writes
automation. `verify_change` captures the track before and after a move and
reports what changed in the audio, so you are not taking its word for it.
Every move sits inside a REAPER undo block, and your ears make the call.

Example prompts:

- "The bass is muddy around 300 Hz, pull it down."
- "Bring the vocal down so it sits with the mix instead of on top."
- "Tune the bass until its LUFS is down 3 dB."
- "What plugins are on the drum bus, and what are they set to?"

See [MCP server](docs/mcp.md) for setup and [Verify](docs/verify.md) for
what the measurements do and do not prove.

## What it does

| Area | Commands |
| --- | --- |
| Project | transport, tempo, cursor, time selection, render, save |
| Tracks | add, delete, rename, select, volume, pan, mute, solo, arm, color |
| Routing | read sends and receives, create sends, toggle master feed |
| FX | add, remove, bypass, reorder, set parameters, write automation, save chains |
| Markers, regions, media items | full read and write |
| MIDI | insert MIDI files, plus a drum DSL with humanization |
| Guitar and bass | `shred` renders a humanized riff, `band` cuts a whole rhythm section |
| Discovery | `scan_fx` dumps every plugin and parameter; `discover_drum_map` reads any kit's note map |
| Verify | measure loudness, spectrum and dynamics before and after a mix move |

The bridge knows nothing about any specific plugin or drum library. Agents
discover what a project contains and act on it by name.

## Three ways to talk to it

**CLI.** One Python entry point for everything an agent does.

```bash
python3 reaperd.py send commands/examples/get_context.json --wait
python3 reaperd.py shred --track argent-l --bars-file riff.txt --seed 101
```

**MCP server.** `reaper_mcp.py` wraps the bridge as tools over stdio.
Ask Claude Desktop to "measure the drums" and it does.

[![Reaper Daemon MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/wretcher207/reaper-daemon/badges/card.svg)](https://glama.ai/mcp/servers/wretcher207/reaper-daemon)

**Daemon Console.** A chat panel docked inside REAPER, backed by a headless
Claude Code session that always knows which track you have selected.

## Docs

| | |
| --- | --- |
| [Install](docs/install.md) | one-line installer, ReaPack, manual load |
| [CLI](docs/cli.md) | `reaperd.py` reference |
| [MCP server](docs/mcp.md) | setup for Claude Desktop and other clients |
| [Daemon Console](docs/CONSOLE.md) | the in-REAPER chat panel |
| [Drums](docs/drums.md) | kit discovery, stem profiling, humanize |
| [Guitar and bass](docs/guitar-bass.md) | `shred` and `band` |
| [Mix recipes](docs/mix-recipes.md) | capture, compare and rebuild a mix setup |
| [Verify](docs/verify.md) | closed-loop mix moves with measured proof |
| [Protocol](docs/protocol.md) | the file wire format |
| [Troubleshooting](docs/troubleshooting.md) | silent captures, stale heartbeats |
| [Security](docs/security.md) | trust model and the optional token |
| [Develop](docs/develop.md) | tests, CI, repo layout |

Agents should read [AGENTS.md](AGENTS.md), [CLAUDE.md](CLAUDE.md), and
[bridge/command_schema.md](bridge/command_schema.md). Two bundled skills,
[arrangement-midi](skills/arrangement-midi/SKILL.md) and
[drum-humanize](skills/drum-humanize/SKILL.md), cover MIDI composition and
drum humanization.

## Security

Any process that can write to `inbox/` can drive REAPER. Keep the bridge
folder local and off shared drives. Details in [docs/security.md](docs/security.md).

## License

MIT. See [LICENSE](LICENSE).

Keyboard performance setup, MIDI/controller tests and template saving are documented in [the performance workflow](docs/keyboard-performance.md).
