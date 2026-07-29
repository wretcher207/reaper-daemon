"""profile.py against synthesized stems with KNOWN playing styles.

Every signal is built from decaying sine bursts — τ (decay constant) is the
palm-mute knob, burst spacing is the technique knob. 120 bpm throughout so a
bar is exactly 2.0s and a 16th is 0.125s. sr=24000 keeps the pure-Python
passes fast; nothing here depends on rate.

Asserts are RELATIVE (chug < open, wall < stop-start), never absolute dB —
matching the module's own rule that only same-stem comparisons mean anything.
"""
import math
import os
import struct
import tempfile

from drumgen.profile import (profile_bars, profile_track, find_boundaries,
                             segment, group_segments, format_profile)

SR = 24000
TEMPO = 120.0
BAR_S = 2.0


def _burst(freq, tau, length_s, amp=0.9):
    """One picked note: instant attack, exponential decay."""
    n = int(length_s * SR)
    return [amp * math.exp(-i / SR / tau) * math.sin(2 * math.pi * freq * i / SR)
            for i in range(n)]


def _bar(hits, freq, tau, amp=0.9):
    """One 2s bar with bursts at the given beat-fraction offsets (0..1)."""
    out = [0.0] * int(BAR_S * SR)
    for frac in hits:
        start = int(frac * BAR_S * SR)
        burst = _burst(freq, tau, min(0.6, BAR_S - frac * BAR_S), amp=amp)
        for j, v in enumerate(burst):
            k = start + j
            if k < len(out):
                out[k] += v
    return out


# The cast: four playing styles with clearly different physics.
def chug_bar():        # 8ths, dead palm mutes, low string
    return _bar([i / 8 for i in range(8)], freq=100, tau=0.03)


def open_bar():        # two ringing open hits
    return _bar([0.0, 0.5], freq=100, tau=0.5)


def trem_bar():        # 16ths, machine-regular, higher note
    return _bar([i / 16 for i in range(16)], freq=400, tau=0.05)


def stopstart_bar():   # three stabs and air — the djent-y figure
    return _bar([0.0, 0.375, 0.625], freq=100, tau=0.03)


def _profile(bars_audio, **kw):
    samples = [v for bar in bars_audio for v in bar]
    return profile_bars(samples, SR, TEMPO, len(bars_audio), **kw)


# ---- feature separation ----------------------------------------------------

def test_decay_ratio_separates_muted_from_open():
    rows = _profile([chug_bar()] * 2 + [open_bar()] * 2)
    chug, opn = rows[:2], rows[2:]
    for r in chug:
        assert r["decay_ratio"] is not None and r["decay_ratio"] < 0.1, r
    for r in opn:
        assert r["decay_ratio"] is not None and r["decay_ratio"] > 0.3, r
    # both live on the low string -> low_ratio high, brightness low
    for r in rows:
        assert r["low_ratio"] > 0.6
        assert r["bright_ratio"] < 0.2


def test_tremolo_reads_dense_and_regular():
    rows = _profile([trem_bar()] * 2)
    for r in rows:
        assert r["n_onsets"] >= 14            # detector may merge a neighbor
        assert r["density_hz"] > 6.0
        assert r["ioi_cv"] is not None and r["ioi_cv"] < 0.15
        assert r["grid"].count(".") <= 2      # nearly every 16th cell filled


def test_stop_start_shows_as_silence():
    rows = _profile([chug_bar(), stopstart_bar()])
    wall, stop = rows
    assert stop["silence_ratio"] > wall["silence_ratio"] + 0.15
    assert stop["n_onsets"] == 3
    # the three stabs land where written: 1, the "e" of 2 (step 6), and step 10
    assert [i for i, c in enumerate(stop["grid"]) if c != "."] == [0, 6, 10]


def test_grid_marks_accents():
    # 16ths with beat 1 much louder than the rest -> exactly the loud hits get X
    quiet = _bar([i / 16 for i in range(1, 16)], freq=200, tau=0.05, amp=0.25)
    loud = _bar([0.0], freq=200, tau=0.05, amp=0.9)
    both = [a + b for a, b in zip(loud, quiet)]
    rows = profile_bars(both, SR, TEMPO, 1)
    g = rows[0]["grid"]
    assert g[0] == "X"
    assert g.count("X") == 1          # ONLY the genuinely loud hit is accented
    assert "x" in g[1:]


# ---- structure -------------------------------------------------------------

