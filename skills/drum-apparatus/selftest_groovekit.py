#!/usr/bin/env python3
"""selftest_groovekit — proves the three mandatory rules from the note stream.

Generates several DSL specs, writes MIDI, parses it back with parse_smf, and
prints concrete PASS/FAIL checks with numbers for:
  1. GOLDEN RULE  — adjacent same-lane velocity deltas all >= 4
  2. FATIGUE      — downward trend across a blast run + per-bar recovery, not
                    monotonic / not a straight line
  3. TIMING       — a fraction of hits land exactly on grid, the rest are
                    jittered; offsets are non-trivially distributed; unison
                    hits share one offset (lock)
"""

import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from drumgen.groovekit import build, build_midi, parse_dsl, FEEL_CENTER  # noqa
from drumgen.smf import parse_smf  # noqa
from drumgen.catalog import load_maps  # noqa


# ---------------------------------------------------------------------------
# DSL specs
# ---------------------------------------------------------------------------

VERSE = """
@tempo 140
@map RS Monarch
@seed 101
[verse] bars=2 feel=mp
grid 16
kick  | x . . . . . x . x . . . . . . . |
snare | . . . . X . . . . . . . X . o . |
hat_o | x . x . x . x . x . x . x . x . |
"""

CHORUS = """
@tempo 180
@map RS Monarch
@seed 202
[chorus] bars=2 feel=f
grid 32
kick    | xxxxxxxxxxxxxxxx xxxxxxxxxxxxxxxx |
snare   | . . . . . . . . X . . . . . . . . . . . . . . . X . . . . . . . |
crash_r | X . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . |
"""

BREAKDOWN = """
@tempo 90
@map RS Monarch
@seed 303
[breakdown] bars=2 feel=ff
grid 16
kick  | X . . . . . x . X . . . . . x . |
snare | . . . . X . . . . . . . X . . . |
crash | X . . . . . . . X . . . . . . . |
china | . . . . X . . . . . . . X . . . |
"""


def role_pitch(name):
    return load_maps()["RS Monarch"][name]


def notes_for_pitch(notes, pitch):
    return [n for n in notes if n["pitch"] == pitch]


def check_golden(notes, pitch, label):
    seq = sorted(notes_for_pitch(notes, pitch), key=lambda n: n["tick"])
    vels = [n["vel"] for n in seq]
    bad = []
    for a, b in zip(vels, vels[1:]):
        if abs(a - b) < 4:
            bad.append((a, b))
    ok = not bad and len(vels) >= 2
    mindelta = min((abs(a - b) for a, b in zip(vels, vels[1:])), default=0)
    status = "PASS" if ok else "FAIL"
    print(f"[GOLDEN] {status} {label}: {len(vels)} hits, "
          f"min adjacent delta={mindelta}, violations={len(bad)}")
    if bad:
        print(f"         offending pairs: {bad[:5]}")
    return ok


