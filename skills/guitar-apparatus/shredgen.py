#!/usr/bin/env python3
"""shredgen — CLI for the guitargen virtual-guitar/bass engine.

Renders a riff into a humanized Standard MIDI File (notes + mod-wheel CC) for one
part, and prints a one-line summary. The Reaper Daemon's `shred` subcommand calls
this, then inserts the file onto a named track via the bridge.

    # the built-in demo riff onto the left guitar take
    python shredgen.py --riff demo --part guitar --seed 101 --out /tmp/l.mid
    # the same riff, other seed = the double-track's right take
    python shredgen.py --riff demo --part guitar --seed 202 --out /tmp/r.mid
    # a locked bass line derived from the same riff
    python shredgen.py --riff demo --part bass --seed 303 --out /tmp/b.mid
    # the range probe that confirms where the low string sounds
    python shredgen.py --riff probe --part guitar --out /tmp/probe.mid
    # a custom riff: one 16-char bar per line
    python shredgen.py --bars-file myriff.txt --part guitar --out /tmp/x.mid
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guitargen import perform, write_smf                       # noqa: E402
from guitargen.riffs import (demo_guitar_spec, range_probe_spec,  # noqa: E402
                             make_spec, bass_from_guitar)


def _guitar_spec(args):
    if args.bars_file:
        bars = [ln.strip() for ln in Path(args.bars_file).read_text().splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
        return make_spec(bars, args.map, tempo=args.tempo)
    if args.riff == "demo":
        return demo_guitar_spec()
    if args.riff == "probe":
        return range_probe_spec()
    raise ValueError(f"unknown riff {args.riff!r} (use demo, probe, or --bars-file)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="guitargen virtual-guitar/bass engine.")
    ap.add_argument("--riff", default="demo",
                    help="built-in riff: demo | probe (ignored if --bars-file)")
    ap.add_argument("--bars-file", help="text file, one 16-char bar per line")
    ap.add_argument("--part", choices=["guitar", "bass"], default="guitar")
    ap.add_argument("--map", default="argent_e", help="guitar tuning map")
    ap.add_argument("--seed", type=int, default=0x5152)
    ap.add_argument("--low-string", type=int, default=None,
                    help="override the map's low-string MIDI note (probe result)")
    ap.add_argument("--tempo", type=int, default=120)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    try:
        gspec = _guitar_spec(args)
        if args.part == "bass":
            spec = bass_from_guitar(gspec)
            is_bass = True
            map_name = spec["map"]
        else:
            spec = gspec
            is_bass = False
            map_name = args.map
        if args.low_string is not None and "low_string_override" not in spec:
            spec["low_string_override"] = args.low_string
        events, info = perform(spec, map_name, seed=args.seed, is_bass=is_bass)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f"error: {str(exc).strip()}", file=sys.stderr)
        return 2

    data = write_smf(events, ppq=int(spec.get("ppq", 480)), tempo=args.tempo)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)

    print(f"guitargen: {info['notes']} notes | {info['ccs']} cc | "
          f"{info['bars']} bars | part={args.part} | map={info['map']} | "
          f"low_string={info['low_string']} | seed={info['seed']} | -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
