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
    assert len(grooves) == 41  # 36 base + 5 breakdown grooves (cymbal lane)
    cats = {g["category"] for g in grooves}
    assert len(cats) == 10
    legal_kick, legal_snare = set("KkS-"), set("Ssgf-")  # 'S'-in-kick = ghost kick (Linear Precision)
    legal_cymbal = set("CXprb-")  # china/crash/splash/ride/bell/rest
    for g in grooves:
        assert set(g["kick"]) <= legal_kick, g["name"]
        assert set(g["snare"]) <= legal_snare, g["name"]
        assert len(g["kick"]) == len(g["snare"]), g["name"]
        if "cymbal" in g:  # optional accent lane
            assert set(g["cymbal"]) <= legal_cymbal, g["name"]
            assert len(g["cymbal"]) == len(g["kick"]), g["name"]


def test_breakdown_grooves_have_cymbal_lane():
    grooves = {g["name"]: g for g in load_grooves()}
    we = grooves["World Ending Stomp"]
    assert "cymbal" in we and len(we["cymbal"]) == 32  # 2-bar pattern
    assert we["cymbal"][0] == "X" and we["cymbal"][8] == "C"
    # New chug-rhythm variations
    for name in ["Tight Chug Breakdown", "Gallop Stomp", "Dead Stop Breakdown"]:
        assert name in grooves, f"missing {name}"
        g = grooves[name]
        assert "cymbal" in g, f"{name} missing cymbal lane"
        assert len(g["cymbal"]) == len(g["kick"]), f"{name} cymbal/kick length mismatch"
    # Dead Stop is the 2-bar sparse one
    assert len(grooves["Dead Stop Breakdown"]["kick"]) == 32
