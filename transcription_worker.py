"""Model worker for drum_transcription.py; optional dependencies stay here."""
from pathlib import Path
from collections import Counter
import importlib.metadata
import json
import os
import random
import subprocess
import sys
import threading
import time

from drum_transcription import LIMITATIONS, file_identity, map_events, write_json


def check():
    import torch
    import librosa
    import soundfile
    import scipy
    import mido
    import demucs
    from adtof_pytorch import get_default_weights_path
    weights = get_default_weights_path()
    if not weights or not Path(weights).is_file():
        raise ValueError("ADTOF weights are missing")
    return {name: importlib.metadata.version(name) for name in
            ("torch", "librosa", "soundfile", "scipy", "demucs", "adtof-pytorch")}


class Progress:
    def __init__(self, folder, job_id):
        self.folder, self.lock, self.done = folder, threading.Lock(), threading.Event()
        self.data = {"job_id": job_id, "state": "running", "stage": "loading", "limitations": LIMITATIONS}
        self.update()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def update(self, **values):
        with self.lock:
            self.data.update(values)
            self.data["updated_at"] = time.time()
            write_json(self.folder / "status.json", self.data)

    def _heartbeat(self):
        while not self.done.wait(10):
            self.update()

    def finish(self, **values):
        self.done.set()
        self.thread.join(timeout=15)
        self.update(**values)


def infer(path, progress, device):
    import numpy as np
    import torch
    from adtof_pytorch import (calculate_n_bins, create_frame_rnn_model,
                              get_default_weights_path, load_audio_for_model, load_pytorch_weights)
    model = create_frame_rnn_model(calculate_n_bins())
    load_pytorch_weights(model, get_default_weights_path(), strict=True)
    model.eval().to(device)
    x = load_audio_for_model(str(path))
    prediction = np.zeros((x.shape[1], 5), dtype=np.float32)
    with torch.inference_mode():
        for start in range(0, x.shape[1], 2000):
            end = min(x.shape[1], start + 2000)
            left, right = max(0, start - 200), min(x.shape[1], end + 200)
            part = model(x[:, left:right].to(device)).cpu().numpy()[0]
            prediction[start:end] = part[start-left:end-left]
            progress.update(stage="transcribing", analyzed_seconds=end / 100)
    return prediction


def detect_hits(audio, sr, prediction):
    """The Ohio Rizz extraction: neural peaks, roll recovery, audio onsets.

    Returns kit-independent roles and measured strengths. No grid snapping or
    random timing changes. Individual tom/cymbal articulations are estimates.
    """
    import numpy as np
    from scipy.signal import stft, find_peaks
    from scipy.ndimage import gaussian_filter1d
    from adtof_pytorch import PeakPicker
    labels = [35, 38, 47, 42, 49]
    hits = PeakPicker().pick(prediction)[0]
    for c in (0, 1, 2):
        indices, _ = find_peaks(prediction[:, c], height=.52, prominence=.24, distance=5)
        for index in indices:
            t = float(index) / 100
            if all(abs(t-h) > .045 for h in hits[labels[c]]):
                hits[labels[c]].append(t)
        hits[labels[c]].sort()
    y = audio.mean(axis=1)
    f, ft, spectrum = stft(y, sr, nperseg=2048, noverlap=1828)
    magnitude = np.abs(spectrum)
    del spectrum
    flux = np.pad(np.maximum(magnitude[:, 1:] - magnitude[:, :-1], 0), ((0, 0), (1, 0)))
    bands = {35: (30, 160), 38: (160, 7000), 47: (65, 650), 42: (3500, 16000), 49: (2000, 16000)}
    envelopes = {lab: flux[(f >= low) & (f <= high)].sum(axis=0) for lab, (low, high) in bands.items()}
    del flux
    rows = []
    duration = len(audio) / sr
    for label, times in hits.items():
        for t in times:
            a = max(0, int(np.searchsorted(ft, t-.035)))
            b = min(len(ft), int(np.searchsorted(ft, t+.015))+1)
            if b <= a:
                continue
            env = envelopes[label]
            k = a + int(np.argmax(env[a:b]))
            onset = max(0, float(ft[k])-.004)
            if onset >= duration:
                continue
            attack = magnitude[:, k:min(len(ft), k+8)].mean(axis=1)
            pre = magnitude[:, max(0, k-10):max(1, k-3)].mean(axis=1)
            residual = np.maximum(attack-pre*.8, 0)
            low = (f >= 65) & (f <= 350)
            freq = float(f[low][np.argmax(gaussian_filter1d(residual[low], .7))])
            n = int(onset*sr)
            segment = audio[n:min(len(audio), n+int(.08*sr))]
            power = np.mean(segment**2, axis=0)+1e-12
            pan = float((power[1]-power[0])/power.sum()) if len(power) > 1 else 0.0
            rows.append({"time": onset, "model_time": t, "label": label, "strength": float(env[k]),
                         "frequency": freq, "pan": pan,
                         "confidence": float(prediction[min(len(prediction)-1, round(t*100)), labels.index(label)])})
    rows.sort(key=lambda r: (r["time"], r["label"]))
    clean, last = [], {}
    for row in rows:
        label = row["label"]
        if label in last and row["time"]-last[label]["time"] < .045:
            if row["confidence"] > last[label]["confidence"]:
                clean.remove(last[label]); clean.append(row); last[label] = row
        else:
            clean.append(row); last[label] = row
    rows = sorted(clean, key=lambda r: (r["time"], r["label"]))
    for label in labels:
        group = [r for r in rows if r["label"] == label]
        if not group:
            continue
        low, high = np.percentile([r["strength"] for r in group], [10, 90])
        for row in group:
            amount = float(np.clip((row["strength"]-low)/(high-low+1e-10), 0, 1))
            if label == 35:
                role, velocity = "KICK_R", round(86+27*amount)
            elif label == 38:
                role, velocity = "SNARE", round(90+20*amount)
            elif label == 47:
                freq = row["frequency"]
                role = "TOM_1" if freq >= 190 else "TOM_2" if freq >= 140 else "TOM_3" if freq >= 105 else "TOM_4"
                velocity = round(79+31*amount)
            elif label == 42:
                role, velocity = "HH_OPEN_1", round(72+32*amount)
            else:
                role, velocity = ("CRASH_L" if row["pan"] < 0 else "CRASH_R"), round(86+29*amount)
            row.update(role=role, velocity=velocity)
    return rows


