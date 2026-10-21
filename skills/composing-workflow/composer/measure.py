"""Measure what a patch actually SOUNDS like. Labels, octave conventions and
Transpose knobs lie; an FFT of the rendered audio does not.

    from composer import measure
    res = measure.probe(send, "Argent 3.5-L", [28, 31, 40], bpm=140, bar=101,
                        out_dir=r"C:\\temp\\probe", also_solo=[])
    for r in res: print(r["midi"], measure.analyze(r["file"]))

`send` is reaperd.send_type (or any callable(type, payload, timeout_ms=...)).
probe() places a temporary item past the song end, solos the track (and any
track its audio routes through, e.g. an instrument feeding a bus), captures each
note with capture_track_audio, then always unsolos and deletes the probe item.
Capture a baseline with no note too (`baseline=True`) to prove the sound comes
from the instrument. Watch for other soloed tracks leaking into the capture.
analyze() needs numpy + soundfile.
"""
import math
import os
import time

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def hz_to_midi(f):
    return 69 + 12 * math.log2(f / 440.0)


def name(midi_float):
    m = round(midi_float)
    return f"{NAMES[m % 12]}{m // 12 - 1}"     # scientific pitch (MIDI 60 = C4)


def probe(send, track, pitches, *, bpm, bar, out_dir, beats_per_bar=4,
          also_solo=(), velocity=100, hold_beats=7.5, baseline=False):
    spb = beats_per_bar * 60.0 / bpm
    os.makedirs(out_dir, exist_ok=True)
    plan = [(i * 2, p) for i, p in enumerate(pitches)]
    if baseline:
        plan.append((len(pitches) * 2, None))
    nbars = len(plan) * 2
    ev = [dict(type="note", time=off * beats_per_bar + 0.01, duration=hold_beats,
               pitch=p, velocity=velocity) for off, p in plan if p is not None]
    r = send("insert_midi_events", {"target_track_name": track, "time_unit": "beats",
                                    "start_bar": bar, "length_beats": nbars * beats_per_bar,
                                    "events": ev})
    if not r.get("ok"):
        raise RuntimeError(f"probe insert failed: {r}")
    solos = [track, *also_solo]
    out = []
    try:
        for t in solos:
            send("solo_track", {"target_track_name": t, "solo": True})
        for off, p in plan:
            f = os.path.join(out_dir, f"{track.replace(' ', '_')}_{p if p is not None else 'none'}_{int(time.time())}.wav")
            c = send("capture_track_audio", {"target_track_name": track, "duration_seconds": 1.5,
                     "start_seconds": (bar - 1 + off) * spb + 0.3, "output_file": f},
                     timeout_ms=120000)
            out.append(dict(midi=p, file=f, ok=bool(c.get("ok")), error=c.get("error")))
    finally:
        for t in solos:
            send("solo_track", {"target_track_name": t, "solo": False})
        send("delete_items_in_range", {"target_track_name": track,
             "range": {"type": "time", "seconds": (bar - 1) * spb + 0.005,
                       "length_seconds": nbars * spb - 0.01},
             "length_seconds": nbars * spb - 0.01})
    return out


def analyze(path, lo_hz=25, hi_hz=1000, top=8, rel=0.15):
    """-> dict(rms, peaks=[(hz, midi_float, name, rel_mag)]) lowest first.
    The fundamental is usually the lowest strong peak; check harmonics agree."""
    import numpy as np
    import soundfile as sf
    x, sr = sf.read(path)
    if x.ndim > 1:
        x = x.mean(axis=1)
    seg = x[int(0.2 * sr):int(1.2 * sr)]
    seg = seg * np.hanning(len(seg))
    X = np.abs(np.fft.rfft(seg, 8 * len(seg)))
    fr = np.fft.rfftfreq(8 * len(seg), 1 / sr)
    band = (fr > lo_hz) & (fr < hi_hz)
    Xb, fb = X[band], fr[band]
    mx = Xb.max() if len(Xb) else 0
    pk = [i for i in range(1, len(Xb) - 1)
          if Xb[i] > Xb[i - 1] and Xb[i] > Xb[i + 1] and Xb[i] > rel * mx]
    pk = sorted(pk, key=lambda i: -Xb[i])[:top]
    peaks = [(round(float(fb[i]), 1), round(hz_to_midi(fb[i]), 2), name(hz_to_midi(fb[i])),
              round(float(Xb[i] / mx), 2)) for i in sorted(pk, key=lambda i: fb[i])]
    return dict(rms=round(float(np.sqrt((seg ** 2).mean())), 4), peaks=peaks)


def offset(sent_midi, sounding_midi_float):
    """Semitone offset sent -> sounding (e.g. -3 means it sounds 3 st low)."""
    return round(sounding_midi_float) - sent_midi
