#!/usr/bin/env python3
"""install.py — wire Reaper Daemon into REAPER's startup, cross-platform.

Replaces the old setup/macos-install.sh and setup/macos-uninstall.sh. Detects
the OS, locates REAPER's per-user resource directory, and writes (or refreshes)
a marker-delimited block into Scripts/__startup.lua that auto-loads the bridge
on every REAPER launch, pointing at THIS clone. Idempotent. Re-run safely after
moving the repo.

Usage:
    python3 setup/install.py             # install / refresh the auto-loader
    python3 setup/install.py --dry-run   # preview, change nothing
    python3 setup/install.py --uninstall # remove the managed block
    python3 setup/install.py --bridge-root /path/to/clone

Four commands write to disk and are gated separately from everything else,
because undo cannot reach them. The gates:

    python3 setup/install.py --allow-audio-writes       # render + capture,
                                                        # so any measurement
    python3 setup/install.py --allow-project-save       # save over the .rpp
    python3 setup/install.py --allow-preference-writes  # REAPER's own prefs
    python3 setup/install.py --allow-disk-writes        # all three
    python3 setup/install.py --no-disk-writes           # none (the default)

Each has a --no- form, and a specific flag beats --allow-disk-writes on the
same command line: --allow-disk-writes --no-project-save means "everything
except overwriting my project".

After installing, (re)start REAPER, then verify the bridge is live:
    python3 reaperd.py send commands/examples/get_context.json --wait
"""

import argparse
import json
import os
import platform
import sys

BEGIN = "-- >>> reaper-agent-bridge (managed) >>>"
END = "-- <<< reaper-agent-bridge (managed) <<<"


def find_resource_dir():
    env = os.environ.get("REAPER_RESOURCE_PATH")
    if env:
        return env
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/REAPER")
    if system == "Windows":
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
        return os.path.join(appdata, "REAPER")
    # Linux / other Unix: REAPER uses XDG_CONFIG_HOME/REAPER (default ~/.config/REAPER).
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(xdg, "REAPER")


def block_text(bridge_dir):
    # Escape backslashes and double quotes so the path is safe inside a Lua
    # double-quoted string (matters on Windows, and for paths containing ").
    esc = bridge_dir.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f"{BEGIN}\n"
        "-- Auto-load the Reaper Daemon watcher. Managed by setup/install.py.\n"
        "do\n"
        f'  local BRIDGE_DIR = "{esc}"\n'
        '  local bridge_file = BRIDGE_DIR .. "/reaper_agent_bridge.lua"\n'
        '  local repo_root = BRIDGE_DIR:match("^(.+)[/\\\\][^/\\\\]+$") or BRIDGE_DIR\n'
        '  local lockfile = repo_root .. "/logs/bridge.lock"\n'
        "  local RENDER_LOCK_MAX_AGE = 6 * 3600\n"
        "\n"
        "  local function read_lock()\n"
        '    local f = io.open(lockfile, "r")\n'
        "    if not f then return nil end\n"
        '    local content = f:read("*a")\n'
        "    f:close()\n"
        "    -- v3.1+ writes JSON; accept the old bare epoch during upgrades.\n"
        '    local started = tonumber(content:match(\'"started"%s*:%s*(%d+)\'))\n'
        '      or tonumber(content:match("^%s*(%d+)%s*$"))\n'
        '    local busy = content:match(\'"busy"%s*:%s*"([^\"]+)"\') or "none"\n'
        "    if not started then return nil end\n"
        "    return { started = started, busy = busy }\n"
        "  end\n"
        "\n"
        "  local function lock_is_stale(lock, now)\n"
        "    if not lock then return true end\n"
        "    local age = now - lock.started\n"
        '    if lock.busy == "render" then return age > RENDER_LOCK_MAX_AGE end\n'
        "    return age >= 60\n"
        "  end\n"
        "\n"
        "  local function load_bridge()\n"
        '  local f = io.open(bridge_file, "r")\n'
        "    if f then\n"
        "      f:close()\n"
        "      REAPER_AGENT_BRIDGE_DIR = BRIDGE_DIR\n"
        "      local ok, err = pcall(dofile, bridge_file)\n"
        "      if not ok then\n"
        '        reaper.ShowConsoleMsg("[agent-bridge] startup load failed: " .. tostring(err) .. "\\n")\n'
        "      end\n"
        "    else\n"
        '      reaper.ShowConsoleMsg("[agent-bridge] startup: bridge NOT found at " .. bridge_file ..\n'
        '        " -- repo moved/renamed? re-run setup/install.py from the bridge folder.\\n")\n'
        "    end\n"
        "  end\n"
        "\n"
        "  load_bridge()\n"
        "\n"
        "  local watchdog_interval = 10\n"
        "  local watchdog_last = reaper.time_precise()\n"
        "  local function watchdog()\n"
        "    local now = reaper.time_precise()\n"
        "    if now - watchdog_last >= watchdog_interval then\n"
        "      watchdog_last = now\n"
        "      if lock_is_stale(read_lock(), os.time()) then\n"
        '        reaper.ShowConsoleMsg("[agent-bridge] watchdog: bridge stopped, restarting...\\n")\n'
        "        load_bridge()\n"
        "      end\n"
        "    end\n"
        "    reaper.defer(watchdog)\n"
        "  end\n"
        "  reaper.defer(watchdog)\n"
        "end\n"
        f"{END}\n"
    )


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# --- the write gates (bridge_config.json) -----------------------------------
#
# Four commands are gated, because each one writes to disk and so is the one
# class of mutation REAPER's undo block cannot take back. Everything else the
# bridge does is undoable and ungated.
#
# They used to share a single flag, allow_risk_level_3, which meant measuring a
# mix (capture) could not be allowed without also allowing the project file to
# be overwritten. Split three ways on 2026-09-02:
#
#   allow_audio_writes       render, capture_track_audio   (writes NEW files)
#   allow_project_save       save_project                  (overwrites the .rpp)
#   allow_preference_writes  set_media_offline_when_inactive (REAPER's own prefs)
#
# allow_risk_level_3 stays the fallback for any gate not named in the file, so
# every config written before the split behaves exactly as it did.

