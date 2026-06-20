# Install Prompt: Reaper Daemon (cross-platform)

**For humans:** paste this entire file into a coding agent that runs on your
machine with shell access (Claude Code, Codex CLI, Cursor's terminal agent,
etc.). The agent will install the bridge end-to-end and verify it works. You
only have to restart REAPER once when it asks. Works on macOS, Windows, and
Linux.

**For the agent:** the rest of this file is for you. Do the phases in order.
Each phase ends with a checkpoint — do not move on until you confirm the
checkpoint. If anything fails, stop and report the exact error to the user
instead of pushing forward. Use `python3` on macOS/Linux and `python` on
Windows (treat them as interchangeable below).

---

## Phase 0 — Capability check

Confirm you can:

- Execute shell commands.
- Read and write files.
- Pause mid-task and wait for a user reply.

If any of these are missing, stop now and tell the user verbatim:

> I can't drive this install end-to-end from here. Open the install prompt in
> Claude Code or another agent with shell access on your machine and paste it
> there.

## Phase 1 — Preflight

Run these and read the output:

```bash
uname -s            # macOS/Linux. On Windows the agent already knows its OS.
command -v git || echo "missing"
command -v python3 || command -v python || echo "missing"
```

On Windows, `uname -s` won't exist — that's fine; skip it and confirm `git`
and `python` are on PATH.

Checkpoints:

- `git` must be on PATH. If missing, tell the user to install it (Xcode CLT on
  macOS via `xcode-select --install`; from git-scm.com on Windows/Linux) and
  re-run when finished.
- Python 3.8+ must be on PATH. If missing, point the user at
  <https://www.python.org/downloads/> and stop.
- REAPER must be installed. Ask the user where if you can't find it
  (`/Applications/REAPER.app` on macOS, `C:\Program Files\REAPER (x64)\REAPER.exe`
  on Windows). If they don't have it, tell them to install REAPER from
  <https://reaper.fm/download.php> first, then re-run.

## Phase 2 — Choose install location

Ask the user:

> Where do you want to install the bridge? Default is `~/reaper-daemon`.
> Press enter to accept, or paste a different absolute path.

Expand `~` to `$HOME` (or `%USERPROFILE%` on Windows). Call the result `$REPO`
from here on.

- If `$REPO` does not exist: continue to Phase 3.
- If `$REPO` exists and is already a clone of `reaper-daemon`
  (`git -C $REPO remote -v` includes that name): offer to `git -C $REPO pull`
  and skip Phase 3. If pull fails, surface the error and stop.
- If `$REPO` exists and is **not** a clone of this repo: stop and ask for a
  different path. Do not overwrite something you did not create.

## Phase 3 — Clone

```bash
git clone https://github.com/wretcher207/reaper-daemon.git "$REPO"
```

If clone fails (network, auth, disk), surface the exact error to the user and
stop.

## Phase 4 — Install the auto-loader

```bash
cd "$REPO"
python3 setup/install.py
```

This detects the OS, finds REAPER's per-user resource directory, and writes a
marker-delimited block into `Scripts/__startup.lua` that auto-loads the bridge
on every launch. Idempotent — re-run is safe.

If it errors with `REAPER resource dir not found`, ask the user to find their
REAPER resource directory (REAPER → Options → Show REAPER resource path in
Finder/Explorer), then re-run as:

```bash
REAPER_RESOURCE_PATH="<path the user gave>" python3 setup/install.py
```

## Phase 5 — Restart REAPER (human step)

Tell the user verbatim:

> Fully quit REAPER (Cmd+Q on macOS, File → Quit on Windows/Linux — not just
> close the window) and relaunch it. Reply with "ready" once REAPER is open
> again.

Wait for their reply before continuing. Do not assume.

## Phase 6 — Verify the bridge is alive

```bash
cd "$REPO"
python3 reaperd.py status
```

Checkpoints:

- `CONNECTED` → continue to Phase 7.
- `DEAD` (REAPER not running) → confirm REAPER is actually open; if not, ask
  the user to launch it. If it is open and still DEAD, tell the user to load
  the bridge manually one time:
  > In REAPER: Actions → Show action list → ReaScript: Load → pick
  > `$REPO/bridge/reaper_agent_bridge.lua` → run it.
  Wait for them to confirm, then re-check.
- `STALE` (heartbeat old) → surface the heartbeat age and the contents of the
  managed block in REAPER's `Scripts/__startup.lua`, then stop — something is
  off with the startup hook.

## Phase 7 — Smoke test

```bash
cd "$REPO"
python3 reaperd.py send commands/examples/get_context.json --wait
```

The reply is a single JSON object. Required:

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
> - "discover the drum map on my Drums track and save it as MyKit"
>
> Full command surface: `$REPO/AGENTS.md`. The agent CLI: `python3 reaperd.py --help`.
> Working JSON examples for every command: `$REPO/commands/examples/`. If you
> ever want to remove the bridge, ask me to run `python3 setup/install.py --uninstall`.

## Error recovery — general rule

If any phase fails, do not retry blindly and do not invent fixes. Read the
actual error, surface it to the user verbatim, propose the smallest next
step, and wait for direction. The bridge is local and reversible — every
mutating command runs inside a REAPER undo block — so the safest move on an
unclear failure is always to stop and ask.
