"""Profile a guitar stem bar-by-bar so an agent can hear its shape in numbers.

riff.py answers WHEN David picks. This answers WHAT KIND of playing each bar
is: dense machine-regular tremolo, tight palm-muted chugs with air between
them, ringing open chords, half-silent stop-start djent figures. It emits
per-bar FEATURES — numbers, not labels. The agent reading the table does the
labeling ("bars 9-16 look like a breakdown") and David corrects it, same
contract as riff's kick proposal. Hard thresholds ("density>6 = tremolo")
don't survive contact with different tempos/tunings, so none live here.

Also finds structure: bars where the feel changes (section boundaries) and
groups of bars that repeat elsewhere in the song (the repeated one is usually
the chorus — repetition is structural, not acoustic, so the code only says
"these chunks match", never "this is the chorus").

Best on a DI stem. High-gain distortion compresses the decay contrast that
separates an open ring from a dead chug; onset density and silence survive
the amp, decay_ratio much less so.

ponytail: stdlib only, same as riff.py. Band split is two one-pole IIR passes
(O(n), no FFT); everything else rides the hop-energy array riff already needs.
"""
import json
import math

from .riff import parse_project, read_wav_mono, detect_onsets, hop_energy

HOP = 512  # keep in lockstep with detect_onsets' default


# ---- signal helpers --------------------------------------------------------

def band_energies_zc(samples, sr, hop=HOP, low_hz=200.0, high_hz=2000.0):
    """Per-hop energy of a ~<low_hz band and a ~>high_hz band via one-pole
    IIR filters, plus zero-crossing counts, in ONE pass. Crude 6 dB/oct
    skirts are fine: we only compare a bar against the rest of the SAME
    stem, never against an absolute scale. The IIRs carry state sample to
    sample, so this is the analyzer's only per-sample Python loop — every
    other pass runs at C speed via math.sumprod."""
    a_lo = math.exp(-2.0 * math.pi * low_hz / sr)
    a_hi = math.exp(-2.0 * math.pi * high_hz / sr)
    b_lo = 1.0 - a_lo
    b_hi = 1.0 - a_hi
    nh = len(samples) // hop
    lo_e = [0.0] * nh
    hi_e = [0.0] * nh
    zc = [0] * nh
    ylo = 0.0
    yhi = 0.0
    prev = 0.0
    for k in range(nh):
        slo = 0.0
        shi = 0.0
        c = 0
        for v in samples[k * hop:(k + 1) * hop]:
            ylo = b_lo * v + a_lo * ylo   # low-pass @ low_hz
            yhi = b_hi * v + a_hi * yhi   # low-pass @ high_hz ...
            h = v - yhi                    # ... subtracted = high-pass
            slo += ylo * ylo
            shi += h * h
            if (v > 0.0 and prev <= 0.0) or (v < 0.0 and prev >= 0.0):
                c += 1
            prev = v
        lo_e[k] = slo
        hi_e[k] = shi
        zc[k] = c
    return lo_e, hi_e, zc


# ---- per-onset decay -------------------------------------------------------

def onset_decay_ratio(energy, onset_hop, sr, hop=HOP,
                      attack_s=(0.010, 0.060), tail_s=(0.150, 0.220),
                      next_gap_s=None):
    """Energy in the tail window over energy in the attack window, for one
    onset. Palm mutes die before the tail window opens (ratio near 0); open
    strings/chords still ring there (ratio climbs toward and past 0.5).

    next_gap_s is the time until the NEXT onset. Without it the fixed
    150-220ms tail window swallows the next hit's attack on dense playing:
    a 16th-note dead-mute chug at 100 BPM (IOI 150ms) measured MORE ringing
    (0.85) than actual open chords. Windows shrink to fit inside the gap —
    attack ends by 40% of it, tail lives in 60-90% — same question ('has
    the note died yet?'), asked before the next note answers for it.

    Returns None when the windows can't be measured honestly: hits too
    close together, or a window running off the stem end (a truncated tail
    would misread the final ringing chord as a dead mute)."""
    a0, a1 = attack_s
    t0, t1 = tail_s
    if next_gap_s is not None:
        a1 = min(a1, 0.40 * next_gap_s)
        t0 = min(t0, 0.60 * next_gap_s)
        t1 = min(t1, 0.90 * next_gap_s)
        if a1 <= a0 or t1 <= t0:
            return None

    def win_mean(w0, w1):
        k0 = onset_hop + int(w0 * sr / hop)
        k1 = max(k0 + 1, onset_hop + int(w1 * sr / hop))
        if k1 > len(energy):
            return None                   # window runs off the stem end
        seg = energy[k0:k1]
        return (sum(seg) / len(seg)) if seg else None
    att = win_mean(a0, a1)
    tail = win_mean(t0, t1)
    if att is None or tail is None or att <= 0.0:
        return None
    return tail / att