# gate key -> (CLI flag stem, what it covers)
GATES = (
    ("allow_audio_writes", "audio-writes",
     "render audio and capture a track (every measurement needs this)"),
    ("allow_project_save", "project-save",
     "save the project over its .rpp"),
    ("allow_preference_writes", "preference-writes",
     "change REAPER's own preferences"),
)
LEGACY_GATE = "allow_risk_level_3"


def config_path(repo_root):
    return os.path.join(os.path.abspath(repo_root), "bridge", "bridge_config.json")


def read_config(repo_root):
    """Parse bridge_config.json, or return None when it is absent/unreadable.

    Absent is normal: the bridge writes the file itself on first load.
    """
    path = config_path(repo_root)
    if not os.path.isfile(path):
        return None
    try:
        parsed = json.loads(read(path))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def gate_state(config, key):
    """Resolve one gate the way the bridge does: specific key, else fallback.

    Returns None when there is no config file at all, so "off" and "not written
    yet" stay distinguishable in what we print.
    """
    if config is None:
        return None
    specific = config.get(key)
    if isinstance(specific, bool):
        return specific
    return config.get(LEGACY_GATE) is True


def set_gates(repo_root, updates, dry_run=False):
    """Write gate keys, preserving every other key in the file.

    Mirrors the bridge's own defaults when the file does not exist yet, so a
    pre-REAPER-launch install can still answer the question.
    """
    path = config_path(repo_root)
    config = read_config(repo_root)
    if config is None:
        # The bridge re-derives bridge_root from its own location on load, so
        # this value is a starting point, not a binding one.
        config = {"bridge_root": os.path.abspath(repo_root),
                  "poll_interval_seconds": 0.25}
    for key, value in updates.items():
        config[key] = bool(value)
    written = ", ".join(f"{k}={str(bool(v)).lower()}" for k, v in updates.items())
    if dry_run:
        print(f"[dry-run] would set {written} in {path}")
        return
    write(path, json.dumps(config))
    print(f"Set {written} in {path}")


def print_gate_status(repo_root):
    """Say where each gate stands and how to change it. Printed on every run."""
    py = "python" if platform.system() == "Windows" else "python3"
    config = read_config(repo_root)
    print()
    if config is None:
        print("Write gates: all off (the bridge writes bridge_config.json the")
        print("first time it loads, so there is nothing to read yet).")
    else:
        print("Write gates:")
    for key, flag, covers in GATES:
        state = gate_state(config, key)
        label = {True: "ALLOWED", False: "off", None: "off"}[state]
        print(f"  {label:>7}  {covers}")
        if state is not True:
            print(f"           allow with: {py} setup/install.py --allow-{flag}")
        else:
            print(f"           turn off with: {py} setup/install.py --no-{flag}")
    print("  Track, FX, and automation commands work either way. They are")
    print("  undoable; these four write to disk.")
    print("  A change applies when the bridge next loads: send the")
    print("  reload_bridge command, or restart REAPER.")


def strip_block(text):
    """Remove the managed BEGIN..END block, preserve everything else.

    A BEGIN with no matching END (hand-edited startup file) aborts instead of
    silently deleting everything after BEGIN — __startup.lua may hold the
    user's own code below our block.
    """
    lines = text.splitlines()
    if BEGIN in lines and END not in lines:
        raise RuntimeError(
            "managed BEGIN marker found without its END marker in __startup.lua; "
            "refusing to rewrite (everything after BEGIN would be deleted). "
            "Restore the END marker line or remove the block by hand.")
    out = []
    skip = False
    for line in lines:
        if line == BEGIN:
            skip = True
            continue
        if line == END:
            skip = False
            continue
        if not skip:
            out.append(line)
    # Drop a single trailing blank line that removal may leave behind.
    while out and out[-1].strip() == "":
        out.pop()
        break
    return "\n".join(out) + ("\n" if out else "")


