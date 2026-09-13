# MCP server

`reaper_mcp.py` wraps the file bridge as an
[MCP](https://modelcontextprotocol.io) stdio server, so any MCP client can
drive REAPER conversationally. No dependencies, no network listener. Tool
calls become the same inbox and outbox files, with the same safety rules:
undo blocks, `dry_run`, risk gating.

## Setup

Claude Code:

```bash
claude mcp add reaper -- python3 /path/to/reaper-daemon/reaper_mcp.py
```

Claude Desktop, in `claude_desktop_config.json`:

```json
{ "mcpServers": { "reaper": {
    "command": "python",
    "args": ["C:/path/to/reaper-daemon/reaper_mcp.py"] } } }
```

Use `python` instead of `python3` on Windows if that is what is on PATH.

Claude Desktop extension: download `reaper-daemon.mcpb` from the
[latest release](https://github.com/wretcher207/reaper-daemon/releases/latest),
open it, and pick your Reaper Daemon folder when asked. The bundle is a small
launcher for the install you already have, so clone and install first. The
same bundle is listed in the official MCP registry as
`io.github.wretcher207/reaper-daemon`, built from `packaging/mcpb/`.

Then ask: "add a ReaEQ to the bass and carve 2 dB at 300 Hz", "what plugins
are on the master?", "program a d-beat groove at bar 33".

## Tools

29 tools, grouped:

| Group | Tools |
| --- | --- |
| Discovery | `get_context`, `scan_fx`, `get_fx_parameters`, `get_track_routing` |
| Project | transport, tracks, markers and regions, MIDI insertion |
| FX | FX chains, `set_fx_param` (formatted values like `"-16.00 dB"` work), `get_fx_param_automation` and automation writes |
| Batch | `batch` runs several commands in one undo block |
| Capture | post-FX stem capture |
| Drums | `profile_track`, `riff_grid`, `insert_groove` |
| Parts | `insert_riff` (one guitar or bass part), `cut_band` (the four-track jam, per-leg results), `humanize_take` (dynamics and micro-timing on an existing take, `--follow-lead` included) |
| Closed loop | `verify_change` (one mutation with measured pre/post proof, see [Verify](verify.md)), `tune_param` (search a parameter until a measured target like "bass LUFS-I down 3 dB" is hit) |
| Analysis | `analyze_track`, `compare_tracks` (need [Post Mortem](https://github.com/wretcher207/post-mortem)) |

Things the tools tell you, so you do not have to guess:

- `profile_track` and `riff_grid` read the saved `.rpp` on disk, not the
  live project. Save first. Every payload states this.
- `tune_param` runs a baseline render plus up to 5 iterations, 6 renders
  total, and reports converged, unconverged, or non-monotone.
- `analyze_track` and `compare_tracks` hand the model measured mix data:
  LUFS, true peak, spectrum, stereo image, masking table.

## Safety

- Every mutation is undoable with Ctrl/Cmd+Z. Single commands and batches
  are one undo step each. `tune_param` leaves one undo point per iteration
  set.
- Destructive tools ask the model to confirm intent.
- Audio capture sits behind the `allow_audio_writes` config gate. Turn it on
  with `python3 setup/install.py --allow-audio-writes`, then send
  `reload_bridge`. Saving the project and writing REAPER preferences are
  separate gates, so measuring never requires granting those.

## Stable identities

Discovery returns REAPER's real track and FX GUIDs alongside names and
indices. Use the GUIDs when planning a later change. Names can repeat and
indices move when a user edits the chain. The identity fields and
compatibility rules are in
[`bridge/command_schema.md`](../bridge/command_schema.md#stable-discovery-identities).

Post Mortem consumes these read-only identities to validate structured
recommendations. Reaper Daemon does not preview or apply those
recommendations. Its mutation commands stay explicit, independently
authorized bridge operations. Reaper Daemon stays MIT-licensed and local.
Planned hosted Post Mortem services do not move this bridge behind a
commercial boundary.
