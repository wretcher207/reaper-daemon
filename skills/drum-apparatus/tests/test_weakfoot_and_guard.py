"""#4 weak-foot drop (fast runs only) + #7 bare-focal-hit guard, live engine."""
import random

from drumgen.groovekit import parse_dsl, render, build


def _render(dsl, humanize=0, seed=1):
    d = parse_dsl(dsl)
    params = {"tempo": d["tempo"], "ppq": d["ppq"], "map": d["map"], "humanize": humanize}
    from drumgen.catalog import load_maps
    return render(d["sections"], params, random.Random(seed)), load_maps()[d["map"]]


# Two consecutive kicks (a fast run) off the beat -> same metrical weight, so the
# only velocity gap is the weak-foot drop on the 2nd (left) kick.
RUN = "@tempo 144\n@map RS Monarch\n[v] bars=1 feel=mf\ngrid 16\nkick | ..xx............ |\n"
# Isolated kicks (no adjacency) -> the alternation labels the 2nd one left, but it
# must NOT be weakened (not a real double-kick run).
ISO = "@tempo 144\n@map RS Monarch\n[v] bars=1 feel=mf\ngrid 16\nkick | x...x........... |\n"


def test_weak_foot_drops_second_kick_in_a_run():
    evs, dm = _render(RUN)
    kicks = [v for _, v in sorted((e["tick"], e["vel"]) for e in evs if e["pitch"] == dm["KICK_R"])]
    assert len(kicks) == 2
    assert kicks[1] < kicks[0]                  # left foot weaker
    assert 7 <= kicks[0] - kicks[1] <= 9        # by David's -7..-9


def test_isolated_kick_not_weakened():
    evs, dm = _render(ISO)
    kicks = sorted((e["tick"], e["vel"]) for e in evs if e["pitch"] == dm["KICK_R"])
    # second kick is on-beat (88 + 6 metrical = 94); weak-foot would drop it to ~86
    assert kicks[1][1] >= 92


GUARD_BARE = ("@tempo 144\n@map RS Monarch\n[v] bars=1 feel=mf\ngrid 16\n"
              "kick  | x............... |\n"
              "snare | ....x........... |\n")     # snare at step 4 has nothing with it
GUARD_OK = ("@tempo 144\n@map RS Monarch\n[v] bars=1 feel=mf\ngrid 16\n"
            "kick  | ....x........... |\n"
            "snare | ....x........... |\n")        # snare lands with the kick


def test_guard_flags_bare_snare():
    _, info = build(GUARD_BARE, seed=1)
    assert any("snare" in w for w in info["warnings"])


def test_guard_silent_when_accompanied():
    _, info = build(GUARD_OK, seed=1)
    assert info["warnings"] == []
