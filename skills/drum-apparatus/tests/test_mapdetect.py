"""Tests for drumgen.mapdetect — auto-discovery of a drum library's note map.

These cover the major naming conventions (GGD, Superior Drummer, EZdrummer,
Addictive Drums, GM) so the classifier stays honest as libraries change.
"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drumgen.mapdetect import match_roles, classify  # noqa: E402
from drumgen.catalog import ROLE_KEYS  # noqa: E402


def test_classify_basic_families():
    assert classify("Kick") == ("kick", None, None)
    assert classify("Snare (Ghost)") == ("snare", "ghost", None)
    assert classify("Snare Rimshot") == ("snare", "rim", None)
    assert classify("Hi-Hat Closed") == ("hat", "closed", None)
    assert classify("Open Hat 1") == ("hat", "open", None)
    assert classify("Pedal Hat") == ("hat", "pedal", None)
    assert classify("Ride Bell") == ("ride", "bell", None)
    assert classify("Ride Bow") == ("ride", "tip", None)
    assert classify("Ride Edge") == ("ride", "crash", None)
    assert classify("Tom 1") == ("tom", None, 1)
    assert classify("Floor Tom") == ("tom", None, 4)
    assert classify("China") == ("china", None, None)
    assert classify("Cowbell") == ("bell", None, None)


def test_ggd_style_map():
    notes = {
        36: "Kick", 38: "Snare", 40: "Snare Rim", 37: "Snare Ghost",
        48: "Tom 1", 47: "Tom 2", 45: "Tom 3", 43: "Floor Tom",
        42: "Closed Hat", 46: "Open Hat", 44: "Pedal Hat",
        49: "Crash 1", 57: "Crash 2", 52: "China", 51: "Ride", 53: "Ride Bell",
        55: "Splash", 59: "Ride Edge",
    }
    m, rep = match_roles(notes)
    assert rep["complete"], rep
    assert m["KICK_R"] == 36 and m["SNARE"] == 38
    assert m["SNARE_RIM"] == 40 and m["SNARE_GHOST"] == 37
    assert m["TOM_1"] == 48 and m["TOM_2"] == 47 and m["TOM_3"] == 45 and m["TOM_4"] == 43
    assert m["HH_CLOSED_TIP"] == 42 and m["HH_OPEN_1"] == 46 and m["HH_PEDAL"] == 44
    assert m["CRASH_R"] == 49 and m["CRASH_L"] == 57
    assert m["CHINA_R"] == 52 and m["RIDE_TIP"] == 51 and m["RIDE_BELL"] == 53
    assert m["RIDE_CRASH"] == 59 and m["SPLASH_R"] == 55


def test_sparse_map_falls_back():
    # A kit that only names kick, snare, one open hat, one crash, one china.
    # Articulation sub-roles must still resolve via fallback so the engine
    # never KeyErrors. Roles with no chain to a real piece stay absent.
    notes = {36: "Kick", 38: "Snare", 46: "Open Hat", 49: "Crash", 52: "China"}
    m, rep = match_roles(notes)
    assert rep["complete"], rep
    assert m["KICK_R"] == 36 and m["SNARE"] == 38 and m["HH_OPEN_1"] == 46
    assert m["KICK_L"] == 36
    assert m["SNARE_GHOST"] == 38 and m["SNARE_FLAM"] == 38 and m["SNARE_RIM"] == 38
    assert m["HH_OPEN_2"] == 46 and m["HH_OPEN_3"] == 46
    assert m["CHINA_L"] == 52
    assert m["STACK"] == 52                       # falls back to CHINA_R
    assert m["CRASH_L"] == 49 and m["BIG_CRASH"] == 49
    # No closed hat, no toms, no ride -> those have no chain and stay absent.
    assert "HH_CLOSED_TIP" not in m
    assert "TOM_1" not in m
    assert "RIDE_TIP" not in m


def test_unnumbered_toms_ranked_by_pitch():
    # Higher pitch = TOM_1, descending. Names carry no numbers.
    notes = {
        36: "Kick", 38: "Snare", 46: "Open Hat", 49: "Crash",
        50: "Rack Tom", 45: "Mid Tom", 41: "Low Tom", 43: "Floor Tom",
    }
    m, rep = match_roles(notes)
    assert rep["complete"]
    assert m["TOM_1"] == 50
    assert m["TOM_2"] == 45
    assert m["TOM_3"] == 41
    assert m["TOM_4"] == 43


def test_double_kick():
    notes = {36: "Kick 1", 35: "Kick 2", 38: "Snare", 46: "Open Hat", 49: "Crash"}
    m, rep = match_roles(notes)
    assert rep["complete"]
    assert m["KICK_R"] == 35 and m["KICK_L"] == 36   # sorted asc: first=R, last=L


def test_empty_and_garbage():
    m, rep = match_roles({})
    assert not rep["complete"]
    assert m == {}
    m, rep = match_roles({36: "Kick", 200: "Out of range", 38: "Snare",
                           46: "Open Hat", 49: "Crash"})
    assert rep["complete"]
    assert m["KICK_R"] == 36
    assert 200 not in {v for v in m.values()}


def test_all_roles_known():
    for role in ROLE_KEYS:
        assert isinstance(role, str)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


# ---- 2026-07-02 review minors ----------------------------------------------

def test_no_crash_kit_reports_partial():
    # "complete" is documented as kick + snare + timekeeper + CRASH; has_crash
    # was computed but never enforced, so crash-less kits claimed complete.
    notes = {36: "Kick", 38: "Snare", 46: "Open Hat"}
    _, rep = match_roles(notes)
    assert not rep["complete"]
    assert rep["reason"] == "PARTIAL_MAP"


def test_hihat_foot_splash_is_a_hat_not_a_splash():
    fam, _mod, _ = classify("Hi-Hat Foot Splash")
    assert fam == "hat"


def test_classify_survives_non_string_names():
    assert classify(42) is None
    assert classify(None) is None


def test_stack_pick_is_lowest_pitch_deterministic():
    notes = {36: "Kick", 38: "Snare", 46: "Open Hat", 49: "Crash",
             60: "Stack High", 55: "Stack Low"}
    m, _ = match_roles(notes)
    assert m["STACK"] == 55


# ---- 2026-09-22: RS Monarch .midnam ----------------------------------------
#
# The exact note names the RS Monarch Kontakt kit reports, read live with
# discover_drum_map on 2026-09-22. Before the fix, choke keys became the left
# crash, big crash, left china and left splash (mute triggers, so those lanes
# went silent), Rack/Floor toms and the hat strokes without "hat" in their
# names went unclassified, and HH_PEDAL could land on "Pedal Splash".

MONARCH_NOTES = {
    24: "Kick", 26: "Snare Center", 27: "Snare Flam", 28: "Snare Rimshot",
    29: "Snare Rimshot Flam", 30: "Side Stick", 33: "Floor R2",
    35: "Floor R1", 36: "Floor L", 37: "Rack 2", 38: "Rack 1",
    40: "Pedal Hat", 41: "Tight Closed Tip", 42: "Tight Closed Shoulder",
    43: "Normal Closed Tip", 44: "Normal Closed Shoulder", 45: "Small Open",
    46: "Medium Open", 47: "Large Open", 49: "Left Crash",
    50: "Left Crash Choke", 51: "Splash", 52: "Mini Stack", 53: "Mini Bell",
    54: "Right Crash", 55: "Right Crash Choke", 56: "China",
    57: "China Choke", 58: "Far Left Crash", 59: "Far Left Crash Choke",
    60: "Stack", 61: "Ride Bell Shoulder", 62: "Ride Bow Tip",
    63: "Ride Bell Tip", 64: "Splash Choke", 65: "Mini Bell Choke",
    69: "Pedal Splash",
}

# The hand-built catalog map, choke roles included. SNARE_GHOST is the one
# role that differs: the catalog plays ghosts on the Side Stick by choice, and
# the kit has no ghost articulation to discover.
MONARCH_DISCOVERED = {
    "KICK_R": 24, "KICK_L": 24, "SNARE": 26, "SNARE_FLAM": 27,
    "SNARE_RIM": 28, "SNARE_GHOST": 26,
    "TOM_1": 38, "TOM_2": 37, "TOM_3": 36, "TOM_4": 35,
    "HH_CLOSED_TIP": 41, "HH_CLOSED_EDGE": 42, "HH_OPEN_1": 45,
    "HH_OPEN_2": 46, "HH_OPEN_3": 47, "HH_PEDAL": 40,
    "RIDE_TIP": 62, "RIDE_BELL": 63, "RIDE_CRASH": 61,
    "CRASH_L": 49, "CRASH_R": 54, "BIG_CRASH": 58,
    "CHINA_L": 56, "CHINA_R": 56, "STACK": 60,
    "SPLASH_L": 51, "SPLASH_R": 51, "BELL": 53,
    "CRASH_CHOKE": 55, "CHINA_CHOKE": 57, "SPLASH_CHOKE": 64,
}


def test_monarch_discovered_map():
    m, rep = match_roles(MONARCH_NOTES)
    assert rep["complete"], rep
    assert m == MONARCH_DISCOVERED
    # Every note is accounted for: mapped, recognized but spare, or unknown.
    assert set(rep["unused"]) == {29, 33, 43, 44, 50, 52, 59, 65, 69}
    assert rep["ignored"] == {30: "Side Stick"}


def test_monarch_discovery_agrees_with_the_catalog_map():
    maps = Path(__file__).resolve().parent.parent / "catalog" / "maps.json"
    catalog = json.loads(maps.read_text())["RS Monarch"]
    m, _ = match_roles(MONARCH_NOTES)
    assert set(m) == set(catalog)
    roles = [r for r in catalog if r != "SNARE_GHOST"]
    assert {r: m[r] for r in roles} == {r: catalog[r] for r in roles}


def test_monarch_chokes_route_to_choke_roles_never_to_hits():
    assert classify("Left Crash Choke") == ("crash", "choke", None)
    assert classify("China Choke") == ("china", "choke", None)
    assert classify("Splash Choke") == ("splash", "choke", None)
    assert classify("Mini Bell Choke") == ("bell", "choke", None)
    m, _ = match_roles(MONARCH_NOTES)
    choke_keys = {p for p, name in MONARCH_NOTES.items() if "Choke" in name}
    for role, pitch in m.items():
        assert (pitch in choke_keys) == role.endswith("_CHOKE"), role
    # Each choke role grabs the cymbal its lane falls back to (the R side).
    assert m["CRASH_CHOKE"] == 55 and m["CHINA_CHOKE"] == 57 \
        and m["SPLASH_CHOKE"] == 64


def test_monarch_rack_and_floor_toms_rank_by_pitch():
    assert classify("Rack 1") == ("tom", None, 1)
    assert classify("Floor L") == ("tom", None, 4)
    assert classify("Floor R1") == ("tom", None, 4)
    m, rep = match_roles(MONARCH_NOTES)
    assert [m["TOM_%d" % n] for n in (1, 2, 3, 4)] == [38, 37, 36, 35]
    assert rep["unused"][33] == "Floor R2"  # a fifth tom has no slot


def test_monarch_left_and_right_crash_follow_their_names():
    m, _ = match_roles(MONARCH_NOTES)
    assert m["CRASH_L"] == 49       # Left Crash, though lower than Right
    assert m["CRASH_R"] == 54       # Right Crash
    assert m["BIG_CRASH"] == 58     # Far Left Crash, the one left over


def test_monarch_hat_strokes_without_hat_in_the_name():
    # One name alone could be anything; the kit's "Pedal Hat" settles it.
    assert classify("Small Open") is None
    assert classify("Tight Closed Tip") is None
    assert classify("Pedal Splash") == ("hat", "foot_splash", None)
    m, _ = match_roles(MONARCH_NOTES)
    assert m["HH_CLOSED_TIP"] == 41 and m["HH_CLOSED_EDGE"] == 42
    assert [m["HH_OPEN_%d" % n] for n in (1, 2, 3)] == [45, 46, 47]
    assert m["HH_PEDAL"] == 40      # the chick, not the foot splash


def test_monarch_map_does_not_depend_on_note_order():
    # The bridge's JSON object order is not stable across runs.
    items = list(MONARCH_NOTES.items())
    orders = [items[::-1], sorted(items, key=lambda kv: kv[1])]
    for seed in range(20):
        shuffled = list(items)
        random.Random(seed).shuffle(shuffled)
        orders.append(shuffled)
    for order in orders:
        assert match_roles(dict(order))[0] == MONARCH_DISCOVERED


def test_bare_hat_strokes_need_a_named_hat_that_lacks_them():
    base = {36: "Kick", 38: "Snare", 49: "Crash", 51: "Ride"}
    # No hi-hat named anywhere: bare "Closed"/"Open" stay unclassified.
    m, rep = match_roles({**base, 42: "Closed", 46: "Open"})
    assert "HH_CLOSED_TIP" not in m and "HH_OPEN_1" not in m
    assert rep["ignored"] == {42: "Closed", 46: "Open"}
    # The kit already names its closed hat, so a bare closed stroke is
    # ambiguous, while the open strokes it never named are adopted.
    m, rep = match_roles({
        **base, 42: "Hi-Hat Closed", 44: "Hi-Hat Pedal", 60: "Closed Tip",
        46: "Small Open"})
    assert m["HH_CLOSED_TIP"] == 42 and m["HH_OPEN_1"] == 46
    assert rep["ignored"] == {60: "Closed Tip"}
    # Other instruments' open/closed strokes are never hats.
    m, rep = match_roles({**base, 44: "Pedal Hat", 81: "Open Triangle"})
    assert "HH_OPEN_1" not in m and 81 in rep["ignored"]


def test_sided_cymbal_names_beat_pitch_order():
    notes = {36: "Kick", 38: "Snare", 46: "Open Hat",
             49: "Crash L", 57: "Crash R", 55: "Crash L Choke",
             58: "Crash R Choke", 52: "China L", 54: "China R"}
    m, rep = match_roles(notes)
    assert rep["complete"]
    assert m["CRASH_L"] == 49 and m["CRASH_R"] == 57
    assert m["CHINA_L"] == 52 and m["CHINA_R"] == 54
    assert m["CRASH_CHOKE"] == 58   # the choke of the right-hand crash
    assert m["BIG_CRASH"] == 57     # no third crash: falls back to CRASH_R
    assert rep["unused"] == {55: "Crash L Choke"}


def test_lone_left_cymbal_still_plays_the_right_hand_role():
    notes = {36: "Kick", 38: "Snare", 46: "Open Hat", 49: "Crash Left"}
    m, rep = match_roles(notes)
    assert rep["complete"]
    assert m["CRASH_R"] == 49 and m["CRASH_L"] == 49


def test_tom_rim_stroke_does_not_take_a_tom_slot():
    notes = {36: "Kick", 38: "Snare", 46: "Open Hat", 49: "Crash",
             48: "Tom 1", 50: "Tom 1 Rim", 45: "Tom 2", 43: "Tom 3",
             41: "Floor Tom"}
    m, rep = match_roles(notes)
    assert [m["TOM_%d" % n] for n in (1, 2, 3, 4)] == [48, 45, 43, 41]
    assert rep["unused"] == {50: "Tom 1 Rim"}
