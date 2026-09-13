"""MCPB entry point for Reaper Daemon.

The bundle does not carry the bridge. It runs reaper_mcp.py from an existing
Reaper Daemon install so the inbox, outbox, recipes and helper scripts keep
the repository layout they depend on. The install folder comes from the
bundle's user_config as REAPER_DAEMON_ROOT.
"""
import os
import runpy
import sys

root = os.path.abspath(os.path.expanduser(os.environ.get("REAPER_DAEMON_ROOT", "").strip()))
server = os.path.join(root, "reaper_mcp.py")

if not os.environ.get("REAPER_DAEMON_ROOT") or not os.path.isfile(server):
    sys.stderr.write(
        "[reaper-daemon] reaper_mcp.py not found in %r. Install Reaper Daemon "
        "(https://github.com/wretcher207/reaper-daemon) and set the install "
        "folder in this extension's settings.\n" % root
    )
    sys.exit(1)

sys.argv = [server] + sys.argv[1:]
runpy.run_path(server, run_name="__main__")
