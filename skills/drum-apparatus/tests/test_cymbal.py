import random
from collections import Counter
from drumgen.render import render_section
from drumgen.catalog import load_maps, find_groove

BASE = dict(humanize=0, push_pull=0, velocity_mode=1, power_hand="none",
            ph_velocity=90, ph_variance=40, fills=False, fill_velocity=115,
            tempo=120, ppq=480, map_name="RS Monarch",
            bar_length_qn=4.0, step_qn=0.25, ph_spacing_qn=0.5, seed=1,
            accent_cymbal="none", accent_every_bars=1, cymbal_density=1)


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


def test_cymbal_density_doubles_hits():
    # Chug Breakdown has X at step 0 (crash) and C at step 8 (china).
    # cymbal_density=2 should double each: repeat at 8th-note offset (step 2, step 10).
    g = find_groove("Chug Breakdown")
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 1, _params(cymbal_density=2), random.Random(1))
    crashes = sorted(e["tick"] for e in evs if e["pitch"] == dm["CRASH_R"])
    chinas = sorted(e["tick"] for e in evs if e["pitch"] == dm["CHINA_R"])
    assert len(crashes) == 2  # steps 0 and 2
    assert len(chinas) == 2   # steps 8 and 10
    assert crashes[1] - crashes[0] == 2 * 120  # 8th note = 2 steps * 120 ticks
    assert chinas[1] - chinas[0] == 2 * 120


def test_cymbal_density_decay_reduces_repeat_velocity():
    # Power hit should be louder than the ring/choke repeat.
    g = find_groove("Chug Breakdown")
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 1, _params(cymbal_density=2, cymbal_decay=0.72), random.Random(1))
    crashes = sorted((e["tick"], e["vel"]) for e in evs if e["pitch"] == dm["CRASH_R"])
    power_vel = crashes[0][1]
    repeat_vel = crashes[1][1]
    assert power_vel > repeat_vel, f"power {power_vel} should exceed repeat {repeat_vel}"


def test_cymbal_louder_when_landing_with_a_shell():
    # Crash at step 0 lands with a kick; crash at step 8 lands alone. The
    # with-shell strike must be louder (CYMBAL_SHELL_BOOST).
    g = {"kick": "x" + "-" * 15, "snare": "-" * 16, "cymbal": "X" + "-" * 7 + "X" + "-" * 7}
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 1, _params(), random.Random(1))
    crashes = sorted((e["tick"], e["vel"]) for e in evs if e["pitch"] == dm["CRASH_R"])
    with_shell, alone = crashes[0][1], crashes[1][1]
    assert with_shell > alone


def test_hihat_closed_is_softer_than_open():
    # Same groove, closed vs open power hand. Closed hats play softer than open.
    g = {"kick": "x" + "-" * 7 + "x" + "-" * 7, "snare": "-" * 4 + "x" + "-" * 7 + "x" + "-" * 3}
    dm = load_maps()["RS Monarch"]
    closed = render_section(g, dm, 1, _params(power_hand="hh_closed"), random.Random(1))
    opn = render_section(g, dm, 1, _params(power_hand="hh_open"), random.Random(1))
    c = [e["vel"] for e in closed if e["pitch"] in (dm["HH_CLOSED_TIP"], dm["HH_CLOSED_EDGE"])]
    o = [e["vel"] for e in opn if e["pitch"] in (dm["HH_OPEN_1"], dm["HH_OPEN_2"], dm["HH_OPEN_3"])]
    assert c and o
    assert max(c) < min(o)  # closed ~0.8*90 well below open ~90


def test_china_breakdown_groove_exists_and_china_dominant():
    g = find_groove("China Breakdown")
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 4, _params(), random.Random(1))
    pitches = Counter(e["pitch"] for e in evs)
    # 4 bars * 2 china hits per bar = 8; no crash should appear
    assert pitches[dm["CHINA_R"]] == 8
    assert pitches[dm["CRASH_R"]] == 0
