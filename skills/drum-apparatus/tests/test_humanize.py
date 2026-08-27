"""Tests for drumgen.humanize: the after-the-fact humanize pass."""
from drumgen.catalog import load_maps
from drumgen.humanize import plan_humanize, pitch_families, FAMILY_BAND

PPQ = 960
BAR = PPQ * 4
SIXTEENTH = PPQ // 4


def _note(index, ppq, pitch, vel=127, dur=120):
    return {"index": index, "ppq": ppq, "end_ppq": ppq + dur,
            "pitch": pitch, "velocity": vel, "channel": 0}


def _flat_take():
    """A small flat-127 take on the RS Monarch pitches, incl. a stacked pair."""
    notes, i = [], 0
    # four on-the-floor kicks (pitch 24), one per beat
    for beat in range(4):
        notes.append(_note(i, beat * PPQ, 24)); i += 1
    # backbeat rimshot snare (pitch 28) on beats 2 and 4
    for beat in (1, 3):
        notes.append(_note(i, beat * PPQ, 28)); i += 1
    # eighth-note closed hats (pitch 42)
    for step in range(0, 16, 2):
        notes.append(_note(i, step * SIXTEENTH, 42)); i += 1
    # a crash (pitch 49) on the downbeat, sharing the tick with kick beat 0
    notes.append(_note(i, 0, 49)); i += 1
    # a STACKED pair: two kicks on the same (empty) offbeat tick AND pitch
    # (a double-trigger) -- reachable only by index, not by (tick,pitch)
    notes.append(_note(i, 3 * SIXTEENTH, 24)); i += 1
    notes.append(_note(i, 3 * SIXTEENTH, 24)); i += 1
    return notes


def test_flat_take_gets_dynamics_within_bands():
    notes = _flat_take()
    out = plan_humanize(notes, PPQ, map_name="RS Monarch",
                        kit_map=load_maps()["RS Monarch"], amount=25)
    edits = {e["index"]: e for e in out["edits"]}
    # every note gets a velocity edit
    assert len(edits) == len(notes)
    # nothing is left dead-flat at 127
    assert all(e["velocity"] < 127 for e in edits.values())
    # kick ceiling honoured
    for n in notes:
        if n["pitch"] == 24:
            assert edits[n["index"]]["velocity"] <= 114
    # rimshot stays in its 90-110 band
    for n in notes:
        if n["pitch"] == 28:
            assert 90 <= edits[n["index"]]["velocity"] <= 110


def test_stacked_notes_are_both_edited():
    """The whole point of index addressing: two notes on one tick+pitch both move."""
    notes = _flat_take()
    stacked = [n for n in notes if n["pitch"] == 24 and n["ppq"] == 3 * SIXTEENTH]
    assert len(stacked) == 2
    out = plan_humanize(notes, PPQ, map_name="RS Monarch",
                        kit_map=load_maps()["RS Monarch"], amount=25)
    edits = {e["index"]: e for e in out["edits"]}
    for n in stacked:
        assert n["index"] in edits          # neither skipped
        assert edits[n["index"]]["velocity"] <= 114


def test_determinism():
    notes = _flat_take()
    a = plan_humanize(notes, PPQ, map_name="RS Monarch",
                      kit_map=load_maps()["RS Monarch"], amount=30, seed=7)
    b = plan_humanize(notes, PPQ, map_name="RS Monarch",
                      kit_map=load_maps()["RS Monarch"], amount=30, seed=7)
    assert a["edits"] == b["edits"]
    c = plan_humanize(notes, PPQ, map_name="RS Monarch",
                      kit_map=load_maps()["RS Monarch"], amount=30, seed=8)
    assert a["edits"] != c["edits"]         # a different seed differs


def test_amount_zero_leaves_timing_but_still_contours_velocity():
    notes = _flat_take()
    out = plan_humanize(notes, PPQ, map_name="RS Monarch",
                        kit_map=load_maps()["RS Monarch"], amount=0)
    # amount 0 => no random spread and no timing moves...
    assert out["summary"]["position_edits"] == 0
    # ...but the dynamic contour still un-flattens 127
    assert all(e["velocity"] < 127 for e in out["edits"])


