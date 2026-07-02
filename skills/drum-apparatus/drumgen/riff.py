"""Read a guitar rhythm track's transients and propose a kick pattern from them.

The whole point of David's SOP: kicks come first, built off the guitar's picked
notes. He can't describe a riff in words, but the riff is already a recorded stem
in the REAPER project. So: parse the .RPP for the track's audio item + source
WAV, detect note onsets in the audio, quantize to the grid, hand back a kick
string he edits. Transients give onset TIMING, not open-vs-muted — so this is a
PROPOSAL, not a finished beat.

ponytail: stdlib only. Float-WAV reader + energy-flux onset detector beats pulling
in numpy/librosa for a one-shot read. Upgrade to a spectral detector only if the
energy one misses notes on real material.
"""
import array
import os
import re
import struct
import sys


# ---- RPP parsing -----------------------------------------------------------

def parse_project(rpp_path, track_name):
    """Return {tempo, items:[{source, position, length}]} for the named track.
    source is an absolute path to the item's WAV."""
    with open(rpp_path, "r", errors="replace") as f:
        text = f.read()
    proj_dir = os.path.dirname(os.path.abspath(rpp_path))

    m = re.search(r"^\s*TEMPO\s+([0-9.]+)", text, re.M)
    tempo = float(m.group(1)) if m else 120.0

    # Split into TRACK blocks and find ours by NAME (quoted or bare).
    items = []
    for blk in re.split(r"^\s*<TRACK ", text, flags=re.M)[1:]:
        nm = re.search(r'^\s*NAME\s+"?([^"\n]+?)"?\s*$', blk, re.M)
        if not nm or nm.group(1).strip() != track_name:
            continue
        for item in re.split(r"^\s*<ITEM", blk, flags=re.M)[1:]:
            pos = re.search(r"^\s*POSITION\s+([0-9.]+)", item, re.M)
            length = re.search(r"^\s*LENGTH\s+([0-9.]+)", item, re.M)
            src = re.search(r'FILE\s+"?([^"\n]+?)"?\s*$', item, re.M)
            if pos and src:
                path = src.group(1)
                if not os.path.isabs(path):
                    path = os.path.join(proj_dir, path)
                items.append({"source": path,
                              "position": float(pos.group(1)),
                              "length": float(length.group(1)) if length else None})
        break
    return {"tempo": tempo, "items": items}


# ---- WAV reading (handles 32-bit float, which stdlib `wave` rejects) -------

