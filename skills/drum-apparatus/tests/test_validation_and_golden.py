"""Phase 5 minors (2026-07-02 review): parse-time validation, SMF guards,
cross-lane golden rule, fatigue first-hit, kick alternation across sections."""
import random

import pytest

from drumgen.catalog import load_maps
from drumgen.groovekit import DSLError, parse_dsl, render
from drumgen.smf import write_smf


def _render(dsl, seed=1, **extra):
    d = parse_dsl(dsl)
    params = {"tempo": d["tempo"], "ppq": d["ppq"], "map": d["map"], "humanize": 0}
    params.update(extra)
    return render(d["sections"], params, random.Random(seed)), load_maps()[d["map"]], d


# ---- parse-time range validation (was raw tracebacks / silent corruption) --

BODY = "[v] bars=1 feel=mf\ngrid 16\nkick | x............... |\n"


@pytest.mark.parametrize("directive", ["@tempo 0", "@tempo -60", "@tempo 1000",
                                       "@tempo 3", "@ppq 4", "@ppq 40000"])
def test_out_of_range_directives_raise_dslerror(directive):
    with pytest.raises(DSLError):
        parse_dsl(f"{directive}\n@map RS Monarch\n{BODY}")


def test_negative_bars_raise_dslerror():
    with pytest.raises(DSLError):
        parse_dsl("@tempo 144\n[v] bars=-1 feel=mf\ngrid 16\n"
                  "kick | x............... |\n")


# ---- SMF guards -------------------------------------------------------------

def test_write_smf_rejects_out_of_range_pitch():
    with pytest.raises(ValueError):
        write_smf([{"tick": 0, "pitch": 200, "vel": 100, "dur": 60}])


def test_write_smf_rejects_sub_range_tempo():
    # The 3-byte tempo meta silently truncated below ~3.6 BPM.
    with pytest.raises(ValueError):
        write_smf([{"tick": 0, "pitch": 36, "vel": 100, "dur": 60}], tempo=2)
    with pytest.raises(ValueError):
        write_smf([], tempo=0)


# ---- golden rule runs across lanes sharing one output pitch ----------------

def test_golden_rule_across_lanes_sharing_a_pitch():
    # crash and crash_r both resolve to CRASH_R; per-lane passes used to let
    # cross-lane neighbors land at machine-gun velocity gap 1.
    dsl = ("@tempo 144\n@map RS Monarch\n[v] bars=1 feel=ff\ngrid 8\n"
           "crash   | x.x.x.x. |\n"
           "crash_r | .x.x.x.x |\n")
    evs, dm, _ = _render(dsl, seed=7)
    stream = sorted((e["tick"], e["vel"]) for e in evs if e["pitch"] == dm["CRASH_R"])
    assert len(stream) == 8
    gaps = [abs(stream[i + 1][1] - stream[i][1]) for i in range(len(stream) - 1)]
    # >=3 not 4: the rule is enforced pre-rounding; int rounding can shave 1.
    assert min(gaps) >= 3, f"machine-gun gap in {gaps}"


# ---- fatigue: no phantom recovery boost on a run's very first hit ----------

def test_run_first_hit_not_boosted_but_bar2_recovery_is():
    dsl = ("@tempo 144\n@map RS Monarch\n[v] bars=2 feel=mf\ngrid 16\n"
           "kick | xxxxxxxxxxxxxxxx |\n")
    # Run kicks now start from the double-bass band (KICK_RUN_BAND), which pins
    # a bar-start hit at the ceiling and would hide the boost this test guards.
    # Push the band down so the arithmetic is visible again — the mechanism under
    # test is the fatigue envelope, not where the band sits.
    evs, dm, d = _render(dsl, seed=1, kick_run_band=(60, 70), kick_vel_min=1)
    ppq = d["ppq"]
    kicks = sorted((e["tick"], e["vel"]) for e in evs if e["pitch"] == dm["KICK_R"])
    first = kicks[0][1]
    # band top 70 + on-beat 6 + bar-start 10 = ~86 (± small gauss). The old
    # phantom +8..10 "recovery" pushed this to ~95.
    assert first <= 92, f"first hit of the run looks recovery-boosted: {first}"
    bar2_first = next(v for t, v in kicks if t >= ppq * 4)
    # Real recovery on bar 2's first run-hit still applies (fights the decay).
    assert bar2_first >= first - 5