def test_boundaries_and_repeat_grouping():
    # A A A A  B B B B  A A A A  — verse / big change / verse again
    audio = [chug_bar()] * 4 + [trem_bar()] * 4 + [chug_bar()] * 4
    rows = _profile(audio)
    bounds = find_boundaries(rows)
    assert 5 in bounds and 9 in bounds, bounds
    segs = group_segments(rows, segment(rows, bounds))
    labels = [s["label"] for s in segs]
    # first and third sections are the same music; middle is different
    assert labels[0] == labels[-1]
    assert labels[1] != labels[0]
    first = next(s for s in segs if s["start_bar"] == 1)
    assert first["end_bar"] == 4


def test_uniform_song_has_no_boundaries():
    rows = _profile([chug_bar()] * 6)
    assert find_boundaries(rows) == []
    segs = group_segments(rows, segment(rows, []))
    assert len(segs) == 1 and segs[0]["label"] == "A"


# ---- end to end ------------------------------------------------------------

def _write_float_wav(path, samples):
    data = struct.pack("<%df" % len(samples), *samples)
    with open(path, "wb") as f:
        f.write(b"RIFF"); f.write(struct.pack("<I", 36 + len(data))); f.write(b"WAVE")
        f.write(b"fmt "); f.write(struct.pack("<IHHIIHH", 16, 3, 1, SR, SR * 4, 4, 32))
        f.write(b"data"); f.write(struct.pack("<I", len(data))); f.write(data)


def test_profile_track_end_to_end():
    audio = [chug_bar()] * 2 + [open_bar()] * 2
    samples = [v for bar in audio for v in bar]
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "di.wav")
        _write_float_wav(wav, samples)
        rpp = (
            '<REAPER_PROJECT 0.1\n  TEMPO 120 4 4 0\n'
            '  <TRACK {AAA}\n    NAME GTR_DI\n'
            '    <ITEM\n      POSITION 0\n      LENGTH 8\n'
            f'      <SOURCE WAVE\n        FILE "di.wav"\n      >\n    >\n  >\n>\n'
        )
        p = os.path.join(d, "song.RPP")
        open(p, "w").write(rpp)
        prof = profile_track(p, "GTR_DI")
        assert prof["tempo"] == 120.0
        assert len(prof["bars"]) == 4
        assert prof["bars"][0]["decay_ratio"] < prof["bars"][3]["decay_ratio"]
        text = format_profile(prof)
        assert "sections:" in text and "GTR_DI" in text


def test_stem_a_few_samples_short_still_counts_final_bar():
    """Glued/trimmed stems land a hair short of the exact bar line; the whole
    final bar of music must not be silently dropped (40-bar song read as 39)."""
    audio = [chug_bar()] * 3 + [open_bar()]
    samples = [v for bar in audio for v in bar][:-200]  # ~8ms short of 4 bars
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "di.wav")
        _write_float_wav(wav, samples)
        rpp = (
            '<REAPER_PROJECT 0.1\n  TEMPO 120 4 4 0\n'
            '  <TRACK {AAA}\n    NAME GTR_DI\n'
            '    <ITEM\n      POSITION 0\n      LENGTH 8\n'
            f'      <SOURCE WAVE\n        FILE "di.wav"\n      >\n    >\n  >\n>\n'
        )
        p = os.path.join(d, "song.RPP")
        open(p, "w").write(rpp)
        prof = profile_track(p, "GTR_DI")
        assert len(prof["bars"]) == 4  # int() truncation would say 3


def test_edit_sliver_past_bar_line_does_not_add_a_bar():
    """A few extra samples past the bar line are an edit artifact, not a bar."""
    audio = [chug_bar()] * 4
    samples = [v for bar in audio for v in bar] + [0.0] * 200
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "di.wav")
        _write_float_wav(wav, samples)
        rpp = (
            '<REAPER_PROJECT 0.1\n  TEMPO 120 4 4 0\n'
            '  <TRACK {AAA}\n    NAME GTR_DI\n'
            '    <ITEM\n      POSITION 0\n      LENGTH 8\n'
            f'      <SOURCE WAVE\n        FILE "di.wav"\n      >\n    >\n  >\n>\n'
        )
        p = os.path.join(d, "song.RPP")
        open(p, "w").write(rpp)
        prof = profile_track(p, "GTR_DI")
        assert len(prof["bars"]) == 4  # ceil() alone would say 5