def read_wav_mono(path, max_seconds=None):
    """Return (sample_rate, [mono float samples]). Supports PCM int16/24/32,
    IEEE float32 (fmt tag 3), and WAVE_FORMAT_EXTENSIBLE (0xFFFE) wrapping
    either. Downmixes to mono."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"not a WAV: {path}")
    fmt = None
    raw = None
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        sz = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + sz]
        if cid == b"fmt ":
            tag, nch, sr, _byterate, _align, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:
                # WAVE_FORMAT_EXTENSIBLE: the effective tag is the first two
                # bytes of the SubFormat GUID (fmt body offset 24).
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, nch, sr, bits)
        elif cid == b"data":
            raw = body
        pos += 8 + sz + (sz & 1)  # chunks are word-aligned
    if fmt is None or raw is None:
        raise ValueError(f"missing fmt/data chunk: {path}")
    tag, nch, sr, bits = fmt
    if max_seconds is not None:
        raw = raw[: int(max_seconds * sr) * nch * (bits // 8)]

    if tag == 3 and bits == 32:                      # IEEE float
        a = array.array("f"); a.frombytes(raw[: len(raw) // 4 * 4])
        flo = a
    elif tag == 1 and bits == 16:                    # PCM 16
        a = array.array("h"); a.frombytes(raw[: len(raw) // 2 * 2])
        flo = array.array("f", (x / 32768.0 for x in a))
    elif tag == 1 and bits == 24:                    # PCM 24, REAPER's default
        n = len(raw) // 3
        buf = bytearray(n * 4)      # 3 LE bytes into the top of each int32:
        buf[1::4] = raw[:n * 3:3]   # a free <<8 with sign extension, so the
        buf[2::4] = raw[1:n * 3:3]  # 32-bit scale divisor applies unchanged
        buf[3::4] = raw[2:n * 3:3]
        a = array.array("i"); a.frombytes(bytes(buf))
        flo = array.array("f", (x / 2147483648.0 for x in a))
    elif tag == 1 and bits == 32:                    # PCM 32
        a = array.array("i"); a.frombytes(raw[: len(raw) // 4 * 4])
        flo = array.array("f", (x / 2147483648.0 for x in a))
    else:
        raise ValueError(f"unsupported WAV format tag={tag} bits={bits}")

    if nch > 1:
        mono = array.array("f", (sum(flo[i:i + nch]) / nch
                                 for i in range(0, len(flo) - nch + 1, nch)))
    else:
        mono = flo
    return sr, mono


# ---- onset detection -------------------------------------------------------

def detect_onsets(samples, sr, hop=512, sensitivity=1.6, min_gap_s=0.05):
    """Energy-flux onset detection. Returns a list of onset times (seconds).
    Picks local peaks of positive energy rise that beat an adaptive local mean."""
    n = len(samples)
    nh = n // hop
    energy = [0.0] * nh
    for k in range(nh):
        base = k * hop
        s = 0.0
        for i in range(base, base + hop):
            v = samples[i]; s += v * v
        energy[k] = s
    flux = [0.0] * nh
    for k in range(1, nh):
        d = energy[k] - energy[k - 1]
        flux[k] = d if d > 0 else 0.0

    onsets = []  # (time_seconds, strength)
    last = -1e9
    win = 16  # ~170ms either side at hop 512 / 48k
    for k in range(1, nh - 1):
        lo, hi = max(0, k - win), min(nh, k + win + 1)
        local = sum(flux[lo:hi]) / (hi - lo)
        t = k * hop / sr
        if (flux[k] > local * sensitivity + 1e-12
                and flux[k] >= flux[k - 1] and flux[k] >= flux[k + 1]
                and t - last >= min_gap_s):
            onsets.append((t, flux[k]))
            last = t
    return onsets


def strong_onsets(onsets, keep_pct):
    """Keep only the loudest keep_pct (0-100) of onsets by attack strength.
    This is the open/accented notes — what David's kicks actually follow —
    pulled out of a constant chug. Returns a list of times."""
    if not onsets:
        return []
    strengths = sorted(s for _, s in onsets)
    cutoff = strengths[min(len(strengths) - 1, int((1 - keep_pct / 100) * len(strengths)))]
    return [t for t, s in onsets if s >= cutoff]


# ---- quantize to a kick grid ----------------------------------------------

def onsets_to_kick_grid(onsets, tempo, bars, grid=16, item_start=0.0, start_bar=0,
                        offset_steps=0):
    """Snap onsets to a `grid`-per-bar lane and return a kick string of bars*grid
    cells ('x' = hit, '.' = rest), covering [start_bar, start_bar+bars).
    Rests are '.' because that is the only rest cell the groove DSL accepts —
    this string's whole purpose is to be pasted into a DSL kick lane.

    offset_steps: calibration shift in grid steps (+1 = everything one 16th later).
    The grid is anchored to item time 0, but a stem's musical downbeat may sit a
    step off that (lead-in / where bar 1 actually starts). David's GTR_1 read one
    16th early -> offset_steps=+1 lands it. Per-stem, so it's a knob, not a const.
    """
    step_s = (60.0 / tempo) * 4.0 / grid
    total = bars * grid
    offset = start_bar * grid
    cells = ["."] * total
    for t in onsets:
        idx = round((t - item_start) / step_s) - offset + offset_steps
        if 0 <= idx < total:
            cells[idx] = "x"
    return "".join(cells)


def format_grid(kick, grid=16):
    """Pretty-print a kick string in bars, beats space-separated."""
    lines = []
    for b in range(0, len(kick), grid):
        bar = kick[b:b + grid]
        beats = " ".join(bar[i:i + 4] for i in range(0, len(bar), 4))
        lines.append(f"bar {b // grid + 1}: {beats}")
    return "\n".join(lines)


def riff_to_kicks(rpp_path, track_name, bars=4, start_bar=0, grid=16,
                  keep_pct=100, offset_steps=0, max_seconds=None):
    """End to end: project + track name -> proposed kick string + meta.
    keep_pct<100 keeps only the loudest attacks (open/accented notes -> sparser,
    closer to David's one-kick-per-transient slam style). keep_pct=100 keeps every
    pick attack (gallops/triplets on fast riffs). offset_steps calibrates phase."""
    proj = parse_project(rpp_path, track_name)
    if not proj["items"]:
        raise ValueError(f"no audio items on track {track_name!r} in {rpp_path}")
    if len(proj["items"]) > 1:
        # Comped/spliced tracks have several items; only the first is analyzed,
        # so say so instead of silently transcribing a fraction of the riff.
        print(f"[riff] WARNING: track {track_name!r} has {len(proj['items'])} items; "
              f"only the first (at {proj['items'][0]['position']:.2f}s) is analyzed.",
              file=sys.stderr)
    it = proj["items"][0]
    sr, mono = read_wav_mono(it["source"], max_seconds=max_seconds)
    onsets = detect_onsets(mono, sr)
    times = strong_onsets(onsets, keep_pct) if keep_pct < 100 else [t for t, _ in onsets]
    kick = onsets_to_kick_grid(times, proj["tempo"], bars, grid, it["position"],
                               start_bar, offset_steps)
    return {"tempo": proj["tempo"], "source": it["source"], "sr": sr,
            "onsets": onsets, "kept": len(times), "kick": kick, "grid": grid,
            "item_count": len(proj["items"])}


if __name__ == "__main__":
    import sys
    rpp, track = sys.argv[1], sys.argv[2]
    bars = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    start = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    proj = parse_project(rpp, track)
    sr, mono = read_wav_mono(proj["items"][0]["source"])
    onsets = detect_onsets(mono, sr)
    tempo, pos = proj["tempo"], proj["items"][0]["position"]
    print(f"tempo {tempo}  sr {sr}  total onsets {len(onsets)}  (bars {start+1}..{start+bars})")
    for pct in (100, 50, 30):
        times = strong_onsets(onsets, pct) if pct < 100 else [t for t, _ in onsets]
        kick = onsets_to_kick_grid(times, tempo, bars, 16, pos, start)
        print(f"\n--- keep {pct}% strongest ({len(times)} onsets) ---")
        print(format_grid(kick, 16))
