from drumgen.smf import write_smf, parse_smf


def test_roundtrip_note_count_and_positions():
    events = [
        {"tick": 0,   "pitch": 24, "vel": 120, "dur": 58},
        {"tick": 240, "pitch": 26, "vel": 110, "dur": 58},
        {"tick": 480, "pitch": 24, "vel": 100, "dur": 58},
    ]
    data = write_smf(events, ppq=480, tempo=120)
    assert data[:4] == b"MThd"
    parsed = parse_smf(data)
    assert parsed["ppq"] == 480
    assert len(parsed["notes"]) == 3
    assert [n["tick"] for n in parsed["notes"]] == [0, 240, 480]
    assert [n["pitch"] for n in parsed["notes"]] == [24, 26, 24]
    assert parsed["notes"][0]["vel"] == 120
