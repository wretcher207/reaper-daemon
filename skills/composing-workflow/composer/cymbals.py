"""Cymbal-layer builder: keep the kick/snare groove, rewrite cymbals with
movement per phrase, accents aligned to the guitar, chokes for stops.

Roles are kit-agnostic names; map them to the pitches of the loaded kit from
`discover_drum_map` / skills/drum-apparatus maps (never guess):

    kit = {"hat_tip": 42, "hat_edge": ..., "hat_open": ..., "hat_pedal": ...,
           "ride_tip": ..., "ride_bell": ..., "ride_crash": ..., "crash_l": ...,
           "crash_l_choke": ..., "crash_r": ..., "china_l": ..., "stack": ...,
           "splash": ..., "big_crash": ...}

    hits = []
    hits += pattern("hat_closed", bars=range(0, 4))
    hits += pattern("ride", bars=range(4, 8), pedal=True)
    hits += downbeats([0, 4], ["crash_l", "china_l"])
    hits += accents_from_tokens(guitar_tokens, ["splash", "crash_r"], tags={"pc"})
    hits += choke(7, 12, "crash_r", "crash_r_choke")
    notes = realize(hits, kit, bpm=140, swing=0.6, level=1.0, seed=3)
    # notes -> merge with the groove, then golden_rule(all_notes) before writing
"""
import os
import random
import sys

from . import artic

# Loud by default: crashes/chinas ~108-127. Only stay quiet when it serves the part.
BASEVEL = {"hat_tip": 66, "hat_edge": 90, "hat_open": 92, "hat_pedal": 62,
           "ride_tip": 84, "ride_bell": 104, "ride_crash": 108, "crash_l": 118,
           "crash_r": 116, "big_crash": 122, "china_l": 114, "china_r": 116,
           "stack": 104, "splash": 106, "bell": 100}
CHOKE_VEL = 92
SHORT_ROLES = {"hat_tip", "hat_edge", "hat_pedal"}

MODES = ("hat_closed", "hat_open", "hat_16", "ride", "ride_crash", "china",
         "crash_quarters", "stack_8ths", "bell_quarters")


def pattern(mode, bars, upto=16, pedal=False):
    """-> [(bar, step, role, vel_scale)] for one rotation mode over `bars`.
    `upto` cuts the last bar short (leave room for a fill)."""
    H = []
    bars = list(bars)
    for b in bars:
        u = upto if b == bars[-1] else 16
        if mode == "hat_closed":
            H += [(b, s, "hat_edge" if s % 4 == 0 else "hat_tip", 1.0 if s % 4 == 0 else 0.85) for s in range(0, u, 2)]
        elif mode == "hat_open":
            H += [(b, s, "hat_edge" if s % 4 == 0 else "hat_open", 0.95) for s in range(0, u, 2)]
        elif mode == "hat_16":
            H += [(b, s, "hat_edge" if s % 4 == 0 else "hat_tip", 1.0 if s % 4 == 0 else (0.75 if s % 2 == 0 else 0.6)) for s in range(u)]
        elif mode == "ride":
            H += [(b, s, "ride_bell" if s % 4 == 0 else "ride_tip", 1.0) for s in range(0, u, 2)]
        elif mode == "ride_crash":
            H += [(b, s, "ride_crash", 1.0) for s in range(0, u, 4)]
        elif mode == "china":
            H += [(b, s, "china_l", 0.95) for s in range(0, u, 4)]
            H += [(b, s, "stack", 0.8) for s in range(2, u, 4)]
        elif mode == "crash_quarters":
            H += [(b, s, ("crash_l", "crash_r")[(i + b) % 2], 1.0 if s == 0 else 0.9) for i, s in enumerate(range(0, u, 4))]
        elif mode == "stack_8ths":
            H += [(b, s, "stack", 1.0 if s % 4 == 0 else 0.8) for s in range(0, u, 2)]
        elif mode == "bell_quarters":
            H += [(b, s, "ride_bell", 1.0) for s in range(0, u, 4)]
        else:
            raise ValueError(f"unknown mode {mode!r}; one of {MODES}")
        if pedal:
            H += [(b, s, "hat_pedal", 1.0) for s in (4, 12) if s < u]
    return H


