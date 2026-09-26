# Develop

## Tests

CI runs on Windows, macOS, and Linux. From the repository root:

```bash
python -m pip install pytest
python -m pytest tests skills/drum-apparatus/tests -q
python -m py_compile reaperd.py reaper_mcp.py setup/install.py
lua bridge/test_bridge.lua
lua bridge/test_json.lua
```

The Lua checks need Lua 5.4. Live REAPER integration is a separate manual
gate, because CI does not launch the DAW. See
[SMOKE_VERIFY.md](SMOKE_VERIFY.md) for that checklist.

## Layout

| Path | What it is |
| --- | --- |
| `bridge/reaper_agent_bridge.lua` | the bridge. Runs inside REAPER, OS-neutral. |
| `bridge/automation.lua` | FX-envelope automation, loaded by the bridge on every load |
| `bridge/bridge_config.json` | machine-specific config, regenerated on first run |
| `bridge/command_schema.md` | full command reference |
| `bridge/reaper_daemon_console.lua` | Daemon Console panel (ReaImGui, clone-only) |
| `reaperd.py` | cross-platform agent CLI |
| `reaper_mcp.py` | MCP stdio server over the same bridge |
| `console_sidecar.py` | Daemon Console broker (headless Claude session) |
| `setup/install.py` | wires auto-start into REAPER |
| `commands/examples/` | one JSON example per command |
| `skills/drum-apparatus/` | drum DSL engine and kit-map discovery |
| `skills/guitar-apparatus/` | guitar and bass riff notation and performance engine |
| `docs/CONSOLE.md` | console architecture and operating rules |
| `inbox/ outbox/ processing/ archive/ failed/` | runtime folders |
