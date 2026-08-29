"""The golden rule is the one invariant every velocity pass must hold.

No drum ever hits the same velocity twice in a row, per pitch in time order,
regardless of which other drums land in between. These tests pin that for the
shared implementation, for the humanizer, and for the follow-the-lead pass, so a
future change to any of them cannot quietly drop it.
"""
import random

import pytest

from drumgen import goldenrule as G
from drumgen.humanize import plan_humanize
from drumgen import learn as L

PPQ = 960
BAR = PPQ * 4
KIT = {24: "Kick", 28: "Snare Rimshot", 38: "Rack 1", 37: "Rack 2",
       49: "Left Crash", 54: "Right Crash", 56: "China", 47: "Large Open",
       63: "Ride Bell Tip"}


def flat_take(bars=16, vel=127):
    """A dead-flat programmed take: the input this whole module exists for."""
    notes, i = [], 0
    for b in range(bars):
        for step, pitch in ((0, 24), (0, 49), (0, 54), (2, 24), (3, 24),
                            (4, 24), (4, 54), (6, 24), (8, 28), (8, 54),
                            (10, 24), (12, 24), (12, 56), (13, 24), (14, 28),
                            (14, 49)):
            t = b * BAR + step * (PPQ // 4)
            notes.append({"index": i, "ppq": t, "end_ppq": t + 120,
                          "pitch": pitch, "velocity": vel})
            i += 1
    notes.sort(key=lambda n: (n["ppq"], n["pitch"]))
    for j, n in enumerate(notes):
        n["index"] = j
    return notes


def as_vel(notes, edits):
    v = {n["index"]: n["velocity"] for n in notes}
    for e in edits:
        v[e["index"]] = e["velocity"]
    return v


# ---------------------------------------------------------------- the rule ---

def test_violations_ignores_other_drums_in_between():
    """The trap: a kick, other drums, then the same kick velocity again."""
    notes = [
        {"index": 0, "ppq": 0, "pitch": 24, "velocity": 104},
        {"index": 1, "ppq": 240, "pitch": 49, "velocity": 116},
        {"index": 2, "ppq": 480, "pitch": 28, "velocity": 94},
        {"index": 3, "ppq": 720, "pitch": 24, "velocity": 104},   # violation
    ]
    bad = G.violations(notes, {n["index"]: n["velocity"] for n in notes})
    assert len(bad) == 1
    assert bad[0]["pitch"] == 24 and bad[0]["velocity"] == 104


def test_enforce_separates_and_stays_in_band():
    notes = [{"index": i, "ppq": i * 240, "pitch": 24, "velocity": 104}
             for i in range(12)]
    vel = G.enforce(notes, {n["index"]: n["velocity"] for n in notes},
                    {24: (99, 106)})
    assert not G.violations(notes, vel)
    assert all(99 <= v <= 106 for v in vel.values())


def test_enforce_is_deterministic():
    notes = [{"index": i, "ppq": i * 240, "pitch": 24, "velocity": 110}
             for i in range(20)]
    seed = {n["index"]: n["velocity"] for n in notes}
    assert G.enforce(notes, seed, {24: (96, 114)}) == \
           G.enforce(notes, seed, {24: (96, 114)})


def test_enforce_leaves_skipped_notes_alone_but_still_counts_them():
    """A note owned by another pass does not move, yet still separates the next."""
    notes = [{"index": 0, "ppq": 0, "pitch": 24, "velocity": 104},
             {"index": 1, "ppq": 240, "pitch": 24, "velocity": 104}]
    vel = G.enforce(notes, {0: 104, 1: 104}, {24: (99, 110)}, skip={0})
    assert vel[0] == 104
    assert vel[1] != 104


def test_band_too_narrow_is_left_alone_not_blown_open():
    notes = [{"index": i, "ppq": i * 240, "pitch": 28, "velocity": 94}
             for i in range(4)]
    vel = G.enforce(notes, {n["index"]: 94 for n in notes}, {28: (94, 94)})
    assert set(vel.values()) == {94}


# ------------------------------------------------------------ the passes ----

@pytest.mark.parametrize("amount", [0, 25, 60, 100])
def test_humanize_output_never_repeats_a_drums_velocity(amount):
    notes = flat_take()
    plan = plan_humanize(notes, PPQ, note_names=KIT, amount=amount, seed=7)
    assert not G.violations(notes, as_vel(notes, plan["edits"]))


@pytest.mark.parametrize("seed", [1, 20260828, 999])
def test_follow_lead_output_never_repeats_a_drums_velocity(seed):
    """Learn from a humanized head, carry it, and hold the rule across the join."""
    notes = flat_take(bars=16)
    boundary = 6 * BAR
    rng = random.Random(4)
    prev = {}
    for n in notes:                      # hand-humanize the first six bars
        if n["ppq"] < boundary:
            base = {24: 103, 28: 94, 49: 116, 54: 116, 56: 118}.get(n["pitch"], 104)
            v = base + rng.randint(-2, 2)
            while v == prev.get(n["pitch"]):
                v = base + rng.randint(-2, 2)
            prev[n["pitch"]] = v
            n["velocity"] = v

    prof = L.learn_profile(notes, PPQ, note_names=KIT, through_ppq=boundary)
    plan = L.plan_follow(notes, PPQ, prof, from_ppq=boundary,
                         note_names=KIT, seed=seed)
    vel = as_vel(notes, plan["edits"])
    assert not G.violations(notes, vel)          # including the bar 6/7 join
    assert plan["summary"]["golden_rule_violations"] == 0


def test_follow_lead_learns_the_weak_sixteenth_kick():
    """His kick ducks on the in-between 16th; a learned pass must carry that."""
    notes = flat_take(bars=16)
    boundary = 6 * BAR
    for n in notes:
        if n["ppq"] >= boundary:
            continue
        step = (n["ppq"] % BAR) / (PPQ / 4)
        if n["pitch"] == 24:
            n["velocity"] = 100 if step % 2 else 105
        elif n["pitch"] == 28:
            n["velocity"] = 94
        else:
            n["velocity"] = 116
    prof = L.learn_profile(notes, PPQ, note_names=KIT, through_ppq=boundary)
    strong = prof["bands"][("kick", "eighth")]
    weak = prof["bands"][("kick", "sixteenth")]
    assert weak[1] < strong[0]           # the whole weak band sits under the strong


def test_follow_lead_finds_the_boundary_itself():
    notes = flat_take(bars=12)
    for n in notes:
        if n["ppq"] < 6 * BAR:
            n["velocity"] = 100 + (n["index"] % 7)
    boundary, done = L.find_example_boundary(notes, BAR)
    assert done == 6 and boundary == 6 * BAR


def test_follow_lead_does_not_move_notes_in_the_example():
    notes = flat_take(bars=12)
    for n in notes:
        if n["ppq"] < 6 * BAR:
            n["velocity"] = 100 + (n["index"] % 7)
    prof = L.learn_profile(notes, PPQ, note_names=KIT, through_ppq=6 * BAR)
    plan = L.plan_follow(notes, PPQ, prof, from_ppq=6 * BAR, note_names=KIT)
    assert all(e["index"] >= 0 for e in plan["edits"])
    touched = {e["index"] for e in plan["edits"]}
    assert not any(n["index"] in touched for n in notes if n["ppq"] < 6 * BAR)


def test_follow_lead_flags_voices_it_had_to_guess():
    notes = flat_take(bars=12)
    for n in notes:
        if n["ppq"] < 6 * BAR:
            n["velocity"] = 100 + (n["index"] % 5)
    notes.append({"index": len(notes), "ppq": 8 * BAR, "end_ppq": 8 * BAR + 120,
                  "pitch": 63, "velocity": 127})       # a bell he never played
    prof = L.learn_profile(notes, PPQ, note_names=KIT, through_ppq=6 * BAR)
    plan = L.plan_follow(notes, PPQ, prof, from_ppq=6 * BAR, note_names=KIT)
    assert "bell" in plan["summary"]["guessed"]
