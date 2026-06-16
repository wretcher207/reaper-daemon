import random
from collections import Counter
from drumgen.render import render_section, render_arrangement
from drumgen.catalog import load_maps, find_groove

BASE = dict(humanize=0, push_pull=0, velocity_mode=1, power_hand="none",
            ph_velocity=90, ph_variance=40, fills=False, fill_velocity=115,
            tempo=120, ppq=480, map_name="RS Monarch",
            bar_length_qn=4.0, step_qn=0.25, ph_spacing_qn=0.5, seed=1)


def _params(**kw):
    p = dict(BASE); p.update(kw); return p


def test_kick_and_snare_histogram_matches_string():
    g = find_groove("Hammer Blast")  # kick all 16 K, snare 8 S
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 1, _params(), random.Random(1))
    pitches = Counter(e["pitch"] for e in evs)
    assert pitches[24] == 16   # kick (KICK_R==KICK_L==24 for Monarch)
    assert pitches[26] == 8    # snare


def test_power_hand_respects_two_limb_cap():
    g = find_groove("Hammer Blast")  # every step already kick+snare on even...
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 1, _params(power_hand="ride"), random.Random(1))
    # ride tip is 62; on steps already holding 2 limbs none added
    ride = [e for e in evs if e["pitch"] == 62]
    assert len(ride) <= 8  # only where limb_count < 2


def test_fill_on_last_bar_adds_toms_and_crash():
    g = find_groove("Standard 16th Stream")
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 2, _params(fills=True), random.Random(1))
    toms = {38, 37}  # TOM_1, TOM_2
    assert any(e["pitch"] in toms for e in evs)
    assert any(e["pitch"] == dm["CRASH_R"] for e in evs)


def test_arrangement_concatenates_bars():
    secs = [{"groove": "Tech Death Pulse", "bars": 2},
            {"groove": "The Pit Opener", "bars": 2}]
    evs = render_arrangement(secs, _params(fills=False))
    max_tick = max(e["tick"] for e in evs)
    # 4 bars * 4 qn * 480 ppq = 7680 ticks total span
    assert max_tick < 4 * 4 * 480
    assert max_tick >= 3 * 4 * 480  # content reaches the 4th bar
