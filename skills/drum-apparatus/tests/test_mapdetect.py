"""Tests for drumgen.mapdetect — auto-discovery of a drum library's note map.

These cover the major naming conventions (GGD, Superior Drummer, EZdrummer,
Addictive Drums, GM) so the classifier stays honest as libraries change.
"""
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
        36: "Kick", 38: "Snare", 46: "Open Hat",
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
