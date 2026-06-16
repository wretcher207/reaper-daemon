#!/usr/bin/env python3
import argparse, json, sys
from drumgen.render import render_arrangement
from drumgen.smf import write_smf

DEFAULTS = dict(humanize=45, push_pull=0, velocity_mode=1, power_hand="hh_open",
                ph_velocity=90, ph_variance=40, fills=True, fill_velocity=115,
                tempo=120, ppq=480, map_name="RS Monarch",
                bar_length_qn=4.0, step_qn=0.25, ph_spacing_qn=0.5, seed=1)


def build_params(args, overrides):
    p = dict(DEFAULTS); p.update(overrides)
    if args.map: p["map_name"] = args.map
    if args.tempo: p["tempo"] = args.tempo
    if args.humanize is not None: p["humanize"] = args.humanize
    if args.push_pull is not None: p["push_pull"] = args.push_pull
    if args.power_hand: p["power_hand"] = args.power_hand
    if args.no_fills: p["fills"] = False
    if args.seed is not None: p["seed"] = args.seed
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--groove"); ap.add_argument("--bars", type=int, default=4)
    ap.add_argument("--spec"); ap.add_argument("--out", required=True)
    ap.add_argument("--map"); ap.add_argument("--tempo", type=int)
    ap.add_argument("--humanize", type=int); ap.add_argument("--push-pull", dest="push_pull", type=int)
    ap.add_argument("--power-hand", dest="power_hand")
    ap.add_argument("--no-fills", action="store_true"); ap.add_argument("--seed", type=int)
    args = ap.parse_args()

    overrides = {}
    if args.spec:
        spec = json.loads(open(args.spec).read())
        sections = spec["sections"]
        overrides = {k: v for k, v in spec.items() if k != "sections"}
    elif args.groove:
        sections = [{"groove": args.groove, "bars": args.bars}]
    else:
        print("error: pass --groove or --spec", file=sys.stderr); sys.exit(2)

    params = build_params(args, overrides)
    events = render_arrangement(sections, params)
    data = write_smf(events, ppq=params["ppq"], tempo=params["tempo"])
    with open(args.out, "wb") as f:
        f.write(data)
    bars = sum(s["bars"] for s in sections)
    print(f"wrote {args.out}: {len(events)} notes, {bars} bars, map={params['map_name']}")


if __name__ == "__main__":
    main()
