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
import sys
import tempfile

import pytest

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


# ---- decay at real tempos ---------------------------------------------------

def _bar_at(hits, freq, tau, tempo, amp=0.9):
    """One bar at an arbitrary tempo (the module-level _bar is pinned to 120)."""
    bar_s = 60.0 / tempo * 4.0
    out = [0.0] * int(bar_s * SR)
    for h in hits:
        b = _burst(freq, tau, bar_s, amp)
        start = int(h * bar_s * SR)
        for i, v in enumerate(b):
            if start + i < len(out):
                out[start + i] += v
    return out


def test_decay_separates_mutes_from_rings_at_dense_tempos():
    """16th-note dead mutes must read muted at ANY tempo. The fixed 150-220ms
    tail window used to swallow the next hit's attack once the IOI dropped
    under ~220ms: a dead-mute chug at 100 BPM (IOI 150ms) measured decay 0.85
    — MORE ringing than actual open chords. Hidden because the test chug was
    8ths at 120 BPM (IOI 250ms, just clear of the window).

    Thresholds carry slack for this suite's 24 kHz rate (21.3ms hops blur
    the shrunken windows); at the production 48 kHz, measured margins are
    far wider (mute <= 0.08, ring >= 0.73 through 180 BPM)."""
    sixteenths = [i / 16 for i in range(16)]
    for tempo in (100.0, 146.0, 180.0):
        mute = _bar_at(sixteenths, freq=100, tau=0.03, tempo=tempo)
        ring = _bar_at(sixteenths, freq=100, tau=0.5, tempo=tempo)
        m = profile_bars(mute * 2, SR, tempo, 2)[0]["decay_ratio"]
        r = profile_bars(ring * 2, SR, tempo, 2)[0]["decay_ratio"]
        assert m is not None and m < 0.25, f"tempo {tempo}: mute read {m}"
        assert r is not None and r > 0.6, f"tempo {tempo}: ring read {r}"
        assert r - m > 0.35, f"tempo {tempo}: no separation ({m} vs {r})"


def test_final_onset_at_stem_end_is_not_misread_as_muted():
    """A ringing chord whose decay windows run off the end of the stem is
    unmeasurable, not 'muted': a truncated/empty tail used to read as ~0 and
    drag the bar's median toward dead-mute."""
    bar = _bar([0.0, 0.9], freq=100, tau=0.5)   # last ring ~200ms before EOF
    row = profile_bars(bar, SR, TEMPO, 1)[0]
    # the first hit measures high; the cut-off last hit is excluded, not zeroed
    assert row["decay_ratio"] is not None and row["decay_ratio"] > 0.4


def test_max_seconds_composes_with_start_bar():
    """--max-seconds caps ANALYZED material, measured from --start-bar. The
    old read-from-zero cap starved a later start window entirely."""
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
        prof = profile_track(p, "GTR_DI", start_bar=2, max_seconds=2 * BAR_S)
        assert [r["bar"] for r in prof["bars"]] == [3, 4]  # the two open bars
        assert all(r["n_onsets"] == 2 for r in prof["bars"])


def test_windowed_start_bar_reproduces_full_profile_rows():
    """start_bar analysis is windowed: audio before start_bar minus one
    pre-roll bar is never run through the signal passes. The windowed rows
    must reproduce the full run's rows EXACTLY past the pre-roll: the cut
    is hop-aligned and passed as origin_hops (hop phase and bar grid stay
    on the item's ruler), and decay gaps come from integer hop indices (a
    t2-t1 float gap wiggles by an ulp with the cut and used to flip decay
    windows by a whole hop). silence_ratio alone gets tolerance: its loud
    reference is window-local by design."""
    audio = ([chug_bar()] * 2 + [open_bar()] * 2
             + [trem_bar()] * 2 + [stopstart_bar()] * 2)
    samples = [v for bar in audio for v in bar]
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "di.wav")
        _write_float_wav(wav, samples)
        rpp = (
            '<REAPER_PROJECT 0.1\n  TEMPO 120 4 4 0\n'
            '  <TRACK {AAA}\n    NAME GTR_DI\n'
            '    <ITEM\n      POSITION 0\n      LENGTH 16\n'
            f'      <SOURCE WAVE\n        FILE "di.wav"\n      >\n    >\n  >\n>\n'
        )
        p = os.path.join(d, "song.RPP")
        open(p, "w").write(rpp)
        full = profile_track(p, "GTR_DI")
        win = profile_track(p, "GTR_DI", start_bar=4)
        assert [r["bar"] for r in win["bars"]] == [5, 6, 7, 8]
        by_bar = {r["bar"]: r for r in full["bars"]}
        for r in win["bars"]:
            f = by_bar[r["bar"]]
            for k in r:
                if k == "silence_ratio":
                    assert abs(f[k] - r[k]) < 0.05, (r["bar"], k, f[k], r[k])
                else:
                    assert f[k] == r[k], (r["bar"], k, f[k], r[k])