def test_timing_is_subtle_and_flams_are_not_moved():
    # give the take a flam (SNARE_FLAM = pitch 27 on RS Monarch)
    notes = _flat_take()
    notes.append(_note(len(notes), PPQ, 27))
    out = plan_humanize(notes, PPQ, map_name="RS Monarch",
                        kit_map=load_maps()["RS Monarch"], amount=40)
    moved = {e["index"]: e for e in out["edits"] if "new_ppq" in e}
    # subtle: mean move well under a 16th
    assert out["summary"]["mean_move_ticks"] < SIXTEENTH
    # the flam note is never moved
    flam_index = notes[-1]["index"]
    assert flam_index not in moved


def test_unison_lock_notes_on_one_tick_move_together():
    notes = _flat_take()
    out = plan_humanize(notes, PPQ, map_name="RS Monarch",
                        kit_map=load_maps()["RS Monarch"], amount=50)
    moved = {e["index"]: e for e in out["edits"] if "new_ppq" in e}
    # kick beat 0 (index 0) and crash (shares tick 0) must land on the same tick
    kick0 = next(n for n in notes if n["pitch"] == 24 and n["ppq"] == 0)
    crash = next(n for n in notes if n["pitch"] == 49)
    if kick0["index"] in moved and crash["index"] in moved:
        assert moved[kick0["index"]]["new_ppq"] == moved[crash["index"]]["new_ppq"]


def test_pitch_families_map_then_name_fallback():
    kit = load_maps()["RS Monarch"]
    fam = pitch_families(kit_map=kit, note_names={"99": "Woodblock"})
    assert fam[24] == "kick"
    assert fam[28] == "snare_rim"
    assert fam[49] == "crash"
    # a pitch not in the map, named, with no keyword match -> left unclassified
    assert 99 not in fam
    # a named pitch that DOES match a keyword is classified
    fam2 = pitch_families(kit_map=kit, note_names={"70": "China Choke"})
    assert fam2[70] == "china"


def _consecutive_equal_runs(notes, edits):
    """Count consecutive same-pitch hits (time order) at an identical velocity."""
    vel = {e["index"]: e["velocity"] for e in edits}
    by_pitch = {}
    for n in notes:
        by_pitch.setdefault(n["pitch"], []).append(n)
    bad = 0
    for pitch, lst in by_pitch.items():
        lst.sort(key=lambda n: n["ppq"])
        prev = None
        for n in lst:
            v = vel[n["index"]]
            if prev is not None and v == prev:
                bad += 1
            prev = v
    return bad


def test_golden_rule_no_consecutive_equal_on_same_drum():
    """The rule the whole system is built on: never the same drum at the same
    velocity twice in a row. Stress it with many hits per pitch clamped to a
    narrow, ceiling-hugging band (the crash case that broke the first cut)."""
    notes, i = [], 0
    for step in range(64):                     # 64 crashes back to back
        notes.append(_note(i, step * (PPQ // 4), 49)); i += 1
    for beat in range(32):                     # 32 four-on-floor kicks
        notes.append(_note(i, beat * PPQ, 24)); i += 1
    out = plan_humanize(notes, PPQ, map_name="RS Monarch",
                        kit_map=load_maps()["RS Monarch"], amount=25)
    assert _consecutive_equal_runs(notes, out["edits"]) == 0
    # and it holds at every amount, including 0 (the backstop is not random)
    for amt in (0, 10, 50, 100):
        o = plan_humanize(notes, PPQ, map_name="RS Monarch",
                          kit_map=load_maps()["RS Monarch"], amount=amt)
        assert _consecutive_equal_runs(notes, o["edits"]) == 0, f"amount={amt}"


def test_cymbals_do_not_all_pile_at_the_ceiling():
    notes = [_note(i, i * (PPQ // 2), 49) for i in range(40)]
    out = plan_humanize(notes, PPQ, map_name="RS Monarch",
                        kit_map=load_maps()["RS Monarch"], amount=25)
    vels = [e["velocity"] for e in out["edits"]]
    ceiling = FAMILY_BAND["crash"][2]
    at_ceiling = sum(1 for v in vels if v >= ceiling)
    assert at_ceiling <= len(vels) * 0.1        # not a pile
    assert len(set(vels)) >= 8                    # real spread


def test_every_family_has_a_band():
    for fam in set(FAMILY_BAND):
        c, lo, hi = FAMILY_BAND[fam]
        assert 1 <= lo <= c <= hi <= 127
