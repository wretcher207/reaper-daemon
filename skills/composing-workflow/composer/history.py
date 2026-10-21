"""Compact song history so new songs don't repeat the last ones.

One JSON line per verified song in songs/history.jsonl (gitignored: songs/ is
the user's data). Lines are capped; recent() reads only the file tail, never
the whole file.

    from composer import history
    print(history.recent())          # before planning: last 10, one line each
    history.append_song(title="...", key="C# minor", bpm=140, ...)  # after verify
"""
import datetime
import json
import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
DEFAULT_PATH = os.path.join(ROOT, "songs", "history.jsonl")
FIELDS = ("date", "title", "key", "bpm", "time_sig", "tuning", "structure",
          "riff", "tricks", "drums")
FIELD_MAX = 120
LINE_MAX = 600


def _clip(v, n=FIELD_MAX):
    if isinstance(v, (list, tuple)):
        v = "; ".join(str(x) for x in v)
    v = " ".join(str(v).split())
    return v if len(v) <= n else v[: n - 1] + "~"


def append_song(path=DEFAULT_PATH, **song):
    unknown = set(song) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown fields {sorted(unknown)}; use {FIELDS}")
    if not song.get("title"):
        raise ValueError("title is required")
    song.setdefault("date", datetime.date.today().isoformat())
    rec = {k: (song[k] if k == "bpm" and isinstance(song[k], (int, float)) else _clip(song[k]))
           for k in FIELDS if song.get(k) not in (None, "")}
    line = json.dumps(rec, ensure_ascii=False)
    while len(line) > LINE_MAX:
        big = max((k for k in rec if isinstance(rec[k], str)), key=lambda k: len(rec[k]))
        rec[big] = _clip(rec[big], max(10, len(rec[big]) - (len(line) - LINE_MAX) - 1))
        line = json.dumps(rec, ensure_ascii=False)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return rec


def _tail_lines(path, n, chunk=4096):
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos, data = f.tell(), b""
        while pos > 0 and data.count(b"\n") <= n:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    return [l for l in data.decode("utf-8", "replace").splitlines() if l.strip()][-n:]


def recent(n=10, path=DEFAULT_PATH):
    """Last n songs as compact text, one line each (empty string if none)."""
    if not os.path.exists(path):
        return ""
    out = []
    for l in _tail_lines(path, n):
        try:
            r = json.loads(l)
        except ValueError:
            continue
        out.append(" | ".join(f"{r[k]}" if k in ("date", "title") else f"{k}={r[k]}"
                              for k in FIELDS if k in r))
    return "\n".join(out)