def test_decay_stable_when_gap_fraction_lands_on_hop_boundary():
    """IOI of exactly 5 hops puts 60% of the gap exactly on a hop edge —
    the fragile spot: a gap computed as t2 - t1 differs by an ulp between
    windowed and full runs (different integer hop offsets behind the same
    float times) and used to flip the decay tail window by a whole hop.
    Gaps must come from integer hop indices so windowed decay is identical
    to the full run, bit for bit."""
    hop = 512
    ioi = 5 * hop                       # 2560 samples, exactly 5 hops
    bar_len = 16 * ioi                  # 40960 samples = 80 hops exactly
    tempo = 240.0 * SR / bar_len        # 140.625 BPM at SR 24000
    hits = [i / 16 for i in range(16)]
    bar = _bar_at(hits, freq=100, tau=0.03, tempo=tempo)
    assert len(bar) == bar_len
    samples = bar * 6
    full = profile_bars(samples, SR, tempo, 3, start_bar=3)
    cut = 2 * bar_len                   # skip 2 bars; bar 3 is the pre-roll
    assert cut % hop == 0
    win = profile_bars(samples[cut:], SR, tempo, 3, start_bar=3,
                       origin_hops=cut // hop)
    assert [r["bar"] for r in win] == [r["bar"] for r in full]
    for f, w in zip(full, win):
        assert f["decay_ratio"] == w["decay_ratio"], (f["bar"], f, w)
        assert f["grid"] == w["grid"]
        assert f["n_onsets"] == w["n_onsets"]


@pytest.mark.parametrize("bars,expect_bars_analyzed", [
    (None, 5),   # pre-roll + bars 5-8 (whole remainder)
    (2, 4),      # pre-roll + bars 5-6 + post-roll, NOT through EOF
])
def test_windowed_start_bar_skips_prefix_analysis(monkeypatch, bars,
                                                  expect_bars_analyzed):
    """The windowing is about COST, not just output: a late start_bar must
    run the signal passes on one pre-roll bar plus the requested window
    (plus one post-roll bar when the request has an end), never the whole
    prefix or tail (that regression would keep this suite green while
    restoring O(stem) analysis time)."""
    import drumgen.profile as profile_mod
    audio = [chug_bar()] * 8
    samples = [v for bar in audio for v in bar]
    seen = {}
    real = profile_mod.band_energies_zc

    def spy(s, sr, hop=512, **kw):
        seen["n"] = len(s)
        return real(s, sr, hop, **kw)
    monkeypatch.setattr(profile_mod, "band_energies_zc", spy)
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "di.wav")
        _write_float_wav(wav, samples)
        rpp = (
            '<REAPER_PROJECT 0.1\n  TEMPO 120 4 4 0\n'
            '  <TRACK {AAA}\n    NAME GTR_DI\n'
            '    <ITEM\n      POSITION 0\n      LENGTH 16\n'
            f'      <SOURCE WAVE\n        FILE "di.wav"\n      >\n    >\n  >\n>\n'
        )
        p = os.path.join(d, "song.RPP")
        open(p, "w").write(rpp)
        prof = profile_track(p, "GTR_DI", start_bar=4, bars=bars)
    assert [r["bar"] for r in prof["bars"]] == \
        list(range(5, 5 + (bars or 4)))
    bar_len = len(audio[0])
    assert abs(seen["n"] - expect_bars_analyzed * bar_len) <= 512, seen


def test_explicit_bars_clamped_by_max_seconds():
    """Both flags together: the tighter one wins. An explicit bars larger
    than the max_seconds window used to run profile_bars into the analysis
    post-roll and report it as a requested bar."""
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
        prof = profile_track(p, "GTR_DI", bars=10,
                             start_bar=1, max_seconds=2 * BAR_S)
        assert [r["bar"] for r in prof["bars"]] == [2, 3]  # not the post-roll
        # and the reverse: a tight bars beats a loose max_seconds
        prof = profile_track(p, "GTR_DI", bars=1,
                             start_bar=1, max_seconds=10 * BAR_S)
        assert [r["bar"] for r in prof["bars"]] == [2]


