#!/usr/bin/env bash
# One-line installer for Reaper Daemon. Clones the repo and wires the bridge
# into REAPER's startup via setup/install.py. Needs bash (the shebang is bash
# and it uses `set -o pipefail`, which POSIX sh lacks) — on macOS/Linux that's
# already present; on Windows use Git Bash or WSL, or just run
# `python setup/install.py` directly from a cloned repo. Idempotent — re-run
# safely after moving the repo.
set -euo pipefail

REPO_URL="https://github.com/wretcher207/reaper-daemon.git"
INSTALL_DIR="${1:-$HOME/reaper-daemon}"

command -v git >/dev/null 2>&1 || {
  echo "error: git not found. Install it (Xcode CLT on macOS, git-scm.com elsewhere)." >&2
  exit 1
}
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "error: Python 3 not found. Install from https://www.python.org/downloads/" >&2
  exit 1
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "Updating existing clone at $INSTALL_DIR ..."
  git -C "$INSTALL_DIR" pull --ff-only
else
  echo "Cloning to $INSTALL_DIR ..."
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
"$PY" setup/install.py

cat <<EOF

Done. (Re)start REAPER, then verify the bridge is live:
  cd "$INSTALL_DIR"
  $PY reaperd.py status
  $PY reaperd.py send commands/examples/get_context.json --wait

Agent docs: $INSTALL_DIR/AGENTS.md
EOF
