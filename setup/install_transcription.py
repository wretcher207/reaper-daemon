#!/usr/bin/env python3
"""Explicit optional install; never runs when starting the bridge or MCP."""
from pathlib import Path
import argparse
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description="Install local drum transcription dependencies")
    parser.add_argument("--python", default=sys.executable, help="Python 3.11 executable for the separate environment")
    args = parser.parse_args()
    if not shutil.which("ffmpeg") or not shutil.which("git"):
        parser.error("Install ffmpeg and git on PATH first")
    version = subprocess.check_output([args.python, "-c", "import sys; print('%d.%d'%sys.version_info[:2])"], text=True).strip()
    if version != "3.11":
        parser.error("This dependency set uses Python 3.11. Pass --python /path/to/python3.11")
    environment = ROOT / ".venvs" / "drum-transcription"
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.exists():
        subprocess.run([args.python, "-m", "venv", str(environment)], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "setup" / "requirements-transcription.txt")], check=True)
    sys.path.insert(0, str(ROOT))
    from drum_transcription import configure
    configure(ROOT, python)
    print("Transcription is configured. Model weights download on the first separation job.")


if __name__ == "__main__":
    main()
