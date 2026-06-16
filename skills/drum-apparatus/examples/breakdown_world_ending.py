#!/usr/bin/env python3
"""Hand-authored 'world ending' breakdown — APPROVED BY DAVID 2026-06-16 (by ear).

NOT produced by the drumgen engine: it uses a cymbal/accent lane + naked kick
chug clusters + deliberate space, none of which render.py supports yet. This
script is the reference for the breakdown vocabulary the engine should learn
(see the planned cymbal/accent lane). Run it to regenerate the .mid.

Feel: spaced stabs (kick+crash), snare smash on cymbal, bare palm-mute kick
chug bursts between accents, big ring-out ender. RS Monarch, channel 1.
"""
import random
from drumgen.smf import write_smf
from drumgen.catalog import load_maps

m = load_maps()["RS Monarch"]
K, S, CH, CR = m["KICK_R"], m["SNARE"], m["CHINA_R"], m["CRASH_R"]
PPQ = 480; STEP = PPQ // 4
rng = random.Random(21)


def hv(b, j=4):
    return max(1, min(127, b + rng.randint(-j, j)))


events = []


def emit(base, hits):
    for step, voices in hits:
        t = base + step * STEP
        for v in voices:
            dur = int((v[2] if len(v) > 2 else 0.12) * PPQ)
            pitch = {"K": K, "S": S, "CH": CH, "CR": CR}[v[0]]
            events.append({"tick": t, "pitch": pitch, "vel": hv(v[1]), "dur": dur})


# MAIN (2 bars): stabs + snare smash carry the cymbal; kick chug clusters are naked.
MAIN = [
    (0,  [("K", 127), ("CR", 127, 0.5)]),
    (8,  [("S", 125), ("CH", 120, 0.5)]),
    (11, [("K", 115)]), (12, [("K", 116)]), (13, [("K", 117)]),
    (16, [("K", 127), ("CR", 127, 0.5)]),
    (24, [("S", 125), ("CH", 120, 0.5)]),
    (27, [("K", 115)]), (28, [("K", 116)]), (29, [("K", 117)]), (30, [("K", 118)]),
]
END = [
    (0,  [("K", 127), ("CR", 127, 1.0), ("CH", 122, 1.0)]),
    (8,  [("S", 126), ("CR", 126, 0.5)]),
    (16, [("K", 127), ("CR", 127, 0.5)]),
    (18, [("K", 116)]), (20, [("K", 117)]), (22, [("K", 118)]),
    (24, [("K", 127), ("CR", 127, 4.0), ("CH", 124, 4.0)]),  # world-ender, rings out
]
P = 2 * 4 * PPQ
emit(0 * P, MAIN); emit(1 * P, MAIN); emit(2 * P, MAIN); emit(3 * P, END)

if __name__ == "__main__":
    out = "/tmp/breakdown_world_ending.mid"
    open(out, "wb").write(write_smf(events, ppq=PPQ, tempo=107))
    print(f"wrote {out}: {len(events)} notes, 8 bars, RS Monarch")
