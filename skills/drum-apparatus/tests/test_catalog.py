import json
from drumgen.catalog import load_maps, load_grooves, ROLE_KEYS


def test_four_maps_with_required_roles():
    maps = load_maps()
    assert set(maps) >= {"RS Monarch", "Odeholm Default (Wretcher Fix)",
                         "Ultimate Heavy Drums (MDL Tone)", "Sleep Token II by MixWave"}
    for name, m in maps.items():
        for role in ROLE_KEYS:
            assert role in m, f"{name} missing {role}"
            assert 0 <= m[role] <= 127


def test_rs_monarch_known_notes():
    m = load_maps()["RS Monarch"]
    assert m["KICK_R"] == 24 and m["KICK_L"] == 24
    assert m["SNARE"] == 26 and m["RIDE_TIP"] == 62


def test_grooves_count_and_validity():
    grooves = load_grooves()
    assert len(grooves) == 36
    cats = {g["category"] for g in grooves}
    assert len(cats) == 10
    legal_kick, legal_snare = set("KkS-"), set("Ssgf-")  # 'S'-in-kick = ghost kick (Linear Precision)
    for g in grooves:
        assert set(g["kick"]) <= legal_kick, g["name"]
        assert set(g["snare"]) <= legal_snare, g["name"]
        assert len(g["kick"]) == len(g["snare"]), g["name"]
