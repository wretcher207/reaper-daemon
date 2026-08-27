import sys
from pathlib import Path

# Make `import guitargen` resolve when pytest is invoked from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))
