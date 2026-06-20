from drumgen.groovekit import build
from drumgen.catalog import load_maps

DSL = (
    "@tempo 144\n@map RS Monarch\n@seed 1\n"
    "[v] bars=2 feel=fff\ngrid 16\n"
    "kick  | xxxxxxxxxxxxxxxx |\n"
    "snare | x...x...x...x... |\n"
)


def test_kick_ceiling_114_on_monarch():
    dm = load_maps()["RS Monarch"]
    events, _ = build(DSL, seed=1)
    kick_pitches = {dm["KICK_R"], dm["KICK_L"]}
    kicks = [e["vel"] for e in events if e["pitch"] in kick_pitches]
    assert kicks, "no kicks rendered"
    assert max(kicks) <= 114                      # ceiling holds even at feel=fff


def test_monarch_snare_is_rimshot_in_band():
    dm = load_maps()["RS Monarch"]
    events, _ = build(DSL, seed=1)
    rim = [e["vel"] for e in events if e["pitch"] == dm["SNARE_RIM"]]
    plain = [e for e in events if e["pitch"] == dm["SNARE"]]
    assert rim, "snare did not render as rimshot on Monarch"
    assert not plain, "plain SNARE leaked through; should be the rimshot"
    assert all(90 <= v <= 110 for v in rim)        # held in David's band


def test_golden_rule_survives_tight_snare_band():
    # 96-107 is only 11 wide; consecutive rim hits must still differ.
    dm = load_maps()["RS Monarch"]
    events, _ = build(DSL, seed=1)
    rim = [e["vel"] for e in sorted(
        (e for e in events if e["pitch"] == dm["SNARE_RIM"]), key=lambda e: e["tick"])]
    assert all(a != b for a, b in zip(rim, rim[1:]))
