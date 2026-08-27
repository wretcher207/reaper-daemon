"""Standard MIDI File writer for guitar/bass performances.

The drum apparatus' smf.py writes note events on channel 0 only. A believable
Shreddage/virtual-guitar performance needs two things it does not have:

  * **Control-change events** — Shreddage 3 morphs sustain <-> palm-mute with the
    mod wheel (CC1). The palm-mute chug that defines modern metal *is* a mod-wheel
    ride, so a notes-only file cannot voice the part. We emit CC events inline.
  * **Explicit channel** — keyswitch notes, played notes, and CC all ride the same
    channel here (Shreddage's default), but the channel is a parameter so a future
    part can split keyswitches onto their own channel.

One track, format 0. Events are dicts:

    {"type": "note", "tick": int, "pitch": 0..127, "vel": 1..127,
     "dur": int>0, "chan": 0..15}
    {"type": "cc",   "tick": int, "cc": 0..127, "val": 0..127, "chan": 0..15}

`dur`, `chan`, and `type` default sensibly (note, chan 0). No running status is
used — every event carries its status byte, which REAPER imports cleanly and
keeps the writer trivial to reason about.
"""
import struct


def _vlq(n):
    if n < 0:
        raise ValueError(f"delta time cannot be negative: {n}")
    b = [n & 0x7F]; n >>= 7
    while n:
        b.insert(0, (n & 0x7F) | 0x80); n >>= 7
    return bytes(b)


def write_smf(events, ppq=480, tempo=120):
    """Serialize events to SMF bytes.

    Note events become an on/off pair; CC events a single message. At one tick,
    ordering is: CC first, then note-offs, then note-ons — so a mod-wheel move
    written for a chug lands before the note it shapes, and a note-off frees a
    pitch before the next note-on on it.
    """
    if tempo <= 0:
        raise ValueError(f"tempo must be positive, got {tempo}")
    us_per_qn = int(round(60_000_000 / tempo))
    if us_per_qn > 0xFFFFFF:
        raise ValueError(f"tempo {tempo} below the SMF representable range")

    # (tick, order, status, d1, d2). order: 0 CC, 1 note-off, 2 note-on.
    msgs = []
    for e in events:
        kind = e.get("type", "note")
        chan = int(e.get("chan", 0))
        if not 0 <= chan <= 15:
            raise ValueError(f"MIDI channel {chan} out of range 0-15")
        tick = int(e["tick"])
        if tick < 0:
            raise ValueError(f"event tick cannot be negative: {tick}")
        if kind == "cc":
            cc = int(e["cc"]); val = int(e["val"])
            if not 0 <= cc <= 127:
                raise ValueError(f"CC number {cc} out of range 0-127")
            if not 0 <= val <= 127:
                raise ValueError(f"CC value {val} out of range 0-127")
            msgs.append((tick, 0, 0xB0 | chan, cc, val))
        elif kind == "note":
            pitch = int(e["pitch"]); vel = int(e["vel"]); dur = int(e["dur"])
            if not 0 <= pitch <= 127:
                raise ValueError(f"MIDI pitch {pitch} out of range 0-127")
            if not 1 <= vel <= 127:
                raise ValueError(f"note velocity {vel} out of range 1-127")
            if dur <= 0:
                raise ValueError(f"note duration must be positive, got {dur}")
            msgs.append((tick, 2, 0x90 | chan, pitch, vel))
            msgs.append((tick + dur, 1, 0x80 | chan, pitch, 0))
        else:
            raise ValueError(f"unknown event type: {kind!r}")

    msgs.sort(key=lambda m: (m[0], m[1]))

    trk = bytearray()
    trk += _vlq(0) + bytes([0xFF, 0x51, 0x03]) + struct.pack(">I", us_per_qn)[1:]
    trk += _vlq(0) + bytes([0xFF, 0x58, 0x04, 4, 2, 24, 8])
    prev = 0
    for tick, _order, status, d1, d2 in msgs:
        dt = tick - prev; prev = tick
        trk += _vlq(dt) + bytes([status, d1, d2])
    trk += _vlq(0) + bytes([0xFF, 0x2F, 0x00])

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, ppq)
    return header + b"MTrk" + struct.pack(">I", len(trk)) + bytes(trk)


def parse_smf(data):
    """Read notes and CC back out. Used by the tests to prove a round trip."""
    ppq = struct.unpack(">H", data[12:14])[0]
    i = data.index(b"MTrk"); length = struct.unpack(">I", data[i + 4:i + 8])[0]
    p = i + 8; end = p + length

    def vlq(p):
        n = 0
        while True:
            b = data[p]; p += 1; n = (n << 7) | (b & 0x7F)
            if not b & 0x80:
                return n, p

    notes = []; ccs = []; t = 0; status = None
    while p < end:
        dt, p = vlq(p); t += dt
        b = data[p]
        if b & 0x80:
            status = b; p += 1
        hi = status & 0xF0
        if status == 0xFF:
            p += 1; l, p = vlq(p); p += l; continue
        if hi == 0xB0:
            cc = data[p]; val = data[p + 1]; p += 2
            ccs.append({"tick": t, "cc": cc, "val": val, "chan": status & 0x0F})
        elif hi == 0x90:
            pitch = data[p]; vel = data[p + 1]; p += 2
            if vel > 0:
                notes.append({"tick": t, "pitch": pitch, "vel": vel,
                              "chan": status & 0x0F})
        elif hi == 0x80:
            p += 2
        else:  # any other channel message: 2 data bytes
            p += 2
    return {"ppq": ppq, "notes": notes, "ccs": ccs}
