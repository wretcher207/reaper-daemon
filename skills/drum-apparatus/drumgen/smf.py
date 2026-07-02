import struct


def _vlq(n):
    b = [n & 0x7F]; n >>= 7
    while n:
        b.insert(0, (n & 0x7F) | 0x80); n >>= 7
    return bytes(b)


def write_smf(events, ppq=480, tempo=120):
    if tempo <= 0:
        raise ValueError(f"tempo must be positive, got {tempo}")
    us_per_qn = int(round(60_000_000 / tempo))
    if us_per_qn > 0xFFFFFF:
        # The SMF tempo meta is 3 bytes; below ~3.6 BPM it truncated silently
        # and wrote a wildly wrong tempo.
        raise ValueError(f"tempo {tempo} below the SMF representable range")
    evs = sorted(events, key=lambda e: e["tick"])
    pairs = []  # (tick, kind, pitch, vel); kind 1=on, 0=off
    for e in evs:
        if not 0 <= e["pitch"] <= 127:
            # An out-of-range pitch wrote a corrupt SMF.
            raise ValueError(f"MIDI pitch {e['pitch']} out of range 0-127")
        pairs.append((e["tick"], 1, e["pitch"], e["vel"]))
        pairs.append((e["tick"] + e["dur"], 0, e["pitch"], 0))
    pairs.sort(key=lambda p: (p[0], p[1]))  # offs before ons at same tick

    trk = bytearray()
    trk += _vlq(0) + bytes([0xFF, 0x51, 0x03]) + struct.pack(">I", us_per_qn)[1:]
    trk += _vlq(0) + bytes([0xFF, 0x58, 0x04, 4, 2, 24, 8])
    prev = 0
    for tick, kind, pitch, vel in pairs:
        dt = tick - prev; prev = tick
        status = (0x90 if kind else 0x80)  # channel 0
        trk += _vlq(dt) + bytes([status, pitch, vel])
    trk += _vlq(0) + bytes([0xFF, 0x2F, 0x00])

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, ppq)
    return header + b"MTrk" + struct.pack(">I", len(trk)) + bytes(trk)


def parse_smf(data):
    ppq = struct.unpack(">H", data[12:14])[0]
    i = data.index(b"MTrk"); length = struct.unpack(">I", data[i+4:i+8])[0]
    p = i + 8; end = p + length
    def vlq(p):
        n = 0
        while True:
            b = data[p]; p += 1; n = (n << 7) | (b & 0x7F)
            if not b & 0x80:
                return n, p
    notes = []; t = 0; status = None
    while p < end:
        dt, p = vlq(p); t += dt
        b = data[p]
        if b & 0x80:
            status = b; p += 1
        if status == 0xFF:
            p += 1; l, p = vlq(p); p += l; continue
        pitch = data[p]; vel = data[p+1]; p += 2
        if (status & 0xF0) == 0x90 and vel > 0:
            notes.append({"tick": t, "pitch": pitch, "vel": vel})
    return {"ppq": ppq, "notes": notes}
