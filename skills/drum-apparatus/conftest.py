import sys
from pathlib import Path

# Make the `drumgen` package importable regardless of pytest's invocation cwd
# (so the suite runs from the repo root as well as from this skill directory).
sys.path.insert(0, str(Path(__file__).resolve().parent))
