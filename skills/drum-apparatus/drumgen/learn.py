"""Follow David's lead: learn a velocity profile from the part he already
humanized by hand, then carry it across the rest of the take.

THE WORKFLOW THIS SERVES
------------------------
David humanizes as he programs — he nudges each hit by ear as he places it. On a
long song he does that for the first section and then wants the same hand
carried through the rest. That is not the same job as `humanize.plan_humanize`,
which applies the SHARED taste model (the renderer's bands, the same voice on
every project). This module applies THIS take's own measured hand: whatever he
actually did in bars 1-6 is the spec, even where it disagrees with the defaults.

When they disagree, the take wins. He is the ground truth (see SKILL.md,
"Ground truth from David's own part") and a learned profile is that section of
the doc, measured live, per song.

WHAT IT LEARNS
--------------
Per (family, metric class), the observed velocity band:

  down       the bar's downbeat
  beat       another quarter-note beat
  eighth     the off-eighth
  sixteenth  the in-between 16th  <- where his kick drops ~5 for the weak hit
  offgrid    a 32nd or a nudged hit
  fill       inside a detected fill run (shaped as a ramp, not a band)

Families with no example in the humanized region (the ride he has not reached
yet) are extrapolated: the shared FAMILY_BAND anchors are shifted by the offset
between his measured level and the default level on the families he DID play, so
an unlearned voice lands in the right relationship to the ones he set rather
than at a number someone guessed. Those are reported in `summary["guessed"]` so
they can be flagged for a listen — they are inference, not his demonstrated taste.

WHAT IT DOES NOT DO
-------------------
Positions. If he only moved velocities, this only moves velocities. Pass
`timing=True` to let a learned pass nudge time as well, and only if the example
region actually shows off-grid hits.

Every plan ends in `goldenrule.enforce` and is checked with `goldenrule.violations`.
"""
import random

from .goldenrule import enforce, violations
from .humanize import (FAMILY_BAND, detect_fills, pitch_families,
                       CYMBAL_FAMILIES, SHELL_FAMILIES)

METRIC_CLASSES = ("down", "beat", "eighth", "sixteenth", "offgrid")
# Coarser fallback when a class has no example: an odd 16th behaves like an
# off-grid hit (both are the weak in-between), a beat like a downbeat.
CLASS_FALLBACK = {"down": "beat", "beat": "eighth", "eighth": "beat",
                  "sixteenth": "offgrid", "offgrid": "sixteenth"}
MIN_EXAMPLES = 2          # below this a band is too thin to trust on its own


def metric_class(ppq, bar_ticks, sixteenth):
    """Which metric slot a hit sits in. The unit David's dynamics track."""
    off = ppq % bar_ticks
    step = off / sixteenth
    if abs(step - round(step)) > 1e-6:
        return "offgrid"
    step = int(round(step))
    if step == 0:
        return "down"
    if step % 4 == 0:
        return "beat"
    if step % 2 == 0:
        return "eighth"
    return "sixteenth"