def install(repo_root, resource_dir, dry_run=False):
    bridge_dir = os.path.join(os.path.abspath(repo_root), "bridge")
    bridge_file = os.path.join(bridge_dir, "reaper_agent_bridge.lua")
    if not os.path.isdir(resource_dir):
        print(f"error: REAPER resource dir not found at: {resource_dir}", file=sys.stderr)
        print("       Set REAPER_RESOURCE_PATH and re-run.", file=sys.stderr)
        return 1
    if not os.path.isfile(bridge_file):
        print(f"error: bridge not found at {bridge_file}", file=sys.stderr)
        return 1
    scripts = os.path.join(resource_dir, "Scripts")
    startup = os.path.join(scripts, "__startup.lua")
    block = block_text(bridge_dir)

    existing = read(startup) if os.path.isfile(startup) else ""
    if BEGIN in existing:
        # Replace the existing managed block in place.
        stripped = strip_block(existing)
        new_text = stripped
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += block
    else:
        new_text = existing
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += block

    if dry_run:
        print(f"[dry-run] would write managed block to {startup}")
        print(f"[dry-run] BRIDGE_DIR = {bridge_dir}")
        print("---- block ----")
        print(block, end="")
        print("---- end block ----")
        print_gate_status(repo_root)
        return 0

    write(startup, new_text)
    if BEGIN in existing:
        print(f"Updated managed bridge block in {startup}")
    else:
        print(f"Installed bridge auto-start into {startup}")
    print()
    print(f"Done. (Re)start REAPER, then verify the bridge is live:")
    py = "python" if platform.system() == "Windows" else "python3"
    repo = os.path.abspath(repo_root)
    print(f'  cd "{repo}" && {py} reaperd.py send commands/examples/get_context.json --wait')
    print_gate_status(repo_root)
    return 0


def uninstall(resource_dir, dry_run=False):
    startup = os.path.join(resource_dir, "Scripts", "__startup.lua")
    if not os.path.isfile(startup):
        print(f"Nothing to remove: {startup} does not exist.")
        return 0
    text = read(startup)
    if BEGIN not in text:
        print(f"Nothing to remove: no managed bridge block found in {startup}.")
        return 0
    new_text = strip_block(text)
    if dry_run:
        print(f"[dry-run] would strip managed block from {startup}")
        return 0
    write(startup, new_text)
    print(f"Removed managed bridge block from {startup}")
    print()
    print("Quit REAPER fully and relaunch to finish unloading.")
    print("The bridge files in your clone are untouched — delete the clone")
    print("folder if you want to remove them too.")
    return 0


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    default_repo = os.path.dirname(here)  # setup/ -> repo root
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bridge-root", default=default_repo,
                   help=f"repo root (default: {default_repo})")
    p.add_argument("--resource-dir", default=None,
                   help="REAPER resource dir (default: auto-detect; env REAPER_RESOURCE_PATH)")
    p.add_argument("--uninstall", action="store_true",
                   help="remove the managed auto-start block")
    p.add_argument("--dry-run", action="store_true",
                   help="preview; change nothing")
    everything = p.add_mutually_exclusive_group()
    everything.add_argument("--allow-disk-writes", dest="disk_writes",
                            action="store_true", default=None,
                            help="open all three gates below at once")
    everything.add_argument("--no-disk-writes", dest="disk_writes",
                            action="store_false",
                            help="close all three (the default)")
    for key, flag, covers in GATES:
        one = p.add_mutually_exclusive_group()
        one.add_argument(f"--allow-{flag}", dest=key, action="store_true",
                         default=None, help=f"allow the agent to {covers}")
        one.add_argument(f"--no-{flag}", dest=key, action="store_false",
                         help=f"refuse to {covers}")
    args = p.parse_args(argv)

    resource_dir = args.resource_dir or find_resource_dir()
    if args.uninstall:
        return uninstall(resource_dir, dry_run=args.dry_run)
    # Gate flags are usable on their own: any of them with no other argument
    # still refreshes the auto-loader, which is harmless and idempotent.
    #
    # --disk-writes is applied first so a specific flag alongside it wins
    # ("everything except save" is one command, not two).
    updates = {}
    if args.disk_writes is not None:
        updates[LEGACY_GATE] = args.disk_writes
        for key, _flag, _covers in GATES:
            updates[key] = args.disk_writes
    for key, _flag, _covers in GATES:
        chosen = getattr(args, key)
        if chosen is not None:
            updates[key] = chosen
    if updates:
        set_gates(args.bridge_root, updates, dry_run=args.dry_run)
    return install(args.bridge_root, resource_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