def rotate(modes, bars_per_phrase, nbars, fill_steps=8, pedal_modes=("ride", "ride_crash")):
    """Change cymbal every phrase; the last bar stops at `fill_steps` for a fill."""
    H = []
    for ph, start in enumerate(range(0, nbars, bars_per_phrase)):
        bars = range(start, min(nbars, start + bars_per_phrase))
        last = bars[-1] == nbars - 1
        H += pattern(modes[ph % len(modes)], bars, upto=fill_steps if last else 16,
                     pedal=modes[ph % len(modes)] in pedal_modes)
    return H


def downbeats(bars, roles, scale=1.0):
    """Crash / china / big crash on phrase downbeats, cycling roles."""
    return [(b, 0, roles[i % len(roles)], scale) for i, b in enumerate(bars)]


def accents_from_tokens(tokens, roles, tags=None, skip_bars=(), skip_downbeat=True):
    """Cymbal on each accented ('!') guitar token, optionally only for `tags`
    (e.g. {"pc"} = open power-chord accents), alternating `roles`."""
    notes, _ = artic.parse(tokens)
    H, k = [], 0
    for n in notes:
        if not n["accent"] or (tags and n["tag"] not in tags):
            continue
        b, s = divmod(n["step"], 16)
        if (skip_downbeat and s == 0) or b in skip_bars:
            continue
        H.append((b, s, roles[k % len(roles)], 0.95))
        k += 1
    return H


def choke(bar, step, role, choke_role, gap_steps=2):
    """Crash then its choke note `gap_steps` later (stops/impacts)."""
    return [(bar, step, role, 1.0), (bar, step + gap_steps, choke_role, 1.0)]


def realize(hits, kit, *, bpm, swing=0.5, level=1.0, seed=1, jitter_ms=3.0,
            straight=False, basevel=None):
    """(bar, step, role, scale) hits -> note dicts in beats. Duplicates dropped.
    straight=True for blasts (no swing, ~1 ms jitter)."""
    rng = random.Random(seed)
    ms = bpm / 60000.0
    bv = dict(BASEVEL, **(basevel or {}))
    out, seen = [], set()
    for b, s, role, sc in hits:
        if role not in kit:
            raise KeyError(f"kit has no pitch for role {role!r}")
        key = (b, s, role)
        if key in seen:
            continue
        seen.add(key)
        sw = 0.5 if straight else swing
        t = b * 4 + artic.swing_pos(s, sw)
        t = max(0.0, t + rng.gauss(0, 1.0 if straight else jitter_ms) * ms)
        base = CHOKE_VEL if role.endswith("_choke") else bv.get(role, 100)
        v = base * sc * (0.94 + 0.08 * (level - 0.8) / 0.24) + rng.gauss(0, 4)
        out.append(dict(time=t, duration=0.12 if role in SHORT_ROLES else 0.5,
                        pitch=kit[role], velocity=int(min(127, max(40, round(v))))))
    return out


def golden_rule(notes, ppq=960):
    """Apply drum-apparatus goldenrule.enforce to beat-timed notes in place and
    return the remaining violation count (must be 0 before writing)."""
    here = os.path.dirname(os.path.abspath(__file__))
    dg = os.path.normpath(os.path.join(here, "..", "..", "drum-apparatus"))
    if dg not in sys.path:
        sys.path.insert(0, dg)
    from drumgen import goldenrule
    idx = [dict(index=i, pitch=n["pitch"], ppq=round(n["time"] * ppq), velocity=n["velocity"])
           for i, n in enumerate(notes)]
    vel = goldenrule.enforce(idx, {d["index"]: d["velocity"] for d in idx})
    for i, n in enumerate(notes):
        n["velocity"] = int(vel[i])
    return len(goldenrule.violations(idx, vel))
