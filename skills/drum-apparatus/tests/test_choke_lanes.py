"""Choke articulation lanes (china_choke / crash_choke / splash_choke).

Chokes are OPTIONAL roles: a kit that has them (MDL Tone) plays the real
hit-and-grab note; a kit without them falls back through ROLE_FALLBACKS to
the open cymbal instead of dropping the hit.
"""
from drumgen.catalog import load_maps
from drumgen.groovekit import build

MDL = "Ultimate Heavy Drums (MDL Tone)"

CHOKE_DSL = (
    "@tempo 146\n@map {map}\n[stab] bars=1 feel=ff\ngrid 16\n"
    "kick        | X............... |\n"
    "china_choke | X............... |\n"
    "crash_choke | ....X........... |\n"
    "splash_choke| ........X....... |\n"
)


def _pitches(events):
    return {e["pitch"] for e in events}


def test_mdl_map_plays_real_choke_notes():
    events, info = build(CHOKE_DSL.format(map=MDL), seed=7)
    m = load_maps()[MDL]
    assert m["CHINA_CHOKE"] == 54 and m["CRASH_CHOKE"] == 58 \
        and m["SPLASH_CHOKE"] == 70
    got = _pitches(events)
    assert {54, 58, 70} <= got


def test_kit_without_chokes_falls_back_to_open_cymbal():
    events, info = build(CHOKE_DSL.format(map="GM Standard"), seed=7)
    gm = load_maps()["GM Standard"]
    assert "CHINA_CHOKE" not in gm  # premise: GM has no choke notes
    got = _pitches(events)
    # Falls back to the open R-side cymbals — nothing dropped.
    assert gm["CHINA_R"] in got
    assert gm["CRASH_R"] in got
    assert gm["SPLASH_R"] in got


def test_choke_counts_as_cymbal_for_shell_boost():
    # A choke landing with the kick is a cymbal-with-shell hit: it should
    # render louder than the same choke alone at the same feel.
    with_kick = (
        "@tempo 146\n@map {m}\n[a] bars=1 feel=mf\ngrid 16\n"
        "kick        | x............... |\n"
        "china_choke | x............... |\n"
    ).format(m=MDL)
    alone = (
        "@tempo 146\n@map {m}\n[a] bars=1 feel=mf\ngrid 16\n"
        "china_choke | x............... |\n"
    ).format(m=MDL)
    ev_with, _ = build(with_kick, seed=11)
    ev_alone, _ = build(alone, seed=11)
    v_with = max(e["vel"] for e in ev_with if e["pitch"] == 54)
    v_alone = max(e["vel"] for e in ev_alone if e["pitch"] == 54)
    assert v_with > v_alone