def test_max_seconds_partial_bar_does_not_leak_a_full_bar():
    """An artificial cap uses FLOOR: 2.43 bars of max_seconds must report
    2 bars, not 3 — the post-roll read supplies the third bar's audio, so
    a ceil there reported a full bar of music past the requested span
    (real leak: --max-seconds 4 at 146 BPM printed 3 bars). A cap typed a
    hair short of an exact bar line still gets the 2% tolerance."""
    audio = [chug_bar()] * 4
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
        # 2.435 bars requested -> 2 bars reported (bars=None and explicit)
        prof = profile_track(p, "GTR_DI", max_seconds=2.435 * BAR_S)
        assert [r["bar"] for r in prof["bars"]] == [1, 2]
        prof = profile_track(p, "GTR_DI", bars=10, max_seconds=2.435 * BAR_S)
        assert [r["bar"] for r in prof["bars"]] == [1, 2]
        # a cap 1.5% short of two exact bars still counts both
        prof = profile_track(p, "GTR_DI", max_seconds=1.985 * BAR_S)
        assert [r["bar"] for r in prof["bars"]] == [1, 2]


def test_max_seconds_shorter_than_one_bar_is_rejected():
    """A cap that cannot hold one complete bar must refuse loudly, not
    silently report a bar of music outside the requested span."""
    audio = [chug_bar()] * 2
    samples = [v for bar in audio for v in bar]
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "di.wav")
        _write_float_wav(wav, samples)
        rpp = (
            '<REAPER_PROJECT 0.1\n  TEMPO 120 4 4 0\n'
            '  <TRACK {AAA}\n    NAME GTR_DI\n'
            '    <ITEM\n      POSITION 0\n      LENGTH 4\n'
            f'      <SOURCE WAVE\n        FILE "di.wav"\n      >\n    >\n  >\n>\n'
        )
        p = os.path.join(d, "song.RPP")
        open(p, "w").write(rpp)
        with pytest.raises(ValueError, match="shorter than one bar"):
            profile_track(p, "GTR_DI", max_seconds=0.5 * BAR_S)


def test_max_seconds_zero_negative_or_nonfinite_is_rejected():
    """A non-positive or non-finite cap is a mistake, not 'no cap':
    silently profiling the whole item behind --max-seconds 0 would be the
    opposite of the flag's promise, inf used to overflow the read-length
    int() as a traceback, and nan used to die in floor(). All four refuse
    with the same clean error."""
    audio = [chug_bar()] * 2
    samples = [v for bar in audio for v in bar]
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "di.wav")
        _write_float_wav(wav, samples)
        rpp = (
            '<REAPER_PROJECT 0.1\n  TEMPO 120 4 4 0\n'
            '  <TRACK {AAA}\n    NAME GTR_DI\n'
            '    <ITEM\n      POSITION 0\n      LENGTH 4\n'
            f'      <SOURCE WAVE\n        FILE "di.wav"\n      >\n    >\n  >\n>\n'
        )
        p = os.path.join(d, "song.RPP")
        open(p, "w").write(rpp)
        for bad in (0.0, -3.0, float("inf"), float("nan")):
            with pytest.raises(ValueError, match="positive finite"):
                profile_track(p, "GTR_DI", max_seconds=bad)


def test_cli_rejects_nonfinite_max_seconds_cleanly():
    """End to end through drumgen.profile's __main__: 'inf' from the CLI
    must exit 1 with a one-line error, not a traceback."""
    import subprocess
    skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    audio = [chug_bar()] * 2
    samples = [v for bar in audio for v in bar]
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "di.wav")
        _write_float_wav(wav, samples)
        rpp = (
            '<REAPER_PROJECT 0.1\n  TEMPO 120 4 4 0\n'
            '  <TRACK {AAA}\n    NAME GTR_DI\n'
            '    <ITEM\n      POSITION 0\n      LENGTH 4\n'
            f'      <SOURCE WAVE\n        FILE "di.wav"\n      >\n    >\n  >\n>\n'
        )
        p = os.path.join(d, "song.RPP")
        open(p, "w").write(rpp)
        for bad in ("inf", "nan"):
            r = subprocess.run(
                [sys.executable, "-m", "drumgen.profile", p, "GTR_DI",
                 "0", "0", bad],
                cwd=skill_root, capture_output=True, text=True, timeout=120)
            assert r.returncode == 1, r.stderr
            assert "positive finite" in r.stderr
            assert "Traceback" not in r.stderr
