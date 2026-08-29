"""THE GOLDEN RULE — no drum ever hits the same velocity twice in a row.

    A real drummer never strikes the same drum at the exact same velocity on two
    consecutive hits. Identical velocities are the machine-gun tell, and one
    audible pair is enough to give a whole part away as programmed.

The rule is **per drum, in time order, across the whole take**. Other drums
landing in between are irrelevant: a kick at 104, then a crash, a snare and
another crash, then a kick at 104 again is a violation. This is the single
most-often-missed part of the rule, and it is the reason this module exists as
its own importable thing instead of a loop buried inside one humanizer.

WHY THIS FILE EXISTS
--------------------
`humanize.plan_humanize` has always enforced the rule. But any code path that
writes velocities and does NOT go through it — a hand-rolled pass for one take,
a section re-cut, a quick fix through `set_note_velocities` — silently skips it.
That happened on the stigmergy Monarch pass (2026-08-28): a bespoke velocity
model matched David's bands exactly and still shipped 66 violations, because it
never touched the humanizer.

So: **every velocity pass, generated or hand-rolled, ends with `enforce()`, and
`violations()` is the check to run before shipping the write.** No exceptions.

    from drumgen.goldenrule import enforce, violations
    vel = enforce(notes, vel, bands)
    assert not violations(notes, vel)

Both functions take `notes` as `get_midi_notes` rows (dicts with at least
`index`, `ppq`, `pitch`) and `vel` as `{index: velocity}`. Both are pure.
"""

RULE = ("No drum ever hits the same velocity twice in a row, per pitch in time "
        "order, no matter which other drums land in between.")

# Groove hits get real daylight between repeats; a 1-unit nudge is only the
# hard floor for notes whose shape is owned by another pass (a fill crescendo),
# where a bigger move would invert the build.
DEFAULT_MIN_GAP = 3
HARD_FLOOR_GAP = 1


def by_pitch(notes):
    """{pitch: [note, ...]} with each drum's hits in time order."""
    out = {}
    for n in notes:
        out.setdefault(n["pitch"], []).append(n)
    for lst in out.values():
        lst.sort(key=lambda n: (n["ppq"], n["index"]))
    return out


def violations(notes, vel, min_gap=HARD_FLOOR_GAP):
    """Every consecutive same-pitch pair closer together than `min_gap`.

    Returns [{pitch, prev_index, index, prev_ppq, ppq, prev_velocity,
    velocity}]. Empty list means the take satisfies the rule. Run this on the
    planned velocities BEFORE writing, and on the take re-read AFTER.
    """
    bad = []
    for pitch, lst in by_pitch(notes).items():
        prev = None
        for n in lst:
            v = vel.get(n["index"], n.get("velocity"))
            if v is None:
                continue
            if prev is not None and abs(v - prev[1]) < min_gap:
                bad.append({"pitch": pitch,
                            "prev_index": prev[0]["index"], "index": n["index"],
                            "prev_ppq": prev[0]["ppq"], "ppq": n["ppq"],
                            "prev_velocity": prev[1], "velocity": v})
            prev = (n, v)
    return bad


def enforce(notes, vel, bands=None, min_gap=HARD_FLOOR_GAP, skip=None,
            default_band=(1, 127)):
    """Return a copy of `vel` with the golden rule satisfied.

    bands: {pitch: (lo, hi)} — the velocity band that drum is allowed to move
        inside, so a separation never breaks the part's dynamic shape. A band
        narrower than `min_gap` is left alone rather than blown open.
    skip: indices whose value is owned by another pass (fill crescendo notes).
        They are not moved, but they still count as `prev` for the hit after,
        so a groove hit following a fill is still separated from it.

    The separation magnitude is derived from the note index, not random, so a
    forced fix does not settle into a mechanical two-value sawtooth, and the
    same input always produces the same output.
    """
    vel = dict(vel)
    skip = skip or set()
    for pitch, lst in by_pitch(notes).items():
        lo, hi = (bands or {}).get(pitch, default_band)
        if hi - lo < min_gap:
            continue
        prev = None
        for n in lst:
            v = vel.get(n["index"], n.get("velocity"))
            if v is None:
                continue
            if n["index"] in skip:
                prev = v
                continue
            if prev is not None and abs(v - prev) < min_gap:
                mag = min_gap + (n["index"] * 2654435761 + pitch) % 5
                v2 = _clamp(prev + mag if v >= prev else prev - mag, lo, hi)
                if abs(v2 - prev) < min_gap:
                    # Clamping collapsed the gap (both pinned at a bound) —
                    # push the other way, still inside the band.
                    v2 = _clamp(prev - mag if prev >= hi - min_gap else prev + mag,
                                lo, hi)
                v = v2
                vel[n["index"]] = v
            prev = v
    return vel


def _clamp(v, lo, hi):
    return int(round(max(lo, min(hi, v))))


def report(notes, vel, note_names=None):
    """One human-readable line per violation, for a pre-ship check."""
    names = {int(k): v for k, v in (note_names or {}).items()}
    lines = []
    for b in violations(notes, vel):
        lines.append("%-16s ppq %d -> %d  both v%d"
                     % (names.get(b["pitch"], "pitch %d" % b["pitch"]),
                        b["prev_ppq"], b["ppq"], b["velocity"]))
    return lines