# ---- the per-bar table -----------------------------------------------------

def _median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _db(x):
    return 10.0 * math.log10(x) if x > 0.0 else -120.0


def bar_grid(bar_steps, grid=16):
    """One bar of (step_index, strength) onsets as a 16-cell accent string:
    X accented, x normal, . rest. Accented = at least twice this bar's median
    attack strength — 'punches above the bar's typical', so an even wall of
    16ths gets NO X (all typical) instead of a rank-forced top third."""
    cells = ["."] * grid
    if not bar_steps:
        return "".join(cells)
    med = _median([s for _, s in bar_steps])
    cut = 2.0 * med if med else float("inf")
    for idx, s in bar_steps:
        if 0 <= idx < grid:
            mark = "X" if s >= cut else "x"
            if cells[idx] != "X":   # an X never downgrades to x
                cells[idx] = mark
    return "".join(cells)


def profile_bars(samples, sr, tempo, bars, start_bar=0, hop=HOP, grid=16,
                 origin_hops=0):
    """The core table: one feature dict per bar. Assumes 4/4 (every stem of
    David's so far; revisit if a 7/8 riff ever shows up).

    Bars are counted from ITEM time 0, same anchor riff.py documents — a
    stem whose musical downbeat sits off the item start gets start_bar /
    riff's offset_steps treatment, not silent correction here.

    origin_hops: samples[0] sits origin_hops*hop samples into the item
    (profile_track's windowed analysis). Bar windows and onset bucketing
    use item-absolute time via integer hop arithmetic, so a windowed run
    reproduces the full run's rows exactly instead of drifting by hop
    phase. start_bar stays item-absolute."""
    bar_s = 60.0 / tempo * 4.0
    step_s = bar_s / grid
    energy = hop_energy(samples, hop)
    lo_e, hi_e, zc = band_energies_zc(samples, sr, hop)
    onsets = detect_onsets(samples, sr, hop=hop, energy=energy)
    # Time to the next onset ANYWHERE in the stem (not just this bar), so
    # decay windows never swallow the following hit's attack. Gaps come from
    # INTEGER hop indices, not t2 - t1: the float subtraction wiggles by an
    # ulp depending on where the analysis window was cut, and 0.6*gap lands
    # exactly on a hop edge for common IOIs, so that ulp used to flip a
    # decay window by a whole hop between windowed and full runs.
    _ohops = [int(round(t * sr / hop)) for t, _ in onsets]
    next_gap = {t: ((_ohops[i + 1] - _ohops[i]) * hop / sr
                    if i + 1 < len(onsets) else None)
                for i, (t, _) in enumerate(onsets)}

    # Silence threshold: 30 dB under the stem's loud reference (95th pct of
    # non-zero hop energy). Adaptive, so DI level vs amped level doesn't matter.
    nz = sorted(e for e in energy if e > 0.0)
    loud_ref = nz[int(len(nz) * 0.95)] if nz else 0.0
    silence_thresh = loud_ref * 1e-3

    # Bucket onsets by NEAREST grid step, then step -> bar. Detected times
    # jitter by up to a hop either side of the true attack, and a downbeat
    # detected 20ms early belongs to THIS bar, not the previous one.
    per_bar = {}
    for t, s in onsets:
        gi = round((t + origin_hops * hop / sr) / step_s)
        per_bar.setdefault(gi // grid, []).append((gi % grid, s, t))

    rows = []
    for b in range(start_bar, start_bar + bars):
        t0 = b * bar_s
        t1 = t0 + bar_s
        k0 = int(t0 * sr / hop) - origin_hops
        k1 = int(t1 * sr / hop) - origin_hops
        if k1 <= 0:
            continue
        k0 = max(k0, 0)
        seg = energy[k0:k1]
        if not seg:
            break
        b_on = [(t, s) for _, s, t in sorted(per_bar.get(b, []))]

        # timing features
        density = len(b_on) / bar_s
        iois = [b_on[i + 1][0] - b_on[i][0] for i in range(len(b_on) - 1)]
        if len(iois) >= 2:
            m = sum(iois) / len(iois)
            var = sum((x - m) ** 2 for x in iois) / len(iois)
            ioi_cv = math.sqrt(var) / m if m > 0 else None
        else:
            ioi_cv = None

        # decay: median over the bar's onsets (robust to one weird hit)
        decays = []
        for t, _ in b_on:
            r = onset_decay_ratio(energy, int(round(t * sr / hop)), sr, hop,
                                  next_gap_s=next_gap.get(t))
            if r is not None:
                decays.append(min(r, 4.0))  # cap: tail>attack blowups are noise
        decay_ratio = _median(decays)

        # level / spectrum features
        mean_e = sum(seg) / len(seg)
        peak_e = max(seg)
        silence_ratio = sum(1 for e in seg if e < silence_thresh) / len(seg)
        lo = sum(lo_e[k0:k1])
        hi = sum(hi_e[k0:k1])
        tot = sum(seg)
        low_ratio = lo / tot if tot > 0 else 0.0
        bright = hi / tot if tot > 0 else 0.0
        zcr = sum(zc[k0:k1]) / len(seg) / hop * sr  # crossings per second

        rows.append({
            "bar": b + 1,                      # 1-based, like riff's printout
            "n_onsets": len(b_on),
            "density_hz": round(density, 2),
            "ioi_cv": round(ioi_cv, 3) if ioi_cv is not None else None,
            "decay_ratio": round(decay_ratio, 3) if decay_ratio is not None else None,
            "silence_ratio": round(silence_ratio, 3),
            "rms_db": round(_db(mean_e / hop), 1),
            "crest_db": round(_db(peak_e / mean_e) if mean_e > 0 else 0.0, 1),
            "low_ratio": round(low_ratio, 3),
            "bright_ratio": round(bright, 3),
            "zcr_hz": round(zcr, 0),
            "grid": bar_grid([(gi, s) for gi, s, _ in per_bar.get(b, [])], grid),
        })
    return rows


# ---- structure: boundaries + repeats ---------------------------------------

_VEC_KEYS = ("density_hz", "ioi_cv", "decay_ratio", "silence_ratio",
             "rms_db", "low_ratio", "bright_ratio")

# Differences smaller than these are musically meaningless. They floor the
# z-score sigma so a song of 32 near-identical bars (sigma ~ numeric noise)
# doesn't have its wiggles amplified into fake section boundaries.
_SIGMA_FLOOR = {"density_hz": 0.25, "ioi_cv": 0.03, "decay_ratio": 0.03,
                "silence_ratio": 0.03, "rms_db": 1.0, "low_ratio": 0.02,
                "bright_ratio": 0.02}


def _bar_vectors(rows):
    """z-scored feature vectors (None -> that stem's mean, i.e. z of 0)."""
    cols = {}
    for k in _VEC_KEYS:
        vals = [r[k] for r in rows if r[k] is not None]
        if not vals:
            cols[k] = (0.0, 1.0)
            continue
        m = sum(vals) / len(vals)
        var = sum((v - m) ** 2 for v in vals) / len(vals)
        cols[k] = (m, max(math.sqrt(var), _SIGMA_FLOOR[k]))
    vecs = []
    for r in rows:
        vecs.append([((r[k] if r[k] is not None else cols[k][0]) - cols[k][0])
                     / cols[k][1] for k in _VEC_KEYS])
    return vecs


def find_boundaries(rows, threshold=1.6):
    """Bars where the feel jumps: euclidean distance between adjacent bars'
    z-vectors, peak-picked above `threshold`. Returns 1-based bar numbers of
    bars that START a new section."""
    vecs = _bar_vectors(rows)
    if len(vecs) < 3:
        return []
    dists = [0.0]
    for i in range(1, len(vecs)):
        dists.append(math.sqrt(sum((a - b) ** 2
                                   for a, b in zip(vecs[i], vecs[i - 1]))))
    bounds = []
    for i in range(1, len(dists)):
        left = dists[i - 1]
        right = dists[i + 1] if i + 1 < len(dists) else 0.0
        if dists[i] >= threshold and dists[i] >= left and dists[i] >= right:
            bounds.append(rows[i]["bar"])
    return bounds


def segment(rows, boundaries):
    """Cut rows into contiguous segments at the boundary bars."""
    if not rows:
        return []
    bset = set(boundaries)
    segs = []
    start = rows[0]["bar"]
    for r in rows[1:]:
        if r["bar"] in bset:
            segs.append((start, r["bar"] - 1))
            start = r["bar"]
    segs.append((start, rows[-1]["bar"]))
    return segs


def group_segments(rows, segs, sim_threshold=0.90):
    """Label segments A, B, C...; two segments share a letter when their mean
    z-vectors match by cosine similarity. 'These chunks are the same music' —
    naming which one is the chorus is David's job, not this function's."""
    vecs = _bar_vectors(rows)
    by_bar = {r["bar"]: v for r, v in zip(rows, vecs)}
    means = []
    for s0, s1 in segs:
        vs = [by_bar[b] for b in range(s0, s1 + 1) if b in by_bar]
        n = len(vs) or 1
        means.append([sum(col) / n for col in zip(*vs)] if vs else [0.0] * len(_VEC_KEYS))

    def cos(a, b):
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 1.0 if na == nb else 0.0
        return sum(x * y for x, y in zip(a, b)) / (na * nb)

    labels = []
    reps = []  # (label, mean_vec) of first segment seen per label
    for mv in means:
        for lab, rv in reps:
            if cos(mv, rv) >= sim_threshold:
                labels.append(lab)
                break
        else:
            lab = chr(ord("A") + len(reps))
            reps.append((lab, mv))
            labels.append(lab)
    return [{"start_bar": s0, "end_bar": s1, "label": lab}
            for (s0, s1), lab in zip(segs, labels)]


# ---- end to end ------------------------------------------------------------

# Bars of audio kept around the requested window so the slice analyzes like
# the full stem. PRE: the band IIRs settle (time constants are
# milliseconds), the onset detector's ±170ms local mean has real context,
# and a hit ringing across the start boundary isn't misread as a fresh
# onset at sample 0. POST: the last requested bar's decay tails (≤220ms)
# and next-onset gaps (they only bind under 250ms, well inside one bar)
# see the audio they need. The rolls are analyzed and then discarded —
# only requested bars are reported.
PRE_ROLL_BARS = 1
POST_ROLL_BARS = 1


def profile_track(rpp_path, track_name, bars=None, start_bar=0,
                  max_seconds=None):
    """Project + track name -> full profile dict. bars=None profiles the
    whole first item (comped tracks get riff.py's same first-item-only rule).

    max_seconds caps how much MUSIC gets analyzed, measured from start_bar,
    so the two flags compose instead of the cap starving the requested
    window (a start past a time-0 cap profiled zero bars). Analysis is
    windowed: the signal passes run over the requested bars plus one
    pre-roll and one post-roll bar, never the whole prefix or tail. (The
    WAV read itself still decodes from the file start through the window's
    end; the per-sample passes are what get windowed.)"""
    proj = parse_project(rpp_path, track_name)
    if not proj["items"]:
        raise ValueError(f"no audio items on track {track_name!r} in {rpp_path}")
    it = proj["items"][0]
    bar_s = 60.0 / proj["tempo"] * 4.0

    # argparse type=float happily parses "inf" and "nan"; inf overflows the
    # read-length int() in read_wav_mono and nan dies in floor() later —
    # both as tracebacks. Refuse them here with the same clean error path.
    if max_seconds is not None and not (math.isfinite(max_seconds)
                                        and max_seconds > 0):
        raise ValueError(
            f"max_seconds must be a positive finite number (got "
            f"{max_seconds:g}); omit it to profile the whole item")

    # Requested musical endpoint (None = whole item), and the read/analysis
    # cap one post-roll bar past it.
    ends = []
    if bars is not None:
        ends.append((start_bar + bars) * bar_s)
    if max_seconds is not None:
        ends.append(start_bar * bar_s + max_seconds)
    end_s = min(ends) if ends else None
    read_cap = None if end_s is None else end_s + POST_ROLL_BARS * bar_s
    sr, mono = read_wav_mono(it["source"], max_seconds=read_cap)

    # Window: drop audio before start_bar minus the pre-roll. The cut is
    # floored to a HOP multiple and passed as origin_hops so every energy
    # hop and bar window covers the SAME samples as a full-stem run —
    # timing, onset, grid, decay, and band fields reproduce the full run
    # exactly past the pre-roll. Window-local by design (documented, not
    # drift): the silence threshold's loud reference, and the boundary /
    # section z-scores, which always describe only the profiled rows.
    skip_hops = 0
    if start_bar > PRE_ROLL_BARS:
        cut = (int((start_bar - PRE_ROLL_BARS) * bar_s * sr) // HOP) * HOP
        skip_hops = cut // HOP
        mono = mono[cut:] if cut < len(mono) else mono[:0]

    if bars is None:
        avail = skip_hops * HOP / sr + len(mono) / sr
        # NATURAL endpoint (the item's audio): ceil with a 2% sliver guard.
        # Glued/trimmed stems routinely land a few samples SHORT of the
        # exact bar line; int() truncation then silently dropped the entire
        # final bar (a 40-bar song profiled as 39 — real bug, real
        # session). Count a trailing partial bar whenever at least 2% of it
        # exists; below that it's an edit sliver, not music.
        bars = max(1, math.ceil(avail / bar_s - 0.02) - start_bar)
    if max_seconds is not None:
        # ARTIFICIAL endpoint (the user's cap): floor, mirrored 2%
        # tolerance. Only bars that FIT inside the cap are reported — ceil
        # here would report a bar of music the user did not ask for, and
        # the post-roll read supplies its audio, so it WOULD leak (a 4s cap
        # at 146 BPM is 2.43 bars and reported 3). The tolerance still
        # forgives a cap typed a hair short of an exact bar line.
        cap = math.floor(max_seconds / bar_s + 0.02)
        if cap < 1:
            raise ValueError(
                f"max_seconds {max_seconds:g} is shorter than one bar "
                f"({bar_s:.2f}s at {proj['tempo']:g} BPM); raise the cap "
                "or drop it")
        bars = min(bars, cap)
    rows = profile_bars(mono, sr, proj["tempo"], bars, start_bar,
                        origin_hops=skip_hops)
    bounds = find_boundaries(rows)
    segs = group_segments(rows, segment(rows, bounds))
    return {"tempo": proj["tempo"], "sr": sr, "source": it["source"],
            "track": track_name, "item_count": len(proj["items"]),
            "bars": rows, "boundaries": bounds, "segments": segs}


def format_profile(p):
    """The human table. Compact enough to eyeball, complete enough to label."""
    out = [f"tempo {p['tempo']}  sr {p['sr']}  track {p['track']}  "
           f"bars {len(p['bars'])}"]
    if p["item_count"] > 1:
        out.append(f"WARNING: {p['item_count']} items on track; "
                   "first item only (same rule as riff)")
    hdr = (f"{'bar':>4} {'hits':>4} {'dens':>5} {'ioi':>6} {'decay':>6} "
           f"{'sil':>5} {'rms':>6} {'crest':>5} {'low':>5} {'brt':>5}  grid")
    out.append(hdr)
    for r in p["bars"]:
        ioi = f"{r['ioi_cv']:.2f}" if r["ioi_cv"] is not None else "-"
        dec = f"{r['decay_ratio']:.2f}" if r["decay_ratio"] is not None else "-"
        grid = " ".join(r["grid"][i:i + 4] for i in range(0, len(r["grid"]), 4))
        out.append(f"{r['bar']:>4} {r['n_onsets']:>4} {r['density_hz']:>5.1f} "
                   f"{ioi:>6} {dec:>6} {r['silence_ratio']:>5.2f} "
                   f"{r['rms_db']:>6.1f} {r['crest_db']:>5.1f} "
                   f"{r['low_ratio']:>5.2f} {r['bright_ratio']:>5.2f}  {grid}")
    if p["boundaries"]:
        out.append(f"feel changes at bar(s): "
                   f"{', '.join(str(b) for b in p['boundaries'])}")
    out.append("sections: " + "  ".join(
        f"[{s['label']}] bars {s['start_bar']}-{s['end_bar']}"
        for s in p["segments"]))
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    as_json = "--json" in sys.argv
    argv = [a for a in sys.argv[1:] if a != "--json"]
    rpp, track = argv[0], argv[1]
    bars = int(argv[2]) if len(argv) > 2 else None
    if bars is not None and bars <= 0:
        bars = None                       # reaperd forwards 0 for "whole item"
    start = int(argv[3]) if len(argv) > 3 else 0
    max_s = float(argv[4]) if len(argv) > 4 else None
    # A non-positive cap is rejected by profile_track — passing it through
    # keeps the CLI honest instead of silently profiling the whole item.
    try:
        p = profile_track(rpp, track, bars=bars, start_bar=start,
                          max_seconds=max_s)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(p, indent=2) if as_json else format_profile(p))
