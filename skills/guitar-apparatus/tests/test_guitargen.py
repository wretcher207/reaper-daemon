"""Tests for the guitar apparatus: SMF writer, performance engine, riff notation."""
import pytest

from guitargen.smf import write_smf, parse_smf
from guitargen.perform import perform, ART_VEL, NO_REPEAT_GAP
from guitargen.maps import get_map, MODWHEEL, SHREDDAGE3_KS, KS_NOTES
from guitargen import riffs

KS_PITCHES = {p for p, _v in KS_NOTES.values()}


def _played(events):
    """Notes that are actual guitar/bass notes, not keyswitch triggers."""
    return [e for e in events
            if e["type"] == "note" and e["pitch"] not in KS_PITCHES]


# ---- SMF writer -----------------------------------------------------------

def test_smf_note_and_cc_roundtrip():
    events = [
        {"type": "cc", "tick": 0, "cc": MODWHEEL, "val": 120},
        {"type": "note", "tick": 0, "pitch": 33, "vel": 100, "dur": 120},
        {"type": "note", "tick": 240, "pitch": 40, "vel": 90, "dur": 60, "chan": 0},
    ]
    d = parse_smf(write_smf(events, ppq=480, tempo=120))
    assert d["ppq"] == 480
    assert len(d["notes"]) == 2
    assert d["notes"][0]["pitch"] == 33 and d["notes"][0]["vel"] == 100
    assert len(d["ccs"]) == 1 and d["ccs"][0]["val"] == 120


def test_smf_cc_lands_before_note_at_same_tick():
    # a mod-wheel move written for a chug must precede the note it shapes.
    events = [
        {"type": "note", "tick": 100, "pitch": 33, "vel": 100, "dur": 50},
        {"type": "cc", "tick": 100, "cc": MODWHEEL, "val": 115},
    ]
    data = write_smf(events)
    # decode message order by walking the track: the CC status (0xB0) must appear
    # before the note-on status (0x90) in the byte stream at that tick.
    i = data.index(b"MTrk") + 8
    cc_pos = data.index(bytes([0xB0, MODWHEEL, 115]), i)
    on_pos = data.index(bytes([0x90, 33, 100]), i)
    assert cc_pos < on_pos


def test_smf_rejects_bad_values():
    for bad in (
        {"type": "note", "tick": 0, "pitch": 200, "vel": 100, "dur": 10},
        {"type": "note", "tick": 0, "pitch": 33, "vel": 0, "dur": 10},
        {"type": "note", "tick": 0, "pitch": 33, "vel": 100, "dur": 0},
        {"type": "cc", "tick": 0, "cc": 1, "val": 200},
    ):
        with pytest.raises(ValueError):
            write_smf([bad])


# ---- riff notation --------------------------------------------------------

def test_parse_bars_rejects_wrong_length():
    with pytest.raises(ValueError):
        riffs.parse_bars(["xxx"])  # not 16 steps


def test_parse_bars_ignores_spaces():
    # spaces inside a bar are visual grouping only, like the drum DSL
    assert (riffs.parse_bars(["Xxx.mx.xXx.vXxk."])
            == riffs.parse_bars(["Xxx. mx.x Xx.v Xxk."]))


def test_parse_bars_intervals_and_ring_length():
    hits = riffs.parse_bars(["o...x..........."])  # let-ring root, then a mute
    assert hits[0]["interval"] == 0 and hits[0]["art"] == "let_ring"
    # the let-ring holds until the next onset at step 4
    assert hits[0]["len_steps"] == 4
    assert hits[1]["len_steps"] == 1  # the mute is short


def test_bass_collapses_intervals_to_root():
    g = riffs.make_spec(["5...b...7...h..."], "argent_e")
    b = riffs.bass_from_guitar(g)
    assert all(h["interval"] == 0 for h in b["hits"])
    assert b["map"] == "nolly_e"


# ---- performance engine ---------------------------------------------------

