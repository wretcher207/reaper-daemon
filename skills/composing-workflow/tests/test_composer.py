import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from composer import artic, cymbals  # noqa: E402

BPM = 140


def test_parse_requires_whole_bars():
    import pytest
    with pytest.raises(ValueError):
        artic.parse("3pm 2pm")


def test_palm_mute_holds_to_next_pick_not_stabby():
    ev, _ = artic.render("4pm3 4pm3 4pm3 4pm3", instrument="argent", bpm=BPM, jitter_ms=0)
    played = [e for e in ev if not e.get("ks")]
    ms = BPM / 60000.0
    for e in played[:-1]:
        assert e["duration"] > 0.9 - 15 * ms    # quarter slot minus ~12 ms
    assert [e["pitch"] for e in ev if e.get("ks")] == [artic.ARGENT_KS["mute"]]


def test_pm_depth_is_velocity_band():
    ev, _ = artic.render("16pm1", instrument="argent", bpm=BPM)
    v = [e["velocity"] for e in ev if not e.get("ks")][0]
    lo, hi = artic.PM_DEPTH[1]
    assert lo <= v <= hi


def test_no_accidental_overlap_but_legato_overlaps():
    ev, _ = artic.render("1pc 1pc 2pc 4lg 4sus:F 4pm", instrument="argent", bpm=BPM, jitter_ms=8, seed=3)
    played = [e for e in ev if not e.get("ks")]
    for a, b in zip(played, played[1:]):
        if a["tag"] == "lg":
            assert a["time"] + a["duration"] > b["time"]
        else:
            assert a["time"] + a["duration"] < b["time"]


def test_pinch_and_rake_resend_ks0_with_velocity():
    ev, _ = artic.render("4sus 4ph 4rk 4sus", instrument="argent", bpm=BPM)
    ks = [(e["pitch"], e["velocity"]) for e in ev if e.get("ks")]
    assert ks == [(0, 64), (0, 127), (0, 120), (0, 64)]


def test_bass_resends_finger_after_ghost_and_respects_floor():
    ev, _ = artic.render("4f 4gh 8f", instrument="umansky", bpm=BPM, root=37)
    assert [e["pitch"] for e in ev if e.get("ks")] == [0, 3, 0]
    import pytest
    with pytest.raises(ValueError):
        artic.render("16f", instrument="umansky", bpm=BPM, root=30)


def test_note_fold_at_or_above_root():
    assert artic.pitch_of(28, "E") == 28
    assert artic.pitch_of(28, "D") == 38
    assert artic.pitch_of(28, "F", -12) == 17


def test_double_takes_differ():
    a, _ = artic.render("4pc 4pm 4pm 4pc", instrument="argent", bpm=BPM, seed=1)
    b, _ = artic.render("4pc 4pm 4pm 4pc", instrument="argent", bpm=BPM, seed=2, lean_ms=2)
    assert [(e["time"], e["velocity"]) for e in a] != [(e["time"], e["velocity"]) for e in b]


KIT = {r: 40 + i for i, r in enumerate(
    ["hat_tip", "hat_edge", "hat_open", "hat_pedal", "ride_tip", "ride_bell", "ride_crash",
     "crash_l", "crash_l_choke", "crash_r", "crash_r_choke", "china_l", "china_r",
     "stack", "splash", "big_crash"])}


def test_rotate_changes_cymbal_per_phrase_and_leaves_fill_room():
    H = cymbals.rotate(["hat_closed", "ride", "china"], 4, 12, fill_steps=8)
    roles = [{r for b, s, r, _ in H if b // 4 == ph} for ph in range(3)]
    assert roles[0] != roles[1] != roles[2]
    assert all(s < 8 for b, s, r, _ in H if b == 11)


def test_accents_follow_guitar_and_golden_rule_zero():
    toks = "3pc! 2pm3 1pm2 3pc!:F 2pm4 1st 4lr:G"
    acc = cymbals.accents_from_tokens(toks, ["splash", "crash_r"], tags={"pc"})
    assert [(b, s) for b, s, _, _ in acc] == [(0, 6)]
    H = cymbals.rotate(["ride"], 4, 4) + cymbals.downbeats([0], ["big_crash"]) + acc \
        + cymbals.choke(3, 4, "crash_r", "crash_r_choke")
    notes = cymbals.realize(H, KIT, bpm=BPM, swing=0.6, seed=5)
    assert min(n["velocity"] for n in notes if n["pitch"] == KIT["big_crash"]) >= 108
    assert cymbals.golden_rule(notes) == 0
