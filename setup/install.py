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

    python3 setup/install.py --allow-disk-writes  # let the agent render,
                                                  # capture, save, and set
                                                  # REAPER preferences
    python3 setup/install.py --no-disk-writes     # turn that back off

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


# --- the disk-writes gate (bridge_config.json: allow_risk_level_3) ----------
#
# Four commands are gated: render, capture_track_audio, save_project, and
# set_media_offline_when_inactive. They are grouped because each one writes to
# disk (a rendered file, a WAV, the .rpp, reaper.ini) and so is the one class of
# mutation REAPER's undo block cannot take back. Everything else the bridge does
# is undoable and ungated.
#
# The flag lived only in a JSON file nothing in the install path mentioned, so
# the first a user heard of it was a CAPTURE_BLOCKED error. These helpers let
# the installer ask once and write the answer.

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


def disk_writes_enabled(repo_root):
    """True / False, or None when there is no config file to read yet."""
    config = read_config(repo_root)
    if config is None:
        return None
    return config.get("allow_risk_level_3") is True


def set_disk_writes(repo_root, enabled, dry_run=False):
    """Write allow_risk_level_3, preserving every other key in the file.

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
    config["allow_risk_level_3"] = bool(enabled)
    if dry_run:
        print(f"[dry-run] would set allow_risk_level_3={str(bool(enabled)).lower()} in {path}")
        return
    write(path, json.dumps(config))
    print(f"Set allow_risk_level_3={str(bool(enabled)).lower()} in {path}")


def print_gate_status(repo_root):
    """Say where the gate stands and how to change it. Printed on every run."""
    py = "python" if platform.system() == "Windows" else "python3"
    state = disk_writes_enabled(repo_root)
    print()
    if state is True:
        print("Rendering, capture, save, and preference writes: ALLOWED.")
        print(f"  Turn back off with: {py} setup/install.py --no-disk-writes")
    else:
        if state is None:
            print("Rendering, capture, save, and preference writes: off (the")
            print("bridge writes bridge_config.json the first time it loads).")
        else:
            print("Rendering, capture, save, and preference writes: OFF.")
        print("  That means render, capture_track_audio (so any measurement),")
        print("  save_project, and set_media_offline_when_inactive are refused.")
        print("  Track, FX, and automation commands work either way. They are")
        print("  undoable; these four write to disk.")
        print(f"  Allow them with: {py} setup/install.py --allow-disk-writes")
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
    gate = p.add_mutually_exclusive_group()
    gate.add_argument("--allow-disk-writes", dest="disk_writes",
                      action="store_true", default=None,
                      help="let the agent render audio, capture a track for "
                           "measurement, save the project, and change REAPER "
                           "preferences (sets allow_risk_level_3 true)")
    gate.add_argument("--no-disk-writes", dest="disk_writes",
                      action="store_false",
                      help="refuse those four commands (the default)")
    args = p.parse_args(argv)

    resource_dir = args.resource_dir or find_resource_dir()
    if args.uninstall:
        return uninstall(resource_dir, dry_run=args.dry_run)
    # The gate flag is usable on its own: `--allow-disk-writes` with no other
    # argument still refreshes the auto-loader, which is harmless and idempotent.
    if args.disk_writes is not None:
        set_disk_writes(args.bridge_root, args.disk_writes, dry_run=args.dry_run)
    return install(args.bridge_root, resource_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
