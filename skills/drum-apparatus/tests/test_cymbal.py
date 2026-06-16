import random
from collections import Counter
from drumgen.render import render_section
from drumgen.catalog import load_maps, find_groove

BASE = dict(humanize=0, push_pull=0, velocity_mode=1, power_hand="none",
            ph_velocity=90, ph_variance=40, fills=False, fill_velocity=115,
            tempo=120, ppq=480, map_name="RS Monarch",
            bar_length_qn=4.0, step_qn=0.25, ph_spacing_qn=0.5, seed=1)


def _params(**kw):
    p = dict(BASE); p.update(kw); return p


def test_cymbal_lane_places_crash_and_china():
    g = find_groove("Chug Breakdown")  # cymbal X@0 (crash), C@8 (china)
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 1, _params(), random.Random(1))
    pitches = Counter(e["pitch"] for e in evs)
    assert pitches[dm["CRASH_R"]] == 1   # X at step 0
    assert pitches[dm["CHINA_R"]] == 1   # C at step 8


def test_cymbal_lane_suppresses_grid_power_hand():
    # Even with a power_hand requested, an authored cymbal lane takes over:
    # no hi-hat grid ostinato should appear.
    g = find_groove("Chug Breakdown")
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 1, _params(power_hand="hh_open"), random.Random(1))
    hh = {dm["HH_OPEN_1"], dm["HH_OPEN_2"], dm["HH_OPEN_3"]}
    assert not any(e["pitch"] in hh for e in evs)


def test_naked_kick_chug_is_quieter_than_stab():
    g = find_groove("Chug Breakdown")  # K@0 (stab 127), k@11-14 (chug 110)
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 1, _params(), random.Random(1))
    kicks = sorted(e["tick"] for e in evs if e["pitch"] == dm["KICK_R"])
    by_tick = {e["tick"]: e["vel"] for e in evs if e["pitch"] == dm["KICK_R"]}
    stab_vel = by_tick[kicks[0]]            # step 0 = K (accent, 127)
    chug_vel = by_tick[kicks[1]]            # next = a chug k (110 base, less if left foot)
    assert stab_vel == 127 and chug_vel < stab_vel


def test_cross_bar_pattern_indexing_reaches_second_bar():
    # World Ending Stomp is a 32-step (2-bar) pattern. Bar 2 must render its own
    # half of the pattern (a stab at step 16), not restart bar 1.
    g = find_groove("World Ending Stomp")
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 2, _params(), random.Random(1))
    crashes = sorted(e["tick"] for e in evs if e["pitch"] == dm["CRASH_R"])
    # crashes authored at global steps 0 and 16 -> ticks 0 and 16*120=1920
    assert 0 in crashes
    assert 1920 in crashes