def test_perform_is_deterministic():
    spec = riffs.demo_guitar_spec()
    a, _ = perform(spec, "argent_e", seed=101)
    b, _ = perform(spec, "argent_e", seed=101)
    assert a == b


def test_double_track_seeds_differ():
    spec = riffs.demo_guitar_spec()
    left, _ = perform(spec, "argent_e", seed=101)
    right, _ = perform(spec, "argent_e", seed=202)
    lv = [e for e in left if e["type"] == "note"]
    rv = [e for e in right if e["type"] == "note"]
    # same notes, but the humanization (velocity/timing) must differ -> width
    assert [e["pitch"] for e in lv] != []
    assert lv != rv


def test_guitar_articulation_via_keyswitches():
    # the demo's chugs and dissonant palm-muted rings fire the Mute keyswitch,
    # root accents fire Power Chord Mute; Argent does not mute via the mod wheel.
    spec = riffs.demo_guitar_spec()
    events, _ = perform(spec, "argent_e", seed=1)
    pitches = {e["pitch"] for e in events
               if e["type"] == "note" and e["pitch"] in KS_PITCHES}
    assert SHREDDAGE3_KS["mute"] in pitches       # chugs + palm-muted rings
    assert SHREDDAGE3_KS["pwr_mute"] in pitches    # root accents -> power chords
    assert not any(e["type"] == "cc" for e in events)


def test_letring_and_powerchord_keyswitches():
    # a melodic let-ring selects Sustain; a ROOT let-ring selects Power Chord
    # Sustain (Argent voices the power chord).
    ev1, _ = perform(riffs.make_spec(["5..............."], "argent_e"),
                     "argent_e", seed=1)
    assert any(e["pitch"] == SHREDDAGE3_KS["sustain"]
               for e in ev1 if e["type"] == "note")
    ev2, _ = perform(riffs.make_spec(["o..............."], "argent_e"),
                     "argent_e", seed=1)
    assert any(e["pitch"] == SHREDDAGE3_KS["pwr_sustain"]
               for e in ev2 if e["type"] == "note")


def test_mute_ring_is_palm_muted_and_rings():
    # a mute_ring note fires the Mute keyswitch (palm mute) and holds far longer
    # than a fast chug next to it.
    ev, _ = perform(riffs.make_spec(["r...x..........."], "argent_e"),
                    "argent_e", seed=1)
    assert any(e["pitch"] == SHREDDAGE3_KS["mute"]
               for e in ev if e["type"] == "note")
    played = sorted((e for e in _played(ev)), key=lambda e: e["tick"])
    ring, chug = played[0], played[1]
    assert ring["dur"] > chug["dur"] * 2


def test_keyswitch_precedes_the_note_it_governs():
    spec = riffs.make_spec(["x..............."], "argent_e")
    events, _ = perform(spec, "argent_e", seed=1)
    ks = next(e for e in events if e["pitch"] == SHREDDAGE3_KS["mute"])
    note = next(e for e in events if e["pitch"] == get_map("argent_e")["low_string"])
    assert ks["tick"] <= note["tick"]


def test_bass_has_no_keyswitch_or_cc():
    g = riffs.demo_guitar_spec()
    b = riffs.bass_from_guitar(g)
    events, _ = perform(b, "nolly_e", seed=7, is_bass=True)
    assert all(e["type"] == "note" for e in events)
    assert all(e["pitch"] == get_map("nolly_e")["low_string"] for e in events)


def test_no_repeat_golden_rule_on_root():
    # a bar of straight root mutes must not repeat a velocity back-to-back.
    spec = riffs.make_spec(["xxxxxxxxxxxxxxxx"], "argent_e")
    events, _ = perform(spec, "argent_e", seed=42)
    roots = [e for e in events
             if e["type"] == "note" and e["pitch"] == get_map("argent_e")["low_string"]]
    roots.sort(key=lambda e: e["tick"])
    for a, b in zip(roots, roots[1:]):
        assert abs(a["vel"] - b["vel"]) >= NO_REPEAT_GAP


