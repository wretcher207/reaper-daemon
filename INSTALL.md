# Install Prompt: Reaper Daemon (macOS)

**For humans:** paste this entire file into a coding agent that runs on your
Mac with shell access (Claude Code, Codex CLI, Cursor's terminal agent, etc.).
The agent will install the bridge end-to-end and verify it works. You only
have to restart REAPER once when it asks.

**For the agent:** the rest of this file is for you. Do the phases in order.
Each phase ends with a checkpoint — do not move on until you confirm the
checkpoint. If anything fails, stop and report the exact error to the user
instead of pushing forward.

---

## Phase 0 — Capability check

Confirm you can:

- Execute shell commands.
- Read and write files.
- Pause mid-task and wait for a user reply.

If any of these are missing, stop now and tell the user verbatim:

> I can't drive this install end-to-end from here. Open the install prompt in
> Claude Code or another agent with shell access on your Mac and paste it
> there.

## Phase 1 — Preflight

Run these and read the output:

```bash
uname -s
command -v git || echo "missing"
ls -d /Applications/REAPER.app 2>/dev/null || echo "missing"
```

Checkpoints:

- `uname -s` must print `Darwin`. If not, this prompt is macOS-only — point
  the user at the Windows bridge: <https://github.com/wretcher207/reaper-agent-bridge>
- `git` must be on PATH. If missing, tell the user to install Xcode Command
  Line Tools with `xcode-select --install` and re-run when finished.
- REAPER.app must exist. If missing, ask the user where REAPER is installed.
  If they don't have it, tell them to install REAPER from
  <https://reaper.fm/download.php> first, then re-run.

## Phase 2 — Choose install location

Ask the user:

> Where do you want to install the bridge? Default is `~/reaper-agent-bridge`.
> Press enter to accept, or paste a different absolute path.

Expand `~` to `$HOME`. Call the result `$REPO` from here on.

- If `$REPO` does not exist: continue to Phase 3.
- If `$REPO` exists and is already a clone of `reaper-agent-bridge-macos`
  (`git -C $REPO remote -v` includes that name): offer to `git -C $REPO pull`
  and skip Phase 3. If pull fails, surface the error and stop.
- If `$REPO` exists and is **not** a clone of this repo: stop and ask for a
  different path. Do not overwrite something you did not create.

## Phase 3 — Clone

```bash
git clone https://github.com/wretcher207/reaper-agent-bridge-macos.git "$REPO"
```

If clone fails (network, auth, disk), surface the exact error to the user and
stop.

## Phase 4 — Install the auto-loader

```bash
cd "$REPO"
./setup/macos-install.sh
```

This writes a marker-delimited block into REAPER's `Scripts/__startup.lua`
that auto-loads the bridge on every launch. The script is idempotent — re-run
is safe.

If it errors with `REAPER resource dir not found`, ask the user to find their
REAPER resource directory (REAPER → Options → Show REAPER resource path in
Finder), then re-run as:

```bash
REAPER_RESOURCE_PATH="<path the user gave>" ./setup/macos-install.sh
```

## Phase 5 — Restart REAPER (human step)

Tell the user verbatim:

> Fully quit REAPER (Cmd+Q — not just close the window) and relaunch it.
> Reply with "ready" once REAPER is open again.

Wait for their reply before continuing. Do not assume.

## Phase 6 — Verify the bridge is alive

The bridge rewrites `bridge/heartbeat.json` every ~250ms while it's running.
Check the file's modification time, not the timestamp inside it (timezone-safe):

```bash
ls -la "$REPO/bridge/heartbeat.json"
# fresh check: mtime within the last 10 seconds
python3 -c "import os, time, sys; p='$REPO/bridge/heartbeat.json'; age = time.time() - os.path.getmtime(p) if os.path.exists(p) else 9999; print('fresh' if age < 10 else f'stale ({age:.1f}s)')"
```

Checkpoints:

- `fresh` → continue to Phase 7.
- `stale` or file missing → confirm REAPER is actually open. If yes, tell the
  user to load the bridge manually one time:
  > In REAPER: Actions → Show action list → ReaScript: Load → pick
  > `$REPO/bridge/reaper_agent_bridge.lua` → run it.

  Wait for them to confirm, then re-check. If still stale after a manual
  load, surface the contents of `~/Library/Application Support/REAPER/Scripts/__startup.lua`
  (just the managed block) and stop — something is off with REAPER's startup
  hook.

## Phase 7 — Smoke test

```bash
cd "$REPO"
./send_reaper_command.sh commands/examples/get_context.json --wait
```

The helper prints `Sent command <id>` and then the JSON reply on the next
line — parse the JSON line (it starts with `{`). Required:

- `"ok": true`
- `data.project_name` — the open project's name (string)
- `data.tracks` — an array (may be empty if the project is empty)

If `ok` is false or the command times out, surface the error to the user and
stop. Do not retry — read the error first.

## Phase 8 — Onboard the user

Pull the project name and track count out of the smoke-test response and tell
the user verbatim, filling in the values:

> Bridge is installed and live. REAPER reports project **"{project_name}"**
> with **{track_count}** track(s).
>
> Try asking me things like:
> - "add a track called Drums"
> - "set the tempo to 180 bpm"
> - "scan every FX in this project"
> - "save this control setup as a recipe called 'live-mix'"
>
> Full command surface: `$REPO/AGENTS.md`. Working JSON examples for every
> command: `$REPO/commands/examples/`. If you ever want to remove the bridge,
> ask me to run `./setup/macos-uninstall.sh`.

## Error recovery — general rule

If any phase fails, do not retry blindly and do not invent fixes. Read the
actual error, surface it to the user verbatim, propose the smallest next
step, and wait for direction. The bridge is local and reversible — every
mutating command runs inside a REAPER undo block — so the safest move on an
unclear failure is always to stop and ask.
