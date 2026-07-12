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

After installing, (re)start REAPER, then verify the bridge is live:
    python3 reaperd.py send commands/examples/get_context.json --wait
"""

import argparse
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


def bridge_dir_for(repo_root):
    """Absolute path to this clone's bridge/ folder."""
    return os.path.join(os.path.abspath(repo_root), "bridge")


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
    bridge_dir = bridge_dir_for(repo_root)
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
    args = p.parse_args(argv)

    resource_dir = args.resource_dir or find_resource_dir()
    if args.uninstall:
        return uninstall(resource_dir, dry_run=args.dry_run)
    return install(args.bridge_root, resource_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
