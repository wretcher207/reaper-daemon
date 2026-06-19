#!/usr/bin/env python3
"""groovegen — CLI for the groovekit DSL drum engine.

Reads a step-grid DSL (file or inline string), renders humanized MIDI events,
writes a Standard MIDI File, and prints a one-line summary.

Usage:
    python3 groovegen.py --dsl path/to/beat.dsl --out /tmp/beat.mid [--seed 7]
    python3 groovegen.py --spec "$(cat beat.dsl)" --out /tmp/beat.mid
"""

import argparse
import sys
from pathlib import Path

# Allow running as a script from the skill root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from drumgen.groovekit import build_midi, DSLError  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="DSL drum-MIDI engine (groovekit).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dsl", help="path to a DSL file")
    src.add_argument("--spec", help="inline DSL string")
    ap.add_argument("--out", required=True, help="output MIDI path")
    ap.add_argument("--seed", type=int, default=None,
                    help="optional RNG seed (overrides @seed in the DSL)")
    args = ap.parse_args(argv)

    try:
        if args.dsl:
            text = Path(args.dsl).read_text()
        else:
            text = args.spec
        data, info = build_midi(text, seed=args.seed)
    except (DSLError, KeyError, ValueError) as exc:
        # clean, human-readable failure — no raw traceback leaks to stderr.
        msg = str(exc).strip().strip('"').strip("'")
        print(f"error: {msg}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: cannot read DSL file: {exc}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)

    print(f"groovekit: {info['notes']} notes | {info['bars']} bars | "
          f"map={info['map']} | tempo={info['tempo']} | seed={info['seed']} | "
          f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
