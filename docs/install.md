# Install

Requires REAPER and Python 3.8 or newer. No pip packages.

## One command

```bash
git clone https://github.com/wretcher207/reaper-daemon.git
cd reaper-daemon
python3 setup/install.py
```

Use `python` instead of `python3` on Windows. Then restart REAPER.

The installer finds REAPER's per-user resource directory and writes a marked
block into `Scripts/__startup.lua` that loads the bridge on every launch,
pointing at this clone. It is idempotent. Re-run it any time you move the repo.

| Option | Effect |
| --- | --- |
| `--dry-run` | preview, change nothing |
| `--uninstall` | remove the managed auto-start block |
| `--bridge-root /path/to/clone` | point the auto-start at a different clone |
| `REAPER_RESOURCE_PATH=/dir` (env) | override the resource directory |

## Check it

With REAPER open and a project loaded:

```bash
python3 reaperd.py status
python3 reaperd.py send commands/examples/get_context.json --wait
```

`status` reports the bridge heartbeat. `send --wait` prints a JSON description
of the open project. If it times out, check that `bridge/heartbeat.json`
exists and is fresh. REAPER can take close to a minute after launch before
startup scripts run, so a stale heartbeat in the first minute is normal.

Still wrong? See [Troubleshooting](troubleshooting.md).

## Load by hand instead

Skip the auto-start and run the bridge yourself. In REAPER:
`Actions > Show action list > ReaScript: Load…`, pick
`bridge/reaper_agent_bridge.lua`, run it. It runs as a background deferred
script and creates `bridge/bridge_config.json` and the working folders on
first run.

## Via ReaPack

1. `Extensions > ReaPack > Import repositories`
2. Paste `https://github.com/wretcher207/reaper-daemon/raw/main/index.xml`
3. `Extensions > ReaPack > Browse packages`, find **Reaper Daemon**, install.

The ReaPack package (3.22.1) contains both bridge Lua files, the two workflow
skills, and the MIDI note writer. Three things to know:

- **It does not auto-start.** ReaPack installs the bridge as an Action. Run it
  once per session, or add it to `Scripts/__startup.lua`. The installer above
  does that for you.
- **It is not the whole package.** The CLI, `commands/examples/`, and the drum
  engine in `skills/drum-apparatus/` are not in ReaPack. Clone the repo for
  those and point your agent at the clone. Audio-to-drum MIDI also needs the
  [optional transcription environment](transcription.md). ReaPack keeps the bridge and the
  bundled skills updated.
- **Point your agent at the install folder.** ReaPack installs to
  `<REAPER resource>/Scripts/reaper-daemon/`. Right-click the package in
  ReaPack and choose "Show in explorer/finder" for the exact path.