def test_velocities_stay_in_band():
    spec = riffs.demo_guitar_spec()
    events, _ = perform(spec, "argent_e", seed=5)
    lo = min(b[1] for b in ART_VEL.values())
    hi = max(b[2] for b in ART_VEL.values())
    for e in _played(events):
        assert lo <= e["vel"] <= hi


def test_low_string_override_transposes_whole_riff():
    spec = riffs.make_spec(["x...5..........."], "argent_e")
    spec["low_string_override"] = 45
    events, _ = perform(spec, "argent_e", seed=1)
    pitches = sorted(e["pitch"] for e in _played(events))
    # root at 45, fifth at 45+7=52
    assert pitches == [45, 52]


# ---- connection: legato, ties, slides --------------------------------------
# The riff-sounds-stabby class of bug. A ring note whose note-off lands exactly
# on the next note-on makes Shreddage re-pick every note, so no phrase ever leads
# into the next one. These lock the fix in.

def test_ring_to_ring_overlaps_so_the_engine_slurs():
    spec = riffs.make_spec(["o...5...b...7..."], "argent_e", timing_sigma=0.0)
    played = sorted(_played(perform(spec, "argent_e", seed=7)[0]),
                    key=lambda e: e["tick"])
    assert len(played) == 4
    for a, b in zip(played, played[1:]):
        assert a["tick"] + a["dur"] > b["tick"], "ring note must hold past the next attack"


def test_bass_is_never_slurred():
    spec = riffs.bass_from_guitar(riffs.make_spec(["o...5...b...7..."], "argent_e"))
    played = sorted(_played(perform(spec, "nolly_e", seed=7, is_bass=True)[0]),
                    key=lambda e: e["tick"])
    for a, b in zip(played, played[1:]):
        assert a["tick"] + a["dur"] <= b["tick"], "the Nolly has no legato/slides"


def test_tie_holds_a_muted_chug_for_its_written_value():
    short = riffs.parse_bars(["x..............."])[0]
    tied = riffs.parse_bars(["x___............"])[0]
    assert short["len_steps"] == 1 and not short.get("hold")
    assert tied["len_steps"] == 4 and tied["hold"]
    a = _played(perform(riffs.make_spec(["x..............."], "argent_e",
                                        timing_sigma=0.0), "argent_e", seed=1)[0])[0]
    b = _played(perform(riffs.make_spec(["x___............"], "argent_e",
                                        timing_sigma=0.0), "argent_e", seed=1)[0])[0]
    assert b["dur"] > a["dur"] * 3


def test_slide_token_fires_the_slide_keyswitch_and_forces_an_overlap():
    spec = riffs.make_spec(["x......~f......."], "argent_e", timing_sigma=0.0)
    events, _ = perform(spec, "argent_e", seed=1)
    slide_pitch = KS_NOTES["slide"][0]
    assert any(e["pitch"] == slide_pitch for e in events if e["type"] == "note")
    a, b = sorted(_played(events), key=lambda e: e["tick"])
    assert a["tick"] + a["dur"] > b["tick"], "a slide must be slurred, not re-picked"


def test_slide_with_no_preceding_note_is_rejected():
    with pytest.raises(ValueError, match="slide"):
        riffs.parse_bars(["~x.............."])


def test_tie_with_no_preceding_note_is_rejected():
    with pytest.raises(ValueError, match="PREVIOUS"):
        riffs.parse_bars(["_x.............."])


def test_demo_riff_actually_connects_across_barlines():
    spec = riffs.demo_guitar_spec()
    played = sorted(_played(perform(spec, "argent_e", seed=101)[0]),
                    key=lambda e: e["tick"])
    slurs = sum(1 for a, b in zip(played, played[1:])
                if a["tick"] + a["dur"] - b["tick"] >= 20)
    assert slurs >= 15, f"demo riff only slurs {slurs} times — it will sound stabby"
