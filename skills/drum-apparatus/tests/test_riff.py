import os
import struct
import tempfile

from drumgen.riff import (parse_project, read_wav_mono, detect_onsets,
                          onsets_to_kick_grid, strong_onsets)


def test_onsets_to_kick_grid_quantizes():
    # 144 bpm -> 16th = 60/144/4 = 0.104166s. Hits on beats 1 and 3 of bar 1.
    step = 60.0 / 144 / 4
    times = [0.0, 8 * step, 0.001]  # step 0, step 8, and a dupe near 0
    kick = onsets_to_kick_grid(times, 144, bars=1, grid=16)
    assert kick == "x-------x-------"


def test_onsets_to_kick_grid_respects_start_bar():
    step = 60.0 / 144 / 4
    times = [16 * step]            # first 16th of bar 2
    kick = onsets_to_kick_grid(times, 144, bars=1, grid=16, start_bar=1)
    assert kick == "x---------------"


def test_offset_steps_shifts_grid_later():
    step = 60.0 / 144 / 4
    times = [0.0]                                    # hit at step 0
    assert onsets_to_kick_grid(times, 144, 1, offset_steps=0) == "x" + "-" * 15
    assert onsets_to_kick_grid(times, 144, 1, offset_steps=1) == "-x" + "-" * 14


def test_strong_onsets_keeps_loudest():
    onsets = [(0.0, 1.0), (0.5, 9.0), (1.0, 2.0), (1.5, 8.0)]
    kept = strong_onsets(onsets, 50)         # top half by strength
    assert set(kept) == {0.5, 1.5}


def _write_float_wav(path, sr, samples_stereo):
    n = len(samples_stereo)
    with open(path, "wb") as f:
        data = struct.pack("<%df" % (n * 2), *[v for lr in samples_stereo for v in lr])
        f.write(b"RIFF"); f.write(struct.pack("<I", 36 + len(data))); f.write(b"WAVE")
        f.write(b"fmt "); f.write(struct.pack("<IHHIIHH", 16, 3, 2, sr, sr * 8, 8, 32))
        f.write(b"data"); f.write(struct.pack("<I", len(data))); f.write(data)


def test_read_float_wav_and_detect_clicks():
    sr = 48000
    frames = [(0.0, 0.0)] * sr  # 1 second of silence...
    for t in (0.2, 0.5, 0.8):   # ...with sharp clicks at 3 known times
        i = int(t * sr)
        for j in range(20):
            frames[i + j] = (0.9, 0.9)
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "clk.wav")
        _write_float_wav(wav, sr, frames)
        rsr, mono = read_wav_mono(wav)
        assert rsr == sr and abs(len(mono) - sr) <= 1
        onsets = detect_onsets(mono, sr)
        times = [t for t, _ in onsets]
        assert len(times) == 3
        for expect in (0.2, 0.5, 0.8):
            assert any(abs(t - expect) < 0.02 for t in times)


def test_parse_project_reads_tempo_and_item():
    rpp = (
        '<REAPER_PROJECT 0.1\n  TEMPO 144 4 4 0\n'
        '  <TRACK {AAA}\n    NAME GTR_1\n'
        '    <ITEM\n      POSITION 1.5\n      LENGTH 43\n'
        '      <SOURCE WAVE\n        FILE "Media/riff.wav"\n      >\n    >\n  >\n>\n'
    )
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.RPP")
        open(p, "w").write(rpp)
        proj = parse_project(p, "GTR_1")
        assert proj["tempo"] == 144.0
        assert len(proj["items"]) == 1
        assert proj["items"][0]["position"] == 1.5
        assert proj["items"][0]["source"] == os.path.join(d, "Media/riff.wav")


# ---- fix 8 (2026-07-02 review): 24-bit PCM + WAVE_FORMAT_EXTENSIBLE --------
# REAPER records 24-bit PCM by default; the reader used to raise on it and on
# the 0xFFFE extensible header some DAWs write.

def _pcm24(sample):
    return struct.pack("<i", int(sample * 8388607))[:3]  # low 3 LE bytes


def _wav(tag, bits, frames, sr=48000, nch=1, extensible=False):
    if bits == 24:
        payload = b"".join(_pcm24(s) for s in frames)
    elif tag == 3:
        payload = struct.pack("<%df" % len(frames), *frames)
    else:
        payload = struct.pack("<%dh" % len(frames),
                              *(int(s * 32767) for s in frames))
    align = nch * bits // 8
    if extensible:
        guid = struct.pack("<H", tag) + b"\x00\x00" + bytes(
            (0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71))
        fmt = struct.pack("<HHIIHH", 0xFFFE, nch, sr, sr * align, align, bits)
        fmt += struct.pack("<HHI", 22, bits, 0) + guid
    else:
        fmt = struct.pack("<HHIIHH", tag, nch, sr, sr * align, align, bits)
    chunks = (b"fmt " + struct.pack("<I", len(fmt)) + fmt + (b"\x00" if len(fmt) & 1 else b"")
              + b"data" + struct.pack("<I", len(payload)) + payload
              + (b"\x00" if len(payload) & 1 else b""))
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def _roundtrip(blob):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.wav")
        open(p, "wb").write(blob)
        return read_wav_mono(p)


def test_read_wav_24bit_pcm():
    frames = [0.5, -0.5, 0.25, -1.0, 0.0]
    sr, mono = _roundtrip(_wav(1, 24, frames))
    assert sr == 48000
    assert len(mono) == len(frames)
    for got, want in zip(mono, frames):
        assert abs(got - want) < 1e-3


def test_read_wav_extensible_24bit_pcm():
    frames = [0.5, -0.25]
    _, mono = _roundtrip(_wav(1, 24, frames, extensible=True))
    for got, want in zip(mono, frames):
        assert abs(got - want) < 1e-3


def test_read_wav_extensible_float32():
    frames = [0.5, -0.125, 1.0]
    _, mono = _roundtrip(_wav(3, 32, frames, extensible=True))
    for got, want in zip(mono, frames):
        assert abs(got - want) < 1e-6


def test_read_wav_16bit_still_works():
    frames = [0.5, -0.5]
    _, mono = _roundtrip(_wav(1, 16, frames))
    for got, want in zip(mono, frames):
        assert abs(got - want) < 1e-3