def export_midi(path, rows, duration):
    import mido
    sys.path.insert(0, str(Path(__file__).parent / "skills" / "drum-apparatus"))
    from drumgen.catalog import load_maps
    events = map_events(rows, load_maps()["GM Standard"], "GM Standard", duration)
    mid = mido.MidiFile(type=0, ticks_per_beat=960)
    track = mido.MidiTrack(); mid.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name="Drum transcription (GM)", time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    messages = []
    for event in events:
        for key, priority, velocity, t in (("note_on", 1, event["velocity"], event["time"]),
                                          ("note_off", 0, 0, event["time"]+event["duration"])):
            messages.append((round(t*1920), priority, mido.Message(key, channel=9, note=event["pitch"], velocity=velocity)))
    previous = 0
    for tick, _, msg in sorted(messages, key=lambda row: (row[0], row[1])):
        msg.time = tick-previous; track.append(msg); previous = tick
    track.append(mido.MetaMessage("end_of_track", time=max(0, round(duration*1920)-previous)))
    mid.save(str(path))


def run(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    progress = Progress(folder, manifest["job_id"])
    try:
        import numpy as np
        import soundfile as sf
        import torch
        dependencies = check()
        torch.set_num_threads(manifest["threads"])
        torch.manual_seed(0)
        random.seed(0)
        device = manifest["device"]
        if device == "cuda" and not torch.cuda.is_available():
            raise ValueError("CUDA was requested but is unavailable; start a CPU job")
        source = manifest["source"]
        if file_identity(source["source_file"]) != manifest["source_file_identity"]:
            raise ValueError("Source file changed before analysis")
        progress.update(stage="decoding")
        aligned = folder / "source-aligned.wav"
        subprocess.run([manifest["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
                        "-ss", str(source["source_offset"]), "-i", source["source_file"],
                        "-t", str(source["length"]), "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(aligned)],
                       check=True, timeout=180, **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        audio, sr = sf.read(aligned, dtype="float32", always_2d=True)
        if abs(len(audio)/sr-source["length"]) > .003:
            raise ValueError("Decoded source is shorter than the item; loops and missing tail audio are unsupported")
        if not np.isfinite(audio).all() or float(np.sqrt(np.mean(audio**2))) < 1e-6:
            raise ValueError("Source audio is silent or invalid")
        if manifest["separate"]:
            from demucs.pretrained import get_model
            from demucs.apply import apply_model
            progress.update(stage="loading_separation_model")
            model = get_model("htdemucs")
            wave = torch.from_numpy(audio.T)
            reference = wave.mean(0); mean, std = reference.mean(), reference.std()
            if float(std) < 1e-6:
                raise ValueError("Source has no usable signal variation")
            wave = (wave-mean)/std
            progress.update(stage="separating")
            with torch.inference_mode():
                stems = apply_model(model, wave[None], device=device, shifts=1, split=True,
                                    overlap=.25, progress=True, num_workers=0)[0]
            audio = (stems[model.sources.index("drums")]*std+mean).cpu().T.numpy()
            del stems, wave, model
        isolated = folder / "isolated-drums.wav"
        sf.write(isolated, audio, sr, subtype="FLOAT")
        progress.update(stage="transcribing")
        prediction = infer(isolated, progress, device)
        np.save(folder / "activations.npy", prediction)
        progress.update(stage="refining_hits")
        rows = detect_hits(audio, sr, prediction)
        if file_identity(source["source_file"]) != manifest["source_file_identity"]:
            raise ValueError("Source audio changed during analysis")
        write_json(folder / "hits.json", rows)
        export_midi(folder / "drums-gm.mid", rows, source["length"])
        # Keep analysis samples unchanged, supply a separate unclipped audition.
        peak = float(np.max(np.abs(audio)))
        gain = min(1, 10**(-1/20)/max(peak, 1e-12))
        sf.write(folder / "drums-preview.wav", audio*gain, sr, subtype="PCM_24")
        progress.finish(state="completed", stage="complete", note_count=len(rows),
                        role_counts=dict(Counter(row["role"] for row in rows)), dependencies=dependencies,
                        duration_seconds=source["length"], midi_file=str(folder / "drums-gm.mid"),
                        audio_file=str(folder / "drums-preview.wav"), hits_file=str(folder / "hits.json"))
        return 0
    except Exception as exc:
        progress.finish(state="failed", stage="failed", error=f"{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        print(json.dumps(check()))
    elif len(sys.argv) == 2:
        sys.exit(run(sys.argv[1]))
    else:
        raise SystemExit("Usage: transcription_worker.py JOB_DIRECTORY | --check")
