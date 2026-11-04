"""Tests for the agent-run report (tools/session.py): note timing through a tempo map."""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import session  # noqa: E402

# 120 BPM until 10 s, then 60 BPM. One item at 8 s with a note at QN 4-6
# (crosses the tempo change), one item at 12 s with a note at QN 0-1.
RPP = """<REAPER_PROJECT 0.1 "7.0"
  TEMPO 120 4 4 0
  <TEMPOENVEX
    PT 0 120 1
    PT 10 60 1
  >
  <TRACK
    NAME argent-l
    VOLPAN 1 0 -1 -1 1
    <ITEM
      POSITION 8
      <SOURCE MIDI
        HASDATA 1 960 QN
        E 3840 90 28 64
        E 1920 80 28 00
      >
    >
    <ITEM
      POSITION 12
      <SOURCE MIDI
        HASDATA 1 960 QN
        E 0 90 14 70
        E 960 80 14 00
      >
    >
  >
>
"""


def test_note_times_follow_tempo_changes(tmp_path):
    path = tmp_path / "song.rpp"
    path.write_text(RPP)
    proj = session.parse_project(str(path))
    notes = sorted(proj["tracks"][0]["notes"], key=lambda n: n["start"])
    assert [(n["start"], n["end"]) for n in notes] == [(10.0, 12.0), (12.0, 13.0)]
    # pitch 20 is under Argent's lowest note, so it counts as a keyswitch
    assert [n["pitch"] for n in session.pitched("argent-l", notes)] == [40]
    assert "- Tempo: 60, 120 BPM (2 tempo-map points)" in session.song_facts(proj)