def find_example_boundary(notes, bar_ticks):
    """First ppq that is NOT hand-humanized yet.

    A bar counts as done when its velocities are not all one value. Programmed-
    flat bars are the classic dead 127 wall, but any single-value bar reads the
    same way. Returns (boundary_ppq, last_done_bar_index) or (None, 0) when the
    whole take already varies.
    """
    bars = {}
    for n in notes:
        bars.setdefault(int(n["ppq"] // bar_ticks), []).append(n["velocity"])
    if not bars:
        return None, 0
    done = 0
    for b in sorted(bars):
        if len(set(bars[b])) <= 1:
            break
        done = b + 1
    if done == 0:
        return 0, 0
    if done > max(bars):
        return None, done
    return done * bar_ticks, done


def learn_profile(notes, ppq, *, kit_map=None, note_names=None,
                  through_ppq=None, bar_ticks=None):
    """Measure David's hand in the already-humanized region.

    Returns a profile dict consumed by `plan_follow`.
    """
    sixteenth = ppq / 4.0
    bar_ticks = bar_ticks or ppq * 4
    fam = pitch_families(kit_map, note_names)
    example = [n for n in notes
               if through_ppq is None or n["ppq"] < through_ppq]

    def family_of(n):
        return fam.get(n["pitch"], "other")

    fills = detect_fills(example, fam, sixteenth)
    fill_idx = {n["index"] for run in fills for n in run}

    bands, fill_vels, pitch_vels = {}, [], {}
    for n in example:
        f = family_of(n)
        pitch_vels.setdefault(n["pitch"], []).append(n["velocity"])
        if n["index"] in fill_idx:
            fill_vels.append(n["velocity"])
            continue
        cls = metric_class(n["ppq"], bar_ticks, sixteenth)
        bands.setdefault((f, cls), []).append(n["velocity"])

    # How far his hand sits from the shared model, measured only on the
    # families he actually played. This is what carries an unlearned voice.
    seen, deltas = set(), []
    for (f, _cls), vs in bands.items():
        seen.add(f)
        if f in FAMILY_BAND:
            deltas.append(sum(vs) / len(vs) - FAMILY_BAND[f][0])
    shift = round(sum(deltas) / len(deltas)) if deltas else 0

    fill_shape, fill_families = None, set()
    if fills:
        starts = [run[0]["velocity"] for run in fills]
        ends = [run[-1]["velocity"] for run in fills]
        fill_shape = (min(starts), max(ends))
        for run in fills:
            for n in run:
                fill_families.add(family_of(n))

    return {
        "bands": {k: (min(v), max(v), len(v)) for k, v in bands.items()},
        "families_seen": sorted(seen),
        "shift": shift,
        "fill_shape": fill_shape,
        "fill_families": sorted(fill_families),
        "pitch_levels": {p: (min(v), max(v)) for p, v in pitch_vels.items()},
        "example_notes": len(example),
        "bar_ticks": bar_ticks,
        "sixteenth": sixteenth,
        "moved_off_grid": any(n["ppq"] % (sixteenth / 2) for n in example),
    }


def _band_for(profile, family, cls, guessed):
    """Learned band for (family, class), falling back outward until something
    real is found; records families that had to be extrapolated."""
    bands = profile["bands"]
    b = bands.get((family, cls))
    if b and b[2] >= MIN_EXAMPLES:
        return b[0], b[1]
    alt = bands.get((family, CLASS_FALLBACK.get(cls, cls)))
    if alt and alt[2] >= MIN_EXAMPLES:
        # A weak hit borrowed from a strong slot stays a touch under it.
        drop = 4 if cls in ("sixteenth", "offgrid") else 0
        return alt[0] - drop, alt[1] - drop
    if b:
        return b[0], b[1]
    same_family = [v for (f, _c), v in bands.items() if f == family]
    if same_family:
        return min(x[0] for x in same_family), max(x[1] for x in same_family)
    guessed.add(family)
    c, lo, hi = FAMILY_BAND.get(family, FAMILY_BAND["other"])
    s = profile["shift"]
    width = 2
    return max(lo, c + s - width), min(hi, c + s + width)


def plan_follow(notes, ppq, profile, *, from_ppq, kit_map=None, note_names=None,
                seed=20260828, timing=False):
    """Carry the learned hand across every note at or after `from_ppq`.

    Returns {"edits": [...], "summary": {...}}. Velocity only unless
    `timing=True` and the example region shows off-grid placement.
    """
    sixteenth = profile["sixteenth"]
    bar_ticks = profile["bar_ticks"]
    fam = pitch_families(kit_map, note_names)
    rng = random.Random(seed)

    def family_of(n):
        return fam.get(n["pitch"], "other")

    ordered = sorted(notes, key=lambda n: (n["ppq"], n["pitch"]))
    target = [n for n in ordered if n["ppq"] >= from_ppq]
    fills = detect_fills(target, fam, sixteenth)
    fill_pos = {}
    for run in fills:
        ticks = sorted({x["ppq"] for x in run})
        for x in run:
            fill_pos[x["index"]] = (ticks.index(x["ppq"]), len(ticks))

    guessed, vel, bands_by_pitch = set(), {}, {}
    # His own hits seed the golden rule so the boundary can't repeat either.
    for n in ordered:
        if n["ppq"] < from_ppq:
            vel[n["index"]] = n["velocity"]

    for n in target:
        f = family_of(n)
        cls = metric_class(n["ppq"], bar_ticks, sixteenth)
        lo, hi = _band_for(profile, f, cls, guessed)
        if (n["index"] in fill_pos and profile["fill_shape"]
                and f in profile.get("fill_families", [])):
            # A fill is a shape, not a band: ramp across the run the way his do.
            pos, ln = fill_pos[n["index"]]
            flo, fhi = profile["fill_shape"]
            p = (pos / (ln - 1)) if ln > 1 else 1.0
            base = flo + (fhi - flo) * p
            lo, hi = int(round(base)) - 1, int(round(base)) + 1
        if lo > hi:
            lo, hi = hi, lo
        if hi - lo < 2:            # room for the golden rule to breathe
            lo, hi = lo - 1, hi + 1
        bands_by_pitch.setdefault(n["pitch"], (lo, hi))
        b = bands_by_pitch[n["pitch"]]
        bands_by_pitch[n["pitch"]] = (min(b[0], lo), max(b[1], hi))
        vel[n["index"]] = max(1, min(127, rng.randint(lo, hi)))

    vel = enforce(ordered, vel, bands_by_pitch,
                  skip={n["index"] for n in ordered if n["ppq"] < from_ppq})
    left = violations(ordered, vel)

    edits = [{"index": n["index"], "velocity": vel[n["index"]]}
             for n in target if vel[n["index"]] != n["velocity"]]
    return {
        "edits": edits,
        "summary": {
            "example_notes": profile["example_notes"],
            "followed_notes": len(target),
            "velocity_edits": len(edits),
            "learned_slots": len(profile["bands"]),
            "families_learned": profile["families_seen"],
            "guessed": sorted(guessed),
            "fills_shaped": len(fills),
            "shift_from_default": profile["shift"],
            "golden_rule_violations": len(left),
            "golden_rule_remaining_in_example": len(
                [v for v in left if v["ppq"] < from_ppq]),
            "timing": bool(timing and profile["moved_off_grid"]),
        },
    }