# ---- kick weight: David's 2026-08-27 call ----------------------------------

def test_double_bass_kicks_sit_in_the_run_band():
    """A 16th double-bass run lands 105-114, not at the section's feel center.

    David: "your kicks are just really weak — 105 to 112 at least on double
    bass parts." Before this, a plain `x` kick derived from feel (mf=88) and a
    whole run averaged ~91 with a floor in the 50s.
    """
    dsl = ("@tempo 188\n@map RS Monarch\n[v] bars=4 feel=mf\ngrid 16\n"
           "kick | xxxxxxxxxxxxxxxx |\n")
    evs, dm, _ = _render(dsl, seed=3)
    vels = [e["vel"] for e in evs if e["pitch"] == dm["KICK_R"]]
    assert len(vels) == 64
    assert min(vels) >= 105, f"limp kick in a double-bass run: min {min(vels)}"
    assert max(vels) <= 114, f"kick over David's ceiling: max {max(vels)}"
    # fatigue + weak foot still move inside the band; a flat run is the tell.
    assert len(set(vels)) >= 4, f"run is flat: {sorted(set(vels))}"


def test_weak_foot_stays_under_the_strong_foot_in_a_run():
    """Left foot is the weaker one — as a population, not per-hit.

    KICK_R/KICK_L share one pitch on Monarch, so this reads the pre-SMF roles.
    """
    from drumgen.groovekit import parse_dsl as _p, render as _r
    d = _p("@tempo 188\n@map GM Standard\n[v] bars=4 feel=mf\ngrid 16\n"
           "kick | xxxxxxxxxxxxxxxx |\n")
    dm = load_maps()["GM Standard"]
    evs = _r(d["sections"], {"tempo": d["tempo"], "ppq": d["ppq"],
                             "map": d["map"], "humanize": 0}, random.Random(5))
    right = [e["vel"] for e in evs if e["pitch"] == dm["KICK_R"]]
    left = [e["vel"] for e in evs if e["pitch"] == dm["KICK_L"]]
    assert right and left
    assert sum(left) / len(left) < sum(right) / len(right), \
        f"left foot is not the weaker one: L {sum(left)/len(left):.1f} " \
        f"R {sum(right)/len(right):.1f}"


def test_sparse_kicks_are_not_limp_either():
    """A non-run kick still clears the general floor (KICK_VEL_MIN)."""
    dsl = ("@tempo 188\n@map RS Monarch\n[v] bars=2 feel=mp\ngrid 16\n"
           "kick | x...x...x...x... |\n")
    evs, dm, _ = _render(dsl, seed=11)
    vels = [e["vel"] for e in evs if e["pitch"] == dm["KICK_R"]]
    assert min(vels) >= 96, f"limp sparse kick: {min(vels)}"


# ---- kick R/L alternation carries across section seams ----------------------

def test_kick_alternation_carries_across_sections():
    # One kick in section a (R) -> section b's single kick must be LEFT.
    # A per-section counter restarted every section at R, so both feet came
    # down right at every seam. KICK_R/KICK_L share a pitch on RS Monarch, so
    # the observable is the left-kick hat lift: a bar with a left kick lifts
    # hat_c to HH_OPEN_1 (pitch 45); a right-only bar keeps it closed (41).
    dsl = ("@tempo 144\n@map RS Monarch\n"
           "[a] bars=1 feel=mf\ngrid 16\nkick | x............... |\n"
           "[b] bars=1 feel=mf\ngrid 16\n"
           "kick  | x............... |\n"
           "hat_c | x.x.x.x.x.x.x.x. |\n")
    d = parse_dsl(dsl)
    params = {"tempo": d["tempo"], "ppq": d["ppq"], "map": d["map"], "humanize": 0}
    evs = render(d["sections"], params, random.Random(3))
    dm = load_maps()[d["map"]]
    ppq = d["ppq"]
    b_hat_pitches = {e["pitch"] for e in evs
                     if e["tick"] >= ppq * 4 and e["pitch"] != dm["KICK_R"]}
    assert b_hat_pitches == {dm["HH_OPEN_1"]}, (
        f"section b's kick was not left: hats stayed {b_hat_pitches}")
