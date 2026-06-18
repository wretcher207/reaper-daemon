import random
from collections import Counter
from drumgen.render import render_section, render_arrangement
from drumgen.catalog import load_maps, find_groove

BASE = dict(humanize=0, push_pull=0, velocity_mode=1, power_hand="none",
            ph_velocity=90, ph_variance=40, fills=False, fill_velocity=115,
            tempo=120, ppq=480, map_name="RS Monarch",
            bar_length_qn=4.0, step_qn=0.25, ph_spacing_qn=0.5, seed=1,
            accent_cymbal="none", accent_every_bars=1, cymbal_density=1)


def _params(**kw):
    p = dict(BASE); p.update(kw); return p


def test_kick_and_snare_histogram_matches_string():
    g = find_groove("Hammer Blast")  # kick all 16 K, snare 8 S
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 1, _params(), random.Random(1))
    pitches = Counter(e["pitch"] for e in evs)
    assert pitches[24] == 16   # kick (KICK_R==KICK_L==24 for Monarch)
    assert pitches[26] == 8    # snare


def test_ride_fires_over_kick_snare_unison():
    # Hammer Blast: every even step has kick+snare (limb=2). The ride power hand
    # should fire on ALL 8th-note grid steps, not be skipped by the limb cap.
    # The cap only suppresses hi-hats on unison steps (mud); ride over a blast
    # is the whole point.
    g = find_groove("Hammer Blast")
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 1, _params(power_hand="ride", ph_variance=0), random.Random(1))
    ride = [e for e in evs if e["pitch"] == dm["RIDE_TIP"]]
    assert len(ride) == 8  # all 8th-note grid steps fire


def test_hat_still_capped_on_unison():
    # Hi-hat power hand should still be skipped on kick+snare unison steps.
    g = find_groove("Hammer Blast")
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 1, _params(power_hand="hh_closed"), random.Random(1))
    hh = {dm["HH_CLOSED_TIP"], dm["HH_CLOSED_EDGE"]}
    hat_hits = [e for e in evs if e["pitch"] in hh]
    assert len(hat_hits) == 0  # every grid step has limb=2, all skipped


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


def test_accent_cymbal_adds_crash_on_beat_1():
    g = find_groove("Standard 16th Stream")
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 4, _params(accent_cymbal="CRASH_R"), random.Random(1))
    crashes = [e for e in evs if e["pitch"] == dm["CRASH_R"]]
    assert len(crashes) == 4  # one per bar, on beat 1 (tick 0, 1920, 3840, 5760)
    crash_ticks = sorted(e["tick"] for e in crashes)
    assert crash_ticks[0] == 0  # beat 1 of bar 1


def test_accent_cymbal_suppressed_for_breakdowns():
    # Breakdowns have their own cymbal lane; accent_cymbal should not add
    # extra crashes on top.
    g = find_groove("Chug Breakdown")
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 2, _params(accent_cymbal="CRASH_R"), random.Random(1))
    crashes = [e for e in evs if e["pitch"] == dm["CRASH_R"]]
    # Only the authored X hits (1 per bar from the cymbal lane), no accent adds
    assert len(crashes) == 2


def test_accent_every_bars_spacing():
    g = find_groove("Standard 16th Stream")
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 4, _params(accent_cymbal="CRASH_R", accent_every_bars=2),
                         random.Random(1))
    crashes = [e for e in evs if e["pitch"] == dm["CRASH_R"]]
    assert len(crashes) == 2  # bars 0 and 2 only


def test_blast_grooves_default_to_ride_from_catalog():
    # Blast grooves now carry power_hand:"ride" in the catalog, so even when
    # the global param is "hh_open", the groove's own default wins.
    g = find_groove("Hammer Blast")
    dm = load_maps()["RS Monarch"]
    evs = render_section(g, dm, 1, _params(power_hand="hh_open", ph_variance=0), random.Random(1))
    ride = [e for e in evs if e["pitch"] == dm["RIDE_TIP"]]
    assert len(ride) == 8  # ride from catalog default, not hat from param
