import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from composer import history


def test_append_and_recent_tail(tmp_path):
    p = str(tmp_path / "h.jsonl")
    assert history.recent(path=p) == ""
    for i in range(15):
        history.append_song(p, title=f"s{i}", key="E minor", bpm=100 + i, date="2026-01-01")
    out = history.recent(path=p).splitlines()
    assert len(out) == 10 and out[0].startswith("2026-01-01 | s5") and "bpm=114" in out[-1]
    assert len(history.recent(3, path=p).splitlines()) == 3


def test_line_cap_and_validation(tmp_path):
    p = str(tmp_path / "h.jsonl")
    history.append_song(p, title="long", **{k: "x" * 500 for k in ("structure", "riff", "tricks", "drums", "tuning")})
    line = open(p, encoding="utf-8").read().strip()
    assert len(line) <= history.LINE_MAX
    with pytest.raises(ValueError):
        history.append_song(p, title="t", mood="sad")
    with pytest.raises(ValueError):
        history.append_song(p, key="C")
