---
name: reaper-setup
description: Install, connect or troubleshoot Reaper Daemon. Use when the user asks to set up Reaper Daemon or connect Claude to REAPER, when get_status reports NOT_INSTALLED, WRONG_FOLDER, BRIDGE_NEVER_LOADED or BRIDGE_NOT_RESPONDING, when the reaper MCP tools are missing, or before the first REAPER task in a session that has never reached the bridge.
---

# Reaper Daemon setup

The plugin carries the MCP server and the skills. REAPER itself needs the
bridge, which comes from a clone of the reaper-daemon repository on the same
computer, wired into REAPER by `setup/install.py`. Nothing works until both
halves exist and REAPER has restarted once.

## 1. Find out where things stand

Call `get_status` and read `problem`:

| problem | meaning | go to |
| --- | --- | --- |
| `NOT_INSTALLED` | no folder at `bridge_root` | step 2 |
| `WRONG_FOLDER` | folder exists, no bridge in it | step 2, or point the setting at the real clone (step 3) |
| `BRIDGE_NEVER_LOADED` | cloned, but the bridge has never run | step 2 from Phase 4 |
| `BRIDGE_NOT_RESPONDING` | installed, REAPER closed or bridge stopped | ask the user to start REAPER, then call `get_status` again |
| none, `alive: true` | working | step 4 |

If the reaper tools aren't available at all, the server didn't start. The
usual cause is the plugin's Python command: on Windows `python3` is often
missing or a Microsoft Store stub, so use `python` or `py`. A missing folder
never stops the server from starting, so it is not the cause.

Chat on claude.ai can't reach REAPER. Setup needs Claude Code or Cowork on
the computer REAPER runs on. If this session can't run shell commands, say so
and stop.

## 2. Install the bridge

Follow `INSTALL.md` at the root of this plugin, Phases 1 to 8, with these
changes:

- Install location (Phase 2): use the folder `get_status` reported as
  `bridge_root`, unless the user wants another one. That is where the plugin
  looks.
- Phase 5 (rendering, capture and saving) is the user's decision. Ask it as
  written. Measurement tools such as `verify_change`, `profile_track` and
  `capture_track_audio` need audio writes on.
- Skip Phase 9's `claude mcp add` suggestion. The plugin already provides the
  MCP server, and a second registration doubles every tool.

## 3. Match the plugin settings

The plugin has two settings: the Reaper Daemon folder (default
`~/reaper-daemon`) and the Python command (default `python3`). If the clone
lives somewhere else, or Python runs as `python` or `py`, change them.

In Claude Code, find the plugin's full id (`reaper-daemon@...`) in
`claude plugin list`, then:

```bash
echo '{"install_dir": "/path/to/clone", "python": "python"}' | claude plugin configure <full id> --values-stdin
```

Leave out either key to keep its current value. Cowork uses the defaults
without asking, so there the clone has to be at `~/reaper-daemon` and
`python3` has to work.

Settings and a fresh install take effect when the MCP server restarts, so a
new session may be needed. Then call `get_status` again until `alive` is
true.

## 4. First contact

Report `gates` from `get_status` in plain words: which of audio writes,
project save and preference writes are on.

Then call `get_context` and tell the user the project name and track count.
That proves the round trip without changing anything. Suggest one read-only
next step, such as `scan_fx` on a track they name, before anything audible.
