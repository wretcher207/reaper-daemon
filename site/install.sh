#!/usr/bin/env bash
# install.sh — Reaper Daemon one-shot installer for macOS.
#
# Pipe it from the landing page:
#   curl -fsSL https://reaper-daemon.netlify.app/install.sh | bash
#
# Or download and run locally:
#   curl -fsSLO https://reaper-daemon.netlify.app/install.sh
#   bash install.sh
set -euo pipefail

REPO_URL="https://github.com/wretcher207/reaper-agent-bridge-macos.git"
DEFAULT_DIR="$HOME/reaper-agent-bridge"

bold() { printf '\033[1m%s\033[0m' "$1"; }
dim()  { printf '\033[2m%s\033[0m' "$1"; }
red()  { printf '\033[31m%s\033[0m' "$1"; }

abort() {
  printf '\n%s %s\n' "$(red 'error:')" "$1" >&2
  exit 1
}

# When piped from curl, our stdin is the script itself — read user prompts
# from the controlling tty instead.
if [[ -t 0 ]]; then
  TTY_FD=0
else
  exec 3</dev/tty 2>/dev/null || abort "Need a controlling terminal to prompt for input. Download install.sh and run it from an interactive shell."
  TTY_FD=3
fi

printf '\n%s\n' "$(bold 'Reaper Daemon — installer')"
printf '%s\n\n' "$(dim 'macOS · REAPER · Dead Pixel Design')"

# Phase 1 — preflight
[[ "$(uname -s)" == "Darwin" ]] || abort "Reaper Daemon is macOS-only. Windows: https://github.com/wretcher207/reaper-agent-bridge"
command -v git >/dev/null 2>&1 || abort "git not found. Install Xcode Command Line Tools first: xcode-select --install"
[[ -d /Applications/REAPER.app ]] || abort "REAPER.app not found at /Applications. Install REAPER from https://reaper.fm/download.php first."
command -v python3 >/dev/null 2>&1 || abort "python3 not found. It ships with macOS — try: xcode-select --install"

# Phase 2 — pick install location
printf 'Install location [%s]: ' "$DEFAULT_DIR"
IFS= read -r -u "$TTY_FD" INSTALL_DIR || INSTALL_DIR=""
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_DIR}"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

# Phase 3 — clone or pull
if [[ -d "$INSTALL_DIR" ]]; then
  if [[ -d "$INSTALL_DIR/.git" ]] && git -C "$INSTALL_DIR" remote -v 2>/dev/null | grep -q "reaper-agent-bridge-macos"; then
    printf '%s ' "$(dim 'Updating existing clone…')"
    git -C "$INSTALL_DIR" pull --ff-only >/dev/null 2>&1 || abort "git pull failed in $INSTALL_DIR. Resolve manually and re-run."
    echo "done"
  else
    abort "$INSTALL_DIR exists and is not a clone of reaper-agent-bridge-macos. Pick a different path or remove it first."
  fi
else
  printf '%s ' "$(dim "Cloning into $INSTALL_DIR…")"
  git clone --quiet "$REPO_URL" "$INSTALL_DIR" || abort "git clone failed. Check your network."
  echo "done"
fi

# Phase 4 — wire up REAPER auto-start
printf '%s ' "$(dim 'Installing REAPER auto-loader…')"
( cd "$INSTALL_DIR" && ./setup/macos-install.sh ) >/dev/null || abort "setup/macos-install.sh failed. Run it directly to see the error: cd $INSTALL_DIR && ./setup/macos-install.sh"
echo "done"

# Phase 5 — user restarts REAPER
cat <<EOF

$(bold 'Restart REAPER now.')
Quit fully (Cmd+Q), relaunch, then press Enter.
EOF
read -r -u "$TTY_FD" _ || true

# Phase 6 — verify heartbeat is fresh
HEARTBEAT="$INSTALL_DIR/bridge/heartbeat.json"
printf '%s ' "$(dim 'Verifying bridge heartbeat…')"
fresh=0
for _ in 1 2 3 4 5; do
  if [[ -f "$HEARTBEAT" ]]; then
    age=$(python3 -c "import os,time; print(int(time.time() - os.path.getmtime('$HEARTBEAT')))" 2>/dev/null || echo 9999)
    if [[ "$age" -lt 10 ]]; then
      fresh=1; break
    fi
  fi
  sleep 1
done
if [[ "$fresh" -ne 1 ]]; then
  abort "Bridge heartbeat is stale or missing.
Confirm REAPER is open. If it still won't connect, load the bridge once manually:
  REAPER → Actions → Show action list → ReaScript: Load… → $INSTALL_DIR/bridge/reaper_agent_bridge.lua"
fi
echo "ok"

# Phase 7 — smoke test
printf '%s ' "$(dim 'Smoke test (get_context)…')"
if ! result=$("$INSTALL_DIR/send_reaper_command.sh" "$INSTALL_DIR/commands/examples/get_context.json" --wait 2>&1); then
  echo
  abort "Smoke test command failed:
$result"
fi
# send_reaper_command.sh prints "Sent command <id>" before the JSON reply —
# isolate the JSON line (the compact reply starts with '{').
reply=$(printf '%s\n' "$result" | grep -m1 '^{' || true)
if [[ -z "$reply" ]] || ! printf '%s' "$reply" | grep -q '"ok":true'; then
  echo
  abort "Smoke test returned not-ok:
$result"
fi
echo "ok"

# Phase 8 — done. get_context returns project_name + tracks at the top of data.
project=$(printf '%s' "$reply" | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('data',{}).get('project_name') or 'unknown')
except Exception: print('unknown')" 2>/dev/null || echo "unknown")
tracks=$(printf '%s' "$reply" | python3 -c "import sys,json
try: print(len(json.load(sys.stdin).get('data',{}).get('tracks',[])))
except Exception: print('?')" 2>/dev/null || echo "?")

cat <<EOF

$(bold 'Reaper Daemon is in.')
Project: $project  ·  tracks: $tracks

Talk to your AI agent in any directory now:
  $(dim '"add a track called Drums"')
  $(dim '"scan every FX in this project"')
  $(dim '"set tempo to 180 bpm"')

Bridge:    $INSTALL_DIR
Commands:  $INSTALL_DIR/AGENTS.md
Remove:    $INSTALL_DIR/setup/macos-uninstall.sh

EOF