def check_fatigue(notes, pitch, grid, bars, label):
    """The blast kick run should trend down with a per-bar recovery and not be
    a straight monotonic line."""
    seq = sorted(notes_for_pitch(notes, pitch), key=lambda n: n["tick"])
    vels = [n["vel"] for n in seq]
    if len(vels) < 12:
        print(f"[FATIGUE] FAIL {label}: only {len(vels)} hits, need a long run")
        return False

    n = len(vels)
    first_half = statistics.mean(vels[: n // 2])
    second_half = statistics.mean(vels[n // 2:])
    trend_down = second_half < first_half

    # per-bar recovery: compare last hit of bar k to first hit of bar k+1.
    per_bar = n // bars
    recoveries = []
    for b in range(1, bars):
        last_prev = vels[b * per_bar - 1]
        first_new = vels[b * per_bar]
        recoveries.append(first_new - last_prev)
    has_recovery = any(r > 0 for r in recoveries)

    # not monotonic: there must be at least one up-step somewhere.
    ups = sum(1 for a, b in zip(vels, vels[1:]) if b > a)
    not_monotonic = ups > 0

    # not a straight line: deviation from a best-fit linear ramp is nonzero.
    xs = list(range(n))
    slope = (statistics.covariance(xs, vels) / statistics.variance(xs)
             if n > 1 else 0)
    intercept = statistics.mean(vels) - slope * statistics.mean(xs)
    resid = [vels[i] - (slope * i + intercept) for i in range(n)]
    resid_std = statistics.pstdev(resid)
    not_straight = resid_std > 1.0

    ok = trend_down and has_recovery and not_monotonic and not_straight
    status = "PASS" if ok else "FAIL"
    print(f"[FATIGUE] {status} {label}: hits={n} "
          f"mean(1st half)={first_half:.1f} mean(2nd half)={second_half:.1f} "
          f"down={trend_down}")
    print(f"          bar-recovery deltas={recoveries} (any+? {has_recovery}); "
          f"up-steps={ups} (non-monotonic? {not_monotonic}); "
          f"residual_std={resid_std:.2f} (non-linear? {not_straight})")
    return ok


def check_timing(text, label):
    """Offsets: some hits exactly on grid (offset 0), most jittered; unison
    hits share one offset (lock)."""
    events, info = build(text)
    ppq = info["ppq"]
    parsed = parse_dsl(text)
    grid = parsed["sections"][0]["grid"]
    step_qn = 4.0 / grid

    # nominal tick per event = nearest grid tick; offset = actual - nominal.
    offsets = []
    by_nominal = {}
    for e in events:
        nominal_step = round(e["tick"] / (step_qn * ppq))
        nominal_tick = int(round(nominal_step * step_qn * ppq))
        off = e["tick"] - nominal_tick
        offsets.append(off)
        by_nominal.setdefault(nominal_step, set()).add(off)

    n = len(offsets)
    exact = sum(1 for o in offsets if o == 0)
    jittered = n - exact
    spread = statistics.pstdev(offsets) if n > 1 else 0
    no_negative_ticks = all(e["tick"] >= 0 for e in events)

    # unison lock: every nominal step that has multiple simultaneous hits must
    # resolve to a SINGLE offset value.
    unison_steps = {k: v for k, v in by_nominal.items() if True}
    locked = all(len(v) == 1 for v in by_nominal.values())
    multi = [k for k, v in by_nominal.items()]

    # some exact, some jittered, real spread, all locked, no negatives.
    ok = (exact > 0 and jittered > 0 and spread > 0.5
          and locked and no_negative_ticks)
    status = "PASS" if ok else "FAIL"
    print(f"[TIMING] {status} {label}: {n} hits, exact-on-grid={exact}, "
          f"jittered={jittered}, offset_std={spread:.2f} ticks, "
          f"unison-locked={locked}, no_negative_ticks={no_negative_ticks}")
    return ok


def main():
    all_ok = True
    tmp = Path(tempfile.mkdtemp(prefix="groovekit_"))

    # Write MIDI for all three (also exercises build_midi + parse_smf roundtrip)
    specs = {"verse": VERSE, "chorus": CHORUS, "breakdown": BREAKDOWN}
    parsed_notes = {}
    for name, text in specs.items():
        data, info = build_midi(text, seed=None)  # seed comes from @seed
        path = tmp / f"{name}.mid"
        path.write_bytes(data)
        back = parse_smf(data)
        parsed_notes[name] = back["notes"]
        print(f"--- {name}: wrote {path.name}, {info['notes']} notes, "
              f"{info['bars']} bars, map={info['map']}, seed={info['seed']}; "
              f"parsed back {len(back['notes'])} notes ---")

    print()
    print("=== RULE 1: GOLDEN RULE (adjacent same-lane delta >= 4) ===")
    # verse open-hat ostinato is the strongest test (16 hits/bar).
    all_ok &= check_golden(parsed_notes["verse"], role_pitch("HH_OPEN_1"),
                           "verse hat_o (OPEN_1)")
    # open hat variance can scatter to OPEN_2/3; check those lanes too if used.
    for r in ("HH_OPEN_2", "HH_OPEN_3"):
        seq = notes_for_pitch(parsed_notes["verse"], role_pitch(r))
        if len(seq) >= 2:
            all_ok &= check_golden(parsed_notes["verse"], role_pitch(r),
                                   f"verse {r}")
    all_ok &= check_golden(parsed_notes["chorus"], role_pitch("KICK_R"),
                           "chorus kick_R (32nd blast)")
    all_ok &= check_golden(parsed_notes["chorus"], role_pitch("KICK_L"),
                           "chorus kick_L (32nd blast)")

    print()
    print("=== RULE 2: FATIGUE ENVELOPE (32nd double-bass chorus) ===")
    # Kick alternates R/L; check the RIGHT foot stream which carries the run.
    all_ok &= check_fatigue(parsed_notes["chorus"], role_pitch("KICK_R"),
                            grid=32, bars=2, label="chorus kick_R run")

    print()
    print("=== RULE 3: TIMING (offset distribution + unison lock) ===")
    all_ok &= check_timing(BREAKDOWN, "breakdown (crash+china+snare unison)")
    all_ok &= check_timing(VERSE, "verse")

    print()
    print("OVERALL:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
